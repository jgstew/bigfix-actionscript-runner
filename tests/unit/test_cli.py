"""The command line, with the relevance evaluator replaced by a fake."""

import json
import os
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from bigfix_actionscript_runner import cli
from bigfix_actionscript_runner.host import LocalHost
from bigfix_actionscript_runner.substitution import SubstitutionError

runner = CliRunner()

TASK = """<?xml version="1.0" encoding="UTF-8"?>
<BES xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:noNamespaceSchemaLocation="BES.xsd">
    <Task>
        <Title>Demo Task</Title>
        <Description>d</Description>
        <Relevance>mac of operating system</Relevance>
        <DefaultAction ID="Action1">
            <Description><PreLink>a</PreLink><Link>b</Link><PostLink>c</PostLink></Description>
            <ActionScript MIMEType="application/x-Fixlet-Windows-Shell"><![CDATA[
parameter "logs" = "{pathname of data folder of client}/__Global/Logs"
createfile until END
hello from {name of operating system}
END
copy __createfile "{parameter "logs"}/hello.txt"
wait /bin/echo done
]]></ActionScript>
        </DefaultAction>
    </Task>
</BES>
"""


@pytest.fixture
def fake(monkeypatch):
    """Install a fake evaluator; returns the record of how it was built and called."""
    record = {"built": None, "builds": [], "calls": []}

    def build_evaluator(ssh, timeout_s, overrides, become=None):
        built = {"ssh": ssh, "timeout_s": timeout_s, "overrides": overrides}
        if become is not None:
            built["become"] = become
        record["builds"].append(built)
        record["built"] = record["builds"][0]

        def evaluate(expr):
            record["calls"].append(expr)
            if expr in overrides:
                return [overrides[expr]]
            m = re.fullmatch(r'"([^"]*)"', expr)
            if m:
                return [m.group(1)]
            m = re.fullmatch(r'pathname of folder "([^"]*)"', expr)
            if m:
                return [m.group(1)]
            answers = {"name of operating system": ["macOS"], "mac of operating system": ["True"]}
            if expr in answers:
                return answers[expr]
            raise SubstitutionError(f'The operator "{expr}" is not defined.')

        return evaluate

    monkeypatch.setattr(cli, "build_evaluator", build_evaluator)
    record["hosts"] = []

    def build_host(ssh, become=False):
        record["hosts"].append({"ssh": ssh, "become": become})
        return LocalHost()  # the "target" is this machine, so tests can look at it

    monkeypatch.setattr(cli, "build_host", build_host)
    return record


def write(tmp_path, name="t.bes", text=TASK):
    p = tmp_path / name
    p.write_text(text)
    return p


def test_simulates_a_task_into_the_sandbox(tmp_path, fake):
    sb = tmp_path / "sb"
    r = runner.invoke(cli.app, [str(write(tmp_path)), "--sandbox", str(sb)])
    assert r.exit_code == 0, r.output
    assert (
        sb / "__BESData" / "__Global" / "Logs" / "hello.txt"
    ).read_text() == "hello from macOS\n"
    assert "Demo Task" in r.output
    assert "Completed" in r.output
    assert "/bin/echo done" in r.output
    assert fake["built"]["ssh"] is None  # --local by default


def test_text_trace_shows_sandbox_paths_short(tmp_path, fake):
    sb = tmp_path / "sb"
    r = runner.invoke(cli.app, [str(write(tmp_path)), "--sandbox", str(sb)])
    steps = r.output.split("\n\n", 1)[1]  # after the header, which names the sandbox once
    assert str(sb.resolve()) not in steps
    assert "<sandbox>/__BESData/__Global/Logs/hello.txt" in steps


def test_failed_action_exits_1_and_names_the_line(tmp_path, fake):
    script = write(tmp_path, "a.bas", "delete a\ncopy {bogus} b\n")
    r = runner.invoke(cli.app, [str(script), "--sandbox", str(tmp_path / "sb")])
    assert r.exit_code == 1
    assert "Failed at line 2" in r.output
    assert "not defined" in r.output


def test_preflight_errors_stop_before_anything_runs(tmp_path, fake):
    script = write(tmp_path, "a.bas", "delete a\ncopy {pathname of (x} b\n")
    r = runner.invoke(cli.app, [str(script), "--sandbox", str(tmp_path / "sb")])
    assert r.exit_code == 3
    assert "preflight" in r.output.lower()
    assert fake["calls"] == []


def test_no_preflight_runs_anyway(tmp_path, fake):
    script = write(tmp_path, "a.bas", "delete a\ncopy {pathname of (x} b\n")
    r = runner.invoke(cli.app, [str(script), "--no-preflight", "--sandbox", str(tmp_path / "sb")])
    assert r.exit_code == 1  # qna (the fake) rejects it at run time instead


def test_json_report(tmp_path, fake):
    r = runner.invoke(cli.app, [str(write(tmp_path)), "--json", "--sandbox", str(tmp_path / "sb")])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["status"] == "completed"
    assert data["title"] == "Demo Task"
    assert data["sandbox"] == str((tmp_path / "sb").resolve())
    assert any(s["outcome"] == "shown" for s in data["steps"])
    assert "preflight" in data


def test_ssh_target_and_options_reach_the_evaluator(tmp_path, fake):
    r = runner.invoke(
        cli.app,
        [
            str(write(tmp_path)),
            "--ssh",
            "user@winbox",
            "--timeout",
            "15",
            "--set",
            "name of operating system=>Win2022",
            "--sandbox",
            str(tmp_path / "sb"),
        ],
    )
    assert fake["builds"] == [
        {
            "ssh": "user@winbox",
            "timeout_s": 15.0,
            "overrides": {"name of operating system": "Win2022"},
        },
    ]  # one evaluator: the sandbox is on the target, where qna can see it
    assert fake["hosts"] == [{"ssh": "user@winbox", "become": False}]
    assert "user@winbox" in r.output


def test_set_keeps_equals_signs_in_the_expression(tmp_path, fake):
    runner.invoke(
        cli.app,
        [
            str(write(tmp_path)),
            "--set",
            "exit code of action = 0=>True",
            "--sandbox",
            str(tmp_path / "sb"),
        ],
    )
    assert fake["built"]["overrides"] == {"exit code of action = 0": "True"}


def test_set_needs_the_arrow(tmp_path, fake):
    r = runner.invoke(cli.app, [str(write(tmp_path)), "--set", "x=1"])
    assert r.exit_code == 2


@pytest.mark.skipif(os.name == "nt", reason="runs /bin/sh")
def test_execute_with_ssh_runs_on_the_target(tmp_path, fake):
    script = write(tmp_path, "a.bas", 'wait /bin/sh -c "echo ran > proof.txt"\n')
    sb = tmp_path / "sb"
    r = runner.invoke(cli.app, [str(script), "--ssh", "u@h", "--execute", "--sandbox", str(sb)])
    assert r.exit_code == 0, r.output
    assert fake["hosts"] == [{"ssh": "u@h", "become": False}]
    # the command really ran, in the site folder (the "target" here is this machine)
    assert (sb / "site" / "proof.txt").read_text() == "ran\n"


def test_param_must_be_name_equals_value(tmp_path, fake):
    r = runner.invoke(cli.app, [str(write(tmp_path)), "--param", "novalue"])
    assert r.exit_code == 2


def test_params_are_passed_to_action_parameter_query(tmp_path, fake):
    script = write(
        tmp_path,
        "a.bas",
        'action parameter query "x" with description "d"\ncreatefile until E\n{parameter "x"}\nE\n',
    )
    sb = tmp_path / "sb"
    r = runner.invoke(cli.app, [str(script), "--param", "x=a=b", "--sandbox", str(sb)])
    assert r.exit_code == 0, r.output
    assert (sb / "site" / "__createfile").read_text() == "a=b\n"


def test_help_has_plain_on_switches(fake):
    r = runner.invoke(cli.app, ["--help"])
    assert r.exit_code == 0
    # Rich styles help in CI (GITHUB_ACTIONS / FORCE_COLOR), splitting "--execute"
    # into "-" + escape codes + "-execute"; compare the plain text
    text = re.sub(r"\x1b\[[0-9;]*m", "", r.output)
    for flag in ("--execute", "--no-download", "--no-preflight", "--force", "--json"):
        assert flag in text
    assert "--no-no-" not in text
    assert "--no-execute" not in text


def test_load_error_exits_2(tmp_path, fake):
    r = runner.invoke(cli.app, [str(write(tmp_path, "x.bes", "<BES><Analysis/></BES>"))])
    assert r.exit_code == 2


def test_default_sandbox_is_created_and_reported(tmp_path, fake, monkeypatch):
    # tempfile caches its folder, so TMPDIR alone wouldn't keep this out of the real one
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    r = runner.invoke(cli.app, [str(write(tmp_path, "a.bas", "delete a\n"))])
    assert r.exit_code == 0, r.output
    assert "Sandbox (everything; dry run):" in r.output


def test_baseline_is_on_by_default_and_reported(tmp_path, fake):
    r = runner.invoke(cli.app, [str(write(tmp_path)), "--json", "--sandbox", str(tmp_path / "sb")])
    data = json.loads(r.output)
    exprs = [b["expression"] for b in data["baseline"]]
    assert "name of operating system" in exprs
    # evaluated once for the baseline and again at its step
    assert fake["calls"].count("name of operating system") == 2


def test_no_baseline(tmp_path, fake):
    r = runner.invoke(
        cli.app,
        [str(write(tmp_path)), "--json", "--no-baseline", "--sandbox", str(tmp_path / "sb")],
    )
    assert json.loads(r.output)["baseline"] == []
    assert fake["calls"].count("name of operating system") == 1


def test_parameters_set_later_are_deferred_in_the_baseline(tmp_path, fake):
    script = write(
        tmp_path, "a.bas", 'parameter "p" = "v"\ncreatefile until E\n{parameter "p"}\nE\n'
    )
    r = runner.invoke(cli.app, [str(script), "--sandbox", str(tmp_path / "sb")])
    assert r.exit_code == 0, r.output
    assert "Baseline: 1 expression(s), 0 error(s) before line 1, 1 deferred" in r.output
    assert "-> now" not in r.output


def test_a_change_is_shown_once_not_at_every_use(tmp_path, monkeypatch):
    """An expression whose value changed is noted at its first step, not every use."""

    def build_evaluator(ssh, timeout_s, overrides):
        def evaluate(expr):
            m = re.fullmatch(r'exists file "([^"]*)" of folder "([^"]*)"', expr)
            if not m:
                raise SubstitutionError("unexpected " + expr)
            return [str((Path(m.group(2)) / m.group(1)).exists())]

        return evaluate

    monkeypatch.setattr(cli, "build_evaluator", build_evaluator)
    sb = tmp_path / "sb"
    check = '{exists file "made.txt" of folder "' + str((sb / "site").resolve()) + '"}'
    script = write(
        tmp_path,
        "a.bas",
        "createfile until E\nx\nE\ncopy __createfile made.txt\n"
        f"createfile until F\n{check}\nF\n"
        f"delete __createfile\ncreatefile until G\n{check}\nG\n",
    )
    r = runner.invoke(cli.app, [str(script), "--sandbox", str(sb)])
    assert r.exit_code == 0, r.output
    assert r.output.count('baseline "False" -> now "True"') == 1


def test_become_is_passed_to_the_ssh_evaluator_only(tmp_path, fake):
    runner.invoke(
        cli.app,
        [
            str(write(tmp_path)),
            "--ssh",
            "user@mini",
            "--become",
            "--sandbox",
            str(tmp_path / "sb"),
        ],
    )
    assert fake["builds"][0]["become"] is True
    assert fake["hosts"] == [{"ssh": "user@mini", "become": True}]


# --- sandbox modes ------------------------------------------------------------------


def test_dry_run_reports_a_full_sandbox(tmp_path, fake):
    r = runner.invoke(cli.app, [str(write(tmp_path)), "--sandbox", str(tmp_path / "sb")])
    assert "Sandbox (everything; dry run):" in r.output


def test_execute_sandboxes_only_the_client_folders(tmp_path, fake):
    real = tmp_path / "real"
    script = write(
        tmp_path,
        "a.bas",
        f'createfile until E\nx\nE\nfolder create "{real}"\ncopy __createfile "{real}/x.txt"\n',
    )
    r = runner.invoke(cli.app, [str(script), "--execute", "--sandbox", str(tmp_path / "sb")])
    assert r.exit_code == 0, r.output
    assert (real / "x.txt").read_text() == "x\n"
    assert "Sandbox (client folders only):" in r.output
    assert "really runs" in r.output


def test_no_sandbox_needs_execute(tmp_path, fake):
    r = runner.invoke(cli.app, [str(write(tmp_path)), "--no-sandbox", "--site", "S"])
    assert r.exit_code == 2
    assert "--execute" in r.output


def test_no_sandbox_needs_the_site(tmp_path, fake):
    r = runner.invoke(cli.app, [str(write(tmp_path)), "--no-sandbox", "--execute"])
    assert r.exit_code == 2
    assert "--site" in r.output


def test_no_sandbox_and_sandbox_dir_conflict(tmp_path, fake):
    r = runner.invoke(
        cli.app,
        [str(write(tmp_path)), "--no-sandbox", "--execute", "--site", "S", "--sandbox", "x"],
    )
    assert r.exit_code == 2


def test_no_sandbox_uses_the_real_client_folders(tmp_path, fake, monkeypatch):
    site = tmp_path / "BES" / "__BESData" / "My Site"
    site.mkdir(parents=True)
    fixed = {
        'pathname of client folder of site "My Site"': str(site),
        "pathname of data folder of client": str(site.parent),
    }
    script = write(tmp_path, "a.bas", "createfile until E\nx\nE\n")
    r = runner.invoke(
        cli.app,
        [str(script), "--execute", "--no-sandbox", "--site", "My Site"]
        + [a for k, v in fixed.items() for a in ("--set", f"{k}=>{v}")],
    )
    assert r.exit_code == 0, r.output
    assert (site / "__createfile").read_text() == "x\n"
    assert "No sandbox: the real client folders" in r.output


def test_no_sandbox_fails_cleanly_without_a_client(tmp_path, fake):
    r = runner.invoke(
        cli.app, [str(write(tmp_path)), "--execute", "--no-sandbox", "--site", "Nope"]
    )
    assert r.exit_code == 2
    assert "client folder" in r.output


# --- more of the command line --------------------------------------------------------


def test_build_host_is_local_without_ssh():
    assert isinstance(cli.build_host(None), LocalHost)


def test_exit_is_reported_with_its_code(tmp_path, fake):
    r = runner.invoke(
        cli.app, [str(write(tmp_path, "a.bas", "exit 3\n")), "--sandbox", str(tmp_path / "sb")]
    )
    assert r.exit_code == 0
    assert "Exited with code 3" in r.output


def test_preflight_errors_in_json(tmp_path, fake):
    script = write(tmp_path, "a.bas", "copy {pathname of (x} b\n")
    r = runner.invoke(cli.app, [str(script), "--json"])
    assert r.exit_code == 3
    data = json.loads(r.output)
    assert data["status"] == "not run" and data["sandbox"] is None
    assert data["preflight"]["errors"]


def test_an_unreachable_target_exits_2(tmp_path, fake, monkeypatch):
    from bigfix_actionscript_runner.host import HostError

    def build_host(ssh, become=False):
        raise HostError("could not connect to nowhere: Connection refused")

    monkeypatch.setattr(cli, "build_host", build_host)
    r = runner.invoke(cli.app, [str(write(tmp_path)), "--ssh", "u@nowhere"])
    assert r.exit_code == 2
    assert "Can't reach u@nowhere: could not connect" in r.output


def test_a_sandbox_that_cant_be_made_exits_2(tmp_path, fake, monkeypatch):
    from bigfix_actionscript_runner.host import HostError

    class NoTemp(LocalHost):
        def temp_dir(self, prefix):
            raise HostError("No space left on device")

    monkeypatch.setattr(cli, "build_host", lambda ssh, become=False: NoTemp())
    r = runner.invoke(cli.app, [str(write(tmp_path))])
    assert r.exit_code == 2
    assert "Can't set up the sandbox on local: No space left" in r.output


def test_the_host_is_closed_after_the_run(tmp_path, fake, monkeypatch):
    closed = []

    class Tracked(LocalHost):
        def close(self):
            closed.append(True)

    monkeypatch.setattr(cli, "build_host", lambda ssh, become=False: Tracked())
    runner.invoke(cli.app, [str(write(tmp_path)), "--sandbox", str(tmp_path / "sb")])
    assert closed == [True]


# --- from the review ---------------------------------------------------------------


def test_set_answers_expressions_as_the_action_wrote_them(tmp_path, fake):
    script = write(
        tmp_path,
        "a.bas",
        'continue if {exists file "x" of client folder of current site}\n',
    )
    r = runner.invoke(
        cli.app,
        [
            str(script),
            "--set",
            'exists file "x" of client folder of current site=>True',
            "--sandbox",
            str(tmp_path / "sb"),
        ],
    )
    assert r.exit_code == 0, r.output


def test_become_needs_ssh(tmp_path, fake):
    r = runner.invoke(cli.app, [str(write(tmp_path)), "--become"])
    assert r.exit_code == 2 and "--ssh" in r.output


def test_site_needs_no_sandbox(tmp_path, fake):
    r = runner.invoke(cli.app, [str(write(tmp_path)), "--site", "S"])
    assert r.exit_code == 2 and "--no-sandbox" in r.output


@pytest.mark.parametrize(
    "args",
    [
        ["--no-sandbox", "--site", "S"],  # usage
        ["--set", "x=1"],  # bad option value
    ],
)
def test_json_usage_errors_are_json(tmp_path, fake, args):
    r = runner.invoke(cli.app, [str(write(tmp_path)), "--json", *args])
    assert r.exit_code == 2
    data = json.loads(r.output)
    assert data["status"] == "error" and data["message"]


def test_json_load_errors_are_json(tmp_path, fake):
    r = runner.invoke(cli.app, [str(write(tmp_path, "x.bes", "<BES><Analysis/></BES>")), "--json"])
    assert r.exit_code == 2
    assert json.loads(r.output)["status"] == "error"


def test_json_connection_errors_are_json(tmp_path, fake, monkeypatch):
    from bigfix_actionscript_runner.host import HostError

    def build_host(ssh, become=False):
        raise HostError("could not connect")

    monkeypatch.setattr(cli, "build_host", build_host)
    r = runner.invoke(cli.app, [str(write(tmp_path)), "--json", "--ssh", "u@h"])
    assert r.exit_code == 2
    assert "could not connect" in json.loads(r.output)["message"]


# --- applicability relevance: only run where a client would ------------------------

NOT_RELEVANT = TASK.replace(
    "<Relevance>mac of operating system</Relevance>",
    "<Relevance>mac of operating system</Relevance>"
    "<Relevance>windows of operating system</Relevance>",
)


def test_relevant_items_run_and_their_relevance_is_shown(tmp_path, fake):
    sb = tmp_path / "sb"
    r = runner.invoke(cli.app, [str(write(tmp_path)), "--sandbox", str(sb)])
    assert r.exit_code == 0, r.output
    assert "Relevant on local" in r.output
    assert "True  <=  mac of operating system" in r.output
    assert "Completed" in r.output


def test_not_relevant_items_are_not_run(tmp_path, fake):
    sb = tmp_path / "sb"
    r = runner.invoke(
        cli.app,
        [
            str(write(tmp_path, "t.bes", NOT_RELEVANT)),
            "--set",
            "windows of operating system=>False",
            "--sandbox",
            str(sb),
        ],
    )
    assert r.exit_code == 4, r.output
    assert "Not relevant on local: not run (--force runs it anyway)" in r.output
    assert "False  <=  windows of operating system" in r.output
    assert not (sb / "__BESData" / "__Global" / "Logs" / "hello.txt").exists()
    assert "name of operating system" not in fake["calls"]  # no step was evaluated


def test_relevance_errors_count_as_not_relevant(tmp_path, fake):
    bad = TASK.replace("mac of operating system", "bogus inspector")
    r = runner.invoke(
        cli.app, [str(write(tmp_path, "t.bes", bad)), "--sandbox", str(tmp_path / "sb")]
    )
    assert r.exit_code == 4
    assert "ERROR: " in r.output and "bogus inspector" in r.output


def test_force_runs_anyway(tmp_path, fake):
    r = runner.invoke(
        cli.app,
        [
            str(write(tmp_path, "t.bes", NOT_RELEVANT)),
            "--force",
            "--set",
            "windows of operating system=>False",
            "--sandbox",
            str(tmp_path / "sb"),
        ],
    )
    assert r.exit_code == 0, r.output
    assert "Not relevant on local: running anyway (--force)" in r.output
    assert "Completed" in r.output


def test_not_relevant_in_json(tmp_path, fake):
    r = runner.invoke(
        cli.app,
        [
            str(write(tmp_path, "t.bes", NOT_RELEVANT)),
            "--json",
            "--set",
            "windows of operating system=>False",
        ],
    )
    assert r.exit_code == 4
    data = json.loads(r.output)
    assert data["status"] == "not relevant"
    assert data["relevant"] is False
    assert data["relevance"][1] == {"relevance": "windows of operating system", "result": "False"}


def test_a_temporary_sandbox_is_removed_when_nothing_runs(tmp_path, fake, monkeypatch):
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    r = runner.invoke(
        cli.app,
        [
            str(write(tmp_path, "t.bes", NOT_RELEVANT)),
            "--set",
            "windows of operating system=>False",
        ],
    )
    assert r.exit_code == 4
    assert not list(tmp_path.glob("bigfix-actionscript-runner-*"))


def test_relevance_sees_the_action_context(tmp_path, fake):
    """Relevance about the site's own files is asked about the sandbox's site folder."""
    rel = TASK.replace(
        "<Relevance>mac of operating system</Relevance>",
        '<Relevance>not exists file "done.txt" of client folder of current site</Relevance>',
    )
    sb = tmp_path / "sb"
    r = runner.invoke(
        cli.app,
        [
            str(write(tmp_path, "t.bes", rel)),
            "--set",
            'not exists file "done.txt" of client folder of current site=>True',
            "--sandbox",
            str(sb),
        ],
    )
    assert r.exit_code == 0, r.output


def test_plain_actionscript_has_no_relevance_to_check(tmp_path, fake):
    r = runner.invoke(
        cli.app, [str(write(tmp_path, "a.bas", "delete a\n")), "--sandbox", str(tmp_path / "sb")]
    )
    assert r.exit_code == 0
    assert "Relevant" not in r.output and "Not relevant" not in r.output
