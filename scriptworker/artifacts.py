"""Scriptworker artifact-related operations.

Importing this script updates the mimetypes database. This maps some known extensions to text/plain for a better storage
in S3.

"""
import aiohttp
import arrow
import asyncio
import async_timeout
import gzip
import logging
import mimetypes
import os

from scriptworker.client import validate_artifact_url
from scriptworker.context import TaskContext
from scriptworker.exceptions import DownloadError, ScriptWorkerRetryException, ScriptWorkerTaskException
from scriptworker.task import get_task_id, get_run_id, get_decision_task_id
from scriptworker.utils import (
    add_enumerable_item_to_dict,
    download_file,
    get_loggable_url,
    raise_future_exceptions,
    retry_async,
)


log = logging.getLogger(__name__)


_GZIP_SUPPORTED_CONTENT_TYPE = ('text/plain', 'application/json', 'text/html', 'application/xml')


_EXTENSION_TO_MIME_TYPE = {
    # do not use gzip encoding for .tar.gz or .tgz, or we'll gunzip while
    # downloading, breaking cot verification
    '.tar.gz': ('application/x-tar', None),
    '.tgz': ('application/x-tar', None),
    '.txt': ('text/plain', None),
    '.dmg': ('application/x-apple-diskimage', None),
    '.log': ('text/plain', None),
    '.asc': ('text/plain', None),
    '.diff': ('text/plain', None),
    '.xml': ('application/xml', None),
}


async def upload_artifacts(task_context: TaskContext, files: list):
    """Compress and upload the requested files from ``artifact_dir``, preserving relative paths.

    Compression only occurs with files known to be supported.

    This function expects the directory structure in ``artifact_dir`` to remain
    the same.  So if we want the files in ``public/...``, create an
    ``artifact_dir/public`` and put the files in there.

    Args:
        task_context (TaskContext): the task context.
        files (list of str): files that should be uploaded as artifacts

    Raises:
        Exception: any exceptions the tasks raise.

    """
    def to_upload_future(target_path):
        path = os.path.join(task_context.artifact_dir, target_path)
        content_type, content_encoding = compress_artifact_if_supported(path)
        return asyncio.ensure_future(retry_create_artifact(
            task_context,
            path,
            target_path=target_path,
            content_type=content_type,
            content_encoding=content_encoding,
        ))

    tasks = list(map(to_upload_future, files))
    await raise_future_exceptions(tasks)


def compress_artifact_if_supported(artifact_path):
    """Compress artifacts with GZip if they're known to be supported.

    This replaces the artifact given by a gzip binary.

    Args:
        artifact_path (str): the path to compress

    Returns:
        content_type, content_encoding (tuple):  Type and encoding of the file. Encoding equals 'gzip' if compressed.

    """
    content_type, encoding = guess_content_type_and_encoding(artifact_path)
    log.debug('"{}" is encoded with "{}" and has mime/type "{}"'.format(artifact_path, encoding, content_type))

    if encoding is None and content_type in _GZIP_SUPPORTED_CONTENT_TYPE:
        log.info('"{}" can be gzip\'d. Compressing...'.format(artifact_path))
        with open(artifact_path, 'rb') as f_in:
            text_content = f_in.read()

        with gzip.open(artifact_path, 'wb') as f_out:
            f_out.write(text_content)

        encoding = 'gzip'
        log.info('"{}" compressed'.format(artifact_path))
    else:
        log.debug('"{}" is not supported for compression.'.format(artifact_path))

    return content_type, encoding


def guess_content_type_and_encoding(path):
    """Guess the content type of a path, using ``mimetypes``.

    Falls back to "application/binary" if no content type is found.

    Args:
        path (str): the path to guess the mimetype of

    Returns:
        str: the content type of the file

    """
    for ext, content_type in _EXTENSION_TO_MIME_TYPE.items():
        if path.endswith(ext):
            return content_type

    content_type, encoding = mimetypes.guess_type(path)
    content_type = content_type or "application/binary"
    return content_type, encoding


# retry_create_artifact {{{1
async def retry_create_artifact(*args, **kwargs):
    """Retry create_artifact() calls.

    Args:
        *args: the args to pass on to create_artifact
        **kwargs: the args to pass on to create_artifact

    """
    await retry_async(
        create_artifact,
        retry_exceptions=(
            ScriptWorkerRetryException,
            aiohttp.ClientError
        ),
        args=args,
        kwargs=kwargs
    )


# create_artifact {{{1
async def create_artifact(task_context: TaskContext, path: str,
                          target_path: str, content_type: str,
                          content_encoding: str, storage_type='s3',
                          expires=None):
    """Create an artifact and upload it.

    This should support s3 and azure out of the box; we'll need some tweaking
    if we want to support redirect/error artifacts.

    Args:
        task_context (TaskContext): the task context.
        path (str): the path of the file to upload.
        target_path (str):
        content_type (str): Content type (MIME type) of the artifact. Values can be found via
            scriptworker.artifacts.guess_content_type_and_encoding()
        content_encoding (str): Encoding (per mimetypes' library) of the artifact. None is for no encoding. Values can
            be found via scriptworker.artifacts.guess_content_type_and_encoding()
        storage_type (str, optional): the taskcluster storage type to use.
            Defaults to 's3'
        expires (str, optional): datestring of when the artifact expires.
            Defaults to None.

    Raises:
        ScriptWorkerRetryException: on failure.

    """
    payload = {
        "storageType": storage_type,
        "expires": expires or get_expiration_arrow(task_context).isoformat(),
        "contentType": content_type,
    }
    args = [get_task_id(task_context.claim_task), get_run_id(task_context.claim_task),
            target_path, payload]

    tc_response = await task_context.queue.createArtifact(*args)
    skip_auto_headers = [aiohttp.hdrs.CONTENT_TYPE]
    loggable_url = get_loggable_url(tc_response['putUrl'])
    log.info("uploading {path} to {url}...".format(path=path, url=loggable_url))
    with open(path, "rb") as fh:
        async with async_timeout.timeout(task_context.config['artifact_upload_timeout']):
            async with task_context.session.put(
                tc_response['putUrl'], data=fh, headers=_craft_artifact_put_headers(content_type, content_encoding),
                skip_auto_headers=skip_auto_headers, compress=False
            ) as resp:
                log.info("create_artifact {}: {}".format(path, resp.status))
                response_text = await resp.text()
                log.info(response_text)
                if resp.status not in (200, 204):
                    raise ScriptWorkerRetryException(
                        "Bad status {}".format(resp.status),
                    )


def _craft_artifact_put_headers(content_type, encoding=None):
    log.debug('{} {}'.format(content_type, encoding))
    headers = {
        aiohttp.hdrs.CONTENT_TYPE: content_type,
    }

    if encoding is not None:
        headers[aiohttp.hdrs.CONTENT_ENCODING] = encoding

    return headers


# get_artifact_url {{{1
def get_artifact_url(task_context: TaskContext, task_id: str, path: str):
    """Get a TaskCluster artifact url.

    Args:
        task_context (TaskContext): the task context
        task_id (str): the task id of the task that published the artifact
        path (str): the relative path of the artifact

    Returns:
        str: the artifact url

    Raises:
        TaskClusterFailure: on failure.

    """
    if path.startswith("public/"):
        url = task_context.queue.buildUrl('getLatestArtifact', task_id, path)
    else:
        url = task_context.queue.buildSignedUrl(
            'getLatestArtifact', task_id, path,
            # XXX Can set expiration kwarg in (int) seconds from now;
            # defaults to 15min.
        )

    return url


# get_expiration_arrow {{{1
def get_expiration_arrow(task_context: TaskContext):
    """Return an arrow matching `task_context.task['expires']`.

    Args:
        task_context (TaskContext): the task context

    Returns:
        arrow: `task_context.task['expires']`.

    """
    return arrow.get(task_context.task['expires'])


# download_artifacts {{{1
async def download_artifacts(task_context, file_urls, parent_dir=None,
                             session=None, download_func=download_file,
                             valid_artifact_task_ids=None):
    """Download artifacts in parallel after validating their URLs.

    Valid ``taskId``s for download include the task's dependencies and the
    ``taskGroupId``, which by convention is the ``taskId`` of the decision task.

    Args:
        task_context (TaskContext): the task context.
        file_urls (list): the list of artifact urls to download.
        parent_dir (str, optional): the path of the directory to download the
            artifacts into.  If None, defaults to ``work_dir``.  Default is None.
        session (aiohttp.ClientSession, optional): the session to use to download.
            If None, defaults to task_context.session.  Default is None.
        download_func (function, optional): the function to call to download the files.
            default is ``download_file``.
        valid_artifact_task_ids (list, optional): the list of task ids that are
            valid to download from.  If None, defaults to all task dependencies
            plus the decision taskId.  Defaults to None.

    Returns:
        list: the full paths to the files downloaded

    Raises:
        scriptworker.exceptions.BaseDownloadError: on download failure after
            any applicable retries.

    """
    parent_dir = parent_dir or task_context.work_dir
    session = session or task_context.session

    tasks = []
    files = []
    valid_artifact_rules = task_context.config['valid_artifact_rules']
    # XXX when chain of trust is on everywhere, hardcode the chain of trust task list
    valid_artifact_task_ids = valid_artifact_task_ids or list(task_context.task['dependencies'] + [get_decision_task_id(task_context.task)])
    for file_url in file_urls:
        rel_path = validate_artifact_url(valid_artifact_rules, valid_artifact_task_ids, file_url)
        abs_file_path = os.path.join(parent_dir, rel_path)
        files.append(abs_file_path)
        tasks.append(
            asyncio.ensure_future(
                retry_async(
                    download_func, args=(task_context, file_url, abs_file_path),
                    retry_exceptions=(DownloadError, aiohttp.ClientError, asyncio.TimeoutError),
                    kwargs={'session': session},
                )
            )
        )

    await raise_future_exceptions(tasks)
    return files


def get_upstream_artifacts_full_paths_per_task_id(task_context):
    """List the downloaded upstream artifacts.

    Args:
        task_context (TaskContext): the task context.

    Returns:
        dict, dict: lists of the paths to upstream artifacts, sorted by task_id.
            First dict represents the existing upstream artifacts. The second one
            maps the optional artifacts that couldn't be downloaded

    Raises:
        scriptworker.exceptions.ScriptWorkerTaskException: when an artifact doesn't exist.

    """
    upstream_artifacts = task_context.task['payload']['upstreamArtifacts']
    task_ids_and_relative_paths = [
        (artifact_definition['taskId'], artifact_definition['paths'])
        for artifact_definition in upstream_artifacts
    ]

    optional_artifacts_per_task_id = get_optional_artifacts_per_task_id(upstream_artifacts)

    upstream_artifacts_full_paths_per_task_id = {}
    failed_paths_per_task_id = {}
    for task_id, paths in task_ids_and_relative_paths:
        for path in paths:
            try:
                path_to_add = get_and_check_single_upstream_artifact_full_path(task_context, task_id, path)
                add_enumerable_item_to_dict(
                    dict_=upstream_artifacts_full_paths_per_task_id,
                    key=task_id, item=path_to_add
                )
            except ScriptWorkerTaskException:
                if path in optional_artifacts_per_task_id.get(task_id, []):
                    log.warning('Optional artifact "{}" of task "{}" not found'.format(path, task_id))
                    add_enumerable_item_to_dict(
                        dict_=failed_paths_per_task_id,
                        key=task_id, item=path
                    )
                else:
                    raise

    return upstream_artifacts_full_paths_per_task_id, failed_paths_per_task_id


def get_and_check_single_upstream_artifact_full_path(task_context: TaskContext, task_id: str, path: str):
    """Return the full path where an upstream artifact is located on disk.

    Args:
        task_context (TaskContext): the task context.
        task_id (str): the task id of the task that published the artifact
        path (str): the relative path of the artifact

    Returns:
        str: absolute path to the artifact

    Raises:
        scriptworker.exceptions.ScriptWorkerTaskException: when an artifact doesn't exist.

    """
    abs_path = get_single_upstream_artifact_full_path(task_context, task_id, path)
    if not os.path.exists(abs_path):
        raise ScriptWorkerTaskException(
            'upstream artifact with path: {}, does not exist'.format(abs_path)
        )

    return abs_path


def get_single_upstream_artifact_full_path(task_context: TaskContext, task_id: str, path: str):
    """Return the full path where an upstream artifact should be located.

    Artifact may not exist. If you want to be sure if does, use
    ``get_and_check_single_upstream_artifact_full_path()`` instead.

    This function is mainly used to move artifacts to the expected location.

    Args:
        task_context (TaskContext): the task context.
        task_id (str): the task id of the task that published the artifact
        path (str): the relative path of the artifact

    Returns:
        str: absolute path to the artifact should be.

    """
    return os.path.abspath(os.path.join(task_context.work_dir, 'cot', task_id, path))


def get_optional_artifacts_per_task_id(upstream_artifacts):
    """Return every optional artifact defined in ``upstream_artifacts``, ordered by taskId.

    Args:
        upstream_artifacts: the list of upstream artifact definitions

    Returns:
        dict: list of paths to downloaded artifacts ordered by taskId

    """
    # A given taskId might be defined many times in upstreamArtifacts. Thus, we can't
    # use a dict comprehension
    optional_artifacts_per_task_id = {}

    for artifact_definition in upstream_artifacts:
        if artifact_definition.get('optional', False) is True:
            task_id = artifact_definition['taskId']
            artifacts_paths = artifact_definition['paths']

            add_enumerable_item_to_dict(
                dict_=optional_artifacts_per_task_id,
                key=task_id, item=artifacts_paths
            )

    return optional_artifacts_per_task_id
