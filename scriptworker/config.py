#!/usr/bin/env python
"""Config for scriptworker.

Attributes:
    log (logging.Logger): the log object for the module.
    CREDS_FILES (tuple): an ordered list of files to look for taskcluster
        credentials, if they aren't in the config file or environment.

"""
import argparse
from copy import deepcopy
from frozendict import frozendict
import logging
import os
import re
import sys
from yaml import safe_load
from collections import Mapping

from scriptworker.constants import DEFAULT_CONFIG
from scriptworker.context import Context
from scriptworker.exceptions import ConfigError
from scriptworker.log import update_logging_config
from scriptworker.utils import load_json_or_yaml

log = logging.getLogger(__name__)

CREDS_FILES = (
    os.path.join(os.getcwd(), 'secrets.json'),
    os.path.join(os.environ.get('HOME', '/etc/'), '.scriptworker'),
)

# Based on
# https://github.com/taskcluster/taskcluster-queue/blob/ced3b7bd824b445fd33ce0deb5de87a65f02b8b3/src/api.js#L94
_GENERIC_ID_REGEX = re.compile(r'^[a-zA-Z0-9-_]{1,22}$')
_VALUE_UNDEFINED_MESSAGE = "{path} {key} needs to be defined!"


def get_frozen_copy(values):
    """Convert `values`'s list values into tuples, and dicts into frozendicts.

    A recursive function(bottom-up conversion)

    Args:
        values (dict/list): the values/list to be modified in-place.

    """
    if isinstance(values, (frozendict, dict)):
        return frozendict({key: get_frozen_copy(value) for key, value in values.items()})
    elif isinstance(values, (list, tuple)):
        return tuple([get_frozen_copy(value) for value in values])

    # Nothing to freeze.
    return values


def get_unfrozen_copy(values):
    """Recursively convert `value`'s tuple values into lists, and frozendicts into dicts.

    Args:
        values (frozendict/tuple): the frozendict/tuple.

    Returns:
        values (dict/list): the unfrozen copy.

    """
    if isinstance(values, (frozendict, dict)):
        return {key: get_unfrozen_copy(value) for key, value in values.items()}
    elif isinstance(values, (list, tuple)):
        return [get_unfrozen_copy(value) for value in values]

    # Nothing to unfreeze.
    return values


# read_worker_creds {{{1
def read_worker_creds(key="credentials"):
    """Get credentials from CREDS_FILES or the environment.

    This looks at the CREDS_FILES in order, and falls back to the environment.

    Args:
        key (str, optional): each CREDS_FILE is a json dict.  This key's value
            contains the credentials.  Defaults to 'credentials'.

    Returns:
        dict: the credentials found. None if no credentials found.

    """
    for path in CREDS_FILES:
        if not os.path.exists(path):
            continue
        contents = load_json_or_yaml(path, is_path=True, exception=None)
        if contents.get(key):
            return contents[key]
    else:
        if key == "credentials" and os.environ.get("TASKCLUSTER_ACCESS_TOKEN") and \
                os.environ.get("TASKCLUSTER_CLIENT_ID"):
            credentials = {
                "accessToken": os.environ["TASKCLUSTER_ACCESS_TOKEN"],
                "clientId": os.environ["TASKCLUSTER_CLIENT_ID"],
            }
            if os.environ.get("TASKCLUSTER_CERTIFICATE"):
                credentials['certificate'] = os.environ['TASKCLUSTER_CERTIFICATE']
            return credentials


# check_config {{{1
def check_config(config, path):
    """Validate the config against DEFAULT_CONFIG.

    Any unknown keys or wrong types will add error messages.

    Args:
        config (dict): the running config.
        path (str): the path to the config file, used in error messages.

    Returns:
        list: the error messages found when validating the config.

    """
    messages = []

    config_copy = get_frozen_copy(config)
    missing_keys = set(DEFAULT_CONFIG.keys()) - set(config_copy.keys())
    if missing_keys:
        messages.append("Missing config keys {}!".format(missing_keys))

    for key, value in config_copy.items():
        if key not in DEFAULT_CONFIG:
            messages.append("Unknown key {} in {}!".format(key, path))
            continue
        if value is None:
            messages.append(_VALUE_UNDEFINED_MESSAGE.format(path=path, key=key))
        else:
            value_type = type(value)
            if isinstance(DEFAULT_CONFIG[key], Mapping) and 'by-cot-product' in DEFAULT_CONFIG[key]:
                default_type = type(DEFAULT_CONFIG[key]['by-cot-product'][config['cot_product']])
            else:
                default_type = type(DEFAULT_CONFIG[key])
            if value_type is not default_type:
                messages.append(
                    "{} {}: type {} is not {}!".format(path, key, value_type, default_type)
                )
        if value in ("...", b"..."):
            messages.append(_VALUE_UNDEFINED_MESSAGE.format(path=path, key=key))
        if key in ("provisioner_id", "worker_group", "worker_type", "worker_id") and not _is_id_valid(value):
            messages.append('{} doesn\'t match "{}" (required by Taskcluster)'.format(key, _GENERIC_ID_REGEX.pattern))
    return messages


def _is_id_valid(id_string):
    return _GENERIC_ID_REGEX.match(id_string) is not None


def apply_product_config(config):
    """Apply config values that are keyed by `cot_product`.

    This modifies the passed in configuration.

    Args:
        config dict: the config to apply cot_product keying too

    Returns: dict

    """
    cot_product = config['cot_product']

    for key in config:
        if isinstance(config[key], Mapping) and 'by-cot-product' in config[key]:
            try:
                config[key] = config[key]['by-cot-product'][cot_product]
            except KeyError:
                raise ConfigError("Product {} not specified for key {}".format(cot_product, key))

    return config


# create_config {{{1
def create_config(config_path="scriptworker.yaml"):
    """Create a config from DEFAULT_CONFIG, arguments, and config file.

    Then validate it and freeze it.

    Args:
        config_path (str, optional): the path to the config file.  Defaults to
            "scriptworker.yaml"

    Returns:
        tuple: (config frozendict, credentials dict)

    Raises:
        SystemExit: on failure

    """
    if not os.path.exists(config_path):
        print("{} doesn't exist! Exiting...".format(config_path), file=sys.stderr)
        sys.exit(1)
    with open(config_path, "r", encoding="utf-8") as fh:
        secrets = safe_load(fh)
    config = dict(deepcopy(DEFAULT_CONFIG))
    if not secrets.get("credentials"):
        secrets['credentials'] = read_worker_creds()
    config.update(secrets)
    apply_product_config(config)
    messages = check_config(config, config_path)
    if messages:
        print('\n'.join(messages), file=sys.stderr)
        print("Exiting...", file=sys.stderr)
        sys.exit(1)
    credentials = get_frozen_copy(secrets['credentials'])
    del(config['credentials'])
    config = get_frozen_copy(config)
    return config, credentials


# get_context_from_cmdln {{{1
def get_context_from_cmdln(args, desc="Run scriptworker"):
    """Create a Context object from args.

    Args:
        args (list): the commandline args.  Generally sys.argv

    Returns:
        tuple: ``scriptworker.context.Context`` with populated config, and
            credentials frozendict

    """
    context = Context()
    parser = argparse.ArgumentParser(description=desc)
    parser.add_argument(
        "config_path", type=str, nargs="?", default="scriptworker.yaml",
        help="the path to the config file"
    )
    parsed_args = parser.parse_args(args)
    context.config, credentials = create_config(config_path=parsed_args.config_path)
    update_logging_config(context)
    return context, credentials
