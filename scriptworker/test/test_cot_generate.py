#!/usr/bin/env python
# coding=utf-8
"""Test scriptworker.cot.generate
"""
import logging
import os
import pytest
from scriptworker.exceptions import ScriptWorkerException
import scriptworker.cot.generate as cot
from . import ARTIFACT_SHAS, rw_context

assert rw_context  # silence pyflakes

log = logging.getLogger(__name__)


# constants helpers and fixtures {{{1
ARTIFACT_DIR = os.path.join(os.path.dirname(__file__), "data", "artifacts")


@pytest.yield_fixture(scope='function')
def context(rw_context):
    ED25519_DIR = os.path.join(os.path.dirname(__file__), "data", "ed25519")
    rw_context.config['artifact_dir'] = ARTIFACT_DIR
    rw_context.config['sign_chain_of_trust'] = True
    rw_context.config['ed25519_private_key_path'] = os.path.join(ED25519_DIR, 'scriptworker_private_key')

    rw_context.claim_task = {
        "runId": 2,
        "status": {
            "taskId": "taskId",
        },
        "task": {
            'dependencies': [],
            "payload": {},
            "scopes": ["foo"],
            "taskGroupId": "taskGroupId",
            "workerType": "workerType",
        },
        "workerGroup": "worker_group",
        "credentials": {'c': 'd'},
    }
    yield rw_context


def expected_cot_body(context_, artifacts):
    return {
        'artifacts': artifacts,
        'chainOfTrustVersion': 1,
        'runId': context_.claim_task['runId'],
        'task': context_.task,
        'taskId': context_.claim_task['status']['taskId'],
        'workerGroup': context_.claim_task['workerGroup'],
        'workerId': context_.config['worker_id'],
        'workerType': context_.config['worker_type'],
        'environment': {}
    }


@pytest.fixture(scope='function')
def artifacts():
    artifacts = {}
    for k, v in ARTIFACT_SHAS.items():
        artifacts[k] = {"sha256": v}
    return artifacts


# tests {{{1
def test_get_cot_artifacts(artifacts, context):
    value = cot.get_cot_artifacts(context)
    assert value == artifacts


def test_generate_cot_body(artifacts, context):
    assert cot.generate_cot_body(context) == expected_cot_body(context, artifacts)


def test_generate_cot_body_exception(artifacts, context):
    del context.config['worker_type']
    with pytest.raises(ScriptWorkerException):
        cot.generate_cot_body(context)


def test_generate_cot(artifacts, context):
    path = os.path.join(context.config['work_dir'], "chain-of-trust.json")
    body = cot.generate_cot(context, parent_path=context.config['work_dir'])
    with open(path, "r") as fh:
        assert fh.read() == body


def test_generate_cot_unsigned(artifacts, context):
    context.config['sign_chain_of_trust'] = False
    body = cot.generate_cot(context, parent_path=context.config['work_dir'])
    assert body == cot.format_json(cot.generate_cot_body(context))


def test_generate_cot_exception(artifacts, context):
    context.config['cot_schema_path'] = os.path.join(context.config['work_dir'], "not_a_file")
    with pytest.raises(ScriptWorkerException):
        cot.generate_cot(context)
