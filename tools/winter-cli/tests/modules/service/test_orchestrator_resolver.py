from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import FakeConfigFileReader, FakeFilesystem, FakeSpecLoader
from winter_cli.modules.capability.capability_registry_service import CapabilityRegistryService
from winter_cli.modules.service.orchestrator_resolver import ServiceOrchestratorResolver
from winter_cli.modules.workspace.extension_manifest import EXT_MANIFEST, ExtensionManifestLoader
from winter_cli.modules.workspace.models import RepoError, StandaloneRepository

WS = Path("/ws")
EXT = WS / "local-ext"


class _StubRepoFactory:
    def __init__(self, repos: list[StandaloneRepository]) -> None:
        self._repos = repos

    def get_standalone_repos(self) -> list[StandaloneRepository]:
        return self._repos


def _resolver(
    *,
    orchestrator: str | None,
    repos: list[StandaloneRepository],
    manifests: dict[Path, dict],
    files: dict[Path, str],
    override: str | None = None,
    directories: list[Path] | None = None,
) -> ServiceOrchestratorResolver:
    loader = ExtensionManifestLoader(config_file_reader=FakeConfigFileReader(manifests))
    fs = FakeFilesystem(files=files, directories=directories or [])
    bindings: dict[str, list[str]] = {"service": [orchestrator]} if orchestrator else {}
    registry = CapabilityRegistryService(
        repo_factory=_StubRepoFactory(repos),
        manifest_loader=loader,
        bindings=bindings,
        fs=fs,
        spec_loader=FakeSpecLoader(),
    )
    return ServiceOrchestratorResolver(
        registry=registry,
        repo_factory=_StubRepoFactory(repos),
        manifest_loader=loader,
        fs=fs,
        override=override,
    )


def _tmux_repo() -> StandaloneRepository:
    return StandaloneRepository(name="winter-service-tmux", path=WS / "winter-service-tmux")


def _configured() -> ServiceOrchestratorResolver:
    repo = _tmux_repo()
    entrypoint = repo.path / "workflow/service"
    return _resolver(
        orchestrator="winter-service-tmux",
        repos=[repo],
        manifests={repo.path / EXT_MANIFEST: {"orchestrate_services": "workflow/service"}},
        files={repo.path / EXT_MANIFEST: "", entrypoint: ""},
    )


# ── name mode: existing behaviour (regression) ───────────────────────────────


def test_resolve_returns_correct_entrypoint_path() -> None:
    resolved = _configured().resolve()
    assert resolved.entrypoint == WS / "winter-service-tmux/workflow/service"
    assert resolved.ext_dir == WS / "winter-service-tmux"
    assert resolved.prefix == "winter-service-tmux"


def test_no_orchestrator_registered_raises() -> None:
    res = _resolver(orchestrator=None, repos=[], manifests={}, files={})
    with pytest.raises(RepoError, match="no extension provides"):
        res.resolve()


def test_unknown_extension_name_raises() -> None:
    res = _resolver(orchestrator="winter-service-docker", repos=[_tmux_repo()], manifests={}, files={})
    with pytest.raises(RepoError, match="no installed extension named"):
        res.resolve()


def test_extension_missing_orchestrate_services_key_raises() -> None:
    repo = _tmux_repo()
    res = _resolver(
        orchestrator="winter-service-tmux",
        repos=[repo],
        manifests={repo.path / EXT_MANIFEST: {}},
        files={repo.path / EXT_MANIFEST: ""},
    )
    with pytest.raises(RepoError, match=r"declares no provides\.service"):
        res.resolve()


def test_missing_entrypoint_file_raises() -> None:
    repo = _tmux_repo()
    res = _resolver(
        orchestrator="winter-service-tmux",
        repos=[repo],
        manifests={repo.path / EXT_MANIFEST: {"orchestrate_services": "workflow/service"}},
        files={repo.path / EXT_MANIFEST: ""},  # manifest present, entrypoint absent
    )
    with pytest.raises(RepoError, match="entrypoint not found"):
        res.resolve()


# ── path mode: local extension directory override ────────────────────────────


def _path_resolver(
    *,
    override: str,
    ext_dir: Path = EXT,
    orchestrate_services: str | None = "workflow/orchestrate",
    entrypoint_exists: bool = True,
) -> ServiceOrchestratorResolver:
    """Build a resolver wired for path-mode testing."""
    manifest_data: dict = {}
    if orchestrate_services is not None:
        manifest_data["orchestrate_services"] = orchestrate_services

    files: dict[Path, str] = {ext_dir / EXT_MANIFEST: ""}
    if orchestrate_services and entrypoint_exists:
        files[ext_dir / orchestrate_services] = ""

    return _resolver(
        orchestrator=None,
        repos=[],
        manifests={ext_dir / EXT_MANIFEST: manifest_data},
        files=files,
        override=override,
        directories=[ext_dir],
    )


def test_path_mode_resolves_via_absolute_path() -> None:
    """An absolute path override bypasses the registered-extension lookup."""
    res = _path_resolver(override=str(EXT))
    resolved = res.resolve()
    assert resolved.entrypoint == EXT / "workflow/orchestrate"
    assert resolved.ext_dir == EXT
    assert resolved.prefix == EXT.name  # derived from dir name when manifest has no prefix


def test_path_mode_resolves_via_slash_relative_path() -> None:
    """A value containing '/' (but not absolute) is treated as a path."""
    # Override is relative: "local-ext" lives under WS; the resolver resolves against cwd.
    # In the fake filesystem we seed EXT as a directory, and the _is_path check
    # sees the '/' separator in "ws/local-ext" to enter path mode.
    override = "ws/local-ext"  # contains /
    res = _path_resolver(override=override, ext_dir=EXT)
    # Path mode enters; the resolver resolves against cwd (real cwd during test).
    # Instead of exercising real cwd resolution here (unpredictable), we verify
    # that resolve() raises "not found" when the resolved abs path doesn't match
    # the fake dir we seeded — confirming it entered path mode, not name mode.
    with pytest.raises(RepoError, match=r"not a directory|not found"):
        res.resolve()


def test_path_mode_resolves_relative_path_against_workspace_root() -> None:
    """A relative override resolves against workspace_root, yielding the expected absolute paths."""
    ws = Path("/workspace")
    sub_dir = ws / "exts/my-orch"
    relative_override = "exts/my-orch"  # contains /

    manifest_data = {"orchestrate_services": "workflow/orchestrate"}
    files: dict[Path, str] = {
        sub_dir / EXT_MANIFEST: "",
        sub_dir / "workflow/orchestrate": "",
    }
    loader = ExtensionManifestLoader(config_file_reader=FakeConfigFileReader({sub_dir / EXT_MANIFEST: manifest_data}))
    fs = FakeFilesystem(files=files, directories=[sub_dir])
    registry = CapabilityRegistryService(
        repo_factory=_StubRepoFactory([]),
        manifest_loader=loader,
        bindings={},
        fs=fs,
        spec_loader=FakeSpecLoader(),
    )
    res = ServiceOrchestratorResolver(
        registry=registry,
        repo_factory=_StubRepoFactory([]),
        manifest_loader=loader,
        fs=fs,
        override=relative_override,
        workspace_root=ws,
    )
    resolved = res.resolve()
    assert resolved.ext_dir == sub_dir
    assert resolved.entrypoint == sub_dir / "workflow/orchestrate"


def test_path_mode_missing_dir_raises() -> None:
    """Path override pointing at a non-existent directory gives a clear error."""
    res = _resolver(
        orchestrator=None,
        repos=[],
        manifests={},
        files={},
        override=str(EXT),  # absolute, so path mode; EXT not seeded as dir
    )
    with pytest.raises(RepoError, match="not a directory"):
        res.resolve()


def test_path_mode_missing_manifest_raises() -> None:
    """Path override dir exists but has no winter-ext.toml."""
    res = _resolver(
        orchestrator=None,
        repos=[],
        manifests={},
        files={},
        override=str(EXT),
        directories=[EXT],  # dir exists, but no manifest file
    )
    with pytest.raises(RepoError, match=r"has no winter-ext\.toml"):
        res.resolve()


def test_path_mode_no_orchestrate_services_raises() -> None:
    """Path override's winter-ext.toml exists but declares no orchestrate_services."""
    res = _path_resolver(override=str(EXT), orchestrate_services=None)
    with pytest.raises(RepoError, match="declares no `orchestrate_services` entrypoint"):
        res.resolve()


def test_path_mode_missing_entrypoint_file_raises() -> None:
    """Path override's declared entrypoint file is absent."""
    res = _path_resolver(override=str(EXT), entrypoint_exists=False)
    with pytest.raises(RepoError, match="entrypoint not found"):
        res.resolve()


# ── precedence: override beats config ────────────────────────────────────────


def test_override_beats_config_value() -> None:
    """When both override and registry config are set, override wins."""
    repo = _tmux_repo()
    entrypoint_registered = repo.path / "workflow/service"
    entrypoint_local = EXT / "workflow/orchestrate"

    files = {
        repo.path / EXT_MANIFEST: "",
        entrypoint_registered: "",
        EXT / EXT_MANIFEST: "",
        entrypoint_local: "",
    }
    manifests = {
        repo.path / EXT_MANIFEST: {"orchestrate_services": "workflow/service"},
        EXT / EXT_MANIFEST: {"orchestrate_services": "workflow/orchestrate"},
    }

    loader = ExtensionManifestLoader(config_file_reader=FakeConfigFileReader(manifests))
    fs = FakeFilesystem(files=files, directories=[EXT])
    registry = CapabilityRegistryService(
        repo_factory=_StubRepoFactory([repo]),
        manifest_loader=loader,
        bindings={"service": ["winter-service-tmux"]},  # config value
        fs=fs,
        spec_loader=FakeSpecLoader(),
    )
    res = ServiceOrchestratorResolver(
        registry=registry,
        repo_factory=_StubRepoFactory([repo]),
        manifest_loader=loader,
        fs=fs,
        override=str(EXT),  # override wins
    )
    resolved = res.resolve()
    assert resolved.ext_dir == EXT
    assert resolved.entrypoint == entrypoint_local


def test_no_override_uses_config_value() -> None:
    """When override is None, the config service_orchestrator is used."""
    resolved = _configured().resolve()
    assert resolved.ext_dir == WS / "winter-service-tmux"


def test_no_override_no_config_raises() -> None:
    """When both override and config are absent, the no-orchestrator error fires."""
    res = _resolver(orchestrator=None, repos=[], manifests={}, files={}, override=None)
    with pytest.raises(RepoError, match="no extension provides"):
        res.resolve()


def test_bare_name_override_uses_name_mode() -> None:
    """A bare name override (no separator, not an existing dir) falls through to name-mode."""
    repo = _tmux_repo()
    entrypoint = repo.path / "workflow/service"
    # "winter-service-tmux" has no separator and is not seeded as a directory,
    # so it should resolve as a registered-extension name.
    res = _resolver(
        orchestrator=None,  # config is None — override supplies the name
        repos=[repo],
        manifests={repo.path / EXT_MANIFEST: {"orchestrate_services": "workflow/service"}},
        files={repo.path / EXT_MANIFEST: "", entrypoint: ""},
        override="winter-service-tmux",
    )
    resolved = res.resolve()
    assert resolved.ext_dir == WS / "winter-service-tmux"
    assert resolved.entrypoint == entrypoint
