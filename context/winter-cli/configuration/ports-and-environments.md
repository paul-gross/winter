# Ports & environments

Winter assigns each feature environment a port band derived from its index, and computes per-env derived variables at
runtime. These keys live in `.winter/config.toml`.

## Port allocation

```toml
# Port allocation — all four keys are optional; shown here with their defaults.
base_port = 4000          # start of this workspace's port band; set a different value to separate co-located workspaces
ports_per_env = 20        # ports per feature env; per-env base = base_port + index * ports_per_env
env_aliases = [           # fixed-index env names (1..N); aliases get stable slots, all other names hash into the remainder
  "alpha", "beta", "gamma", "delta", "epsilon",
  "zeta", "eta", "theta", "iota", "kappa",
]
envs_per_workspace = 48   # max feature-env index (1..envs_per_workspace); must be >= len(env_aliases) + 2
```

## Env var bands

Three kinds of scope-bound band can be declared in `.winter/config.toml`. A band entry is either a string — supporting
`${...}` substitution, with literal text passing through unchanged — or an inline table declaring a `command` whose
output contributes to the scope env, for sourcing a value winter cannot compute itself, such as a secret held in an
external store — see [Command-valued entries](./command-env-entries.md). These variables are computed at runtime by
`EnvProvisionerService` and `EnvBandResolverService`, and injected into every provider subprocess by `winter service`.
To inspect the computed values for a scope, use `winter env <scope>` (see [usage/env.md](../usage/env.md)).

```toml
[env.workspace.vars]
SHARED_DB_PORT = "${WINTER_WORKSPACE_PORT_BASE+10}"   # shared workspace service

[env.feature.vars]
WTS_WEB_PORT = "${WINTER_PORT_BASE+10}"
WTS_API_PORT = "${WINTER_PORT_BASE+11}"
WTS_DB_PORT  = "${WINTER_PORT_BASE+12}"
DATABASE_URL = "postgresql://wts:wts@localhost:${WTS_DB_PORT}/wts-${WINTER_ENV}"  # reuses WTS_DB_PORT and WINTER_ENV

[env.alpha.vars]
WTS_WEB_PORT = "8421"   # alpha only — every other env keeps the feature-band value
```

**Workspace band (`[env.workspace.vars]`)** — rendered for both the `workspace` scope and every feature env. Because
`WINTER_PORT_BASE` is omitted from the workspace scope, workspace-band entries that reference a port should use
`${WINTER_WORKSPACE_PORT_BASE+N}`. A workspace-band entry also only ever sees other workspace-band keys plus the base
vars (minus `WINTER_PORT_BASE`) — never a feature- or named-band key — so it resolves identically whether the scope
being computed is `workspace` or a feature env. Visibility is keyed off which band *declared* the key a workspace-band
entry reads, not off whoever ends up winning it: if a feature-band [command entry](./command-env-entries.md)'s output
later overrides that same key for every other reader, the workspace-band entry is unaffected and still resolves against
its own band's declared value.

**Feature band (`[env.feature.vars]`)** — rendered only for feature envs; never emitted for the `workspace` scope.

**Per-env band (`[env.<name>.vars]`)** — rendered only for the one feature env it names, on top of the feature band. Use
it to point a single env at a fixed endpoint or a different backing service while its siblings keep the derived per-env
value. `workspace` and `feature` are band names, so an env named either cannot carry a per-env band. A band naming an
env that does not exist is inert — never looked up, never an error — so an override can outlive the env it was written
for. `winter doctor` warns (never fails) when `<name>` matches no env known to the registry or on disk, so a typo is
distinguishable from an intentionally dormant band — see [usage/doctor.md](../usage/doctor.md).

**Resolution per scope:**

| Scope       | Variables emitted                                                                                                                                                                                                                                                     |
| ----------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `workspace` | `WINTER_ENV`, `WINTER_ENV_INDEX`, `WINTER_WORKSPACE_PORT_BASE`, `WINTER_SERVICE_PREFIX` + workspace-band entries only (per-env bands are never rendered here)                                                                                                         |
| `<feature>` | `WINTER_ENV`, `WINTER_ENV_INDEX`, `WINTER_PORT_BASE`, `WINTER_WORKSPACE_PORT_BASE`, `WINTER_SERVICE_PREFIX` + workspace band rendered first, then feature band, then `[env.<name>.vars]` for that env last (each layer wins key collisions against the ones below it) |

An entry may reference any other band entry's key regardless of which band declared it or where it sits in that band's
TOML table — including a per-env entry's, which overrides it — because resolution runs to a fixpoint rather than in band
order (see [Token grammar](#token-grammar) below). This guarantee has bounds, not a single exception: the workspace
band's own restricted visibility, described above, keeps a workspace-band entry from ever seeing a feature- or
named-band key, and a [command entry](./command-env-entries.md)'s key carries further qualifications of its own — which
call sites even run it, whether a reference targets its declared key versus a key it only imports, and what the `shell`
opt-in narrows — covered there rather than restated here.

**Precedence with the local overlay.** `.winter/config.local.toml` merges into the committed config before bands are
resolved (see [config-files.md](./config-files.md#local-overlay-winterconfiglocaltoml)), so the effective order lowest
to highest is `[env.feature.vars]` < `config.local.toml` feature overlay < `[env.<name>.vars]` — a per-env band wins
over a locally-overlaid feature band.

**Migration from `[env.vars]` (hard break).** A config that still declares the legacy `[env.vars]` table is rejected
with a `ConfigError` at startup. Migrate by moving entries to `[env.feature.vars]` (for feature-env variables) or
`[env.workspace.vars]` (for shared workspace variables). There is no alias or fallback — the failure is intentional so
no variables are silently dropped.

### Token grammar

Two forms are supported:

- `${NAME}` — substitutes the string value of `NAME`.
- `${NAME+N}` — adds a non-negative integer `N` to `NAME` (which must parse as an integer).

`NAME` resolves against an **accumulating scope**, seeded with the base vars available for the scope (see the table
above). A band entry may declare a key that shares a base var's name, and its value then wins for every other entry that
reads it, regardless of where either sits in the TOML — the seeded value is withheld until the declaring entry resolves,
so no entry ever renders against the base value the declaration is about to replace. The one reader exempt from that is
the declaring entry itself, which may reference the name it redeclares
(`WINTER_SERVICE_PREFIX =
"${WINTER_SERVICE_PREFIX}-suffix"` extends the base value rather than forming a cycle).
`WINTER_PORT_BASE` stays outside a workspace-band entry's view at every scope, redeclared or not. Resolution runs to a
**fixpoint**: winter repeatedly renders every entry whose references are already resolved — regardless of which band
declared it or where it sits within that band's TOML table — accumulating into scope, until a full round resolves
nothing further. An entry whose reference is not yet resolved is deferred rather than raising, and is retried next
round. This is what lets a feature-band entry reference a value the per-env band overrides afterwards, and a pure entry
reference a key a [command entry](./command-env-entries.md) contributes. `WINTER_PORT_BASE` is not special:
`${WINTER_PORT_BASE+N}` is just the base-var case.

Precedence is resolved up front, before any rendering: a key declared in more than one band keeps only the
highest-precedence declaration (workspace < feature < named) — the lower one is dropped entirely and, if it is a command
entry, never run. For two entries that both *declare* the same key outright, which one wins is decided entirely by that
precedence rule; the fixpoint only decides *when*, within a round, a ready entry renders — never which of two such
colliding declarations survives. A collision between a declared key and a key a *different* entry's command output only
emits — or between two commands' output keys — is a narrower case band precedence alone does not settle; see
[Command-entry output collisions](./command-entry-collisions.md) for that rule.

If a full round makes no further progress and entries remain unresolved, resolution raises, naming every such entry and
why it could not be resolved. For an ordinary `${...}` reference this distinguishes a reference nothing could ever
supply (an **undefined variable**, including one permanently out of reach because of the workspace band's restricted
visibility) from one genuinely part of a **reference cycle**, from one merely **blocked** downstream of one of those two
(whose own line carries the real cause). Not every failure here is reference-shaped, though: a `shell = true` entry
withheld by the [multi-value resolution bar](./command-env-entries.md) names the unresolved command entry holding it up
rather than a specific reference, since its own references may already be satisfied.

Resolution is computed at dispatch time by `EnvProvisionerService` and `EnvBandResolverService` — concrete values are
injected into the subprocess environment. A non-integer `+N` offset, or any other malformed `${...}` token, is also a
fatal error, surfaced the same way.

A band entry may also be a [command entry](./command-env-entries.md) instead of a `${...}` template — an inline table
that sources its value (or several) by running a command, gated per call site and resolved to the same fixpoint above.

## Index reservation

The env name → index mapping itself is recorded in [`.winter/state.toml`](./config-files.md#state-registry). Two indices
are reserved and never assigned to a regular feature env:

Index 0 (`base_port`..`base_port+ports_per_env-1`) is the **workspace shared-service band**, exposed to providers as
`WINTER_WORKSPACE_PORT_BASE`. It is never assigned to a feature env — feature envs start at index 1 — so
workspace-scoped services (shared db, broker) get a stable port window that sits below every feature env's. The slot
immediately after the aliases (`N+1`, default index 11 with the 10-alias default) is reserved as a buffer between the
fixed alias band and the hash band; this is why the invariant requires `envs_per_workspace >= len(env_aliases) + 2` (not
`+1`).
