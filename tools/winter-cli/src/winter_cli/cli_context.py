from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from winter_cli.container import Container


class LazyContainer:
    """Builds the process's one `Container` on first need.

    Paths that never resolve a provider (`winter --version`, `--help`) never pay for importing the
    container module. `built` tells the CLI boundary whether there is a tracer to export.
    """

    def __init__(self, factory: Callable[[], Container]) -> None:
        self._factory = factory
        self._container: Container | None = None

    @property
    def built(self) -> bool:
        return self._container is not None

    def get(self) -> Container:
        if self._container is None:
            self._container = self._factory()
        return self._container


@dataclass(frozen=True)
class CliContext:
    container_source: LazyContainer
    source_override: str | None = None
    service_orchestrator_override: str | None = None

    @classmethod
    def of(cls, container: Container) -> CliContext:
        """A context over an already-built `Container`."""
        return cls(container_source=LazyContainer(lambda: container))

    @property
    def container(self) -> Container:
        return self.container_source.get()


def cli_ctx(ctx: click.Context) -> CliContext:
    return ctx.obj
