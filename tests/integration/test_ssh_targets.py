"""Against real SSH test machines (opt in, see tests/conftest.py).

BFASR_SSH_POSIX_HOST=user@host   a macOS or Linux machine with qna
BFASR_SSH_BECOME=1               run under sudo -n there (macOS qna needs root)
BFASR_SSH_WINDOWS_HOST=user@host a Windows machine with qna

These change files on the target, all inside a temp folder they remove
afterwards (sandboxes included).
"""

import json
import os
import uuid

import pytest
from typer.testing import CliRunner

from bigfix_actionscript_runner import cli
from bigfix_actionscript_runner.ssh_host import SSHHost

runner = CliRunner()

TARGETS = [
    pytest.param("BFASR_SSH_POSIX_HOST", marks=pytest.mark.ssh_posix, id="posix"),
    pytest.param("BFASR_SSH_WINDOWS_HOST", marks=pytest.mark.ssh_windows, id="windows"),
]


def become(env):
    return env == "BFASR_SSH_POSIX_HOST" and os.environ.get("BFASR_SSH_BECOME") == "1"


@pytest.fixture(params=TARGETS)
def target(request):
    env = request.param
    host = SSHHost(os.environ[env], become=become(env))
    scratch = host.temp_dir("bfasr-it-")
    yield env, host, scratch
    host.delete_tree(scratch)
    host.close()


def j(host, *parts):
    return str(host.pure(*parts))


def invoke(tmp_path, env, script, sandbox, *args):
    """Run the CLI with its sandbox inside the fixture's scratch folder, so it is removed."""
    p = tmp_path / "a.bas"
    p.write_text(script)
    argv = [str(p), "--json", "--ssh", os.environ[env], "--sandbox", sandbox, *args]
    if become(env):
        argv.append("--become")
    r = runner.invoke(cli.app, argv)
    return r, json.loads(r.output)


def test_host_contract_on_the_target(target, tmp_path):
    _, host, scratch = target
    f = j(host, scratch, "it's a file.bin")
    host.write_bytes(f, bytes(range(256)))
    host.write_bytes(f, b"!", append=True)
    assert host.read_bytes(f) == bytes(range(256)) + b"!"
    local = tmp_path / "x.bin"
    local.write_bytes(b"hello")
    host.mkdir(j(host, scratch, "sub dir"))
    host.put_file(local, j(host, scratch, "sub dir", "x.bin"))
    assert host.sha256(j(host, scratch, "sub dir", "x.bin")) == (
        "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    )
    host.copy_file(f, j(host, scratch, "copy"))
    host.move(j(host, scratch, "copy"), j(host, scratch, "moved"))
    assert host.is_file(j(host, scratch, "moved")) and not host.exists(j(host, scratch, "copy"))


def test_run_waits_and_returns_the_exit_code(target):
    _, host, scratch = target
    if host.windows:
        argv = ["cmd.exe", "/c", "cd > ran-in.txt & exit 5"]
    else:
        argv = ["/bin/sh", "-c", "pwd > ran-in.txt; exit 5"]
    assert host.run(argv, cwd=scratch, wait=True, timeout=60) == 5
    ran_in = host.read_bytes(j(host, scratch, "ran-in.txt")).decode().strip()
    assert ran_in.lower().endswith(host.pure(scratch).name.lower())


def test_dry_run_on_the_target_sees_its_own_sandbox(target, tmp_path):
    env, host, scratch = target
    outside = j(host, scratch, "outside.txt")
    r, data = invoke(
        tmp_path,
        env,
        "createfile until E\nhello\nE\n"
        'continue if {exists file "__createfile" of client folder of current site}\n'
        'parameter "os" = "{name of operating system}"\n'
        f'copy __createfile "{outside}"\n',
        j(host, scratch, "sb"),
    )
    assert r.exit_code == 0, r.output
    assert data["status"] == "completed"
    assert data["sandbox_mode"] == "full"
    assert data["target"] == os.environ[env]
    assert not host.exists(outside)  # a dry run changes nothing outside its sandbox


def test_execute_changes_real_files_on_the_target(target, tmp_path):
    env, host, scratch = target
    real = j(host, scratch, f"real-{uuid.uuid4().hex[:6]}")
    if host.windows:
        script = (
            "createfile until E\n@echo off\necho ran> ran.txt\nexit /b 3\nE\n"
            f'folder create "{real}"\n'
            f'copy __createfile "{real}\\job.cmd"\n'
            f'wait cmd.exe /c "{real}\\job.cmd"\n'
            "continue if {exit code of action = 3}\n"
        )
    else:
        script = (
            "createfile until E\necho ran > ran.txt\nexit 3\nE\n"
            f'folder create "{real}"\n'
            f'copy __createfile "{real}/job.sh"\n'
            f'wait /bin/sh "{real}/job.sh"\n'
            "continue if {exit code of action = 3}\n"
        )
    r, data = invoke(tmp_path, env, script, j(host, scratch, "sb"), "--execute")
    assert r.exit_code == 0, r.output
    assert data["sandbox_mode"] == "client"
    assert host.is_file(j(host, real, "job.cmd" if host.windows else "job.sh"))
    # the job ran in the sandboxed site folder, on the target
    assert host.is_file(j(host, data["sandbox"], "site", "ran.txt"))
