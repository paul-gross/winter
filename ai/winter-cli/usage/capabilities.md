# `winter capabilities` — capability slot introspection

For the hub and the rest of the command surface, see [../index.md](../index.md).

```bash
winter capabilities          # human-readable per-slot binding listing
winter capabilities --json   # JSON array, one object per known slot
```

Read-only introspection of the capability registry. Lists every known slot, which extension is bound to it, how the binding was determined, and whether each candidate's entrypoint file resolves on disk. Always exits 0 — misconfiguration states are reported here but only *fail* under `winter doctor`'s `[capabilities]` probe group.

## Human-readable output

Each slot prints on one line. The format varies by binding kind:

- **explicit** — `<slot> → <ext> (explicit)  [<entrypoint> ✓/✗]` — a `capabilities.<slot>` config binding points at a valid (✓) or missing (✗) entrypoint.
- **implicit** — `<slot> → <ext> (implicit)  [<entrypoint> ✓/✗]` — sole provider, no explicit config binding.
- **unbound (ambiguous)** — `<slot> → (unbound — N candidates: <ext1>, <ext2>)` with one indented line per candidate showing its entrypoint and validity.
- **invalid** — `<slot> → <ext> (invalid)  — <error message>` — the config binding is broken (extension not installed, not providing the slot, or entrypoint missing).
- **incompatible** — `<slot> → <ext> (incompatible)  — <error message>` — the bound extension declares an `[implements] <slot>` version that this winter does not support. The error message names both the declared version and the supported set. Remediation: upgrade winter to a version that supports the declared spec version, or pin the extension to a version compatible with this winter's supported spec versions.
- **no provider** — `<slot> → (no provider installed)`.

## JSON contract

`--json` emits a single JSON array; one object per known slot, in `CapabilitySlot` declaration order:

```json
[
  {
    "slot": "service",
    "bound": "winter-service-tmux",
    "binding_kind": "explicit",
    "ambiguous": false,
    "error": null,
    "candidates": [
      {"extension": "winter-service-tmux", "entrypoint": "workflow/service", "valid": true}
    ]
  }
]
```

Field reference:

| Field | Type | Meaning |
|-------|------|---------|
| `slot` | string | Capability slot name (e.g. `"service"`). |
| `bound` | string \| null | Extension name from an explicit `capabilities.<slot>` config binding, or `null` when no binding is set. |
| `binding_kind` | string | One of `"explicit"`, `"implicit"`, `"unbound"`, `"invalid"`, `"incompatible"`. |
| `ambiguous` | boolean | True when `binding_kind == "unbound"` and there are two or more candidates. |
| `error` | string \| null | Human-readable error for `binding_kind == "invalid"`; `null` otherwise. |
| `candidates` | array | Every installed extension declaring `provides.<slot>`. |
| `candidates[].extension` | string | Extension name (matches its `[[standalone_repository]]` name). |
| `candidates[].entrypoint` | string | Raw entrypoint path from the manifest (relative to the extension repo root). |
| `candidates[].valid` | boolean | True when the entrypoint file exists on disk. |

For the full resolution model (explicit, implicit, ambiguous, invalid), the config and manifest keys, and deprecated alias handling, see [../setup.md#capability-registry](../setup.md#capability-registry). For the service orchestration command that dispatches through the `service` slot, see [service.md](./service.md).
