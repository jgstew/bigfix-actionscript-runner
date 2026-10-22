"""The Host contract: every file operation and command an action performs.

The same tests run against LocalHost (this machine) and against SSHHost
driving a POSIX shell, so the remote commands are proven to behave exactly
like the local operations.
"""

import os
import sys
import time
from pathlib import Path

import pytest

from bigfix_actionscript_runner.host import HostError, LocalHost


@pytest.fixture(params=["local", "ssh-posix"])
def host(request):
    if request.param == "local":
        return LocalHost()
    return request.getfixturevalue("shell_host")


def p(host, *parts):
    return str(host.pure(*(str(x) for x in parts)))


def test_temp_dir_is_new_and_named(host, tmp_path):
    a = host.temp_dir("bfasr-test-")
    b = host.temp_dir("bfasr-test-")
    try:
        assert a != b
        assert host.is_dir(a) and host.is_dir(b)
        assert host.pure(a).name.startswith("bfasr-test-")
    finally:
        host.delete_tree(a)
        host.delete_tree(b)
    assert not host.exists(a)


def test_mkdir_makes_parents_and_is_idempotent(host, tmp_path):
    d = p(host, tmp_path, "a b", "c'd", "e")
    host.mkdir(d)
    host.mkdir(d)
    assert host.is_dir(d)
    assert not host.is_file(d)


def test_write_append_read_bytes(host, tmp_path):
    f = p(host, tmp_path, "it's here.bin")
    data = bytes(range(256))
    host.write_bytes(f, data)
    host.write_bytes(f, b"\r\nmore", append=True)
    assert host.read_bytes(f) == data + b"\r\nmore"
    assert host.is_file(f) and host.exists(f) and not host.is_dir(f)


def test_append_creates_the_file(host, tmp_path):
    f = p(host, tmp_path, "new.txt")
    host.write_bytes(f, b"x\n", append=True)
    assert host.read_bytes(f) == b"x\n"


def test_copy_move_delete(host, tmp_path):
    a, b, c = (p(host, tmp_path, n) for n in ("a.txt", "b $HOME.txt", "c.txt"))
    host.write_bytes(a, b"A")
    host.copy_file(a, b)
    host.move(b, c)
    assert host.read_bytes(a) == b"A" and host.read_bytes(c) == b"A"
    assert not host.exists(b)
    host.delete_file(c)
    host.delete_file(c)  # deleting what isn't there is not an error, like on a client
    assert not host.exists(c)


def test_delete_tree(host, tmp_path):
    d = p(host, tmp_path, "tree")
    host.mkdir(p(host, d, "x", "y"))
    host.write_bytes(p(host, d, "x", "y", "f"), b"1")
    host.delete_tree(d)
    assert not host.exists(d)
    host.delete_tree(d)


def test_put_file_and_sha256(host, tmp_path):
    local = tmp_path / "local.bin"
    local.write_bytes(b"hello")
    dst = p(host, tmp_path, "remote dir", "copy.bin")
    host.mkdir(p(host, tmp_path, "remote dir"))
    host.put_file(local, dst)
    assert host.read_bytes(dst) == b"hello"
    assert host.sha256(dst) == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"


def test_read_of_a_missing_file_raises(host, tmp_path):
    with pytest.raises(HostError):
        host.read_bytes(p(host, tmp_path, "missing"))


@pytest.mark.skipif(os.name == "nt", reason="uses /bin/sh")
def test_run_waits_returns_the_exit_code_in_cwd(host, tmp_path):
    code = host.run(["/bin/sh", "-c", "pwd > out.txt; exit 3"], cwd=str(tmp_path), wait=True)
    assert code == 3
    ran_in = host.read_bytes(p(host, tmp_path, "out.txt")).decode().strip()
    assert Path(ran_in).resolve() == tmp_path.resolve()


@pytest.mark.skipif(os.name == "nt", reason="uses /bin/sh")
def test_run_keeps_arguments_with_spaces_and_quotes(host, tmp_path):
    arg = 'it\'s a "quoted" $HOME arg'
    host.run(["/bin/sh", "-c", 'printf %s "$1" > arg.txt', "sh", arg], cwd=str(tmp_path), wait=True)
    assert host.read_bytes(p(host, tmp_path, "arg.txt")).decode() == arg


@pytest.mark.skipif(os.name == "nt", reason="uses /bin/sh")
def test_run_without_wait_returns_at_once(host, tmp_path):
    start = time.monotonic()
    code = host.run(
        ["/bin/sh", "-c", "sleep 0.3; echo done > late.txt"], cwd=str(tmp_path), wait=False
    )
    assert code == 0
    assert time.monotonic() - start < 0.3
    deadline = time.monotonic() + 10
    while not host.exists(p(host, tmp_path, "late.txt")) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert host.exists(p(host, tmp_path, "late.txt"))


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX timeout")
def test_run_timeout_raises(host, tmp_path):
    with pytest.raises(HostError, match="timed out"):
        host.run(["/bin/sh", "-c", "sleep 5"], cwd=str(tmp_path), wait=True, timeout=0.5)


def test_missing_program_raises(host, tmp_path):
    with pytest.raises(HostError):
        host.run(["/no/such/program-bfasr"], cwd=str(tmp_path), wait=True)


# --- LocalHost failures surface as HostError ----------------------------------------


def test_local_failures_are_host_errors(tmp_path):
    host = LocalHost()
    f = tmp_path / "file"
    f.write_text("x")
    for op in (
        lambda: host.mkdir(str(f / "sub")),
        lambda: host.write_bytes(str(tmp_path / "no" / "dir" / "x"), b""),
        lambda: host.copy_file(str(tmp_path / "missing"), str(tmp_path / "y")),
        lambda: host.move(str(tmp_path / "missing"), str(tmp_path / "y")),
        lambda: host.sha256(str(tmp_path / "missing")),
        lambda: host.delete_file(str(tmp_path)),  # a folder, not a file
    ):
        with pytest.raises(HostError):
            op()
