# Maintenance

For sheriffs, release/relops, taskcluster, or related users, this page describes maintenance for scriptworkers.

Last modified 2019.03.09.

## New docker shas

For chain of trust verification, we verify the docker shas that we run in docker-worker.

For some tasks, we build the docker images in docker-image tasks, and we can verify the image's sha against docker-image task's output.

However, for decision and docker-image tasks, we download the docker image from docker hub.  We allowlist the shas to make sure we are running valid images.

We specify those [here](https://github.com/mozilla-releng/scriptworker/blob/121c474f5b21084a4a3742f21c3f30c018e5c766/scriptworker/constants.py#L96-L106).  However, if we only specified them in `scriptworker.constants`, we'd have to push a new scriptworker release every time we update this allowlist.  So we override this list [here](https://hg.mozilla.org/build/puppet/file/09df8cec082b/modules/scriptworker/templates/scriptworker.yaml.erb#l53).

For now, we need to keep both locations updated.  Puppet governs production instances, and the scriptworker repo is used for scriptworker development, and a full allowlist is required for chain of trust verification.

## Chain of Trust settings

As above, other chain of trust settings live in [constants.py](https://github.com/mozilla-releng/scriptworker/blob/121c474f5b21084a4a3742f21c3f30c018e5c766/scriptworker/constants.py#L124-L244).  However, if we only specified them in `scriptworker.constants`, we'd have to push a new scriptworker release every time we update them.  So we can override them [here](https://hg.mozilla.org/build/puppet/file/09df8cec082b/modules/scriptworker/templates/scriptworker.yaml.erb).

Ideally we keep the delta small, and remove the overrides in puppet when we release a new scriptworker version that updates these defaults.  As currently written, each scriptworker instance type will need its scriptworker version bumped individually.

## Ed25519 keys

For ed25519 key maintenance, see the [chain of trust docs](chain_of_trust.html#ed25519-key-management)
