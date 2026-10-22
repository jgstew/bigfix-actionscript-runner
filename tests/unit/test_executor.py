"""The executor: ActionScript semantics against a sandbox, with a fake evaluator."""

import hashlib
import re

from bigfix_actionscript_runner.executor import Simulator
from bigfix_actionscript_runner.sandbox import Sandbox
from bigfix_actionscript_runner.substitution import SubstitutionError


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
    assert (sb.site / "__createfile").read_text() == "os=macOS\n"


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
    assert (sb.site / "__createfile").read_text() == "if x; then { a; }\n"
    assert (sb.site / "__appendfile").read_text() == "line1\nline2\n"


def test_unterminated_createfile_fails(tmp_path):
    _, s = sim(tmp_path)
    r = s.run("createfile until END\nabc\n")
    assert r.status == "failed"
    assert "END" in r.message


def test_copy_into_absolute_path_lands_in_the_sandbox(tmp_path):
    sb, s = sim(tmp_path)
    r = s.run('createfile until E\nx\nE\ncopy __createfile "/tmp/run scan.sh"\n')
    assert r.status == "completed"
    assert (sb.fs / "tmp" / "run scan.sh").read_text() == "x\n"


def test_copy_fails_if_destination_exists_like_bigfix(tmp_path):
    sb, s = sim(tmp_path)
    (sb.site / "a").write_text("1")
    (sb.site / "b").write_text("2")
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
    assert (sb.site / "got.txt").read_text() == "real"
    assert real.read_text() == "real"


def test_move_and_delete(tmp_path):
    sb, s = sim(tmp_path)
    (sb.site / "a").write_text("1")
    r = s.run("move a b\ndelete b\ndelete never-existed\n")
    assert r.status == "completed"
    assert not (sb.site / "a").exists() and not (sb.site / "b").exists()


def test_folder_create_and_delete(tmp_path):
    sb, s = sim(tmp_path)
    r = s.run('folder create "/opt/x y/z"\nfolder delete "/opt/x y"\nfolder delete "/never"\n')
    assert r.status == "completed"
    assert not (sb.fs / "opt" / "x y").exists()


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
    assert (sb.site / "__appendfile").read_text() == "B\n"
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
        str(sb.fs / "tmp" / "x.sh"),
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


def test_sandbox_expressions_go_to_the_sandbox_evaluator(tmp_path):
    """With a remote (SSH) evaluator, anything that looks at the sandbox is
    evaluated locally instead: the remote machine can't see it."""
    remote, local = [], []
    sb = Sandbox(tmp_path / "sb")
    s = Simulator(
        sb,
        fake_evaluator({"pathname of system folder": "C:\\Windows\\System32"}, remote),
        sandbox_evaluate=fake_evaluator({f'pathname of folder "{sb.data}"': str(sb.data)}, local),
        cache_dir=tmp_path / "cache",
    )
    r = s.run(
        'parameter "d" = "{pathname of data folder of client}"\n'
        'parameter "s" = "{pathname of system folder}"\n'
    )
    assert r.status == "completed", r.message
    assert local == [f'pathname of folder "{sb.data}"']
    assert remote == ["pathname of system folder"]
    assert s.parameters == {"d": str(sb.data), "s": "C:\\Windows\\System32"}


def test_without_a_sandbox_evaluator_everything_goes_to_the_one_evaluator(tmp_path):
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
    assert (sb.site / "got.tgz").read_bytes() == DATA
    assert r.prefetched == ["arm.tgz"]


def test_prefetch_statement_outside_block(tmp_path):
    calls = []
    sb, s = sim(tmp_path, downloader=downloader(calls))
    r = s.run(f"prefetch a.zip sha1:{SHA1} size:7 https://x/a.zip sha256:{SHA256}\n")
    assert r.status == "completed"
    assert (sb.site / "__Download" / "a.zip").read_bytes() == DATA


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
    assert (sb.site / "__Download" / "n.bin").exists()
    assert [st.outcome for st in r.steps] == ["ok", "ok"]


# --- baseline: every expression evaluated up front, then again at its step ---


def stateful(sb):
    """Answers that depend on the sandbox, like real relevance does."""
    marker = sb.site / "made.txt"

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
    made = sb.site / "made.txt"

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
