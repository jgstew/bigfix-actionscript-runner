"""The version: one source in __init__.py, read the way the release workflow reads it."""

import importlib.metadata
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

import bigfix_actionscript_runner

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "tag_and_release.yaml"
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text())

# MAJOR.MINOR.PATCH and nothing else: no pre/post/dev suffix, no leading
# zeros. Such a string is already in PEP 440 normal form, so the v<version>
# tag and the version PyPI shows can't differ. [0-9], not \d, which would also
# match non-ASCII digits. tag_and_release.yaml checks the same format.
VERSION_FORMAT = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")


def workflow_version_code() -> str:
    """The Python the release workflow's version step runs, taken from the
    workflow itself so the test breaks if the step and the package drift."""
    if not WORKFLOW.exists():  # the sdist ships tests/ but not .github/
        pytest.skip("no .github/ in this tree")
    step = re.search(r"python3 -c '([^']+)'", WORKFLOW.read_text())
    assert step, "tag_and_release.yaml no longer reads the version with python3 -c"
    return step.group(1)


def test_installed_metadata_matches_dunder_version():
    # hatch builds [project] version from __init__.py, so they agree by
    # construction; a mismatch means a stale editable install (see cache-keys)
    assert importlib.metadata.version("bigfix-actionscript-runner") == bigfix_actionscript_runner.__version__


def read_version_bare(code: str) -> str:
    """Run `code` the way the version job does: it installs nothing, so -S
    drops site-packages (every dependency, and the editable install), leaving
    only the stdlib and src/ on PYTHONPATH, as on the runner."""
    done = subprocess.run(
        [sys.executable, "-S", "-c", code],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": "src"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def test_release_workflow_reads_dunder_version_without_dependencies():
    assert read_version_bare(workflow_version_code()) == bigfix_actionscript_runner.__version__


def test_dunder_version_imports_without_dependencies():
    # A fixed copy of the workflow's read, so the package side is pinned even
    # if the workflow step is edited, and in an sdist that has no .github/.
    code = "from bigfix_actionscript_runner import __version__; print(__version__)"
    assert read_version_bare(code) == bigfix_actionscript_runner.__version__


@pytest.mark.parametrize("version", ["0.0.0", "0.2.1", "10.100.1000"])
def test_version_format_accepts(version):
    assert VERSION_FORMAT.fullmatch(version)


@pytest.mark.parametrize(
    "version",
    ["0.3", "1.2.3.4", "v1.2.3", "01.2.3", "1.02.3", "1.2.03", "1.2.3rc1", "1.2.3-rc1", "1.2.3.post1", "1.2.3+local", "1.2.3\n", "１.2.3"],
)
def test_version_format_rejects(version):
    assert not VERSION_FORMAT.fullmatch(version)


def test_dunder_version_is_major_minor_patch():
    assert VERSION_FORMAT.fullmatch(bigfix_actionscript_runner.__version__)


def test_version_is_set_only_in_dunder_version():
    # a static [project] version beside the dynamic one would be a second copy
    # to keep in sync, which is what moving it to __init__.py removed
    project = PYPROJECT["project"]
    assert "version" not in project
    assert project["dynamic"] == ["version"]
    assert PYPROJECT["tool"]["hatch"]["version"]["path"] == "src/bigfix_actionscript_runner/__init__.py"


def test_no_direct_url_dependencies():
    # A copy of the workflow's "Refuse to publish what PyPI would reject" step,
    # which only runs once PYPI_PUBLISH is on, so it catches a `pkg @ git+...`
    # in the PR instead of mid-release. PyPI rejects them in extras too.
    project = PYPROJECT["project"]
    published = project["dependencies"] + [d for extra in project.get("optional-dependencies", {}).values() for d in extra]
    assert [d for d in published if " @ " in d] == []
