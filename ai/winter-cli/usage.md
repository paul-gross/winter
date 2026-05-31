# Winter CLI — Usage

Command reference for agents executing `winter` commands. For installation and configuration, see [setup.md](./setup.md).

## When to use the CLI vs raw git

**Use the CLI** for operations that span multiple repos — init, status, fetch, pull, connect, push, diff. The CLI handles pinned repos, parallel fetching, source checkout fast-forwarding, and idempotent setup automatically.

**Use raw git** for single-repo operations — staging files, committing, resolving conflicts, interactive rebase, branch inspection. The CLI doesn't replace git for per-repo work.

## `winter ws init` — reconcile the workspace against the config

One idempotent command with three modes. Safe to re-run any time.

| Form | What it reconciles |
|------|--------------------|
| `winter ws init` | Source checkouts in `projects/` and standalone repos. |
| `winter ws init <name>` | The `./<name>/` feature environment. |
| `winter ws init --all` | Source checkouts, standalones, and every existing feature environment. |

Each mode applies the same per-repo reconcile steps (git identity, excludes, `cmd` list, extension processing, pinned-repo tracking on worktrees). See [worktree-ops.md](../worktree-ops.md) for the full step list and the pinned-repo specifics.

Greek letters (`alpha`, `beta`, …) are the suggested convention for feature environment names because they carry a fixed port-offset index 1..24. Any other valid directory name is accepted and gets a deterministic SHA-1-derived index in the range 26..281 (index 25 is reserved as a buffer). Hash collisions among non-Greek names are possible but unlikely.

## Workspace commands (`winter ws`)

| Command | Usage | Purpose |
|---------|-------|---------|
| `winter ws init` | `winter ws init [TARGET] [--all] [--json]` | Reconcile source checkouts or a feature environment |
| `winter ws destroy` | `winter ws destroy ENV [--force\|--strict\|--dry-run] [--json]` | Tear down a feature env: fire `on_env_destroy` hooks, then remove every per-repo worktree and the env directory |
| `winter ws checkout` | `winter ws checkout ENV FEATURE_BRANCH [--force] [--json]` | Adopt a remote feature branch into ENV, all-or-nothing across every repo (no network — run `winter ws fetch` first if needed) |
| `winter ws list` | `winter ws list [--json]` | List all feature environments |
| `winter ws status` | `winter ws status [ENV] [--json]` | Git status across all repos in a feature environment |
| `winter ws fetch` | `winter ws fetch [PATTERNS...] [--standalone\|--all] [--json]` | Fetch refs from `origin` for project worktrees matched by PATTERNS, and fast-forward each matched source checkout's local main |
| `winter ws pull` | `winter ws pull [PATTERNS...] [--standalone\|--all] [--ff-only\|--merge\|--rebase] [--autostash] [--json]` | Fetch + ff-only integrate (default) project worktrees matched by PATTERNS |
| `winter ws merge` | `winter ws merge SOURCE_REF [PATTERNS...] [--standalone\|--all] [--ff-only\|--merge\|--no-ff] [--autostash] [--exclude-pinned\|--only-pinned] [--json]` | Merge an arbitrary SOURCE_REF (env name, branch, `origin/...`) into project worktrees matched by PATTERNS |
| `winter ws push` | `winter ws push [PATTERNS...] [--standalone\|--all] [--include-pinned\|--only-pinned] [--json]` | Push project worktrees matched by PATTERNS to their tracked upstream |
| `winter ws connect` | `winter ws connect ENV FEATURE_BRANCH [--json]` | Connect a feature environment to a remote feature branch |
| `winter ws disconnect` | `winter ws disconnect ENV [--json]` | Disconnect a feature environment from its feature branch |
| `winter ws diff` | `winter ws diff ENV [--staged\|--branch] [--repo REPO] [--no-headers] [--json]` | Unified diff across all repos in a feature environment (`--no-headers` omits the per-repo separator headers) |
| `winter ws index` | `winter ws index NAME [--json]` | Print the port-offset index for a feature environment name (Greek = 1..24, other = hashed 26..281) |
| `winter ws prune` | `winter ws prune [--dry-run\|--force] [--json]` | Remove disk state for repos no longer in the workspace config (orphan project clones, orphan standalone clones, broken `.claude/` symlinks). Refuses repos with uncommitted changes or attached worktrees |
| `winter ws worktrees` | `winter ws worktrees [--status] [--json]` | List every existing feature-environment worktree and standalone repo as a flat table or JSON array — intended for editor integrations (e.g. Neovim fuzzy-finder `cd` picker). Omits entries whose directory does not exist on disk. `--status` adds per-repo git status (ahead/behind/dirty) at the cost of a git call per repo |

### `fetch` / `pull` / `push` / `merge` patterns and scope

All four commands accept any number of segment-aware glob `PATTERNS` over `<env>/<repo>` (no patterns = `*/*`). A bare env name is treated as `<env>/*`. Standalone repos are reached via `--standalone` / `--all` and ignore `PATTERNS` — to operate on a single standalone repo, use raw git. `merge` takes a required `SOURCE_REF` as its first positional, then patterns trail; the other three take patterns only.

| Invocation | Operates on |
|------------|-------------|
| `winter ws <cmd>` | every env's project worktrees |
| `winter ws <cmd> alpha` | `alpha`'s project worktrees (== `alpha/*`) |
| `winter ws <cmd> alpha/winter` | one specific worktree |
| `winter ws <cmd> '*/winter'` | every env's `winter` worktree |
| `winter ws <cmd> 'alpha/*' 'beta/*'` | `alpha` + `beta` worktrees |
| `winter ws <cmd> --standalone` | every standalone repo (no project worktrees) |
| `winter ws <cmd> --all` | project worktrees + every standalone repo |
| `winter ws <cmd> '*/winter' --all` | every env's `winter` worktree + every standalone repo |

Pinned-scope behavior per command:

| Command | _(default)_ | Opt-in / opt-out flags |
|---------|-------------|------------------------|
| `fetch` / `pull` | both | n/a — always include both |
| `push` | non-pinned only | `--include-pinned` (+ pinned), `--only-pinned` (pinned only) |
| `merge` | both | `--exclude-pinned` (non-pinned only), `--only-pinned` (pinned only) |

Mutex rules: pinned-scope flags are mutually exclusive within a command (`--include-pinned` xor `--only-pinned` for push; `--exclude-pinned` xor `--only-pinned` for merge); `--standalone` xor `--all`; `--standalone` rejects PATTERNS, and on `push`/`merge` also rejects the pinned-scope flags.

Pattern syntax: `*` matches any chars within a segment (does not cross `/`); `?` matches one char. Quote patterns in your shell to prevent expansion.

**`pull` per-repo target ref.** Non-pinned project worktrees pull from `origin/<feature-branch>` (set by `winter ws connect`). Pinned project worktrees pull from `origin/<main-branch>` because they don't participate in feature branching. Standalone repos pull from whatever their local branch tracks.

**`pull` integration mode** (mutually exclusive, default `--ff-only`):

| Flag | Behavior |
|------|----------|
| `--ff-only` (default) | Fast-forward or report diverged — never produces a merge commit or rewrites history |
| `--merge` | Fall back to a 3-way merge commit when ff-only fails |
| `--rebase` | Replay local commits onto the upstream tip when ff-only fails |

`--autostash` (orthogonal) passes through to `git merge` / `git rebase`, which stash a dirty working tree before integrating and restore it after. If autostash fails, git aborts and the repo is reported as diverged.

**`push` per-repo target.** Non-pinned project worktrees push `HEAD:refs/heads/<feature-branch>`. Pinned project worktrees (when included via `--include-pinned` or `--only-pinned`) and standalone repos plain-push to whatever their local branch tracks (typically `origin/<main>`, but they will follow any custom upstream you set with `git branch --set-upstream-to`). Only repos with commits ahead of upstream are pushed.

`push` excludes pinned worktrees by default because pinned repos track the main branch and aren't part of the feature-push flow. Use `--include-pinned` when you've landed commits on a pinned repo's main branch and want to ship them, or `--only-pinned` to ship just those without touching feature branches.

**`fetch` and source checkouts.** Beyond refreshing remote-tracking refs, `fetch` fast-forwards each matched source checkout's local main (`projects/<repo>`) to `origin/<main-branch>`. Worktrees of a project repo share the source checkout's `.git`, so this is a single fetch per repo that both updates the shared refs every worktree sees and keeps the base `winter ws init` branches new envs off of current. Feature worktrees are never touched. A diverged source checkout main (it should only ever track main) is reported as a failed fetch for that repo.

### `merge` — fold an arbitrary ref into matched worktrees

`merge` is a sibling of `pull` with an explicit source ref. Read `pull` for the shared behavior (patterns, scope flags, autostash semantics, abort-on-conflict reporting); the deltas are:

- `SOURCE_REF` is an explicit positional arg, applied verbatim per repo. `pull` uses each worktree's tracked upstream.
- `--no-ff` replaces `--rebase` in the mode trio — force a merge commit even when fast-forward is possible (matches `git merge --no-ff`). `--ff-only` (default) and `--merge` behave exactly as in `pull`.
- No fetch. `pull` fetches first; `merge` doesn't, because `SOURCE_REF` is often a local branch. Run `winter ws fetch` first if you need fresh refs.
- Pinned worktrees are included by default (`pull` always includes them; `push` excludes by default). Opt out with `--exclude-pinned` or restrict to pinned with `--only-pinned`.

```bash
winter ws merge alpha gamma            # merge alpha into gamma's project worktrees (== 'gamma/*')
winter ws merge master gamma           # merge master into gamma's project worktrees
winter ws merge origin/master gamma    # explicit remote ref also accepted
winter ws merge master '*/winter'      # merge master into every env's winter worktree
winter ws merge master --all           # merge master into every env's worktrees + every standalone
```

**Per-repo outcomes** (literal CLI output):

| Outcome | Meaning |
|---------|---------|
| `up-to-date` | Source ref already reachable from HEAD; nothing to do |
| `fast-forwarded` | HEAD advanced to source ref without a merge commit |
| `merged (merge commit created)` | A merge commit was created (only under `--merge` or `--no-ff`) |
| `diverged: +N/-N` | `--ff-only` refusal, or a conflict during `--merge`/`--no-ff` that aborted the merge |
| `skipped: source ref not found` | The source ref doesn't resolve in this repo |

Exit code is `0` when every selected repo merged cleanly (`up-to-date`, `fast-forwarded`, or `merged (merge commit created)`), and `1` if any repo diverged or had a missing source ref. Cross-repo atomicity is not provided — if one repo merges cleanly and another diverges, the clean merge stays. Conflicts that abort don't leave a merge in progress; use raw `git reset --hard ORIG_HEAD` per repo if you want to undo a fast-forward or merge commit.

**When to use `merge` vs `pull`:**

- `merge` — the source ref isn't the worktree's tracked upstream (env-to-env, explicit branch). Offline; doesn't fetch.
- `pull` — integrate the tracked upstream (feature branch for non-pinned, main for pinned). Fetches first.

### `destroy` — tear down a feature env

`winter ws destroy ENV` is the symmetric counterpart to `winter ws init ENV`:

1. **Safety check** — refuses on missing env path or dirty worktrees (override with `--force`).
2. **Hooks** — fires every extension's `on_env_destroy` hook (mirror of `on_env_init`). With `--strict`, a non-zero hook exit aborts the teardown; without it, hook failures are logged and teardown proceeds.
3. **Worktree removal** — `git worktree remove` for every per-repo worktree.
4. **Env cleanup** — removes the env directory and strips the matching `# >>> winter-dir/<env>` block from the workspace's `.git/info/exclude`.

Use `--dry-run` to preview the plan with no side effects.

**Prefer this over `rm -rf <env>/` + manual `git worktree remove`.** Manual removal bypasses `on_env_destroy` hooks the same way manual env creation bypasses `on_env_init` — extensions that need to clean up per-env state (tmux sessions, watchers, provisioned DBs) get skipped.

### `checkout` — adopt a remote feature branch into an env

`winter ws checkout ENV FEATURE_BRANCH` resets each non-pinned project worktree to the local `origin/FEATURE_BRANCH` ref and wires upstream tracking. **No network** — run `winter ws fetch` first if you need fresh remote-tracking refs.

Phase 1 checks each repo for dirty working tree, commits not present on `origin/FEATURE_BRANCH`, and whether the ref exists locally. **If any repo is dirty or divergent (and `--force` is not set), the whole command refuses with a per-repo report — no `git reset --hard` runs anywhere.** Repos missing the local remote-tracking ref are reported as skipped regardless of `--force`.

### `prune` — remove orphaned disk state

`winter ws prune` finds and removes state for repos no longer in the workspace config:

- Orphan project clones under `projects/`.
- Orphan standalone clones referenced by stale entries in `.git/info/exclude`.
- Broken symlinks under `.claude/skills/` and `.claude/agents/`.

Refuses to delete repos with uncommitted changes or attached worktrees. Use `--dry-run` to preview, `--force` to skip the interactive confirmation.

## Repository commands (`winter repo`)

| Command | Usage | Purpose |
|---------|-------|---------|
| `winter repo list` | `winter repo list [--json]` | List all project and standalone repositories and their types |
| `winter repo add` | `winter repo add URL [--standalone] [--name N] [--main-branch B] [--git-exclude E] [--cmd C] [--pinned] [--path P] [--prefix P] [--local] [--json]` | Add a repository to the workspace config (writes `.winter/config.toml` unless `--local` writes `.winter/config.local.toml`) |
| `winter repo remove` | `winter repo remove <project\|standalone>/NAME [--local] [--json]` | Remove a repository entry from the config |

## Dashboard

```bash
winter dashboard
```

Interactive TUI showing workspace status, feature environments, and repo details. Navigate with keyboard. Every key below is the **default** for a stable *action id* and can be remapped — see [Keybindings](#keybindings).

**Default keys:**

- `r` — refresh. `L` — open the **Log tab** (captured `RepoError` entries with subcommand, args, cwd, and stderr; inspect a failure without re-running the command). `q` — quit (workspace) / back (detail screens).
- `enter` — drill into the focused row's detail view. `h` / `j` / `k` / `l` — move the detail-screen cursor.
- `ctrl+k` / `ctrl+j` — jump table focus.
- `c` — clear the Log tab (Log screen only; not remappable).

**Tracking glyphs** in the repo rows: `[+N, -N]` shows commits ahead/behind upstream; `[+]` marks an unborn upstream ref (the local branch tracks a remote that doesn't exist yet); the pin glyph marks pinned repos.

### Keybindings

Every built-in action listed below has a stable **action id**. A `[keybindings]` table in `.winter/config.toml` (with the `.winter/config.local.toml` overlay applying per-machine) maps action ids to key specs; an id with no entry keeps its default. Invalid specs and unknown ids are reported as a dashboard toast and otherwise ignored — the rest of the bindings still load. See [setup.md](./setup.md#keybindings) for the config schema. (The Log tab is a separate screen whose `q`/`r`/`c` keys are fixed and not part of this table.)

| Action id | Default | Action |
|-----------|---------|--------|
| `app.quit` | `q` | Quit the dashboard (offered on the workspace screen) |
| `workspace.refresh` | `r` | Re-read all git status |
| `workspace.open_log` | `L` | Open the Log tab |
| `worktree.open_detail` | `<enter>` | Drill into the focused worktree / standalone row |
| `workspace.jump_prev` | `<C-k>` | Jump focus to the first table |
| `workspace.jump_next` | `<C-j>` | Jump focus to the last table |
| `worktree.refresh` | `r` | Re-read the env's git status |
| `worktree.open_log` | `L` | Open the Log tab |
| `worktree.back` | `q` | Back to the workspace screen |
| `worktree.cursor_left` / `worktree.cursor_down` / `worktree.cursor_up` / `worktree.cursor_right` | `h` / `j` / `k` / `l` | Move the repo cursor |
| `standalone.refresh` | `r` | Re-read the standalone repo's status |
| `standalone.open_log` | `L` | Open the Log tab |
| `standalone.back` | `q` | Back to the workspace screen |
| `plugin.<name>` | the plugin's `TuiAction.key` | Run a plugin-contributed action (see `winter-harness:/python/plugin-author.md`) |

**Key-spec grammar** (Neovim-inspired):

- **Single printable keys** are written bare: `s`, `D`, `,`. Uppercase means the shifted key.
- **Special keys** use angle brackets: `<enter>` (`<CR>`), `<tab>`, `<space>`, `<escape>`, `<backspace>`, `<up>`/`<down>`/`<left>`/`<right>`, `<f1>`…`<f12>`.
- **Modifier chords**: `<C-s>` (ctrl), `<A-s>` / `<M-s>` (alt / meta), `<S-s>` (shift), composed as `<C-A-s>`. These normalize to Textual tokens (`ctrl+s`, `alt+s`, `ctrl+alt+s`); `<S-` on a letter is just the uppercase letter.
- **`<leader>` prefix** expands to the configured `leader` key (default `\`), e.g. `<leader>S`.
- **Multi-key sequences** are an ordered run of the above: `<leader>S`, `gd`, `<C-x><C-s>`. The next key must arrive within `timeoutlen` milliseconds; if it doesn't and the keys so far are themselves a complete binding, that binding fires (Neovim's resolution). Avoid sequence keys the focused table already consumes (arrows, `enter`, `pageup`/`pagedown`) — the table intercepts them before the chord engine sees them. (This is an authoring caveat, not validated by the parser.)

## Doctor

```bash
winter doctor            # human-readable table
winter doctor --json     # NDJSON event stream
```

Runs preflight checks for the workspace and every installed extension. Each probe reports `pass`, `warn`, or `fail` with a one-line message and an optional remediation hint shown under failures. Exit code is `0` when nothing failed (warnings allowed), `1` if any probe failed.

**Built-in core probes** cover `git --version`, the running python version (>=3.11), `.winter/config.toml` parses, every declared project repo exists at `projects/<name>/`, every declared standalone repo exists at its configured path, every feature env's per-repo worktrees exist on the env-named branch, and the `.claude/` symlinks (agents and skills contributed by extensions) resolve to existing targets.

**Workspace probes** are contributed via a top-level `doctor = "path/to/probe-script"` field in `.winter/config.toml`. Use this to add project-specific checks ("postgres reachable", "node_modules installed", "secrets present"). See [setup.md](./setup.md#workspace-doctor-probe) for the script contract.

**Extension probes** are contributed via a `doctor = "path/to/probe-script"` field in the extension's `winter-ext.toml`. See [setup.md#extension-doctor-probes](./setup.md#extension-doctor-probes) for the script contract.

`--json` emits one NDJSON object per line: `{"type": "started"}` once, `{"type": "probe_result", "source": ..., "name": ..., "status": ..., "message": ..., "remediation": ...}` per probe, then `{"type": "finished", "total": N, "fails": N, "warns": N}`. The per-probe object's shape — `source`, `name`, `status`, `message`, `remediation` — is the same one each extension's probe script emits on its own stdout; see [setup.md#probe-output-contract](./setup.md#probe-output-contract) for the probe-side contract.

## Lint

```bash
winter lint                # the whole workspace (same as --all)
winter lint <repo>         # one repo by name
winter lint <env>          # every worktree in a feature env
winter lint --changed      # only the dirty / un-pushed files in the current repo
winter lint --all --json   # NDJSON event stream
```

Runs winter-ecosystem **convention** checks — path notation, agent frontmatter, module boundaries, and the like — as opposed to `winter doctor`, which checks workspace *materialization* (is this clone wired up correctly). The two are complementary: `doctor` answers "is the workspace healthy", `lint` answers "does the content follow winter's rules".

`winter lint` is a **dispatcher, not a checker** — it discovers the lint scripts contributed by installed extensions (and an optional workspace-level one), runs the applicable ones over the selected scope, and aggregates their findings. It contains no check logic itself: a workspace with no lint-contributing extension lints nothing and says so. Each finding reports `pass`, `warn`, or `fail` with an optional `file:line` location and a remediation hint shown under failures. Exit code is `0` when nothing failed (warnings allowed), `1` if any check failed — usable in CI and pre-push.

**Scope** selects which content the checks run over (the resolved paths are handed to each check; the check decides which it recognizes):

- a **repo name** — that project / standalone repo's directory.
- an **env name** — every project worktree directory inside the env.
- `--all` (the default) — the whole workspace tree, rooted at the workspace root.
- `--changed` — files that are dirty or in un-pushed commits in the git repository containing the current directory. Run it from the repo or worktree you're about to push.

A name that matches both a repo and an env is rejected as ambiguous; `--all` and `--changed` are mutually exclusive with each other and with a name.

**Workspace checks** are contributed via a top-level `lint = "path/to/lint-script"` field in `.winter/config.toml`; **extension checks** via the same field in an extension's `winter-ext.toml`. Both follow the same script contract as doctor probes, plus the scope env vars — see [setup.md#lint-checks](./setup.md#lint-checks).

`--json` emits one NDJSON object per line: `{"type": "started", "scope": ..., "label": ..., "paths": [...]}` once, `{"type": "finding", "source": ..., "check": ..., "status": ..., "message": ..., "file": ..., "line": ..., "remediation": ...}` per finding, then `{"type": "finished", "contributors": N, "total": N, "fails": N, "warns": N}`. `contributors` is the number of lint scripts that ran — `0` means nothing was contributed.

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

Drift detection currently covers project repos only. Missing or undeclared standalone repos are not warned about; if a `[[standalone_repository]]` entry's directory is missing, `winter ws init` clones it on the next run.

## Common workflows

### Bootstrap a new workspace
```bash
winter ws init              # clone every declared repo into projects/
winter ws init alpha        # create the alpha/ feature environment
```

### Check workspace state
```bash
winter ws status alpha
```

### Merge main before starting work
```bash
winter ws merge master alpha                # offline ff-only against local master — no network call
winter ws merge master alpha beta gamma     # fan one source ref across multiple envs in a single call
winter ws fetch alpha                       # add this first if you need a fresh origin/master
winter ws merge origin/master alpha         # then merge the freshly-fetched ref
winter ws merge master alpha --merge        # 3-way fallback when ff-only would refuse
```

Use the offline `winter ws merge master alpha` form when local `master` is already current — it doesn't hit the remote, so it's faster and won't stall on a hanging fetch. When you need a fresh `origin/master` first, run `winter ws fetch alpha` (which also fast-forwards the source checkout's local main), then `winter ws merge origin/master alpha`.

### Pull remote feature-branch commits into the local env
```bash
winter ws pull alpha               # ff-only against origin/<feature-branch>; diverged repos reported, not touched
winter ws pull alpha --rebase      # ff or replay local commits onto upstream
winter ws pull alpha --autostash   # stash dirty tree first, restore after
```

### Start a new feature
```bash
winter ws init alpha                       # ensures alpha/ exists
winter ws connect alpha feature/my-feature
```

### Fold one env into another
```bash
winter ws merge alpha gamma                # merge alpha into gamma's worktrees
```

### Push completed work
```bash
winter ws push alpha                       # alpha's non-pinned worktrees
winter ws push alpha/winter                # one specific worktree
winter ws push 'alpha/*' 'beta/*'          # alpha + beta non-pinned worktrees
winter ws push --include-pinned            # all envs, pinned and non-pinned
winter ws push --all                       # all envs' non-pinned worktrees + standalone
```

### Update everything from remotes (refs + source-checkout mains)
```bash
winter ws fetch --all                      # refresh refs for every env + standalone, ff each source checkout's local main
```
Feature worktrees are left untouched; only remote-tracking refs and the source checkouts' local main move.

### Review changes before committing
```bash
winter ws diff alpha --branch          # full branch diff vs main
winter ws diff alpha --staged          # staged changes only
winter ws diff alpha --repo my-app     # single repo
```

### Reuse a feature environment for a different feature
```bash
winter ws disconnect alpha
winter ws connect alpha feature/other-feature
```

### Adopt an existing remote feature branch
```bash
winter ws fetch alpha                              # refresh origin refs first
winter ws checkout alpha feature/existing-branch   # reset every repo's worktree to origin/feature/existing-branch
```

### Tear down a feature environment
```bash
winter ws destroy alpha --dry-run    # preview: hooks that will fire, worktrees that will be removed
winter ws destroy alpha              # standard teardown (fires on_env_destroy hooks, then removes)
winter ws destroy alpha --force      # bypass dirty-worktree check
winter ws destroy alpha --strict     # abort if any hook exits non-zero
```

### Clean up orphan disk state
```bash
winter ws prune --dry-run    # list orphan project clones, orphan standalone clones, broken .claude/ symlinks
winter ws prune              # interactive confirm + delete
```

### Propagate a config change
After adding a repo to the config or changing `cmd`/`git_excludes`, reconcile everything:
```bash
winter ws init --all
```
