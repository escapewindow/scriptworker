Chain of Trust Artifact Generation
----------------------------------

Each chain-of-trust-enabled taskcluster worker generates and uploads a chain of trust artifact after each task.  This artifact contains details about the task, worker, and artifacts, and is signed by the embedded ed25519 key.

Embedded ed25519 keys
~~~~~~~~~~~~~~~~~~~~~

Each supported taskcluster ``workerType`` has an embedded ed25519 keypair.
These are the second factor.

``docker-worker`` has the ed25519 privkey embedded in the AMI, inaccessible
to tasks run inside the docker container.

``generic-worker`` can embed the ed25519 privkey into the AMI for EC2
instances, or into the system directories for hardware. This are
permissioned so the task user doesn't have access to it.

Chain-of-Trust-enabled ``scriptworker`` workers have a valid ed25519 keypair.

The pubkeys for trusted workerTypes are recorded in
``scriptworker.constants.ed25519_public_keys``.

Chain of Trust artifacts
~~~~~~~~~~~~~~~~~~~~~~~~

After the task finishes, the worker creates a chain of trust json blob,
ed25519 signs it, then uploads it as ``public/chain-of-trust.json`` and its
detached signature, ``public/chain-of-trust.json.sig``. It looks like

.. code:: python

        {
          "artifacts": {
            "path/to/artifact": {
              "sha256": "abcd1234"
            },
            ...
          },
          "chainOfTrustVersion": 1,
          "environment": {
            # worker-impl specific stuff, like ec2 instance id, ip
          },
          "runId": 0,
          "task": {
            # task defn
          },
          "taskId": "...",
          "workerGroup": "...",
          "workerId": "..."
        }

-  The v1 chain-of-trust json artifact schema is viewable `here
   <https://github.com/mozilla-releng/scriptworker/blob/master/scriptworker/data/cot_v1_schema.json>`__.
-  This is a real :download:`example artifact <_static/chain-of-trust.json>`.
