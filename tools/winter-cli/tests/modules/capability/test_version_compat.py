from __future__ import annotations

from winter_cli.modules.capability.models import CapabilityBindingError
from winter_cli.modules.capability.version_compat import VersionCompatError, check_compat

# ── check_compat: version string checks ──────────────────────────────────────


def test_check_compat_returns_none_when_implemented_version_is_none() -> None:
    """No implements declaration → treated as compatible."""
    result = check_compat("service", None, {"v1"})
    assert result is None


def test_check_compat_returns_none_when_version_in_supported() -> None:
    """Declared version matches a supported version → compatible."""
    result = check_compat("service", "v1", {"v1"})
    assert result is None


def test_check_compat_returns_message_when_version_not_in_supported() -> None:
    """Declared version not in supported set → incompatible, returns error message."""
    result = check_compat("service", "v2", {"v1"})
    assert result is not None
    assert "v2" in result
    assert "v1" in result


def test_check_compat_message_names_both_implemented_and_supported() -> None:
    """Error message must name the implemented version AND the supported set."""
    result = check_compat("service", "v2", {"v1"})
    assert result is not None
    # Both versions appear in the message
    assert "v2" in result
    assert "{v1}" in result


def test_check_compat_message_names_slot() -> None:
    """Error message includes the capability slot name."""
    result = check_compat("service", "v2", {"v1"})
    assert result is not None
    assert "service" in result


def test_check_compat_compatible_when_version_is_in_multi_version_set() -> None:
    """Provider implements v1; winter supports {v1, v2} → compatible."""
    result = check_compat("service", "v1", {"v1", "v2"})
    assert result is None


def test_check_compat_incompatible_against_empty_supported() -> None:
    """Declared version against empty supported set → always incompatible."""
    result = check_compat("service", "v1", set())
    assert result is not None


# ── VersionCompatError is a subclass of CapabilityBindingError ────────────────


def test_version_compat_error_is_capability_binding_error() -> None:
    err = VersionCompatError("test")
    assert isinstance(err, CapabilityBindingError)
