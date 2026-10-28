# Winter CLI — Install

Installing the `winter` CLI requires `uv` 0.13 (it provisions Python 3.14 for the CLI).

## Installation

```bash
./tools/winter-cli/install.sh
```

This copies the `winter` wrapper to `~/.local/bin/`. The wrapper auto-discovers the workspace root by searching upward
for `.winter/config.toml` + `tools/winter-cli/`, then runs `uv run` against `tools/winter-cli/` — no manual virtualenv
setup needed.

## Upgrading from a mise-era install

Earlier installs ran the CLI through `mise`. Upgrade `uv` to 0.13 (`uv self update`), then re-run
`./tools/winter-cli/install.sh` to replace the old shim; `mise` is no longer used.

## Bootstrap order

On a fresh clone, run in this order:

1. **Install the CLI** — `./tools/winter-cli/install.sh`
2. **Run `winter init`** — a one-time bootstrap for a fresh clone that has no `.winter/config.toml` yet. Run from the
   workspace root; it creates an empty `.winter/config.toml` (every field falls back to its default) and then delegates
   to `winter ws init`. On a workspace that already has `.winter/config.toml` (e.g. a clone of an already-configured
   workspace), skip straight to step 3 — `winter init`'s only job is creating that file. `winter ws init` reconciles the
   CLI against `.winter/config.toml` and projects workspace skills into all three vendor skill directories (ClaudeCode,
   Codex, OpenCode). This must run **before** any `ws-*` skill is invocable on any harness.
3. **Run `/ws-setup`** — clones repos, wires worktrees, and performs first-time workspace setup.

### Workspace skills (projection)

`winter ws init` projects every skill directory under `workspace_root/<skills_dir>/` into per-vendor skill directories.
This is always-on — no config key is required to enable it. The default `prefix` is `ws` and the default `skills_dir` is
`skills`, so a fresh workspace with a `skills/` directory is automatically projected on `winter ws init`.

Naming rule: a skill directory whose name equals the prefix (e.g. `skills/ws/`) projects as the bare prefix (`ws`); all
other directories project as `<prefix>-<dirname>` (e.g. `skills/init/` → `ws-init`).

With the defaults (`prefix = "ws"`, `skills_dir = "skills"`) and a `skills/my-skill/SKILL.md` at the workspace root,
`winter ws init` creates:

- `.claude/skills/ws-my-skill` (symlink, for ClaudeCode)
- `.codex/skills/ws-my-skill` (symlink, for Codex)
- `.opencode/skill/ws-my-skill/` (copy, for OpenCode)

These projected entries are generated artifacts that `winter ws init` writes; they are git-excluded automatically via a
managed block in the workspace's git exclude file — see
[Workspace exclude file](./usage/ws/init.md#workspace-exclude-file) for where that file lives.

**`SKILL.md` constraint:** Workspace skill files must not set a `name:` frontmatter key — the projected directory name
is the authoritative identity. `winter ws init` rejects any skill directory whose `SKILL.md` declares `name:`.

### Workspace skill prefix and skills_dir

The top-level `prefix` and `skills_dir` keys in `.winter/config.toml` control the workspace skill namespace and source
directory. See [configuration/config-files.md](./configuration/config-files.md#workspace-skill-prefix) for full details
and disambiguation from the per-`[[standalone_repository]]` `prefix` field.
