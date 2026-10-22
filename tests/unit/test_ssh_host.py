"""SSHHost specifics: OS detection, --become, and the Windows (PowerShell) commands.

The POSIX commands are proven against a real shell in test_host.py; here the
connection is a ScriptedRunner, so what is asserted is the command sent.
"""

import base64
from pathlib import PureWindowsPath

import pytest
from conftest import ScriptedRunner

from bigfix_actionscript_runner.host import HostError
from bigfix_actionscript_runner.ssh_host import SSHHost


def test_detects_posix_when_uname_answers():
    runner = ScriptedRunner(lambda s, d: (b"Darwin\n", b"", 0))
    host = SSHHost("u@mac", runner=runner)
    assert host.windows is False
    assert runner.commands == ["uname -s"]


def test_detects_windows_when_uname_fails():
    runner = ScriptedRunner(
        lambda s, d: (b"", b"'uname' is not recognized", 1) if s == "uname -s" else (b"", b"", 0)
    )
    host = SSHHost("u@win", runner=runner)
    assert host.windows is True
    assert host.pure is PureWindowsPath


@pytest.mark.parametrize(
    "uname", [b"MINGW64_NT-10.0-20348\r\n", b"MSYS_NT-10.0\n", b"CYGWIN_NT-10.0\n"]
)
def test_detects_windows_with_a_unix_toolset_on_path(uname):
    """Git for Windows puts a GNU uname on PATH, but the login shell is still cmd.exe."""
    runner = ScriptedRunner(lambda s, d: (uname, b"", 0))
    assert SSHHost("u@win", runner=runner).windows is True


def test_name_is_the_destination():
    assert SSHHost("u@h", runner=ScriptedRunner(), windows=False).name == "u@h"


def test_become_runs_posix_file_operations_and_commands_under_sudo():
    runner = ScriptedRunner()
    host = SSHHost("u@h", runner=runner, windows=False, become=True)
    host.mkdir("/opt/x")
    host.write_bytes("/opt/x/f", b"1")
    host.run(["/bin/true"], cwd="/tmp", wait=True)
    assert all(c.startswith("sudo -n ") for c in runner.commands)


def test_become_never_makes_the_temp_dir_as_root():
    """The sandbox belongs to the SSH user, so SFTP can put prefetches into it."""
    runner = ScriptedRunner(lambda s, d: (b"/tmp/bigfix-actionscript-runner-ab12\n", b"", 0))
    host = SSHHost("u@h", runner=runner, windows=False, become=True)
    assert host.temp_dir("bigfix-actionscript-runner-") == "/tmp/bigfix-actionscript-runner-ab12"
    assert not runner.commands[0].startswith("sudo")


def test_become_puts_files_through_a_staging_copy():
    """SFTP runs as the SSH user; a root-owned destination needs a sudo mv."""
    runner = ScriptedRunner(lambda s, d: (b"/tmp/stage.x\n", b"", 0))
    host = SSHHost("u@h", runner=runner, windows=False, become=True)
    host.put_file("local.bin", "/opt/BigFix/__Download/a.bin")
    assert runner.puts == [("local.bin", "/tmp/stage.x")]
    assert runner.commands[-1].startswith("sudo -n ")
    assert "mv" in runner.commands[-1] and "/opt/BigFix/__Download/a.bin" in runner.commands[-1]


def test_failed_operation_raises_with_stderr():
    runner = ScriptedRunner(lambda s, d: (b"", b"mkdir: /x: Permission denied\n", 1))
    host = SSHHost("u@h", runner=runner, windows=False)
    with pytest.raises(HostError, match="Permission denied"):
        host.mkdir("/x/y")


def test_close_closes_the_connection():
    runner = ScriptedRunner()
    SSHHost("u@h", runner=runner, windows=False).close()
    assert runner.closed


# --- Windows -----------------------------------------------------------------


def windows(respond=None):
    runner = ScriptedRunner(respond)
    return SSHHost("u@win", runner=runner, windows=True), runner


def test_windows_commands_go_through_powershell_encoded():
    """Windows OpenSSH's login shell is cmd.exe unless changed; see powershell_command."""
    host, runner = windows()
    host.mkdir("C:\\Program Files (x86)\\x")
    assert runner.commands[0].startswith("powershell -NoProfile -NonInteractive -EncodedCommand ")
    # .NET, not New-Item -Path: -Path treats [ and ] as wildcards
    assert "[IO.Directory]::CreateDirectory('C:\\Program Files (x86)\\x')" in runner.scripts[0]


def test_windows_paths_are_single_quoted_literals():
    host, runner = windows()
    host.delete_file("C:\\Users\\O'Brien\\$x.txt")
    assert "-LiteralPath 'C:\\Users\\O''Brien\\$x.txt'" in runner.scripts[0]


def test_windows_exists_uses_the_exit_code():
    host, _ = windows(lambda s, d: (b"", b"", 0 if "Leaf" in s else 1))
    assert host.is_file("C:\\a.txt")
    assert not host.is_dir("C:\\a.txt")


def test_windows_read_bytes_comes_back_as_base64():
    data = bytes(range(256))
    host, _ = windows(lambda s, d: (base64.b64encode(data) + b"\r\n", b"", 0))
    assert host.read_bytes("C:\\a.bin") == data


def test_windows_write_bytes_sends_base64_on_stdin():
    host, runner = windows()
    host.write_bytes("C:\\a.bin", b"\x00\xff", append=True)
    assert base64.b64decode(runner.inputs[0]) == b"\x00\xff"
    assert "Append" in runner.scripts[0]


def test_windows_sha256_is_lowercase():
    host, _ = windows(lambda s, d: (b"2CF24DBA\r\n", b"", 0))
    assert host.sha256("C:\\a") == "2cf24dba"


def test_windows_run_waits_with_start_process_and_returns_the_exit_code():
    host, runner = windows(lambda s, d: (b"", b"", 5))
    code = host.run(
        ["C:\\Program Files\\x\\app.exe", "/quiet", "a b"], cwd="C:\\Temp\\sb\\site", wait=True
    )
    assert code == 5
    script = runner.scripts[0]
    assert "Start-Process -FilePath 'C:\\Program Files\\x\\app.exe'" in script
    assert "-ArgumentList '/quiet \"a b\"'" in script
    assert "-WorkingDirectory 'C:\\Temp\\sb\\site'" in script
    assert "-Wait" in script and "exit $p.ExitCode" in script


def test_windows_run_without_wait_does_not_wait():
    host, runner = windows()
    assert host.run(["cmd.exe", "/c", "x"], cwd="C:\\", wait=False) == 0
    assert "-Wait" not in runner.scripts[0]


def test_windows_put_file_goes_straight_to_the_destination():
    host, runner = windows()
    host.put_file("local.bin", "C:\\Temp\\sb\\site\\__Download\\a.bin")
    assert runner.puts == [("local.bin", "C:/Temp/sb/site/__Download/a.bin")]


def test_windows_temp_dir_is_under_the_users_temp():
    host, runner = windows(
        lambda s, d: (b"C:\\Users\\u\\AppData\\Local\\Temp\\bfasr-1a2b\r\n", b"", 0)
    )
    assert host.temp_dir("bfasr-") == "C:\\Users\\u\\AppData\\Local\\Temp\\bfasr-1a2b"
    assert "GetTempPath()" in runner.scripts[0] and "'bfasr-'" in runner.scripts[0]


def test_timeouts_become_host_errors():
    def respond(source, data):
        raise TimeoutError(source)

    host = SSHHost("u@h", runner=ScriptedRunner(respond), windows=False)
    with pytest.raises(HostError, match="timed out after 3 seconds"):
        host.run(["/bin/sleep", "9"], cwd="/tmp", wait=True, timeout=3)


def test_a_garbled_hash_is_an_error():
    host = SSHHost("u@h", runner=ScriptedRunner(lambda s, d: (b"", b"", 0)), windows=False)
    with pytest.raises(HostError, match="can't hash"):
        host.sha256("/x")


def test_a_program_that_exits_127_by_itself_is_not_missing():
    host = SSHHost("u@h", runner=ScriptedRunner(lambda s, d: (b"", b"oops", 127)), windows=False)
    assert host.run(["/bin/prog"], cwd="/tmp", wait=True) == 127


def test_windows_start_failures_are_host_errors():
    host, _ = windows(lambda s, d: (b"", b"bfasr: This command cannot be run\r\n", 127))
    with pytest.raises(HostError, match="can't run: This command cannot be run"):
        host.run(["C:\\nope.exe"], cwd="C:\\", wait=True)
