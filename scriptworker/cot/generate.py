#!/usr/bin/env python
"""Chain of Trust artifact generation.

Attributes:
    log (logging.Logger): the log object for this module.

"""
import logging
import os
from scriptworker.client import validate_json_schema
from scriptworker.ed25519 import ed25519_private_key_from_file
from scriptworker.exceptions import ScriptWorkerException
from scriptworker.utils import (
    filepaths_in_dir,
    format_json,
    get_hash,
    load_json_or_yaml,
    write_to_file,
)

log = logging.getLogger(__name__)


# get_cot_artifacts {{{1
def get_cot_artifacts(context):
    """Generate the artifact relative paths and shas for the chain of trust.

    Args:
        context (scriptworker.context.Context): the scriptworker context.

    Returns:
        dict: a dictionary of {"path/to/artifact": {"hash_alg": "..."}, ...}

    """
    artifacts = {}
    filepaths = filepaths_in_dir(context.config['artifact_dir'])
    hash_alg = context.config['chain_of_trust_hash_algorithm']
    for filepath in sorted(filepaths):
        path = os.path.join(context.config['artifact_dir'], filepath)
        sha = get_hash(path, hash_alg=hash_alg)
        artifacts[filepath] = {hash_alg: sha}
    return artifacts


# get_cot_environment {{{1
def get_cot_environment(context):
    """Get environment information for the chain of trust artifact.

    Args:
        context (scriptworker.context.Context): the scriptworker context.

    Returns:
        dict: the environment info.

    """
    env = {}
    # TODO
    return env


# generate_cot_body {{{1
def generate_cot_body(context):
    """Generate the chain of trust dictionary.

    This is the unsigned and unformatted chain of trust artifact contents.

    Args:
        context (scriptworker.context.Context): the scriptworker context.

    Returns:
        dict: the unsignd and unformatted chain of trust artifact contents.

    Raises:
        ScriptWorkerException: on error.

    """
    try:
        cot = {
            'artifacts': get_cot_artifacts(context),
            'chainOfTrustVersion': 1,
            'runId': context.claim_task['runId'],
            'task': context.task,
            'taskId': context.claim_task['status']['taskId'],
            'workerGroup': context.claim_task['workerGroup'],
            'workerId': context.config['worker_id'],
            'workerType': context.config['worker_type'],
            'environment': get_cot_environment(context),
        }
    except (KeyError, ) as exc:
        raise ScriptWorkerException("Can't generate chain of trust! {}".format(str(exc)))

    return cot


# generate_cot {{{1
def generate_cot(context, parent_path=None):
    """Format and sign the cot body, and write to disk.

    Args:
        context (scriptworker.context.Context): the scriptworker context.
        parent_path (str, optional): The directory to write the chain of trust
            artifacts to.  If None, this is ``artifact_dir/public/``.
            Defaults to None.

    Returns:
        str: the contents of the chain of trust artifact.

    Raises:
        ScriptWorkerException: on schema error.

    """
    body = generate_cot_body(context)
    schema = load_json_or_yaml(
        context.config['cot_schema_path'], is_path=True,
        exception=ScriptWorkerException,
        message="Can't read schema file {}: %(exc)s".format(context.config['cot_schema_path'])
    )
    validate_json_schema(body, schema, name="chain of trust")
    body = format_json(body)
    parent_path = parent_path or os.path.join(context.config['artifact_dir'], 'public')
    unsigned_path = os.path.join(parent_path, 'chain-of-trust.json')
    write_to_file(unsigned_path, body)
    if context.config['sign_chain_of_trust']:
        ed25519_signature_path = '{}.sig'.format(unsigned_path)
        ed25519_private_key = ed25519_private_key_from_file(context.config['ed25519_private_key_path'])
        ed25519_signature = ed25519_private_key.sign(body.encode('utf-8'))
        write_to_file(ed25519_signature_path, ed25519_signature, file_type='binary')
    return body
