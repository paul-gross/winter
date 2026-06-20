from __future__ import annotations

import logging

from winter_cli.core.filesystem import IFilesystemReader
from winter_cli.modules.capability.models import (
    CapabilityBindingError,
    CapabilityCandidate,
    CapabilitySlot,
    ResolvedCapability,
    SlotResolution,
)
from winter_cli.modules.capability.spec_loader import ISpecLoader
from winter_cli.modules.capability.version_compat import VersionCompatError, check_compat
from winter_cli.modules.workspace.extension_manifest import EXT_MANIFEST, ExtensionManifestLoader
from winter_cli.modules.workspace.models import RepoError
from winter_cli.modules.workspace.repository_factory import IStandaloneRepoProvider

logger = logging.getLogger(__name__)


class CapabilityRegistryService:
    """Resolves which extension(s) provide each capability slot.

    Combines three inputs — manifest `[provides]`, config `[capabilities]` bindings,
    and the installed-extension set — to determine the provider(s) for each slot.
    Mirrors `GraphService` in constructor shape and manifest-load tolerance (bad
    manifests are skipped with a warning rather than aborting).

    `bindings` is the parsed `[capabilities]` table from `.winter/config.toml`
    (slot name → ordered list of extension names). Do not inject `WorkspaceConfig`
    directly; the caller (container) extracts this dict and passes it in.

    The active provider set for a slot:
    - Explicit `capabilities.<slot>` list if present (validated in order).
    - Otherwise ALL self-registered candidates. 1 candidate → implicit;
      0 → unbound; 2+ → implicit-all (all bound, deterministic name order).

    Callers that need exactly one provider use `resolve()` (returns first).
    Use `resolve_all()` for the full ordered list.
    """

    def __init__(
        self,
        repo_factory: IStandaloneRepoProvider,
        manifest_loader: ExtensionManifestLoader,
        bindings: dict[str, list[str]],
        fs: IFilesystemReader,
        spec_loader: ISpecLoader,
    ) -> None:
        self._repo_factory = repo_factory
        self._manifest_loader = manifest_loader
        self._bindings = bindings
        self._fs = fs
        self._spec_loader = spec_loader

    def candidates(self, slot: CapabilitySlot) -> list[CapabilityCandidate]:
        """Return every installed extension that declares it provides `slot`.

        Walks `get_standalone_repos()`; for each repo whose directory contains a
        `winter-ext.toml`, loads the manifest and asks whether it provides the slot.
        Repos with unreadable manifests are skipped with a warning.
        Returns candidates in enumeration order.
        """
        result: list[CapabilityCandidate] = []
        for repo in self._repo_factory.get_standalone_repos():
            manifest_path = repo.path / EXT_MANIFEST
            if not self._fs.is_file(manifest_path):
                continue
            try:
                manifest = self._manifest_loader.load(repo, manifest_path)
            except RepoError as exc:
                logger.warning("skipping %s in capability registry — %s", repo.name, exc)
                continue
            entrypoint_rel = manifest.capability_entrypoint(slot.value)
            if not entrypoint_rel:
                continue
            entrypoint_path = repo.path / entrypoint_rel
            result.append(
                CapabilityCandidate(
                    extension_name=repo.name,
                    entrypoint_rel=entrypoint_rel,
                    entrypoint_path=entrypoint_path,
                    ext_dir=repo.path,
                    prefix=manifest.prefix,
                    entrypoint_valid=self._fs.is_file(entrypoint_path),
                    implemented_version=manifest.implemented_version(slot.value),
                )
            )
        return result

    def describe(self, slot: CapabilitySlot) -> SlotResolution:
        """Compute the full resolution picture for `slot` without raising.

        This is the read-only introspection path used by the command and doctor.
        Derives `binding_kind`, `bound_extension`, `bound_extensions`, and `error`
        from the installed candidates and the config binding. Never raises; use
        `resolve()` for the dispatch path that raises on non-resolvable states.

        Explicit list path (capabilities.<slot> is set):
          Validates each member in order. First invalid member → `invalid` with a
          per-member error referencing `capabilities.<slot>`. All valid → `explicit`;
          `bound_extensions` carries the full ordered list.

        Implicit path (no capabilities.<slot> binding):
          0 candidates → `unbound`.
          1 candidate → `implicit` (sole provider).
          2+ candidates → `implicit` (all bound, deterministic name order);
            `bound_extensions` carries all in sorted order.
        """
        slot_candidates = self.candidates(slot)
        ordered_list: list[str] = self._bindings.get(slot.value) or []

        if ordered_list:
            # Explicit binding: validate each member in order.
            all_names = {r.name for r in self._repo_factory.get_standalone_repos()}
            for name in ordered_list:
                matching = [c for c in slot_candidates if c.extension_name == name]
                if not matching:
                    if name in all_names:
                        error = (
                            f"capabilities.{slot.value} = {name!r} — extension {name!r} is installed"
                            f" but declares no provides.{slot.value} in its winter-ext.toml."
                        )
                    else:
                        error = (
                            f"capabilities.{slot.value} = {name!r} — no installed extension named {name!r}"
                            f" (capabilities.{slot.value} must name a [[standalone_repository]])."
                        )
                    return SlotResolution(
                        slot=slot,
                        candidates=tuple(slot_candidates),
                        bound_extension=ordered_list[0],
                        bound_extensions=tuple(ordered_list),
                        binding_kind="invalid",
                        error=error,
                    )
                candidate = matching[0]
                if not candidate.entrypoint_valid:
                    error = (
                        f"capabilities.{slot.value} provider {name!r} entrypoint not found"
                        f" at {candidate.entrypoint_path}."
                    )
                    return SlotResolution(
                        slot=slot,
                        candidates=tuple(slot_candidates),
                        bound_extension=ordered_list[0],
                        bound_extensions=tuple(ordered_list),
                        binding_kind="invalid",
                        error=error,
                    )
                compat_error = self._check_version_compat(slot, candidate)
                if compat_error is not None:
                    return SlotResolution(
                        slot=slot,
                        candidates=tuple(slot_candidates),
                        bound_extension=ordered_list[0],
                        bound_extensions=tuple(ordered_list),
                        binding_kind="incompatible",
                        error=compat_error,
                    )

            return SlotResolution(
                slot=slot,
                candidates=tuple(slot_candidates),
                bound_extension=ordered_list[0],
                bound_extensions=tuple(ordered_list),
                binding_kind="explicit",
                error=None,
            )

        # Implicit path: no explicit binding.
        if len(slot_candidates) == 0:
            return SlotResolution(
                slot=slot,
                candidates=(),
                bound_extension=None,
                binding_kind="unbound",
                error=None,
            )

        if len(slot_candidates) == 1:
            return SlotResolution(
                slot=slot,
                candidates=tuple(slot_candidates),
                bound_extension=None,
                binding_kind="implicit",
                error=None,
            )

        # 2+ candidates, no explicit binding → implicit-all: bind every candidate
        # in deterministic (extension name) order.
        all_implicit = sorted(slot_candidates, key=lambda c: c.extension_name)
        return SlotResolution(
            slot=slot,
            candidates=tuple(slot_candidates),
            bound_extension=None,
            bound_extensions=tuple(c.extension_name for c in all_implicit),
            binding_kind="implicit",
            error=None,
        )

    def _check_version_compat(self, slot: CapabilitySlot, candidate: CapabilityCandidate) -> str | None:
        """Return a compat error message for `candidate`'s `slot` implementation, or None.

        Uses the `implemented_version` already loaded onto the candidate in
        `candidates()` — no second filesystem walk or manifest load needed.
        """
        supported = self._spec_loader.supported_versions(slot.value)
        return check_compat(slot.value, candidate.implemented_version, supported)

    def describe_all(self) -> list[SlotResolution]:
        """Return a `SlotResolution` for every known capability slot."""
        return [self.describe(s) for s in CapabilitySlot]

    def resolve_all(self, slot: CapabilitySlot) -> list[ResolvedCapability]:
        """Resolve the ordered list of providers for `slot`, or raise.

        For a single-provider slot (whether explicit or implicit) this returns a
        one-element list. For a multi-provider slot (explicit list or implicit-all
        with 2+ candidates) this returns all providers in the resolved order.

        Raises `CapabilityBindingError` (or `VersionCompatError`) for any
        non-resolvable state in the same way `resolve()` does.
        """
        resolution = self.describe(slot)
        slot_candidates = list(resolution.candidates)

        if resolution.binding_kind in ("invalid", "incompatible"):
            assert resolution.error is not None
            from winter_cli.modules.capability.version_compat import VersionCompatError

            if resolution.binding_kind == "incompatible":
                raise VersionCompatError(
                    resolution.error + " Run `winter capabilities` to see all candidates and their binding state."
                )
            raise CapabilityBindingError(
                resolution.error + " Run `winter capabilities` to see all candidates and their binding state."
            )

        # Multi-provider path: explicit list OR implicit-all (2+ candidates).
        if resolution.bound_extensions:
            result: list[ResolvedCapability] = []
            for name in resolution.bound_extensions:
                candidate = next((c for c in slot_candidates if c.extension_name == name), None)
                if candidate is None:
                    raise CapabilityBindingError(
                        f"capabilities.{slot.value} provider {name!r} has no matching candidate."
                        " Run `winter capabilities` to see all candidates and their binding state."
                    )
                result.append(
                    ResolvedCapability(
                        slot=slot,
                        extension_name=candidate.extension_name,
                        entrypoint=candidate.entrypoint_path,
                        ext_dir=candidate.ext_dir,
                        prefix=candidate.prefix,
                    )
                )
            return result

        # Single-provider path — delegate to resolve() and wrap in a list.
        return [self.resolve(slot)]

    def resolve(self, slot: CapabilitySlot) -> ResolvedCapability:
        """Resolve the single winning provider for `slot`, or raise.

        This is the dispatch path. Raises `CapabilityBindingError` for any
        non-resolvable state: invalid binding, or zero providers (unbound).
        Two or more providers with no explicit binding resolve to implicit-all;
        this method returns the first in sorted order.
        """
        resolution = self.describe(slot)
        slot_candidates = list(resolution.candidates)
        bound = resolution.bound_extension

        if resolution.binding_kind == "invalid":
            assert resolution.error is not None
            raise CapabilityBindingError(
                resolution.error + " Run `winter capabilities` to see all candidates and their binding state."
            )

        if resolution.binding_kind == "incompatible":
            assert resolution.error is not None
            raise VersionCompatError(
                resolution.error + " Run `winter capabilities` to see all candidates and their binding state."
            )

        if resolution.binding_kind == "explicit":
            assert bound is not None
            candidate = next((c for c in slot_candidates if c.extension_name == bound), None)
            if candidate is None:
                raise CapabilityBindingError(
                    f"capabilities.{slot.value} explicit binding {bound!r} has no matching candidate."
                    " Run `winter capabilities` to see all candidates and their binding state."
                )
            return ResolvedCapability(
                slot=slot,
                extension_name=candidate.extension_name,
                entrypoint=candidate.entrypoint_path,
                ext_dir=candidate.ext_dir,
                prefix=candidate.prefix,
            )

        if resolution.binding_kind == "implicit":
            # Pick the first provider. For a sole candidate that's slot_candidates[0].
            # For implicit-all (2+ candidates), bound_extensions is set in sorted order;
            # return the first entry there.
            if resolution.bound_extensions:
                first_name = resolution.bound_extensions[0]
                candidate = next((c for c in slot_candidates if c.extension_name == first_name), None)
                if candidate is None:
                    raise CapabilityBindingError(
                        f"capabilities.{slot.value} provider {first_name!r} has no matching candidate."
                        " Run `winter capabilities` to see all candidates and their binding state."
                    )
            else:
                candidate = slot_candidates[0]
            if not candidate.entrypoint_valid:
                raise CapabilityBindingError(
                    f"capabilities.{slot.value} provider {candidate.extension_name!r} entrypoint not found"
                    f" at {candidate.entrypoint_path}."
                )
            return ResolvedCapability(
                slot=slot,
                extension_name=candidate.extension_name,
                entrypoint=candidate.entrypoint_path,
                ext_dir=candidate.ext_dir,
                prefix=candidate.prefix,
            )

        # binding_kind == "unbound" → zero providers.
        raise CapabilityBindingError(
            f"no extension provides the {slot.value!r} capability — install an extension"
            f" whose winter-ext.toml declares provides.{slot.value}."
        )
