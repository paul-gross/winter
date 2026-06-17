from __future__ import annotations

import logging
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from tests.conftest import FakeConfigFileReader, FakeFilesystem
from winter_cli.modules.workspace.models import StandaloneRepository, Workspace
from winter_cli.plugins.loader import PluginRegistry
from winter_cli.plugins.types import IDetailPanel, PluginRegistration

WORKSPACE_ROOT = Path("/ws")


class FakePluginLoader:
    """IPluginLoader fake — returns canned modules keyed by entry-point path.

    Tests register `(path, module)` to control what `_load_plugin` sees.
    Modules must expose `create_plugin()` to be installed.
    """

    def __init__(self, modules: dict[Path, ModuleType]) -> None:
        self._modules = modules
        self.load_calls: list[tuple[str, Path]] = []

    def load(self, name: str, entry_point: Path) -> ModuleType:
        self.load_calls.append((name, entry_point))
        if entry_point not in self._modules:
            raise ImportError(f"unknown entry point: {entry_point}")
        return self._modules[entry_point]


def _make_module(name: str, *, config_received: list[dict]) -> ModuleType:
    """Build a fake plugin module that records the config it was registered with."""
    module = ModuleType(name)

    def create_plugin() -> SimpleNamespace:
        def register(config: object) -> PluginRegistration:
            assert isinstance(config, dict)
            config_received.append(config)
            return PluginRegistration()

        return SimpleNamespace(name=name, register=register)

    module.create_plugin = create_plugin  # type: ignore[attr-defined]
    return module


@pytest.fixture
def workspace() -> Workspace:
    return Workspace(root_path=WORKSPACE_ROOT, session_prefix="t", main_branch="main")


class _StubPanel:
    """Minimal IDetailPanel for asserting the loader collects detail panels."""

    name = "info"
    title = "Info"

    def render(self, context: object) -> object:
        return "x"


def _make_panel_module(name: str, panel: IDetailPanel) -> ModuleType:
    """Build a fake plugin module that contributes a single detail panel."""
    module = ModuleType(name)

    def create_plugin() -> SimpleNamespace:
        def register(config: object) -> PluginRegistration:
            return PluginRegistration(detail_panels=[panel])

        return SimpleNamespace(name=name, register=register)

    module.create_plugin = create_plugin  # type: ignore[attr-defined]
    return module


def test_discover_collects_detail_panels(workspace: Workspace) -> None:
    plugin_dir = WORKSPACE_ROOT / ".winter" / "plugins" / "paneled"
    plugin_py = plugin_dir / "plugin.py"
    fs = FakeFilesystem(
        directories=[WORKSPACE_ROOT / ".winter" / "plugins", plugin_dir],
        files={plugin_py: ""},
    )
    panel = _StubPanel()
    loader = FakePluginLoader({plugin_py: _make_panel_module("paneled", panel)})

    registry = PluginRegistry(fs, FakeConfigFileReader({}), loader).discover(workspace, standalone_repos=[])

    assert registry.detail_panels == [panel]


def test_discover_loads_workspace_local_plugin(workspace: Workspace) -> None:
    plugin_dir = WORKSPACE_ROOT / ".winter" / "plugins" / "demo"
    plugin_py = plugin_dir / "plugin.py"
    fs = FakeFilesystem(
        directories=[WORKSPACE_ROOT / ".winter" / "plugins", plugin_dir],
        files={plugin_py: ""},
    )
    config_received: list[dict] = []
    loader = FakePluginLoader({plugin_py: _make_module("demo", config_received=config_received)})

    registry = PluginRegistry(fs, FakeConfigFileReader({}), loader).discover(workspace, standalone_repos=[])

    assert [p.name for p in registry.plugins] == ["demo"]
    assert loader.load_calls == [("demo", plugin_py)]
    assert config_received == [{}]  # no config.toml present → empty dict


def test_discover_reads_plugin_config_when_present(workspace: Workspace) -> None:
    plugin_dir = WORKSPACE_ROOT / ".winter" / "plugins" / "demo"
    plugin_py = plugin_dir / "plugin.py"
    config_toml = plugin_dir / "config.toml"
    fs = FakeFilesystem(
        directories=[WORKSPACE_ROOT / ".winter" / "plugins", plugin_dir],
        files={plugin_py: "", config_toml: ""},
    )
    config_received: list[dict] = []
    loader = FakePluginLoader({plugin_py: _make_module("demo", config_received=config_received)})
    reader = FakeConfigFileReader({config_toml: {"opt_in": True, "name": "demo"}})

    PluginRegistry(fs, reader, loader).discover(workspace, standalone_repos=[])

    assert config_received == [{"opt_in": True, "name": "demo"}]


def test_discover_skips_extension_plugin_when_workspace_plugin_wins(
    workspace: Workspace, caplog: pytest.LogCaptureFixture
) -> None:
    """Workspace plugin shadows a same-named plugin shipped by an extension — emits WARNING."""
    ws_dir = WORKSPACE_ROOT / ".winter" / "plugins" / "demo"
    ws_plugin = ws_dir / "plugin.py"
    ext_path = WORKSPACE_ROOT / "ext-demo"
    ext_plugin = ext_path / "plugin.py"
    fs = FakeFilesystem(
        directories=[WORKSPACE_ROOT / ".winter" / "plugins", ws_dir, ext_path],
        files={ws_plugin: "", ext_plugin: ""},
    )
    config_received: list[dict] = []
    loader = FakePluginLoader(
        {
            ws_plugin: _make_module("demo", config_received=config_received),
        }
    )

    ext_repo = StandaloneRepository(name="demo", path=ext_path)
    with caplog.at_level(logging.WARNING, logger="winter_cli.plugins.loader"):
        registry = PluginRegistry(fs, FakeConfigFileReader({}), loader).discover(workspace, standalone_repos=[ext_repo])

    # Workspace one loaded, extension one skipped (same name).
    assert loader.load_calls == [("demo", ws_plugin)]
    assert len(registry.plugins) == 1
    # A WARNING must be emitted naming the shadowed extension.
    assert any("demo" in r.message and r.levelno == logging.WARNING for r in caplog.records)


def test_discover_standalone_plugins_loaded_in_sorted_order(workspace: Workspace) -> None:
    """Standalone extension plugins are loaded in deterministic (sorted) name order."""
    ext_b_path = WORKSPACE_ROOT / "ext-b"
    ext_a_path = WORKSPACE_ROOT / "ext-a"
    ext_b_plugin = ext_b_path / "plugin.py"
    ext_a_plugin = ext_a_path / "plugin.py"
    fs = FakeFilesystem(
        directories=[ext_b_path, ext_a_path],
        files={ext_b_plugin: "", ext_a_plugin: ""},
    )
    b_received: list[dict] = []
    a_received: list[dict] = []
    loader = FakePluginLoader(
        {
            ext_b_plugin: _make_module("ext-b", config_received=b_received),
            ext_a_plugin: _make_module("ext-a", config_received=a_received),
        }
    )

    repos = [
        StandaloneRepository(name="ext-b", path=ext_b_path),
        StandaloneRepository(name="ext-a", path=ext_a_path),
    ]
    PluginRegistry(fs, FakeConfigFileReader({}), loader).discover(workspace, standalone_repos=repos)

    # ext-a sorts before ext-b regardless of the order in the input list.
    loaded_names = [name for name, _ in loader.load_calls]
    assert loaded_names == ["ext-a", "ext-b"]


def test_discover_skips_plugin_module_without_create_plugin(workspace: Workspace) -> None:
    """A module that doesn't export create_plugin() is silently skipped."""
    plugin_dir = WORKSPACE_ROOT / ".winter" / "plugins" / "broken"
    plugin_py = plugin_dir / "plugin.py"
    fs = FakeFilesystem(
        directories=[WORKSPACE_ROOT / ".winter" / "plugins", plugin_dir],
        files={plugin_py: ""},
    )
    loader = FakePluginLoader({plugin_py: ModuleType("broken")})  # no create_plugin

    registry = PluginRegistry(fs, FakeConfigFileReader({}), loader).discover(workspace, standalone_repos=[])
    assert registry.plugins == []


def test_discover_isolates_exception_raised_inside_create_plugin(workspace: Workspace) -> None:
    """A plugin that raises in create_plugin() is skipped, not propagated.

    The registry is a Singleton consumed by every `winter ws *` command path —
    if any plugin's create_plugin() / register() raised out of discover(), a
    single buggy extension would take the whole CLI offline.
    """
    plugin_dir = WORKSPACE_ROOT / ".winter" / "plugins" / "exploding"
    plugin_py = plugin_dir / "plugin.py"
    fs = FakeFilesystem(
        directories=[WORKSPACE_ROOT / ".winter" / "plugins", plugin_dir],
        files={plugin_py: ""},
    )

    module = ModuleType("exploding")

    def create_plugin() -> SimpleNamespace:
        raise RuntimeError("plugin author typo")

    module.create_plugin = create_plugin  # type: ignore[attr-defined]
    loader = FakePluginLoader({plugin_py: module})

    registry = PluginRegistry(fs, FakeConfigFileReader({}), loader).discover(workspace, standalone_repos=[])
    assert registry.plugins == []


def test_discover_isolates_exception_raised_inside_register(workspace: Workspace) -> None:
    """A plugin that raises in register() is skipped, not propagated."""
    plugin_dir = WORKSPACE_ROOT / ".winter" / "plugins" / "bad-register"
    plugin_py = plugin_dir / "plugin.py"
    fs = FakeFilesystem(
        directories=[WORKSPACE_ROOT / ".winter" / "plugins", plugin_dir],
        files={plugin_py: ""},
    )

    module = ModuleType("bad-register")

    def create_plugin() -> SimpleNamespace:
        def register(config: object) -> PluginRegistration:
            raise ValueError("bad config schema")

        return SimpleNamespace(name="bad-register", register=register)

    module.create_plugin = create_plugin  # type: ignore[attr-defined]
    loader = FakePluginLoader({plugin_py: module})

    registry = PluginRegistry(fs, FakeConfigFileReader({}), loader).discover(workspace, standalone_repos=[])
    assert registry.plugins == []


def test_discover_other_plugins_load_when_one_explodes(workspace: Workspace) -> None:
    """One broken plugin does not stop sibling plugins from loading."""
    plugins_dir = WORKSPACE_ROOT / ".winter" / "plugins"
    good_dir = plugins_dir / "good"
    good_py = good_dir / "plugin.py"
    bad_dir = plugins_dir / "bad"
    bad_py = bad_dir / "plugin.py"
    fs = FakeFilesystem(
        directories=[plugins_dir, good_dir, bad_dir],
        files={good_py: "", bad_py: ""},
    )

    bad_module = ModuleType("bad")

    def create_plugin() -> SimpleNamespace:
        raise RuntimeError("kaboom")

    bad_module.create_plugin = create_plugin  # type: ignore[attr-defined]

    config_received: list[dict] = []
    loader = FakePluginLoader(
        {
            good_py: _make_module("good", config_received=config_received),
            bad_py: bad_module,
        }
    )

    registry = PluginRegistry(fs, FakeConfigFileReader({}), loader).discover(workspace, standalone_repos=[])
    assert [p.name for p in registry.plugins] == ["good"]
