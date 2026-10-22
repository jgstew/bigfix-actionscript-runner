"""Run ActionScript line by line on a host, through a sandbox.

Relevance substitution is real (through the evaluator); file commands act on
the host, at the paths the sandbox maps them to; run/wait-style commands are
shown, or run on the host with `execute=True`. Prefetch statements are handled
in a pre-pass before line 1, like a client.
"""

import hashlib
import re
import shlex
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .host import HostError
from .prefetch import AUTO, Downloader, PrefetchError, PrefetchItem, fetch, parse_prefetch
from .rewrite import ActionContext, rewrite
from .sandbox import Sandbox, SandboxEscape
from .substitution import Evaluator, SubstitutionError, find_expressions, substitute

Runner = Callable[..., int]


class LineError(Exception):
    """The current line fails; the action stops (like a failed command on a client)."""


@dataclass
class Evaluation:
    """One {expression} evaluated at its step, next to its baseline (if any)."""

    expression: str
    value: str | None = None
    error: str | None = None
    baseline_value: str | None = None
    baseline_error: str | None = None
    changed: bool = False


@dataclass
class BaselineEntry:
    """An {expression} evaluated once before line 1, in the action's starting state."""

    line: int
    expression: str
    value: str | None = None
    error: str | None = None
    deferred: bool = False
    """Uses a parameter the action sets later, so it can't be evaluated up front."""


@dataclass
class Step:
    line: int
    text: str
    outcome: str  # ok | shown | skipped | noop | warning | prefetched | failed
    substituted: str | None = None
    detail: str = ""
    evaluations: list[Evaluation] = field(default_factory=list)


@dataclass
class Report:
    status: str = "completed"  # completed | failed | exited
    failed_line: int | None = None
    message: str = ""
    exit_code: int | None = None
    steps: list[Step] = field(default_factory=list)
    prefetched: list[str] = field(default_factory=list)
    baseline: list[BaselineEntry] = field(default_factory=list)


def split_args(text: str) -> list[str]:
    """ActionScript arguments: "quoted strings" (quotes removed) or bare words."""
    return [
        m.group(1) if m.group(1) is not None else m.group(2)
        for m in re.finditer(r'"([^"]*)"|(\S+)', text)
    ]


_WAITS = {
    "wait": True,
    "waithidden": True,
    "dos": True,
    "script": True,
    "run": False,
    "runhidden": False,
    "rundetached": False,
}
_NOOP_PREFIXES = (
    "action requires restart",
    "action may require restart",
    "action requires login",
    "action uses wow64 redirection",
    "action launch preference",
    "setting ",
    "setting delete",
    "regset ",
    "regset64 ",
    "regdelete ",
    "regdelete64 ",
    "notify client",
    "utility ",
    "relay select",
    "restart",
    "shutdown",
    "action parameter query",  # query handled first
)


def _starts(low: str, keyword: str) -> bool:
    """`low` starts with the keyword followed by whitespace (a space or a tab)."""
    return low.startswith(keyword) and low[len(keyword) : len(keyword) + 1] in (" ", "\t")


def _after(text: str, keyword: str) -> str:
    return text[len(keyword) :].strip()


@dataclass
class _Frame:
    active: bool
    taken: bool
    parent_active: bool


class Simulator:
    def __init__(
        self,
        sandbox: Sandbox,
        evaluate: Evaluator,
        *,
        cache_dir: Path,
        params: dict[str, str] | None = None,
        execute: bool = False,
        assumed_exit_code: int = 0,
        action_id: int = 1,
        runner: Runner | None = None,
        downloader: Downloader | None = AUTO,
        baseline: bool = False,
        overrides: dict[str, str] | None = None,
    ) -> None:
        if not execute and sandbox.mode != "full":
            raise ValueError(
                f"a {sandbox.mode!r} sandbox lets real paths through: that needs --execute"
            )
        self.sandbox = sandbox
        self.host = sandbox.host
        self._evaluate = evaluate
        self.cache_dir = cache_dir
        self.given_params = dict(params or {})
        self.parameters: dict[str, str] = {}
        self.execute = execute
        self.exit_code = assumed_exit_code
        self.assumed_exit_code = assumed_exit_code
        self.action_id = action_id
        self.runner: Runner = runner if runner is not None else self.host.run
        self.downloader = downloader
        self._override: dict[str, str] | None = None
        # --set answers, matched against the expression as the action wrote it
        self.overrides = dict(overrides or {})
        self.baseline = baseline
        self._baseline: dict[str, BaselineEntry] = {}
        self._pending: list[Evaluation] = []

    # -- relevance ------------------------------------------------------------

    def _context(self) -> ActionContext:
        return ActionContext(
            parameters=self.parameters,
            action_id=self.action_id,
            exit_code=self.exit_code,
            site_folder=self.sandbox.site,
            data_folder=self.sandbox.data,
        )

    def _eval(self, expr: str) -> list[str]:
        if expr.strip() in self.overrides:
            return [self.overrides[expr.strip()]]
        return self._evaluate(rewrite(expr, self._context()))

    def _eval_recorded(self, expr: str) -> list[str]:
        """Evaluate at this step (state may have changed) and record it for the step."""
        ev = Evaluation(expr)
        base = self._baseline.get(expr)
        if base is not None:
            ev.baseline_value, ev.baseline_error = base.value, base.error
        try:
            answers = self._eval(expr)
            ev.value = answers[0] if len(answers) == 1 else ", ".join(answers)
        except SubstitutionError as e:
            ev.error = str(e)
            raise
        finally:
            if base is not None and not base.deferred:
                ev.changed = (ev.error is None) != (base.error is None) or (
                    ev.error is None and ev.value != base.value
                )
            self._pending.append(ev)
        return answers

    def _sub(self, text: str) -> str:
        try:
            return substitute(text, self._eval_recorded)
        except SubstitutionError as e:
            raise LineError(f"relevance substitution failed: {e}") from e

    def _bool(self, text: str) -> bool:
        value = self._sub(text).strip()
        if value.lower() not in ("true", "false"):
            raise LineError(f"condition is not a boolean: {value!r}")
        return value.lower() == "true"

    # -- running --------------------------------------------------------------

    def run(self, script: str, first_line: int = 1) -> Report:
        lines = script.split("\n")
        report = Report()
        self._report = report
        if self.baseline:
            self._baseline_pass(lines, first_line)
        try:
            handled = self._prefetch_pass(lines, first_line)
        except LineError as e:
            line, msg = e.args if len(e.args) == 2 else (first_line, str(e))
            report.status, report.failed_line, report.message = "failed", line, msg
            return report
        stack: list[_Frame] = []
        i = 0
        while i < len(lines):
            raw = lines[i]
            lineno = first_line + i
            text = raw.strip()
            i += 1
            if not text or text.startswith("//") or (i - 1) in handled:
                continue
            active = not stack or stack[-1].active
            try:
                try:
                    i = self._line(text, lineno, lines, i, stack, active)
                except (HostError, SandboxEscape) as e:
                    raise LineError(str(e)) from e
            except LineError as e:
                self._step(lineno, text, "failed", detail=str(e))
                report.status, report.failed_line, report.message = "failed", lineno, str(e)
                return report
            except _Exit as e:
                report.status, report.exit_code = "exited", e.code
                return report
        if stack:
            report.status, report.message = "failed", "if without endif"
            report.failed_line = first_line + len(lines) - 1
        return report

    def _step(
        self, lineno: int, text: str, outcome: str, substituted: str | None = None, detail: str = ""
    ) -> None:
        step = Step(lineno, text, outcome, substituted, detail, self._pending)
        self._pending = []
        self._report.steps.append(step)

    def _baseline_pass(self, lines: list[str], first_line: int) -> None:
        """Evaluate every distinct expression once, before anything runs.

        `action parameter query` values are known before an action starts (the
        operator answers them first), so they are available here; expressions
        that use a parameter only set later by a `parameter` line are deferred.
        """
        known: dict[str, str] = {}
        set_later: set[str] = set()
        for raw in lines:
            text = raw.strip()
            m = re.match(
                r'action\s+parameter\s+query\s+"([^"]+)"(?:.*?\bwith\s+default\s+"([^"]*)")?',
                text,
                re.IGNORECASE,
            )
            if m:
                value = self.given_params.get(m.group(1), m.group(2))
                if value is not None:
                    known[m.group(1)] = value
                continue
            m = re.match(r'parameter\s+"([^"]+)"\s*=', text, re.IGNORECASE)
            if m:
                set_later.add(m.group(1))
        saved, self.parameters = self.parameters, dict(known)
        try:
            for idx, raw in enumerate(lines):
                if raw.strip().startswith("//"):
                    continue
                for expr in find_expressions(raw):
                    if expr in self._baseline:
                        continue
                    entry = BaselineEntry(first_line + idx, expr)
                    used = set(re.findall(r'\bparameter\s+"([^"]*)"', expr, re.IGNORECASE))
                    if used & (set_later - known.keys()):
                        entry.deferred = True
                    else:
                        try:
                            answers = self._eval(expr)
                            entry.value = answers[0] if len(answers) == 1 else ", ".join(answers)
                        except SubstitutionError as e:
                            entry.error = str(e)
                    self._baseline[expr] = entry
                    self._report.baseline.append(entry)
        finally:
            self.parameters = saved

    def _prefetch_pass(self, lines: list[str], first_line: int) -> set[int]:
        """Download prefetch items before line 1. Returns line indexes it consumed."""
        handled: set[int] = set()
        in_block = False
        block_start = 0
        stack: list[_Frame] = []
        body_end: str | None = None  # inside a createfile body: text, not ActionScript
        for idx, raw in enumerate(lines):
            text = raw.strip()
            low = text.lower()
            lineno = first_line + idx
            if body_end is not None:
                if text == body_end:
                    body_end = None
                continue
            if not in_block and low.startswith(("createfile until ", "createfile until\t")):
                body_end = text.split(None, 2)[2].strip()
                continue
            if low == "begin prefetch block":
                in_block, block_start = True, lineno
                handled.add(idx)
                continue
            if in_block:
                handled.add(idx)
                if low == "end prefetch block":
                    in_block = False
                    continue
                if not text or text.startswith("//"):
                    continue
                try:
                    self._block_line(text, lineno, stack)
                except LineError as e:
                    self._step(lineno, text, "failed", detail=str(e))
                    raise LineError(lineno, str(e)) from e
                continue
            if low.startswith("prefetch ") or (
                low.startswith("download ") and not low.startswith("download now")
            ):
                handled.add(idx)
                try:
                    self._fetch(self._sub(text), lineno, text)
                except LineError as e:
                    self._step(lineno, text, "failed", detail=str(e))
                    raise LineError(lineno, str(e)) from e
        if in_block:
            message = "begin prefetch block without end prefetch block"
            self._step(block_start, "begin prefetch block", "failed", detail=message)
            raise LineError(block_start, message)
        return handled

    def _block_line(self, text: str, lineno: int, stack: list[_Frame]) -> None:
        low = text.lower()
        active = not stack or stack[-1].active
        if _starts(low, "if"):
            cond = active and self._bool(_after(text, "if"))
            stack.append(_Frame(cond, cond or not active, active))
        elif _starts(low, "elseif") or low in ("else", "endif"):
            if not stack:
                raise LineError(f"{low.split()[0]} without if")
            top = stack[-1]
            if low == "endif":
                stack.pop()
            elif low == "else":
                top.active, top.taken = top.parent_active and not top.taken, True
            else:
                cond = top.parent_active and not top.taken and self._bool(_after(text, "elseif"))
                top.active, top.taken = cond, top.taken or cond
        elif active and low.startswith(("add prefetch item", "add nohash prefetch item")):
            self._fetch(self._sub(text), lineno, text)
        elif active:
            self._step(lineno, text, "warning", detail="not simulated in a prefetch block")

    def _fetch(self, substituted: str, lineno: int, text: str) -> None:
        try:
            item = parse_prefetch(substituted)
        except PrefetchError as e:
            raise LineError(str(e)) from e
        if item is None:
            raise LineError(f"can't parse prefetch/download statement: {substituted}")
        download = self.sandbox.resolve("__Download")
        dest = str(self.host.pure(download, item.name))
        # downloaded and verified here, then put on the host (like a relay
        # handing a client its prefetch), and checked again where it landed
        with tempfile.TemporaryDirectory(prefix="bfasr-prefetch-") as staging:
            try:
                local = fetch(item, Path(staging), self.cache_dir, self.downloader)
            except PrefetchError as e:
                raise LineError(str(e)) from e
            except Exception as e:  # network (OSError) and requests errors
                raise LineError(f"download failed: {e}") from e
            try:
                self.host.mkdir(download)
                self.host.put_file(local, dest)
                landed = self.host.sha256(dest)
            except HostError as e:
                raise LineError(f"can't put {item.name} on the target: {e}") from e
            if landed != hashlib.sha256(local.read_bytes()).hexdigest():
                raise LineError(f"{item.name}: SHA-256 on the target differs from the download")
        self._report.prefetched.append(item.name)
        detail = "placeholder" if self.downloader is None else _verified(item)
        outcome = "ok" if item.now else "prefetched"
        self._step(lineno, text, outcome, substituted, f"{self.sandbox.display(dest)} ({detail})")

    # -- one line ---------------------------------------------------------------

    def _line(
        self, text: str, lineno: int, lines: list[str], i: int, stack: list[_Frame], active: bool
    ) -> int:
        low = text.lower()

        # control flow first: it decides whether anything else runs
        if _starts(low, "if"):
            cond = active and self._bool(_after(text, "if"))
            stack.append(_Frame(cond, cond or not active, active))
            self._step(
                lineno, text, "ok" if active else "skipped", detail=str(cond) if active else ""
            )
            return i
        if _starts(low, "elseif") or low == "else" or low == "endif":
            if not stack:
                raise LineError(f"{low.split()[0]} without if")
            top = stack[-1]
            if low == "endif":
                stack.pop()
            elif low == "else":
                top.active, top.taken = top.parent_active and not top.taken, True
            else:
                cond = top.parent_active and not top.taken and self._bool(_after(text, "elseif"))
                top.active, top.taken = cond, top.taken or cond
            self._step(lineno, text, "ok")
            return i

        if low.startswith(("createfile until ", "createfile until\t")):
            marker = text.split(None, 2)[2].strip()
            body: list[str] = []
            while i < len(lines) and lines[i].strip() != marker:
                body.append(lines[i])
                i += 1
            if i >= len(lines):
                raise LineError(f"createfile: end marker {marker} not found")
            i += 1
            if not active:
                self._step(lineno, text, "skipped")
                return i
            content = "\n".join(self._sub(b) for b in body) + "\n"
            self.host.write_bytes(self.sandbox.resolve("__createfile"), content.encode())
            self._step(lineno, text, "ok", detail=f"{len(body)} line(s) -> __createfile")
            return i

        if not active:
            self._step(lineno, text, "skipped")
            return i

        if low.startswith("action parameter query"):
            m = re.match(
                r'action\s+parameter\s+query\s+"([^"]+)"(?:.*?\bwith\s+default\s+"([^"]*)")?',
                text,
                re.IGNORECASE,
            )
            if not m:
                raise LineError("can't parse action parameter query")
            name, default = m.group(1), m.group(2)
            if name in self.given_params:
                value = self.given_params[name]
            elif default is not None:
                value = default
            else:
                raise LineError(f"no value for parameter {name}: pass --param {name}=VALUE")
            self.parameters[name] = value
            self._step(lineno, text, "ok", detail=f"{name} = {value!r}")
            return i

        if low.startswith("parameter "):
            sub = self._sub(text)
            m = re.match(r'parameter\s+"([^"]+)"\s*=\s*"(.*)"\s*$', sub, re.IGNORECASE)
            if not m:
                raise LineError("can't parse parameter line")
            if m.group(1) in self.parameters:
                raise LineError(f'parameter "{m.group(1)}" is already defined')
            self.parameters[m.group(1)] = m.group(2)
            self._step(lineno, text, "ok", sub, f"{m.group(1)} = {m.group(2)!r}")
            return i

        if low.startswith("appendfile"):
            sub = self._sub(text[len("appendfile") :].lstrip(" \t"))
            self.host.write_bytes(
                self.sandbox.resolve("__appendfile"), (sub + "\n").encode(), append=True
            )
            self._step(lineno, text, "ok", sub)
            return i

        if low.startswith(("copy ", "move ")):
            sub = self._sub(text)
            args = split_args(sub[5:])
            if len(args) != 2:
                raise LineError(f"{low[:4]} needs a source and a destination")
            src, dst = self.sandbox.resolve_read(args[0]), self.sandbox.resolve(args[1])
            if not self.host.is_file(src):
                raise LineError(f"source does not exist: {args[0]}")
            if self.host.exists(dst):
                raise LineError(f"destination exists: {args[1]}")
            self.host.mkdir(self.sandbox.parent(dst))
            # a dry run never moves a real file away: it copies it into the sandbox
            if low.startswith("move ") and (
                self.sandbox.mode != "full" or self.sandbox.contains(src)
            ):
                self.host.move(src, dst)
            else:
                self.host.copy_file(src, dst)
            self._step(
                lineno,
                text,
                "ok",
                sub,
                f"{self.sandbox.display(src)} -> {self.sandbox.display(dst)}",
            )
            return i

        if low.startswith(("folder create ", "folder delete ")):
            sub = self._sub(text)
            args = split_args(sub[len("folder create ") :])
            target = self.sandbox.resolve(args[0] if args else "")
            if low.startswith("folder create "):
                self.host.mkdir(target)
            elif not self.sandbox.is_protected(target) and self.host.is_dir(target):
                self.host.delete_tree(target)
            self._step(lineno, text, "ok", sub, self.sandbox.display(target))
            return i

        if low.startswith("delete "):
            sub = self._sub(text)
            args = split_args(sub[7:])
            target = self.sandbox.resolve(args[0] if args else "")
            if self.host.is_file(target):
                self.host.delete_file(target)
            self._step(lineno, text, "ok", sub, self.sandbox.display(target))
            return i

        if low.startswith("continue if "):
            if not self._bool(text[len("continue if ") :]):
                raise LineError("continue if is false: the action stops here")
            self._step(lineno, text, "ok")
            return i

        if low.startswith("pause while "):
            if self._bool(text[len("pause while ") :]):
                self._step(
                    lineno, text, "warning", detail="condition is true: a client would wait here"
                )
            else:
                self._step(lineno, text, "ok")
            return i

        if low == "exit" or low.startswith("exit "):
            sub = self._sub(text)
            words = sub.split()
            try:
                code = int(words[1]) if len(words) > 1 else 0
            except ValueError as e:
                raise LineError(f"exit needs a number: {words[1]!r}") from e
            self._step(lineno, text, "ok", sub)
            raise _Exit(code)

        if low.startswith("override "):
            # `override wait` is followed by key=value lines up to the command it applies to
            self._override = {"kind": low.split()[1] if len(low.split()) > 1 else ""}
            self._step(lineno, text, "ok")
            key_lineno = lineno
            while i < len(lines) and re.match(r"^\s*\w+\s*=", lines[i]):
                key_lineno += 1
                k, _, v = lines[i].strip().partition("=")
                self._override[k.strip().lower()] = v.strip()
                self._step(key_lineno, lines[i].strip(), "ok")
                i += 1
            return i

        if low.startswith("download now"):
            self._fetch(self._sub(text), lineno, text)
            return i

        cmd = low.split()[0]
        if cmd in _WAITS:
            self._command(cmd, text, lineno)
            return i

        if low.startswith(_NOOP_PREFIXES):
            sub = self._sub(text)
            self._step(lineno, text, "noop", sub, "recorded, not performed")
            return i

        self._step(lineno, text, "warning", detail="command not simulated")
        return i

    def _command(self, cmd: str, text: str, lineno: int) -> None:
        sub = self._sub(text)
        command = sub[len(cmd) :].strip()
        override, self._override = self._override or {}, None
        try:
            timeout = float(override["timeout_seconds"]) if "timeout_seconds" in override else None
        except ValueError as e:
            raise LineError(
                f"timeout_seconds is not a number: {override['timeout_seconds']!r}"
            ) from e
        wait = _WAITS[cmd]
        argv = self._argv(cmd, command)  # also checks the command line in a dry run
        if not self.execute:
            if wait:
                self.exit_code = self.assumed_exit_code
            self._step(
                lineno,
                text,
                "shown",
                sub,
                f"not run (exit code {self.exit_code} assumed)" if wait else "not run",
            )
            return
        code = self.runner(argv, cwd=self.sandbox.site, wait=wait, timeout=timeout)
        if wait:
            self.exit_code = code
        self._step(
            lineno, text, "ok", sub, f"exit code {code}" if wait else "started, not waited for"
        )

    def _argv(self, cmd: str, command: str) -> list[str]:
        """Split a command line, swapping absolute paths that exist in the sandbox.

        Only a dry run's sandbox remaps other absolute paths; with --execute
        they resolve to themselves and stay as written.

        On a Windows host argv[0] is the program and argv[1:] are the rest of
        the command line's tokens exactly as written (quotes kept): a Windows
        program parses its own command line, so re-quoting them would change
        what it gets (`TARGETDIR="C:\\x y"`). `dos` runs through cmd.exe, as on
        a client.
        """
        if self.host.windows:
            if cmd == "dos":
                return ["cmd.exe", "/c", command]
            tokens = split_win(command)
            if not tokens:
                raise LineError(f"{cmd} needs a command")
            program, rest = _unquote(tokens[0]), tokens[1:]
            return [self._remap(program), *[_requote(t, self._remap(_unquote(t))) for t in rest]]
        try:
            argv = shlex.split(command)
        except ValueError as e:
            raise LineError(f"can't split the command line: {e}") from e
        if not argv:
            raise LineError(f"{cmd} needs a command")
        return [self._remap(arg) for arg in argv]

    def _remap(self, arg: str) -> str:
        if not (arg.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:[\\/]", arg)):
            return arg
        try:
            mapped = self.sandbox.resolve(arg)
        except SandboxEscape:
            return arg
        return mapped if mapped != arg and self.host.exists(mapped) else arg


class _Exit(Exception):
    def __init__(self, code: int) -> None:
        self.code = code


def _unquote(token: str) -> str:
    return token[1:-1] if len(token) > 1 and token[0] == token[-1] == '"' else token


def _requote(token: str, value: str) -> str:
    """`token` with its value replaced (when remapped), keeping its quotes."""
    if value == _unquote(token):
        return token
    return f'"{value}"'


def split_win(command: str) -> list[str]:
    """Split a Windows command line on whitespace outside double quotes."""
    return re.findall(r'(?:"[^"]*"|[^\s"])+', command)


def _verified(item: PrefetchItem) -> str:
    checks = [c for c in ("size", "sha1", "sha256") if getattr(item, c) is not None]
    return "verified " + ", ".join(checks) if checks else "downloaded, no hash to verify"
