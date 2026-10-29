"""The release workflow's jobs don't cancel each other, or regular CI runs."""

import re
from pathlib import Path

import pytest

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
RELEASE = "tag_and_release.yaml"
REF = "refs/heads/main"


def workflow_text(name: str) -> str:
    path = WORKFLOWS / name
    if not path.exists():  # the sdist ships tests/ but not .github/
        pytest.skip("no .github/ in this tree")
    return path.read_text()


def workflow_name(name: str) -> str:
    found = re.search(r"^name: (.+)$", workflow_text(name), re.MULTILINE)
    assert found, f"{name}: no top-level name"
    return found.group(1)


def concurrency_group(name: str, *, caller: str | None = None) -> str:
    """The concurrency group a run of workflow `name` gets, standalone or, when
    `caller` is given, as a reusable workflow called from that workflow. In a
    called run the github context is the caller's, so github.workflow is the
    caller's name, which is how the release's two gates once shared a group."""
    group = re.search(
        r"^concurrency:\n(?:  #.*\n)*  group: (.+)$", workflow_text(name), re.MULTILINE
    )
    assert group, f"{name}: no top-level concurrency group"
    workflow = workflow_name(caller or name)
    resolved = (
        group.group(1).replace("${{ github.workflow }}", workflow).replace("${{ github.ref }}", REF)
    )
    assert "${{" not in resolved, f"{name}: unhandled expression in {resolved!r}"
    return resolved


def cancels_in_progress(name: str, *, caller: str | None = None) -> bool:
    """Whether a run of `name` (standalone, or called from `caller`) cancels
    the run already in its group. Handles the two forms the workflows use: a
    literal, or `${{ github.workflow == '<name>' }}`."""
    text = workflow_text(name)
    value = re.search(r"^  cancel-in-progress: (.+)$", text, re.MULTILINE)
    if not value:
        return False
    if value.group(1) in ("true", "false"):
        return value.group(1) == "true"
    compared = re.fullmatch(r"\$\{\{ github\.workflow == '([^']+)' \}\}", value.group(1))
    assert compared, f"{name}: unhandled cancel-in-progress {value.group(1)!r}"
    return workflow_name(caller or name) == compared.group(1)


def gates() -> list[str]:
    """The reusable workflows the release calls with `uses: ./...`."""
    return re.findall(r"uses: \./\.github/workflows/(\S+\.yaml)", workflow_text(RELEASE))


def test_release_calls_both_gates():
    assert sorted(gates()) == ["pre-commit.yaml", "test.yaml"]


def test_release_gates_have_their_own_concurrency_groups():
    # One group shared by two gates means cancel-in-progress kills one of them
    # and the release is skipped; one shared with a regular run of the same
    # workflow means a push's CI and its release cancel each other.
    called = {gate: concurrency_group(gate, caller=RELEASE) for gate in gates()}
    standalone = {gate: concurrency_group(gate) for gate in gates()}
    taken = [concurrency_group(RELEASE), *called.values(), *standalone.values()]
    assert len(set(taken)) == len(taken), taken


def test_release_gates_never_cancel_in_progress():
    # a cancelled gate skips build and everything after it, with no error
    assert [gate for gate in gates() if cancels_in_progress(gate, caller=RELEASE)] == []
    assert not cancels_in_progress(RELEASE)


def test_regular_ci_runs_still_cancel_superseded_ones():
    assert all(cancels_in_progress(gate) for gate in gates())
