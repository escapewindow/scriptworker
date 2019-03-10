# Adding new scriptworker instance types

This doc describes when and how to add a new scriptworker instance type, e.g. signing, pushapk, beetmover, balrog.

Last updated 2016.11.18

## Is scriptworker the right tool?

Scriptworker is designed to run security-sensitive tasks with limited capabilities.

* Does this task require elevated privileges or access to sensitive secrets to run?
* Is this task sufficiently important to spin up and maintain a new pool of workers?
* Is the expected load sufficiently contained so we don't require a dynamically sizable pool of workers?

If you answered yes to the above, scriptworker may be a good option.

### Chain of Trust considerations

If this is for a new task type in a product/graph that already has scriptworker tasks and chain of trust verification, then adding a new task should be an incremental change.

If this is for a new graph type or new product, and the graph doesn't look like the Firefox graph, there may be significant changes required to support the chain of trust.  This is an important consideration when choosing your solution.

## Creating a new scriptworker instance type

Once you've decided to use scriptworker, these are the steps to take to implement.

### Write a script

This can be a script in any language that can be called from the commandline, although we prefer async python 3.  This is standalone, so it's possible to develop and test this script without scriptworker.

#### Single purpose, but generic

The script should aim to support a single purpose, like signing or pushing updates.  However, ideally it's generic, so it can sign a number of different file types or push various products to various accounts, given the right config and creds.

#### Commandline args

Currently, we call the script from scriptworker with the commandline

```python
    # e.g., ["python", "/path/to/script.py", "/path/to/script_config.json"]
    [interpreter, script, config]
```

Where `interpreter` could be python3, `script` is the path to the script, and `config` is the path to the runtime configuration, which doesn't change between runs.

#### Config
The config could be anything you need the script to know, including paths to other config files.  These config items must be specified, and must match the eventual scriptworker config:

* `work_dir`: this is an absolute path.  This directory is deleted after the task and recreated before the next task.  Scriptworker will place files in here for the script's consumption.
* `artifact_dir`: this is where to put the artifacts that scriptworker will upload to taskcluster.  The directory layout will look like the directory layout in taskcluster, e.g. `public/build/target.apk` or `public/logs/foo.log`

#### Task

Scriptworker will place the task definition in `$work_dir/task.json`.  The script can read this task definition and behave accordingly.

When testing locally without scriptworker, you can create your own `task.json`.

##### task.payload.upstreamArtifacts

If the task defines `payload.upstreamArtifacts`, these are artifacts for scriptworker to download and verify their shas against the chain of trust.

`payload.upstreamArtifacts` currently looks like:

```python
    [{
      "taskId": "upstream-task-id1",
      "taskType": "build",
      "paths": ["public/artifact/path1", "public/artifact/path2"],
      "formats": []
    }, {
      ...
    }]
```

It will download them into `$artifact_dir/public/cot/$upstream-task-id/$path`.

##### Scopes

[Taskcluster scopes](https://docs.taskcluster.net/presentations/scopes/#/) are its ACLs: restricted behavior is placed behind scopes, and only those people and processes that need access to that behavior are given those scopes.  With the Chain of Trust, we can verify that restricted scopes can only be used in specific repos.

If your script is going to have different levels of access (e.g., CI- signing, nightly- signing, and release- signing), then it's best to put them each behind a different scope, and use that scope for determining which credentials to use.

## Deployment considerations

You don't have to address the below during script development, but it may be helpful to know some of the considerations that will affect deployment.

### Graph

We need to trace upstream tasks back to the tree.  We're able to find our decision task by the `taskGroupId`, but other dependencies we need to either use [`upstreamArtifacts`](#task-payload-upstreamartifacts) or `task.extra.chainOfTrust.inputs`, which looks like

```python
    "inputs": {
      "docker-image": "docker-image-taskid"
    }
```

If there are upstream tasks that depend on the output of other tasks, make sure all of them are connected via at least one of these two data structures.

### Puppet

There is a [scriptworker module](http://hg.mozilla.org/build/puppet/file/tip/modules/scriptworker) that we should use for new Firefox scriptworker instances, following the [signing scriptworker](http://hg.mozilla.org/build/puppet/file/tip/modules/signing_scriptworker) example.

For other products, we can either support them within MoCo Releng, or we can spin up a new parallel set of scriptworker pools to keep the secrets and access separate.  If we choose the latter, any deployment solution is acceptable: ansible, puppet, nix, ...

### Monitoring

We use nagios to ensure the health of instances running a scriptworker type. For each instance we
typically monitor the host (ping, disk usage, load, time, and puppet freshness) and the scriptworker
service (process running, log freshness). There is also a check for the queue length of each type.

Setting up a new type is more work than adding a new instance of an existing type. Once you've
cloned the IT puppet repo
([see point 10 for the location](https://mana.mozilla.org/wiki/display/MOC/NEW+New+Hire+Onboarding),
VPN required), a good approach is to look in the `modules/nagios4/manifests/prod/releng/`
directory to see how balrog-scriptworkers work. Some of the components are
 * `mdc1.pp` - adding the instance host names, and assigning them to a new hostgroup
 * `hostgroups.pp` - adding an alias for the new hostgroup
 * `services/mdc1.pp` - adding the hostgroup to all the checks needed.
 * `clusterchecks/mdc1.pp` - adding a queue check for the new type
 * `servicegroups/mdc1.pp` - adding an alias for the queue check

 Create a patch, attach it to Bugzilla, and ask someone from RelOps for review, before
 landing. After a 15-30 minute delay the checks should be live and you can look for them on the
 [mdc1 nagios server](https://nagios1.private.releng.mdc1.mozilla.com/releng-mdc1/).
