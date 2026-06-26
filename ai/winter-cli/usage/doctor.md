# `winter doctor` — preflight checks

For the hub and the rest of the command surface, see [../index.md](../index.md).

```bash
winter doctor            # human-readable table
winter doctor --json     # NDJSON event stream
```

Runs preflight checks for the workspace and every installed extension. Each probe reports `pass`, `warn`, or `fail` with a one-line message and an optional remediation hint shown under failures. Exit code is `0` when nothing failed (warnings allowed), `1` if any probe failed.

**Built-in core probes** cover `git --version`, the running python version (>=3.11), `.winter/config.toml` parses, every declared project repo exists at `projects/<name>/`, every declared standalone repo exists at its configured path, every feature env's per-repo worktrees exist on the env-named branch, the extension symlinks (agents and skills contributed by extensions, under `.claude/{agents,skills}` and `.codex/skills`) resolve to existing targets, and the **port-allocation invariant** (`envs_per_workspace ≥ len(env_aliases) + 2`) plus **registry drift** (stale `.winter/state.toml` entries, unregistered env dirs, out-of-range or duplicate indices).

**Built-in provision probe** validates every `[[provision.*]]` entry declared in `.winter/config.toml` and in each installed extension's `winter-ext.toml`. Each bad entry emits one finding under a `[provision]` source group without aborting other checks. The probe validates: `scope` is a known value (`workspace`, `feature-environment`, `feature-worktree`); `apply` is present and is a non-empty string or a non-empty list of non-empty strings; `destroy` and `reset`, when present, are each a non-empty string or a non-empty list of non-empty strings; `required_services` is only declared on `resource` or `data` entries (not `dependency`); no unknown keys are present. A missing or empty `[provision]` table is silently skipped.

**Built-in capabilities probes** run unconditionally — one probe per known capability slot (currently just `service`). A slot that is invalid (broken config binding, missing entrypoint) → `fail`; a slot whose bound provider declares an `[implements]` version this winter does not support → `fail` (incompatible — upgrade winter or pin the extension); implicit provider(s) → `pass` with a note; an explicit valid binding → `pass`; no provider installed → `warn`. Two or more self-registered providers with no explicit binding resolve to implicit-all (all bound) — not an error. See [configuration/capabilities.md#capability-registry](../configuration/capabilities.md#capability-registry) for the full resolution model and `winter capabilities` ([capabilities.md](./capabilities.md)) to introspect the registry interactively.

**Workspace probes** are contributed via a top-level `doctor = "path/to/probe-script"` field in `.winter/config.toml`. Use this to add project-specific checks ("postgres reachable", "node_modules installed", "secrets present"). See [configuration/doctor.md](../configuration/doctor.md#workspace-doctor-probe) for the script contract.

**Extension probes** are contributed via a `doctor = "path/to/probe-script"` field in the extension's `winter-ext.toml`. See [configuration/doctor.md#extension-doctor-probes](../configuration/doctor.md#extension-doctor-probes) for the script contract.

`--json` emits one NDJSON object per line: `{"type": "started"}` once, `{"type": "probe_result", "source": ..., "name": ..., "status": ..., "message": ..., "remediation": ...}` per probe, then `{"type": "finished", "total": N, "fails": N, "warns": N}`. The per-probe object's shape — `source`, `name`, `status`, `message`, `remediation` — is the same one each extension's probe script emits on its own stdout; see [configuration/doctor.md#probe-output-contract](../configuration/doctor.md#probe-output-contract) for the probe-side contract.
