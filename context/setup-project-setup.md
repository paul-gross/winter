# Guide: Creating project-setup.md

## What it is

`workspace:/context/project/project-setup.md` is a reproducible recipe for initializing any new feature environment. When an agent creates a new feature environment (e.g., `gamma/`), it follows `workspace:/context/project/project-setup.md` to create environment files, set up databases, seed data, and do whatever else `winter ws init` doesn't already cover.

**Scope: purely setup, and only the setup winter doesn't already do for you.** `project-setup.md` is about one thing — bringing a fresh worktree to a runnable state. Most of that heavy lifting belongs to `winter provision`: its `[[provision.*]]` handlers install dependencies, provision resources, and load data automatically for every environment (see [Division of responsibility](#division-of-responsibility-config-provision-handlers-and-project-setupmd) below). `project-setup.md` captures only the **residual** setup steps provision doesn't execute for you — conditional, multi-step, or environment-specific work, and anything not yet migrated to a handler. It must never duplicate a step a provision handler already performs (defer it to the handler), and it does **not** document how to *run* the project — starting and operating services is owned by `winter service up` and the service extensions. If a step is automated by provision, or is "how to run," it does not belong here.

## Why it exists

Each feature environment is independent — its own checkout, its own dependencies, its own ports and databases — intended to run in parallel with other feature environments on the same machine. Each environment gets a port window from its index; `winter service` injects `WINTER_PORT_BASE` and related vars into every provider subprocess at runtime (inspectable via `winter env <name>`). This is what allows multiple agents to work on different features simultaneously without interfering with each other. Without setup instructions, agents have to guess how to get things running — or ask the user every time. This file makes environment initialization fast, repeatable, and autonomous.

## Division of responsibility: config, provision handlers, and project-setup.md

`winter ws init <letter>` handles the **structural** parts that are uniform across environments:

- Cloning each repo from `[[project_repository]].url`
- Cloning standalone repos declared in `[[standalone_repository]]` and processing winter extensions
- Creating per-repo `git worktree`s on a branch matching the worktree name
- Stamping git identity from `.winter/config.local.toml`
- Writing `git_excludes` (workspace-wide and per-repo) into each repo's `.git/info/exclude`
- Running each repo's `cmd` list — a lightweight trust/bootstrap step (e.g. `mise trust`, `direnv allow`), **not** full dependency installation
- Running every installed extension's `on_env_init` hook

`winter provision <letter>` handles **readiness** via `[[provision.*]]` handlers declared in `.winter/config.toml` and installed extension `winter-ext.toml` files. The three sub-targets map naturally to the setup categories below:

- `dependency` — install language dependencies (`npm install`, `pip install`, `dotnet restore`, etc.)
- `resource` — provision per-env resources (create databases, message-queue vhosts, S3 buckets)
- `data` — load baseline state (run migrations, seed fixtures, create admin users)

Migrating existing `project-setup.md` steps into `[[provision.*]]` handlers is **opt-in** — the handler model is a better long-term home for these steps (re-runnable, machine-parseable, orchestrated by winter), but rewriting working prose is out of scope for any individual feature. Migrate a step when it makes sense to do so; leave the rest in `project-setup.md`.

The goal here is **not** to replace `project-setup.md` entirely. It's to give a cleaner home for steps that can be expressed as simple scripts, while `project-setup.md` remains the right place for conditional, multi-step, or environment-specific logic that doesn't fit the handler model.

Rule of thumb:
- **Goes in `[[provision.*]]` handler:** a single script that can be run idempotently with no branching logic; install, create, or seed steps.
- **Goes in config (`cmd` list):** a one-line trust/bootstrap step that must run before anything else (e.g. `mise trust`).
- **Stays in project-setup.md:** anything conditional, multi-step, or that references dynamic `<letter>`/`<index>` values in ways a handler script can't easily parameterise.

## How to create it with the user

Offer the user two approaches: *"I can research your codebase and figure out the setup requirements automatically, or you can walk me through it. Which do you prefer?"*

**If researching automatically:** Spawn an Opus subagent to explore the project repos. The subagent searches for package managers, dockerfiles, docker-compose files, env templates (`.env.example`, `.env.sample`), migration scripts, README setup sections, and existing documentation. Using a subagent keeps the research out of the main setup context — we only care about the findings. The subagent reports back a structured summary of what it found, and you present those findings to the user for confirmation before writing.

**If the user prefers a guided approach**, walk through each area below with focused questions.

Either way, synthesize the answers — some go into `.winter/config.toml`, some go into `context/project/project-setup.md`.

### 1. Dependencies → `[[provision.dependency]]` or `[[project_repository]].cmd` in `.winter/config.toml`

Ask: *"How are dependencies installed for each repo? (e.g., `npm install`, `pip install -r requirements.txt`, `cargo build`)"*

If the install is a single, unconditional command that can be run idempotently, it belongs in a `[[provision.dependency]]` handler — run by `winter provision <env> dependency` (or the full `winter provision <env>` chain).

**Inline the command in `apply` by default.** `apply` takes the command directly — a string, or an array of strings run in order — so a one- or few-line install needs no separate script file:

```toml
[[provision.dependency]]
scope = "feature-worktree"
apply = "npm ci"                         # string form — a single command
# apply = ["uv sync", "uv run pre-commit install"]   # array form — runs in order, stops at first failure
```

Reach for a standalone `scripts/*.sh` file only when an existing project script already encapsulates the step — point `apply` at it; don't fabricate a wrapper script for a command that fits inline. See [winter-cli/configuration/provision.md](./winter-cli/configuration/provision.md) for the full `apply` grammar.

**If a step doesn't fit reasonably inline — heavy, complex, or multi-step setup with no ready-made script — defer it rather than inventing one.** Leave it out of the generated config and tell the user plainly that they must complete that step themselves to enable winter functionality for it. A fabricated, untested setup script is worse than an honest gap: surface the gap and let the user wire it up.

The repo's `cmd` list in `.winter/config.toml` is reserved for lightweight trust/bootstrap steps that must run before anything else (e.g. `mise trust`, `direnv allow`):

```toml
[[project_repository]]
name = "my-app"
url = "..."
cmd = ["mise trust"]
```

If the install needs branching, env-dependent decisions, or post-install steps that read environment state, document it in `project-setup.md` instead.

### 2. Environment files → mostly `project-setup.md`, generated artifacts → `git_excludes`

Ask: *"Does the project need environment files (e.g., `.env`)? What variables are required?"*

Probe for:
- Base ports for each service (web server, API, database, etc.)
- How ports should be offset per environment (alpha=+1, beta=+2, etc.)
- Database connection strings — do they need per-environment database names?
- API keys or secrets — can they be shared across environments or do they need to be unique?
- Any other per-environment config (Redis prefix, S3 bucket, etc.)

The env-file *generation logic* (heredocs that write per-environment values into env files) goes into `project-setup.md` as numbered steps.

#### Config-driven vars via env var bands

winter computes the environment at runtime from the env var bands and the managed base vars, and injects it via two paths:

- **`winter service up`** injects the full env into every provider subprocess environment directly.
- **`winter env <name>`** prints the vars as sourceable `export KEY=value` lines for shell use.

**Declare project-specific port offsets in `[env.feature.vars]`** rather than writing them by hand per environment:

```toml
# .winter/config.toml
[env.feature.vars]
BACKEND_PORT  = "${WINTER_PORT_BASE+0}"
FRONTEND_PORT = "${WINTER_PORT_BASE+1}"
DB_PORT       = "${WINTER_PORT_BASE+2}"
DATABASE_URL  = "postgres://localhost:${DB_PORT}/myapp-${WINTER_ENV}"  # reuses DB_PORT and WINTER_ENV
```

This means every new environment gets the right ports automatically, without any manual step in `project-setup.md`. Use `[env.workspace.vars]` for variables tied to shared workspace services (use `${WINTER_WORKSPACE_PORT_BASE+N}` there, since `WINTER_PORT_BASE` is absent at workspace scope). Use this for any variable derived from the managed base vars or from an earlier band entry. Only variables that depend on state winter doesn't know (secrets, externally provisioned values) need to be documented in `project-setup.md` instead.

**Allocate ports as `${WINTER_PORT_BASE+N}`, never `${WINTER_ENV_INDEX+<literal port>}`.** `WINTER_PORT_BASE` is the start of this env's reserved port window (`base_port + index * ports_per_env`), and `N` is a small per-service offset *within* that window — so `WEB`, `API`, `DB` become `WINTER_PORT_BASE+0`, `+1`, `+2`, landing inside the env's own block and never colliding with another env. `WINTER_ENV_INDEX` is only the env's small ordinal (alpha=1, beta=2, …); adding it to a literal base port (`${WINTER_ENV_INDEX+4200}` → alpha=4201, beta=4202) puts adjacent envs one port apart and overflows each env's window. That older "base + greek-index" scheme is obsolete — do not emit it.

**There is no `.winter.env` file.** Per-env variables live entirely in `[env.feature.vars]` / `[env.workspace.vars]` and are injected at runtime; winter does not read or write any `.winter.env`. Do not generate one, reference one, or add one to `git_excludes`.

To inspect the computed vars for a given env:

```bash
winter env alpha                        # print as export lines
source <(winter env alpha)              # source into the current shell
```

For the full band semantics and token grammar, see [winter-cli/configuration/ports-and-environments.md — Env var bands](./winter-cli/configuration/ports-and-environments.md#env-var-bands).

#### Other env files

For `.env`, `.env.development.local`, `.env.production`, etc., generate them with full `>` heredocs (or whatever tool the project provides). Those aren't shared with `winter ws init`, so they can be rewritten freely.

The *generated file paths* go into `.winter/config.toml` as `git_excludes` so they're never committed:

```toml
# Workspace-wide (every repo gets these excludes)
git_excludes = ["*.local.*", "*.generated.*"]

[[project_repository]]
name = "frontend"
git_excludes = [".env.development.local"]   # only this repo
```

### 3. Databases → `[[provision.resource]]` / `[[provision.data]]` or `project-setup.md`

Ask: *"Does the project use databases? How are they created and migrated?"*

Probe for:
- Database engine (Postgres, MySQL, SQLite, etc.)
- Does each environment need its own database? (Usually yes — e.g., `myapp_alpha`, `myapp_beta`)
- Migration commands
- Whether the database server is shared or per-environment

Database creation and migration are natural candidates for `[[provision.resource]]` and `[[provision.data]]` handlers — idempotent scripts that create the per-env database and run migrations. If you haven't migrated to handlers yet, document these steps in `project-setup.md` as per-environment orchestration.

### 4. Build steps → usually `project-setup.md`

Ask: *"Are there any build or codegen steps needed before the project can run? (e.g., `npm run build`, code generation, compiling protos)"*

Default to `project-setup.md`. Only append a build step to a repo's `cmd` list if it's a single command with no dependencies on env files, ports, or the environment's database — same rule as section 1.

### 5. Seed data → `[[provision.data]]` or `project-setup.md`

Ask: *"Does the project need seed data or initial state to be useful? (e.g., fixtures, migrations with default data, creating admin users)"*

Seed data is a natural candidate for a `[[provision.data]]` handler — a re-runnable, wipe-and-reload script. If you haven't migrated to handlers yet, document these steps in `project-setup.md` as per-environment orchestration.

### 6. Verification → `project-setup.md`

Ask: *"How can we verify that setup worked? Is there a health check, test suite, or command that confirms things are ready?"*

Final step of `project-setup.md`.

### 7. Pinned repos → `[[project_repository]].pinned`

Ask: *"Are any of these repos shared tooling that should always track `origin/main` instead of being branched per-feature? (e.g., a CLI tool, mock services)"*

For each one, set `pinned = true` on its `[[project_repository]]` entry. `winter ws init <letter>` skips feature branching for pinned repos and just keeps them on the main branch.

### Output

Two or three artifacts:

1. **`.winter/config.toml`** — enriched with trust/bootstrap `cmd` entries, `[[provision.*]]` handlers (commands inlined in `apply`, per section 1) for dependency/resource/data steps that fit the handler model, plain-pattern `git_excludes`, and `pinned` flags. Keep it boring; if in doubt, leave it out.
2. **`workspace:/context/project/project-setup.md`** — numbered steps for setup not covered by config or handlers: conditional installs, env file generation with port offsets, database creation/migration, seed data, post-init build steps, and verification steps not yet migrated to handlers. Use variables like `<letter>` and `<index>` where environment-specific values are needed, and explain how to derive them. Stay within the setup-only scope from [What it is](#what-it-is) — no "how to run" content, and defer command-expressible steps to handlers. Author it as prose with **no manual line wrapping** — one sentence or paragraph per physical line, never reflowed to a fixed column — so later one-word edits don't reflow the whole paragraph and bury the real change in churn.
3. *(rare)* **Handler scripts** under an agreed path (e.g. `scripts/`) — only for `[[provision.*]]` steps too multi-step or conditional to inline in `apply`. Inline simple commands directly (section 1); don't default to a script file.

This guide stops at writing the artifacts. Applying the changes to existing environments and running the setup against an environment is the caller's responsibility (see the ws-setup skill).
