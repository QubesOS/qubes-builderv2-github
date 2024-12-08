import datetime
import subprocess
from pathlib import Path

from conftest import get_issue

PROJECT_PATH = Path(__file__).resolve().parents[1]
DEFAULT_BUILDER_CONF = PROJECT_PATH / "tests/builder.yml"


def test_notify_00_template_build_success_upload(
    token, github_repository, workdir
):
    tmpdir, env = workdir
    build_log = "dummy"
    # We need seconds because we create multiple issues successively.
    timestamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%d%H%M%s")
    template_name = "fedora-42"
    package_name = f"qubes-template-{template_name}-4.2.0-{timestamp}"
    distribution = "vm-fc42"

    #
    # build
    #

    status = "building"
    cmd = [
        str(PROJECT_PATH / "utils/notify_issues.py"),
        f"--build-log={build_log}",
        f"--message-templates-dir={PROJECT_PATH}/templates",
        f"--github-report-repo-name={github_repository.full_name}",
        "build",
        "r4.2",
        str(tmpdir),
        package_name,
        distribution,
        status,
    ]
    subprocess.run(cmd, check=True, env=env)

    issue_title = f"qubes-template-{template_name} 4.2.0-{timestamp} (r4.2)"
    issue_desc = f"""Template {template_name} 4.2.0-{timestamp} for Qubes OS r4.2, see comments below for details and build status.

If you're release manager, you can issue GPG-inline signed command (depending on template):

* `Upload-template r4.2 {template_name} 4.2.0-{timestamp} templates-itl` (available 5 days from now)
* `Upload-template r4.2 {template_name} 4.2.0-{timestamp} templates-community` (available 5 days from now)

Above commands will work only if package in testing repository isn't superseded by newer version.

For more information on how to test this update, please take a look at https://www.qubes-os.org/doc/testing/#updates.
"""

    issue = get_issue(issue_title=issue_title, repository=github_repository)

    # Check if issue has been created
    assert issue is not None

    # Only one status tag
    assert len(issue.labels) == 1
    assert issue.labels[0].name == f"r4.2-{status}"

    # Check description
    assert issue.body == issue_desc

    #
    # built
    #
    status = "built"
    cmd = [
        str(PROJECT_PATH / "utils/notify_issues.py"),
        f"--build-log={build_log}",
        f"--message-templates-dir={PROJECT_PATH}/templates",
        f"--github-report-repo-name={github_repository.full_name}",
        "build",
        "r4.2",
        str(tmpdir),
        package_name,
        distribution,
        status,
    ]
    subprocess.run(cmd, check=True, env=env)

    #
    # upload
    #
    upload_repository = "templates-itl-testing"
    cmd = [
        PROJECT_PATH / "utils/notify_issues.py",
        f"--build-log={build_log}",
        f"--message-templates-dir={PROJECT_PATH}/templates",
        f"--github-report-repo-name={github_repository.full_name}",
        "upload",
        "r4.2",
        tmpdir,
        package_name,
        distribution,
        upload_repository,
        str(tmpdir / "state_file"),
        str(tmpdir / "stable_state_file"),
    ]
    subprocess.run(cmd, check=True, env=env)

    # Refresh issue object
    issue.update()

    # Only one status tag
    assert len(issue.labels) == 1
    assert issue.labels[0].name == f"r4.2-testing"

    # Check that comment exists
    comments = list(issue.get_comments())
    assert len(comments) == 2
    assert (
        comments[0].body
        == f"Template {template_name}-4.2.0-{timestamp} was built ([build log]({build_log}))."
    )
    assert (
        comments[1].body
        == f"Template {template_name}-4.2.0-{timestamp} was uploaded to {upload_repository} repository."
    )


def test_notify_01_template_build_failure(token, github_repository, workdir):
    tmpdir, env = workdir
    build_log = "dummy"
    timestamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%d%H%M%s")
    template_name = "debian-13"
    package_name = f"qubes-template-{template_name}-4.2.0-{timestamp}"
    distribution = "vm-trixie"

    #
    # build
    #

    status = "building"
    cmd = [
        str(PROJECT_PATH / "utils/notify_issues.py"),
        f"--build-log={build_log}",
        f"--message-templates-dir={PROJECT_PATH}/templates",
        f"--github-report-repo-name={github_repository.full_name}",
        "build",
        "r4.2",
        str(tmpdir),
        package_name,
        distribution,
        status,
    ]
    subprocess.run(cmd, check=True, env=env)

    issue_title = f"qubes-template-{template_name} 4.2.0-{timestamp} (r4.2)"
    issue = get_issue(issue_title=issue_title, repository=github_repository)

    #
    # failure
    #

    status = "failed"
    cmd = [
        str(PROJECT_PATH / "utils/notify_issues.py"),
        f"--build-log={build_log}",
        f"--message-templates-dir={PROJECT_PATH}/templates",
        f"--github-report-repo-name={github_repository.full_name}",
        "build",
        "r4.2",
        str(tmpdir),
        package_name,
        distribution,
        status,
    ]
    subprocess.run(cmd, check=True, env=env)

    # Refresh issue object
    issue.update()

    # Only one status tag
    assert len(issue.labels) == 1
    assert issue.labels[0].name == f"r4.2-failed"

    # Check that comment exists
    comments = list(issue.get_comments())
    assert len(comments) == 1
    assert (
        comments[0].body
        == f"Template {template_name}-4.2.0-{timestamp} failed to build ([build log]({build_log}))."
    )


def test_notify_02_iso_build_success_upload(token, github_repository, workdir):
    tmpdir, env = workdir
    build_log = "dummy"
    timestamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%d%H%M%s")
    distribution = "host-fc42"
    package_name = f"iso-{distribution}-4.2.{timestamp}"

    #
    # build
    #

    status = "building"
    cmd = [
        str(PROJECT_PATH / "utils/notify_issues.py"),
        f"--build-log={build_log}",
        f"--message-templates-dir={PROJECT_PATH}/templates",
        f"--github-report-repo-name={github_repository.full_name}",
        "build",
        "r4.2",
        str(tmpdir),
        package_name,
        distribution,
        status,
    ]
    subprocess.run(cmd, check=True, env=env)

    issue_title = f"iso 4.2.{timestamp} (r4.2)"
    issue_desc = f"""ISO 4.2.{timestamp} for Qubes OS r4.2, see comments below for details and build status.

For more information on how to test this update, please take a look at https://www.qubes-os.org/doc/testing/#updates.
"""

    issue = get_issue(issue_title=issue_title, repository=github_repository)

    # Check if issue has been created
    assert issue is not None

    # Only one status tag
    assert len(issue.labels) == 1
    assert issue.labels[0].name == f"r4.2-{status}"

    # Check description
    assert issue.body == issue_desc

    #
    # built
    #

    status = "built"
    cmd = [
        str(PROJECT_PATH / "utils/notify_issues.py"),
        f"--build-log={build_log}",
        f"--message-templates-dir={PROJECT_PATH}/templates",
        f"--github-report-repo-name={github_repository.full_name}",
        "build",
        "r4.2",
        str(tmpdir),
        package_name,
        distribution,
        status,
    ]
    subprocess.run(cmd, check=True, env=env)

    #
    # upload
    #
    upload_repository = "iso-testing"
    cmd = [
        PROJECT_PATH / "utils/notify_issues.py",
        f"--build-log={build_log}",
        f"--message-templates-dir={PROJECT_PATH}/templates",
        f"--github-report-repo-name={github_repository.full_name}",
        "upload",
        "r4.2",
        tmpdir,
        package_name,
        distribution,
        upload_repository,
        str(tmpdir / "state_file"),
        str(tmpdir / "stable_state_file"),
    ]
    subprocess.run(cmd, check=True, env=env)

    # Refresh issue object
    issue.update()

    # Only one status tag
    assert len(issue.labels) == 1
    assert issue.labels[0].name == f"r4.2-testing"

    # Check that comment exists
    comments = list(issue.get_comments())
    assert len(comments) == 2
    assert (
        comments[0].body
        == f"ISO for r4.2 was built ([build log]({build_log}))."
    )
    assert (
        comments[1].body == f"ISO for r4.2 was uploaded to testing repository."
    )


def test_notify_03_iso_build_failure(token, github_repository, workdir):
    tmpdir, env = workdir
    build_log = "dummy"
    timestamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%d%H%M%s")
    distribution = "host-fc42"
    package_name = f"iso-{distribution}-4.2.{timestamp}"

    #
    # build
    #

    status = "building"
    cmd = [
        str(PROJECT_PATH / "utils/notify_issues.py"),
        f"--build-log={build_log}",
        f"--message-templates-dir={PROJECT_PATH}/templates",
        f"--github-report-repo-name={github_repository.full_name}",
        "build",
        "r4.2",
        str(tmpdir),
        package_name,
        distribution,
        status,
    ]
    subprocess.run(cmd, check=True, env=env)

    issue_title = f"iso 4.2.{timestamp} (r4.2)"
    issue = get_issue(issue_title=issue_title, repository=github_repository)

    #
    # failure
    #

    status = "failed"
    cmd = [
        str(PROJECT_PATH / "utils/notify_issues.py"),
        f"--build-log={build_log}",
        f"--message-templates-dir={PROJECT_PATH}/templates",
        f"--github-report-repo-name={github_repository.full_name}",
        "build",
        "r4.2",
        str(tmpdir),
        package_name,
        distribution,
        status,
    ]
    subprocess.run(cmd, check=True, env=env)

    # Refresh issue object
    issue.update()

    # Only one status tag
    assert len(issue.labels) == 1
    assert issue.labels[0].name == f"r4.2-failed"

    # Check that comment exists
    comments = list(issue.get_comments())
    assert len(comments) == 1
    assert (
        comments[0].body
        == f"ISO for r4.2 failed to build ([build log](dummy))."
    )


def test_notify_04_component_build_success_upload(
    token, github_repository, workdir
):
    tmpdir, env = workdir
    build_log = "dummy"
    distribution = "vm-fc42"
    package_name = "core-admin-linux"
    version = "4.2.6"

    subprocess.run(
        [
            "git",
            "-C",
            str(tmpdir),
            "clone",
            "-b",
            f"v{version}",
            f"https://github.com/QubesOS/qubes-{package_name}",
            package_name,
        ],
        check=True,
    )

    #
    # build
    #

    status = "building"
    cmd = [
        str(PROJECT_PATH / "utils/notify_issues.py"),
        f"--build-log={build_log}",
        f"--message-templates-dir={PROJECT_PATH}/templates",
        f"--github-report-repo-name={github_repository.full_name}",
        "build",
        "r4.2",
        str(tmpdir / package_name),
        package_name,
        distribution,
        status,
    ]
    subprocess.run(cmd, check=True, env=env)

    # FIXME: improve generation of expected desc?
    issue_desc = f"""Update of {package_name} to v4.2.6 for Qubes OS r4.2, see comments below for details and build status.

From commit: https://github.com/QubesOS/qubes-{package_name}/commit/a6ff3071aa650f6ae9639c07e133eb27cffd91df

[Changes since previous version](https://github.com/QubesOS/qubes-{package_name}/compare/v4.2.5...v4.2.6):
QubesOS/qubes-{package_name}@a6ff307 version 4.2.6
QubesOS/qubes-{package_name}@3ddb7e5 Merge remote-tracking branch 'origin/pr/118'
QubesOS/qubes-{package_name}@690f1a7 qubes-vm-update: summary in the end of output
QubesOS/qubes-{package_name}@d362831 Move the zvol ignore rules much earlier in the udev chain of events.
QubesOS/qubes-{package_name}@241a5f7 Handle every other error condition explicitly and add -e.
QubesOS/qubes-{package_name}@dd6d3ee Fix prefix.
QubesOS/qubes-{package_name}@7ca327a Fix build.
QubesOS/qubes-{package_name}@65a1c29 This variable does not point to the right place in 64 bit systems.
QubesOS/qubes-{package_name}@26ca480 Add missing files.
QubesOS/qubes-{package_name}@2da3cf1 Tab instead of space.
QubesOS/qubes-{package_name}@9984d65 Ignore all ZFS volumes that are part of a Qubes storage pool.

Referenced issues:


If you're release manager, you can issue GPG-inline signed command:

* `Upload-component r4.2 {package_name} a6ff3071aa650f6ae9639c07e133eb27cffd91df current all` (available 5 days from now)
* `Upload-component r4.2 {package_name} a6ff3071aa650f6ae9639c07e133eb27cffd91df security-testing all`

You can choose subset of distributions like:
* `Upload-component r4.2 {package_name} a6ff3071aa650f6ae9639c07e133eb27cffd91df current vm-bookworm,vm-fc37` (available 5 days from now)

Above commands will work only if packages in current-testing repository were built from given commit (i.e. no new version superseded it).

For more information on how to test this update, please take a look at https://www.qubes-os.org/doc/testing/#updates.
"""
    issue_title = f"{package_name} v{version} (r4.2)"
    issue = get_issue(issue_title=issue_title, repository=github_repository)

    # Check if issue has been created
    assert issue is not None

    # Only one status tag
    assert len(issue.labels) == 1
    assert issue.labels[0].name == f"r4.2-{distribution}-{status}"

    # Check description
    assert issue.body == issue_desc

    # with open(tmpdir / "state_file", "w") as fd:
    #     fd.write("1178add9fcb18e865b0fc3408cfbd2baa1391024")

    with open(tmpdir / "stable_state_file", "w") as fd:
        fd.write("1178add9fcb18e865b0fc3408cfbd2baa1391024")

    #
    # built
    #
    status = "built"
    cmd = [
        str(PROJECT_PATH / "utils/notify_issues.py"),
        f"--build-log={build_log}",
        f"--message-templates-dir={PROJECT_PATH}/templates",
        f"--github-report-repo-name={github_repository.full_name}",
        "build",
        "r4.2",
        str(tmpdir / package_name),
        package_name,
        distribution,
        status,
    ]
    subprocess.run(cmd, check=True, env=env)

    # Refresh issue object
    issue.update()

    #
    # upload
    #
    upload_repository = "current-testing"
    cmd = [
        PROJECT_PATH / "utils/notify_issues.py",
        f"--build-log={build_log}",
        f"--message-templates-dir={PROJECT_PATH}/templates",
        f"--github-report-repo-name={github_repository.full_name}",
        "upload",
        "r4.2",
        str(tmpdir / package_name),
        package_name,
        distribution,
        upload_repository,
        str(tmpdir / "state_file"),
        str(tmpdir / "stable_state_file"),
    ]
    subprocess.run(cmd, check=True, env=env)

    # Refresh issue object
    issue.update()

    # Only one status tag
    assert len(issue.labels) == 1
    assert issue.labels[0].name == f"r4.2-{distribution}-cur-test"

    # Check that comment exists
    comments = list(issue.get_comments())
    assert len(comments) == 2
    assert (
        comments[0].body
        == f"Package for vm-fc42 was built ([build log]({build_log}))."
    )
    assert (
        comments[1].body
        == f"Package for vm-fc42 was uploaded to current-testing repository."
    )
