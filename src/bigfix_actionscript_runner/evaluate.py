"""Evaluate client relevance with bigfix-remote-client-relevance (local qna or SSH)."""

import asyncio
from collections.abc import Callable, Coroutine, Mapping
from typing import Any

from bigfix_remote_client_relevance import Target, evaluate_client_relevance

from .substitution import SubstitutionError

Api = Callable[..., Coroutine[Any, Any, list[Any]]]


def make_target(ssh: str | None = None, become: bool | None = None) -> Target:
    """`--local` by default; `ssh="user@host"` evaluates on that machine instead.

    `become=True` runs qna under sudo on the SSH target (macOS qna needs root).
    None leaves it to bigfix-remote-client-relevance (on for --local on macOS).
    """
    if ssh is None:
        return Target(kind="local", name="local")
    user, sep, host = ssh.rpartition("@")
    return Target(kind="ssh", name=host, user=user if sep else None, become=become)


class QnaEvaluator:
    """Callable(expr) -> answers; raises SubstitutionError like a failed client substitution.

    `overrides` maps an exact expression to a fixed answer (for inspectors the
    evaluating machine doesn't have, e.g. Windows ones when simulating on a Mac).
    """

    def __init__(
        self,
        target: Target,
        *,
        timeout_s: float = 60.0,
        overrides: Mapping[str, str] | None = None,
        api: Api = evaluate_client_relevance,
    ) -> None:
        self.target = target
        self.timeout_s = timeout_s
        self.overrides = dict(overrides or {})
        self._api = api
        self.calls = 0

    def __call__(self, expr: str) -> list[str]:
        if expr in self.overrides:
            return [self.overrides[expr]]
        self.calls += 1
        results: list[Any] = asyncio.run(self._api(expr, [self.target], timeout_s=self.timeout_s))
        result = results[0]
        if result.error:
            raise SubstitutionError(self._message(result.error, result.error_kind or "error"))
        return list(result.answers)

    def _message(self, error: str, kind: str) -> str:
        message = error if kind == "relevance" else f"{kind}: {error}"
        if "no inspector context" in error.lower():
            # qna has no running client behind it: client-only inspectors fail
            message += (
                " (this inspector needs a running BigFix client, which qna doesn't "
                "have; give it an answer with --set 'EXPR=>VALUE')"
            )
        elif kind == "transport" and not error.split(":", 1)[-1].strip():
            message += (
                f" (no details: possibly the {self.timeout_s:g} s timeout; "
                "a slow expression may need a larger --timeout)"
            )
        return message
