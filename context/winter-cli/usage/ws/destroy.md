# `winter ws destroy` — tear down one or more feature envs

For the rest of the family, see the [`winter ws` hub](./index.md). `winter ws destroy PATTERNS...` is the symmetric
counterpart to [`winter ws init ENV`](./init.md), fanned out across every env `PATTERNS` matches.

Each `PATTERN` is a **bare env-name glob** — destroy operates on whole envs, not `<env>/<repo>` worktrees, so a
`/`-qualified pattern is rejected. At least one `PATTERN` is required (no implicit "all"). See
[patterns.md](./patterns.md#winter-provision--winter-clean--winter-ws-destroy--env-level-patterns) for the shared
grammar with `winter provision` and `winter clean`.

```bash
winter ws destroy alpha              # one env, no prompt
winter ws destroy alpha beta         # multiple envs — prints the resolved list, asks to confirm
winter ws destroy 'feature-*'        # glob — prints the resolved list, asks to confirm
winter ws destroy alpha beta --force # skip the confirmation prompt (scripted use)
```

Because teardown is irreversible, a glob or more than one `PATTERN` prints the resolved env list and asks for
confirmation before doing anything; `--force` skips the prompt. A single literal `PATTERN` destroys immediately with no
prompt.

Per matched env, in order:

1. **Provision teardown** — runs `data --destroy` then `resource --destroy` (reverse of apply order) using the
   `[[provision.*]]` handlers declared in `.winter/config.toml` and extension manifests. Handlers without a declared
   `destroy` script warn and no-op without aborting structural teardown. Pass `--no-provision-teardown` to skip this
   phase entirely.
2. **Safety check** — refuses on missing env path or dirty worktrees (override with `--force`).
3. **Hooks** — fires every extension's `on_env_destroy` hook (mirror of `on_env_init`). With `--strict`, a non-zero hook
   exit aborts the teardown; without it, hook failures are logged and teardown proceeds.
4. **Worktree removal** — `git worktree remove` for every per-repo worktree.
5. **Env cleanup** — removes the env directory, strips the matching `# >>> winter-dir/<env>` block from the workspace's
   `.git/info/exclude`, and removes the env's index entry from `.winter/state.toml`.

A failure in any one matched env is reported and does not stop teardown of the remaining matched envs; the command exits
non-zero if any env failed.

Use `--dry-run` to preview the plan with no side effects — the provision teardown plan (which `destroy` scripts would
run) is emitted first, followed by the structural plan, per matched env. `--dry-run` never prompts for confirmation.

**`--strict` behaviour for provision teardown:** when a `destroy` script exits non-zero, `--strict` aborts the entire
teardown *before* removing worktrees or the env directory, preventing resources from being orphaned. Without `--strict`,
the failure is surfaced as an error (and the command exits non-zero) but structural removal proceeds.

**Prefer this over `rm -rf <env>/` + manual `git worktree remove`.** Manual removal bypasses provision teardown and
`on_env_destroy` hooks — extensions that need to clean up per-env state (tmux sessions, watchers, provisioned DBs, RMQ
vhosts, buckets) get skipped, leaving provisioned resources orphaned.

## `--json` action vocabulary

`winter ws destroy --json` emits NDJSON. The structural actions appear alongside any provision-teardown actions from the
same stream:

| `action`                         | Phase   | Meaning                                                                  |
| -------------------------------- | ------- | ------------------------------------------------------------------------ |
| `provision_teardown_started`     | 2a      | Provision teardown is beginning; `detail` is `data → resource`           |
| `provision_subtarget_started`    | 2a      | A teardown sub-target is starting                                        |
| `provision_no_handlers`          | 2a      | No handlers declared for a sub-target                                    |
| `provision_handler_done`         | 2a      | A teardown handler completed; `detail` is the action (`destroy`)         |
| `provision_handler_warn`         | 2a      | Handler skipped (no `destroy` script); `detail` is the warning message   |
| `provision_teardown_finished`    | 2a      | All teardown subtargets done; `detail` is `"ok"` or `"error"`            |
| `would_provision_teardown`       | dry-run | Handler that would run; `detail` is `destroy: <script>`                  |
| `worktree_removed`               | 4       | A per-repo worktree was removed                                          |
| `env_removed`                    | 5       | The env directory was removed                                            |
| `workspace_excludes_updated`     | 5       | The `# >>> winter-dir/<env>` block was stripped from `.git/info/exclude` |
| `would_remove_worktree`          | dry-run | Worktree that would be removed                                           |
| `would_remove_env`               | dry-run | Env directory that would be removed                                      |
| `would_remove_workspace_exclude` | dry-run | Exclude block that would be stripped                                     |
