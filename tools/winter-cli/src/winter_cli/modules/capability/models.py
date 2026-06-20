from __future__ import annotations

import enum
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from winter_cli.modules.workspace.models import RepoError


class CapabilitySlot(enum.Enum):
    """The known capability slots.

    Only `service` is in scope now. Future slots (data, feedback, verification)
    are added here — the enum is what lets the command and doctor probe enumerate
    "every known slot" without hard-coding the list anywhere else.
    """

    service = "service"


BindingKind = Literal["explicit", "implicit", "unbound", "invalid", "incompatible"]


@dataclass(frozen=True)
class CapabilityCandidate:
    """One installed extension that provides a capability slot.

    `extension_name` is the standalone repo's name (matches its name in the config).
    `entrypoint_rel` is the raw relative path string from the manifest (e.g. `workflow/service`).
    `entrypoint_path` is the fully resolved absolute path (`ext_dir / entrypoint_rel`).
    `ext_dir` is the extension's on-disk root directory.
    `prefix` is the resolved symlink prefix for this extension.
    `entrypoint_valid` is True when `entrypoint_path` names an existing file on disk.
    `implemented_version` is the spec version declared in `[implements]` for this slot,
        or None when the extension predates the `implements` field (treated as compatible).
    """

    extension_name: str
    entrypoint_rel: str
    entrypoint_path: Path
    ext_dir: Path
    prefix: str
    entrypoint_valid: bool
    implemented_version: str | None = None


@dataclass(frozen=True)
class SlotResolution:
    """Full introspection of one capability slot — what the command and doctor render.

    `slot` is the capability slot being resolved.
    `candidates` is every installed extension that declares it provides this slot.
    `bound_extension` is the explicit config binding name (from `capabilities.<slot>`) if any.
        For multi-provider service slots this is the first provider; use `bound_extensions`
        for the full ordered list.
    `bound_extensions` is the ordered list of bound provider names when more than
        one is active. See the `bound_extensions` field docstring for details.
    `binding_kind` is one of: "explicit" (config binding to a valid provider or list),
        "implicit" (no config binding — sole provider, OR all candidates bound implicitly
        when 2+ are installed), "unbound" (0 providers, no binding),
        "invalid" (config binding is broken — extension not installed, not providing, or
        entrypoint missing).
    `error` is a human-readable string for `binding_kind == "invalid"`; None otherwise.

    Note on explicit-vs-implicit asymmetry: an explicit binding whose entrypoint file is
    missing is reported as `invalid` (with `error` set). An implicit sole provider whose
    entrypoint is missing stays `implicit` (entrypoint validity is carried on the candidate's
    `entrypoint_valid` and re-checked by `resolve()`/the doctor probe). Do not "fix" this
    asymmetry — it is deliberate: an explicit binding is a user assertion that must be valid,
    while an implicit provider is a discovery result that may be partially configured.

    Note on 2+ candidates without explicit binding: the slot is `implicit` (not unbound)
    and all candidates are bound. `is_ambiguous` always returns False in this model.
    """

    slot: CapabilitySlot
    candidates: tuple[CapabilityCandidate, ...]
    bound_extension: str | None
    binding_kind: BindingKind
    error: str | None
    bound_extensions: tuple[str, ...] = ()
    """Ordered list of bound provider names.

    Set when more than one provider is active for the slot. For an explicit
    `capabilities.<slot> = [...]` list this is the config-declared order. For
    implicit-all (2+ self-registered candidates, no explicit binding) this is
    the deterministic sorted order of candidate extension names. Empty when a
    single explicit or implicit binding is stored in `bound_extension` alone.
    """

    @property
    def is_ambiguous(self) -> bool:
        """True when the slot is unbound with two or more candidate providers."""
        return self.binding_kind == "unbound" and len(self.candidates) >= 2


@dataclass(frozen=True)
class ResolvedCapability:
    """The single winning provider for dispatch (analogous to `ResolvedOrchestrator`).

    `slot` is the capability slot resolved.
    `extension_name` is the name of the winning extension.
    `entrypoint` is the absolute path to the entrypoint script.
    `ext_dir` is the extension's on-disk root directory.
    `prefix` is the resolved symlink prefix for the extension.
    """

    slot: CapabilitySlot
    extension_name: str
    entrypoint: Path
    ext_dir: Path
    prefix: str


class CapabilityBindingError(RepoError):
    """Raised by `CapabilityRegistryService.resolve()` on any non-resolvable state.

    Non-resolvable states: zero providers (unbound), invalid binding (extension not
    installed, not providing the slot, or entrypoint file missing). Two or more
    providers with no explicit binding resolve to implicit-all (never raises).
    Subclasses `RepoError` so the CLI boundary renders it cleanly and tests/doctor
    can match on the specific type.
    """
