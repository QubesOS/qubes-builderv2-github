import subprocess

from conftest import load_action_module, run_cmd


def test_qubesbuilder_buildlog(workdir, monkeypatch):
    tmpdir, env = workdir
    env["QREXEC_REMOTE_DOMAIN"] = "testvm"
    p = run_cmd(
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
    mod = load_action_module(env, tmpdir / "qubes-builder-github", monkeypatch)

    log_file = mod.get_log_file_from_qubesbuilder_buildlog(p.stdout)
    assert log_file is not None
    assert log_file.startswith("testvm/log_")
