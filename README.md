# bigfix-actionscript-runner

Dry-run the ActionScript in a BigFix `.bes` file (or a plain ActionScript file)
without a BigFix server or agent:

- **Real relevance substitution:** every `{expression}` is evaluated by `qna`
  through [bigfix-remote-client-relevance](https://github.com/jgstew/bigfix-remote-client-relevance),
  on this machine (`--local`) or on another one over SSH (`--ssh`).
- **Preflight:** relevance is checked first with
  [bigfix-relevance-analyzer](https://github.com/jgstew/bigfix-relevance-analyzer)
  and the ActionScript with [pre-commit-bigfix](https://github.com/jgstew/pre-commit-bigfix)
  (never with auto-fix). Errors stop the run before anything happens;
  warnings never do (see [Preflight](#preflight)).
- **Baseline, then every step:** every expression is evaluated once before
  line 1, and again at the step that uses it, because an earlier step can
  change the answer (an error before a prefetch can be a file name after it).
  The trace notes each value that changed since the baseline.
- **Sandboxed file commands:** `createfile`, `copy`, `move`, `delete`,
  `folder create/delete` work on a sandbox folder that mimics the client's
  folders. Nothing outside the sandbox is changed.
- **Real prefetches:** prefetch statements and items are parsed, validated,
  downloaded and compared with [bigfix-prefetch](https://github.com/jgstew/bigfix-prefetch)
  into the sandbox's `__Download` (size, SHA-1, SHA-256).
- **Commands are shown, not run:** `run`, `wait`, `waithidden`, `dos`, ... print
  their fully substituted command line. `--execute` really runs them.

```bash
uvx bigfix-actionscript-runner "Scan for PHP Installs with YARA-X - macOS.bes"
```

Until it is published, run it from a checkout:

```bash
uvx --from ~/Projects/bigfix-actionscript-runner bigfix-actionscript-runner path/to/task.bes
```

(`uvx` caches the build of a local checkout by version; after changing the
code, add `--no-cache`, or use `uv run bigfix-actionscript-runner ...` inside the
checkout.)

pre-commit-bigfix isn't on PyPI yet, so it is a git dependency, and PyPI
rejects packages with git dependencies: it needs publishing first
([jgstew/pre-commit-bigfix#46](https://github.com/jgstew/pre-commit-bigfix/issues/46)).

## Example

```text
Scan for PHP Installs with YARA-X - macOS  [Action1]
Relevance evaluated on: local
Sandbox: /var/folders/.../bigfix-actionscript-runner-k2j3h4
Preflight: 24 relevance expression(s) checked, 0 error(s), 5 warning(s)
  line 46: warning [non-unique-risk] 'pathname' is written singular over an object that may be plural; ...

   38  prefetched add prefetch item name=yara-x-v1.22.0-aarch64-apple-darwin.tar.gz sha1=... size=8421133 url=...
                    <sandbox>/site/__Download/yara-x-v1.22.0-aarch64-apple-darwin.tar.gz (verified size, sha1, sha256)
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
| `--ssh USER@HOST` | Evaluate relevance on that machine over SSH instead of locally. |
| `--become` | Run `qna` under sudo on the `--ssh` target (needed for macOS targets; passwordless sudo). |
| `--execute` | Really run `run`/`wait`/... commands, from the sandbox's site folder. Not with `--ssh`. |
| `--param NAME=VALUE` | Value for an `action parameter query` (repeatable). Otherwise its default is used. |
| `--set 'EXPR=>VALUE'` | Fixed answer for an expression (repeatable), e.g. for an inspector the evaluating machine doesn't have. |
| `--exit-code N` | Exit code assumed for `wait` commands that aren't run (default 0). |
| `--relevance` | Also evaluate the item's own relevance clauses first; says so if a client wouldn't run the action. |
| `--no-baseline` | Skip the up-front evaluation of every expression (each is still evaluated at its step). |
| `--no-download` | Placeholder files instead of downloading prefetches. |
| `--no-preflight` | Skip the preflight checks. |
| `--sandbox DIR` | Sandbox folder (default: a new temporary folder, kept afterwards). |
| `--json` | A JSON report instead of the text trace. |
| `--timeout S` | Seconds per relevance evaluation (default 60). |

Exit codes: `0` the action completed (or ran `exit`), `1` it failed (the
message names the line), `2` usage or load error, `3` preflight errors.

## Preflight

| Checker | Checks | Blocks the run | Reported only |
|---|---|---|---|
| bigfix-relevance-analyzer | the item's relevance and every `{...}` | parse and type errors | style/risk rules (`non-unique-risk`, `plural-preferred`, ...) |
| pre-commit-bigfix (`bes-actionscript-validate-script`, `-lint-schclass`, `-validate-prefetch`) | ActionScript structure, commands, prefetch lines | E-codes: things a client rejects or fails on (`E500` unclosed `if`, `E300` unknown command, `E400` bad prefetch, ...) | W-codes, and the convention-only `E401` (no sha256), `E402` (old unzip), `E304`/`E404` |

## How relevance is evaluated

`qna` has no action context, so before an expression is evaluated:

- `parameter "x"` becomes the parameter's value (a string literal),
- `id of action` and `exit code of action` become numbers (the exit code of
  the last `wait`, or `--exit-code` when commands aren't run),
- `data folder(s) of client` and `client folder(s) of current site` point
  into the sandbox, so relevance about files the action created sees them.

Before line 1 every distinct expression is evaluated once (the baseline),
with `action parameter query` values already known (the operator answers
them before an action starts). Expressions that use a parameter set later by
a `parameter` line are deferred instead. Each expression is evaluated again at
its step, and the trace notes where the value differs from the baseline:

```text
   54  ok         copy "__Download/{name of file whose(name of it starts with "yara-x-v") of folder "__Download" ...
                    {name of file whose(...)...}: baseline error (Singular expression refers to nonexistent object.) -> now "yara-x-v1.22.0-x86_64-apple-darwin.tar.gz"
```

With `--ssh`, expressions that refer to the sandbox are evaluated locally
(the remote machine can't see it); everything else, such as `x64 of
operating system`, `pathname of system folder` or registry checks, is
evaluated on the remote machine. On macOS `qna` needs root, so local
evaluation uses `sudo` (see bigfix-remote-client-relevance).

An action is simulated against the machine that evaluates its relevance: a
Linux task simulated on a Mac, for example, gets the Mac's answers (its
prefetch `if` conditions are false, so nothing is downloaded). Use `--ssh` to
a matching machine, or `--set` for individual expressions.

## The sandbox

```text
<sandbox>/
  site/               the current site's client folder: relative paths, __createfile, __appendfile
  site/__Download/    prefetched and downloaded files
  __BESData/          data folder of client (__Global/Logs, ...)
  fs/                 any other absolute path: /tmp/x -> fs/tmp/x, C:\x -> fs/C/x
```

Prefetches with hashes are downloaded by bigfix-prefetch, whose download
doesn't check TLS certificates (the hashes are checked afterwards). A
`download URL` with no hashes is fetched with TLS verification instead.

Reads (the source of a `copy`) fall back to the real file when the sandbox
has no copy of it, so an action can copy real files in. Verified prefetch
downloads are cached by SHA-256 so repeated runs don't download them again.

## What is simulated

| ActionScript | |
|---|---|
| `parameter`, `action parameter query` | Set; redefining a parameter fails, like on a client. |
| `createfile until`, `appendfile` | Written to the sandbox, with `{{` as an escaped `{`. |
| `copy`, `move`, `delete`, `folder create`, `folder delete` | In the sandbox; `copy`/`move` fail if the source is missing or the destination exists. |
| `if`/`elseif`/`else`/`endif`, `continue if`, `exit`, `pause while` | Evaluated; `pause while` only warns. |
| `prefetch`, `add prefetch item`, `download`, `begin/end prefetch block` | Handled before line 1 (including `if` in the block); `download now` at its line. |
| `run`, `wait`, `waithidden`, `runhidden`, `rundetached`, `dos`, `script`, `override` | Shown, or run with `--execute` (with the `override` timeout). |
| `action uses wow64 redirection`, `action requires restart`, `setting`, `regset`, `regdelete`, `notify client`, `utility`, ... | Recorded, not performed. |
| Anything else | A warning; the action continues. |

Only ActionScript is simulated; actions in other script types (sh,
AppleScript, PowerShell) are refused.

## Development

```bash
uv sync
uv run pytest                      # offline unit tests; live_qna tests run when qna is installed
BFASR_NETWORK_TESTS=1 uv run pytest   # + real downloads
uv run ruff check . && uv run mypy src && uv run pyright
```

Every part is tested with a fake relevance evaluator and downloader, so the
default test run needs no `qna` and no network.
