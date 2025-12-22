import os
import random
import shutil
import string
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest
import yaml
from github import Github, Auth

PROJECT_PATH = Path(__file__).resolve().parents[1]
DEFAULT_BUILDER_CONF = PROJECT_PATH / "tests/builder.yml"


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
    yield repo
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

    with open(f"{tmpdir}/builder.yml", "a") as f:
        f.write(
            f"""
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
        )

    # Clone qubes-builderv2 (GitLab)
    subprocess.run(
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


def set_conf_options(builder_conf, options):
    with open(builder_conf, "r") as f:
        conf = yaml.safe_load(f.read())
    conf = deep_merge(conf, options)
    with open(builder_conf, "w") as f:
        f.write(yaml.dump(conf))


def set_dry_run(builder_conf):
    set_conf_options(builder_conf, {"github": {"dry-run": True}})


def get_issue(issue_title, repository):
    issue = None
    for i in repository.get_issues():
        if i.title == issue_title:
            issue = i
            break
    return issue
