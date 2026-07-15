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

    async def action_quit(self) -> None:
        """Cancel in-flight refresh workers, then exit immediately.

        The dashboard's refresh/detail workers are `@work(thread=True)` git
        probes. Textual's default quit tears down the UI but leaves those
        threads marshaling `call_from_thread` callbacks against the dying app —
        a slow, error-spewing exit. Cancelling every worker first flips each
        one's `is_cancelled`, which the screens' guarded `_call_from_thread_safe`
        checks to stop marshaling at once; the threads can't be force-killed,
        but they no longer block quit or emit errors as they unwind.
        """
        self.workers.cancel_all()
        self.exit()
