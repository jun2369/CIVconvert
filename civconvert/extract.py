"""Read the line-item table of a Commercial Invoice PDF (pdfplumber table detection).

Columns are located by their header text (Model, Description, HS Code, ...), so the
result does not depend on column positions or on an external PDF-to-Excel converter.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field

import pdfplumber

from .profiles import TableSpec

_SPACES = re.compile(r"\s+")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_STRATEGIES = {
    "lines": {"vertical_strategy": "lines", "horizontal_strategy": "lines"},
    "text": {"vertical_strategy": "text", "horizontal_strategy": "text"},
}


class PdfError(Exception):
    """The PDF cannot be opened or read."""


@dataclass
class ColumnMap:
    index: dict[str, int]  # field -> cell position in the table row
    width: int


@dataclass
class PdfExtract:
    name: str
    page_count: int = 0
    text: str = ""
    header_found: bool = False
    items: list[dict[str, str]] = field(default_factory=list)
    totals: dict[str, str] | None = None
    notes: list[str] = field(default_factory=list)


def clean_cell(value: object) -> str:
    if value is None:
        return ""
    return _SPACES.sub(" ", _CONTROL.sub("", str(value))).strip()


def _open(data: bytes, name: str):
    try:
        return pdfplumber.open(io.BytesIO(data))
    except Exception as e:  # pdfminer raises many different exception types
        raise PdfError(f"{name}: cannot read the PDF ({e.__class__.__name__}: {e})") from e


def read_text(data: bytes, name: str) -> str:
    with _open(data, name) as pdf:
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
    with _open(data, name) as pdf:
        result.page_count = len(pdf.pages)
        for page_no, page in enumerate(pdf.pages, 1):
            texts.append(page.extract_text() or "")
            if done:
                continue
            tables = sorted(page.find_tables(settings), key=lambda t: (round(t.bbox[1]), t.bbox[0]))
            for table in tables:
                for raw in table.extract():
                    cells = [clean_cell(c) for c in raw]
                    if not any(cells):
                        continue
                    header = match_header(cells, spec)
                    if header is not None:  # also skips headers repeated on later pages
                        colmap, result.header_found = ColumnMap(header, len(cells)), True
                        continue
                    if colmap is None or len(cells) != colmap.width:
                        continue  # not (yet) inside the item table
                    first = next(c for c in cells if c)
                    if spec.stop_row is not None and spec.stop_row.search(first):
                        if spec.totals_row is not None and spec.totals_row.search(first):
                            result.totals = {f: cells[i] for f, i in colmap.index.items()}
                        done = True
                        break
                    item = {f: cells[i] for f, i in colmap.index.items()}
                    if result.items and _is_continuation(item, spec):
                        prev = result.items[-1]
                        for f in spec.continuation_fields:
                            if item.get(f):
                                prev[f] = f"{prev[f]} {item[f]}".strip()
                        result.notes.append(
                            f"{name} page {page_no}: a row without {spec.continuation_anchor} was joined to the line above ('{first}')"
                        )
                        continue
                    item["_source"] = f"{name} p.{page_no}"
                    result.items.append(item)
                if done:
                    break
    result.text = "\n".join(texts)
    return result, colmap
