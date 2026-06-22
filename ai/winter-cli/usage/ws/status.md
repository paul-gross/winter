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

`--json` emits a single JSON object (not NDJSON) on **stdout**; all diagnostics (including those enabled by `-v`/`WINTER_LOG_LEVEL`) go to **stderr** and never appear in the JSON stream. Consumers should reject or warn on unexpected `schema_version` values.

A machine-readable JSON Schema is checked into the repo at `tools/winter-cli/schemas/ws-status-v1.json`. Validate programmatically with `jsonschema`:

```python
import json, jsonschema
schema = json.load(open("tools/winter-cli/schemas/ws-status-v1.json"))
data   = json.loads(subprocess.check_output(["winter", "ws", "status", "--json"]))
jsonschema.validate(data, schema)
```

**Top-level object:**

| Field | Type | Description |
|-------|------|-------------|
| `schema_version` | `int` | Schema version; currently `1`. |
| `environments` | `array` | One `EnvSnapshot` object per matching feature environment. When patterns are given, only the matched environments appear here; source checkouts and workspace sections remain unfiltered. |
| `source_checkouts` | `array` | One `SourceCheckoutSnapshot` per source checkout with drift or non-zero counts. Always the full workspace view regardless of patterns. Empty array when everything is clean. |
| `workspace` | `object` | `WorkspaceLevelSnapshot` — extensions, orphans, config drift. Always the full workspace view regardless of patterns. |
| `dashboard` | `object` | `DashboardSnapshot` — the configured dashboard grid layout and the concrete layout it resolves to for the current workspace shape. Always the full-workspace view regardless of patterns. |

**`environments[]` — `EnvSnapshot`:**

| Field | Type | Description |
|-------|------|-------------|
| `name` | `string` | Environment name (e.g. `"alpha"`). |
| `index` | `int` | Persisted port-offset index for this env (from `.winter/state.toml`). |
| `port_base` | `int` | Assigned port base (`base_port + index * ports_per_env`; defaults to `4000 + index * 20`). |
| `feature_branch` | `string \| null` | Remote feature branch this env tracks (e.g. `"feature/my-branch"`), or `null` when not connected. |
| `worktrees` | `array` | One `WorktreeSnapshot` per matching repo worktree in the env. When patterns are given, only matched worktrees appear. |
| `extensions` | `object` | Per-plugin badge dict keyed by plugin prefix (e.g. `"wst"`); values are short display strings (e.g. `"● 3/3"`). Empty object `{}` when no environment decorators ran or all wrote empty strings. Only populated on the `--json` path — the human table omits it. |

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
| `main_branch` | `string \| null` | The repo's configured main branch (e.g. `"master"` or `"main"`), or `null` when unknown. Non-TUI clients use this to reproduce the `tracking_differs_from_main` divergence-marker logic without re-reading config. |

> **Note — per-worktree extension badges:** `WorktreeRepoStatus.extensions` is populated by worktree-repo decorator plugins (used by the Textual TUI to render per-cell badges), but is deliberately not serialized into the JSON contract. If you are building a non-TUI client and need per-worktree extension data, open a contract proposal — the field will be added when a tested decorator exists to drive it.

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
| `standalone_pins` | `array` | One `StandalonePinSnapshot` per declared standalone repo that has a `ref` configured. Empty array when no standalone repos have a pin. See below. |

**`dashboard` — `DashboardSnapshot`:**

The dashboard grid is interactive-only — its resolved layout is otherwise observable only inside the Textual TUI. This block exposes the resolution non-interactively so scripts and agents can confirm which layout `auto` picks (or that a `[tui.dashboard]` config change took effect) without driving a Textual pilot. The resolution reflects the whole-workspace shape (every env, every project repo) and is **unaffected by `ws status` patterns**.

| Field | Type | Description |
|-------|------|-------------|
| `configured_layout` | `string` | The `[tui.dashboard] layout` config value verbatim: `"auto"` (default), `"repos-as-rows"`, `"repos-as-columns"`, or `"list"`. |
| `resolved_layout` | `string` | The concrete layout the configured value resolves to for the current workspace shape — one of `"repos-as-rows"`, `"repos-as-columns"`, `"list"`. Equals `configured_layout` unless it is `"auto"`, which resolves via the same heuristic the dashboard TUI grid uses — see the `auto` row in [dashboard.md](../dashboard.md#layouts). |

**`workspace.standalone_pins[]` — `StandalonePinSnapshot`:**

Each entry represents the pin/lock state of one standalone repo pinned via `ref` in the config. Agents can use this to check whether a standalone is drifted from its pinned commit without running a mutating command.

| Field | Type | Description |
|-------|------|-------------|
| `name` | `string` | Matches `[[standalone_repository]].name` in the config. |
| `ref` | `string` | The `ref` string currently declared in the config (branch name, tag, or commit SHA). |
| `kind` | `string \| null` | How the ref was classified at the last `ws update` or `ws init`: `"branch"`, `"tag"`, or `"commit"`. `null` when no lock entry exists yet (the repo has never been pinned/updated). |
| `locked_commit` | `string \| null` | Full 40-char SHA recorded in `.winter/config.lock` at the last pin operation. `null` when no lock entry exists. |
| `config_ref_drift` | `bool` | `true` when the config's `ref` differs from the `ref` recorded in the lock file — the lock is stale and `ws update` is needed. `false` when they match or when no lock entry exists. |
| `head_drift` | `bool` | `true` when the standalone's current HEAD commit does not match `locked_commit` — the checkout has drifted from the recorded pin. `false` when they match or when no lock entry exists. |
| `head_commit` | `string \| null` | Current HEAD commit of the standalone repo (full 40-char SHA), or `null` when the repo is absent on disk or the HEAD probe fails. |
