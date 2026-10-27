"""Prefetch / download statements: parse, download into __Download, verify."""

import hashlib

import pytest

from bigfix_actionscript_runner.prefetch import (
    AUTO,
    PrefetchError,
    PrefetchItem,
    cached_copy,
    fetch,
    parse_prefetch,
)

DATA = b"hello yara\n"
SHA1 = hashlib.sha1(DATA).hexdigest()
SHA256 = hashlib.sha256(DATA).hexdigest()
URL = "https://example.com/dl/yara-x-v1.22.0-aarch64-apple-darwin.tar.gz"


def test_prefetch_statement():
    item = parse_prefetch(
        f"prefetch speculationcontrol.nupkg sha1:{SHA1} size:11 {URL} sha256:{SHA256}"
    )
    assert item == PrefetchItem("speculationcontrol.nupkg", URL, 11, SHA1, SHA256)


def test_prefetch_statement_without_sha256():
    item = parse_prefetch(f"prefetch a.exe sha1:{SHA1} size:11 {URL}")
    assert (item.name, item.sha256) == ("a.exe", None)


def test_add_prefetch_item_any_order():
    item = parse_prefetch(
        f"add prefetch item url={URL} sha256={SHA256} size=11 sha1={SHA1} name=x.tar.gz"
    )
    assert item == PrefetchItem("x.tar.gz", URL, 11, SHA1, SHA256)


def test_download_forms():
    assert parse_prefetch(f"download {URL}").name == "yara-x-v1.22.0-aarch64-apple-darwin.tar.gz"
    assert parse_prefetch(f"download as y.tgz {URL}").name == "y.tgz"
    assert parse_prefetch(f"download now {URL}").now
    item = parse_prefetch(f"download now as z.tgz {URL}")
    assert (item.name, item.now, item.size) == ("z.tgz", True, None)


def test_url_query_string_is_not_part_of_the_name():
    assert parse_prefetch("download https://x.com/a/file.zip?sig=1").name == "file.zip"


def test_other_lines_are_not_prefetch():
    assert parse_prefetch("copy a b") is None
    assert parse_prefetch("begin prefetch block") is None


def fake_downloader(data=DATA, calls=None):
    def download(url, dest):
        if calls is not None:
            calls.append(url)
        dest.write_bytes(data)

    return download


def test_fetch_downloads_and_verifies(tmp_path):
    item = PrefetchItem("a.tgz", URL, 11, SHA1, SHA256)
    out = fetch(item, tmp_path / "dl", tmp_path / "cache", fake_downloader())
    assert out == tmp_path / "dl" / "a.tgz"
    assert out.read_bytes() == DATA


def test_fetch_uses_the_cache_the_second_time(tmp_path):
    item = PrefetchItem("a.tgz", URL, 11, SHA1, SHA256)
    calls = []
    fetch(item, tmp_path / "dl1", tmp_path / "cache", fake_downloader(calls=calls))
    fetch(item, tmp_path / "dl2", tmp_path / "cache", fake_downloader(calls=calls))
    assert calls == [URL]
    assert (tmp_path / "dl2" / "a.tgz").read_bytes() == DATA


@pytest.mark.parametrize(
    "field,value,message",
    [
        ("size", 99, "file_size does not match"),
        ("sha1", "0" * 40, "SHA1 does not match"),
        ("sha256", "f" * 64, "file_sha256 does not match"),
    ],
)
def test_fetch_rejects_mismatches(tmp_path, field, value, message):
    kw = {"name": "a.tgz", "url": URL, "size": 11, "sha1": SHA1, "sha256": SHA256}
    kw[field] = value
    with pytest.raises(PrefetchError, match=message):
        fetch(PrefetchItem(**kw), tmp_path / "dl", tmp_path / "cache", fake_downloader())
    assert not (tmp_path / "dl" / "a.tgz").exists()


def test_cached_copy_is_the_cache_file_itself(tmp_path):
    item = PrefetchItem("a.tgz", URL, 11, SHA1, SHA256)
    calls = []
    first = cached_copy(item, tmp_path / "cache", fake_downloader(calls=calls))
    again = cached_copy(item, tmp_path / "cache", fake_downloader(calls=calls))
    assert first == again == tmp_path / "cache" / SHA256
    assert first.read_bytes() == DATA
    assert calls == [URL]
    assert [p.name for p in (tmp_path / "cache").iterdir()] == [SHA256]


def test_cached_copy_is_none_without_a_sha256(tmp_path):
    item = PrefetchItem("a.tgz", URL, 11, SHA1)
    assert cached_copy(item, tmp_path / "cache", fake_downloader()) is None


def test_cached_copy_rejects_a_mismatch_and_caches_nothing(tmp_path):
    item = PrefetchItem("a.tgz", URL, 99, SHA1, SHA256)
    with pytest.raises(PrefetchError, match="file_size does not match"):
        cached_copy(item, tmp_path / "cache", fake_downloader())
    assert list((tmp_path / "cache").iterdir()) == []


def test_unhashed_download_is_not_cached(tmp_path):
    item = PrefetchItem("a.tgz", URL)
    calls = []
    fetch(item, tmp_path / "dl1", tmp_path / "cache", fake_downloader(calls=calls))
    fetch(item, tmp_path / "dl2", tmp_path / "cache", fake_downloader(calls=calls))
    assert calls == [URL, URL]


def test_placeholder_instead_of_download(tmp_path):
    item = PrefetchItem("a.tgz", URL, 11, SHA1, SHA256)
    out = fetch(item, tmp_path / "dl", tmp_path / "cache", None)
    assert out.exists()
    assert URL in out.read_text()


def test_invalid_prefetch_statement_is_an_error():
    with pytest.raises(PrefetchError, match="invalid prefetch"):
        parse_prefetch(f"prefetch a.zip sha1:abc size:11 {URL} sha256:{SHA256}")  # short sha1


def test_hashed_items_are_downloaded_by_bigfix_prefetch(tmp_path, monkeypatch):
    calls = []

    def url_to_prefetch(url, bool_return_dictionary=False, file_save_path=None):
        calls.append((url, bool_return_dictionary, file_save_path))
        with open(file_save_path, "wb") as f:
            f.write(DATA)
        return {"file_size": len(DATA), "file_sha1": SHA1, "file_sha256": SHA256}

    monkeypatch.setattr("bigfix_prefetch.prefetch_from_url.url_to_prefetch", url_to_prefetch)
    item = PrefetchItem("a.tgz", URL, 11, SHA1, SHA256)
    out = fetch(item, tmp_path / "dl", tmp_path / "cache", AUTO)
    assert out.read_bytes() == DATA
    assert calls and calls[0][0] == URL and calls[0][1] is True


def test_unhashed_downloads_use_a_tls_verified_download(tmp_path, monkeypatch):
    used = []
    monkeypatch.setattr(
        "bigfix_actionscript_runner.prefetch.tls_download",
        lambda url, dest: used.append(url) or dest.write_bytes(DATA),
    )
    monkeypatch.setattr(
        "bigfix_prefetch.prefetch_from_url.url_to_prefetch",
        lambda *a, **k: pytest.fail("bigfix-prefetch turns off TLS verification"),
    )
    fetch(PrefetchItem("a.tgz", URL), tmp_path / "dl", tmp_path / "cache", AUTO)
    assert used == [URL]


def test_size_only_items_are_checked(tmp_path):
    item = PrefetchItem("a.bin", URL, size=len(DATA) + 1)
    with pytest.raises(PrefetchError, match="a.bin"):
        fetch(item, tmp_path / "dl", tmp_path / "cache", fake_downloader())


def test_unhashed_items_without_a_size_are_not_checked(tmp_path):
    path = fetch(PrefetchItem("a.bin", URL), tmp_path / "dl", tmp_path / "cache", fake_downloader())
    assert path.read_bytes() == DATA


def test_tls_download_streams_to_the_file(tmp_path, monkeypatch):
    import requests

    from bigfix_actionscript_runner.prefetch import tls_download

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def raise_for_status(self):
            pass

        def iter_content(self, size):
            return [DATA[:3], DATA[3:]]

    seen = {}

    def get(url, **kw):
        seen.update(kw, url=url)
        return Response()

    monkeypatch.setattr(requests, "get", get)
    tls_download(URL, tmp_path / "out")
    assert (tmp_path / "out").read_bytes() == DATA
    assert seen["stream"] is True and "verify" not in seen  # certificates are checked
