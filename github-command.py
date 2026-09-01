#!/usr/bin/python3
# The Qubes OS Project, http://www.qubes-os.org
#
# Copyright (C) 2022 Frédéric Pierret (fepitre) <frederic@invisiblethingslab.com>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program. If not, see <https://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: GPL-3.0-or-later

import argparse
import datetime
import logging
import os
import shlex
import subprocess
import sys
import traceback
from pathlib import Path
from typing import List

log = logging.getLogger("github-command")


class GithubCommandError(Exception):
    pass


def setup_logging():
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [github-command] %(levelname)s: %(message)s"
        )
    )
    log.addHandler(handler)
    log.setLevel(logging.DEBUG)


def command_log_tail(command_log):
    """
    Tail of this process' own command log, usually its redirected
    stdout/stderr.
    """
    if not command_log:
        return None
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        text = Path(command_log).read_text(errors="replace")
    except OSError:
        return None
    return text or None


def run_command(
    cmd, env=None, wait=False, ignore_exit_codes=(0,), stdout=None, stderr=None
):
    if wait:
        result = subprocess.run(cmd, env=env, stdout=stdout, stderr=stderr)
        if result.returncode not in ignore_exit_codes:
            raise GithubCommandError(
                f"Command exited with code {result.returncode}: {cmd}"
            )
    else:
        subprocess.Popen(cmd, env=env, stdout=stdout, stderr=stderr)


#
# dispatch subcommand
#


def _run_dispatch(args):
    scripts_dir = Path(args.scripts_dir).resolve()
    if not scripts_dir.exists():
        raise GithubCommandError("Cannot find GitHub scripts directory.")

    if args.command not in (
        "Build-component",
        "Upload-component",
        "Build-template",
        "Upload-template",
        "Build-iso",
    ):
        raise GithubCommandError("Invalid command.")

    command_file = Path(args.command_file).resolve()
    if not command_file.exists():
        raise GithubCommandError("Cannot find command file.")

    command = command_file.read_text().rstrip("\n").split()
    if command[0] != args.command:
        raise GithubCommandError("Wrong command file for requested command.")

    # one reporting issue per command, shared by all builder instances
    command_title = " ".join(command)

    timestamp = None
    component_name = None
    commit_sha = None
    repository_publish = None
    distribution_name = None
    template_name = None
    template_timestamp = None
    template_sha = None
    iso_version = None
    iso_timestamp = None
    try:
        if args.command == "Build-component":
            release_name, component_name = None, command[1]
        elif args.command == "Upload-component":
            (
                release_name,
                component_name,
                commit_sha,
                repository_publish,
                distribution_name,
            ) = command[1:]
        elif args.command == "Build-template":
            release_name, template_name, template_timestamp = command[1:]
            timestamp = datetime.datetime.strptime(
                template_timestamp + "Z", "%Y%m%d%H%M%z"
            )
        elif args.command == "Build-iso":
            release_name, iso_version, iso_timestamp = command[1:]
            timestamp = datetime.datetime.strptime(
                iso_timestamp + "Z", "%Y%m%d%H%M%z"
            )
        elif args.command == "Upload-template":
            (
                release_name,
                template_name,
                template_sha,
                repository_publish,
            ) = command[1:]
        else:
            raise GithubCommandError(f"Unsupported command: {args.command}")
    except IndexError as e:
        raise GithubCommandError(f"Wrong number of args provided: {str(e)}")

    if timestamp:
        # we are not seeking nanosecond precision
        utcnow = datetime.datetime.now(datetime.UTC)
        timestamp_max = utcnow + datetime.timedelta(minutes=5)
        timestamp_min = utcnow - datetime.timedelta(hours=1)
        if (
            timestamp.timestamp() < timestamp_min.timestamp()
            or timestamp_max.timestamp() < timestamp.timestamp()
        ):
            raise GithubCommandError(
                f"Timestamp outside of allowed range (min: {timestamp_min}, max: {timestamp_max}, current={timestamp})"
            )

    # Update GitHub Builder
    cmd = [
        "flock",
        "-x",
        "-n",
        "-E",
        "11",
        str(scripts_dir / "builder.lock"),
        "bash",
        "-c",
        f"trap 'rm -f /tmp/update-qubes-builder' EXIT && cp {str(scripts_dir / 'utils/update-qubes-builder')} /tmp && /tmp/update-qubes-builder {str(scripts_dir)}",
    ]
    if not args.no_builders_update:
        run_command(cmd, wait=args.wait, ignore_exit_codes=(0, 11))

    with open(args.config_file, "r") as f:
        content = f.read().splitlines()

    failed_builders = []
    for line in content:
        builder_release_name, builder_dir_str, builder_conf = line.split("=")

        if not Path(builder_dir_str).resolve().exists():
            log.error(f"Cannot find {builder_dir_str}")
            continue

        # Check if requested release name is supported by this builder instance
        if release_name is not None and release_name != builder_release_name:
            log.info(f"Requested release does not match builder release.")
            continue

        builder_dir = Path(builder_dir_str).resolve()

        # Update Qubes Builder
        cmd = [
            "flock",
            "-x",
            "-n",
            "-E",
            "11",
            str(builder_dir / "builder.lock"),
            str(scripts_dir / "utils/update-qubes-builder"),
            str(builder_dir),
        ]
        if not args.no_builders_update:
            try:
                run_command(cmd, wait=args.wait, ignore_exit_codes=(0, 11))
            except GithubCommandError as e:
                log.error(f"{builder_release_name}: {e}")
                failed_builders.append(builder_release_name)
                continue

        # Prepare github-command action invocation
        action_cmd = [str(scripts_dir / "github-command.py"), "action"]
        if args.signer_fpr:
            action_cmd += ["--signer-fpr", args.signer_fpr]
        else:
            action_cmd += ["--no-signer-github-command-check"]

        action_cmd += ["--command-title", command_title]
        action_cmd += ["--release-name", builder_release_name]

        if args.local_log_file:
            action_cmd += ["--local-log-file", args.local_log_file]

        # Give each action invocation its own command log, next to ours.
        # The action process reports it itself on failure.
        child_log = None
        if args.command_log:
            command_log = Path(args.command_log)
            child_log = (
                command_log.parent
                / f"{command_log.stem}-{builder_release_name}.log"
            )
            action_cmd += ["--command-log", str(child_log)]

        action_cmd += [
            str(args.command).lower(),
            str(builder_dir),
            builder_conf,
        ]
        if args.command == "Build-component":
            assert component_name
            action_cmd += [component_name]
        elif args.command == "Upload-component":
            assert (
                component_name
                and commit_sha
                and repository_publish
                and distribution_name
            )
            action_cmd += [
                component_name,
                commit_sha,
                repository_publish,
            ]
            if distribution_name == "all":
                action_cmd += ["--distribution", "all"]
            else:
                for d in distribution_name.split(","):
                    action_cmd += ["--distribution", d]
        elif args.command == "Build-template":
            assert template_name and template_timestamp
            action_cmd += [template_name, template_timestamp]
        elif args.command == "Upload-template":
            assert template_name and template_sha and repository_publish
            action_cmd += [
                template_name,
                template_sha,
                repository_publish,
            ]
        elif args.command == "Build-iso":
            assert iso_version and iso_timestamp
            action_cmd += [iso_version, iso_timestamp]

        cmd = [
            "flock",
            "-x",
            str(builder_dir / "builder.lock"),
            "bash",
            "-c",
            " ".join(shlex.quote(c) for c in action_cmd),
        ]
        child_log_fd = None
        try:
            stdout = stderr = None
            if child_log:
                child_log_fd = open(child_log, "ab")
                stdout = child_log_fd
                stderr = subprocess.STDOUT
            run_command(
                cmd,
                wait=args.wait,
                stdout=stdout,
                stderr=stderr,
                env={
                    "PYTHONPATH": f"{builder_dir!s}:{os.environ.get('PYTHONPATH','')}",
                    **os.environ,
                },
            )
        except GithubCommandError as e:
            # the action process already reported its own command log
            log.error(f"{builder_release_name}: {e}")
            failed_builders.append(builder_release_name)
        finally:
            if child_log_fd:
                child_log_fd.close()

    if failed_builders:
        raise GithubCommandError(
            f"Failed to handle command for: {', '.join(failed_builders)}"
        )


#
# action subcommand
#


def _run_action(args):
    from qubesbuilder.config import Config

    from githubbuilder.action import format_additional_info
    from githubbuilder.command_report import CommandReport, make_issue_body

    label = args.release_name or "builder"
    # the builder conf path hints which builder instance reported
    instance = f"`{label}` (`{args.builder_conf}`)"
    reporter = None
    command_issue = None
    try:
        config = Config(args.builder_conf)
        github_config = config.get("github", {})
        api_key = github_config.get("api-key")
        commands_repo = github_config.get("commands-repo")
        if args.command_title and api_key and commands_repo:
            reporter = CommandReport(token=api_key, repo_name=commands_repo)
            # entry point: the issue exists even if this instance skips
            command_issue = reporter.find_or_create_issue(
                args.command_title,
                make_issue_body(args.command_title, args.signer_fpr),
            )
        status, reason = _run_action_impl(args, config)
        if reporter and command_issue:
            if status == "handled":
                reporter.comment(command_issue, f"**handled** by {instance}")
            elif status == "refused":
                reporter.comment(
                    command_issue,
                    f"**refused** by {instance}\n\nReason: {reason}.",
                )
            elif status == "skipped" and reason:
                reporter.comment(
                    command_issue,
                    f"**skipped** by {instance}\n\nReason: {reason}.",
                )
    except Exception:
        log.exception("Failed to handle command")
        if reporter and command_issue:
            body = f"**failed** on {instance}"
            info = format_additional_info(
                base=None, tail=command_log_tail(args.command_log)
            )
            if info:
                body = f"{body}\n\n{info}"
            reporter.comment(command_issue, body)
        # already logged and reported, exit without a second traceback
        sys.exit(1)


def _run_action_impl(args, config):
    from githubbuilder.action import (
        AutoAction,
        AutoActionTemplate,
        AutoActionISO,
        AutoActionError,
        AutoActionTimeout,
        CommitMismatchError,
    )
    from qubesbuilder.config import ConfigError
    from qubesbuilder.log import QubesBuilderLogger

    log_action = QubesBuilderLogger

    commit_sha = None
    command_timestamp = None
    if args.subcommand == "upload-component":
        commit_sha = args.commit_sha
    elif args.subcommand == "build-template":
        command_timestamp = args.template_timestamp
    elif args.subcommand == "upload-template":
        commit_sha = args.template_sha
        command_timestamp = commit_sha.split("-")[-1]
    elif args.subcommand == "build-iso":
        commit_sha = args.iso_version
        command_timestamp = args.iso_timestamp

    if args.subcommand in ("upload-component", "upload-template"):
        repository_publish = args.repository_publish
    elif args.subcommand == "build-iso":
        repository_publish = "iso" if args.final else "iso-testing"
    else:
        repository_publish = None

    local_log_file = (
        Path(args.local_log_file).resolve() if args.local_log_file else None
    )

    cli_list: List = []
    dry_run = args.dry_run or config.get("github", {}).get("dry-run", False)

    if args.subcommand in ("build-component", "upload-component"):
        distributions = config.get_distributions()
        try:
            components = config.get_components(
                [args.component_name], url_match=True
            )
        except ConfigError as e:
            raise AutoActionError(
                f"No such component '{args.component_name}'."
            ) from e

        # maintainers checks
        if not args.no_signer_github_command_check:
            allowed_components = (
                config.get("github", {})
                .get("maintainers", {})
                .get(args.signer_fpr, {})
                .get("components", [])
            )
            if allowed_components != "_all_":
                components = [
                    c for c in components if c.name in allowed_components
                ]
            if not components:
                reason = f"signer `{args.signer_fpr}` is not allowed to handle component `{args.component_name}`"
                log.warning(f"Command not handled: {reason}")
                return "refused", reason

            # maintainers distributions filtering (only supported for upload)
            if args.subcommand == "upload-component":
                allowed_distributions = (
                    config.get("github", {})
                    .get("maintainers", {})
                    .get(args.signer_fpr, {})
                    .get("distributions", [])
                )
                if allowed_distributions == "_all_":
                    allowed_distributions = [
                        d.distribution for d in distributions
                    ]
                if args.distribution == ["all"]:
                    args.distribution = [d.distribution for d in distributions]
                distributions = [
                    d
                    for d in distributions
                    if d.distribution in allowed_distributions
                    and d.distribution in args.distribution
                ]
                if not distributions:
                    reason = f"signer `{args.signer_fpr}` has no allowed distribution among `{args.distribution}`"
                    log.warning(f"Command not handled: {reason}")
                    return "refused", reason

        for component in components:
            cli_list.append(
                AutoAction(
                    builder_dir=args.builder_dir,
                    config=config,
                    component=component,
                    distributions=distributions,
                    state_dir=args.state_dir,
                    commit_sha=commit_sha,
                    repository_publish=repository_publish,
                    local_log_file=local_log_file,
                    dry_run=dry_run,
                )
            )
    elif args.subcommand in ("build-template", "upload-template"):
        supported_templates = [t.name for t in config.get_templates()]
        if args.template_name not in supported_templates:
            reason = f"unsupported template `{args.template_name}`"
            log.warning(f"Command not handled: {reason}")
            return "skipped", reason
        if not args.no_signer_github_command_check:
            allowed_templates = (
                config.get("github", {})
                .get("maintainers", {})
                .get(args.signer_fpr, {})
                .get("templates", [])
            )
            if allowed_templates == "_all_":
                allowed_templates = supported_templates
            if args.template_name not in allowed_templates:
                reason = f"signer `{args.signer_fpr}` is not allowed to handle template `{args.template_name}`"
                log.warning(f"Command not handled: {reason}")
                return "refused", reason
        cli_list.append(
            AutoActionTemplate(
                builder_dir=args.builder_dir,
                config=config,
                template_name=args.template_name,
                template_timestamp=command_timestamp,
                state_dir=args.state_dir,
                commit_sha=commit_sha,
                repository_publish=repository_publish,
                local_log_file=local_log_file,
                dry_run=dry_run,
            )
        )
    elif args.subcommand == "build-iso":
        if not args.no_signer_github_command_check:
            allowed_to_trigger_build_iso = (
                config.get("github", {})
                .get("maintainers", {})
                .get(args.signer_fpr, {})
                .get("iso", False)
            )
            if not allowed_to_trigger_build_iso:
                reason = f"signer `{args.signer_fpr}` is not allowed to trigger ISO builds"
                log.warning(f"Command not handled: {reason}")
                return "refused", reason
        cli_list.append(
            AutoActionISO(
                builder_dir=args.builder_dir,
                config=config,
                iso_timestamp=command_timestamp,
                state_dir=args.state_dir,
                commit_sha=commit_sha,
                repository_publish=repository_publish,
                local_log_file=local_log_file,
                dry_run=dry_run,
            )
        )
    else:
        return "skipped", None

    for cli in cli_list:
        try:
            if args.subcommand in (
                "build-component",
                "build-template",
                "build-iso",
            ):
                cli.build()
            elif args.subcommand in ("upload-component", "upload-template"):
                cli.upload()
            else:
                return "skipped", None
        except CommitMismatchError as exc:
            # this is expected for multi-branch components, don't interrupt processing
            log_action.warning(str(exc))
        except AutoActionTimeout as autobuild_exc:
            raise AutoActionTimeout(str(autobuild_exc)) from autobuild_exc

    return "handled", None


#
# main
#


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Qubes Builder GitHub automation tool."
    )
    subparsers = parser.add_subparsers(required=True)

    # dispatch
    dispatch = subparsers.add_parser(
        "dispatch",
        help="Read a command file and dispatch to builders (old github-command behaviour).",
    )
    dispatch.set_defaults(func=_run_dispatch)
    dispatch.add_argument(
        "--command-log",
        help="Path to the command log, given to each action invocation (with a per-release suffix) for reporting on failure.",
    )
    dispatch.add_argument(
        "--no-builders-update",
        action="store_true",
        default=False,
        help="Don't update builders.",
    )
    dispatch.add_argument(
        "--wait",
        action="store_true",
        default=False,
        help="Don't put processes into background.",
    )
    dispatch.add_argument(
        "--config-file",
        default=Path.home() / ".config/qubes-builder-github/builders.list",
    )
    dispatch.add_argument(
        "--scripts-dir",
        default=Path("/usr/local/lib/qubes-builder-github"),
    )
    dispatch.add_argument(
        "--local-log-file",
        help="Use local log file instead of qubesbuilder.BuildLog RPC.",
    )
    signer = dispatch.add_mutually_exclusive_group()
    signer.add_argument(
        "--no-signer-github-command-check",
        action="store_true",
        default=False,
        help="Don't check signer fingerprint.",
    )
    signer.add_argument(
        "--signer-fpr", help="Signer GitHub command fingerprint."
    )
    dispatch.add_argument("command")
    dispatch.add_argument("command_file")

    # action
    action = subparsers.add_parser(
        "action",
        help="Run a build/upload action directly (former github-action.py CLI).",
    )
    action.set_defaults(func=_run_action)
    action_signer = action.add_mutually_exclusive_group()
    action_signer.add_argument(
        "--no-signer-github-command-check",
        action="store_true",
        default=False,
        help="Don't check signer fingerprint.",
    )
    action_signer.add_argument(
        "--signer-fpr", help="Signer GitHub command fingerprint."
    )
    action.add_argument("--dry-run", action="store_true", default=False)
    action.add_argument(
        "--command-title",
        help="Verbatim command line, used as the title of the reporting issue shared by all builder instances.",
    )
    action.add_argument(
        "--release-name",
        help="Release name identifying this instance in reporting comments.",
    )
    action.add_argument(
        "--command-log",
        help="Path to the command log, its tail is included in the failure reporting comment.",
    )
    action.add_argument(
        "--state-dir",
        default=Path.home() / "github-notify-state",
        type=Path,
    )
    action.add_argument(
        "--local-log-file",
        help="Use local log file instead of qubesbuilder.BuildLog RPC.",
    )
    action_sub = action.add_subparsers(dest="subcommand")
    action_sub.required = True

    build_component = action_sub.add_parser("build-component")
    build_component.add_argument("builder_dir", type=Path)
    build_component.add_argument("builder_conf")
    build_component.add_argument("component_name")

    upload_component = action_sub.add_parser("upload-component")
    upload_component.add_argument("builder_dir", type=Path)
    upload_component.add_argument("builder_conf")
    upload_component.add_argument("component_name")
    upload_component.add_argument("commit_sha")
    upload_component.add_argument("repository_publish")
    upload_component.add_argument("--distribution", nargs="+", default=[])

    build_template = action_sub.add_parser("build-template")
    build_template.add_argument("builder_dir", type=Path)
    build_template.add_argument("builder_conf")
    build_template.add_argument("template_name")
    build_template.add_argument("template_timestamp")

    upload_template = action_sub.add_parser("upload-template")
    upload_template.add_argument("builder_dir", type=Path)
    upload_template.add_argument("builder_conf")
    upload_template.add_argument("template_name")
    upload_template.add_argument("template_sha")
    upload_template.add_argument("repository_publish")

    build_iso = action_sub.add_parser("build-iso")
    build_iso.add_argument("builder_dir", type=Path)
    build_iso.add_argument("builder_conf")
    build_iso.add_argument("iso_version")
    build_iso.add_argument("iso_timestamp")
    build_iso.add_argument("--final", action="store_true", default=False)

    return parser


def main():
    setup_logging()
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        traceback.print_exc()
        log.error(str(e))
        sys.exit(1)
