"""Static preflight, before anything is evaluated.

Two checkers, each where it applies:

- bigfix-relevance-analyzer for relevance: .bes files go through its own
  linter (the item's relevance and every substitution in its ActionScript);
  plain ActionScript files have their substitutions extracted and linted one
  by one. Its errors are parse and type errors; style/risk rules are warnings.
- pre-commit-bigfix for the ActionScript itself (structure, commands,
  prefetch lines), never with auto-fix, so the file is never changed. Its
  E-codes are things a client rejects or fails on and block the run, except
  the few that are conventions rather than failures (NON_BLOCKING); its
  W-codes are warnings.

Warnings never block a run.
"""

from dataclasses import dataclass, field
from pathlib import Path

import bigfix_relevance_analyzer as bra

ANALYZER = "bigfix-relevance-analyzer"
PRE_COMMIT_BIGFIX = "pre-commit-bigfix"

# pre-commit-bigfix E-codes that don't make an action fail on a client
NON_BLOCKING = frozenset(
    {
        "E401",  # prefetch without sha256: optional unless enhanced security is on
        "E402",  # an outdated unzip.exe prefetch: still works
        "E304",  # guards for pre-commit-bigfix's own auto-fix, which never runs here
        "E404",
    }
)


@dataclass
class Issue:
    severity: str  # "error" | "warning"
    line: int | None
    code: str
    message: str
    relevance: str = ""
    source: str = ANALYZER


@dataclass
class PreflightResult:
    checked: int = 0
    errors: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)


_BES_SUFFIXES = {".bes"}


def _severity(value: object) -> str:
    return str(getattr(value, "value", value)).lower()


def _add(result: PreflightResult, issue: Issue) -> None:
    (result.errors if issue.severity == "error" else result.warnings).append(issue)


def _relevance_bes(path: Path, result: PreflightResult) -> None:
    result.checked = len(bra.extract_relevance_from_file(path))
    for f in bra.lint_file(path, bra.LintConfig()):
        site = getattr(f, "site", None)
        text = getattr(site, "text", "")
        _add(result, Issue(_severity(f.severity), f.line, str(f.code), f.message, text))


def _relevance_actionscript(text: str, result: PreflightResult) -> None:
    sites = bra.extract_relevance_from_actionscript(text)
    result.checked = len(sites)
    # lint_text gives the same severities lint_file uses for .bes files: parse and
    # parse and type errors are errors, style/risk rules (plural-preferred, ...) warnings
    config = bra.LintConfig(dialect=bra.Dialect.CLIENT)
    for site in sites:
        for f in bra.lint_text(site.text, config):
            _add(result, Issue(_severity(f.severity), site.line, str(f.code), f.message, site.text))


def _actionscript_checks(path: Path, result: PreflightResult) -> None:
    from pre_commit_bigfix import (
        bes_actionscript_lint_schclass,
        bes_actionscript_validate_prefetch,
        bes_actionscript_validate_script,
    )

    for module in (
        bes_actionscript_validate_script,
        bes_actionscript_lint_schclass,
        bes_actionscript_validate_prefetch,
    ):
        for _path, issues, _fixed in module.check_files([str(path)], auto_fix=False):
            for line, code, message in issues:
                blocking = code.startswith("E") and code not in NON_BLOCKING
                severity = "error" if blocking else "warning"
                _add(result, Issue(severity, line, code, message, source=PRE_COMMIT_BIGFIX))


def preflight(path: str | Path) -> PreflightResult:
    path = Path(path)
    result = PreflightResult()
    if path.suffix.lower() in _BES_SUFFIXES:
        _relevance_bes(path, result)
    else:
        _relevance_actionscript(path.read_text(encoding="utf-8"), result)
    _actionscript_checks(path, result)
    return result
