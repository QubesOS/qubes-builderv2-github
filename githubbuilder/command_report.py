# The Qubes OS Project, http://www.qubes-os.org
#
# Copyright (C) 2026 Frédéric Pierret (fepitre) <frederic@invisiblethingslab.com>
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

import logging
import random
import time
import re

from github import Github, Auth

log = logging.getLogger("command-report")

BUILD_LOG_PATH_RE = re.compile(r".*[\S\w.-]+/log_[\S\w.-]+")


def parse_build_log_path(stdout, logger=None):
    """
    Extract the log path reported by the qubesbuilder.BuildLog RPC.
    """
    if not stdout:
        if logger:
            logger.error(
                "No output from qubesbuilder.BuildLog. Any policy RPC or LogVM issue?"
            )
        return None

    for raw in stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        if BUILD_LOG_PATH_RE.fullmatch(line):
            return line

    if logger:
        logger.error(
            "Cannot parse log file provided by qubesbuilder.BuildLog RPC."
        )
    return None


def make_issue_body(command_line, signer_fpr):
    body = f"Signed command received:\n\n```\n{command_line}\n```\n"
    if signer_fpr:
        body += f"\n**Signer:** `{signer_fpr}`\n"
    return body


class CommandReport:
    """
    Report command processing in a dedicated repository: one issue per
    command, results as comments.
    """

    def __init__(self, token, repo_name):
        self.token = token
        self.repo_name = repo_name
        self._repo = None

    def _get_repo(self):
        if self._repo is None:
            gi = Github(auth=Auth.Token(self.token))
            self._repo = gi.get_repo(self.repo_name)
        return self._repo

    @staticmethod
    def _find_issue(repo, title):
        for issue in repo.get_issues():
            if issue.title == title:
                return issue.number
        return None

    def find_or_create_issue(self, title, body):
        try:
            repo = self._get_repo()
            issue_no = self._find_issue(repo, title)
            if issue_no is None:
                # several builder instances process the same command: wait a
                # random time and look again, so most of them find the issue
                # created by the fastest one
                time.sleep(random.uniform(0, 10))
                issue_no = self._find_issue(repo, title)
            if issue_no is None:
                issue_no = repo.create_issue(title=title, body=body).number
            return issue_no
        except Exception as e:
            log.warning(f"Failed to find or create command issue: {e}")
            return None

    def comment(self, issue_no, body):
        try:
            repo = self._get_repo()
            repo.get_issue(issue_no).create_comment(body=body)
            return True
        except Exception as e:
            log.warning(f"Failed to comment command issue {issue_no}: {e}")
            return False
