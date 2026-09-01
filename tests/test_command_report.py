import datetime
import shutil
import subprocess
import time
from pathlib import Path

import yaml

from conftest import get_issue, set_conf_options
from test_command import FEPITRE_FPR

NOBODY_FPR = "0" * 40


def setup_report_conf(tmpdir, token, github_repository):
    """
    Copy of builder.yml with command reporting pointed at the throwaway
    test repository, referenced by builders.list, so the shared builder.yml
    keeps its empty api-key for the other test files.
    """
    conf = Path(tmpdir) / "builder-report.yml"
    shutil.copy2(Path(tmpdir) / "builder.yml", conf)
    set_conf_options(
        conf,
        {
            "github": {
                "api-key": token,
                "commands-repo": github_repository.full_name,
                "build-report-repo": github_repository.full_name,
                "dry-run": True,
            }
        },
    )
    with open(f"{tmpdir}/builders.list", "w") as f:
        f.write(f"r4.3={tmpdir}/qubes-builderv2={conf}")
    return conf


def run_dispatch(tmpdir, env, command_name, command_text, signer_fpr):
    with open(f"{tmpdir}/command", "w") as f:
        f.write(command_text)
    command_log = Path(tmpdir) / "command-report.log"
    command_log.unlink(missing_ok=True)
    cmd = [
        str(tmpdir / "qubes-builder-github/github-command.py"),
        "dispatch",
        "--wait",
        "--no-builders-update",
        "--scripts-dir",
        str(tmpdir / "qubes-builder-github"),
        "--config-file",
        f"{tmpdir}/builders.list",
        "--command-log",
        str(command_log),
        "--signer-fpr",
        signer_fpr,
        command_name,
        f"{tmpdir}/command",
    ]
    return subprocess.run(cmd, env=env, capture_output=True, text=True)


def get_comments(issue, expected, retries=3, delay=5):
    # the comments listing can lag behind a just-posted comment
    comments = []
    for attempt in range(retries):
        if attempt:
            time.sleep(delay)
        comments = list(issue.get_comments())
        if len(comments) >= expected:
            break
    return comments


def test_report_00_unsupported_template_skipped_comment(
    token, github_repository, workdir
):
    tmpdir, env = workdir
    conf = setup_report_conf(tmpdir, token, github_repository)

    timestamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%d%H%M")
    command_text = f"Build-template r4.3 no-such-template {timestamp}"

    result = run_dispatch(
        tmpdir, env, "Build-template", command_text, FEPITRE_FPR
    )
    # an unsupported template is a skip, not a failure
    assert result.returncode == 0

    issue = get_issue(issue_title=command_text, repository=github_repository)
    assert issue is not None
    assert command_text in issue.body
    assert FEPITRE_FPR in issue.body
    comments = get_comments(issue, expected=1)
    assert len(comments) == 1
    assert f"**skipped** by `r4.3` (`{conf}`)" in comments[0].body
    assert "unsupported template `no-such-template`" in comments[0].body


def test_report_01_unknown_component_failed_comment(
    token, github_repository, workdir
):
    tmpdir, env = workdir
    conf = setup_report_conf(tmpdir, token, github_repository)

    commit_sha = "c5316c91107b8930ab4dc3341bc75293139b5b84"
    command_text = f"Upload-component r4.3 no-such-component {commit_sha} security-testing vm-trixie"

    result = run_dispatch(
        tmpdir, env, "Upload-component", command_text, FEPITRE_FPR
    )
    assert result.returncode != 0
    assert "Failed to handle command for: r4.3" in result.stderr

    issue = get_issue(issue_title=command_text, repository=github_repository)
    assert issue is not None
    comments = get_comments(issue, expected=1)
    assert len(comments) == 1
    assert f"**failed** on `r4.3` (`{conf}`)" in comments[0].body
    assert "Log tail" in comments[0].body
    assert "No such component 'no-such-component'" in comments[0].body


def test_report_02_refused_comment(token, github_repository, workdir):
    tmpdir, env = workdir
    conf = setup_report_conf(tmpdir, token, github_repository)

    timestamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%d%H%M")
    command_text = f"Build-template r4.3 debian-13-minimal {timestamp}"

    result = run_dispatch(
        tmpdir, env, "Build-template", command_text, NOBODY_FPR
    )
    # a refusal is deliberate, not a failure
    assert result.returncode == 0

    issue = get_issue(issue_title=command_text, repository=github_repository)
    assert issue is not None
    comments = get_comments(issue, expected=1)
    assert len(comments) == 1
    assert f"**refused** by `r4.3` (`{conf}`)" in comments[0].body
    assert "not allowed to handle template `debian-13-minimal`" in (
        comments[0].body
    )


def test_report_03_handled_comment(token, github_repository, workdir):
    tmpdir, env = workdir
    conf = setup_report_conf(tmpdir, token, github_repository)

    now = datetime.datetime.now(datetime.UTC)
    command_timestamp = (now - datetime.timedelta(minutes=30)).strftime(
        "%Y%m%d%H%M"
    )
    command_text = f"Build-template r4.3 debian-13 {command_timestamp}"

    # a newer existing build artifact makes the template build skip
    # without an executor, the action still completes as handled
    artifacts_templates = Path(tmpdir) / "artifacts" / "templates"
    artifacts_templates.mkdir(parents=True, exist_ok=True)
    ts_file = artifacts_templates / "debian-13.build.yml"
    ts_file.write_text(yaml.dump({"timestamp": now.strftime("%Y%m%d%H%M")}))

    try:
        result = run_dispatch(
            tmpdir, env, "Build-template", command_text, FEPITRE_FPR
        )
        assert result.returncode == 0

        issue = get_issue(
            issue_title=command_text, repository=github_repository
        )
        assert issue is not None
        comments = get_comments(issue, expected=1)
        assert len(comments) == 1
        assert comments[0].body == f"**handled** by `r4.3` (`{conf}`)"
    finally:
        ts_file.unlink(missing_ok=True)
