Overview
--------

Taskcluster is versatile and self-serve, and enables developers to make
automation changes without being blocked on other teams.  In the case of
developer testing and debugging, this is very powerful and enabling. In
the case of release automation, the ability to schedule arbitrary tasks
with arbitrary configs can present a security concern.

The chain of trust is a second factor that isn't automatically compromised
if scopes are compromised. This chain allows us to trace a task's request
back to the tree.

High level view
~~~~~~~~~~~~~~~

`Scopes <https://docs.taskcluster.net/manual/integrations/apis/scopes>`__ are how Taskcluster controls access to certain features. These are granted to `roles <https://docs.taskcluster.net/manual/integrations/apis/roles>`__, which are granted to users or LDAP groups.

Scopes and their associated Taskcluster credentials are not leak-proof. Also, by their nature, more people will have restricted scopes than you want, given any security-sensitive scope.  Without the chain of trust, someone with release-signing scopes would be able to schedule any arbitrary task to sign any arbitrary binary with the release keys, for example.

The chain of trust is a second factor.  The embedded ed25519 keys on the workers are either the `something you have <http://searchsecurity.techtarget.com/definition/possession-factor>`__ or the `something you are <http://searchsecurity.techtarget.com/definition/inherence-factor>`__, depending on how you view the taskcluster workers.

Each chain-of-trust-enabled taskcluster worker generates and signs chain of trust artifacts, which can be used to verify each task and its artifacts, and trace a given request back to the tree.

The scriptworker nodes are the verification points.  Scriptworkers run the release sensitive tasks, like signing and publishing releases.  They verify their task definitions, as well as all upstream tasks that generate inputs into their task.  Any broken link in the chain results in a task exception.

In conjunction with other best practices, like `separation of roles <https://en.wikipedia.org/wiki/Separation_of_duties>`__, we can reduce attack vectors and make penetration attempts more visible, with task exceptions on release branches.

Chain of Trust Versions
=======================

1. Initial Chain of Trust implementation with GPG signatures: Initial `1.0.0b1 on 2016-11-14 <https://github.com/mozilla-releng/scriptworker/blob/master/CHANGELOG.md#100b1---2016-11-14>`_
2. CoT v2: rebuild task definitions via json-e. `7.0.0 on 2018-01-18 <https://github.com/mozilla-releng/scriptworker/blob/master/CHANGELOG.md#700---2018-01-18>`_
3. Generic action hook support. `12.0.0 on 2018-05-29 <https://github.com/mozilla-releng/scriptworker/blob/master/CHANGELOG.md#1200---2018-05-29>`_
4. Release promotion action hook support. `17.1.0 on 2018-12-28 <https://github.com/mozilla-releng/scriptworker/blob/master/CHANGELOG.md#1710---2018-12-28>`_
5. ed25519 support; deprecate GPG support.
6. drop support for gpg

Planned future versions:

* drop support for non-hook actions
