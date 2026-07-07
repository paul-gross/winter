# ❄️ Winter

Winter is a reusable workspace framework for AI-assisted development on both local and cloud-based environments. Run many agents in parallel across isolated, fully-running feature environments — spanning many repos at once, with zero collisions.

📚 **Documentation:** <https://paul-gross.github.io/winter-docs/>

https://github.com/user-attachments/assets/79c3b9a7-c220-4e03-89a0-20539789d2b6

## ✨ Features

- **Polyrepo multi-worktree management** — Multiple project repos managed as one workspace. Each feature environment contains a coordinated set of git worktrees across every repo, all on the same branch.
- **Local ephemeral environments** — Each feature environment gets an isolated runtime: its own services, ports, databases, and dependencies. Spin one up, hand it off between humans and agents, tear it down when you're done.
- **Cross-repository agentic development** — Make sweeping changes across many repositories with feature-environment-level git operations. Treat the worktree as the unit of work — branches, commits, and service state stay aligned across every repo.
- **Service orchestration built for agents** — One simple, uniform interface to manage services at either scope: workspace-level shared singletons or per-feature-environment services within those local ephemeral environments — via docker, tmux sessions, or BYO orchestration.
- **Unified multi-repo CLI** — One `winter` command drives init, fetch, pull, push, status, and diff across every repo in the workspace at once — no more looping the same git operation through N checkouts by hand.
- **Resource provisioning** — Provisioning hooks set up and tear down resources across three layers — dependencies, workspace-scoped resources, and feature-environment-scoped resources — as environments come and go.
- **Separation of App, Harness, Workspace, and Workflow** — Four strictly separated components: your application code, the harness (agent context), the workspace, and the agentic workflow that orchestrates the work. Each evolves independently. Let each developer bring their own workflow, or use specific workflows for cloud agent work.
- **Cloud agent ready** — Supports cloud-based agent workflows through deterministic git operations, dependency installation, service orchestration, and resource provisioning. Have your cloud agents run a full local ephemeral environment for E2E testing.
- **Pluggable capability interfaces** — Core winter capabilities are swappable slots, not hardwired. Pick the service orchestrator, forge, and other providers that fit your stack, or implement your own against the interface.
- **Winter extensions** — Drop an extension into the workspace to surface its `index.md` context into the workspace's `AGENTS.md`/`CLAUDE.md`, contribute additional services and provisioned resources, and implement core winter capability interfaces.
- **Shared, versioned workspace** — The workspace is itself a git repo. Share it across the team. Clone it and the entire setup — agents, skills, services, planning conventions — comes with it. Use multiple varied workspaces for different perspectives of the same set of applications.
- **Workspace visualization** — A TUI dashboard shows a matrix of statuses across dozens of repositories at a glance. Decorator plugins let you surface your own data and add custom actions alongside it. Bring your own one-click diff viewer to optimize your review flow.

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

`/ws-setup` is an interactive walkthrough that connects your project repositories to the workspace: declaring and cloning them into `.winter/config.toml`, setting git identity, capturing per-repo setup commands and provisioning requirements (`project-setup.md`), authoring any installed extension's setup (e.g. service manifests for `winter-service-tmux`), creating your first feature environment, and recording delivery conventions (`contributing.md`). Idempotent — re-run it any time to reconfigure.

As part of setup, `/ws-setup` re-points the remotes for you: the original origin becomes `winter` (your upstream for framework updates) and `origin` connects to your own repository. Later, `/ws-update` brings subsequent framework updates down from `winter` (see [Forking](#-forking)).

## 🧩 How it works

Winter is two pieces of machinery: a directory convention and a CLI that maintains it.

**Directory convention.** Each declared project repo is cloned into `projects/<name>/` (the source-of-truth checkout, always on its main branch). Feature environments live in their own top-level directories — configurable shorthands like `alpha/`, `beta/`, ... or arbitrary names like `feature-xyz/` or `jira-123-feature/` — each containing a per-repo git worktree on a branch matching the directory name. Extensions are cloned within the workspace (wherever you like) and discovered automatically. Nothing in your application repos changes: the workspace is the only thing that knows about winter.

**`winter ws init <name>`** is the single entry point: it creates the feature environment — worktrees, git identity, stable port allocation — and reconciles every installed extension's capabilities through their lifecycle hooks. Idempotent — safe to re-run. With no name, it bootstraps the workspace itself rather than a feature environment. Environment variables (the winter base vars like `WINTER_ENV` and `WINTER_PORT_BASE`, plus your `[env.workspace.vars]` / `[env.feature.vars]` entries) are computed at runtime and sourced with `source <(winter env <name>)` or injected automatically by `winter service up`.

**Configurable port allocation.** Each environment gets a configurable port window keyed off its index (`base_port + index * ports_per_env`; defaults: `base_port=4000`, `ports_per_env=20`). Preconfigured shorthands (like alpha, beta, …) have fixed indices (alpha=1, beta=2, …) so alpha starts at 4020, beta at 4040, and so on. Multiple environments can run their services simultaneously without colliding.

**Extensions** are independent repos that drop in skills, agents, and winter process hooks. They install themselves on `winter ws init` — each one is cloned into the workspace and `@`-mentioned in `AGENTS.md`/`CLAUDE.md`, so its context loads automatically. This is how multi-repo agent configuration stays organized: rather than scattering skills and agents across every project repo (where they'd be duplicated, diverge, and pollute the application code), the workspace pulls them all into a single place. Extensions are cross-harness — the same skills work across Claude Code, Codex, and OpenCode, reducing the vendor lock-in of per-tool skill marketplaces. Your agent then operates across every project worktree with the full set available at once — one context, one toolkit, every repo.

## 🌲 Winter Ecosystem

Winter is extensible by design. The framework, the consumable extensions that add capability, and the reference implementations you can study and adapt each ship as their own repos, and compose together via `winter ws init`.

**The framework**

- **[winter](https://github.com/paul-gross/winter)** — the framework itself: Python CLI, workspace skills, conventions; **fork this to start your own**

**Consumable extensions** — generic capabilities a workspace installs and uses as-is:

- **[winter-service-tmux](https://github.com/paul-gross/winter-service-tmux)** — implements the winter service orchestration capability via workspace-level or project-level tmux sessions that manage the services, giving humans and agents alike a view of the running applications
- **[winter-service-docker](https://github.com/paul-gross/winter-service-docker)** — docker compose-based service orchestration with per-env isolation and real container healthchecks
- **[winter-product](https://github.com/paul-gross/winter-product)** — a basic git-backed product backlog for refining ideas, product plans, technical plans, and phase documents; forkable, so you can keep your own product history
- **[winter-github](https://github.com/paul-gross/winter-github)** — product planning and an agentic feedback mechanism using GitHub's issue tracking
- **[winter-codeberg](https://github.com/paul-gross/winter-codeberg)** — product planning and an agentic feedback mechanism using Codeberg's issue tracking

**Examples** — the maintainer's own opinionated sidecars:

- **[winter-workflow](https://github.com/paul-gross/winter-workflow)** — a suite of agentic workflows tuned to different kinds of work (one large feature vs. many small ones), with subagent feedback loops that let you build human-on-the-loop (HOTL) rather than human-in-the-loop (HITL); adopt it or fork your own
- **[winter-harness](https://github.com/paul-gross/winter-harness)** — the agentic harness used to develop winter itself, an example of how harness and application separation can work; usable as-is, or a template to fork for your own
- **[winter-workspace](https://github.com/paul-gross/winter-workspace)** — the meta-workspace winter itself is built with; an example of a real, configured workspace that demonstrates agentic development (see [Contributing](#contributing))

**Related** — not a winter extension, but built around winter:

- **[winter-nvim](https://github.com/paul-gross/winter-nvim)** — a Neovim plugin that drives a winter workspace from inside the editor, adding an in-Neovim TUI and a workspace repo picker to swap sessions across all your repos in a keystroke; it consumes winter rather than extending it

## 🌿 Forking

We recommend you fork [`paul-gross/winter`](https://github.com/paul-gross/winter) and customize it for your application. `/ws-setup` and the winter CLI handle the remote configuration for you — `winter` becomes the upstream you pull framework updates from, and `origin` points to your fork. Your customizations (project-specific agents, skills, workflow scripts, integration config) live in your fork.

Winter isn't embedded into your application repos — a winter fork *is* the integration of winter and your application. The framework lives upstream; the customizations you build on top of it live in your fork.

To take framework updates after the initial fork, run `/ws-update`: it fetches the `winter` remote and integrates the upstream branch into your workspace branch by rebase or merge — detecting which your workspace uses, or asking.

## ⌨️ Winter CLI

The workspace includes a CLI (for agent use) and a TUI dashboard (for human use) that together expose the full feature-environment surface across all project repos at once:

- **Feature environments & worktrees** — create, inspect, sync (`fetch`/`pull`/`push`/`merge`), connect, check out, and tear down feature environments and their per-repo worktrees
- **Runtime environment** — print a scope's `WINTER_*` and env-band variables as shell-sourceable `export` lines (`winter env <scope>`)
- **Repositories** — add, remove, and list the repos the workspace tracks
- **Service orchestration** — start, stop, restart, inspect, and tail the logs of a feature env's services
- **Resource provisioning** — bring a fresh environment to a working state: install dependencies, create resources, load seed data
- **Status & health** — feature-environment status, `doctor` config-health probes, and `lint` checks against documented conventions
- **Introspection** — the module dependency `graph` and the `capabilities` map of which extension fills each slot
- **Extensions** — verify an extension against a capability spec, or scaffold a new one

```bash
# Install (one-time) — copies a thin wrapper to ~/.local/bin that
# auto-discovers the workspace root and runs the CLI from
# tools/winter-cli/ within that workspace, so any customizations
# you've made to your fork are picked up automatically.
./tools/winter-cli/install.sh

winter dashboard
```

Requires `mise` (dependencies are managed automatically). See `context/winter-cli/index.md` for the full command reference, including [configurable dashboard keybindings](context/winter-cli/usage/dashboard.md#keybindings) (remap any action, with Neovim-style chord sequences).

## 🧭 Principles

The core tenets and philosophy behind winter — what it really is and why it exists. See [PRINCIPLES.md](./PRINCIPLES.md) for the full rationale.

- **Remove the single-agent-flow bottleneck**
  - **Support local ephemeral environments**
- **Separation of application, harness, workspace, and workflow**
- **The workspace is a git repo**
- **Coordinate agentic work across many repositories**
- **Pluggable, choose your tools or bring your own**
- **Read-only views for humans, tools for agents**
- **Local agentic development over distributed agentic development**

## 💭 Why the name, Winter?

The name gives you an unambiguous way to reference and speak about the workspace itself with LLMs. Talking about a workspace, repository, or worktree leads an LLM straight to native git concepts — generic terminology with generic associations. Talking about a **winter** workspace, a **winter** repository, or **winter** in general directs the model immediately to the associated context within the conversation: this framework, its conventions, its tooling.

## Contributing

Winter is not currently accepting outside contributions. If you'd like to talk about becoming a contributor, get in touch via [LinkedIn](https://www.linkedin.com/in/pjgross) or email at [paul@grosscode.net](mailto:paul@grosscode.net).

That said, the core winter foundation being closed to PRs doesn't stop you from building on it — use the [winter-workspace](https://github.com/paul-gross/winter-workspace) repo as your base to develop your own extensions and share them.

## License

MIT
