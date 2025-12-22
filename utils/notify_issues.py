#!/usr/bin/python3
# coding=utf-8
#
# The Qubes OS Project, https://www.qubes-os.org/
#
# Copyright (C) 2015 Marek Marczykowski-Górecki <marmarek@invisiblethingslab.com>
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
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#

import logging
import os
import re
import subprocess
import sys
from argparse import ArgumentParser
from pathlib import Path
from typing import Optional

from github import Github, GithubException, Auth

from qubesbuilder.distribution import QubesDistribution

log = logging.getLogger("notify-issues")

github_issues_repo = "QubesOS/qubes-issues"
github_api_prefix = "https://api.github.com"
github_repo_prefix = "QubesOS/qubes-"
github_baseurl = "https://github.com"

fixes_re = re.compile(
    r"(fixes|closes)( (https://github.com/[^ ]+/|"
    r"QubesOS/Qubes-issues#)[0-9]+)",
    re.IGNORECASE,
)
issue_re = re.compile(r"QubesOS/Qubes-issues(#|/issues/)[0-9]+", re.IGNORECASE)
cleanup_re = re.compile(r"[^ ]+[#/]")
release_name_re = re.compile("r[0-9.]+")
number_re = re.compile('"number": *([0-9]+)')


class NotifyIssueError(Exception):
    pass


class NotifyIssueCli:
    def __init__(
        self,
        token: str,
        release_name: str,
        source_dir: Path,
        message_templates_dir: Path,
        github_report_repo_name: str,
        min_age_days: int,
    ):
        self.token = token or None
        self.release_name = release_name
        self.source_dir = source_dir
        self.message_templates_dir = message_templates_dir
        self.github_report_repo_name = github_report_repo_name
        self.min_age_days = min_age_days
        self.gi = Github(auth=Auth.Token(self.token), retry=5, seconds_between_requests=1)

    def get_labels(
        self, command, repository_type, build_status, dist_label, package_name
    ):
        if package_name.startswith("iso") or package_name.startswith(
            "qubes-template"
        ):
            prefix_label = f"{self.release_name}"
        else:
            prefix_label = f"{self.release_name}-{dist_label}"

        add_labels = []
        delete_labels = []
        if command == "upload" and build_status == "uploaded":
            delete_labels = [
                f"{prefix_label}-failed",
                f"{prefix_label}-building",
            ]
            if repository_type in ("current", "stable"):
                delete_labels += [
                    f"{prefix_label}-cur-test",
                    f"{prefix_label}-sec-test",
                ]
                add_labels = [
                    f"{prefix_label}-stable",
                    f"{self.release_name}-stable",
                ]
            elif repository_type == "current-testing":
                add_labels = [f"{prefix_label}-cur-test"]
            elif repository_type == "security-testing":
                add_labels = [f"{prefix_label}-sec-test"]
            elif repository_type == "templates-itl":
                delete_labels += [f"{self.release_name}-testing"]
                add_labels = [f"{self.release_name}-stable"]
            elif repository_type == "templates-itl-testing":
                add_labels += [f"{self.release_name}-testing"]
            elif repository_type == "templates-community":
                delete_labels += [f"{self.release_name}-testing", "iso"]
                add_labels = [f"{self.release_name}-stable"]
            elif repository_type in (
                "templates-community-testing",
                "iso-testing",
            ):
                add_labels = [f"{self.release_name}-testing"]
            else:
                log.warning(f"Ignoring {repository_type}")
                return [], []
        elif command == "build":
            if build_status == "failed":
                add_labels = [f"{prefix_label}-failed"]
                delete_labels = [f"{prefix_label}-building"]
            elif build_status == "building":
                add_labels = [f"{prefix_label}-building"]
                # we ensure that we don't keep those labels in case of previous failures
                delete_labels = [f"{prefix_label}-failed"]
            elif build_status == "built":
                delete_labels = [
                    f"{prefix_label}-failed",
                    f"{prefix_label}-building",
                ]
            else:
                delete_labels = []

        return add_labels, delete_labels

    def get_current_commit(self):
        git_proc = subprocess.Popen(
            [
                "git",
                "-C",
                str(self.source_dir),
                "log",
                "-n",
                "1",
                "--pretty=format:%H",
            ],
            stdout=subprocess.PIPE,
        )
        (b_current_commit, _) = git_proc.communicate()
        current_commit = b_current_commit.decode().strip()
        return current_commit

    def get_package_changes(
        self, git_url, current_commit, previous_current_commit=None
    ):
        """Returns a tuple of:
        - current version
        - previous version
        - git short log formatted with GitHub links
        - referenced GitHub issues, in GitHub syntax
        """
        git_proc = subprocess.Popen(
            [
                "git",
                "-C",
                str(self.source_dir),
                "describe",
                "--match",
                "v*",
                "--always",
            ],
            stdout=subprocess.PIPE,
        )
        (version_tags, _) = git_proc.communicate()
        versions = version_tags.decode().splitlines()
        if not versions:
            raise ValueError("No version tags found")
        version = versions[0]

        # get previous version
        if previous_current_commit:
            git_proc = subprocess.Popen(
                [
                    "git",
                    "-C",
                    str(self.source_dir),
                    "describe",
                    "--match",
                    "v*",
                    "--exact-match",
                    previous_current_commit,
                ],
                stdout=subprocess.PIPE,
            )
            (version_tags, _) = git_proc.communicate()
            if not version_tags:
                # if no tag there, point at the commit directly
                version_tags = previous_current_commit.encode()
        else:
            git_proc = subprocess.Popen(
                [
                    "git",
                    "-C",
                    str(self.source_dir),
                    "describe",
                    "--match",
                    "v*",
                    "--abbrev=0",
                    current_commit + "~",
                ],
                stdout=subprocess.PIPE,
            )
            (version_tags, _) = git_proc.communicate()
        if not version_tags:
            # if no previous version tag, check from (some) root commit
            git_proc = subprocess.Popen(
                [
                    "git",
                    "-C",
                    str(self.source_dir),
                    "rev-list",
                    "--max-parents=0",
                    current_commit + "~",
                ],
                stdout=subprocess.PIPE,
            )
            (version_tags, _) = git_proc.communicate()

        if not version_tags:
            # still nothing - looks there is only one commit - no history
            return version, version, "", ""
        previous_version = version_tags.decode().splitlines()[0]

        git_log_proc = subprocess.Popen(
            [
                "git",
                "-C",
                str(self.source_dir),
                "log",
                "{}..{}".format(previous_version, version),
            ],
            stdout=subprocess.PIPE,
        )

        (b_git_log, _) = git_log_proc.communicate()
        git_log = b_git_log.decode()
        referenced_issues = []
        for line in git_log.splitlines():
            match = issue_re.search(line)
            if match:
                issues_string = match.group(0)
                issues_numbers = [
                    int(cleanup_re.sub("", s)) for s in issues_string.split()
                ]
                referenced_issues.extend(issues_numbers)

        referenced_issues_txt = "\n".join(
            "QubesOS/qubes-issues#{}".format(x) for x in set(referenced_issues)
        )

        github_full_repo_name = "/".join(git_url.split("/")[-2:])

        git_log_proc = subprocess.Popen(
            [
                "git",
                "-C",
                str(self.source_dir),
                "log",
                "--pretty=format:{}@%h %s".format(github_full_repo_name),
                "{}..{}".format(previous_version, version),
            ],
            stdout=subprocess.PIPE,
        )
        (b_shortlog, _) = git_log_proc.communicate()
        shortlog = b_shortlog.decode()

        return version, previous_version, shortlog, referenced_issues_txt

    def search_or_create_issue(
        self,
        release,
        component,
        version,
        create=True,
        message_template_kwargs=None,
    ):
        try:
            github_repo = self.gi.get_repo(self.github_report_repo_name)
        except GithubException as e:
            raise NotifyIssueError(str(e)) from e

        issue_title = "{component} {version} ({release})".format(
            component=component, version=version, release=release
        )
        issue_no = None
        for issue in github_repo.get_issues():
            if issue.title == issue_title:
                issue_no = issue.number
                break

        # if nothing, create new issue
        if issue_no is None:
            # don't create if requested so
            if not create:
                return None

            if component.startswith("qubes-template"):
                message_template_path = (
                    self.message_templates_dir / "message-build-report-template"
                )
            elif component.startswith("iso"):
                message_template_path = (
                    self.message_templates_dir / "message-build-report-iso"
                )
            else:
                message_template_path = (
                    self.message_templates_dir / "message-build-report"
                )

            if not message_template_path.exists():
                log.warning(f"Cannot find template message")
                return None

            with open(message_template_path) as f:
                message_template = f.read()

            message = (
                message_template.replace("@COMPONENT@", component)
                .replace("@RELEASE_NAME@", release)
                .replace("@VERSION@", version)
                .replace("@MIN_AGE_DAYS@", str(self.min_age_days))
            )

            if message_template_kwargs is not None:
                for key, value in message_template_kwargs.items():
                    message = message.replace(key, value)

            try:
                issue = github_repo.create_issue(
                    title=issue_title, body=message
                )
                issue_no = issue.number
            except GithubException as e:
                log.warning(f"Failed to create issue: {str(e)}")

        return issue_no

    def comment_issue(
        self,
        issue_no,
        message,
        add_labels,
        delete_labels,
        github_repo=github_issues_repo,
    ):

        try:
            github_repo = self.gi.get_repo(github_repo)
            issue = github_repo.get_issue(issue_no)
        except GithubException as e:
            raise NotifyIssueError(str(e)) from e

        if message:
            try:
                issue.create_comment(body=message)
            except GithubException as e:
                log.warning(f"Failed to create comment on {issue_no}: {str(e)}")

        for label in delete_labels:
            try:
                issue.remove_from_labels(label)
            except GithubException as e:
                log.warning(
                    f"Failed to delete {label} label from issue {issue_no}: {str(e)}"
                )

        for label in add_labels:
            try:
                issue.add_to_labels(label)
            except GithubException as e:
                log.warning(
                    f"Failed to add {label} label to issue {issue_no}: {str(e)}"
                )

    def notify_closed_issues(
        self,
        dist,
        package_name,
        repo_type,
        current_commit,
        previous_commit,
        add_labels,
        delete_labels,
    ):
        message = f"message-{repo_type}-{dist.package_set}"
        if (self.message_templates_dir / f"{message}-{dist.name}").exists():
            message_template_path = (
                self.message_templates_dir / f"{message}-{dist.name}"
            )
        elif (
            self.message_templates_dir / f"{message}-{dist.fullname}"
        ).exists():
            message_template_path = (
                self.message_templates_dir / f"{message}-{dist.fullname}"
            )
        else:
            log.warning("Cannot find message template not adding comments")
            message_template_path = None

        git_log_proc = subprocess.Popen(
            [
                "git",
                "-C",
                str(self.source_dir),
                "log",
                "{}..{}".format(previous_commit, current_commit),
            ],
            stdout=subprocess.PIPE,
        )
        (b_git_log, _) = git_log_proc.communicate()
        closed_issues = []
        for line in b_git_log.decode().splitlines():
            match = fixes_re.search(line)
            if match:
                issues_string = match.group(0)
                issues_numbers = [
                    int(cleanup_re.sub("", s))
                    for s in issues_string.split()[1:]
                ]
                closed_issues.extend(issues_numbers)

        closed_issues = set(closed_issues)  # type: ignore

        git_log_proc = subprocess.Popen(
            [
                "git",
                "-C",
                str(self.source_dir),
                "log",
                "--pretty=format:{}-{}@%h %s".format(
                    github_repo_prefix, self.source_dir.name
                ),
                "{}..{}".format(previous_commit, current_commit),
            ],
            stdout=subprocess.PIPE,
        )
        (b_shortlog, _) = git_log_proc.communicate()
        shortlog = b_shortlog.decode()

        git_url_var = "GIT_URL_" + self.source_dir.name.replace("-", "_")
        if git_url_var in os.environ:
            git_url = os.environ[git_url_var]
        else:
            git_url = "{base}/{prefix}{repo}".format(
                base=github_baseurl,
                prefix=github_repo_prefix,
                repo=self.source_dir.name,
            )
        git_log_url = "{git_url}/compare/{prev_commit}...{curr_commit}".format(
            git_url=git_url,
            prev_commit=previous_commit,
            curr_commit=current_commit,
        )

        component = self.source_dir.name

        for issue in closed_issues:
            log.info(f"Adding a comment to issue #{issue}")
            if message_template_path:
                issue_message: Optional[str] = (
                    open(message_template_path, "r")
                    .read()
                    .replace("@DIST@", dist.name)
                    .replace("@PACKAGE_SET@", dist.package_set)
                    .replace("@PACKAGE_NAME@", package_name)
                    .replace("@COMPONENT@", component)
                    .replace("@REPOSITORY@", repo_type)
                    .replace("@RELEASE_NAME@", self.release_name)
                    .replace("@GIT_LOG@", shortlog)
                    .replace("@GIT_LOG_URL@", git_log_url)
                )
            else:
                issue_message = None

            self.comment_issue(issue, issue_message, add_labels, delete_labels)

    def run(
        self,
        command,
        dist,
        package_name,
        build_status,
        repository_type=None,
        repository_url=None,
        state_file=None,
        stable_state_file=None,
        build_log=None,
        additional_info=None,
    ):
        if dist.package_set == "host":
            dist_label = "host"
        else:
            dist_label = dist.distribution

        current_commit = self.get_current_commit()
        previous_stable_commit = None

        if command == "upload" and repository_type == "current":
            repository_type = "stable"

        add_labels, delete_labels = self.get_labels(
            command=command,
            repository_type=repository_type,
            build_status=build_status,
            dist_label=dist_label,
            package_name=package_name,
        )

        if command == "upload" and build_status == "uploaded":
            if not state_file.exists():
                log.warning(
                    f"{str(state_file)} does not exist, initializing with the current state"
                )
                previous_commit = None
            else:
                previous_commit = state_file.read_text().strip()

            if previous_commit is not None and repository_type in [
                "stable",
                "current-testing",
            ]:
                self.notify_closed_issues(
                    dist,
                    package_name,
                    repository_type,
                    current_commit,
                    previous_commit,
                    add_labels,
                    delete_labels,
                )

            state_file.write_text(current_commit)

            if stable_state_file.exists():
                previous_stable_commit = stable_state_file.read_text().strip()

        if package_name.startswith("iso"):
            base_message = f"ISO for {self.release_name}"
            if repository_url:
                upload_suffix_message = (
                    f"[testing]({repository_url}) repository"
                )
            else:
                upload_suffix_message = f"testing repository"
        elif package_name.startswith("qubes-template"):
            base_message = (
                f"Template {package_name.replace('qubes-template-', '')}"
            )
            upload_suffix_message = f"{repository_type} repository"
        else:
            base_message = f"Package for {dist_label}"
            upload_suffix_message = f"{repository_type} repository"

        if build_status == "building":
            report_message = None
        elif build_status == "built":
            if build_log:
                report_message = (
                    f"{base_message} was built ([build log]({build_log}))."
                )
            else:
                report_message = f"{base_message} was built."
            if additional_info:
                report_message = (
                    f"{report_message.rstrip('.')} ({additional_info})."
                )
        elif build_status == "uploaded":
            report_message = (
                f"{base_message} was uploaded to {upload_suffix_message}."
            )
            if additional_info:
                report_message = (
                    f"{report_message.rstrip('.')} ({additional_info})."
                )
        elif build_status == "failed" and command in ["build", "upload"]:
            if command == "build":
                suffix_message = "build"
            else:
                suffix_message = f"upload to {upload_suffix_message}"
            if build_log:
                report_message = f"{base_message} failed to {suffix_message} ([build log]({build_log}))."
            else:
                report_message = f"{base_message} failed to {suffix_message}."
            if additional_info:
                report_message = (
                    f"{report_message.rstrip('.')}:\n\n{additional_info}"
                )
        else:
            raise NotifyIssueError(f"Unexpected build status '{build_status}'")

        component = self.source_dir.name

        git_url_var = "GIT_URL_" + component.replace("-", "_")
        if git_url_var in os.environ:
            git_url = os.environ[git_url_var]
        else:
            git_url = "{base}/{prefix}{repo}".format(
                base=github_baseurl, prefix=github_repo_prefix, repo=component
            )

        if package_name.startswith("qubes-template"):
            # qubes-template-fedora-25-4.0.0-201710170053
            version = "-".join(package_name.split("-")[-2:])
            component = "-".join(package_name.split("-")[:-2])
            template_name = component.replace("qubes-template-", "")
            message_kwargs = {
                # "@COMMIT_SHA@": current_commit,
                # "@GIT_URL@": git_url,
                "@TEMPLATE_NAME@": template_name,
                "@DIST@": os.getenv("DIST_ORIG_ALIAS", "(dist)"),
            }
        elif package_name.startswith("iso"):
            parsed_package_name = package_name.split("-")
            version = parsed_package_name[-1]
            component = "iso"
            message_kwargs = {"@ISO_VERSION@": version}
        else:
            (
                version,
                previous_version,
                shortlog,
                referenced_issues_txt,
            ) = self.get_package_changes(
                git_url,
                current_commit,
                previous_current_commit=previous_stable_commit,
            )

            git_log_url = (
                "{git_url}/compare/{prev_commit}...{curr_commit}".format(
                    git_url=git_url,
                    prev_commit=previous_version,
                    curr_commit=version,
                )
            )

            message_kwargs = {
                "@COMMIT_SHA@": current_commit,
                "@GIT_URL@": git_url,
                "@GIT_LOG@": shortlog,
                "@GIT_LOG_URL@": git_log_url,
                "@ISSUES@": referenced_issues_txt,
            }

        report_issue_no = self.search_or_create_issue(
            self.release_name,
            component,
            version=version,
            create=True,
            message_template_kwargs=message_kwargs,
        )
        if report_issue_no:
            self.comment_issue(
                report_issue_no,
                report_message,
                add_labels,
                delete_labels,
                github_repo=self.github_report_repo_name,
            )


def add_required_args_to_parser(parser):
    parser.add_argument("release_name", help="Release name (e.g. r4.2)")
    parser.add_argument("source_dir", help="Component sources path")
    parser.add_argument("package_name", help="Binary package name")
    parser.add_argument(
        "distribution",
        help="Qubes OS Distribution name (e.g. host-fc32)",
        type=QubesDistribution,
    )
    parser.add_argument(
        "status",
        help="Build status",
        choices=["failed", "building", "built", "uploaded"],
    )


def parse_args():
    epilog = "When state_file doesn't exists, no notify is sent, but the current state is recorded"

    parser = ArgumentParser(epilog=epilog)
    parser.add_argument(
        "--auth-token", help="Github authentication token (OAuth2)"
    )
    parser.add_argument(
        "--build-log", help="Build log name in build-logs repository"
    )
    parser.add_argument(
        "--message-templates-dir", help="Message templates directory"
    )
    parser.add_argument(
        "--github-report-repo-name", help="Github repository to report"
    )
    parser.add_argument(
        "--additional-info", help="Add additional info on comment"
    )
    parser.add_argument(
        "--days",
        action="store",
        type=int,
        default=5,
        help="ensure package at least this time in testing (default: %(default)d)",
    )
    subparsers = parser.add_subparsers(help="command")

    # Upload status parser
    upload_parser = subparsers.add_parser("upload")
    upload_parser.set_defaults(command="upload")
    # Common args
    add_required_args_to_parser(upload_parser)
    # Extra args
    upload_parser.add_argument(
        "repo_type",
        help="Repository type",
        choices=[
            "current",
            "current-testing",
            "security-testing",
            "unstable",
            "templates-itl",
            "templates-itl-testing",
            "templates-community",
            "templates-community-testing",
            "iso-testing",
        ],
    )
    upload_parser.add_argument(
        "state_file",
        help="File to store internal state (previous commit id)",
        type=Path,
    )
    upload_parser.add_argument(
        "stable_state_file",
        help="File to store internal state (previous commit id of a stable aka current package)",
        type=Path,
    )
    upload_parser.add_argument(
        "--repository-url",
        help="URL where is uploaded the package, template or ISO.",
        default=None,
    )

    # Build status
    build_parser = subparsers.add_parser("build")
    build_parser.set_defaults(command="build")
    # Common args
    add_required_args_to_parser(build_parser)

    return parser.parse_args()


def main():
    args = parse_args()

    token = args.auth_token or os.environ.get("GITHUB_API_KEY")
    github_report_repo_name = args.github_report_repo_name or os.environ.get(
        "GITHUB_BUILD_REPORT_REPO"
    )

    if not token:
        log.error("Please provide GITHUB_API_KEY either as CLI arg or environ.")
        return 1

    if not release_name_re.match(args.release_name):
        log.error(f"Ignoring release {args.release_name}")
        return 1

    if not github_report_repo_name:
        log.error(
            "Please provide GITHUB_BUILD_REPORT_REPO either as CLI arg or environ."
        )
        return 1

    message_templates_dir = (
        Path(args.message_templates_dir).resolve()
        if args.message_templates_dir
        else Path("templates").resolve()
    )
    if not message_templates_dir.exists():
        log.error("Cannot find message templates directory.")
        return 1

    try:
        cli = NotifyIssueCli(
            token=token,
            release_name=args.release_name,
            source_dir=Path(args.source_dir).resolve(),
            github_report_repo_name=github_report_repo_name,
            message_templates_dir=message_templates_dir,
            min_age_days=args.days,
        )

        cli.run(
            command=args.command,
            dist=args.distribution,
            package_name=args.package_name,
            build_status=args.status,
            additional_info=args.additional_info,
            build_log=getattr(args, "build_log", None),
            repository_type=getattr(args, "repo_type", None),
            repository_url=getattr(args, "repository_url", None),
            state_file=getattr(args, "state_file", None),
            stable_state_file=getattr(args, "stable_state_file", None),
        )

    except NotifyIssueError as e:
        log.error(str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
