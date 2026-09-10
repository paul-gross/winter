# `winter env` — print the runtime environment for a scope

```text
winter env <scope> [--resolve]
```

Print the complete runtime environment for *scope* as sourceable `export KEY=value` lines, one per variable, in the
order the provisioner returns them:

```text
export WINTER_ENV=alpha
export WINTER_ENV_INDEX=1
export WINTER_PORT_BASE=4060
export WINTER_WORKSPACE_PORT_BASE=4000
export WINTER_SERVICE_PREFIX=winter
export MY_APP_PORT=4061
```

*scope* is either a feature-env name (e.g. `alpha`, `beta`) or the reserved word `workspace` for the workspace-level
singleton scope.

## Usage

**Source into the current shell:**

```bash
source <(winter env alpha)
```

**Source in a script or Dockerfile:**

```bash
eval "$(winter env alpha)"
```

**Inspect the environment for a scope:**

```bash
winter env alpha          # feature env
winter env workspace      # workspace singleton scope
```

**Resolve command-derived values:**

```bash
winter env alpha --resolve
```

## Variables printed

The exact set depends on the scope:

**`winter env workspace`** emits only the workspace trio plus any `[env.workspace.vars]` entries:

| Variable                     | Meaning                                                   |
| ---------------------------- | --------------------------------------------------------- |
| `WINTER_ENV`                 | `workspace`                                               |
| `WINTER_ENV_INDEX`           | `0`                                                       |
| `WINTER_WORKSPACE_PORT_BASE` | Port-band start for index 0                               |
| `WINTER_SERVICE_PREFIX`      | Resolved workspace service-orchestration namespace prefix |

`WINTER_PORT_BASE` is NOT emitted for the workspace scope.

**`winter env <feature>`** emits the full feature set:

| Variable                     | Meaning                                                              |
| ---------------------------- | -------------------------------------------------------------------- |
| `WINTER_ENV`                 | Scope name (e.g. `alpha`)                                            |
| `WINTER_ENV_INDEX`           | Stable index used for port allocation                                |
| `WINTER_PORT_BASE`           | Port-band start for this scope (`base_port + index * ports_per_env`) |
| `WINTER_WORKSPACE_PORT_BASE` | Port-band start for index 0 (the workspace port base)                |
| `WINTER_SERVICE_PREFIX`      | Resolved workspace service-orchestration namespace prefix            |

Followed by the env var band entries from `.winter/config.toml` that apply to the scope. See
[ports-and-environments.md](../configuration/ports-and-environments.md#env-var-bands) for which bands those are, their
resolution order, collision rules, and token grammar.

## Masking

A band entry can be a [command entry](../configuration/command-env-entries.md) rather than a plain `${...}` template —
an inline table that sources its value by running a command (e.g. reading a secret out of Vault or AWS SSM). Running an
arbitrary configured command is not something `winter env` does by default:

- **Without `--resolve`** (the default), the key a command entry declares prints with a placeholder value
  (`<unresolved:command>`) instead of its real one, and the command is never run; an entry interpolating that key prints
  the placeholder inside its own value. This is what keeps the default `winter env` pure and offline — printing a scope
  never executes anything the workspace configured. A `_`-prefixed (anonymous import slot) key behaves differently under
  both gates — see [configuration/command-env-entries.md#the-gate](../configuration/command-env-entries.md#the-gate).
- **With `--resolve`**, every command entry actually runs (once its own `${...}` references resolve) and its real value
  is printed in place of the placeholder.

The default masks only what it can name. An entry whose value a `dotenv`/`json` command *imports* — a key that entry
never declares — is unknown until the command has run, so referencing one without `--resolve` is an undefined-variable
error (exit 1), not a placeholder. Print such a scope with `--resolve`.

Masking is not redaction: `--resolve` prints the real value in the clear, exactly like every other variable this command
prints — there is no partial reveal or scrubbing of a resolved secret from the output.

## Exit codes

| Exit code | Meaning                                                                                                                                                                                                                                              |
| --------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 0         | Success — every line written to stdout.                                                                                                                                                                                                              |
| 1         | Scope unknown, an env-band template error (undefined reference, resolution cycle, malformed token), or — under `--resolve` — a command entry that failed (non-zero exit, timeout, or unparseable output). No output is written to stdout on failure. |

## Notes

- Output is shell-safe: values are quoted with `shlex.quote` so special characters do not break the `source`/`eval`
  recipe.
- `winter env` is the canonical way to load an env's variables into a shell. Services run by `winter service up` receive
  the same variable set injected directly into the provider subprocess environment — no file sourcing needed — except
  for a [command entry](../configuration/command-env-entries.md): `up` always runs it for real, so the default
  `winter env` output (no `--resolve`) prints that key's placeholder rather than the value the service actually gets;
  pass `--resolve` to see it (see [Masking](#masking) above).
