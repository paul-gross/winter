# `winter ws` patterns — scope and pattern vocabulary

Shared vocabulary for every `winter ws` command that takes a `PATTERNS`/`REPOS`/`SCOPE` argument — the four remote-sync commands (`fetch`, `pull`, `push`, `merge`), `connect`/`disconnect`, `update`, `status`/`diff`, and `destroy` — plus the top-level `provision` command, whose env-level pattern grammar is identical to `ws destroy`'s and so is hosted here rather than duplicated under `usage/provision.md` (its placement under `ws/` is deliberate, not a routing miss). Each command's own file ([fetch](./fetch.md), [pull](./pull.md), [push](./push.md), [merge](./merge.md), etc.) covers its own deltas; this file is the single source for `PATTERNS` grammar, scope flags, and pinned-scope rules. For the family, see the [`winter ws` hub](./index.md).

All four commands accept any number of segment-aware glob `PATTERNS` over `<env>/<repo>`. A bare env name is treated as `<env>/*`. Standalone repos are reached via `--standalone` / `--all` and ignore `PATTERNS` — to operate on a single standalone repo, use raw git. `merge` takes a required `SOURCE_REF` as its first positional, then patterns trail; the other three take patterns only.

`fetch` / `pull` / `push` default to `*/*` when no patterns are given (operate on every env's project worktrees). **`merge` is the exception**: it requires an explicit pattern whenever project worktrees are in scope — bare `winter ws merge <ref>` (and `winter ws merge <ref> --all`) are rejected, because silently folding `SOURCE_REF` into every worktree is rarely intended. Pass `'*/*'` to opt into that fan-out on purpose. The table rows below assume `merge` carries an explicit pattern; the `<cmd>` and `<cmd> --all` rows (no pattern) apply to `fetch` / `pull` / `push` only.

| Invocation | Operates on |
|------------|-------------|
| `winter ws <cmd>` | every env's project worktrees (not `merge` — see above) |
| `winter ws <cmd> alpha` | `alpha`'s project worktrees (== `alpha/*`) |
| `winter ws <cmd> alpha/winter` | one specific worktree |
| `winter ws <cmd> '*/winter'` | every env's `winter` worktree |
| `winter ws <cmd> 'alpha/*' 'beta/*'` | `alpha` + `beta` worktrees |
| `winter ws <cmd> --standalone` | every standalone repo (no project worktrees) |
| `winter ws <cmd> --all` | project worktrees + every standalone repo (`merge` needs an explicit pattern, e.g. `'*/*' --all`) |
| `winter ws <cmd> '*/winter' --all` | every env's `winter` worktree + every standalone repo |

Pinned-scope behavior per command:

| Command | _(default)_ | Opt-in / opt-out flags |
|---------|-------------|------------------------|
| `fetch` / `pull` | both | n/a — always include both |
| `push` | non-pinned only | `--include-pinned` (+ pinned), `--only-pinned` (pinned only) |
| `merge` | both | `--exclude-pinned` (non-pinned only), `--only-pinned` (pinned only) |

Mutex rules: pinned-scope flags are mutually exclusive within a command (`--include-pinned` xor `--only-pinned` for push; `--exclude-pinned` xor `--only-pinned` for merge); `--standalone` xor `--all`; `--standalone` rejects PATTERNS, and on `push`/`merge` also rejects the pinned-scope flags.

Pattern syntax: `*` matches any chars within a segment (does not cross `/`); `?` matches one char. Quote patterns in your shell to prevent expansion.

## `winter ws connect` / `winter ws disconnect`

`connect` and `disconnect` use the same segment-aware `<env>/<repo>` glob as
the four commands above, scoped to non-pinned project worktrees (pinned
worktrees are always skipped). Both require at least one `PATTERN` — there
is no implicit "all". `connect` takes a trailing `FEATURE_BRANCH` after its
patterns; `disconnect` takes patterns only.

```bash
winter ws connect alpha feature/x            # every non-pinned worktree in alpha
winter ws disconnect alpha                   # every non-pinned worktree in alpha
winter ws disconnect alpha/winter beta/api    # two specific worktrees
winter ws disconnect '*/winter'               # every env's winter worktree
```

## `winter ws update` — bare repo-name patterns

`update` operates on standalone repos, which aren't scoped to an env, so its
`REPOS` are **bare glob over repo names only** — same "no `/`" restriction as
`provision`/`ws destroy` below, but over standalone-repo names instead of env
names. `REPOS` is optional; an empty invocation re-pins every pinned
standalone.

| Invocation | Operates on |
|------------|-------------|
| `winter ws update` | every pinned standalone repo |
| `winter ws update my-lib` | just `my-lib` |
| `winter ws update my-lib other-lib` | `my-lib` + `other-lib` |
| `winter ws update 'winter-*'` | every pinned standalone repo whose name matches the glob |

A literal name that isn't a pinned standalone raises a clear error (unknown
name, or a known-but-unpinned name); a glob matching zero pinned standalones
is a no-op, not an error.

## `winter ws status` / `winter ws diff`

`status` and `diff` use the same segment-aware `<env>/<repo>` glob as the four
commands above, but scope only to project worktrees — neither has
`--standalone`/`--all` or a pinned-scope flag. Both default to `*/*` when no
patterns are given.

`diff` has no separate `--repo` filter — fold the repo into the pattern
instead of combining a bare env with a flag:

```bash
winter ws diff alpha/winter        # in place of: winter ws diff alpha --repo winter
```

A glob or multiple `PATTERNS` that resolve to more than one env produce
concatenated per-repo diff sections, one bold env header per env; a single
matched env renders with no env header.

## `winter provision` / `winter ws destroy` — env-level patterns

`provision` and `destroy` operate on whole feature environments, not
`<env>/<repo>` worktrees, so their `PATTERNS` are **bare env-name globs
only** — a `/`-qualified pattern (`alpha/winter`) is rejected with a clear
error rather than silently matching nothing. Both require at least one
`PATTERN` (no implicit "all").

| Invocation | Operates on |
|------------|-------------|
| `winter provision alpha` / `winter ws destroy alpha` | just `alpha` |
| `winter provision alpha beta` / `winter ws destroy alpha beta` | `alpha` + `beta`, in deterministic (sorted) order |
| `winter provision 'feature-*'` / `winter ws destroy 'feature-*'` | every env whose name matches the glob |

`ws destroy` is irreversible, so a glob or more than one `PATTERN` prints the
resolved env list and asks for confirmation before touching anything; a
single literal `PATTERN` destroys immediately with no prompt. `--force`
skips the confirmation (in addition to its existing meaning: bypass the
dirty-worktree check and force `git worktree remove`).

`provision`'s sub-target (`dependency` / `resource` / `data`) is a `--stage`
option, not a second positional — a bare positional there would be
ambiguous with the variadic `PATTERNS` list. Use `winter provision alpha
--stage resource` to run a single stage.
