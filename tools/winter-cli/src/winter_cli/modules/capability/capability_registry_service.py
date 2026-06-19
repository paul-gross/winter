from __future__ import annotations

import logging

from winter_cli.core.filesystem import IFilesystemReader
from winter_cli.modules.capability.models import (
    BindingKind,
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
    """Resolves which extension provides each capability slot.

    Combines three inputs — manifest `[provides]`, config `[capabilities]` bindings,
    and the installed-extension set — to determine the provider for each slot.
    Mirrors `GraphService` in constructor shape and manifest-load tolerance (bad
    manifests are skipped with a warning rather than aborting).

    `bindings` is the parsed `[capabilities]` table from `.winter/config.toml`
    (slot name → extension name). Do not inject `WorkspaceConfig` directly; the
    caller (container) extracts this dict and passes it in.
    """

    def __init__(
        self,
        repo_factory: IStandaloneRepoProvider,
        manifest_loader: ExtensionManifestLoader,
        bindings: dict[str, str],
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
        Derives `binding_kind`, `bound_extension`, and `error` from the installed
        candidates and the config binding. Never raises; use `resolve()` for the
        dispatch path that raises on non-resolvable states.
        """
        slot_candidates = self.candidates(slot)
        bound = self._bindings.get(slot.value)

        binding_kind: BindingKind
        bound_extension: str | None
        error: str | None

        if bound is not None:
            bound_extension = bound
            matching = [c for c in slot_candidates if c.extension_name == bound]
            if not matching:
                # Determine whether `bound` names any installed extension at all.
                all_names = {r.name for r in self._repo_factory.get_standalone_repos()}
                if bound in all_names:
                    error = (
                        f"capabilities.{slot.value} = {bound!r} — extension {bound!r} is installed"
                        f" but declares no provides.{slot.value} in its winter-ext.toml."
                    )
                else:
                    error = (
                        f"capabilities.{slot.value} = {bound!r} — no installed extension named {bound!r}"
                        f" (capabilities.{slot.value} must name a [[standalone_repository]])."
                    )
                binding_kind = "invalid"
            else:
                candidate = matching[0]
                if not candidate.entrypoint_valid:
                    binding_kind = "invalid"
                    error = (
                        f"capabilities.{slot.value} provider {bound!r} entrypoint not found"
                        f" at {candidate.entrypoint_path}."
                    )
                else:
                    # Check version compatibility for the bound provider.
                    compat_error = self._check_version_compat(slot, candidate)
                    if compat_error is not None:
                        binding_kind = "incompatible"
                        error = compat_error
                    else:
                        binding_kind = "explicit"
                        error = None
        else:
            bound_extension = None
            if len(slot_candidates) == 0:
                binding_kind = "unbound"
                error = None
            elif len(slot_candidates) == 1:
                binding_kind = "implicit"
                error = None
            else:
                binding_kind = "unbound"
                error = None

        return SlotResolution(
            slot=slot,
            candidates=tuple(slot_candidates),
            bound_extension=bound_extension,
            binding_kind=binding_kind,
            error=error,
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

    def resolve(self, slot: CapabilitySlot) -> ResolvedCapability:
        """Resolve the single winning provider for `slot`, or raise.

        This is the dispatch path. Raises `CapabilityBindingError` for any
        non-resolvable state: invalid binding, zero providers, or ambiguous
        (two or more providers with no explicit binding).
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

        # binding_kind == "unbound"
        if len(slot_candidates) == 0:
            raise CapabilityBindingError(
                f"no extension provides the {slot.value!r} capability — install an extension"
                f" whose winter-ext.toml declares provides.{slot.value}."
            )

        # Ambiguous: ≥2 candidates, no binding.
        names = [c.extension_name for c in slot_candidates]
        names_str = f"{names[0]} and {names[1]}" if len(names) == 2 else ", ".join(names[:-1]) + f", and {names[-1]}"
        raise CapabilityBindingError(
            f"capabilities.{slot.value} is ambiguous — {names_str} both provide it;"
            f' bind one explicitly with `capabilities.{slot.value} = "<name>"` in .winter/config.toml.'
            " Run `winter capabilities` to see all candidates and their binding state."
        )
