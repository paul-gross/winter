"""Parse a Neovim-inspired key spec into a sequence of Textual key tokens.

A *key spec* is the string a user writes in `[keybindings.bindings]`. It resolves
to an ordered tuple of Textual key tokens — one token for a single key or a
modifier chord, more than one for a multi-key sequence:

    "s"          -> ("s",)
    "D"          -> ("D",)
    "<C-s>"      -> ("ctrl+s",)
    "<A-s>"      -> ("alt+s",)        # <M-s> normalizes the same (meta == alt)
    "<S-s>"      -> ("S",)            # shift on a letter is the uppercase letter
    "<C-A-s>"    -> ("ctrl+alt+s",)
    "<leader>S"  -> ("backslash", "S")   # with the default "\\" leader
    "gd"         -> ("g", "d")

Single-character tokens are normalized through Textual's own `_character_to_key`
so they match the `key` field of a runtime `events.Key` exactly (e.g. `\\` ->
`backslash`, `,` -> `comma`). Modifier chords compose to Textual's `ctrl+`,
`alt+`, `shift+` tokens in that canonical order.

Anything malformed raises `KeySpecError` with a human-readable reason; callers
(the resolver) catch it, surface a located error, and fall back to the default.
"""

from __future__ import annotations

import dataclasses

from textual.keys import _character_to_key

# Neovim-style key names -> Textual base tokens. Used for `<...>` group bases
# and for the leader when it is written as a named key.
_NAMED_KEYS = {
    "cr": "enter",
    "return": "enter",
    "enter": "enter",
    "tab": "tab",
    "space": "space",
    "bs": "backspace",
    "backspace": "backspace",
    "esc": "escape",
    "escape": "escape",
    "del": "delete",
    "delete": "delete",
    "up": "up",
    "down": "down",
    "left": "left",
    "right": "right",
    "home": "home",
    "end": "end",
    "pageup": "pageup",
    "pagedown": "pagedown",
    "insert": "insert",
    "leader": "leader",  # sentinel — expanded by the caller, never a final token
    **{f"f{n}": f"f{n}" for n in range(1, 13)},  # <f1>..<f12>
}

_MODIFIERS = {
    "c": "ctrl",
    "a": "alt",
    "m": "alt",  # meta normalizes to alt, matching Neovim's <M-> == <A->
    "s": "shift",
}

# Canonical compose order for modifier tokens (matches Textual, e.g. ctrl+shift+1).
_MODIFIER_ORDER = ("ctrl", "alt", "shift")


class KeySpecError(ValueError):
    """A key spec could not be parsed (unclosed group, unknown key, empty)."""


@dataclasses.dataclass(frozen=True)
class KeySpec:
    """A parsed key spec: an ordered tuple of Textual key tokens.

    `len(keys) == 1` is a single key or modifier chord (bound natively through
    Textual); `len(keys) > 1` is a multi-key sequence handled by the chord engine.
    """

    keys: tuple[str, ...]
    source: str

    @property
    def is_sequence(self) -> bool:
        return len(self.keys) > 1


def parse_key_spec(spec: str, *, leader: str) -> KeySpec:
    """Parse `spec` into a `KeySpec`, expanding `<leader>` to `leader`'s tokens.

    Raises `KeySpecError` if the spec is empty or malformed.
    """
    if not leader or not leader.strip():
        raise KeySpecError("empty leader key")
    # The leader is itself a key spec but may not reference <leader> — tokenize it
    # with `leader_tokens=None` so a nested <leader> errors rather than recurses.
    leader_tokens = _tokenize(leader, leader_tokens=None)
    return KeySpec(keys=_tokenize(spec, leader_tokens=leader_tokens), source=spec)


def spec_from_textual_token(token: str) -> KeySpec:
    """Wrap an already-normalized Textual key token as a single-key `KeySpec`.

    Used for plugin `TuiAction.key` defaults, which are raw Textual tokens
    (`"e"`, `"ctrl+e"`, `"enter"`) rather than the user-facing spec grammar — so
    they are taken literally instead of split into a char sequence (`"enter"`
    must not become e-n-t-e-r). Single characters still pass through Textual's
    normalizer to match runtime key events.
    """
    normalized = _character_to_key(token) if len(token) == 1 else token
    return KeySpec(keys=(normalized,), source=token)


def _tokenize(spec: str, *, leader_tokens: tuple[str, ...] | None) -> tuple[str, ...]:
    """Tokenize `spec` into Textual key tokens.

    `leader_tokens` is the expansion for `<leader>`; `None` means `<leader>` is
    not allowed here (used when tokenizing the leader spec itself).
    """
    if not spec or not spec.strip():
        raise KeySpecError("empty key spec")

    tokens: list[str] = []
    i = 0
    n = len(spec)
    while i < n:
        char = spec[i]
        if char == "<":
            end = spec.find(">", i)
            if end == -1:
                raise KeySpecError(f"unclosed '<' in {spec!r}")
            group = spec[i + 1 : end]
            if not group:
                raise KeySpecError(f"empty '<>' group in {spec!r}")
            if group.lower() == "leader":
                if leader_tokens is None:
                    raise KeySpecError("leader key may not contain <leader>")
                tokens.extend(leader_tokens)
            else:
                tokens.append(_parse_angle_group(group, spec))
            i = end + 1
        elif char == ">":
            raise KeySpecError(f"unexpected '>' in {spec!r}")
        else:
            tokens.append(_character_to_key(char))
            i += 1

    if not tokens:
        raise KeySpecError(f"no keys in {spec!r}")
    return tuple(tokens)


def _parse_angle_group(group: str, spec: str) -> str:
    """Resolve one `<...>` group (named key or modifier chord) to a Textual token."""
    parts = group.split("-")
    base = parts[-1]
    mods = parts[:-1]

    base_token = _resolve_base(base, spec)

    if not mods:
        return base_token

    seen: set[str] = set()
    for raw in mods:
        key = raw.lower()
        if key not in _MODIFIERS:
            raise KeySpecError(f"unknown modifier {raw!r} in {spec!r}")
        seen.add(_MODIFIERS[key])

    # Shift on a single letter is the uppercase letter, not a `shift+` token —
    # that is how the terminal (and Textual) deliver it.
    if seen == {"shift"} and len(base) == 1 and base.isalpha():
        return base.upper()

    ordered = [m for m in _MODIFIER_ORDER if m in seen]
    return "+".join((*ordered, base_token))


def _resolve_base(base: str, spec: str) -> str:
    """Normalize the base key of an angle group to a Textual token."""
    if not base:
        raise KeySpecError(f"missing key after modifier in {spec!r}")
    lowered = base.lower()
    if lowered == "leader":
        raise KeySpecError("<leader> may not be combined with modifiers")
    if lowered in _NAMED_KEYS:
        return _NAMED_KEYS[lowered]
    if len(base) == 1:
        # Single char base: lowercase letters so <C-S> == <C-s> == ctrl+s.
        return _character_to_key(base.lower() if base.isalpha() else base)
    raise KeySpecError(f"unknown key name {base!r} in {spec!r}")
