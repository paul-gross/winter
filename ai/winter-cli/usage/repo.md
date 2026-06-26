# `winter repo` — repository commands

Manage the repositories declared in the workspace config. For the hub and the rest of the command surface, see [../index.md](../index.md).

| Command | Usage | Purpose |
|---------|-------|---------|
| `winter repo list` | `winter repo list [--json]` | List all project and standalone repositories and their types |
| `winter repo add` | `winter repo add URL [--standalone] [--name N] [--main-branch B] [--ref R] [--git-exclude E] [--cmd C] [--pinned] [--path P] [--prefix P] [--local] [--json]` | Add a repository to the workspace config (writes `.winter/config.toml` unless `--local` writes `.winter/config.local.toml`) |
| `winter repo remove` | `winter repo remove <project\|standalone>/NAME [--local] [--json]` | Remove a repository entry from the config |

**`--ref`** (standalone only) — pins the standalone repo to a branch, tag, or commit SHA. Accepted only when `--standalone` is also given; rejected for project repos. Writes `ref = "<value>"` into the `[[standalone_repository]]` entry. See [configuration/repositories.md — `ref`](../configuration/repositories.md#ref--standalone-repo-pins) for the full pin semantics.

For the `.winter/config.toml` repository schema these commands read and write, see [configuration/repositories.md](../configuration/repositories.md).
