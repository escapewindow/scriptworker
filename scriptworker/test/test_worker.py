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
import scriptworker.worker as worker
from scriptworker.worker import RunTasks, do_run_task
from . import noop_async, noop_sync, rw_context, successful_queue, \
    tmpdir, TIMEOUT_SCRIPT, create_async, create_slow_async, create_finished_future, create_sync

assert rw_context, tmpdir  # silence flake8
assert successful_queue  # silence flake8


# constants helpers and fixtures {{{1
@pytest.yield_fixture(scope='function')
def context(rw_context):
    rw_context.credentials_timestamp = arrow.utcnow().replace(minutes=-10).timestamp
    yield rw_context


# main {{{1
def test_main(mocker, context, event_loop):
    config = dict(context.config)
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


def test_main_sigterm(mocker, context, event_loop):
    """Test that sending SIGTERM causes the main loop to stop after the next
    call to async_main."""
    run_tasks_cancelled = event_loop.create_future()

    class MockRunTasks:
        @staticmethod
        def cancel():
            run_tasks_cancelled.set_result(True)

    async def async_main(internal_context, _):
        # scriptworker reads context from a file, so we have to modify the context given here instead of the variable
        # from the fixture
        internal_context.running_tasks = MockRunTasks()
        # Send SIGTERM to ourselves so that we stop
        os.kill(os.getpid(), signal.SIGTERM)

    _, tmp = tempfile.mkstemp()
    try:
        with open(tmp, "w") as fh:
            json.dump(context.config, fh)
        mocker.patch.object(worker, 'async_main', new=async_main)
        mocker.patch.object(sys, 'argv', new=['x', tmp])
        worker.main(event_loop=event_loop)
    finally:
        os.remove(tmp)

    event_loop.run_until_complete(run_tasks_cancelled)
    assert run_tasks_cancelled.result()


# async_main {{{1
@pytest.mark.asyncio
async def test_async_main(context, mocker, tmpdir):

    def exit(*args, **kwargs):
        sys.exit()

    try:
        mocker.patch.object(worker, 'run_tasks', new=noop_async)
        mocker.patch.object(asyncio, 'sleep', new=noop_async)
        mocker.patch.object(worker, 'rm', new=noop_sync)
        mocker.patch.object(os, 'rename', new=noop_sync)
        await worker.async_main(context, {})
        await worker.async_main(context, {})
        with pytest.raises(SystemExit):
            await worker.async_main(context, {})
    finally:
        if os.path.exists(path):
            shutil.rmtree(path)


# run_tasks {{{1
@pytest.mark.asyncio
@pytest.mark.parametrize("verify_cot", (True, False))
async def test_mocker_run_tasks(context, successful_queue, verify_cot, mocker):
    task = {"foo": "bar", "credentials": {"a": "b"}, "task": {'task_defn': True}}

    successful_queue.task = task

    async def claim_work(*args, **kwargs):
        return {'tasks': [deepcopy(task)]}

    async def run_task(*args, **kwargs):
        return 19

    fake_cot = mock.MagicMock

    context.config['verify_chain_of_trust'] = verify_cot

    context.queue = successful_queue
    mocker.patch.object(worker, "claim_work", new=claim_work)
    mocker.patch.object(worker, "reclaim_task", new=noop_async)
    mocker.patch.object(worker, "prepare_to_run_task", new=noop_sync)
    mocker.patch.object(worker, "run_task", new=run_task)
    mocker.patch.object(worker, "ChainOfTrust", new=fake_cot)
    mocker.patch.object(worker, "verify_chain_of_trust", new=noop_async)
    mocker.patch.object(worker, "generate_cot", new=noop_sync)
    mocker.patch.object(worker, "upload_artifacts", new=noop_async)
    mocker.patch.object(worker, "complete_task", new=noop_async)
    status = await worker.run_tasks(context)
    assert status == 19


@pytest.mark.asyncio
async def test_mocker_run_tasks_noop(context, successful_queue, mocker):
    context.queue = successful_queue
    mocker.patch.object(worker, "claim_work", new=noop_async)
    mocker.patch.object(worker, "reclaim_task", new=noop_async)
    mocker.patch.object(worker, "prepare_to_run_task", new=noop_sync)
    mocker.patch.object(worker, "run_task", new=noop_async)
    mocker.patch.object(worker, "generate_cot", new=noop_sync)
    mocker.patch.object(worker, "upload_artifacts", new=noop_async)
    mocker.patch.object(worker, "complete_task", new=noop_async)
    mocker.patch.object(asyncio, "sleep", new=noop_async)
    status = await worker.run_tasks(context)
    assert context.credentials is None
    assert status is None


def _mocker_run_tasks_helper(mocker, exc, func_to_raise):
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
    mocker.patch.object(worker, "prepare_to_run_task", new=noop_sync)
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


@pytest.mark.parametrize("func_to_raise,exc,expected", ((
    'run_task', ScriptWorkerException, ScriptWorkerException.exit_code
), (
    'upload_artifacts', ScriptWorkerException, ScriptWorkerException.exit_code
), (
    'upload_artifacts', aiohttp.ClientError, STATUSES['intermittent-task']
)))
@pytest.mark.asyncio
async def test_mocker_run_tasks_caught_exception(context, successful_queue, mocker,
                                                 func_to_raise, exc, expected):
    """Raise an exception within the run_tasks try/excepts and return status.

    """
    _mocker_run_tasks_helper(mocker, exc, func_to_raise)

    context.queue = successful_queue
    status = await worker.run_tasks(context)
    assert status == expected


@pytest.mark.parametrize("func_to_raise,exc", ((
    'run_task', ValueError
), (
    'upload_artifacts', OSError
)))
@pytest.mark.asyncio
async def test_mocker_run_tasks_uncaught_exception(context, successful_queue, mocker,
                                                   func_to_raise, exc):
    """Raise an uncaught exception within the run_tasks try/excepts.

    """
    _mocker_run_tasks_helper(mocker, exc, func_to_raise)

    context.queue = successful_queue
    with pytest.raises(exc):
        await worker.run_tasks(context)


@pytest.mark.asyncio
async def test_run_tasks_timeout(context, successful_queue, mocker):
    temp_dir = os.path.join(context.config['work_dir'], "timeout")
    task = {"foo": "bar", "credentials": {"a": "b"}, "task": {'task_defn': True}}
    context.config['task_script'] = (
        sys.executable, TIMEOUT_SCRIPT, temp_dir
    )
    context.config['task_max_timeout'] = 1
    context.queue = successful_queue

    async def claim_work(*args, **kwargs):
        return {'tasks': [task]}

    mocker.patch.object(worker, "claim_work", new=claim_work)
    mocker.patch.object(worker, "reclaim_task", new=noop_async)
    mocker.patch.object(worker, "generate_cot", new=noop_sync)
    mocker.patch.object(worker, "prepare_to_run_task", new=noop_sync)
    mocker.patch.object(worker, "upload_artifacts", new=noop_async)
    mocker.patch.object(worker, "complete_task", new=noop_async)
    status = await worker.run_tasks(context)
    assert status == context.config['task_max_timeout_status']


_MOCK_CLAIM_WORK_RETURN = {
    'tasks': [{
        # don't need to worry about the contents of each task for these tests
    }]
}

_MOCK_CLAIM_WORK_NONE_RETURN = {
    'tasks': []
}


class MockChainOfTrust:
    def __init__(self, context, cot_job_type):
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


@pytest.mark.asyncio
async def test_run_tasks_no_cancel(context, mocker):
    mocker.patch('scriptworker.worker.claim_work', create_async(_MOCK_CLAIM_WORK_RETURN))
    mocker.patch.object(asyncio, 'sleep', noop_async)
    mocker.patch('scriptworker.worker.prepare_to_run_task', noop_sync)
    mocker.patch('scriptworker.worker.reclaim_task', noop_async)
    mocker.patch('scriptworker.worker.do_run_task', create_async(0))
    mocker.patch('scriptworker.worker.cleanup', noop_sync)
    mocker.patch('scriptworker.worker.filepaths_in_dir', create_sync(['one', 'public/two']))
    mock_complete_task = mocker.patch('scriptworker.worker.complete_task')
    mock_do_upload = mocker.patch('scriptworker.worker.do_upload')
    mock_do_upload.return_value = create_finished_future(0)

    future = asyncio.Future()
    future.set_result(None)
    mock_complete_task.return_value = future

    run_tasks = RunTasks()
    await run_tasks.invoke(context)
    mock_complete_task.assert_called_once_with(mock.ANY, 0)
    mock_do_upload.assert_called_once_with(context, ['one', 'public/two'])


@pytest.mark.asyncio
async def test_run_tasks_cancel_claim_work(context, mocker):
    slow_function_called, slow_function = create_slow_async()
    mocker.patch('scriptworker.worker.claim_work', slow_function)

    mock_sleep = mocker.patch.object(asyncio, 'sleep')
    mock_sleep.return_value = create_finished_future()

    mock_prepare_task = mocker.patch('scriptworker.worker.prepare_to_run_task')
    mock_prepare_task.return_value = create_finished_future()

    mock_do_upload = mocker.patch('scriptworker.worker.do_upload')
    mock_do_upload.return_value = create_finished_future(0)

    mock_complete_task = mocker.patch('scriptworker.worker.complete_task')
    mock_complete_task.return_value = create_finished_future()

    run_tasks = RunTasks()
    run_tasks_future = asyncio.get_event_loop().create_task(run_tasks.invoke(context))
    await slow_function_called
    await run_tasks.cancel()
    await run_tasks_future

    mock_sleep.assert_not_called()
    mock_prepare_task.assert_not_called()
    mock_do_upload.assert_not_called()
    mock_complete_task.assert_not_called()


@pytest.mark.asyncio
async def test_run_tasks_cancel_sleep(context, mocker):
    slow_function_called, slow_function = create_slow_async()
    mocker.patch.object(asyncio, 'sleep', slow_function)

    mocker.patch('scriptworker.worker.claim_work', create_async(_MOCK_CLAIM_WORK_NONE_RETURN))

    mock_prepare_task = mocker.patch('scriptworker.worker.prepare_to_run_task')
    mock_prepare_task.return_value = create_finished_future()

    mock_do_upload = mocker.patch('scriptworker.worker.do_upload')
    mock_do_upload.return_value = create_finished_future(0)

    mock_complete_task = mocker.patch('scriptworker.worker.complete_task')
    mock_complete_task.return_value = create_finished_future()

    run_tasks = RunTasks()
    run_tasks_future = asyncio.get_event_loop().create_task(run_tasks.invoke(context))
    await slow_function_called
    await run_tasks.cancel()
    await run_tasks_future

    mock_prepare_task.assert_not_called()
    mock_do_upload.assert_not_called()
    mock_complete_task.assert_not_called()


@pytest.mark.asyncio
async def test_run_tasks_cancel_cot(context, mocker):
    context.config['verify_chain_of_trust'] = True

    slow_function_called, slow_function = create_slow_async()
    mocker.patch('scriptworker.worker.verify_chain_of_trust', slow_function)

    mock_prepare_task = mocker.patch('scriptworker.worker.prepare_to_run_task')
    mock_prepare_task.return_value = create_finished_future()

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
    run_tasks_future = asyncio.get_event_loop().create_task(run_tasks.invoke(context))
    await slow_function_called
    await run_tasks.cancel()
    await run_tasks_future

    mock_prepare_task.assert_called_once()
    mock_complete_task.assert_called_once_with(mock.ANY, STATUSES['worker-shutdown'])
    mock_do_upload.assert_called_once_with(context, ['public/logs/chain_of_trust.log', 'public/logs/live_backing.log'])


@pytest.mark.asyncio
async def test_run_tasks_cancel_run_tasks(context, mocker):
    mock_prepare_task = mocker.patch('scriptworker.worker.prepare_to_run_task')
    mock_prepare_task.return_value = create_finished_future()

    mocker.patch('scriptworker.worker.claim_work', create_async(_MOCK_CLAIM_WORK_RETURN))
    mocker.patch('scriptworker.worker.reclaim_task', noop_async)
    mocker.patch('scriptworker.worker.run_task', noop_async)
    mocker.patch('scriptworker.worker.generate_cot', noop_sync)
    mocker.patch('scriptworker.worker.cleanup', noop_sync)
    mocker.patch('os.path.isfile', create_sync(True))

    mock_do_upload = mocker.patch('scriptworker.worker.do_upload')
    mock_do_upload.return_value = create_finished_future(0)

    mock_complete_task = mocker.patch('scriptworker.worker.complete_task')
    mock_complete_task.return_value = create_finished_future()

    task_process = MockTaskProcess()
    run_task_called = asyncio.Future()

    async def mock_run_task(_, to_cancellable_process):
        await to_cancellable_process(task_process)
        run_task_called.set_result(None)
        await task_process._wait()
        raise WorkerShutdownDuringTask
    mocker.patch('scriptworker.worker.run_task', mock_run_task)

    run_tasks = RunTasks()
    run_tasks_future = asyncio.get_event_loop().create_task(run_tasks.invoke(context))
    await run_task_called
    await run_tasks.cancel()
    await run_tasks_future

    assert task_process.stopped_due_to_worker_shutdown
    mock_prepare_task.assert_called_once()
    mock_complete_task.assert_called_once_with(mock.ANY, STATUSES['worker-shutdown'])
    mock_do_upload.assert_called_once_with(context, ['public/logs/chain_of_trust.log', 'public/logs/live_backing.log'])


@pytest.mark.asyncio
async def test_run_tasks_cancel_right_before_cot(context, mocker):
    context.config['verify_chain_of_trust'] = True

    mock_prepare_task = mocker.patch('scriptworker.worker.prepare_to_run_task')
    mock_prepare_task.return_value = create_finished_future()

    mock_run_task = mocker.patch('scriptworker.worker.run_task')
    mock_run_task.return_value = create_finished_future()

    mock_do_upload = mocker.patch('scriptworker.worker.do_upload')
    mock_do_upload.return_value = create_finished_future(0)

    mock_complete_task = mocker.patch('scriptworker.worker.complete_task')
    mock_complete_task.return_value = create_finished_future()

    verify_cot_future = asyncio.Future()
    mock_verify_chain_of_trust = mocker.patch('scriptworker.worker.verify_chain_of_trust')
    mock_verify_chain_of_trust.return_value = verify_cot_future

    mocker.patch('scriptworker.worker.claim_work', create_async(_MOCK_CLAIM_WORK_RETURN))
    mocker.patch('scriptworker.worker.ChainOfTrust', MockChainOfTrust)
    mocker.patch('scriptworker.worker.reclaim_task', noop_async)
    mocker.patch('scriptworker.worker.generate_cot', noop_sync)
    mocker.patch('scriptworker.worker.cleanup', noop_sync)

    run_tasks = RunTasks()

    async def mock_do_run_task(*args, **kwargs):
        # Mock out do_run_task so we can cancel _right_ before cot happens
        # still call the underlying do_run_task function so we see how it behaves
        await run_tasks.cancel()
        return await do_run_task(*args, **kwargs)

    mocker.patch('scriptworker.worker.do_run_task', mock_do_run_task)
    await run_tasks.invoke(context)

    assert verify_cot_future.cancelled()
    mock_run_task.assert_not_called()
    mock_prepare_task.assert_called_once()
    mock_complete_task.assert_called_once_with(mock.ANY, STATUSES['worker-shutdown'])
    mock_do_upload.assert_called_once_with(context, [])


@pytest.mark.asyncio
async def test_run_tasks_cancel_right_before_proc_created(context, mocker):
    mock_prepare_task = mocker.patch('scriptworker.worker.prepare_to_run_task')
    mock_prepare_task.return_value = create_finished_future()

    mock_do_upload = mocker.patch('scriptworker.worker.do_upload')
    mock_do_upload.return_value = create_finished_future(0)

    mock_complete_task = mocker.patch('scriptworker.worker.complete_task')
    mock_complete_task.return_value = create_finished_future()

    mock_verify_chain_of_trust = mocker.patch('scriptworker.worker.verify_chain_of_trust')
    mock_verify_chain_of_trust.return_value = create_finished_future()

    mocker.patch('scriptworker.worker.claim_work', create_async(_MOCK_CLAIM_WORK_RETURN))
    mocker.patch('scriptworker.worker.ChainOfTrust', MockChainOfTrust)
    mocker.patch('scriptworker.worker.reclaim_task', noop_async)
    mocker.patch('scriptworker.worker.generate_cot', noop_sync)
    mocker.patch('scriptworker.worker.cleanup', noop_sync)
    mocker.patch('os.path.isfile', create_sync(True))

    run_tasks = RunTasks()

    async def mock_do_run_task(_, __, to_cancellable_process):
        await run_tasks.cancel()
        task_process = MockTaskProcess()
        await to_cancellable_process(task_process)
        assert task_process.stopped_due_to_worker_shutdown
        raise WorkerShutdownDuringTask

    mocker.patch('scriptworker.worker.do_run_task', mock_do_run_task)
    await run_tasks.invoke(context)

    mock_prepare_task.assert_called_once()
    mock_complete_task.assert_called_once_with(mock.ANY, STATUSES['worker-shutdown'])
    mock_do_upload.assert_called_once_with(context, ['public/logs/chain_of_trust.log', 'public/logs/live_backing.log'])


@pytest.mark.asyncio
async def test_run_tasks_cancel_right_before_claim_work(context, mocker):
    claim_work_called = False

    async def mock_claim_work(_):
        nonlocal claim_work_called
        claim_work_called = True

    mocker.patch('scriptworker.worker.claim_work', mock_claim_work)

    mock_prepare_task = mocker.patch('scriptworker.worker.prepare_to_run_task')
    mock_prepare_task.return_value = create_finished_future()

    mock_do_upload = mocker.patch('scriptworker.worker.do_upload')
    mock_do_upload.return_value = create_finished_future(0)

    mock_complete_task = mocker.patch('scriptworker.worker.complete_task')
    mock_complete_task.return_value = create_finished_future()

    run_tasks = RunTasks()
    await run_tasks.cancel()
    await run_tasks.invoke(context)

    assert not claim_work_called
    mock_prepare_task.assert_not_called()
    mock_do_upload.assert_not_called()
    mock_complete_task.assert_not_called()
