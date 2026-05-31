# Winter CLI — Setup

Installation and configuration reference. For day-to-day command usage, see [usage.md](./usage.md).

## Installation

```bash
./tools/winter-cli/install.sh
```

This copies the `winter` wrapper to `~/.local/bin/`. The wrapper auto-discovers the workspace root by searching upward for `.winter/config.toml` + `tools/winter-cli/`, then runs via `mise` and `uv` — no manual virtualenv setup needed.

## Configuration

Winter loads two files and merges them:

- `.winter/config.toml` — committed workspace config (repo list, excludes, defaults)
- `.winter/config.local.toml` — gitignored overlay for per-user settings (git identity)

### Shared config (`.winter/config.toml`)

```toml
session_prefix = "my-project"       # tmux session prefix
main_branch = "main"                # workspace-default main branch (per-repo override below)
adopt_extensions = "winter"         # how aggressively standalone repos contribute skills/agents
doctor = "ai/project/doctor.sh"     # optional; workspace-level `winter doctor` probe script (see Doctor below)

# Entries appended to every repo's .git/info/exclude on `winter ws init`.
git_excludes = ["*.Local.csproj"]

# Project repositories — cloned to projects/ and worktreed into Greek-letter dirs.
# Entries appear in CLI and TUI output in the order they're listed here, so put
# high-priority repos first.
[[project_repository]]
name = "web"                       # directory name under projects/ — overrides what would be derived from `url`
url = "git@example.com:org/winter-app.git"
cmd = ["pnpm install"]             # run after clone and in every worktree
git_excludes = [".env.development.local"]   # per-repo excludes, merged with workspace-wide

[[project_repository]]
name = "api"
url = "git@example.com:org/winter-api.git"
main_branch = "development"        # per-repo override of the top-level main_branch
cmd = ["dotnet restore"]

# Pinned repos always track origin/main and are skipped during feature branching.
[[project_repository]]
name = "shared-tools"
url = "git@example.com:org/shared-tools.git"
pinned = true

# Standalone repositories — cloned to the workspace root (or `path`), no worktree,
# no feature branching. Useful for winter extensions (skills/agents) and any repo
# you want available alongside project repos but not multiplied per-feature.
[[standalone_repository]]
name = "winter-backlog"
url = "git@github.com:user/winter-backlog.git"
prefix = "wsb"                     # optional symlink-prefix override; see "Extensions" below
path = "extensions/winter-backlog" # optional; relative to the workspace root, defaults to `name`
```

### Local overlay (`.winter/config.local.toml`)

```toml
[git]
user.name = "John Doe"
user.email = "john.doe@example.com"
```

The overlay uses the same schema as the shared config. Keys in the overlay override the shared config key-by-key. The `[git]` identity is applied to every repo winter-cli manages during `winter ws init`.

### Keybindings

The `winter dashboard` TUI binds each action to a configurable key. Override the defaults in a `[keybindings]` table; the `config.local.toml` overlay applies per-machine, merging key-by-key.

```toml
[keybindings]
leader = "\\"          # what <leader> expands to (default backslash); single key spec
timeoutlen = 1000      # ms to wait for the next key of a chord sequence (Neovim's timeoutlen)

# Action id -> key spec. Quoted ids keep the dotted name flat (not nested tables).
# Absent ids keep their built-in default. Full id list + grammar: usage.md#keybindings.
[keybindings.bindings]
"workspace.refresh" = "<C-r>"     # modifier chord
"worktree.open_detail" = "o"      # rebind Enter for opening a row's detail
"workspace.open_log" = "<leader>l" # leader chord sequence
"plugin.codediff" = "<leader>d"   # remap a plugin action by its plugin.<name> id
```

The `[keybindings.bindings]` keys are *quoted* action ids — the quotes keep a dotted id (`workspace.refresh`) a flat key instead of a nested TOML table. For the action-id reference, the full key-spec grammar, and the invalid-spec / unknown-id behavior, see [usage.md#keybindings](./usage.md#keybindings).

### Display names and ordering

`name` doubles as the directory under `projects/` and as the user-facing label everywhere a repo is shown (grid columns, status tables, sync/push/diff headers). When `name` is omitted, it's derived from the trailing path segment of `url` (with `.git` stripped). Set `name` explicitly when you want a friendlier label than the canonical repo name.

Repos appear in CLI tables and the TUI grid in the order they're declared in `.winter/config.toml`. Put the repos you work with most often at the top.

### Implicit Repositories

The `workspace` repo is discovered implicitly — it doesn't appear in `[[project_repository]]` or `[[standalone_repository]]`. Winter detects it from the filesystem: the workspace itself is the repo this CLI is invoked from.

## Extensions

Standalone repositories can opt into contributing skills and agents to the workspace's `.claude/` directory by shipping a `winter-ext.toml` file at the repo root.

### `winter-ext.toml` schema

```toml
name = "winter-backlog"        # default symlink prefix when no override is set
prefix = "wsb"                 # optional shorter prefix; takes precedence over `name`
skills_dir = "skills"          # optional; explicit path overrides default discovery
agents_dir = "agents"          # optional; explicit path overrides default discovery
doctor = "scripts/doctor.sh"   # optional; executable that emits NDJSON probe events for `winter doctor`
lint = "scripts/lint.sh"       # optional; executable that emits NDJSON findings for `winter lint`
```

The final symlink prefix is resolved with this precedence: `prefix` on the workspace config entry > `prefix` in `winter-ext.toml` > `name` in `winter-ext.toml` > the standalone repo's directory name.

### What gets symlinked

When `skills_dir` and `agents_dir` aren't set explicitly, winter searches for them in this order and uses the first that exists:

- `skills/` then `.claude/skills/`
- `agents/` then `.claude/agents/`

That covers both the winter convention (top-level `skills/`/`agents/`) and the Claude Code convention (`.claude/skills/`/`.claude/agents/`), so a vanilla Claude Code repo can be adopted as an extension without modification. Setting `skills_dir`/`agents_dir` explicitly in `winter-ext.toml` skips the fallback and uses exactly the declared path.

For each subdirectory under the resolved skills root, winter creates a symlink at `.claude/skills/<prefix>-<dir>` pointing to it. For each `.md` file or subdirectory under the resolved agents root, winter creates a symlink at `.claude/agents/<prefix>-<name>`.

The workspace `.gitignore` is updated with a marker-bracketed block per extension:

```
# >>> winter-backlog (managed by winter)
/winter-backlog/
.claude/skills/wsb-*
.claude/agents/wsb-*
# <<< winter-backlog
```

### Frontmatter convention

Claude Code lets a SKILL.md frontmatter `name` field override the directory name during skill discovery. That defeats the prefix-by-symlink design, so winter requires extension SKILL.md files to **omit the `name` field** — letting the directory name (which winter controls via the symlink) be authoritative. Winter validates this on install and refuses if any SKILL.md sets `name`.

### Extension hooks

Extensions can also declare lifecycle hooks in `winter-ext.toml`:

```toml
[hooks]
on_env_init    = "./hooks/init-worktree.sh"
on_env_destroy = "./hooks/destroy-worktree.sh"
```

- `on_env_init` fires after `winter ws init <env>` creates every per-repo worktree and seeds `.winter.env`. Use it to provision per-env state (tmux sessions, databases, watchers).
- `on_env_destroy` fires *before* `winter ws destroy <env>` removes any per-repo worktree or the env directory. Use it to release whatever `on_env_init` provisioned.

Hook scripts must be **relative paths inside the extension directory** (so the extension owns its scripts; winter resolves them against the extension root).

#### Hook env-var contract

Every hook is invoked with:

| Var | Meaning |
|-----|---------|
| `WINTER_WORKSPACE_DIR` | Absolute path to the workspace root. |
| `WINTER_EXT_DIR` | Absolute path to this extension's clone (the dir containing `winter-ext.toml`). |
| `WINTER_EXT_PREFIX` | The resolved symlink prefix for this extension (`wf`, `wst`, …). |
| `WINTER_ENV` | The env name (`alpha`, `beta`, …). |
| `WINTER_ENV_INDEX` | The port-offset index (1..24 for Greek letters, hashed 26..281 otherwise). |
| `WINTER_PORT_BASE` | `4000 + 100 * WINTER_ENV_INDEX`. |

The hook's **cwd is the env root** (`<workspace>/<env>/`). Hooks should read these env vars rather than parse `argv`.

**Strict vs non-strict on destroy.** By default, a non-zero exit from a destroy hook is logged and the teardown continues so a broken hook doesn't trap an env on disk. Pass `--strict` to `winter ws destroy` (or set it in CI/scripted use) when a hook failure must surface as a user-actionable error before any worktree is removed.

### `adopt_extensions` modes

The top-level `adopt_extensions` field controls when winter processes a standalone repo's skills and agents:

| Value | Behavior |
|-------|----------|
| `winter` (default) | Process only standalone repos that have a `winter-ext.toml` at the repo root. SKILL.md frontmatter is strictly validated. |
| `all` | Process any standalone repo with `skills/`, `agents/`, `.claude/skills/`, or `.claude/agents/` directories, with or without a manifest. Frontmatter validation downgrades from refuse-to-warn — collisions become the user's problem. |
| `none` | Skip all extension processing. Standalone repos are still cloned, but no symlinks are created. |

## Doctor probes

`winter doctor` (see [usage.md#doctor](./usage.md#doctor)) aggregates probe results from three sources: built-in core checks in winter-cli, an optional workspace-level probe, and one probe per installed extension. The workspace and extension probes are opt-in shell scripts that follow the same output contract.

### Probe output contract

Every probe script emits **NDJSON to stdout**, one object per line:

```json
{"name": "tea auth", "status": "pass", "message": "logged in as pgross"}
{"name": "tmux version", "status": "warn", "message": "v2.8 (recommend >= 3.0)", "remediation": "Upgrade tmux: `dnf install tmux`."}
```

Required fields: `name` (string) and `status` (one of `pass` / `warn` / `fail`). Optional: `message` (one-line summary) and `remediation` (one-line fix hint, shown under failures in the table view).

**Exit handling.** A non-zero exit becomes a single synthetic `fail` result with the probe's stderr as the message — surfaced even if no NDJSON was emitted. Lines that don't parse as JSON, or that are missing required fields, become `warn` results so the contract violation is visible without aborting the run.

**Common misconfigurations** (workspace and extension probes alike): a missing `doctor` field is silently skipped; a `doctor` value pointing at a missing script surfaces as a `fail`; a script that exists but isn't executable surfaces as a `fail` so the misconfiguration is actionable; a path that escapes its declaring directory (workspace root for workspace probes, extension directory for extension probes) is refused.

### Workspace doctor probe

The workspace itself can contribute a probe script that runs between the core probes and each extension's probes. Declare it as a top-level field in `.winter/config.toml`:

```toml
doctor = "ai/project/doctor.sh"
```

The path is **relative to the workspace root** and must point to an executable file. The probe runs with cwd at the workspace root and `WINTER_WORKSPACE_DIR` set. Use it for project-specific checks that don't belong in any extension — database reachable, `.env` populated, secrets present, build toolchain installed.

Results appear under a `[project]` source group in the table view, between `[core]` and each `[<ext-prefix>]` block.

### Extension doctor probes

Extensions opt in via a top-level field in `winter-ext.toml`:

```toml
doctor = "scripts/doctor.sh"
```

`doctor` is a **top-level scalar** in `winter-ext.toml`, not part of `[hooks]` — there's at most one probe script per extension. The path is **relative to the extension directory** (same rule as hook scripts) and must point to an executable file.

The probe's **cwd is the workspace root**. Probes are workspace-scoped, not per-env, so the env vars are a subset of the hook contract:

| Var | Meaning |
|-----|---------|
| `WINTER_WORKSPACE_DIR` | Absolute path to the workspace root. |
| `WINTER_EXT_DIR` | Absolute path to this extension's clone. |
| `WINTER_EXT_PREFIX` | The resolved symlink prefix for this extension. |

Results appear under a `[<ext-prefix>]` source group, one block per installed extension that contributes probes.

## Lint checks

`winter lint` (see [usage.md#lint](./usage.md#lint)) is the convention-checking counterpart of `winter doctor`. It's a dispatcher: it discovers lint scripts contributed by the workspace and by installed extensions, runs the applicable ones over the selected scope, and aggregates their findings. It owns scope selection, aggregation, and reporting — the check logic lives entirely in the contributed scripts.

### Finding output contract

A lint script follows the **same NDJSON contract as a doctor probe** ([above](#probe-output-contract)) with two additions per object — `check` (the field name; `name` is also accepted as an alias, so an existing doctor probe can be repointed at lint with minimal change) and optional `file` / `line` location fields:

```json
{"check": "path-notation", "status": "fail", "message": "non-canonical ref `../harness`", "file": "ai/index.md", "line": 12, "remediation": "Use the `winter-harness:` prefix."}
{"check": "agent-frontmatter", "status": "warn", "message": "missing `model`", "file": ".claude/agents/wf-developer.md"}
```

Required fields: `check` (string) and `status` (`pass` / `warn` / `fail`). Optional: `message`, `file`, `line`, `remediation`. Exit handling and misconfiguration behavior (missing field silently skipped; missing / non-executable / directory-escaping script surfaces as a `fail`; unparseable lines become `warn`) match the doctor probe contract exactly.

### Scope environment variables

On top of the doctor probe's env (`WINTER_WORKSPACE_DIR`, and for extension scripts also `WINTER_EXT_DIR` / `WINTER_EXT_PREFIX`), every lint script receives the resolved scope:

| Var | Meaning |
|-----|---------|
| `WINTER_LINT_SCOPE` | The scope kind: `all`, `repo`, `env`, or `changed`. |
| `WINTER_LINT_PATHS` | Newline-delimited absolute paths in scope. Under `changed` these are individual **files**; under `all` / `repo` / `env` they are **directory** roots. A check must `stat` each path and handle both. |

**A check MUST confine itself to `WINTER_LINT_PATHS`.** `winter lint` runs every contributed script for every scope and never filters by content — keeping a run "applicable to that scope" is the script's job. A check walks the given paths, applies its rules only to files under them, and emits nothing for a scope whose content it doesn't recognize.

- **Do**: iterate `WINTER_LINT_PATHS`, walk each (a file is itself; a directory is recursed), match the files you own, stay silent otherwise.
- **Don't**: glob the whole workspace, read `$WINTER_WORKSPACE_DIR` wholesale, or use the current directory — that leaks findings outside the scope and silently breaks `--changed` and per-repo runs.

### Workspace lint check

The workspace contributes a lint script via a top-level field in `.winter/config.toml`, symmetric with the workspace doctor probe:

```toml
lint = "ai/project/lint.sh"
```

The path is **relative to the workspace root** and must point to an executable file. It runs first, before extension checks, with cwd at the workspace root. Findings appear under a `[project]` source group. Use it for checks this specific workspace owns. Ecosystem-general checks meant to travel between workspaces belong in an installed extension instead (the `lint` field below) — e.g. a dedicated `winter-lint` extension hosting the cross-cutting checks no single domain extension owns.

### Extension lint checks

Extensions opt in via the top-level `lint` field in `winter-ext.toml` (path **relative to the extension directory**, executable, at most one per extension — same rules as `doctor`). Each runs with cwd at the workspace root and the scope env vars above; findings appear under the extension's `[<ext-prefix>]` source group.

A minimal check skeleton — walk the scope, match the files you own, emit one finding per violation, stay silent on the rest:

```bash
#!/usr/bin/env bash
# Flag Markdown files that reference the harness with a bare relative path
# instead of the canonical `winter-harness:` notation.
set -euo pipefail

emit() { printf '{"check":"path-notation","status":"%s","message":"%s","file":"%s","line":%s}\n' "$1" "$2" "$3" "$4"; }

while IFS= read -r path; do
  [ -z "$path" ] && continue
  # A directory root is recursed; a single changed file is checked directly.
  while IFS= read -r md; do
    while IFS=: read -r line _; do
      emit fail "use the \`winter-harness:\` prefix" "$md" "$line"
    done < <(grep -nE '\.\./harness' "$md" || true)
  done < <(find "$path" -type f -name '*.md' 2>/dev/null)
done <<< "$WINTER_LINT_PATHS"
```

Exit non-zero only for the script's own failures — winter turns that into a synthetic `fail`. A clean run that found nothing exits `0` and emits nothing.
