"""Chain of Trust artifact verification.

Attributes:
    DECISION_TASK_TYPES (tuple): the decision task types.
    PARENT_TASK_TYPES (tuple): the parent task types.
    log (logging.Logger): the log object for this module.

"""
import aiohttp
import argparse
import asyncio
from copy import deepcopy
import dictdiffer
from frozendict import frozendict
import jsone
import logging
import os
import pprint
import sys
import tempfile
from urllib.parse import urlparse
from scriptworker.artifacts import (
    download_artifacts,
    get_artifact_url,
    get_optional_artifacts_per_task_id,
    get_single_upstream_artifact_full_path,
)
from scriptworker.config import read_worker_creds, apply_product_config
from scriptworker.constants import DEFAULT_CONFIG
from scriptworker.context import Context
from scriptworker.ed25519 import ed25519_public_key_from_string, verify_ed25519_signature
from scriptworker.exceptions import CoTError, BaseDownloadError, ScriptWorkerEd25519Error
from scriptworker.github import (
    GitHubRepository,
    extract_github_repo_owner_and_name,
    extract_github_repo_full_name,
)
from scriptworker.log import contextual_log_handler
from scriptworker.task import (
    get_action_callback_name,
    get_project,
    get_and_check_tasks_for,
    get_commit_message,
    get_decision_task_id,
    get_parent_task_id,
    get_pull_request_number,
    get_push_date_time,
    get_repo,
    get_repo_scope,
    get_revision,
    get_branch,
    get_triggered_by,
    get_task_id,
    get_worker_pool_id,
    is_try_or_pull_request,
    is_action,
)
from scriptworker.utils import (
    add_enumerable_item_to_dict,
    get_hash,
    get_loggable_url,
    get_results_and_future_exceptions,
    format_json,
    load_json_or_yaml,
    load_json_or_yaml_from_url,
    makedirs,
    match_url_path_callback,
    match_url_regex,
    raise_future_exceptions,
    read_from_file,
    remove_empty_keys,
    rm,
    write_to_file,
)
from scriptworker.version import __version_string__
from taskcluster.exceptions import TaskclusterFailure
from taskcluster.aio import Queue

log = logging.getLogger(__name__)


DECISION_TASK_TYPES = ('decision', )
PARENT_TASK_TYPES = ('decision', 'action')


# ChainOfTrust {{{1
class ChainOfTrust(object):
    """The master Chain of Trust, tracking all the various ``LinkOfTrust``s.

    Attributes:
        context (scriptworker.context.Context): the scriptworker context
        decision_task_id (str): the task_id of self.task's decision task
        parent_task_id (str): the task_id of self.task's parent task
        links (list): the list of ``LinkOfTrust``s
        name (str): the name of the task (e.g., signing)
        task_id (str): the taskId of the task
        task_type (str): the task type of the task (e.g., decision, build)
        worker_impl (str): the taskcluster worker class (e.g., docker-worker) of the task

    """

    def __init__(self, context, name, task_id=None):
        """Initialize ChainOfTrust.

        Args:
            context (scriptworker.context.Context): the scriptworker context
            name (str): the name of the task (e.g., signing)
            task_id (str, optional): the task_id of the task.  If None, use
                ``get_task_id(context.claim_task)``.  Defaults to None.

        """
        self.name = name
        self.context = context
        self.task_id = task_id or get_task_id(context.claim_task)
        self.task = context.task
        self.task_type = guess_task_type(name, self.task)
        self.worker_impl = guess_worker_impl(self)  # this should be scriptworker
        self.decision_task_id = get_decision_task_id(self.task)
        self.parent_task_id = get_parent_task_id(self.task)
        self.links = []

    def dependent_task_ids(self):
        """Get all ``task_id``s for all ``LinkOfTrust`` tasks.

        Returns:
            list: the list of ``task_id``s

        """
        return [x.task_id for x in self.links]

    async def is_try_or_pull_request(self):
        """Determine if any task in the chain is a try task.

        Returns:
            bool: True if a task is a try task.

        """
        tasks = [asyncio.ensure_future(link.is_try_or_pull_request()) for link in self.links]
        tasks.insert(0, asyncio.ensure_future(is_try_or_pull_request(self.context, self.task)))

        conditions = await raise_future_exceptions(tasks)
        return any(conditions)

    def get_link(self, task_id):
        """Get a ``LinkOfTrust`` by task id.

        Args:
            task_id (str): the task id to find.

        Returns:
            LinkOfTrust: the link matching the task id.

        Raises:
            CoTError: if no ``LinkOfTrust`` matches.

        """
        links = [x for x in self.links if x.task_id == task_id]
        if len(links) != 1:
            raise CoTError("No single Link matches task_id {}!\n{}".format(task_id, self.dependent_task_ids()))
        return links[0]

    def is_decision(self):
        """Determine if the chain is a decision task.

        Returns:
            bool: whether it is a decision task.

        """
        return self.task_type in DECISION_TASK_TYPES

    def has_restricted_scopes(self):
        """Determine if this task is requesting any restricted scopes.

        Returns:
            bool: whether this task requested restricted scopes.

        """
        return any(
            (scope in self.context.config['cot_restricted_scopes'])
            for scope in self.task['scopes']
        )

    def get_all_links_in_chain(self):
        """Return all links in the chain of trust, including the target task.

        By default, we're checking a task and all its dependencies back to the
        tree, so the full chain is ``self.links`` + ``self``. However, we also
        support checking the decision task itself. In that case, we populate
        the decision task as a link in ``self.links``, and we don't need to add
        another check for ``self``.

        Returns:
            list: of all ``LinkOfTrust``s to verify.

        """
        if self.is_decision() and self.get_link(self.task_id):
            return self.links
        return [self] + self.links


# LinkOfTrust {{{1
class LinkOfTrust(object):
    """Each LinkOfTrust represents a task in the Chain of Trust and its status.

    Attributes:
        context (scriptworker.context.Context): the scriptworker context
        decision_task_id (str): the task_id of self.task's decision task
        parent_task_id (str): the task_id of self.task's parent task
        is_try_or_pull_request (bool): whether the task is a try or a pull request task
        name (str): the name of the task (e.g., signing.decision)
        task_id (str): the taskId of the task
        task_graph (dict): the task graph of the task, if this is a decision task
        task_type (str): the task type of the task (e.g., decision, build)
        worker_impl (str): the taskcluster worker class (e.g., docker-worker) of the task

    """

    _task = None
    _cot = None
    _task_graph = None
    status = None

    def __init__(self, context, name, task_id):
        """Initialize ChainOfTrust.

        Args:
            context (scriptworker.context.Context): the scriptworker context
            name (str): the name of the task (e.g., signing)
            task_id (str): the task_id of the task

        """
        self.name = name
        self.context = context
        self.task_id = task_id

    def _set(self, prop_name, value):
        prev = getattr(self, prop_name)
        if prev is not None:
            raise CoTError(
                "LinkOfTrust {}: re-setting {} to {} when it is already set to {}!".format(
                    str(self.name), prop_name, value, prev
                )
            )
        return setattr(self, prop_name, value)

    @property
    def task(self):
        """dict: the task definition.

        When set, we also set ``self.decision_task_id``, ``self.parent_task_id``,
        and ``self.worker_impl`` based on the task definition.

        """
        return self._task

    @task.setter
    def task(self, task):
        self._set('_task', task)
        self.task_type = guess_task_type(self.name, self.task)
        self.decision_task_id = get_decision_task_id(self.task)
        self.parent_task_id = get_parent_task_id(self.task)
        self.worker_impl = guess_worker_impl(self)

    async def is_try_or_pull_request(self):
        """bool: the task is either a try or a pull request one."""
        return await is_try_or_pull_request(self.context, self.task)

    @property
    def cot(self):
        """dict: the chain of trust json body."""
        return self._cot

    @cot.setter
    def cot(self, cot):
        cot_task_id = cot.get('taskId')
        if cot_task_id != self.task_id:
            raise CoTError("Chain of Trust artifact taskId {} doesn't match task taskId {}!".format(cot_task_id, self.task_id))
        self._set('_cot', cot)

    @property
    def task_graph(self):
        """dict: the decision task graph, if this is a decision task."""
        return self._task_graph

    @task_graph.setter
    def task_graph(self, task_graph):
        self._set('_task_graph', task_graph)

    @property
    def cot_dir(self):
        """str: the local path containing this link's artifacts."""
        return self.get_artifact_full_path(path='.')

    def get_artifact_full_path(self, path):
        """str: the full path where an artifact should be located."""
        return get_single_upstream_artifact_full_path(self.context, self.task_id, path)


# raise_on_errors {{{1
def raise_on_errors(errors, level=logging.CRITICAL):
    """Raise a CoTError if errors.

    Helper function because I had this code block everywhere.

    Args:
        errors (list): the error errors
        level (int, optional): the log level to use.  Defaults to logging.CRITICAL

    Raises:
        CoTError: if errors is non-empty

    """
    if errors:
        log.log(level, "\n".join(errors))
        raise CoTError("\n".join(errors))


# guess_worker_impl {{{1
def guess_worker_impl(link):
    """Given a task, determine which worker implementation (e.g., docker-worker) it was run on.

    * check for the `worker-implementation` tag
    * docker-worker: ``task.payload.image`` is not None
    * check for scopes beginning with the worker type name.
    * generic-worker: ``task.payload.osGroups`` is not None
    * generic-worker: ``task.payload.mounts`` is not None

    Args:
        link (LinkOfTrust or ChainOfTrust): the link to check.

    Returns:
        str: the worker type.

    Raises:
        CoTError: on inability to determine the worker implementation

    """
    worker_impls = []
    task = link.task
    name = link.name
    errors = []

    if task['payload'].get("image"):
        worker_impls.append("docker-worker")
    if task['provisionerId'] in link.context.config['scriptworker_provisioners']:
        worker_impls.append("scriptworker")
    if get_worker_pool_id(task) in link.context.config['scriptworker_worker_pools']:
        worker_impls.append("scriptworker")
    if task['payload'].get("mounts") is not None:
        worker_impls.append("generic-worker")
    if task['payload'].get("osGroups") is not None:
        worker_impls.append("generic-worker")
    if task.get('tags', {}).get("worker-implementation", {}):
        worker_impls.append(task['tags']['worker-implementation'])

    for scope in task['scopes']:
        if scope.startswith("docker-worker:"):
            worker_impls.append("docker-worker")

    if not worker_impls:
        errors.append("guess_worker_impl: can't find a worker_impl for {}!\n{}".format(name, task))
    if len(set(worker_impls)) > 1:
        errors.append("guess_worker_impl: too many matches for {}: {}!\n{}".format(name, set(worker_impls), task))
    raise_on_errors(errors)
    log.debug("{} {} is {}".format(name, link.task_id, worker_impls[0]))
    return worker_impls[0]


# get_valid_worker_impls {{{1
def get_valid_worker_impls():
    """Get the valid worker_impls, e.g. docker-worker.

    No longer a constant, due to code ordering issues.

    Returns:
        frozendict: maps the valid worker_impls (e.g., docker-worker) to their
            validation functions.

    """
    # TODO support taskcluster worker
    return frozendict({
        'docker-worker': verify_docker_worker_task,
        'generic-worker': verify_generic_worker_task,
        'scriptworker': verify_scriptworker_task,
    })


# guess_task_type {{{1
def guess_task_type(name, task_defn):
    """Guess the task type of the task.

    Args:
        name (str): the name of the task.

    Returns:
        str: the task_type.

    Raises:
        CoTError: on invalid task_type.

    """
    parts = name.split(':')
    task_type = parts[-1]
    if task_type == 'parent':
        if is_action(task_defn):
            task_type = 'action'
        else:
            task_type = 'decision'
    if task_type not in get_valid_task_types():
        raise CoTError(
            "Invalid task type for {}!".format(name)
        )
    return task_type


# get_valid_task_types {{{1
def get_valid_task_types():
    """Get the valid task types, e.g. signing.

    No longer a constant, due to code ordering issues.

    Returns:
        frozendict: maps the valid task types (e.g., signing) to their validation functions.

    """
    return frozendict({
        'scriptworker': verify_scriptworker_task,
        'balrog': verify_scriptworker_task,
        'beetmover': verify_scriptworker_task,
        'bouncer': verify_scriptworker_task,
        'build': verify_build_task,
        'l10n': verify_build_task,
        'repackage': verify_build_task,
        'action': verify_parent_task,
        'decision': verify_parent_task,
        'docker-image': verify_docker_image_task,
        'pushapk': verify_scriptworker_task,
        'pushsnap': verify_scriptworker_task,
        'shipit': verify_scriptworker_task,
        'signing': verify_scriptworker_task,
        'partials': verify_partials_task,
    })


# check_interactive_docker_worker {{{1
def check_interactive_docker_worker(link):
    """Given a task, make sure the task was not defined as interactive.

    * ``task.payload.features.interactive`` must be absent or False.
    * ``task.payload.env.TASKCLUSTER_INTERACTIVE`` must be absent or False.

    Args:
        link (LinkOfTrust): the task link we're checking.

    Returns:
        list: the list of error errors.  Success is an empty list.

    """
    errors = []
    log.info("Checking for {} {} interactive docker-worker".format(link.name, link.task_id))
    try:
        if link.task['payload']['features'].get('interactive'):
            errors.append("{} is interactive: task.payload.features.interactive!".format(link.name))
        if link.task['payload']['env'].get('TASKCLUSTER_INTERACTIVE'):
            errors.append("{} is interactive: task.payload.env.TASKCLUSTER_INTERACTIVE!".format(link.name))
    except KeyError:
        errors.append("check_interactive_docker_worker: {} task definition is malformed!".format(link.name))
    return errors


# verify_docker_image_sha {{{1
def verify_docker_image_sha(chain, link):
    """Verify that built docker shas match the artifact.

    Args:
        chain (ChainOfTrust): the chain we're operating on.
        link (LinkOfTrust): the task link we're checking.

    Raises:
        CoTError: on failure.

    """
    cot = link.cot
    task = link.task
    errors = []

    image = task['payload'].get('image')
    if isinstance(image, dict) and image['type'] == 'task-image':
        # Using pre-built image from docker-image task
        docker_image_task_id = task['extra']['chainOfTrust']['inputs']['docker-image']
        log.debug("Verifying {} {} against docker-image {}".format(
            link.name, link.task_id, docker_image_task_id
        ))
        if docker_image_task_id != image['taskId']:
            errors.append("{} {} docker-image taskId isn't consistent!: {} vs {}".format(
                link.name, link.task_id, docker_image_task_id,
                task['payload']['image']['taskId']
            ))
        else:
            path = image['path']
            # we need change the hash alg everywhere if we change, and recreate
            # the docker images...
            image_hash = cot['environment']['imageArtifactHash']
            alg, sha = image_hash.split(':')
            docker_image_link = chain.get_link(docker_image_task_id)
            upstream_sha = docker_image_link.cot['artifacts'].get(path, {}).get(alg)
            if upstream_sha is None:
                errors.append("{} {} docker-image docker sha {} is missing! {}".format(
                    link.name, link.task_id, alg,
                    docker_image_link.cot['artifacts'][path]
                ))
            elif upstream_sha != sha:
                errors.append("{} {} docker-image docker sha doesn't match! {} {} vs {}".format(
                    link.name, link.task_id, alg, sha, upstream_sha
                ))
            else:
                log.debug("Found matching docker-image sha {}".format(upstream_sha))
    elif isinstance(image, dict) and image['type'] == 'indexed-image':
        # FIXME: Indexed image should be verified by CoT as well
        if chain.has_restricted_scopes():
            errors.append("Indexed images are not usable for tasks with restricted scopes.")
        prebuilt_task_types = chain.context.config['prebuilt_docker_image_task_types']
        if prebuilt_task_types != "any" and link.task_type not in prebuilt_task_types:
            errors.append(
                "Task type {} not allowed to use a indexed docker image!".format(
                    link.task_type
                )
            )
    elif isinstance(image, dict):
        errors.append("Unknown type of docker image {}.".format(image.get('type')))
    else:
        prebuilt_task_types = chain.context.config['prebuilt_docker_image_task_types']
        if prebuilt_task_types != "any" and link.task_type not in prebuilt_task_types:
            errors.append(
                "Task type {} not allowed to use a prebuilt docker image!".format(
                    link.task_type
                )
            )
    raise_on_errors(errors)


# find_sorted_task_dependencies {{{1
def find_sorted_task_dependencies(task, task_name, task_id):
    """Find the taskIds of the chain of trust dependencies of a given task.

    Args:
        task (dict): the task definition to inspect.
        task_name (str): the name of the task, for logging and naming children.
        task_id (str): the taskId of the task.

    Returns:
        list: tuples associating dependent task ``name`` to dependent task ``taskId``.

    """
    log.info("find_sorted_task_dependencies {} {}".format(task_name, task_id))

    cot_input_dependencies = [
        _craft_dependency_tuple(task_name, task_type, task_id)
        for task_type, task_id in task['extra'].get('chainOfTrust', {}).get('inputs', {}).items()
    ]

    upstream_artifacts_dependencies = [
        _craft_dependency_tuple(task_name, artifact_dict['taskType'], artifact_dict['taskId'])
        for artifact_dict in task.get('payload', {}).get('upstreamArtifacts', [])
    ]

    dependencies = [*cot_input_dependencies, *upstream_artifacts_dependencies]
    dependencies = _sort_dependencies_by_name_then_task_id(dependencies)

    parent_task_id = get_parent_task_id(task) or get_decision_task_id(task)
    parent_task_type = 'parent'
    # make sure we deal with the decision task first, or we may populate
    # signing:build0:decision before signing:decision
    parent_tuple = _craft_dependency_tuple(task_name, parent_task_type, parent_task_id)
    dependencies.insert(0, parent_tuple)

    log.info('found dependencies: {}'.format(dependencies))
    return dependencies


def _craft_dependency_tuple(task_name, task_type, task_id):
    return ('{}:{}'.format(task_name, task_type), task_id)


def _sort_dependencies_by_name_then_task_id(dependencies):
    return sorted(dependencies, key=lambda dep: '{}_{}'.format(dep[0], dep[1]))


# build_task_dependencies {{{1
async def build_task_dependencies(chain, task, name, my_task_id):
    """Recursively build the task dependencies of a task.

    Args:
        chain (ChainOfTrust): the chain of trust to add to.
        task (dict): the task definition to operate on.
        name (str): the name of the task to operate on.
        my_task_id (str): the taskId of the task to operate on.

    Raises:
        CoTError: on failure.

    """
    log.info("build_task_dependencies {} {}".format(name, my_task_id))
    if name.count(':') > chain.context.config['max_chain_length']:
        raise CoTError("Too deep recursion!\n{}".format(name))
    sorted_dependencies = find_sorted_task_dependencies(task, name, my_task_id)

    for task_name, task_id in sorted_dependencies:
        if task_id not in chain.dependent_task_ids():
            link = LinkOfTrust(chain.context, task_name, task_id)
            json_path = link.get_artifact_full_path('task.json')
            try:
                task_defn = await chain.context.queue.task(task_id)
                link.task = task_defn
                chain.links.append(link)
                # write task json to disk
                makedirs(os.path.dirname(json_path))
                with open(json_path, 'w') as fh:
                    fh.write(format_json(task_defn))
                await build_task_dependencies(chain, task_defn, task_name, task_id)
            except TaskclusterFailure as exc:
                raise CoTError(str(exc))


# download_cot {{{1
async def download_cot(chain):
    """Download the signed chain of trust artifacts.

    Args:
        chain (ChainOfTrust): the chain of trust to add to.

    Raises:
        BaseDownloadError: on failure.

    """
    artifact_tasks = []
    # only deal with chain.links, which are previously finished tasks with
    # signed chain of trust artifacts.  ``chain.task`` is the current running
    # task, and will not have a signed chain of trust artifact yet.
    for link in chain.links:
        task_id = link.task_id
        parent_dir = link.cot_dir
        urls = []

        unsigned_url = get_artifact_url(chain.context, task_id, 'public/chain-of-trust.json')
        urls.append(unsigned_url)
        if chain.context.config['verify_cot_signature']:
            urls.append(
                get_artifact_url(chain.context, task_id, 'public/chain-of-trust.json.sig')
            )

        artifact_tasks.append(
            asyncio.ensure_future(
                download_artifacts(
                    chain.context, urls, parent_dir=parent_dir,
                    valid_artifact_task_ids=[task_id]
                )
            )
        )

    artifacts_paths = await raise_future_exceptions(artifact_tasks)

    for path in artifacts_paths:
        sha = get_hash(path[0])
        log.debug("{} downloaded; hash is {}".format(path[0], sha))


# download_cot_artifact {{{1
async def download_cot_artifact(chain, task_id, path):
    """Download an artifact and verify its SHA against the chain of trust.

    Args:
        chain (ChainOfTrust): the chain of trust object
        task_id (str): the task ID to download from
        path (str): the relative path to the artifact to download

    Returns:
        str: the full path of the downloaded artifact

    Raises:
        CoTError: on failure.

    """
    link = chain.get_link(task_id)
    log.debug("Verifying {} is in {} cot artifacts...".format(path, task_id))
    if not link.cot:
        log.warning('Chain of Trust for "{}" in {} does not exist. See above log for more details. \
Skipping download of this artifact'.format(path, task_id))
        return

    if path not in link.cot['artifacts']:
        raise CoTError("path {} not in {} {} chain of trust artifacts!".format(path, link.name, link.task_id))
    url = get_artifact_url(chain.context, task_id, path)
    loggable_url = get_loggable_url(url)
    log.info("Downloading Chain of Trust artifact:\n{}".format(loggable_url))
    await download_artifacts(
        chain.context, [url], parent_dir=link.cot_dir, valid_artifact_task_ids=[task_id]
    )
    full_path = link.get_artifact_full_path(path)
    for alg, expected_sha in link.cot['artifacts'][path].items():
        if alg not in chain.context.config['valid_hash_algorithms']:
            raise CoTError("BAD HASH ALGORITHM: {}: {} {}!".format(link.name, alg, full_path))
        real_sha = get_hash(full_path, hash_alg=alg)
        if expected_sha != real_sha:
            raise CoTError("BAD HASH on file {}: {}: Expected {} {}; got {}!".format(
                full_path, link.name, alg, expected_sha, real_sha
            ))
        log.debug("{} matches the expected {} {}".format(full_path, alg, expected_sha))
    return full_path


# download_cot_artifacts {{{1
async def download_cot_artifacts(chain):
    """Call ``download_cot_artifact`` in parallel for each "upstreamArtifacts".

    Optional artifacts are allowed to not be downloaded.

    Args:
        chain (ChainOfTrust): the chain of trust object

    Returns:
        list: list of full paths to downloaded artifacts. Failed optional artifacts
        aren't returned

    Raises:
        CoTError: on chain of trust sha validation error, on a mandatory artifact
        BaseDownloadError: on download error on a mandatory artifact

    """
    upstream_artifacts = chain.task['payload'].get('upstreamArtifacts', [])
    all_artifacts_per_task_id = get_all_artifacts_per_task_id(chain, upstream_artifacts)

    mandatory_artifact_tasks = []
    optional_artifact_tasks = []
    for task_id, paths in all_artifacts_per_task_id.items():
        for path in paths:
            coroutine = asyncio.ensure_future(download_cot_artifact(chain, task_id, path))

            if is_artifact_optional(chain, task_id, path):
                optional_artifact_tasks.append(coroutine)
            else:
                mandatory_artifact_tasks.append(coroutine)

    mandatory_artifacts_paths = await raise_future_exceptions(mandatory_artifact_tasks)
    succeeded_optional_artifacts_paths, failed_optional_artifacts = \
        await get_results_and_future_exceptions(optional_artifact_tasks)

    if failed_optional_artifacts:
        log.warning('Could not download {} artifacts: {}'.format(len(failed_optional_artifacts), failed_optional_artifacts))

    return mandatory_artifacts_paths + succeeded_optional_artifacts_paths


def is_artifact_optional(chain, task_id, path):
    """Tells whether an artifact is flagged as optional or not.

    Args:
        chain (ChainOfTrust): the chain of trust object
        task_id (str): the id of the aforementioned task

    Returns:
        bool: True if artifact is optional

    """
    upstream_artifacts = chain.task['payload'].get('upstreamArtifacts', [])
    optional_artifacts_per_task_id = get_optional_artifacts_per_task_id(upstream_artifacts)
    return path in optional_artifacts_per_task_id.get(task_id, [])


def get_all_artifacts_per_task_id(chain, upstream_artifacts):
    """Return every artifact to download, including the Chain Of Trust Artifacts.

    Args:
        chain (ChainOfTrust): the chain of trust object
        upstream_artifacts: the list of upstream artifact definitions

    Returns:
        dict: sorted list of paths to downloaded artifacts ordered by taskId

    """
    all_artifacts_per_task_id = {}
    for link in chain.links:
        # Download task-graph.json for decision+action task cot verification
        if link.task_type in PARENT_TASK_TYPES:
            add_enumerable_item_to_dict(
                dict_=all_artifacts_per_task_id, key=link.task_id, item='public/task-graph.json'
            )
        # Download actions.json for decision+action task cot verification
        if link.task_type in DECISION_TASK_TYPES:
            add_enumerable_item_to_dict(
                dict_=all_artifacts_per_task_id, key=link.task_id, item='public/actions.json'
            )
            add_enumerable_item_to_dict(
                dict_=all_artifacts_per_task_id, key=link.task_id, item='public/parameters.yml'
            )

    if upstream_artifacts:
        for upstream_dict in upstream_artifacts:
            add_enumerable_item_to_dict(
                dict_=all_artifacts_per_task_id, key=upstream_dict['taskId'], item=upstream_dict['paths']
            )

    # Avoid duplicate paths per task_id
    for task_id, paths in all_artifacts_per_task_id.items():
        all_artifacts_per_task_id[task_id] = sorted(set(paths))

    return all_artifacts_per_task_id


# verify_cot_signatures {{{1
def verify_link_ed25519_cot_signature(chain, link, unsigned_path, signature_path):
    """Verify the ed25519 signatures of the chain of trust artifacts populated in ``download_cot``.

    Populate each link.cot with the chain of trust json body.

    Args:
        chain (ChainOfTrust): the chain of trust to add to.

    Raises:
        (CoTError, ScriptWorkerEd25519Error): on signature verification failure.

    """
    if chain.context.config['verify_cot_signature']:
        log.debug("Verifying the {} {} {} ed25519 chain of trust signature".format(
            link.name, link.task_id, link.worker_impl
        ))
        signature = read_from_file(signature_path, file_type='binary', exception=CoTError)
        binary_contents = read_from_file(unsigned_path, file_type='binary', exception=CoTError)
        errors = []
        verify_key_seeds = chain.context.config['ed25519_public_keys'].get(link.worker_impl, [])
        for seed in verify_key_seeds:
            try:
                verify_key = ed25519_public_key_from_string(seed)
                verify_ed25519_signature(
                    verify_key, binary_contents, signature,
                    "{} {}: {} ed25519 cot signature doesn't verify against {}: %(exc)s".format(
                        link.name, link.task_id, link.worker_impl, seed
                    )
                )
                log.debug("{} {}: ed25519 cot signature verified.".format(link.name, link.task_id))
                break
            except ScriptWorkerEd25519Error as exc:
                errors.append(str(exc))
        else:
            errors = errors or [
                "{} {}: Unknown error verifying ed25519 cot signature. worker_impl {} verify_keys {}".format(
                    link.name, link.task_id, link.worker_impl,
                    verify_key_seeds
                )
            ]
            message = "\n".join(errors)
            raise CoTError(message)
    link.cot = load_json_or_yaml(
        unsigned_path, is_path=True, exception=CoTError,
        message="{} {}: Invalid unsigned cot json body! %(exc)s".format(link.name, link.task_id)
    )


def verify_cot_signatures(chain):
    """Verify the signatures of the chain of trust artifacts populated in ``download_cot``.

    Populate each link.cot with the chain of trust json body.

    Args:
        chain (ChainOfTrust): the chain of trust to add to.

    Raises:
        CoTError: on failure.

    """
    for link in chain.links:
        unsigned_path = link.get_artifact_full_path('public/chain-of-trust.json')
        ed25519_signature_path = link.get_artifact_full_path('public/chain-of-trust.json.sig')
        verify_link_ed25519_cot_signature(chain, link, unsigned_path, ed25519_signature_path)


# verify_task_in_task_graph {{{1
def verify_task_in_task_graph(task_link, graph_defn, level=logging.CRITICAL):
    """Verify a given task_link's task against a given graph task definition.

    This is a helper function for ``verify_link_in_task_graph``; this is split
    out so we can call it multiple times when we fuzzy match.

    Args:
        task_link (LinkOfTrust): the link to try to match
        graph_defn (dict): the task definition from the task-graph.json to match
            ``task_link`` against
        level (int, optional): the logging level to use on errors. Defaults to logging.CRITICAL

    Raises:
        CoTError: on failure

    """
    ignore_keys = ("created", "deadline", "expires", "dependencies", "schedulerId")
    errors = []
    runtime_defn = deepcopy(task_link.task)
    # dependencies
    # Allow for the decision task ID in the dependencies; otherwise the runtime
    # dependencies must be a subset of the graph dependencies.
    bad_deps = set(runtime_defn['dependencies']) - set(graph_defn['task']['dependencies'])
    # it's OK if a task depends on the decision task
    bad_deps = bad_deps - {task_link.decision_task_id}
    if bad_deps:
        errors.append("{} {} dependencies don't line up!\n{}".format(
            task_link.name, task_link.task_id, bad_deps
        ))
    # payload - eliminate the 'expires' key from artifacts because the datestring
    # will change
    runtime_defn['payload'] = _take_expires_out_from_artifacts_in_payload(runtime_defn['payload'])
    graph_defn['task']['payload'] = _take_expires_out_from_artifacts_in_payload(graph_defn['task']['payload'])

    # test all non-ignored key/value pairs in the task defn
    for key, value in graph_defn['task'].items():
        if key in ignore_keys:
            continue
        if value != runtime_defn[key]:
            errors.append("{} {} {} differs!\n graph: {}\n task: {}".format(
                task_link.name, task_link.task_id, key,
                format_json(value), format_json(runtime_defn[key])
            ))
    raise_on_errors(errors, level=level)


def _take_expires_out_from_artifacts_in_payload(payload):
    returned_payload = deepcopy(payload)
    artifacts = returned_payload.get('artifacts', None)
    if artifacts is None:
        return returned_payload
    elif type(artifacts) not in (dict, list):
        raise CoTError('Unsupported type of artifacts. Found: "{}". Expected: dict, list or undefined. Payload: {}'.format(
            type(artifacts), payload
        ))

    artifacts_iterable = artifacts.values() if isinstance(artifacts, dict) else artifacts
    for artifact_definition in artifacts_iterable:
        if isinstance(artifact_definition, dict) and 'expires' in artifact_definition:
            del(artifact_definition['expires'])

    return returned_payload


# verify_link_in_task_graph {{{1
def verify_link_in_task_graph(chain, decision_link, task_link):
    """Compare the runtime task definition against the decision task graph.

    Args:
        chain (ChainOfTrust): the chain we're operating on.
        decision_link (LinkOfTrust): the decision task link
        task_link (LinkOfTrust): the task link we're testing

    Raises:
        CoTError: on failure.

    """
    log.info("Verifying the {} {} task definition is part of the {} {} task graph...".format(
        task_link.name, task_link.task_id, decision_link.name, decision_link.task_id
    ))
    if task_link.task_id in decision_link.task_graph:
        graph_defn = deepcopy(decision_link.task_graph[task_link.task_id])
        verify_task_in_task_graph(task_link, graph_defn)
        log.info("Found {} in the graph; it's a match".format(task_link.task_id))
        return
    raise_on_errors(["Can't find task {} {} in {} {} task-graph.json!".format(
        task_link.name, task_link.task_id, decision_link.name, decision_link.task_id
    )])


# get_pushlog_info {{{1
async def get_pushlog_info(decision_link):
    """Get pushlog info for a decision LinkOfTrust.

    Args:
        decision_link (LinkOfTrust): the decision link to get pushlog info about.

    Returns:
        dict: pushlog info.

    """
    source_env_prefix = decision_link.context.config['source_env_prefix']
    repo = get_repo(decision_link.task, source_env_prefix)
    rev = get_revision(decision_link.task, source_env_prefix)
    context = decision_link.context
    pushlog_url = context.config['pushlog_url'].format(
        repo=repo, revision=rev
    )
    log.info("Pushlog url {}".format(pushlog_url))
    file_path = os.path.join(context.config["work_dir"], "{}_push_log.json".format(decision_link.name))
    pushlog_info = await load_json_or_yaml_from_url(
        context, pushlog_url, file_path, overwrite=False
    )
    if len(pushlog_info['pushes']) != 1:
        log.warning("Pushlog error: expected a single push at {} but got {}!".format(
            pushlog_url, pushlog_info['pushes']
        ))
    return pushlog_info


# get_scm_level {{{1
async def get_scm_level(context, project):
    """Get the scm level for a project from ``projects.yml``.

    We define all known projects in ``projects.yml``. Let's make sure we have
    it populated in ``context``, then return the scm level of ``project``.

    SCM levels are an integer, 1-3, matching Mozilla commit levels.
    https://www.mozilla.org/en-US/about/governance/policies/commit/access-policy/

    Args:
        context (scriptworker.context.Context): the scriptworker context
        project (str): the project to get the scm level for.

    Returns:
        str: the level of the project, as a string.

    """
    await context.populate_projects()
    config = context.projects[project]
    if 'access' in config:
        return config['access'].replace("scm_level_", "")
    elif 'level' in config:
        return str(config['level'])
    else:
        raise ValueError("Can't find level for project {}".format(project))


async def _get_additional_hg_action_jsone_context(parent_link, decision_link):
    params_path = decision_link.get_artifact_full_path('public/parameters.yml')
    parameters = load_json_or_yaml(params_path, is_path=True, file_type='yaml')
    jsone_context = deepcopy(parent_link.task['extra']['action']['context'])
    jsone_context['parameters'] = parameters
    jsone_context['task'] = None
    return jsone_context


async def _get_additional_hgpush_info(decision_link):
    pushlog_info = await get_pushlog_info(decision_link)
    pushlog_id = list(pushlog_info['pushes'].keys())[0]
    push_comment = pushlog_info['pushes'][pushlog_id]['changesets'][0]['desc']
    return {'id': pushlog_id,
            'comment': push_comment,
            'date': pushlog_info['pushes'][pushlog_id]['date'],
            'user': pushlog_info['pushes'][pushlog_id]['user'],
            }


async def _get_additional_hg_push_jsone_context(parent_link, decision_link):
    source_env_prefix = decision_link.context.config['source_env_prefix']
    rev = get_revision(decision_link.task, source_env_prefix)
    pushinfo = await _get_additional_hgpush_info(decision_link)
    push_comment = pushinfo['comment']
    decision_comment = get_commit_message(decision_link.task)
    allowed_comments = [' ']
    # try logic from
    # https://searchfox.org/mozilla-central/rev/795575287259a370d00518098472eaa5b362bfa7/taskcluster/taskgraph/try_option_syntax.py#184-188  # noqa
    if 'try:' in push_comment:
        try_idx = push_comment.index('try:')
        allowed_comments.append(push_comment[try_idx:].split('\n')[0])
    if decision_comment not in allowed_comments:
        raise CoTError(
            "Decision task {} comment doesn't match the push comment!\n"
            "Decision comment: \n{}\nPush comment: \n{}".format(
                decision_link.name, decision_comment, push_comment
            )
        )
    return {
        "push": {
            "revision": rev,
            "comment": decision_comment,
            "owner": pushinfo['user'],
            "pushlog_id": pushinfo['id'],
            "pushdate": pushinfo['date'],
        }
    }


async def _get_additional_github_releases_jsone_context(decision_link):
    context = decision_link.context
    source_env_prefix = context.config['source_env_prefix']
    task = decision_link.task
    repo_url = get_repo(task, source_env_prefix)
    repo_owner, repo_name = extract_github_repo_owner_and_name(repo_url)
    tag_name = get_revision(task, source_env_prefix)

    release_data = GitHubRepository(
        repo_owner, repo_name, context.config['github_oauth_token']
    ).get_release(tag_name)

    # The release data expose by the API[1] is not the same as the original event[2]. That's why
    # we have to rebuild the object manually
    #
    # [1] https://developer.github.com/v3/repos/releases/#get-a-single-release
    # [2] https://developer.github.com/v3/activity/events/types/#releaseevent
    return {
        'event': {
            'action': 'published',
            'repository': {
                'clone_url': "{}.git".format(repo_url),
                'full_name': extract_github_repo_full_name(repo_url),
                'html_url': repo_url,
                'name': repo_name,
            },
            'release': {
                'tag_name': tag_name,
                'target_commitish': release_data['target_commitish'],
                # Releases are supposed to be unique. Therefore, we're able to use the latest
                # timestamp exposed by the API (unlike PRs, for instance)
                'published_at': release_data['published_at'],
            },
            'sender': {
                'login': release_data['author']['login'],
            },
        },
    }


async def _get_additional_git_cron_jsone_context(decision_link):
    source_env_prefix = decision_link.context.config['source_env_prefix']
    task = decision_link.task
    repo = get_repo(task, source_env_prefix)
    _, repo_name = extract_github_repo_owner_and_name(repo)
    revision = get_revision(task, source_env_prefix)
    branch = get_branch(task, source_env_prefix)

    # TODO remove the call to get_triggered_by() once Github repos don't define it anymore.
    user = get_triggered_by(task, source_env_prefix)
    if user is None:
        # We can't default to 'cron' (like in hg) because https://github.com/cron is already taken
        user = 'TaskclusterHook'

    # Different projects have different cron contexts. We provide the union of the two
    # contexts to handle both cases.
    return {
        'cron': load_json_or_yaml(decision_link.task['extra']['cron']),
        # Non-taskgraph Cron context mocks a GitHub release one. However, there
        # is no GitHub API to call since this is built on Taskcluster.
        'event': {
            'repository': {
                # TODO: Append ".git" to clone_url in order to match what GitHub actually provide.
                # This can't be done at the moment because some mobile projects still rely on the
                # bad value
                'clone_url': repo,
                'full_name': extract_github_repo_full_name(repo),
                'html_url': repo,
                'name': repo_name,
            },
            'release': {
                'tag_name': revision,
                'target_commitish': branch,
                'published_at': get_push_date_time(task, source_env_prefix),
            },
            'sender': {
                'login': user,
            },
        },
        # Taskgraph cron contexts mirror hg-push contexts
        'repository': {
            'url': repo,
            'project': repo_name,
            'level': await get_scm_level(decision_link.context, repo_name)
        },
        'push': {
            'revision': revision,
            'branch': branch,
        }
    }


async def _get_additional_github_pull_request_jsone_context(decision_link):
    context = decision_link.context
    source_env_prefix = context.config['source_env_prefix']
    task = decision_link.task
    repo_url = get_repo(task, source_env_prefix)
    repo_owner, repo_name = extract_github_repo_owner_and_name(repo_url)
    pull_request_number = get_pull_request_number(task, source_env_prefix)
    token = context.config['github_oauth_token']

    github_repo = GitHubRepository(repo_owner, repo_name, token)
    repo_definition = github_repo.definition

    if repo_definition['fork']:
        github_repo = GitHubRepository(
            owner=repo_definition['parent']['owner']['login'],
            repo_name=repo_definition['parent']['name'],
            token=token,
        )

    pull_request_data = github_repo.get_pull_request(pull_request_number)
    # Even though pull_request_data['head']['repo']['pushed_at'] does exist,
    # we can't reliably use it because a new commit would update the HEAD data.
    # This becomes a problem if a staging release was kicked off and the PR got
    # updated in the meantime.
    pull_request_data['head']['updated_at'] = get_push_date_time(task, source_env_prefix)

    return {
        'event': {
            # TODO: Expose the action that triggered the graph in payload.env
            'action': 'synchronize',
            'repository': pull_request_data['base']['repo'],
            'pull_request': pull_request_data,
            'sender': {
                'login': pull_request_data['head']['user']['login'],
            },
        },
    }


async def _get_additional_github_push_jsone_context(decision_link):
    context = decision_link.context
    source_env_prefix = context.config['source_env_prefix']
    task = decision_link.task
    repo_url = get_repo(task, source_env_prefix)
    repo_owner, repo_name = extract_github_repo_owner_and_name(repo_url)
    commit_hash = get_revision(task, source_env_prefix)

    commit_data = GitHubRepository(
        repo_owner, repo_name, context.config['github_oauth_token']
    ).get_commit(commit_hash)

    committer_login = commit_data['committer']['login']
    # committer_email = commit_data['commit']['committer']['email']
    # https://github.com/mozilla-releng/scriptworker/issues/334: web-flow is the User used by
    # GitHub to create some commits on github.com or the Github Desktop app. For sure, this user
    # is not the one who triggered a push event. Let's fall back to the author login, instead.
    if committer_login == 'web-flow':
        committer_login = commit_data['author']['login']
        # committer_email = commit_data['commit']['author']['email']

    # Github users can have multiple emails. The commit_data contains
    # their primary email, but the task may contain a secondary email.
    # We may want to verify that this email is one of `login`'s email
    # addresses, but for now we'll just use the task data.
    task_email = task["metadata"]["owner"]

    # The commit data expose by the API[1] is not the same as the original event[2]. That's why
    # we have to rebuild the object manually
    #
    # [1] https://developer.github.com/v3/repos/commits/#get-a-single-commit
    # [2] https://developer.github.com/v3/activity/events/types/#pushevent
    return {
        'event': {
            'after': commit_hash,
            'pusher': {
                'email': task_email,
            },
            'ref': get_branch(task, source_env_prefix),
            'repository': {
                'full_name': extract_github_repo_full_name(repo_url),
                'html_url': repo_url,
                'name': repo_name,
                'pushed_at': get_push_date_time(task, source_env_prefix),
            },
            'sender': {
                'login': committer_login,
            },
        },
    }


async def _get_additional_hg_cron_jsone_context(parent_link, decision_link):
    jsone_context = {}
    source_env_prefix = decision_link.context.config['source_env_prefix']
    rev = get_revision(decision_link.task, source_env_prefix)
    jsone_context['cron'] = load_json_or_yaml(parent_link.task['extra']['cron'])

    pushinfo = await _get_additional_hgpush_info(decision_link)

    # On trees without pushlog support for cron some push values are
    # hardcoded in the .taskcluster.yml
    jsone_context["push"] = {
        "revision": rev,
        "comment": '',
        "owner": 'cron',
        "pushlog_id": pushinfo['id'],
        "pushdate": pushinfo['date'],
    }
    return jsone_context


# populate_jsone_context {{{1
async def populate_jsone_context(chain, parent_link, decision_link, tasks_for):
    """Populate the json-e context to rebuild ``parent_link``'s task definition.

    This defines the context that `.taskcluster.yml` expects to be rendered
    with.  See comments at the top of that file for details.

    Args:
        chain (ChainOfTrust): the chain of trust to add to.
        parent_link (LinkOfTrust): the parent link to test.
        decision_link (LinkOfTrust): the parent link's decision task link.
        tasks_for (str): the reason the parent link was created (cron,
            hg-push, action)

    Raises:
        CoTError, KeyError, ValueError: on failure.

    Returns:
        dict: the json-e context.

    """
    task_ids = {
         "default": parent_link.task_id,
         "decision": decision_link.task_id,
    }
    log.debug("task_ids: {}".format(task_ids))
    jsone_context = {
        'now': parent_link.task['created'],
        'as_slugid': lambda x: task_ids.get(x, task_ids['default']),
        'tasks_for': tasks_for,
        'ownTaskId': parent_link.task_id,
        'taskId': None
    }

    if chain.context.config['cot_product'] in ('mobile', 'application-services', 'xpi'):
        if tasks_for == 'github-release':
            jsone_context.update(
                await _get_additional_github_releases_jsone_context(decision_link)
            )
        elif tasks_for == 'cron':
            jsone_context.update(await _get_additional_git_cron_jsone_context(decision_link))
        elif tasks_for == 'github-pull-request':
            jsone_context.update(
                await _get_additional_github_pull_request_jsone_context(decision_link)
            )
        elif tasks_for == 'github-push':
            jsone_context.update(
                await _get_additional_github_push_jsone_context(decision_link)
            )
        else:
            raise CoTError('Unknown tasks_for "{}" for cot_product "{}"!'.format(tasks_for, chain.context.config['cot_product']))
    else:
        source_url = get_source_url(decision_link)
        project = await get_project(chain.context, source_url)
        jsone_context['repository'] = {
            'url': get_repo(decision_link.task, decision_link.context.config['source_env_prefix']),
            'level': await get_scm_level(chain.context, project),
            'project': project,
        }

        if tasks_for == 'action':
            jsone_context.update(
                await _get_additional_hg_action_jsone_context(parent_link, decision_link)
            )
        elif tasks_for == 'hg-push':
            jsone_context.update(
                await _get_additional_hg_push_jsone_context(parent_link, decision_link)
            )
        elif tasks_for == 'cron':
            jsone_context.update(
                await _get_additional_hg_cron_jsone_context(parent_link, decision_link)
            )
        else:
            raise CoTError("Unknown tasks_for {}!".format(tasks_for))

    log.debug("{} json-e context:".format(parent_link.name))
    # format_json() breaks on lambda values; use pprint.pformat here.
    log.debug(pprint.pformat(jsone_context))
    return jsone_context


# get_in_tree_template {{{1
async def get_in_tree_template(link):
    """Get the in-tree json-e template for a given link.

    By convention, this template is SOURCE_REPO/.taskcluster.yml.

    Args:
        link (LinkOfTrust): the parent link to get the source url from.

    Raises:
        CoTError: on non-yaml `source_url`
        KeyError: on non-well-formed source template

    Returns:
        dict: the first task in the template.

    """
    context = link.context
    source_url = get_source_url(link)
    if not source_url.endswith(('.yml', '.yaml')):
        raise CoTError("{} source url {} doesn't end in .yml or .yaml!".format(
            link.name, source_url
        ))
    tmpl = await load_json_or_yaml_from_url(
        context, source_url, os.path.join(
            context.config["work_dir"], "{}_taskcluster.yml".format(link.name)
        )
    )
    return tmpl


def _get_action_from_actions_json(all_actions, callback_name):
    for defn in all_actions:
        if defn.get('kind') == 'hook':
            if defn.get('hookPayload', {}).get('decision', {}).get('action', {}).get('cb_name') == callback_name:
                return defn
        elif defn.get('kind') == 'task':
            if defn.get('task', {}).get('$let', {}).get('action', {}).get('cb_name') == callback_name:
                return defn
        else:
            raise CoTError('Unknown action kind `{kind}` for action `{name}`.'.format(
                kind=defn.get('kind', '<MISSING>'),
                name=defn.get('name', '<MISSING>'),
            ))
    raise CoTError('No action with {} callback found.'.format(callback_name))


def _wrap_action_hook_with_let(tmpl, action_perm):
    """Construct the hook task template body.

    Given the content of .taskcluster.yml, construct the task template that
    would appear in the corresponding hook definition.  This is an attempt to
    duplicate the logic here:
    https://hg.mozilla.org/ci/ci-admin/file/edad9f8/ciadmin/generate/in_tree_actions.py#l154

    """
    return {
        '$let': {
            'tasks_for': 'action',
            'action': {
                'name': '${payload.decision.action.name}',
                'title': '${payload.decision.action.title}',
                'description': '${payload.decision.action.description}',
                'taskGroupId': '${payload.decision.action.taskGroupId}',
                'symbol': '${payload.decision.action.symbol}',

                'repo_scope': 'assume:repo:${payload.decision.repository.url[8:]}:action:' + action_perm,

                'cb_name': '${payload.decision.action.cb_name}',
            },

            'push': {'$eval': 'payload.decision.push'},
            'repository': {'$eval': 'payload.decision.repository'},
            'input': {'$eval': 'payload.user.input'},
            'parameters': {'$eval': "payload.decision['parameters']"},

            'taskId': {'$eval': 'payload.user.taskId'},
            'taskGroupId': {'$eval': 'payload.user.taskGroupId'},
            'ownTaskId': {'$eval': 'taskId'},
        },
        'in': tmpl,
    }


def _render_action_hook_payload(action_defn, action_context, action_task):
    hook_payload = action_defn['hookPayload']
    context = {
        'input': action_context['input'],
        'parameters': action_context.get('parameters'),
        'taskGroupId': action_task.decision_task_id,
        'taskId': action_context['taskId'],
    }
    return jsone.render(hook_payload, context)


def _get_action_perm(action_defn):
    # handle either the preferred action.extra.actionPerm or the older action.actionPerm
    action_perm = action_defn.get('extra', {}).get('actionPerm', action_defn.get('actionPerm'))
    if action_perm is None:
        if 'generic/' in action_defn.get('hookId', 'generic/'):
            action_perm = 'generic'
        else:
            action_perm = action_defn['hookPayload']['decision']['action']['cb_name']
    return action_perm


async def get_action_context_and_template(chain, parent_link, decision_link):
    """Get the appropriate json-e context and template for an action task.

    Args:
        chain (ChainOfTrust): the chain of trust.
        parent_link (LinkOfTrust): the parent link to test.
        decision_link (LinkOfTrust): the parent link's decision task link.
        tasks_for (str): the reason the parent link was created (cron,
            hg-push, action)

    Returns:
        (dict, dict): the json-e context and template.

    """
    actions_path = decision_link.get_artifact_full_path('public/actions.json')
    all_actions = load_json_or_yaml(actions_path, is_path=True)['actions']
    action_name = get_action_callback_name(parent_link.task)
    action_defn = _get_action_from_actions_json(all_actions, action_name)
    jsone_context = await populate_jsone_context(chain, parent_link, decision_link, "action")
    if 'task' in action_defn and chain.context.config['min_cot_version'] <= 2:
        tmpl = {'tasks': [action_defn['task']]}
    elif action_defn.get('kind') == 'hook':
        # action-hook.
        in_tree_tmpl = await get_in_tree_template(decision_link)
        action_perm = _get_action_perm(action_defn)
        tmpl = _wrap_action_hook_with_let(in_tree_tmpl, action_perm)

        # define the JSON-e context with which the hook's task template was
        # rendered, defined at
        # https://docs.taskcluster.net/docs/reference/core/taskcluster-hooks/docs/firing-hooks#triggerhook
        # This is created by working backward from the json-e context the
        # .taskcluster.yml expects
        jsone_context = {
            'payload': _render_action_hook_payload(
                action_defn, jsone_context, parent_link
            ),
            'taskId': parent_link.task_id,
            'now': jsone_context['now'],
            'as_slugid': jsone_context['as_slugid'],
            'clientId': jsone_context.get('clientId'),
        }
    elif action_defn.get('kind') == 'task':
        # XXX Get rid of this block when all actions are hooks
        tmpl = await get_in_tree_template(decision_link)
        for k in ('action', 'push', 'repository'):
            jsone_context[k] = deepcopy(action_defn['hookPayload']['decision'].get(k, {}))
        jsone_context['action']['repo_scope'] = get_repo_scope(parent_link.task, parent_link.name)
    else:
        raise CoTError('Unknown action kind `{kind}` for action `{name}`.'.format(
            kind=action_defn.get('kind', '<MISSING>'),
            name=action_defn.get('name', '<MISSING>'),
        ))

    return jsone_context, tmpl


# get_jsone_context_and_template {{{1
async def get_jsone_context_and_template(chain, parent_link, decision_link, tasks_for):
    """Get the appropriate json-e context and template for any parent task.

    Args:
        chain (ChainOfTrust): the chain of trust.
        parent_link (LinkOfTrust): the parent link to test.
        decision_link (LinkOfTrust): the parent link's decision task link.
        tasks_for (str): the reason the parent link was created (cron,
            hg-push, action)

    Returns:
        (dict, dict): the json-e context and template.

    """
    if tasks_for == 'action':
        jsone_context, tmpl = await get_action_context_and_template(
            chain, parent_link, decision_link
        )
    else:
        tmpl = await get_in_tree_template(decision_link)
        jsone_context = await populate_jsone_context(
            chain, parent_link, decision_link, tasks_for
        )
    return jsone_context, tmpl


# verify_parent_task_definition {{{1
async def verify_parent_task_definition(chain, parent_link):
    """Rebuild the decision/action/cron task definition via json-e.

    This is Chain of Trust verification version 2, aka cotv2.
    Instead of looking at various parts of the parent task's task definition
    and making sure they look well-formed, let's rebuild the task definition
    from the tree and make sure it matches.

    We use the the link with the ``task_id`` of ``parent_link.decision_task_id``
    for a number of the checks here. If ``parent_link`` is a decision or cron
    task, they're the same task. If ``parent_link`` is an action task, this
    will reference the decision task that the action task is based off of.

    Args:
        chain (ChainOfTrust): the chain of trust to add to.
        parent_link (LinkOfTrust): the parent link to test.

    Raises:
        CoTError: on failure.

    """
    log.info("Verifying {} {} definition...".format(parent_link.name, parent_link.task_id))
    decision_link = chain.get_link(parent_link.decision_task_id)
    try:
        tasks_for = get_and_check_tasks_for(
            chain.context,
            parent_link.task,
            '{} {}: '.format(parent_link.name, parent_link.task_id)
        )
        jsone_context, tmpl = await get_jsone_context_and_template(
            chain, parent_link, decision_link, tasks_for
        )
        rebuilt_definitions = jsone.render(tmpl, jsone_context)
        if tasks_for == 'action':
            check_and_update_action_task_group_id(parent_link, decision_link, rebuilt_definitions)
    except jsone.JSONTemplateError as e:
        log.exception("JSON-e error while rebuilding {} task definition!".format(parent_link.name))
        raise CoTError("JSON-e error while rebuilding {} task definition: {}".format(parent_link.name, str(e)))
    except (KeyError, ValueError) as e:
        msg = "Error while rebuilding {} {} task definition!".format(
            parent_link.name, parent_link.task_id
        )
        log.exception(msg)
        raise CoTError(msg + "\n{}".format(str(e)))

    compare_jsone_task_definition(parent_link, rebuilt_definitions)


# check_and_update_action_task_group_id {{{1
def check_and_update_action_task_group_id(parent_link, decision_link, rebuilt_definitions):
    """Update the ``ACTION_TASK_GROUP_ID`` of an action after verifying.

    Actions have varying ``ACTION_TASK_GROUP_ID`` behavior.  Release Promotion
    action tasks set the ``ACTION_TASK_GROUP_ID`` to match the action ``taskId``
    so the large set of release tasks have their own taskgroup. Non-relpro
    action tasks set the ``ACTION_TASK_GROUP_ID`` to match the decision
    ``taskId``, so tasks are more discoverable inside the original on-push
    taskgroup.

    This poses a json-e task definition problem, hence this function.

    This function first checks to make sure the ``ACTION_TASK_GROUP_ID`` is
    a member of ``{action_task_id, decision_task_id}``. Then it makes sure
    the ``ACTION_TASK_GROUP_ID`` in the ``rebuilt_definition`` is set to the
    ``parent_link.task``'s ``ACTION_TASK_GROUP_ID`` so the json-e comparison
    doesn't fail out.

    Ideally, we want to obsolete and remove this function.

    Args:
        parent_link (LinkOfTrust): the parent link to test.
        decision_link (LinkOfTrust): the decision link to test.
        rebuilt_definitions (dict): the rebuilt definitions to check and update.

    Raises:
        CoTError: on failure.

    """
    rebuilt_gid = rebuilt_definitions['tasks'][0]['payload']['env']['ACTION_TASK_GROUP_ID']
    runtime_gid = parent_link.task['payload']['env']['ACTION_TASK_GROUP_ID']
    acceptable_gids = {parent_link.task_id, decision_link.task_id}
    if rebuilt_gid not in acceptable_gids:
        raise CoTError("{} ACTION_TASK_GROUP_ID {} not in {}!".format(
            parent_link.name, rebuilt_gid, acceptable_gids
        ))
    if runtime_gid != rebuilt_gid:
        log.debug("runtime gid {} rebuilt gid {}".format(runtime_gid, rebuilt_gid))
    rebuilt_definitions['tasks'][0]['payload']['env']['ACTION_TASK_GROUP_ID'] = runtime_gid


# compare_jsone_task_definition {{{1
def compare_jsone_task_definition(parent_link, rebuilt_definitions):
    """Compare the json-e rebuilt task definition vs the runtime definition.

    Args:
        parent_link (LinkOfTrust): the parent link to test.
        rebuilt_definitions (dict): the rebuilt task definitions.

    Raises:
        CoTError: on failure.

    """
    diffs = []
    for compare_definition in rebuilt_definitions['tasks']:
        # Rebuilt decision tasks have an extra `taskId`; remove
        if 'taskId' in compare_definition:
            del(compare_definition['taskId'])
        # remove key/value pairs where the value is empty, since json-e drops
        # them instead of keeping them with a None/{}/[] value.
        compare_definition = remove_empty_keys(compare_definition)
        runtime_definition = remove_empty_keys(parent_link.task)

        diff = list(dictdiffer.diff(compare_definition, runtime_definition))
        if diff:
            diffs.append(pprint.pformat(diff))
            continue
        log.info("{}: Good.".format(parent_link.name))
        break
    else:
        error_msg = "{} {}: the runtime task doesn't match any rebuilt definition!\n{}".format(
            parent_link.name, parent_link.task_id, pprint.pformat(diffs)
        )
        log.critical(error_msg)
        raise CoTError(error_msg)


# verify_parent_task {{{1
async def verify_parent_task(chain, link):
    """Verify the parent task Link.

    Action task verification is currently in the same verification function as
    decision tasks, because sometimes we'll have an action task masquerading as
    a decision task, e.g. in templatized actions for release graphs. To make
    sure our guess of decision or action task isn't fatal, we call this
    function; this function uses ``is_action()`` to determine how to verify
    the task.

    Args:
        chain (ChainOfTrust): the chain we're operating on.
        link (LinkOfTrust): the task link we're checking.

    Raises:
        CoTError: on chain of trust verification error.

    """
    pool = get_worker_pool_id(link.task)
    if pool not in chain.context.config['valid_decision_worker_pools']:
        raise CoTError("{} is not a valid decision workerPoolId!".format(pool))
    if chain is not link:
        # make sure all tasks generated from this parent task match the published
        # task-graph.json. Not applicable if this link is the ChainOfTrust object,
        # since this task won't have generated a task-graph.json yet.
        path = link.get_artifact_full_path('public/task-graph.json')
        if not os.path.exists(path):
            raise CoTError("{} {}: {} doesn't exist!".format(link.name, link.task_id, path))
        link.task_graph = load_json_or_yaml(
            path, is_path=True, exception=CoTError, message="Can't load {}! %(exc)s".format(path)
        )
        # This check may want to move to a per-task check?
        for target_link in chain.get_all_links_in_chain():
            # Verify the target's task is in the parent task's task graph, unless
            # it's this task or a parent task.
            # (Decision tasks will not exist in a parent task's task-graph.json;
            #  action tasks, which are generated later, will also be missing.)
            # https://github.com/mozilla-releng/scriptworker/issues/77
            if target_link.parent_task_id == link.task_id and \
                    target_link.task_id != link.task_id and \
                    target_link.task_type not in PARENT_TASK_TYPES:
                verify_link_in_task_graph(chain, link, target_link)
    try:
        await verify_parent_task_definition(chain, link)
    except (BaseDownloadError, KeyError) as e:
        raise CoTError(e)


# verify_build_task {{{1
async def verify_build_task(chain, link):
    """Verify the build Link.

    The main points of concern are tested elsewhere:
    The task is the same as the task graph task; the command;
    the docker-image for docker-worker builds; the revision and repo.

    Args:
        chain (ChainOfTrust): the chain we're operating on.
        link (LinkOfTrust): the task link we're checking.

    """
    pass


# verify_docker_image_task {{{1
async def verify_docker_image_task(chain, link):
    """Verify the docker image Link.

    Args:
        chain (ChainOfTrust): the chain we're operating on.
        link (LinkOfTrust): the task link we're checking.

    """
    errors = []
    # workerType
    worker_pool = get_worker_pool_id(link.task)
    if worker_pool not in chain.context.config['valid_docker_image_worker_pools']:
        errors.append("{} is not a valid docker-image workerPoolId!".format(worker_pool))
    raise_on_errors(errors)


# verify_partials_task {{{1
async def verify_partials_task(chain, obj):
    """Verify the partials trust object.

    The main points of concern are tested elsewhere:
    Runs as a docker-worker.

    Args:
        chain (ChainOfTrust): the chain we're operating on
        obj (ChainOfTrust or LinkOfTrust): the trust object for the balrog task.

    Raises:
        CoTError: on error.

    """
    pass


# verify_task_types {{{1
async def verify_task_types(chain):
    """Verify the task type (e.g. decision, build) of each link in the chain.

    Args:
        chain (ChainOfTrust): the chain we're operating on

    Returns:
        dict: mapping task type to the number of links.

    """
    valid_task_types = get_valid_task_types()
    task_count = {}
    for obj in chain.get_all_links_in_chain():
        task_type = obj.task_type
        log.info("Verifying {} {} as a {} task...".format(obj.name, obj.task_id, task_type))
        task_count.setdefault(task_type, 0)
        task_count[task_type] += 1
        # Run tests synchronously for now.  We can parallelize if efficiency
        # is more important than a single simple logfile.
        await valid_task_types[task_type](chain, obj)
    return task_count


# verify_docker_worker_task {{{1
async def verify_docker_worker_task(chain, link):
    """Docker-worker specific checks.

    Args:
        chain (ChainOfTrust): the chain we're operating on
        link (ChainOfTrust or LinkOfTrust): the trust object for the signing task.

    Raises:
        CoTError: on failure.

    """
    if chain != link:
        # These two checks will die on `link.cot` if `link` is a ChainOfTrust
        # object (e.g., the task we're running `verify_cot` against is a
        # docker-worker task). So only run these tests if they are not the chain
        # object.
        check_interactive_docker_worker(link)
        verify_docker_image_sha(chain, link)


# verify_generic_worker_task {{{1
async def verify_generic_worker_task(chain, link):
    """generic-worker specific checks.

    Args:
        chain (ChainOfTrust): the chain we're operating on
        link (ChainOfTrust or LinkOfTrust): the trust object for the signing task.

    Raises:
        CoTError: on failure.

    """
    pass


# verify_scriptworker_task {{{1
async def verify_scriptworker_task(chain, obj):
    """Verify the signing trust object.

    Currently the only check is to make sure it was run on a scriptworker.

    Args:
        chain (ChainOfTrust): the chain we're operating on
        obj (ChainOfTrust or LinkOfTrust): the trust object for the signing task.

    """
    errors = []
    if obj.worker_impl != "scriptworker":
        errors.append("{} {} must be run from scriptworker!".format(obj.name, obj.task_id))
    raise_on_errors(errors)


# verify_worker_impls {{{1
async def verify_worker_impls(chain):
    """Verify the task type (e.g. decision, build) of each link in the chain.

    Args:
        chain (ChainOfTrust): the chain we're operating on

    Raises:
        CoTError: on failure

    """
    valid_worker_impls = get_valid_worker_impls()
    for obj in chain.get_all_links_in_chain():
        worker_impl = obj.worker_impl
        log.info("Verifying {} {} as a {} task...".format(obj.name, obj.task_id, worker_impl))
        # Run tests synchronously for now.  We can parallelize if efficiency
        # is more important than a single simple logfile.
        await valid_worker_impls[worker_impl](chain, obj)


# get_source_url {{{1
def verify_repo_matches_url(repo, url):
    """Verify ``url`` is a part of ``repo``.

    We were using ``startswith()`` for a while, which isn't a good comparison.
    This function allows us to ``urlparse`` and compare host and path.

    Args:
        repo (str): the repo url
        url (str): the url to verify is part of the repo

    Returns:
        bool: ``True`` if the repo matches the url.

    """
    repo_parts = urlparse(repo)
    url_parts = urlparse(url)
    errors = []
    repo_path_parts = repo_parts.path.split('/')
    url_path_parts = url_parts.path.split('/')
    if repo_parts.hostname != url_parts.hostname:
        errors.append("verify_repo_matches_url: Hostnames don't match! {} {}".format(
            repo_parts.hostname, url_parts.hostname
        ))
    if not url_parts.path.startswith(repo_parts.path) or \
            url_path_parts[:len(repo_path_parts)] != repo_path_parts:
        errors.append("verify_repo_matches_url: Paths don't match! {} {}".format(
            repo_parts.path, url_parts.path
        ))
    if errors:
        log.warning("\n".join(errors))
        return False
    return True


def get_source_url(obj):
    """Get the source url for a Trust object.

    Args:
        obj (ChainOfTrust or LinkOfTrust): the trust object to inspect

    Raises:
        CoTError: if repo and source are defined and don't match

    Returns:
        str: the source url.

    """
    source_env_prefix = obj.context.config['source_env_prefix']
    task = obj.task
    log.debug("Getting source url for {} {}...".format(obj.name, obj.task_id))
    repo = get_repo(obj.task, source_env_prefix=source_env_prefix)
    source = task['metadata']['source']
    if repo and not verify_repo_matches_url(repo, source):
        raise CoTError("{name} {task_id}: {source_env_prefix} {repo} doesn't match source {source}!".format(
            name=obj.name, task_id=obj.task_id, source_env_prefix=source_env_prefix, repo=repo, source=source
        ))
    log.info("{} {}: found {}".format(obj.name, obj.task_id, source))
    return source


# trace_back_to_tree {{{1
async def trace_back_to_tree(chain):
    """Trace the chain back to the tree.

    task.metadata.source: "https://hg.mozilla.org/projects/date//file/a80373508881bfbff67a2a49297c328ff8052572/taskcluster/ci/build"
    task.payload.env.GECKO_HEAD_REPOSITORY "https://hg.mozilla.org/projects/date/"

    Args:
        chain (ChainOfTrust): the chain we're operating on

    Raises:
        CoTError: on error.

    """
    errors = []
    repos = {}
    rules = {}
    for my_key, config_key in {
        'scopes': 'cot_restricted_scopes',
        'trees': 'cot_restricted_trees'
    }.items():
        rules[my_key] = chain.context.config[config_key]

    restricted_privs = chain.has_restricted_scopes()

    # a repo_path of None means we have no restricted privs.
    # a string repo_path may mean we have higher privs
    for obj in [chain] + chain.links:
        source_url = get_source_url(obj)
        repo_path = match_url_regex(
            chain.context.config['trusted_vcs_rules'], source_url, match_url_path_callback
        )
        repos[obj] = repo_path
    # check for restricted scopes.
    my_repo = repos[chain]
    for scope in chain.task['scopes']:
        if scope in rules['scopes']:
            log.info("Found privileged scope {}".format(scope))
            level = rules['scopes'][scope]
            if my_repo not in rules['trees'][level]:
                errors.append("{} {}: repo {} not allowlisted for scope {}!".format(
                    chain.name, chain.task_id, my_repo, scope
                ))
    # verify all tasks w/ same decision_task_id have the same source repo.
    if len(set(repos.values())) > 1:
        for obj, repo in repos.items():
            if obj.decision_task_id == chain.decision_task_id:
                if repo != my_repo:
                    errors.append("{} {} repo {} doesn't match my repo {}!".format(
                        obj.name, obj.task_id, repo, my_repo
                    ))
            # if we have restricted privs, the non-sibling tasks must at least be in
            # a known repo.
            # (Not currently requiring that all tasks have the same privilege level,
            #  in case a docker-image build is run on mozilla-central and that image
            #  is used for a release-priv task, for example.)
            elif restricted_privs and repo is None:
                errors.append("{} {} has no privileged repo on an restricted privilege scope!".format(
                    obj.name, obj.task_id
                ))
    # Disallow restricted privs on is_try_or_pull_request.  This may be a redundant check.
    if restricted_privs and await chain.is_try_or_pull_request():
        errors.append(
            "{} {} has restricted privilege scope, and is_try_or_pull_request()!".format(
                chain.name, chain.task_id
            )
        )
    raise_on_errors(errors)


# AuditLogFormatter {{{1
class AuditLogFormatter(logging.Formatter):
    """Format the chain of trust log."""

    def format(self, record):
        """Space debug messages for more legibility."""
        if record.levelno == logging.DEBUG:
            record.msg = ' {}'.format(record.msg)
        return super(AuditLogFormatter, self).format(record)


# verify_chain_of_trust {{{1
async def verify_chain_of_trust(chain):
    """Build and verify the chain of trust.

    Args:
        chain (ChainOfTrust): the chain we're operating on

    Raises:
        CoTError: on failure

    """
    log_path = os.path.join(chain.context.config["task_log_dir"], "chain_of_trust.log")
    scriptworker_log = logging.getLogger('scriptworker')
    with contextual_log_handler(
        chain.context, path=log_path, log_obj=scriptworker_log,
        formatter=AuditLogFormatter(
            fmt=chain.context.config['log_fmt'],
            datefmt=chain.context.config['log_datefmt'],
        )
    ):
        log.info("Running scriptworker version {}".format(__version_string__))
        try:
            # build LinkOfTrust objects
            await build_task_dependencies(chain, chain.task, chain.name, chain.task_id)
            # download the signed chain of trust artifacts
            await download_cot(chain)
            # verify the signatures and populate the ``link.cot``s
            verify_cot_signatures(chain)
            # download all other artifacts needed to verify chain of trust
            await download_cot_artifacts(chain)
            # verify the task types, e.g. decision
            await verify_task_types(chain)
            # verify the worker_impls, e.g. docker-worker
            await verify_worker_impls(chain)
            await trace_back_to_tree(chain)
        except (BaseDownloadError, KeyError, TypeError, AttributeError) as exc:
            log.critical("Chain of Trust verification error!", exc_info=True)
            if isinstance(exc, CoTError):
                raise
            else:
                raise CoTError(str(exc))
        log.info("Good.")


# verify_cot_cmdln {{{1
async def _async_verify_cot_cmdln(opts, tmp):
    async with aiohttp.ClientSession() as session:
        context = Context()
        context.session = session
        context.config = dict(deepcopy(DEFAULT_CONFIG))
        context.credentials = read_worker_creds()
        context.queue = context.queue or Queue(
            session=session,
            options={
                'rootUrl': os.environ.get('TASKCLUSTER_ROOT_URL', 'https://taskcluster.net'),
            },
        )
        context.task = await context.queue.task(opts.task_id)
        context.config.update({
            'cot_product': opts.cot_product,
            'work_dir': os.path.join(tmp, 'work'),
            'artifact_dir': os.path.join(tmp, 'artifacts'),
            'task_log_dir': os.path.join(tmp, 'artifacts', 'public', 'logs'),
            'verify_cot_signature': opts.verify_sigs,
        })
        context.config = apply_product_config(context.config)
        cot = ChainOfTrust(context, opts.task_type, task_id=opts.task_id)
        await verify_chain_of_trust(cot)
        log.info(format_json(cot.dependent_task_ids()))
        log.info("{} : {}".format(cot.name, cot.task_id))
        for link in cot.links:
            log.info("{} : {}".format(link.name, link.task_id))


def verify_cot_cmdln(args=None, event_loop=None):
    """Test the chain of trust from the commandline, for debugging purposes.

    Args:
        args (list, optional): the commandline args to parse.  If None, use
            ``sys.argv[1:]`` .  Defaults to None.

        event_loop (asyncio.events.AbstractEventLoop): the event loop to use.
            If ``None``, use ``asyncio.get_event_loop()``. Defaults to ``None``.

    """
    args = args or sys.argv[1:]
    parser = argparse.ArgumentParser(
        description="""Verify a given task's chain of trust.

Given a task's `task_id`, get its task definition, then trace its chain of
trust back to the tree.  This doesn't verify chain of trust artifact signatures,
but does run the other tests in `scriptworker.cot.verify.verify_chain_of_trust`.

This is helpful in debugging chain of trust changes or issues.

To use, first either set your taskcluster creds in your env http://bit.ly/2eDMa6N
or in the CREDS_FILES http://bit.ly/2fVMu0A""")
    parser.add_argument('task_id', help='the task id to test')
    parser.add_argument('--task-type', help='the task type to test',
                        choices=sorted(get_valid_task_types().keys()), required=True)
    parser.add_argument('--cleanup', help='clean up the temp dir afterwards',
                        dest='cleanup', action='store_true', default=False)
    parser.add_argument('--cot-product', help='the product type to test', default='firefox')
    parser.add_argument('--verify-sigs', help='enable signature verification', action='store_true', default=False)
    opts = parser.parse_args(args)
    tmp = tempfile.mkdtemp()
    log = logging.getLogger('scriptworker')
    log.setLevel(logging.DEBUG)
    logging.basicConfig()
    event_loop = event_loop or asyncio.get_event_loop()
    try:
        event_loop.run_until_complete(_async_verify_cot_cmdln(opts, tmp))
    finally:
        if opts.cleanup:
            rm(tmp)
        else:
            log.info("Artifacts are in {}".format(tmp))


# create_test_workdir {{{1
async def _async_create_test_workdir(task_id, path, queue=None):
    async with aiohttp.ClientSession() as session:
        context = Context()
        context.session = session
        context.config = dict(deepcopy(DEFAULT_CONFIG))
        context.credentials = read_worker_creds()
        context.queue = queue or context.queue or Queue(
            session=session,
            options={
                'rootUrl': os.environ.get('TASKCLUSTER_ROOT_URL', 'https://taskcluster.net'),
            },
        )
        context.task = await context.queue.task(task_id)
        work_dir = os.path.abspath(path)
        context.config.update({
            'work_dir': work_dir,
            'artifact_dir': os.path.join(work_dir, 'artifacts'),
            'task_log_dir': os.path.join(work_dir, 'artifacts', 'public', 'logs'),
            'verify_cot_signature': False,
        })
        context.config = apply_product_config(context.config)
        write_to_file(os.path.join(work_dir, "task.json"), context.task, file_type='json')
        # we could add chain-of-trust.json and verify sha
        for upstream_artifact in context.task["payload"]["upstreamArtifacts"]:
            task_id = upstream_artifact["taskId"]
            for path in upstream_artifact["paths"]:
                parent_dir = os.path.join(work_dir, "cot", task_id)
                url = get_artifact_url(context, task_id, path)
                loggable_url = get_loggable_url(url)
                log.info("Downloading artifact:\n{}".format(loggable_url))
                await download_artifacts(
                    context, [url], parent_dir=parent_dir,
                    valid_artifact_task_ids=[task_id]
                )


def create_test_workdir(args=None, event_loop=None):
    """Create a test workdir, for manual testing purposes.

    Args:
        args (list, optional): the commandline args to parse.  If None, use
            ``sys.argv[1:]`` .  Defaults to None.

        event_loop (asyncio.events.AbstractEventLoop): the event loop to use.
            If ``None``, use ``asyncio.get_event_loop()``. Defaults to ``None``.

    """
    args = args or sys.argv[1:]
    parser = argparse.ArgumentParser(
        description="""Populate a test `work_dir`.

Given a scriptworker task's `task_id`, get its task definition, write it to
`./work/task.json`, then download its `upstreamArtifacts` and put them in
`./work/cot/TASK_ID/PATH`.

This is helpful in manually testing a *script run.""")
    parser.add_argument('--path', help='relative path to the work_dir', default='work')
    parser.add_argument('--overwrite', help='overwrite an existing work_dir',
                        action='store_true')
    parser.add_argument('task_id', help='the task id to test')
    opts = parser.parse_args(args)

    log = logging.getLogger('scriptworker')
    log.setLevel(logging.DEBUG)
    logging.basicConfig()
    if os.path.exists(opts.path):
        if not opts.overwrite:
            log.critical("Scriptworker will not delete %s without --overwrite!", opts.path)
            sys.exit(1)
        rm(opts.path)
    makedirs(opts.path)
    event_loop = event_loop or asyncio.get_event_loop()
    event_loop.run_until_complete(_async_create_test_workdir(opts.task_id, opts.path))
    log.info("Done.")
