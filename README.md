# Strix quota guard

**English** | [Čeština](README.cs.md)

A standalone Python wrapper for **local headless Strix** that monitors your
ChatGPT subscription's weekly quota and requests a graceful stop at **80% used**.
Requires Python 3.9+ and macOS/Linux (POSIX). Standard library only.
Not part of Strix or an official OpenAI integration.

## Installation

```sh
git clone https://github.com/zlukes/strix-quota-guard.git ~/strix-quota-guard
```

No pip packages are required. Install Strix separately. If the directory already
exists, do not overwrite it: use another path or update the existing clone.

## Usage

Sign in using `strix auth login chatgpt` and configure a subscription model
(`chatgpt/<your-model>`), not a paid API model. The guard does not change model
configuration, implement a paid API fallback, or monitor other providers' costs.
A custom `--executable` must be a trusted, compatible Strix installation using
the same HOME and authentication file, not a launcher switching users/profiles.

```sh
python3 ~/strix-quota-guard/strix_quota_guard.py --help

# Read quota once without starting Strix:
python3 ~/strix-quota-guard/strix_quota_guard.py --check

# Run from the working directory where strix_runs/ should be stored.
# Only test targets you are authorized to assess.
python3 ~/strix-quota-guard/strix_quota_guard.py -- -t https://example.invalid

# Optional timing settings (seconds) and executable path:
python3 ~/strix-quota-guard/strix_quota_guard.py \
  --interval 15 --timeout 10 --grace 60 --executable /path/to/strix \
  -- -t https://example.invalid

# After quota becomes available, resume from the SAME working directory:
python3 ~/strix-quota-guard/strix_quota_guard.py -- --resume RUN_NAME
```

`RUN_NAME` is an existing run directory under `./strix_runs/`. The guard preserves
the working directory and does not move or delete state. `--resume` is a **Strix
argument after `--`**, not a guard mode. Resuming performs a fresh quota check;
it will not start at >=80%. There is no automatic wait for reset or restart.
Bare `--resume` without a name is rejected because it would require a picker.

The guard always adds `-n`; interactive mode is not supported. Child stdin is
`/dev/null`; Strix output remains in the terminal. This is not a daemon: a new
POSIX session isolates the child's process group for signals, while the guard
runs in the foreground. **CLI help and diagnostic messages are in English.**

## Example: audit a local project

Run this command from the project directory. Environment variables are passed
to Strix unchanged. The model names illustrate one configuration; availability
depends on your account and Strix version. The original audit instruction below
explicitly requests Czech findings and reports; change that language request if
needed.

```sh
STRIX_LLM="chatgpt/gpt-6.1-sol" \
STRIX_REASONING_EFFORT="low" \
STRIX_DEDUPE_MODEL="chatgpt/gpt-6-luna" \
python3 ~/strix-quota-guard/strix_quota_guard.py -- \
--target ./ --scan-mode quick \
--instruction "I own this repository and authorize a security audit of its source code and isolated local test deployment. Review the code for vulnerabilities, validate only with non-destructive local tests using synthetic data, and recommend fixes. Production systems, external hosts, third-party services, real credentials, and real user data are out of scope. Report affected files and lines, evidence, impact, and remediation. Distinguish confirmed findings from hypotheses. Communicate with me in Czech and write all findings, explanations, and the final report in Czech. Preserve code, commands, file paths, and identifiers in their original form."
```

Specify `--scan-mode quick` only once and change it as needed. Everything after
`--` belongs to Strix; guard options go before it. Instructions define the
authorized scope but do not replace network and data isolation of the test environment.

Once weekly quota is available, resume from the same working directory:

```sh
STRIX_LLM="chatgpt/gpt-6.1-sol" \
STRIX_REASONING_EFFORT="low" \
STRIX_DEDUPE_MODEL="chatgpt/gpt-6-luna" \
python3 ~/strix-quota-guard/strix_quota_guard.py -- \
--resume NAZEV_BEHU
```

Replace `NAZEV_BEHU` with the existing run name. Do not add `--target` when
resuming. Keep `strix_runs/` and the working files intact.

## How it works

1. Before starting Strix, read `~/.strix/subscription-auth.json` and perform only
   `GET https://chatgpt.com/backend-api/wham/usage`.
2. Use the Strix schema `{"codex": {"type": "oauth", "access": "…",
   "refresh": "…", "account_id": "…", "expires_at": 1234567890}}`.
   `expires_at` is Unix time in seconds. Send `Authorization: Bearer <token>` and
   `ChatGPT-Account-Id`. Check that the refresh token exists, but never send it.
3. Find exactly one window in `rate_limit` with `limit_window_seconds == 604800`.
   Use `used_percent`, not the order or name of `primary_window`/`secondary_window`.
   Missing, ambiguous, or invalid weekly data is an error, not 0% usage.
4. Below 80%, start `[strix, -n, ...arguments]` without a shell. By default, check
   again 30 seconds after the previous check completes. No automatic request
   retries. Short-term limits (such as five-hour windows) are not managed.
5. At >=80% **or on a quota-check error**, send the child's process group one
   `SIGINT` and wait for the main child process to exit. A preflight error prevents
   the child from starting at all.

The authentication file is reread on every check to pick up tokens refreshed by
Strix. The guard **never writes the file, refreshes tokens, or rotates them**,
preventing races over a single-use refresh token. Expiry (including a `--timeout`
safety margin), HTTP 401, or an invalid auth file causes a fail-closed stop.
After the run exits, sign in again with `strix auth login chatgpt` and rerun the
guard. Never put tokens in command arguments or logs.

HTTP redirects are rejected and the endpoint cannot be changed through the CLI.
TLS verification remains enabled. Python urllib respects normal proxy environment
settings. Error response bodies, tokens, account IDs, and network exception text
are not logged. Usage responses are limited to 1 MiB. `--timeout` applies to
urllib network operations; it is not a guaranteed end-to-end transaction deadline.

## Shutdown and limitations

- Upstream headless Strix handles SIGINT/SIGTERM/SIGHUP with
  `report_state.cleanup(status="interrupted")`, then exits. The guard forwards
  the first user signal; repeated signals do not interrupt cleanup. An in-flight
  HTTP request may delay signal forwarding.
- After `--grace` seconds (default 60), the guard warns that **the process is still
  running** and continues waiting. **It never sends SIGKILL or claims a process
  stopped merely because a signal was sent.** Grace is a warning threshold, not
  a forced-kill deadline. If the child ignores the signal, waiting may be indefinite;
  inspect the process from another terminal.
- It observes the main child's exit, not proof that every remote task, container,
  or descendant that left its process group has stopped. Strix owns persistence;
  stopping very early may leave no resumable state.
- **80% is not a hard cap.** Usage can increase between checks, during in-flight
  requests, backend reporting delays, and cleanup. Other processes using the same
  account are not stopped. For more margin, shorten the interval and monitor usage.
- The internal endpoint and response schema are not a stable public API. Auth
  compatibility was checked against upstream source, **not live against OpenAI**.
  Tests use synthetic responses and do not establish endpoint availability or
  account permissions. Schema changes cause a fail-closed stop.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | `--check`: below threshold; otherwise successful Strix exit |
| 80 | Quota >=80% before startup or during the run |
| 81 | Authentication, usage, network, or process startup failure |
| 2 | Invalid guard arguments |
| 130 / 143 / 129 | User SIGINT / SIGTERM / SIGHUP forwarded by the guard |
| Other | Normal child exit code is preserved |

80/81 are guard-specific codes, but a child returning the same code cannot be
distinguished by exit status alone. The guard does not reinterpret Strix exit
codes, such as its code 2 for findings. If the child independently exits from a
signal, the shell convention `128 + signal number` is used.

## Tests and provenance

```sh
cd ~/strix-quota-guard
python3 -m unittest -v
```

Tests use no real credentials, network calls, or real Strix. Harmless Python
processes with real signal handlers verify cleanup on quota/error, user signals,
waiting beyond grace, and prevention of startup after a failed preflight.
Temporary test files use Python's standard temporary directory. Set
`STRIX_GUARD_TEST_TMPDIR` to use a custom existing directory.

Implementation references upstream Strix (`strix/config/codex.py`,
`strix/interface/cli.py`, `strix/interface/cli_args.py`), commit
`685bbc8138d35fc9fe378ac37ec0d9198b9c46a3`.
