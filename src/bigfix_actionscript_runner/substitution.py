"""Relevance substitution in ActionScript lines.

Like the BigFix client: `{expr}` is replaced by the single answer of `expr`,
`{{` is a literal `{`, and an expression ends at the first `}` (relevance
can't contain a `}` inside a substitution). A lone `}` needs no escape.
"""

from collections.abc import Callable, Iterator


class SubstitutionError(Exception):
    """A substitution could not be evaluated (the line fails, like on a client)."""


Evaluator = Callable[[str], list[str]]


def _scan(line: str) -> Iterator[tuple[str, str]]:
    """Yield ("text", chunk) and ("expr", expression) pieces of a line.

    An unterminated expression is yielded as ("unterminated", rest-of-line).
    """
    i = 0
    text: list[str] = []
    while i < len(line):
        if line.startswith("{{", i):
            text.append("{")
            i += 2
            continue
        if line[i] == "{":
            end = line.find("}", i + 1)
            if text:
                yield "text", "".join(text)
                text = []
            if end == -1:
                yield "unterminated", line[i:]
                return
            yield "expr", line[i + 1 : end].strip()
            i = end + 1
            continue
        text.append(line[i])
        i += 1
    if text:
        yield "text", "".join(text)


def find_expressions(line: str) -> list[str]:
    """The relevance expressions a line would substitute, without evaluating them."""
    return [piece for kind, piece in _scan(line) if kind == "expr"]


def substitute(line: str, evaluate: Evaluator) -> str:
    out: list[str] = []
    for kind, piece in _scan(line):
        if kind == "text":
            out.append(piece)
        elif kind == "unterminated":
            raise SubstitutionError(f"unterminated relevance substitution: {piece!r}")
        else:
            answers = evaluate(piece)
            if not answers:
                raise SubstitutionError(f"{{{piece}}} has no value")
            if len(answers) > 1:
                raise SubstitutionError(f"{{{piece}}} has {len(answers)} values, not one")
            out.append(answers[0])
    return "".join(out)
