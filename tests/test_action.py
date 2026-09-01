import asyncio
import datetime
import subprocess
import tempfile
import time
from pathlib import Path

import dnf
import pytest
import yaml

from conftest import set_conf_options, get_issue, run_cmd

PROJECT_PATH = Path(__file__).resolve().parents[1]
DEFAULT_BUILDER_CONF = PROJECT_PATH / "tests/builder.yml"

FEPITRE_FPR = "9FA64B92F95E706BF28E2CA6484010B5CDC576E2"
TESTUSER_FPR = "632F8C69E01B25C9E0C3ADF2F360C0D259FB650C"


def get_labels_and_comments(issue_title, github_repository):
    issue = get_issue(issue_title=issue_title, repository=github_repository)
    if not issue:
        return [], []
    labels = [label.name for label in issue.labels]
    comments = set([comment.body for comment in issue.get_comments()])
    return labels, comments


# From fepitre/qubes-builderv2/tests/test_cli.py
def deb_packages_list(repository_dir, suite, **kwargs):
    return (
        subprocess.check_output(
            ["reprepro", "-b", repository_dir, "list", suite],
            **kwargs,
        )
        .decode()
        .splitlines()
    )


# From fepitre/qubes-builderv2/tests/test_cli.py
def rpm_packages_list(repository_dir):
    with tempfile.TemporaryDirectory() as tmpdir:
        base = dnf.Base()
        base.conf.installroot = tmpdir
        base.conf.cachedir = tmpdir + "/cache"
        base.repos.add_new_repo(
            repoid="local", conf=base.conf, baseurl=[repository_dir]
        )
        try:
            base.fill_sack()
        except dnf.exceptions.RepoError:
            # no repo created at all, treat as empty
            return []
        q = base.sack.query()
        return [str(p) + ".rpm" for p in q.available()]


def _build_component_check(tmpdir):
    assert (
        tmpdir
        / f"artifacts/components/app-linux-split-gpg/2.0.60-1/host-fc37/publish/rpm_spec_gpg-split-dom0.spec.publish.yml"
    ).exists()

    assert (
        tmpdir
        / f"artifacts/components/app-linux-split-gpg/2.0.60-1/vm-trixie/publish/debian.publish.yml"
    ).exists()

    assert (
        tmpdir
        / f"artifacts/components/app-linux-split-gpg/2.0.60-1/vm-fc38/publish/rpm_spec_gpg-split.spec.publish.yml"
    ).exists()


def _build_component_check_multi(tmpdir):
    assert (
        tmpdir
        / f"artifacts/components/input-proxy/1.0.35-1/host-fc37/publish/rpm_spec_input-proxy.spec.publish.yml"
    ).exists()
    assert (
        tmpdir
        / f"artifacts/components/input-proxy-clone/1.0.36-1/host-fc37/publish/rpm_spec_input-proxy.spec.publish.yml"
    ).exists()

    assert (
        tmpdir
        / f"artifacts/components/input-proxy/1.0.35-1/vm-trixie/publish/debian.publish.yml"
    ).exists()
    assert (
        tmpdir
        / f"artifacts/components/input-proxy-clone/1.0.36-1/vm-trixie/publish/debian.publish.yml"
    ).exists()

    assert (
        tmpdir
        / f"artifacts/components/input-proxy/1.0.35-1/vm-fc38/publish/rpm_spec_input-proxy.spec.publish.yml"
    ).exists()
    assert (
        tmpdir
        / f"artifacts/components/input-proxy-clone/1.0.36-1/vm-fc38/publish/rpm_spec_input-proxy.spec.publish.yml"
    ).exists()


def _build_component_check_noversion(tmpdir):
    assert (
        tmpdir
        / f"artifacts/components/gui-common-noversion/4.2.4-1.1/host-fc37/publish/rpm_spec_gui-common.spec.publish.yml"
    ).exists()

    assert (
        tmpdir
        / f"artifacts/components/gui-common-noversion/4.2.4-1.1/vm-trixie/publish/debian.publish.yml"
    ).exists()

    assert (
        tmpdir
        / f"artifacts/components/gui-common-noversion/4.2.4-1.1/vm-fc38/publish/rpm_spec_gui-common.spec.publish.yml"
    ).exists()


def _fix_timestamp_artifacts_path(artifacts_path):
    info = yaml.safe_load(artifacts_path.read())

    timestamp = None
    for repo in info["repository-publish"]:
        if repo["name"] == "current-testing":
            timestamp = datetime.datetime.strptime(
                repo["timestamp"], "%Y%m%d%H%M"
            )
            break

    if not timestamp:
        raise ValueError("Cannot find timestamp value.")

    for repo in info["repository-publish"]:
        if repo["name"] == "current-testing":
            repo["timestamp"] = (
                timestamp - datetime.timedelta(days=7)
            ).strftime("%Y%m%d%H%M")
            break

    with open(artifacts_path, "w") as f:
        f.write(yaml.dump(info))


def _fix_timestamp_repo(tmpdir):
    for distribution in ["host-fc37", "vm-trixie", "vm-fc38"]:
        if distribution == "host-fc37":
            artifacts_path = (
                tmpdir
                / f"artifacts/components/app-linux-split-gpg/2.0.60-1/{distribution}/publish/rpm_spec_gpg-split-dom0.spec.publish.yml"
            )
        elif distribution == "vm-trixie":
            artifacts_path = (
                tmpdir
                / f"artifacts/components/app-linux-split-gpg/2.0.60-1/{distribution}/publish/debian.publish.yml"
            )
        else:
            artifacts_path = (
                tmpdir
                / f"artifacts/components/app-linux-split-gpg/2.0.60-1/{distribution}/publish/rpm_spec_gpg-split.spec.publish.yml"
            )
        _fix_timestamp_artifacts_path(artifacts_path)


def _upload_component_check(
    tmpdir, with_input_proxy=False, with_gui_common=False
):
    # host-fc37
    rpms = [
        "qubes-gpg-split-dom0-2.0.60-1.fc37.src.rpm",
        "qubes-gpg-split-dom0-2.0.60-1.fc37.x86_64.rpm",
    ]
    rpms_input_proxy = [
        "qubes-input-proxy-@VERSION@-1.@DIST@.src.rpm",
        "qubes-input-proxy-@VERSION@-1.@DIST@.x86_64.rpm",
        "qubes-input-proxy-debuginfo-@VERSION@-1.@DIST@.x86_64.rpm",
        "qubes-input-proxy-debugsource-@VERSION@-1.@DIST@.x86_64.rpm",
        "qubes-input-proxy-receiver-@VERSION@-1.@DIST@.x86_64.rpm",
        "qubes-input-proxy-receiver-debuginfo-@VERSION@-1.@DIST@.x86_64.rpm",
        "qubes-input-proxy-sender-@VERSION@-1.@DIST@.x86_64.rpm",
        "qubes-input-proxy-sender-debuginfo-@VERSION@-1.@DIST@.x86_64.rpm",
    ]
    rpms_testing = []
    if with_gui_common:
        rpms_testing += [
            "qubes-gui-common-devel-4.2.4-1.1.fc37.src.rpm",
            "qubes-gui-common-devel-4.2.4-1.1.fc37.noarch.rpm",
        ]
    if with_input_proxy:
        rpms_testing += [
            rpm.replace("@VERSION@", "1.0.35").replace("@DIST@", "fc37")
            for rpm in rpms_input_proxy
        ]
        rpms_testing += [
            rpm.replace("@VERSION@", "1.0.36").replace("@DIST@", "fc37")
            for rpm in rpms_input_proxy
        ]
    for repository in ["current-testing", "security-testing", "current"]:
        repository_dir = f"file://{tmpdir}/artifacts/repository-publish/rpm/r4.2/{repository}/host/fc37"
        packages = rpm_packages_list(repository_dir)
        if repository == "current-testing":
            assert set(rpms + rpms_testing) == set(packages)
        elif repository == "security-testing":
            assert set([]) == set(packages)
        else:
            assert set(rpms) == set(packages)

    # vm-fc38
    rpms = [
        "qubes-gpg-split-2.0.60-1.fc38.src.rpm",
        "qubes-gpg-split-2.0.60-1.fc38.x86_64.rpm",
        "qubes-gpg-split-tests-2.0.60-1.fc38.x86_64.rpm",
        "qubes-gpg-split-debuginfo-2.0.60-1.fc38.x86_64.rpm",
        "qubes-gpg-split-debugsource-2.0.60-1.fc38.x86_64.rpm",
    ]
    rpms_testing = []
    if with_gui_common:
        rpms_testing += [
            "qubes-gui-common-devel-4.2.4-1.1.fc38.src.rpm",
            "qubes-gui-common-devel-4.2.4-1.1.fc38.noarch.rpm",
        ]
    if with_input_proxy:
        rpms_testing += [
            rpm.replace("@VERSION@", "1.0.35").replace("@DIST@", "fc38")
            for rpm in rpms_input_proxy
        ]
        rpms_testing += [
            rpm.replace("@VERSION@", "1.0.36").replace("@DIST@", "fc38")
            for rpm in rpms_input_proxy
        ]
    for repository in ["current-testing", "security-testing", "current"]:
        repository_dir = f"file://{tmpdir}/artifacts/repository-publish/rpm/r4.2/{repository}/vm/fc38"
        packages = rpm_packages_list(repository_dir)
        if repository == "current-testing":
            assert set(rpms + rpms_testing) == set(packages)
        elif repository == "security-testing":
            assert set([]) == set(packages)
        else:
            assert set(rpms) == set(packages)

    # vm-trixie
    repository_dir = tmpdir / "artifacts/repository-publish/deb/r4.2/vm"
    for codename in [
        "trixie-testing",
        "trixie-securitytesting",
        "trixie",
    ]:
        packages = deb_packages_list(repository_dir, codename)
        expected_packages = [
            f"{codename}|main|amd64: qubes-gpg-split 2.0.60-1+deb13u1",
            f"{codename}|main|amd64: qubes-gpg-split-dbgsym 2.0.60-1+deb13u1",
            f"{codename}|main|amd64: qubes-gpg-split-tests 2.0.60-1+deb13u1",
            f"{codename}|main|source: qubes-gpg-split 2.0.60-1+deb13u1",
        ]
        if "-testing" in codename and with_gui_common:
            expected_packages += [
                f"{codename}|main|amd64: qubes-gui-common 4.2.4+deb13u1+devel1",
                f"{codename}|main|source: qubes-gui-common 4.2.4+deb13u1+devel1",
            ]
        if "-testing" in codename and with_input_proxy:
            # default reprepro keeps only the latest version,
            # 1.0.35 won't be visible here
            expected_packages += [
                f"{codename}|main|source: qubes-input-proxy 1.0.36-1+deb13u1",
                f"{codename}|main|amd64: qubes-input-proxy-sender 1.0.36-1+deb13u1",
                f"{codename}|main|amd64: qubes-input-proxy-sender-dbgsym 1.0.36-1+deb13u1",
                f"{codename}|main|amd64: qubes-input-proxy-receiver 1.0.36-1+deb13u1",
                f"{codename}|main|amd64: qubes-input-proxy-receiver-dbgsym 1.0.36-1+deb13u1",
            ]
        assert set(packages) == set(expected_packages)


def _build_template_check(tmpdir):
    assert (
        tmpdir / f"artifacts/templates/debian-13-minimal.publish.yml"
    ).exists()


def _fix_template_timestamp_repo(tmpdir):
    artifacts_path = (
        tmpdir / f"artifacts/templates/debian-13-minimal.publish.yml"
    )
    info = yaml.safe_load(artifacts_path.read())
    publish_timestamp = None
    for repo in info["repository-publish"]:
        if repo["name"] == "templates-itl-testing":
            publish_timestamp = datetime.datetime.strptime(
                repo["timestamp"], "%Y%m%d%H%M"
            )
            break

    if not publish_timestamp:
        raise ValueError("Cannot find timestamp value.")

    for repo in info["repository-publish"]:
        if repo["name"] == "templates-itl-testing":
            repo["timestamp"] = (
                publish_timestamp - datetime.timedelta(days=7)
            ).strftime("%Y%m%d%H%M")
            break

    with open(artifacts_path, "w") as f:
        f.write(yaml.dump(info))

    return info


def _upload_template_check(tmpdir, build_timestamp):
    # host-fc37
    rpms = [
        f"qubes-template-debian-13-minimal-4.2.0-{build_timestamp}.noarch.rpm",
    ]
    for repository in ["templates-itl-testing", "templates-itl"]:
        repository_dir = f"file://{tmpdir}/artifacts/repository-publish/rpm/r4.2/{repository}"
        packages = rpm_packages_list(repository_dir)
        assert set(rpms) == set(packages)


def _build_iso_check(tmpdir, timestamp):
    iso_file = tmpdir / f"artifacts/iso/Qubes-4.2.{timestamp}-x86_64.iso"
    latest_timestamp_file = (
        tmpdir / f"artifacts/installer/latest_fc37_iso_timestamp"
    )

    assert iso_file.exists()
    assert latest_timestamp_file.exists()

    with open(latest_timestamp_file, "r") as f:
        latest_timestamp = f.read().rstrip("\n")

    assert timestamp == latest_timestamp


def test_action_component_build(token, github_repository, workdir):
    tmpdir, env = workdir
    set_conf_options(
        tmpdir / "builder.yml",
        {
            "github": {
                "api-key": token,
                "build-report-repo": github_repository.full_name,
            }
        },
    )
    cmd = [
        str(PROJECT_PATH / "github-command.py"),
        "action",
        "--local-log-file",
        f"{tmpdir}/build-component.log",
        "--no-signer-github-command-check",
        "build-component",
        f"{tmpdir}/qubes-builderv2",
        f"{tmpdir}/builder.yml",
        "app-linux-split-gpg",
    ]
    run_cmd(cmd, check=True, env=env, capture_output=True, text=True)
    _build_component_check(tmpdir)

    labels, comments = get_labels_and_comments(
        "app-linux-split-gpg v2.0.60 (r4.2)", github_repository
    )

    # Check that labels exist
    assert set(labels) == {
        "r4.2-host-cur-test",
        "r4.2-vm-fc38-cur-test",
        "r4.2-vm-trixie-cur-test",
    }

    # Check that comments exist
    assert comments == {
        f"Package for host was built ([build log]({tmpdir / 'build-component.log'})).",
        f"Package for vm-fc38 was built ([build log]({tmpdir / 'build-component.log'})).",
        f"Package for vm-trixie was built ([build log]({tmpdir / 'build-component.log'})).",
        "Package for host was uploaded to current-testing repository.",
        "Package for vm-fc38 was uploaded to current-testing repository.",
        "Package for vm-trixie was uploaded to current-testing repository.",
    }


def test_action_component_build_multi(workdir):
    tmpdir, env = workdir

    cmd = [
        str(PROJECT_PATH / "github-command.py"),
        "action",
        "--local-log-file",
        f"{tmpdir}/build-component.log",
        "--signer-fpr",
        FEPITRE_FPR,
        "build-component",
        f"{tmpdir}/qubes-builderv2",
        f"{tmpdir}/builder.yml",
        "app-linux-input-proxy",
    ]
    run_cmd(cmd, check=True, env=env, capture_output=True, text=True)

    _build_component_check_multi(tmpdir)


def test_action_component_build_noversion(token, github_repository, workdir):
    tmpdir, env = workdir
    set_conf_options(
        tmpdir / "builder.yml",
        {
            "github": {
                "api-key": token,
                "build-report-repo": github_repository.full_name,
            },
            "increment-devel-versions": True,
        },
    )
    cmd = [
        str(PROJECT_PATH / "github-command.py"),
        "action",
        "--local-log-file",
        f"{tmpdir}/build-component.log",
        "--no-signer-github-command-check",
        "build-component",
        f"{tmpdir}/qubes-builderv2",
        f"{tmpdir}/builder.yml",
        "gui-common",
    ]
    run_cmd(cmd, check=True, env=env, capture_output=True, text=True)
    _build_component_check_noversion(tmpdir)

    labels, comments = get_labels_and_comments(
        "gui-common-noversion v4.2.4-1-g1af0612 (r4.2)", github_repository
    )

    # Check that labels exist
    assert set(labels) == {
        "r4.2-host-cur-test",
        "r4.2-vm-fc38-cur-test",
        "r4.2-vm-trixie-cur-test",
    }

    # Check that comments exist
    assert comments == {
        f"Package for host was built ([build log]({tmpdir / 'build-component.log'})).",
        f"Package for vm-fc38 was built ([build log]({tmpdir / 'build-component.log'})).",
        f"Package for vm-trixie was built ([build log]({tmpdir / 'build-component.log'})).",
        "Package for host was uploaded to current-testing repository.",
        "Package for vm-fc38 was uploaded to current-testing repository.",
        "Package for vm-trixie was uploaded to current-testing repository.",
    }


def test_action_component_upload(workdir):
    tmpdir, env = workdir

    cmd = [
        str(PROJECT_PATH / "github-command.py"),
        "action",
        "--local-log-file",
        f"{tmpdir}/upload-component.log",
        "--signer-fpr",
        FEPITRE_FPR,
        "upload-component",
        f"{tmpdir}/qubes-builderv2",
        f"{tmpdir}/builder.yml",
        "app-linux-split-gpg",
        "c5316c91107b8930ab4dc3341bc75293139b5b84",
        "security-testing",
        "--distribution",
        "vm-trixie",
    ]
    run_cmd(cmd, check=True, env=env, capture_output=True, text=True)

    _fix_timestamp_repo(tmpdir)

    cmd = [
        str(PROJECT_PATH / "github-command.py"),
        "action",
        "--local-log-file",
        f"{tmpdir}/upload-component.log",
        "--signer-fpr",
        FEPITRE_FPR,
        "upload-component",
        f"{tmpdir}/qubes-builderv2",
        f"{tmpdir}/builder.yml",
        "app-linux-split-gpg",
        "c5316c91107b8930ab4dc3341bc75293139b5b84",
        "current",
        "--distribution",
        "all",
    ]
    run_cmd(cmd, check=True, env=env, capture_output=True, text=True)
    _upload_component_check(tmpdir, with_input_proxy=True, with_gui_common=True)


def test_action_template_build(token, github_repository, workdir):
    tmpdir, env = workdir
    set_conf_options(
        tmpdir / "builder.yml",
        {
            "github": {
                "api-key": token,
                "build-report-repo": github_repository.full_name,
            }
        },
    )

    timestamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%d%H%M")
    with open(tmpdir / "timestamp", "w") as f:
        f.write(timestamp)

    cmd = [
        str(PROJECT_PATH / "github-command.py"),
        "action",
        "--local-log-file",
        f"{tmpdir}/build-template.log",
        "--signer-fpr",
        FEPITRE_FPR,
        "build-template",
        f"{tmpdir}/qubes-builderv2",
        f"{tmpdir}/builder.yml",
        "debian-13-minimal",
        timestamp,
    ]
    run_cmd(cmd, check=True, env=env, capture_output=True, text=True)
    _build_template_check(tmpdir)

    labels, comments = get_labels_and_comments(
        f"qubes-template-debian-13-minimal 4.2.0-{timestamp} (r4.2)",
        github_repository,
    )

    # Check that labels exist
    assert set(labels) == {"r4.2-testing"}

    # Check that comments exist
    assert comments == {
        f"Template debian-13-minimal-4.2.0-{timestamp} was built ([build log]({tmpdir / 'build-template.log'})).",
        f"Template debian-13-minimal-4.2.0-{timestamp} was uploaded to templates-itl-testing repository.",
    }


def test_action_template_upload(token, github_repository, workdir):
    tmpdir, env = workdir
    set_conf_options(
        tmpdir / "builder.yml",
        {
            "github": {
                "api-key": token,
                "build-report-repo": github_repository.full_name,
            }
        },
    )

    info = _fix_template_timestamp_repo(tmpdir)
    build_timestamp = info["timestamp"]

    cmd = [
        str(PROJECT_PATH / "github-command.py"),
        "action",
        "--local-log-file",
        f"{tmpdir}/upload-template.log",
        "--signer-fpr",
        FEPITRE_FPR,
        "upload-template",
        f"{tmpdir}/qubes-builderv2",
        f"{tmpdir}/builder.yml",
        "debian-13-minimal",
        f"4.2.0-{build_timestamp}",
        "templates-itl",
    ]
    run_cmd(cmd, check=True, env=env, capture_output=True, text=True)
    _upload_template_check(tmpdir, build_timestamp)

    labels, comments = get_labels_and_comments(
        f"qubes-template-debian-13-minimal 4.2.0-{build_timestamp} (r4.2)",
        github_repository,
    )

    # Check that labels exist
    assert set(labels) == {"r4.2-stable"}

    # Check that comments exist
    assert comments == {
        f"Template debian-13-minimal-4.2.0-{build_timestamp} was built ([build log]({tmpdir / 'build-template.log'})).",
        f"Template debian-13-minimal-4.2.0-{build_timestamp} was uploaded to templates-itl-testing repository.",
        f"Template debian-13-minimal-4.2.0-{build_timestamp} was uploaded to templates-itl repository.",
    }


@pytest.mark.asyncio
async def test_action_iso_build(token, github_repository, workdir):
    tmpdir, env = workdir
    set_conf_options(
        tmpdir / "builder.yml",
        {
            "github": {
                "api-key": token,
                "build-report-repo": github_repository.full_name,
            }
        },
    )

    timestamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%d%H%M")
    with open(tmpdir / "timestamp", "w") as f:
        f.write(timestamp)

    cmd = [
        str(PROJECT_PATH / "github-command.py"),
        "action",
        "--local-log-file",
        f"{tmpdir}/build-iso.log",
        "--signer-fpr",
        FEPITRE_FPR,
        "build-iso",
        f"{tmpdir}/qubes-builderv2",
        f"{tmpdir}/builder.yml",
        f"4.2.{timestamp}",
        timestamp,
    ]

    process = await asyncio.create_subprocess_exec(
        *cmd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    # Give some time for creating issue
    time.sleep(10)
    labels, _ = get_labels_and_comments(
        f"iso 4.2.{timestamp} (r4.2)",
        github_repository,
    )

    # Check that labels exist
    assert set(labels) == {"r4.2-building"}

    await process.communicate()

    assert process.returncode == 0

    labels, comments = get_labels_and_comments(
        f"iso 4.2.{timestamp} (r4.2)",
        github_repository,
    )

    # Check that labels exist
    assert set(labels) == {"r4.2-testing"}

    # Check that comments exist
    assert comments == {
        f"ISO for r4.2 was built ([build log]({tmpdir}/build-iso.log)).",
        f"ISO for r4.2 was uploaded to testing repository.",
    }
