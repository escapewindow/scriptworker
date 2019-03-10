Chain of Trust Testing / debugging
----------------------------------

The ``verify_cot`` entry point allows you to test chain of trust
verification without running a scriptworker instance locally.

Create the virtualenv
~~~~~~~~~~~~~~~~~~~~~

-  Install git, ``python>=3.6``, and python3 virtualenv.

-  Clone scriptworker and create virtualenv:

.. code:: bash

        git clone https://github.com/mozilla-releng/scriptworker
        cd scriptworker
        virtualenv3 venv
        . venv/bin/activate
        python setup.py develop

Set up the test env
~~~~~~~~~~~~~~~~~~~~

-  Create a ~/.scriptworker or ./secrets.json with test client creds.

-  Create the client at `the client
   manager <https://tools.taskcluster.net/auth/clients/>`__. Mine has
   the ``assume:project:taskcluster:worker-test-scopes`` scope, but I
   don't think that's required.

-  The ~/.scriptworker or ./secrets.json file will look like this (fill
   in your clientId and accessToken):

.. code:: python

        {
          "credentials": {
            "clientId": "mozilla-ldap/asasaki@mozilla.com/signing-test",
            "accessToken": "********"
          }
        }

Find a task to test
~~~~~~~~~~~~~~~~~~~

-  Find a cot-enabled task on
   `treeherder <https://treeherder.mozilla.org>`__ to test.

-  Click it, click 'inspect task' in the lower left corner.

-  The taskId will be in a field near the top of the page.

Run the test
~~~~~~~~~~~~

-  Now you should be able to test chain of trust verification!

.. code:: bash

        verify_cot --task-type TASKTYPE TASKID  # e.g., verify_cot --task-type signing cbYd3U6dRRCKPUbKsEj1Iw

-  To test with signature verification, use the ``--verify-sigs`` option. This only works for level 3 trusted workers, since we don't keep track of the other pubkeys..
