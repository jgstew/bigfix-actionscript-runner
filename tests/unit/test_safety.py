"""Hostile or malformed .bes content: nothing escapes the sandbox, nothing crashes.

A dry run must never change anything outside the sandbox, whatever the
action says, and a malformed line fails that line cleanly (a report, not a
traceback).
"""

import hashlib
import os
import re
from pathlib import Path, PureWindowsPath

import pytest
from conftest import ScriptedRunner

from bigfix_actionscript_runner.executor import Simulator
from bigfix_actionscript_runner.sandbox import Sandbox
from bigfix_actionscript_runner.ssh_host import SSHHost, _ps
from bigfix_actionscript_runner.substitution import SubstitutionError

DATA = b"payload"
SHA1 = hashlib.sha1(DATA).hexdigest()
SHA256 = hashlib.sha256(DATA).hexdigest()


def literal_evaluator(answers=None):
    answers = answers or {}

    def evaluate(expr):
        m = re.fullmatch(r'"([^"]*)"', expr)
        if m:
            return [m.group(1)]
        if expr in answers:
            return [answers[expr]]
        raise SubstitutionError(f"no answer for {expr!r}")

    return evaluate


def writes_payload(url, dest):
    Path(dest).write_bytes(DATA)


def run(tmp_path, script, *, answers=None, downloader=writes_payload, **kw):
    sb = Sandbox(tmp_path / "sb")
    s = Simulator(
        sb, literal_evaluator(answers), cache_dir=tmp_path / "cache", downloader=downloader, **kw
    )
    return sb, s.run(script)


def files_outside(tmp_path, sb):
    return sorted(
        str(p)
        for p in tmp_path.rglob("*")
        if p.is_file() and not str(p).startswith(sb.root) and "cache" not in p.parts
    )


# --- 1. prefetch names come from the .bes file ---------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "download as ../../escaped http://x/y",
        "download now as ../../escaped http://x/y",
        "download as {abs} http://x/y",
        f"prefetch ../../escaped sha1:{SHA1} size:7 http://x/y sha256:{SHA256}",
        f"prefetch sub/escaped sha1:{SHA1} size:7 http://x/y sha256:{SHA256}",
        f"prefetch C:escaped sha1:{SHA1} size:7 http://x/y sha256:{SHA256}",
        f"prefetch .. sha1:{SHA1} size:7 http://x/y sha256:{SHA256}",
        "download as sub\\escaped http://x/y",
    ],
)
@pytest.mark.parametrize("downloader", [writes_payload, None], ids=["download", "placeholder"])
def test_prefetch_names_cannot_leave_download(tmp_path, line, downloader):
    line = line.replace("{abs}", str(tmp_path / "escaped"))
    sb, r = run(tmp_path, line + "\n", downloader=downloader)
    assert r.status == "failed"
    assert "file name" in r.message
    assert files_outside(tmp_path, sb) == []
    assert not any(p.name == "escaped" for p in Path(sb.root).rglob("*"))


def test_a_plain_prefetch_name_still_works(tmp_path):
    sb, r = run(tmp_path, "download as ok.bin http://x/y\n")
    assert r.status == "completed", r.message
    assert Path(sb.site, "__Download", "ok.bin").read_bytes() == DATA


# --- 2. PowerShell quoting ----------------------------------------------------------


@pytest.mark.parametrize("quote", ["'", "\u2018", "\u2019", "\u201a", "\u201b"])
def test_every_powershell_single_quote_is_doubled(quote):
    """PowerShell treats the typographic single quotes as ' too."""
    literal = _ps(f"C:\\x{quote}; New-Item pwned; {quote}")
    inner = literal[1:-1]
    # no lone quote character survives inside: each is doubled
    for q in ("'", "\u2018", "\u2019", "\u201a", "\u201b"):
        assert re.search(f"(?<!{q}){q}(?!{q})", inner.replace(q * 2, "")) is None


def test_windows_file_operations_quote_typographic_quotes():
    runner = ScriptedRunner()
    host = SSHHost("u@win", runner=runner, windows=True)
    host.delete_file("C:\\x\u2019; New-Item pwned; \u2019")
    assert "\u2019\u2019; New-Item pwned; \u2019\u2019" in runner.scripts[-1]


# --- 3. Windows drive components can't switch drives ---------------------------------


class WindowsPaths:
    windows, pure, name = True, PureWindowsPath, "win"

    def mkdir(self, path):
        pass

    def exists(self, path):
        return False


@pytest.mark.parametrize(
    "path",
    ["a/C:/Windows/x", "C:Windows\\x", "\\\\?\\C:\\Windows\\x", "\\\\.\\C:\\Windows\\x", "x\\D:"],
)
def test_windows_paths_stay_in_a_full_sandbox(path):
    sb = Sandbox("D:\\sb", host=WindowsPaths())
    resolved = sb.resolve(path)
    assert sb.contains(resolved), resolved
    assert resolved.lower().startswith("d:\\sb\\")


def test_extended_length_paths_are_the_paths_they_name_in_client_mode():
    sb = Sandbox("D:\\sb", host=WindowsPaths(), mode="client")
    assert sb.resolve("\\\\?\\C:\\Windows\\x") == "C:\\Windows\\x"


def test_relative_paths_stay_in_the_site_folder_in_every_mode(tmp_path):
    sb = Sandbox("D:\\sb", host=WindowsPaths(), mode="client")
    assert sb.resolve("a/C:/Windows/x").lower().startswith("d:\\sb\\site\\")


def test_folder_delete_of_the_sandbox_root_in_other_case_is_refused():
    sb = Sandbox("D:\\SB", host=WindowsPaths())
    assert sb.is_protected("d:\\sb") and sb.is_protected("D:\\sb\\SITE\\")


# --- 4. malformed lines fail the line, not the program -------------------------------


@pytest.mark.parametrize(
    "script",
    [
        "prefetch x sha1:zz size:1 http://x/y\n",
        "begin prefetch block\nendif\nend prefetch block\n",
        "begin prefetch block\nelse\nend prefetch block\n",
        "begin prefetch block\nelseif {true}\nend prefetch block\n",
        "exit abc\n",
        "override wait\ntimeout_seconds=abc\nwait /bin/true\n",
        pytest.param(
            'wait /bin/echo "unbalanced\n',
            marks=pytest.mark.skipif(os.name == "nt", reason="POSIX quoting"),
        ),
    ],
)
def test_malformed_lines_fail_cleanly(tmp_path, script):
    _, r = run(tmp_path, script)
    assert r.status == "failed"
    assert r.failed_line is not None and r.message


def test_an_unterminated_prefetch_block_fails(tmp_path):
    _, r = run(tmp_path, "begin prefetch block\nwait /bin/true\nfolder delete /etc\n")
    assert (r.status, r.failed_line) == ("failed", 1)
    assert "end prefetch block" in r.message


# --- 5. createfile bodies are text, not ActionScript --------------------------------


def test_the_prefetch_pass_skips_createfile_bodies(tmp_path):
    calls = []

    def downloader(url, dest):
        calls.append(url)
        writes_payload(url, dest)

    sb, r = run(
        tmp_path,
        "createfile until END\ndownload as zzz http://x/y\nbegin prefetch block\nEND\n",
        downloader=downloader,
    )
    assert r.status == "completed", r.message
    assert calls == [] and r.prefetched == []
    assert Path(sb.site, "__createfile").read_text() == (
        "download as zzz http://x/y\nbegin prefetch block\n"
    )


# --- 6. --set matches the expression as written in the action ------------------------


def test_overrides_match_the_expression_before_it_is_rewritten(tmp_path):
    _, r = run(
        tmp_path,
        'parameter "p" = "v"\n'
        'continue if {exists file "a" of client folder of current site}\n'
        'continue if {parameter "p" = "v"}\n',
        overrides={
            'exists file "a" of client folder of current site': "True",
            'parameter "p" = "v"': "True",
        },
    )
    assert r.status == "completed", r.message


# --- 7. whitespace after if / elseif -----------------------------------------------


def test_a_tab_after_if_is_still_an_if(tmp_path):
    _, r = run(tmp_path, "if\t{false}\nexit 3\nendif\n", answers={"false": "False"})
    assert r.status == "completed", r.message


def test_a_tab_after_elseif_is_still_an_elseif(tmp_path):
    _, r = run(
        tmp_path,
        "if {false}\nelseif\t{false}\nexit 3\nendif\n",
        answers={"false": "False"},
    )
    assert r.status == "completed", r.message


# --- a rooted path on Windows is on the current drive --------------------------------


def test_windows_rooted_paths_are_on_the_site_folders_drive():
    full = Sandbox("D:\\sb", host=WindowsPaths())
    assert full.resolve("\\Windows\\Temp\\x") == "D:\\sb\\fs\\D\\Windows\\Temp\\x"
    client = Sandbox("D:\\sb", host=WindowsPaths(), mode="client")
    assert client.resolve("\\Windows\\Temp\\x") == "D:\\Windows\\Temp\\x"
    assert client.resolve("/Windows/Temp/x") == "D:\\Windows\\Temp\\x"
