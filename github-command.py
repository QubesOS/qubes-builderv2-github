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
import sys
import os
import subprocess
import datetime
import logging
from pathlib import Path

log = logging.getLogger("github-command")


class GithubCommandError(Exception):
    pass


def run_command(cmd, env=None, wait=False, ignore_exit_codes=(0,)):
    if wait:
        try:
            subprocess.run(cmd, env=env, check=True, capture_output=True)
        except subprocess.CalledProcessError as e:
            if e.returncode in ignore_exit_codes:
                return
            raise GithubCommandError(f"Failed to run command: {e.stderr}")
    else:
        subprocess.Popen(cmd, env=env)


def main():

    parser = argparse.ArgumentParser()
    parser.add_argument("--log-basename")
    parser.add_argument(
        "--no-builders-update",
        action="store_true",
        default=False,
        help="Don't update builders.",
    )
    parser.add_argument(
        "--wait",
        action="store_true",
        default=False,
        help="Don't put processes into background.",
    )
    parser.add_argument(
        "--config-file",
        default=Path.home() / ".config/qubes-builder-github/builders.list",
    )
    parser.add_argument(
        "--scripts-dir", default=Path("/usr/local/lib/qubes-builder-github")
    )
    parser.add_argument(
        "--local-log-file",
        help="Use local log file instead of qubesbuilder.BuildLog RPC.",
    )
    signer = parser.add_mutually_exclusive_group()
    signer.add_argument(
        "--no-signer-github-command-check",
        action="store_true",
        default=False,
        help="Don't check signer fingerprint.",
    )
    signer.add_argument(
        "--signer-fpr",
        help="Signer GitHub command fingerprint.",
    )
    parser.add_argument("command")
    parser.add_argument("command_file")

    args = parser.parse_args()

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
                f"Timestamp outside of allowed range (min: {timestamp_min}, max: {timestamp_max}, current={timestamp}"
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
            run_command(cmd, wait=args.wait, ignore_exit_codes=(0, 11))

        # Prepare github-action
        github_action_cmd = [str(scripts_dir / "github-action.py")]
        if args.signer_fpr:
            github_action_cmd += ["--signer-fpr", args.signer_fpr]
        else:
            github_action_cmd += ["--no-signer-github-command-check"]

        if args.local_log_file:
            github_action_cmd += ["--local-log-file", args.local_log_file]

        github_action_cmd += [
            str(args.command).lower(),
            str(builder_dir),
            builder_conf,
        ]
        if args.command == "Build-component":
            assert component_name
            github_action_cmd += [component_name]
        elif args.command == "Upload-component":
            assert (
                component_name
                and commit_sha
                and repository_publish
                and distribution_name
            )
            github_action_cmd += [
                component_name,
                commit_sha,
                repository_publish,
            ]
            if distribution_name == "all":
                github_action_cmd += ["--distribution", "all"]
            else:
                for d in distribution_name.split(","):
                    github_action_cmd += ["--distribution", d]
        elif args.command == "Build-template":
            assert template_name and template_timestamp
            github_action_cmd += [template_name, template_timestamp]
        elif args.command == "Upload-template":
            assert template_name and template_sha and repository_publish
            github_action_cmd += [
                template_name,
                template_sha,
                repository_publish,
            ]
        elif args.command == "Build-iso":
            assert iso_version and iso_timestamp
            github_action_cmd += [iso_version, iso_timestamp]
        cmd = [
            "flock",
            "-x",
            str(builder_dir / "builder.lock"),
            "bash",
            "-c",
            " ".join(github_action_cmd),
        ]
        run_command(
            cmd,
            wait=args.wait,
            env={
                "PYTHONPATH": f"{builder_dir!s}:{os.environ.get('PYTHONPATH','')}",
                **os.environ,
            },
        )


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log.error(str(e))
        sys.exit(1)
