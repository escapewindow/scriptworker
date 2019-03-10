#!/usr/bin/env python
# coding=utf-8
"""Scriptworker integration tests.
"""
import aiohttp
import arrow
import asyncio
from asyncio_extras.contextmanager import async_contextmanager
import json
import logging
import os
import pytest
import re

from scriptworker.exceptions import Download404
import slugid
import tempfile
from scriptworker.config import (
    CREDS_FILES,
    apply_product_config,
    get_unfrozen_copy,
    read_worker_creds,
)
from scriptworker.constants import DEFAULT_CONFIG
from scriptworker.context import Context
from scriptworker.cot.verify import ChainOfTrust, verify_chain_of_trust
import scriptworker.log as swlog
import scriptworker.artifacts as artifacts
import scriptworker.worker as worker
import scriptworker.utils as utils
from taskcluster.aio import Index, Queue
from . import integration_create_task_payload

log = logging.getLogger(__name__)

# constants helpers and fixtures {{{1
SKIP_REASON = "NO_TESTS_OVER_WIRE: skipping integration test"


def read_integration_creds():
    creds = read_worker_creds(key="integration_credentials")
    if creds:
        return creds
    raise Exception(
        """To run integration tests, put your worker-test clientId creds, in json format,
in one of these files:

    {files}

with the format

    {{
        "integration_credentials": {{
            "accessToken": "...", "clientId": "...", "certificate": "..."
        }}
    }}

(only specify "certificate" if using temporary credentials)

This clientId will need the scope assume:project:taskcluster:worker-test-scopes

To skip integration tests, set the environment variable NO_TESTS_OVER_WIRE""".format(files=CREDS_FILES)
    )


def build_config(override, basedir):
    randstring = slugid.nice()[0:6]
    config = get_unfrozen_copy(DEFAULT_CONFIG)
    ED25519_DIR = os.path.join(os.path.dirname(__file__), "data", "ed25519")
    config.update({
        'log_dir': os.path.join(basedir, "log"),
        'artifact_dir': os.path.join(basedir, "artifact"),
        'task_log_dir': os.path.join(basedir, "artifact", "public", "logs"),
        'work_dir': os.path.join(basedir, "work"),
        "worker_type": "dummy-worker-{}".format(randstring),
        "worker_id": "dummy-worker-{}".format(randstring),
        'artifact_upload_timeout': 60 * 2,
        'poll_interval': 5,
        'reclaim_interval': 5,
        'task_script': ('bash', '-c', '>&2 echo bar && echo foo && sleep 9 && exit 1'),
        'task_max_timeout': 60,
        'cot_product': 'firefox',
        'ed25519_private_key_path': os.path.join(ED25519_DIR, 'scriptworker_private_key'),
        'ed25519_public_keys': {
            'docker-worker': ['8dBv4bbnZ3RsDzQiPKTJ18uo3hq5Rjm94JG6HXzAcBM='],
            'generic-worker': ['PkI5NslA78wSsYaKNzKq7iD7MLQy7W6wYO/0WFd4tWM='],
            'scriptworker': ['KxYrV3XAJ3uOyAUX0Wcl1Oeu6GSMrI/5hOn39q8Lf0I='],
        },
    })
    creds = read_integration_creds()
    del(config['credentials'])
    if isinstance(override, dict):
        config.update(override)
    with open(os.path.join(basedir, "config.json"), "w") as fh:
        json.dump(config, fh, indent=2, sort_keys=True)
    config = apply_product_config(config)
    return config, creds


@async_contextmanager
async def get_context(config_override=None):
    context = Context()
    with tempfile.TemporaryDirectory() as tmp:
        context.config, credentials = build_config(config_override, basedir=tmp)
        swlog.update_logging_config(context)
        utils.cleanup(context)
        async with aiohttp.ClientSession() as session:
            context.session = session
            context.credentials = credentials
            yield context


def get_temp_creds(context):
    if 'certificate' in context.credentials:
        return
    temp_creds = utils.create_temp_creds(
        context.credentials['clientId'],
        context.credentials['accessToken'],
        expires=arrow.utcnow().replace(minutes=10).datetime,
    )
    if temp_creds:
        context.credentials = temp_creds
        print("Using temp creds!")
    else:
        raise Exception("Can't get temp_creds!")


@async_contextmanager
async def get_temp_creds_context(config_override=None):
    async with get_context(config_override) as context:
        get_temp_creds(context)
        yield context


async def create_task(context, task_id, task_group_id):
    payload = integration_create_task_payload(context.config, task_group_id)
    return await context.queue.createTask(task_id, payload)


async def task_status(context, task_id):
    return await context.queue.status(task_id)


@async_contextmanager
async def remember_cwd():
    """http://stackoverflow.com/a/170174
    """
    curdir = os.getcwd()
    try:
        yield
    finally:
        os.chdir(curdir)


# run_successful_task {{{1
@pytest.mark.skipif(os.environ.get("NO_TESTS_OVER_WIRE"), reason=SKIP_REASON)
@pytest.mark.parametrize("context_function", [get_context, get_temp_creds_context])
@pytest.mark.asyncio
async def test_run_successful_task(context_function):
    task_id = slugid.nice()
    task_group_id = slugid.nice()
    async with context_function(None) as context:
        result = await create_task(context, task_id, task_group_id)
        assert result['status']['state'] == 'pending'
        async with remember_cwd():
            os.chdir(os.path.dirname(context.config['work_dir']))
            status = await worker.run_tasks(context)
        assert status == 1
        result = await task_status(context, task_id)
        assert result['status']['state'] == 'failed'


# run_maxtimeout {{{1
@pytest.mark.skipif(os.environ.get("NO_TESTS_OVER_WIRE"), reason=SKIP_REASON)
@pytest.mark.parametrize("context_function", [get_context, get_temp_creds_context])
@pytest.mark.asyncio
async def test_run_maxtimeout(context_function):
    task_id = slugid.nice()
    partial_config = {
        'task_max_timeout': 2,
        'task_script': ('bash', '-c', '>&2 echo bar && echo foo && sleep 30 && exit 1'),
    }
    async with context_function(partial_config) as context:
        result = await create_task(context, task_id, task_id)
        assert result['status']['state'] == 'pending'
        async with remember_cwd():
            os.chdir(os.path.dirname(context.config['work_dir']))
            status = await worker.run_tasks(context)
            assert status == context.config['task_max_timeout_status']


# cancel task {{{1
async def do_cancel(context, task_id):
    count = 0
    while True:
        await asyncio.sleep(1)
        count += 1
        assert count < 30, "do_cancel Timeout!"
        if not context.task or not context.proc:
            continue
        await context.queue.cancelTask(task_id)
        break


async def run_task_until_stopped(context):
    async with remember_cwd():
        os.chdir(os.path.dirname(context.config['work_dir']))
        status = await worker.run_tasks(context)
        return status


@pytest.mark.skipif(os.environ.get("NO_TESTS_OVER_WIRE"), reason=SKIP_REASON)
@pytest.mark.asyncio
async def test_cancel_task():
    task_id = slugid.nice()
    partial_config = {
        'invalid_reclaim_status': 19,
        'task_script': ('bash', '-c', '>&2 echo bar && echo foo && sleep 30 && exit 1'),
    }
    # Don't use temporary credentials from claimTask, since they don't allow us
    # to cancel the created task.
    async with get_context(partial_config) as context:
        result = await create_task(context, task_id, task_id)
        assert result['status']['state'] == 'pending'
        cancel_fut = asyncio.ensure_future(do_cancel(context, task_id))
        task_fut = asyncio.ensure_future(run_task_until_stopped(context))
        await utils.raise_future_exceptions([cancel_fut, task_fut])
        status = await context.queue.status(task_id)
        assert len(status['status']['runs']) == 1
        assert status['status']['state'] == 'exception'
        assert status['status']['runs'][0]['reasonResolved'] == 'canceled'
        log_url = context.queue.buildUrl(
            'getLatestArtifact', task_id, 'public/logs/live_backing.log'
        )
        log_path = os.path.join(context.config['work_dir'], 'log')
        await utils.download_file(context, log_url, log_path)
        with open(log_path) as fh:
            contents = fh.read()
        assert contents.rstrip() == "bar\nfoo\nAutomation Error: python exited with signal -15"


# cancel task {{{1
async def do_shutdown(context):
    count = 0
    while True:
        await asyncio.sleep(1)
        count += 1
        assert count < 30, "do_shutdown Timeout!"
        if not context.running_tasks or not context.task or not context.proc:
            continue
        await context.running_tasks.cancel()
        break


@pytest.mark.skipif(os.environ.get("NO_TESTS_OVER_WIRE"), reason=SKIP_REASON)
@pytest.mark.asyncio
async def test_shutdown():
    task_id = slugid.nice()
    partial_config = {
        'task_script': ('bash', '-c', '>&2 echo running task script && sleep 30 && exit 1'),
    }
    # Don't use temporary credentials from claimTask, since they don't allow us
    # to cancel the created task.
    async with get_context(partial_config) as context:
        result = await create_task(context, task_id, task_id)
        assert result['status']['state'] == 'pending'
        fake_cot_log = os.path.join(context.config['artifact_dir'], 'public', 'logs', 'chain_of_trust.log')
        fake_other_artifact = os.path.join(context.config['artifact_dir'], 'public', 'artifact.apk')

        with open(fake_cot_log, 'w') as file:
            file.write('CoT logs')
        with open(fake_other_artifact, 'w') as file:
            file.write('unrelated artifact')
        cancel_fut = asyncio.ensure_future(do_shutdown(context))
        task_fut = asyncio.ensure_future(run_task_until_stopped(context))
        await utils.raise_future_exceptions([cancel_fut, task_fut])
        status = await context.queue.status(task_id)
        assert len(status['status']['runs']) == 2  # Taskcluster should create a replacement task
        assert status['status']['runs'][0]['state'] == 'exception'
        assert status['status']['runs'][0]['reasonResolved'] == 'worker-shutdown'
        log_url = context.queue.buildUrl(
            'getArtifact', task_id, 0, 'public/logs/live_backing.log'
        )
        cot_log_url = context.queue.buildUrl(
            'getArtifact', task_id, 0, 'public/logs/chain_of_trust.log'
        )
        other_artifact_url = context.queue.buildUrl(
            'getArtifact', task_id, 0, 'public/artifact.apk'
        )
        log_path = os.path.join(context.config['work_dir'], 'log')
        cot_log_path = os.path.join(context.config['work_dir'], 'cot_log')
        other_artifact_path = os.path.join(context.config['work_dir'], 'artifact.apk')
        await utils.download_file(context, log_url, log_path)
        await utils.download_file(context, cot_log_url, cot_log_path)
        with pytest.raises(Download404):
            await utils.download_file(context, other_artifact_url, other_artifact_path)

        with open(log_path) as fh:
            contents = fh.read()
        assert contents.rstrip() == "running task script\nAutomation Error: python exited with signal -15"

        with open(cot_log_path) as fh:
            contents = fh.read()
        assert contents.rstrip() == "CoT logs"


# empty_queue {{{1
@pytest.mark.skipif(os.environ.get("NO_TESTS_OVER_WIRE"), reason=SKIP_REASON)
@pytest.mark.parametrize("context_function", [get_context, get_temp_creds_context])
@pytest.mark.asyncio
async def test_empty_queue(context_function):
    async with context_function(None) as context:
        async with remember_cwd():
            os.chdir(os.path.dirname(context.config['work_dir']))
            status = await worker.run_tasks(context)
        assert status is None


# temp_creds {{{1
@pytest.mark.skipif(os.environ.get("NO_TESTS_OVER_WIRE"), reason=SKIP_REASON)
@pytest.mark.parametrize("context_function", [get_context, get_temp_creds_context])
@pytest.mark.asyncio
async def test_temp_creds(context_function):
    async with context_function(None) as context:
        async with remember_cwd():
            os.chdir(os.path.dirname(context.config['work_dir']))
            context.temp_credentials = utils.create_temp_creds(
                context.credentials['clientId'], context.credentials['accessToken'],
                expires=arrow.utcnow().replace(minutes=10).datetime
            )
            result = await context.temp_queue.ping()
            assert result['alive']


# verify_cot {{{1
@pytest.mark.skipif(os.environ.get("NO_TESTS_OVER_WIRE"), reason=SKIP_REASON)
@pytest.mark.parametrize("branch_context", ({
    "name": "mozilla-central nightly desktop",
    "index": "gecko.v2.mozilla-central.latest.firefox.decision-nightly-desktop",
    "task_label_to_task_type": {
        "balrog-.*-nightly/opt": "balrog",
        "beetmover-repackage-.*-nightly/opt": "beetmover",
    },
}, {
    "name": "mozilla-central nightly android",
    "index": "gecko.v2.mozilla-central.latest.firefox.decision-nightly-android",
    "task_label_to_task_type": {
        "push-apk/opt": "pushapk",
    },
}, {
    "name": "mozilla-central win64 en-US repackage signing",
    "index": "gecko.v2.mozilla-central.nightly.latest.firefox.win64-nightly-repackage-signing",
    "task_type": "signing",
}, {
    "name": "mozilla-beta win64 en-US repackage signing",
    "index": "gecko.v2.mozilla-beta.nightly.latest.firefox.win64-nightly-repackage-signing",
    "task_type": "signing",
}, {
    "name": "mozilla-release win64 en-US repackage signing",
    "index": "gecko.v2.mozilla-release.nightly.latest.firefox.win64-nightly-repackage-signing",
    "task_type": "signing",
}, {
    "name": "mozilla-esr60 win64 en-US repackage signing",
    "index": "gecko.v2.mozilla-esr60.nightly.latest.firefox.win64-nightly-repackage-signing",
    "task_type": "signing",
}))
@pytest.mark.asyncio
async def test_verify_production_cot(branch_context):
    index = Index(options={'rootUrl': DEFAULT_CONFIG['taskcluster_root_url']})
    queue = Queue(options={'rootUrl': DEFAULT_CONFIG['taskcluster_root_url']})

    async def get_task_id_from_index(index_path):
        res = await index.findTask(index_path)
        return res['taskId']

    async def get_completed_task_info_from_labels(decision_task_id, label_to_task_type):
        label_to_taskid = await queue.getLatestArtifact(
            decision_task_id, "public/label-to-taskid.json"
        )
        task_info = {}
        for re_label, task_type in label_to_task_type.items():
            r = re.compile(re_label)
            for label, task_id in label_to_taskid.items():
                if r.match(label):
                    status = await queue.status(task_id)
                    # only run verify_cot against tasks with completed deps.
                    if status['status']['state'] in ('completed', 'running', 'pending', 'failed'):
                        task_info[task_id] = task_type
                        break
            else:
                log.warning(
                    "Not running verify_cot against {} {} because there are no elegible completed tasks".format(
                        decision_task_id, task_type
                    )
                )
        return task_info

    async def verify_cot(name, task_id, task_type):
        log.info("Verifying {} {} {}...".format(name, task_id, task_type))
        async with get_context() as context:
            context.task = await queue.task(task_id)
            cot = ChainOfTrust(context, task_type, task_id=task_id)
            await verify_chain_of_trust(cot)

    task_id = await get_task_id_from_index(branch_context['index'])
    assert task_id, "{}: Can't get task_id from index {}!".format(
        branch_context['name'], branch_context['index']
    )
    if branch_context.get('task_label_to_task_type'):
        task_info = await get_completed_task_info_from_labels(
            task_id, branch_context['task_label_to_task_type']
        )
        for task_id, task_type in task_info.items():
            name = "{} {}".format(branch_context['name'], task_type)
            await verify_cot(name, task_id, task_type)
    else:
        await verify_cot(branch_context['name'], task_id, branch_context['task_type'])


# private artifacts {{{1
@pytest.mark.skipif(os.environ.get("NO_TESTS_OVER_WIRE"), reason=SKIP_REASON)
@pytest.mark.parametrize("context_function", [get_context, get_temp_creds_context])
@pytest.mark.asyncio
async def test_private_artifacts(context_function):
    task_group_id = task_id = slugid.nice()
    override = {
        'task_script': (
            'bash', '-c',
            '>&2 echo'
        ),
    }
    async with context_function(override) as context:
        result = await create_task(context, task_id, task_group_id)
        assert result['status']['state'] == 'pending'
        path = os.path.join(context.config['artifact_dir'], 'SampleArtifacts/_/X.txt')
        utils.makedirs(os.path.dirname(path))
        with open(path, "w") as fh:
            fh.write("bar")
        async with remember_cwd():
            os.chdir(os.path.dirname(context.config['work_dir']))
            status = await worker.run_tasks(context)
        assert status == 0
        result = await task_status(context, task_id)
        assert result['status']['state'] == 'completed'
        url = artifacts.get_artifact_url(context, task_id, 'SampleArtifacts/_/X.txt')
        path2 = os.path.join(context.config['work_dir'], 'downloaded_file')
        await utils.download_file(context, url, path2)
        with open(path2, "r") as fh:
            contents = fh.read().strip()
        assert contents == 'bar'
