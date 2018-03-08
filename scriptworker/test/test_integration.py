#!/usr/bin/env python
# coding=utf-8
"""Scriptworker integration tests.
"""
import aiohttp
import arrow
import asyncio
from contextlib import contextmanager
import json
import logging
import os
import pytest
import re
import slugid
import sys
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
from taskcluster.async import Index, Queue
from . import event_loop, integration_create_task_payload

assert event_loop  # silence pyflakes
log = logging.getLogger(__name__)

# constants helpers and fixtures {{{1
TIMEOUT_SCRIPT = os.path.join(os.path.dirname(__file__), "data", "long_running.py")
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
    randstring = slugid.nice()[0:6].decode('utf-8')
    config = get_unfrozen_copy(DEFAULT_CONFIG)
    GPG_HOME = os.path.join(os.path.basename(__file__), "data", "gpg")
    config.update({
        'log_dir': os.path.join(basedir, "log"),
        'artifact_dir': os.path.join(basedir, "artifact"),
        'task_log_dir': os.path.join(basedir, "artifact", "public", "logs"),
        'work_dir': os.path.join(basedir, "work"),
        "worker_type": "dummy-worker-{}".format(randstring),
        "worker_id": "dummy-worker-{}".format(randstring),
        'artifact_upload_timeout': 60 * 2,
        'gpg_home': GPG_HOME,
        "gpg_encoding": 'utf-8',
        "gpg_options": None,
        "gpg_path": os.environ.get("GPG_PATH", None),
        "gpg_public_keyring": os.path.join(GPG_HOME, "pubring.gpg"),
        "gpg_secret_keyring": os.path.join(GPG_HOME, "secring.gpg"),
        "gpg_use_agent": None,
        'reclaim_interval': 5,
        'task_script': ('bash', '-c', '>&2 echo bar && echo foo && sleep 9 && exit 1'),
        'task_max_timeout': 60,
        'cot_product': 'firefox'
    })
    creds = read_integration_creds()
    del(config['credentials'])
    if isinstance(override, dict):
        config.update(override)
    with open(os.path.join(basedir, "config.json"), "w") as fh:
        json.dump(config, fh, indent=2, sort_keys=True)
    config = apply_product_config(config)
    return config, creds


@contextmanager
def get_context(config_override):
    context = Context()
    with tempfile.TemporaryDirectory() as tmp:
        context.config, credentials = build_config(config_override, basedir=tmp)
        swlog.update_logging_config(context)
        utils.cleanup(context)
        session = aiohttp.ClientSession()
        context.session = session
        context.credentials = credentials
        yield context
        # session.close() - to do this we need to make this function async!!


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


@contextmanager
def get_temp_creds_context(config_override):
    with get_context(config_override) as context:
        get_temp_creds(context)
        yield context


async def create_task(context, task_id, task_group_id):
    payload = integration_create_task_payload(context.config, task_group_id)
    return await context.queue.createTask(task_id, payload)


async def task_status(context, task_id):
    return await context.queue.status(task_id)


@contextmanager
def remember_cwd():
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
def test_run_successful_task(event_loop, context_function):
    task_id = slugid.nice().decode('utf-8')
    task_group_id = slugid.nice().decode('utf-8')
    with context_function(None) as context:
        result = event_loop.run_until_complete(
            create_task(context, task_id, task_group_id)
        )
        assert result['status']['state'] == 'pending'
        with remember_cwd():
            os.chdir(os.path.dirname(context.config['work_dir']))
            status = event_loop.run_until_complete(
                worker.run_tasks(context, creds_key="integration_credentials")
            )
        assert status == 1
        result = event_loop.run_until_complete(
            task_status(context, task_id)
        )
        assert result['status']['state'] == 'failed'


# run_maxtimeout {{{1
@pytest.mark.skipif(os.environ.get("NO_TESTS_OVER_WIRE"), reason=SKIP_REASON)
@pytest.mark.parametrize("context_function", [get_context, get_temp_creds_context])
def test_run_maxtimeout(event_loop, context_function):
    task_id = slugid.nice().decode('utf-8')
    task_group_id = slugid.nice().decode('utf-8')
    partial_config = {
        'task_max_timeout': 2,
    }
    with context_function(partial_config) as context:
        result = event_loop.run_until_complete(
            create_task(context, task_id, task_group_id)
        )
        assert result['status']['state'] == 'pending'
        with remember_cwd():
            os.chdir(os.path.dirname(context.config['work_dir']))
            with pytest.raises(RuntimeError):
                # Because we're using asyncio to kill tasks in the loop,
                # we're going to hit a RuntimeError on [non-osx?] python3.5
                event_loop.run_until_complete(
                    worker.run_tasks(context, creds_key="integration_credentials")
                )
                if sys.version_info >= (3, 6):
                    raise RuntimeError(
                        "Force RuntimeError on 3.6+ for "
                        "https://github.com/mozilla-releng/scriptworker/issues/135"
                    )
        result = event_loop.run_until_complete(task_status(context, task_id))
        # TODO We need to be able to ensure this is 'failed'.
        assert result['status']['state'] in ('failed', 'running')


# empty_queue {{{1
@pytest.mark.skipif(os.environ.get("NO_TESTS_OVER_WIRE"), reason=SKIP_REASON)
@pytest.mark.parametrize("context_function", [get_context, get_temp_creds_context])
def test_empty_queue(event_loop, context_function):
    with context_function(None) as context:
        with remember_cwd():
            os.chdir(os.path.dirname(context.config['work_dir']))
            status = event_loop.run_until_complete(
                worker.run_tasks(context, creds_key="integration_credentials")
            )
        assert status is None


# temp_creds {{{1
@pytest.mark.skipif(os.environ.get("NO_TESTS_OVER_WIRE"), reason=SKIP_REASON)
@pytest.mark.parametrize("context_function", [get_context, get_temp_creds_context])
def test_temp_creds(event_loop, context_function):
    with context_function(None) as context:
        with remember_cwd():
            os.chdir(os.path.dirname(context.config['work_dir']))
            context.temp_credentials = utils.create_temp_creds(
                context.credentials['clientId'], context.credentials['accessToken'],
                expires=arrow.utcnow().replace(minutes=10).datetime
            )
            result = event_loop.run_until_complete(context.temp_queue.ping())
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
    "name": "mozilla-beta linux64 en-US repackage signing",
    "index": "gecko.v2.mozilla-beta.nightly.latest.firefox.linux64-nightly-repackage-signing",
    "task_type": "signing",
}, {
    "name": "mozilla-release linux64 en-US repackage signing",
    "index": "gecko.v2.mozilla-release.nightly.latest.firefox.linux64-nightly-repackage-signing",
    "task_type": "signing",
}))
@pytest.mark.asyncio
async def test_verify_production_cot(branch_context):
    index = Index()
    queue = Queue()

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
        with get_context({'verify_cot_signature': False}) as context:
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
    task_group_id = task_id = slugid.nice().decode('utf-8')
    override = {
        'task_script': (
            'bash', '-c',
            '>&2 echo'
        ),
    }
    with context_function(override) as context:
        result = await create_task(context, task_id, task_group_id)
        assert result['status']['state'] == 'pending'
        path = os.path.join(context.config['artifact_dir'], 'SampleArtifacts/_/X.txt')
        utils.makedirs(os.path.dirname(path))
        with open(path, "w") as fh:
            fh.write("bar")
        with remember_cwd():
            os.chdir(os.path.dirname(context.config['work_dir']))
            status = await worker.run_tasks(context, creds_key="integration_credentials")
        assert status == 0
        result = await task_status(context, task_id)
        assert result['status']['state'] == 'completed'
        url = artifacts.get_artifact_url(context, task_id, 'SampleArtifacts/_/X.txt')
        path2 = os.path.join(context.config['work_dir'], 'downloaded_file')
        await utils.download_file(context, url, path2)
        with open(path2, "r") as fh:
            contents = fh.read().strip()
        assert contents == 'bar'
