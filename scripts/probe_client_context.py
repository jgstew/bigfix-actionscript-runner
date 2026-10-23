"""Probe which client inspectors qna can't evaluate: the evidence behind client_context.

qna is a client relevance evaluator without a running BES Client behind it, so
an inspector that needs the client's runtime state (an action, the site being
evaluated, the download folder of an action) fails with "No inspector context"
rather than answering. The analyzer's inspector table can't say which ones: to
it they are client inspectors like any other. This script asks qna on real
targets, one probe per inspector, and writes what each said to a dump that
`tests/unit/test_client_context.py` checks the curated tables against.

Probed: every client property with no direct object (`parameter <string>`,
`active action`), plus the properties of the objects only a running client
has (`<action>`, `<client>`, `<site>`, `<action lock state>`), each reached
through the global that produces it. A target is first asked its OS, then
probed only for the inspectors the analyzer records on that platform (an
inspector it doesn't define would only answer "not defined"). Index arguments are a placeholder string or
integer; signatures with any other index type are skipped. Relevance is
read-only, so probing changes nothing on a target.

Targets come from the bigfix-remote-client-relevance inventory
(~/.bigfix/remote_clients.toml): by default every `ssh` and `container` host
in it, all in one pass. `local` hosts need sudo here, `online_evaluator` would
send the probes to a third party and `fastquery` reaches real clients, not qna,
so those run only when named with --host. Dumps are named, and headed, by the
OS qna reports, never by host name, so they can be committed.

    uv run python scripts/probe_client_context.py                  # the whole inventory
    uv run python scripts/probe_client_context.py --host debian-12 --host <WINDOWS_HOST>
"""

import argparse
import asyncio
import dataclasses
import logging
import re
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from bigfix_relevance_analyzer import inspectors
from bigfix_remote_client_relevance import (
    ClientRelevanceResult,
    Target,
    evaluate_many,
    evaluate_many_stream,
    find_inventory_path,
    load_inventory,
)

log = logging.getLogger("probe_client_context")

DUMP_DIR = Path(__file__).resolve().parent.parent / "tests" / "data" / "client_context"
DEFAULT_KINDS = frozenset({"ssh", "container"})

# how a probe reaches an object of each type; each path is probed itself too, and
# when qna has no such object (no context, or it doesn't exist) every property
# probed through it is recorded "no-object": the property can't be told apart
# from its object there, but qna can't answer it either
OBJECT_PATHS = {
    "action": "active action",
    "client": "client",
    "site": "current site",
    "action lock state": "action lock state",
}
# which of the analyzer's inspector dumps a target's qna is probed against: an
# inspector no dump for its platform defines would only answer "not defined"
PLATFORM_SOURCES = {
    "windows": frozenset({"client:windows"}),
    "macos": frozenset({"client:macos"}),
    "linux": frozenset({"client:debian", "client:ubuntu", "client:rhel"}),
}
INDEX_PLACEHOLDERS = {"string": '"bfasr-probe"', "integer": "0"}
NO_CONTEXT = "no inspector context"
# what names a dump: qna's own version (an SSH result doesn't otherwise carry
# it) and the OS and architecture it reports
VERSION_PROBE = "version of client as string"
OS_PROBE = "name of operating system as string"
ARCH_PROBE = "architecture of operating system as string"


def probe_expression(entry: inspectors.Inspector) -> str | None:
    """`exists (<name> [index] [of <object>])`, or None when it can't be written."""
    phrase = entry.name
    if entry.index_type is not None:
        placeholder = INDEX_PLACEHOLDERS.get(entry.index_type)
        if placeholder is None:
            return None
        phrase += " " + placeholder
    if entry.operands:
        path = OBJECT_PATHS.get(entry.operands[0])
        if path is None:
            return None
        phrase += f" of {path}"
    # `exists` keeps a plural or a nonexistent singular from erroring for
    # reasons that have nothing to do with context
    return f"exists ({phrase})"


def platform_of(os_name: str) -> str:
    """The PLATFORM_SOURCES key for what `name of operating system` answered."""
    name = os_name.lower()
    return "windows" if name.startswith("win") else "macos" if name.startswith("mac") else "linux"


def candidates(platform: str) -> list[tuple[str, str, str | None]]:
    """(signature, probe, object path or None) for each probe-able inspector on `platform`."""
    sources = PLATFORM_SOURCES[platform]
    out: dict[str, tuple[str, str | None]] = {}
    for entry in inspectors.properties():
        if not sources & entry.sources:
            continue
        if entry.operands and entry.operands[0] not in OBJECT_PATHS:
            continue
        expr = probe_expression(entry)
        if expr is not None:
            path = OBJECT_PATHS[entry.operands[0]] if entry.operands else None
            out.setdefault(entry.signature, (expr, path))
    return [(sig, expr, path) for sig, (expr, path) in sorted(out.items())]


def outcome(error: str | None, error_kind: str | None) -> str:
    if error is None:
        return "answers"
    if NO_CONTEXT in error.lower():
        return "no-context"
    # a parse or type error, a nonexistent object, ...: qna has the inspector
    return "error" if error_kind == "relevance" else f"{error_kind or 'error'}-failure"


def select_targets(hosts: list[str]) -> list[Target]:
    path = find_inventory_path()
    if path is None:
        raise SystemExit(
            "no remote_clients.toml inventory found (see bigfix-remote-client-relevance)"
        )
    inventory = load_inventory(path)
    if not hosts:
        return [t for t in inventory if t.kind in DEFAULT_KINDS]
    by_name = {t.name: t for t in inventory}
    missing = [h for h in hosts if h not in by_name]
    if missing:
        raise SystemExit(f"not in {path}: {', '.join(missing)}")
    return [by_name[h] for h in hosts]


def _first(result: ClientRelevanceResult | None) -> str | None:
    return result.answers[0] if result is not None and result.answers else None


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def write_dump(
    results: dict[str, ClientRelevanceResult],
    probes: list[tuple[str, str, str | None]],
    path_probes: dict[str, str],
    out_dir: Path,
    index: int,
    taken: set[str],
) -> tuple[Path, dict[str, int]] | None:
    """One target's dump, or None when nothing could be evaluated there."""
    if all(r.error and r.error_kind != "relevance" for r in results.values()):
        first = next(iter(results.values()))
        log.error("%s: every probe failed to run: %s", first.host, first.error)
        return None
    absent: dict[str, str] = {}
    for path, expr in path_probes.items():
        r = results[expr]
        if r.error or r.answers != ["True"]:
            absent[path] = r.error.splitlines()[0] if r.error else "doesn't exist"
    os_name = _first(results.get(OS_PROBE)) or f"unknown-{index}"
    arch = _first(results.get(ARCH_PROBE)) or "unknown"
    version = _first(results.get(VERSION_PROBE)) or "unknown"
    lines = [
        f"# qna {version} on {os_name} ({arch}), {datetime.now(UTC):%Y-%m-%d}",
        "# generated by scripts/probe_client_context.py; do not edit by hand",
        "# signature\tprobe\toutcome\tdetail",
    ]
    counts: dict[str, int] = defaultdict(int)
    for signature, expr, path in probes:
        r = results[expr]
        kind = outcome(r.error, r.error_kind)
        detail = r.error.splitlines()[0] if r.error else ", ".join(r.answers)
        if path in absent and kind != "no-context":
            kind, detail = "no-object", f"{path}: {absent[path]}"
        counts[kind] += 1
        lines.append(f"{signature}\t{expr}\t{kind}\t{detail}")
    name = f"qna_{_slug(os_name)}_{_slug(arch)}_{_slug(version)}"
    if name in taken:  # two targets reporting the same OS: keep both
        name += f"_{index}"
    taken.add(name)
    out = out_dir / f"{name}.tsv"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out, counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--host",
        action="append",
        default=[],
        help="inventory host to probe (repeatable; default: every ssh and container host)",
    )
    parser.add_argument("--timeout", type=float, default=30.0, help="seconds per probe")
    parser.add_argument(
        "--qna-version", help="qna version spec for every target, over the inventory's"
    )
    parser.add_argument("--out-dir", type=Path, default=DUMP_DIR, help="where the dumps go")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    targets = select_targets(args.host)
    if args.qna_version:
        targets = [dataclasses.replace(t, qna_version=args.qna_version) for t in targets]
    args.out_dir.mkdir(parents=True, exist_ok=True)
    return asyncio.run(_probe_all(targets, args))


async def _probe_all(targets: list[Target], args: argparse.Namespace) -> int:
    # one cheap question first, so each target is probed only against its own
    # platform's inspectors; then every platform's targets at once
    found = await evaluate_many([OS_PROBE], targets, timeout_s=args.timeout)
    by_platform: dict[str, list[Target]] = defaultdict(list)
    failed = 0
    for target, r in zip(targets, found, strict=True):
        os_name = _first(r)
        if os_name is None:
            log.error("%s: can't tell its OS: %s", target.name, r.error)
            failed += 1
            continue
        by_platform[platform_of(os_name)].append(target)
    taken: set[str] = set()
    started = time.monotonic()
    groups = [
        _probe(platform, group, taken, started, args) for platform, group in by_platform.items()
    ]
    return 1 if failed or any(await asyncio.gather(*groups)) else 0


async def _probe(
    platform: str,
    targets: list[Target],
    taken: set[str],
    started: float,
    args: argparse.Namespace,
) -> int:
    probes = candidates(platform)
    path_probes = {path: f"exists ({path})" for path in OBJECT_PATHS.values()}
    exprs = sorted({expr for _, expr, _ in probes} | set(path_probes.values()))
    exprs += [VERSION_PROBE, OS_PROBE, ARCH_PROBE]
    log.info("%s: %d inspectors on %d target(s)", platform, len(probes), len(targets))
    # each target keeps one connection or container for all its probes, and its
    # dump is written as soon as it is done, so a slow (e.g. emulated) target
    # holds up only itself
    per_target: dict[str, dict[str, ClientRelevanceResult]] = defaultdict(dict)
    failed = 0
    stream = evaluate_many_stream(exprs, targets, max_parallel=len(targets), timeout_s=args.timeout)
    async for r in stream:
        by_expr = per_target[r.host]
        by_expr[r.client_relevance] = r
        if len(by_expr) % 100 == 0:
            log.info("%s: %d/%d", r.host, len(by_expr), len(exprs))
        if len(by_expr) < len(exprs):
            continue
        written = write_dump(by_expr, probes, path_probes, args.out_dir, len(taken) + 1, taken)
        if written is None:
            failed += 1
            continue
        out, counts = written
        summary = ", ".join(f"{k}: {v}" for k, v in sorted(counts.items()))
        log.info(
            "%s done in %.0fs: %s -> %s", r.host, time.monotonic() - started, summary, out.name
        )
    return failed


if __name__ == "__main__":
    sys.exit(main())
