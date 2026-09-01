import importlib.util
import os
import random
import shutil
import string
import subprocess
import sys
import time
from copy import deepcopy
from pathlib import Path

import pytest
import yaml
from github import Github, Auth

PROJECT_PATH = Path(__file__).resolve().parents[1]
DEFAULT_BUILDER_CONF = PROJECT_PATH / "tests/builder.yml"

_keyboard_interrupted = False


def pytest_keyboard_interrupt(excinfo):
    global _keyboard_interrupted
    _keyboard_interrupted = True


# qubesbuilder/config
def deep_merge(a: dict, b: dict, allow_append: bool = False) -> dict:
    result = deepcopy(a)
    for b_key, b_value in b.items():
        a_value = result.get(b_key, None)
        if isinstance(a_value, dict) and isinstance(b_value, dict):
            result[b_key] = deep_merge(a_value, b_value, allow_append)
        else:
            if allow_append and isinstance(result.get(b_key, None), list):
                result[b_key] += deepcopy(b_value)
            else:
                result[b_key] = deepcopy(b_value)
    return result


def get_random_string(length):
    letters = string.ascii_lowercase
    result_str = "".join(random.choice(letters) for _ in range(length))
    return result_str


def load_module(name: str, path: Path):
    """
    Load a Python module from *path* by file, register it as *name*
    in sys.modules, and return the module object.
    """
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def load_qubesbuilder_module(tmpdir, name: str):
    """
    Load any qubesbuilder.* submodule from the cloned
    qubes-builderv2 tree and return the module object.
    """
    qb_root = Path(tmpdir) / "qubes-builderv2"
    if str(qb_root) not in sys.path:
        sys.path.insert(0, str(qb_root))
    return load_module(name, qb_root / (name.replace(".", "/") + ".py"))


def load_action_module(env: dict, project_path: Path, monkeypatch):
    """
    Load githubbuilder/action.py as a fresh module instance with env applied.
    """
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    py_path = env.get("PYTHONPATH", "")
    for entry in reversed([p for p in py_path.split(os.pathsep) if p]):
        monkeypatch.syspath_prepend(entry)
    monkeypatch.syspath_prepend(str(project_path))
    mod = load_module(
        "githubbuilder.action", project_path / "githubbuilder/action.py"
    )
    monkeypatch.setitem(sys.modules, "githubbuilder.action", mod)
    return mod


def load_notify_issues_module():
    """
    Load githubbuilder/notify_issues.py as a module object.
    """
    return load_module(
        "githubbuilder.notify_issues",
        PROJECT_PATH / "githubbuilder/notify_issues.py",
    )


def load_command_report_module():
    """
    Load githubbuilder/command_report.py as a module object.
    """
    return load_module(
        "githubbuilder.command_report",
        PROJECT_PATH / "githubbuilder/command_report.py",
    )


def make_distribution(distribution: str):
    """
    Return a QubesDistribution instance.
    """
    return sys.modules["qubesbuilder.distribution"].QubesDistribution(
        distribution
    )


def make_config(builder_conf):
    """
    Return a Config instance.
    """
    return sys.modules["qubesbuilder.config"].Config(str(builder_conf))


@pytest.fixture(scope="session")
def token():
    github_api_key = os.environ.get("GITHUB_API_KEY")
    if not github_api_key:
        raise ValueError("Cannot find GITHUB_API_KEY.")
    return github_api_key


@pytest.fixture(scope="session")
def github_repository(token):
    g = Github(auth=Auth.Token(token))
    user = g.get_user()
    if user.login != "fepitre2-bot":
        raise ValueError(f"Unexpected user '{user}'.")
    repo_name = f"tests-{get_random_string(16)}"
    repo = user.create_repo(repo_name)
    try:
        yield repo
    finally:
        # optional inspection window before the repo goes away, skipped on
        # keyboard interrupt
        inspect_seconds = int(os.environ.get("TESTS_INSPECT_SECONDS", "0"))
        if inspect_seconds > 0 and not _keyboard_interrupted:
            print(
                f"\nInspect {repo.html_url} (deleting in {inspect_seconds} seconds)",
                flush=True,
            )
            try:
                time.sleep(inspect_seconds)
            except KeyboardInterrupt:
                pass
        repo.delete()


@pytest.fixture(scope="session")
def base_workdir(tmpdir_factory):
    tmpdir = tmpdir_factory.mktemp("github-")
    shutil.copytree(PROJECT_PATH, tmpdir / "qubes-builder-github")

    env = os.environ.copy()
    # Enforce keyring location
    env["GNUPGHOME"] = str(tmpdir / ".gnupg")
    # We prevent rpm to find ~/.rpmmacros and put logs into workdir
    env["HOME"] = str(tmpdir)

    yield tmpdir, env


@pytest.fixture(scope="session")
def workdir(base_workdir):
    tmpdir, env = base_workdir

    # Better copy testing keyring into a separate directory to prevent locks inside
    # local sources (when executed locally).
    gnupghome = f"{tmpdir}/.gnupg"
    shutil.copytree(PROJECT_PATH / "tests/gnupg", gnupghome)
    os.chmod(gnupghome, 0o700)

    # Copy builder.yml
    shutil.copy2(DEFAULT_BUILDER_CONF, tmpdir)

    extra_conf = f"""
artifacts-dir: {tmpdir}/artifacts

repository-upload-remote-host:
  rpm: {tmpdir}/repo/rpm/r4.2
  deb: {tmpdir}/repo/deb/r4.2
  iso: {tmpdir}/repo/iso/r4.2

executor:
  type: qubes
  options:
    dispvm: "builder-dvm"
"""
    with open(f"{tmpdir}/builder.yml", "a") as f:
        f.write(extra_conf)

    # Clone qubes-builderv2 (GitLab)
    run_cmd(
        [
            "git",
            "-C",
            str(tmpdir),
            "clone",
            "-b",
            os.getenv("CI_QUBES_BUILDER_BRANCH", "main"),
            "--recurse-submodules",
            os.getenv(
                "CI_QUBES_BUILDER_URL",
                "https://gitlab.com/QubesOS/qubes-builderv2",
            ),
        ],
        check=True,
        capture_output=True,
    )

    # Load qubesbuilder and githubbuilder modules into sys.modules
    load_qubesbuilder_module(tmpdir, "qubesbuilder.distribution")
    load_qubesbuilder_module(tmpdir, "qubesbuilder.config")
    load_notify_issues_module()

    # Enforce keyring location
    env["GNUPGHOME"] = str(tmpdir / ".gnupg")
    # Set PYTHONPATH with cloned qubes-builderv2
    env["PYTHONPATH"] = (
        f"{tmpdir / 'qubes-builderv2'!s}:{os.environ.get('PYTHONPATH','')}"
    )
    env["PYTHONUNBUFFERED"] = "1"

    if env.get("CI_PROJECT_DIR", None):
        cache_dir = (Path(env["CI_PROJECT_DIR"]) / "cache").resolve()
        if cache_dir.is_dir():
            shutil.copytree(cache_dir, tmpdir / "artifacts/cache")

    yield tmpdir, env


def make_fake_qrexec(bin_dir, capture_dir):
    """
    Create a fake qrexec-client-vm capturing its stdin and service name,
    replying like qubesbuilder.BuildLog does.
    """
    bin_dir = Path(bin_dir)
    capture_dir = Path(capture_dir)
    bin_dir.mkdir(parents=True, exist_ok=True)
    capture_dir.mkdir(parents=True, exist_ok=True)
    fake = bin_dir / "qrexec-client-vm"
    fake.write_text(
        "#!/bin/sh\n"
        f'cat > "{capture_dir}/upload.$$"\n'
        f'echo "$2" > "{capture_dir}/service.$$"\n'
        'echo "test-vm/log_2026-01-01_00-00-00"\n'
    )
    fake.chmod(0o755)
    return fake


def get_fake_qrexec_uploads(capture_dir):
    return sorted(Path(capture_dir).glob("upload.*"))


def set_conf_options(builder_conf, options):
    with open(builder_conf, "r") as f:
        conf = yaml.safe_load(f.read())
    conf = deep_merge(conf, options)
    with open(builder_conf, "w") as f:
        f.write(yaml.dump(conf))


def set_dry_run(builder_conf):
    set_conf_options(builder_conf, {"github": {"dry-run": True}})


def get_issue(issue_title, repository, retries=3, delay=5):
    # the issues listing can lag behind a just-created issue
    for attempt in range(retries):
        if attempt:
            time.sleep(delay)
        for i in repository.get_issues():
            if i.title == issue_title:
                return i
    return None


def run_cmd(cmd, **kwargs):
    try:
        return subprocess.run(cmd, **kwargs)
    except subprocess.CalledProcessError as e:
        pytest.fail(
            f"Command failed:\n{' '.join(e.cmd)}\n"
            f"Return code: {e.returncode}\n"
            f"STDOUT:\n{e.stdout}\n"
            f"STDERR:\n{e.stderr}"
        )
