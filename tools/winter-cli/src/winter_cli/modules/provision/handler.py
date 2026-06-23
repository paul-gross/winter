from __future__ import annotations

import dataclasses
import sys

from winter_cli.modules.provision.provision_reporter import (
    IProvisionReporter,
    JsonProvisionReporter,
    StreamProvisionReporter,
)
from winter_cli.modules.provision.provision_service import ProvisionService


@dataclasses.dataclass
class ProvisionParams:
    """Parsed parameters for a ``winter provision`` invocation."""

    env: str
    subtarget: str | None = None
    reset: bool = False
    destroy: bool = False
    seed: bool = False
    no_service_check: bool = False
    dry_run: bool = False
    output_json: bool = False


class ProvisionCommandHandler:
    """Dispatches ``winter provision`` runs to the service with the right reporter.

    Named ``ProvisionCommandHandler`` (not ``ProvisionHandler``) to avoid
    collision with the manifest dataclass ``ProvisionHandler`` in
    ``modules.provision.manifest``.
    """

    def __init__(
        self,
        provision_service: ProvisionService,
        stream_reporter: StreamProvisionReporter,
        json_reporter: JsonProvisionReporter,
    ) -> None:
        self._provision_service = provision_service
        self._stream_reporter = stream_reporter
        self._json_reporter = json_reporter

    def run(self, params: ProvisionParams) -> None:
        reporter: IProvisionReporter = self._json_reporter if params.output_json else self._stream_reporter
        summary = self._provision_service.run(
            env_name=params.env,
            subtarget=params.subtarget,
            reset=params.reset,
            destroy=params.destroy,
            seed=params.seed,
            no_service_check=params.no_service_check,
            reporter=reporter,  # type: ignore[arg-type]
            dry_run=params.dry_run,
        )
        if summary.exit_code != 0:
            sys.exit(summary.exit_code)
