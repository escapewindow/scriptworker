===============================
Scriptworker Readme
===============================

.. image:: https://travis-ci.org/mozilla-releng/scriptworker.svg?branch=master
    :target: https://travis-ci.org/mozilla-releng/scriptworker

.. image:: https://coveralls.io/repos/github/mozilla-releng/scriptworker/badge.svg?branch=master
    :target: https://coveralls.io/github/mozilla-releng/scriptworker?branch=master

.. image:: https://readthedocs.org/projects/scriptworker/badge/?version=latest
    :target: http://scriptworker.readthedocs.io/en/latest/?badge=latest
    :alt: Documentation Status

Scriptworker implements the `TaskCluster worker model`_, then launches a pre-defined script.

.. _TaskCluster worker model: http://docs.taskcluster.net/queue/worker-interaction/

This worker was designed for `Releng processes`_ that need specific, limited, and pre-defined capabilities.

.. _Releng processes: https://bugzilla.mozilla.org/show_bug.cgi?id=1245837

Free software: MPL2 license

-----
Usage
-----
* Create a config file.  By default scriptworker will look in ``./scriptworker.yaml``, but this config path can be specified as the first and only commandline argument.  There is an `example config file`_, and all config items are specified in `scriptworker.constants.DEFAULT_CONFIG`_.

.. _example config file: https://github.com/mozilla-releng/scriptworker/blob/master/scriptworker.yaml.tmpl
.. _scriptworker.constants.DEFAULT_CONFIG: https://github.com/mozilla-releng/scriptworker/blob/master/scriptworker/constants.py

Credentials can live in ``./scriptworker.yaml``, ``./secrets.json``, ``~/.scriptworker``, or in environment variables:  ``TASKCLUSTER_ACCESS_TOKEN``, ``TASKCLUSTER_CLIENT_ID``, and ``TASKCLUSTER_CERTIFICATE``.

* Launch: ``scriptworker [config_path]``

-------
Building a docker image
-------

First, create a `secrets.json`. For integration testing, you'll need to define the `integration_credentials`; to do any other authenticated work, you'll need to define `credentials`.

Then::

    PY_DOT_VERSION=3.7  # or 3.6
    docker build -t scriptworker-test-$PY_DOT_VERSION --build-arg PY_DOT_VERSION=$PY_DOT_VERSION  --file docker/Dockerfile.test .

-------
Testing
-------

Without integration tests, install tox, then

``NO_TESTS_OVER_WIRE=1 tox -e py36``

With integration tests, first create a client with the scopes::

    assume:project:taskcluster:worker-test-scopes
    queue:cancel-task:test-dummy-scheduler/*

Then  create a ``./secrets.json`` or ``~/.scriptworker`` that looks like::

    {
        "integration_credentials": {
            "clientId": "...",
            "accessToken": "...",
            "certificate": "..."
        }
    }


(``certificate`` is only specified if using temp creds)


then

``tox``

It's also possible to test in docker. First, `build the docker image`_, making sure to add integration credentials to `secrets.json`. Then::

    docker run -i scriptworker-test-$PY_DOT_VERSION
