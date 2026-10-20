"""Read the line-item table of a Commercial Invoice PDF (pdfplumber table detection).

Columns are located by their header text (Model, Description, HS Code, ...), so the
result does not depend on column positions or on an external PDF-to-Excel converter.
Where each row sits on the page is kept as well, so the preview can point at it.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field, replace

import pdfplumber

from .profiles import TableSpec

_SPACES = re.compile(r"\s+")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_STRATEGIES = {
    "lines": {"vertical_strategy": "lines", "horizontal_strategy": "lines"},
    "text": {"vertical_strategy": "text", "horizontal_strategy": "text"},
}

Bbox = tuple[float, float, float, float]  # x0, top, x1, bottom in PDF points (pdfplumber page coordinates)


class PdfError(Exception):
    """The PDF cannot be opened or read."""


@dataclass
class ColumnMap:
    index: dict[str, int]  # field -> cell position in the table row
    width: int


@dataclass
class RowBox:
    """Where a table row is on its page."""

    page: int  # 1-based
    bbox: Bbox
    cells: dict[str, Bbox]  # field -> cell
    joined: int = 0  # wrapped rows below that were joined to this line

    def join(self, other: RowBox) -> RowBox:
        if other.page != self.page:  # the wrapped text continues on the next page
            return replace(self, joined=self.joined + 1)
        cells = {f: _union(c, other.cells[f]) if f in other.cells else c for f, c in self.cells.items()}
        return RowBox(self.page, _union(self.bbox, other.bbox), cells, self.joined + 1)


@dataclass
class PdfExtract:
    name: str
    page_count: int = 0
    text: str = ""
    header_found: bool = False
    labels: dict[str, str] = field(default_factory=dict)  # field -> its header text in the PDF
    items: list[dict[str, str]] = field(default_factory=list)
    rows: list[RowBox] = field(default_factory=list)  # where each item is (same order as items)
    headers: list[RowBox] = field(default_factory=list)  # the header row, and its repeats on later pages
    totals: dict[str, str] | None = None
    totals_row: RowBox | None = None
    notes: list[str] = field(default_factory=list)
    layout: dict | None = None  # Excel: the sheet as drawn for the preview (generic.read_excel)


def clean_cell(value: object) -> str:
    if value is None:
        return ""
    return _SPACES.sub(" ", _CONTROL.sub("", str(value))).strip()


def _union(a: Bbox, b: Bbox) -> Bbox:
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def _row_box(page_no: int, row, index: dict[str, int]) -> RowBox:
    """`row` is a pdfplumber table row; a cell is None where a merged cell spans it."""
    cells = {f: tuple(row.cells[i]) for f, i in index.items() if i < len(row.cells) and row.cells[i] is not None}
    return RowBox(page_no, tuple(row.bbox), cells)


def open_pdf(data: bytes, name: str):
    try:
        return pdfplumber.open(io.BytesIO(data))
    except Exception as e:  # pdfminer raises many different exception types
        raise PdfError(f"{name}: cannot read the PDF ({e.__class__.__name__}: {e})") from e


def read_text(data: bytes, name: str) -> str:
    with open_pdf(data, name) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages)


def match_header(cells: list[str], spec: TableSpec) -> dict[str, int] | None:
    """Field -> cell index if this row is the item-table header, else None."""
    found: dict[str, int] = {}
    for fld, pattern in spec.columns.items():
        for i, cell in enumerate(cells):
            if cell and i not in found.values() and pattern.search(cell):
                found[fld] = i
                break
    return found if all(f in found for f in spec.required) else None


def _is_continuation(item: dict[str, str], spec: TableSpec) -> bool:
    """A row that only carries wrapped text of the line above (no model, no numbers)."""
    if not spec.continuation_anchor or item.get(spec.continuation_anchor):
        return False
    filled = [f for f, v in item.items() if v]
    return bool(filled) and all(f in spec.continuation_fields for f in filled)


def extract(
    data: bytes, name: str, spec: TableSpec, carry: ColumnMap | None = None
) -> tuple[PdfExtract, ColumnMap | None]:
    """Line items of one PDF.

    `carry` is the column layout found in a previous PDF part of the same invoice, so a
    table that continues in the next file without repeating its header is still read.
    """
    settings = {**_STRATEGIES[spec.strategy], **spec.pdfplumber}
    result = PdfExtract(name=name)
    colmap, done, texts = carry, False, []
    with open_pdf(data, name) as pdf:
        result.page_count = len(pdf.pages)
        for page_no, page in enumerate(pdf.pages, 1):
            texts.append(page.extract_text() or "")
            if done:
                continue
            tables = sorted(page.find_tables(settings), key=lambda t: (round(t.bbox[1]), t.bbox[0]))
            for table in tables:
                for row, raw in zip(table.rows, table.extract()):  # extract() returns table.rows in order
                    cells = [clean_cell(c) for c in raw]
                    if not any(cells):
                        continue
                    header = match_header(cells, spec)
                    if header is not None:  # also skips headers repeated on later pages
                        colmap, result.header_found = ColumnMap(header, len(cells)), True
                        result.labels = result.labels or {f: cells[i] for f, i in header.items()}
                        result.headers.append(_row_box(page_no, row, header))
                        continue
                    if colmap is None or len(cells) != colmap.width:
                        continue  # not (yet) inside the item table
                    first = next(c for c in cells if c)
                    if spec.stop_row is not None and spec.stop_row.search(first):
                        if spec.totals_row is not None and spec.totals_row.search(first):
                            result.totals = {f: cells[i] for f, i in colmap.index.items()}
                            result.totals_row = _row_box(page_no, row, colmap.index)
                        done = True
                        break
                    item = {f: cells[i] for f, i in colmap.index.items()}
                    box = _row_box(page_no, row, colmap.index)
                    if result.items and _is_continuation(item, spec):
                        prev = result.items[-1]
                        for f in spec.continuation_fields:
                            if item.get(f):
                                prev[f] = f"{prev[f]} {item[f]}".strip()
                        result.rows[-1] = result.rows[-1].join(box)
                        result.notes.append(
                            f"{name} page {page_no}: a row without {spec.continuation_anchor} was joined to the line above ('{first}')"
                        )
                        continue
                    item["_source"] = f"{name} p.{page_no}"
                    result.items.append(item)
                    result.rows.append(box)
                if done:
                    break
    result.text = "\n".join(texts)
    return result, colmap
