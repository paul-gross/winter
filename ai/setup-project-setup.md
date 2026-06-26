# Guide: Creating project-setup.md

## What it is

`workspace:/ai/project/project-setup.md` is a reproducible recipe for initializing any new feature environment. When an agent creates a new feature environment (e.g., `gamma/`), it follows `workspace:/ai/project/project-setup.md` to create environment files, set up databases, seed data, and do whatever else `winter ws init` doesn't already cover.

## Why it exists

Each feature environment is independent — its own checkout, its own dependencies, its own ports and databases — intended to run in parallel with other feature environments on the same machine. Each environment gets a port window from its index (workspace base seeds `WINTER_PORT_BASE` in the per-environment `.winter.env`); a service orchestration extension like `winter-service-tmux` runs services that consume those vars. This is what allows multiple agents to work on different features simultaneously without interfering with each other. Without setup instructions, agents have to guess how to get things running — or ask the user every time. This file makes environment initialization fast, repeatable, and autonomous.

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

Either way, synthesize the answers — some go into `.winter/config.toml`, some go into `ai/project/project-setup.md`.

### 1. Dependencies → `[[provision.dependency]]` or `[[project_repository]].cmd` in `.winter/config.toml`

Ask: *"How are dependencies installed for each repo? (e.g., `npm install`, `pip install -r requirements.txt`, `cargo build`)"*

If the install is a single, unconditional command that can be run idempotently, it belongs in a `[[provision.dependency]]` handler — run by `winter provision <env> dependency` (or the full `winter provision <env>` chain):

```toml
[[provision.dependency]]
scope = "feature-worktree"
apply = "scripts/install-deps.sh"
```

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

#### `.winter.env` — config-driven vars via `[env.vars]`

`winter ws init <name>` seeds `<name>/.winter.env` with two marker-bracketed managed blocks:

1. **Base block** (written first, at the top) — the env's identity and port window:

```
# >>> winter (managed) — base environment variables; do not edit by hand
WINTER_ENV=alpha
WINTER_ENV_INDEX=1
WINTER_PORT_BASE=4020
WINTER_WORKSPACE_PORT_BASE=4000
# <<< winter (managed) — base block end; hand-managed vars go below the last managed block
```

(`WINTER_WORKSPACE_PORT_BASE` is the index-0 base shared by every env — the port band reserved for workspace-scope singleton services. The workspace root also gets its own `.winter.workspace.env` carrying `WINTER_PORT_BASE` for that scope.)

2. **Derived-vars block** (written below the base block, when `[env.vars]` is declared in `.winter/config.toml`) — project-specific derived variables:

```
# >>> winter (managed) — [env.vars] derived variables; do not edit by hand
export BACKEND_PORT=4020
export FRONTEND_PORT=4021
export DATABASE_URL=postgres://localhost/myapp_4022
# <<< winter (managed) — end of [env.vars] derived variables
```

Both blocks are rewritten idempotently on every `winter ws init` run. Hand-managed lines go below **both** managed blocks (below the `[env.vars]` block closing marker when present, or below the base block otherwise) and are preserved across re-runs.

**Declare project-specific port offsets in `[env.vars]`** rather than appending them by hand per environment:

```toml
# .winter/config.toml
[env.vars]
BACKEND_PORT  = "${WINTER_PORT_BASE+0}"
FRONTEND_PORT = "${WINTER_PORT_BASE+1}"
DB_PORT       = "${WINTER_PORT_BASE+2}"
DATABASE_URL  = "postgres://localhost:${DB_PORT}/myapp-${WINTER_ENV}"  # reuses DB_PORT and WINTER_ENV
```

This means every new environment gets the right ports automatically on `winter ws init <name>`, without any manual step in `project-setup.md`. Use this for any variable derived from the managed base vars (`WINTER_PORT_BASE`, `WINTER_WORKSPACE_PORT_BASE`, `WINTER_ENV`, `WINTER_ENV_INDEX`) or from an earlier `[env.vars]` entry. Only variables that depend on state winter doesn't know (secrets, externally provisioned values) need to be documented in `project-setup.md` instead.

For the full `[env.vars]` token grammar and supported substitutions, see [winter-cli/configuration/ports-and-environments.md — `[env.vars]`](./winter-cli/configuration/ports-and-environments.md#per-env-derived-variables).

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

1. **`.winter/config.toml`** — enriched with trust/bootstrap `cmd` entries, `[[provision.*]]` handlers for dependency/resource/data steps that fit the handler model, plain-pattern `git_excludes`, and `pinned` flags. Keep it boring; if in doubt, leave it out.
2. **`workspace:/ai/project/project-setup.md`** — numbered steps for everything else: conditional installs, env file generation with port offsets, database creation/migration, seed data, post-init build steps, and verification steps not yet migrated to handlers. Use variables like `<letter>` and `<index>` where environment-specific values are needed, and explain how to derive them.
3. *(optional)* **Handler scripts** under an agreed path (e.g. `scripts/`) — the scripts referenced by `[[provision.*]]` `apply`/`destroy`/`reset` fields.

This guide stops at writing the artifacts. Applying the changes to existing environments and running the setup against an environment is the caller's responsibility (see the ws-setup skill).
