# Extensions

Standalone repositories can opt into contributing skills and agents to the workspace's `.claude/` directory by shipping
a `winter-ext.toml` file at the repo root. A project repo (declared via `[[project_repository]]`) can opt in the same
way — see [Project-repo extensions](#project-repo-extensions) below for how its eligibility and context delivery differ
from a standalone's.

## `winter-ext.toml` schema

```toml
name = "winter-backlog"        # default symlink prefix when no override is set
prefix = "wsb"                 # optional shorter prefix; takes precedence over `name`
description = "Backlog tooling for winter issues." # optional; one-line summary shown on a lazy AGENTS.winter.md bullet
load = "lazy"                  # optional; "eager" (@-import) or "lazy" (link) — see Context delivery below
skills_dir = "skills"          # optional; explicit path overrides default discovery
agents_dir = "agents"          # optional; explicit path overrides default discovery
doctor = "scripts/doctor.sh"   # optional; executable that emits NDJSON probe events for `winter doctor`
lint = "scripts/lint.sh"       # optional; executable(s) emitting NDJSON findings for `winter lint` (str, list, or [lint] table)
requires = ["winter-product"]  # optional; other modules this one depends on (see `winter graph`)

# Lint suppression + scripts — the table form of `lint`, needed because TOML cannot
# hold both `lint = [...]` and `[lint.ignore]`. Globs are repo-relative. See lint.md.
[lint]
scripts = ["scripts/lint.sh"]  # optional; same meaning as the scalar `lint` above

[lint.ignore]
paths = ["fixtures/**"]        # optional; every check goes quiet on these paths

[lint.ignore.checks]
link-anchors = ["fixtures/**"] # optional; silence one check, keep the rest of the file covered

[provides]
service = "workflow/service"   # this extension provides the `service` capability; entrypoint relative to repo root

# Workspace-root excludes — appended to this extension's managed block in the workspace exclude file
[excludes]
paths = ["/.winter/process-compose/"]       # optional; Git ignore patterns anchored at the workspace root
space_kinds = ["scores", "manifests"]       # optional; winter-space kinds whose directories to exclude

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

- `name` is required and must be unique across all sources (the workspace config and every installed extension). A name
  collision across sources is a fatal error at `service up` time.
- `scope` defaults to `"feature-environment"` when omitted. Accepted values: `"feature-environment"` (one instance per
  active env), `"workspace"` (one shared instance). Unknown scope values are rejected.
- `command` is required.
- `target` is required by the tmux provider (window.pane address, e.g. `"2.0"`). Container-based providers use `image`
  instead. Unknown keys are rejected.
- `ports` is an optional list of integer port numbers the service listens on; used by status rendering.
- `image` is optional; used by container-based providers.

The extension's `[[service]]` entries are aggregated by `winter service up` alongside any `[[service]]` entries in the
workspace's `.winter/config.toml`, deduplication is enforced by name, and the result is written to a temporary TOML file
whose path is injected into each provider subprocess as `WINTER_SERVICE_MANIFEST`. See
[../contracts/service-orchestrator.md](../contracts/service-orchestrator.md#per-action-env-var-winter_service_manifest)
for the full manifest format and consume-or-ignore rule.

`requires` declares the other winter modules this one references and therefore needs when installed on its own. Each
entry is a module name — the `<context>` half of a `<context>:/path` reference. It is the data `winter graph` aggregates
and the module-extractability lint check validates references against.

The final symlink prefix is resolved with this precedence: `prefix` on the workspace config entry > `prefix` in
`winter-ext.toml` > `name` in `winter-ext.toml` > the standalone repo's directory name.

## What gets symlinked

When `skills_dir` and `agents_dir` aren't set explicitly, winter searches for them in this order and uses the first that
exists:

- `skills/` then `.claude/skills/`
- `agents/` then `.claude/agents/`

That covers both the winter convention (top-level `skills/`/`agents/`) and the Claude Code convention
(`.claude/skills/`/`.claude/agents/`), so a vanilla Claude Code repo can be adopted as an extension without
modification. Setting `skills_dir`/`agents_dir` explicitly in `winter-ext.toml` skips the fallback and uses exactly the
declared path.

For each subdirectory under the resolved skills root, winter installs the skill into every code-agent vendor's skills
directory, choosing the install mechanism from the vendor's capability (the `CodeAgentVendor` enum):

- **Claude Code** (`.claude/skills/<prefix>-<dir>`) and **Codex** (`.codex/skills/<prefix>-<dir>`) get a **relative
  symlink** to the source.
- **OpenCode** (`.opencode/skill/<prefix>-<dir>`) gets a **real-directory copy**. OpenCode discovers skills by globbing
  `skill/**/SKILL.md` and its globber does **not** traverse symlinked directories — a symlink there would be invisible
  to it, so the skill must be a real directory. The copy is idempotent: on each `winter ws init`, winter content-hashes
  the source and destination and re-copies (delete-then-copy) only on a mismatch; nothing is persisted. Stale
  `<prefix>-*` copies with no live source are pruned, mirroring the symlink prune.

`.opencode/skill/` is read only by OpenCode, and OpenCode's read of `.claude/skills` only picks up real directories (not
the symlinks there), so there is no double-loading across the symlink and copy sets.

For each `.md` file under the resolved agents root, winter transforms the canonical agent into a per-harness,
git-excluded copy at `.claude/agents/<prefix>-<name>.md` (Claude), `.codex/agents/<prefix>-<name>.toml` (Codex), and
`.opencode/agent/<prefix>-<name>.md` (OpenCode) — not a symlink.

The workspace `.gitignore` is updated with a marker-bracketed block per extension:

```text
# >>> winter-backlog (managed by winter)
/winter-backlog/
.claude/skills/wsb-*
.codex/skills/wsb-*
.opencode/skill/wsb-*
.claude/agents/wsb-*
# <<< winter-backlog
```

### Declaring excludes

An extension that writes machine-local files or generated artifacts at the workspace root declares them in `[excludes]`;
both fields are optional lists of non-empty strings. The rules land in the workspace's Git **exclude file**
(`.git/info/exclude`), inside the extension's own managed block after the repository and projection rules — never in the
tracked `.gitignore`, which winter does not touch.

- `paths` — workspace-root-relative Git ignore patterns. A single leading `/` is a Git root anchor, not a filesystem
  path; a pattern without it is anchored when emitted. Negated (`!`) patterns, `..` segments, home (`~`), drive, and UNC
  forms, root-only entries, comment-looking (`#`) entries, and newline, carriage-return, or NUL characters are rejected.
- `space_kinds` — names accepted by `winter space`. Each resolves through the effective `[space]` configuration
  ([Artifact space](./space.md)) without creating the directory, and contributes an anchored directory rule with Git
  metacharacters escaped so the name matches literally. A kind that resolves outside the workspace contributes no rule;
  one that resolves to the workspace root is an error.

A malformed `[excludes]` table, field type, or entry is a manifest error naming the extension and offending value; init
exits non-zero, the declaration is not partially applied, and the block keeps only its repository-path rules. Removing a
declaration, relocating a space kind, or removing the extension updates or removes the block on the next
`winter ws init`. Repositories with `extension = false` or `adopt_extensions = "none"` contribute no declaration rules.

## Workspace skills

Besides extension skills from standalone repos, `winter ws init` always projects skills authored directly in the
workspace. Projection is always-on: the `prefix` defaults to `"ws"` and the source directory defaults to `skills/`
(configurable via `skills_dir`). The workspace skill namespace must be distinct from any `[[standalone_repository]]`
`prefix` value — both prune `<prefix>-*` and bare `<prefix>` entries in the same target directories, and a collision is
rejected at config load. See [config-files.md — workspace skill prefix](./config-files.md#workspace-skill-prefix) for
the full configuration surface.

## Frontmatter convention

Claude Code lets a SKILL.md frontmatter `name` field override the directory name during skill discovery. That defeats
the prefix-by-directory-name design, so winter requires extension SKILL.md files to **omit the `name` field** — letting
the directory name (which winter controls via the symlink or copy) be authoritative. Winter validates this on install
and refuses if any SKILL.md sets `name`. This rule applies equally to workspace-authored skills.

## Extension hooks

Extensions can also declare lifecycle hooks in `winter-ext.toml`:

```toml
[hooks]
on_env_init            = "./hooks/init-worktree.sh"
on_env_destroy         = "./hooks/destroy-worktree.sh"
on_workspace_reconcile = "./hooks/reconcile-workspace.sh"
```

- `on_env_init` fires after `winter ws init <env>` creates every per-repo worktree and allocates the env index. Use it
  to provision per-env state (tmux sessions, databases, watchers).
- `on_env_destroy` fires *before* `winter ws destroy <env>` removes any per-repo worktree or the env directory. Use it
  to release whatever `on_env_init` provisioned.
- `on_workspace_reconcile` fires **once per workspace-level reconcile** — specifically `winter ws init` (no target) and
  `winter ws init --all`. Fires after standalone/extension repos are reconciled so the extension exists on disk, and for
  the `--all` path, before the per-env loop. Use it for one-time workspace setup that should re-run when the workspace
  is re-reconciled (e.g. writing workspace-level config files, registering extensions with external tools).

Hook scripts must be **relative paths inside the extension directory** (so the extension owns its scripts; winter
resolves them against the extension root).

### Hook env-var contract

**Env hooks** (`on_env_init`, `on_env_destroy`) are invoked with:

| Var                          | Meaning                                                                                                                                                                                                                                  |
| ---------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `WINTER_WORKSPACE_DIR`       | Absolute path to the workspace root.                                                                                                                                                                                                     |
| `WINTER_EXT_DIR`             | Absolute path to this extension's clone (the dir containing `winter-ext.toml`).                                                                                                                                                          |
| `WINTER_EXT_PREFIX`          | The resolved symlink prefix for this extension (`wf`, `wst`, …).                                                                                                                                                                         |
| `WINTER_EXT_CONFIG_DIR`      | Absolute path to this extension's writable config/asset directory (default `.winter/config/<name>/`).                                                                                                                                    |
| `WINTER_SERVICE_PREFIX`      | The resolved workspace-level service-orchestration namespace prefix. Workspace-invariant — present for **every** hook regardless of scope.                                                                                               |
| `WINTER_WORKSPACE_PORT_BASE` | `base_port + ports_per_env * 0` — the index-0 base, i.e. the port band reserved for workspace-scope services. Present for **every** hook, including `on_workspace_reconcile` (which otherwise gets no `WINTER_ENV*`/`WINTER_PORT_BASE`). |
| `WINTER_ENV`                 | The env name (`alpha`, `beta`, …). Env-scoped hooks only.                                                                                                                                                                                |
| `WINTER_ENV_INDEX`           | The persisted port-offset index for this env (alias envs get fixed slots `1..N`; ad-hoc names hash into the remainder band). Env-scoped hooks only.                                                                                      |
| `WINTER_PORT_BASE`           | `base_port + ports_per_env * WINTER_ENV_INDEX` (defaults: `4000 + 20 * index`). Env-scoped hooks only.                                                                                                                                   |

The hook's **cwd is the env root** (`<workspace>/<env>/`). Hooks should read these env vars rather than parse `argv`.

**Workspace hook** (`on_workspace_reconcile`) is invoked with only:

| Var                     | Meaning                                                              |
| ----------------------- | -------------------------------------------------------------------- |
| `WINTER_WORKSPACE_DIR`  | Absolute path to the workspace root.                                 |
| `WINTER_EXT_DIR`        | Absolute path to this extension's clone.                             |
| `WINTER_EXT_PREFIX`     | The resolved symlink prefix for this extension.                      |
| `WINTER_EXT_CONFIG_DIR` | Absolute path to this extension's writable config/asset directory.   |
| `WINTER_SERVICE_PREFIX` | The resolved workspace-level service-orchestration namespace prefix. |

The hook's **cwd is the workspace root**.

**Strict vs non-strict on destroy.** By default, a non-zero exit from a destroy hook is logged and the teardown
continues so a broken hook doesn't trap an env on disk. Pass `--strict` to `winter ws destroy` (or set it in CI/scripted
use) when a hook failure must surface as a user-actionable error before any worktree is removed.

## Context delivery

`AGENTS.winter.md` opens with a single `# Path notation resolution` heading; every bullet under it binds an extension
name (its `<name>:` path-notation prefix) to the location that resolves it. The entry point is resolved the same way for
every extension — `index.md`, then `AGENTS.md`, then `context/index.md`, first match wins (mirroring the
`skills_dir`/`agents_dir` default-discovery fallback above) — unless the repo's workspace entry declares `entry`, which
replaces that list (see below). How the entry point reaches an agent is set by `load`.

| `load`  | Bullet <!-- winter-lint:example -->                              | Cost                                                                                   |
| ------- | ---------------------------------------------------------------- | -------------------------------------------------------------------------------------- |
| `eager` | `- **winter-canon**: @.winter/ext/canon/index.md`                | Injected into every session, with everything it `@`-imports, against `injected_bytes`. |
| `lazy`  | `- **[winter-canon](.winter/ext/canon/index.md)** — description` | One routing line. The agent follows the link only when the row's subject is in scope.  |

Pick `eager` for context every session needs regardless of the task; pick `lazy` for context that only matters when its
subject is in scope. `description` is what tells a reader whether to follow a lazy link, so an extension that declares
`load = "lazy"` should declare one too — it is not required, and a row without it renders as the bare link.

**Precedence.** `load` is resolved as: the repo's workspace entry (`[[standalone_repository]]` /
`[[project_repository]]`) > `load` in `winter-ext.toml` > the repo-kind default. The workspace entry also accepts
`load = "none"`, which renders no bullet while leaving every other extension feature in place; a manifest may not
declare `none`. The keys themselves are defined in
[repositories.md](./repositories.md#extension-load-entry--the-workspace-governs-a-repos-extension-role).

**Defaults.** When neither declares `load`, the mode falls back to the extension's repo kind — a standalone is `eager`,
a project repo is `lazy`.

**Manifest-less repos.** A repo with no `winter-ext.toml` renders a bullet only under `adopt_extensions = "all"` or when
its workspace entry opts it in explicitly. The opt-in is a declared `load = "eager"` or `"lazy"`, or a declared `entry`;
an `entry` alone renders at the repo kind's default load. It holds under `winter` too, and it opts the repo into context
delivery only, never into skills, agents, hooks, or any other extension feature. For a `[[project_repository]]` with no
manifest it yields the lazy routing row described under [Project-repo extensions](#project-repo-extensions). Under the
default `winter` mode a plain repo cloned for its data (a root `AGENTS.md` written for contributors to that repo)
otherwise injects nothing, the same gate skill and agent projection apply.

**`entry`.** The workspace entry's `entry` is a prioritized candidate list: winter takes the first listed path that
exists in the repo — one entry point, never several — in place of the default `index.md`, `AGENTS.md`,
`context/index.md`. A repo that needs several files injected `@`-imports them from its entry point. When no listed path
exists, the repo renders no bullet and `winter ws init` reports a per-repo warning naming the paths it tried, since the
workspace asked for one by name. A repo with no declared `entry` and none of the defaults renders no bullet, silently.

**`load = "eager"` is refused for a project repo**, whether declared by the manifest or the workspace entry. A project
repo has N copies on disk (see below), and an eager import resolves to exactly one of them. `winter ws init` reports the
declaration and renders the bullet lazily rather than failing the reconcile.

## Project-repo extensions

A `[[project_repository]]` whose `projects/<name>/` root carries a `winter-ext.toml` is extension-eligible in place — no
separate `[[standalone_repository]]` clone is needed. Every extension feature (`winter doctor`, `winter lint`,
`winter graph`, the capability registry, service-manifest collection, provision handlers, hooks, skills/agents
projection) resolves it exactly like a standalone, reading `winter-ext.toml` and everything it declares from the
`projects/<name>/` root — the source checkout, never a per-env worktree. A project repo with no manifest is not
extension-eligible; the explicit `load` / `entry` opt-in described under [Context delivery](#context-delivery) gives it
the routing row below and nothing more.

Context delivery is the one place a project-repo extension renders differently from a standalone. A project repo has N
copies on disk — the source checkout plus one worktree per feature env — so it defaults to `load = "lazy"` and cannot
opt into `eager`: an `@`-import would inject the `master` copy, which goes stale against whatever feature branch an
agent is actually editing.

Its bullet also differs in shape from a lazy standalone's. There is no single path to link to, so it carries the literal
`<env>/<name>/` entry-point path template in backticks, plus the manifest's one-line `description` when one is declared:

```text
- **winter-docs**: `<env>/winter-docs/index.md` — Public documentation site generator.
```

**`<env>` binding.** `AGENTS.winter.md` auto-loads into every session via the workspace's `# Winter Extensions` block,
including a session with no feature env in scope — `context/winter/workspace-layout.md` mandates subagents spawn from
the workspace root, where `<env>` is unbound and no env directory may even exist yet. When one or more project-repo
bullets are rendered, `AGENTS.winter.md` carries a one-line note ahead of them: `<env>` binds to the feature-env
directory the reading agent is actually working in; a workspace-root reader with no env bound instead opens the source
checkout at `projects/<name>/`.

A repo declared as **both** `[[project_repository]]` (with a root `winter-ext.toml`) and `[[standalone_repository]]`
dedupes to a single extension entry: the **standalone** checkout wins, silently. Double-declaring is a supported way to
pin an extension to a `path`, `prefix`, or `ref` that `[[project_repository]]` has no field for, so it is not a
misconfiguration and winter does not warn about it — the repo keeps rendering as its eager `@`-import. Drop the
`[[standalone_repository]]` declaration and the project-repo entry takes over automatically, rendering the extension as
a no-`@` path-template bullet instead.

## `adopt_extensions` modes

The top-level `adopt_extensions` field controls when winter processes a standalone repo's skills and agents:

| Value              | Behavior                                                                                                                                                                                                                                                                                       |
| ------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `winter` (default) | Process only standalone repos that have a `winter-ext.toml` at the repo root — skills, agents, and the `AGENTS.winter.md` bullet alike. SKILL.md frontmatter is strictly validated. A manifest-less repo renders a bullet only on explicit opt-in (see [Context delivery](#context-delivery)). |
| `all`              | Process any standalone repo with `skills/`, `agents/`, `.claude/skills/`, or `.claude/agents/` directories, with or without a manifest, and render a bullet for any with an entry point. Frontmatter validation downgrades from refuse-to-warn — collisions become the user's problem.         |
| `none`             | Skip all extension processing. Standalone repos are still cloned, but no symlinks are created.                                                                                                                                                                                                 |

A repo entry's `extension = false` overrides all three modes: the repo is cloned and pinned but processed by nothing.
`nested = true` on a `[[project_repository]]` implies `extension = false` — see
[repositories.md — nested](./repositories.md#nested--a-project-repo-that-is-itself-a-workspace).
