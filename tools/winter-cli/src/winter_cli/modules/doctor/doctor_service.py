from __future__ import annotations

from dataclasses import dataclass

from winter_cli.modules.doctor.agents_md_probe_service import AgentsMdProbeService
from winter_cli.modules.doctor.capability_probe_service import CapabilityProbeService
from winter_cli.modules.doctor.core_probe_service import CoreProbeService
from winter_cli.modules.doctor.doctor_reporter import IDoctorReporter
from winter_cli.modules.doctor.extension_probe_service import ExtensionProbeService
from winter_cli.modules.doctor.models import ProbeResult, ProbeStatus
from winter_cli.modules.doctor.port_probe_service import PortProbeService
from winter_cli.modules.doctor.skill_probe_service import SkillProbeService
from winter_cli.modules.doctor.workspace_probe_service import WorkspaceProbeService
from winter_cli.modules.provision.manifest_probe_service import ProvisionManifestProbeService
from winter_cli.modules.workspace.repository_factory import RepositoryFactory


@dataclass(frozen=True)
class DoctorSummary:
    total: int
    fails: int
    warns: int

    @property
    def exit_code(self) -> int:
        return 1 if self.fails else 0


class DoctorService:
    """Aggregates core preflight checks and each installed extension's probes.

    Probes are run sequentially (cheap, mostly local) and reported as they
    finish. The reporter is responsible for output formatting; this service
    only owns orchestration and aggregation.
    """

    def __init__(
        self,
        core_probe_svc: CoreProbeService,
        workspace_probe_svc: WorkspaceProbeService,
        extension_probe_svc: ExtensionProbeService,
        repo_factory: RepositoryFactory,
        capability_probe_svc: CapabilityProbeService,
        port_probe_svc: PortProbeService | None = None,
        provision_manifest_probe_svc: ProvisionManifestProbeService | None = None,
        skill_probe_svc: SkillProbeService | None = None,
        agents_md_probe_svc: AgentsMdProbeService | None = None,
    ) -> None:
        self._core_probe_svc = core_probe_svc
        self._workspace_probe_svc = workspace_probe_svc
        self._extension_probe_svc = extension_probe_svc
        self._repo_factory = repo_factory
        self._capability_probe_svc = capability_probe_svc
        self._port_probe_svc = port_probe_svc
        self._provision_manifest_probe_svc = provision_manifest_probe_svc
        self._skill_probe_svc = skill_probe_svc
        self._agents_md_probe_svc = agents_md_probe_svc

    def run(self, reporter: IDoctorReporter) -> DoctorSummary:
        reporter.started()
        results: list[ProbeResult] = []

        for result in self._core_probe_svc.run():
            reporter.probe_result(result)
            results.append(result)

        if self._port_probe_svc is not None:
            for result in self._port_probe_svc.run():
                reporter.probe_result(result)
                results.append(result)

        for result in self._workspace_probe_svc.run():
            reporter.probe_result(result)
            results.append(result)

        standalone_repos = self._repo_factory.get_standalone_repos()
        for result in self._extension_probe_svc.run(standalone_repos):
            reporter.probe_result(result)
            results.append(result)

        if self._provision_manifest_probe_svc is not None:
            for result in self._provision_manifest_probe_svc.run(standalone_repos):
                reporter.probe_result(result)
                results.append(result)

        if self._skill_probe_svc is not None:
            standalone_repos = self._repo_factory.get_standalone_repos()
            for result in self._skill_probe_svc.run(standalone_repos):
                reporter.probe_result(result)
                results.append(result)

        if self._agents_md_probe_svc is not None:
            for result in self._agents_md_probe_svc.run():
                reporter.probe_result(result)
                results.append(result)

        for result in self._capability_probe_svc.run():
            reporter.probe_result(result)
            results.append(result)

        fails = sum(1 for r in results if r.status == ProbeStatus.fail)
        warns = sum(1 for r in results if r.status == ProbeStatus.warn)
        summary = DoctorSummary(total=len(results), fails=fails, warns=warns)
        reporter.finished(summary.total, summary.fails, summary.warns)
        return summary
