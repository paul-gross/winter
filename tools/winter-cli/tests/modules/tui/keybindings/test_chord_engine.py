"""Chord engine: multi-key sequence matching with timeout resolution (issue/23).

The engine is pure (no timers), so these tests drive `feed`/`timeout` directly:
a sequence firing within the timeout, a sequence abandoned after the timeout
with prefix-binding resolution, irrelevant keys passing through, and a broken
sequence restarting cleanly.
"""

from __future__ import annotations

from winter_cli.modules.tui.keybindings.chord_engine import ChordEngine


def _engine(sequences=None, singles=None) -> ChordEngine:
    return ChordEngine(sequences or {}, singles or {})


def test_sequence_fires_on_completing_key() -> None:
    engine = _engine({("backslash", "S"): "sync"})

    first = engine.feed("backslash")
    assert first.consume and first.pending and first.fire == ()

    second = engine.feed("S")
    assert second.consume and not second.pending
    assert second.fire == ("sync",)
    assert engine.pending == ()


def test_bare_sequence_fires() -> None:
    engine = _engine({("g", "d"): "goto_def"})
    assert engine.feed("g").pending
    assert engine.feed("d").fire == ("goto_def",)


def test_irrelevant_key_passes_through() -> None:
    engine = _engine({("g", "d"): "goto_def"})
    outcome = engine.feed("r")
    assert not outcome.consume
    assert outcome.fire == ()


def test_timeout_resolves_prefix_binding() -> None:
    # `g` is both a standalone action and the prefix of `gd`. Pressing `g` then
    # letting the deadline lapse fires the standalone `g` action (Neovim rule).
    engine = _engine(sequences={("g", "d"): "goto_def"}, singles={"g": "top"})

    pending = engine.feed("g")
    assert pending.consume and pending.pending and pending.fire == ()

    fired = engine.timeout()
    assert fired.fire == ("top",)
    assert engine.pending == ()


def test_breaking_native_key_fires_prefix_and_passes_through() -> None:
    # `g` is a standalone action and prefix of `gd`. Pressing `g` then a key the
    # engine doesn't own fires the held prefix (`top`) AND does not consume the
    # breaking key, so its native binding still runs.
    engine = _engine(sequences={("g", "d"): "goto_def"}, singles={"g": "top"})
    engine.feed("g")
    outcome = engine.feed("r")  # `r` is a native binding the engine doesn't know
    assert outcome.fire == ("top",)
    assert not outcome.consume
    assert engine.pending == ()


def test_timeout_without_prefix_binding_fires_nothing() -> None:
    engine = _engine({("backslash", "S"): "sync"})
    engine.feed("backslash")
    assert engine.timeout().fire == ()


def test_completing_key_beats_timeout_for_prefix_binding() -> None:
    # With `g` standalone and `gd` a sequence, pressing g then d fires the
    # sequence, not the prefix binding.
    engine = _engine(sequences={("g", "d"): "goto_def"}, singles={"g": "top"})
    engine.feed("g")
    assert engine.feed("d").fire == ("goto_def",)


def test_broken_sequence_restarts() -> None:
    # `\` then a key that is not `S` abandons the pending sequence; an unrelated
    # key produces no action but is consumed (we already swallowed the leader).
    engine = _engine({("backslash", "S"): "sync"})
    engine.feed("backslash")
    outcome = engine.feed("x")
    assert outcome.fire == ()
    assert engine.pending == ()


def test_broken_sequence_fires_prefix_then_restarts_into_new_sequence() -> None:
    engine = _engine(
        sequences={("g", "g"): "top", ("d", "d"): "bottom"},
        singles={},
    )
    engine.feed("g")  # prefix of gg
    outcome = engine.feed("d")  # breaks gg, starts dd
    assert outcome.fire == ()  # gg has no prefix binding
    assert outcome.pending  # now pending on `d`
    assert engine.feed("d").fire == ("bottom",)
