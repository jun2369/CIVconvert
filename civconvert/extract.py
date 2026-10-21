"""Read the line-item table of a Commercial Invoice PDF (pdfplumber table detection).

Columns are located by their header text (Model, Description, HS Code, ...), so the
result does not depend on column positions or on an external PDF-to-Excel converter.
Where each row sits on the page is kept as well, so the preview can point at it.
A picture (PNG, JPG …) is opened as a PDF, and pages without text are read by OCR (ocr.py).
"""

from __future__ import annotations

import hashlib
import io
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation

import pdfplumber

from . import ocr, sources
from .profiles import TableSpec

_SPACES = re.compile(r"\s+")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_AMOUNT = re.compile(r"^[-+(]?(?:[A-Za-z]{2,3})?[$€£¥]?\d[\d,]*(?:\.\d+)?\)?(?:[A-Za-z]{1,5})?$")  # 21,480.00 / USD21480 / 358PCS
NUMBER_FIELDS = ("qty", "amount", "unit_price", "net_weight", "gross_weight")
TEXT_FIELDS = ("model", "description", "hs_code", "origin")
_picture_pdfs: OrderedDict[str, bytes] = OrderedDict()  # pictures opened lately, as PDFs
_picture_lock = threading.Lock()
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
    label_values: str = ""  # Excel: labels with their value in the cell below, as "label: value" lines
    ocr_pages: list[int] = field(default_factory=list)  # pages read by OCR (a picture or a scan)


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


def _picture_as_pdf(data: bytes, name: str) -> bytes:
    key = hashlib.sha1(data).hexdigest()
    with _picture_lock:
        if key in _picture_pdfs:
            _picture_pdfs.move_to_end(key)
            return _picture_pdfs[key]
    try:
        pdf_data, _ = sources.picture_pdf(data, name)
    except sources.SourceError as e:
        raise PdfError(str(e)) from e
    with _picture_lock:
        _picture_pdfs[key] = pdf_data
        while len(_picture_pdfs) > 16:
            _picture_pdfs.popitem(last=False)
    return pdf_data


def open_pdf(data: bytes, name: str, ocr_mode: str = ocr.AUTO, ocr_limit: int = ocr.MAX_PAGES):
    """The PDF (pdfplumber) – a picture opened as a PDF (one page per picture page). Pages without text are
    read by OCR as `ocr_mode` says (ocr.fill), at most `ocr_limit` of them. `pdf.ocr_pages`: the pages read
    so; `pdf.unread_pages`: pictures of pages left without text (a PDF with text on other pages)."""
    picture = sources.kind(name, data) == sources.IMAGE
    try:
        pdf = pdfplumber.open(io.BytesIO(_picture_as_pdf(data, name) if picture else data))
    except PdfError:
        raise
    except Exception as e:  # pdfminer raises many different exception types
        raise PdfError(f"{name}: cannot read the PDF ({e.__class__.__name__}: {e})") from e
    pages = (lambda: sources.picture_pages(data, name)) if picture else None
    pdf.ocr_pages, pdf.unread_pages = ocr.fill(pdf, data, pages, ocr_mode, ocr_limit)
    return pdf


def read_text(data: bytes, name: str, ocr_mode: str = ocr.AUTO) -> str:
    with open_pdf(data, name, ocr_mode) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages)


def _decimal(text: str | None) -> Decimal | None:
    digits = re.sub(r"[^\d.\-]", "", (text or "").replace(",", ""))
    try:
        return Decimal(digits) if re.search(r"\d", digits) else None
    except InvalidOperation:
        return None


def unlabeled_totals(items: list[dict[str, str]], row: dict[str, str]) -> bool:
    """A row without a "Total" label that is the table's totals row: numbers only, no product code /
    description / HS code, and its quantity or amount is the sum of the lines above."""
    if any(row.get(f) for f in TEXT_FIELDS):
        return False
    numbers = {f: row[f] for f in NUMBER_FIELDS if row.get(f)}
    if not numbers or not all(_AMOUNT.match(v.replace(" ", "")) for v in numbers.values()):
        return False
    return any(_is_sum(items, f, numbers.get(f)) for f in ("qty", "amount"))


def _is_sum(items: list[dict[str, str]], f: str, text: str | None) -> bool:
    total = _decimal(text)
    values = [_decimal(i.get(f)) for i in items]
    return total is not None and bool(values) and None not in values and abs(sum(values, Decimal(0)) - total) <= Decimal("0.01")


def place_sums(items: list[dict[str, str]], row: dict[str, str]) -> dict[str, str]:
    """A totals row with its quantity / amount sum printed under another column ("358" under "Unit"):
    a number there that is exactly the sum counts for the quantity / amount."""
    out = dict(row)
    for f in ("qty", "amount"):
        if out.get(f):
            continue
        for other, text in row.items():
            if other not in (*NUMBER_FIELDS, *TEXT_FIELDS) and not other.startswith("_") and _AMOUNT.match((text or "").replace(" ", "")) and _is_sum(items, f, text):
                out[f] = text
                break
    return out


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
    data: bytes, name: str, spec: TableSpec, carry: ColumnMap | None = None, ocr_mode: str = ocr.AUTO
) -> tuple[PdfExtract, ColumnMap | None]:
    """Line items of one PDF.

    `carry` is the column layout found in a previous PDF part of the same invoice, so a
    table that continues in the next file without repeating its header is still read.
    """
    settings = {**_STRATEGIES[spec.strategy], **spec.pdfplumber}
    result = PdfExtract(name=name)
    colmap, done, texts = carry, False, []
    with open_pdf(data, name, ocr_mode) as pdf:
        result.page_count = len(pdf.pages)
        result.ocr_pages = list(pdf.ocr_pages)
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
                    if spec.numeric_totals and result.rows and result.rows[-1].page == page_no and unlabeled_totals(result.items, item):
                        # the sums right under the lines (not a carried-forward amount at the top of the next page)
                        result.totals, result.totals_row, done = place_sums(result.items, item), box, True
                        break
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
