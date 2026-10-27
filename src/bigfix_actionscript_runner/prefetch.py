"""Prefetch and download statements: parse, download into __Download, verify.

Supported forms:
    prefetch NAME sha1:X size:N URL [sha256:Y]
    add prefetch item name=N sha1=X size=N url=U sha256=Y   (any order)
    download [now] [as NAME] URL

`prefetch` statements and prefetch items are parsed, validated, downloaded
and compared with bigfix-prefetch. A `download` with no hashes is fetched
with TLS verification instead: bigfix-prefetch turns certificate checking
off, which is safe only when hashes are checked afterwards. Verified
downloads are cached by sha256 so repeat runs don't download them again
(`cached_copy` hands out the cached file itself).
"""

import contextlib
import hashlib
import io
import re
import shutil
import tempfile
import warnings
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from bigfix_prefetch import prefetch_parse, prefetch_validate, prefetches_have_matching_hashes


class PrefetchError(Exception):
    pass


@dataclass(frozen=True)
class PrefetchItem:
    name: str
    url: str
    size: int | None = None
    sha1: str | None = None
    sha256: str | None = None
    now: bool = False

    @property
    def hashed(self) -> bool:
        return bool(self.sha1 or self.sha256)

    def as_bigfix_prefetch(self) -> dict[str, Any]:
        d: dict[str, Any] = {"file_name": self.name, "download_url": self.url}
        if self.size is not None:
            d["file_size"] = self.size
        if self.sha1:
            d["file_sha1"] = self.sha1
        if self.sha256:
            d["file_sha256"] = self.sha256
        return d


Downloader = Callable[[str, Path], None]


class _Auto:
    """Pick the downloader per item: bigfix-prefetch when hashed, else TLS-verified."""

    def __repr__(self) -> str:
        return "AUTO"


AUTO: Any = _Auto()

_PREFETCH_START = re.compile(r"^(prefetch\s|add\s+prefetch\s+item\s)", re.IGNORECASE)
_DOWNLOAD = re.compile(
    r"^download(?P<now>\s+now)?(?:\s+as\s+(?P<name>\S+))?\s+(?P<url>\S+)\s*$", re.IGNORECASE
)


def _url_name(url: str) -> str:
    return Path(urlsplit(url).path).name or "download"


@contextlib.contextmanager
def _quiet() -> Iterator[list[warnings.WarningMessage]]:
    """bigfix-prefetch prints and warns as it goes; keep that out of our output."""
    with warnings.catch_warnings(record=True) as caught, contextlib.redirect_stdout(io.StringIO()):
        warnings.simplefilter("always")
        yield caught


def check_name(name: str) -> str:
    """`name` if it is a plain file name, else PrefetchError.

    The name comes from the .bes file and is joined onto __Download (and a
    local staging folder), so a separator, `..`, a drive or an absolute path
    would put the file somewhere else entirely, even in a dry run.
    """
    if not name or name in (".", "..") or any(c in name for c in "/\\:\0") or name != name.strip():
        raise PrefetchError(f"not a plain file name: {name!r}")
    return name


def parse_prefetch(line: str) -> PrefetchItem | None:
    """A PrefetchItem for a prefetch/download line, None for any other line."""
    item = _parse(line)
    if item is not None:
        check_name(item.name)
    return item


def _parse(line: str) -> PrefetchItem | None:
    line = line.strip()
    if _PREFETCH_START.match(line):
        # bigfix-prefetch expects the block form with a leading space before each key
        text = re.sub(r"^add\s+prefetch\s+item\s+", "add prefetch item ", line, flags=re.IGNORECASE)
        with _quiet() as caught:
            valid = prefetch_validate.validate_prefetch(text)
            parsed = prefetch_parse.parse_prefetch(text) if valid else None
        if not parsed:
            reasons = "; ".join(str(w.message).strip() for w in caught) or "could not be parsed"
            raise PrefetchError(f"invalid prefetch: {reasons}: {line}")
        return PrefetchItem(
            parsed["file_name"],
            parsed["download_url"],
            int(parsed["file_size"]) if parsed.get("file_size") is not None else None,
            (parsed.get("file_sha1") or "").lower() or None,
            (parsed.get("file_sha256") or "").lower() or None,
        )
    m = _DOWNLOAD.match(line)
    if m:
        return PrefetchItem(m["name"] or _url_name(m["url"]), m["url"], now=bool(m["now"]))
    return None


def bigfix_prefetch_download(url: str, dest: Path) -> None:
    from bigfix_prefetch import prefetch_from_url

    with _quiet():
        prefetch_from_url.url_to_prefetch(url, True, str(dest))


def tls_download(url: str, dest: Path) -> None:
    import requests

    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            f.writelines(r.iter_content(1 << 16))


def _actual(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    return {
        "file_size": len(data),
        "file_sha1": hashlib.sha1(data, usedforsecurity=False).hexdigest(),
        "file_sha256": hashlib.sha256(data).hexdigest(),
    }


def _verify(item: PrefetchItem, path: Path) -> None:
    if not item.hashed and item.size is None:
        return
    expected, actual = item.as_bigfix_prefetch(), _actual(path)
    if item.size is None:  # bigfix-prefetch requires a size to compare
        expected["file_size"] = actual["file_size"]
    with _quiet() as caught:
        ok = prefetches_have_matching_hashes.prefetches_have_matching_hashes(expected, actual)
    if not ok:
        why = "; ".join(str(w.message).replace("ERROR: ", "").strip() for w in caught)
        got = ", ".join(f"{k} {actual[k]}" for k in ("file_size", "file_sha1", "file_sha256"))
        raise PrefetchError(f"{item.name}: {why or 'does not match'} (downloaded: {got})")


def fetch(
    item: PrefetchItem, dest_dir: Path, cache_dir: Path, downloader: Downloader | None = AUTO
) -> Path:
    """Put `item` at dest_dir/name. `downloader=None` writes a placeholder instead."""
    check_name(item.name)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / item.name
    if downloader is None:
        dest.write_text(f"placeholder (not downloaded): {item.url}\n")
        return dest
    cached = cached_copy(item, cache_dir, downloader)
    if cached is not None:
        shutil.copyfile(cached, dest)
        return dest
    with tempfile.TemporaryDirectory(dir=dest_dir) as tmp:
        shutil.move(_download(item, Path(tmp), downloader), dest)
    return dest


def cached_copy(item: PrefetchItem, cache_dir: Path, downloader: Downloader = AUTO) -> Path | None:
    """The verified file in the cache for an item with a sha256, downloaded
    into the cache first if it isn't there; None for an item without one."""
    if not item.sha256:
        return None
    check_name(item.name)
    cached = cache_dir / item.sha256
    if cached.exists():
        try:
            _verify(item, cached)
        except PrefetchError:
            cached.unlink()  # damaged or tampered with: download it again
        else:
            return cached
    cache_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=cache_dir) as tmp:
        shutil.move(_download(item, Path(tmp), downloader), cached)
    return cached


def _download(item: PrefetchItem, tmp_dir: Path, downloader: Downloader) -> Path:
    """Download `item` into tmp_dir and verify it; returns the file."""
    if downloader is AUTO:
        downloader = bigfix_prefetch_download if item.hashed else tls_download
    part = tmp_dir / item.name
    downloader(item.url, part)
    _verify(item, part)
    return part
