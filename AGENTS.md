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

## Running against the inventory's machines

The runner takes one target per run: `--ssh [USER@]HOST`, or none for this
machine. It doesn't read an inventory, but the user's test machines are
listed in the one
[bigfix-remote-client-relevance](https://github.com/jgstew/bigfix-remote-client-relevance#inventory)
uses: `remote_clients.toml`, the first found of

1. `./remote_clients.toml`
2. `~/.bigfix/remote_clients.toml` (under `sudo`, also `$SUDO_USER`'s)
3. the all-users folder: `/Library/Application Support/bigfix` (macOS),
   `/etc/xdg/bigfix` (Linux), `C:\ProgramData\bigfix` (Windows)

It's the user's own config, outside this repo: read it, but don't edit it
or copy its host names into the repo (tests and docs use placeholders like
`admin@mac-test`).

Each `[hosts.NAME]` table maps to runner flags like this; `[defaults]` keys
apply to every host unless the host sets its own:

| `transport` | Runner flags |
|---|---|
| `ssh` (the default when unset) | `--ssh USER@NAME` (`user` key; NAME is the table name: a host name, IP or `~/.ssh/config` alias), plus `--become` when `become = true` and the target is macOS or Linux. `--become` is POSIX-only, so leave it off for `platform = "windows"`. |
| `local` | no target flag: this machine |
| `container`, `fastquery`, `online_evaluator` | not usable: the runner needs a shell and a filesystem on the target, not just a relevance evaluator |

Other keys:
- **`qna_version`:** ignored. The runner uses whatever qna is installed on the target.
- **`platform`/`arch`:** only tell you which `.bes` fits the machine, such as a
  Windows Task for a Windows host.
- **Port and key:** the runner has no flags for these, so set them in
  `~/.ssh/config`. Host keys are always checked against `~/.ssh/known_hosts`,
  even for a host with `verify_host_key = false` in the inventory.

List the SSH hosts as runner flags (needs Python 3.11+ for `tomllib`, so use
`uv run`):

```bash
uv run python - <<'EOF'
import tomllib, pathlib
inv = tomllib.loads(pathlib.Path("~/.bigfix/remote_clients.toml").expanduser().read_text())
d = inv.get("defaults", {})
for name, h in inv.get("hosts", {}).items():
    h = {**d, **h}  # a host's own keys win over [defaults]
    if h.get("transport", "ssh") == "ssh":  # ssh is the inventory's default transport
        user = f"{h['user']}@" if h.get("user") else ""
        become = " --become" if h.get("become") and h.get("platform") != "windows" else ""
        print(f"{name}: --ssh {user}{name}{become}  # {h.get('platform', '?')}/{h.get('arch', '?')}")
EOF
```

Then run on the matching machine. Dry run first, which changes nothing outside
the sandbox, and add `--execute` only when the user asks for it, since these
are real machines:

```bash
uv run bigfix-actionscript-runner path/to/task.bes --ssh admin@mac-test --become
uv run bigfix-actionscript-runner path/to/task.bes --ssh admin@win-test
```

The SSH-marked tests take the same values from the environment:
`BFASR_SSH_POSIX_HOST=USER@NAME` for a macOS or Linux host (plus
`BFASR_SSH_BECOME=1` for macOS), and `BFASR_SSH_WINDOWS_HOST=USER@NAME` for a
Windows one.

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
