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

# This is script to automate build process in reaction to pushing updates
# sources to git. The workflow is:
# - fetch sources, check if properly signed
# - check if version tag is on top
# - build package(s) according to builder.yml
# - upload to current-testing repository
#
# All the above should be properly logged

import argparse
import datetime
import logging
import os
import signal
import subprocess
import sys
import time
import traceback
from abc import abstractmethod, ABC
from contextlib import contextmanager
from pathlib import Path
from typing import List

try:
    from openqa_client.client import OpenQA_Client
    from openqa_client.exceptions import OpenQAClientError
except ImportError:
    OpenQA_Client = None
    OpenQAClientError = Exception

from qubesbuilder.cli.cli_package import _component_stage
from qubesbuilder.cli.cli_template import _template_stage
from qubesbuilder.cli.cli_repository import (
    _publish,
    _upload,
    _check_release_status_for_component,
    _check_release_status_for_template,
)
from qubesbuilder.cli.cli_installer import _installer_stage
from qubesbuilder.config import Config
from qubesbuilder.exc import ConfigError
from qubesbuilder.log import init_logging
from qubesbuilder.component import ComponentError
from qubesbuilder.plugins import PluginError
from qubesbuilder.plugins.template import TEMPLATE_VERSION
from qubesbuilder.pluginmanager import PluginManager

from urllib.parse import urljoin

PROJECT_PATH = Path(__file__).resolve().parent

log = init_logging(level="DEBUG")
log.name = "github-action"


def raise_timeout(signum, frame):
    raise TimeoutError


@contextmanager
def timeout(time):
    signal.signal(signal.SIGALRM, raise_timeout)
    signal.alarm(time)
    try:
        yield
    except TimeoutError:
        pass
    finally:
        signal.signal(signal.SIGALRM, signal.SIG_IGN)


class AutoActionError(Exception):
    def __init__(self, *args, log_file=None):
        self.args = args
        self.log_file = log_file


class AutoActionTimeout(Exception):
    pass


class CommitMismatchError(AutoActionError):
    pass


class BaseAutoAction(ABC):
    def __init__(
        self,
        builder_dir,
        state_dir,
        config: Config,
        commit_sha=None,
        repository_publish=None,
        local_log_file=None,
        dry_run=False,
    ):
        self.builder_dir = Path(builder_dir).resolve()
        self.state_dir = Path(state_dir).resolve()
        self.config = config
        self.manager = PluginManager(self.config.get_plugins_dirs())
        self.timeout = 21600
        self.qubes_release = self.config.get("qubes-release")
        self.commit_sha = commit_sha
        self.repository_publish = repository_publish
        self.dry_run = dry_run

        if not self.builder_dir.exists():
            raise AutoActionError(
                f"No such directory for builder '{self.builder_dir}'."
            )

        self.state_dir.mkdir(exist_ok=True, parents=True)
        self.local_log_file = local_log_file
        self.api_key = self.config.get("github", {}).get("api-key", None)
        self.build_report_repo = self.config.get("github", {}).get(
            "build-report-repo", "QubesOS/updates-status"
        )
        self.logs_repo = self.config.get("github", {}).get(
            "logs-repo", "QubesOS/build-logs"
        )

        self.env = os.environ.copy()
        self.env.update(
            {
                "PYTHONPATH": builder_dir,
                "GITHUB_API_KEY": self.api_key,
                "GITHUB_BUILD_REPORT_REPO": self.build_report_repo,
            }
        )

    def get_build_log_url(self, log_file):
        return f"https://github.com/{self.logs_repo}/tree/master/{log_file}"

    @staticmethod
    def display_head_info(args):
        pass

    def make_with_log(self, func, *args, **kwargs):
        if self.local_log_file:
            return self.make_with_log_local(func, *args, **kwargs)
        else:
            return self.make_with_log_qrexec(func, *args, **kwargs)

    def make_with_log_local(self, func, *args, **kwargs):
        log_fh = logging.FileHandler(self.local_log_file)
        log.addHandler(log_fh)
        log.debug("> starting build with log")
        self.display_head_info(args)
        try:
            if self.dry_run:
                log.debug(f"[DRY-RUN] func: {func.__func__.__qualname__}")
                log.debug(f"[DRY-RUN] args: {args}")
                log.debug(f"[DRY-RUN] kwargs: {kwargs}")
            else:
                func(*args, **kwargs)
            log.debug("> done")
        except PluginError as e:
            raise AutoActionError(e.args, log_file=self.local_log_file) from e
        finally:
            log.removeHandler(log_fh)
        return self.local_log_file

    def make_with_log_qrexec(self, func, *args, **kwargs):
        with subprocess.Popen(
            ["qrexec-client-vm", "dom0", "qubesbuilder.BuildLog"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE,
        ) as p:
            assert p.stdin is not None
            assert p.stdout is not None
            qrexec_stream = logging.StreamHandler(stream=p.stdin)

            log.addHandler(qrexec_stream)
            log.debug("> starting build with log")
            self.display_head_info(args)
            try:
                func(*args, **kwargs)
                log.debug("> done")
            except PluginError as e:
                p.stdin.close()
                p.wait()
                log_file_list = list(p.stdout)
                log_file = log_file_list[0].rstrip("\n")
                raise AutoActionError(e.args, log_file=log_file) from e
            else:
                p.stdin.close()
                p.wait()
                log_file_list = list(p.stdout)
                log_file = log_file_list[0].rstrip("\n")
            finally:
                log.removeHandler(qrexec_stream)
            return log_file

    @abstractmethod
    def build(self):
        pass

    @abstractmethod
    def upload(self):
        pass

    @abstractmethod
    def notify_build_status_on_timeout(self):
        pass

    def notify_github(self, notify_issues_cmd, build_target, env):
        try:
            if self.dry_run:
                log.debug(f"[DRY-RUN] cmd: {notify_issues_cmd}")
                log.debug(f"[DRY-RUN] env: {env}")
            else:
                subprocess.run(notify_issues_cmd, env=env)
        except subprocess.CalledProcessError as e:
            msg = f"{build_target}: Failed to notify GitHub: {str(e)}"
            log.error(msg)


class AutoAction(BaseAutoAction):
    def __init__(
        self,
        builder_dir,
        config,
        component,
        distributions,
        state_dir,
        commit_sha,
        repository_publish,
        local_log_file,
        dry_run,
    ):
        super().__init__(
            builder_dir=builder_dir,
            config=config,
            state_dir=state_dir,
            commit_sha=commit_sha,
            repository_publish=repository_publish,
            local_log_file=local_log_file,
            dry_run=dry_run,
        )

        self.component = component
        self.distributions = distributions

        self.repository_publish = repository_publish or self.config.get(
            "repository-publish", {}
        ).get("components", None)
        if not self.repository_publish:
            raise AutoActionError(f"No repository defined for component publication.")

        self.timeout = self.component.timeout

        self.built_for_dist = []

    def run_stages(self, dist, stages):
        for stage in stages:
            _component_stage(
                stage_name=stage,
                config=self.config,
                manager=self.manager,
                components=[self.component],
                distributions=[dist],
            )

    def publish_and_upload(self, repository_publish: str, distributions: List):
        _publish(
            config=self.config,
            manager=self.manager,
            repository_publish=repository_publish,
            components=[self.component],
            distributions=distributions,
            templates=[],
        )
        _upload(
            config=self.config,
            manager=self.manager,
            repository_publish=repository_publish,
            distributions=distributions,
            templates=[],
        )

    def notify_build_status_on_timeout(self):
        for dist in self.distributions:
            if dist.name not in self.built_for_dist:
                self.notify_build_status(dist, "failed", additional_info="Timeout")

    def notify_build_status(
        self, dist, status, stage="build", log_file=None, additional_info=None
    ):
        notify_issues_cmd = [
            f"{str(PROJECT_PATH)}/utils/notify-issues",
            f"--days={self.config.get('min-age-days', 5)}",
            f"--message-templates-dir={str(PROJECT_PATH)}/templates",
        ]

        if log_file:
            notify_issues_cmd += [
                f"--build-log={self.get_build_log_url(log_file=log_file)}"
            ]

        if additional_info:
            notify_issues_cmd += [f"--additional-info={str(additional_info)}"]

        notify_issues_cmd += [
            stage,
            self.qubes_release,
            str(self.component.source_dir),
            self.component.name,
            dist.distribution,
            status,
        ]

        self.notify_github(
            notify_issues_cmd=notify_issues_cmd,
            build_target=f"{self.component.name}:{dist}",
            env=self.env,
        )

    def notify_upload_status(self, dist, log_file=None, additional_info=None):
        notify_issues_cmd = [
            f"{str(PROJECT_PATH)}/utils/notify-issues",
            f"--days={self.config.get('min-age-days', 5)}",
            f"--message-templates-dir={str(PROJECT_PATH)}/templates",
        ]

        if log_file:
            notify_issues_cmd += [
                f"--build-log={self.get_build_log_url(log_file=log_file)}"
            ]

        if additional_info:
            notify_issues_cmd += [f"--additional-info={str(additional_info)}"]

        state_file = (
            self.state_dir
            / f"{self.qubes_release}-{self.component.name}-{dist.package_set}-{dist.name}-{self.repository_publish}"
            # type: ignore
        )
        stable_state_file = (
            self.state_dir
            / f"{self.qubes_release}-{self.component.name}-{dist.package_set}-{dist.name}-current"
            # type: ignore
        )
        notify_issues_cmd += [
            "upload",
            self.qubes_release,
            str(self.component.source_dir),
            self.component.name,
            dist.distribution,
            str(self.repository_publish),
            str(state_file),
            str(stable_state_file),
        ]

        self.notify_github(
            notify_issues_cmd=notify_issues_cmd,
            build_target=f"{self.component.name}:{dist}",
            env=self.env,
        )

    def display_head_info(self, args):
        log.debug(f">> args:")
        log.debug(f">>   {args}")
        log.debug(f">> component:")
        log.debug(f">>   {self.component}")
        try:
            log.debug(f">>     commit-hash: {self.component.get_source_commit_hash()}")
            log.debug(f">>     source-hash: {self.component.get_source_hash()}")
        except ComponentError:
            # we may have not yet source (like calling fetch stage)
            pass
        log.debug(f">> distributions:")
        log.debug(f">>   {self.distributions}")

    def build(self):
        self.make_with_log(
            _component_stage,
            config=self.config,
            manager=self.manager,
            components=[self.component],
            distributions=self.distributions,
            stage_name="fetch",
        )

        for dist in self.distributions:
            release_status = _check_release_status_for_component(
                config=self.config,
                manager=self.manager,
                components=[self.component],
                distributions=[dist],
            )
            if (
                release_status.get(self.component.name, {})
                .get(dist.distribution, {})
                .get("status", None)
                == "not released"
                and release_status.get(self.component.name, {})
                .get(dist.distribution, {})
                .get("tag", None)
                != "no version tag"
            ):
                with timeout(self.timeout):
                    stage = "build"
                    try:
                        self.notify_build_status(
                            dist,
                            "building",
                        )

                        build_log_file = self.make_with_log(
                            self.run_stages,
                            dist=dist,
                            stages=["prep", "build"],
                        )

                        stage = "upload"
                        self.make_with_log(
                            self.run_stages,
                            dist=dist,
                            stages=["sign", "publish", "upload"],
                        )

                        self.notify_upload_status(dist, build_log_file)

                        self.built_for_dist.append(dist)
                    except AutoActionError as autobuild_exc:
                        log.error(str(autobuild_exc.args))
                        self.notify_build_status(
                            dist,
                            "failed",
                            stage=stage,
                            log_file=autobuild_exc.log_file,
                            additional_info=autobuild_exc.args,
                        )
                        pass
                    except TimeoutError as timeout_exc:
                        raise AutoActionTimeout(
                            "Timeout reached for build!"
                        ) from timeout_exc
                    except Exception as exc:
                        self.notify_build_status(
                            dist,
                            "failed",
                            additional_info=f"Internal error: '{str(exc.__class__.__name__)}'",
                        )
                        log.error(str(exc))
                        pass

        if not self.built_for_dist:
            log.warning(
                "Nothing was built, something gone wrong or version tag was not found."
            )

    def upload(self):
        actual_commit_sha = self.component.get_source_commit_hash()
        if self.commit_sha != actual_commit_sha:
            raise CommitMismatchError(
                f"Source have changed in the meantime (current: {actual_commit_sha})"
            )
        release_status = _check_release_status_for_component(
            config=self.config,
            manager=self.manager,
            components=[self.component],
            distributions=self.distributions,
        )
        for dist in self.distributions:
            if (
                release_status.get(self.component.name, {})
                .get(dist.distribution, {})
                .get("status", None)
            ) in (None, "no packages defined"):
                # skip not applicable distributions
                continue
            with timeout(self.timeout):
                try:
                    upload_log_file = self.make_with_log(
                        self.publish_and_upload,
                        repository_publish=self.repository_publish,
                        distributions=[dist],
                    )
                    self.notify_upload_status(dist, upload_log_file)
                except AutoActionError as autobuild_exc:
                    self.notify_build_status(
                        dist, "failed", stage="upload", log_file=autobuild_exc.log_file
                    )
                    pass
                except TimeoutError as timeout_exc:
                    raise AutoActionTimeout(
                        "Timeout reached for upload!"
                    ) from timeout_exc
                except Exception as exc:
                    self.notify_build_status(
                        dist,
                        "failed",
                        additional_info=f"Internal error: '{str(exc.__class__.__name__)}'",
                    )
                    log.error(str(exc))
                    pass


class AutoActionTemplate(BaseAutoAction):
    def __init__(
        self,
        builder_dir,
        config,
        template_name,
        template_timestamp,
        state_dir,
        commit_sha,
        repository_publish,
        local_log_file,
        dry_run,
    ):
        super().__init__(
            builder_dir=builder_dir,
            config=config,
            state_dir=state_dir,
            commit_sha=commit_sha,
            repository_publish=repository_publish,
            local_log_file=local_log_file,
            dry_run=dry_run,
        )

        try:
            self.templates = self.config.get_templates([template_name])
        except ConfigError as e:
            raise AutoActionError(f"No such template '{template_name}'.") from e
        self.template_timestamp = template_timestamp

        self.repository_publish = repository_publish or self.config.get(
            "repository-publish", {}
        ).get("templates", None)
        if not self.repository_publish:
            raise AutoActionError(f"No repository defined for template publication.")

        self.timeout = self.templates[0].timeout

    def run_stages(self, stages):
        for stage in stages:
            _template_stage(
                stage_name=stage,
                config=self.config,
                manager=self.manager,
                templates=self.templates,
                template_timestamp=self.template_timestamp,
            )

    def publish_and_upload(self, repository_publish: str):
        _publish(
            config=self.config,
            manager=self.manager,
            repository_publish=repository_publish,
            templates=self.templates,
            components=[],
            distributions=[],
        )
        _upload(
            config=self.config,
            manager=self.manager,
            repository_publish=repository_publish,
            templates=self.templates,
            distributions=[],
        )

    def notify_build_status_on_timeout(self):
        self.notify_build_status("failed", additional_info="Timeout")

    def notify_build_status(
        self, status, stage="build", log_file=None, additional_info=None
    ):
        notify_issues_cmd = [
            f"{str(PROJECT_PATH)}/utils/notify-issues",
            f"--days={self.config.get('min-age-days', 5)}",
            f"--message-templates-dir={str(PROJECT_PATH)}/templates",
        ]

        if log_file:
            notify_issues_cmd += [
                f"--build-log={self.get_build_log_url(log_file=log_file)}"
            ]

        if additional_info:
            notify_issues_cmd += [f"--additional-info={str(additional_info)}"]

        template = self.templates[0]
        package_name = f"qubes-template-{template.name}-{TEMPLATE_VERSION}-{self.template_timestamp}"

        notify_issues_cmd += [
            stage,
            self.qubes_release,
            str(self.builder_dir),
            package_name,
            template.distribution.distribution,
            status,
        ]

        self.notify_github(
            notify_issues_cmd=notify_issues_cmd,
            build_target=template,
            env=self.env,
        )

    def notify_upload_status(self, log_file=None, additional_info=None):
        notify_issues_cmd = [
            f"{str(PROJECT_PATH)}/utils/notify-issues",
            f"--days={self.config.get('min-age-days', 5)}",
            f"--message-templates-dir={str(PROJECT_PATH)}/templates",
        ]

        if log_file:
            notify_issues_cmd += [
                f"--build-log={self.get_build_log_url(log_file=log_file)}"
            ]

        if additional_info:
            notify_issues_cmd += [f"--additional-info={str(additional_info)}"]

        template = self.templates[0]
        package_name = f"qubes-template-{template.name}-{TEMPLATE_VERSION}-{self.template_timestamp}"

        state_file = (
            self.state_dir
            / f"{self.qubes_release}-template-vm-{template.distribution.name}-{self.repository_publish}"
            # type: ignore
        )
        stable_state_file = (
            self.state_dir
            / f"{self.qubes_release}-template-vm-{template.distribution.name}-current"
            # type: ignore
        )
        notify_issues_cmd += [
            "upload",
            self.qubes_release,
            str(self.builder_dir),
            package_name,
            template.distribution.distribution,
            str(self.repository_publish),
            str(state_file),
            str(stable_state_file),
        ]

        self.notify_github(
            notify_issues_cmd=notify_issues_cmd,
            build_target=template,
            env=self.env,
        )

    def build(self):
        timestamp_file = (
            self.config.artifacts_dir
            / "templates"
            / f"build_timestamp_{self.templates[0].name}"
        )
        if timestamp_file.exists():
            try:
                timestamp_existing = datetime.datetime.strptime(
                    timestamp_file.read_text().rstrip("\n"), "%Y%m%d%H%M"
                )
                template_timestamp = datetime.datetime.strptime(
                    self.template_timestamp, "%Y%m%d%H%M"
                )
            except (OSError, ValueError) as exc:
                raise AutoActionError(
                    f"Failed to read or parse timestamp: {str(exc)}"
                ) from exc
            if template_timestamp <= timestamp_existing:
                log.info(
                    f"Newer template ({timestamp_existing.strftime('%Y%m%d%H%M')}) already built."
                )
                return

        with timeout(self.timeout):
            stage = "build"
            try:
                self.notify_build_status(
                    "building",
                )

                build_log_file = self.make_with_log(
                    self.run_stages,
                    stages=["prep", "build"],
                )

                stage = "upload"
                self.make_with_log(
                    self.run_stages,
                    stages=["sign", "publish", "upload"],
                )

                self.notify_upload_status(build_log_file)

            except AutoActionError as autobuild_exc:
                self.notify_build_status(
                    "failed", stage=stage, log_file=autobuild_exc.log_file
                )
                pass
            except TimeoutError as timeout_exc:
                raise AutoActionTimeout("Timeout reached for build!") from timeout_exc
            except Exception as exc:
                self.notify_build_status(
                    "failed",
                    additional_info=f"Internal error: '{str(exc.__class__.__name__)}'",
                )
                log.error(str(exc))
                pass

    def upload(self):
        timestamp_file = (
            self.config.artifacts_dir
            / "templates"
            / f"build_timestamp_{self.templates[0].name}"
        )
        if not timestamp_file.exists():
            raise AutoActionError("Cannot upload template, no build timestamp found.")
        try:
            timestamp_existing = datetime.datetime.strptime(
                timestamp_file.read_text().rstrip("\n"), "%Y%m%d%H%M"
            ).strftime("%Y%m%d%H%M")
        except (OSError, ValueError) as exc:
            raise AutoActionError(
                f"Failed to read or parse timestamp: {str(exc)}"
            ) from exc
        if self.commit_sha != f"{TEMPLATE_VERSION}-{timestamp_existing}":
            raise AutoActionError(
                f"Different template was built in the meantime (current: {TEMPLATE_VERSION}-{timestamp_existing})"
            )
        with timeout(self.timeout):
            try:
                upload_log_file = self.make_with_log(
                    self.publish_and_upload,
                    repository_publish=self.repository_publish,
                )
                self.notify_upload_status(upload_log_file)
            except AutoActionError as autobuild_exc:
                self.notify_build_status(
                    "failed", stage="upload", log_file=autobuild_exc.log_file
                )
                pass
            except TimeoutError as timeout_exc:
                raise AutoActionTimeout("Timeout reached for upload!") from timeout_exc
            except Exception as exc:
                self.notify_build_status(
                    "failed",
                    additional_info=f"Internal error: '{str(exc.__class__.__name__)}'",
                )
                log.error(str(exc))
                pass


class AutoActionISO(BaseAutoAction):
    def __init__(
        self,
        builder_dir,
        config,
        iso_timestamp,
        state_dir,
        commit_sha,
        repository_publish,
        local_log_file,
        dry_run,
        is_final=False,
    ):
        super().__init__(
            builder_dir=builder_dir,
            config=config,
            state_dir=state_dir,
            commit_sha=commit_sha,
            repository_publish=repository_publish,
            local_log_file=local_log_file,
            dry_run=dry_run,
        )

        self.timeout = self.config.get("timeout", 21600)

        config_iso = self.config.get("iso", {})
        config_iso["is-final"] = is_final
        config_iso["version"] = commit_sha
        self.config.set("iso", config_iso)

        if iso_timestamp:
            try:
                self.iso_timestamp = str(
                    datetime.datetime.strptime(iso_timestamp, "%Y%m%d%H%M")
                )
            except (OSError, ValueError) as exc:
                raise AutoActionError(f"Failed to parse timestamp: {str(exc)}") from exc
        else:
            self.iso_timestamp = ""

        host_distributions = [
            d for d in self.config.get_distributions() if d.package_set == "host"
        ]
        if len(host_distributions) != 1:
            raise AutoActionError(
                f"None or more than one host distribution in builder configuration file!"
            )
        self.dist = host_distributions[0]
        self.iso_version = self.commit_sha
        self.iso_base_url = self.config.get("github", {}).get("iso-base-url", None)

        if not self.config.get("repository-upload-remote-host", {}).get("iso", None):
            raise AutoActionError(
                f"No remote host configured in builder configuration file!"
            )

    def run_stages(self, stages):
        for stage in stages:
            _installer_stage(
                stage_name=stage,
                config=self.config,
                manager=self.manager,
                iso_timestamp=self.iso_timestamp,
            )

    def notify_build_status_on_timeout(self):
        self.notify_build_status("failed", additional_info="Timeout")

    def notify_build_status(
        self, status, stage="build", log_file=None, additional_info=None
    ):
        notify_issues_cmd = [
            f"{str(PROJECT_PATH)}/utils/notify-issues",
            f"--message-templates-dir={str(PROJECT_PATH)}/templates",
        ]

        if log_file:
            notify_issues_cmd += [
                f"--build-log={self.get_build_log_url(log_file=log_file)}"
            ]

        if additional_info:
            notify_issues_cmd += [f"--additional-info={str(additional_info)}"]

        package_name = f"iso-{self.dist.name}-{self.iso_version}"

        notify_issues_cmd += [
            stage,
            self.qubes_release,
            str(self.builder_dir),
            package_name,
            self.dist.distribution,
            status,
        ]

        self.notify_github(
            notify_issues_cmd=notify_issues_cmd,
            build_target=package_name,
            env=self.env,
        )

    def notify_upload_status(self, log_file=None, additional_info=None):
        notify_issues_cmd = [
            f"{str(PROJECT_PATH)}/utils/notify-issues",
            f"--message-templates-dir={str(PROJECT_PATH)}/templates",
        ]

        if log_file:
            notify_issues_cmd += [
                f"--build-log={self.get_build_log_url(log_file=log_file)}"
            ]

        if additional_info:
            notify_issues_cmd += [f"--additional-info={str(additional_info)}"]

        package_name = f"iso-{self.dist.name}-{self.iso_version}"

        state_file = (
            self.state_dir
            / f"{self.qubes_release}-iso-{self.dist.name}"
            # type: ignore
        )
        stable_state_file = (
            self.state_dir
            / f"{self.qubes_release}-iso-{self.dist.name}-current"
            # type: ignore
        )
        notify_issues_cmd += [
            "upload",
            self.qubes_release,
            str(self.builder_dir),
            package_name,
            self.dist.distribution,
            str(self.repository_publish),
            str(state_file),
            str(stable_state_file),
        ]
        if self.iso_base_url:
            notify_issues_cmd += [
                "--repository-url",
                f"{urljoin(self.iso_base_url, self.repository_publish)}/",
            ]

        self.notify_github(
            notify_issues_cmd=notify_issues_cmd,
            build_target=package_name,
            env=self.env,
        )

    def trigger_openqa(self):
        openqa_client_path = (Path.home() / ".config/openqa/client.conf").resolve()
        if not openqa_client_path.exists():
            log.debug(f"Cannot find openQA configuration file: {openqa_client_path}")
            return
        if not (OpenQA_Client and OpenQAClientError and callable(OpenQA_Client)):
            log.debug(
                "Cannot trigger openQA. Check if 'python3-openqa_client' is installed."
            )
            return
        try:
            version = self.qubes_release.lstrip("r")
            url = urljoin(self.iso_base_url, self.repository_publish)
            params = {
                "DISTRI": "qubesos",
                "VERSION": version,
                "FLAVOR": "install-iso",
                "ARCH": "x86_64",
                "BUILD": self.iso_version,
                "ISO_URL": f"{url}/Qubes-{self.iso_version}-x86_64.iso",
            }
            log.debug(f"openQA request: {params}")
            job_url = f"https://openqa.qubes-os.org/tests/overview?distri=qubesos&version={version}&build={self.iso_version}&groupid=1"
            log.debug(f"openQA job url: {job_url}")
            if self.dry_run:
                return
            client = OpenQA_Client()
            if client.openqa_request("POST", "isos", params):
                additional_info = f"see [openQA]({job_url}) test result overview"
                return additional_info
        except OpenQAClientError as exc:
            log.error(str(exc))

    def upload(self):
        raise NotImplementedError

    def build(self):
        with timeout(self.timeout):
            stage = "build"
            try:
                self.notify_build_status(
                    "building",
                )

                build_log_file = self.make_with_log(
                    self.run_stages,
                    stages=["init-cache", "prep", "build", "sign"],
                )

                stage = "upload"
                self.make_with_log(
                    self.run_stages,
                    stages=["upload"],
                )

                additional_info = self.trigger_openqa()

                self.notify_upload_status(
                    build_log_file,
                    additional_info=additional_info,
                )

            except AutoActionError as autobuild_exc:
                self.notify_build_status(
                    "failed", stage=stage, log_file=autobuild_exc.log_file
                )
                pass
            except TimeoutError as timeout_exc:
                raise AutoActionTimeout("Timeout reached for build!") from timeout_exc
            except Exception as exc:
                self.notify_build_status(
                    "failed",
                    additional_info=f"Internal error: '{str(exc.__class__.__name__)}'",
                )
                log.error(str(exc))
                pass


def main():
    parser = argparse.ArgumentParser()

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
    parser.add_argument("--dry-run", action="store_true", default=False)
    parser.add_argument("--state-dir", default=Path.home() / "github-notify-state")
    parser.add_argument(
        "--local-log-file",
        help="Use local log file instead of qubesbuilder.BuildLog RPC.",
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.required = True

    # build component parser
    build_component_parser = subparsers.add_parser("build-component")
    build_component_parser.set_defaults(command="build-component")
    build_component_parser.add_argument("builder_dir")
    build_component_parser.add_argument("builder_conf")
    build_component_parser.add_argument("component_name")

    # upload component parser
    upload_component_parser = subparsers.add_parser("upload-component")
    upload_component_parser.set_defaults(command="upload-component")
    upload_component_parser.add_argument("builder_dir")
    upload_component_parser.add_argument("builder_conf")
    upload_component_parser.add_argument("component_name")
    upload_component_parser.add_argument("commit_sha")
    upload_component_parser.add_argument("repository_publish")
    upload_component_parser.add_argument("--distribution", nargs="+", default=[])

    # build template parser
    build_template_parser = subparsers.add_parser("build-template")
    build_template_parser.set_defaults(command="build-template")
    build_template_parser.add_argument("builder_dir")
    build_template_parser.add_argument("builder_conf")
    build_template_parser.add_argument("template_name")
    build_template_parser.add_argument("template_timestamp")

    # upload template parser
    template_parser = subparsers.add_parser("upload-template")
    template_parser.set_defaults(command="upload-template")
    template_parser.add_argument("builder_dir")
    template_parser.add_argument("builder_conf")
    template_parser.add_argument("template_name")
    template_parser.add_argument("template_sha")
    template_parser.add_argument("repository_publish")

    # build iso parser
    build_iso_parser = subparsers.add_parser("build-iso")
    build_iso_parser.set_defaults(command="build-iso")
    build_iso_parser.add_argument("builder_dir")
    build_iso_parser.add_argument("builder_conf")
    build_iso_parser.add_argument("iso_version")
    build_iso_parser.add_argument("iso_timestamp")
    build_iso_parser.add_argument(
        "--final",
        action="store_true",
        default=False,
    )

    args = parser.parse_args()

    commit_sha = None
    command_timestamp = None
    if args.command == "upload-component":
        commit_sha = args.commit_sha
    elif args.command == "build-template":
        command_timestamp = args.template_timestamp
    elif args.command == "upload-template":
        commit_sha = args.template_sha
        command_timestamp = commit_sha.split("-")[-1]
    elif args.command == "build-iso":
        commit_sha = args.iso_version
        command_timestamp = args.iso_timestamp

    if args.command in ("upload-component", "upload-template"):
        repository_publish = args.repository_publish
    elif args.command == "build-iso":
        repository_publish = "iso" if args.final else "iso-testing"
    else:
        repository_publish = None

    if args.local_log_file:
        local_log_file = Path(args.local_log_file).resolve()
    else:
        local_log_file = None

    cli_list: List[BaseAutoAction] = []

    config = Config(args.builder_conf)
    if args.command in ("build-component", "upload-component"):
        distributions = config.get_distributions()
        try:
            components = config.get_components([args.component_name], url_match=True)
        except ConfigError as e:
            raise AutoActionError(f"No such component '{args.component_name}'.") from e

        # maintainers checks
        if not args.no_signer_github_command_check:
            # maintainers components filtering
            allowed_components = (
                config.get("github", {})
                .get("maintainers", {})
                .get(args.signer_fpr, {})
                .get("components", [])
            )
            if allowed_components != "_all_":
                components = [c for c in components if c.name in allowed_components]
            if not components:
                log.info("Cannot find any allowed components.")
                return

            # maintainers distributions filtering (only supported for upload)
            if args.command == "upload-component":
                allowed_distributions = (
                    config.get("github", {})
                    .get("maintainers", {})
                    .get(args.signer_fpr, {})
                    .get("distributions", [])
                )
                if allowed_distributions == "_all_":
                    allowed_distributions = [d.distribution for d in distributions]
                if args.distribution == ["all"]:
                    args.distribution = [d.distribution for d in distributions]
                distributions = [
                    d
                    for d in distributions
                    if d.distribution in allowed_distributions
                    and d.distribution in args.distribution
                ]
                if not distributions:
                    log.info("Cannot find any allowed distributions.")
                    return
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
                    dry_run=args.dry_run,
                )
            )
    elif args.command in ("build-template", "upload-template"):
        supported_templates = [t.name for t in config.get_templates()]
        # check if requested template name exists
        if args.template_name not in supported_templates:
            return
        # maintainers checks
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
                return
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
                dry_run=args.dry_run,
            )
        )
    elif args.command == "build-iso":
        # maintainers checks
        if not args.no_signer_github_command_check:
            allowed_to_trigger_build_iso = (
                config.get("github", {})
                .get("maintainers", {})
                .get(args.signer_fpr, {})
                .get("iso", False)
            )
            if not allowed_to_trigger_build_iso:
                log.info(f"Trigger build for ISO is not allowed.")
                return
        cli_list.append(
            AutoActionISO(
                builder_dir=args.builder_dir,
                config=config,
                iso_timestamp=command_timestamp,
                state_dir=args.state_dir,
                commit_sha=commit_sha,
                repository_publish=repository_publish,
                local_log_file=local_log_file,
                dry_run=args.dry_run,
            )
        )
    else:
        return

    if config.get("github", {}).get("dry-run", False):
        # Dry-run mode (for tests only)
        time.sleep(1)
        return

    for cli in cli_list:
        try:
            if args.command in ("build-component", "build-template", "build-iso"):
                cli.build()
            elif args.command in ("upload-component", "upload-template"):
                cli.upload()
            else:
                return
        except CommitMismatchError as exc:
            # this is expected for multi-branch components, don't interrupt processing
            log.warning(str(exc))
        except AutoActionTimeout as autobuild_exc:
            cli.notify_build_status_on_timeout()
            raise AutoActionTimeout(str(autobuild_exc))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        traceback.print_exc()
        log.error(str(e))
        sys.exit(1)
