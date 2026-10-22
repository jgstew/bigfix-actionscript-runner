"""The machine an action runs on: every file operation and command goes through a Host.

Paths are strings in the host's own flavor (`pure` is PurePosixPath or
PureWindowsPath), because the host may be another OS than this one.
LocalHost is this machine; SSHHost (ssh_host.py) is a target over SSH.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Protocol


class HostError(Exception):
    """A file operation or command could not be carried out on the host."""


class Host(Protocol):
    name: str
    windows: bool

    @property
    def pure(self) -> type[PurePath]: ...

    def temp_dir(self, prefix: str) -> str: ...
    def mkdir(self, path: str) -> None: ...
    def exists(self, path: str) -> bool: ...
    def is_file(self, path: str) -> bool: ...
    def is_dir(self, path: str) -> bool: ...
    def read_bytes(self, path: str) -> bytes: ...
    def write_bytes(self, path: str, data: bytes, *, append: bool = False) -> None: ...
    def copy_file(self, src: str, dst: str) -> None: ...
    def move(self, src: str, dst: str) -> None: ...
    def delete_file(self, path: str) -> None: ...
    def delete_tree(self, path: str) -> None: ...
    def put_file(self, local: Path | str, dst: str) -> None: ...
    def sha256(self, path: str) -> str: ...
    def run(self, argv: list[str], *, cwd: str, wait: bool, timeout: float | None = None) -> int:
        """Run argv[0] in cwd: its exit code if `wait`, else 0 once started.

        On a Windows host argv[1:] are command-line tokens, passed as written.
        """
        ...

    def close(self) -> None: ...


class LocalHost:
    """This machine."""

    name = "local"

    def __init__(self) -> None:
        self.windows = os.name == "nt"

    @property
    def pure(self) -> type[PurePath]:
        return PureWindowsPath if self.windows else PurePosixPath

    def temp_dir(self, prefix: str) -> str:
        return tempfile.mkdtemp(prefix=prefix)

    def mkdir(self, path: str) -> None:
        try:
            Path(path).mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise HostError(str(e)) from e

    def exists(self, path: str) -> bool:
        return Path(path).exists()

    def is_file(self, path: str) -> bool:
        return Path(path).is_file()

    def is_dir(self, path: str) -> bool:
        return Path(path).is_dir()

    def read_bytes(self, path: str) -> bytes:
        try:
            return Path(path).read_bytes()
        except OSError as e:
            raise HostError(str(e)) from e

    def write_bytes(self, path: str, data: bytes, *, append: bool = False) -> None:
        try:
            with open(path, "ab" if append else "wb") as f:
                f.write(data)
        except OSError as e:
            raise HostError(str(e)) from e

    def copy_file(self, src: str, dst: str) -> None:
        try:
            shutil.copyfile(src, dst)
        except OSError as e:
            raise HostError(str(e)) from e

    def move(self, src: str, dst: str) -> None:
        try:
            shutil.move(src, dst)
        except OSError as e:
            raise HostError(str(e)) from e

    def delete_file(self, path: str) -> None:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError as e:
            raise HostError(str(e)) from e

    def delete_tree(self, path: str) -> None:
        shutil.rmtree(path, ignore_errors=True)

    def put_file(self, local: Path | str, dst: str) -> None:
        self.copy_file(str(local), dst)

    def sha256(self, path: str) -> str:
        h = hashlib.sha256()
        try:
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
        except OSError as e:
            raise HostError(str(e)) from e
        return h.hexdigest()

    def run(self, argv: list[str], *, cwd: str, wait: bool, timeout: float | None = None) -> int:
        command: list[str] | str = argv
        if self.windows:
            # argv[1:] are command-line tokens as written (see executor._argv)
            command = " ".join([subprocess.list2cmdline(argv[:1]), *argv[1:]])
        try:
            if not wait:
                subprocess.Popen(command, cwd=cwd, start_new_session=True)
                return 0
            return subprocess.run(command, cwd=cwd, timeout=timeout, check=False).returncode
        except subprocess.TimeoutExpired as e:
            raise HostError(f"timed out after {timeout} seconds") from e
        except OSError as e:
            raise HostError(f"can't run: {e}") from e

    def close(self) -> None:
        pass
