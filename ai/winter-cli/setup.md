# Winter CLI — Setup

Installation and configuration reference. For day-to-day command usage, see [index.md](./index.md).

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
session_prefix = "my-project"                 # tmux session prefix
main_branch = "main"                          # workspace-default main branch (per-repo override below)
adopt_extensions = "winter"                   # how aggressively standalone repos contribute skills/agents
doctor = "ai/project/doctor.sh"               # optional; workspace-level `winter doctor` probe script (see Doctor below)

[capabilities]
service = "winter-service-tmux"               # bind the `service` slot to a single installed provider extension

# Multi-provider alternative: bind a list of providers to the slot.
# service = ["winter-service-tmux", "winter-service-docker"]
# All bound providers participate; order carries no execution semantics.

# Port allocation — all four keys are optional; shown here with their defaults.
base_port = 4000          # start of this workspace's port band; set a different value to separate co-located workspaces
ports_per_env = 20        # ports per feature env; per-env base = base_port + index * ports_per_env
env_aliases = [           # fixed-index env names (1..N); aliases get stable slots, all other names hash into the remainder
  "alpha", "beta", "gamma", "delta", "epsilon",
  "zeta", "eta", "theta", "iota", "kappa",
]
envs_per_workspace = 48   # max feature-env index (1..envs_per_workspace); must be >= len(env_aliases) + 2

# Entries appended to every repo's .git/info/exclude on `winter ws init`.
git_excludes = ["*.Local.csproj"]

# Project repositories — cloned to projects/ and worktreed into Greek-letter dirs.
# Entries appear in CLI and TUI output in the order they're listed here, so put
# high-priority repos first.
[[project_repository]]
name = "web"                               # directory name under projects/ — overrides what would be derived from `url`
url = "git@example.com:org/winter-app.git"
cmd = ["pnpm install"]                     # run after clone and in every worktree
git_excludes = [".env.development.local"]  # per-repo excludes, merged with workspace-wide

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
prefix = "wsb"                                # optional symlink-prefix override; see "Extensions" below
path = "extensions/winter-backlog"            # optional; relative to the workspace root, defaults to `name`
ref = "v1.4.2"                                # optional; pin this repo to a branch, tag, or commit SHA
config_dir = ".winter/config/winter-backlog"  # optional; override where WINTER_EXT_CONFIG_DIR points
```

#### `config_dir` — per-extension writable config/asset directory

The optional `config_dir` field overrides where winter stores and exports this extension's writable config/asset directory. When omitted, winter defaults to `.winter/config/<name>/` relative to the workspace root.

The resolved absolute path is exported as `WINTER_EXT_CONFIG_DIR` on every dispatch to this extension (service, doctor, lint, and hooks). Extensions use this directory to read their workspace-level config files (e.g. `config.toml`), write generated assets, or store per-workspace state that should be git-tracked alongside the workspace config.

`config_dir` must be a relative path under the workspace root with no `..` segments (the same guard applied to `path`). Absolute paths are rejected. An optional `config.local.toml` overlay convention within this directory is typically git-excluded by the workspace's blanket exclude for `*.local.*` files.

#### `ref` — standalone repo pins

The optional `ref` field pins a standalone repo to a branch, tag, or commit SHA. Winter resolves `ref` against the fetched remote refs in this order: `refs/remotes/origin/<ref>` (branch) → `refs/tags/<ref>` (tag) → `<ref>^{commit}` (raw SHA). First match wins; no match → unresolvable-ref error (run `winter ws fetch <name>` to refresh refs).

| `ref` value | Behavior | Lock behavior |
|-------------|----------|---------------|
| absent | Today's behavior: clone tracks the default branch; `pull` integrates the tracked upstream | No lock entry written |
| branch name | Checkout on that tracking branch (`main_branch` effectively set to `<ref>`); `pull` fast-forwards to `origin/<ref>` | Lock written; rewritten on each `pull` advance |
| tag or commit SHA | Detached checkout held exactly at the resolved commit; `pull` **never** advances it | Lock written; only updated by `winter ws update` |

**`ref` vs `pinned` vs `main_branch`** — three distinct concepts that are easy to conflate:

- **`pinned`** (`[[project_repository]]` only, UNRELATED) — means "exclude this *project* repo from feature branching entirely." The term is not reused on standalone repos; standalone repos have no `pinned` field.
- **`main_branch`** — the standalone repo's integration target / tracking branch when `ref` is absent or is a branch name.
- **`ref`** (new, `[[standalone_repository]]` only) — the pin intent: which branch, tag, or commit to lock the checkout to.

#### Lock file (`.winter/config.lock`)

When any standalone repo has a `ref`, winter maintains `.winter/config.lock` at the workspace root. This file records the resolved commit per pinned repo and is **intentionally committed** to the workspace repo — committing it makes the pin reproducible across machines and surfaces pin updates as reviewable `git diff`.

```toml
# .winter/config.lock — managed by winter; commit this file.
version = 1

[[standalone]]
name   = "winter-backlog"   # matches [[standalone_repository]].name
ref    = "v1.4.2"           # intent string copied from config (mismatch = stale lock)
kind   = "tag"              # "branch" | "tag" | "commit"
commit = "9f3c1ab2e4d5c6f7089a1b2c3d4e5f60718293a4"  # full 40-char SHA
```

- Repos without a `ref` get **no entry**. Entries are sorted by `name` for stable diffs.
- A mismatch between the lock's `ref` and the config's `ref` marks the lock as stale; `winter ws init` or `winter ws update` re-resolves and rewrites it.
- The lock is **not** added to `.gitignore` or `.git/info/exclude` by any winter command — it is committed alongside the config.

**What rewrites the lock:**

| Command | Condition | Action |
|---------|-----------|--------|
| `winter ws init` | Lock absent or stale | Resolves `ref`, checks out, writes lock |
| `winter ws init` | Lock present and fresh | Checks out locked commit; no rewrite |
| `winter ws pull` | Branch `ref` fast-forwards | Checks out new tip, rewrites lock |
| `winter ws pull` | Tag / commit `ref` | Held; lock unchanged |
| `winter ws update` | Always (explicit re-pin) | Fetches, re-resolves, checks out, rewrites |

### Local overlay (`.winter/config.local.toml`)

```toml
[git]
user.name = "John Doe"
user.email = "john.doe@example.com"
```

The overlay uses the same schema as the shared config. Keys in the overlay override the shared config key-by-key. The `[git]` identity is applied to every repo winter-cli manages during `winter ws init`.

### State registry (`.winter/state.toml`)

`.winter/state.toml` is a machine-local, gitignored file (not a config file) that winter manages automatically. It records the **env name → assigned index** mapping written by `winter ws init` and cleared by `winter ws destroy`. You never edit it by hand.

- `winter ws init <name>` allocates an index (alias → fixed slot; ad-hoc → hash then linear-probe upward within the hash band) and writes the assignment here.
- `winter ws destroy <name>` removes the entry.
- The read path loads the recorded index from this file; when no entry exists (pre-registry env), it falls back to recomputing from the name.
- `winter ws index <name>` returns the persisted index for an existing env, or the suggested (hash) slot for a hypothetical name — with a note that the suggestion may shift on create if another env already occupies that slot.
- `winter doctor` cross-checks this registry against on-disk env directories and warns on stale entries, unregistered env dirs, out-of-range indices, and duplicate assignments.

**Index reservation:** index 0 (`base_port`..`base_port+ports_per_env-1`) is reserved for a future single-slot "local" environment — a pre-seeded shared dataset/area distinct in purpose from the regular alias and hash-band slots. It is never assigned. The slot immediately after the aliases (`N+1`, default index 11 with the 10-alias default) is reserved as a buffer between the fixed alias band and the hash band; this is why the invariant requires `envs_per_workspace >= len(env_aliases) + 2` (not `+1`).

### Dashboard layout

The `winter dashboard` TUI can render the feature-worktrees grid in four orientations. Set the default in a `[tui.dashboard]` table; the `config.local.toml` overlay applies per-machine, merging key-by-key.

```toml
[tui.dashboard]
layout = "auto"   # auto | repos-as-columns | repos-as-rows | list
```

Accepted values: `auto` (default), `repos-as-columns`, `repos-as-rows`, `list`. See [the dashboard Layouts reference](./usage/dashboard.md#layouts) for what each layout does and how `auto` resolves.

An unknown `layout` value is a config error at startup. The `t` key cycles layouts live for the current session (overriding the configured default); see [usage/dashboard.md#layouts](./usage/dashboard.md#layouts).

### Keybindings

The `winter dashboard` TUI binds each action to a configurable key. Override the defaults in a `[keybindings]` table; the `config.local.toml` overlay applies per-machine, merging key-by-key.

```toml
[keybindings]
leader = "\\"          # what <leader> expands to (default backslash); single key spec
timeoutlen = 1000      # ms to wait for the next key of a chord sequence (Neovim's timeoutlen)

# Action id -> key spec. Quoted ids keep the dotted name flat (not nested tables).
# Absent ids keep their built-in default. Full id list + grammar: usage/dashboard.md#keybindings.
[keybindings.bindings]
"workspace.refresh" = "<C-r>"     # modifier chord
"worktree.open_detail" = "o"      # rebind Enter for opening a row's detail
"workspace.open_log" = "<leader>l" # leader chord sequence
"plugin.codediff" = "<leader>d"   # remap a plugin action by its plugin.<name> id
```

The `[keybindings.bindings]` keys are *quoted* action ids — the quotes keep a dotted id (`workspace.refresh`) a flat key instead of a nested TOML table. For the action-id reference, the full key-spec grammar, and the invalid-spec / unknown-id behavior, see [usage/dashboard.md#keybindings](./usage/dashboard.md#keybindings).

### Display names and ordering

`name` doubles as the directory under `projects/` and as the user-facing label everywhere a repo is shown (grid columns, status tables, sync/push/diff headers). When `name` is omitted, it's derived from the trailing path segment of `url` (with `.git` stripped). Set `name` explicitly when you want a friendlier label than the canonical repo name.

Repos appear in CLI tables and the TUI grid in the order they're declared in `.winter/config.toml`. Put the repos you work with most often at the top.

### Implicit Repositories

The `workspace` repo is discovered implicitly — it doesn't appear in `[[project_repository]]` or `[[standalone_repository]]`. Winter detects it from the filesystem: the workspace itself is the repo this CLI is invoked from.

## Provision handlers

`winter provision` reads `[[provision.*]]` tables from `.winter/config.toml` alongside the workspace's other config. Declare handlers per sub-target (`dependency`, `resource`, `data`):

```toml
[[provision.dependency]]
scope = "feature-worktree"
apply = "scripts/install-deps.sh"

[[provision.resource]]
scope             = "workspace"
apply             = "scripts/create-db.sh"
destroy           = "scripts/drop-db.sh"
required_services = ["workspace/postgres"]

[[provision.data]]
scope             = "feature-environment"
apply             = "scripts/seed.sh"
reset             = "scripts/reseed.sh"
required_services = ["workspace/postgres"]
```

For per-entry field semantics (`scope`, `apply`, `destroy`, `reset`, `required_services`, allowed sub-targets, and unknown-key rejection), see [usage/provision.md](./usage/provision.md). Extensions declare the same `[[provision.*]]` shape in their `winter-ext.toml` (paths relative to the extension directory). See [usage/provision.md](./usage/provision.md) for the full command reference, execution model, service-check behavior, and `--json` event contract.

## Extensions

Standalone repositories can opt into contributing skills and agents to the workspace's `.claude/` directory by shipping a `winter-ext.toml` file at the repo root.

### `winter-ext.toml` schema

```toml
name = "winter-backlog"        # default symlink prefix when no override is set
prefix = "wsb"                 # optional shorter prefix; takes precedence over `name`
skills_dir = "skills"          # optional; explicit path overrides default discovery
agents_dir = "agents"          # optional; explicit path overrides default discovery
doctor = "scripts/doctor.sh"   # optional; executable that emits NDJSON probe events for `winter doctor`
lint = "scripts/lint.sh"       # optional; executable(s) emitting NDJSON findings for `winter lint` (str or list)
requires = ["winter-product"]  # optional; other modules this one depends on (see `winter graph`)

[provides]
service = "workflow/service"   # this extension provides the `service` capability; entrypoint relative to repo root

# Provision handlers — same shape as [[provision.*]] in .winter/config.toml (paths relative to extension directory)
[[provision.dependency]]
scope = "feature-worktree"
apply = "scripts/install.sh"

[[provision.resource]]
scope             = "workspace"
apply             = "scripts/create-db.sh"
destroy           = "scripts/drop-db.sh"
required_services = ["workspace/postgres"]

[[provision.data]]
scope             = "feature-environment"
apply             = "scripts/seed.sh"
reset             = "scripts/reseed.sh"

# Extension-declared services — merged by winter into WINTER_SERVICE_MANIFEST on `service up`.
# The service orchestrator reads the manifest and adds these services to its live session.
[[service]]
name    = "worker"              # required; must be unique across all sources (workspace + all extensions)
scope   = "feature-environment" # "feature-environment" (default) or "workspace"
command = "python -m worker"    # required; the command to run
target  = "2.0"                 # provider-specific routing key (required by the tmux provider: window.pane)
# image = "myorg/worker:latest" # optional; for container-based providers
# ports = [8080]                # optional; port numbers declared by this service
```

**`[[service]]` declaration rules:**

- `name` is required and must be unique across all sources (the workspace config and every installed extension). A name collision across sources is a fatal error at `service up` time.
- `scope` defaults to `"feature-environment"` when omitted. Accepted values: `"feature-environment"` (one instance per active env), `"workspace"` (one shared instance). Unknown scope values are rejected.
- `command` is required.
- `target` is required by the tmux provider (window.pane address, e.g. `"2.0"`). Container-based providers use `image` instead. Unknown keys are rejected.
- `ports` is an optional list of integer port numbers the service listens on; used by status rendering.
- `image` is optional; used by container-based providers.

The extension's `[[service]]` entries are aggregated by `winter service up` alongside any `[[service]]` entries in the workspace's `.winter/config.toml`, deduplication is enforced by name, and the result is written to a temporary TOML file whose path is injected into each provider subprocess as `WINTER_SERVICE_MANIFEST`. See [usage/service.md](./usage/service.md#per-action-env-var-winter_service_manifest) for the full manifest format and consume-or-ignore rule.

`requires` declares the other winter modules this one references and therefore needs when installed on its own. Each entry is a module name — the `<context>` half of a `<context>:/path` reference. It is the data `winter graph` aggregates and the module-extractability lint check validates references against.

The final symlink prefix is resolved with this precedence: `prefix` on the workspace config entry > `prefix` in `winter-ext.toml` > `name` in `winter-ext.toml` > the standalone repo's directory name.

### What gets symlinked

When `skills_dir` and `agents_dir` aren't set explicitly, winter searches for them in this order and uses the first that exists:

- `skills/` then `.claude/skills/`
- `agents/` then `.claude/agents/`

That covers both the winter convention (top-level `skills/`/`agents/`) and the Claude Code convention (`.claude/skills/`/`.claude/agents/`), so a vanilla Claude Code repo can be adopted as an extension without modification. Setting `skills_dir`/`agents_dir` explicitly in `winter-ext.toml` skips the fallback and uses exactly the declared path.

For each subdirectory under the resolved skills root, winter installs the skill into every code-agent vendor's skills directory, choosing the install mechanism from the vendor's capability (the `CodeAgentVendor` enum):

- **Claude Code** (`.claude/skills/<prefix>-<dir>`) and **Codex** (`.codex/skills/<prefix>-<dir>`) get a **relative symlink** to the source.
- **OpenCode** (`.opencode/skill/<prefix>-<dir>`) gets a **real-directory copy**. OpenCode discovers skills by globbing `skill/**/SKILL.md` and its globber does **not** traverse symlinked directories — a symlink there would be invisible to it, so the skill must be a real directory. The copy is idempotent: on each `winter ws init`, winter content-hashes the source and destination and re-copies (delete-then-copy) only on a mismatch; nothing is persisted. Stale `<prefix>-*` copies with no live source are pruned, mirroring the symlink prune.

`.opencode/skill/` is read only by OpenCode, and OpenCode's read of `.claude/skills` only picks up real directories (not the symlinks there), so there is no double-loading across the symlink and copy sets. For each `.md` file or subdirectory under the resolved agents root, winter creates a symlink at `.claude/agents/<prefix>-<name>` (agents are Claude-only for now).

The workspace `.gitignore` is updated with a marker-bracketed block per extension:

```
# >>> winter-backlog (managed by winter)
/winter-backlog/
.claude/skills/wsb-*
.codex/skills/wsb-*
.opencode/skill/wsb-*
.claude/agents/wsb-*
# <<< winter-backlog
```

### Frontmatter convention

Claude Code lets a SKILL.md frontmatter `name` field override the directory name during skill discovery. That defeats the prefix-by-directory-name design, so winter requires extension SKILL.md files to **omit the `name` field** — letting the directory name (which winter controls via the symlink or copy) be authoritative. Winter validates this on install and refuses if any SKILL.md sets `name`.

### Extension hooks

Extensions can also declare lifecycle hooks in `winter-ext.toml`:

```toml
[hooks]
on_env_init            = "./hooks/init-worktree.sh"
on_env_destroy         = "./hooks/destroy-worktree.sh"
on_workspace_reconcile = "./hooks/reconcile-workspace.sh"
```

- `on_env_init` fires after `winter ws init <env>` creates every per-repo worktree and seeds `.winter.env`. Use it to provision per-env state (tmux sessions, databases, watchers).
- `on_env_destroy` fires *before* `winter ws destroy <env>` removes any per-repo worktree or the env directory. Use it to release whatever `on_env_init` provisioned.
- `on_workspace_reconcile` fires **once per workspace-level reconcile** — specifically `winter ws init` (no target) and `winter ws init --all`. Fires after standalone/extension repos are reconciled so the extension exists on disk, and for the `--all` path, before the per-env loop. Use it for one-time workspace setup that should re-run when the workspace is re-reconciled (e.g. writing workspace-level config files, registering extensions with external tools).

Hook scripts must be **relative paths inside the extension directory** (so the extension owns its scripts; winter resolves them against the extension root).

#### Hook env-var contract

**Env hooks** (`on_env_init`, `on_env_destroy`) are invoked with:

| Var | Meaning |
|-----|---------|
| `WINTER_WORKSPACE_DIR` | Absolute path to the workspace root. |
| `WINTER_EXT_DIR` | Absolute path to this extension's clone (the dir containing `winter-ext.toml`). |
| `WINTER_EXT_PREFIX` | The resolved symlink prefix for this extension (`wf`, `wst`, …). |
| `WINTER_EXT_CONFIG_DIR` | Absolute path to this extension's writable config/asset directory (default `.winter/config/<name>/`). |
| `WINTER_WORKSPACE_PORT_BASE` | `base_port + ports_per_env * 0` — the index-0 base, i.e. the port band reserved for workspace-scope services. Present for **every** hook, including `on_workspace_reconcile` (which otherwise gets no `WINTER_ENV*`/`WINTER_PORT_BASE`). |
| `WINTER_ENV` | The env name (`alpha`, `beta`, …). Env-scoped hooks only. |
| `WINTER_ENV_INDEX` | The persisted port-offset index for this env (alias envs get fixed slots `1..N`; ad-hoc names hash into the remainder band). Env-scoped hooks only. |
| `WINTER_PORT_BASE` | `base_port + ports_per_env * WINTER_ENV_INDEX` (defaults: `4000 + 20 * index`). Env-scoped hooks only. |

The hook's **cwd is the env root** (`<workspace>/<env>/`). Hooks should read these env vars rather than parse `argv`.

**Workspace hook** (`on_workspace_reconcile`) is invoked with only:

| Var | Meaning |
|-----|---------|
| `WINTER_WORKSPACE_DIR` | Absolute path to the workspace root. |
| `WINTER_EXT_DIR` | Absolute path to this extension's clone. |
| `WINTER_EXT_PREFIX` | The resolved symlink prefix for this extension. |
| `WINTER_EXT_CONFIG_DIR` | Absolute path to this extension's writable config/asset directory. |

The hook's **cwd is the workspace root**.

**Strict vs non-strict on destroy.** By default, a non-zero exit from a destroy hook is logged and the teardown continues so a broken hook doesn't trap an env on disk. Pass `--strict` to `winter ws destroy` (or set it in CI/scripted use) when a hook failure must surface as a user-actionable error before any worktree is removed.

### `adopt_extensions` modes

The top-level `adopt_extensions` field controls when winter processes a standalone repo's skills and agents:

| Value | Behavior |
|-------|----------|
| `winter` (default) | Process only standalone repos that have a `winter-ext.toml` at the repo root. SKILL.md frontmatter is strictly validated. |
| `all` | Process any standalone repo with `skills/`, `agents/`, `.claude/skills/`, or `.claude/agents/` directories, with or without a manifest. Frontmatter validation downgrades from refuse-to-warn — collisions become the user's problem. |
| `none` | Skip all extension processing. Standalone repos are still cloned, but no symlinks are created. |

## Capability registry

Winter routes capabilities (service orchestration and future slots) through a uniform registry. Three inputs combine to determine the provider for each slot:

1. **Extension manifest** — a `[provides]` table in `winter-ext.toml`, where each key is a slot name and the value is the entrypoint path relative to the extension repo root.
2. **Workspace config** — a `[capabilities]` table in `.winter/config.toml` (or the `config.local.toml` overlay), where each key is a slot name and the value is the name of an installed extension. The table merges through the overlay key-by-key like every other table.
3. **Installed-extension set** — the standalone repos on disk that the registry walks at resolve time.

### Resolution rules

| State | Result |
|-------|--------|
| Explicit `capabilities.<slot>` binding → valid provider | **explicit** — dispatches to that extension |
| No binding, exactly one extension provides the slot | **implicit** — dispatches to the sole provider |
| No binding, exactly one provider but entrypoint file missing | **implicit** (describe) / dispatch error (resolve) — entrypoint validity re-checked at dispatch time |
| No binding, two or more providers | **implicit (all bound)** — every candidate is bound, in deterministic name order; all are dispatched |
| Binding to an extension that is not installed, or installed but not declaring `provides.<slot>`, or entrypoint file missing | **invalid** — any dispatch errors with a specific message |
| No provider installed | no dispatch possible |

`winter capabilities` introspects the registry (read-only, always exits 0 — see [usage/capabilities.md](./usage/capabilities.md)). `winter doctor`'s `[capabilities]` probe group evaluates each slot: invalid → `fail`, implicit provider(s) → `pass` (with a note), explicit valid binding → `pass`, no provider → `warn`.

After changing the service contract (adding, removing, or updating a provider), run `winter ext verify <path-to-extension-dir>` against each installed provider to confirm it conforms to the bundled spec (see [usage/ext.md](./usage/ext.md)).

The only in-scope slot today is `service`. Future slots are added to `CapabilitySlot` in the code and the registry handles them uniformly.

### Deprecated keys

- **`service_orchestrator`** in config — single-string legacy key; normalised at config load into a one-element `capabilities.service` binding. Ignored when `capabilities.service` is already set explicitly. Use `[capabilities].service` for new workspaces.
- **`orchestrate_services`** in manifest — the service-slot-only predecessor of `provides.service`; still read as a fallback via `capability_entrypoint()`. Use `[provides].service` for new extensions.

## Service orchestration

`winter service` (see [usage/service.md](./usage/service.md)) owns a stable `up`/`down`/`status`/`restart`/`logs` interface and dispatches each invocation to the extension(s) bound to the `service` capability slot. The interface lives in core winter; the implementation lives in whichever extension(s) the workspace points at (tmux, containers, a daemon), so consumers never depend on the implementation.

### Registering orchestrator(s)

Three config paths connect the interface to an implementation:

- **Single provider** — `capabilities.service = "<extension-name>"` in the `[capabilities]` table in `.winter/config.toml` (or the `config.local.toml` overlay). The name must match a `[[standalone_repository]]` that ships a `winter-ext.toml`. If only one installed extension declares `provides.service`, the binding is implicit and the explicit config entry is optional.
- **Multiple providers** — `capabilities.service = ["<name-1>", "<name-2>"]` (a list value in the same `[capabilities]` table). Every named provider is bound; list order carries no execution semantics. Each provider must declare `provides.service` in its `winter-ext.toml`. Repeated names are de-duplicated (preserving order) at config load.
- **Extension manifest** — `provides.service = "<path>"` in the `[provides]` table in each extension's `winter-ext.toml`, an executable entrypoint relative to the extension's repo root.

With binding and manifest in place, `winter service <action> …` resolves through the capability registry. Self-registration and explicit binding compose: an explicit `capabilities.service` (string or list) selects exactly those providers; with no explicit binding, **all** installed extensions that declare `provides.service` are bound (one → implicit; two or more → all bound, implicitly). For the full resolution model and deprecated key handling, see [## Capability registry](#capability-registry) above.

For multi-provider fan-out behavior (`up` aborts on first failure, `down` is best-effort, the ownership index for targeted `logs`/`restart`, the `logs -f` single-owner restriction, and merged `status` — all with no readiness gate or ordering semantics), see [usage/service.md](./usage/service.md).

The legacy keys `service_orchestrator` (config) and `orchestrate_services` (manifest) are still accepted as deprecated aliases — see the Capability registry section for the fallback semantics.

### Entrypoint contract

The full implementer-facing contract — uniform argv rule, per-action env vars, NDJSON wire format for `logs`, structured JSON status document (schema, shape-stability rule, and graceful-degradation behavior) for `status`, `describe` action for multi-provider ownership, plain-line and table render formats, idempotent backstop filters, tail-with-follow limitation, and exit codes — lives in [usage/service.md#orchestrator-contract](./usage/service.md#orchestrator-contract). A third-party orchestrator can conform without reading winter's source.

## Doctor probes

`winter doctor` (see [usage/doctor.md](./usage/doctor.md)) aggregates probe results from three sources: built-in core checks in winter-cli, an optional workspace-level probe, and one probe per installed extension. The workspace and extension probes are opt-in shell scripts that follow the same output contract.

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

`winter lint` (see [usage/lint.md](./usage/lint.md)) is the convention-checking counterpart of `winter doctor`. It aggregates findings from three sources — symmetric with doctor's probe sources: built-in core checks bundled with winter-cli, an optional workspace-level script, and one script per installed extension. The workspace and extension checks are opt-in scripts that follow the same output contract; the core checks always run, with no per-workspace registration. It owns scope selection, aggregation, and reporting — the check logic lives entirely in the checks it dispatches.

### Built-in core checks

These ship with winter-cli and run on every `winter lint`, the same way `winter doctor` runs its built-in core probes — no `.winter/config.toml` or `winter-ext.toml` registration needed. They run first, before the workspace and extension checks, and their findings appear under a `[core]` source group.

**Unlike contributed lint scripts, core checks are not confined to `WINTER_LINT_PATHS`.** Core checks may read workspace-root injection roots (`CLAUDE.md`, `CLAUDE.winter.md`) and `.winter/` manifests directly to do their work. This is intentional — the file-size check walks the `@import` graph rooted at those files regardless of scope, and the required-services check reads every manifest to find `required_services` declarations. Contributed scripts MUST confine themselves to `WINTER_LINT_PATHS`; core checks operate at a higher privilege tier because they ship with winter-cli and their scope is the whole workspace by design.

The three built-in core checks are:

- **module extractability** (`tools/winter-lint/extractability.py`): validates dependency direction across the ecosystem graph, flagging a `<context>:/path` reference whose target a module isn't guaranteed to have when shipped standalone — a core module pointing at an extension (a layering inversion) or an undeclared sibling (a dead pointer at the consumption edge). It is graph-driven (it calls back into `$WINTER_CLI graph --json` rather than rebuilding the graph) and honors the `<!-- winter-lint:example -->` line exemption and fenced-code-block skip. Full rules in [tools/winter-lint/README.md](../../tools/winter-lint/README.md).

- **file-size**: checks agent-facing markdown files against configurable byte-size thresholds. Files in the auto-injected `@import` graph (rooted at `CLAUDE.md` and `CLAUDE.winter.md` in the workspace root) are held to the tighter `injected_bytes` threshold; all other `.md` files in scope are checked against the looser `reference_bytes` threshold. Override thresholds in `.winter/config.toml` under `[core_checks.file_size]`.

- **required-services**: validates `required_services` entries in `[[provision.resource]]` and `[[provision.data]]` handlers (in `.winter/config.toml` and each installed extension's `winter-ext.toml`) against the merged service catalog from all bound service-orchestrator providers. Each entry must be scope-qualified (`workspace/<name>` or `<env>/<name>`), and the named service must appear in at least one provider's `catalog` output. See [usage/provision.md](./usage/provision.md#service-check-required_services) for the runtime `required_services` enforcement; this static lint check is a separate, complementary surface — it validates catalog membership ahead of time and runs even when no env is provisioned.

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
| `WINTER_CLI` | Absolute path to the winter CLI that launched the run. A check may call back into it for workspace-wide data it can't derive from its own scope — e.g. `$WINTER_CLI graph --json` for the dependency graph — instead of rebuilding it. A check must **never** call `winter lint` (that recurses). |

**A check MUST confine itself to `WINTER_LINT_PATHS`.** `winter lint` runs every contributed script for every scope and never filters by content — keeping a run "applicable to that scope" is the script's job. A check walks the given paths, applies its rules only to files under them, and emits nothing for a scope whose content it doesn't recognize.

- **Do**: iterate `WINTER_LINT_PATHS`, walk each (a file is itself; a directory is recursed), match the files you own, stay silent otherwise.
- **Don't**: glob the whole workspace, read `$WINTER_WORKSPACE_DIR` wholesale, or use the current directory — that leaks findings outside the scope and silently breaks `--changed` and per-repo runs.

### Workspace lint check

The workspace contributes a lint script via a top-level field in `.winter/config.toml`, symmetric with the workspace doctor probe:

```toml
lint = "ai/project/lint.sh"             # single script
lint = ["ai/project/lint.sh", "ai/project/lint_docs.sh"]   # or a list
```

`lint` accepts a single path or a list; a bare string is coerced to a one-element list. Paths are **relative to the workspace root** and must point to executable files. They run first, before extension checks, with cwd at the workspace root, and their findings appear under a `[project]` source group. Use them for checks this specific workspace owns. Ecosystem-general checks meant to travel between workspaces belong in an installed extension instead (the `lint` field below) — e.g. a dedicated `winter-lint` extension hosting the cross-cutting checks no single domain extension owns.

### Extension lint checks

Extensions opt in via the top-level `lint` field in `winter-ext.toml` (paths **relative to the extension directory**, executable). Like the workspace field, it accepts a single path or a list — an extension that contributes several distinct checks (say, one per convention) lists them all, and each runs as its own script. Each runs with cwd at the workspace root and the scope env vars above; findings appear under the extension's `[<ext-prefix>]` source group.

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
