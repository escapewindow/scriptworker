#!/usr/bin/env python
# coding=utf-8
"""Test scriptworker.worker
"""
import aiohttp
import arrow
import asyncio
from copy import deepcopy
import json
import mock
import os
import pytest
import tempfile
import shutil
import sys
import signal

from scriptworker.constants import STATUSES
from scriptworker.exceptions import ScriptWorkerException, WorkerShutdownDuringTask
from scriptworker.utils import makedirs
import scriptworker.worker as worker
from scriptworker.worker import RunTasks, do_run_task
from . import (
    noop_async, noop_sync, task_context, worker_context,
    successful_queue, TIMEOUT_SCRIPT, create_async, create_slow_async,
    create_finished_future, create_sync,
)

assert task_context  # silence flake8
assert successful_queue  # silence flake8


# constants helpers and fixtures {{{1
_MOCK_CLAIM_WORK_RETURN = {
    'tasks': [{
        # don't need to worry about the contents of each task for these tests
    }]
}

_MOCK_CLAIM_WORK_NONE_RETURN = {
    'tasks': []
}


class MockChainOfTrust:
    def __init__(self, task_context, cot_job_type):
        pass


class MockTaskProcess:
    def __init__(self):
        self.stopped_due_to_worker_shutdown = False
        self.worker_stop_future = asyncio.Future()

    async def worker_shutdown_stop(self):
        self.stopped_due_to_worker_shutdown = True
        self.worker_stop_future.set_result(None)

    async def _wait(self):
        await self.worker_stop_future


def _mocker_run_tasks_helper(mocker, exc, func_to_raise, task_context):
    """Mock run_tasks for the test_mocker_run_tasks_* tests.

    """
    task = {"foo": "bar", "credentials": {"a": "b"}, "task": {'task_defn': True}}

    async def claim_work(*args, **kwargs):
        return {'tasks': [task]}

    async def fail(*args, **kwargs):
        raise exc("foo")

    async def run_task(*args, **kwargs):
        return 0

    mocker.patch.object(worker, "claim_work", new=claim_work)
    mocker.patch.object(worker, "reclaim_task", new=noop_async)
    mocker.patch.object(worker, "prepare_to_run_task", return_value=task_context)
    if func_to_raise == "run_task":
        mocker.patch.object(worker, "run_task", new=fail)
    else:
        mocker.patch.object(worker, "run_task", new=run_task)
    mocker.patch.object(worker, "generate_cot", new=noop_sync)
    if func_to_raise == "upload_artifacts":
        mocker.patch.object(worker, "upload_artifacts", new=fail)
    else:
        mocker.patch.object(worker, "upload_artifacts", new=noop_async)
    mocker.patch.object(worker, "complete_task", new=noop_async)


# main {{{1
def test_main(mocker, task_context, event_loop):
    config = dict(task_context.config)
    config['poll_interval'] = 1
    creds = {'fake_creds': True}
    config['credentials'] = deepcopy(creds)

    async def foo(arg, credentials):
        # arg.credentials will be a dict copy of a frozendict.
        assert credentials == dict(creds)
        raise ScriptWorkerException("foo")

    try:
        _, tmp = tempfile.mkstemp()
        with open(tmp, "w") as fh:
            json.dump(config, fh)
        del(config['credentials'])
        mocker.patch.object(worker, 'async_main', new=foo)
        mocker.patch.object(sys, 'argv', new=['x', tmp])
        with pytest.raises(ScriptWorkerException):
            worker.main(event_loop=event_loop)
    finally:
        os.remove(tmp)


@pytest.mark.parametrize('running', (True, False))
def test_main_running_sigterm(mocker, task_context, event_loop, running):
    """Test that sending SIGTERM causes the main loop to stop after the next
    call to async_main."""
    run_tasks_cancelled = event_loop.create_future()

    class MockRunTasks:
        @staticmethod
        def cancel():
            run_tasks_cancelled.set_result(True)

    async def async_main(internal_task_context, _):
        # scriptworker reads task_context from a file, so we have to modify the task_context given here instead of the variable
        # from the fixture
        if running:
            internal_task_context.running_tasks = MockRunTasks()
        # Send SIGTERM to ourselves so that we stop
        os.kill(os.getpid(), signal.SIGTERM)

    _, tmp = tempfile.mkstemp()
    try:
        with open(tmp, "w") as fh:
            json.dump(task_context.config, fh)
        mocker.patch.object(worker, 'async_main', new=async_main)
        mocker.patch.object(sys, 'argv', new=['x', tmp])
        worker.main(event_loop=event_loop)
    finally:
        os.remove(tmp)

    if running:
        event_loop.run_until_complete(run_tasks_cancelled)
        assert run_tasks_cancelled.result()


@pytest.mark.parametrize('running', (True, False))
def test_main_running_sigusr1(mocker, task_context, event_loop, running):
    """Test that sending SIGUSR1 causes the main loop to stop after the next
    call to async_main without cancelling the task."""
    run_tasks_cancelled = event_loop.create_future()

    class MockRunTasks:
        @staticmethod
        def cancel():
            run_tasks_cancelled.set_result(True)

    async def async_main(internal_task_context, _):
        # scriptworker reads task_context from a file, so we have to modify the
        # task_context given here instead of the variable from the fixture
        if running:
            internal_task_context.running_tasks = MockRunTasks()
        # Send SIGUSR1 to ourselves so that we stop
        os.kill(os.getpid(), signal.SIGUSR1)

    _, tmp = tempfile.mkstemp()
    try:
        with open(tmp, "w") as fh:
            json.dump(task_context.config, fh)
        mocker.patch.object(worker, 'async_main', new=async_main)
        mocker.patch.object(sys, 'argv', new=['x', tmp])
        worker.main(event_loop=event_loop)
    finally:
        os.remove(tmp)

    assert not run_tasks_cancelled.done()


# async_main {{{1
@pytest.mark.asyncio
async def test_async_main(task_context, mocker):
    mocker.patch.object(worker, 'run_tasks', new=noop_async)
    await worker.async_main(task_context, {})


# run_tasks {{{1
@pytest.mark.asyncio
@pytest.mark.parametrize("verify_cot", (True, False))
async def test_mocker_run_tasks(task_context, successful_queue, verify_cot, mocker):
    task = {"foo": "bar", "credentials": {"a": "b"}, "task": {'task_defn': True}}

    successful_queue.task = task

    async def claim_work(*args, **kwargs):
        return {'tasks': [deepcopy(task)]}

    async def run_task(*args, **kwargs):
        return 19

    fake_cot = mock.MagicMock

    task_context.config['verify_chain_of_trust'] = verify_cot

    task_context.queue = successful_queue
    mocker.patch.object(worker, "claim_work", new=claim_work)
    mocker.patch.object(worker, "reclaim_task", new=noop_async)
    mocker.patch.object(worker, "prepare_to_run_task", return_value=task_context)
    mocker.patch.object(worker, "run_task", new=run_task)
    mocker.patch.object(worker, "ChainOfTrust", new=fake_cot)
    mocker.patch.object(worker, "verify_chain_of_trust", new=noop_async)
    mocker.patch.object(worker, "generate_cot", new=noop_sync)
    mocker.patch.object(worker, "upload_artifacts", new=noop_async)
    mocker.patch.object(worker, "complete_task", new=noop_async)
    status = await worker.run_tasks(task_context)
    assert status == [19]


@pytest.mark.asyncio
async def test_mocker_run_tasks_noop(task_context, successful_queue, mocker):
    task_context.queue = successful_queue
    mocker.patch.object(worker, "claim_work", new=noop_async)
    mocker.patch.object(worker, "reclaim_task", new=noop_async)
    mocker.patch.object(worker, "prepare_to_run_task", new=noop_sync)
    mocker.patch.object(worker, "run_task", new=noop_async)
    mocker.patch.object(worker, "generate_cot", new=noop_sync)
    mocker.patch.object(worker, "upload_artifacts", new=noop_async)
    mocker.patch.object(worker, "complete_task", new=noop_async)
    mocker.patch.object(asyncio, "sleep", new=noop_async)
    status = await worker.run_tasks(task_context)
    assert task_context.credentials is None
    assert status is None



@pytest.mark.parametrize("func_to_raise,exc,expected", ((
    'run_task', ScriptWorkerException, ScriptWorkerException.exit_code
), (
    'upload_artifacts', ScriptWorkerException, ScriptWorkerException.exit_code
), (
    'upload_artifacts', aiohttp.ClientError, STATUSES['intermittent-task']
)))
@pytest.mark.asyncio
async def test_mocker_run_tasks_caught_exception(task_context, successful_queue, mocker,
                                                 func_to_raise, exc, expected):
    """Raise an exception within the run_tasks try/excepts and return status.

    """
    _mocker_run_tasks_helper(mocker, exc, func_to_raise, task_context)

    task_context.queue = successful_queue
    status = await worker.run_tasks(task_context)
    assert status == [expected]


@pytest.mark.asyncio
async def test_mocker_upload_artifacts_uncaught_exception(task_context, successful_queue, mocker):
    """Raise an uncaught exception within the run_tasks try/excepts.

    """
    _mocker_run_tasks_helper(mocker, OSError, 'upload_artifacts', task_context)

    task_context.queue = successful_queue
    with pytest.raises(OSError):
        await worker.run_tasks(task_context)


@pytest.mark.asyncio
async def test_unexpected_exception_catch_and_tell_taskcluster(task_context, successful_queue, mocker):
    calls = {'upload': [], 'complete': []}
    task_context.config['verify_chain_of_trust'] = True

    def fail():
        raise Exception()

    async def run(coroutine):
        await coroutine

    async def fake_do_upload(*args, **kwargs):
        calls['upload'].append((args, kwargs))
        return 0

    async def fake_complete_task(*args, **kwargs):
        calls['complete'].append((args, kwargs))

    mocker.patch.object(worker, "verify_chain_of_trust", new=fail)
    mocker.patch.object(worker, "do_upload", new=fake_do_upload)
    mocker.patch.object(worker, "complete_task", new=fake_complete_task)
    status = await do_run_task(task_context, run)
    assert status == STATUSES['internal-error']
    assert len(calls['upload']) == 1
    assert len(calls['complete']) == 1


@pytest.mark.asyncio
async def test_run_tasks_timeout(worker_context, task_context, successful_queue, mocker):
    temp_dir = os.path.join(task_context.work_dir, "timeout")
    task = {"foo": "bar", "credentials": {"a": "b"}, "task": {'task_defn': True}}
    task_context.config['task_script'] = (
        sys.executable, TIMEOUT_SCRIPT, temp_dir
    )
    task_context.config['task_max_timeout'] = 1
    task_context.queue = successful_queue

    async def claim_work(*args, **kwargs):
        return {'tasks': [task]}

    mocker.patch.object(worker, "claim_work", new=claim_work)
    mocker.patch.object(worker, "reclaim_task", new=noop_async)
    mocker.patch.object(worker, "generate_cot", new=noop_sync)
    mocker.patch.object(worker, "prepare_to_run_task", return_value=task_context)
    mocker.patch.object(worker, "upload_artifacts", new=noop_async)
    mocker.patch.object(worker, "complete_task", new=noop_async)
    status = await worker.run_tasks(task_context)
    assert status == [task_context.config['task_max_timeout_status']]


@pytest.mark.asyncio
async def test_run_tasks_no_cancel(task_context, mocker):
    mocker.patch('scriptworker.worker.claim_work', create_async(_MOCK_CLAIM_WORK_RETURN))
    mocker.patch.object(asyncio, 'sleep', noop_async)
    mocker.patch('scriptworker.worker.prepare_to_run_task', return_value=task_context)
    mocker.patch('scriptworker.worker.reclaim_task', noop_async)
    mocker.patch('scriptworker.worker.do_run_task', create_async(0))
    mocker.patch('scriptworker.worker.cleanup', noop_sync)

    run_tasks = RunTasks()
    await run_tasks.invoke(task_context)


@pytest.mark.asyncio
async def test_run_tasks_cancel_claim_work(task_context, mocker):
    slow_function_called, slow_function = create_slow_async()
    mocker.patch('scriptworker.worker.claim_work', slow_function)

    mock_sleep = mocker.patch.object(asyncio, 'sleep')
    mock_sleep.return_value = create_finished_future()

    mock_prepare_task = mocker.patch('scriptworker.worker.prepare_to_run_task')
    mock_prepare_task.return_value = task_context

    run_tasks = RunTasks()
    run_tasks_future = asyncio.get_event_loop().create_task(run_tasks.invoke(task_context))
    await slow_function_called
    await run_tasks.cancel()
    await run_tasks_future

    mock_sleep.assert_not_called()


@pytest.mark.asyncio
async def test_run_tasks_cancel_sleep(task_context, mocker):
    slow_function_called, slow_function = create_slow_async()
    mocker.patch.object(asyncio, 'sleep', slow_function)

    mocker.patch('scriptworker.worker.claim_work', create_async(_MOCK_CLAIM_WORK_NONE_RETURN))

    mock_prepare_task = mocker.patch('scriptworker.worker.prepare_to_run_task')
    mock_prepare_task.return_value = task_context

    run_tasks = RunTasks()
    run_tasks_future = asyncio.get_event_loop().create_task(run_tasks.invoke(task_context))
    await slow_function_called
    await run_tasks.cancel()
    await run_tasks_future

    mock_prepare_task.assert_not_called()


@pytest.mark.asyncio
async def test_run_tasks_cancel_cot(task_context, mocker):
    task_context.config['verify_chain_of_trust'] = True

    slow_function_called, slow_function = create_slow_async()
    mocker.patch('scriptworker.worker.verify_chain_of_trust', slow_function)

    mock_prepare_task = mocker.patch('scriptworker.worker.prepare_to_run_task')
    mock_prepare_task.return_value = task_context

    mocker.patch('scriptworker.worker.claim_work', create_async(_MOCK_CLAIM_WORK_RETURN))
    mocker.patch('scriptworker.worker.ChainOfTrust', MockChainOfTrust)
    mocker.patch('scriptworker.worker.reclaim_task', noop_async)
    mocker.patch('scriptworker.worker.run_task', noop_async)
    mocker.patch('scriptworker.worker.generate_cot', noop_sync)
    mocker.patch('scriptworker.worker.cleanup', noop_sync)
    mocker.patch('os.path.isfile', create_sync(True))

    mock_do_upload = mocker.patch('scriptworker.worker.do_upload')
    mock_do_upload.return_value = create_finished_future(0)

    mock_complete_task = mocker.patch('scriptworker.worker.complete_task')
    mock_complete_task.return_value = create_finished_future()

    run_tasks = RunTasks()
    run_tasks_future = asyncio.get_event_loop().create_task(run_tasks.invoke(task_context))
    await slow_function_called
    await run_tasks.cancel()
    await run_tasks_future

    mock_prepare_task.assert_called_once()
    mock_complete_task.assert_called_once_with(mock.ANY, STATUSES['worker-shutdown'])
    mock_do_upload.assert_called_once_with(task_context, ['public/logs/chain_of_trust.log', 'public/logs/live_backing.log'])


@pytest.mark.asyncio
async def test_run_tasks_cancel_run_tasks(task_context, mocker):
    task_context.task_process = MockTaskProcess()
    mocker.patch.object(worker, 'claim_work', new=create_async(_MOCK_CLAIM_WORK_RETURN))
    mocker.patch.object(worker, "prepare_to_run_task", return_value=task_context)
    mocker.patch.object(worker, 'reclaim_task', new=noop_async)
    mocker.patch.object(worker, 'run_task', new=noop_async)
    mocker.patch.object(worker, 'cleanup', noop_sync)
    mocker.patch('os.path.isfile', create_sync(True))

    mock_do_upload = mocker.patch('scriptworker.worker.do_upload')
    mock_do_upload.return_value = create_finished_future(0)

    mock_complete_task = mocker.patch('scriptworker.worker.complete_task')
    mock_complete_task.return_value = create_finished_future()

    run_task_called = asyncio.Future()

    async def mock_run_task(task_context):
        run_task_called.set_result(None)
        await task_context.task_process._wait()
        raise WorkerShutdownDuringTask

    mocker.patch.object(worker, 'run_task', new=mock_run_task)

    run_tasks = RunTasks()
    run_tasks_future = asyncio.get_event_loop().create_task(run_tasks.invoke(task_context))
    await run_task_called
    await run_tasks.cancel()
    await run_tasks_future

    assert task_context.task_process.stopped_due_to_worker_shutdown
    mock_complete_task.assert_called_once_with(mock.ANY, STATUSES['worker-shutdown'])
    mock_do_upload.assert_called_once_with(task_context, ['public/logs/chain_of_trust.log', 'public/logs/live_backing.log'])
