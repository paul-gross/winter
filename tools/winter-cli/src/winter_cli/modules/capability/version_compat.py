from __future__ import annotations

from winter_cli.modules.capability.models import CapabilityBindingError


class VersionCompatError(CapabilityBindingError):
    """Raised by `CapabilityRegistryService.resolve()` when the bound provider
    implements a spec version that this winter installation does not support.

    Subclasses `CapabilityBindingError` so the CLI boundary renders it cleanly
    and callers can match on either the broad or the specific type.
    """


def check_compat(slot: str, implemented_version: str | None, supported_versions: set[str]) -> str | None:
    """Return an error message when `implemented_version` is incompatible, else None.

    Compatibility rules:
    - `implemented_version` is None → extension predates the `implements` field;
      treated as compatible (lenient-when-absent). Returns None.
    - `implemented_version` is in `supported_versions` → compatible. Returns None.
    - Otherwise → incompatible. Returns a human-readable error message naming
      both the implemented version and the supported set.
    """
    if implemented_version is None:
        return None
    if implemented_version in supported_versions:
        return None
    supported_str = "{" + ", ".join(sorted(supported_versions)) + "}"
    return (
        f"capabilities.{slot} provider implements {slot} spec {implemented_version},"
        f" but this winter supports {supported_str}."
        f" Upgrade winter or pin the extension to a supported version."
    )
