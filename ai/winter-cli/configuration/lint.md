# Lint checks

`winter lint` (see [../usage/lint.md](../usage/lint.md)) is the convention-checking counterpart of `winter doctor`. It aggregates findings from three sources — symmetric with doctor's probe sources: built-in core checks bundled with winter-cli, an optional workspace-level script, and one script per installed extension. The workspace and extension checks are opt-in scripts that follow the same output contract; the core checks always run, with no per-workspace registration. It owns scope selection, aggregation, and reporting — the check logic lives entirely in the checks it dispatches.

## Built-in core checks

These ship with winter-cli and run on every `winter lint`, the same way `winter doctor` runs its built-in core probes — no `.winter/config.toml` or `winter-ext.toml` registration needed. They run first, before the workspace and extension checks, and their findings appear under a `[core]` source group.

**Unlike contributed lint scripts, core checks are not confined to `WINTER_LINT_PATHS`.** Core checks may read workspace-root injection roots (`CLAUDE.md`, `CLAUDE.winter.md`) and `.winter/` manifests directly to do their work. This is intentional — the file-size check walks the `@import` graph rooted at those files regardless of scope, and the required-services check reads every manifest to find `required_services` declarations. Contributed scripts MUST confine themselves to `WINTER_LINT_PATHS`; core checks operate at a higher privilege tier because they ship with winter-cli and their scope is the whole workspace by design.

The three built-in core checks are:

- **module extractability** (`tools/winter-lint/extractability.py`): validates dependency direction across the ecosystem graph, flagging a `<context>:/path` reference whose target a module isn't guaranteed to have when shipped standalone — a core module pointing at an extension (a layering inversion) or an undeclared sibling (a dead pointer at the consumption edge). It is graph-driven (it calls back into `$WINTER_CLI graph --json` rather than rebuilding the graph) and honors the `<!-- winter-lint:example -->` line exemption and fenced-code-block skip. Full rules in [tools/winter-lint/README.md](../../../tools/winter-lint/README.md).

- **file-size**: checks agent-facing markdown files against configurable byte-size thresholds. Files in the auto-injected `@import` graph (rooted at `CLAUDE.md` and `CLAUDE.winter.md` in the workspace root) are held to the tighter `injected_bytes` threshold; all other `.md` files in scope are checked against the looser `reference_bytes` threshold. Override thresholds in `.winter/config.toml` under `[core_checks.file_size]`.

- **required-services**: validates `required_services` entries in `[[provision.resource]]` and `[[provision.data]]` handlers (in `.winter/config.toml` and each installed extension's `winter-ext.toml`) against the merged service catalog from all bound service-orchestrator providers. Each entry must be scope-qualified (`workspace/<name>` or `<env>/<name>`), and the named service must appear in at least one provider's `catalog` output. See [../usage/provision.md](../usage/provision.md#service-check-required_services) for the runtime `required_services` enforcement; this static lint check is a separate, complementary surface — it validates catalog membership ahead of time and runs even when no env is provisioned.

## Finding output contract

A lint script follows the **same NDJSON contract as a doctor probe** (see [doctor.md](./doctor.md#probe-output-contract)) with two additions per object — `check` (the field name; `name` is also accepted as an alias, so an existing doctor probe can be repointed at lint with minimal change) and optional `file` / `line` location fields:

```json
{"check": "path-notation", "status": "fail", "message": "non-canonical ref `../harness`", "file": "ai/index.md", "line": 12, "remediation": "Use the `winter-harness:` prefix."}
{"check": "agent-frontmatter", "status": "warn", "message": "missing `model`", "file": ".claude/agents/wf-developer.md"}
```

Required fields: `check` (string) and `status` (`pass` / `warn` / `fail`). Optional: `message`, `file`, `line`, `remediation`. Exit handling and misconfiguration behavior (missing field silently skipped; missing / non-executable / directory-escaping script surfaces as a `fail`; unparseable lines become `warn`) match the doctor probe contract exactly.

## Scope environment variables

On top of the doctor probe's env (`WINTER_WORKSPACE_DIR`, and for extension scripts also `WINTER_EXT_DIR` / `WINTER_EXT_PREFIX`), every lint script receives the resolved scope:

| Var | Meaning |
|-----|---------|
| `WINTER_LINT_SCOPE` | The scope kind: `all`, `repo`, `env`, or `changed`. |
| `WINTER_LINT_PATHS` | Newline-delimited absolute paths in scope. Under `changed` these are individual **files**; under `all` / `repo` / `env` they are **directory** roots. A check must `stat` each path and handle both. |
| `WINTER_CLI` | Absolute path to the winter CLI that launched the run. A check may call back into it for workspace-wide data it can't derive from its own scope — e.g. `$WINTER_CLI graph --json` for the dependency graph — instead of rebuilding it. A check must **never** call `winter lint` (that recurses). |

**A check MUST confine itself to `WINTER_LINT_PATHS`.** `winter lint` runs every contributed script for every scope and never filters by content — keeping a run "applicable to that scope" is the script's job. A check walks the given paths, applies its rules only to files under them, and emits nothing for a scope whose content it doesn't recognize.

- **Do**: iterate `WINTER_LINT_PATHS`, walk each (a file is itself; a directory is recursed), match the files you own, stay silent otherwise.
- **Don't**: glob the whole workspace, read `$WINTER_WORKSPACE_DIR` wholesale, or use the current directory — that leaks findings outside the scope and silently breaks `--changed` and per-repo runs.

## Workspace lint check

The workspace contributes a lint script via a top-level field in `.winter/config.toml`, symmetric with the workspace doctor probe:

```toml
lint = "ai/project/lint.sh"             # single script
lint = ["ai/project/lint.sh", "ai/project/lint_docs.sh"]   # or a list
```

`lint` accepts a single path or a list; a bare string is coerced to a one-element list. Paths are **relative to the workspace root** and must point to executable files. They run first, before extension checks, with cwd at the workspace root, and their findings appear under a `[project]` source group. Use them for checks this specific workspace owns. Ecosystem-general checks meant to travel between workspaces belong in an installed extension instead (the `lint` field below) — e.g. a dedicated `winter-lint` extension hosting the cross-cutting checks no single domain extension owns.

## Extension lint checks

Extensions opt in via the top-level `lint` field in `winter-ext.toml` (paths **relative to the extension directory**, executable). Like the workspace field, it accepts a single path or a list — an extension that contributes several distinct checks (say, one per convention) lists them all, and each runs as its own script. Each runs with cwd at the workspace root and the scope env vars above; findings appear under the extension's `[<ext-prefix>]` source group.

A minimal check skeleton — walk the scope, match the files you own, emit one finding per violation, stay silent on the rest:

```bash
#!/usr/bin/env bash
# Flag Markdown files that reference the harness with a bare relative path
# instead of the canonical `winter-harness:` notation.
set -euo pipefail

emit() { printf '{"check":"path-notation","status":"%s","message":"%s","file":"%s","line":%s}\n' "$1" "$2" "$3" "$4"; }

while IFS= read -r path; do
  [ -z "$path" ] && continue
  # A directory root is recursed; a single changed file is checked directly.
  while IFS= read -r md; do
    while IFS=: read -r line _; do
      emit fail "use the \`winter-harness:\` prefix" "$md" "$line"
    done < <(grep -nE '\.\./harness' "$md" || true)
  done < <(find "$path" -type f -name '*.md' 2>/dev/null)
done <<< "$WINTER_LINT_PATHS"
```

Exit non-zero only for the script's own failures — winter turns that into a synthetic `fail`. A clean run that found nothing exits `0` and emits nothing.
