# Lint checks

`winter lint` (see [../usage/lint.md](../usage/lint.md)) is the convention-checking counterpart of `winter doctor`. It
aggregates findings from three sources — symmetric with doctor's probe sources: built-in core checks bundled with
winter-cli, an optional workspace-level script, and one script per installed extension. The workspace and extension
checks are opt-in scripts that follow the same output contract; the core checks always run, with no per-workspace
registration. It owns scope selection, aggregation, and reporting — the check logic lives entirely in the checks it
dispatches.

## Built-in core checks

These ship with winter-cli and run on every `winter lint`, the same way `winter doctor` runs its built-in core probes —
no `.winter/config.toml` or `winter-ext.toml` registration needed. They run first, before the workspace and extension
checks, and their findings appear under a `[core]` source group.

**Unlike contributed lint scripts, core checks are not confined to `WINTER_LINT_PATHS`.** Core checks may read
workspace-root injection roots (`AGENTS.md`, `AGENTS.winter.md`, and the committed `CLAUDE.md` shim) and `.winter/`
manifests directly to do their work. This is intentional — the file-size check walks the `@import` graph rooted at those
files regardless of scope, and the required-services check reads every manifest to find `required_services`
declarations. Contributed scripts MUST confine themselves to `WINTER_LINT_PATHS`; core checks operate at a higher
privilege tier because they ship with winter-cli and their scope is the whole workspace by design.

The three built-in core checks are:

- **module extractability** (`tools/winter-lint/extractability.py`): validates dependency direction across the ecosystem
  graph, flagging a `<context>:/path` reference whose target a module isn't guaranteed to have when shipped standalone —
  a core module pointing at an extension (a layering inversion) or an undeclared sibling (a dead pointer at the
  consumption edge). It is graph-driven (it calls back into `$WINTER_CLI graph --json` rather than rebuilding the graph)
  and honors the `<!-- winter-lint:example -->` block exemption and fenced-code-block skip. Full rules in
  [tools/winter-lint/README.md](../../../tools/winter-lint/README.md).

- **file-size**: checks agent-facing markdown files against configurable size thresholds. Files in the auto-injected
  `@import` graph (rooted at `AGENTS.md`, `AGENTS.winter.md`, and the committed `CLAUDE.md` shim in the workspace root)
  are held to the tighter `injected_bytes` threshold; all other `.md` files in scope are checked against the looser
  `reference_bytes` threshold. Override thresholds in `.winter/config.toml` under `[core_checks.file_size]`.

  Discovery consults the owning repo's git for every scope path that's a directory walk — a file the repo's `.gitignore`
  already excludes (a vendored tree such as `.terraform/` after `terraform init`, say) never becomes a candidate, with
  no `[lint.ignore]` rule required. A scope path that's already a single file, such as the `--changed` scope's, is taken
  as given and never filtered this way.

  Only *untracked* ignored files are dropped. An untracked file that isn't ignored stays in scope, and so does a
  **tracked** file that matches an ignore pattern — git applies ignore rules only to untracked paths, so committing a
  file puts it back in scope whatever `.gitignore` says about it. The rule when anything is uncertain is that a
  candidate stays in scope: a scope path outside any git repository, one owned by an enclosing repo rather than itself,
  a git that can't be invoked or refuses the directory, and a failed ignore lookup all walk unfiltered beyond the fixed
  prune list rather than dropping anything. Run `winter --verbose lint` to see which of these a run took.

  Both thresholds are measured in **effective bytes**, not raw ones. The check is standing in for token cost, so the two
  kinds of formatting that inflate a markdown file without adding tokens are normalized away before measuring: every run
  of whitespace collapses to a single byte (349 spaces of table padding cost one), and every run of four or more of the
  same non-alphanumeric character collapses to three (`|-------|` measures as `|---|`). A wide, formatter-padded table
  is therefore judged on the content in its cells, and reflowing or re-padding a file never changes its measured size.
  Findings report both numbers — `4812 effective bytes (9034 raw)`.

- **required-services**: validates `required_services` entries in `[[provision.resource]]` and `[[provision.data]]`
  handlers (in `.winter/config.toml` and each installed extension's `winter-ext.toml`) against the merged service
  catalog from all bound service-orchestrator providers. Each entry must be scope-qualified (`workspace/<name>` or
  `<env>/<name>`), and the named service must appear in at least one provider's `catalog` output. See
  [../usage/provision.md](../usage/provision.md#service-check-required_services) for the runtime `required_services`
  enforcement; this static lint check is a separate, complementary surface — it validates catalog membership ahead of
  time and runs even when no env is provisioned.

## Ignoring findings

A repo installable in any winter workspace must lint clean in any winter workspace. So a repo that legitimately carries
unresolvable content — a template tree, a fixture, recorded results — **declares that itself, in its own
`winter-ext.toml`**, and ships clean everywhere it is installed. A workspace never declares exemptions on behalf of a
repo it owns; that would leave the repo dirty in every other workspace.

Gitignored content skips this declaration for the **file-size** check alone (see
[Built-in core checks](#built-in-core-checks)). Extractability, required-services, and every contributed script discover
their own paths independently and never consult git — a gitignored tree that trips one of those still needs an explicit
`[lint.ignore]` rule, the same as any other exempted content.

```toml
name = "winter-benchmark"

[lint.ignore]
# Whole paths — every check goes quiet here. Repo-relative globs.
paths = ["environments/workspace-template/**"]

# Narrower, and the form to reach for: silence one check, keep the rest of the
# file covered.
[lint.ignore.checks]
link-anchors = ["environments/workspace-template/**"]
path-notation = ["results/**"]
```

Each value takes a single glob or a list. Globs are **repo-relative, always** — that is what makes the declaration
portable: the same manifest suppresses the same findings in a source checkout, in a feature-env worktree, in
`.winter/ext/`, and in a stranger's workspace.

Prefer `[lint.ignore.checks]` over `[lint.ignore] paths`. A blanket path ignore on a file turns link checking off there
permanently, so the day someone genuinely breaks a link in it, lint stays silent. Per-check keeps the other checks live.

### Declaring ignores alongside lint scripts

TOML cannot spell both `lint = [...]` and `[lint.ignore]` in one file, so `lint` also accepts a **table** form. A repo
that contributes checks *and* declares ignores writes:

```toml
[lint]
scripts = ["scripts/lint-agents.py", "scripts/lint-methodology.py"]

[lint.ignore.checks]
agent-frontmatter = ["fixtures/**"]
```

`[lint] scripts` means exactly what `lint = [...]` means. Use the scalar/list form when there are no ignore rules.

### Matching semantics

- A finding matches a rule when its `file` resolves under a listed glob **and**, for a `[lint.ignore.checks]` entry, its
  `check` equals the key.
- `**` descends recursively and stands for *zero or more* segments, so `<dir>/**` also covers `<dir>` itself and
  `a/**/b` covers `a/b`. `*` and `?` stay inside one path segment, so `results/*` covers `results/a.md` but not
  `results/run/a.md` — write `results/**` for a whole tree. Character classes work as in `fnmatch`: `[abc]`, `[a-z]`,
  `[!abc]` to negate, with a leading `^` a literal member rather than a negation. Every divergence from the plain
  `fnmatch` used by `lint_doc_references.py`'s `--allow` is this matcher being **narrower**; a suppression rule is the
  wrong place for a glob that quietly matches more than its author meant.
- The dispatcher maps each finding's `file` back to its owning repo root — the nearest ancestor carrying a
  `winter-ext.toml` — to evaluate repo-relative globs. That resolution works identically for every scope, including
  `--changed`, where the scope is individual files rather than repo directories.
- A finding with **no `file`** — a module-level failure like an unset `WINTER_CLI` or a `requires` cycle — is never
  ignorable by a path rule. Those are configuration errors, not content judgments.
- Suppression is post-hoc: the check still runs and still does the work, so this buys correctness, not speed. A check
  that *crashes* on a file therefore cannot be silenced by an ignore — suppressing a finding and suppressing a failure
  are different problems, and this solves only the first.

### Suppression stays visible

An ignore that nobody can see is a way to hide rot, so:

- A suppressed finding never counts toward `fails` and never fails the run, but it is **always** counted in the summary:
  `✗ 3 fail / 2 warn / 10 finding(s) / 7 ignored`.
- `winter lint --show-ignored` re-prints suppressed findings in an `[ignored]` block, grouped under the rule that
  matched each. `--json` emits them as their own `ignored` event type carrying the same payload plus a `rule` field.
- **A rule that suppresses nothing is itself reported as a `warn`.** A stale ignore is a lie about the corpus, and is
  exactly how this feature decays. Two cases are reported: a glob matching no path in the repo at all (the tree was
  renamed or deleted), and a glob whose files this run actually linted without suppressing anything (whatever it hid got
  fixed, and the rule outlived it). A rule whose paths simply were not linted this run — the common case under
  `--changed` — is judged neither way and stays silent.
- **A declaration winter cannot use is reported too**, under the same `lint-ignore` check: an unknown key (`path` for
  `paths`, `[lint.ignores]` for `[lint.ignore]`), a wrong-typed value, an empty or malformed glob, or a
  `winter-ext.toml` that will not parse. A typo that never becomes a rule is worse than a stale one — nothing about it
  is visible otherwise — so none of it is dropped silently. Every one of these failure modes suppresses **nothing**, so
  a broken ignore reveals findings rather than hiding them, and none of them fails the run or aborts it.

**One case sits outside all of this:** the file-size check's gitignore-aware discovery (above) drops a candidate before
any finding exists — there is nothing to count, re-print, or warn about. To see which rule dropped a given path, run
`git check-ignore -v <path>` from within the owning repo; `-v` names the pattern and the file it came from, where a bare
`git check-ignore` only echoes back paths that are ignored.

### Where the filter lives

`LintService` flattens core, workspace, and extension findings into one list before reporting, and the filter sits on
that one line. So **no lint script parses ignore config** — scripts stay stateless and independently runnable, which is
how they are tested — and every future contributed check inherits ignore support for free.

### The workspace surface

`.winter/config.toml` carries the same table, for the case the repo surface cannot reach: a finding in a repo the
workspace has no standing to fix from here — vendored, third-party, or a repo with no `winter-ext.toml` of its own to
declare anything in.

```toml
[lint.ignore]
# The workspace's own files. These globs are workspace-relative, because content
# at the workspace root belongs to no repo and nothing else can speak for it.
paths = ["context/project/scratch.md"]

# A whole repo, when none of its conventions are ours.
repos = ["some-vendor-repo"]

# One repo, narrowed. These globs are repo-relative — the only spelling that
# survives the same repo appearing once per feature env.
[lint.ignore.repo."some-vendor-repo"]
paths = ["docs/**"]

[lint.ignore.repo."some-vendor-repo".checks]
link-anchors = ["README.md"]
```

**Reach for the repo's own manifest first.** A workspace that exempts a repo it owns leaves that repo dirty in every
other workspace that installs it; the exemption travels with the repo or it does not travel at all. This surface is for
when that is not an option.

Both this table and the `[[project_repository]]` declarations a rule's target is checked against are read from
`.winter/config.toml` merged with its gitignored `config.local.toml` overlay — the same two-file model the rest of the
CLI loads — so a repo added with `winter ws repo add --local` is a valid target and a rule may live in either file.

A name under `repos` / `[lint.ignore.repo]` is checked against those declarations, so a typo is reported rather than
quietly matching nothing. The check is skipped in a workspace that declares no project repositories at all, which is a
config winter has no business second-guessing. A name that is real but outside the current scope resolves to nothing and
stays silent — the same judgment a `--changed` run makes about rules it had no chance to exercise.

### Precedence

Union, not override. A finding is suppressed if **any** applicable rule matches, and neither surface can un-ignore what
the other ignored. The two answer to different owners: a workspace cannot force a finding back on for a repo whose
author declared it acceptable, and a repo cannot override a workspace's judgment about content it vendored. Making one
win would only invite a fight over whose config is authoritative.

## Finding output contract

A lint script follows the **same NDJSON contract as a doctor probe** (see
[doctor.md](./doctor.md#probe-output-contract)) with two additions per object — `check` (the field name; `name` is also
accepted as an alias, so an existing doctor probe can be repointed at lint with minimal change) and optional `file` /
`line` location fields:

```json
{"check": "path-notation", "status": "fail", "message": "non-canonical ref `../harness`", "file": "context/index.md", "line": 12, "remediation": "Use the `winter-context:` prefix."}
{"check": "agent-frontmatter", "status": "warn", "message": "missing `model`", "file": ".claude/agents/wf-developer.md"}
```

Required fields: `check` (string) and `status` (`pass` / `warn` / `fail`). Optional: `message`, `file`, `line`,
`remediation`. Exit handling and misconfiguration behavior (missing field silently skipped; missing / non-executable /
directory-escaping script surfaces as a `fail`; unparseable lines become `warn`) match the doctor probe contract
exactly.

## Scope environment variables

On top of the doctor probe's env (`WINTER_WORKSPACE_DIR`, and for extension scripts also `WINTER_EXT_DIR` /
`WINTER_EXT_PREFIX`), every lint script receives the resolved scope:

| Var                 | Meaning                                                                                                                                                                                                                                                                                           |
| ------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `WINTER_LINT_SCOPE` | The scope kind: `all`, `repo`, `env`, or `changed`.                                                                                                                                                                                                                                               |
| `WINTER_LINT_PATHS` | Newline-delimited absolute paths in scope. Under `changed` these are individual **files**; under `all` / `repo` / `env` they are **directory** roots. A check must `stat` each path and handle both.                                                                                              |
| `WINTER_CLI`        | Absolute path to the winter CLI that launched the run. A check may call back into it for workspace-wide data it can't derive from its own scope — e.g. `$WINTER_CLI graph --json` for the dependency graph — instead of rebuilding it. A check must **never** call `winter lint` (that recurses). |

**A check MUST confine itself to `WINTER_LINT_PATHS`.** `winter lint` runs every contributed script for every scope and
never filters by content — keeping a run "applicable to that scope" is the script's job. A check walks the given paths,
applies its rules only to files under them, and emits nothing for a scope whose content it doesn't recognize.

- **Do**: iterate `WINTER_LINT_PATHS`, walk each (a file is itself; a directory is recursed), match the files you own,
  stay silent otherwise.
- **Don't**: glob the whole workspace, read `$WINTER_WORKSPACE_DIR` wholesale, or use the current directory — that leaks
  findings outside the scope and silently breaks `--changed` and per-repo runs.

## Workspace lint check

The workspace contributes a lint script via a top-level field in `.winter/config.toml`, symmetric with the workspace
doctor probe:

```toml
lint = "context/project/lint.sh"             # single script
lint = ["context/project/lint.sh", "context/project/lint_docs.sh"]   # or a list
```

`lint` accepts a single path or a list; a bare string is coerced to a one-element list. It also accepts the
`[lint] scripts = [...]` table form, for the same reason `winter-ext.toml` does — see
[Declaring ignores alongside lint scripts](#declaring-ignores-alongside-lint-scripts). Paths are **relative to the
workspace root** and must point to executable files. They run first, before extension checks, with cwd at the workspace
root, and their findings appear under a `[project]` source group. Use them for checks this specific workspace owns.
Ecosystem-general checks meant to travel between workspaces belong in an installed extension instead (the `lint` field
below) — e.g. a dedicated `winter-lint` extension hosting the cross-cutting checks no single domain extension owns.

## Extension lint checks

Extensions opt in via the top-level `lint` field in `winter-ext.toml` (paths **relative to the extension directory**,
executable). Like the workspace field, it accepts a single path or a list — an extension that contributes several
distinct checks (say, one per convention) lists them all, and each runs as its own script. Each runs with cwd at the
workspace root and the scope env vars above; findings appear under the extension's `[<ext-prefix>]` source group.

A minimal check skeleton — walk the scope, match the files you own, emit one finding per violation, stay silent on the
rest:

```bash
#!/usr/bin/env bash
# Flag Markdown files that reference the harness with a bare relative path
# instead of the canonical `winter-context:` notation.
set -euo pipefail

emit() { printf '{"check":"path-notation","status":"%s","message":"%s","file":"%s","line":%s}\n' "$1" "$2" "$3" "$4"; }

while IFS= read -r path; do
  [ -z "$path" ] && continue
  # A directory root is recursed; a single changed file is checked directly.
  while IFS= read -r md; do
    while IFS=: read -r line _; do
      emit fail "use the \`winter-context:\` prefix" "$md" "$line"
    done < <(grep -nE '\.\./harness' "$md" || true)
  done < <(find "$path" -type f -name '*.md' 2>/dev/null)
done <<< "$WINTER_LINT_PATHS"
```

Exit non-zero only for the script's own failures — winter turns that into a synthetic `fail`. A clean run that found
nothing exits `0` and emits nothing.
