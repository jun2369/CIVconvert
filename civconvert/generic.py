"""The general reader: invoices of any supplier, PDF or Excel, without a customer profile.

Instead of one customer's fixed header texts, the item table is found by the usual ways invoices name
their columns (config/generic.yaml): Model / Part No / Item# -> product code, Description, HS / HTS /
Tariff, Qty, Unit Price, Amount ... When a file has several tables (packing list, contract, invoice),
the one that looks most like the invoice's line items wins: HS codes and prices count more, and a
"Commercial Invoice" title helps. The rows are then read by position:

  - PDF tables with ruling lines: by their cells, like a customer profile (extract.py)
  - PDF tables without lines: every word goes to the column whose header it sits under (text starts
    under its header, numbers end under it); a line without an item number, HS code or numbers is
    wrapped text of the line above; the table may go on over the next pages, with or without
    repeating its header; the last "Total" line is the one that is checked
  - Excel: by the header row's cells; the sheet is drawn for the preview (see preview.py)
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field, replace
from typing import Any

from openpyxl.utils import get_column_letter

from . import extract, sources
from .extract import Bbox, PdfExtract, RowBox
from .profiles import CustomerProfile, GenericSpec, TableSpec

NUMBER_FIELDS = ("qty", "amount", "unit_price")
_NUMBER = re.compile(r"^\s*(?:[A-Za-z]{0,3}\s?[$€£¥]|[$€£¥])?\s*[-+(]?\d[\d,]*(?:\.\d+)?\)?\s*(?:[A-Za-z]{1,5}|%)?\s*$")
_ZERO_WIDTH = re.compile(r"[​-‏⁠﻿­]")
_LEADING = re.compile(r"^[\W_]+")


def norm(text: str) -> str:
    """A header or cell text for matching: half-width, lower case, single spaces, no trailing colon."""
    t = _ZERO_WIDTH.sub("", unicodedata.normalize("NFKC", text or "")).lower()
    return re.sub(r"\s+", " ", t).strip().rstrip(":").strip()


def is_number(text: str | None) -> bool:
    return bool(text) and bool(_NUMBER.match(text))


# ───────────────────────────── which cells are the header ─────────────────────────────


@dataclass
class Match:
    fields: dict[str, int]  # field -> position of its header label
    score: int


def match_labels(texts: list[str], spec: GenericSpec) -> Match | None:
    """The fields of an item-table header row, or None if these labels are not one."""
    norms = [norm(t) for t in texts]
    fields: dict[str, int] = {}
    taken: set[int] = set()
    for f, patterns in spec.synonyms.items():  # fields in the order of generic.yaml: HS code first
        ex = spec.exclude.get(f)
        best = None
        for rx in patterns:  # best wording first
            best = next((i for i, n in enumerate(norms) if n and i not in taken and rx.search(n) and not (ex and ex.search(n))), None)
            if best is not None:
                break
        if best is not None:
            fields[f] = best
            taken.add(best)
    if not all(f in fields for f in spec.needs) or not all(any(f in fields for f in group) for group in spec.needs_one_of):
        return None
    return Match(fields, sum(spec.weights.get(f, 0) for f in fields))


def _context(text: str, spec: GenericSpec) -> int:
    """Title of the page / sheet: an invoice counts more than a packing list or a contract."""
    if spec.invoice_title.search(text):
        return 3
    return -4 if spec.other_title.search(text) else 0


# ───────────────────────────── PDF ─────────────────────────────


@dataclass
class Label:
    text: str
    x0: float
    x1: float
    top: float
    bottom: float
    field: str | None = None

    @property
    def bbox(self) -> Bbox:
        return (self.x0, self.top, self.x1, self.bottom)


@dataclass
class Header:
    score: int
    kind: str  # ruled | words | excel
    page: int  # PDF page (1-based) / sheet position
    fields: dict[str, int]
    labels: list[Label] = field(default_factory=list)  # words: the header labels, left to right
    cells: list[str] = field(default_factory=list)  # ruled / excel: the header row's cells
    top: float = 0.0
    bottom: float = 0.0
    row: int = 0  # excel: row index of the header


def _lines(words: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Words grouped into text lines, top to bottom, each left to right."""
    lines: list[list[dict[str, Any]]] = []
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        h = w["bottom"] - w["top"]
        if lines and abs(w["top"] - lines[-1][0]["top"]) <= max(2.0, 0.35 * h):
            lines[-1].append(w)
        else:
            lines.append([w])
    for line in lines:
        line.sort(key=lambda w: w["x0"])
    return lines


def _labels(line: list[dict[str, Any]]) -> list[Label]:
    """The words of a line grouped into labels: words closer than about half a letter height belong together."""
    labels: list[Label] = []
    for w in line:
        h = w["bottom"] - w["top"]
        if labels and w["x0"] - labels[-1].x1 <= 0.6 * h:
            last = labels[-1]
            last.text, last.x1 = f"{last.text} {w['text']}", max(last.x1, w["x1"])
            last.top, last.bottom = min(last.top, w["top"]), max(last.bottom, w["bottom"])
        else:
            labels.append(Label(w["text"], w["x0"], w["x1"], w["top"], w["bottom"]))
    return labels


def _merge_below(labels: list[Label], below: list[Label]) -> list[Label] | None:
    """A header written over two lines ("Unit Price" / "(USD)", "United States" / "HS Code")."""
    out = [replace(lb) for lb in labels]
    for b in below:
        hits = [lb for lb in out if min(b.x1, lb.x1) - max(b.x0, lb.x0) > 0]
        if not hits:
            return None
        lb = hits[0]
        lb.text, lb.x0, lb.x1, lb.bottom = f"{lb.text} {b.text}", min(lb.x0, b.x0), max(lb.x1, b.x1), max(lb.bottom, b.bottom)
    return out


def _word_headers(page_no: int, lines: list[list[dict[str, Any]]], spec: GenericSpec, bonus: int) -> list[Header]:
    found = []
    for i, line in enumerate(lines):
        labels = _labels(line)
        if len(labels) < 3:
            continue
        options = [labels]
        if i + 1 < len(lines):
            below = _labels(lines[i + 1])
            gap = min(w["top"] for w in lines[i + 1]) - max(w["bottom"] for w in line)
            merged = _merge_below(labels, below) if gap < 1.2 * (line[0]["bottom"] - line[0]["top"]) else None
            if merged:
                options.append(merged)
        for opt in options:
            m = match_labels([lb.text for lb in opt], spec)
            if m is None:
                continue
            for f, idx in m.fields.items():
                opt[idx].field = f
            found.append(Header(m.score + bonus, "words", page_no, m.fields, labels=opt,
                                top=min(lb.top for lb in opt), bottom=max(lb.bottom for lb in opt)))
    return found


def _ruled_headers(page_no: int, page, spec: GenericSpec, bonus: int) -> list[Header]:
    found = []
    for table in page.find_tables():
        for raw in table.extract()[:8]:  # the header is near the top of its table
            cells = [extract.clean_cell(c) for c in raw]
            if sum(1 for c in cells if c) < 3:
                continue
            m = match_labels(cells, spec)
            if m is not None:
                found.append(Header(m.score + bonus, "ruled", page_no, m.fields, cells=cells))
    return found


def _best(headers: list[Header]) -> Header | None:
    # higher score first; on a tie a ruled table (exact cells), then the earlier page
    return max(headers, key=lambda h: (h.score, h.kind == "ruled", -h.page), default=None)


def find_pdf_header(data: bytes, name: str, spec: GenericSpec) -> Header | None:
    """The item-table header of a PDF: the best candidate of all pages (ruled tables and text lines)."""
    headers: list[Header] = []
    with extract.open_pdf(data, name) as pdf:
        for page_no, page in enumerate(pdf.pages, 1):
            text = page.extract_text() or ""
            if not text.strip():
                continue
            bonus = _context(text, spec)
            headers += _ruled_headers(page_no, page, spec, bonus)
            headers += _word_headers(page_no, _lines(page.extract_words()), spec, bonus)
    return _best(headers)


def _ruled_spec(header: Header, spec: GenericSpec) -> TableSpec:
    """A customer-profile style table description for a ruled table: its own header texts."""
    columns = {f: re.compile("^" + re.escape(header.cells[i]) + "$", re.IGNORECASE) for f, i in header.fields.items()}
    anchor = next((f for f in ("model", "hs_code", "description") if f in columns), next(iter(columns)))
    return TableSpec(
        strategy="lines",
        columns=columns,
        required=list(columns),
        stop_row=spec.total_row,
        totals_row=spec.total_row,
        continuation_anchor=anchor,
        continuation_fields=[f for f in ("description",) if f in columns and f != anchor],
        pdfplumber={},
    )


def read_pdf(data: bytes, name: str, spec: GenericSpec, carry: Header | None = None) -> tuple[PdfExtract, Header | None]:
    """Line items of a PDF read with the general reader. `carry`: the header of an earlier part of the same invoice."""
    header = find_pdf_header(data, name, spec) or carry
    if header is None:
        result = PdfExtract(name=name)
        with extract.open_pdf(data, name) as pdf:
            result.page_count = len(pdf.pages)
            result.text = "\n".join(page.extract_text() or "" for page in pdf.pages)
        return result, None
    if header.kind == "ruled":
        result, _ = extract.extract(data, name, _ruled_spec(header, spec))
        return result, header
    return _read_words(data, name, header, spec, carried=header is carry), header


def _union(boxes: list[Bbox]) -> Bbox:
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))


def _column_of(w: dict[str, Any], cols: list[Label]) -> int:
    """The column a word belongs to: the header it overlaps; else text goes to the column that starts to
    its left (text is written from the left edge of its column), a number to the nearer header."""
    best, overlap = None, 0.0
    for i, c in enumerate(cols):
        ov = min(w["x1"], c.x1) - max(w["x0"], c.x0)
        if ov > overlap:
            best, overlap = i, ov
    if best is not None:
        return best
    left = max((i for i, c in enumerate(cols) if c.x0 <= w["x0"]), default=None)
    right = next((i for i, c in enumerate(cols) if c.x0 > w["x0"]), None)
    if left is None:
        return right if right is not None else 0
    if right is None or not is_number(w["text"]):
        return left
    return right if cols[right].x0 - w["x1"] < w["x0"] - cols[left].x1 else left


@dataclass
class _Line:
    words: list[dict[str, Any]]
    by_col: dict[int, list[dict[str, Any]]]
    values: dict[str, str]  # field -> text

    @property
    def top(self) -> float:
        return min(w["top"] for w in self.words)

    @property
    def bottom(self) -> float:
        return max(w["bottom"] for w in self.words)

    @property
    def bbox(self) -> Bbox:
        return _union([(w["x0"], w["top"], w["x1"], w["bottom"]) for w in self.words])

    def cell_boxes(self, cols: list[Label]) -> dict[str, Bbox]:
        return {cols[i].field: _union([(w["x0"], w["top"], w["x1"], w["bottom"]) for w in ws])
                for i, ws in self.by_col.items() if cols[i].field and ws}


def _read_line(words: list[dict[str, Any]], cols: list[Label]) -> _Line:
    by_col: dict[int, list[dict[str, Any]]] = {}
    for w in words:
        by_col.setdefault(_column_of(w, cols), []).append(w)
    values = {cols[i].field: " ".join(w["text"] for w in ws) for i, ws in by_col.items() if cols[i].field}
    return _Line(words, by_col, values)


def _is_item(values: dict[str, str], fields: dict[str, int]) -> bool:
    """A line that starts a new item: it has an HS code, or numbers in the quantity / price / amount
    columns next to some text (without such columns: an item number)."""
    if re.search(r"\d{4}", values.get("hs_code", "")):
        return True
    if not (values.get("model") or values.get("description")):
        return False
    if any(f in fields for f in NUMBER_FIELDS):
        return any(is_number(values.get(f)) for f in NUMBER_FIELDS)
    return bool(values.get("model"))


def _read_words(data: bytes, name: str, header: Header, spec: GenericSpec, carried: bool = False) -> PdfExtract:
    cols = sorted(header.labels, key=lambda lb: lb.x0)
    result = PdfExtract(name=name, header_found=True)
    result.labels = {lb.field: lb.text for lb in cols if lb.field}
    height = max(header.bottom - header.top, 6.0)
    max_gap = 3.2 * min(height, 14.0)  # more space than this between two lines: the table has ended
    texts: list[str] = []
    with extract.open_pdf(data, name) as pdf:
        result.page_count = len(pdf.pages)
        pages = [(n, p, _lines(p.extract_words())) for n, p in enumerate(pdf.pages, 1)]
        texts = [p.extract_text() or "" for _, p, _ in pages]
    if not carried:
        result.headers.append(RowBox(header.page, _union([lb.bbox for lb in cols]), {lb.field: lb.bbox for lb in cols if lb.field}))
    # which columns hold text (wrapped text of a line may continue in them): judged from the item lines
    text_cols: set[int] = set()
    first_line = norm(" ".join(w["text"] for w in pages[header.page - 1][2][0])) if pages[header.page - 1][2] else ""
    start_page = 1 if carried else header.page
    for page_no, _page, lines in pages[start_page - 1:]:
        if page_no == header.page and not carried:
            lines = [ln for ln in lines if min(w["top"] for w in ln) > header.bottom - 0.5]
        elif lines and norm(" ".join(w["text"] for w in lines[0])) == first_line and not carried:
            # the same page layout as the header's page: the table goes on where its header was
            lines = [ln for ln in lines if min(w["top"] for w in ln) >= header.top - 1.5 * height]
        before = len(result.items)
        pending: list[_Line] = []
        last_bottom: float | None = None
        for words in lines:
            line = _read_line(words, cols)
            if _repeats_header(words, cols):
                last_bottom = line.bottom
                continue
            lead = _LEADING.sub("", norm(" ".join(w["text"] for w in words[:3])))
            if spec.total_row.search(lead) and not line.values.get("model") and not re.search(r"\d{4}", line.values.get("hs_code", "")):
                if result.items:
                    result.totals = {f: v for f, v in line.values.items()}
                    result.totals_row = RowBox(page_no, line.bbox, line.cell_boxes(cols))
                    break  # the rest of this page is not the table; it may go on on the next page
                continue
            if _is_item(line.values, header.fields):
                if pending and len(result.items) == before and result.items:
                    _attach_pending(result, pending, line, cols, text_cols, max_gap)
                pending = []
                item = {f: v for f, v in line.values.items()}
                item["_source"] = f"{name} p.{page_no}"
                result.items.append(item)
                result.rows.append(RowBox(page_no, line.bbox, line.cell_boxes(cols)))
                text_cols |= {i for i, ws in line.by_col.items() if any(re.search(r"[A-Za-z]{2}", w["text"]) for w in ws)}
                last_bottom = line.bottom
                continue
            if not result.items:
                continue  # under the header, before the first item (units such as "(USD)")
            if len(result.items) == before:
                pending.append(line)  # top of a following page: maybe the end of the last item
                continue
            if last_bottom is not None and line.top - last_bottom > max_gap:
                break  # far below the last item: the table has ended on this page
            if all(i in text_cols for i in line.by_col):
                _join(result, line, cols)
                last_bottom = line.bottom
        if page_no > header.page and len(result.items) == before:
            break  # the table does not go on to this page
    result.text = "\n".join(texts)
    return result


def _repeats_header(words: list[dict[str, Any]], cols: list[Label]) -> bool:
    texts = {norm(lb.text) for lb in _labels(words)}
    head = {norm(lb.text) for lb in cols}
    return len(texts & head) >= max(3, int(0.6 * len(head)))


def _join(result: PdfExtract, line: _Line, cols: list[Label]) -> None:
    """Wrapped text: added to the item above."""
    item, box = result.items[-1], result.rows[-1]
    for f, v in line.values.items():
        if v:
            item[f] = f"{item.get(f, '')} {v}".strip()
    cells = dict(box.cells)
    for f, b in line.cell_boxes(cols).items():
        cells[f] = _union([cells[f], b]) if f in cells else b
    page_box = _union([box.bbox, line.bbox]) if box.page else line.bbox
    result.rows[-1] = RowBox(box.page, page_box, cells, box.joined)


def _attach_pending(result: PdfExtract, pending: list[_Line], first: _Line, cols: list[Label], text_cols: set[int], max_gap: float) -> None:
    """Lines at the top of a page, just above its first item, that continue the last item of the page before."""
    keep: list[_Line] = []
    below = first.top
    for line in reversed(pending):
        if below - line.bottom > max_gap or not all(i in text_cols for i in line.by_col):
            break
        keep.insert(0, line)
        below = line.top
    for line in keep:
        item = result.items[-1]
        for f, v in line.values.items():
            if v:
                item[f] = f"{item.get(f, '')} {v}".strip()


# ───────────────────────────── Excel ─────────────────────────────

MARGIN, LETTERS_H, ROWNO_W, HEAD_H, ROW_H, CHAR_W, PAD = 12.0, 14.0, 28.0, 20.0, 16.0, 5.0, 5.0


def _hs_text(value: Any, text: str) -> str:
    """An HS code typed as a number loses its leading zero in Excel (0101.21 -> 101210000): put it back."""
    if isinstance(value, (int, float)) and not isinstance(value, bool) and text.isdigit() and len(text) in (5, 7, 9):
        return text.zfill(len(text) + 1)
    return text


def read_excel(data: bytes, name: str, file_kind: str, spec: GenericSpec, profile: CustomerProfile | None = None) -> PdfExtract:
    """Line items of an .xlsx / .xls invoice. With `profile`, its own header texts are tried first."""
    grids = sources.read_workbook(data, name, file_kind)
    result = PdfExtract(name=name, page_count=1, text="\n\n".join(g.text() for g in grids))
    best: Header | None = None
    for gi, grid in enumerate(grids):
        bonus = _context(grid.text()[:4000], spec)
        for ri, row in enumerate(grid.cells[:300]):
            texts = [sources.cell_text(v) for v in row]
            if sum(1 for t in texts if t) < 3:
                continue
            if profile is not None:
                fields = extract.match_header(texts, profile.table)
                if fields is not None:
                    cand = Header(1000 + bonus, "excel", gi, fields, cells=texts, row=ri)
                    best = cand if best is None or cand.score > best.score else best
                    continue
            m = match_labels(texts, spec)
            if m is not None:
                cand = Header(m.score + bonus, "excel", gi, m.fields, cells=texts, row=ri)
                if best is None or cand.score > best.score:
                    best = cand
    if best is None:
        return result
    grid = grids[best.page]
    total_rx = profile.table.totals_row if profile is not None and profile.table.totals_row is not None else spec.total_row
    stop_rx = profile.table.stop_row if profile is not None and profile.table.stop_row is not None else spec.total_row
    fields = best.fields
    result.header_found = True
    result.labels = {f: best.cells[i] for f, i in fields.items()}
    shown = [c for c, t in enumerate(best.cells) if t]  # the columns drawn in the preview
    drawn: list[dict[str, Any]] = [{"kind": "head", "excel_row": best.row + 1, "cells": [best.cells[c] for c in shown]}]
    item_rows: list[int] = []  # drawn-row position of each item
    empty = 0
    for ri in range(best.row + 1, len(grid.cells)):
        row = grid.cells[ri]
        texts = [sources.cell_text(v) for v in row]
        if not any(texts):
            empty += 1
            if empty >= 5 and result.items:
                break
            continue
        empty = 0
        values = {f: texts[c] if c < len(texts) else "" for f, c in fields.items()}
        if "hs_code" in fields and fields["hs_code"] < len(row):
            values["hs_code"] = _hs_text(row[fields["hs_code"]], values["hs_code"])
        first = _LEADING.sub("", norm(next(t for t in texts if t)))
        cells = [texts[c] if c < len(texts) else "" for c in shown]
        if (stop_rx.search(first) or total_rx.search(first)) and not values.get("model") and not re.search(r"\d{4}", values.get("hs_code", "")):
            if total_rx.search(first):
                result.totals = values
                drawn.append({"kind": "total", "excel_row": ri + 1, "cells": cells})
            break
        if _is_item(values, fields):
            values["_source"] = f"{name} / {grid.name} row {ri + 1}"
            result.items.append(values)
            drawn.append({"kind": "item", "excel_row": ri + 1, "cells": cells})
            item_rows.append(len(drawn) - 1)
        elif result.items and values.get("description") and not any(values.get(f) for f in ("model", "hs_code", *NUMBER_FIELDS)):
            prev = result.items[-1]
            prev["description"] = f"{prev.get('description', '')} {values['description']}".strip()
            drawn.append({"kind": "more", "excel_row": ri + 1, "cells": cells})
        elif match_labels(texts, spec) is not None:
            break  # another table starts
    layout, boxes = _sheet_layout(grid.name, shown, drawn, {f: shown.index(c) for f, c in fields.items() if c in shown})
    result.layout = layout
    result.headers = [boxes[0]]
    # an item's box covers its wrapped rows below it
    for n, pos in enumerate(item_rows):
        end = item_rows[n + 1] if n + 1 < len(item_rows) else len(drawn)
        while end > pos + 1 and drawn[end - 1]["kind"] == "total":
            end -= 1
        rows = [boxes[p] for p in range(pos, end)]
        result.rows.append(RowBox(1, _union([b.bbox for b in rows]), rows[0].cells))
    if drawn[-1]["kind"] == "total":
        result.totals_row = boxes[-1]
    return result


def _sheet_layout(sheet: str, shown: list[int], drawn: list[dict[str, Any]], field_col: dict[str, int]) -> tuple[dict[str, Any], list[RowBox]]:
    """Where the drawn rows and columns of a sheet go on the preview picture (points)."""
    widths = []
    for k, _c in enumerate(shown):
        longest = max(len(r["cells"][k]) for r in drawn)
        widths.append(min(max(longest * CHAR_W + 2 * PAD, 34.0), 300.0))
    xs = [MARGIN + ROWNO_W]
    for w in widths:
        xs.append(xs[-1] + w)
    boxes, y = [], MARGIN + LETTERS_H
    for r in drawn:
        h = HEAD_H if r["kind"] == "head" else ROW_H
        r["y"], r["h"] = y, h
        boxes.append(RowBox(1, (xs[0], y, xs[-1], y + h), {f: (xs[k], y, xs[k + 1], y + h) for f, k in field_col.items()}))
        y += h
    layout = {
        "sheet": sheet,
        "width": round(xs[-1] + MARGIN, 2),
        "height": round(y + MARGIN, 2),
        "columns": [{"letter": get_column_letter(c + 1), "x": round(xs[k], 2), "width": round(widths[k], 2)} for k, c in enumerate(shown)],
        "rows": drawn,
    }
    return layout, boxes


# ───────────────────────────── invoice fields & checks ─────────────────────────────


def document_fields(text: str, spec: GenericSpec) -> dict[str, str]:
    """Invoice number and date found anywhere in the text (the first match of each)."""
    flat = unicodedata.normalize("NFKC", text)
    out = {}
    for key, patterns in spec.document_fields.items():
        for rx in patterns:
            m = rx.search(flat)
            if m:
                out[key] = m.group(1).strip()
                break
    return out


def effective(profile: CustomerProfile, found: set[str], has_totals: bool) -> CustomerProfile:
    """The general profile for one invoice: only the columns it has, and only the checks it allows
    (no qty x price = amount without a price column, no subtotal check without a total line)."""
    c = profile.checks
    checks = {
        **c,
        "totals": [f for f in c["totals"] if f in found] if has_totals else [],
        "line_math": c["line_math"] if c["line_math"] and all(f in found for f in c["line_math"]) else [],
        "consistent": {k: [d for d in v if d in found] for k, v in c["consistent"].items() if k in found},
    }
    table = replace(profile.table, columns={f: p for f, p in profile.table.columns.items() if f in found})
    return replace(profile, table=table, checks=checks)
