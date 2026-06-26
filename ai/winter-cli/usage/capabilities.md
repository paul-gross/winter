# `winter capabilities` — capability slot introspection

For the hub and the rest of the command surface, see [../index.md](../index.md).

```bash
winter capabilities          # human-readable per-slot binding listing
winter capabilities --json   # JSON array, one object per known slot
```

Read-only introspection of the capability registry. Lists every known slot, which extension is bound to it, how the binding was determined, and whether each candidate's entrypoint file resolves on disk. Always exits 0 — misconfiguration states are reported here but only *fail* under `winter doctor`'s `[capabilities]` probe group.

## Human-readable output

Each slot prints on one line. The format varies by binding kind:

- **explicit** — `<slot> → <ext> (explicit)  [<entrypoint> ✓/✗]` — a single-name `capabilities.<slot>` config binding points at a valid (✓) or missing (✗) entrypoint.
- **explicit (multi-provider)** — `<slot> → [<ext1>, <ext2>] (explicit)` — a list-valued `capabilities.<slot>` binding; each member is listed with its entrypoint and validity. List order carries no execution semantics.
- **implicit** — `<slot> → <ext> (implicit)  [<entrypoint> ✓/✗]` — sole self-registered provider, no explicit config binding.
- **implicit (all candidates)** — `<slot> → [<ext1>, <ext2>] (implicit, all candidates)` — two or more self-registered providers and no explicit binding; all are bound, each listed with its entrypoint and validity.
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

For a multi-provider binding (`capabilities.service = [...]`, or two or more self-registered candidates), `bound` is a JSON **array** instead of a scalar string:

```json
[
  {
    "slot": "service",
    "bound": ["winter-service-tmux", "winter-service-docker"],
    "binding_kind": "explicit",
    "ambiguous": false,
    "error": null,
    "candidates": [
      {"extension": "winter-service-tmux", "entrypoint": "workflow/service", "valid": true},
      {"extension": "winter-service-docker", "entrypoint": "workflow/service", "valid": true}
    ]
  }
]
```

Field reference:

| Field | Type | Meaning |
|-------|------|---------|
| `slot` | string | Capability slot name (e.g. `"service"`). |
| `bound` | string \| array \| null | Extension name for a single-provider binding; JSON array of names for a multi-provider binding (explicit list or 2+ self-registered candidates); `null` when no binding is set. Scalar-for-single, array-for-multi — single-provider machine clients are not broken by the multi-provider extension. |
| `binding_kind` | string | One of `"explicit"`, `"implicit"`, `"unbound"`, `"invalid"`, `"incompatible"`. Two or more self-registered candidates with no explicit binding resolve to `"implicit"` (all bound). |
| `ambiguous` | boolean | Always `false` in the current model — 2+ candidates bind all (implicit) rather than refusing. Retained for machine-contract stability. |
| `error` | string \| null | Human-readable error for `binding_kind == "invalid"`; `null` otherwise. |
| `candidates` | array | Every installed extension declaring `provides.<slot>`. |
| `candidates[].extension` | string | Extension name (matches its `[[standalone_repository]]` name). |
| `candidates[].entrypoint` | string | Raw entrypoint path from the manifest (relative to the extension repo root). |
| `candidates[].valid` | boolean | True when the entrypoint file exists on disk. |

For the full resolution model (explicit, implicit, invalid), the config and manifest keys, and deprecated alias handling, see [../configuration/capabilities.md#capability-registry](../configuration/capabilities.md#capability-registry). For the service orchestration command that dispatches through the `service` slot, see [service.md](./service.md).
