from __future__ import annotations

from pathlib import Path
from typing import Protocol

from winter_cli.core.config_file import ConfigFileReadError, IConfigFileReader
from winter_cli.modules.capability.spec_models import (
    ArityKind,
    CapabilitySpec,
    CheckKind,
    ExitCodeSpec,
    SpecAction,
    SpecCheck,
    SpecEnvVar,
)


class ISpecLoader(Protocol):
    """Protocol seam for spec loading — allows test doubles without dragging
    the real TOML parsing and file-glob logic into unit tests."""

    def supported_versions(self, slot: str) -> set[str]:
        """Return the set of supported versions for *slot*."""
        ...

    def load(self, slot: str, version: str) -> CapabilitySpec:
        """Load and parse the spec for *slot* at *version*."""
        ...

# Bundled specs directory — resolved relative to this file so the path works
# from both source (editable install) and an installed wheel.
_SPECS_DIR = Path(__file__).parent / "specs"


class SpecLoadError(Exception):
    """Raised when a capability spec cannot be loaded or parsed."""


class SpecLoader:
    """Loads machine-readable capability specs from the bundled `specs/` directory.

    `supported_versions(slot)` derives the available versions by globbing the
    shipped spec files — the set of files IS the declaration of what winter
    supports. No version list is hard-coded here.

    `load(slot, version)` reads and parses the corresponding TOML file into a
    `CapabilitySpec` dataclass tree.
    """

    def __init__(self, config_file_reader: IConfigFileReader) -> None:
        self._reader = config_file_reader

    def supported_versions(self, slot: str) -> set[str]:
        """Return the set of supported versions for *slot* (derived from shipped files)."""
        versions: set[str] = set()
        for path in _SPECS_DIR.glob(f"{slot}-*.toml"):
            # filename form: <slot>-<version>.toml  (e.g. service-v1.toml)
            stem = path.stem  # e.g. "service-v1"
            prefix = f"{slot}-"
            if stem.startswith(prefix):
                versions.add(stem[len(prefix) :])
        return versions

    def load(self, slot: str, version: str) -> CapabilitySpec:
        """Load and parse the spec for *slot* at *version*.

        Raises `SpecLoadError` if the spec file does not exist or cannot be parsed.
        """
        spec_path = _SPECS_DIR / f"{slot}-{version}.toml"
        try:
            data = self._reader.load(spec_path)
        except (ConfigFileReadError, FileNotFoundError) as exc:
            raise SpecLoadError(f"no spec for {slot}/{version}: {exc}") from exc

        return _parse(data, spec_path)


def _parse(data: dict, source: Path) -> CapabilitySpec:
    """Parse a raw TOML dict into a `CapabilitySpec`."""
    try:
        slot = str(data["slot"])
        version = str(data["version"])
        title = str(data["title"])

        env_vars = tuple(_parse_env_var(e) for e in data.get("env_var", []))
        exit_codes = tuple(_parse_exit_code(e) for e in data.get("exit_code", []))
        actions = tuple(_parse_action(a) for a in data.get("action", []))
        checks = tuple(_parse_check(c) for c in data.get("check", []))

    except (KeyError, ValueError, TypeError) as exc:
        raise SpecLoadError(f"malformed spec at {source}: {exc}") from exc

    return CapabilitySpec(
        slot=slot,
        version=version,
        title=title,
        actions=actions,
        exit_codes=exit_codes,
        env_vars=env_vars,
        checks=checks,
    )


def _parse_env_var(data: dict) -> SpecEnvVar:
    return SpecEnvVar(name=str(data["name"]), description=str(data["description"]))


def _parse_exit_code(data: dict) -> ExitCodeSpec:
    return ExitCodeSpec(code=int(data["code"]), meaning=str(data["meaning"]))


def _parse_action(data: dict) -> SpecAction:
    arity = ArityKind(data["arity"])
    env_vars = tuple(_parse_env_var(e) for e in data.get("env_var", []))
    return SpecAction(
        name=str(data["name"]),
        arity=arity,
        summary=str(data["summary"]),
        env_vars=env_vars,
    )


def _parse_check(data: dict) -> SpecCheck:
    kind = CheckKind(data["kind"])
    return SpecCheck(kind=kind, description=str(data["description"]))
