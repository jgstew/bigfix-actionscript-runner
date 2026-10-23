"""Simulate the inspectors only a running BES Client can answer.

qna (and the Fixlet Debugger without a client) is a client relevance evaluator
with no client behind it: an inspector that reads the client's runtime state
-- the running action's parameters, id and exit code, the site being
evaluated, the download folder of the action -- fails there with "No
inspector context". :func:`simulate` rewrites each such reference to the
literal a running client would have produced, from a :class:`ClientContext`,
so the rest of the expression can still be evaluated by qna.

It works on the analyzer's parse tree, not on the text: relevance has no
reserved words and multi-word names, so only a parse can say that
``exists parameter "x"`` is one ``exists`` over one ``parameter``, and that
``"id of action"`` is a string. Replacements are spliced back by source span,
so everything else is kept exactly as written.

:data:`NO_CONTEXT_INSPECTORS` is the evidence: the signatures qna reported as
"No inspector context" on a real target, as dumped by
``scripts/probe_client_context.py`` into ``tests/data/client_context/``.
A reference to one that :func:`simulate` has no value for is reported in
:attr:`Simulation.unresolved` rather than guessed.

This module depends on nothing but bigfix-relevance-analyzer, so that it can
move there unchanged.
"""

from __future__ import annotations

import functools
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import unquote

from bigfix_relevance_analyzer import inspectors, try_parse_relevance
from bigfix_relevance_analyzer.nodes import (
    Cast,
    Exists,
    Node,
    Of,
    Reference,
    StringLiteral,
    Whose,
    children,
)

# Signatures qna answered with "No inspector context", from the dumps in
# tests/data/client_context/ (test_client_context checks they match exactly).
NO_CONTEXT_INSPECTORS: frozenset[str] = frozenset(
    {
        "action",
        "action <integer>",
        "action lock state",
        "active action",
        "active count of <action>",
        "active line number of <action>",
        "active of <action>",
        "active start time of <action>",
        "agent interface <string> of <client>",
        "agent interfaces of <client>",
        "analysis",
        "apparent registration server time",
        "application usages <string>",
        "authenticating of <client>",
        "banned prefetch plugins of <client>",
        "bes license",
        "certificate of <client>",
        "character sets of <client>",
        "client license",
        "cloud provider",
        "complete time of <action>",
        "computer id",
        "constrained of <action>",
        "constraint of <action>",
        "controller of <action lock state>",
        "current analysis",
        "download failure of <action>",
        "download file <string>",
        "download folder",
        "download path <string>",
        "effective date of <action lock state>",
        "evaluationcycle of <client>",
        "executions <string>",
        "exit code of <action>",
        "expiration date of <action lock state>",
        "first active count of <action>",
        "group leader of <action>",
        "headers <string> of <action>",
        "headers of <action>",
        "id of <action>",
        "last active line number of <action>",
        "last active time of <action>",
        "last change time of <action>",
        "last command time of <client>",
        "last relay select time",
        "last report time of <client>",
        "lock string of <action lock state>",
        "locked of <action lock state>",
        "offer accepted of <action>",
        "offer of <action>",
        "origin fixlet id of <action>",
        "parameter <string>",
        "parameter <string> of <action>",
        "pending login of <action>",
        "pending of <action>",
        "pending restart",
        "pending restart <string>",
        "pending restart names",
        "pending restart of <action>",
        "pending time of <action>",
        "persistent constraint of <action>",
        "power history",
        "registration address of <client>",
        "registration cidr address of <client>",
        "registration mac address of <client>",
        "registration subnet address of <client>",
        "report character set of <client>",
        "selected server",
        "status of <action>",
        "system constraint of <action>",
        "upload progress of <client>",
        "waiting for download of <action>",
        "wake on lan cidr subnet",
        "wake on lan subnet cidr string",
    }
)

# Globals qna answers as nonexistent though a running client always has them
# while an action runs: their properties were probed "no-object" (checked
# against the dumps by test_client_context, as above).
NO_OBJECT_GLOBALS: frozenset[str] = frozenset({"current site"})

# "of action" and "of active action" are the same action while it runs
_ACTION = frozenset({"action", "active action"})


@dataclass(frozen=True)
class ClientContext:
    """What a running client would answer. ``None`` means unknown: left as written."""

    parameters: Mapping[str, str] = field(default_factory=dict)
    """Every parameter that is set; any other name does not exist."""
    action_id: int | None = None
    exit_code: int | None = None
    """The exit code of the action's last ``wait`` (or ``run``)."""
    site_folder: str | None = None
    """``client folder of current site``."""
    data_folder: str | None = None
    """``data folder of client``: the client's __BESData."""
    download_folder: str | None = None
    """``download folder``: where the action's prefetches are."""


@dataclass(frozen=True)
class Replacement:
    start: int
    end: int
    original: str
    text: str


@dataclass(frozen=True)
class Unresolved:
    start: int
    end: int
    text: str
    reason: str


@dataclass(frozen=True)
class Simulation:
    text: str
    """The expression with every simulated reference replaced."""
    replaced: tuple[Replacement, ...] = ()
    unresolved: tuple[Unresolved, ...] = ()
    """Client-context references left as written: the evaluator will fail on them."""
    parse_error: str | None = None
    """Set when the expression didn't parse; ``text`` is then unchanged."""


def relevance_string(value: str) -> str:
    """A relevance string literal: % and " are percent-escaped."""
    return '"' + value.replace("%", "%25").replace('"', "%22") + '"'


def _integer(value: int) -> str:
    # `-1` is unary minus over 1: parenthesized so it binds as one value
    return str(value) if value >= 0 else f"({value})"


def _object(kind: str, phrase: str, path: str) -> str:
    """`folder "p"`, or `folders "p"` when the original was written plural."""
    plural = "s" if phrase.endswith("s") else ""
    return f"{kind}{plural} {relevance_string(path)}"


def _join(folder: str, name: str) -> str:
    # the target's separator: a Windows download folder has only backslashes
    sep = "\\" if "\\" in folder and "/" not in folder else "/"
    return folder.rstrip(sep) + sep + name


@functools.cache
def _no_context_global(phrase: str) -> bool:
    """Whether `phrase`, written with no `of`, names a no-context global."""
    return any(
        not entry.operands and entry.signature in NO_CONTEXT_INSPECTORS
        for entry in inspectors.lookup(phrase)
    )


def _phrase(node: Node | None, *names: str) -> bool:
    return isinstance(node, Reference) and node.phrase in names


def _running_action(node: Node | None) -> bool:
    # `action 3` is another action, not the one running
    return _phrase(node, *_ACTION) and node.index is None  # type: ignore[union-attr]


class _Simulator:
    def __init__(self, source: str, context: ClientContext) -> None:
        self.source = source
        self.ctx = context
        self.replaced: list[Replacement] = []
        self.unresolved: list[Unresolved] = []
        self.stack: list[Node] = []

    def _text(self, node: Node) -> str:
        return self.source[node.span.start : node.span.end]

    def _replace(self, node: Node, text: str) -> None:
        original = self._text(node)
        # a grouped node's span includes its parentheses: keep the author's grouping
        if original.startswith("(") and original.endswith(")"):
            text = f"({text})"
        self.replaced.append(Replacement(node.span.start, node.span.end, original, text))

    def _unresolved(self, node: Node, reason: str) -> None:
        self.unresolved.append(Unresolved(node.span.start, node.span.end, self._text(node), reason))

    # -- recognizers ----------------------------------------------------------

    def _parameter(self, node: Node) -> tuple[str, ...] | None:
        """Candidate names for `parameter(s) <name> [of the running action]`.

        None when `node` is no such reference; () when it is, but its name
        isn't known without evaluating. A literal name is tried as written
        first (how the script's `parameter` command stored it), then with its
        %xx escapes decoded (how relevance reads the literal).
        """
        if isinstance(node, Of) and not node.prop_grouped and _running_action(node.obj):
            node = node.prop
        if not isinstance(node, Reference) or node.phrase not in ("parameter", "parameters"):
            return None
        if isinstance(node.index, StringLiteral):
            return (node.index.content, unquote(node.index.content))
        if node.index is None:
            return None
        value = self._parameter_value(node.index)
        return (value,) if value is not None else ()

    def _set_name(self, names: tuple[str, ...]) -> str | None:
        return next((n for n in names if n in self.ctx.parameters), None)

    def _parameter_value(self, node: Node) -> str | None:
        names = self._parameter(node)
        name = self._set_name(names) if names else None
        return self.ctx.parameters[name] if name is not None else None

    def _string_value(self, node: Node | None) -> str | None:
        """A string known without evaluating: a literal or a set parameter."""
        if isinstance(node, StringLiteral):
            return unquote(node.content)
        return self._parameter_value(node) if node is not None else None

    # -- the walk -------------------------------------------------------------

    def run(self, root: Node) -> None:
        # iterative, like the analyzer's own whole-tree passes: a long flat
        # chain (`a or b or ...`) is as deep as it is long
        self.stack = [root]
        while self.stack:
            node = self.stack.pop()
            if self._simulate(node):
                continue
            if isinstance(node, Of) and not node.prop_grouped and isinstance(node.prop, Reference):
                # an ungrouped property side names a property of `obj`, not a
                # global; a grouped one is evaluated against the world, so it is
                if node.prop.index is not None:
                    self.stack.append(node.prop.index)
                self.stack.append(node.obj)
                continue
            if isinstance(node, Reference):
                if node.phrase in NO_OBJECT_GLOBALS:
                    self._unresolved(node, "qna has no such object; no value to simulate it with")
                elif _no_context_global(node.phrase):
                    self._unresolved(
                        node, "needs a running BigFix client; no value to simulate it with"
                    )
            self.stack.extend(reversed(children(node)))

    def _simulate(self, node: Node) -> bool:
        """Replace `node` if it is a client-context reference with a known value."""
        ctx = self.ctx
        if isinstance(node, Exists):
            # `exists parameter "x"`, also filtered or cast: an unset one has no
            # value to filter or cast, so the whole `exists` is decided by it
            core = node.operand
            while isinstance(core, Whose | Cast):
                core = core.collection if isinstance(core, Whose) else core.operand
            names = self._parameter(core)
            if names:
                is_set = self._set_name(names) is not None
                if core is node.operand or not is_set:
                    self._replace(node, "true" if is_set != node.negated else "false")
                    return True
        names = self._parameter(node)
        if names is not None:
            name = self._set_name(names) if names else None
            if name is not None:
                self._replace(node, relevance_string(ctx.parameters[name]))
            else:
                self._unresolved(node, "the parameter is not set (a client fails on it too)")
                if isinstance(node, Reference) and node.index is not None:
                    self.stack.append(node.index)
            return True
        if isinstance(node, Of) and not node.prop_grouped and isinstance(node.prop, Reference):
            prop = node.prop.phrase
            if _running_action(node.obj) and prop in ("id", "ids", "exit code", "exit codes"):
                value = ctx.action_id if prop in ("id", "ids") else ctx.exit_code
                if value is None:
                    self._unresolved(node, f"no {prop.rstrip('s')} for the action to simulate")
                else:
                    self._replace(node, _integer(value))
                return True
            if (
                prop in ("client folder", "client folders")
                and _phrase(node.obj, "current site")
                and ctx.site_folder is not None
            ):
                self._replace(node, _object("folder", prop, ctx.site_folder))
                return True
            if (
                prop in ("data folder", "data folders")
                and _phrase(node.obj, "client")
                and ctx.data_folder is not None
            ):
                self._replace(node, _object("folder", prop, ctx.data_folder))
                return True
        if (
            isinstance(node, Reference)
            and node.phrase in ("download folder", "download folders")
            and node.index is None
            and ctx.download_folder is not None
        ):
            self._replace(node, _object("folder", node.phrase, ctx.download_folder))
            return True
        if isinstance(node, Reference) and node.phrase in ("download file", "download files"):
            name = self._string_value(node.index)
            if name is not None and ctx.download_folder is not None:
                self._replace(node, _object("file", node.phrase, _join(ctx.download_folder, name)))
                return True
            self._unresolved(node, "no download folder or file name to simulate it with")
            if node.index is not None:
                self.stack.append(node.index)
            return True
        return False


def simulate(expr: str, context: ClientContext) -> Simulation:
    """`expr` with its client-context references replaced by what `context` says.

    Never raises: an expression that doesn't parse comes back unchanged with
    :attr:`Simulation.parse_error` set, for the caller to fall back on.
    """
    parsed = try_parse_relevance(expr)
    if parsed.error is not None or parsed.node is None:
        return Simulation(expr, parse_error=str(parsed.error or "no expression"))
    sim = _Simulator(expr, context)
    sim.run(parsed.node)
    out, pos = [], 0
    for r in sorted(sim.replaced, key=lambda r: r.start):
        out.append(expr[pos : r.start])
        out.append(r.text)
        pos = r.end
    out.append(expr[pos:])
    return Simulation(
        "".join(out),
        tuple(sorted(sim.replaced, key=lambda r: r.start)),
        tuple(sorted(sim.unresolved, key=lambda u: u.start)),
    )
