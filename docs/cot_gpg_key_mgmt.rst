.. _gpg-key-management:

Chain of Trust GPG Key Management
---------------------------------

GPG key management is a critical part of the chain of trust. There are
several types of gpg keys:

-  [taskcluster team] worker keys, which are unsigned pubkeys for
   docker- and generic- workers.
-  [releng team] scriptworker keys, which are signed pubkeys for
   scriptworkers.
-  [releng team] scriptworker trusted keys, which are the pubkeys of
   releng team members who are allowed to generate and sign scriptworker
   keys.
-  [various] git commit signing keys. We keep the above pubkeys in a git
   repo, and we sign the commits. These are the pubkeys that are allowed
   to sign the git commits.

Adding new git commit signing gpg keys
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

To update the other pubkeys, we need to be able to add them to the `git
repo <https://github.com/mozilla-releng/cot-gpg-keys>`__. We add the new
pubkeys in two places: `add the long keyid
in-repo <https://github.com/mozilla-releng/cot-gpg-keys/blob/master/check_commit_signatures.py#L13>`__,
and `add the pubkey itself in
puppet <http://hg.mozilla.org/build/puppet/file/tip/modules/scriptworker/files/git_pubkeys>`__

Adding new worker gpg keys
~~~~~~~~~~~~~~~~~~~~~~~~~~

New worker gpg keys should be committed to the
`repo <https://github.com/mozilla-releng/cot-gpg-keys>`__ with signed
commits. Only certain people can sign the commits, as per
`above <#adding-new-git-commit-signing-gpg-keys>`__.

new docker and generic worker gpg keys
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

When generating a new AMI or image, the docker and generic workers
generate a new gpg keypair. The Taskcluster team has the option of
recording the public key and adding it to the repo.

The pubkeys for build, decision, and docker-image workerTypes should be
added to the repo, with a signed tag per the
`readme <https://github.com/mozilla-releng/cot-gpg-keys/blob/master/README.md#tagging-git-commits>`__.

new scriptworker gpg keys
^^^^^^^^^^^^^^^^^^^^^^^^^

First, you will need access to a trusted key (The trusted keys are in
the `scriptworker/trusted
dir <https://github.com/mozilla-releng/cot-gpg-keys/tree/master/scriptworker/trusted>`__.
That may mean someone else needs to generate the keys, or you may
petition for access to create and sign these keys. (To do so, update the
trusted keys with a new pubkey, sign that commit with a trusted git
commit key, and merge. If you don't have a trusted git key, see `adding
new git commit signing gpg
keys <#adding-new-git-commit-signing-gpg-keys>`__.)

Once you have access to a trusted key, generate new gpg keypairs for
each host. The email address will be ``username``\ @\ ``fqdn``, e.g.
``cltsign@signing-linux-1.srv.releng.use1.mozilla.com``. You can use
`this
script <https://github.com/mozilla-releng/scriptworker/blob/master/scripts/create_gpg_keys.py>`__,
like

.. code:: bash

    scriptworker/scripts/create_gpg_keys.py -u cltsign -s host1.fqdn.com host2.fqdn.com
    # This will generate a gpg homedir in ./gpg
    # Keys will be written to ./host{1,2}.fqdn.com.{pub,sec}

Next, sign the newly created gpg keys with your trusted gpg key.

1. `import
   pubkey <https://access.redhat.com/documentation/en-US/Red_Hat_Enterprise_Linux/4/html/Step_by_Step_Guide/s1-gnupg-import.html>`__

.. code:: bash

       gpg --import FQDN.pub

2. sign pubkey

.. code:: bash

    gpg --list-keys EMAIL
    gpg --sign-key EMAIL  # or fingerprint

3. `export signed
   pubkey <https://access.redhat.com/documentation/en-US/Red_Hat_Enterprise_Linux/4/html/Step_by_Step_Guide/s1-gnupg-export.html>`__

.. code:: bash

    gpg --armor --export EMAIL > USERNAME@FQDN.pub  # or fingerprint

The signed pubkey + private key will need to go into hiera, as described
`here <new_instance.html#puppet>`__.

The signed pubkey will need to land in
`scriptworker/valid <https://github.com/mozilla-releng/cot-gpg-keys/tree/master/scriptworker/valid>`__
with a signed tag, per `the readme <https://github.com/mozilla-releng/cot-gpg-keys/blob/master/README.md#tagging-git-commits>`__.
