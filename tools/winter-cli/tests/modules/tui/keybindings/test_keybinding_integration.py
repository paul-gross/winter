"""End-to-end keybinding behavior through a real Textual event loop (issue/23).

A minimal screen mixes in `KeybindingMixin` and records which actions fire, so
these tests exercise the actual native-binding + chord-engine + timeout-timer
path that `on_key` drives — complementing the pure unit tests for the parser,
resolver, and engine.
"""

from __future__ import annotations

import pytest
from textual.app import App
from textual.screen import Screen

from winter_cli.config.models import KeybindingsConfig
from winter_cli.modules.tui.keybindings import KeybindingMixin, KeybindingResolver
from winter_cli.modules.tui.keybindings.actions import ActionBinding

# Built-in-style actions under test: a single key, a modifier chord, and the
# rebindable open-detail action (default Enter).
_ACTIONS = [
    ActionBinding("demo.refresh", "r", "refresh", "Refresh"),
    ActionBinding("demo.sync", "<C-s>", "sync", "Sync"),
    ActionBinding("demo.open_detail", "<enter>", "open_detail", "Open", show=False),
]


class _KbScreen(KeybindingMixin, Screen):
    def __init__(self, resolver: KeybindingResolver) -> None:
        super().__init__()
        self._keybinding_resolver = resolver
        self.fired: list[str] = []

    def on_mount(self) -> None:
        self._install_keybindings(list(_ACTIONS))

    def action_refresh(self) -> None:
        self.fired.append("refresh")

    def action_sync(self) -> None:
        self.fired.append("sync")

    def action_open_detail(self) -> None:
        self.fired.append("open_detail")


class _KbApp(App):
    def __init__(self, resolver: KeybindingResolver) -> None:
        super().__init__()
        self.kb_screen = _KbScreen(resolver)

    def on_mount(self) -> None:
        self.push_screen(self.kb_screen)


def _resolver(bindings=None, leader="\\", timeoutlen=1000) -> KeybindingResolver:
    return KeybindingResolver(KeybindingsConfig(leader=leader, timeoutlen=timeoutlen, bindings=bindings or {}))


@pytest.mark.asyncio
async def test_default_single_key_fires() -> None:
    app = _KbApp(_resolver())
    async with app.run_test() as pilot:
        await pilot.press("r")
        await pilot.pause()
        assert app.kb_screen.fired == ["refresh"]


@pytest.mark.asyncio
async def test_single_key_override_fires_on_new_key() -> None:
    app = _KbApp(_resolver({"demo.refresh": "g"}))
    async with app.run_test() as pilot:
        await pilot.press("g")
        await pilot.pause()
        assert app.kb_screen.fired == ["refresh"]


@pytest.mark.asyncio
async def test_modifier_chord_default_fires() -> None:
    app = _KbApp(_resolver())
    async with app.run_test() as pilot:
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app.kb_screen.fired == ["sync"]


@pytest.mark.asyncio
async def test_open_detail_is_rebindable() -> None:
    app = _KbApp(_resolver({"demo.open_detail": "o"}))
    async with app.run_test() as pilot:
        await pilot.press("o")
        await pilot.pause()
        assert app.kb_screen.fired == ["open_detail"]


@pytest.mark.asyncio
async def test_leader_sequence_fires_within_timeout() -> None:
    app = _KbApp(_resolver({"demo.refresh": "<leader>r"}, leader="\\"))
    async with app.run_test() as pilot:
        await pilot.press("backslash")
        await pilot.press("r")
        await pilot.pause()
        assert app.kb_screen.fired == ["refresh"]


@pytest.mark.asyncio
async def test_bare_sequence_fires() -> None:
    app = _KbApp(_resolver({"demo.refresh": "gr"}))
    async with app.run_test() as pilot:
        await pilot.press("g")
        await pilot.press("r")
        await pilot.pause()
        assert app.kb_screen.fired == ["refresh"]


@pytest.mark.asyncio
async def test_sequence_prefix_fires_standalone_action_after_timeout() -> None:
    # `r` is a standalone action and also the prefix of the `rs` sequence. A bare
    # `r` followed by the timeout lapsing fires the standalone refresh.
    app = _KbApp(_resolver({"demo.refresh": "r", "demo.sync": "rs"}, timeoutlen=40))
    async with app.run_test() as pilot:
        await pilot.press("r")
        await pilot.pause(0.15)  # well past the 40ms timeout
        assert app.kb_screen.fired == ["refresh"]


@pytest.mark.asyncio
async def test_sequence_completes_before_timeout() -> None:
    app = _KbApp(_resolver({"demo.refresh": "r", "demo.sync": "rs"}, timeoutlen=1000))
    async with app.run_test() as pilot:
        await pilot.press("r")
        await pilot.press("s")
        await pilot.pause()
        assert app.kb_screen.fired == ["sync"]


@pytest.mark.asyncio
async def test_breaking_native_key_fires_prefix_and_its_own_action() -> None:
    # `g` is a standalone action (refresh) and the prefix of `gd` (sync). Pressing
    # `g` then `o` (a native binding for open_detail, not a continuation of `gd`)
    # must fire BOTH refresh (the held prefix) and open_detail — the breaking key
    # is not swallowed.
    app = _KbApp(
        _resolver(
            {"demo.refresh": "g", "demo.sync": "gd", "demo.open_detail": "o"},
            timeoutlen=1000,
        )
    )
    async with app.run_test() as pilot:
        await pilot.press("g")
        await pilot.press("o")
        await pilot.pause()
        assert set(app.kb_screen.fired) == {"refresh", "open_detail"}
