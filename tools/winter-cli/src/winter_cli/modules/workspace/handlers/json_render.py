from __future__ import annotations

import dataclasses
import enum
import json
from typing import Any

import click


def to_dict(obj: Any) -> Any:
    """Recursively unwrap dataclasses/enums/lists/dicts into plain JSON-able
    values — the one conversion every `--json`-rendering handler needs, and
    the same shape regardless of which report dataclass it's walking.

    Was duplicated byte-for-byte in `restack_handler.py` and
    `workspace_handler.py`; lifted here the moment a third caller would have
    made it three.
    """
    if isinstance(obj, enum.Enum):
        return obj.value
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: to_dict(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, list):
        return [to_dict(i) for i in obj]
    if isinstance(obj, dict):
        return {k: to_dict(v) for k, v in obj.items()}
    return obj


def echo_json(data: Any) -> None:
    click.echo(json.dumps(data, default=str, indent=2))
