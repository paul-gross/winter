# Artifact space — `[space]`

The **winter space** is where winter and its extensions write *generated artifacts* — harness scores, review manifests, workflow session docs, logs, and whatever else an extension owns — as opposed to repo deliverables. Skills resolve a directory at run time with [`winter space <kind>`](../usage/space.md) and write into it, so the location is configured here in one place rather than hardcoded into any one code harness's home directory.

Unconfigured, the space lives inside the workspace under `.winter/`, so artifacts travel with the checkout (and are easy to `.gitignore` if you don't want them tracked). The `[space]` table moves the root and individual kinds.

## `root` — the space root

```toml
[space]
root = ".winter"        # the default; rarely written explicitly
```

`root` resolves by a three-form rule:

| `root` value | Form | Resolves to |
|--------------|------|-------------|
| *(unset)* / `.winter` | workspace-relative | `<workspace>/.winter` |
| `artifacts/winter` | workspace-relative | `<workspace>/artifacts/winter` |
| `~/.winter` | home-relative (leading `~`) | `<home>/.winter` |
| `/var/winter/space` | absolute (leading `/`) | `/var/winter/space` |

A kind with no override is then a sub-directory of the resolved root named after the kind — e.g. with the default root, `winter space scores` → `<workspace>/.winter/scores`.

## `[space.kinds]` — per-kind overrides

```toml
[space.kinds]
scores = "audits"            # -> <root>/audits
logs   = "/var/log/winter"   # -> absolute, outside the space root
cache  = "~/.cache/winter"   # -> home-relative, outside the space root
```

The keys are **dynamic and untyped** — each extension defines its own kinds, and winter enforces no fixed set; an unknown kind simply resolves to `<root>/<kind>`. An override **value** follows the same three-form rule as `root`, with one difference: a *relative* value is taken **relative to the resolved root** (not the workspace), while `~` and absolute values escape the root entirely. This lets an extension keep most kinds tidy under the space while redirecting one (say, `logs`) to an external location.

## Where to put it

- **`.winter/config.toml`** (committed) — a workspace-wide choice all contributors share.
- **`.winter/config.local.toml`** (gitignored per-user overlay) — a personal choice that shouldn't bind teammates, e.g. routing *your* artifacts to `~/.winter` so they survive across worktrees and never touch the repo:

  ```toml
  # .winter/config.local.toml
  [space]
  root = "~/.winter"
  ```

The overlay merges over the committed file by the usual [merge model](./config-files.md), so a local `[space]` root overrides a committed one.

## Resolution is read-only

`winter space <kind>` only **resolves and prints** a path — it creates no directory and writes nothing. Whoever writes artifacts into the space creates the directory (`mkdir -p "$(winter space <kind>)"`) and decides how it is tracked. The default in-workspace location (`.winter/<kind>/`) is a natural candidate for `.gitignore` if you don't want generated artifacts committed; add the entry yourself.
