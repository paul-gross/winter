# `winter ws restack` — rebase a stacked chain of envs

For the rest of the family, see the [`winter ws` hub](./index.md).

`winter ws restack ENV... BASE [--cut ENV_OR_REF] [--dry-run] [--json]` maintains a **live stack** of feature
environments after the operator rewrites something low in it — appended commits, amended history, or a local squash. It
rebases across every non-pinned project repo, the same reach as `checkout` / `reset` / `clean`. Every refusal is
pre-flight and all-or-nothing — no link executes anywhere until every one clears — but that guarantee doesn't extend to
execution itself: see [Conflict, stop, and resume](#conflict-stop-and-resume) for what a mid-run conflict leaves
partially done.

**Exit code.** A refusal and a mid-run conflict both exit 1. `--dry-run` and a run that completes the whole plan without
conflict both exit 0 — `--dry-run` exits 0 whenever the plan itself is unrefused, independent of what the plan would go
on to do.

## Argument order and the locally-existing chain

Envs are named **top of stack first, descending**, with `BASE` trailing. Every adjacent pair is one **link** — the left
element rebased onto the right, its predecessor:

```bash
winter ws restack env2 master              # one link: env2 onto master
winter ws restack env3 env2 master         # two links: env3 onto env2, env2 onto master
```

Every `ENV` is a **literal env name, never a glob and never `<env>/<repo>`-scoped**. Only `BASE` and `--cut` may be
arbitrary refs, resolved per repo (see [ref tokens](./ref-tokens.md)). This ordered-literal-chain shape is `restack`'s
declared exception to the family's `PATTERNS` grammar — see
[patterns.md](./patterns.md#winter-ws-restack--ordered-literal-chain-not-patterns) for why.

Every chain member must already exist locally, in the same sense `checkout` / `reset` require: restack reads each
element's own worktree branch and never infers parentage from anything else. An element that resolves in **no**
participating repo refuses the whole run (`refused-missing-ref`) rather than silently planning `skipped` everywhere and
exiting 0 having done nothing.

## Execution runs base-ward first

Links **execute base-ward first** — the bottom link (the one rebasing onto `BASE`) runs before the one above it, the
reverse of the argument order — so each env replays onto its predecessor's already-restacked tip rather than onto a tip
this same run is about to invalidate.

## The boundary and its provenance

Each link's boundary is derived from git **before anything moves** and reported with its source. The default is
`git merge-base --fork-point <predecessor> <env>`, which reads the predecessor's reflog — the record of where it used to
be, which a local rewrite erases from the graph but leaves in the reflog — so only the env's own commits replay. Both
the boundary sha and its source (`fork-point` or `cut`) render per repo, per link, in text and in `--json`.

A link with **no fork point** refuses the whole run (`refused-unknown-boundary`), naming the link and repos. Recourse is
`--cut` for the bottom-most link; for any link above it, there is no in-command recourse — fall back to raw git in that
repo.

## `--cut` and its per-repo guard

`--cut ENV_OR_REF` supplies the boundary for the **bottom-most link only**, for when ancestry to `BASE` is severed
because that env's own predecessor was squash-merged onto the base by a forge. The cut ref is never itself rebased —
only its tip is read, per repo, like `BASE`. It is not repeatable: the cut is the chain's floor, so cutting at the
higher of two landed envs already excludes both. It also cannot repeat any chain element or `BASE` — a repeated ref
refuses `refused-inverted-order` before the cut guard ever runs.

The guard classifies each participating repo of the bottom link independently, never once for the whole run:

| Class              | Test in that repo                                                   | Consequence                |
| ------------------ | ------------------------------------------------------------------- | -------------------------- |
| `cut applies`      | the cut's tip **is** an ancestor of the bottom env's branch         | replays the frozen range   |
| `cut already done` | the cut's tip is **not** an ancestor, and `BASE`'s tip **is**       | `up-to-date`, no movement  |
| neither            | neither the cut's tip nor `BASE`'s tip is an ancestor of the branch | `refused-cut-not-ancestor` |

A cut that fails to resolve in a participating repo of the bottom link — one where both the bottom env's branch and
`BASE` already resolve — refuses `refused-missing-ref` rather than reporting `skipped`, which would leave one repo
un-restacked while its siblings moved.

**Declared limit.** `cut already done` is decided from ancestry alone, never from content: an operator who already
rebased the env onto `BASE` by hand and resolved every conflict there gets `up-to-date` and no movement, but if that
manual rebase landed as a squash rather than a clean replay, the duplicated commits stay in the branch and `--cut` has
no way to detect or warn about it.

## Outcomes

Execution classifies each participating repo of each link as one of three:

| Outcome      | Condition                                                                                                                                                                                      |
| ------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `up-to-date` | the predecessor's execution-time tip is already an ancestor of the env branch, **and** the repo isn't a still-pending `cut applies` case (below) — also how a completed link reads on a re-run |
| `rebased`    | the env's own commits replayed onto the predecessor's new tip — including the zero-commit case, where an env carrying nothing past its boundary fast-forwards to that tip                      |
| `conflict`   | the replay stopped; the repo is left mid-rebase and the run ends                                                                                                                               |

The `cut applies` exemption is gated by [`--cut` and its per-repo guard](#--cut-and-its-per-repo-guard) above: only a
cut-sourced boundary can hold `up-to-date` back once the predecessor's tip already qualifies, and only until the frozen
cut range is actually replayed.

A repo that never participates (its env branch, or the link's predecessor, doesn't resolve there) renders `skipped` and
is not an exit-code input.

## Refusal vocabulary

Every refusal below is pre-flight and all-or-nothing: no link executes anywhere until every one clears.

- `refused-missing-ref` — a chain element or `BASE` resolves in no participating repo, or `--cut` fails to resolve in a
  participating repo of the bottom link.
- `refused-dirty` — staged or unstaged changes in a participating worktree. Untracked files alone do not refuse.
- `refused-rebase-in-progress` — a participating worktree already mid-rebase; restack neither continues nor aborts a
  rebase it did not start.
- `refused-detached-head` — a participating worktree whose HEAD is detached or parked on a branch other than its env
  branch.
- `refused-inverted-order` — an element (including the `--cut` ref) repeats, or a link is inverted at the chain level:
  the env carries commits past its boundary in every participating repo, and its tip is a proper ancestor of the
  predecessor's in every one of them. See the note below — this check does not catch every backwards chain.
- `refused-unknown-boundary` — a link's fork-point probe came back empty; recourse is `--cut` for the bottom link,
  otherwise raw git for that repo.
- `refused-cut-not-ancestor` — `--cut`'s third per-repo class (above).

### A chain typed backwards is not refused

**Declared limit.** Naming an env above the env it actually branched from does **not** trip `refused-inverted-order` in
either of two shapes, and both run to completion reporting `rebased`:

- When the element named on top **has not advanced** since the other branched from it, the frozen boundary sits at that
  element's own tip, so it carries no commits past its boundary anywhere — every repo is exempt from the inversion check
  — and the link runs as a fast-forward. The whole chain collapses into a single branch.
- When it **has advanced**, the exemption above no longer holds, but neither does the ancestry half the refusal also
  needs (the element's tip is no longer an ancestor of the predecessor's), so the chain still runs unrefused — this time
  replaying that element's own post-branch commits onto the other env instead of collapsing.

Either way the report reads `rebased`, not a refusal — an operator who mistypes a chain recognizes what happened from
that report, not from a refusal. Recovery is raw git: the affected branch's own reflog names where it pointed before the
run.

## Conflict, stop, and resume

A conflict stops the run and leaves that repo mid-rebase; no repo past it in that link, and no upper link, is ever
touched. The report carries every repo that finished, the conflict detail (replayed commit, conflicted paths), and the
remaining plan.

Resolve the conflict in that worktree with `git rebase --continue`, then **re-run this same command — the same chain, in
the same order, with the same `--cut`** — to continue. A completed link reads `up-to-date` on that re-run and the run
picks up from where it stopped; a `--cut` run in particular relies on the same `--cut` value so a repo that already
completed classifies `cut already done` instead of replaying the frozen range again.

## `--dry-run` — the plan, not outcomes

`--dry-run` renders **the plan alone**: every non-pinned repo, one link at a time in execution order, with the boundary
sha and source the plan froze for it. It carries no per-repo outcome — an upper link's outcome is unknowable before its
predecessor actually moves — and makes zero write calls at the repo seam. A repo the plan didn't freeze a boundary for
(no `boundary`/`source`, rendered `-` in the text table and `null` in `--json`) is the one that will read `skipped` when
the chain actually runs — the plan carries no separate participation field, boundary absence *is* the participation
signal. Use `--dry-run` to preview a chain before running it for real; the
[destructive-command verification guidance](../../../worktree-ops.md#verifying-destructive-commands-safely) applies.

## `--json` output

`--json` prints a single JSON document to stdout — never NDJSON — and the document has **no discriminator field**, so a
consumer has to infer the shape from which of the three run paths produced it:

| Path                      | Top-level keys                                | Rendered by       |
| ------------------------- | --------------------------------------------- | ----------------- |
| refused (pre-flight)      | `refusals`                                    | `_render_refusal` |
| `--dry-run`, unrefused    | `links`                                       | `_render_plan`    |
| executed (no `--dry-run`) | `links`, `completed`, `conflict`, `remaining` | `_render_report`  |

`refusals[]` entries carry `result` (one of the [refusal vocabulary](#refusal-vocabulary) values), `env`, `predecessor`,
`repos` (empty for a chain-shape refusal like a repeated element, populated for a per-repo one), and `element` — `null`
for every code but `refused-missing-ref`, where it names which of that code's three triggers fired: the chain element
itself, the trailing base, or the literal string `--cut`.

`links[]` entries carry `env`, `predecessor`, and `repos[]`; each `repos[]` entry carries `repo_name`, `boundary`,
`source`. On the executed shape only, each `repos[]` entry also carries `outcome`, taking the values from the
[Outcomes](#outcomes) table plus two the table doesn't carry:

- `"skipped"` — the repo doesn't participate in that link (its env branch, or the link's predecessor, doesn't resolve
  there).
- `null` — the repo participates, but the run never reached it: a link above the one the run stopped on with a conflict,
  or a sibling repo of the stopped link itself that was ordered after the conflicting repo and so was never reached
  either.

`completed[]` — executed shape only — lists every repo that finished cleanly (`up-to-date` or `rebased`), each entry
carrying `env`, `repo_name`, `result`. `conflict` is `null` on a run that finished the whole plan, otherwise an object
carrying `env`, `predecessor`, `repo_name`, `replayed_commit`, `conflicted_paths`. `remaining` — the unexecuted tail of
the plan, starting at the link the run stopped on, shaped like the `--dry-run` document (`links`, plus an always-empty
`refusals`) — is **`--json`-only**: the text report has no separate rendering for the unfinished tail. It renders every
link, reached or not, through the same per-repo table `--dry-run` prints plus an `OUTCOME` column — a repo the run never
got to shows `-` there (or `skipped`, if it never participated), the same way `remaining`'s `null` reads in `--json`.

## Publishing a restacked env

Restack never pushes. Once a chain is rebased, publish each env with raw git per repo — force-pushing a rebased branch
is not something [`winter ws push`](./push.md) does; it carries no force option at all.
