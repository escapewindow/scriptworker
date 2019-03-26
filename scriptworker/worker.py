#!/usr/bin/env python
"""Scriptworker worker functions.

Attributes:
    log (logging.Logger): the log object for the module.

"""
import aiohttp
import arrow
import asyncio
import logging
import os
import sys
import signal
import typing

from scriptworker.artifacts import upload_artifacts
from scriptworker.config import get_context_from_cmdln
from scriptworker.constants import STATUSES
from scriptworker.cot.generate import generate_cot
from scriptworker.cot.verify import ChainOfTrust, verify_chain_of_trust
from scriptworker.exceptions import ScriptWorkerException, WorkerShutdownDuringTask
from scriptworker.task import claim_work, complete_task, prepare_to_run_task, \
    reclaim_task, run_task, worst_level
from scriptworker.task_process import TaskProcess
from scriptworker.utils import cleanup, filepaths_in_dir

log = logging.getLogger(__name__)


# do_run_task {{{1
async def do_run_task(context, run_cancellable, to_cancellable_process):
    """Run the task logic.

    Returns the integer status of the task.

    args:
        context (scriptworker.context.Context): the scriptworker context.
        run_cancellable (typing.Callable): wraps future such that it'll cancel upon worker shutdown
        to_cancellable_process (typing.Callable): wraps ``TaskProcess`` such that it will stop if the worker is shutting
            down

    Raises:
        Exception: on unexpected exception.

    Returns:
        int: exit status

    """
    status = 0
    try:
        if context.config['verify_chain_of_trust']:
            chain = ChainOfTrust(context, context.config['cot_job_type'])
            await run_cancellable(verify_chain_of_trust(chain))
        status = await run_task(context, to_cancellable_process)
        generate_cot(context)
    except asyncio.CancelledError:
        log.info("CoT cancelled asynchronously")
        raise WorkerShutdownDuringTask
    except ScriptWorkerException as e:
        status = worst_level(status, e.exit_code)
        log.error("Hit ScriptWorkerException: {}".format(e))
    except Exception as e:
        log.exception("SCRIPTWORKER_UNEXPECTED_EXCEPTION task {}".format(e))
        raise
    return status


# do_upload {{{1
async def do_upload(context, files):
    """Upload artifacts and return status.

    Returns the integer status of the upload.

    args:
        context (scriptworker.context.Context): the scriptworker context.
        files (list of str): list of files to be uploaded as artifacts

    Raises:
        Exception: on unexpected exception.

    Returns:
        int: exit status

    """
    status = 0
    try:
        await upload_artifacts(context, files)
    except ScriptWorkerException as e:
        status = worst_level(status, e.exit_code)
        log.error("Hit ScriptWorkerException: {}".format(e))
    except aiohttp.ClientError as e:
        status = worst_level(status, STATUSES['intermittent-task'])
        log.error("Hit aiohttp error: {}".format(e))
    except Exception as e:
        log.exception("SCRIPTWORKER_UNEXPECTED_EXCEPTION upload {}".format(e))
        raise
    return status


class RunTasks:
    """Manages processing of Taskcluster tasks."""

    def __init__(self):
        """Constructor."""
        self.future = None
        self.task_process = None
        self.is_cancelled = False

    async def invoke(self, context):
        """Claims and processes Taskcluster work.

        Args:
            context (scriptworker.context.Context): context of worker

        Returns: status code of build

        """
        try:
            # Note: claim_work(...) might not be safely interruptible! See
            # https://bugzilla.mozilla.org/show_bug.cgi?id=1524069
            tasks = await self._run_cancellable(claim_work(context))
            if not tasks or not tasks.get('tasks', []):
                await self._run_cancellable(asyncio.sleep(context.config['poll_interval']))
                return None

            # Assume only a single task, but should more than one fall through,
            # run them sequentially.  A side effect is our return status will
            # be the status of the final task run.
            status = None
            for task_defn in tasks.get('tasks', []):
                prepare_to_run_task(context, task_defn)
                reclaim_fut = context.event_loop.create_task(reclaim_task(context, context.task))
                try:
                    status = await do_run_task(context, self._run_cancellable, self._to_cancellable_process)
                    artifacts_paths = filepaths_in_dir(context.config['artifact_dir'])
                except WorkerShutdownDuringTask:
                    shutdown_artifact_paths = [os.path.join('public', 'logs', log_file)
                                               for log_file in ['chain_of_trust.log', 'live_backing.log']]
                    artifacts_paths = [path for path in shutdown_artifact_paths
                                       if os.path.isfile(os.path.join(context.config['artifact_dir'], path))]
                    status = STATUSES['worker-shutdown']
                status = worst_level(status, await do_upload(context, artifacts_paths))
                await complete_task(context, status)
                reclaim_fut.cancel()
                cleanup(context)

            return status

        except asyncio.CancelledError:
            return None

    async def _run_cancellable(self, coroutine: typing.Awaitable):
        self.future = asyncio.ensure_future(coroutine)
        if self.is_cancelled:
            self.future.cancel()
        result = await self.future
        self.future = None
        return result

    async def _to_cancellable_process(self, task_process: TaskProcess):
        self.task_process = task_process

        if self.is_cancelled:
            await task_process.worker_shutdown_stop()

        return task_process

    async def cancel(self):
        """Cancel current work."""
        self.is_cancelled = True
        if self.future is not None:
            self.future.cancel()
        if self.task_process is not None:
            log.warning("Worker is shutting down, but a task is running. Terminating task")
            await self.task_process.worker_shutdown_stop()


# run_tasks {{{1
async def run_tasks(context):
    """Run any tasks returned by claimWork.

    Returns the integer status of the task that was run, or None if no task was
    run.

    args:
        context (scriptworker.context.Context): the scriptworker context.

    Raises:
        Exception: on unexpected exception.

    Returns:
        int: exit status
        None: if no task run.

    """
    running_tasks = RunTasks()
    context.running_tasks = running_tasks
    status = await running_tasks.invoke(context)
    context.running_tasks = None
    return status


# async_main {{{1
async def async_main(context, credentials):
    """Set up and run tasks for this iteration.

    http://docs.taskcluster.net/queue/worker-interaction/

    Args:
        context (scriptworker.context.Context): the scriptworker context.
    """
    conn = aiohttp.TCPConnector(limit=context.config['aiohttp_max_connections'])
    async with aiohttp.ClientSession(connector=conn) as session:
        context.session = session
        context.credentials = credentials
        await run_tasks(context)


# main {{{1
def main(event_loop=None):
    """Scriptworker entry point: get everything set up, then enter the main loop.

    Args:
        event_loop (asyncio.BaseEventLoop, optional): the event loop to use.
            If None, use ``asyncio.get_event_loop()``. Defaults to None.

    """
    context, credentials = get_context_from_cmdln(sys.argv[1:])
    log.info("Scriptworker starting up at {} UTC".format(arrow.utcnow().format()))
    cleanup(context)
    context.event_loop = event_loop or asyncio.get_event_loop()

    done = False

    async def _handle_sigterm():
        log.info("SIGTERM received; shutting down")
        nonlocal done
        done = True
        if context.running_tasks is not None:
            await context.running_tasks.cancel()

    context.event_loop.add_signal_handler(signal.SIGTERM, lambda: asyncio.ensure_future(_handle_sigterm()))

    while not done:
        try:
            context.event_loop.run_until_complete(async_main(context, credentials))
        except Exception:
            log.critical("Fatal exception", exc_info=1)
            raise
