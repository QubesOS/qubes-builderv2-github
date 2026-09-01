import datetime
from pathlib import Path

import yaml

from conftest import (
    load_action_module,
    load_command_report_module,
    make_config,
)


def test_format_additional_info_base_only(workdir, monkeypatch):
    tmpdir, env = workdir
    mod = load_action_module(env, tmpdir / "qubes-builder-github", monkeypatch)
    result = mod.format_additional_info(base="Build failed")
    assert result == "**Additional info:** Build failed"


def test_format_additional_info_with_tail(workdir, monkeypatch):
    tmpdir, env = workdir
    mod = load_action_module(env, tmpdir / "qubes-builder-github", monkeypatch)
    result = mod.format_additional_info(base="Error", tail="line1\nline2")
    assert result is not None
    assert "**Additional info:** Error" in result
    assert "**Log tail" in result
    assert "line1" in result
    assert "line2" in result


def test_format_additional_info_none_base_no_tail_returns_none(
    workdir, monkeypatch
):
    tmpdir, env = workdir
    mod = load_action_module(env, tmpdir / "qubes-builder-github", monkeypatch)
    assert mod.format_additional_info(base=None) is None


def test_format_additional_info_tuple_base(workdir, monkeypatch):
    tmpdir, env = workdir
    mod = load_action_module(env, tmpdir / "qubes-builder-github", monkeypatch)
    result = mod.format_additional_info(base=("Part1", "Part2", None))
    assert result == "**Additional info:** Part1 Part2"


def test_format_additional_info_tail_only(workdir, monkeypatch):
    tmpdir, env = workdir
    mod = load_action_module(env, tmpdir / "qubes-builder-github", monkeypatch)
    result = mod.format_additional_info(base=None, tail="some log line")
    assert result is not None
    assert "**Log tail" in result
    assert "some log line" in result
    assert "Additional info" not in result


def test_format_additional_info_tail_truncated(workdir, monkeypatch):
    tmpdir, env = workdir
    mod = load_action_module(env, tmpdir / "qubes-builder-github", monkeypatch)
    long_tail = "x" * 666
    result = mod.format_additional_info(
        base="msg", tail=long_tail, max_tail_chars=100
    )
    assert result is not None
    assert "…" in result
    # Truncated tail: only last 100 chars of the original tail
    assert "x" * 100 in result
    assert "x" * 101 not in result


def test_get_log_file_valid_path(workdir, monkeypatch):
    tmpdir, env = workdir
    mod = load_action_module(env, tmpdir / "qubes-builder-github", monkeypatch)
    out = "some/path/log_build_abc123\n"
    assert (
        mod.get_log_file_from_qubesbuilder_buildlog(out)
        == "some/path/log_build_abc123"
    )


def test_get_log_file_empty_stdout_returns_none(workdir, monkeypatch):
    tmpdir, env = workdir
    mod = load_action_module(env, tmpdir / "qubes-builder-github", monkeypatch)
    assert mod.get_log_file_from_qubesbuilder_buildlog("") is None


def test_get_log_file_no_match_returns_none(workdir, monkeypatch):
    tmpdir, env = workdir
    mod = load_action_module(env, tmpdir / "qubes-builder-github", monkeypatch)
    assert (
        mod.get_log_file_from_qubesbuilder_buildlog("just some output\n")
        is None
    )


def test_get_log_file_skips_blank_lines(workdir, monkeypatch):
    tmpdir, env = workdir
    mod = load_action_module(env, tmpdir / "qubes-builder-github", monkeypatch)
    out = "\n\n\nsome/path/log_abc\n"
    assert (
        mod.get_log_file_from_qubesbuilder_buildlog(out) == "some/path/log_abc"
    )


def test_action_template_build_timestamp_skip(workdir, monkeypatch):
    tmpdir, env = workdir
    mod = load_action_module(env, tmpdir / "qubes-builder-github", monkeypatch)
    config = make_config(tmpdir / "builder.yml")

    newer_ts = datetime.datetime.now(datetime.timezone.utc)
    older_ts = newer_ts - datetime.timedelta(hours=1)

    # Write a build artifact with a newer timestamp than what we'll request
    artifacts_templates = Path(str(tmpdir)) / "artifacts" / "templates"
    artifacts_templates.mkdir(parents=True, exist_ok=True)
    ts_file = artifacts_templates / "debian-13-minimal.build.yml"
    ts_file.write_text(
        yaml.dump({"timestamp": newer_ts.strftime("%Y%m%d%H%M")})
    )

    try:
        notify_calls = []
        cli = mod.AutoActionTemplate(
            builder_dir=tmpdir / "qubes-builderv2",
            config=config,
            template_name="debian-13-minimal",
            template_timestamp=older_ts.strftime("%Y%m%d%H%M"),
            state_dir=tmpdir / "github-notify-state-ts-skip",
            commit_sha=None,
            repository_publish="templates-itl-testing",
            local_log_file=None,
            dry_run=False,
        )
        monkeypatch.setattr(
            cli,
            "notify_build_status",
            lambda **kw: notify_calls.append(kw),
        )

        cli.build()

        result = cli.get_result(cli.template.name)
        assert result.status == "skipped"
        assert "newer template" in (result.reason or "")
        assert (
            not notify_calls
        ), "notify_build_status must NOT be called when the build is skipped"
    finally:
        # Clean up so other tests (test_action_template_build) are not affected
        ts_file.unlink(missing_ok=True)


def test_action_component_build_skipped_no_packages(workdir, monkeypatch):
    tmpdir, env = workdir
    mod = load_action_module(env, tmpdir / "qubes-builder-github", monkeypatch)
    config = make_config(tmpdir / "builder.yml")
    components = config.get_components(["app-linux-split-gpg"], url_match=True)

    # Stub _check_release_status_for_component → "no packages defined" for all dists
    def fake_release_status(config, components, distributions):
        return {
            c.name: {
                d.distribution: {"status": "no packages defined", "tag": None}
                for d in distributions
            }
            for c in components
        }

    monkeypatch.setattr(
        mod, "_check_release_status_for_component", fake_release_status
    )
    monkeypatch.setattr(
        mod.BaseAutoAction, "make_with_log", lambda self, *a, **kw: None
    )

    action = mod.AutoAction(
        builder_dir=tmpdir / "qubes-builderv2",
        config=config,
        component=components[0],
        distributions=config.get_distributions(),
        state_dir=tmpdir / "github-notify-state-no-pkg",
        commit_sha=None,
        repository_publish=None,
        local_log_file=None,
        dry_run=False,
    )
    action.build()

    assert action.results, "results dict must not be empty"
    for result in action.results.values():
        assert result.status == "skipped"
        assert result.reason == "no packages defined"


def test_action_component_build_skipped_already_released(workdir, monkeypatch):
    tmpdir, env = workdir
    mod = load_action_module(env, tmpdir / "qubes-builder-github", monkeypatch)
    config = make_config(tmpdir / "builder.yml")
    components = config.get_components(["app-linux-split-gpg"], url_match=True)

    def fake_release_status(config, components, distributions):
        return {
            c.name: {
                d.distribution: {"status": "current", "tag": "v2.0.60"}
                for d in distributions
            }
            for c in components
        }

    monkeypatch.setattr(
        mod, "_check_release_status_for_component", fake_release_status
    )
    monkeypatch.setattr(
        mod.BaseAutoAction, "make_with_log", lambda self, *a, **kw: None
    )

    action = mod.AutoAction(
        builder_dir=tmpdir / "qubes-builderv2",
        config=config,
        component=components[0],
        distributions=config.get_distributions(),
        state_dir=tmpdir / "github-notify-state-released",
        commit_sha=None,
        repository_publish=None,
        local_log_file=None,
        dry_run=False,
    )
    action.build()

    for result in action.results.values():
        assert result.status == "skipped"
        assert "current" in (result.reason or "")


def test_report_parse_build_log_path_valid():
    mod = load_command_report_module()
    out = "test-vm/log_2026-01-01_00-00-00\n"
    assert mod.parse_build_log_path(out) == "test-vm/log_2026-01-01_00-00-00"


def test_report_parse_build_log_path_empty():
    mod = load_command_report_module()
    assert mod.parse_build_log_path("") is None
    assert mod.parse_build_log_path("no log path here") is None
