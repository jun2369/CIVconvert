"""What kind of file an invoice is – PDF or Excel – and Excel workbooks as plain grids of cell values.

The file type is told by the content, not only by the name: a PDF starts with %PDF, an .xlsx is a zip
holding xl/workbook.xml, an .xls is an OLE2 file. Cell values become text the way Excel shows them
in the General format (no 0.0012000000000000001), dates as YYYY-MM-DD.
"""

from __future__ import annotations

import datetime as dt
import io
import re
import unicodedata
import zipfile
from dataclasses import dataclass, field
from typing import Any

from .formula import format_number

PDF, XLSX, XLS = "pdf", "xlsx", "xls"
CONTENT_TYPES = {
    PDF: "application/pdf",
    XLSX: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    XLS: "application/vnd.ms-excel",
}
EXTENSIONS = (".pdf", ".xlsx", ".xlsm", ".xls")
MAX_ROWS, MAX_COLS = 3000, 80  # of a sheet that are read
_OLE2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ZERO_WIDTH = re.compile(r"[​-‏⁠﻿­]")
_SPACES = re.compile(r"\s+")


class SourceError(Exception):
    """The file cannot be read."""


def kind(name: str, data: bytes) -> str | None:
    """PDF, XLSX, XLS – or None for anything else."""
    if b"%PDF" in data[:1024]:
        return PDF
    if data[:4] == b"PK\x03\x04":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                if "xl/workbook.xml" in z.namelist():
                    return XLSX
        except zipfile.BadZipFile:
            return None
        return None
    if data[:8] == _OLE2 and name.lower().endswith(".xls"):
        return XLS
    return None


def strip_extension(name: str) -> str:
    return re.sub(r"\.(pdf|xlsx|xlsm|xls)$", "", name, flags=re.IGNORECASE)


@dataclass
class Grid:
    """One worksheet: rows of cell values (str, int, float, bool, date/datetime or None)."""

    name: str
    cells: list[list[Any]]
    # merged cells: (first row, first column, last row, last column), 0-based; the value is in the first cell
    merged: list[tuple[int, int, int, int]] = field(default_factory=list)

    def text(self) -> str:
        return "\n".join(" ".join(t for t in (cell_text(v) for v in row) if t) for row in self.cells)


def clean_text(value: str) -> str:
    return _SPACES.sub(" ", _ZERO_WIDTH.sub("", value)).strip()


def cell_text(value: Any) -> str:
    """A cell value as Excel's General format shows it."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, dt.datetime):
        return value.date().isoformat() if value.time() == dt.time(0) else value.isoformat(sep=" ", timespec="minutes")
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value.is_integer() and abs(value) < 1e15:
            return str(int(value))
        return format_number(value)
    return clean_text(unicodedata.normalize("NFC", str(value)))


def read_workbook(data: bytes, name: str, file_kind: str) -> list[Grid]:
    """The visible worksheets of an .xlsx / .xlsm / .xls file."""
    try:
        if file_kind == XLSX:
            return _read_xlsx(data)
        if file_kind == XLS:
            return _read_xls(data)
    except SourceError:
        raise
    except Exception as e:  # the readers raise many different exception types
        raise SourceError(f"{name}: cannot read the Excel file ({e.__class__.__name__}: {e})") from e
    raise SourceError(f"{name}: not an Excel file")


def _read_xlsx(data: bytes) -> list[Grid]:
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(data), data_only=True)  # formulas: the values Excel saved with them
    grids = []
    for ws in wb.worksheets:
        if ws.sheet_state != "visible":
            continue
        rows = []
        for row in ws.iter_rows(max_row=min(ws.max_row, MAX_ROWS), max_col=min(ws.max_column, MAX_COLS), values_only=True):
            rows.append(list(row))
        merged = [(m.min_row - 1, m.min_col - 1, m.max_row - 1, m.max_col - 1) for m in ws.merged_cells.ranges]
        grids.append(Grid(ws.title, rows, [m for m in merged if m[0] < MAX_ROWS and m[1] < MAX_COLS]))
    return grids


def _read_xls(data: bytes) -> list[Grid]:
    import xlrd

    try:  # with the cell formats: needed for the merged cells
        book = xlrd.open_workbook(file_contents=data, formatting_info=True)
    except Exception:
        book = xlrd.open_workbook(file_contents=data)
    grids = []
    for sheet in book.sheets():
        if getattr(sheet, "visibility", 0):
            continue
        rows = []
        for r in range(min(sheet.nrows, MAX_ROWS)):
            row = []
            for c in range(min(sheet.ncols, MAX_COLS)):
                cell = sheet.cell(r, c)
                if cell.ctype == xlrd.XL_CELL_DATE:
                    row.append(xlrd.xldate.xldate_as_datetime(cell.value, book.datemode))
                elif cell.ctype == xlrd.XL_CELL_NUMBER:
                    row.append(int(cell.value) if float(cell.value).is_integer() else cell.value)
                elif cell.ctype == xlrd.XL_CELL_TEXT:
                    row.append(cell.value)
                elif cell.ctype == xlrd.XL_CELL_BOOLEAN:
                    row.append(bool(cell.value))
                else:
                    row.append(None)
            rows.append(row)
        merged = [(r0, c0, r1 - 1, c1 - 1) for r0, r1, c0, c1 in getattr(sheet, "merged_cells", [])]
        grids.append(Grid(sheet.name, rows, [m for m in merged if m[0] < MAX_ROWS and m[1] < MAX_COLS]))
    return grids
