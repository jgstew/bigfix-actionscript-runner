"""End to end with real qna (and optionally the network). Opt in, see conftest.py."""

import json
import os
import sys

import pytest
from typer.testing import CliRunner

from bigfix_actionscript_runner import cli

runner = CliRunner()


def run(tmp_path, script, *args):
    p = tmp_path / "a.bas"
    p.write_text(script)
    r = runner.invoke(cli.app, [str(p), "--json", "--sandbox", str(tmp_path / "sb"), *args])
    return r, json.loads(r.output)


@pytest.mark.live_qna
def test_real_relevance_and_sandboxed_client_folders(tmp_path):
    r, data = run(
        tmp_path,
        'parameter "logs" = "{pathname of folders "Logs" of folders "__Global" of data folders of client}"\n'
        "createfile until END\n"
        "os={name of operating system}\n"
        "END\n"
        'copy __createfile "{parameter "logs"}/os.txt"\n'
        'continue if {exists file "os.txt" of folder (parameter "logs")}\n',
    )
    assert r.exit_code == 0, r.output
    logs = tmp_path / "sb" / "__BESData" / "__Global" / "Logs"
    assert (logs / "os.txt").read_text().startswith("os=")
    assert data["status"] == "completed"


@pytest.mark.live_qna
@pytest.mark.skipif(os.name == "nt", reason="uses /bin/sh")
def test_execute_changes_real_files_and_uses_the_real_exit_code(tmp_path):
    """--execute: the script lands at the real path, runs, and its exit code is real."""
    job = tmp_path / "real" / "exit7.sh"
    script = (
        "createfile until END\nexit 7\nEND\n"
        f'folder create "{job.parent}"\n'
        f'copy __createfile "{job}"\n'
        f'wait /bin/sh "{job}"\n'
        "continue if {exit code of action = 7}\n"
        "continue if {exit code of action = 0}\n"
    )
    _, data = run(tmp_path, script, "--execute")
    assert data["status"] == "failed"
    assert data["failed_line"] == 8
    assert data["sandbox_mode"] == "client"
    assert job.read_text() == "exit 7\n"  # the real file, not a sandbox copy


@pytest.mark.live_qna
def test_dry_run_leaves_real_paths_alone(tmp_path):
    target = tmp_path / "untouched.txt"
    r, _ = run(tmp_path, f'createfile until E\nx\nE\ncopy __createfile "{target}"\n')
    assert r.exit_code == 0, r.output
    assert not target.exists()


@pytest.mark.live_qna
@pytest.mark.network
@pytest.mark.skipif(sys.platform != "darwin", reason="prefetches the macOS yr archive")
def test_real_prefetch_is_verified(tmp_path):
    url = "https://github.com/VirusTotal/yara-x/releases/download/v1.22.0/yara-x-v1.22.0-x86_64-apple-darwin.tar.gz"
    r, data = run(
        tmp_path,
        "begin prefetch block\n"
        f"add prefetch item name=yr.tgz sha1=560ace0fca8543195a71d52545bef248e92854e4 size=9476144 url={url} "
        "sha256=d059da4b91c7d7b43f6f50174a7739e3362b51f349f2ba8b33d46e3334bb3300\n"
        "end prefetch block\n"
        'continue if {size of file "yr.tgz" of folder "__Download" of client folder of current site = 9476144}\n',
    )
    assert r.exit_code == 0, r.output
    assert data["prefetched"] == ["yr.tgz"]
