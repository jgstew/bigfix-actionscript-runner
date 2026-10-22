"""bigfix-actionscript-runner: dry-run the ActionScript in a .bes (or plain) file."""

import json
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Annotated

import platformdirs
import typer

from .executor import Report, Simulator
from .load import LoadedAction, LoadError, load_action
from .prefetch import AUTO
from .preflight import PreflightResult, preflight
from .sandbox import Sandbox
from .substitution import Evaluator, SubstitutionError

app = typer.Typer(add_completion=False, help=__doc__)

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_PREFLIGHT = 0, 1, 2, 3


def build_evaluator(
    ssh: str | None, timeout_s: float, overrides: dict[str, str], become: bool | None = None
) -> Evaluator:
    """The real evaluator: bigfix-remote-client-relevance, --local or over SSH."""
    from .evaluate import QnaEvaluator, make_target

    return QnaEvaluator(make_target(ssh, become), timeout_s=timeout_s, overrides=overrides)


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


def _relevance(action: LoadedAction, evaluate: Evaluator) -> list[dict[str, str]]:
    results = []
    for clause in action.relevance:
        try:
            answers = evaluate(clause)
            results.append({"relevance": clause, "result": ", ".join(answers)})
        except SubstitutionError as e:
            results.append({"relevance": clause, "error": str(e)})
    return results


def _print_text(
    action: LoadedAction,
    target: str,
    sandbox: Sandbox,
    pre: PreflightResult | None,
    relevance: list[dict[str, str]] | None,
    report: Report | None,
) -> None:
    print(f"{action.title}" + (f"  [{action.action_id}]" if action.action_id else ""))
    print(f"Relevance evaluated on: {target}")
    print(f"Sandbox: {sandbox.root}")
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
        if any(r.get("result", "").lower() != "true" for r in relevance):
            print(
                f"Not relevant on {target}: a client would not run this action "
                "(simulating it anyway)"
            )
    if report is None:
        return
    if report.baseline:
        errors = sum(1 for b in report.baseline if b.error)
        deferred = sum(1 for b in report.baseline if b.deferred)
        print(
            f"Baseline: {len(report.baseline)} expression(s), {errors} error(s) before line 1"
            + (f", {deferred} deferred (use parameters set later)" if deferred else "")
        )
    print()
    root = str(sandbox.root)

    def short(text: str) -> str:  # the header names the sandbox once; steps say <sandbox>
        return text.replace(root, "<sandbox>")

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
        str | None, typer.Option(help="Evaluate relevance on USER@HOST over SSH instead of --local")
    ] = None,
    become: Annotated[
        bool,
        typer.Option(
            "--become",
            help="Run qna under sudo on the --ssh target (needed for macOS targets)",
        ),
    ] = False,
    execute: Annotated[
        bool,
        typer.Option(
            "--execute", help="Really run run/wait/... commands (from the sandbox site folder)"
        ),
    ] = False,
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
        Path | None, typer.Option("--sandbox", help="Sandbox folder (default: a new temp folder)")
    ] = None,
    no_preflight: Annotated[
        bool, typer.Option("--no-preflight", help="Skip the bigfix-relevance-analyzer preflight")
    ] = False,
    relevance: Annotated[
        bool, typer.Option("--relevance", help="Also evaluate the item's own relevance first")
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
    if execute and ssh:
        print(
            "--execute can't be combined with --ssh: commands would run here, "
            "while relevance comes from the remote machine."
        )
        raise typer.Exit(EXIT_USAGE)
    params = _params(param or [])
    overrides = _overrides(set_ or [])
    try:
        action = load_action(path, action_id)
    except LoadError as e:
        print(f"Can't load {path}: {e}")
        raise typer.Exit(EXIT_USAGE) from e

    pre = None if no_preflight else preflight(path)
    sandbox = Sandbox(sandbox_dir or tempfile.mkdtemp(prefix="bigfix-actionscript-runner-"))
    target = ssh or "local"
    if pre is not None and pre.errors:
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
    rel = _relevance(action, evaluate) if relevance else None
    simulator = Simulator(
        sandbox,
        evaluate,
        cache_dir=Path(platformdirs.user_cache_dir("bigfix-actionscript-runner")) / "prefetch",
        params=params,
        execute=execute,
        assumed_exit_code=exit_code,
        downloader=None if no_download else AUTO,
        sandbox_evaluate=build_evaluator(None, timeout, overrides) if ssh else None,
        baseline=not no_baseline,
    )
    report = simulator.run(action.script, first_line=action.first_line)
    if as_json:
        print(json.dumps(_payload(action, target, sandbox, pre, rel, report), indent=2))
    else:
        _print_text(action, target, sandbox, pre, rel, report)
    raise typer.Exit(EXIT_FAILED if report.status == "failed" else EXIT_OK)


def _payload(
    action: LoadedAction,
    target: str,
    sandbox: Sandbox,
    pre: PreflightResult | None,
    relevance: list[dict[str, str]] | None,
    report: Report | None,
) -> dict[str, object]:
    return {
        "title": action.title,
        "action": action.action_id,
        "target": target,
        "sandbox": str(sandbox.root),
        "preflight": asdict(pre) if pre is not None else None,
        "relevance": relevance,
        "status": report.status if report else "not run",
        "failed_line": report.failed_line if report else None,
        "message": report.message if report else "",
        "exit_code": report.exit_code if report else None,
        "prefetched": report.prefetched if report else [],
        "baseline": [asdict(b) for b in report.baseline] if report else [],
        "steps": [asdict(s) for s in report.steps] if report else [],
    }


def main() -> None:
    app()
