"""bigfix-actionscript-runner: dry-run the ActionScript in a .bes (or plain) file."""

import json
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, NoReturn

import platformdirs
import typer

from .executor import Report, Simulator
from .host import Host, HostError, LocalHost
from .load import LoadedAction, LoadError, load_action
from .prefetch import AUTO
from .preflight import PreflightResult, preflight
from .sandbox import Sandbox
from .substitution import Evaluator, SubstitutionError

app = typer.Typer(add_completion=False, help=__doc__)

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_PREFLIGHT, EXIT_NOT_RELEVANT = 0, 1, 2, 3, 4


def build_evaluator(
    ssh: str | None, timeout_s: float, overrides: dict[str, str], become: bool | None = None
) -> Evaluator:
    """The real evaluator: bigfix-remote-client-relevance, --local or over SSH."""
    from .evaluate import QnaEvaluator, make_target

    return QnaEvaluator(make_target(ssh, become), timeout_s=timeout_s, overrides=overrides)


def build_host(ssh: str | None, become: bool = False) -> Host:
    """Where the action runs: this machine, or the --ssh target."""
    if ssh is None:
        return LocalHost()
    from .ssh_host import SSHHost

    return SSHHost(ssh, become=become)


def _params(values: list[str]) -> dict[str, str]:
    out = {}
    for v in values:
        name, sep, value = v.partition("=")
        if not sep or not name:
            raise typer.BadParameter(f"expected NAME=VALUE, got {v!r}", param_hint="--param")
        out[name] = value
    return out


def _overrides(values: list[str]) -> dict[str, str]:
    out = {}
    for v in values:
        expr, sep, value = v.rpartition("=>")
        if not sep or not expr.strip():
            raise typer.BadParameter(f"expected 'EXPR=>VALUE', got {v!r}", param_hint="--set")
        out[expr.strip()] = value.strip()
    return out


def _sandbox_line(sandbox: Sandbox) -> str:
    if sandbox.mode == "off":
        return f"No sandbox: the real client folders ({sandbox.site}, {sandbox.data})"
    label = "everything; dry run" if sandbox.mode == "full" else "client folders only"
    return f"Sandbox ({label}): {sandbox.root}"


def _print_text(
    action: LoadedAction,
    target: str,
    sandbox: Sandbox | None,
    pre: PreflightResult | None,
    relevance: list[dict[str, str]] | None,
    report: Report | None,
    force: bool = False,
) -> None:
    print(f"{action.title}" + (f"  [{action.action_id}]" if action.action_id else ""))
    print(f"Target: {target}")
    if sandbox is not None:
        print(_sandbox_line(sandbox))
    if sandbox is not None and sandbox.mode != "full":
        print(f"--execute: this really runs on {target}; files outside the sandbox change")
    if pre is not None:
        print(
            f"Preflight: {pre.checked} relevance expression(s) checked, "
            f"{len(pre.errors)} error(s), {len(pre.warnings)} warning(s)"
        )
        for issue in pre.errors + pre.warnings:
            print(
                f"  line {issue.line}: {issue.severity} [{issue.code} {issue.source}] {issue.message}"
            )
    if relevance:
        print("Relevance:")
        for r in relevance:
            print(f"  {r.get('result', 'ERROR: ' + r.get('error', ''))}  <=  {r['relevance']}")
        if all_true(relevance):
            print(f"Relevant on {target}")
        elif force:
            print(f"Not relevant on {target}: running anyway (--force)")
        else:
            print(f"Not relevant on {target}: not run (--force runs it anyway)")
    if report is None or sandbox is None:
        return
    if report.baseline:
        errors = sum(1 for b in report.baseline if b.error)
        deferred = sum(1 for b in report.baseline if b.deferred)
        print(
            f"Baseline: {len(report.baseline)} expression(s), {errors} error(s) before line 1"
            + (f", {deferred} deferred (use parameters set later)" if deferred else "")
        )
    print()
    root = sandbox.root

    def short(text: str) -> str:  # the header names the sandbox once; steps say <sandbox>
        return text.replace(root, "<sandbox>") if root else text

    shown: set[str] = set()  # each change since the baseline is noted once, where it first shows
    for s in report.steps:
        print(f"{s.line:5}  {s.outcome:10} {s.text}")
        if s.substituted is not None and s.substituted != s.text:
            print(f"{'':17}-> {short(s.substituted)}")
        if s.detail and not s.text.lower().startswith("parameter "):
            print(f"{'':17}   {short(s.detail)}")
        for ev in s.evaluations:
            if ev.changed and ev.expression not in shown:
                shown.add(ev.expression)
                before = (
                    f"error ({ev.baseline_error})"
                    if ev.baseline_error
                    else f'"{ev.baseline_value}"'
                )
                now = f"error ({ev.error})" if ev.error else f'"{ev.value}"'
                print(
                    f"{'':17}   {{{ev.expression}}}: baseline {short(before)} -> now {short(now)}"
                )
    print()
    if report.status == "completed":
        print("Completed")
    elif report.status == "exited":
        print(f"Exited with code {report.exit_code}")
    else:
        print(f"Failed at line {report.failed_line}: {report.message}")


@app.command()
def simulate(
    path: Annotated[
        Path, typer.Argument(exists=True, dir_okay=False, help=".bes file or plain ActionScript")
    ],
    action_id: Annotated[
        str | None, typer.Option("--action", help="Action ID (default: DefaultAction)")
    ] = None,
    ssh: Annotated[
        str | None,
        typer.Option(help="Run on USER@HOST over SSH (relevance, files, commands) instead of here"),
    ] = None,
    become: Annotated[
        bool,
        typer.Option(
            "--become",
            help=(
                "On a POSIX --ssh target, run qna, file commands and commands under sudo -n "
                "(like a client, as root; macOS qna needs it)"
            ),
        ),
    ] = False,
    execute: Annotated[
        bool,
        typer.Option(
            "--execute",
            help=(
                "Really run the action: commands run, and paths outside the client "
                "folders are the real ones"
            ),
        ),
    ] = False,
    no_sandbox: Annotated[
        bool,
        typer.Option(
            "--no-sandbox",
            help="With --execute: use the real client's folders for --site too",
        ),
    ] = False,
    site: Annotated[
        str | None,
        typer.Option(help="With --no-sandbox: the site whose client folder is the current site"),
    ] = None,
    param: Annotated[
        list[str] | None,
        typer.Option(help="Value for action parameter query: NAME=VALUE (repeatable)"),
    ] = None,
    set_: Annotated[
        list[str] | None,
        typer.Option("--set", help="Fixed answer for an expression: 'EXPR=>VALUE' (repeatable)"),
    ] = None,
    exit_code: Annotated[
        int, typer.Option(help="Exit code assumed for wait commands that aren't run")
    ] = 0,
    no_download: Annotated[
        bool,
        typer.Option("--no-download", help="Placeholder files instead of downloading prefetches"),
    ] = False,
    sandbox_dir: Annotated[
        str | None,
        typer.Option(
            "--sandbox", help="Sandbox folder on the target (default: a new temp folder there)"
        ),
    ] = None,
    no_preflight: Annotated[
        bool, typer.Option("--no-preflight", help="Skip the bigfix-relevance-analyzer preflight")
    ] = False,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help=(
                "Run even when the item's relevance isn't True on the target "
                "(e.g. to run it again while testing)"
            ),
        ),
    ] = False,
    no_baseline: Annotated[
        bool,
        typer.Option(
            "--no-baseline",
            help="Don't evaluate every expression up front (each is still evaluated at its step)",
        ),
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Print a JSON report")] = False,
    timeout: Annotated[float, typer.Option(help="Seconds per relevance evaluation")] = 60.0,
) -> None:
    """Simulate one action: real relevance substitution, file commands in a sandbox."""
    problem = _usage_problem(execute, no_sandbox, site, sandbox_dir, ssh, become)
    if problem:
        _fail(as_json, problem)
    try:
        params = _params(param or [])
        overrides = _overrides(set_ or [])
    except typer.BadParameter as e:
        _fail(as_json, f"{e.param_hint}: {e.message}")
    try:
        action = load_action(path, action_id)
    except LoadError as e:
        _fail(as_json, f"Can't load {path}: {e}")

    pre = None if no_preflight else preflight(path)
    target = ssh or "local"
    if pre is not None and pre.errors:
        sandbox = None
        if as_json:
            print(json.dumps(_payload(action, target, sandbox, pre, None, None), indent=2))
        else:
            _print_text(action, target, sandbox, pre, None, None)
            print("\nPreflight found errors; nothing was run (use --no-preflight to run anyway).")
        raise typer.Exit(EXIT_PREFLIGHT)

    evaluate = (
        build_evaluator(ssh, timeout, overrides, become=True)
        if ssh and become
        else build_evaluator(ssh, timeout, overrides)
    )
    try:
        host = build_host(ssh, become)
    except HostError as e:
        _fail(as_json, f"Can't reach {target}: {e}")
    try:
        sandbox = _make_sandbox(
            host, evaluate, execute, no_sandbox, site, sandbox_dir, target, as_json
        )
        simulator = Simulator(
            sandbox,
            evaluate,
            cache_dir=Path(platformdirs.user_cache_dir("bigfix-actionscript-runner")) / "prefetch",
            params=params,
            execute=execute,
            assumed_exit_code=exit_code,
            downloader=None if no_download else AUTO,
            baseline=not no_baseline,
            overrides=overrides,
        )
        # like a client: the action only runs where the item is relevant
        rel = simulator.evaluate_relevance(action.relevance) if action.relevance else None
        report = None
        if rel is None or all_true(rel) or force:
            report = simulator.run(action.script, first_line=action.first_line)
        elif sandbox_dir is None and sandbox.root is not None:
            host.delete_tree(sandbox.root)  # a temporary sandbox nothing ran in
    finally:
        host.close()
    if as_json:
        print(json.dumps(_payload(action, target, sandbox, pre, rel, report), indent=2))
    else:
        _print_text(action, target, sandbox, pre, rel, report, force)
    if report is None:
        raise typer.Exit(EXIT_NOT_RELEVANT)
    raise typer.Exit(EXIT_FAILED if report.status == "failed" else EXIT_OK)


def all_true(relevance: list[dict[str, str]]) -> bool:
    """Every clause answered True, as a client requires before it runs an action."""
    return all(r.get("result", "").lower() == "true" for r in relevance)


def _usage_problem(
    execute: bool,
    no_sandbox: bool,
    site: str | None,
    sandbox_dir: str | None,
    ssh: str | None,
    become: bool,
) -> str | None:
    if no_sandbox and not execute:
        return "--no-sandbox needs --execute: a dry run never touches the real client folders."
    if no_sandbox and not site:
        return "--no-sandbox needs --site NAME: whose client folder is the current site's."
    if no_sandbox and sandbox_dir:
        return "--no-sandbox and --sandbox contradict each other."
    if site and not no_sandbox:
        return "--site only means something with --no-sandbox."
    if become and not ssh:
        return "--become is for --ssh targets; run this tool itself under sudo for a local run."
    return None


def _fail(as_json: bool, message: str, code: int = EXIT_USAGE) -> NoReturn:
    """Stop with `message`: as a JSON document with --json, so stdout stays parseable."""
    if as_json:
        print(json.dumps({"status": "error", "message": message}, indent=2))
    else:
        print(message)
    raise typer.Exit(code)


def _make_sandbox(
    host: Host,
    evaluate: Evaluator,
    execute: bool,
    no_sandbox: bool,
    site: str | None,
    sandbox_dir: str | None,
    target: str,
    as_json: bool,
) -> Sandbox:
    try:
        if no_sandbox:
            # straight to qna: the action context rewrite would point these at a sandbox
            try:
                site_folder = evaluate(f'pathname of client folder of site "{site}"')[0]
                data_folder = evaluate("pathname of data folder of client")[0]
            except SubstitutionError as e:
                _fail(as_json, f'No real client folder for site "{site}" on {target}: {e}')
            return Sandbox(host=host, mode="off", site_folder=site_folder, data_folder=data_folder)
        root = sandbox_dir or host.temp_dir("bigfix-actionscript-runner-")
        return Sandbox(root, host=host, mode="client" if execute else "full")
    except HostError as e:
        _fail(as_json, f"Can't set up the sandbox on {target}: {e}")


def _payload(
    action: LoadedAction,
    target: str,
    sandbox: Sandbox | None,
    pre: PreflightResult | None,
    relevance: list[dict[str, str]] | None,
    report: Report | None,
) -> dict[str, object]:
    return {
        "title": action.title,
        "action": action.action_id,
        "target": target,
        "sandbox": sandbox.root if sandbox else None,
        "sandbox_mode": sandbox.mode if sandbox else None,
        "preflight": asdict(pre) if pre is not None else None,
        "relevance": relevance,
        "relevant": all_true(relevance) if relevance is not None else None,
        "status": report.status
        if report
        else "not relevant"
        if relevance is not None and not all_true(relevance)
        else "not run",
        "failed_line": report.failed_line if report else None,
        "message": report.message if report else "",
        "exit_code": report.exit_code if report else None,
        "staged": report.staged if report else [],
        "prefetched": report.prefetched if report else [],
        "baseline": [asdict(b) for b in report.baseline] if report else [],
        "steps": [asdict(s) for s in report.steps] if report else [],
    }


def main() -> None:
    app()
