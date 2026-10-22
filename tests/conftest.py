"""Shared test scaffolding: marker auto-skip.

The unit suite must pass offline on a bare machine. Anything needing a real
qna binary, an SSH target or the network carries a marker and is skipped
automatically when the prerequisite is absent:

    live_qna     a local qna (bigfix-remote-client-relevance --local; on macOS
                 it runs qna under `sudo -n`, so passwordless sudo for qna)
    ssh_posix    BFASR_SSH_POSIX_HOST=user@host, a macOS or Linux test machine
                 with qna (macOS targets also need BFASR_SSH_BECOME=1)
    ssh_windows  BFASR_SSH_WINDOWS_HOST=user@host, a Windows test machine with qna
    network      BFASR_NETWORK_TESTS=1

The SSH tests change files on the target outside the sandbox (that is what
they test), so point them only at test machines.
"""

from __future__ import annotations

import base64
import functools
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable

import pytest

# --- stand-ins for an SSH connection ------------------------------------------


class LocalShellRunner:
    """An "SSH connection" to this machine: each command runs under `sh -c`.

    Real processes rather than a mock: the POSIX commands SSHHost builds
    (quoting, sudo-less `sh -c` wrappers, exit codes, binary stdin/stdout) are
    exactly what needs proving, and only a real shell proves them.
    """

    def __init__(self) -> None:
        self.commands: list[str] = []
        self.closed = False

    def run(
        self, command: str, *, input: bytes | None = None, timeout: float | None = None
    ) -> tuple[bytes, bytes, int]:
        self.commands.append(command)
        try:
            p = subprocess.run(
                ["sh", "-c", command],
                input=input,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as e:
            raise TimeoutError(command) from e
        return p.stdout, p.stderr, p.returncode

    def put_file(self, local, remote: str) -> None:
        shutil.copyfile(local, remote)

    def close(self) -> None:
        self.closed = True


class ScriptedRunner:
    """Records commands and answers from `respond(source, input)`.

    Windows commands arrive as `powershell ... -EncodedCommand <base64>`;
    `scripts` holds the decoded PowerShell, which is what tests assert on.
    """

    def __init__(
        self,
        respond: Callable[[str, bytes | None], tuple[bytes, bytes, int]] | None = None,
    ) -> None:
        self.commands: list[str] = []
        self.inputs: list[bytes | None] = []
        self.puts: list[tuple[str, str]] = []
        self.respond = respond or (lambda source, data: (b"", b"", 0))
        self.closed = False

    @property
    def scripts(self) -> list[str]:
        return [decode_powershell(c) for c in self.commands]

    def run(
        self, command: str, *, input: bytes | None = None, timeout: float | None = None
    ) -> tuple[bytes, bytes, int]:
        self.commands.append(command)
        self.inputs.append(input)
        return self.respond(decode_powershell(command), input)

    def put_file(self, local, remote: str) -> None:
        self.puts.append((str(local), remote))

    def close(self) -> None:
        self.closed = True


def decode_powershell(command: str) -> str:
    """The PowerShell source inside a powershell -EncodedCommand line (else the command)."""
    m = re.search(r"-EncodedCommand (\S+)", command)
    if not m:
        return command
    return base64.b64decode(m.group(1)).decode("utf-16-le")


@pytest.fixture
def shell_host():
    """An SSHHost whose "SSH connection" is a local POSIX shell."""
    if os.name == "nt":
        pytest.skip("needs a POSIX shell")
    from bigfix_actionscript_runner.ssh_host import SSHHost

    host = SSHHost("tester@shell", runner=LocalShellRunner(), windows=False)
    yield host
    host.close()


@functools.lru_cache(maxsize=1)
def _live_qna_status() -> tuple[bool, str]:
    from bigfix_remote_client_relevance.qna_paths import find_qna_path

    qna = find_qna_path()
    if qna is None:
        return False, "no local qna binary found"
    if sys.platform == "darwin" and os.geteuid() != 0:
        # bigfix-remote-client-relevance runs macOS qna under `sudo -n`; `-l` asks
        # whether that is allowed without running it (sudoers rules are often qna-only)
        sudo = subprocess.run(["sudo", "-n", "-l", qna], capture_output=True, check=False)
        if sudo.returncode != 0:
            return False, "macOS qna needs root: passwordless sudo, or sudo -E uv run pytest"
    return True, ""


@functools.lru_cache(maxsize=4)
def _ssh_status(env: str) -> tuple[bool, str]:
    host = os.environ.get(env)
    if not host:
        return False, f"set {env}=user@host to run these tests against a real test machine"
    completed = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host, "echo ok"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode == 0:
        return True, ""
    return False, f"cannot ssh to {host}: {completed.stderr.strip() or 'unknown failure'}"


def pytest_collection_modifyitems(config, items):
    for item in items:
        checks = []
        if "live_qna" in item.keywords:
            checks.append(_live_qna_status())
        if "ssh_posix" in item.keywords:
            checks.append(_ssh_status("BFASR_SSH_POSIX_HOST"))
        if "ssh_windows" in item.keywords:
            checks.append(_ssh_status("BFASR_SSH_WINDOWS_HOST"))
        if "network" in item.keywords and os.environ.get("BFASR_NETWORK_TESTS") != "1":
            checks.append((False, "network tests are opt-in: set BFASR_NETWORK_TESTS=1"))
        for ok, reason in checks:
            if not ok:
                item.add_marker(pytest.mark.skip(reason=reason))
                break
