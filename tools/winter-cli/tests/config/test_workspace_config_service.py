from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import FakeFilesystem
from winter_cli.config.models import AdoptExtensions, SingletonType
from winter_cli.config.workspace import (
    CONFIG_FILE,
    LOCAL_CONFIG_FILE,
    WINTER_DIR,
    WorkspaceConfigService,
)

WORKSPACE_ROOT = Path("/ws/demo")


class _StubLocator:
    """IWorkspaceLocator fake — returns a fixed path instead of walking cwd."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def find_workspace_root(self) -> Path:
        return self._root


class _DictConfigFileReader:
    """IConfigFileReader fake — returns canned dicts keyed by path."""

    def __init__(self, contents: dict[Path, dict]) -> None:
        self._contents = contents

    def load(self, path: Path) -> dict:
        if path not in self._contents:
            raise FileNotFoundError(path)
        return self._contents[path]


def _service(
    fs: FakeFilesystem,
    configs: dict[Path, dict],
    root: Path = WORKSPACE_ROOT,
) -> WorkspaceConfigService:
    return WorkspaceConfigService(
        workspace_locator=_StubLocator(root),
        fs=fs,
        config_file_reader=_DictConfigFileReader(configs),
    )


def test_load_reads_shared_config() -> None:
    config_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
    fs = FakeFilesystem(files={config_path: ""})  # presence-only; reader returns canned dict
    svc = _service(
        fs,
        {
            config_path: {
                "main_branch": "trunk",
                "session_prefix": "ws",
                "git_excludes": ["/.idea/"],
                "git": {"user": {"name": "Test User", "email": "test@example.com"}},
                "project_repository": [
                    {"name": "frontend", "url": "git@example.com:org/frontend.git", "pinned": True},
                ],
                "standalone_repository": [
                    {"name": "ext-one", "url": "git@example.com:org/ext-one.git"},
                ],
            },
        },
    )

    config = svc.load()

    assert config.workspace_root == WORKSPACE_ROOT
    assert config.session_prefix == "ws"
    assert config.main_branch == "trunk"
    assert config.git_excludes == ["/.idea/"]
    assert config.git_identity is not None
    assert config.git_identity.name == "Test User"
    assert config.git_identity.email == "test@example.com"
    assert config.adopt_extensions == AdoptExtensions.winter

    assert any(r.type == SingletonType.workspace for r in config.singleton_repos)
    assert len(config.project_repos) == 1
    assert config.project_repos[0].name == "frontend"
    assert config.project_repos[0].pinned is True
    assert len(config.standalone_repos) == 1
    assert config.standalone_repos[0].name == "ext-one"


def test_load_maps_workspace_doctor_and_lint_scripts() -> None:
    config_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
    fs = FakeFilesystem(files={config_path: ""})
    svc = _service(fs, {config_path: {"doctor": "ai/doctor.sh", "lint": "ai/lint.sh"}})

    config = svc.load()

    assert config.doctor == "ai/doctor.sh"
    assert config.lint == "ai/lint.sh"


def test_load_merges_local_overlay() -> None:
    config_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
    local_path = WORKSPACE_ROOT / WINTER_DIR / LOCAL_CONFIG_FILE
    fs = FakeFilesystem(files={config_path: "", local_path: ""})
    svc = _service(
        fs,
        {
            config_path: {
                "main_branch": "trunk",
                "project_repository": [
                    {"name": "frontend", "url": "git@example.com:org/frontend.git"},
                ],
            },
            local_path: {
                "main_branch": "develop",
                "project_repository": [
                    {"name": "backend", "url": "git@example.com:org/backend.git"},
                ],
            },
        },
    )

    config = svc.load()

    # Scalars in the overlay win.
    assert config.main_branch == "develop"
    # Arrays of tables concatenate via deep_merge.
    names = sorted(r.name for r in config.project_repos if r.name)
    assert names == ["backend", "frontend"]


def test_load_picks_up_singletons_present_on_disk() -> None:
    config_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
    fs = FakeFilesystem(
        files={
            config_path: "",
            WORKSPACE_ROOT / "ai" / "harness" / ".git": "",  # treated as exists()
        },
        directories=[WORKSPACE_ROOT / "product"],
    )
    svc = _service(fs, {config_path: {}})

    config = svc.load()

    types = {r.type for r in config.singleton_repos}
    assert SingletonType.product in types
    assert SingletonType.harness in types


def test_load_rejects_invalid_adopt_extensions() -> None:
    config_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
    fs = FakeFilesystem(files={config_path: ""})
    svc = _service(fs, {config_path: {"adopt_extensions": "bogus"}})

    with pytest.raises(RuntimeError, match="adopt_extensions"):
        svc.load()


def test_load_returns_empty_when_config_files_absent() -> None:
    """No config files present → load() succeeds with defaults (empty overlay path)."""
    fs = FakeFilesystem()  # nothing seeded
    svc = _service(fs, {})  # config_file_reader would be called only if is_file() said yes

    config = svc.load()
    assert config.workspace_root == WORKSPACE_ROOT
    assert config.main_branch == "main"  # default
    assert config.project_repos == []


def test_keybindings_default_when_absent() -> None:
    config_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
    fs = FakeFilesystem(files={config_path: ""})
    svc = _service(fs, {config_path: {}})

    kb = svc.load().keybindings
    assert kb.leader == "\\"
    assert kb.timeoutlen == 1000
    assert kb.bindings == {}


def test_keybindings_parsed_from_table() -> None:
    config_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
    fs = FakeFilesystem(files={config_path: ""})
    svc = _service(
        fs,
        {
            config_path: {
                "keybindings": {
                    "leader": ",",
                    "timeoutlen": 400,
                    "bindings": {
                        "workspace.refresh": "g",
                        "worktree.open_detail": "o",
                    },
                },
            },
        },
    )

    kb = svc.load().keybindings
    assert kb.leader == ","
    assert kb.timeoutlen == 400
    assert kb.bindings == {"workspace.refresh": "g", "worktree.open_detail": "o"}


def test_keybindings_overlay_overrides_per_key() -> None:
    config_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
    local_path = WORKSPACE_ROOT / WINTER_DIR / LOCAL_CONFIG_FILE
    fs = FakeFilesystem(files={config_path: "", local_path: ""})
    svc = _service(
        fs,
        {
            config_path: {"keybindings": {"timeoutlen": 1000, "bindings": {"workspace.refresh": "g"}}},
            local_path: {"keybindings": {"bindings": {"workspace.refresh": "R"}}},
        },
    )

    kb = svc.load().keybindings
    # Per-machine overlay wins for the overridden id.
    assert kb.bindings["workspace.refresh"] == "R"
    assert kb.timeoutlen == 1000
