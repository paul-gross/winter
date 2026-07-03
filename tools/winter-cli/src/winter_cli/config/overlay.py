"""Spec-driven TOML config overlay engine.

A caller supplies a ``MergeSpec`` describing the shape of its config, and
``overlay_merge`` merges two parsed TOML dicts accordingly.  No mutation of
either input dict occurs.

Merge-spec field descriptors
-----------------------------
``ScalarField``
    The overlay value replaces the base value when present.  Any TOML scalar
    or list-of-scalars that should be replaced wholesale uses this.

``TableField``
    The overlay ``[table]`` is merged per-key into the base table (one level
    deep).  Keys present in the overlay replace the corresponding base key;
    keys absent from the overlay are inherited from base.

``ArrayAppendField``
    The overlay list is *appended* to the base list.  Used for TOML
    array-of-tables (``[[key]]``) where the overlay adds entries without
    wiping the base set.

``ArrayKeyedField(key)``
    The overlay list is merged keyed by *key* with override-or-append
    semantics: an overlay entry whose *key* matches an existing base entry
    *overrides* it in place (preserving position); an entry with a new *key*
    value is appended after all base entries.  Entries without *key* are
    always appended.  Per-entry merging is a shallow dict update
    (``{**base_entry, **overlay_entry}``), so partial overrides work.

Unspecified keys
    Any top-level key in the overlay that is not named in the spec replaces
    the base value wholesale (scalar replace — the safest default).

Usage example
-------------
::

    SPEC = MergeSpec(fields={
        "service_prefix": ScalarField(),
        "logs": TableField(),
        "service": ArrayKeyedField(key="name"),
    })

    merged = overlay_merge(base, overlay, spec=SPEC)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Field descriptors
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScalarField:
    """Overlay value replaces base value."""


@dataclass(frozen=True)
class TableField:
    """Overlay table is merged per-key (one level deep)."""


@dataclass(frozen=True)
class ArrayAppendField:
    """Overlay list is appended to base list."""


@dataclass(frozen=True)
class ArrayKeyedField:
    """Overlay list is merged keyed by *key* with override-or-append."""

    key: str


# The union of all field descriptor types.
_FieldDescriptor = ScalarField | TableField | ArrayAppendField | ArrayKeyedField


# ---------------------------------------------------------------------------
# MergeSpec
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MergeSpec:
    """Declarative merge specification for a TOML config shape.

    ``fields`` maps top-level key names to their merge strategy.  Keys not
    present in ``fields`` default to scalar-replace behaviour.
    """

    fields: dict[str, _FieldDescriptor]

    def descriptor(self, key: str) -> _FieldDescriptor:
        """Return the field descriptor for *key*, defaulting to ``ScalarField``."""
        return self.fields.get(key, ScalarField())


# ---------------------------------------------------------------------------
# Keyed-list merge helper
# ---------------------------------------------------------------------------


def _merge_keyed(
    base_list: list[dict[str, Any]],
    overlay_list: list[dict[str, Any]],
    key: str,
) -> list[dict[str, Any]]:
    """Merge *overlay_list* onto *base_list* keyed by *key*.

    An overlay entry whose *key* matches an existing base entry overrides it
    in place (shallow dict update preserving position).  An entry with a new
    *key* value is appended after all base entries.  Entries without *key*
    are always appended.

    Neither *base_list* nor *overlay_list* is mutated.
    """
    key_to_idx: dict[Any, int] = {}
    for i, entry in enumerate(base_list):
        if key in entry:
            key_to_idx[entry[key]] = i

    result: list[dict[str, Any]] = [dict(e) for e in base_list]
    for overlay_entry in overlay_list:
        entry_key = overlay_entry.get(key)
        if entry_key is not None and entry_key in key_to_idx:
            idx = key_to_idx[entry_key]
            result[idx] = {**result[idx], **overlay_entry}
        else:
            result.append(dict(overlay_entry))
            if entry_key is not None:
                key_to_idx[entry_key] = len(result) - 1

    return result


# ---------------------------------------------------------------------------
# Public merge function
# ---------------------------------------------------------------------------


def overlay_merge(
    base: dict[str, Any],
    overlay: dict[str, Any],
    *,
    spec: MergeSpec,
) -> dict[str, Any]:
    """Merge *overlay* on top of *base* according to *spec*.

    Returns a new dict; neither *base* nor *overlay* is mutated.

    For each key in *overlay*:

    - ``ScalarField`` (or unspecified key): overlay value replaces base value.
    - ``TableField``: one-level per-key merge (overlay keys win; base keys
      absent from overlay are kept).
    - ``ArrayAppendField``: overlay list appended to base list.
    - ``ArrayKeyedField(key)``: keyed override-or-append (see ``_merge_keyed``).
    """
    if not overlay:
        return dict(base)

    result: dict[str, Any] = dict(base)

    for k, v in overlay.items():
        descriptor = spec.descriptor(k)

        if isinstance(descriptor, ScalarField):
            result[k] = v

        elif isinstance(descriptor, TableField):
            base_table: dict[str, Any] = dict(result.get(k) or {})
            if isinstance(v, dict):
                base_table.update(v)
            result[k] = base_table

        elif isinstance(descriptor, ArrayAppendField):
            base_list: list[Any] = list(result.get(k) or [])
            if isinstance(v, list):
                result[k] = base_list + v
            else:
                result[k] = v

        elif isinstance(descriptor, ArrayKeyedField):
            base_list_k: list[dict[str, Any]] = list(result.get(k) or [])
            if isinstance(v, list):
                result[k] = _merge_keyed(base_list_k, v, descriptor.key)
            else:
                result[k] = v

        else:
            result[k] = v

    return result
