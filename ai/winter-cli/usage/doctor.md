# `winter doctor` — preflight checks

For the hub and the rest of the command surface, see [../index.md](../index.md).

```bash
winter doctor            # human-readable table
winter doctor --json     # NDJSON event stream
```

Runs preflight checks for the workspace and every installed extension. Each probe reports `pass`, `warn`, or `fail` with a one-line message and an optional remediation hint shown under failures. Exit code is `0` when nothing failed (warnings allowed), `1` if any probe failed.

**Built-in core probes** cover `git --version`, the running python version (>=3.11), `.winter/config.toml` parses, every declared project repo exists at `projects/<name>/`, every declared standalone repo exists at its configured path, every feature env's per-repo worktrees exist on the env-named branch, the `.claude/` symlinks (agents and skills contributed by extensions) resolve to existing targets, and the **port-allocation invariant** (`envs_per_workspace ≥ len(env_aliases) + 2`) plus **registry drift** (stale `.winter/state.toml` entries, unregistered env dirs, out-of-range or duplicate indices).

**Built-in capabilities probes** run unconditionally — one probe per known capability slot (currently just `service`). A slot that is invalid (broken config binding, missing entrypoint) → `fail`; a slot whose bound provider declares an `[implements]` version this winter does not support → `fail` (incompatible — upgrade winter or pin the extension); implicit provider(s) → `pass` with a note; an explicit valid binding → `pass`; no provider installed → `warn`. Two or more self-registered providers with no explicit binding resolve to implicit-all (all bound) — not an error. See [setup.md#capability-registry](../setup.md#capability-registry) for the full resolution model and `winter capabilities` ([capabilities.md](./capabilities.md)) to introspect the registry interactively.

**Workspace probes** are contributed via a top-level `doctor = "path/to/probe-script"` field in `.winter/config.toml`. Use this to add project-specific checks ("postgres reachable", "node_modules installed", "secrets present"). See [setup.md](../setup.md#workspace-doctor-probe) for the script contract.

**Extension probes** are contributed via a `doctor = "path/to/probe-script"` field in the extension's `winter-ext.toml`. See [setup.md#extension-doctor-probes](../setup.md#extension-doctor-probes) for the script contract.

`--json` emits one NDJSON object per line: `{"type": "started"}` once, `{"type": "probe_result", "source": ..., "name": ..., "status": ..., "message": ..., "remediation": ...}` per probe, then `{"type": "finished", "total": N, "fails": N, "warns": N}`. The per-probe object's shape — `source`, `name`, `status`, `message`, `remediation` — is the same one each extension's probe script emits on its own stdout; see [setup.md#probe-output-contract](../setup.md#probe-output-contract) for the probe-side contract.
