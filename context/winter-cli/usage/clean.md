# `winter clean` — declared artifact removal

For the hub and the rest of the command surface, see [../index.md](../index.md).

```bash
# Full chain — runs dependency → resource → data in order
winter clean alpha

# Multiple envs / glob — PATTERNS is a bare env-name glob, at least one required
winter clean alpha beta                   # full chain, two envs, deterministic order
winter clean 'feature-*'                  # full chain, every env matching the glob

# Sub-targets — run one stage only, via --stage (not a second positional)
winter clean alpha --stage dependency     # clean the dependency sub-target only
winter clean alpha --stage resource       # clean the resource sub-target only
winter clean alpha --stage data           # clean the data sub-target only

# Named entry selector — target one named entry instead of a whole stage
winter clean alpha --name workspace.mydb  # clean just that entry

# Global flags
winter clean alpha --no-service-check     # skip the required_services check entirely
winter clean alpha --json                 # NDJSON event stream
winter clean alpha --dry-run              # print plan; no commands run, no services started
winter clean alpha --dry-run --json       # structured plan as NDJSON
```

**This is not `winter ws clean`.** [`winter ws clean`](./ws/clean.md) is a git-level `git clean -fd` over a worktree's
untracked files — it knows nothing about the provision manifest and removes whatever git considers untracked.
`winter clean` runs each matched `[[provision.*]]` handler's own declared `clean` command in its manifest entry
(configured per [configuration/provision.md](../configuration/provision.md)) — it touches only what that command names,
so it can remove build output, caches, or other artifacts a `.gitignore` entry would hide from `ws clean` entirely, and
it does nothing at all for a handler that declares no `clean`. The two are unrelated mechanisms; never assume one
implies the other.

`winter clean` exists for handing a feature environment from one tenant to the next: it returns the environment to a
state a fresh tenant can trust without destroying the expensive dependency trees a reinstall would have to rebuild. Only
each project knows which of its own artifacts are disposable, so the answer is declared per handler and winter fans out
over the declarations — a handler that declares nothing survives the run untouched.

That tenant-handover framing assumes an env-scoped handler (`feature-environment` or `feature-worktree`); it doesn't
hold for a `workspace`-scope handler's `clean` command. Scope resolves the same way it does for `winter provision` (see
[configuration/provision.md § Scope: working directory and environment](../configuration/provision.md#scope-working-directory-and-environment)):
a `workspace`-scope handler's cwd is the live workspace root, not `<env>/`, regardless of which `PATTERNS`/`ENV` you
named — so its `clean` command reaches shared, cross-env state, and running against more than one matched env runs that
same workspace-scope command once per env, not once total.

`PATTERNS` is a **bare env-name glob** — the same grammar `winter provision` uses (see
[patterns.md](./ws/patterns.md#winter-provision--winter-clean--winter-ws-destroy--env-level-patterns)) — a `/`-qualified
pattern is rejected, since clean operates on a whole env, not an `<env>/<repo>` worktree. At least one `PATTERN` is
required. The sub-target is selected with `--stage`, not a positional. Each matched env runs independently against the
same handler set; `--json` emits one `started`/`finished`-bracketed event stream per env, in the same NDJSON stream.

## What runs

`winter clean` runs the declared `clean` command for every handler that has one, across the same `dependency` →
`resource` → `data` chain (or a single explicit `--stage`) and the same within-sub-target ordering `winter provision`
uses. A handler declaring no `clean` contributes nothing and is not an error — the run simply skips it. A sub-target
where **nothing** declares `clean` reports the existing `no_handlers` event and starts no service, the same as an empty
sub-target does for `provision`.

Unlike `--reset` / `--destroy` on `winter provision`, `clean` is not an action flag layered on top of a required
`--stage` — it *is* the whole verb, so the full chain runs with no `--stage` given. `--stage` and `--name` are optional
modifiers that narrow an otherwise-complete fan-out, not a requirement to enable it.

**A failing clean does not stop the run.** Clean is best-effort: the rest of that sub-target's handlers and every
remaining sub-target still run, so one bad handler doesn't stop the others from being cleaned. Every failure is reported
on its own handler line, and the run's exit status is non-zero overall. No other run verb behaves this way — an apply
failure aborts every remaining sub-target, and a failing `--destroy` or `--reset` stops at the handler that failed.

## Confirmation and previewing

**`winter clean` has no confirmation prompt and no `--force` flag** — every invocation runs immediately against every
matched env. `--dry-run` is the operator's check in place of a prompt; preview with it every time the `PATTERNS` are not
ones you have run before. See
[worktree-ops.md § Verifying destructive commands safely](../../worktree-ops.md#verifying-destructive-commands-safely)
for why this is a deliberate exemption and how it compares to `provision --destroy`'s confinement.

`--dry-run` prints the ordered list of handlers whose `clean` command **would** run without executing anything or
starting any service — the same preview contract [`provision --dry-run`](./provision.md#--dry-run) has, resolved against
the `clean` action instead of `apply`/`destroy`/`reset`. A sub-target with no declared `clean` handlers is reported as a
no-op.

## Named entry selector (`--name`)

`--name <scope>.<name>` targets a single named `[[provision.*]]` entry (see
[configuration/provision.md#name-field](../configuration/provision.md#name-field)) — every sibling handler, including
others in the same sub-target, is skipped. `--stage` is optional when `--name` is given; when both are present, the
resolved handler must belong to the given `--stage` or the run aborts with a clear error before anything runs.

An explicit `--name` selector naming a handler with **no declared `clean`** produces a warn line rather than silently
doing nothing — the same treatment `--destroy` gives an explicitly named handler with no `destroy` command. This is the
one case where a clean-less handler is not simply filtered out: an explicitly named target that cannot act deserves a
line.

`<scope>` is the same short-token vocabulary `winter provision --name` uses:

| Selector token | Manifest `scope` value |
| -------------- | ---------------------- |
| `workspace`    | `workspace`            |
| `feature`      | `feature-environment`  |
| `worktree`     | `feature-worktree`     |

## Service check (`required_services`)

Same contract as [`provision`](./provision.md#service-check-required_services): a `resource` or `data` handler declaring
`required_services` has those services checked (and started if needed) before its `clean` command runs, unless
`--no-service-check` is given. The check runs over the handlers actually selected to run — a sub-target where nothing
declares `clean` starts no service, since the required-services check would otherwise start services for handlers that
will never execute.

## `--json` output

`--json` emits the same NDJSON event stream `winter provision --json` does — `IProvisionReporter` is shared between the
two verbs, so the event vocabulary, field shapes, and the `started` event's `action` key (here always `"clean"`) are one
table, not two. See [the `started` event table in `usage/provision.md`](./provision.md#--json-output) for the full event
list and field reference; nothing here duplicates it.

## Manifest schema

The `clean` field on a `[[provision.*]]` entry — string or list form, optional, validated the same way `destroy` and
`reset` are — is owned by [configuration/provision.md](../configuration/provision.md). This page covers running the
handlers it declares.

## Doctor probe

The same `winter doctor` `[provision]` probe that validates `apply`/`destroy`/`reset` also validates `clean` entries.
See [doctor.md](./doctor.md) for the full doctor probe contract.
