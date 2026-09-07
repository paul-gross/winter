# Worktree Operations

Git commands for the polyrepo workspace topology. Paths in prose are given relative to the workspace root;
`<workspace-root>` below is a placeholder for that directory's own absolute path — see
[workspace-layout.md § Path notation](./workspace-layout.md#path-notation) for where it's registered. Resolve it from
`winter ws status --json`'s `root_path` field (see
[winter-cli/usage/ws/status.md](./winter-cli/usage/ws/status.md#json-schema-schema_version-1)), or a per-worktree path
from `winter ws worktrees --json`'s `path` field (see the `worktrees` row in
[winter-cli/usage/ws/index.md](./winter-cli/usage/ws/index.md)) — not `pwd`, which depends on the same untrustworthy cwd
this file warns against.

> **Tip:** For multi-repo setup and bulk operations, prefer `winter ws init` and the other `winter ws` commands over the
> raw git sequences below — the CLI is idempotent, reads the workspace config, handles pinned repos, and runs in
> parallel. See [winter-cli/index.md](./winter-cli/index.md) for the full command reference. The raw git commands here
> are still useful for single-repo work and for understanding what the CLI does under the hood.

## Anchor raw git commands with `-C`

Run every raw, state-mutating git command as `git -C <absolute-path> <verb> …`, resolving `<absolute-path>` from the
workspace layout (e.g. `<workspace-root>/<name>/<repo-name>` for a worktree, or `<workspace-root>/projects/<repo-name>`
for a source checkout) rather than trusting the shell's current directory — cwd is not trustworthy agent state: it
persists across some tool calls, resets across others, and rarely survives context summarization.

**Scope — must use `-C`:** every command that writes to the ref store, index, or config, whether or not it touches the
working tree: `clone`, `worktree add`/`remove`/`prune`, `add`, `commit`, `push`, `fetch`, `remote add`, `config`,
`checkout`, `rebase`, `merge`, `reset`, `clean`, `stash push`/`stash pop`, `branch`, … **Recommended**, not required (a
misread is not a mutation): the read-only commands `status`, `log`, `diff`.

Every fenced git command below follows this convention. An inline code span inside prose that narrates what a
`winter ws` command does internally — not meant to be copied and run — is exempt.

This convention has not yet been swept into `skills/*/SKILL.md`; a bare command there is a gap still to close, not a
counter-example to follow.

## Pinned repos

Some repos are **pinned** — they always track the remote main branch and never participate in feature branching. Declare
pinning by setting `pinned = true` on a `[[project_repository]]` entry in `workspace:/.winter/config.toml`. The main
branch comes from the entry's `main_branch` field, falling back to the top-level workspace-wide `main_branch`.

For how each `winter ws` command treats pinned repos — init upstream wiring, connect/disconnect skip, pull from
`origin/<main-branch>`, and push exclusion (`--include-pinned`/`--only-pinned`) — see the per-command leaves under
[winter-cli/usage/ws/](./winter-cli/usage/ws/index.md); the pinned-scope table in
[winter-cli/usage/ws/patterns.md](./winter-cli/usage/ws/patterns.md) is the cross-command summary.

## Cloning (source checkouts)

```bash
winter ws init
```

This reads `.winter/config.toml`, clones every declared repo that's missing into `projects/`, applies git identity,
writes git-exclude entries, and runs each repo's `cmd` list. Safe to re-run. It also git-excludes the runtime
`.winter/logs/` capture dir.

Raw equivalent for a single repo:

```bash
git -C <workspace-root> clone <repo-url> <workspace-root>/projects/<repo-name>
```

## Creating a feature environment

```bash
winter ws init <name>
```

This command:

- Creates the `./<name>/` directory.
- For each project repo, runs `git worktree add -b <name> <main-branch>`.
- Copies git identity into each worktree.
- Writes git-exclude entries.
- For pinned repos, wires the upstream to `origin/<main-branch>` — see [Pinned repos](#pinned-repos).
- For non-pinned repos that are **newly added** (worktree absent before this run) and have no upstream: if every
  non-pinned sibling worktree that already exists agrees on the same upstream, init connects the new worktree to that
  inferred ref (e.g. `origin/master` or `origin/<feature-branch>`). When siblings diverge or there is no connected
  sibling to infer from, the worktree is left unconnected — use `winter ws connect` explicitly in that case. See
  [Connecting a feature environment](#connecting-a-feature-environment-to-a-remote-feature-branch).
- Runs each repo's `cmd` list.
- Allocates and persists a stable index for `<name>` so runtime env injection (`WINTER_ENV`, `WINTER_ENV_INDEX`,
  `WINTER_PORT_BASE`, `WINTER_WORKSPACE_PORT_BASE`) is collision-free. No env file is written — env is injected at
  dispatch time. Inspect with `winter env <name>`.
- Runs every installed extension's `on_env_init` hook.

Greek letters (`alpha`, `beta`, …) are the convention. The first 10 (`alpha`…`kappa`) are the default `env_aliases` and
receive fixed port-offset indices; other names hash into a higher band. Any valid directory name is accepted.

After this runs, `winter ws init` is structural — it creates the worktrees, allocates the env index, and runs each
repo's `cmd` list as a lightweight trust/bootstrap step (e.g. `mise trust`, `direnv allow`), not full dependency
installation.

To bring the environment to a working state, run:

```bash
winter provision <name>
```

This installs dependencies, provisions resources (databases, queues, buckets), and loads seed data using
`[[provision.*]]` handlers declared in `.winter/config.toml` and installed extension `winter-ext.toml` files. See
[usage/provision.md](./winter-cli/usage/provision.md) for the full command reference. For any project-specific readiness
steps not yet migrated to `[[provision.*]]` handlers, also follow `workspace:/context/project/project-setup.md`.

Raw equivalent, per repo:

```bash
git -C <workspace-root>/projects/<repo-name> worktree add <workspace-root>/<name>/<repo-name> -b <name> <main-branch>
```

## Connecting a feature environment to a remote feature branch

```bash
winter ws connect <name> <feature-branch>             # every non-pinned worktree in the env
winter ws connect <name>/<repo> <feature-branch>       # just the matched worktree(s)
```

The trailing argument is the branch; everything before it is one or more segment-aware `<env>/<repo>` globs (a bare
`<name>` matches `<name>/*`), so a single `connect` can target the whole env or one repo. Sets `push.default=upstream`
and the upstream (`origin/<feature-branch>`) on each matched non-pinned worktree. The usual shape points every
non-pinned repo at the same remote feature branch, but repos in one env may carry independent branch names — `ws status`
/ `ws pull` / `ws push` each resolve each worktree's target per-worktree from its own tracking config, so a worktree you
re-point individually still works. (The env-wide `feature_branch` shown by `ws status` / the dashboard is read from the
first *connected* non-pinned repo, so that summary assumes the uniform case; the dashboard additionally appends a `+N`
suffix to flag how many other distinct remotes the env spans.) The remote branch is not created yet — that happens on
first push:

```bash
git -C <workspace-root>/<name>/<repo-name> push -u origin <name>:<feature-branch>
```

**If the recorded feature branch is empty when the user asks to push**, do not guess — ask the user which remote branch
they want to push to. Once they provide one, run `winter ws connect` before pushing.

**Before pushing**, ask the user: "Want me to run pre-release checks (lint, format, tests) on the changed repos before
pushing?" If a project repo documents pre-release checks in its `CONTRIBUTING.md` or `context/`, run them for every repo
with changes and fix any issues before pushing.

Pinned repos are skipped during connect/disconnect (no feature branch tracking to set/unset) and excluded from `push` by
default. See the [Pinned repos](#pinned-repos) section for how to include them.

**Shortcut for newly-added repos:** If you added a repo to `.winter/config.toml` and its env siblings already all share
the same upstream, re-running `winter ws init <env>` will auto-connect the new worktree to that inferred ref — no manual
`winter ws connect` needed. Manual connect is only required when siblings have divergent upstreams or there is no
connected sibling to infer from.

## Disconnecting a feature environment

```bash
winter ws disconnect <name>
```

Unsets upstream tracking on each non-pinned repo. With no upstream set, the env reads as disconnected.

## Pulling remote feature-branch commits

`winter ws pull <name>` integrates each worktree's own tracked upstream. For per-repo target resolution, integration
modes (`--ff-only`/`--merge`/`--rebase`/`--autostash`), and outcomes, see
[winter-cli/usage/ws/pull.md](./winter-cli/usage/ws/pull.md). If a repo diverges, resolve it with raw git in that
worktree.

## Destroying a feature environment

`winter ws destroy <name>` is the symmetric counterpart to env creation. For the teardown order, the
`--dry-run`/`--force`/`--strict`/`--no-provision-teardown` semantics, and why to prefer it over manual
`rm -rf <name>/` + `git worktree remove`, see [winter-cli/usage/ws/destroy.md](./winter-cli/usage/ws/destroy.md).

Raw equivalent, per repo (without provision teardown, hooks, or stripping the exclude block):

```bash
git -C <workspace-root>/projects/<repo-name> worktree remove <workspace-root>/<name>/<repo-name>
```

## Verifying destructive commands safely

`winter ws checkout`, `winter ws reset --hard`, `winter ws clean`, `winter ws destroy`, `winter ws restack`, and
`winter clean` mutate real worktrees or run project-declared removal commands, and none of them can be scoped narrower
than their own `PATTERNS`/`ENV`/chain argument — a wrong or missing pattern reaches every worktree or env it matches,
not just the one you meant to touch. `ws checkout` in particular has **no repo-scoping flag at all**: it always operates
env-wide (see [Adopting a remote feature branch](#adopting-a-remote-feature-branch) below); use
`ws reset <env>/<repo> REF` when you need to touch exactly one worktree. `ws restack` reaches every non-pinned project
repo across its whole chain at once and, unlike the others, rewrites history rather than moving a pointer — see
[winter-cli/usage/ws/restack.md](./winter-cli/usage/ws/restack.md) for the argument order, boundary derivation, and
conflict-resume contract. It also runs with **no confirmation prompt and no `--force`** — it takes only `--cut`,
`--dry-run`, and `--json`; do not assume a gate will stop a wrong invocation before it rewrites history. `winter clean`
is a distinct verb from `winter ws clean` — it runs each provision handler's own declared `clean` command rather than
`git clean -fd`; see [winter-cli/usage/clean.md](./winter-cli/usage/clean.md). For a `winter clean` run,
`PATTERNS`/`ENV` does not even bound a `workspace`-scope handler the way it bounds everything else in this list: that
scope resolves to the live workspace root with no per-env qualification, so the handler's `clean` command runs at the
shared workspace root — not inside the matched env's own directory — once for every env `PATTERNS` matches, not once
total.

**`winter clean` and `ws restack` are the ungated verbs in this group — no prompt, no `--force` — and `winter clean`
alone has no refusal guard either.** That's a deliberate exemption, not an oversight: `winter provision --destroy` is
likewise ungated. Neither gate rests on a guarantee winter enforces — a declared `clean` (or `destroy`) command is an
arbitrary shell string invoked via `sh -c`
([winter-cli/configuration/provision.md](./winter-cli/configuration/provision.md#command-execution-semantics)), so
winter neither knows nor checks what it removes; each handler's author is trusted to declare only what's safe to run
unprompted, and that authoring convention, not the verb itself, is what the missing prompt relies on. The two verbs do
differ in confinement: `provision --destroy` requires an explicit sub-target selector (`--stage` or `--name`), so one
invocation reaches at most one stage or one named entry per matched env; `winter clean` has no such confinement — a bare
invocation runs every matched handler across the whole `dependency → resource → data` chain. (Both fan out identically
across every env `PATTERNS` matches — that part isn't what distinguishes them.) `--dry-run` is the operator's check in
place of a prompt; preview with it every time the `PATTERNS` are not ones you have run before.

**`ws clean` and `winter clean` both need their own care, because the usual audit will not catch a bad one.** `checkout`
and `reset` move refs, so a mistake is normally recoverable from the reflog and detectable by checking where each branch
points. `ws clean` removes untracked files, and `winter clean` runs a handler's declared removal command, inside an env
you otherwise intend to keep: nothing restores what either deletes, and a branch-position audit sees nothing wrong.
(`ws destroy` is also irreversible — it drops databases and deletes the env — but it announces itself by taking the
whole env, where a clean leaves the env looking intact.) Preview with `--dry-run` every time the `PATTERNS` are not ones
you have run before.

**Never run or exercise a destructive `winter` command against a live env you don't intend to mutate.** This includes
via the `--winter=<path>`/`--service-orchestrator=<path>` core overrides: `--winter=<path>` selects which CLI *code*
runs — in-progress source instead of the installed build — and does not itself create a sandbox. The workspace a command
mutates is resolved from the working directory it's invoked in, override or not, so an override run from a live worktree
still targets that live workspace; only a scratch working directory (a throwaway env or a fully scratch workspace) keeps
a destructive verb off live worktrees. To verify destructive-command behavior:

- Build a throwaway env (`winter ws init <scratch-env>`) or a fully scratch workspace (its own config + throwaway git
  repos) and exercise the command there. A throwaway env only contains a `winter clean` run for `feature-environment`-
  and `feature-worktree`-scope handlers — a `workspace`-scope handler's `clean` command still runs at the live workspace
  root regardless of which env you named, so verifying one of those needs the fully scratch workspace, not just a
  throwaway env.
- When a throwaway env isn't practical, drive the underlying service classes directly against a scoped, disposable git
  repo instead of going through the live CLI.
- Prefer `--dry-run`/`--json` to preview a command's plan before running it for real — every destructive `ws` verb, and
  `winter clean`, reports the exact per-repo or per-handler effect with no side effects when it supports the flag,
  **except `ws restack`**: its `--dry-run` renders the plan alone — each link's boundary sha and source, per repo — and
  carries no per-repo outcome at all, because an upper link's outcome is unknowable before its predecessor actually
  moves. See [restack.md § `--dry-run`](./winter-cli/usage/ws/restack.md#--dry-run--the-plan-not-outcomes).
- Before finishing, audit every worktree you touched (or could have touched): branch attached where expected, and
  working tree clean unless intentionally left dirty. For every verb but `ws restack`, also confirm the commits you
  expect are present — not silently stranded off every ref. `ws restack` rewrites history in place, so a sha-presence
  check doesn't work there: `git rebase --onto` abandons the original commits by design, so it fails a healthy run and
  passes a bad one vacuously; use the subject-set comparison instead — see the `ws restack` post-run audit in
  `winter-context:/verification/winter.md`. After a `ws clean`, also confirm no untracked file you meant to keep is gone
  — read the per-path list the command prints, which is the only record of what it took. `winter ws status --json`
  reports a per-repo `untracked` count, but it counts one entry per file and cannot see an empty untracked directory, so
  it is a rough cross-check rather than a match. After a `winter clean`, the same rule applies to declared artifacts:
  nothing else records what a handler's `clean` command removed, so the per-handler run report — not a status snapshot —
  is the only audit trail.

This came out of a real incident: an agent reproducing a bug ran `winter ws checkout alpha master --force` through the
core-override against its own live `alpha` env; `ws checkout` has no repo-scoping flag, so it force-moved every worktree
in `alpha`, including the agent's own, and erased a completed, unpushed commit. `--force` bypasses the dirty/abandonment
safety gate entirely, so no guard in the CLI would have stopped that specific command — the operating rule above (verify
in a throwaway env or scratch workspace, never the live one) is what actually prevents a recurrence.

## Adopting a remote feature branch

`winter ws checkout <name> <feature-branch>` is an all-or-nothing connect + force-checkout across every non-pinned repo
— it re-attaches HEAD onto the env-named branch (moving it off whatever branch a worktree was parked on, or re-attaching
a detached one) and force-moves that branch to the target ref (use `--new` for a branch that doesn't exist anywhere
yet). It is env-wide with no repo filter. For the `refused-unknown-branch` / `refused-missing-ref` refusals, the
dirty/abandonment guard, and what `--force` does and doesn't bypass, see
[winter-cli/usage/ws/checkout.md](./winter-cli/usage/ws/checkout.md).

To move a single worktree's branch pointer without touching the rest of the env — or without re-attaching a detached
HEAD — use `winter ws reset <name>/<repo-name> <ref>` instead; see
[winter-cli/usage/ws/reset.md](./winter-cli/usage/ws/reset.md) for the soft/mixed/hard modes and safety-gate details.

## Pushing completed work

`winter ws push [<patterns>]` pushes each matched worktree to its own tracked upstream. For the pattern/scope
vocabulary, the per-repo target rule, pinned exclusion (`--include-pinned`/`--only-pinned`), and the
`N pinned repo(s) with commits skipped` signal, see [winter-cli/usage/ws/push.md](./winter-cli/usage/ws/push.md) and
[winter-cli/usage/ws/patterns.md](./winter-cli/usage/ws/patterns.md).

To push a single standalone repo, use raw git — patterns don't apply to standalone repos.
