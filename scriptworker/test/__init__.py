#!/usr/bin/env python
# coding=utf-8
"""Test base files
"""
import aiohttp
import arrow
import asyncio
from copy import deepcopy
import functools
import json
import mock
import os
import pytest
import tempfile
import taskcluster.exceptions
from scriptworker.config import get_unfrozen_copy, apply_product_config
from scriptworker.constants import DEFAULT_CONFIG
from scriptworker.context import Context
from scriptworker.utils import makedirs
try:
    import yarl
    YARL = True
except ImportError:
    YARL = False


VERBOSE = os.environ.get("SCRIPTWORKER_VERBOSE_TESTS", False)

ARTIFACT_SHAS = {
    "public/foo": "b5bb9d8014a0f9b1d61e21e796d78dccdf1352f23cd32812f4850b878ae4944c",
    "public/baz": "bf07a7fbb825fc0aae7bf4a1177b2b31fcf8a3feeaf7092761e18c859ee52a9c",
    "public/logs/bar": "7d865e959b2466918c9863afca942d0fb89d7c9ac0c99bafc3749504ded97730",
}

TIMEOUT_SCRIPT = os.path.join(os.path.dirname(__file__), "data", "long_running.py")


def read(path):
    """Return the contents of a file.
    """
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def touch(path):
    """ Create an empty file.  Different from the system 'touch' in that it
    will overwrite an existing file.
    """
    with open(path, "w") as fh:
        print(path, file=fh, end="")


class SuccessfulQueue(object):
    result = "yay"
    info = None
    status = 409
    task = {}
    reclaim_task = {
        'credentials': {'a': 'b'},
    }

    async def claimTask(self, *args, **kwargs):
        return self.result

    async def reclaimTask(self, *args, **kwargs):
        self.info = ['reclaimTask', args, kwargs]
        raise taskcluster.exceptions.TaskclusterRestFailure("foo", None, status_code=self.status)

    async def reportCompleted(self, *args, **kwargs):
        self.info = ['reportCompleted', args, kwargs]

    async def reportFailed(self, *args, **kwargs):
        self.info = ['reportFailed', args, kwargs]

    async def reportException(self, *args, **kwargs):
        self.info = ['reportException', args, kwargs]

    async def createArtifact(self, *args, **kwargs):
        self.info = ['createArtifact', args, kwargs]
        return {
            "contentType": "text/plain",
            "putUrl": "url",
        }

    async def claimWork(self, *args, **kwargs):
        self.info = ['claimWork', args, kwargs]
        return {'tasks': [self.task]}


class UnsuccessfulQueue(object):
    status = 409

    async def claimTask(self, *args, **kwargs):
        raise taskcluster.exceptions.TaskclusterRestFailure("foo", None, status_code=self.status)

    async def reportCompleted(self, *args, **kwargs):
        raise taskcluster.exceptions.TaskclusterRestFailure("foo", None, status_code=self.status)

    async def reportFailed(self, *args, **kwargs):
        raise taskcluster.exceptions.TaskclusterRestFailure("foo", None, status_code=self.status)


class FakeResponse(aiohttp.client_reqrep.ClientResponse):
    """Integration tests allow us to test everything's hooked up to aiohttp
    correctly.  When we don't want to actually hit an external url, have
    the aiohttp session's _request method return a FakeResponse.
    """
    def __init__(self, *args, status=200, payload=None, **kwargs):
        self._connection = mock.MagicMock()
        self._payload = payload or {}
        self.status = status
        self._headers = {'content-type': 'application/json'}
        self._cache = {}
        self._loop = mock.MagicMock()
        self.content = self
        self.resp = [b"asdf", b"asdf"]
        self._url = args[1]
        self._history = ()
        if YARL:
            # fix aiohttp 1.1.0
            self._url_obj = yarl.URL(args[1])

    @asyncio.coroutine
    def text(self, *args, **kwargs):
        return json.dumps(self._payload)

    @asyncio.coroutine
    def json(self, *args, **kwargs):
        return self._payload

    @asyncio.coroutine
    def release(self):
        return

    async def read(self, *args):
        if self.resp:
            return self.resp.pop(0)


@pytest.fixture(scope='function')
def successful_queue():
    return SuccessfulQueue()


@pytest.fixture(scope='function')
def unsuccessful_queue():
    return UnsuccessfulQueue()


@asyncio.coroutine
def _fake_request(resp_status, method, url, *args, **kwargs):
    resp = FakeResponse(method, url, status=resp_status)
    resp._history = (FakeResponse(method, url, status=302),)
    return resp


@pytest.mark.asyncio
@pytest.fixture(scope='function')
async def fake_session():
    session = aiohttp.ClientSession()
    session._request = functools.partial(_fake_request, 200)
    yield session
    await session.close()


@pytest.mark.asyncio
@pytest.fixture(scope='function')
async def fake_session_500():
    session = aiohttp.ClientSession()
    session._request = functools.partial(_fake_request, 500)
    yield session
    await session.close()


@pytest.mark.asyncio
@pytest.fixture(scope='function')
async def fake_session_404():
    session = aiohttp.ClientSession()
    session._request = functools.partial(_fake_request, 404)
    yield session
    await session.close()


def integration_create_task_payload(config, task_group_id, scopes=None,
                                    task_payload=None, task_extra=None):
    """For various integration tests, we need to call createTask for test tasks.

    This function creates a dummy payload for those createTask calls.
    """
    now = arrow.utcnow()
    deadline = now.replace(hours=1)
    expires = now.replace(days=3)
    scopes = scopes or []
    task_payload = task_payload or {}
    task_extra = task_extra or {}
    return {
        'provisionerId': config['provisioner_id'],
        'schedulerId': 'test-dummy-scheduler',
        'workerType': config['worker_type'],
        'taskGroupId': task_group_id,
        'dependencies': [],
        'requires': 'all-completed',
        'routes': [],
        'priority': 'normal',
        'retries': 5,
        'created': now.isoformat(),
        'deadline': deadline.isoformat(),
        'expires': expires.isoformat(),
        'scopes': scopes,
        'payload': task_payload,
        'metadata': {
            'name': 'ScriptWorker Integration Test',
            'description': 'ScriptWorker Integration Test',
            'owner': 'release+python@mozilla.com',
            'source': 'https://github.com/mozilla-releng/scriptworker/'
        },
        'tags': {},
        'extra': task_extra,
    }


@pytest.yield_fixture(scope='function')
def tmpdir():
    """Yield a tmpdir that gets cleaned up afterwards.

    This is because various pytest tmpdir implementations either don't return
    a string, or don't clean up properly.
    """
    with tempfile.TemporaryDirectory() as tmp:
        yield tmp


@pytest.yield_fixture(scope='function')
def tmpdir2():
    """Yield a tmpdir that gets cleaned up afterwards.

    Sometimes I need 2 tmpdirs in a test.
    a string, or don't clean up properly.
    """
    with tempfile.TemporaryDirectory() as tmp:
        yield tmp


async def _close_session(obj):
    """Get rid of all the unclosed session warnings.

    """
    if not hasattr(obj, 'session'):
        return
    if isinstance(obj.session, aiohttp.ClientSession):
        await obj.session.close()


@pytest.mark.asyncio
@pytest.yield_fixture(scope='function')
async def rw_context(event_loop):
    async with aiohttp.ClientSession() as session:
        with tempfile.TemporaryDirectory() as tmp:
            context = _craft_rw_context(tmp, event_loop, cot_product='firefox', session=session)
            yield context


@pytest.mark.asyncio
@pytest.yield_fixture(scope='function')
async def mobile_rw_context(event_loop):
    async with aiohttp.ClientSession() as session:
        with tempfile.TemporaryDirectory() as tmp:
            context = _craft_rw_context(tmp, event_loop, cot_product='mobile', session=session)
            yield context


def _craft_rw_context(tmp, event_loop, cot_product, session):
    config = get_unfrozen_copy(DEFAULT_CONFIG)
    config['cot_product'] = cot_product
    context = Context()
    context.session = session
    context.config = apply_product_config(config)
    context.config['cot_job_type'] = "signing"
    for key, value in context.config.items():
        if key.endswith("_dir"):
            context.config[key] = os.path.join(tmp, key)
            makedirs(context.config[key])
        if key.endswith("key_path"):
            context.config[key] = os.path.join(tmp, key)
    context.config['verbose'] = VERBOSE
    context.event_loop = event_loop
    return context


async def noop_async(*args, **kwargs):
    pass


def noop_sync(*args, **kwargs):
    pass


def create_finished_future(result=None):
    future = asyncio.Future()
    future.set_result(result)
    return future


def create_rejected_future(exception=BaseException):
    future = asyncio.Future()
    future.set_exception(exception)
    return future


def create_slow_async(result=None):
    future = asyncio.Future()

    async def slow_function(*args, **kwargs):
        future.set_result(None)
        await asyncio.Future()
        return result

    return future, slow_function


def create_sync(result=None):
    def fn(*args, **kwargs):
        return result
    return fn


def create_async(result=None):
    async def fn(*args, **kwargs):
        return result
    return fn
