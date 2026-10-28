# Guide: Creating project-setup.md

## What it is

`workspace:/context/project/project-setup.md` is a reproducible recipe for initializing any new feature environment.
When an agent creates a new feature environment (e.g., `gamma/`), it follows
`workspace:/context/project/project-setup.md` to create environment files, set up databases, seed data, and do whatever
else `winter ws init` doesn't already cover.

**Scope: purely setup, and only the setup winter doesn't already do for you.** `project-setup.md` is about one thing —
bringing a fresh worktree to a runnable state. Most of that heavy lifting belongs to `winter provision`: its
`[[provision.*]]` handlers install dependencies, provision resources, and load data automatically for every environment
(see [Division of responsibility](#division-of-responsibility-config-provision-handlers-and-project-setupmd) below).
`project-setup.md` captures only the **residual** setup steps provision doesn't execute for you — conditional,
multi-step, or environment-specific work, and anything not yet migrated to a handler. It must never duplicate a step a
provision handler already performs (defer it to the handler), and it does **not** document how to *run* the project —
starting and operating services is owned by `winter service up` and the service extensions. If a step is automated by
provision, or is "how to run," it does not belong here.

## Why it exists

Each feature environment is independent — its own checkout, its own dependencies, its own ports and databases — intended
to run in parallel with other feature environments on the same machine. Each environment gets a port window from its
index; `winter service` injects `WINTER_PORT_BASE` and related vars into every provider subprocess at runtime
(inspectable via `winter env <name>`). This is what allows multiple agents to work on different features simultaneously
without interfering with each other. Without setup instructions, agents have to guess how to get things running — or ask
the user every time. This file makes environment initialization fast, repeatable, and autonomous.

## Caller contract

This guide is invoked from the `ws-setup` skill's project-settings step. The caller tells you whether **service
discovery is in scope** for this run (chosen when the user picked "environment + services" over "environment only").
When it's in scope, extend both the automatic research and the guided walkthrough to also produce the services facet
(see section 6 below), in the same research pass as everything else — [`service-discovery.md`](./service-discovery.md)
is the canonical definition of exactly what gets discovered per service; this guide follows that schema rather than
defining its own. When it's out of scope, skip that facet entirely — the five settings facets below are unaffected
either way.

## Division of responsibility: config, provision handlers, and project-setup.md

`winter ws init <letter>` handles the **structural** parts that are uniform across environments:

- Cloning each repo from `[[project_repository]].url`
- Cloning standalone repos declared in `[[standalone_repository]]` and processing winter extensions
- Creating per-repo `git worktree`s on a branch matching the worktree name
- Stamping git identity from `.winter/config.local.toml`
- Writing `git_excludes` (workspace-wide and per-repo) into each repo's `.git/info/exclude`
- Running each repo's `cmd` list — a lightweight trust/bootstrap step (e.g. `direnv allow`), **not** full dependency
  installation
- Running every installed extension's `on_env_init` hook

`winter provision <letter>` handles **readiness** via `[[provision.*]]` handlers declared in `.winter/config.toml` and
installed extension `winter-ext.toml` files. The three sub-targets map naturally to the setup categories below:

- `dependency` — install language dependencies (`npm install`, `pip install`, `dotnet restore`, etc.)
- `resource` — provision per-env resources (create databases, message-queue vhosts, S3 buckets)
- `data` — load baseline state (run migrations, seed fixtures, create admin users)

Migrating existing `project-setup.md` steps into `[[provision.*]]` handlers is **opt-in** — the handler model is a
better long-term home for these steps (re-runnable, machine-parseable, orchestrated by winter), but rewriting working
prose is out of scope for any individual feature. Migrate a step when it makes sense to do so; leave the rest in
`project-setup.md`.

The goal here is **not** to replace `project-setup.md` entirely. It's to give a cleaner home for steps that can be
expressed as simple scripts, while `project-setup.md` remains the right place for conditional, multi-step, or
environment-specific logic that doesn't fit the handler model.

Rule of thumb:

- **Goes in `[[provision.*]]` handler:** a single script that can be run idempotently with no branching logic; install,
  create, or seed steps.
- **Goes in config (`cmd` list):** a one-line trust/bootstrap step that must run before anything else (e.g.
  `direnv allow`).
- **Stays in project-setup.md:** anything conditional, multi-step, or that references dynamic `<letter>`/`<index>`
  values in ways a handler script can't easily parameterise.

## How to create it with the user

Offer the user two approaches: *"I can research your codebase and figure out the setup requirements automatically, or
you can walk me through it. Which do you prefer?"*

**If researching automatically:** Spawn an Opus-class (or equivalent) subagent to explore the project repos. The
subagent searches for package managers, dockerfiles, docker-compose files, env templates (`.env.example`,
`.env.sample`), migration scripts, README setup sections, and existing documentation. **When service discovery is in
scope**, extend the subagent's brief to also gather the services facet defined in
[`service-discovery.md`](./service-discovery.md) — going deep on how the app is actually wired, including its
container/image wiring if the project already uses one. Using a subagent keeps the research out of the main setup
context — we only care about the findings. The subagent reports back a structured summary of what it found — the five
settings facets, plus the services facet when in scope — and you present those findings to the user for confirmation
before writing.

**If the user prefers a guided approach**, walk through each area below with focused questions.

Either way, synthesize the answers — some go into `.winter/config.toml`, some go into
`context/project/project-setup.md`.

### 1. Dependencies → `[[provision.dependency]]` or `[[project_repository]].cmd` in `.winter/config.toml`

Ask: *"How are dependencies installed for each repo? (e.g., `npm install`, `pip install -r requirements.txt`,
`cargo build`)"*

If the install is a single, unconditional command that can be run idempotently, it belongs in a
`[[provision.dependency]]` handler — run by `winter provision <env> dependency` (or the full `winter provision <env>`
chain).

**Inline the command in `apply` by default.** `apply` takes the command directly — a string, or an array of strings run
in order — so a one- or few-line install needs no separate script file:

```toml
[[provision.dependency]]
scope = "feature-worktree"
apply = "npm ci"                         # string form — a single command
# apply = ["uv sync", "uv run pre-commit install"]   # array form — runs in order, stops at first failure
```

Reach for a standalone `scripts/*.sh` file only when an existing project script already encapsulates the step — point
`apply` at it; don't fabricate a wrapper script for a command that fits inline. See
[cli/configuration/provision.md](../cli/configuration/provision.md) for the full `apply` grammar.

**If a step doesn't fit reasonably inline — heavy, complex, or multi-step setup with no ready-made script — defer it
rather than inventing one.** Leave it out of the generated config and tell the user plainly that they must complete that
step themselves to enable winter functionality for it. A fabricated, untested setup script is worse than an honest gap:
surface the gap and let the user wire it up.

The repo's `cmd` list in `.winter/config.toml` is reserved for lightweight trust/bootstrap steps that must run before
anything else (e.g. `direnv allow`):

```toml
[[project_repository]]
name = "my-app"
url = "..."
cmd = ["direnv allow"]
```

If the install needs branching, env-dependent decisions, or post-install steps that read environment state, document it
in `project-setup.md` instead.

### 2. Environment files → mostly `project-setup.md`, generated artifacts → `git_excludes`

Ask: *"Does the project need environment files (e.g., `.env`)? What variables are required?"*

Probe for:

- The services that need a port (web server, API, database, etc.)
- The per-service offset of each within the env's port window (`web=+0`, `api=+1`, `db=+2`, …) — winter already gives
  each env its own window, so you offset *per service*, never per environment
- Database connection strings — do they need per-environment database names?
- API keys or secrets — can they be shared across environments or do they need to be unique?
- Any other per-environment config (Redis prefix, S3 bucket, etc.)

The env-file *generation logic* (heredocs that write per-environment values into env files) goes into `project-setup.md`
as numbered steps.

#### Config-driven vars via env var bands

winter computes the environment at runtime from the env var bands and the managed base vars, and injects it via two
paths:

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

This means every new environment gets the right ports automatically, without any manual step in `project-setup.md`. Use
`[env.workspace.vars]` for variables tied to shared workspace services (use `${WINTER_WORKSPACE_PORT_BASE+N}` there,
since `WINTER_PORT_BASE` is absent at workspace scope). Use this for any variable derived from the managed base vars or
from an earlier band entry. Only variables that depend on state winter doesn't know (secrets, externally provisioned
values) need to be documented in `project-setup.md` instead.

**Allocate ports as `${WINTER_PORT_BASE+N}`.** `WINTER_PORT_BASE` is the start of this env's reserved port window
(`base_port + index * ports_per_env`), and `N` is a small per-service offset *within* that window — so `WEB`, `API`,
`DB` become `WINTER_PORT_BASE+0`, `+1`, `+2`, landing inside the env's own block and never colliding with another env.

Per-env variables live in `[env.feature.vars]` / `[env.workspace.vars]` and are injected at runtime.

To inspect the computed vars for a given env:

```bash
winter env alpha                        # print as export lines
source <(winter env alpha)              # source into the current shell
```

For the full band semantics and token grammar, see
[cli/configuration/ports-and-environments.md — Env var bands](../cli/configuration/ports-and-environments.md#env-var-bands).

#### Other env files

For `.env`, `.env.development.local`, `.env.production`, etc., generate them with full `>` heredocs (or whatever tool
the project provides). Those aren't shared with `winter ws init`, so they can be rewritten freely.

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

Database creation and migration are natural candidates for `[[provision.resource]]` and `[[provision.data]]` handlers —
idempotent scripts that create the per-env database and run migrations. If you haven't migrated to handlers yet,
document these steps in `project-setup.md` as per-environment orchestration.

### 4. Seed data → `[[provision.data]]` or `project-setup.md`

Ask: *"Does the project need seed data or initial state to be useful? (e.g., fixtures, migrations with default data,
creating admin users)"*

Seed data is a natural candidate for a `[[provision.data]]` handler — a re-runnable, wipe-and-reload script. If you
haven't migrated to handlers yet, document these steps in `project-setup.md` as per-environment orchestration.

### 5. Verification → `project-setup.md`

Ask: *"How can we verify that setup worked? Is there a health check, test suite, or command that confirms things are
ready?"*

Final step *written into* `project-setup.md` — section 6 below runs next when service discovery is in scope; it produces
a different, unwritten output (see [Output](#output)).

### 6. Services (only when service discovery is in scope)

Skip this section entirely if the caller told you service discovery is out of scope for this run.

**Gather only — never write these facts into `project-setup.md`.** This section collects run-shaped information (start
commands, ports, container wiring), which is exactly the kind of content [What it is](#what-it-is) excludes from
`project-setup.md`. Everything gathered here stays in conversation context; see the unwritten fourth output in
[Output](#output).

Ask: *"What services need to run for the application to work? List them by name."*

For each service, ask **one** question at a time: *"Is `<name>` a workspace-level service (shared once across all
feature envs — e.g. a database or message broker) or a feature-environment service (runs per-env — e.g. an API server or
frontend)?"*

Then, for each service, resolve as much of its wiring as the project already reveals — don't interrogate the user field
by field for what's discoverable. [`service-discovery.md`](./service-discovery.md) defines exactly which fields to
gather (`start_command`, `port`, `container`) and where to look for each; follow that schema rather than improvising
your own field list. Only ask the user directly about a service's wiring when nothing in the project reveals it.

Once all services are captured, ask: *"Add another service, or move on?"* Loop until the user moves on.

#### Confirm the service list

Present the full list — name, scope, and whatever wiring was resolved — and ask **one** question: *"Here's what I found
for your services: `<name>` (`<scope>`) — `<cmd/port/image summary>`, ... Does this look right, or anything to add or
change?"* Wait for confirmation.

### Output

Two or three artifacts:

1. **`.winter/config.toml`** — enriched with trust/bootstrap `cmd` entries, `[[provision.*]]` handlers (commands inlined
   in `apply`, per section 1) for dependency/resource/data steps that fit the handler model, and plain-pattern
   `git_excludes`. Keep it boring; if in doubt, leave it out.
2. **`workspace:/context/project/project-setup.md`** — numbered steps for setup not covered by config or handlers:
   conditional installs, env file generation with port offsets, database creation/migration, seed data, and verification
   steps not yet migrated to handlers. Use variables like `<letter>` and `<index>` where environment-specific values are
   needed, and explain how to derive them. Stay within the setup-only scope from [What it is](#what-it-is) — no "how to
   run" content, and defer command-expressible steps to handlers. Author it as prose with **no manual line wrapping** —
   one sentence or paragraph per physical line, never reflowed to a fixed column — so later one-word edits don't reflow
   the whole paragraph and bury the real change in churn.
3. *(last resort)* **Handler scripts** under `.winter/scripts/` — only for `[[provision.*]]` steps too multi-step or
   conditional to inline in `apply`. Inline simple commands directly (section 1); a script file is the last resort, not
   the default.

When service discovery was in scope, the discovered service list and wiring findings are a **fourth, unwritten output**,
in the schema defined by [`service-discovery.md`](./service-discovery.md) — they are not persisted into
`project-setup.md` or `.winter/config.toml` (this guide's scope explicitly excludes "how to run" content). They're
carried forward in conversation context to the `ws-setup` service-orchestration step, which uses them directly instead
of researching again.

This guide stops at writing the artifacts. Applying the changes to existing environments and running the setup against
an environment is the caller's responsibility (see the ws-setup skill).
