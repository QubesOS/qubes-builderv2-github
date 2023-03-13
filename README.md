Integration with next generation of Qubes Builder:
===

Parts below are mostly outdated and needs to be rewritten according to
the new Qubes Builder.

Description
-----------

This is Qubes builder plugin which reports to GitHub issues when package
containing a fix is uploaded to the repository. Reporting is done using a
comment and additionally a label, so it is easy to check if the issue was
uploaded somewhere (including backports!).

The plugin will report only when building, having failed builds or
uploading to standard repositories. Only `current` and
`current-testing` repositories are taken into account, others (for example
`unstable` or `security-testing`) are ignored.

Optionally additional repository may be configured to have dedicated issues
created for the sole purpose of tracking uploaded updates (regardless of
comments in issues mentioned in git log). One issue will be used for multiple
target templates (Debian, Fedora etc).

Configuration
-------------

To use this plugin you need to enable it in  `builder.yml` by adding `github`
top level key with settings:

 * `api-key` - GitHub API key
 * `state-dir` - directory for plugin state

Optional:

  * `logs-repo` - repository in which every log are uploaded.
  * `isos-url` - public URL where ISOs are uploaded. It is used only for comments.
  * `build-report-repo` - repository in which every build status package and template
    should have issue created (regardless of commenting issues mentioned in git log).
  * `maintainers` - GPG keys allowed to trigger GitHub command provided by the plugin.

For example:

```yaml
github:
  api-key: "0123456789ABCDEF"
  state-dir: /home/user/github-notify-state
  build-report-repo: "fepitre/test-updates-status"
  logs-repo: "fepitre/test-build-logs"
  isos-url: "https://qubes.notset.fr/iso/"
  maintainers:
    9FA64B92F95E706BF28E2CA6484010B5CDC576E2:
      isos: true
      distributions:
        - host-fc37
        - vm-bookworm
      templates:
        - fedora-35-xfce
        - debian-11
```

RPC services configuration
--------------------------

RPC services are configured differently, because are not running from within
qubes-builder, so don't know where to look for `builder.conf`. Instead, it looks
into `~/.config/qubes-builder-github/builders.list`. The file have a simple
`key=value` syntax, where key is Qubes release (like `r4.2`) and value is a
full path to qubes-builder directory.

Example configuration:

    r4.2=/home/user/qubes-builder-r4.2
    r4.3=/home/user/qubes-builder-r4.3

In addition to this,
`~/.config/qubes-builder-github/trusted-keys-for-commands.gpg` contains a
GPG keyring with public keys allowed to sign repository action commands (see below).

Commands in GitHub issues comments
----------------------------------

`qubesbuilder.ProcessGithubCommand` rpc service can respond to GPG-signed
commands, for example sent as a comment on (some) GitHub issue. Each such
command needs to be properly inline GPG signed, with a key included in
`~/.config/qubes-builder-github/trusted-keys-for-commands.gpg`. The service
does not try to validate where such comment is placed, it trusts only signed
content of the comment (this is conscious design decision).
Additionally, set `fingerprint` option in `builder.yml` (replace
`fingerprint` with actual full key fingerprint) to list what
distribution can be controlled with a given key (see example in Configuration section.)

### Upload command ###

Issues created in repository pointed by `build-report-repo` have one
more purpose. Can be used to control when packages should be moved from testing
(`current-testing`) to stable (`current`) repository. This can be achieved by
adding GPG-signed comments there. A command consists of one line in form:

    "Upload" component_name commit_sha release_name "current" dists "repo"

(words in quotes should be used verbatim - without quotes, others are parameters)

Parameters:

  - `component_name` - name of component to handle
  - `commit_sha` - commit SHA of that component; the command is considered only
    if packages recently uploaded (or precisely: local git repository state)
    matches this commit; this is mainly to prevent replay attacks
  - `release_name` - name of release, like `r4.2`; must match name used in
    `builders.list` configuration and name used in updates repositories
    (apt/yum/...)
  - `dists` - optional list of distributions to which upload should be limited;
    this should be a (space separated) list of pairs `host`/`vm` and distribution
    codename (like `fc42`), separated with `-`; for example `host-fc42` or
    `vm-bookworm`.

Command needs to be signed with key for which public part is in
`~/.config/qubes-builder-github/trusted-keys-for-commands.gpg` keyring.

### Build-template command ###

One can use Build-template command to start a template build. A command
consists of one line in form:

    "Build-template" release_name dist timestamp

(words in quotes should be used verbatim - without quotes, others are parameters)

Parameters:

  - `release_name` - name of release, like `r4.2`; must match name used in
    `builders.list` configuration and name used in updates repositories
    (apt/yum/...)
  - `dist` - template code name, as defined in builder.yml, `templates` option
  - `timestamp` - timestamp part of template version, in form `%Y%m%d%H%M`, UTC
    (for example `201806281345Z`); must be not older than 1h and not greater
    than 5 minutes into the future
  
Command needs to be signed with key for which public part is in
`~/.config/qubes-builder-github/trusted-keys-for-commands.gpg` keyring.

Comments text
=============

Comment messages can be configured in `message-*` files. Available files:
 * `message-stable-host`, `message-testing-host` - when the package is uploaded to
   host repository
 * `message-stable-vm`, `message-testing-vm` - when the package is uploaded to
   VM repository
 * `message-stable-vm-DIST`, `message-testing-vm-DIST` (where `DIST` is code
   name of target distribution) - if exists, it is used instead of
   corresponding `message-stable-vm` or `message-testing-vm`
 * `message-build-report` - template for issue description (if
   `build-report-repo` set)

Each file is actually message template, which can contain following placeholders:
 * `@DIST@` - code name of the target distribution
 * `@PACKAGE_SET@` - either `host` or `vm`
 * `@PACKAGE_NAME@` - primary package name, including the version being
   uploaded; in case of multiple packages being build from the same component,
   only the first one is listed
 * `@COMPONENT@` - Qubes component name (as listed in `COMPONENTS` setting of `builder.yml`)
 * `@REPOSITORY@` - either `testing` or `stable`
 * `@RELEASE_NAME@` - name of target Qubes release (`r2`, `r3.0` etc)
 * `@GIT_LOG@` - `git log --pretty=oneline previous_commit..current_commit` with github-like commits references
 * `@GIT_LOG_URL@` - GitHub URL to commits between previous version and the current one. "compare" GitHub feature.
 * `@COMMIT_SHA@` - Commit SHA used to build the package.
 * `@ISO_VERSION@` - ISO version.

Ideally the message should include instruction how to install the update.

Installation
------------

1. Adjust `builder.yml`, see 'Configuration' chapter above for details

2. (optional) Place rpc services in `/usr/local/etc/qubes-rpc` directory of
   build VM. Also, copy (or symlink) `lib` directory to
   `/usr/local/lib/qubes-builder-github`. There are two services:

- `qubesbuilder.TriggerBuild`: Trigger a build for a given component. The
   service will check if configured branch (according to `builder.yml`) have
   new version tag at the top (and if it's properly signed) and only then will
   build the component and upload package(s) to current-testing repository.
   Service accept only component name on its standard input. See the next step
   for actual integration with GitHub. See also 'RPC services configuration'
   chapter.
- `qubesbuilder.ProcessGithubCommand`: Process command issued as GPG inline
   signed comment on some GitHub issue. See 'Commands in GitHub issues
   comments' chapter for details. Service accept the comment body on its stdin.
   See also 'RPC services configuration' chapter.

3. (optional) Install GitHub webhooks (see `webhooks` directory)
   somewhere reachable from `github.com` - this probably means `sys-net` in
   default Qubes OS installation. You need to configure a web server there to
   launch them as CGI scripts. Then add the hook(s) to repository/organization
   configuration on `github.com`. Then fill
   `~/.config/qubes-builder-github/build-vms.list` with a list to which
   information should be delivered (one per line). And setup qrexec policy for
   services mentioned in point 2 to actually allow such calls.
