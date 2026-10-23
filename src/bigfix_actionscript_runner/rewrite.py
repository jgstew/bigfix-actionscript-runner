"""Rewrite action-only inspectors so qna (which has no action) can evaluate.

`parameter "x"` becomes a string literal, `exists parameter "x"` true or
false, `id of action` / `exit code of action` numbers, and the client data
folder, the current site's client folder and the download folder point into
the sandbox. The work is client_context's, on the analyzer's parse tree; the
regexes below are only the fallback for an expression the analyzer can't
parse, so such an expression still gets the rewrites it always had.
"""

import logging
import re
from dataclasses import dataclass, field

from .client_context import ClientContext, relevance_string, simulate

log = logging.getLogger(__name__)


@dataclass
class ActionContext:
    parameters: dict[str, str] = field(default_factory=dict)
    action_id: int = 0
    exit_code: int = 0
    site_folder: str = ""
    data_folder: str = ""
    download_folder: str = ""


__all__ = ["ActionContext", "relevance_string", "rewrite"]


# "of action" and "of active action" are the same action while it runs
_ACTION = r"of\s+(?:active\s+)?action\b"
_PARAMETER = re.compile(rf'\bparameter\s+"([^"]*)"(?:\s+{_ACTION})?', re.IGNORECASE)
_ID = re.compile(rf"\bid\s+{_ACTION}", re.IGNORECASE)
# the plural is the same number: the simulator tracks the last wait's code
_EXIT = re.compile(rf"\bexit\s+codes?\s+{_ACTION}", re.IGNORECASE)
_SITE = re.compile(r"\bclient\s+folder(s?)\s+of\s+current\s+site\b", re.IGNORECASE)
_DATA = re.compile(r"\bdata\s+folder(s?)\s+of\s+client\b", re.IGNORECASE)


def _rewrite_code(code: str, ctx: ActionContext, folders: bool) -> str:
    def parameter(m: re.Match[str]) -> str:
        name = m.group(1)
        return relevance_string(ctx.parameters[name]) if name in ctx.parameters else m.group(0)

    code = _PARAMETER.sub(parameter, code)
    code = _ID.sub(str(ctx.action_id), code)
    code = _EXIT.sub(str(ctx.exit_code), code)
    if folders:
        code = _SITE.sub(lambda m: f"folder{m.group(1)} {relevance_string(ctx.site_folder)}", code)
        code = _DATA.sub(lambda m: f"folder{m.group(1)} {relevance_string(ctx.data_folder)}", code)
    return code


def rewrite(expr: str, ctx: ActionContext, *, folders: bool = True) -> str:
    """Rewrite `expr`, leaving string literals untouched.

    `folders=False` keeps the real client/site folder inspectors (for evaluating
    on a remote machine, which can't see the local sandbox).
    """
    simulated = simulate(
        expr,
        ClientContext(
            parameters=ctx.parameters,
            action_id=ctx.action_id,
            exit_code=ctx.exit_code,
            site_folder=ctx.site_folder if folders else None,
            data_folder=ctx.data_folder if folders else None,
            download_folder=(ctx.download_folder or None) if folders else None,
        ),
    )
    if simulated.parse_error is None:
        for u in simulated.unresolved:
            # qna will most likely fail on these; the log says why
            log.debug("not simulated: %s: %s (in %s)", u.text, u.reason, expr)
        return simulated.text
    log.debug("falling back to the regex rewrite (%s): %s", simulated.parse_error, expr)
    return _rewrite_regex(expr, ctx, folders=folders)


def _rewrite_regex(expr: str, ctx: ActionContext, *, folders: bool = True) -> str:
    """The text-only rewrite, for an expression the analyzer can't parse.

    A string literal is everything from a `"` to the next `"`; in relevance a
    quote inside a string is written %22, so there are no escaped quotes.
    The parameter pattern spans a literal (`parameter "x"`), so code segments
    are rewritten together with the literal that follows `parameter`.
    """
    parts = re.split(r'("[^"]*")', expr)
    out: list[str] = []
    i = 0
    while i < len(parts):
        part = parts[i]
        if i % 2 == 1:  # a string literal
            out.append(part)
            i += 1
            continue
        # glue `... parameter` + `"name"` (+ ` of action ...`) so the pattern sees it
        if re.search(r"\bparameter\s*$", part, re.IGNORECASE) and i + 2 < len(parts) + 1:
            literal = parts[i + 1] if i + 1 < len(parts) else ""
            rest = parts[i + 2] if i + 2 < len(parts) else ""
            m = re.match(rf"\s+{_ACTION}", rest, re.IGNORECASE)
            tail = m.group(0) if m else ""
            combined = _rewrite_code(part + literal + tail, ctx, folders)
            out.append(combined)
            if i + 2 < len(parts):
                parts[i + 2] = rest[len(tail) :]
            i += 2
            continue
        out.append(_rewrite_code(part, ctx, folders))
        i += 1
    return "".join(out)
