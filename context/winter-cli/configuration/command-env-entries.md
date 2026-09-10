# Command-valued env entries

A band entry in [`.winter/config.toml`](./ports-and-environments.md#env-var-bands) may be an inline table instead of a
string, declaring a command whose output contributes to the scope env — winter runs the command and ships no knowledge
of any secrets tool, so a workspace can source a value from Vault, AWS SSM, or any other store without winter depending
on, bundling, or naming it:

```toml
[env.feature.vars]
SECRET_TIER  = "dev"

# single-value: stdout, trimmed, becomes this key
DB_PASSWORD  = { command = "vals get ref+vault://secret/data/winter/${SECRET_TIER}/db#/password" }

# multi-value: stdout parsed per `format`, every key merged into scope
_aws         = { command = "chamber export myapp-${SECRET_TIER}", format = "json" }

DATABASE_URL = "postgresql://wts:${DB_PASSWORD}@localhost/wts"

[env.alpha.vars]
SECRET_TIER = "qa"
```

| Field     | Type                        | Default      | Meaning                                                                                                                                 |
| --------- | --------------------------- | ------------ | --------------------------------------------------------------------------------------------------------------------------------------- |
| `command` | string                      | — (required) | The command line to run. `${...}` tokens render first, using the grammar in [Token grammar](./ports-and-environments.md#token-grammar). |
| `format`  | `raw` \| `dotenv` \| `json` | `raw`        | How to interpret captured stdout — see below.                                                                                           |
| `shell`   | bool                        | `false`      | Run `command` through a shell instead of tokenizing it with `shlex.split`.                                                              |
| `exports` | string[]                    | none         | Allow-list restricting which of the command's output keys may be merged into scope.                                                     |

`format` controls how captured stdout becomes scope entries:

- `raw` (the default) — stdout, trimmed, becomes the value of the entry's own declared key.
- `dotenv` — stdout is `KEY=VALUE` lines (blank lines and `#`-comments skipped); every key merges into scope.
- `json` — stdout is a flat JSON object; every key's string form merges into scope.

Under `dotenv`/`json` the declared key (`_aws` above) is not one of the keys the output contributes — it only labels the
entry in diagnostics. Prefix it with `_` to mark it as an **anonymous import slot**; a `_`-prefixed key is dropped from
a command's output under every format and is never exported itself, whether the command ran or was [gated](#the-gate)
off, so a resolved `_aws` contributes only the keys its own output names. Because it is never claimed, a *reference* to
it is refused identically with the gate open or closed. A key in a command's output matching `WINTER_*` is always
dropped, too — winter's managed vars can never be overwritten this way.

A command entry **declared** under a `WINTER_*` key name is a different case entirely, and not a drop at all: winter
rejects it at config load, before any resolution runs, in every band that accepts a command entry
(`[env.workspace.vars]`, `[env.feature.vars]`, `[env.<name>.vars]`), regardless of whether the gate for the triggering
operation is open or closed:

```text
error: [env.feature.vars] key 'WINTER_SECRET' is a command entry declared under a `WINTER_*` name.
       ... declare 'WINTER_SECRET' as a plain string instead, or use a different key.
exit=1
```

This is a hard config-load failure (exit 1) — the workspace never starts up with a `WINTER_*`-named command entry
present at all, unlike every silent drop described below.

**These drops are silent for an *imported* key, by design — the same way a gated-off command entry never fails a
computation that would otherwise succeed (see the gate below).** A reference to a dropped imported key just behaves as
an undefined variable wherever it is read, which can be far from the entry that dropped it: a mis-spelled `exports`
entry surfaces as an "undefined reference" error on the reading line, not a warning at the command entry — check
`exports` first when a command's own output looks right but a value never shows up. `exports` filters the parsed output
before merging, regardless of `format`: for a `raw` entry this includes its own single declared key, so an `exports`
list omitting it drops that key too, even though it is the only value the entry produces — but unlike the imported-key
case, this one is not silent: a reference to a `raw` entry's own declared key that `exports` excludes is diagnosed by
name. For example, with `R = { command = "echo v", exports = ["OTHER"] }` and `Q = "q=${R}"`:

```text
env.feature.vars key 'Q': reference to 'R' can never be supplied — 'R' is excluded by
the command entry's `exports` allow-list.
```

**Execution.** The command runs with cwd at the workspace root, tokenized via `shlex.split` unless `shell = true`, with
a fixed 30-second wall-clock timeout that is not configurable (the field set above is closed). Its environment is
winter's own inherited environment overlaid with the *visible* scope at the point this entry resolves — the full
accumulated scope for a feature- or named-band entry, but only the
[restricted workspace-band view](./ports-and-environments.md#env-var-bands) for a workspace-band one — so those values
are also readable as ordinary env vars, alongside whatever winter's own process already had. `${...}` substitution
happens on the whole `command` string *before* tokenizing, not after — a substituted value that contains whitespace
splits into multiple `shlex` tokens rather than filling exactly one argv slot. Quote the reference in the declared
command (`vals get "${TOKEN}"`) to keep it one argument regardless of what the resolved value contains.

## Stdin and `/dev/tty` prompts

Stdin is closed (`/dev/null`), so a prompt that reads *stdin* (an unlock prompt, a passphrase read) gets immediate EOF —
that can fail the reading tool, but it can just as easily let the tool continue past the EOF and resolve the entry to an
empty value at exit 0, the same silent-wrong-value risk this page warns about elsewhere for a dropped key. A prompt that
instead opens `/dev/tty` directly — as `pinentry` does behind tools like `sops`, and as `op` does in some modes —
bypasses the closed stdin entirely. With no controlling terminal, as in CI, that open fails immediately (`ENXIO`) rather
than blocking; under `shell = true` a failed `/dev/tty` redirect can let the shell continue anyway, again landing on an
empty or truncated value at exit 0 rather than failing the command. With a terminal present, the prompt reaches it and
can succeed once answered — and an unanswered prompt on a real terminal is the case that actually consumes the full
30-second timeout. Neither prompt style reliably *fails*: the outcome to watch for is a silently empty or truncated
resolved value at exit 0, not a hang — the timeout is reached only by a command that genuinely runs long, including that
unanswered terminal prompt.

A non-zero exit, a timeout, a program that cannot be found or run, or output that fails to parse as the declared
`format` fails the operation outright, naming the entry's key, the exit code, and the command **as declared** — the
unrendered string the operator wrote, never the rendered line actually executed, at every failure site including the
error log winter writes: the rendered line can carry a value interpolated from a *different* entry's output, and winter
never reconstructs or echoes it. The failing command's own stderr is still surfaced verbatim, though, so a command that
prints its own arguments on error can leak an interpolated value that way; prefer the injected environment over an argv
slot for a value passed to a command prone to that. This does not degrade the way a missing pure-entry reference does
for `status` — a failing command always propagates.

An entry declared under `shell = true` may not reference a command-derived key in its `command` string (refused at
resolution time, naming the entry and the reference) — interpolating one command's output into another's shell command
string is not supported. A `shell = true` entry carrying a `${...}` reference of its own is also deferred until every
`format = "dotenv"`/`"json"` command entry in the band set has **resolved**, since any still-pending one could yet
overwrite a key it reads; this keeps the refusal's answer independent of which one resolves first. Still-deferred
multi-value `shell = true` entries are admitted in declaration-position order, each clearing the bar for the others, so
two of them never hold each other back forever. An entry with no `${...}` reference is never deferred — having nothing
to interpolate, it can never be refused.

That declaration-order clearing rule holds only *among* `shell` entries waiting on the same bar — it settles which of
several ready `shell` entries goes first, never whether one of them ultimately runs at all. The bar can be terminal: if
a `format = "dotenv"`/`"json"` command entry elsewhere in the band set never resolves — because it errors, or is itself
stuck in an unrelated cycle or undefined reference — every `shell = true` entry still waiting on that bar fails along
with it, even one whose own references are already satisfied. For example:

```toml
BASE = "b"
_m = { command = "echo K=${MISSING}", format = "dotenv" }
S  = { command = "echo ${BASE}", shell = true }
```

```text
env.feature.vars key 'S': a shell = true entry withheld for the whole run — its own references are
satisfied, but a format = "dotenv"/"json" command entry elsewhere in the band set never resolved,
so the shell refusal could never be decided.
```

That refusal covers only winter's `${...}` grammar, not the residual surface `shell = true` still accepts: a real shell
does its own `$NAME` expansion independent of `${...}`, so a declared `foo $DB_PASSWORD` reaches the shell with that
value already expanded in — out of the full accumulated scope for a feature- or named-band entry, but only the narrower
restricted view for a workspace-band one, since that is the only scope its process ever receives (see Execution above).

For how a key some entry declares directly resolves against a key a *different* entry's `dotenv`/`json` output only
emits — including a mid-resolution read gap that ordering leaves open — see
[Command-entry output collisions](./command-entry-collisions.md).

## The gate

Whether a command actually runs depends on the operation, not the entry:

| Operation                                                                                               | Command entries run?                                                                     |
| ------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------- |
| `winter service up`                                                                                     | Yes.                                                                                     |
| `winter service down`                                                                                   | No — teardown needs only ports and names.                                                |
| Status / readiness probing (`winter service status`, and the polling behind `winter service up --wait`) | No — a command runs once per scope when `up` resolves it, never once per readiness poll. |
| `winter env <scope>`                                                                                    | No, unless `--resolve` — see [usage/env.md](../usage/env.md).                            |

When a command does not run, a reference to the key it **declares** renders the placeholder (`<unresolved:command>`)
instead of failing, and the key itself prints as that placeholder too. This guarantee — gating a command off never fails
a computation that would otherwise succeed — holds only for the entry's own declared, non-`_`-prefixed key. A
`_`-prefixed slot is outside it entirely: referencing one is refused under either gate, so gating changes nothing there
(see above). It does **not** extend to a key a `dotenv`/`json` entry's output would *import*: that key is not known
until the command actually runs, so it is never placeholder'd while gated off. A pure entry referencing an imported key
(rather than the command entry's own declared key) sees a plain undefined reference and raises under the default gate
even though the identical config would succeed under `--resolve`. For example,
`_aws = { command = "...", format = "json" }` emitting `AWS_KEY`, referenced elsewhere as `DSN = "pg://${AWS_KEY}"`,
raises for `winter env alpha` and degrades `service down` / `status` (see below) but resolves under
`winter env alpha --resolve` or `service up`. Keep a value other entries must reference in a command's own `raw`
declared key, rather than behind a multi-value import, to avoid this gap. See [usage/env.md](../usage/env.md#masking)
for what the placeholder looks like and how to force resolution.

An undefined reference of this kind is fatal for `winter env` (exit 1). For `service down` and `service status`, it
degrades the same way any other band error does there — see
[contracts/service-orchestrator.md](../contracts/service-orchestrator.md#always-present-environment-variables) for what
a provider actually receives when that happens.

For one-line `command` recipes for common secrets tools (`vals`, `chamber`, `summon`, `teller`, `op`, `sops`), see the
[command-entry cookbook](./command-entry-cookbook.md).
