# Workspace Layout (Polyrepo)

This workspace manages **multiple project repositories** as peers. All repos are treated equally.

## Directory Structure

```
./                              workspace branch - this is where you are
├── CLAUDE.md                   # Workspace instructions
├── CLAUDE.winter.md            # Installed-extension block (@-imported from CLAUDE.md)
├── ai/                         # Workspace documentation
│   ├── workspace-layout.md     # This file
│   ├── worktree-ops.md         # Git commands for this topology
│   ├── winter-cli/             # CLI command reference + setup guide
│   ├── setup-project-setup.md  # Walkthrough for authoring project-setup.md
│   ├── contributing-setup.md   # Walkthrough for authoring contributing.md
│   └── project/                # Project-specific integration config (contributing.md, plus any extension config)
├── .claude/                    # Workspace-level agents, skills, and settings
│   ├── agents/                 # Top-level .md files plus <prefix>-* symlinks from extensions
│   └── skills/                 # Top-level skill dirs plus <prefix>-* symlinks from extensions
├── .winter/                    # Workspace-level winter config and installed extensions
│   ├── config.toml             # Repo declarations (project + standalone)
│   ├── config.local.toml       # Optional local override (gitignored)
│   ├── state.toml              # Machine-local env-index registry (gitignored, managed by winter)
│   └── ext/<short-name>/       # Standalone clones for installed extensions
├── .winter.workspace.env       # Workspace-scope shell env (WINTER_PORT_BASE for index 0); gitignored, managed by winter
├── tools/                      # Workspace tooling
│   └── winter-cli/             # The `winter` CLI source
├── projects/                   # All project repositories (source checkouts)
│   ├── <repo-1>/               # Project repo (main branch)
│   ├── <repo-2>/               # Project repo (main branch)
│   └── <repo-n>/               # Project repo (main branch)
├── <standalone-repo>/          # Standalone repos cloned at workspace root (when no path override; see Repo Inventory)
├── up / down / status          # Symlinked into every feature env by winter-service-tmux
└── {greek-letter}/             # Feature environment directories
    ├── <repo-1>/               # Worktree of project repo (feature branch)
    ├── <repo-2>/               # Worktree of project repo (feature branch)
    ├── <repo-n>/               # Worktree of project repo (feature branch)
    ├── up / down / status      # Symlinks to the extension scripts above (running services from the env dir)
    ├── .winter.env             # Per-environment shell env file (WINTER_ENV, WINTER_PORT_BASE, WINTER_WORKSPACE_PORT_BASE, project-specific vars)
    └── .winter/                # Per-env service logs/state (e.g. logs/<svc>.log) — NOT a workspace root
```

> **Workspace root identity.** The workspace root is the single directory holding **`.winter/config.toml`**. A feature env's `<env>/.winter/` (created by winter-service-tmux to hold `logs/<svc>.log` and per-env state) has no `config.toml` and is never a root — winter-cli's locator walks past it to the real root.

## Source Checkouts

The following directories are source checkouts — **never work in these directly**:
- `./projects/<name>/` — main branch checkouts for each repo

All development happens in feature worktrees (e.g., `./alpha/<repo-name>/`).

## Inherited docs at the workspace root

Many files at the workspace root — most `ai/` docs, `CLAUDE.md`, and others — are **not authored here**. They are inherited copies carried in from an upstream project repo (chiefly the `winter` tool) by the shared-lineage rebase: the workspace sits one customization commit on top of `winter/master`, so everything that commit doesn't own arrives from upstream and travels downstream automatically on the next sync.

**Before editing any root file, check whether it is an inherited copy.** If it is, fix it at the source repo (in that repo's feature env) and let the sync carry it down — never hand-edit the downstream copy. The next rebase clobbers the edit (if upstream touched the same lines) or silently diverges (if it didn't), so a downstream hand-edit disappears or drifts back without warning.

How to tell: run `git show winter/master:<path>` — if the file exists there, it is inherited. The customization commit's own diff (`git show HEAD`) lists every file the workspace owns independently; anything not in that diff is inherited.

## Feature Worktree Structure

Each Greek letter directory (e.g., `alpha/`) contains a git worktree for **every** repository in `projects/`. All worktrees within a feature directory share the same branch name (the Greek letter).

When working on a feature in `alpha/`:
- Repo code is at `./alpha/<repo-name>/`
- Environment shell vars are at `./alpha/.winter.env` — `WINTER_ENV`, `WINTER_ENV_INDEX`, `WINTER_PORT_BASE`, and `WINTER_WORKSPACE_PORT_BASE` are seeded by `winter ws init`; project-specific vars (per-service ports, database URLs, etc.) are appended below the managed block by `project-setup.md`. Workspace-scope services read their own band from `.winter.workspace.env` at the workspace root (`WINTER_PORT_BASE` = the index-0 base).

## Repo Inventory

The authoritative repo lists live in `workspace:/.winter/config.toml`:

- `[[project_repository]]` — repos that get cloned into `./projects/` and worktreed into Greek-letter feature directories.
- `[[standalone_repository]]` — repos cloned at the workspace root (or under a configured relative `path`), skipped during feature branching. Used for winter extensions and any auxiliary repo that shouldn't be multiplied per-feature.

Each entry declares the repo's name, clone URL, git-exclude entries, and setup commands. Project entries also carry an optional main branch (falls back to the top-level `main_branch`) and pinned status. Standalone entries can additionally declare a `prefix` override for the extension symlink prefix.

The directories under `./projects/` and the standalone directories at the workspace root are a materialization of that config — `winter ws init` brings them into alignment. To list the declared repos:

```bash
winter repo list
```

## Feature environments and Greek letters

Feature environments are named after Greek letters: `alpha/`, `beta/`, `gamma/`, etc. Each env contains a feature worktree for every project repository, all on a branch matching the env name. For example, `./alpha/my-app/` is a worktree of `my-app` on branch `alpha`.

Each env has an index used to assign unique ports per env. The port layout is config-driven: `per-env port base = base_port + index * ports_per_env`. With defaults (`base_port=4000`, `ports_per_env=20`): alpha (1) → 4020, beta (2) → 4040, gamma (3) → 4060, and so on. This ensures multiple envs can run services simultaneously without port conflicts.

The first 10 Greek letters (`alpha`…`kappa`) are configured as `env_aliases` with fixed indices `1..10`. Other Greek letters and non-Greek env names hash into a higher index band. To look up the index for any name:

```bash
winter ws index <name>
```

For an existing env this returns the **persisted** index from `.winter/state.toml`. For a hypothetical name it returns the **suggested** slot (which may shift on create if there is a collision).

Default to **alpha**. Use **beta** if alpha is occupied. Only create additional envs when needed, and confirm with the user first.

Conventional names: alpha, beta, gamma, delta, epsilon, zeta, eta, theta, iota, kappa, lambda, mu, nu, xi, omicron, pi, rho, sigma, tau, upsilon, phi, chi, psi, omega

## Path notation

Paths use a `<context>:<path>` prefix to clarify which repo/branch a file lives in:
- `workspace:` — the workspace root (this repo's workspace branch)
- `<env>:` — a feature environment (e.g., `alpha:` resolves to a per-repo worktree inside `alpha/`)
- `<extension-name>:` — a winter extension (e.g., `winter-product:`)

## Rules

1. **Never work in source checkouts directly** — use feature environments for all code changes (see the [Source Checkouts](#source-checkouts) section for which directories are source checkouts)
2. **Local branch = Greek letter, remote branch = feature name** — each env's worktrees use a Greek-letter branch locally (e.g., `alpha`). The remote feature branch (e.g., `feature/basic-addon`) is a separate name configured via tracking. See [worktree-ops.md](./worktree-ops.md) for how to connect an env to a remote feature branch.
3. **Confirm before working** — always verify which env to work in
4. **Always spawn subagents from the workspace root** — subagents and teammates must be created while the working directory is the workspace root. Spawning from a project subdirectory causes the subagent to lose workspace context including this CLAUDE.md, agent definitions, and skills.
5. **Follow the project's contributing conventions when completing work** — see [project/contributing.md](./project/contributing.md). If that file doesn't exist, guide the user to establish one documenting how completed work should be merged, pushed, and delivered for their specific projects.
