# ❄️ Winter

Winter is a reusable workspace framework for AI-assisted development on both local and cloud-based environments. Run
many agents in parallel across isolated, fully-running feature environments — spanning many repos at once, with zero
collisions.

📚 **Documentation:** <https://paul-gross.github.io/winter-docs/>

<https://github.com/user-attachments/assets/79c3b9a7-c220-4e03-89a0-20539789d2b6>

## ✨ Features

The things a multi-repo workspace usually makes you do by hand — run the same git command in ten checkouts, wire up a
new environment's ports and databases, start its services — are one winter command each. They're safe to re-run, and
they print JSON. An agent does far better with one command that either works or says why it didn't than with twenty that
can each go wrong.

- **[Polyrepo git & worktrees](#-polyrepo-git--worktrees)** — many repos driven as one workspace, with git operations
  that act on a whole feature environment at once.
- **[Feature environments](#-feature-environments)** — isolated, disposable local runtimes so many agents work in
  parallel with zero collisions.
- **[Service orchestration](#-service-orchestration)** — one uniform interface to run each environment's services, via
  docker, tmux, or your own provider.
- **[Resource provisioning](#-resource-provisioning)** — a re-runnable dependency → resource → data lifecycle that
  brings an environment to a working state.
- **[Agent harness](#-agent-harness)** — bring your own harness. The same skills, agents, and context project into
  Claude Code, Codex, and OpenCode, and none of them live in your application repos.
- **[Extensibility](#-extensibility)** — extensions, pluggable capability slots, lifecycle hooks, and TUI plugins.
- **[Diagnostics & conventions](#-diagnostics--conventions)** — a workspace that checks itself, so drift and broken
  conventions surface as a failed probe rather than as a confusing agent failure an hour later.
- **[Interfaces](#-interfaces)** — a CLI for agents, a TUI for humans, JSON everywhere, and editor integration.
- **[Guided setup & updates](#-guided-setup--updates)** — agentic onboarding that configures the workspace, discovers
  your services, and teaches you winter.

### 🌲 Polyrepo git & worktrees

- **One command across every repo** — `fetch`, `pull`, `push`, `merge`, `status`, `diff`, `reset`, and `clean` run over
  every repo in a feature environment at once, no more looping the same git operation through N checkouts by hand
  ([polyrepo git operations](https://paul-gross.github.io/winter-docs/operations/polyrepo-git/)).
- **Coordinated worktrees** — each feature environment holds one git worktree per project repo, all on the branch named
  after the environment; `connect`, `disconnect`, and `checkout` move the whole set together, all-or-nothing.
- **Segment-aware targeting** — every multi-repo command takes `<env>/<repo>` glob patterns, so one invocation can hit
  one repo, one environment, or every environment at once ([patterns](context/winter-cli/usage/ws/patterns.md)).
- **Repository registry** — declare which repos the workspace tracks, and pin the ones that shouldn't follow your
  feature branches to a fixed branch, tag, or commit ([repositories](context/winter-cli/configuration/repositories.md)).
- **Safe cleanup** — `winter ws prune` removes disk state for repos no longer in config, refusing anything with
  uncommitted changes or attached worktrees ([prune](context/winter-cli/usage/ws/prune.md)).

### 🧊 Feature environments

- **Local ephemeral environments** — each one gets an isolated runtime: its own services, ports, databases, and
  dependencies. Spin one up, hand it off between humans and agents, tear it down when you're done
  ([feature environments](https://paul-gross.github.io/winter-docs/operations/feature-environments/)).
- **A defined lifecycle, one command per phase** — `winter ws init` (structural) → `winter provision` (readiness) →
  `winter service up` (run) → `winter ws destroy` (teardown)
  ([environment lifecycle](context/environment-lifecycle.md)).
- **Configurable port allocation** — each environment gets a private port window keyed off its index
  (`base_port + index * ports_per_env`). Shorthand names hold fixed indices; arbitrary names hash into the remaining
  band, so nothing collides ([ports & environments](context/winter-cli/configuration/ports-and-environments.md)).
- **Runtime environment variables** — services come up already knowing which environment they're in and which ports and
  hosts they own, and one `source` line gets your own shell the same values ([env](context/winter-cli/usage/env.md)).
- **Any name you like** — configurable shorthands (`alpha`, `beta`, …) or arbitrary names like `jira-123-feature`.

### 🔧 Service orchestration

- **One uniform interface** — `winter service up` / `down` / `status` / `restart` / `logs`, addressed with the same
  `<env>/<service>` glob grammar as everything else
  ([running services](https://paul-gross.github.io/winter-docs/operations/services/)).
- **Two scopes** — workspace-level shared singletons and per-feature-environment services, in one command surface.
- **Bring your own orchestrator** — tmux sessions, docker compose, or your own provider behind the same interface;
  consumers depend on `winter service …` and never on the implementation
  ([service orchestrator contract](context/winter-cli/contracts/service-orchestrator.md)).
- **Multiple providers at once** — bind more than one provider (e.g. tmux *and* docker) and winter dispatches to each.
- **Aggregated service manifest** — the workspace config and every installed extension contribute `[[service]]` entries,
  merged into a single manifest handed to the provider.
- **Health waits and log streaming** — `up --wait`/`--timeout` blocks until nothing reports unhealthy; `logs` supports
  `-f`, `--since`, `-n`, and cross-environment aggregation.

### 📦 Resource provisioning

- **Three ordered stages** — `dependency` → `resource` → `data`: install dependencies, create databases and queues and
  buckets, then load seed data ([provisioning](https://paul-gross.github.io/winter-docs/operations/provisioning/)).
- **Three scopes** — handlers run at workspace, feature-environment, or feature-worktree scope, so shared resources and
  per-environment resources are provisioned by the same mechanism.
- **Re-runnable and reversible** — `--reset`, `--destroy`, and `--seed` per stage, `--name` to target a single declared
  entry, and automatic teardown when an environment is destroyed.
- **Contributed by extensions too** — an extension declares `[[provision.*]]` handlers in its manifest alongside the
  workspace's own ([provision handlers](context/winter-cli/configuration/provision.md)).
- **Gated and previewable** — `required_services` holds a handler until its service is up; `--dry-run` prints the plan
  without running anything, and `--json` emits it as a stream.

### 🤖 Agent harness

- **Separation of app, harness, workspace, and workflow** — four strictly separated components: your application code,
  the harness (agent context), the workspace, and the agentic workflow that orchestrates the work. Each evolves
  independently. Let each developer bring their own workflow, or use specific workflows for cloud agent work.
- **Cross-harness skills and agents** — the same skills and agents are projected into Claude Code, Codex, and OpenCode
  on every `winter ws init`, reducing the vendor lock-in of per-tool skill marketplaces
  ([extensions](context/winter-cli/configuration/extensions.md#what-gets-symlinked)).
- **Model and effort retargeting** — `[agent_model_overrides]` and `[model_tiers]` point an installed agent at a
  different model or reasoning effort workspace-wide, without editing its committed source
  ([agent configuration](context/winter-cli/configuration/agents.md)).
- **Budgeted context delivery** — each extension's context is surfaced into `AGENTS.md`/`CLAUDE.md` either *eagerly* (an
  `@`-import every session pays for) or *lazily* (one routing line the agent follows only when relevant)
  ([context delivery](context/winter-cli/configuration/extensions.md#context-delivery)).
- **A place for generated artifacts** — `winter space <kind>` resolves where winter and its extensions write scores,
  manifests, workflow documents, and logs, so a skill never hardcodes one harness's home directory
  ([space](context/winter-cli/usage/space.md)).
- **Cloud agent ready** — deterministic git operations, dependency installation, service orchestration, and resource
  provisioning let your cloud agents run a full local ephemeral environment for E2E testing.

### 🔌 Extensibility

- **Winter extensions** — drop a repo carrying a `winter-ext.toml` into the workspace and it contributes skills, agents,
  context, services, provision handlers, doctor probes, and lint checks — discovered and installed automatically
  ([extensions](https://paul-gross.github.io/winter-docs/extensions/)).
- **Pluggable capability interfaces** — core capabilities are swappable slots, not hardwired. Bind a provider
  explicitly, or let a sole provider bind implicitly ([capabilities](context/winter-cli/configuration/capabilities.md)).
- **Lifecycle hooks** — `on_env_init`, `on_env_destroy`, and `on_workspace_reconcile` fire at defined points with a
  documented environment-variable contract ([hooks](context/winter-cli/configuration/extensions.md#extension-hooks)).
- **Scaffold and verify** — `winter ext new` scaffolds an extension that conforms out of the box; `winter ext verify`
  checks an existing one against the bundled capability spec ([ext](context/winter-cli/usage/ext.md)).
- **TUI plugins** — a second, distinct extension point that decorates the *running tool*: status badges, full TUI
  screens, keybound actions, and new `winter` subcommands
  ([TUI plugins](https://paul-gross.github.io/winter-docs/tui-plugins/)).

### 🩺 Diagnostics & conventions

- **`winter doctor`** — preflight probes over workspace *materialization*: config parses, every declared repo and
  worktree exists on the right branch, port invariants hold, the registry hasn't drifted, and skills and agents are
  projected into every harness. Extensions contribute their own probes ([doctor](context/winter-cli/usage/doctor.md)).
- **`winter lint`** — convention checks over workspace *content*: path notation, agent frontmatter, module boundaries. A
  dispatcher, not a checker — core checks plus whatever each extension contributes, scoped to a repo, an environment, or
  just your `--changed` files, exiting non-zero for CI and pre-push ([lint](context/winter-cli/usage/lint.md)).
- **Targeted suppression** — `[lint.ignore]` silences a check on paths a given workspace or extension cannot fix,
  without going quiet everywhere else.
- **`winter graph`** — the module dependency graph as a human listing or a JSON adjacency map, consumable by lint checks
  and tooling ([graph](context/winter-cli/usage/graph.md)).
- **`winter capabilities`** — which extension fills each capability slot and how it got bound
  ([capabilities](context/winter-cli/usage/capabilities.md)).

### 💻 Interfaces

- **Unified multi-repo CLI** — one `winter` command drives the entire surface. A thin installed wrapper auto-discovers
  the workspace root and runs the CLI from within it, so your fork's customizations are picked up automatically
  ([CLI reference](https://paul-gross.github.io/winter-docs/cli-reference/)).
- **Workspace visualization** — a TUI dashboard shows a matrix of statuses across dozens of repositories at a glance, in
  four layouts that auto-resolve to the workspace's shape, with live config reload, drill-down detail screens, and a log
  tab that captures failures so you can inspect one without re-running the command
  ([dashboard](context/winter-cli/usage/dashboard.md)).
- **Remappable keybindings** — every dashboard action has a stable id you can rebind, including Neovim-style leader and
  chord sequences ([keybindings](context/winter-cli/usage/dashboard.md#keybindings)).
- **Machine-readable everywhere** — `--json` / NDJSON output on `status`, `doctor`, `lint`, `provision`, `service`,
  `graph`, `capabilities`, and more: the stable contract agents and scripts consume instead of parsing tables.
- **Editor integration** — `winter ws worktrees` feeds editor pickers, and
  [winter-nvim](https://github.com/paul-gross/winter-nvim) drives the whole workspace from inside Neovim.

### 🎓 Guided setup & updates

- **`/ws-setup` — agentic onboarding** — an interactive walkthrough that stands the workspace up *and* teaches you
  winter as it goes: declaring and cloning your project repos, setting git identity, **researching your applications to
  discover their services, dependencies, ports, databases, and seed data**, authoring the service manifest for your
  chosen orchestrator, creating your first feature environment, and recording your delivery conventions. Idempotent —
  re-run it any time to reconfigure.
- **`/ws-init` — the non-interactive counterpart** — applies declared config to the workspace, an environment, or a
  single repo. It never *changes* config, so it is the safe "I just cloned this, make it work" path.
- **`/ws-update` — framework updates** — fetches the `winter` remote and integrates upstream framework changes into your
  workspace branch by rebase or merge, detecting which your workspace uses (see [Forking](#-forking)).
- **Workspace skills** — `/ws`, `/ws-fetch`, `/ws-pull`, and `/ws-push` ship with the workspace and are projected into
  every supported code harness, alongside skills you author yourself.
- **Shared, versioned workspace** — the workspace is itself a git repo. Share it across the team. Clone it and the
  entire setup — agents, skills, services, planning conventions — comes with it. Use multiple varied workspaces for
  different perspectives of the same set of applications.

## 🚀 Quick Start

Clone it, install and bootstrap the CLI, then run `/ws-setup` and start building.

```bash
# Clone winter with any name you prefer
git clone https://github.com/paul-gross/winter.git my-workspace
cd my-workspace

# Install the CLI (one-time)
./tools/winter-cli/install.sh

# Link code-harness agnostic skills
winter init

# Run workspace setup (in your code harness)
/ws-setup
```

`/ws-setup` is the interactive onboarding walkthrough described in [Guided setup & updates](#-guided-setup--updates)
above — it connects your project repositories to the workspace and leaves you with a running feature environment.
Idempotent: re-run it any time to reconfigure.

As part of setup, `/ws-setup` re-points the remotes for you: the original origin becomes `winter` (your upstream for
framework updates) and `origin` connects to your own repository. Later, `/ws-update` brings subsequent framework updates
down from `winter` (see [Forking](#-forking)).

## 🧩 How it works

Winter is two pieces of machinery: a directory convention and a CLI that maintains it.

**Directory convention.** Each declared project repo is cloned into `projects/<name>/` (the source-of-truth checkout,
always on its main branch). Feature environments live in their own top-level directories — configurable shorthands like
`alpha/`, `beta/`, ... or arbitrary names like `feature-xyz/` or `jira-123-feature/` — each containing a per-repo git
worktree on a branch matching the directory name. Extensions are cloned within the workspace (wherever you like) and
discovered automatically. Nothing in your application repos changes: the workspace is the only thing that knows about
winter.

**`winter ws init <name>`** is the single entry point: it creates the feature environment — worktrees, git identity,
stable port allocation — and reconciles every installed extension's capabilities through their lifecycle hooks.
Idempotent — safe to re-run. With no name, it bootstraps the workspace itself rather than a feature environment.
Environment variables (the winter base vars like `WINTER_ENV` and `WINTER_PORT_BASE`, plus your `[env.*]` band entries)
are computed at runtime and sourced with `source <(winter env <name>)` or injected automatically by `winter service up`.

**Configurable port allocation.** Each environment gets a configurable port window keyed off its index
(`base_port + index * ports_per_env`; defaults: `base_port=4000`, `ports_per_env=20`). Preconfigured shorthands (like
alpha, beta, …) have fixed indices (alpha=1, beta=2, …) so alpha starts at 4020, beta at 4040, and so on. Multiple
environments can run their services simultaneously without colliding.

**Extensions** are independent repos that drop in skills, agents, and winter process hooks. They install themselves on
`winter ws init` — each one is cloned into the workspace and `@`-mentioned in `AGENTS.md`/`CLAUDE.md`, so its context
loads automatically. This is how multi-repo agent configuration stays organized: rather than scattering skills and
agents across every project repo (where they'd be duplicated, diverge, and pollute the application code), the workspace
pulls them all into a single place. Extensions are cross-harness — the same skills work across Claude Code, Codex, and
OpenCode, reducing the vendor lock-in of per-tool skill marketplaces. Your agent then operates across every project
worktree with the full set available at once — one context, one toolkit, every repo.

## 🌲 Winter Ecosystem

Winter is extensible by design. The framework, the consumable extensions that add capability, and the reference
implementations you can study and adapt each ship as their own repos, and compose together via `winter ws init`.

**The framework**

- **[winter](https://github.com/paul-gross/winter)** — the framework itself: Python CLI, workspace skills, conventions;
  **fork this to start your own**

**Consumable extensions** — generic capabilities a workspace installs and uses as-is:

- **[winter-service-tmux](https://github.com/paul-gross/winter-service-tmux)** — implements the winter service
  orchestration capability via workspace-level or project-level tmux sessions that manage the services, giving humans
  and agents alike a view of the running applications
- **[winter-service-docker](https://github.com/paul-gross/winter-service-docker)** — docker compose-based service
  orchestration with per-env isolation and real container healthchecks
- **[winter-product](https://github.com/paul-gross/winter-product)** — a basic git-backed product backlog for refining
  ideas, product plans, technical plans, and phase documents; forkable, so you can keep your own product history
- **[winter-github](https://github.com/paul-gross/winter-github)** — product planning and an agentic feedback mechanism
  using GitHub's issue tracking
- **[winter-codeberg](https://github.com/paul-gross/winter-codeberg)** — product planning and an agentic feedback
  mechanism using Codeberg's issue tracking

**Examples** — the maintainer's own opinionated sidecars:

- **[winter-workflow](https://github.com/paul-gross/winter-workflow)** — a suite of agentic workflows tuned to different
  kinds of work (one large feature vs. many small ones), with subagent feedback loops that let you build
  human-on-the-loop (HOTL) rather than human-in-the-loop (HITL); adopt it or fork your own
- **[winter-context](https://github.com/paul-gross/winter-context)** — the agent-facing context used to develop winter
  itself, an example of how context and application separation can work; usable as-is, or a template to fork for your
  own
- **[winter-workspace](https://github.com/paul-gross/winter-workspace)** — the meta-workspace winter itself is built
  with; an example of a real, configured workspace that demonstrates agentic development (see
  [Contributing](#contributing))

**Related** — not a winter extension, but built around winter:

- **[winter-nvim](https://github.com/paul-gross/winter-nvim)** — a Neovim plugin that drives a winter workspace from
  inside the editor, adding an in-Neovim TUI and a workspace repo picker to swap sessions across all your repos in a
  keystroke; it consumes winter rather than extending it

## 🌿 Forking

We recommend you fork [`paul-gross/winter`](https://github.com/paul-gross/winter) and customize it for your application.
`/ws-setup` and the winter CLI handle the remote configuration for you — `winter` becomes the upstream you pull
framework updates from, and `origin` points to your fork. Your customizations (project-specific agents, skills, workflow
scripts, integration config) live in your fork.

Winter isn't embedded into your application repos — a winter fork *is* the integration of winter and your application.
The framework lives upstream; the customizations you build on top of it live in your fork.

To take framework updates after the initial fork, run `/ws-update`: it fetches the `winter` remote and integrates the
upstream branch into your workspace branch by rebase or merge — detecting which your workspace uses, or asking.

## ⌨️ Winter CLI

The workspace includes a CLI (for agent use) and a TUI dashboard (for human use) that together expose the full
feature-environment surface across all project repos at once — every capability in [Features](#-features) above is
reachable from one `winter` command.

```bash
# Install (one-time) — copies a thin wrapper to ~/.local/bin that
# auto-discovers the workspace root and runs the CLI from
# tools/winter-cli/ within that workspace, so any customizations
# you've made to your fork are picked up automatically.
./tools/winter-cli/install.sh

winter dashboard
```

Requires `mise` (dependencies are managed automatically). See `context/winter-cli/index.md` for the full command
reference, including [configurable dashboard keybindings](context/winter-cli/usage/dashboard.md#keybindings) (remap any
action, with Neovim-style chord sequences).

## 🧭 Principles

The core tenets and philosophy behind winter — what it really is and why it exists. See [PRINCIPLES.md](./PRINCIPLES.md)
for the full rationale.

- **Remove the single-agent-flow bottleneck**
  - **Support local ephemeral environments**
- **Separation of application, harness, workspace, and workflow**
- **The workspace is a git repo**
- **Coordinate agentic work across many repositories**
- **Pluggable, choose your tools or bring your own**
- **Read-only views for humans, tools for agents**
- **Local agentic development over distributed agentic development**

## 💭 Why the name, Winter?

The name gives you an unambiguous way to reference and speak about the workspace itself with LLMs. Talking about a
workspace, repository, or worktree leads an LLM straight to native git concepts — generic terminology with generic
associations. Talking about a **winter** workspace, a **winter** repository, or **winter** in general directs the model
immediately to the associated context within the conversation: this framework, its conventions, its tooling.

## Contributing

Contributions are welcome. For changes to the winter core, reach out first to become a collaborator — opening an issue
that introduces what you'd like to work on is perfect — so we can align on direction before you invest in a PR. Issues,
bug reports, and ideas are welcome from anyone, any time.

You also don't need anyone's go-ahead to build *on* winter — use the
[winter-workspace](https://github.com/paul-gross/winter-workspace) repo as your base to develop your own extensions and
share them.

## License

MIT
