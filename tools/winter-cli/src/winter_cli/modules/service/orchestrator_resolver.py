from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from winter_cli.core.filesystem import IFilesystemReader
from winter_cli.modules.capability.capability_registry_service import CapabilityRegistryService
from winter_cli.modules.capability.models import CapabilitySlot, ResolvedCapability
from winter_cli.modules.workspace.extension_manifest import EXT_MANIFEST, ExtensionManifestLoader
from winter_cli.modules.workspace.models import RepoError, StandaloneRepository
from winter_cli.modules.workspace.repository_factory import IStandaloneRepoProvider


@dataclass(frozen=True)
class ResolvedOrchestrator:
    entrypoint: Path
    ext_dir: Path
    prefix: str


class ServiceOrchestratorResolver:
    """Resolves the registered service orchestrator entrypoint.

    Config/name resolution flows through `CapabilityRegistryService`, which handles
    the no-provider, invalid-binding, and ambiguous-provider failure cases. The
    override branch (path-mode and bare-name) is the local-checkout affordance that
    bypasses the registry entirely.

    When `override` is supplied (from `--service-orchestrator` or
    `WINTER_SERVICE_ORCHESTRATOR`), it takes precedence over the registry and is
    interpreted as a **local path** when it contains an `os.sep` or resolves to an
    existing directory on disk. A bare name with no path separator falls through to
    the normal registered-extension lookup (`_resolve_name`), also bypassing the
    registry.

    The override branch failures each raise a distinct `RepoError`:
      1. the path override directory does not exist,
      2. the path override directory has no `winter-ext.toml`,
      3. the matched extension declares no `orchestrate_services` entrypoint,
      4. the declared entrypoint file is missing on disk.

    Shared by `ServiceDispatchService` and `ServiceLogsService` so orchestrator
    resolution logic is not duplicated.
    """

    def __init__(
        self,
        registry: CapabilityRegistryService,
        repo_factory: IStandaloneRepoProvider,
        manifest_loader: ExtensionManifestLoader,
        fs: IFilesystemReader,
        override: str | None = None,
        workspace_root: Path | None = None,
    ) -> None:
        self._registry = registry
        self._repo_factory = repo_factory
        self._manifest_loader = manifest_loader
        self._fs = fs
        self._override = override
        self._workspace_root = workspace_root

    def resolve(self) -> ResolvedOrchestrator:
        """Return the resolved entrypoint path or raise RepoError."""
        if self._override:
            if self._is_path(self._override):
                return self._resolve_path(self._override)
            return self._resolve_name(self._override)

        resolved = self._registry.resolve(CapabilitySlot.service)
        return ResolvedOrchestrator(entrypoint=resolved.entrypoint, ext_dir=resolved.ext_dir, prefix=resolved.prefix)

    def resolve_all(self) -> list[ResolvedCapability]:
        """Return the ordered list of providers, collapsing to one when an override is active.

        When ``--service-orchestrator`` / ``WINTER_SERVICE_ORCHESTRATOR`` is set, the
        override collapses fan-out to a single provider for this invocation.  When no
        override is active, delegates to ``registry.resolve_all()`` which returns the
        full ordered list of providers (explicit or implicit-all).
        """
        if self._override:
            resolved = self.resolve()  # raises RepoError on any setup failure
            # Wrap as a ResolvedCapability so callers get a uniform list type.
            cap = ResolvedCapability(
                slot=CapabilitySlot.service,
                extension_name=resolved.prefix,
                entrypoint=resolved.entrypoint,
                ext_dir=resolved.ext_dir,
                prefix=resolved.prefix,
            )
            return [cap]
        return self._registry.resolve_all(CapabilitySlot.service)

    def try_resolve_extension(self, extension: str) -> ResolvedOrchestrator | str:
        """Non-raising resolution for a bare extension path or name.

        Accepts a local path or an installed-extension name (same path-vs-name
        semantics as the `--service-orchestrator` override), bypassing the
        registry entirely. Returns `ResolvedOrchestrator` on success, or an
        error string describing the setup failure.

        Used by `ConformanceVerifyService` so orchestrator resolution logic is
        not duplicated there.
        """
        if self._is_path(extension):
            return self._try_resolve_path(extension)
        return self._try_resolve_name(extension)

    def _try_resolve_path(self, value: str) -> ResolvedOrchestrator | str:
        """Path mode: resolve a local extension directory, returning errors as strings."""
        ext_dir = Path(value)
        if not ext_dir.is_absolute():
            base = self._workspace_root if self._workspace_root is not None else Path.cwd()
            ext_dir = base / ext_dir
        ext_dir = ext_dir.resolve()

        if not self._fs.is_dir(ext_dir):
            return self._verify_error_path_not_found(value, ext_dir)

        manifest_path = ext_dir / EXT_MANIFEST
        if not self._fs.is_file(manifest_path):
            return self._verify_error_no_manifest(value, manifest_path)

        synthetic_repo = StandaloneRepository(name=ext_dir.name, path=ext_dir)
        try:
            manifest = self._manifest_loader.load(synthetic_repo, manifest_path)
        except Exception as exc:
            return f"could not load manifest at {manifest_path}: {exc}"

        entrypoint_rel = manifest.capability_entrypoint("service")
        if not entrypoint_rel:
            return self._verify_error_no_entrypoint(value, manifest_path)

        entrypoint = ext_dir / entrypoint_rel
        if not self._fs.is_file(entrypoint):
            return self._verify_error_entrypoint_missing(value, entrypoint, entrypoint_rel, manifest_path)

        return ResolvedOrchestrator(entrypoint=entrypoint, ext_dir=ext_dir, prefix=manifest.prefix)

    def _try_resolve_name(self, name: str) -> ResolvedOrchestrator | str:
        """Name mode: look up a registered installed extension, returning errors as strings."""
        repo = self._find_extension(name)
        if repo is None:
            return self._verify_error_name_not_installed(name)

        manifest_path = repo.path / EXT_MANIFEST
        try:
            manifest = self._manifest_loader.load(repo, manifest_path)
        except Exception as exc:
            return f"could not load manifest at {manifest_path}: {exc}"

        entrypoint_rel = manifest.capability_entrypoint("service")
        if not entrypoint_rel:
            return self._verify_error_no_entrypoint(name, manifest_path)

        entrypoint = repo.path / entrypoint_rel
        if not self._fs.is_file(entrypoint):
            return self._verify_error_entrypoint_missing(name, entrypoint, entrypoint_rel, manifest_path)

        return ResolvedOrchestrator(entrypoint=entrypoint, ext_dir=repo.path, prefix=manifest.prefix)

    def _is_path(self, value: str) -> bool:
        """Return True when `value` should be treated as a local extension path.

        A value is a path when it contains a path separator OR when it
        resolves to an existing directory.  A bare registered name (e.g.
        `winter-service-tmux`) never contains a separator and does not resolve
        to a directory in the workspace, so it falls through to name-mode.
        """
        if "/" in value or os.sep in value:
            return True
        return self._fs.is_dir(Path(value))

    def _resolve_path(self, value: str) -> ResolvedOrchestrator:
        """Path mode: treat `value` as a local extension directory, skipping the
        registered-extension lookup (failures 1 and 2).  Failures 3 and 4 still
        apply so a misconfigured local checkout surfaces a clear error.
        """
        ext_dir = Path(value)
        if not ext_dir.is_absolute():
            # workspace_root is always injected from config in production;
            # Path.cwd() is a test/standalone-only fallback.
            base = self._workspace_root if self._workspace_root is not None else Path.cwd()
            ext_dir = base / ext_dir
        ext_dir = ext_dir.resolve()

        if not self._fs.is_dir(ext_dir):
            raise RepoError(f"service orchestrator override {value!r} not found — {ext_dir} is not a directory.")

        manifest_path = ext_dir / EXT_MANIFEST
        if not self._fs.is_file(manifest_path):
            raise RepoError(
                f"service orchestrator override {value!r} has no {EXT_MANIFEST} — expected at {manifest_path}."
            )

        # Build a synthetic StandaloneRepository so the manifest loader's
        # prefix-resolution logic (workspace override → manifest prefix → name → dir)
        # works the same way as for registered extensions.
        synthetic_repo = StandaloneRepository(name=ext_dir.name, path=ext_dir)
        manifest = self._manifest_loader.load(synthetic_repo, manifest_path)

        entrypoint_rel = manifest.capability_entrypoint("service")
        if not entrypoint_rel:
            raise RepoError(
                f"service orchestrator override {value!r} declares no `orchestrate_services` entrypoint — "
                f'add `orchestrate_services = "<path>"` to {manifest_path}.'
            )

        entrypoint = ext_dir / entrypoint_rel
        if not self._fs.is_file(entrypoint):
            raise RepoError(
                f"service orchestrator override {value!r} entrypoint not found at {entrypoint} "
                f'(declared as `orchestrate_services = "{entrypoint_rel}"` in {manifest_path}).'
            )

        return ResolvedOrchestrator(entrypoint=entrypoint, ext_dir=ext_dir, prefix=manifest.prefix)

    def _resolve_name(self, name: str) -> ResolvedOrchestrator:
        """Name mode: look up a registered installed extension — the original behavior."""
        repo = self._find_extension(name)
        if repo is None:
            raise RepoError(
                f"service orchestrator {name!r} is not an installed extension — "
                "`service_orchestrator` must match the name of a "
                "[[standalone_repository]] in .winter/config.toml."
            )

        manifest = self._manifest_loader.load(repo, repo.path / EXT_MANIFEST)
        entrypoint_rel = manifest.capability_entrypoint("service")
        if not entrypoint_rel:
            raise RepoError(
                f"service orchestrator {name!r} declares no `orchestrate_services` entrypoint — "
                f'add `orchestrate_services = "<path>"` to {repo.path / EXT_MANIFEST}.'
            )

        entrypoint = repo.path / entrypoint_rel
        if not self._fs.is_file(entrypoint):
            manifest_path = repo.path / EXT_MANIFEST
            raise RepoError(
                f"service orchestrator {name!r} entrypoint not found at {entrypoint} "
                f'(declared as `orchestrate_services = "{entrypoint_rel}"` in {manifest_path}).'
            )
        return ResolvedOrchestrator(entrypoint=entrypoint, ext_dir=repo.path, prefix=manifest.prefix)

    def _find_extension(self, name: str) -> StandaloneRepository | None:
        for repo in self._repo_factory.get_standalone_repos():
            if repo.name == name:
                return repo
        return None

    # Verify-mode error message templates — used by `try_resolve_extension` to
    # surface setup failures with user-readable messages that match the messages
    # produced when `ConformanceVerifyService` used its own private resolution copy.
    @staticmethod
    def _verify_error_path_not_found(value: str, ext_dir: Path) -> str:
        return f"extension path {value!r} not found — {ext_dir} is not a directory"

    @staticmethod
    def _verify_error_no_manifest(value: str, manifest_path: Path) -> str:
        return f"extension path {value!r} has no {EXT_MANIFEST} — expected at {manifest_path}"

    @staticmethod
    def _verify_error_no_entrypoint(value: str, manifest_path: Path) -> str:
        return (
            f"extension {value!r} declares no service entrypoint — "
            f'add `orchestrate_services = "<path>"` or `[provides] service = "<path>"` to {manifest_path}'
        )

    @staticmethod
    def _verify_error_entrypoint_missing(value: str, entrypoint: Path, entrypoint_rel: str, manifest_path: Path) -> str:
        return (
            f"extension {value!r} entrypoint not found at {entrypoint} "
            f"(declared as {entrypoint_rel!r} in {manifest_path})"
        )

    @staticmethod
    def _verify_error_name_not_installed(name: str) -> str:
        return (
            f"extension {name!r} is not an installed extension — "
            "it must match the name of a [[standalone_repository]] in .winter/config.toml"
        )
