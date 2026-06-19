# `winter ext` — extension management

For the hub and the rest of the command surface, see [../index.md](../index.md).

```bash
winter ext verify <extension>          # verify an extension conforms to the service spec
winter ext verify <extension> --json   # emit check results as JSON
winter ext new <name> --capability service               # scaffold a new service extension
winter ext new <name> --capability service --dir <path>  # scaffold to a specific directory
winter ext new <name> --capability service --force       # allow writing into a non-empty dir
```

The `ext` command group manages the extension contract: it verifies that an installed or local extension correctly implements a capability spec, and scaffolds a new extension skeleton that passes verification out of the box.

## `winter ext verify`

```bash
winter ext verify <extension>
winter ext verify <extension> --json
```

Runs the conformance checks from the bundled capability spec against the extension's declared service entrypoint. `<extension>` is either:

- A **local path** — any value containing an OS path separator (`/`) or resolving to an existing directory on disk. `winter-ext.toml` is read directly from that directory.
- An **installed extension name** — a bare name (e.g. `winter-service-tmux`) looked up among the `[[standalone_repository]]` entries in `.winter/config.toml`.

### What conformance verification checks

Three check kinds are run. The machine-readable source of truth for all three is `tools/winter-cli/src/winter_cli/modules/capability/specs/service-v1.toml` — the spec file bundled with winter-cli.

- **accepts-action** — one check per declared action word (`up`, `down`, `status`, `restart`, `logs`). The entrypoint is invoked with the action and probe arguments. It passes when the exit code is anything other than 2 (exit 2 = unknown-action signal; exit 0 = success; exit 3 = recognized-but-refused — all pass this check).
- **refuses-unknown** — the entrypoint is invoked with a synthetic unknown action word. It must exit non-zero. Exit 2 or exit 3 are both accepted.
- **forwards-params** — a sentinel token is passed as an argv argument; the entrypoint must echo it back on stdout or stderr, confirming argv is forwarded. The three `WINTER_*` env vars are set on every dispatch but are not asserted by this check.

A setup failure (directory missing, no `winter-ext.toml`, no service entrypoint declared, entrypoint file missing) is reported on stderr and the command exits non-zero; no checks are run.

### Exit codes

- **0** — all conformance checks passed.
- **1** — one or more checks failed, or a setup failure occurred.

The exit code is the same regardless of `--json`.

### `--json` output

`--json` emits a single JSON object on stdout:

```json
{
  "setup_failure": null,
  "any_failed": false,
  "results": [
    {"check_id": "accepts-up", "passed": true, "detail": "...", "argv": [...], "observed_exit": 0}
  ]
}
```

| Field | Type | Meaning |
|-------|------|---------|
| `setup_failure` | string \| null | Human-readable error when the extension could not be resolved; `null` otherwise. |
| `any_failed` | boolean | True when `setup_failure` is set or at least one check failed. |
| `results` | array | Per-check outcomes, in spec declaration order. Empty when `setup_failure` is set. |
| `results[].check_id` | string | Stable identifier: `"accepts-up"`, `"accepts-down"`, `"accepts-status"`, `"accepts-restart"`, `"accepts-logs"`, `"refuses-unknown"`, `"forwards-params"`. |
| `results[].passed` | boolean | True when the check succeeded. |
| `results[].detail` | string | Human-readable description of the observed outcome. |
| `results[].argv` | array | The golden invocation that was run against the entrypoint. |
| `results[].observed_exit` | integer | Exit code returned by the entrypoint subprocess. |

### `implements` and version compatibility

An extension can declare which spec version it targets in its `winter-ext.toml`:

```toml
[implements]
service = "v1"
```

Winter enforces this at **binding resolution time** — whenever `winter service` dispatches, not just at `ext verify` time. If the declared version is not in the set supported by the running winter, the capability slot is reported as `binding_kind = "incompatible"` and `winter service` raises an error naming both the declared version and the supported set. Extensions without an `[implements]` declaration are treated as compatible (lenient-when-absent, for backwards compatibility). Use `winter capabilities` ([capabilities.md](./capabilities.md)) to inspect the current binding kind, and `winter doctor` to surface incompatibility as a health probe.

The set of supported versions is derived solely from the spec files bundled in `tools/winter-cli/src/winter_cli/modules/capability/specs/` — one file per version (e.g. `service-v1.toml`).

## `winter ext new`

```bash
winter ext new <name> --capability <slot>
winter ext new <name> --capability <slot> --dir <path>
winter ext new <name> --capability <slot> --force
```

Scaffolds a new extension skeleton that implements a capability slot. The generated skeleton passes `winter ext verify` out of the box.

### What `ext new` generates

Three files are created under the output directory (default: `<current-directory>/<name>/`):

| File | Purpose |
|------|---------|
| `winter-ext.toml` | Extension manifest declaring `[provides] service` and `[implements] service = "<version>"`. |
| `index.md` | Documentation skeleton per the harness extension-index convention. |
| `workflow/service` | Executable refuse-all stub entrypoint. |

### The refuse-all stub

The generated `workflow/service` is a Python script whose action set and exit codes are rendered from the same spec file `ext verify` reads, so scaffold↔verify drift is impossible by construction:

- **Known action** (`up`, `down`, `status`, `restart`, `logs`) → exits 3 (recognized-but-refused). This is non-2, so `accepts-action` passes.
- **Unknown action** → exits 2. This is non-zero, so `refuses-unknown` passes.
- **Argv is echoed to stderr** so `forwards-params` finds the sentinel.

Replace the stub's action implementations with real orchestration logic when the extension is ready.

### Options

| Flag | Default | Meaning |
|------|---------|---------|
| `--capability <slot>` | required | Capability slot to implement. Currently only `service` is supported. |
| `--dir <path>` | `<current-directory>/<name>/` | Output directory. An absolute path is used as-is; a relative path is resolved against the directory you ran the command from. |
| `--force` | false | Allow writing into a non-empty existing directory. |

Exits 1 with an error message if the output directory is non-empty and `--force` is not set.
