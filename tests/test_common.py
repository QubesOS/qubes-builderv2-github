import importlib.util
import subprocess
import sys
from pathlib import Path

PROJECT_PATH = Path(__file__).resolve().parents[1]


def test_qubesbuilder_buildlog(workdir):
    tmpdir, env = workdir
    env["QREXEC_REMOTE_DOMAIN"] = "testvm"
    p = subprocess.run(
        [
            "python3",
            str(
                tmpdir
                / "qubes-builder-github/rpc-services/qubesbuilder.BuildLog"
            ),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        input="build-vm-42",
        check=True,
        text=True,
        env=env,
    )
    # fixme: find a better way to load/unload module for each future test
    sys.path.insert(0, str(tmpdir / "qubes-builder-github"))
    sys.path.insert(0, str(tmpdir / "qubes-builderv2"))

    github_action_spec = importlib.util.spec_from_file_location(
        "github_action", str(PROJECT_PATH / "github-action.py")
    )
    github_action = importlib.util.module_from_spec(github_action_spec)
    github_action_spec.loader.exec_module(github_action)

    log_file = github_action.get_log_file_from_qubesbuilder_buildlog(p.stdout)
    assert log_file is not None
    assert log_file.startswith("testvm/log_")
