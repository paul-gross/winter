# Ports & environments

Winter assigns each feature environment a port band derived from its index, and can write per-env derived variables into each env's `.winter.env`. These keys live in `.winter/config.toml`.

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

## Per-env derived variables

The `[env.vars]` table is rendered into each env's `.winter.env` as a second managed block on `winter ws init`. Values support `${...}` substitution; literal text passes through unchanged.

```toml
[env.vars]
WTS_WEB_PORT = "${WINTER_PORT_BASE+10}"
WTS_API_PORT = "${WINTER_PORT_BASE+11}"
WTS_DB_PORT  = "${WINTER_PORT_BASE+12}"
DATABASE_URL = "postgresql://wts:wts@localhost:${WTS_DB_PORT}/wts-${WINTER_ENV}"  # reuses WTS_DB_PORT and WINTER_ENV
```

**Token grammar.** Two forms are supported:

- `${NAME}` — substitutes the string value of `NAME`.
- `${NAME+N}` — adds a non-negative integer `N` to `NAME` (which must parse as an integer).

`NAME` resolves against an **accumulating scope**: seeded with the managed base vars (`WINTER_ENV`, `WINTER_ENV_INDEX`, `WINTER_PORT_BASE`, `WINTER_WORKSPACE_PORT_BASE`) and grown by each rendered `[env.vars]` entry **in TOML declaration order** — so a later entry can reuse an earlier one (as `DATABASE_URL` reuses `WTS_DB_PORT` above). `WINTER_PORT_BASE` is not special: `${WINTER_PORT_BASE+N}` is just the base-var case.

Resolution happens in the parser — concrete values are written into `.winter.env`, not deferred to shell-source time. An undefined name, `+N` applied to a non-integer value, or any other malformed `${...}` token is a fatal per-env error at init time.

## Index reservation

The env name → index mapping itself is recorded in [`.winter/state.toml`](./config-files.md#state-registry). Two indices are reserved and never assigned to a regular feature env:

Index 0 (`base_port`..`base_port+ports_per_env-1`) is reserved for a future single-slot "local" environment — a pre-seeded shared dataset/area distinct in purpose from the regular alias and hash-band slots. It is never assigned. The slot immediately after the aliases (`N+1`, default index 11 with the 10-alias default) is reserved as a buffer between the fixed alias band and the hash band; this is why the invariant requires `envs_per_workspace >= len(env_aliases) + 2` (not `+1`).
