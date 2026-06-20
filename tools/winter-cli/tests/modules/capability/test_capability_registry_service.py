from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import FakeConfigFileReader, FakeFilesystem, FakeSpecLoader
from winter_cli.modules.capability.capability_registry_service import CapabilityRegistryService
from winter_cli.modules.capability.models import CapabilityBindingError, CapabilitySlot, ResolvedCapability
from winter_cli.modules.capability.version_compat import VersionCompatError
from winter_cli.modules.workspace.extension_manifest import EXT_MANIFEST, ExtensionManifestLoader
from winter_cli.modules.workspace.models import StandaloneRepository

WS = Path("/ws")
TMUX = WS / "winter-service-tmux"
DOCKER = WS / "winter-service-docker"


class _StubRepoFactory:
    def __init__(self, repos: list[StandaloneRepository]) -> None:
        self._repos = repos

    def get_standalone_repos(self) -> list[StandaloneRepository]:
        return self._repos


def _registry(
    *,
    repos: list[StandaloneRepository],
    manifests: dict[Path, dict],
    files: dict[Path, str],
    bindings: dict[str, list[str]] | None = None,
    spec_loader: FakeSpecLoader | None = None,
) -> CapabilityRegistryService:
    loader = ExtensionManifestLoader(config_file_reader=FakeConfigFileReader(manifests))
    return CapabilityRegistryService(
        repo_factory=_StubRepoFactory(repos),
        manifest_loader=loader,
        bindings=bindings or {},
        fs=FakeFilesystem(files=files),
        spec_loader=spec_loader or FakeSpecLoader(),
    )


def _tmux_repo() -> StandaloneRepository:
    return StandaloneRepository(name="winter-service-tmux", path=TMUX)


def _docker_repo() -> StandaloneRepository:
    return StandaloneRepository(name="winter-service-docker", path=DOCKER)


def _tmux_manifest(entrypoint: str = "workflow/service") -> dict:
    return {"provides": {"service": entrypoint}}


def _docker_manifest(entrypoint: str = "workflow/service") -> dict:
    return {"provides": {"service": entrypoint}}


# ── 1. Explicit binding to a valid providing extension ────────────────────────


def test_explicit_binding_describe_kind() -> None:
    repo = _tmux_repo()
    entrypoint = TMUX / "workflow/service"
    reg = _registry(
        repos=[repo],
        manifests={TMUX / EXT_MANIFEST: _tmux_manifest()},
        files={TMUX / EXT_MANIFEST: "", entrypoint: ""},
        bindings={"service": ["winter-service-tmux"]},
    )
    resolution = reg.describe(CapabilitySlot.service)
    assert resolution.binding_kind == "explicit"
    assert resolution.bound_extension == "winter-service-tmux"
    assert resolution.error is None


def test_explicit_binding_resolve_returns_candidate() -> None:
    repo = _tmux_repo()
    entrypoint = TMUX / "workflow/service"
    reg = _registry(
        repos=[repo],
        manifests={TMUX / EXT_MANIFEST: _tmux_manifest()},
        files={TMUX / EXT_MANIFEST: "", entrypoint: ""},
        bindings={"service": ["winter-service-tmux"]},
    )
    resolved = reg.resolve(CapabilitySlot.service)
    assert isinstance(resolved, ResolvedCapability)
    assert resolved.extension_name == "winter-service-tmux"
    assert resolved.entrypoint == entrypoint
    assert resolved.ext_dir == TMUX


# ── 2. Implicit single provider, no binding ───────────────────────────────────


def test_implicit_single_provider_describe_kind() -> None:
    repo = _tmux_repo()
    entrypoint = TMUX / "workflow/service"
    reg = _registry(
        repos=[repo],
        manifests={TMUX / EXT_MANIFEST: _tmux_manifest()},
        files={TMUX / EXT_MANIFEST: "", entrypoint: ""},
    )
    resolution = reg.describe(CapabilitySlot.service)
    assert resolution.binding_kind == "implicit"
    assert resolution.bound_extension is None
    assert resolution.error is None
    assert len(resolution.candidates) == 1


def test_implicit_single_provider_resolve_returns_sole_provider() -> None:
    repo = _tmux_repo()
    entrypoint = TMUX / "workflow/service"
    reg = _registry(
        repos=[repo],
        manifests={TMUX / EXT_MANIFEST: _tmux_manifest()},
        files={TMUX / EXT_MANIFEST: "", entrypoint: ""},
    )
    resolved = reg.resolve(CapabilitySlot.service)
    assert resolved.extension_name == "winter-service-tmux"
    assert resolved.entrypoint == entrypoint


# ── 3. Two providers, no binding → implicit-all (R3 behavior change) ──────────


def test_two_providers_no_binding_describe_is_implicit_all() -> None:
    """With R3: 2 providers, no config binding → implicit (not unbound/ambiguous).
    Both providers are bound; bound_extensions carries them in sorted name order."""
    tmux = _tmux_repo()
    docker = _docker_repo()
    tmux_ep = TMUX / "workflow/service"
    docker_ep = DOCKER / "workflow/service"
    reg = _registry(
        repos=[tmux, docker],
        manifests={
            TMUX / EXT_MANIFEST: _tmux_manifest(),
            DOCKER / EXT_MANIFEST: _docker_manifest(),
        },
        files={
            TMUX / EXT_MANIFEST: "",
            tmux_ep: "",
            DOCKER / EXT_MANIFEST: "",
            docker_ep: "",
        },
    )
    resolution = reg.describe(CapabilitySlot.service)
    assert resolution.binding_kind == "implicit"
    assert resolution.is_ambiguous is False
    assert len(resolution.candidates) == 2
    # Both are carried in bound_extensions (sorted order).
    assert set(resolution.bound_extensions) == {"winter-service-tmux", "winter-service-docker"}


def test_two_providers_no_binding_resolve_returns_first_alphabetically() -> None:
    """With R3: 2+ implicit candidates → resolve() returns the first in sorted order."""
    tmux = _tmux_repo()
    docker = _docker_repo()
    tmux_ep = TMUX / "workflow/service"
    docker_ep = DOCKER / "workflow/service"
    reg = _registry(
        repos=[tmux, docker],
        manifests={
            TMUX / EXT_MANIFEST: _tmux_manifest(),
            DOCKER / EXT_MANIFEST: _docker_manifest(),
        },
        files={
            TMUX / EXT_MANIFEST: "",
            tmux_ep: "",
            DOCKER / EXT_MANIFEST: "",
            docker_ep: "",
        },
    )
    resolved = reg.resolve(CapabilitySlot.service)
    # "winter-service-docker" sorts before "winter-service-tmux"
    assert resolved.extension_name == "winter-service-docker"


def test_two_providers_no_binding_resolve_all_returns_both() -> None:
    """With R3: 2 implicit candidates → resolve_all() returns both in sorted order."""
    tmux = _tmux_repo()
    docker = _docker_repo()
    tmux_ep = TMUX / "workflow/service"
    docker_ep = DOCKER / "workflow/service"
    reg = _registry(
        repos=[tmux, docker],
        manifests={
            TMUX / EXT_MANIFEST: _tmux_manifest(),
            DOCKER / EXT_MANIFEST: _docker_manifest(),
        },
        files={
            TMUX / EXT_MANIFEST: "",
            tmux_ep: "",
            DOCKER / EXT_MANIFEST: "",
            docker_ep: "",
        },
    )
    resolved_list = reg.resolve_all(CapabilitySlot.service)
    assert len(resolved_list) == 2
    names = [r.extension_name for r in resolved_list]
    assert sorted(names) == names  # deterministic sorted order


# ── 4. Binding names an extension that is not installed ───────────────────────


def test_binding_to_uninstalled_extension_describe_invalid() -> None:
    repo = _tmux_repo()
    entrypoint = TMUX / "workflow/service"
    reg = _registry(
        repos=[repo],
        manifests={TMUX / EXT_MANIFEST: _tmux_manifest()},
        files={TMUX / EXT_MANIFEST: "", entrypoint: ""},
        bindings={"service": ["winter-service-docker"]},  # not installed
    )
    resolution = reg.describe(CapabilitySlot.service)
    assert resolution.binding_kind == "invalid"
    assert resolution.bound_extension == "winter-service-docker"
    assert resolution.error is not None
    assert "no installed extension named" in resolution.error


def test_binding_to_uninstalled_extension_resolve_raises() -> None:
    repo = _tmux_repo()
    entrypoint = TMUX / "workflow/service"
    reg = _registry(
        repos=[repo],
        manifests={TMUX / EXT_MANIFEST: _tmux_manifest()},
        files={TMUX / EXT_MANIFEST: "", entrypoint: ""},
        bindings={"service": ["winter-service-docker"]},
    )
    with pytest.raises(CapabilityBindingError, match="no installed extension named"):
        reg.resolve(CapabilitySlot.service)


# ── 5. Binding names an installed extension that does NOT provide the slot ────


def test_binding_to_non_providing_extension_describe_invalid() -> None:
    repo = _tmux_repo()
    # Manifest with no provides.service
    reg = _registry(
        repos=[repo],
        manifests={TMUX / EXT_MANIFEST: {}},  # no provides at all
        files={TMUX / EXT_MANIFEST: ""},
        bindings={"service": ["winter-service-tmux"]},
    )
    resolution = reg.describe(CapabilitySlot.service)
    assert resolution.binding_kind == "invalid"
    assert "installed but declares no provides.service" in (resolution.error or "")


def test_binding_to_non_providing_extension_resolve_raises() -> None:
    repo = _tmux_repo()
    reg = _registry(
        repos=[repo],
        manifests={TMUX / EXT_MANIFEST: {}},
        files={TMUX / EXT_MANIFEST: ""},
        bindings={"service": ["winter-service-tmux"]},
    )
    with pytest.raises(CapabilityBindingError, match=r"installed but declares no provides\.service"):
        reg.resolve(CapabilitySlot.service)


# ── 6. Binding valid but entrypoint file missing ──────────────────────────────


def test_binding_valid_entrypoint_missing_describe_invalid() -> None:
    repo = _tmux_repo()
    reg = _registry(
        repos=[repo],
        manifests={TMUX / EXT_MANIFEST: _tmux_manifest()},
        files={TMUX / EXT_MANIFEST: ""},  # entrypoint file NOT seeded
        bindings={"service": ["winter-service-tmux"]},
    )
    resolution = reg.describe(CapabilitySlot.service)
    assert resolution.binding_kind == "invalid"
    assert "entrypoint not found" in (resolution.error or "")


def test_binding_valid_entrypoint_missing_resolve_raises() -> None:
    repo = _tmux_repo()
    reg = _registry(
        repos=[repo],
        manifests={TMUX / EXT_MANIFEST: _tmux_manifest()},
        files={TMUX / EXT_MANIFEST: ""},
        bindings={"service": ["winter-service-tmux"]},
    )
    with pytest.raises(CapabilityBindingError, match="entrypoint not found"):
        reg.resolve(CapabilitySlot.service)


# ── 7. Zero providers, no binding ─────────────────────────────────────────────


def test_zero_providers_no_binding_resolve_raises_no_extension() -> None:
    reg = _registry(repos=[], manifests={}, files={})
    with pytest.raises(CapabilityBindingError, match="no extension provides"):
        reg.resolve(CapabilitySlot.service)


def test_zero_providers_no_binding_describe_unbound() -> None:
    reg = _registry(repos=[], manifests={}, files={})
    resolution = reg.describe(CapabilitySlot.service)
    assert resolution.binding_kind == "unbound"
    assert resolution.is_ambiguous is False


# ── 8. Back-compat: orchestrate_services shim flows through the registry ──────


def test_orchestrate_services_backcompat_discovered_as_service_candidate() -> None:
    """An extension declaring only `orchestrate_services` (no `provides.service`)
    is still discovered as a service candidate via the manifest shim."""
    repo = _tmux_repo()
    entrypoint = TMUX / "workflow/orchestrate"
    reg = _registry(
        repos=[repo],
        manifests={TMUX / EXT_MANIFEST: {"orchestrate_services": "workflow/orchestrate"}},
        files={TMUX / EXT_MANIFEST: "", entrypoint: ""},
    )
    resolution = reg.describe(CapabilitySlot.service)
    assert resolution.binding_kind == "implicit"
    assert len(resolution.candidates) == 1
    assert resolution.candidates[0].extension_name == "winter-service-tmux"
    assert resolution.candidates[0].entrypoint_rel == "workflow/orchestrate"

    resolved = reg.resolve(CapabilitySlot.service)
    assert resolved.entrypoint == entrypoint
    assert resolved.extension_name == "winter-service-tmux"


# ── 9. Version compat: provider implements v2, winter only supports {v1} ──────


def test_incompatible_version_describe_returns_incompatible_kind() -> None:
    """Provider declares implements.service = v2; winter supports {v1} only →
    describe() reports binding_kind == 'incompatible' with an error message."""
    repo = _tmux_repo()
    entrypoint = TMUX / "workflow/service"
    reg = _registry(
        repos=[repo],
        manifests={TMUX / EXT_MANIFEST: {"provides": {"service": "workflow/service"}, "implements": {"service": "v2"}}},
        files={TMUX / EXT_MANIFEST: "", entrypoint: ""},
        bindings={"service": ["winter-service-tmux"]},
        spec_loader=FakeSpecLoader({"service": {"v1"}}),  # winter only ships v1
    )
    resolution = reg.describe(CapabilitySlot.service)
    assert resolution.binding_kind == "incompatible"
    assert resolution.error is not None
    assert "v2" in resolution.error
    assert "v1" in resolution.error


def test_incompatible_version_resolve_raises_version_compat_error() -> None:
    """resolve() raises VersionCompatError naming both versions when bound provider
    implements an unsupported spec version."""
    repo = _tmux_repo()
    entrypoint = TMUX / "workflow/service"
    reg = _registry(
        repos=[repo],
        manifests={TMUX / EXT_MANIFEST: {"provides": {"service": "workflow/service"}, "implements": {"service": "v2"}}},
        files={TMUX / EXT_MANIFEST: "", entrypoint: ""},
        bindings={"service": ["winter-service-tmux"]},
        spec_loader=FakeSpecLoader({"service": {"v1"}}),
    )
    with pytest.raises(VersionCompatError) as exc_info:
        reg.resolve(CapabilitySlot.service)
    msg = str(exc_info.value)
    assert "v2" in msg
    assert "v1" in msg


def test_incompatible_version_resolve_raises_is_capability_binding_error() -> None:
    """VersionCompatError is a CapabilityBindingError — callers matching the broad
    type still catch it."""
    repo = _tmux_repo()
    entrypoint = TMUX / "workflow/service"
    reg = _registry(
        repos=[repo],
        manifests={TMUX / EXT_MANIFEST: {"provides": {"service": "workflow/service"}, "implements": {"service": "v2"}}},
        files={TMUX / EXT_MANIFEST: "", entrypoint: ""},
        bindings={"service": ["winter-service-tmux"]},
        spec_loader=FakeSpecLoader({"service": {"v1"}}),
    )
    with pytest.raises(CapabilityBindingError):
        reg.resolve(CapabilitySlot.service)


# ── 10. Version compat: provider implements v1, winter supports {v1} ─────────


def test_compatible_version_v1_describe_returns_explicit() -> None:
    """Provider declares implements.service = v1; winter supports {v1} → compatible,
    binding_kind == 'explicit'."""
    repo = _tmux_repo()
    entrypoint = TMUX / "workflow/service"
    reg = _registry(
        repos=[repo],
        manifests={TMUX / EXT_MANIFEST: {"provides": {"service": "workflow/service"}, "implements": {"service": "v1"}}},
        files={TMUX / EXT_MANIFEST: "", entrypoint: ""},
        bindings={"service": ["winter-service-tmux"]},
        spec_loader=FakeSpecLoader({"service": {"v1"}}),
    )
    resolution = reg.describe(CapabilitySlot.service)
    assert resolution.binding_kind == "explicit"
    assert resolution.error is None


def test_compatible_version_v1_resolve_returns_resolved_capability() -> None:
    repo = _tmux_repo()
    entrypoint = TMUX / "workflow/service"
    reg = _registry(
        repos=[repo],
        manifests={TMUX / EXT_MANIFEST: {"provides": {"service": "workflow/service"}, "implements": {"service": "v1"}}},
        files={TMUX / EXT_MANIFEST: "", entrypoint: ""},
        bindings={"service": ["winter-service-tmux"]},
        spec_loader=FakeSpecLoader({"service": {"v1"}}),
    )
    resolved = reg.resolve(CapabilitySlot.service)
    assert isinstance(resolved, ResolvedCapability)
    assert resolved.extension_name == "winter-service-tmux"


# ── 11. Backwards compat: no implements declaration → compatible ───────────────


def test_no_implements_declaration_describe_returns_explicit() -> None:
    """Extension with no [implements] table → backwards compat, treated as
    compatible (lenient-when-absent). This is the winter-service-tmux case."""
    repo = _tmux_repo()
    entrypoint = TMUX / "workflow/service"
    reg = _registry(
        repos=[repo],
        # No "implements" key in manifest at all — predates the field
        manifests={TMUX / EXT_MANIFEST: {"provides": {"service": "workflow/service"}}},
        files={TMUX / EXT_MANIFEST: "", entrypoint: ""},
        bindings={"service": ["winter-service-tmux"]},
        spec_loader=FakeSpecLoader({"service": {"v1"}}),
    )
    resolution = reg.describe(CapabilitySlot.service)
    assert resolution.binding_kind == "explicit"
    assert resolution.error is None


def test_no_implements_declaration_resolve_returns_resolved_capability() -> None:
    """Extension with no [implements] → resolves cleanly without version check."""
    repo = _tmux_repo()
    entrypoint = TMUX / "workflow/service"
    reg = _registry(
        repos=[repo],
        manifests={TMUX / EXT_MANIFEST: {"provides": {"service": "workflow/service"}}},
        files={TMUX / EXT_MANIFEST: "", entrypoint: ""},
        bindings={"service": ["winter-service-tmux"]},
        spec_loader=FakeSpecLoader({"service": {"v1"}}),
    )
    resolved = reg.resolve(CapabilitySlot.service)
    assert isinstance(resolved, ResolvedCapability)
    assert resolved.extension_name == "winter-service-tmux"


# ── 12. capabilities.<slot> = list binding (R2/R3) ───────────────────────────


def test_explicit_list_describe_returns_explicit_with_bound_extensions() -> None:
    """capabilities.service = ["tmux", "docker"] → describe() returns explicit with
    bound_extensions carrying both in declared order."""
    tmux = _tmux_repo()
    docker = _docker_repo()
    tmux_ep = TMUX / "workflow/service"
    docker_ep = DOCKER / "workflow/service"
    reg = _registry(
        repos=[tmux, docker],
        manifests={
            TMUX / EXT_MANIFEST: _tmux_manifest(),
            DOCKER / EXT_MANIFEST: _docker_manifest(),
        },
        files={
            TMUX / EXT_MANIFEST: "",
            tmux_ep: "",
            DOCKER / EXT_MANIFEST: "",
            docker_ep: "",
        },
        bindings={"service": ["winter-service-tmux", "winter-service-docker"]},
    )
    resolution = reg.describe(CapabilitySlot.service)
    assert resolution.binding_kind == "explicit"
    assert resolution.bound_extension == "winter-service-tmux"
    assert resolution.bound_extensions == ("winter-service-tmux", "winter-service-docker")
    assert resolution.error is None


def test_explicit_list_resolve_all_returns_providers_in_order() -> None:
    """resolve_all() with an explicit list returns both providers in declared order."""
    tmux = _tmux_repo()
    docker = _docker_repo()
    tmux_ep = TMUX / "workflow/service"
    docker_ep = DOCKER / "workflow/service"
    reg = _registry(
        repos=[tmux, docker],
        manifests={
            TMUX / EXT_MANIFEST: _tmux_manifest(),
            DOCKER / EXT_MANIFEST: _docker_manifest(),
        },
        files={
            TMUX / EXT_MANIFEST: "",
            tmux_ep: "",
            DOCKER / EXT_MANIFEST: "",
            docker_ep: "",
        },
        bindings={"service": ["winter-service-tmux", "winter-service-docker"]},
    )
    resolved_list = reg.resolve_all(CapabilitySlot.service)
    assert len(resolved_list) == 2
    assert resolved_list[0].extension_name == "winter-service-tmux"
    assert resolved_list[0].entrypoint == tmux_ep
    assert resolved_list[1].extension_name == "winter-service-docker"
    assert resolved_list[1].entrypoint == docker_ep


def test_explicit_list_reverse_order_resolve_all_returns_docker_first() -> None:
    """Ordering is config-declared: docker first → resolve_all()[0] is docker."""
    tmux = _tmux_repo()
    docker = _docker_repo()
    tmux_ep = TMUX / "workflow/service"
    docker_ep = DOCKER / "workflow/service"
    reg = _registry(
        repos=[tmux, docker],
        manifests={
            TMUX / EXT_MANIFEST: _tmux_manifest(),
            DOCKER / EXT_MANIFEST: _docker_manifest(),
        },
        files={
            TMUX / EXT_MANIFEST: "",
            tmux_ep: "",
            DOCKER / EXT_MANIFEST: "",
            docker_ep: "",
        },
        bindings={"service": ["winter-service-docker", "winter-service-tmux"]},
    )
    resolved_list = reg.resolve_all(CapabilitySlot.service)
    assert resolved_list[0].extension_name == "winter-service-docker"
    assert resolved_list[1].extension_name == "winter-service-tmux"


def test_single_string_back_compat_resolve_all_returns_one_element_list() -> None:
    """Single-element list binding → resolve_all() returns a one-element list."""
    repo = _tmux_repo()
    entrypoint = TMUX / "workflow/service"
    reg = _registry(
        repos=[repo],
        manifests={TMUX / EXT_MANIFEST: _tmux_manifest()},
        files={TMUX / EXT_MANIFEST: "", entrypoint: ""},
        bindings={"service": ["winter-service-tmux"]},
    )
    resolved_list = reg.resolve_all(CapabilitySlot.service)
    assert len(resolved_list) == 1
    assert resolved_list[0].extension_name == "winter-service-tmux"


def test_implicit_single_provider_resolve_all_returns_one_element_list() -> None:
    """Implicit sole provider → resolve_all() returns a one-element list."""
    repo = _tmux_repo()
    entrypoint = TMUX / "workflow/service"
    reg = _registry(
        repos=[repo],
        manifests={TMUX / EXT_MANIFEST: _tmux_manifest()},
        files={TMUX / EXT_MANIFEST: "", entrypoint: ""},
    )
    resolved_list = reg.resolve_all(CapabilitySlot.service)
    assert len(resolved_list) == 1
    assert resolved_list[0].extension_name == "winter-service-tmux"


def test_explicit_list_invalid_member_describe_returns_invalid() -> None:
    """An invalid member in capabilities.service list → describe() returns invalid."""
    tmux = _tmux_repo()
    docker = _docker_repo()
    tmux_ep = TMUX / "workflow/service"
    reg = _registry(
        repos=[tmux, docker],
        manifests={
            TMUX / EXT_MANIFEST: _tmux_manifest(),
            DOCKER / EXT_MANIFEST: _docker_manifest(),
        },
        files={
            TMUX / EXT_MANIFEST: "",
            tmux_ep: "",
            DOCKER / EXT_MANIFEST: "",
            # docker_ep intentionally missing — entrypoint invalid
        },
        bindings={"service": ["winter-service-tmux", "winter-service-docker"]},
    )
    resolution = reg.describe(CapabilitySlot.service)
    assert resolution.binding_kind == "invalid"
    assert "winter-service-docker" in (resolution.error or "")


def test_explicit_list_uninstalled_member_describe_returns_invalid() -> None:
    """An uninstalled member in capabilities.service list → describe() returns invalid."""
    tmux = _tmux_repo()
    entrypoint = TMUX / "workflow/service"
    reg = _registry(
        repos=[tmux],
        manifests={TMUX / EXT_MANIFEST: _tmux_manifest()},
        files={TMUX / EXT_MANIFEST: "", entrypoint: ""},
        bindings={"service": ["winter-service-tmux", "winter-service-docker"]},  # docker not installed
    )
    resolution = reg.describe(CapabilitySlot.service)
    assert resolution.binding_kind == "invalid"
    assert "winter-service-docker" in (resolution.error or "")


def test_explicit_list_single_entry_bound_extensions_has_one_element() -> None:
    """A one-entry capabilities.service list populates bound_extensions with one element."""
    repo = _tmux_repo()
    entrypoint = TMUX / "workflow/service"
    reg = _registry(
        repos=[repo],
        manifests={TMUX / EXT_MANIFEST: _tmux_manifest()},
        files={TMUX / EXT_MANIFEST: "", entrypoint: ""},
        bindings={"service": ["winter-service-tmux"]},
    )
    resolution = reg.describe(CapabilitySlot.service)
    assert resolution.binding_kind == "explicit"
    assert resolution.bound_extensions == ("winter-service-tmux",)


def test_implicit_single_provider_bound_extensions_is_empty_tuple() -> None:
    """Implicit sole provider (no config binding) → bound_extensions is an empty tuple."""
    repo = _tmux_repo()
    entrypoint = TMUX / "workflow/service"
    reg = _registry(
        repos=[repo],
        manifests={TMUX / EXT_MANIFEST: _tmux_manifest()},
        files={TMUX / EXT_MANIFEST: "", entrypoint: ""},
    )
    resolution = reg.describe(CapabilitySlot.service)
    assert resolution.bound_extensions == ()


# ── 13. Error messages reference capabilities.<slot>, not service_orchestrators ─


def test_invalid_list_member_error_references_capabilities_slot() -> None:
    """Error for an invalid member references capabilities.service, not service_orchestrators."""
    tmux = _tmux_repo()
    entrypoint = TMUX / "workflow/service"
    reg = _registry(
        repos=[tmux],
        manifests={TMUX / EXT_MANIFEST: _tmux_manifest()},
        files={TMUX / EXT_MANIFEST: "", entrypoint: ""},
        bindings={"service": ["winter-service-tmux", "winter-service-docker"]},  # docker not installed
    )
    resolution = reg.describe(CapabilitySlot.service)
    assert "capabilities.service" in (resolution.error or "")
    assert "service_orchestrators" not in (resolution.error or "")
