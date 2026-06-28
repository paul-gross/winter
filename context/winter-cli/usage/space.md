# `winter space` — resolve a generated-artifact directory

```
winter space <kind>
```

Print the absolute directory the **winter space** resolves *kind* to. This is a **pure, read-only** resolution — it reads the `[space]` config and prints a path. It creates no directory, writes nothing, and does not touch git; the caller owns all of that. The winter space is where winter and its extensions write *generated artifacts* — harness scores, review manifests, workflow session docs, logs, and whatever else an extension owns — as opposed to repo deliverables. A consuming skill reads the resolved value instead of hardcoding an artifact path into one code harness's home directory:

```bash
dir="$(winter space scores)"
# -> /path/to/workspace/.winter/scores   (default)
mkdir -p "$dir"                          # create it yourself before writing
```

*kind* is a **dynamic, extension-defined** bucket name — a single segment starting with a letter or digit, then letters, digits, `.`, `_`, or `-` (not a path), so the resolved directory cannot escape the space root via the argument. Winter enforces no fixed set: any kind resolves to a directory, defaulting to a sub-directory of the space root named after the kind. The `winter-workflow` extension uses `scores`, `manifests`, `workflows`, and `retrospectives`; another extension can introduce its own (e.g. `logs`).

## Where it resolves

The location comes from the `[space]` table in `.winter/config.toml`. With no configuration the space root is the workspace-relative `.winter`, so:

```bash
winter space scores          # -> <workspace>/.winter/scores
winter space manifests       # -> <workspace>/.winter/manifests
```

The root and any individual kind are overridable — workspace-relative, home-relative (`~`), or absolute. See [configuration/space.md](../configuration/space.md) for the table, the override rules, and worked examples.

## Exit codes

| Exit code | Meaning |
|-----------|---------|
| 0 | Success — the absolute directory is written to stdout (one line). |
| 1 | *kind* is malformed (contains a path separator or traversal), or a fatal config error — message on stderr, no path written. |

## Notes

- The single stdout line is meant to be captured (`dir="$(winter space <kind>)"`); diagnostics go to stderr, so command substitution stays clean.
- Resolution only — `winter space` computes and prints the directory. Creating it, writing into it, and deciding whether to git-ignore it are the caller's responsibility. The default in-workspace location (`.winter/<kind>/`) is convenient to add to `.gitignore` if you don't want generated artifacts tracked.
- Pure and idempotent: re-running with the same config returns the same path and changes nothing on disk.
