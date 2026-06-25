from __future__ import annotations

from winter_cli.config.models import AdoptExtensions, WorkspaceConfig
from winter_cli.core.config_file import ConfigFileReadError, IConfigFileReader
from winter_cli.core.filesystem import IFilesystemReader
from winter_cli.modules.doctor.models import ProbeResult, ProbeStatus
from winter_cli.modules.provision.manifest import (
    _ENTRY_ALLOWED_KEYS,
    _SUBTARGETS_WITH_REQUIRED_SERVICES,
    PROVISION_SUBTARGETS,
    ProvisionScope,
)
from winter_cli.modules.workspace.extension_manifest import EXT_MANIFEST, IExtensionManifestLoader
from winter_cli.modules.workspace.models import RepoError, StandaloneRepository

PROVISION_SOURCE = "provision"

# Known scope values for the lenient validator.
_VALID_SCOPES = frozenset(s.value for s in ProvisionScope)


def _validate_command_field(
    raw: object,
    location: str,
    field: str,
    *,
    required: bool,
) -> str | None:
    """Return an error message string if *raw* is invalid for a command field,
    or ``None`` if it is acceptable.

    Accepts a non-empty string or a non-empty list of non-empty strings.
    When *required* is ``False`` and *raw* is ``None`` (field absent), returns
    ``None`` (no finding).  Mirrors the accept/reject logic of
    ``manifest._parse_commands`` so the lenient probe and the strict parser
    agree on what is valid.
    """
    if raw is None:
        if required:
            return (
                f"{location} is missing required field {field!r} "
                f"(must be a non-empty string or non-empty list of strings)."
            )
        return None

    if isinstance(raw, str):
        if not raw:
            return f"{location} field {field!r} must be a non-empty string (got empty string)."
        return None

    if isinstance(raw, list):
        if not raw:
            return f"{location} field {field!r} must be a non-empty list (got empty list)."
        for idx, item in enumerate(raw):
            if not isinstance(item, str) or not item:
                return f"{location} field {field!r}[{idx}] must be a non-empty string (got {type(item).__name__!r})."
        return None

    return f"{location} field {field!r} must be a string or list of strings (got {type(raw).__name__!r})."


def _validate_raw_provision(raw: dict, source: str) -> list[ProbeResult]:
    """Lenient per-entry validator for a raw ``[provision]`` table.

    Unlike ``ProvisionManifestParser`` (which raises on the first error),
    this function collects every finding across all sub-targets and entries,
    emitting one ``fail`` ``ProbeResult`` per violation.  A well-formed
    manifest produces a single ``pass`` result; an empty or absent manifest
    produces no results.
    """
    if not raw:
        return []

    findings: list[ProbeResult] = []
    valid_entries = 0

    for key, entries in raw.items():
        if key not in PROVISION_SUBTARGETS:
            valid = ", ".join(repr(s) for s in PROVISION_SUBTARGETS)
            findings.append(
                ProbeResult(
                    source=PROVISION_SOURCE,
                    name=f"provision manifest: {source}",
                    status=ProbeStatus.fail,
                    message=(f"Unknown provision sub-target {key!r} in {source!r}. Must be one of: {valid}."),
                )
            )
            continue

        if not isinstance(entries, list):
            findings.append(
                ProbeResult(
                    source=PROVISION_SOURCE,
                    name=f"provision manifest: {source}",
                    status=ProbeStatus.fail,
                    message=(
                        f"provision.{key} in {source!r} must be a list of tables, got {type(entries).__name__!r}."
                    ),
                )
            )
            continue

        for i, entry in enumerate(entries):
            location = f"provision.{key}[{i}] in {source!r}"
            entry_ok = True

            if not isinstance(entry, dict):
                findings.append(
                    ProbeResult(
                        source=PROVISION_SOURCE,
                        name=f"provision manifest: {source}",
                        status=ProbeStatus.fail,
                        message=f"{location} must be a table (dict), got {type(entry).__name__!r}.",
                    )
                )
                continue

            # Unknown keys.
            unknown = set(entry.keys()) - _ENTRY_ALLOWED_KEYS
            if unknown:
                bad = ", ".join(repr(k) for k in sorted(unknown))
                allowed = ", ".join(repr(k) for k in sorted(_ENTRY_ALLOWED_KEYS))
                findings.append(
                    ProbeResult(
                        source=PROVISION_SOURCE,
                        name=f"provision manifest: {source}",
                        status=ProbeStatus.fail,
                        message=f"Unknown key(s) {bad} in {location}. Allowed keys: {allowed}.",
                    )
                )
                entry_ok = False

            # scope present and valid.
            scope_raw = entry.get("scope")
            if scope_raw is None:
                findings.append(
                    ProbeResult(
                        source=PROVISION_SOURCE,
                        name=f"provision manifest: {source}",
                        status=ProbeStatus.fail,
                        message=f"{location} is missing required field 'scope'.",
                    )
                )
                entry_ok = False
            elif scope_raw not in _VALID_SCOPES:
                valid_scopes = ", ".join(repr(s) for s in sorted(_VALID_SCOPES))
                findings.append(
                    ProbeResult(
                        source=PROVISION_SOURCE,
                        name=f"provision manifest: {source}",
                        status=ProbeStatus.fail,
                        message=(f"Invalid scope {scope_raw!r} in {location}. Must be one of: {valid_scopes}."),
                    )
                )
                entry_ok = False

            # apply present and non-empty string or non-empty list of non-empty strings.
            apply_raw = entry.get("apply")
            apply_msg = _validate_command_field(apply_raw, location, "apply", required=True)
            if apply_msg is not None:
                findings.append(
                    ProbeResult(
                        source=PROVISION_SOURCE,
                        name=f"provision manifest: {source}",
                        status=ProbeStatus.fail,
                        message=apply_msg,
                    )
                )
                entry_ok = False

            # destroy optional; present-but-invalid is a failure.
            destroy_raw = entry.get("destroy")
            destroy_msg = _validate_command_field(destroy_raw, location, "destroy", required=False)
            if destroy_msg is not None:
                findings.append(
                    ProbeResult(
                        source=PROVISION_SOURCE,
                        name=f"provision manifest: {source}",
                        status=ProbeStatus.fail,
                        message=destroy_msg,
                    )
                )
                entry_ok = False

            # reset optional; present-but-invalid is a failure.
            reset_raw = entry.get("reset")
            reset_msg = _validate_command_field(reset_raw, location, "reset", required=False)
            if reset_msg is not None:
                findings.append(
                    ProbeResult(
                        source=PROVISION_SOURCE,
                        name=f"provision manifest: {source}",
                        status=ProbeStatus.fail,
                        message=reset_msg,
                    )
                )
                entry_ok = False

            # required_services only on resource/data.
            required_services_raw = entry.get("required_services")
            if required_services_raw is not None and key not in _SUBTARGETS_WITH_REQUIRED_SERVICES:
                findings.append(
                    ProbeResult(
                        source=PROVISION_SOURCE,
                        name=f"provision manifest: {source}",
                        status=ProbeStatus.fail,
                        message=(
                            f"'required_services' is not allowed on {location}. "
                            f"'required_services' may only be declared on 'resource' or 'data' handlers."
                        ),
                    )
                )
                entry_ok = False

            if entry_ok:
                valid_entries += 1

    if not findings:
        return [
            ProbeResult(
                source=PROVISION_SOURCE,
                name=f"provision manifest: {source}",
                status=ProbeStatus.pass_,
                message=f"{valid_entries} provision handler(s) valid",
            )
        ]

    return findings


class ProvisionManifestProbeService:
    """Doctor probe that validates declared ``[[provision.*]]`` manifests.

    Performs a per-entry lenient walk (collecting ALL findings rather than
    stopping at the first error) across:

    1. The workspace ``[provision]`` table from ``.winter/config.toml``
       (via ``config.provision_raw``).
    2. Each installed extension's ``[provision]`` table from
       ``winter-ext.toml`` (via ``ExtensionManifestLoader``).

    When an extension manifest fails strict loading (e.g. because of a bad
    provision entry), the probe falls back to reading the raw ``winter-ext.toml``
    via ``config_file_reader`` and validates leniently, so per-finding results
    are emitted rather than one opaque ``RepoError``.

    This surfaces authoring mistakes before a ``winter provision`` run without
    aborting unrelated commands.
    """

    def __init__(
        self,
        config: WorkspaceConfig,
        fs: IFilesystemReader,
        manifest_loader: IExtensionManifestLoader,
        config_file_reader: IConfigFileReader,
    ) -> None:
        self._config = config
        self._fs = fs
        self._manifest_loader = manifest_loader
        self._config_file_reader = config_file_reader

    def run(self, standalone_repos: list[StandaloneRepository]) -> list[ProbeResult]:
        results: list[ProbeResult] = []
        results.extend(self._probe_workspace_provision())
        results.extend(self._probe_extension_provision(standalone_repos))
        return results

    def _probe_workspace_provision(self) -> list[ProbeResult]:
        raw = self._config.provision_raw
        if not raw:
            return []
        return _validate_raw_provision(raw, "project")

    def _probe_extension_provision(self, standalone_repos: list[StandaloneRepository]) -> list[ProbeResult]:
        if self._config.adopt_extensions == AdoptExtensions.none:
            return []

        results: list[ProbeResult] = []
        for repo in standalone_repos:
            results.extend(self._probe_one_extension(repo))
        return results

    def _probe_one_extension(self, repo: StandaloneRepository) -> list[ProbeResult]:
        manifest_path = repo.path / EXT_MANIFEST
        if not self._fs.is_file(manifest_path):
            return []

        # Attempt to load the manifest via the strict loader.  If it succeeds,
        # the provision entries already parsed cleanly — nothing more to check.
        try:
            self._manifest_loader.load(repo, manifest_path)
            return []
        except RepoError:
            # The manifest raised RepoError — may be due to a bad provision
            # entry.  Fall through to read the raw file leniently.
            pass

        # Load the raw TOML via the shared config_file_reader so that both
        # the real path and test fakes (FakeConfigFileReader) are supported.
        prefix = repo.prefix or repo.name
        try:
            raw_data = self._config_file_reader.load(manifest_path)
            prefix = repo.prefix or raw_data.get("prefix") or raw_data.get("name") or repo.name
        except (ConfigFileReadError, FileNotFoundError):
            return [
                ProbeResult(
                    source=PROVISION_SOURCE,
                    name=f"provision manifest: {prefix}",
                    status=ProbeStatus.fail,
                    message=f"could not read {EXT_MANIFEST} for {prefix!r}",
                )
            ]

        raw_provision = raw_data.get("provision")
        if not raw_provision or not isinstance(raw_provision, dict):
            # No provision section in the raw data; the RepoError was from
            # something else — not our concern here.
            return []

        return _validate_raw_provision(raw_provision, prefix)
