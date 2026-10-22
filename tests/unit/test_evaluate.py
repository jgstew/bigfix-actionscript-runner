"""Relevance evaluation through bigfix-remote-client-relevance (local or SSH)."""

from types import SimpleNamespace

import pytest

from bigfix_actionscript_runner.evaluate import QnaEvaluator, make_target
from bigfix_actionscript_runner.substitution import SubstitutionError


def test_local_target():
    t = make_target()
    assert (t.kind, t.name) == ("local", "local")


def test_ssh_target_splits_user_and_host():
    t = make_target(ssh="admin@192.0.2.10")
    assert (t.kind, t.name, t.user) == ("ssh", "192.0.2.10", "admin")


def test_ssh_target_without_user():
    t = make_target(ssh="winbox")
    assert (t.kind, t.name, t.user) == ("ssh", "winbox", None)


def test_ssh_target_with_at_sign_in_user():
    t = make_target(ssh="me@corp.com@host")
    assert (t.name, t.user) == ("host", "me@corp.com")


def fake_api(answers=None, error=None, error_kind=None, calls=None):
    async def evaluate_client_relevance(expr, targets, **kw):
        if calls is not None:
            calls.append((expr, targets, kw))
        return [SimpleNamespace(answers=answers or [], error=error, error_kind=error_kind)]

    return evaluate_client_relevance


def test_evaluator_returns_answers_and_passes_target_and_timeout():
    calls = []
    ev = QnaEvaluator(make_target(), timeout_s=12, api=fake_api(["macOS"], calls=calls))
    assert ev("name of operating system") == ["macOS"]
    expr, targets, kw = calls[0]
    assert expr == "name of operating system"
    assert targets[0].kind == "local"
    assert kw["timeout_s"] == 12


def test_evaluator_raises_on_relevance_error():
    ev = QnaEvaluator(
        make_target(),
        api=fake_api(error='The operator "x" is not defined.', error_kind="relevance"),
    )
    with pytest.raises(SubstitutionError, match='operator "x" is not defined'):
        ev("x")


def test_evaluator_raises_on_transport_error_with_kind():
    ev = QnaEvaluator(
        make_target(ssh="h"), api=fake_api(error="connection refused", error_kind="transport")
    )
    with pytest.raises(SubstitutionError, match="transport: connection refused"):
        ev("1")


def test_evaluator_counts_calls():
    ev = QnaEvaluator(make_target(), api=fake_api(["1"]))
    ev("a")
    ev("b")
    assert ev.calls == 2


def test_overrides_short_circuit_qna():
    calls = []
    ev = QnaEvaluator(
        make_target(),
        api=fake_api(["real"], calls=calls),
        overrides={"pathname of system folder": "C:\\Windows\\System32"},
    )
    assert ev("pathname of system folder") == ["C:\\Windows\\System32"]
    assert ev("other") == ["real"]
    assert [c[0] for c in calls] == ["other"]


def test_become_for_ssh_targets_that_need_root():
    assert make_target(ssh="jgstew@mini", become=True).become is True
    assert make_target(ssh="jgstew@mini").become is None  # left to bigfix-remote-client-relevance
