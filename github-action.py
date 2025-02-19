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
import os
import re
import signal
import subprocess
import sys
import traceback
from abc import abstractmethod, ABC
from contextlib import contextmanager
from pathlib import Path
from typing import List

import yaml

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
)
from qubesbuilder.cli.cli_installer import _installer_stage
from qubesbuilder.config import Config
from qubesbuilder.exc import ConfigError
from qubesbuilder.log import (
    init_logger,
    QubesBuilderLogger,
    create_file_handler,
    create_console_handler,
)
from qubesbuilder.component import ComponentError
from qubesbuilder.plugins import PluginError
from qubesbuilder.pluginmanager import PluginManager

from urllib.parse import urljoin

from utils.notify_issues import NotifyIssueCli, NotifyIssueError

PROJECT_PATH = Path(__file__).resolve().parent

init_logger(verbose=True)
log = QubesBuilderLogger


def get_log_file_from_qubesbuilder_buildlog(stdout, logger=None):
    lines = stdout.splitlines()
    if not stdout or not lines:
        if logger:
            logger.error(
                "No output from qubesbuilder.BuildLog. Any policy RPC or LogVM issue?"
            )
    if re.match(r"^.*[\S\w.-]+/log_[\S\w.-]+$", lines[0]):
        return lines[0]
    else:
        if logger:
            logger.error(
                "Cannot parse log file provided by qubesbuilder.BuildLog RPC."
            )


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
        source_dir=None,
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

        self.source_dir = source_dir

        notify_cli_kwargs = {
            "release_name": self.qubes_release,
            "source_dir": self.source_dir,
            "github_report_repo_name": self.build_report_repo,
            "message_templates_dir": PROJECT_PATH / "templates",
            "min_age_days": self.config.get("min-age-days", 5),
        }

        self.notify_cli = NotifyIssueCli(
            token=self.api_key, **notify_cli_kwargs
        )

    def get_build_log_url(self, log_file):
        if self.local_log_file:
            return log_file
        else:
            return f"https://github.com/{self.logs_repo}/tree/master/{log_file}"

    def display_head_info(self, args):
        pass

    def make_with_log(self, func, *args, **kwargs):
        if self.dry_run:
            log.debug(f"[DRY-RUN] func: {func.__qualname__}")
            log.debug(f"[DRY-RUN] args: {args}")
            log.debug(f"[DRY-RUN] kwargs: {kwargs}")
            return
        if self.local_log_file:
            return self.make_with_log_local(func, *args, **kwargs)
        else:
            return self.make_with_log_qrexec(func, *args, **kwargs)

    def make_with_log_local(self, func, *args, **kwargs):
        log_fh = create_file_handler(self.local_log_file)
        log.addHandler(log_fh)
        log.debug("> starting build with log")
        self.display_head_info(args)
        try:
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
            qrexec_stream = create_console_handler(True, stream=p.stdin)

            log.addHandler(qrexec_stream)
            log.debug("> starting build with log")
            self.display_head_info(args)
            try:
                func(*args, **kwargs)
                log.debug("> done")
            except PluginError as e:
                p.stdin.close()
                p.wait()
                log_file = get_log_file_from_qubesbuilder_buildlog(
                    p.stdout.read(), log
                )
                raise AutoActionError(e.args, log_file=log_file) from e
            else:
                p.stdin.close()
                p.wait()
                log_file = get_log_file_from_qubesbuilder_buildlog(
                    p.stdout.read(), log
                )
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

    def notify_github(self, cli_run_kwargs, build_target):
        if not self.api_key:
            log.debug(
                f"API key not set, not calling notify CLI: {cli_run_kwargs}"
            )
            return
        try:
            if self.dry_run:
                log.debug(f"[DRY-RUN] kwargs: {cli_run_kwargs}")
            else:
                self.notify_cli.run(**cli_run_kwargs)
        except NotifyIssueError as e:
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
            source_dir=component.source_dir,
        )

        self.component = component
        self.distributions = distributions

        self.repository_publish = repository_publish or self.config.get(
            "repository-publish", {}
        ).get("components", None)
        if not self.repository_publish:
            raise AutoActionError(
                f"No repository defined for component publication."
            )

        self.timeout = self.component.timeout

        self.built_for_dist = []

    def run_stages(self, dist, stages):
        for stage in stages:
            _component_stage(
                stage_name=stage,
                config=self.config,
                components=[self.component],
                distributions=[dist],
            )

    def publish_and_upload(self, repository_publish: str, distributions: List):
        _publish(
            config=self.config,
            repository_publish=repository_publish,
            components=[self.component],
            distributions=distributions,
            templates=[],
        )
        _upload(
            config=self.config,
            repository_publish=repository_publish,
            distributions=distributions,
            templates=[],
        )

    def notify_build_status_on_timeout(self):
        for dist in self.distributions:
            if dist.name not in self.built_for_dist:
                self.notify_build_status(
                    dist, "failed", additional_info="Timeout"
                )

    def notify_build_status(
        self, dist, status, stage="build", log_file=None, additional_info=None
    ):
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

        cli_run_kwargs = {
            "command": stage,
            "dist": dist,
            "package_name": self.component.name,
            "repository_type": self.repository_publish,
            "additional_info": additional_info,
            "build_status": status,
            "build_log": (
                self.get_build_log_url(log_file=log_file) if log_file else None
            ),
            "state_file": state_file,
            "stable_state_file": stable_state_file,
        }

        self.notify_github(
            cli_run_kwargs=cli_run_kwargs,
            build_target=f"{self.component.name}:{dist}",
        )

    def display_head_info(self, args):
        log.debug(f">> args:")
        log.debug(f">>   {args}")
        log.debug(f">> component:")
        log.debug(f">>   {self.component}")
        try:
            log.debug(
                f">>     commit-hash: {self.component.get_source_commit_hash()}"
            )
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
            components=[self.component],
            distributions=self.distributions,
            stage_name="fetch",
        )
        for dist in self.distributions:
            release_status = _check_release_status_for_component(
                config=self.config,
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
            ) or self.dry_run:
                with timeout(self.timeout):
                    stage = "build"
                    try:
                        self.notify_build_status(
                            dist=dist,
                            status="building",
                        )

                        build_log_file = self.make_with_log(
                            self.run_stages,
                            dist=dist,
                            stages=["prep", "build", "sign", "publish"],
                        )

                        self.notify_build_status(
                            dist=dist,
                            status="built",
                            stage=stage,
                            log_file=build_log_file,
                        )

                        # FIXME: possibly send sign/publish logs

                        stage = "upload"
                        self.make_with_log(
                            self.run_stages,
                            dist=dist,
                            stages=["upload"],
                        )

                        self.notify_build_status(
                            dist=dist, status="uploaded", stage=stage
                        )

                        self.built_for_dist.append(dist)
                    except AutoActionError as autobuild_exc:
                        log.error(str(autobuild_exc.args))
                        self.notify_build_status(
                            dist=dist,
                            status="failed",
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
                    self.notify_build_status(
                        dist=dist,
                        status="uploaded",
                        stage="upload",
                        log_file=upload_log_file,
                    )
                except AutoActionError as autobuild_exc:
                    self.notify_build_status(
                        dist=dist,
                        status="failed",
                        stage="upload",
                        log_file=autobuild_exc.log_file,
                    )
                    pass
                except TimeoutError as timeout_exc:
                    raise AutoActionTimeout(
                        "Timeout reached for upload!"
                    ) from timeout_exc
                except Exception as exc:
                    self.notify_build_status(
                        dist=dist,
                        status="failed",
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
            source_dir=builder_dir,
        )

        try:
            self.templates = self.config.get_templates([template_name])
        except ConfigError as e:
            raise AutoActionError(f"No such template '{template_name}'.") from e
        self.template_timestamp = template_timestamp
        self.template_version = self.config.qubes_release.lstrip("r") + ".0"

        self.template = self.templates[0]
        self.package_name = f"qubes-template-{self.template.name}-{self.template_version}-{self.template_timestamp}"

        self.repository_publish = repository_publish or self.config.get(
            "repository-publish", {}
        ).get("templates", None)
        if not self.repository_publish:
            raise AutoActionError(
                f"No repository defined for template publication."
            )

        self.timeout = self.templates[0].timeout

    def run_stages(self, stages):
        for stage in stages:
            _template_stage(
                stage_name=stage,
                config=self.config,
                templates=self.templates,
                template_timestamp=self.template_timestamp,
            )

    def publish_and_upload(self, repository_publish: str):
        _publish(
            config=self.config,
            repository_publish=repository_publish,
            templates=self.templates,
            components=[],
            distributions=[],
        )
        _upload(
            config=self.config,
            repository_publish=repository_publish,
            templates=self.templates,
            distributions=[],
        )

    def notify_build_status_on_timeout(self):
        self.notify_build_status("failed", additional_info="Timeout")

    def notify_build_status(
        self, status, stage="build", log_file=None, additional_info=None
    ):
        state_file = (
            self.state_dir
            / f"{self.qubes_release}-template-vm-{self.template.distribution.name}-{self.repository_publish}"
            # type: ignore
        )
        stable_state_file = (
            self.state_dir
            / f"{self.qubes_release}-template-vm-{self.template.distribution.name}-current"
            # type: ignore
        )

        cli_run_kwargs = {
            "command": stage,
            "dist": self.template.distribution,
            "package_name": self.package_name,
            "repository_type": self.repository_publish,
            "build_status": status,
            "additional_info": additional_info,
            "build_log": (
                self.get_build_log_url(log_file=log_file) if log_file else None
            ),
            "state_file": state_file,
            "stable_state_file": stable_state_file,
        }

        self.notify_github(
            cli_run_kwargs=cli_run_kwargs, build_target=self.template
        )

    def build(self):
        timestamp_file = (
            self.config.artifacts_dir
            / "templates"
            / f"{self.templates[0].name}.build.yml"
        )
        if timestamp_file.exists():
            try:
                info = yaml.safe_load(timestamp_file.read_text())
                timestamp_existing = datetime.datetime.strptime(
                    info["timestamp"], "%Y%m%d%H%M"
                )
                template_timestamp = datetime.datetime.strptime(
                    self.template_timestamp, "%Y%m%d%H%M"
                )
            except (OSError, ValueError, KeyError, yaml.YAMLError) as exc:
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
                    status="building",
                )

                build_log_file = self.make_with_log(
                    self.run_stages,
                    stages=["prep", "build", "sign", "publish"],
                )

                self.notify_build_status(
                    status="built",
                    stage=stage,
                    log_file=build_log_file,
                )

                stage = "upload"
                self.make_with_log(
                    self.run_stages,
                    stages=["upload"],
                )

                self.notify_build_status(
                    status="uploaded", stage=stage, log_file=build_log_file
                )
            except AutoActionError as autobuild_exc:
                self.notify_build_status(
                    status="failed",
                    stage=stage,
                    log_file=autobuild_exc.log_file,
                )
                pass
            except TimeoutError as timeout_exc:
                raise AutoActionTimeout(
                    "Timeout reached for build!"
                ) from timeout_exc
            except Exception as exc:
                self.notify_build_status(
                    status="failed",
                    additional_info=f"Internal error: '{str(exc.__class__.__name__)}'",
                )
                log.error(str(exc))
                pass

    def upload(self):
        upload_artifact_file = (
            self.config.artifacts_dir
            / "templates"
            / f"{self.templates[0].name}.publish.yml"
        )
        if not upload_artifact_file.exists():
            raise AutoActionError(
                "Cannot upload template, no upload artifact found!"
            )
        try:
            artifact = yaml.safe_load(upload_artifact_file.read_text())
            timestamp_existing = datetime.datetime.strptime(
                artifact["timestamp"], "%Y%m%d%H%M"
            ).strftime("%Y%m%d%H%M")
        except (OSError, ValueError, KeyError, yaml.YAMLError) as exc:
            raise AutoActionError(
                f"Failed to read or parse timestamp: {str(exc)}"
            ) from exc
        if self.commit_sha != f"{self.template_version}-{timestamp_existing}":
            raise AutoActionError(
                f"Different template was built in the meantime (current: {self.template_version}-{timestamp_existing})"
            )
        with timeout(self.timeout):
            try:
                upload_log_file = self.make_with_log(
                    self.publish_and_upload,
                    repository_publish=self.repository_publish,
                )
                self.notify_build_status(
                    status="uploaded", stage="upload", log_file=upload_log_file
                )
            except AutoActionError as autobuild_exc:
                self.notify_build_status(
                    status="failed",
                    stage="upload",
                    log_file=autobuild_exc.log_file,
                )
                pass
            except TimeoutError as timeout_exc:
                raise AutoActionTimeout(
                    "Timeout reached for upload!"
                ) from timeout_exc
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
            source_dir=builder_dir,
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
                raise AutoActionError(
                    f"Failed to parse timestamp: {str(exc)}"
                ) from exc
        else:
            self.iso_timestamp = ""

        host_distributions = [
            d
            for d in self.config.get_distributions()
            if d.package_set == "host"
        ]
        if len(host_distributions) != 1:
            raise AutoActionError(
                f"None or more than one host distribution in builder configuration file!"
            )
        self.iso_version = self.commit_sha
        self.iso_base_url = self.config.get("github", {}).get(
            "iso-base-url", None
        )

        if not self.config.get("repository-upload-remote-host", {}).get(
            "iso", None
        ):
            raise AutoActionError(
                f"No remote host configured in builder configuration file!"
            )

        self.dist = host_distributions[0]
        self.package_name = f"iso-{self.dist.name}-{self.iso_version}"

    def run_stages(self, stages):
        for stage in stages:
            _installer_stage(
                stage_name=stage,
                config=self.config,
                iso_timestamp=self.iso_timestamp,
            )

    def notify_build_status_on_timeout(self):
        self.notify_build_status("failed", additional_info="Timeout")

    def notify_build_status(
        self, status, stage="build", log_file=None, additional_info=None
    ):
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

        cli_run_kwargs = {
            "command": stage,
            "dist": self.dist,
            "package_name": self.package_name,
            "repository_type": self.repository_publish,
            "additional_info": additional_info,
            "build_status": status,
            "build_log": (
                self.get_build_log_url(log_file=log_file) if log_file else None
            ),
            "state_file": state_file,
            "stable_state_file": stable_state_file,
        }

        if self.iso_base_url:
            cli_run_kwargs["repository_url"] = (
                f"{urljoin(self.iso_base_url, self.repository_publish)}/"
            )

        self.notify_github(
            cli_run_kwargs=cli_run_kwargs,
            build_target=self.package_name,
        )

    def trigger_openqa(self):
        openqa_client_path = (
            Path.home() / ".config/openqa/client.conf"
        ).resolve()
        if not openqa_client_path.exists():
            log.debug(
                f"Cannot find openQA configuration file: {openqa_client_path}"
            )
            return
        if not (
            OpenQA_Client and OpenQAClientError and callable(OpenQA_Client)
        ):
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
                additional_info = (
                    f"see [openQA]({job_url}) test result overview"
                )
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

                self.make_with_log(
                    _component_stage,
                    config=self.config,
                    components=self.config.get_components(["qubes-release"]),
                    distributions=[],
                    stage_name="fetch",
                )

                build_log_file = self.make_with_log(
                    self.run_stages,
                    stages=["init-cache", "prep", "build", "sign"],
                )

                self.notify_build_status(
                    status="built",
                    stage=stage,
                    log_file=build_log_file,
                )

                stage = "upload"
                self.make_with_log(
                    self.run_stages,
                    stages=["upload"],
                )

                additional_info = self.trigger_openqa()

                self.notify_build_status(
                    status="uploaded",
                    stage=stage,
                    log_file=build_log_file,
                    additional_info=additional_info,
                )
            except AutoActionError as autobuild_exc:
                self.notify_build_status(
                    "failed", stage=stage, log_file=autobuild_exc.log_file
                )
                pass
            except TimeoutError as timeout_exc:
                raise AutoActionTimeout(
                    "Timeout reached for build!"
                ) from timeout_exc
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
    parser.add_argument(
        "--state-dir", default=Path.home() / "github-notify-state", type=Path
    )
    parser.add_argument(
        "--local-log-file",
        help="Use local log file instead of qubesbuilder.BuildLog RPC.",
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.required = True

    # build component parser
    build_component_parser = subparsers.add_parser("build-component")
    build_component_parser.set_defaults(command="build-component")
    build_component_parser.add_argument("builder_dir", type=Path)
    build_component_parser.add_argument("builder_conf")
    build_component_parser.add_argument("component_name")

    # upload component parser
    upload_component_parser = subparsers.add_parser("upload-component")
    upload_component_parser.set_defaults(command="upload-component")
    upload_component_parser.add_argument("builder_dir", type=Path)
    upload_component_parser.add_argument("builder_conf")
    upload_component_parser.add_argument("component_name")
    upload_component_parser.add_argument("commit_sha")
    upload_component_parser.add_argument("repository_publish")
    upload_component_parser.add_argument(
        "--distribution", nargs="+", default=[]
    )

    # build template parser
    build_template_parser = subparsers.add_parser("build-template")
    build_template_parser.set_defaults(command="build-template")
    build_template_parser.add_argument("builder_dir", type=Path)
    build_template_parser.add_argument("builder_conf")
    build_template_parser.add_argument("template_name")
    build_template_parser.add_argument("template_timestamp")

    # upload template parser
    template_parser = subparsers.add_parser("upload-template")
    template_parser.set_defaults(command="upload-template")
    template_parser.add_argument("builder_dir", type=Path)
    template_parser.add_argument("builder_conf")
    template_parser.add_argument("template_name")
    template_parser.add_argument("template_sha")
    template_parser.add_argument("repository_publish")

    # build iso parser
    build_iso_parser = subparsers.add_parser("build-iso")
    build_iso_parser.set_defaults(command="build-iso")
    build_iso_parser.add_argument("builder_dir", type=Path)
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

    dry_run = args.dry_run or config.get("github", {}).get("dry-run", False)

    if args.command in ("build-component", "upload-component"):
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
            # maintainers components filtering
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
                    dry_run=dry_run,
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
                dry_run=dry_run,
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
                dry_run=dry_run,
            )
        )
    else:
        return

    for cli in cli_list:
        try:
            if args.command in (
                "build-component",
                "build-template",
                "build-iso",
            ):
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
