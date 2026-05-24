from __future__ import annotations

import tomllib
from pathlib import Path

from winter_cli.core.config_file import ConfigFileReadError, IConfigFileReader


class TomllibConfigFileReader:
    """Stdlib `tomllib` adapter for IConfigFileReader.

    Confines `tomllib` usage to this file. Wraps both `OSError` and
    `tomllib.TOMLDecodeError` in `ConfigFileReadError` so callers don't
    have to import either type.
    """

    @staticmethod
    def load(path: Path) -> dict:
        try:
            with path.open("rb") as f:
                return tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigFileReadError(f"reading {path} — {exc}") from exc


def _conforms_tomllib_config_file_reader(x: TomllibConfigFileReader) -> IConfigFileReader:
    return x
