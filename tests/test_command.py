import datetime
import subprocess
import time
from pathlib import Path

import psutil

from conftest import set_dry_run

FEPITRE_FPR = "9FA64B92F95E706BF28E2CA6484010B5CDC576E2"
TESTUSER_FPR = "632F8C69E01B25C9E0C3ADF2F360C0D259FB650C"


def get_all_processes():
    all_processes = []
    for i in range(50):
        for proc in psutil.process_iter():
            try:
                if not proc.cmdline():
                    continue
                cmd = " ".join(proc.cmdline())
                if cmd not in all_processes:
                    all_processes.append(cmd)
            except psutil.Error:
                pass
        time.sleep(0.1)
    return all_processes


def find_github_action(processes, cmdline):
    found = False
    for p in processes:
        if p == cmdline:
            found = True
    return found


def create_builders_list(directory, release="r4.3"):
    builders = [
        (release, f"{directory}/qubes-builderv2", f"{directory}/builder.yml")
    ]
    with open(f"{directory}/builders.list", "w") as f:
        for line in builders:
            release, builder_dir, builder_conf = line
            f.write(f"{release}={builder_dir}={builder_conf}")
    return builders


def test_command_00_build_component(workdir):
    tmpdir, env = workdir

    # Create builder list
    builders_list = create_builders_list(tmpdir)

    # Write command
    with open(f"{tmpdir}/command", "w") as f:
        f.write(f"Build-component app-linux-split-gpg")

    # Dry-run
    set_dry_run(f"{tmpdir}/builder.yml")

    cmd = [
        str(tmpdir / "qubes-builder-github/github-command.py"),
        "dispatch",
        "--scripts-dir",
        str(tmpdir / "qubes-builder-github"),
        "--config-file",
        f"{tmpdir}/builders.list",
        "--signer-fpr",
        FEPITRE_FPR,
        "Build-component",
        f"{tmpdir}/command",
    ]
    command_process = subprocess.Popen(cmd, env=env)
    all_processes = get_all_processes()
    for b in builders_list:
        release, builder_dir, builder_conf = b
        cmdline = f"flock -x {builder_dir}/builder.lock bash -c {tmpdir / 'qubes-builder-github'}/github-command.py action --signer-fpr {FEPITRE_FPR} --command-title 'Build-component app-linux-split-gpg' --release-name {release} build-component {builder_dir} {builder_conf} app-linux-split-gpg"
        if not find_github_action(all_processes, cmdline):
            raise ValueError(f"{cmdline}: cannot find process.")
    command_process.communicate()
    if command_process.poll() != 0:
        raise ValueError("github-command failed.")


def test_command_01_upload_component(workdir):
    tmpdir, env = workdir

    commit_sha = "c5316c91107b8930ab4dc3341bc75293139b5b84"

    # Create builder list
    builders_list = create_builders_list(tmpdir)

    # Write command
    with open(f"{tmpdir}/command", "w") as f:
        f.write(
            f"Upload-component r4.3 app-linux-split-gpg {commit_sha} current all"
        )

    # Dry-run
    set_dry_run(f"{tmpdir}/builder.yml")

    cmd = [
        str(tmpdir / "qubes-builder-github/github-command.py"),
        "dispatch",
        "--scripts-dir",
        str(tmpdir / "qubes-builder-github"),
        "--config-file",
        f"{tmpdir}/builders.list",
        "--signer-fpr",
        FEPITRE_FPR,
        "Upload-component",
        f"{tmpdir}/command",
    ]
    command_process = subprocess.Popen(cmd, env=env)
    all_processes = get_all_processes()
    for b in builders_list:
        release, builder_dir, builder_conf = b
        cmdline = f"flock -x {builder_dir}/builder.lock bash -c {tmpdir / 'qubes-builder-github'}/github-command.py action --signer-fpr {FEPITRE_FPR} --command-title 'Upload-component r4.3 app-linux-split-gpg {commit_sha} current all' --release-name {release} upload-component {builder_dir} {builder_conf} app-linux-split-gpg {commit_sha} current --distribution all"
        if not find_github_action(all_processes, cmdline):
            raise ValueError(f"{cmdline}: cannot find process.")
    command_process.communicate()
    if command_process.poll() != 0:
        raise ValueError("github-command failed.")


def test_command_02_build_template(workdir):
    tmpdir, env = workdir

    # Create builder list
    builders_list = create_builders_list(tmpdir)

    timestamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%d%H%M")
    with open(tmpdir / "timestamp", "w") as f:
        f.write(timestamp)

    # Write command
    with open(f"{tmpdir}/command", "w") as f:
        f.write(f"Build-template r4.3 debian-13 {timestamp}")

    # Dry-run
    set_dry_run(f"{tmpdir}/builder.yml")

    cmd = [
        str(tmpdir / "qubes-builder-github/github-command.py"),
        "dispatch",
        "--scripts-dir",
        str(tmpdir / "qubes-builder-github"),
        "--config-file",
        f"{tmpdir}/builders.list",
        "--signer-fpr",
        FEPITRE_FPR,
        "Build-template",
        f"{tmpdir}/command",
    ]
    command_process = subprocess.Popen(cmd, env=env)
    all_processes = get_all_processes()
    for b in builders_list:
        release, builder_dir, builder_conf = b
        cmdline = f"flock -x {builder_dir}/builder.lock bash -c {tmpdir / 'qubes-builder-github'}/github-command.py action --signer-fpr {FEPITRE_FPR} --command-title 'Build-template r4.3 debian-13 {timestamp}' --release-name {release} build-template {builder_dir} {builder_conf} debian-13 {timestamp}"
        if not find_github_action(all_processes, cmdline):
            raise ValueError(f"{cmdline}: cannot find process.")
    command_process.communicate()
    if command_process.poll() != 0:
        raise ValueError("github-command failed.")


def test_command_03_upload_template(workdir):
    tmpdir, env = workdir

    # Create builder list
    builders_list = create_builders_list(tmpdir)

    with open(tmpdir / "timestamp", "r") as f:
        timestamp = f.read().rstrip("\n")

    # Write command
    with open(f"{tmpdir}/command", "w") as f:
        f.write(
            f"Upload-template r4.3 debian-13 4.3.0-{timestamp} templates-itl"
        )

    # Dry-run
    set_dry_run(f"{tmpdir}/builder.yml")

    cmd = [
        str(tmpdir / "qubes-builder-github/github-command.py"),
        "dispatch",
        "--scripts-dir",
        str(tmpdir / "qubes-builder-github"),
        "--config-file",
        f"{tmpdir}/builders.list",
        "--signer-fpr",
        FEPITRE_FPR,
        "Upload-template",
        f"{tmpdir}/command",
    ]
    command_process = subprocess.Popen(cmd, env=env)
    all_processes = get_all_processes()
    for b in builders_list:
        release, builder_dir, builder_conf = b
        cmdline = f"flock -x {builder_dir}/builder.lock bash -c {tmpdir / 'qubes-builder-github'}/github-command.py action --signer-fpr {FEPITRE_FPR} --command-title 'Upload-template r4.3 debian-13 4.3.0-{timestamp} templates-itl' --release-name {release} upload-template {builder_dir} {builder_conf} debian-13 4.3.0-{timestamp} templates-itl"
        if not find_github_action(all_processes, cmdline):
            raise ValueError(f"{cmdline}: cannot find process.")
    command_process.communicate()
    if command_process.poll() != 0:
        raise ValueError("github-command failed.")


def test_command_04_build_iso(workdir):
    tmpdir, env = workdir

    # Create builder list
    builders_list = create_builders_list(tmpdir)

    timestamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%d%H%M")
    with open(tmpdir / "timestamp", "w") as f:
        f.write(timestamp)

    # Write command
    with open(f"{tmpdir}/command", "w") as f:
        f.write(f"Build-iso r4.3 4.3.{timestamp} {timestamp}")

    # Dry-run
    set_dry_run(f"{tmpdir}/builder.yml")

    cmd = [
        str(tmpdir / "qubes-builder-github/github-command.py"),
        "dispatch",
        "--scripts-dir",
        str(tmpdir / "qubes-builder-github"),
        "--config-file",
        f"{tmpdir}/builders.list",
        "--signer-fpr",
        FEPITRE_FPR,
        "Build-iso",
        f"{tmpdir}/command",
    ]
    command_process = subprocess.Popen(cmd, env=env)
    all_processes = get_all_processes()
    for b in builders_list:
        release, builder_dir, builder_conf = b
        cmdline = f"flock -x {builder_dir}/builder.lock bash -c {tmpdir / 'qubes-builder-github'}/github-command.py action --signer-fpr {FEPITRE_FPR} --command-title 'Build-iso r4.3 4.3.{timestamp} {timestamp}' --release-name {release} build-iso {builder_dir} {builder_conf} 4.3.{timestamp} {timestamp}"
        if not find_github_action(all_processes, cmdline):
            raise ValueError(f"{cmdline}: cannot find process.")
    command_process.communicate()
    if command_process.poll() != 0:
        raise ValueError("github-command failed.")


def test_command_05_reject_stale_timestamp(workdir):
    tmpdir, env = workdir

    # Create builder list
    create_builders_list(tmpdir, release="r4.3")

    stale = (
        datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=2)
    ).strftime("%Y%m%d%H%M")

    # Write command
    with open(f"{tmpdir}/command", "w") as f:
        f.write(f"Build-template r4.3 whonix-gateway-18 {stale}")

    cmd = [
        str(tmpdir / "qubes-builder-github/github-command.py"),
        "dispatch",
        "--wait",
        "--no-builders-update",
        "--scripts-dir",
        str(tmpdir / "qubes-builder-github"),
        "--config-file",
        f"{tmpdir}/builders.list",
        "--signer-fpr",
        FEPITRE_FPR,
        "Build-template",
        f"{tmpdir}/command",
    ]
    result = subprocess.run(cmd, env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert "Timestamp outside of allowed range" in result.stderr


def test_command_06_skip_unsupported_template(workdir):
    tmpdir, env = workdir

    # Create builder list
    create_builders_list(tmpdir, release="r4.3")

    timestamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%d%H%M")

    # Write command
    with open(f"{tmpdir}/command", "w") as f:
        f.write(f"Build-template r4.3 no-such-template {timestamp}")

    command_log = Path(tmpdir) / "skip-template.log"

    cmd = [
        str(tmpdir / "qubes-builder-github/github-command.py"),
        "dispatch",
        "--wait",
        "--no-builders-update",
        "--scripts-dir",
        str(tmpdir / "qubes-builder-github"),
        "--config-file",
        f"{tmpdir}/builders.list",
        "--command-log",
        str(command_log),
        "--signer-fpr",
        FEPITRE_FPR,
        "Build-template",
        f"{tmpdir}/command",
    ]
    result = subprocess.run(cmd, env=env, capture_output=True, text=True)
    # an unsupported template is a skip, not a failure
    assert result.returncode == 0

    child_log = Path(tmpdir) / "skip-template-r4.3.log"
    assert "unsupported template `no-such-template`" in child_log.read_text()


def test_command_07_unknown_component_fails(workdir):
    tmpdir, env = workdir

    # Create builder list
    create_builders_list(tmpdir, release="r4.3")

    commit_sha = "c5316c91107b8930ab4dc3341bc75293139b5b84"

    # Write command
    with open(f"{tmpdir}/command", "w") as f:
        f.write(
            f"Upload-component r4.3 no-such-component {commit_sha} security-testing vm-trixie"
        )

    command_log = Path(tmpdir) / "unknown-component.log"

    cmd = [
        str(tmpdir / "qubes-builder-github/github-command.py"),
        "dispatch",
        "--wait",
        "--no-builders-update",
        "--scripts-dir",
        str(tmpdir / "qubes-builder-github"),
        "--config-file",
        f"{tmpdir}/builders.list",
        "--command-log",
        str(command_log),
        "--signer-fpr",
        FEPITRE_FPR,
        "Upload-component",
        f"{tmpdir}/command",
    ]
    result = subprocess.run(cmd, env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert "Failed to handle command for: r4.3" in result.stderr

    child_log = Path(tmpdir) / "unknown-component-r4.3.log"
    assert "No such component 'no-such-component'" in child_log.read_text()
