# winter-lint — module extractability

`extractability.py` is a `winter lint` check: it verifies that every winter
module references only what it is guaranteed to have when shipped standalone.

A winter module (anything with a `winter-ext.toml`) is developed inside this
multi-repo workspace but installed on its own elsewhere. So an outbound
reference to a sibling that isn't declared as a dependency is a dead pointer at
the consumption edge, and a core module pointing at an extension is a layering
inversion. This check catches both.

## What counts as a reference

- A path-notation reference `<context>:/path` (e.g. `winter-product:/ai/todos.md`). <!-- winter-lint:example -->
- A Claude `@import` whose target escapes the module's own directory.

## Rules

For a reference in module `M` pointing at context `T`:

| Case | Result |
|------|--------|
| `T` is `M` itself | allowed |
| `T` is core (`winter`, `winter-cli`, `workspace`) | allowed |
| `T` is listed in `M`'s `winter-ext.toml` `requires` | allowed |
| `M` is core and `T` is an extension | **fail** — layering violation |
| `T` is a sibling not in `requires` | **fail** — undeclared dependency |
| `T` is unknown / not installed | **fail** |

The `requires` graph must also be **acyclic** — a cycle is a fail.

## Functional vs. illustrative

Every reference is a real dependency *by default*. A reference that only
illustrates the notation (a conventions doc citing another module as an
example, not depending on it) is exempted with a same-line marker:

```markdown
See `winter-service-tmux:/plugin.py` for a reference plugin. <!-- winter-lint:example -->
```

The marker exempts every reference on its line from the undeclared-sibling and
layering rules.

References inside a fenced code block (```` ``` ```` or `~~~`) are skipped
entirely — code fences hold illustrative literals (sample commands, example
prompts), not live dependencies, and can't carry an inline HTML-comment marker
without corrupting the sample.

## Remediating a failure

When the check fails on a reference, fix it in this order — most-correct first:

1. **It's a real dependency** → declare the target in the owner module's
   `winter-ext.toml` `requires`. **This is a maintainer decision, not an
   agent's:** a `requires` edge commits the module to needing that dependency
   wherever it ships, so an agent must get explicit user sign-off before adding
   one — it may *propose* the edge, never add it unilaterally.
2. **It's purely illustrative** — a doc citing another module to show the
   notation, not to depend on it → mark the line `<!-- winter-lint:example -->`.
   An agent may do this on its own.
3. **The reference is wrong or unwanted** → remove or rephrase it. Last resort,
   only when the reference shouldn't exist at all — don't reach for deletion to
   silence the check when (1) or (2) is the honest fix.

A **core** module (`winter` / `winter-cli` / `workspace`) can't use option 1 —
core must not depend on an extension — so its only honest fixes are (2) or (3).

There is no first-class *optional dependency* tier yet: an optional integration
(a module that references another only when it happens to be installed) is
declared in `requires` today, the same as a hard dependency, with a comment
noting it's optional. The same sign-off rule applies — only a maintainer adds
the edge.

## How it gets the graph

The check is graph-driven. It does not rebuild the ecosystem graph itself — it
calls back into the CLI that launched it: `$WINTER_CLI graph --json` returns the
`{module: [requires...]}` adjacency map, used for the known-module set and cycle
detection. A module's own `requires` is read from the local `winter-ext.toml` of
the checkout being linted.

**`WINTER_CLI` is required.** If it is unset the check fails loudly — there is no
graph-less fallback. A lint script may call `winter graph` but must never call
`winter lint` (that would recurse).

## Implementation shape

`extractability.py` is organized into four service classes, each injected with
collaborators at construction time:

- **`GraphClient`** — wraps the `$WINTER_CLI graph --json` subprocess call.
  Constructed with the CLI path; exposes `fetch_graph(cwd)`.
- **`ManifestReader`** — reads `winter-ext.toml` manifests. Exposes
  `module_name`, `module_requires`, and `owning_module` (walks ancestor dirs to
  find the nearest manifest).
- **`ReferenceScanner`** — scans markdown content. Exposes `references_in_line`
  (path-notation refs), `import_target_module` (@import resolution), and
  `collect_md_files` (directory walker).
- **`ExtractabilityLint`** — orchestrates the full check. Constructed with the
  three collaborators above; exposes `check_paths` (validates a list of paths
  against a graph) and `cycle_findings` (detects `requires` cycles in the graph).

`main()` is the composition root: it reads env vars, constructs all four
services, wires them together, calls `check_paths` and `cycle_findings`, and
prints NDJSON findings on stdout.

## How it runs

It is a **built-in core lint check** — `winter lint` runs it on every
invocation, with no `.winter/config.toml` registration, the same way
`winter doctor` runs its built-in core probes. The CLI's `CoreLintService`
locates this script relative to the winter-cli source tree (its sibling
`tools/winter-lint/` directory), runs it over the selected scope with the
standard lint env (`WINTER_LINT_SCOPE`, `WINTER_LINT_PATHS`, `WINTER_CLI`, …),
and aggregates its NDJSON findings under the `[core]` source group. See
`workspace:/ai/winter-cli/setup.md` ("Built-in core checks").

It can also be run directly:

```bash
WINTER_CLI=$(command -v winter) python3 extractability.py <path>...
```

## Tests

```bash
python3 -m unittest test_extractability
```

Stdlib `unittest` only — no third-party dependency, so the whole directory can
later move into a dedicated `winter-lint` extension intact.

**Testing-standard carve-out:** `test_extractability.py` uses `unittest.TestCase`
rather than plain pytest functions. This is a documented exception to the
`winter-harness:/standards/testing.md` pytest-only rule. The rationale: this tool <!-- winter-lint:example -->
directory is intentionally stdlib-only so it can ship standalone without `pytest`
as an install dependency. If the tool ever gains a proper `pyproject.toml`, the
tests should be migrated to pytest at that point.
