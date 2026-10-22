"""The per-run sandbox every file command writes into.

Layout under the root:

    site/                 the current site's client folder (relative paths, __createfile)
    site/__Download/      prefetched / downloaded files
    __BESData/            "data folder of client"
    __BESData/__Global/Logs/
    fs/                   any other absolute path: /tmp/x -> fs/tmp/x, C:\\x -> fs/C/x

Writes always land inside the root. Reads of an absolute path fall back to
the real file when the sandbox has no copy yet (reading is harmless and lets
an action copy real files in).
"""

import os
import re
from pathlib import Path, PurePosixPath

_DRIVE = re.compile(r"^([A-Za-z]):[\\/]?(.*)$")


class Sandbox:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.site = self.root / "site"
        self.data = self.root / "__BESData"
        self.fs = self.root / "fs"
        for d in (self.site / "__Download", self.data / "__Global" / "Logs", self.fs):
            d.mkdir(parents=True, exist_ok=True)

    def contains(self, path: Path) -> bool:
        try:
            Path(os.path.normpath(path)).relative_to(self.root)
            return True
        except ValueError:
            return False

    @staticmethod
    def _parts(text: str) -> list[str]:
        """Path components, with .. resolved but never above the start."""
        out: list[str] = []
        for part in PurePosixPath(text.replace("\\", "/")).parts:
            if part in ("/", "", "."):
                continue
            if part == "..":
                if out:
                    out.pop()
                continue
            out.append(part)
        return out

    def _real(self, path: str) -> Path | None:
        """`path` as an absolute path on this machine, or None if it is relative."""
        p = path.replace("\\", "/") if os.sep == "/" else path
        if os.path.isabs(p):
            return Path(os.path.normpath(p))
        return None

    def resolve(self, path: str) -> Path:
        """Where a write to `path` goes (always inside the sandbox)."""
        m = _DRIVE.match(path)
        if m:
            return self.fs.joinpath(m.group(1).upper(), *self._parts(m.group(2)))
        real = self._real(path)
        if real is not None:
            if self.contains(real):
                return real
            return self.fs.joinpath(*self._parts(str(real)))
        return self.site.joinpath(*self._parts(path))

    def resolve_read(self, path: str) -> Path:
        """Where a read of `path` comes from: the sandbox copy, else the real file."""
        mapped = self.resolve(path)
        if mapped.exists():
            return mapped
        real = self._real(path)
        if real is not None and not self.contains(real) and real.exists():
            return real
        return mapped

    def display(self, path: Path) -> str:
        try:
            return "<sandbox>/" + Path(path).relative_to(self.root).as_posix()
        except ValueError:
            return str(path)
