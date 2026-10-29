# bigfix-actionscript-runner

Run the ActionScript in a BigFix `.bes` file (or a plain ActionScript file)
the way a BigFix client would, without a BigFix server, on this machine or on
a test machine over SSH. By default it is a dry run; `--execute` really runs
the action so its changes can be measured.

- **Real relevance substitution:** every `{expression}` is evaluated by `qna`
  through [bigfix-remote-client-relevance](https://github.com/jgstew/bigfix-remote-client-relevance),
  on the machine the action runs on.
- **Runs on the target:** with `--ssh USER@HOST`, the sandbox, the file
  commands, the commands and the relevance all happen on that machine
  (macOS, Linux or Windows). Nothing has to be set up there beyond SSH and `qna`.
- **Only where it is relevant:** like a client, the action runs only when
  every one of the item's relevance clauses is True on the target;
  `--force` runs it anyway (handy for running it again while testing).
- **Preflight:** relevance is checked first with
  [bigfix-relevance-analyzer](https://github.com/jgstew/bigfix-relevance-analyzer)
  and the ActionScript with [pre-commit-bigfix](https://github.com/jgstew/pre-commit-bigfix)
  (never with auto-fix). Errors stop the run before anything happens;
  warnings never do (see [Preflight](#preflight)).
- **Baseline, then every step:** every expression is evaluated once before
  line 1, and again at the step that uses it, because an earlier step can
  change the answer (an error before a prefetch can be a file name after it).
  The trace notes each value that changed since the baseline.
- **A sandbox for the client folders:** a dry run keeps every path in a
  sandbox; `--execute` sandboxes only the client folders and lets every other
  path be real; `--no-sandbox` uses the real client's folders too (see
  [Sandbox modes](#sandbox-modes)).
- **Real prefetches:** prefetch statements and items are parsed, validated,
  downloaded and compared with [bigfix-prefetch](https://github.com/jgstew/bigfix-prefetch)
  (size, SHA-1, SHA-256), then put in the target's `__Download` and hashed
  again there.

It's on [PyPI](https://pypi.org/project/bigfix-actionscript-runner/), so it
runs with
`uvx bigfix-actionscript-runner "Scan for PHP Installs with YARA-X - macOS.bes"`.
To run your own changes, run it from a checkout:

```bash
uvx --from ~/Projects/bigfix-actionscript-runner bigfix-actionscript-runner path/to/task.bes
```

(`uvx` caches the build of a local checkout by version; after changing the
code, add `--no-cache`, or use `uv run bigfix-actionscript-runner ...` inside
the checkout.)

### Verifying a release

Releases after 0.2.1 are signed three ways. 0.2.1 has the checksums only.

- **Checksums:** each GitHub release has a `SHA256SUMS.txt`, also printed in
  its notes. Check the downloaded files with `sha256sum -c SHA256SUMS.txt`.
- **Build provenance:** each wheel and sdist has a GitHub artifact
  attestation, and its Sigstore bundle
  (`bigfix_actionscript_runner-<version>.sigstore.json`) is a release asset.
  Check a file with
  `gh attestation verify <file> --repo jgstew/bigfix-actionscript-runner`.
- **PyPI:** the files on PyPI carry PEP 740 attestations from the same
  workflow, shown as provenance on each file's page.

## Examples

```bash
# dry run here
bigfix-actionscript-runner task.bes
# dry run on a Mac test machine (qna needs root there)
bigfix-actionscript-runner task.bes --ssh admin@mac-test --become
# really run it on a Windows test machine; only the client folders are sandboxed
bigfix-actionscript-runner task.bes --ssh admin@win-test --execute
# really run it in the real client's folders for the site "My Site"
bigfix-actionscript-runner task.bes --ssh admin@linux-test --become --execute --no-sandbox --site "My Site"
```

```text
Scan for PHP Installs with YARA-X - macOS  [Action1]
Target: admin@mac-test
Sandbox (everything; dry run): /var/folders/.../bigfix-actionscript-runner-k2j3h4
Preflight: 24 relevance expression(s) checked, 0 error(s), 5 warning(s)
  line 46: warning [non-unique-risk bigfix-relevance-analyzer] 'pathname' is written singular ...
Relevance:
  True  <=  mac of operating system
  True  <=  not exists files whose(name of it as lowercase starts with "yara_results_php.text" ...
Relevant on admin@mac-test
Baseline: 9 expression(s), 1 error(s) before line 1, 3 deferred (use parameters set later)

   38  prefetched add prefetch item name=yara-x-v1.22.0-x86_64-apple-darwin.tar.gz sha1=... size=9476144 url=...
                    <sandbox>/site/__Download/yara-x-v1.22.0-x86_64-apple-darwin.tar.gz (verified size, sha1, sha256)
   46  ok         parameter "destination_folder" = "{pathname of folders "Logs" of folders "__Global" of data folders of client}"
                 -> parameter "destination_folder" = "<sandbox>/__BESData/__Global/Logs"
   ...
  555  shown      run /bin/bash "{parameter "temp_folder"}/run_scan.sh"
                 -> run /bin/bash "<sandbox>/__BESData/__Global/yara_php_scan/run_scan.sh"
                    not run

Completed
```

## Options

| Option | |
|---|---|
| `--action ID` | Which action of a Fixlet/Task (default: the DefaultAction). |
| `--ssh USER@HOST` | Run on that machine over SSH: relevance, sandbox, file commands and commands. |
| `--become` | On a macOS/Linux `--ssh` target, run `qna`, file commands and commands under `sudo -n` (a client runs actions as root; macOS `qna` needs it). Needs passwordless sudo. Only with `--ssh`. |
| `--execute` | Really run the action: commands run, and paths outside the client folders are the real ones. |
| `--no-sandbox` | With `--execute`: use the real client's folders as well. Needs `--site`. |
| `--site NAME` | With `--no-sandbox`: the site whose client folder is "the current site". |
| `--param NAME=VALUE` | An action parameter's value (repeatable): for an `action parameter query` (otherwise its default is used), a secure parameter, or any parameter a console would set. A `parameter` line in the script still sets its own. |
| `--set 'EXPR=>VALUE'` | Fixed answer for an expression as written in the action (repeatable), e.g. for an inspector the target doesn't have. |
| `--exit-code N` | Exit code assumed for `wait` commands that aren't run (default 0). |
| `--force` | Run even when the item's relevance isn't True on the target (it is still evaluated and shown). |
| `--no-baseline` | Skip the up-front evaluation of every expression (each is still evaluated at its step). |
| `--no-download` | Placeholder files instead of downloading prefetches. |
| `--no-preflight` | Skip the preflight checks. |
| `--sandbox DIR` | Sandbox folder on the target (default: a new temporary folder there, kept afterwards). |
| `--json` | A JSON report instead of the text trace. |
| `--timeout S` | Seconds per relevance evaluation (default 60). |

Exit codes: `0` the action completed (or ran `exit`), `1` it failed (the
message names the line), `2` usage, load or connection error, `3` preflight
errors, `4` not relevant on the target, so not run (without `--force`). With `--json`, errors are JSON too: `{"status": "error", "message": ...}`.

## Sandbox modes

| | Client folders (site folder, `__Download`, data folder of client) | Every other path | Commands |
|---|---|---|---|
| default (dry run) | sandbox | sandbox (`fs/`) | shown, not run |
| `--execute` | sandbox | **real** | run |
| `--execute --no-sandbox --site NAME` | the real client's | **real** | run |

`--execute` changes the target for real: a `copy` to `/usr/local/bin/x` or
`C:\Program Files\...` happens, a `delete` deletes. Use test machines.
`--no-sandbox` writes into a real BigFix client's site folder, so the client
must be installed there; its folders are found with `pathname of client
folder of site "NAME"` and `pathname of data folder of client`.

The sandbox's layout:

```text
<sandbox>/
  site/               the current site's client folder: relative paths, __createfile, __appendfile
  site/__Download/    prefetched and downloaded files
  __BESData/          data folder of client (__Global/Logs, ...)
  fs/                 dry run only: any other absolute path, /tmp/x -> fs/tmp/x, C:\x -> fs/C/x
```

Prefetch and `download as` names must be plain file names (no `/`, `\`,
`:` or `..`): they come from the .bes file and land in `__Download`. On a
Windows target a rooted path such as `\Temp\x` is on the site folder's drive,
`\\?\C:\x` is `C:\x`, and a `:` inside a sandboxed path can't switch drives.

In a dry run, reads (the source of a `copy`) fall back to the real file when
the sandbox has no copy of it, so an action can copy real files in, and a
`move` of a real file copies it instead. The sandbox folder is kept
afterwards so it can be inspected; `folder delete` never removes the client
folders themselves.

## SSH targets

Everything an action does goes over one SSH connection to the target:

- **macOS and Linux:** each operation is an `sh -c` command (under `sudo -n`
  with `--become`); prefetched files go over SFTP.
- **Windows:** each operation is PowerShell, run as `powershell
  -EncodedCommand` because Windows OpenSSH's login shell is `cmd.exe` unless
  changed. Commands are started with `Start-Process`, which hands their
  arguments to the program as one command line, like a client does.
  A Windows target is recognized by `uname -s` failing, or answering
  `MSYS_NT`/`MINGW`/`CYGWIN` (Git for Windows puts `uname` on PATH).
- **Relevance** is evaluated by `qna` on the target through
  bigfix-remote-client-relevance; client folders point into the target's
  sandbox, so relevance sees what the action wrote.
- **Prefetches** are downloaded and verified here (cached by SHA-256), put on
  the target, and their SHA-256 checked again there, much like a relay
  serving a client.

The SSH user's host keys and keys come from `~/.ssh` as for the `ssh` command.

## Preflight

| Checker | Checks | Blocks the run | Reported only |
|---|---|---|---|
| bigfix-relevance-analyzer | the item's relevance and every `{...}` | parse and type errors | style/risk rules (`non-unique-risk`, `plural-preferred`, ...) |
| pre-commit-bigfix (`bes-actionscript-validate-script`, `-lint-schclass`, `-validate-prefetch`) | ActionScript structure, commands, prefetch lines | E-codes: things a client rejects or fails on (`E500` unclosed `if`, `E300` unknown command, `E400` bad prefetch, ...) | W-codes, and the convention-only `E401` (no sha256), `E402` (old unzip), `E525` (unquoted `folder create` path), `E304`/`E404` |

## How relevance is evaluated

The item's own relevance clauses (a Fixlet's or Task's `<Relevance>`) are
evaluated first, on the target, with the same action context as the steps
(so `client folder of current site` is the sandbox's). The action runs only
if every clause is True, as on a client; an error counts as not relevant.
Otherwise nothing runs, a temporary sandbox is removed, and the exit code is
`4`. `--force` runs it anyway. A plain ActionScript file has no relevance and
always runs.

`qna` has no action context, so before an expression is evaluated:

- `parameter "x"` becomes the parameter's value (a string literal);
  `parameter "action issue date" of action` is the run's start time, in a
  client's format (`Wed, 22 Oct 2026 05:00:00 +0000`),
- `exists parameter "x"` becomes `true` or `false`: whether it is set (by
  the script so far, `--param`, or a built-in),
- `id of action` and `exit code(s) of action` become numbers (the exit code of
  the last `wait`, or `--exit-code` when commands aren't run), with `of
  active action` too,
- `data folder(s) of client` and `client folder(s) of current site` point
  at the sandbox's folders (or the real ones with `--no-sandbox`), and
  `download folder` / `download file "x"` at its `__Download` folder.

These are rewritten on bigfix-relevance-analyzer's parse tree, so text in
string literals is never touched. Which inspectors need a running client is
recorded from real `qna` runs in `tests/data/client_context/`; refresh it with
`scripts/probe_client_context.py`.

Before line 1 every distinct expression is evaluated once (the baseline),
with `action parameter query` values already known (the operator answers
them before an action starts). Expressions that use a parameter set later by
a `parameter` line are deferred instead. Each expression is evaluated again at
its step, and the trace notes where the value differs from the baseline:

```text
   54  ok         copy "__Download/{name of file whose(name of it starts with "yara-x-v") of folder "__Download" ...
                    {name of file whose(...)...}: baseline error (Singular expression refers to nonexistent object.) -> now "yara-x-v1.22.0-x86_64-apple-darwin.tar.gz"
```

Some inspectors exist only in a running BigFix client, so `qna` answers
"No inspector context" (for example `computer id`, `last command time of
client`). Give such an expression an answer with `--set`, which matches the
whole expression as written in the action.

An action gets the answers of the machine it runs on: a Linux task run on a
Mac gets the Mac's answers (its Linux `if` conditions are false, so
top-level prefetches in those branches are downloaded but never placed in
`__Download`, and a prefetch block downloads nothing for them). Use `--ssh` to a matching machine, or
`--set` for individual expressions.

Prefetches with hashes are downloaded by bigfix-prefetch, whose download
doesn't check TLS certificates (the hashes are checked afterwards). A
`download URL` with no hashes is fetched with TLS verification instead.

## What is run

| ActionScript | |
|---|---|
| `parameter`, `action parameter query` | Set; redefining a parameter fails, like on a client. |
| `createfile until`, `appendfile` | Written to the site folder, with `{{` as an escaped `{`. |
| `copy`, `move`, `delete`, `folder create`, `folder delete` | At the paths the sandbox mode gives; `copy`/`move` fail if the source is missing or the destination exists. |
| `if`/`elseif`/`else`/`endif`, `continue if`, `exit`, `pause while` | Evaluated; `pause while` only warns. |
| `prefetch`, `add prefetch item`, `download`, `begin/end prefetch block` | Prefetch blocks run before line 1, including the `if`s in them. Top-level `prefetch`/`download` statements are downloaded and verified before line 1, like a client's cache, and copied into `__Download` when the statement runs, so only taken branches place a file. `download now` at its line. In `--json`, `staged` lists the top-level prefetches downloaded before line 1 and `prefetched` the files placed in `__Download`. |
| `run`, `wait`, `waithidden`, `runhidden`, `rundetached`, `dos`, `script`, `override` | Shown, or run with `--execute` from the site folder (with the `override` timeout). On Windows the arguments are passed exactly as written, for the program to parse, and `dos` runs through `cmd.exe /c`. |
| `action uses wow64 redirection`, `action requires restart`, `setting`, `regset`, `regdelete`, `notify client`, `utility`, ... | Recorded, not performed. |
| Anything else | A warning; the action continues. |

Only ActionScript is run; actions in other script types (sh, AppleScript,
PowerShell) are refused. Keywords may be followed directly by a brace, as
clients accept (`if{...}`, `continue if{...}`, `pause while{...}`). A file is
read as BES XML by its content, so `.bes.xml` exports work too.

## Development

```bash
uv sync
uv run pytest                          # unit tests; live_qna tests too when qna is installed
BFASR_NETWORK_TESTS=1 uv run pytest    # + real downloads
BFASR_SSH_POSIX_HOST=<USER>@<MAC_OR_LINUX_TEST_HOST> BFASR_SSH_BECOME=1 \
  BFASR_SSH_WINDOWS_HOST=<USER>@<WINDOWS_TEST_HOST> uv run pytest -m "ssh_posix or ssh_windows"
uv run ruff check . && uv run mypy src && uv run pyright
```

Unit tests use a fake relevance evaluator and downloader, and an "SSH
connection" that runs commands in a local shell, so the default test run
needs no `qna`, no network and no SSH target. The SSH tests change files on
the target (in a temp folder they remove), so only point them at test
machines.
