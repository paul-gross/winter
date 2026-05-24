from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

import click

from winter_cli.core.cli_output_service import Cell, ICliOutputService


class ClickCliOutputService:
    """Click-backed ICliOutputService — renders styled text and space-aligned tables.

    Output goes through ``click.style`` for ANSI styling. The handlers echo the
    resulting strings via ``click.echo``, which strips ANSI when stdout isn't a
    TTY and respects the ``NO_COLOR`` env var.
    """

    def style(self, text: str, style: str) -> str:
        if not text:
            return text
        return click.style(text, **self._style_kwargs(style))

    @staticmethod
    def _style_kwargs(name: str) -> dict[str, Any]:
        """Translate a style name into click.style keyword arguments."""
        if name == "bold":
            return {"bold": True}
        if name == "dim":
            return {"dim": True}
        if name == "dark_orange":
            return {"fg": 208}
        return {"fg": name}

    def render_table(
        self,
        rows: Iterable[Sequence[str | Cell]],
        *,
        headers: Sequence[str | Cell] | None = None,
        row_styles: Sequence[str | None] | None = None,
        gap: int = 2,
    ) -> list[str]:
        """Render rows as space-aligned columns, no borders.

        Each cell may be a plain ``str`` or a ``Cell``. Column widths are
        computed from cell plain-text length; ANSI styling is applied after
        layout so styled cells never perturb alignment.

        ``headers`` adds a header row above the data — the caller passes
        them however they want (typically uppercase).

        ``row_styles`` is an optional list parallel to ``rows``. When set,
        the corresponding row is wrapped in the named style after its cells
        are rendered. Row-level and cell-level styling should not be combined
        on the same row — the row-level reset code will end any embedded
        styling early.

        Trailing empty cells are dropped before rendering so rows can have
        fewer columns than the widest row without producing trailing
        whitespace.
        """
        normalized: list[list[Cell]] = []
        for row in rows:
            normalized.append([c if isinstance(c, Cell) else Cell.of(c) for c in row])

        normalized_headers: list[Cell] | None = None
        if headers is not None:
            normalized_headers = [h if isinstance(h, Cell) else Cell.of(h) for h in headers]

        if not normalized and normalized_headers is None:
            return []

        n_cols = max((len(r) for r in normalized), default=0)
        if normalized_headers is not None:
            n_cols = max(n_cols, len(normalized_headers))

        padded = [r + [Cell.of("")] * (n_cols - len(r)) for r in normalized]

        widths = [0] * n_cols
        for row in padded:
            for i, cell in enumerate(row):
                widths[i] = max(widths[i], len(cell.text))
        if normalized_headers is not None:
            for i, h in enumerate(normalized_headers):
                widths[i] = max(widths[i], len(h.text))

        sep = " " * gap
        lines: list[str] = []

        if normalized_headers is not None:
            header_row = normalized_headers + [Cell.of("")] * (n_cols - len(normalized_headers))
            lines.append(self._render_row(header_row, widths, sep, row_style=None))

        styles: list[str | None]
        if row_styles is None:
            styles = [None] * len(padded)
        else:
            styles = list(row_styles)
            if len(styles) < len(padded):
                styles += [None] * (len(padded) - len(styles))

        for row, row_style in zip(padded, styles, strict=False):
            lines.append(self._render_row(row, widths, sep, row_style=row_style))

        return lines

    def _render_row(
        self,
        cells: Sequence[Cell],
        widths: Sequence[int],
        sep: str,
        row_style: str | None,
    ) -> str:
        last = len(cells)
        while last > 0 and cells[last - 1].text == "":
            last -= 1
        if last == 0:
            return ""

        rendered_parts: list[str] = []
        for i in range(last):
            cell = cells[i]
            plain = cell.text
            rendered = self._render_cell(cell)
            if i < last - 1:
                rendered = rendered + " " * (widths[i] - len(plain))
            rendered_parts.append(rendered)

        line = sep.join(rendered_parts).rstrip()
        if row_style:
            line = self.style(line, row_style)
        return line

    def _render_cell(self, cell: Cell) -> str:
        if len(cell.segments) == 1:
            text, style = cell.segments[0]
            if not style or not text:
                return text
            return click.style(text, **self._style_kwargs(style))
        parts: list[str] = []
        for text, style in cell.segments:
            if style and text:
                parts.append(click.style(text, **self._style_kwargs(style)))
            else:
                parts.append(text)
        return "".join(parts)


def _conforms_click_cli_output_service(x: ClickCliOutputService) -> ICliOutputService:
    return x
