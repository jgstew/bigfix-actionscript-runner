"""The executor: ActionScript semantics against a sandbox, with a fake evaluator."""

import hashlib
import os
import re
from pathlib import Path

import pytest

from bigfix_actionscript_runner.executor import Simulator
from bigfix_actionscript_runner.sandbox import Sandbox
from bigfix_actionscript_runner.substitution import SubstitutionError

posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX paths on a POSIX LocalHost")


def fake_evaluator(answers=None, calls=None):
    """String literals (what `parameter "x"` is rewritten to) evaluate to themselves;
    anything else comes from `answers` (a value, a list, or an exception)."""
    answers = answers or {}

    def evaluate(expr):
        if calls is not None:
            calls.append(expr)
        m = re.fullmatch(r'"([^"]*)"', expr)
        if m:
            return [m.group(1).replace("%22", '"').replace("%25", "%")]
        if expr not in answers:
            raise SubstitutionError(f"fake: no answer for {expr!r}")
        value = answers[expr]
        if isinstance(value, Exception):
            raise value
        return value if isinstance(value, list) else [value]

    return evaluate


def sim(tmp_path, answers=None, calls=None, **kw):
    sb = Sandbox(tmp_path / "sb")
    return sb, Simulator(sb, fake_evaluator(answers, calls), cache_dir=tmp_path / "cache", **kw)


def outcomes(report):
    return [(s.line, s.outcome) for s in report.steps]


# --- basics ----------------------------------------------------------------


def test_comments_and_blank_lines_are_not_steps(tmp_path):
    _, s = sim(tmp_path)
    r = s.run("// hi\n\n   // indented comment\n")
    assert r.status == "completed"
    assert r.steps == []


def test_parameter_is_set_and_substituted(tmp_path):
    sb, s = sim(tmp_path, {"name of operating system": "macOS"})
    r = s.run(
        'parameter "os" = "{name of operating system}"\ncreatefile until END\nos={parameter "os"}\nEND\n'
    )
    assert r.status == "completed"
    assert s.parameters["os"] == "macOS"
    assert (Path(sb.site) / "__createfile").read_text() == "os=macOS\n"


def test_parameter_cannot_be_redefined(tmp_path):
    _, s = sim(tmp_path)
    r = s.run('parameter "a" = "1"\nparameter "a" = "2"\n')
    assert r.status == "failed"
    assert r.failed_line == 2
    assert "already" in r.message


def test_action_parameter_query_uses_given_value_then_default(tmp_path):
    _, s = sim(tmp_path, params={"given": "from-cli"})
    r = s.run(
        'action parameter query "given" with description "d" with default "dflt"\n'
        'action parameter query "other" with description "d" with default "dflt"\n'
    )
    assert r.status == "completed"
    assert s.parameters == {"given": "from-cli", "other": "dflt"}


def test_action_parameter_query_without_value_or_default_fails(tmp_path):
    _, s = sim(tmp_path)
    r = s.run('action parameter query "x" with description "d"\n')
    assert r.status == "failed"
    assert "--param x=" in r.message


def test_line_numbers_include_the_file_offset(tmp_path):
    _, s = sim(tmp_path)
    r = s.run("delete a\nbogus command\n", first_line=40)
    assert outcomes(r) == [(40, "ok"), (41, "warning")]


def test_failed_substitution_fails_the_line(tmp_path):
    _, s = sim(tmp_path, {"bad": SubstitutionError('The operator "bad" is not defined.')})
    r = s.run("delete a\ncopy {bad} b\ndelete c\n")
    assert r.status == "failed"
    assert r.failed_line == 2
    assert "not defined" in r.message
    assert len(r.steps) == 2


# --- files -----------------------------------------------------------------


def test_createfile_escapes_and_appendfile(tmp_path):
    sb, s = sim(tmp_path)
    r = s.run(
        "createfile until __EOF\nif x; then {{ a; }\n__EOF\nappendfile line1\nappendfile line2\n"
    )
    assert r.status == "completed"
    assert (Path(sb.site) / "__createfile").read_text() == "if x; then { a; }\n"
    assert (Path(sb.site) / "__appendfile").read_text() == "line1\nline2\n"


def test_unterminated_createfile_fails(tmp_path):
    _, s = sim(tmp_path)
    r = s.run("createfile until END\nabc\n")
    assert r.status == "failed"
    assert "END" in r.message


@posix_only
def test_copy_into_absolute_path_lands_in_the_sandbox(tmp_path):
    sb, s = sim(tmp_path)
    r = s.run('createfile until E\nx\nE\ncopy __createfile "/tmp/run scan.sh"\n')
    assert r.status == "completed"
    assert (Path(sb.fs) / "tmp" / "run scan.sh").read_text() == "x\n"


def test_copy_fails_if_destination_exists_like_bigfix(tmp_path):
    sb, s = sim(tmp_path)
    (Path(sb.site) / "a").write_text("1")
    (Path(sb.site) / "b").write_text("2")
    r = s.run("copy a b\n")
    assert r.status == "failed"
    assert "exists" in r.message


def test_copy_fails_if_source_is_missing(tmp_path):
    _, s = sim(tmp_path)
    r = s.run("copy nope b\n")
    assert r.status == "failed"
    assert "does not exist" in r.message


def test_copy_reads_a_real_file_outside_without_touching_it(tmp_path):
    real = tmp_path / "real.txt"
    real.write_text("real")
    sb, s = sim(tmp_path)
    r = s.run(f'copy "{real}" got.txt\n')
    assert r.status == "completed"
    assert (Path(sb.site) / "got.txt").read_text() == "real"
    assert real.read_text() == "real"


def test_move_and_delete(tmp_path):
    sb, s = sim(tmp_path)
    (Path(sb.site) / "a").write_text("1")
    r = s.run("move a b\ndelete b\ndelete never-existed\n")
    assert r.status == "completed"
    assert not (Path(sb.site) / "a").exists() and not (Path(sb.site) / "b").exists()


def test_folder_create_and_delete(tmp_path):
    sb, s = sim(tmp_path)
    r = s.run('folder create "/opt/x y/z"\nfolder delete "/opt/x y"\nfolder delete "/never"\n')
    assert r.status == "completed"
    assert not (Path(sb.fs) / "opt" / "x y").exists()


def test_a_dry_run_never_creates_real_folders(tmp_path):
    real = tmp_path / "real dir"
    sb, s = sim(tmp_path)
    r = s.run(f'folder create "{real}/z"\n')
    assert r.status == "completed"
    assert not real.exists()
    assert sb.contains(sb.resolve(str(real / "z")))


@posix_only
def test_a_dry_run_on_an_ssh_target_changes_nothing_outside_the_sandbox(tmp_path, shell_host):
    """The same dry run through SSHHost: every write stays in the target's sandbox."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    sb = Sandbox(str(tmp_path / "remote-sb"), host=shell_host)
    s = Simulator(sb, fake_evaluator(), cache_dir=tmp_path / "c")
    r = s.run(
        "createfile until E\nx\nE\n"
        f'copy __createfile "{outside}/new.txt"\n'
        f'delete "{outside}/keep.txt"\n'
        f'folder create "{outside}/made"\n'
        f'folder delete "{outside}"\n'
        f'move "{outside}/keep.txt" "{outside}/moved.txt"\n'
        f'wait /bin/sh -c "touch {outside}/ran"\n'
    )
    assert r.status == "completed", r.message
    assert sorted(p.name for p in outside.iterdir()) == ["keep.txt"]


def test_delete_of_absolute_path_only_touches_the_sandbox(tmp_path):
    real = tmp_path / "keep.txt"
    real.write_text("keep")
    _, s = sim(tmp_path)
    r = s.run(f'delete "{real}"\nfolder delete "{tmp_path}"\n')
    assert r.status == "completed"
    assert real.read_text() == "keep"


# --- control flow ----------------------------------------------------------


def test_if_elseif_else(tmp_path):
    sb, s = sim(tmp_path, {"a": "False", "b": "True"})
    r = s.run("if {a}\nappendfile A\nelseif {b}\nappendfile B\nelse\nappendfile C\nendif\n")
    assert r.status == "completed"
    assert (Path(sb.site) / "__appendfile").read_text() == "B\n"
    assert "skipped" in [o for _, o in outcomes(r)]


def test_nested_if_inside_a_false_branch_is_not_evaluated(tmp_path):
    calls = []
    _sb, s = sim(tmp_path, {"a": "False"}, calls=calls)
    r = s.run("if {a}\nif {never}\nappendfile X\nendif\nendif\n")
    assert r.status == "completed"
    assert "never" not in calls


def test_unbalanced_if_fails(tmp_path):
    _, s = sim(tmp_path, {"a": "True"})
    assert s.run("if {a}\ndelete x\n").status == "failed"
    _, s = sim(tmp_path)
    assert s.run("endif\n").status == "failed"


def test_non_boolean_if_condition_fails(tmp_path):
    _, s = sim(tmp_path, {"a": "7"})
    r = s.run("if {a}\nendif\n")
    assert r.status == "failed"
    assert "boolean" in r.message


def test_continue_if(tmp_path):
    _, s = sim(tmp_path, {"ok": "True", "no": "False"})
    r = s.run("continue if {ok}\ncontinue if {no}\ndelete x\n")
    assert r.status == "failed"
    assert r.failed_line == 2
    assert "continue if" in r.message


def test_exit_stops_with_a_code(tmp_path):
    _, s = sim(tmp_path)
    r = s.run("exit 3\ndelete x\n")
    assert (r.status, r.exit_code, len(r.steps)) == ("exited", 3, 1)


def test_pause_while_true_is_a_warning(tmp_path):
    _, s = sim(tmp_path, {"busy": "True"})
    r = s.run("pause while {busy}\n")
    assert r.status == "completed"
    assert outcomes(r) == [(1, "warning")]


# --- commands --------------------------------------------------------------


def test_commands_are_shown_not_run_by_default(tmp_path):
    ran = []
    _, s = sim(tmp_path, runner=lambda *a, **k: ran.append(a) or 0)
    r = s.run('wait /bin/bash "{parameter "x"}"\n'.replace('{parameter "x"}', "/tmp/a.sh"))
    assert r.status == "completed"
    assert ran == []
    assert outcomes(r) == [(1, "shown")]


def test_exit_code_of_action_uses_the_assumed_code(tmp_path):
    calls = []
    _, s = sim(tmp_path, {"5 = 0": "False"}, calls=calls, assumed_exit_code=5)
    r = s.run("wait /bin/false\ncontinue if {exit code of action = 0}\n")
    assert r.status == "failed"
    assert "5 = 0" in calls


@posix_only
def test_execute_runs_waits_with_real_exit_code_and_remaps_paths(tmp_path):
    ran = []

    def runner(argv, *, cwd, wait, timeout):
        ran.append((argv, cwd, wait, timeout))
        return 1

    sb, s = sim(tmp_path, {"1 = 0": "False"}, execute=True, runner=runner)
    r = s.run(
        "createfile until E\necho hi\nE\n"
        'copy __createfile "/tmp/x.sh"\n'
        "override wait\ntimeout_seconds=30\nhidden=true\n"
        'wait /bin/bash "/tmp/x.sh" /etc/hosts\n'
        "continue if {exit code of action = 0}\n"
    )
    argv, cwd, wait, timeout = ran[0]
    assert argv == [
        "/bin/bash",
        str(Path(sb.fs) / "tmp" / "x.sh"),
        "/etc/hosts",
    ]  # /etc/hosts isn't in the sandbox
    assert (cwd, wait, timeout) == (sb.site, True, 30)
    assert r.status == "failed" and r.failed_line == 9


def test_override_key_lines_have_their_own_line_numbers(tmp_path):
    _, s = sim(tmp_path)
    r = s.run(
        "delete a\noverride wait\nhidden=true\ntimeout_seconds=9\nwait /bin/true\n", first_line=10
    )
    assert outcomes(r) == [(10, "ok"), (11, "ok"), (12, "ok"), (13, "ok"), (14, "shown")]


def test_sandbox_relevance_goes_to_the_one_evaluator(tmp_path):
    calls = []
    sb, s = sim(tmp_path, {}, calls=calls)
    s.run('parameter "d" = "{pathname of data folder of client}"\n')
    assert calls == [f'pathname of folder "{sb.data}"']


def test_run_does_not_wait_and_override_is_reset(tmp_path):
    ran = []

    def runner(argv, *, cwd, wait, timeout):
        ran.append((wait, timeout))
        return 0

    _, s = sim(tmp_path, execute=True, runner=runner)
    s.run("override run\ntimeout_seconds=5\nrun /bin/echo a\nwait /bin/echo b\n")
    assert ran == [(False, 5), (True, None)]


def test_noop_commands_are_recorded(tmp_path):
    _, s = sim(tmp_path, {"not x64 of operating system": "False"})
    r = s.run(
        "action uses wow64 redirection {not x64 of operating system}\n"
        "action requires restart\n"
        'setting "a"="b" on "{{now}}" for client\n'
        'regset "[HKEY_LOCAL_MACHINE\\Software\\X]" "v"="1"\n'
    )
    assert r.status == "completed"
    assert [o for _, o in outcomes(r)] == ["noop"] * 4


# --- prefetch ----------------------------------------------------------------

DATA = b"archive"
SHA1 = hashlib.sha1(DATA).hexdigest()
SHA256 = hashlib.sha256(DATA).hexdigest()


def downloader(calls):
    def download(url, dest):
        calls.append(url)
        dest.write_bytes(DATA)

    return download


def test_prefetch_block_with_conditions_runs_before_line_one(tmp_path):
    calls = []
    sb, s = sim(
        tmp_path,
        {
            'architecture of operating system = "arm64"': "True",
            'architecture of operating system != "arm64"': "False",
        },
        downloader=downloader(calls),
    )
    r = s.run(
        "delete __createfile\n"
        "begin prefetch block\n"
        '\tif {architecture of operating system = "arm64"}\n'
        f"\t\tadd prefetch item name=arm.tgz sha1={SHA1} size=7 url=https://x/arm.tgz sha256={SHA256}\n"
        "\tendif\n"
        '\tif {architecture of operating system != "arm64"}\n'
        f"\t\tadd prefetch item name=x86.tgz sha1={SHA1} size=7 url=https://x/x86.tgz sha256={SHA256}\n"
        "\tendif\n"
        "end prefetch block\n"
        "copy __Download/arm.tgz got.tgz\n"
    )
    assert r.status == "completed", r.message
    assert calls == ["https://x/arm.tgz"]
    assert (Path(sb.site) / "got.tgz").read_bytes() == DATA
    assert r.prefetched == ["arm.tgz"]


def test_prefetch_statement_outside_block(tmp_path):
    calls = []
    sb, s = sim(tmp_path, downloader=downloader(calls))
    r = s.run(f"prefetch a.zip sha1:{SHA1} size:7 https://x/a.zip sha256:{SHA256}\n")
    assert r.status == "completed"
    assert (Path(sb.site) / "__Download" / "a.zip").read_bytes() == DATA


def test_bad_prefetch_hash_fails_before_any_line_runs(tmp_path):
    _, s = sim(tmp_path, downloader=downloader([]))
    r = s.run(f"delete a\nprefetch a.zip sha1:{'0' * 40} size:7 https://x/a.zip\n")
    assert r.status == "failed"
    assert r.failed_line == 2
    assert r.steps[0].line == 2


def test_download_now_happens_at_its_line(tmp_path):
    calls = []
    sb, s = sim(tmp_path, downloader=downloader(calls))
    r = s.run("delete x\ndownload now as n.bin https://x/n.bin\n")
    assert r.status == "completed"
    assert (Path(sb.site) / "__Download" / "n.bin").exists()
    assert [st.outcome for st in r.steps] == ["ok", "ok"]


# --- baseline: every expression evaluated up front, then again at its step ---


def stateful(sb):
    """Answers that depend on the sandbox, like real relevance does."""
    marker = Path(sb.site) / "made.txt"

    def exists_marker():
        return "True" if marker.exists() else "False"

    return exists_marker


def evaluator_with(answers):
    def evaluate(expr):
        m = re.fullmatch(r'"([^"]*)"', expr)
        if m:
            return [m.group(1)]
        value = answers.get(expr)
        if value is None:
            raise SubstitutionError(f"fake: no answer for {expr!r}")
        return [value() if callable(value) else value]

    return evaluate


def test_baseline_evaluates_every_expression_before_line_one(tmp_path):
    sb = Sandbox(tmp_path / "sb")
    s = Simulator(
        sb,
        evaluator_with({"a": "1", "made": stateful(sb)}),
        cache_dir=tmp_path / "c",
        baseline=True,
    )
    r = s.run("delete x\ncreatefile until E\n{a} {made}\nE\ncopy __createfile made.txt\n")
    assert [(b.expression, b.value, b.error) for b in r.baseline] == [
        ("a", "1", None),
        ("made", "False", None),
    ]


def test_step_values_are_evaluated_again_and_compared_with_the_baseline(tmp_path):
    sb = Sandbox(tmp_path / "sb")
    s = Simulator(
        sb, evaluator_with({"made": stateful(sb)}), cache_dir=tmp_path / "c", baseline=True
    )
    r = s.run(
        "createfile until E\nx\nE\ncopy __createfile made.txt\n"
        "continue if {made}\n"  # False at the start, True by now
    )
    assert r.status == "completed", r.message
    (ev,) = r.steps[-1].evaluations
    assert (ev.expression, ev.value, ev.baseline_value, ev.changed) == (
        "made",
        "True",
        "False",
        True,
    )


def test_an_error_in_the_baseline_can_become_a_value(tmp_path):
    """Like the name of a prefetched file: no such file before the action, a name after."""
    sb = Sandbox(tmp_path / "sb")
    made = Path(sb.site) / "made.txt"

    def name_of_made():
        if not made.exists():
            raise SubstitutionError("Singular expression refers to nonexistent object.")
        return "made.txt"

    s = Simulator(
        sb, evaluator_with({"name of made": name_of_made}), cache_dir=tmp_path / "c", baseline=True
    )
    r = s.run(
        "createfile until E\nx\nE\ncopy __createfile made.txt\ncreatefile until F\n{name of made}\nF\n"
    )
    assert r.status == "completed", r.message
    assert r.baseline[0].error and "nonexistent" in r.baseline[0].error
    (ev,) = r.steps[-1].evaluations
    assert (ev.value, ev.changed) == ("made.txt", True)
    assert ev.baseline_error


def test_unchanged_values_are_not_flagged(tmp_path):
    sb = Sandbox(tmp_path / "sb")
    s = Simulator(sb, evaluator_with({"a": "1"}), cache_dir=tmp_path / "c", baseline=True)
    r = s.run("createfile until E\n{a}\nE\n")
    (ev,) = r.steps[0].evaluations
    assert (ev.value, ev.changed) == ("1", False)


def test_no_baseline_by_default_in_the_executor(tmp_path):
    _, s = sim(tmp_path, {"a": "1"})
    r = s.run("createfile until E\n{a}\nE\n")
    assert r.baseline == []
    assert r.steps[0].evaluations[0].baseline_value is None


def test_baseline_defers_expressions_that_use_a_parameter_set_later(tmp_path):
    calls = []
    sb = Sandbox(tmp_path / "sb")
    ev = evaluator_with({"made": stateful(sb)})

    def counting(expr):
        calls.append(expr)
        return ev(expr)

    s = Simulator(sb, counting, cache_dir=tmp_path / "c", baseline=True)
    r = s.run('parameter "p" = "v"\ncreatefile until E\n{parameter "p"} {made}\nE\n')
    entry = next(b for b in r.baseline if b.expression == 'parameter "p"')
    assert entry.deferred and entry.error is None and entry.value is None
    assert calls[0] == "made"  # the baseline didn't send the parameter expression to qna
    step_ev = next(e for e in r.steps[-1].evaluations if e.expression == 'parameter "p"')
    assert step_ev.changed is False


def test_parameters_given_up_front_are_not_deferred(tmp_path):
    sb = Sandbox(tmp_path / "sb")
    s = Simulator(
        sb, evaluator_with({}), cache_dir=tmp_path / "c", baseline=True, params={"q": "given"}
    )
    r = s.run(
        'action parameter query "q" with description "d"\ncreatefile until E\n{parameter "q"}\nE\n'
    )
    entry = r.baseline[0]
    assert (entry.deferred, entry.value) == (False, "given")


# --- sandbox modes: what a real (--execute) run changes ----------------------------


def test_a_dry_run_refuses_a_sandbox_that_lets_real_paths_through(tmp_path):
    sb = Sandbox(tmp_path / "sb", mode="client")
    try:
        Simulator(sb, fake_evaluator(), cache_dir=tmp_path / "c")
    except ValueError as e:
        assert "--execute" in str(e)
    else:
        raise AssertionError("a dry run must keep every path in the sandbox")


def test_client_mode_changes_real_files_outside_the_client_folders(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (real / "old.txt").write_text("old")
    sb = Sandbox(tmp_path / "sb", mode="client")
    s = Simulator(sb, fake_evaluator(), cache_dir=tmp_path / "c", execute=True)
    r = s.run(
        "createfile until E\nnew\nE\n"
        f'copy __createfile "{real}/new.txt"\n'
        f'delete "{real}/old.txt"\n'
        f'folder create "{real}/made/deeper"\n'
    )
    assert r.status == "completed", r.message
    assert (real / "new.txt").read_text() == "new\n"
    assert not (real / "old.txt").exists()
    assert (real / "made" / "deeper").is_dir()
    assert Path(sb.site, "__createfile").exists()  # the client folder stays in the sandbox


def test_client_mode_move_moves_the_real_file(tmp_path):
    (tmp_path / "a.txt").write_text("a")
    sb = Sandbox(tmp_path / "sb", mode="client")
    s = Simulator(sb, fake_evaluator(), cache_dir=tmp_path / "c", execute=True)
    r = s.run(f'move "{tmp_path}/a.txt" "{tmp_path}/b.txt"\n')
    assert r.status == "completed", r.message
    assert not (tmp_path / "a.txt").exists() and (tmp_path / "b.txt").read_text() == "a"


def test_client_mode_relevance_still_sees_the_sandboxed_client_folders(tmp_path):
    calls = []
    sb = Sandbox(tmp_path / "sb", mode="client")
    s = Simulator(sb, fake_evaluator({}, calls), cache_dir=tmp_path / "c", execute=True)
    s.run('parameter "d" = "{pathname of data folder of client}"\n')
    assert calls == [f'pathname of folder "{sb.data}"']


def test_off_mode_uses_the_real_client_folders(tmp_path):
    site = tmp_path / "BES" / "__BESData" / "MySite"
    site.mkdir(parents=True)
    calls = []
    sb = Sandbox(mode="off", site_folder=str(site), data_folder=str(site.parent))
    answers = {f'pathname of folder "{site}"': str(site)}
    s = Simulator(sb, fake_evaluator(answers, calls), cache_dir=tmp_path / "c", execute=True)
    r = s.run(
        'createfile until E\nx\nE\nparameter "f" = "{pathname of client folder of current site}"\n'
    )
    assert r.status == "completed", r.message
    assert (site / "__createfile").read_text() == "x\n"
    assert calls == [f'pathname of folder "{site}"']


def test_folder_delete_never_removes_the_client_folders(tmp_path):
    site = tmp_path / "MySite"
    site.mkdir()
    sb = Sandbox(mode="off", site_folder=str(site), data_folder=str(tmp_path))
    s = Simulator(sb, fake_evaluator(), cache_dir=tmp_path / "c", execute=True)
    s.run(f'folder delete "{site}"\nfolder delete "{tmp_path}"\n')
    assert site.is_dir()


# --- the host: everything happens on the target ------------------------------------


def test_everything_happens_on_the_host(tmp_path, shell_host):
    """With an SSH host, file commands and executed commands run on the target."""
    real = tmp_path / "target-real"
    sb = Sandbox(str(tmp_path / "remote-sb"), host=shell_host, mode="client")
    s = Simulator(sb, fake_evaluator({"4 = 4": "True"}), cache_dir=tmp_path / "c", execute=True)
    r = s.run(
        "createfile until E\n#!/bin/sh\npwd > ran-in.txt\nexit 4\nE\n"
        f'folder create "{real}"\n'
        f'copy __createfile "{real}/job.sh"\n'
        f'wait /bin/sh "{real}/job.sh"\n'
        "continue if {exit code of action = 4}\n"
    )
    assert r.status == "completed", r.message
    assert (real / "job.sh").exists()
    assert (
        Path(Path(sb.site, "ran-in.txt").read_text().strip()).resolve() == Path(sb.site).resolve()
    )
    assert s.exit_code == 4
    assert any("job.sh" in c for c in shell_host._runner.commands)


def test_prefetches_are_put_on_the_host_and_hashed_there(tmp_path):
    from bigfix_actionscript_runner.host import LocalHost

    class OtherHashHost(LocalHost):
        def sha256(self, path):
            return "0" * 64

    sb = Sandbox(tmp_path / "sb", host=OtherHashHost())
    s = Simulator(sb, fake_evaluator(), cache_dir=tmp_path / "c", downloader=downloader([]))
    r = s.run(f"prefetch a.zip sha1:{SHA1} size:7 https://x/a.zip sha256:{SHA256}\n")
    assert r.status == "failed"
    assert "on the target" in r.message


def test_host_errors_fail_the_line(tmp_path):
    from bigfix_actionscript_runner.host import HostError, LocalHost

    class ReadOnlyHost(LocalHost):
        def write_bytes(self, path, data, *, append=False):
            raise HostError("Read-only file system")

    sb = Sandbox(tmp_path / "sb", host=ReadOnlyHost())
    s = Simulator(sb, fake_evaluator(), cache_dir=tmp_path / "c")
    r = s.run("createfile until E\nx\nE\n")
    assert r.status == "failed" and "Read-only" in r.message


def test_windows_commands_are_split_like_a_windows_command_line(tmp_path):
    from pathlib import PureWindowsPath

    class WindowsShapedHost:
        windows, pure, name = True, PureWindowsPath, "win"

        def __init__(self) -> None:
            self.ran = []

        def mkdir(self, path):
            pass

        def exists(self, path):
            return False

        def run(self, argv, *, cwd, wait, timeout=None):
            self.ran.append((argv, cwd))
            return 0

    host = WindowsShapedHost()
    sb = Sandbox("C:\\T\\sb", host=host, mode="client")
    s = Simulator(sb, fake_evaluator(), cache_dir=tmp_path / "c", execute=True)
    r = s.run('wait "C:\\Program Files\\App\\app.exe" /S "/D=C:\\x y"\n')
    assert r.status == "completed", r.message
    # the program unquoted; the rest exactly as written, for the program to parse
    assert host.ran == [
        (["C:\\Program Files\\App\\app.exe", "/S", '"/D=C:\\x y"'], "C:\\T\\sb\\site")
    ]


def test_windows_arguments_keep_their_own_quoting(tmp_path):
    from pathlib import PureWindowsPath

    class WindowsShapedHost:
        windows, pure, name = True, PureWindowsPath, "win"

        def __init__(self) -> None:
            self.ran = []

        def mkdir(self, path):
            pass

        def exists(self, path):
            return False

        def run(self, argv, *, cwd, wait, timeout=None):
            self.ran.append(argv)
            return 0

    host = WindowsShapedHost()
    sb = Sandbox("C:\\T\\sb", host=host, mode="client")
    s = Simulator(sb, fake_evaluator(), cache_dir=tmp_path / "c", execute=True)
    s.run('wait msiexec /i "C:\\a b.msi" TARGETDIR="C:\\x y"\ndos echo a > b.txt\n')
    assert host.ran == [
        ["msiexec", "/i", '"C:\\a b.msi"', 'TARGETDIR="C:\\x y"'],
        ["cmd.exe", "/c", "echo a > b.txt"],
    ]


# --- more ActionScript semantics ----------------------------------------------------

ARCH = {
    'architecture of operating system = "arm64"': "False",
    'architecture of operating system = "x86_64"': "True",
}


def test_prefetch_block_elseif_and_else_pick_one_item(tmp_path):
    calls = []
    _, s = sim(tmp_path, ARCH, downloader=downloader(calls))
    r = s.run(
        "begin prefetch block\n"
        '  if {architecture of operating system = "arm64"}\n'
        f"    add prefetch item name=a.tgz sha1={SHA1} size=7 url=https://x/a sha256={SHA256}\n"
        '  elseif {architecture of operating system = "x86_64"}\n'
        f"    add prefetch item name=b.tgz sha1={SHA1} size=7 url=https://x/b sha256={SHA256}\n"
        "  else\n"
        f"    add prefetch item name=c.tgz sha1={SHA1} size=7 url=https://x/c sha256={SHA256}\n"
        "  endif\n"
        "end prefetch block\n"
    )
    assert r.status == "completed", r.message
    assert calls == ["https://x/b"]
    assert r.prefetched == ["b.tgz"]


def test_other_lines_in_a_prefetch_block_are_warnings(tmp_path):
    _, s = sim(tmp_path)
    r = s.run("begin prefetch block\ncollect prefetch items\nend prefetch block\n")
    assert r.status == "completed"
    assert [(st.line, st.outcome) for st in r.steps] == [(2, "warning")]


def test_a_failure_in_a_prefetch_block_names_its_line(tmp_path):
    _, s = sim(tmp_path, downloader=downloader([]))
    r = s.run(
        "begin prefetch block\n"
        f"add prefetch item name=a sha1={'0' * 40} size=7 url=https://x/a\n"
        "end prefetch block\n",
        first_line=20,
    )
    assert (r.status, r.failed_line) == ("failed", 21)


def test_download_failures_fail_the_action(tmp_path):
    def broken(url, dest):
        raise ConnectionError("no route to host")

    _, s = sim(tmp_path, downloader=broken)
    r = s.run("download now https://x/a.zip\n")
    assert r.status == "failed"
    assert "download failed: no route to host" in r.message


def test_a_prefetch_that_cant_be_put_on_the_target_fails(tmp_path):
    from bigfix_actionscript_runner.host import HostError, LocalHost

    class FullDisk(LocalHost):
        def put_file(self, local, dst):
            raise HostError("No space left on device")

    sb = Sandbox(tmp_path / "sb", host=FullDisk())
    s = Simulator(sb, fake_evaluator(), cache_dir=tmp_path / "c", downloader=downloader([]))
    r = s.run(f"prefetch a.zip sha1:{SHA1} size:7 https://x/a.zip sha256:{SHA256}\n")
    assert r.status == "failed"
    assert "can't put a.zip on the target: No space left" in r.message


def test_createfile_in_a_false_branch_is_skipped_and_not_substituted(tmp_path):
    calls = []
    sb, s = sim(tmp_path, {"1 = 2": "False"}, calls=calls)
    r = s.run("if {1 = 2}\ncreatefile until E\n{never evaluated}\nE\nendif\n")
    assert r.status == "completed", r.message
    assert "never evaluated" not in calls
    assert not Path(sb.site, "__createfile").exists()
    assert outcomes(r) == [(1, "ok"), (2, "skipped"), (5, "ok")]


@pytest.mark.parametrize("line", ["else", "endif", "elseif {true}"])
def test_else_and_endif_without_if_fail(tmp_path, line):
    _, s = sim(tmp_path)
    r = s.run(line + "\n")
    assert r.status == "failed"
    assert "without if" in r.message


def test_copy_needs_two_arguments(tmp_path):
    _, s = sim(tmp_path)
    r = s.run("copy onlyone\n")
    assert r.status == "failed" and "source and a destination" in r.message


def test_unparsable_parameter_line_fails(tmp_path):
    _, s = sim(tmp_path)
    r = s.run("parameter x = y\n")
    assert r.status == "failed" and "can't parse parameter" in r.message


def test_unknown_commands_warn_and_the_action_continues(tmp_path):
    _, s = sim(tmp_path)
    r = s.run("frobnicate now\ndelete a\n")
    assert r.status == "completed"
    assert outcomes(r) == [(1, "warning"), (2, "ok")]
    assert r.steps[0].detail == "command not simulated"


def test_pause_while_false_is_ok(tmp_path):
    _, s = sim(tmp_path, {"x": "False"})
    r = s.run("pause while {x}\n")
    assert outcomes(r) == [(1, "ok")]


def test_a_dry_run_moves_files_within_the_sandbox(tmp_path):
    sb, s = sim(tmp_path)
    Path(sb.site, "a").write_text("1")
    r = s.run("move a b\n")
    assert r.status == "completed"
    assert not Path(sb.site, "a").exists() and Path(sb.site, "b").read_text() == "1"


def test_a_dry_run_copies_rather_than_moves_a_real_file(tmp_path):
    real = tmp_path / "real.txt"
    real.write_text("r")
    sb, s = sim(tmp_path)
    r = s.run(f'move "{real}" b\n')
    assert r.status == "completed", r.message
    assert real.read_text() == "r"  # untouched
    assert Path(sb.site, "b").read_text() == "r"


def test_baseline_evaluates_each_distinct_expression_once_and_skips_comments(tmp_path):
    calls = []
    _, s = sim(tmp_path, {"a": "1"}, calls=calls, baseline=True)
    s.run("// {not this}\ncreatefile until E\n{a} {a}\nE\n")
    assert calls.count("a") == 3  # once for the baseline, twice at the step
    assert "not this" not in calls


def test_a_dry_run_assumes_the_exit_code_for_wait_but_not_for_run(tmp_path):
    _, s = sim(tmp_path, assumed_exit_code=4)
    s.exit_code = 9
    s.run("run /bin/true\n")
    assert s.exit_code == 9
    s.run("wait /bin/true\n")
    assert s.exit_code == 4


# --- keyword followed directly by { (30+ real fixlets write `if{...}`) ----------------


def test_if_and_elseif_directly_followed_by_a_brace(tmp_path):
    _, s = sim(tmp_path, {"false": "False", "true": "True"})
    r = s.run("if{false}\nexit 3\nelseif{true}\ndelete a\nendif\n")
    assert r.status == "completed", r.message
    assert outcomes(r) == [(1, "ok"), (2, "skipped"), (3, "ok"), (4, "ok"), (5, "ok")]


def test_continue_if_directly_followed_by_a_brace_still_guards(tmp_path):
    _, s = sim(tmp_path, {"false": "False"})
    r = s.run("continue if{false}\ndelete a\n")
    assert (r.status, r.failed_line) == ("failed", 1)


def test_pause_while_directly_followed_by_a_brace(tmp_path):
    _, s = sim(tmp_path, {"true": "True"})
    r = s.run("pause while{true}\n")
    assert outcomes(r) == [(1, "warning")]
    assert "would wait" in r.steps[0].detail


def test_if_with_a_brace_in_a_prefetch_block(tmp_path):
    calls = []
    _, s = sim(tmp_path, {"false": "False"}, downloader=downloader(calls))
    r = s.run(
        "begin prefetch block\nif{false}\n"
        f"add prefetch item name=a sha1={SHA1} size=7 url=https://x/a sha256={SHA256}\n"
        "endif\nend prefetch block\n"
    )
    assert r.status == "completed", r.message
    assert calls == []


# --- parameters a client has before the first line --------------------------------


def test_action_issue_date_is_a_built_in_parameter(tmp_path):
    sb, s = sim(tmp_path)
    r = s.run('createfile until E\n{parameter "action issue date" of action}\nE\n')
    assert r.status == "completed", r.message
    text = Path(sb.site, "__createfile").read_text().strip()
    # the format a client uses, e.g. "Wed, 22 Oct 2026 05:00:00 +0000"
    assert re.fullmatch(r"[A-Z][a-z]{2}, \d{2} [A-Z][a-z]{2} \d{4} \d{2}:\d{2}:\d{2} \+0000", text)


def test_param_gives_any_parameter_a_value(tmp_path):
    """Secure parameters and the like come from the console, not the script."""
    sb, s = sim(
        tmp_path,
        params={"secret": "s3cr3t", "action issue date": "Mon, 01 Jan 2024 00:00:00 +0000"},
    )
    r = s.run(
        'createfile until E\n{parameter "secret" of action} {parameter "action issue date"}\nE\n'
    )
    assert r.status == "completed", r.message
    assert Path(sb.site, "__createfile").read_text() == "s3cr3t Mon, 01 Jan 2024 00:00:00 +0000\n"


def test_a_script_parameter_wins_over_a_given_one_without_redefinition(tmp_path):
    _, s = sim(tmp_path, params={"p": "given"})
    r = s.run('parameter "p" = "script"\n')
    assert r.status == "completed", r.message
    assert s.parameters["p"] == "script"
