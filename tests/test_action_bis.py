import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import load_action_module, make_config, set_conf_options
from test_action import (
    get_labels_and_comments,
)


@pytest.fixture(autouse=True)
def clean_state(workdir, github_repository):
    """
    Reset per-test state: builder.yml, notify-state dir, and GitHub issues.
    """
    tmpdir, _env = workdir
    builder_conf = tmpdir / "builder.yml"
    original_content = builder_conf.read()

    def _clean_github():
        state_dir = tmpdir / "github-notify-state"
        if state_dir.exists():
            state_dir.remove(rec=True)
        for issue in github_repository.get_issues(state="open"):
            for comment in issue.get_comments():
                comment.delete()
            issue.edit(state="closed", labels=[])

    # Clean before the test so state left by other test files is not visible
    _clean_github()

    yield

    # Restore builder.yml and clean up after so the next test starts fresh
    builder_conf.write(original_content)
    _clean_github()


class _SubprocessPopen:
    """
    Proxies the subprocess module but replaces Popen with a test shim.
    """

    def __init__(self, popen_override):
        self._popen = popen_override

    @property
    def Popen(self):
        return self._popen

    def __getattr__(self, name):
        return getattr(subprocess, name)


def _make_auto_action(action_module, workdir, log_path=None):
    """
    Build an AutoAction for app-linux-split-gpg using the standard workdir layout.
    """
    tmpdir, _env = workdir
    config = make_config(tmpdir / "builder.yml")
    components = config.get_components(["app-linux-split-gpg"], url_match=True)
    return action_module.AutoAction(
        builder_dir=tmpdir / "qubes-builderv2",
        config=config,
        component=components[0],
        distributions=config.get_distributions(),
        state_dir=tmpdir / "github-notify-state",
        commit_sha=None,
        repository_publish=None,
        local_log_file=Path(str(log_path)) if log_path is not None else None,
        dry_run=False,
    )


def test_action_component_build_failure_includes_tail(
    token, github_repository, workdir, monkeypatch
):
    tmpdir, env = workdir

    # Load github/action.py as a module for patching
    action_module = load_action_module(
        env, tmpdir / "qubes-builder-github", monkeypatch
    )

    # Configure GitHub credentials used by NotifyIssueCli
    set_conf_options(
        tmpdir / "builder.yml",
        {
            "github": {
                "api-key": token,
                "build-report-repo": github_repository.full_name,
            }
        },
    )

    # Patch _component_stage inside github/action.py: pass fetch through so the
    # issue is created first, then fail on the actual build stages.
    real_component_stage = action_module._component_stage

    def failing_component_stage(*args, **kwargs):
        if "fetch" not in kwargs.get("stages", []):
            raise RuntimeError("injected failure for test")
        return real_component_stage(*args, **kwargs)

    monkeypatch.setattr(
        action_module, "_component_stage", failing_component_stage
    )

    action = _make_auto_action(
        action_module, workdir, log_path=tmpdir / "build-component.log"
    )
    action.build()

    # Validate the resulting GitHub comment contains the tail formatting
    labels, comments = get_labels_and_comments(
        "app-linux-split-gpg v2.0.60 (r4.2)", github_repository
    )
    joined = "\n".join(comments)

    assert "Additional info" in joined
    assert "Log tail" in joined or "Last 30 log lines" in joined
    assert "injected failure for test" in joined


def _make_popen_shim(project_path: Path, tmp_home: Path):
    real_popen = subprocess.Popen
    buildlog_cmd = project_path / "rpc-services/qubesbuilder.BuildLog"

    def popen_shim(args, *popen_args, **popen_kwargs):
        # match exactly what github/action.py calls
        if isinstance(args, (list, tuple)) and list(args[:3]) == [
            "qrexec-client-vm",
            "dom0",
            "qubesbuilder.BuildLog",
        ]:
            # Ensure BuildLog script has what it expects
            env = dict(os.environ)
            env.setdefault("QREXEC_REMOTE_DOMAIN", "testvm")
            env["HOME"] = str(tmp_home)  # so logs go under tmpdir
            # optional: isolate any hooks/dirs
            Path(tmp_home / "QubesIncomingBuildLog").mkdir(
                parents=True, exist_ok=True
            )

            popen_kwargs = dict(popen_kwargs)
            popen_kwargs["env"] = env

            # Run local BuildLog instead of qrexec-client-vm
            return real_popen(buildlog_cmd, *popen_args, **popen_kwargs)

        return real_popen(args, *popen_args, **popen_kwargs)

    return popen_shim


def test_action_component_build_qrexec_log_path(
    token, github_repository, workdir, monkeypatch
):
    tmpdir, env = workdir
    action_module = load_action_module(
        env, tmpdir / "qubes-builder-github", monkeypatch
    )

    set_conf_options(
        tmpdir / "builder.yml",
        {
            "github": {
                "api-key": token,
                "build-report-repo": github_repository.full_name,
            },
            "distributions": ["host-fc37"],
        },
    )

    # Redirect qrexec-client-vm calls to the local BuildLog script.
    # Patch only action_module's subprocess reference so the global module is untouched.
    popen_shim = _make_popen_shim(
        tmpdir / "qubes-builder-github", tmp_home=tmpdir
    )
    monkeypatch.setattr(
        action_module, "subprocess", _SubprocessPopen(popen_shim)
    )

    action = _make_auto_action(action_module, workdir, log_path=None)
    action.build()

    labels, comments = get_labels_and_comments(
        "app-linux-split-gpg v2.0.60 (r4.2)", github_repository
    )

    # Expect build comments include build log link
    joined = "\n".join(comments)
    assert "/log_" in joined


def test_action_component_build_qrexec_brokenpipe_includes_tail(
    token, github_repository, workdir, monkeypatch
):
    tmpdir, env = workdir
    action_module = load_action_module(
        env, tmpdir / "qubes-builder-github", monkeypatch
    )

    set_conf_options(
        tmpdir / "builder.yml",
        {
            "github": {
                "api-key": token,
                "build-report-repo": github_repository.full_name,
            },
            "distributions": ["vm-trixie"],
        },
    )

    # Simulate a BuildLog process that exits immediately so writes trigger BrokenPipe.
    # Patch only action_module's subprocess reference so the global module is untouched.
    def popen_dead(args, *a, **kw):
        if isinstance(args, (list, tuple)) and list(args[:3]) == [
            "qrexec-client-vm",
            "dom0",
            "qubesbuilder.BuildLog",
        ]:
            return subprocess.Popen(
                [sys.executable, "-c", "import sys; sys.exit(0)"], *a, **kw
            )
        return subprocess.Popen(args, *a, **kw)

    monkeypatch.setattr(
        action_module, "subprocess", _SubprocessPopen(popen_dead)
    )

    # Force a failure during build stages (not fetch) to trigger AutoActionError + tail.
    real_component_stage = action_module._component_stage

    def fail_during_build(*args, **kwargs):
        if "fetch" not in kwargs.get("stages", []):
            action_module.log.error("something went wrong")
            action_module.log.error("traceback: boom bada boum")
            raise RuntimeError("Injected failure")
        return real_component_stage(*args, **kwargs)

    monkeypatch.setattr(action_module, "_component_stage", fail_during_build)

    action = _make_auto_action(action_module, workdir, log_path=None)
    action.build()

    _, comments = get_labels_and_comments(
        "app-linux-split-gpg v2.0.60 (r4.2)", github_repository
    )
    joined = "\n".join(comments)

    # Tail must be present even though BuildLog process died
    assert "Log tail" in joined or "Last 30 log lines" in joined
    assert "something went wrong" in joined
    assert "traceback: boom bada boum" in joined
