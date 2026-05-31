"""The documented action-id table is the single source of truth — pin it.

`actions.py` instructs authors to keep `ai/winter-cli/usage.md` in sync when they
add, rename, or remove a built-in action. This test converts that honor-system
reminder into a gate: the action ids in the usage.md keybindings table must match
`all_builtin_action_ids()` exactly, so a new `ActionBinding` (or a rename) fails
CI until the docs catch up.
"""

from __future__ import annotations

import re
from pathlib import Path

from winter_cli.modules.tui.keybindings.actions import all_builtin_action_ids

# this file -> keybindings/tui/modules/tests/winter-cli/tools/<repo root>.
# usage.md lives in the winter repo's ai/ tree.
_REPO_ROOT = Path(__file__).resolve().parents[6]
_USAGE_MD = _REPO_ROOT / "ai" / "winter-cli" / "usage.md"

# An action id: dotted lowercase segments. Excludes the `plugin.<name>` template.
_ACTION_ID = re.compile(r"`([a-z]+(?:\.[a-z_]+)+)`")


def _documented_ids() -> set[str]:
    text = _USAGE_MD.read_text()
    section = text.split("### Keybindings", 1)[1].split("**Key-spec grammar**", 1)[0]
    ids: set[str] = set()
    for line in section.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2 or cells[0] in ("Action id", "-----------"):
            continue
        # Only the first column holds action ids (`plugin.<name>` won't match).
        ids.update(_ACTION_ID.findall(cells[0]))
    return ids


def test_usage_doc_lists_exactly_the_builtin_action_ids() -> None:
    assert _USAGE_MD.is_file(), f"usage.md not found at {_USAGE_MD}"
    documented = _documented_ids()
    expected = all_builtin_action_ids()

    missing = expected - documented
    extra = documented - expected
    assert not missing, f"action ids in code but undocumented in usage.md: {sorted(missing)}"
    assert not extra, f"action ids documented in usage.md but absent from code: {sorted(extra)}"
