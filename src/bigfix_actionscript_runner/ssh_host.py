"""A target machine over SSH: the action's file operations and commands run there.

Everything is a command on one SSH connection (plus SFTP for putting
prefetched files), so the sandbox, the files the action changes and the
commands it runs all live on the target, where relevance (qna) sees them.

POSIX targets get `sh -c` scripts, under `sudo -n` with `become=True` (a
BigFix client runs actions as root). Windows targets get PowerShell, wrapped
by bigfix-remote-client-relevance's powershell_command, because Windows
OpenSSH's login shell is cmd.exe unless someone changed it.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import shlex
import subprocess
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Any, Protocol

from bigfix_remote_client_relevance.transports.ssh import connect_kwargs, powershell_command

from .host import HostError

logger = logging.getLogger(__name__)

# printed by our own wrappers, so "program not found" can be told apart from a
# program that itself exits 127
_MARK = "bfasr:"


class CommandRunner(Protocol):
    """The slice of an SSH connection SSHHost uses. Raises TimeoutError on timeout."""

    def run(
        self, command: str, *, input: bytes | None = None, timeout: float | None = None
    ) -> tuple[bytes, bytes, int]: ...

    def put_file(self, local: Path | str, remote: str) -> None: ...

    def close(self) -> None: ...


class AsyncsshRunner:
    """One asyncssh connection, driven synchronously from its own event loop."""

    def __init__(
        self,
        destination: str,
        *,
        port: int = 22,
        key: str | None = None,
        verify_host_key: bool = True,
    ) -> None:
        user, _, host = destination.rpartition("@")
        self._loop = asyncio.new_event_loop()
        try:
            self._conn: Any = self._loop.run_until_complete(
                self._connect(host, user or None, key, port, verify_host_key)
            )
        except Exception:
            self._loop.close()
            raise

    @staticmethod
    async def _connect(
        host: str, user: str | None, key: str | None, port: int, verify: bool
    ) -> object:
        import asyncssh

        try:
            return await asyncssh.connect(
                host, **connect_kwargs(user, key, port, verify_host_key=verify, host=host)
            )
        except (OSError, asyncssh.Error) as e:
            raise HostError(f"could not connect to {host}: {e}") from e

    def run(
        self, command: str, *, input: bytes | None = None, timeout: float | None = None
    ) -> tuple[bytes, bytes, int]:
        import asyncssh

        async def go() -> tuple[bytes, bytes, int]:
            r = await self._conn.run(
                command, input=input, encoding=None, timeout=timeout, check=False
            )
            code = r.exit_status if r.exit_status is not None else -1
            return r.stdout or b"", r.stderr or b"", int(code)

        try:
            return self._loop.run_until_complete(go())
        except (TimeoutError, asyncssh.TimeoutError) as e:
            raise TimeoutError(command) from e

    def put_file(self, local: Path | str, remote: str) -> None:
        async def go() -> None:
            async with self._conn.start_sftp_client() as sftp:
                await sftp.put(str(local), remote)

        self._loop.run_until_complete(go())

    def close(self) -> None:
        if self._loop.is_closed():
            return
        self._conn.close()
        self._loop.run_until_complete(self._conn.wait_closed())
        self._loop.close()


def _ps(text: str) -> str:
    """A PowerShell single-quoted literal: nothing inside is expanded."""
    return "'" + text.replace("'", "''") + "'"


class SSHHost:
    def __init__(
        self,
        destination: str,
        *,
        become: bool = False,
        runner: CommandRunner | None = None,
        windows: bool | None = None,
    ) -> None:
        self.name = destination
        self.become = become
        self._runner = runner if runner is not None else AsyncsshRunner(destination)
        if windows is None:
            _, _, code = self._runner.run("uname -s")
            windows = code != 0
            logger.debug("%s is %s", destination, "Windows" if windows else "POSIX")
        self.windows = windows

    @property
    def pure(self) -> type[PurePath]:
        return PureWindowsPath if self.windows else PurePosixPath

    # -- sending commands ----------------------------------------------------------

    def _exec(
        self, command: str, *, input: bytes | None = None, timeout: float | None = None
    ) -> tuple[bytes, bytes, int]:
        logger.debug("%s: %s", self.name, command)
        try:
            return self._runner.run(command, input=input, timeout=timeout)
        except TimeoutError as e:
            raise HostError(f"timed out after {timeout} seconds") from e

    def _sh(self, script: str, *args: str, sudo: bool = True) -> str:
        line = shlex.join(["sh", "-c", script, "sh", *args])
        return f"sudo -n {line}" if sudo and self.become else line

    def _do(self, command: str, *, input: bytes | None = None) -> bytes:
        out, err, code = self._exec(command, input=input)
        if code != 0:
            raise HostError(err.decode(errors="replace").strip() or f"exit code {code}")
        return out

    def _posix(
        self, script: str, *args: str, input: bytes | None = None, sudo: bool = True
    ) -> bytes:
        return self._do(self._sh(script, *args, sudo=sudo), input=input)

    def _win(self, source: str, *, input: bytes | None = None) -> bytes:
        return self._do(powershell_command(source), input=input)

    def _test(self, flag: str, ps_type: str, path: str) -> bool:
        if self.windows:
            source = (
                f"if (Test-Path -LiteralPath {_ps(path)}{ps_type}) {{ exit 0 }} else {{ exit 1 }}"
            )
            return self._exec(powershell_command(source))[2] == 0
        return self._exec(self._sh(f'test {flag} "$1"', path))[2] == 0

    # -- Host ------------------------------------------------------------------------

    def temp_dir(self, prefix: str) -> str:
        if self.windows:
            out = self._win(
                "$p = Join-Path ([IO.Path]::GetTempPath()) "
                f"({_ps(prefix)} + [guid]::NewGuid().ToString('N').Substring(0, 8)); "
                "New-Item -ItemType Directory -Path $p | Out-Null; Write-Output $p"
            )
        else:
            # as the SSH user even with become, so SFTP can put files into it
            out = self._posix('mktemp -d "${TMPDIR:-/tmp}/$1XXXXXXXX"', prefix, sudo=False)
        return out.decode().strip()

    def mkdir(self, path: str) -> None:
        if self.windows:
            # .NET, not New-Item -Path: -Path treats [ and ] as wildcards
            self._win(f"[IO.Directory]::CreateDirectory({_ps(path)}) | Out-Null")
        else:
            self._posix('mkdir -p -- "$1"', path)

    def exists(self, path: str) -> bool:
        return self._test("-e", "", path)

    def is_file(self, path: str) -> bool:
        return self._test("-f", " -PathType Leaf", path)

    def is_dir(self, path: str) -> bool:
        return self._test("-d", " -PathType Container", path)

    def read_bytes(self, path: str) -> bytes:
        if self.windows:
            out = self._win(f"[Convert]::ToBase64String([IO.File]::ReadAllBytes({_ps(path)}))")
            return base64.b64decode(out.strip())
        return self._posix('cat -- "$1"', path)

    def write_bytes(self, path: str, data: bytes, *, append: bool = False) -> None:
        if self.windows:
            read = "$d = [Convert]::FromBase64String([Console]::In.ReadToEnd()); "
            if append:
                source = (
                    f"{read}$f = [IO.File]::Open({_ps(path)}, [IO.FileMode]::Append); "
                    "$f.Write($d, 0, $d.Length); $f.Close()"
                )
            else:
                source = f"{read}[IO.File]::WriteAllBytes({_ps(path)}, $d)"
            self._win(source, input=base64.b64encode(data))
        else:
            self._posix('cat >> "$1"' if append else 'cat > "$1"', path, input=data)

    def copy_file(self, src: str, dst: str) -> None:
        if self.windows:
            self._win(f"Copy-Item -LiteralPath {_ps(src)} -Destination {_ps(dst)}")
        else:
            self._posix('cp -- "$1" "$2"', src, dst)

    def move(self, src: str, dst: str) -> None:
        if self.windows:
            self._win(f"Move-Item -LiteralPath {_ps(src)} -Destination {_ps(dst)}")
        else:
            self._posix('mv -f -- "$1" "$2"', src, dst)

    def delete_file(self, path: str) -> None:
        if self.windows:
            self._win(f"Remove-Item -LiteralPath {_ps(path)} -Force -ErrorAction SilentlyContinue")
        else:
            self._posix('rm -f -- "$1"', path)

    def delete_tree(self, path: str) -> None:
        if self.windows:
            self._win(
                f"if (Test-Path -LiteralPath {_ps(path)}) "
                f"{{ Remove-Item -LiteralPath {_ps(path)} -Recurse -Force }}"
            )
        else:
            self._posix('rm -rf -- "$1"', path)

    def put_file(self, local: Path | str, dst: str) -> None:
        if self.windows:
            # Windows OpenSSH's SFTP server takes C:/x/y
            self._runner.put_file(local, dst.replace("\\", "/"))
        elif self.become:
            # SFTP writes as the SSH user; a root-owned destination needs sudo mv
            stage = self._posix('mktemp "${TMPDIR:-/tmp}/bfasr-put.XXXXXXXX"', sudo=False)
            stage_path = stage.decode().strip()
            self._runner.put_file(local, stage_path)
            self._posix('mv -f -- "$1" "$2"', stage_path, dst)
        else:
            self._runner.put_file(local, dst)

    def sha256(self, path: str) -> str:
        if self.windows:
            out = self._win(f"(Get-FileHash -Algorithm SHA256 -LiteralPath {_ps(path)}).Hash")
        else:
            out = self._posix(
                '{ sha256sum -- "$1" 2>/dev/null || shasum -a 256 -- "$1"; } | awk \'{print $1}\'',
                path,
            )
        digest = out.decode().strip().lower()
        if not re.fullmatch(r"[0-9a-f]+", digest):
            raise HostError(f"can't hash {path}")
        return digest

    def run(self, argv: list[str], *, cwd: str, wait: bool, timeout: float | None = None) -> int:
        if self.windows:
            command = powershell_command(self._windows_run(argv, cwd, wait))
        else:
            command = self._sh(self._posix_run(wait), cwd, *argv)
        _, err, code = self._exec(command, timeout=timeout)
        text = err.decode(errors="replace")
        if code == 127 and _MARK in text:
            raise HostError("can't run: " + text.split(_MARK, 1)[1].strip())
        return 0 if not wait else code

    @staticmethod
    def _posix_run(wait: bool) -> str:
        script = (
            f'cd -- "$1" 2>/dev/null || {{ echo "{_MARK} no such folder: $1" >&2; exit 127; }}; '
            "shift; "
            f'command -v -- "$1" >/dev/null 2>&1 || {{ echo "{_MARK} not found: $1" >&2; exit 127; }}; '
        )
        if wait:
            return script + 'exec "$@"'
        # detached, with nothing left holding the SSH channel open
        return script + 'nohup "$@" </dev/null >/dev/null 2>&1 & exit 0'

    @staticmethod
    def _windows_run(argv: list[str], cwd: str, wait: bool) -> str:
        # Start-Process hands -ArgumentList to CreateProcess as one command line,
        # which is how a client starts a program too
        start = f"Start-Process -FilePath {_ps(argv[0])}"
        if argv[1:]:
            start += f" -ArgumentList {_ps(subprocess.list2cmdline(argv[1:]))}"
        start += f" -WorkingDirectory {_ps(cwd)}"
        if wait:
            body = f"$p = {start} -NoNewWindow -Wait -PassThru; exit $p.ExitCode"
        else:
            body = f"{start} -WindowStyle Hidden; exit 0"
        return (
            f"try {{ {body} }} catch {{ "
            f"[Console]::Error.WriteLine('{_MARK} ' + $_.Exception.Message); exit 127 }}"
        )

    def close(self) -> None:
        self._runner.close()
