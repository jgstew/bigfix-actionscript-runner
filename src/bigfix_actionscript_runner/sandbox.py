"""Where the paths an action names end up, on the host the action runs on.

Modes:

    full    a dry run: the client folders AND every other path are in the
            sandbox, so nothing outside it changes
    client  --execute: only the client folders are in the sandbox; every
            other path is the real one, so the action's real changes can be
            measured
    off     --execute --no-sandbox: the real client's folders; everything is real

Layout under the root (full and client):

    site/                 the current site's client folder (relative paths, __createfile)
    site/__Download/      prefetched / downloaded files
    __BESData/            "data folder of client"
    __BESData/__Global/Logs/
    fs/                   full mode only: any other absolute path,
                          /tmp/x -> fs/tmp/x, C:\\x -> fs/C/x

Paths are strings in the host's flavor (the host may be another OS).
"""

from __future__ import annotations

import ntpath
import posixpath
import re
from pathlib import Path, PurePath

from .host import Host, LocalHost

MODES = ("full", "client", "off")


class SandboxEscape(ValueError):
    """A path would land outside where the sandbox allows (a bug or hostile input)."""


_DRIVE = re.compile(r"^([A-Za-z]):[\\/]?(.*)$")


class Sandbox:
    def __init__(
        self,
        root: str | PurePath | None = None,
        *,
        host: Host | None = None,
        mode: str = "full",
        site_folder: str | None = None,
        data_folder: str | None = None,
    ) -> None:
        if mode not in MODES:
            raise ValueError(f"unknown sandbox mode {mode!r}: expected one of {', '.join(MODES)}")
        self.host: Host = host if host is not None else LocalHost()
        self.mode = mode
        self._pure = self.host.pure
        self._ospath = ntpath if self.host.windows else posixpath
        self.fs: str | None = None
        self.root: str | None = None
        if mode == "off":
            if not site_folder or not data_folder:
                raise ValueError("mode 'off' needs site_folder and data_folder (the real ones)")
            self.site, self.data = site_folder, data_folder
            self.protected = {self.site, self.data}
            return
        if root is None:
            raise ValueError(f"mode {mode!r} needs a sandbox root folder")
        if isinstance(self.host, LocalHost):
            root = Path(root).resolve()
        self.root = str(self._pure(str(root)))
        self.site = self._join(self.root, "site")
        self.data = self._join(self.root, "__BESData")
        folders = [self._join(self.site, "__Download"), self._join(self.data, "__Global", "Logs")]
        if mode == "full":
            self.fs = self._join(self.root, "fs")
            folders.append(self.fs)
        for folder in folders:
            self.host.mkdir(folder)
        self.protected = {p for p in (self.root, self.site, self.data, self.fs) if p}

    # -- paths ----------------------------------------------------------------------

    def _join(self, base: str, *parts: str) -> str:
        return str(self._pure(base).joinpath(*parts))

    def _parts(self, text: str) -> list[str]:
        """Path components (either separator), with .. resolved but never above the start.

        On Windows a component like `C:` would make joinpath switch drives
        (`D:\\sb\\site` + `C:` + `Windows` is `C:Windows`), so `:` is replaced:
        it can't be in a Windows file name anyway.
        """
        out: list[str] = []
        for part in re.split(r"[\\/]+", text):
            if part in ("", "."):
                continue
            if part == "..":
                if out:
                    out.pop()
                continue
            out.append(part.replace(":", "_") if self.host.windows else part)
        return out

    @staticmethod
    def _strip_device_prefix(path: str) -> str:
        """`\\\\?\\C:\\x` and `\\\\.\\C:\\x` name `C:\\x`; `\\\\?\\UNC\\s\\x` names `\\\\s\\x`."""
        m = re.match(r"^[\\/]{2}[?.][\\/](UNC[\\/])?", path, re.IGNORECASE)
        if not m:
            return path
        rest = path[m.end() :]
        return "\\\\" + rest if m.group(1) else rest

    def _norm(self, path: str) -> str:
        if not self.host.windows:
            path = path.replace("\\", "/")
        return str(self._ospath.normpath(path))

    def _absolute(self, path: str) -> str | None:
        """`path` normalized, if it is absolute on the host; None if relative."""
        if self.host.windows:
            path = self._strip_device_prefix(path)
            if re.match(r"^[\\/](?![\\/])", path):
                # rooted: the root of the current drive, the site folder's
                path = self._pure(self.site).drive + path
            return self._norm(path) if _DRIVE.match(path) or path.startswith("\\\\") else None
        p = path.replace("\\", "/")
        return self._norm(p) if p.startswith("/") else None

    def contains(self, path: str) -> bool:
        if self.root is None:
            return False
        norm, root = self._norm(path), self.root
        if self.host.windows:
            norm, root = norm.lower(), root.lower()
        return norm == root or norm.startswith(root.rstrip(self._ospath.sep) + self._ospath.sep)

    def resolve(self, path: str) -> str:
        """Where `path` is, for writing."""
        real = self._absolute(path)
        if real is not None:
            if self.mode != "full" or self.contains(real):
                return real
            return self._inside(self._fs(), self._fs_parts(real))
        if _DRIVE.match(path) and self.mode == "full":  # a Windows path on a POSIX target
            return self._inside(self._fs(), self._fs_parts(path))
        return self._inside(self.site, self._parts(path))

    def _inside(self, base: str, parts: list[str]) -> str:
        """base/parts, refusing anything that isn't under base (defence in depth)."""
        joined = self._join(base, *parts)
        norm, root = self._norm(joined), self._norm(base)
        if self.host.windows:
            norm, root = norm.lower(), root.lower()
        if norm != root and not norm.startswith(root.rstrip(self._ospath.sep) + self._ospath.sep):
            raise SandboxEscape(f"{joined} is outside {base}")
        return joined

    def is_protected(self, path: str) -> bool:
        """The sandbox's own folders and the client folders: never deleted."""
        norm = self._norm(path)
        protected = {self._norm(p) for p in self.protected}
        if self.host.windows:
            return norm.lower() in {p.lower() for p in protected}
        return norm in protected

    def _fs(self) -> str:
        assert self.fs is not None  # full mode always has one
        return self.fs

    def _fs_parts(self, path: str) -> list[str]:
        path = self._strip_device_prefix(path)
        drive = _DRIVE.match(path)
        if drive:
            return [drive.group(1).upper(), *self._parts(drive.group(2))]
        return self._parts(path)

    def resolve_read(self, path: str) -> str:
        """Where a read of `path` comes from.

        In a dry run: the sandbox copy if there is one, else the real file
        (reading is harmless and lets an action copy real files in).
        """
        mapped = self.resolve(path)
        if self.mode != "full" or self.host.exists(mapped):
            return mapped
        real = self._absolute(path)
        if real is not None and not self.contains(real) and self.host.exists(real):
            return real
        return mapped

    def parent(self, path: str) -> str:
        return str(self._pure(path).parent)

    def display(self, path: str) -> str:
        if self.root is not None and self.contains(path):
            rel = self._pure(self._norm(path)).relative_to(self._pure(self.root))
            return "/".join(("<sandbox>", *rel.parts))
        return path
