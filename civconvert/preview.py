"""Side-by-side review before export.

Left:  the invoice pages as images, plus where the table header, every line item and the
       subtotal row are on them, so the page can show just the table and point at a line.
Right: the template columns, read back from the Excel file that is exported (values, header
       text, column widths, fonts) – the preview shows what the file contains.

All positions are PDF points measured from the top-left corner of the page image. They are
plain JSON (geometry()), so a saved document can be previewed later without converting again.
"""

from __future__ import annotations

import base64
import csv
import io
from typing import Any

from openpyxl import load_workbook
from PIL import features

from . import extract
from .extract import RowBox
from .pipeline import InputFile, Result
from .template import cell_style, column_width_px, shown_value

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


def _box(b: RowBox) -> dict[str, Any]:
    return {
        "page": b.page,
        "bbox": [round(v, 2) for v in b.bbox],
        "cells": {f: [round(v, 2) for v in c] for f, c in b.cells.items()},
        "joined": b.joined,
    }


def geometry(result: Result) -> list[dict[str, Any]]:
    """Where the table rows are, per PDF part, as JSON (stored with a document)."""
    return [
        {
            "name": p.name,
            "page_count": p.page_count,
            "labels": p.labels,
            "totals": p.totals,
            "rows": [_box(b) for b in p.rows],
            "headers": [_box(b) for b in p.headers],
            "totals_row": _box(p.totals_row) if p.totals_row else None,
        }
        for p in result.parts
    ]


def _crop(page, boxes: list[dict[str, Any]], area) -> list[float]:
    """The table on this page (header, line items, subtotal) plus a margin – without cutting a text line in half."""
    x0 = min(b["bbox"][0] for b in boxes) - PAD
    x1 = max(b["bbox"][2] for b in boxes) + PAD
    top = min(b["bbox"][1] for b in boxes) - PAD
    bottom = max(b["bbox"][3] for b in boxes) + PAD
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


def _place(box: dict[str, Any], index: dict[int, int], origin: dict[int, tuple[float, float]]) -> dict[str, Any]:
    if box["page"] not in index:
        return {"page": None, "bbox": None, "cells": {}, "joined": box.get("joined", 0)}
    ox, oy = origin[box["page"]]

    def move(b: list[float]) -> list[float]:
        return [round(b[0] - ox, 2), round(b[1] - oy, 2), round(b[2] - ox, 2), round(b[3] - oy, 2)]

    return {"page": index[box["page"]], "bbox": move(box["bbox"]), "cells": {f: move(c) for f, c in box["cells"].items()}, "joined": box.get("joined", 0)}


def pages(files: list[InputFile], geo: list[dict[str, Any]]) -> dict[str, Any]:
    """The page images, and where the table header, each line item and the subtotal row are on them."""
    out: list[dict[str, Any]] = []
    lines: list[dict[str, Any]] = []
    headers: list[dict[str, Any]] = []
    totals: dict[str, Any] | None = None
    skipped = 0
    for f, part in zip(files, geo, strict=True):
        boxes = [*part["headers"], *part["rows"], *([part["totals_row"]] if part["totals_row"] else [])]
        index: dict[int, int] = {}  # page number -> position in `out`
        origin: dict[int, tuple[float, float]] = {}
        with extract.open_pdf(f.data, f.name) as pdf:
            for page_no, page in enumerate(pdf.pages, 1):
                on_page = [b for b in boxes if b["page"] == page_no]
                if not on_page and len(out) >= MAX_PAGES:
                    skipped += 1
                    continue
                entry: dict[str, Any] = {"source": f.name, "page": page_no, "page_count": len(pdf.pages)}
                area = tuple(page.bbox)
                try:
                    image = page.to_image(resolution=RESOLUTION)
                    area = tuple(image.bbox)
                    entry["image"] = _data_uri(image.original)
                except Exception as e:  # a page the renderer cannot draw still gets a placeholder
                    entry["image"], entry["error"] = None, f"{e.__class__.__name__}: {e}"
                entry["width"], entry["height"] = round(area[2] - area[0], 2), round(area[3] - area[1], 2)
                entry["table"] = _crop(page, on_page, area) if on_page else None
                index[page_no], origin[page_no] = len(out), (area[0], area[1])
                out.append(entry)
        lines.extend(_place(b, index, origin) for b in part["rows"])
        headers.extend(_place(b, index, origin) for b in part["headers"])
        if part["totals_row"] is not None:  # the last subtotal row is the one that is checked
            totals = _place(part["totals_row"], index, origin)
    return {
        "pages": out,
        "skipped_pages": skipped,
        "lines": lines,
        "headers": headers,
        "totals": totals,
        "totals_values": next((p["totals"] for p in reversed(geo) if p["totals"] is not None), None),
    }


def grid(result) -> dict[str, Any]:
    """The filled template columns as Excel shows them, read back from the exported workbook.

    `result` is a conversion Result or a document's sheet (sheet.SheetExport): anything with
    template, columns, rows, xlsx_bytes() and csv_bytes().
    """
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
            shown = shown_value(cell.value)
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
            "header": shown_value(ws.cell(row=t.header_row, column=c.index + 1).value),
            "field": c.spec.field,
            "width": column_width_px(ws, c.index + 1),
            "head_style": cell_style(ws.cell(row=t.header_row, column=c.index + 1)),
            "cell_style": cell_style(ws.cell(row=t.header_row + 1, column=c.index + 1)),
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
    geo = geometry(result)
    fields = list(result.customer.table.columns)
    labels = next((p["labels"] for p in geo if p["labels"]), {})
    return {
        **pages(files, geo),
        "fields": [{"name": f, "label": labels.get(f) or f} for f in fields],
        "items": [{f: item.get(f, "") for f in fields} for item in result.items],
        "grid": grid(result),
    }
