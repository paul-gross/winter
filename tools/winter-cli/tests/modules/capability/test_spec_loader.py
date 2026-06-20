from __future__ import annotations

import pytest

from tests.conftest import FakeConfigFileReader
from winter_cli.modules.capability.spec_loader import _SPECS_DIR, SpecLoader, SpecLoadError
from winter_cli.modules.capability.spec_models import ArityKind, CheckKind

# ── helpers ─────────────────────────────────────────────────────────────────


def _loader_real() -> SpecLoader:
    """SpecLoader backed by the real TomllibConfigFileReader (reads bundled specs)."""
    from winter_cli.core.internal.tomllib_config_file_reader import TomllibConfigFileReader

    return SpecLoader(config_file_reader=TomllibConfigFileReader())


# ── 1. supported_versions derives from the bundled spec files ────────────────


def test_supported_versions_service_returns_v1() -> None:
    loader = _loader_real()
    assert loader.supported_versions("service") == {"v1"}


def test_supported_versions_unknown_slot_is_empty() -> None:
    loader = _loader_real()
    assert loader.supported_versions("nonexistent") == set()


# ── 2. load("service", "v1") round-trip ─────────────────────────────────────


def test_load_service_v1_slot_and_version() -> None:
    spec = _loader_real().load("service", "v1")
    assert spec.slot == "service"
    assert spec.version == "v1"


def test_load_service_v1_has_six_actions() -> None:
    spec = _loader_real().load("service", "v1")
    action_names = {a.name for a in spec.actions}
    assert action_names == {"up", "down", "status", "restart", "logs", "describe"}


def test_load_service_v1_action_arities() -> None:
    spec = _loader_real().load("service", "v1")
    by_name = {a.name: a for a in spec.actions}

    assert by_name["up"].arity == ArityKind.single_env
    assert by_name["down"].arity == ArityKind.single_env
    assert by_name["status"].arity == ArityKind.patterns_optional
    assert by_name["restart"].arity == ArityKind.patterns_required
    assert by_name["logs"].arity == ArityKind.patterns_required
    assert by_name["describe"].arity == ArityKind.no_positionals


def test_load_service_v1_always_present_env_vars() -> None:
    spec = _loader_real().load("service", "v1")
    env_names = {e.name for e in spec.env_vars}
    assert "WINTER_WORKSPACE_DIR" in env_names
    assert "WINTER_EXT_DIR" in env_names
    assert "WINTER_EXT_PREFIX" in env_names


def test_load_service_v1_logs_per_action_env_vars() -> None:
    spec = _loader_real().load("service", "v1")
    logs_action = next(a for a in spec.actions if a.name == "logs")
    env_names = {e.name for e in logs_action.env_vars}
    assert "WINTER_LOG_FOLLOW" in env_names
    assert "WINTER_LOG_TAIL" in env_names
    assert "WINTER_LOG_SINCE" in env_names
    assert "WINTER_LOG_UNTIL" in env_names
    assert "WINTER_LOG_TIMESTAMPS" in env_names


def test_load_service_v1_exit_codes_include_0_2_3() -> None:
    spec = _loader_real().load("service", "v1")
    codes = {e.code for e in spec.exit_codes}
    assert 0 in codes
    assert 2 in codes
    assert 3 in codes


def test_load_service_v1_checks_cover_all_three_kinds() -> None:
    spec = _loader_real().load("service", "v1")
    kinds = {c.kind for c in spec.checks}
    assert CheckKind.accepts_action in kinds
    assert CheckKind.refuses_unknown in kinds
    assert CheckKind.forwards_params in kinds


# ── 3. error paths ───────────────────────────────────────────────────────────


def test_load_raises_spec_load_error_for_missing_spec() -> None:
    loader = _loader_real()
    with pytest.raises(SpecLoadError):
        loader.load("service", "v99")


def test_load_raises_spec_load_error_for_unknown_slot() -> None:
    loader = _loader_real()
    with pytest.raises(SpecLoadError):
        loader.load("nonexistent", "v1")


def test_load_raises_spec_load_error_for_malformed_toml() -> None:
    """A spec TOML that parses but is missing required keys raises SpecLoadError."""
    spec_path = _SPECS_DIR / "service-v1.toml"

    # Return a dict missing the required "slot" key → KeyError in _parse → SpecLoadError.
    reader = FakeConfigFileReader(files={spec_path: {"version": "v1"}})
    loader = SpecLoader(config_file_reader=reader)
    with pytest.raises(SpecLoadError):
        loader.load("service", "v1")
