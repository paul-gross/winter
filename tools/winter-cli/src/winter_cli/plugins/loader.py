from __future__ import annotations

import logging
from pathlib import Path

import click

from winter_cli.core.config_file import ConfigFileReadError, IConfigFileReader
from winter_cli.core.filesystem import IFilesystemReader
from winter_cli.modules.workspace.models import StandaloneRepository, Workspace
from winter_cli.plugins.plugin_loader import IPluginLoader
from winter_cli.plugins.types import (
    ActionScope,
    IDetailPanel,
    IEnvironmentDecorator,
    IWinterPlugin,
    IWorktreeRepoDecorator,
    PluginRegistration,
    TuiAction,
)

USER_PLUGINS_DIR = Path.home() / ".config" / "winter" / "plugins"

logger = logging.getLogger(__name__)


class PluginRegistry:
    """Discovers and loads winter plugins from workspace, user-global, and extension sources.

    I/O goes through three Protocol seams:
      - `IFilesystemReader` for directory/file probes
      - `IConfigFileReader` for `config.toml` parsing
      - `IPluginLoader` for importing the plugin module from its `plugin.py`

    Each source is consulted in priority order (workspace > user-global >
    standalone extensions); first wins on name collision.
    """

    def __init__(
        self,
        fs: IFilesystemReader,
        config_file_reader: IConfigFileReader,
        plugin_loader: IPluginLoader,
    ) -> None:
        self._fs = fs
        self._config_file_reader = config_file_reader
        self._plugin_loader = plugin_loader
        self.plugins: list[IWinterPlugin] = []
        self.commands: list[click.Command] = []
        self.worktree_repo_decorators: list[IWorktreeRepoDecorator] = []
        self.environment_decorators: list[IEnvironmentDecorator] = []
        self.detail_panels: list[IDetailPanel] = []
        self.screens: list = []
        self.tui_actions: list[TuiAction] = []

    def actions_for_scope(self, scope: ActionScope) -> list[TuiAction]:
        return [a for a in self.tui_actions if a.scope == scope]

    def discover(
        self,
        workspace: Workspace,
        standalone_repos: list[StandaloneRepository] | None = None,
    ) -> PluginRegistry:
        """Discover and load every plugin that contributes to this workspace.

        Three sources, in priority order (first wins on name collision):
          1. Workspace-local: `<workspace>/.winter/plugins/<name>/plugin.py`
          2. User-global:     `~/.config/winter/plugins/<name>/plugin.py`
          3. Installed extensions: `<standalone_repo>/plugin.py` — lets a
             winter extension ship a dashboard plugin alongside its hooks
             without the user having to copy anything into .winter/plugins/.
        """
        workspace_plugins_dir = workspace.root_path / ".winter" / "plugins"
        seen: set[str] = set()

        for plugins_dir in [workspace_plugins_dir, USER_PLUGINS_DIR]:
            if not self._fs.is_dir(plugins_dir):
                continue
            for plugin_dir in sorted(self._fs.iterdir(plugins_dir)):
                if not self._fs.is_dir(plugin_dir) or plugin_dir.name in seen:
                    continue
                if not self._fs.is_file(plugin_dir / "plugin.py"):
                    continue
                self._load_plugin(plugin_dir)
                seen.add(plugin_dir.name)

        for repo in sorted(standalone_repos or [], key=lambda r: r.name):
            if repo.name in seen:
                logger.warning(
                    "plugin '%s' from extension %s shadowed by a higher-priority plugin of the same name",
                    repo.name,
                    repo.path,
                )
                continue
            if not self._fs.is_dir(repo.path) or not self._fs.is_file(repo.path / "plugin.py"):
                continue
            self._load_plugin(repo.path)
            seen.add(repo.name)

        return self

    @classmethod
    def load(
        cls,
        workspace: Workspace,
        fs: IFilesystemReader,
        config_file_reader: IConfigFileReader,
        plugin_loader: IPluginLoader,
        standalone_repos: list[StandaloneRepository] | None = None,
    ) -> PluginRegistry:
        """Factory helper used by the DI container: build a registry and run discovery."""
        return cls(fs, config_file_reader, plugin_loader).discover(workspace, standalone_repos)

    def _load_plugin(self, plugin_dir: Path) -> None:
        plugin_name = plugin_dir.name
        entry_point = plugin_dir / "plugin.py"
        config = self._load_config(plugin_dir)

        try:
            module = self._plugin_loader.load(plugin_name, entry_point)
            if not hasattr(module, "create_plugin"):
                logger.warning("Plugin '%s' has no create_plugin() function, skipping", plugin_name)
                return
            plugin: IWinterPlugin = module.create_plugin()
            registration: PluginRegistration = plugin.register(config)
            self._apply(plugin, registration)
        except Exception:
            # A buggy plugin must not take the whole CLI offline. The registry is
            # consumed as a Singleton by the dashboard and every `winter ws *`
            # command path, so an exception here would brick the tool.
            logger.warning("Failed to load plugin '%s'", plugin_name, exc_info=True)
            return

    def _apply(self, plugin: IWinterPlugin, registration: PluginRegistration) -> None:
        self.plugins.append(plugin)
        self.commands.extend(registration.commands)
        self.worktree_repo_decorators.extend(registration.worktree_repo_decorators)
        self.environment_decorators.extend(registration.environment_decorators)
        self.detail_panels.extend(registration.detail_panels)
        self.screens.extend(registration.tui_screens)
        self.tui_actions.extend(registration.tui_actions)

    def _load_config(self, plugin_dir: Path) -> dict:
        config_path = plugin_dir / "config.toml"
        if not self._fs.is_file(config_path):
            return {}
        try:
            return self._config_file_reader.load(config_path)
        except ConfigFileReadError:
            return {}
