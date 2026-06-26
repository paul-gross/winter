# Extensions

Standalone repositories can opt into contributing skills and agents to the workspace's `.claude/` directory by shipping a `winter-ext.toml` file at the repo root.

## `winter-ext.toml` schema

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

The extension's `[[service]]` entries are aggregated by `winter service up` alongside any `[[service]]` entries in the workspace's `.winter/config.toml`, deduplication is enforced by name, and the result is written to a temporary TOML file whose path is injected into each provider subprocess as `WINTER_SERVICE_MANIFEST`. See [../usage/service.md](../usage/service.md#per-action-env-var-winter_service_manifest) for the full manifest format and consume-or-ignore rule.

`requires` declares the other winter modules this one references and therefore needs when installed on its own. Each entry is a module name — the `<context>` half of a `<context>:/path` reference. It is the data `winter graph` aggregates and the module-extractability lint check validates references against.

The final symlink prefix is resolved with this precedence: `prefix` on the workspace config entry > `prefix` in `winter-ext.toml` > `name` in `winter-ext.toml` > the standalone repo's directory name.

## What gets symlinked

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

## Frontmatter convention

Claude Code lets a SKILL.md frontmatter `name` field override the directory name during skill discovery. That defeats the prefix-by-directory-name design, so winter requires extension SKILL.md files to **omit the `name` field** — letting the directory name (which winter controls via the symlink or copy) be authoritative. Winter validates this on install and refuses if any SKILL.md sets `name`.

## Extension hooks

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

### Hook env-var contract

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

## `adopt_extensions` modes

The top-level `adopt_extensions` field controls when winter processes a standalone repo's skills and agents:

| Value | Behavior |
|-------|----------|
| `winter` (default) | Process only standalone repos that have a `winter-ext.toml` at the repo root. SKILL.md frontmatter is strictly validated. |
| `all` | Process any standalone repo with `skills/`, `agents/`, `.claude/skills/`, or `.claude/agents/` directories, with or without a manifest. Frontmatter validation downgrades from refuse-to-warn — collisions become the user's problem. |
| `none` | Skip all extension processing. Standalone repos are still cloned, but no symlinks are created. |
