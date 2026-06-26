# Doctor probes

`winter doctor` (see [../usage/doctor.md](../usage/doctor.md)) aggregates probe results from three sources: built-in core checks in winter-cli, an optional workspace-level probe, and one probe per installed extension. The workspace and extension probes are opt-in shell scripts that follow the same output contract.

## Probe output contract

Every probe script emits **NDJSON to stdout**, one object per line:

```json
{"name": "tea auth", "status": "pass", "message": "logged in as pgross"}
{"name": "tmux version", "status": "warn", "message": "v2.8 (recommend >= 3.0)", "remediation": "Upgrade tmux: `dnf install tmux`."}
```

Required fields: `name` (string) and `status` (one of `pass` / `warn` / `fail`). Optional: `message` (one-line summary) and `remediation` (one-line fix hint, shown under failures in the table view).

**Exit handling.** A non-zero exit becomes a single synthetic `fail` result with the probe's stderr as the message — surfaced even if no NDJSON was emitted. Lines that don't parse as JSON, or that are missing required fields, become `warn` results so the contract violation is visible without aborting the run.

**Common misconfigurations** (workspace and extension probes alike): a missing `doctor` field is silently skipped; a `doctor` value pointing at a missing script surfaces as a `fail`; a script that exists but isn't executable surfaces as a `fail` so the misconfiguration is actionable; a path that escapes its declaring directory (workspace root for workspace probes, extension directory for extension probes) is refused.

## Workspace doctor probe

The workspace itself can contribute a probe script that runs between the core probes and each extension's probes. Declare it as a top-level field in `.winter/config.toml`:

```toml
doctor = "ai/project/doctor.sh"
```

The path is **relative to the workspace root** and must point to an executable file. The probe runs with cwd at the workspace root and `WINTER_WORKSPACE_DIR` set. Use it for project-specific checks that don't belong in any extension — database reachable, `.env` populated, secrets present, build toolchain installed.

Results appear under a `[project]` source group in the table view, between `[core]` and each `[<ext-prefix>]` block.

## Extension doctor probes

Extensions opt in via a top-level field in `winter-ext.toml`:

```toml
doctor = "scripts/doctor.sh"
```

`doctor` is a **top-level scalar** in `winter-ext.toml`, not part of `[hooks]` — there's at most one probe script per extension. The path is **relative to the extension directory** (same rule as hook scripts) and must point to an executable file.

The probe's **cwd is the workspace root**. Probes are workspace-scoped, not per-env, so the env vars are a subset of the hook contract:

| Var | Meaning |
|-----|---------|
| `WINTER_WORKSPACE_DIR` | Absolute path to the workspace root. |
| `WINTER_EXT_DIR` | Absolute path to this extension's clone. |
| `WINTER_EXT_PREFIX` | The resolved symlink prefix for this extension. |

Results appear under a `[<ext-prefix>]` source group, one block per installed extension that contributes probes.
