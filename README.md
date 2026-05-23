# ❄️ Winter

Winter is a reusable workspace framework for AI-assisted development with Claude Code.

## ✨ Features

- **Polyrepo multi-worktree management** — Multiple project repos managed as one workspace. Each worktree contains a coordinated set of git worktrees across every repo, all on the same branch.
- **Local ephemeral environment service orchestration** — Each worktree gets an isolated runtime: its own services, ports, databases, dependencies. Spin one up, hand it off between humans and agents, tear it down when you're done.
- **Cross-repository agentic development** — A single change spans frontend, BFF, API, shared libraries. Agents (and humans) treat the worktree as the unit of work — branches, commits, and service state stay aligned across every repo.
- **Multi-agent parallelism** — Multiple Claude instances run at once, each in their own environment. No collisions.
- **Separation of App, Harness, and Workflow** — Three strictly separated layers: your application code, the AI harness (agents, skills, instructions), and the workflow scripts that orchestrate them. Each evolves independently. Your app codebases carry zero harness or workflow machinery.
- **Shared, versioned workspace** — The workspace is itself a git repo. Share it across the team. Clone it and the entire setup — agents, skills, services, planning conventions — comes with it.
- **Workspace visualization** — A TUI dashboard shows a matrix of statuses across dozens of repositories at a glance, with optional extensions for GitLab MR or GitHub PR information.

## 🚀 Quick Start

Clone it, run `/ws-setup`, and start building.

```bash
# Clone winter with any name you prefer
git clone <winter-repo-url> my-workspace
cd my-workspace

# Run workspace setup (in Claude Code)
/ws-setup
```

`/ws-setup` walks you through configuring remotes, cloning project repos, resolving branches, creating worktrees, and setting up the winter config (`.winter/config.toml`) and integration config (project-setup.md, workflow.sh, contributing.md).

After cloning, `/ws-setup` re-points the remotes for you: the original origin becomes `winter` (your upstream for framework updates) and `origin` connects to your own repository.

## 🧩 How it works

Winter is two pieces of machinery: a directory convention and a CLI that maintains it.

**Directory convention.** Each declared project repo is cloned into `projects/<name>/` (the source-of-truth checkout, always on its main branch). Feature environments live in their own top-level directories — Greek-letter shorthands like `alpha/`, `beta/`, ... or arbitrary names like `feature-xyz/` or `jira-123-thing/` — each containing a per-repo git worktree on a branch matching the directory name. Extensions are cloned at the workspace root and discovered automatically. Nothing in your project repos changes: the workspace is the only thing that knows about winter.

**`winter ws init <name>`** is the single entry point: it creates the directory, runs `git worktree add` for every project repo (cut from each repo's main branch), copies your git identity, writes git-exclude entries, runs each repo's `cmd` list, seeds `.winter.env` with `WINTER_ENV` / `WINTER_ENV_INDEX` / `WINTER_PORT_BASE`, and fires every installed extension's `on_env_init` hook. Idempotent — safe to re-run.

**Ports per environment.** Each environment gets a 100-port window keyed off its index. Greek letters have fixed indices (alpha=1, beta=2, …) so alpha lives in the 4100s, beta in the 4200s, and so on. Multiple environments can run their services simultaneously without colliding.

**Extensions** are independent repos that drop in skills, agents, and winter process hooks. They install themselves on `winter ws init` — each one is cloned at the workspace root, registered with the CLI, and auto-imported into `CLAUDE.md`. This is how multi-repo agent configuration stays organized: rather than scattering skills and agents across every project repo (where they'd be duplicated, diverge, and pollute the application code), the workspace pulls them all into a single place. Claude Code then operates across every project worktree with the full set of skills and agents available at once — one context, one toolkit, every repo.

See [ai/workspace-layout.md](./ai/workspace-layout.md) for the full directory map and worktree topology.

## 🌲 Winter Ecosystem

Winter is extensible by design — the framework, the meta workspace it's developed in, and its extensions all ship as separate repos that compose via `winter ws init`:

- **[winter](https://codeberg.org/pgross/winter)** — the framework itself: Python CLI, workspace skills, conventions
- **[winter-workspace](https://codeberg.org/pgross/winter-workspace)** — the meta workspace where winter is developed; fork this to start your own
- **[winter-service-tmux](https://codeberg.org/pgross/winter-service-tmux)** — extension: tmux-based service orchestration so agents can launch and monitor application suites
- **[winter-product](https://codeberg.org/pgross/winter-product)** — extension: planning agents and the `todo` skill
- **[winter-workflow](https://codeberg.org/pgross/winter-workflow)** — extension: the author's personal agentic workflow, interchangeable with your own
- **[winter-codeberg](https://codeberg.org/pgross/winter-codeberg)** — extension: AI-native Codeberg issue format and the `/wc-issue` skill

## 🌿 Forking

We recommend you fork the winter workspace and customize it for your application. `/ws-setup` and the winter CLI handle the remote configuration for you — `winter` becomes the upstream you pull framework updates from, and `origin` points to your fork. Your customizations (project-specific agents, skills, workflow scripts, integration config) live in your fork.

Winter is meant to be integrated into your projects, not adopted wholesale. The framework lives upstream; the workflow you build on top of it lives in your fork.

## ⌨️ Winter CLI

The workspace includes a CLI (for agent use) and a TUI dashboard (for human use) for managing worktrees and repositories across all project repos at once.

```bash
# Install (one-time) — copies a thin wrapper to ~/.local/bin that
# auto-discovers the workspace root and runs the CLI from
# tools/winter-cli/ within that workspace, so any customizations
# you've made to your fork are picked up automatically.
./tools/winter-cli/install.sh

winter dashboard
```

Requires `mise` (dependencies are managed automatically). See `ai/winter-cli/usage.md` for the full command reference.

## 🧭 Principles

The design decisions behind winter and why it exists.

### 1. Remove the single-agent-flow bottleneck

Agentic development scales horizontally by adding more parallel agents. We enable parallel workstreams for complex applications through a workspace. We aim to achieve a single agentic interface to manage teams of agents working across multiple feature environments. The end goal: a single agent interface commanding many agents working in many feature environments across many repositories.

### 2. Separation of application, agentic development, and harness engineering

Application code should be about the product. Workflow, workspace, service orchestration, and agentic harnesses are different concerns and belong in their own areas, with a thin integration surface between them. We believe tools that require you to embed their conventions in your application don't leave space for innovation and change.

- **Winter** — an extendable workspace platform for agentic development, composed dynamically from modular extensions
- **Integration** — a sharable winter workflow interface for a specific project or application
- **Application** — the code being built

### 3. The workspace is a git repo

The workspace for a complex application should be shareable and versioned. Treat your workspace like cattle rather than a pet.

### 4. Plug-and-play

There's no canonical agentic harness. Every team and every developer has opinions about how their agents plan, commit, and reason — and those opinions evolve fast. We believe the workspace should be a stable integration surface, and the harness and [workflow](https://codeberg.org/pgross/winter-workflow) should be swappable components chosen for the project at hand.

### 5. Support local ephemeral environments shared between humans and agents

Shared development resources breed contention. Staging servers, single dev databases, and singleton local environments force humans and agents to take turns or step on each other. We believe each in-flight feature should be able to spin up its own runtime, hand it between humans and agents freely, and tear it down without residue. See [`winter-service-tmux`](https://codeberg.org/pgross/winter-service-tmux).

### 6. Coordinate agentic work across repositories

A polyrepo split is an implementation choice, not a unit of work. The natural unit is the feature — and features cross repos. We believe the workspace should let an agent (or a human) reason about a change as one coherent thing instead of N disconnected ones.

### 7. Readonly views for humans, tools for agents

Enable agentic flows for development while maximizing observability for the human.

### 8. Local agentic development over distributed agentic development

There is a lot of speed to be had working locally before we hit the bottleneck that requires distributed agentic development within cloud services. There is significant overhead and new issues to solve when moving to automated cloud agents. We believe empowering the agentic development process locally has undeniable efficiencies.

## 💭 Why the name, Winter?

The name gives you an unambiguous way to reference and speak about the workspace itself with LLMs. Talking about a workspace, repository, or worktree leads an LLM straight to native git concepts — generic terminology with generic associations. Talking about a **winter** workspace, a **winter** repository, or **winter** in general directs the model immediately to the associated context within the conversation: this framework, its conventions, its tooling.

## License

MIT
