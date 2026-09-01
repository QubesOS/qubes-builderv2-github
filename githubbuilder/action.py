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

# Library module: build automation classes for reacting to git pushes.
# The workflow:
# - fetch sources, check if properly signed
# - check if version tag is on top
# - build package(s) according to builder.yml
# - upload to current-testing repository

import datetime
import os
import signal
import subprocess
from abc import abstractmethod, ABC
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from logging import Handler, DEBUG, Logger
from pathlib import Path
from typing import List, Optional, Any
from urllib.parse import urljoin

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
from qubesbuilder.pluginmanager import PluginManager

from githubbuilder.notify_issues import NotifyIssueCli, NotifyIssueError
from githubbuilder.command_report import parse_build_log_path

# Root of the qubes-builder-github repository
PROJECT_PATH = Path(__file__).resolve().parent.parent

init_logger(verbose=True)
log = QubesBuilderLogger


@dataclass
class BuildTargetResult:
    target: Any
    label: str
    status: str = "pending"
    reason: Optional[str] = None
    stage: Optional[str] = None
    log_file: Optional[Any] = None
    additional_info: Optional[str] = None
    release_status: Optional[str] = None
    version_tag: Optional[str] = None
    built_artifacts: Optional[list[str]] = None


def format_additional_info(
    base: Any,
    tail: Optional[str] = None,
    on_error_lines_to_report: int = 30,
    *,
    max_tail_chars: int = 3500,
) -> Optional[str]:
    paragraphs: list[str] = []

    # Base message
    base_txt = ""
    if base is not None:
        if isinstance(base, tuple):
            base_txt = " ".join(str(x) for x in base if x is not None).strip()
        else:
            base_txt = str(base).strip()

    if base_txt:
        paragraphs.append(f"**Additional info:** {base_txt}")

    # Tail block
    if tail:
        t = tail.strip("\n")
        if max_tail_chars and len(t) > max_tail_chars:
            t = "…\n" + t[-max_tail_chars:]

        paragraphs.append(
            "\n".join(
                [
                    f"**Log tail (last ~{on_error_lines_to_report} lines):**",
                    "<details>",
                    "",
                    "```text",
                    t,
                    "```",
                    "</details>",
                ]
            )
        )

    return "\n\n".join(paragraphs) if paragraphs else None


def get_log_file_from_qubesbuilder_buildlog(stdout, logger=None):
    return parse_build_log_path(stdout, logger)


def raise_timeout(signum, frame):
    raise TimeoutError


@contextmanager
def timeout(seconds: int):
    old_handler = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, raise_timeout)
    signal.alarm(int(seconds))
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)


class SafeWrapper(Handler):
    """
    Wrap an existing handler to BrokenPipeError. On first BrokenPipe,
    disables itself and optionally removes itself from logger.
    """

    def __init__(self, inner: Handler, parent_logger: Logger | None = None):
        super().__init__(level=inner.level)
        self.inner = inner
        self.parent_logger = parent_logger
        self._disabled = False

        # Keep existing formatting
        try:
            self.setFormatter(inner.formatter)
        except Exception:
            pass

    def emit(self, record):
        if self._disabled:
            return
        try:
            self.inner.emit(record)
            try:
                self.inner.flush()
            except Exception:
                pass
        except BrokenPipeError:
            self._disabled = True
            # detach so future logging doesn't keep trying
            if self.parent_logger is not None:
                try:
                    self.parent_logger.removeHandler(self)
                except Exception:
                    pass
            try:
                self.inner.close()
            except Exception:
                pass

    def flush(self):
        try:
            self.inner.flush()
        except Exception:
            pass

    def close(self):
        try:
            self.inner.close()
        finally:
            super().close()


class TailBufferHandler(Handler):
    def __init__(self, capacity=30, level=DEBUG, formatter=None):
        super().__init__(level)
        self.capacity = capacity
        self.buffer = deque(maxlen=capacity)  # type: ignore
        if formatter is not None:
            self.setFormatter(formatter)

    def emit(self, record):
        try:
            self.buffer.append(self.format(record))
        except Exception:
            self.buffer.append(record.getMessage())

    def text(self, header=None):
        if not self.buffer:
            return None
        if not header:
            header = f"Last {self.capacity} log lines"
        return header + ":\n" + "\n".join(self.buffer)


class AutoActionError(Exception):
    def __init__(self, *args, log_file=None, tail=None):
        self.args = args
        self.log_file = log_file
        self.tail = tail


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
        self.on_error_lines_to_report = 30

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
                "PYTHONPATH": str(builder_dir),
                "GITHUB_API_KEY": self.api_key or "",
                "GITHUB_BUILD_REPORT_REPO": self.build_report_repo,
            }
        )

        self.source_dir = source_dir

        notify_cli_kwargs = {
            "release_name": self.qubes_release,
            "source_dir": self.source_dir,
            "github_report_repo_name": self.build_report_repo,
            "message_templates_dir": Path(
                self.config.get("github", {}).get(
                    "message-templates-dir", PROJECT_PATH / "templates"
                )
            ),
            "min_age_days": self.config.get("min-age-days", 5),
        }

        self.notify_cli = NotifyIssueCli(
            token=self.api_key, **notify_cli_kwargs
        )
        self.results: dict[str, BuildTargetResult] = {}

    def register_result(
        self, key: str, target: Any, label: str
    ) -> BuildTargetResult:
        result = BuildTargetResult(target=target, label=label)
        self.results[key] = result
        return result

    def get_result(self, key: str) -> BuildTargetResult:
        return self.results[key]

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
            return None
        if self.local_log_file:
            return self.make_with_log_local(func, *args, **kwargs)
        return self.make_with_log_qrexec(func, *args, **kwargs)

    def make_with_log_local(self, func, *args, **kwargs):
        log_fh = create_file_handler(self.local_log_file)
        log.addHandler(log_fh)
        log.debug("> starting build with log")
        self.display_head_info(args)

        try:
            func(*args, **kwargs)
            log.debug("> done")
        except Exception as caught_exc:
            try:
                data = self.local_log_file.read_text()
            except Exception as e:
                return f"> failed to read log tail: {e}]"

            all_lines = data.splitlines()
            tail = "\n".join(all_lines[-self.on_error_lines_to_report :])

            raise AutoActionError(
                f"{caught_exc.__class__.__name__}: {caught_exc}",
                log_file=self.local_log_file,
                tail=f"Last {self.on_error_lines_to_report} log lines:\n{tail}",
            ) from caught_exc

        finally:
            log.removeHandler(log_fh)
            try:
                log_fh.flush()
            finally:
                log_fh.close()

        return self.local_log_file

    def make_with_log_qrexec(self, func, *args, **kwargs):
        log_file = None
        caught_exc = None

        with subprocess.Popen(
            ["qrexec-client-vm", "dom0", "qubesbuilder.BuildLog"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE,
        ) as p:
            assert p.stdin is not None
            assert p.stdout is not None
            assert p.stderr is not None

            raw_qrexec = create_console_handler(True, stream=p.stdin)
            qrexec = SafeWrapper(raw_qrexec, parent_logger=log)
            tail = TailBufferHandler(
                capacity=self.on_error_lines_to_report,
                level=DEBUG,
                formatter=raw_qrexec.formatter,
            )

            # Attach handlers (attach wrapper, not raw handler)
            log.addHandler(qrexec)
            log.addHandler(tail)

            try:
                log.debug("> starting build with log")
                self.display_head_info(args)
                try:
                    func(*args, **kwargs)
                    log.debug("> done")
                except Exception as e:
                    caught_exc = e

                try:
                    p.stdin.close()
                except Exception:
                    pass

                # Wait for BuildLog to finish writing the filename to stdout
                p.wait()

                out = p.stdout.read()
                err = p.stderr.read()

            finally:
                # Now detach handlers
                try:
                    log.removeHandler(tail)
                except Exception:
                    pass
                try:
                    log.removeHandler(qrexec)
                except Exception:
                    pass

            log_file = get_log_file_from_qubesbuilder_buildlog(out, log)
            if not log_file and err:
                # include stderr in tail for reporting
                tail.buffer.append(f"> error: {err.strip()[:400]}")

        if caught_exc:
            raise AutoActionError(
                f"{caught_exc.__class__.__name__}: {caught_exc}",
                log_file=log_file,  # may be None if RPC died
                tail=tail.text(),
            ) from caught_exc

        return log_file

    @abstractmethod
    def notify_build_status(
        self,
        status,
        stage="build",
        log_file=None,
        additional_info=None,
        **kwargs,
    ):
        pass

    def update_result(
        self,
        result: BuildTargetResult,
        *,
        status: str,
        stage: Optional[str] = None,
        reason: Optional[str] = None,
        log_file=None,
        additional_info: Optional[str] = None,
        built_artifacts: Optional[list[str]] = None,
        notify: bool = False,
        **notify_kwargs,
    ):
        result.status = status
        result.stage = stage
        result.reason = reason
        result.log_file = log_file
        result.additional_info = additional_info
        if built_artifacts is not None:
            result.built_artifacts = built_artifacts
        if notify:
            self.notify_build_status(
                status=status,
                stage=stage,
                log_file=log_file,
                additional_info=additional_info,
                **notify_kwargs,
            )

    def _handle_error(
        self,
        result: BuildTargetResult,
        exc: Exception,
        stage: str,
        *,
        label: Optional[str] = None,
        default_msg: str = "Auto action failed",
        **notify_kwargs,
    ):
        """Handle AutoActionError or unexpected Exception uniformly."""
        if isinstance(exc, AutoActionError):
            base_msg = exc.args[0] if exc.args else default_msg
            extra = format_additional_info(
                base=base_msg,
                tail=getattr(exc, "tail", None),
                on_error_lines_to_report=self.on_error_lines_to_report,
            )
            self.update_result(
                result,
                status="failed",
                stage=stage,
                reason=base_msg,
                log_file=exc.log_file,
                additional_info=extra,
                notify=True,
                **notify_kwargs,
            )
        else:
            log.exception(f"Internal error during {label or stage}")
            reason = f"{exc.__class__.__name__}: {exc}"
            self.update_result(
                result,
                status="failed",
                stage=stage,
                reason=reason,
                additional_info=f"Internal error: '{reason}'",
                notify=True,
                **notify_kwargs,
            )

    def notify_kwargs(self, result: BuildTargetResult) -> dict:
        return {}

    @abstractmethod
    def build(self):
        pass

    @abstractmethod
    def upload(self):
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
                "No repository defined for component publication."
            )

        self.timeout = self.component.timeout
        for dist in self.distributions:
            self.register_result(
                dist.name, dist, f"{self.component.name}:{dist}"
            )

    def run_stages(self, dist, stages):
        _component_stage(
            stages=stages,
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

    def notify_kwargs(self, result: BuildTargetResult) -> dict:
        return {"dist": result.target}

    def notify_build_status(
        self,
        status,
        stage="build",
        log_file=None,
        additional_info=None,
        *,
        dist=None,
        **kwargs,
    ):
        state_file = (
            self.state_dir
            / f"{self.qubes_release}-{self.component.name}-{dist.package_set}-{dist.name}-{self.repository_publish}"
        )
        stable_state_file = (
            self.state_dir
            / f"{self.qubes_release}-{self.component.name}-{dist.package_set}-{dist.name}-current"
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
            stages=["fetch"],
        )
        require_version_tag = self.component.fetch_versions_only
        anything_built = False

        for dist in self.distributions:
            result = self.get_result(dist.name)
            result.stage = "build"
            release_status = _check_release_status_for_component(
                config=self.config,
                components=[self.component],
                distributions=[dist],
            )
            dist_release_status = release_status.get(
                self.component.name, {}
            ).get(dist.distribution, {})
            result.release_status = dist_release_status.get("status", None)
            result.version_tag = dist_release_status.get("tag", None)

            if (
                result.release_status in (None, "no packages defined")
                and not self.dry_run
            ):
                reason = "no packages defined"
                extra = format_additional_info(base=f"Skipped: {reason}.")
                self.update_result(
                    result,
                    status="skipped",
                    stage="build",
                    reason=reason,
                    additional_info=extra,
                )
                log.info(f"{result.label}: skipped ({reason})")
                continue

            if result.release_status != "not released" and not self.dry_run:
                reason = f"release status is '{result.release_status}'"
                extra = format_additional_info(base=f"Skipped: {reason}.")
                self.update_result(
                    result,
                    status="skipped",
                    stage="build",
                    reason=reason,
                    additional_info=extra,
                )
                log.info(f"{result.label}: skipped ({reason})")
                continue

            if (
                require_version_tag
                and result.version_tag == "no version tag"
                and not self.dry_run
            ):
                reason = "no version tag found"
                extra = format_additional_info(base=f"Skipped: {reason}.")
                self.update_result(
                    result,
                    status="skipped",
                    stage="build",
                    reason=reason,
                    additional_info=extra,
                )
                log.info(f"{result.label}: skipped ({reason})")
                continue

            with timeout(self.timeout):
                stage = "build"
                try:
                    self.update_result(
                        result,
                        status="building",
                        stage=stage,
                        notify=True,
                        dist=dist,
                    )

                    build_log_file = self.make_with_log(
                        self.run_stages,
                        dist=dist,
                        stages=["prep", "build", "sign", "publish"],
                    )

                    self.update_result(
                        result,
                        status="built",
                        stage=stage,
                        log_file=build_log_file,
                        notify=True,
                        dist=dist,
                    )

                    # FIXME: possibly send sign/publish logs

                    stage = "upload"
                    result.stage = stage
                    self.make_with_log(
                        self.run_stages, dist=dist, stages=["upload"]
                    )

                    self.update_result(
                        result,
                        status="uploaded",
                        stage=stage,
                        log_file=build_log_file,
                        notify=True,
                        dist=dist,
                    )

                    anything_built = True
                except AutoActionError as exc:
                    self._handle_error(
                        result,
                        exc,
                        stage,
                        default_msg="Auto Build failed",
                        dist=dist,
                    )
                except TimeoutError as timeout_exc:
                    self.update_result(
                        result,
                        status="failed",
                        stage=stage,
                        reason="Timeout",
                        additional_info="Timeout",
                        notify=True,
                        **self.notify_kwargs(result),
                    )
                    for r in self.results.values():
                        if r.status in ("pending", "building"):
                            self.update_result(
                                r,
                                status="failed",
                                stage=r.stage or "build",
                                reason="Timeout",
                                additional_info="Timeout",
                                notify=True,
                                **self.notify_kwargs(r),
                            )
                    raise AutoActionTimeout(
                        "Timeout reached for build!"
                    ) from timeout_exc
                except Exception as exc:
                    self._handle_error(
                        result,
                        exc,
                        stage,
                        label="build",
                        dist=dist,
                    )

        if not anything_built:
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
            result = self.get_result(dist.name)
            result.stage = "upload"
            dist_release_status = release_status.get(
                self.component.name, {}
            ).get(dist.distribution, {})
            result.release_status = dist_release_status.get("status", None)
            result.version_tag = dist_release_status.get("tag", None)
            if result.release_status in (None, "no packages defined"):
                # skip not applicable distributions
                reason = "no packages defined"
                extra = format_additional_info(base=f"Skipped: {reason}.")
                self.update_result(
                    result,
                    status="skipped",
                    stage="upload",
                    reason=reason,
                    additional_info=extra,
                )
                log.info(f"{result.label}: skipped ({reason})")
                continue
            with timeout(self.timeout):
                try:
                    upload_log_file = self.make_with_log(
                        self.publish_and_upload,
                        repository_publish=self.repository_publish,
                        distributions=[dist],
                    )
                    self.update_result(
                        result,
                        status="uploaded",
                        stage="upload",
                        log_file=upload_log_file,
                        notify=True,
                        dist=dist,
                    )
                except AutoActionError as exc:
                    self._handle_error(
                        result,
                        exc,
                        "upload",
                        default_msg="Auto Upload failed",
                        dist=dist,
                    )
                except TimeoutError as timeout_exc:
                    self.update_result(
                        result,
                        status="failed",
                        stage="upload",
                        reason="Timeout",
                        additional_info="Timeout",
                        notify=True,
                        **self.notify_kwargs(result),
                    )
                    for r in self.results.values():
                        if r.status in ("pending", "building"):
                            self.update_result(
                                r,
                                status="failed",
                                stage=r.stage or "upload",
                                reason="Timeout",
                                additional_info="Timeout",
                                notify=True,
                                **self.notify_kwargs(r),
                            )
                    raise AutoActionTimeout(
                        "Timeout reached for upload!"
                    ) from timeout_exc
                except Exception as exc:
                    self._handle_error(
                        result,
                        exc,
                        "upload",
                        label="upload",
                        dist=dist,
                    )


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
                "No repository defined for template publication."
            )

        self.timeout = self.templates[0].timeout
        self.register_result(
            self.template.name, self.template, str(self.template)
        )

    def run_stages(self, stages):
        _template_stage(
            stages=stages,
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

    def notify_build_status(
        self,
        status,
        stage="build",
        log_file=None,
        additional_info=None,
        **kwargs,
    ):
        state_file = (
            self.state_dir
            / f"{self.qubes_release}-template-vm-{self.template.distribution.name}-{self.repository_publish}"
        )
        stable_state_file = (
            self.state_dir
            / f"{self.qubes_release}-template-vm-{self.template.distribution.name}-current"
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
        result = self.get_result(self.template.name)
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
                reason = f"newer template ({timestamp_existing.strftime('%Y%m%d%H%M')}) already built"
                self.update_result(
                    result,
                    status="skipped",
                    stage="build",
                    reason=reason,
                    additional_info=format_additional_info(
                        base=f"Skipped: {reason}."
                    ),
                )
                log.info(
                    f"Newer template ({timestamp_existing.strftime('%Y%m%d%H%M')}) already built."
                )
                return

        with timeout(self.timeout):
            stage = "build"
            try:
                self.update_result(
                    result, status="building", stage=stage, notify=True
                )

                self.make_with_log(
                    _component_stage,
                    config=self.config,
                    components=self.config.get_components(),
                    distributions=[],
                    stages=["fetch"],
                )

                build_log_file = self.make_with_log(
                    self.run_stages,
                    stages=["prep", "build", "sign", "publish"],
                )

                self.update_result(
                    result,
                    status="built",
                    stage=stage,
                    log_file=build_log_file,
                    notify=True,
                )

                stage = "upload"
                result.stage = stage
                self.make_with_log(self.run_stages, stages=["upload"])

                self.update_result(
                    result,
                    status="uploaded",
                    stage=stage,
                    log_file=build_log_file,
                    notify=True,
                )
            except AutoActionError as exc:
                self._handle_error(
                    result, exc, stage, default_msg="Auto Build failed"
                )
            except TimeoutError as timeout_exc:
                self.update_result(
                    result,
                    status="failed",
                    stage=stage,
                    reason="Timeout",
                    additional_info="Timeout",
                    notify=True,
                    **self.notify_kwargs(result),
                )
                raise AutoActionTimeout(
                    "Timeout reached for build!"
                ) from timeout_exc
            except Exception as exc:
                self._handle_error(result, exc, stage, label="template build")

    def upload(self):
        result = self.get_result(self.template.name)
        result.stage = "upload"
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
                self.update_result(
                    result,
                    status="uploaded",
                    stage="upload",
                    log_file=upload_log_file,
                    notify=True,
                )
            except AutoActionError as exc:
                self._handle_error(
                    result, exc, "upload", default_msg="Auto Upload failed"
                )
            except TimeoutError as timeout_exc:
                self.update_result(
                    result,
                    status="failed",
                    stage="upload",
                    reason="Timeout",
                    additional_info="Timeout",
                    notify=True,
                    **self.notify_kwargs(result),
                )
                raise AutoActionTimeout(
                    "Timeout reached for upload!"
                ) from timeout_exc
            except Exception as exc:
                self._handle_error(
                    result, exc, "upload", label="template upload"
                )


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
                "None or more than one host distribution in builder configuration file!"
            )
        self.iso_version = self.commit_sha
        self.iso_base_url = self.config.get("github", {}).get(
            "iso-base-url", None
        )

        if not self.config.get("repository-upload-remote-host", {}).get(
            "iso", None
        ):
            raise AutoActionError(
                "No remote host configured in builder configuration file!"
            )

        self.dist = host_distributions[0]
        self.package_name = f"iso-{self.dist.name}-{self.iso_version}"
        self.register_result(
            self.package_name, self.package_name, self.package_name
        )

    def run_stages(self, stages):
        for stage in stages:
            _installer_stage(
                stage_name=stage,
                config=self.config,
                iso_timestamp=self.iso_timestamp,
            )

    def notify_build_status(
        self,
        status,
        stage="build",
        log_file=None,
        additional_info=None,
        **kwargs,
    ):
        state_file = (
            self.state_dir / f"{self.qubes_release}-iso-{self.dist.name}"
        )
        stable_state_file = (
            self.state_dir
            / f"{self.qubes_release}-iso-{self.dist.name}-current"
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
            cli_run_kwargs=cli_run_kwargs, build_target=self.package_name
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
            job_url = (
                "https://openqa.qubes-os.org/tests/overview"
                f"?distri=qubesos&version={version}&build={self.iso_version}&groupid=1"
            )
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
        result = self.get_result(self.package_name)
        with timeout(self.timeout):
            stage = "build"
            try:
                self.update_result(
                    result, status="building", stage=stage, notify=True
                )

                self.make_with_log(
                    _component_stage,
                    config=self.config,
                    components=self.config.get_components(["qubes-release"]),
                    distributions=[],
                    stages=["fetch"],
                )

                build_log_file = self.make_with_log(
                    self.run_stages,
                    stages=["init-cache", "prep", "build", "sign"],
                )

                self.update_result(
                    result,
                    status="built",
                    stage=stage,
                    log_file=build_log_file,
                    notify=True,
                )

                stage = "upload"
                result.stage = stage
                self.make_with_log(self.run_stages, stages=["upload"])

                additional_info = self.trigger_openqa()

                self.update_result(
                    result,
                    status="uploaded",
                    stage=stage,
                    log_file=build_log_file,
                    additional_info=additional_info,
                    notify=True,
                )
            except AutoActionError as exc:
                self._handle_error(
                    result, exc, stage, default_msg="Auto Build failed"
                )
            except TimeoutError as timeout_exc:
                self.update_result(
                    result,
                    status="failed",
                    stage=stage,
                    reason="Timeout",
                    additional_info="Timeout",
                    notify=True,
                    **self.notify_kwargs(result),
                )
                raise AutoActionTimeout(
                    "Timeout reached for build!"
                ) from timeout_exc
            except Exception as exc:
                self._handle_error(result, exc, stage, label="ISO build")
