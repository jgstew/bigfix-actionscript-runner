# AGENTS.md

Guidance for AI coding agents working in this repo. Human-facing docs are
[README.md](README.md) (usage).

## What this is

A Python (>=3.11) CLI and library that runs the ActionScript of a BigFix
`.bes` file (or a plain ActionScript file) the way a BigFix client would,
without a BigFix server: client relevance substitution is real (`qna`, via
[bigfix-remote-client-relevance](https://github.com/jgstew/bigfix-remote-client-relevance)),
locally or on an SSH target. Entry point: `bigfix_actionscript_runner.cli:main`.

- `src/bigfix_actionscript_runner/`
  - `load.py` -- read the action out of a `.bes` file
  - `preflight.py` -- bigfix-relevance-analyzer and pre-commit-bigfix checks
  - `substitution.py` -- `{expression}` scanning and substitution
  - `rewrite.py` -- action context qna lacks (`parameter "x"`, client folders)
  - `evaluate.py` -- client relevance through bigfix-remote-client-relevance
  - `host.py` -- the Host protocol and LocalHost: every file operation and command
  - `ssh_host.py` -- SSHHost: the same over SSH (sh -c / sudo -n, or PowerShell)
  - `sandbox.py` -- where paths go: modes full (dry run), client (--execute), off
  - `prefetch.py` -- prefetch parsing and downloads (bigfix-prefetch)
  - `executor.py` -- the ActionScript interpreter
  - `cli.py` -- the one stdout writer
- `tests/unit/` -- offline; `tests/integration/` -- needs real prerequisites

## Commands

```bash
uv sync                                  # install, dev group included
uv run pytest                            # runs with -n auto (xdist)
uv run pytest -n0 path/to/test.py -k name  # serial, for a focused run
uv run ruff check . && uv run mypy src && uv run pyright
```

Unmarked tests run offline on a bare machine. Tests marked `live_qna`,
`ssh_posix`, `ssh_windows` and `network` need a real prerequisite and
auto-skip without it (see `tests/conftest.py`). The SSH tests really change
files on the target, so only point them at test machines.

## How to work here

- **Test first.** Write the failing test, run it and confirm it fails for
  the right reason, then implement until green. A new test that passes
  before the fix isn't testing the fix.
- **Don't create or switch git branches** without asking; the maintainer
  manages git state.
- **stdout belongs to `cli.py`** (the trace or the `--json` payload). The
  library logs; ruff's `T20` enforces it.
- Match the surrounding code: comments explain *why*, names are
  descriptive, and test fakes implement only the slice of a third-party API
  the code touches (`LocalShellRunner` and `ScriptedRunner` in
  `tests/conftest.py` stand in for an SSH connection).
- **Nothing outside the sandbox changes without `--execute`.** A dry run
  must never write to the real filesystem of the local machine or of an SSH
  target, outside the sandbox folder; `Simulator` refuses a non-`full`
  sandbox without `execute=True`.
- **Every file operation and command goes through the Host**, never
  `pathlib`/`shutil`/`subprocess` in the executor: the target may be another
  machine and another OS. Paths are strings in the host's flavor.
- **Check user-visible changes with a real run** when qna is available,
  e.g. `uv run bigfix-actionscript-runner path/to/task.bes`.
