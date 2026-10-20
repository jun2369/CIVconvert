"""Side-by-side review before export.

Left:  the invoice pages as images, plus where the table header, every line item and the
       subtotal row are on them, so the page can show just the table and point at a line.
Right: the template columns that are filled, read back from the Excel file that is exported
       (values, header text, column widths, fonts) – the preview shows what the file contains.

All positions are PDF points measured from the top-left corner of the page image.
"""

from __future__ import annotations

import base64
import csv
import io
from typing import Any

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter
from PIL import features

from . import extract
from .extract import Bbox, RowBox
from .pipeline import InputFile, Result

RESOLUTION = 216  # dpi = 3 image pixels per point: sharp up to ~200 % zoom on high-DPI screens
MAX_PAGES = 20  # pages without line items are only included up to this many pages
PAD = 8  # points of margin around the table when it is cut out of the page
if features.check("webp"):
    _IMAGE = ("WEBP", {"lossless": True, "quality": 50}, "image/webp")  # ~70 KB per A4 page
else:
    _IMAGE = ("PNG", {"optimize": True}, "image/png")


def _data_uri(image) -> str:
    fmt, options, mime = _IMAGE
    buf = io.BytesIO()
    image.save(buf, fmt, **options)
    return f"data:{mime};base64,{base64.b64encode(buf.getvalue()).decode('ascii')}"


def _crop(page, boxes: list[RowBox], area: Bbox) -> list[float]:
    """The table on this page (header, line items, subtotal) plus a margin – without cutting a text line in half."""
    x0 = min(b.bbox[0] for b in boxes) - PAD
    x1 = max(b.bbox[2] for b in boxes) + PAD
    top = min(b.bbox[1] for b in boxes) - PAD
    bottom = max(b.bbox[3] for b in boxes) + PAD
    words = [w for w in page.extract_words() if w["x1"] > x0 and w["x0"] < x1]
    for _ in range(3):
        moved = False
        for w in words:
            if w["top"] < top < w["bottom"]:
                top, moved = w["top"] - 2, True
            if w["top"] < bottom < w["bottom"]:
                bottom, moved = w["bottom"] + 2, True
        if not moved:
            break
    ax0, atop, ax1, abottom = area
    return [round(v, 2) for v in (max(x0, ax0) - ax0, max(top, atop) - atop, min(x1, ax1) - ax0, min(bottom, abottom) - atop)]


def _place(box: RowBox, index: dict[int, int], origin: dict[int, tuple[float, float]]) -> dict[str, Any]:
    if box.page not in index:
        return {"page": None, "bbox": None, "cells": {}, "joined": box.joined}
    ox, oy = origin[box.page]

    def move(b: Bbox) -> list[float]:
        return [round(b[0] - ox, 2), round(b[1] - oy, 2), round(b[2] - ox, 2), round(b[3] - oy, 2)]

    return {"page": index[box.page], "bbox": move(box.bbox), "cells": {f: move(c) for f, c in box.cells.items()}, "joined": box.joined}


def _shown(value: Any) -> str:
    """A cell value the way Excel shows it with the General number format."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float):
        return format(value, ".11g")
    return str(value)


def _rgb(color) -> str | None:
    if color is None or color.type != "rgb" or not isinstance(color.rgb, str):
        return None  # theme / indexed colors: leave it to the default
    return "#" + color.rgb[-6:]


def _style(cell) -> dict[str, Any]:
    font, fill = cell.font, cell.fill
    style: dict[str, Any] = {"font": font.name or "Arial", "size": float(font.sz or 10), "bold": bool(font.b)}
    if color := _rgb(font.color):
        style["color"] = color
    if fill is not None and fill.fill_type == "solid" and (bg := _rgb(fill.fgColor)):
        style["fill"] = bg
    return style


def _width(ws, col: int) -> int:
    """Column width in screen pixels (Excel: characters of the default font, ~7 px each + 5 px padding)."""
    width = ws.sheet_format.defaultColWidth or 8.43
    for dim in ws.column_dimensions.values():
        if dim.min and dim.max and dim.min <= col <= dim.max and dim.width:
            width = dim.width
            break
    return int(width * 7 + 5)


def grid(result: Result) -> dict[str, Any]:
    """The filled template columns as Excel shows them, read back from the exported workbook."""
    t = result.template.profile
    wb = load_workbook(io.BytesIO(result.xlsx_bytes()))
    ws = wb[t.sheet] if t.sheet else wb.worksheets[0]
    text = result.csv_bytes().decode(t.csv_encoding)
    csv_rows = list(csv.reader(io.StringIO(text, newline=""), delimiter=t.csv_delimiter))[1:]
    columns = []
    for c in sorted(result.columns, key=lambda c: c.index):
        cells = []
        for r in range(len(result.rows)):
            cell = ws.cell(row=t.header_row + 1 + r, column=c.index + 1)
            shown = _shown(cell.value)
            entry: dict[str, Any] = {"v": shown}
            if isinstance(cell.value, (int, float)) and not isinstance(cell.value, bool):
                entry["num"] = True  # Excel aligns numbers right
            in_csv = csv_rows[r][c.index] if r < len(csv_rows) and c.index < len(csv_rows[r]) else ""
            if in_csv != shown:
                entry["csv"] = in_csv
            cells.append(entry)
        columns.append({
            "index": c.index,
            "letter": c.letter,
            "header": _shown(ws.cell(row=t.header_row, column=c.index + 1).value),
            "field": c.spec.field,
            "width": _width(ws, c.index + 1),
            "head_style": _style(ws.cell(row=t.header_row, column=c.index + 1)),
            "cell_style": _style(ws.cell(row=t.header_row + 1, column=c.index + 1)),
            "cells": cells,
        })
    return {
        "sheet": ws.title,
        "header_row": t.header_row,
        "total_columns": len(result.template.headers),
        "columns": columns,
    }


def build(result: Result, files: list[InputFile]) -> dict[str, Any]:
    """Everything the preview page needs besides the conversion result itself."""
    pages: list[dict[str, Any]] = []
    lines: list[dict[str, Any]] = []
    headers: list[dict[str, Any]] = []
    totals: dict[str, Any] | None = None
    skipped = 0
    for f, part in zip(files, result.parts, strict=True):
        boxes = [*part.headers, *part.rows, *([part.totals_row] if part.totals_row else [])]
        index: dict[int, int] = {}  # page number -> position in `pages`
        origin: dict[int, tuple[float, float]] = {}
        with extract.open_pdf(f.data, f.name) as pdf:
            for page_no, page in enumerate(pdf.pages, 1):
                on_page = [b for b in boxes if b.page == page_no]
                if not on_page and len(pages) >= MAX_PAGES:
                    skipped += 1
                    continue
                entry: dict[str, Any] = {"source": f.name, "page": page_no, "page_count": part.page_count}
                area = tuple(page.bbox)
                try:
                    image = page.to_image(resolution=RESOLUTION)
                    area = tuple(image.bbox)
                    entry["image"] = _data_uri(image.original)
                except Exception as e:  # a page the renderer cannot draw still gets a placeholder
                    entry["image"], entry["error"] = None, f"{e.__class__.__name__}: {e}"
                entry["width"], entry["height"] = round(area[2] - area[0], 2), round(area[3] - area[1], 2)
                entry["table"] = _crop(page, on_page, area) if on_page else None
                index[page_no], origin[page_no] = len(pages), (area[0], area[1])
                pages.append(entry)
        lines.extend(_place(b, index, origin) for b in part.rows)
        headers.extend(_place(b, index, origin) for b in part.headers)
        if part.totals_row is not None:  # the last subtotal row is the one that is checked
            totals = _place(part.totals_row, index, origin)
    fields = list(result.customer.table.columns)
    labels = next((p.labels for p in result.parts if p.labels), {})
    return {
        "pages": pages,
        "skipped_pages": skipped,
        "fields": [{"name": f, "label": labels.get(f) or f} for f in fields],
        "items": [{f: item.get(f, "") for f in fields} for item in result.items],
        "lines": lines,
        "headers": headers,
        "totals": totals,
        "totals_values": next((p.totals for p in reversed(result.parts) if p.totals is not None), None),
        "grid": grid(result),
    }
