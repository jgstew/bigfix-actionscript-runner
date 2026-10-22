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

import functools
import os
import subprocess

import pytest


@functools.lru_cache(maxsize=1)
def _live_qna_status() -> tuple[bool, str]:
    from bigfix_remote_client_relevance.qna_paths import find_qna_path

    if find_qna_path() is None:
        return False, "no local qna binary found"
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
