# `winter lint` — convention checks

For the hub and the rest of the command surface, see [../index.md](../index.md).

```bash
winter lint                    # the feature env you're in (every env, if outside one)
winter lint <repo>             # one project repo by name
winter lint <env>              # every project worktree in a feature env
winter lint <repo-or-env> ...  # multiple repo/env names, verified in one run
winter lint '<glob>'           # every repo/env name matching the glob (e.g. 'winter-*')
winter lint --changed          # only the dirty / un-pushed files in the current repo
winter lint --all --json       # every env's project worktrees, as an NDJSON stream
```

Runs winter-ecosystem **convention** checks — path notation, agent frontmatter, module boundaries, and the like — as opposed to `winter doctor`, which checks workspace *materialization* (is this clone wired up correctly). The two are complementary: `doctor` answers "is the workspace healthy", `lint` answers "does the content follow winter's rules".

`winter lint` is a **dispatcher, not a checker** — it runs the built-in core checks bundled with winter-cli plus the lint scripts contributed by installed extensions (and an optional workspace-level one) over the selected scope, and aggregates their findings. It contains no check logic itself. The core checks always run, so even a workspace with no lint-contributing extension still gets module-extractability enforcement; a workspace with no contributed *scripts* lints only with the core checks. Each finding reports `pass`, `warn`, or `fail` with an optional `file:line` location and a remediation hint shown under failures. Exit code is `0` when nothing failed (warnings allowed), `1` if any check failed — usable in CI and pre-push.

**Scope** selects which content the checks run over (the resolved paths are handed to each check; the check decides which it recognizes). Lint only ever targets the **project repos we develop in feature environments** — never the workspace root (the governance layer, which references everything by design) nor the standalone extension clones under `.winter/ext/` (released products that linted clean before shipping):

- _(no argument, the default)_ — the feature env containing the current directory: every project worktree inside it. Run from outside any env (e.g. the workspace root), it falls back to every env's project worktrees.
- an **env name** — every project worktree directory inside that env.
- a **project-repo name** — that repo's source checkout. (Standalone-only names are rejected — standalone clones are out of scope.)
- **multiple names and/or a bare glob** (no `<env>/<repo>` segment) — pass any number of repo/env names, or a glob like `'winter-*'`, to lint exactly that set in one run. Each resolved name is checked and reported independently, same as if you'd run `winter lint` once per name.
- `--all` — every feature environment's project worktrees.
- `--changed` — files that are dirty or in un-pushed commits in the git repository containing the current directory. Run it from the repo or worktree you're about to push.

A name that matches both a repo and an env is rejected as ambiguous (checked per resolved name); scope names, `--all`, and `--changed` are mutually exclusive. A glob matching zero names is a no-op, not an error.

**Core checks** are built into winter-cli and always run; their findings appear under a `[core]` source group. There are three built-in core checks: **module extractability** (validates `<context>:/path` dependency direction across the ecosystem graph), **file-size** (guards agent-facing markdown files against configurable byte-size thresholds), and **required-services** (validates `required_services` entries in provision manifests against the merged service catalog from all bound providers). See [configuration/lint.md#built-in-core-checks](../configuration/lint.md#built-in-core-checks) for the full description of each. **Workspace checks** are contributed via a top-level `lint` field in `.winter/config.toml`; **extension checks** via the same field in an extension's `winter-ext.toml`. The contributed fields take a single script path or a list, so one source can contribute several distinct checks. All follow the same script contract as doctor probes, plus the scope env vars — see [configuration/lint.md](../configuration/lint.md). Each check also receives `WINTER_CLI`, the path to the running CLI, so it can call back for workspace-wide data it can't derive from its own scope — see [graph.md](./graph.md).

`--json` emits one NDJSON object per line: `{"type": "started", "scope": ..., "label": ..., "paths": [...]}` once per resolved scope, `{"type": "finding", "source": ..., "check": ..., "status": ..., "message": ..., "file": ..., "line": ..., "remediation": ...}` per finding, then `{"type": "finished", "contributors": N, "total": N, "fails": N, "warns": N}` once per resolved scope. `contributors` is the number of lint scripts that ran — `0` means nothing was contributed. Multiple names/a glob fan out to one `started`/`finished` pair per resolved name, each with its own findings in between — a single-target run emits exactly one pair.
