from __future__ import annotations

from typing import ClassVar

from textual.app import App
from textual.binding import Binding, BindingType

from winter_cli.container import Container
from winter_cli.modules.tui.screen_factory import ScreenFactory


class WinterDashboardApp(App):
    ENABLE_COMMAND_PALETTE = False

    CSS_PATH = "styles/app.tcss"

    TITLE = "Winter Dashboard"

    # The user-facing quit key is the rebindable `app.quit` action, installed on
    # the workspace screen from `[keybindings]` (default `q`). ctrl+c stays as a
    # non-rebindable hard escape hatch so a bad keymap can never strand the user.
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+c", "quit", "Quit", show=False, priority=True),
    ]

    def __init__(self, container: Container, source_override: str | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        if source_override:
            self.title = f"Winter Dashboard  --winter={source_override}"
        self.screen_factory = ScreenFactory(container)

        plugin_registry = container.plugin_registry()
        for i, screen_cls in enumerate(plugin_registry.screens):
            screen_name = getattr(screen_cls, "SCREEN_NAME", f"plugin-{i}")
            self.install_screen(screen_cls, name=screen_name)

    def on_mount(self) -> None:
        self.push_screen(self.screen_factory.workspace_screen())
