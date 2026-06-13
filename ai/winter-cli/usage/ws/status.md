# `winter ws status` — full reference

For the rest of the `winter ws` family, see the [`winter ws` hub](./index.md); for the full command reference, see [../index.md](../index.md).

## Synopsis

```
winter ws status [PATTERNS]... [--json] [--fetch]
```

## Pattern vocabulary

Each `PATTERN` is a segment-aware glob over `<env>/<repo>`. A bare env name (no `/`) expands to `<env>/*`. `*` matches any characters within a segment and does not cross `/`.

| Pattern | Matches |
|---------|---------|
| _(none)_ | Every env, every worktree (whole workspace) |
| `alpha` | All worktrees in `alpha/` (expands to `alpha/*`) |
| `alpha/winter` | The single `alpha/winter` worktree |
| `alpha/*` | All worktrees in `alpha/` (explicit glob) |
| `alpha/feature-*` | All worktrees in alpha whose repo name starts with `feature-` |
| `*/winter` | The `winter` worktree across every env that has one |
| `alpha` `beta` | All of alpha's worktrees + all of beta's (multiple patterns) |

Shell reminder: quote patterns containing `*` or `?` to prevent shell glob expansion.

**Zero-match behavior.** When patterns are given but nothing matches, the command exits 2 with an error message naming the patterns. This catches typos and missing `winter ws fetch` the same way a named env did previously.

## Flags

| Flag | Description |
|------|-------------|
| `--json` | Emit the full snapshot as JSON to stdout (see schema below). |
| `--fetch` | Run `git fetch origin` across the in-scope repos before collecting state (network). Refreshes project worktrees only — the same project scope as `winter ws fetch`'s default, not standalone or extension repos. When patterns are given, only those matched repos are fetched. When no patterns, all project repos are fetched. Off by default — bare runs report last-fetched state. |

**Network guarantee:** without `--fetch`, `ws status` makes no network calls. All counts (ahead, behind, tracking) are derived from already-fetched remote-tracking refs in the local git store. Run `winter ws fetch` or pass `--fetch` to refresh them first.

## Exit codes

| Code | Meaning |
|------|---------|
| `0` | Clean — no dirty worktrees, no source-checkout drift, no orphans, no config drift. |
| `1` | Dirty or drifted — at least one condition triggers (see scoping rule below). |
| `2` | Command error — no match for the given patterns, a per-repo probe failure during collection (no partial snapshot is emitted), config parse failure, or other internal error. |

**Extensions are not probed.** The probe failure that triggers exit 2 applies to project worktrees and source checkouts. The `workspace.extensions` list is a config-only read — `ws status` never git-probes extension repos to populate it — so a broken extension checkout cannot fail the command.

**Scoping rule.** When patterns are given, the exit code reflects **only the matched worktrees' dirtiness** (`dirty > 0` on any matched worktree → exit 1). Global source-checkout drift, workspace orphans, and config drift are still printed as context but do **not** flip the exit code for a scoped run. An unscoped run (no patterns) considers all four categories.

## JSON schema (`schema_version: 1`)

`--json` emits a single JSON object (not NDJSON). Consumers should reject or warn on unexpected `schema_version` values.

**Top-level object:**

| Field | Type | Description |
|-------|------|-------------|
| `schema_version` | `int` | Schema version; currently `1`. |
| `environments` | `array` | One `EnvSnapshot` object per matching feature environment. When patterns are given, only the matched environments appear here; source checkouts and workspace sections remain unfiltered. |
| `source_checkouts` | `array` | One `SourceCheckoutSnapshot` per source checkout with drift or non-zero counts. Always the full workspace view regardless of patterns. Empty array when everything is clean. |
| `workspace` | `object` | `WorkspaceLevelSnapshot` — extensions, orphans, config drift. Always the full workspace view regardless of patterns. |

**`environments[]` — `EnvSnapshot`:**

| Field | Type | Description |
|-------|------|-------------|
| `name` | `string` | Environment name (e.g. `"alpha"`). |
| `index` | `int` | Port-offset index (Greek: 1..24; other: hashed 26..281). |
| `port_base` | `int` | Assigned port base (`4000 + index * 100`). |
| `feature_branch` | `string \| null` | Remote feature branch this env tracks (e.g. `"feature/my-branch"`), or `null` when not connected. |
| `worktrees` | `array` | One `WorktreeSnapshot` per matching repo worktree in the env. When patterns are given, only matched worktrees appear. |

**`environments[].worktrees[]` — `WorktreeSnapshot`:**

| Field | Type | Description |
|-------|------|-------------|
| `repo` | `string` | Repository name (e.g. `"winter"`). |
| `branch` | `string \| null` | Local branch name. |
| `upstream` | `string \| null` | Configured remote-tracking ref (e.g. `"origin/master"`), or `null` when none is set. |
| `ahead` | `int` | Commits ahead of `origin/<main-branch>`. |
| `behind` | `int` | Commits behind `origin/<main-branch>`. |
| `tracking_ahead` | `int` | Commits ahead of the configured `upstream` ref. |
| `tracking_behind` | `int` | Commits behind the configured `upstream` ref. |
| `tracking_ref_present` | `bool` | Whether the upstream tracking ref resolves in the local git store (`false` for unconnected envs). |
| `staged` | `int` | Count of staged files. |
| `unstaged` | `int` | Count of unstaged modified/deleted files. |
| `untracked` | `int` | Count of untracked files. |
| `dirty` | `int` | Deduplicated union: staged ∪ unstaged ∪ untracked. |
| `last_commit_subject` | `string \| null` | First line of the most recent commit message, or `null` when the branch has no commits beyond `origin/<main>`. |
| `pinned` | `bool` | Whether the repo is pinned to its main branch (does not participate in feature branching). |

**`source_checkouts[]` — `SourceCheckoutSnapshot`:**

| Field | Type | Description |
|-------|------|-------------|
| `repo` | `string` | Repository name. |
| `branch` | `string \| null` | Current local branch in the source checkout. |
| `behind_origin` | `int` | Commits behind `origin/<main-branch>`. |
| `ahead_origin` | `int` | Commits ahead of `origin/<main-branch>` (shouldn't happen on a well-managed main checkout). |
| `dirty` | `int` | Count of changed files (staged + unstaged + untracked) in the source checkout. |
| `drift` | `array[string]` | Drift findings for this checkout (e.g. missing declared sub-paths). Empty array when clean. |

**`workspace` — `WorkspaceLevelSnapshot`:**

| Field | Type | Description |
|-------|------|-------------|
| `root_path` | `string` | Absolute path to the workspace root. |
| `extensions` | `array[string]` | Names of installed standalone repos (extensions), e.g. `["winter-github", "winter-harness"]`. A config-only read — see the exit-codes note below. |
| `orphans` | `array` | `OrphanSnapshot` objects for filesystem entries with no declared owner. Each has: `kind` (short label, e.g. `"worktree_dir"`), `path` (absolute), `safe_to_remove` (`bool`), `notes` (`string`). |
| `drift_missing` | `array[string]` | Repo names declared in config but absent on disk (run `winter ws init` to fix). |
| `drift_undeclared` | `array[string]` | Directory names present under `projects/` but not in config. |
