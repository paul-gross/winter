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

- **[Polyrepo git & worktrees](https://paul-gross.github.io/winter-docs/operations/polyrepo-git/)** — `fetch`, `pull`,
  `push`, `merge`, `status`, and `diff` run across every repo in a feature environment at once, targeted with
  `<env>/<repo>` glob patterns.
- **[Feature environments](https://paul-gross.github.io/winter-docs/operations/feature-environments/)** — isolated,
  disposable local runtimes, each with its own worktrees, ports, databases, and services, so many agents work in
  parallel with zero collisions.
- **[Service orchestration](https://paul-gross.github.io/winter-docs/operations/services/)** — one uniform
  `winter service up` / `down` / `status` / `logs` interface over docker, tmux, or your own provider, at workspace or
  feature-environment scope.
- **[Resource provisioning](https://paul-gross.github.io/winter-docs/operations/provisioning/)** — a re-runnable
  `dependency` → `resource` → `data` lifecycle that brings an environment to a working state, and tears it back down.
- **[Agent harness](https://paul-gross.github.io/winter-docs/getting-started/#what-is-harness-engineering)** — bring
  your own harness. The same skills, agents, and context project into Claude Code, Codex, and OpenCode, and none of them
  live in your application repos; `[agent_model_overrides]` and `[model_tiers]` retarget an installed agent's model or
  reasoning effort workspace-wide, without editing its committed source.
- **[Extensibility](https://paul-gross.github.io/winter-docs/extensions/)** — drop in a repo carrying a
  `winter-ext.toml` and it contributes skills, agents, context, services, provision handlers, and checks; capability
  slots and lifecycle hooks keep the core swappable, and
  [TUI plugins](https://paul-gross.github.io/winter-docs/tui-plugins/) extend the running tool itself.
- **[Diagnostics & conventions](https://paul-gross.github.io/winter-docs/cli-reference/diagnostics/)** — `winter doctor`
  and `winter lint` check the workspace against itself, so drift and broken conventions surface as a failed probe rather
  than as a confusing agent failure an hour later.
- **[Interfaces](https://paul-gross.github.io/winter-docs/cli-reference/)** — a CLI for agents, a TUI dashboard for
  humans, `--json` everywhere, and editor integration.
- **[Guided setup & updates](https://paul-gross.github.io/winter-docs/getting-started/quick-start/)** — `/ws-setup`
  stands the workspace up and teaches you winter as it goes, researching your applications to discover their services,
  ports, databases, and seed data; `/ws-update` integrates framework updates later.

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

`/ws-setup` is the interactive onboarding walkthrough — it declares and clones your project repositories, researches
your applications to discover their services and dependencies, authors the service manifest for your chosen
orchestrator, and leaves you with a running feature environment. Idempotent: re-run it any time to reconfigure.

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
are computed at runtime and either sourced with `source <(winter env <name>)` — pass `--resolve` to also run any
command-sourced band entry, otherwise it prints as a placeholder — or injected automatically, fully resolved, by
`winter service up`.

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
