Chain of Trust Verification
---------------------------

Currently, only chain-of-trust-enabled scriptworker instances verify the chain of trust.  These are tasks like signing, publishing, and submitting updates to the update server.  If the chain of trust is not valid, scriptworker kills the task before it performs any further actions.

The below is how this happens.

.. _decision-task:

Decision Task
~~~~~~~~~~~~~

The decision task is a special task that generates a taskgraph, then submits it to the Taskcluster queue.  This graph contains task definitions and dependencies.  The decision task uploads its generated graph json as an artifact, which can be inspected during chain of trust verification.

We rebuild the decision task's task definition via `json-e`_, and verify that it matches the runtime task definition.

Ed25519 homedir management
~~~~~~~~~~~~~~~~~~~~~~~~~~~

The chain of trust artifacts are signed. We need to keep track of the ed25519
public keys to verify them.

We keep the level 3 gecko pubkeys in ``scriptworker.constants.ed25519_public_keys``, as base64-encoded ascii string. Once decoded, these are the seeds for the ed25519 public keys. These are tuples of valid keys, to allow for key rotation.

Building the chain
~~~~~~~~~~~~~~~~~~

First, scriptworker inspects the [signing/balrog/pushapk/beetmover/etc] task that it claimed from the Taskcluster queue.  It adds itself and its :ref:`decision-task` to the chain.

Any task that generates artifacts for the scriptworker then needs to be inspected.  For scriptworker tasks, we have ``task.payload.upstreamArtifacts``, which looks like

.. code:: python

     [{
       "taskId": "upstream-task-id",
       "taskType": "build",  # for cot verification purposes
       "paths": ["path/to/artifact1", "path/to/artifact2"],
       "formats": ["gpg", "jar"]  # This is signing-specific for now; we could make formats optional, or use it for other task-specific info
     }, {
       ...
     }]

We add each upstream ``taskId`` to the chain, with corresponding ``taskType`` (we use this to know how to verify the task).

For each task added to the chain, we inspect the task definition, and add other upstream tasks:

- if the decision task doesn't match, add it to the chain.
- docker-worker tasks have ``task.extra.chainOfTrust.inputs``, which is a dictionary like ``{"docker-image": "docker-image-taskid"}``.  Add the docker image ``taskId`` to the chain (this will likely have a different decision ``taskId``, so add that to the chain).

Verifying the chain
~~~~~~~~~~~~~~~~~~~

Scriptworker:

-  downloads the chain of trust artifacts for each upstream task in the chain, and verifies their signatures.  This requires detecting which worker implementation each task is run on, to know which ed25519 public key to use.  At some point in the future, we may switch to an OpenSSL CA.
-  downloads each of the ``upstreamArtifacts`` and verify their shas against the corresponding task's chain of trust's artifact shas.  the downloaded files live in ``cot/TASKID/PATH`` , so the script doesn't have to re-download and re-verify.
-  downloads each decision task's ``task-graph.json``.  For every *other* task in the chain, we make sure that their task definition matches a task in their decision task's task graph.
-  rebuilds decision and action task definitions using `json-e`_, and verifies the rebuilt task definition matches the runtime definition.
-  verifies each docker-worker task is either part of the ``prebuild_docker_image_task_types``, or that it downloads its image from a previous docker-image task.
-  verifies each docker-worker task's docker image sha.
-  makes sure the ``interactive`` flag isn't on any docker-worker task.
-  determines which repo we're building off of.
-  matches its task's scopes against the tree; restricted scopes require specific branches.

Once all verification passes, it launches the task script.  If chain of trust verification fails, it exits before launching the task script.

.. _json-e: https://github.com/taskcluster/json-e
