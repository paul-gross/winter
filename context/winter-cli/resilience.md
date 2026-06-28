# Network resilience and drift warnings

Cross-cutting behavior of the remote-git commands and the repo-iterating commands. For the hub and the command surface, see [index.md](./index.md).

## Network resilience

Fetch / pull / push silently retry up to 3 times with jittered exponential backoff when git emits a transient error. Recognized transient stderr substrings include `Connection closed by … port 22`, `kex_exchange_identification`, "remote end hung up", and "Connection timed out" — anything else is reported as a hard failure on the first try. You'll see `transient git error (attempt N/3): … — retrying in Xs` lines on stderr while a command is retrying.

Every remote git invocation is bounded by a per-call timeout (default **40s**). If the underlying `git fetch` / `git push` hangs past that — a wedged TCP socket, an unresponsive SSH server — the subprocess is SIGKILL'd (taking its `ssh` child with it) and the failure flows back through the same retry+backoff path as any other transient error. A persistent hang surfaces as a typed error after `MAX_ATTEMPTS`, not an indefinite block.

Two knobs:

- `WINTER_GIT_TIMEOUT_S` — override the per-call timeout (float seconds). Bump this when a sizeable push or a slow link genuinely needs longer than 40s; an invalid value is ignored with a warning and the default is used.
- `GIT_SSH_COMMAND` — the CLI installs `ssh -o ConnectTimeout=10 -o ServerAliveInterval=30 -o ServerAliveCountMax=3` by default so SSH itself detects a half-dead connection in ~90s. Set this yourself before running `winter` to override (identity file, custom port, ProxyCommand, etc.); your value wins.

## Drift warnings

Operations that iterate repos (`ws list`, `ws status`, `ws fetch`, `ws pull`, `ws push`, `ws merge`, `ws connect`, `ws disconnect`, `ws diff`, `repo list`) warn to stderr when the config and filesystem disagree:

- **Missing:** a declared project repo has no directory under `projects/` — run `winter ws init`
- **Undeclared:** a directory under `projects/` is not in the config — add it to `.winter/config.toml` or remove it

`winter ws init` treats both cases as actionable rather than a warning: missing repos are cloned; undeclared directories are left alone.

- **Missing non-pinned upstream (env path):** when a non-pinned worktree in a feature environment has no upstream and its connected non-pinned siblings all agree on one, `winter ws init <env>` auto-connects it to that inferred ref. Divergent or no-sibling cases are left for explicit `winter ws connect` — see [worktree-ops.md](../worktree-ops.md#creating-a-feature-environment) for the full contract.

Drift detection currently covers project repos only. Missing or undeclared standalone repos are not warned about; if a `[[standalone_repository]]` entry's directory is missing, `winter ws init` clones it on the next run.
