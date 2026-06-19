from __future__ import annotations

from winter_cli.modules.capability.capability_registry_service import CapabilityRegistryService
from winter_cli.modules.capability.models import BindingKind
from winter_cli.modules.doctor.models import ProbeResult, ProbeStatus

CAPABILITY_SOURCE = "capabilities"


class CapabilityProbeService:
    """Probes capability-registry health — one ProbeResult per known slot."""

    def __init__(self, registry: CapabilityRegistryService) -> None:
        self._registry = registry

    def run(self) -> list[ProbeResult]:
        results: list[ProbeResult] = []
        for res in self._registry.describe_all():
            slot_name = res.slot.value
            probe_name = f"slot: {slot_name}"

            if res.is_ambiguous:
                names = ", ".join(c.extension_name for c in res.candidates)
                results.append(
                    ProbeResult(
                        source=CAPABILITY_SOURCE,
                        name=probe_name,
                        status=ProbeStatus.fail,
                        message=f"ambiguous — multiple providers: {names}",
                        remediation=(
                            f'Set capabilities.{slot_name} = "<name>" in .winter/config.toml to disambiguate.'
                        ),
                    )
                )
                continue

            binding_kind: BindingKind = res.binding_kind

            if binding_kind == "invalid":
                results.append(
                    ProbeResult(
                        source=CAPABILITY_SOURCE,
                        name=probe_name,
                        status=ProbeStatus.fail,
                        message=res.error or "invalid binding",
                        remediation=(
                            f"Check capabilities.{slot_name} in .winter/config.toml"
                            f" and the extension's provides.{slot_name} in winter-ext.toml."
                        ),
                    )
                )

            elif binding_kind == "incompatible":
                results.append(
                    ProbeResult(
                        source=CAPABILITY_SOURCE,
                        name=probe_name,
                        status=ProbeStatus.fail,
                        message=res.error or "version incompatible",
                        remediation=(
                            f"Upgrade winter to support the version declared in the extension's"
                            f" [implements] table, or pin the extension to a version compatible"
                            f" with this winter's supported {slot_name} spec versions."
                        ),
                    )
                )

            elif binding_kind == "implicit":
                candidate = res.candidates[0]
                if not candidate.entrypoint_valid:
                    results.append(
                        ProbeResult(
                            source=CAPABILITY_SOURCE,
                            name=probe_name,
                            status=ProbeStatus.fail,
                            message=(
                                f"implicit provider {candidate.extension_name!r} entrypoint not found"
                                f" at {candidate.entrypoint_path}"
                            ),
                            remediation=(f"Check provides.{slot_name} in {candidate.extension_name}/winter-ext.toml."),
                        )
                    )
                else:
                    results.append(
                        ProbeResult(
                            source=CAPABILITY_SOURCE,
                            name=probe_name,
                            status=ProbeStatus.pass_,
                            message=f"implicitly bound to {candidate.extension_name} (sole provider)",
                        )
                    )

            elif binding_kind == "explicit":
                results.append(
                    ProbeResult(
                        source=CAPABILITY_SOURCE,
                        name=probe_name,
                        status=ProbeStatus.pass_,
                        message=f"bound to {res.bound_extension}",
                    )
                )

            else:
                # binding_kind == "unbound" with 0 candidates
                results.append(
                    ProbeResult(
                        source=CAPABILITY_SOURCE,
                        name=probe_name,
                        status=ProbeStatus.warn,
                        message="no provider installed",
                    )
                )

        return results
