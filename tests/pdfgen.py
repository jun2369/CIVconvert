"""A tiny PDF writer for tests: an ISTONE-style invoice table with ruling lines, split over pages, and an
invoice of another supplier without ruling lines (a packing list first, wrapped descriptions …)."""

from __future__ import annotations

HEADER = ["Model", "Description", "QTY(pcs)", "Unit Price(USD)", "Total (USD)", "United States HS Code"]
WIDTHS = [110, 150, 50, 70, 70, 90]
ROW = 18  # points


def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _text(x: float, y: float, text: str, size: int = 8) -> str:
    return f"BT /F1 {size} Tf {x:.1f} {y:.1f} Td ({_esc(text)}) Tj ET"


def _table(rows: list[list[str]], top: float) -> list[str]:
    ops = []
    for r, row in enumerate(rows):
        y = top - (r + 1) * ROW
        x = 28.0
        for w, cell in zip(WIDTHS, row):
            ops.append(f"{x:.1f} {y:.1f} {w} {ROW} re S")
            if cell:
                ops.append(_text(x + 3, y + 6, cell))
            x += w
    return ops


def invoice_pdf(lines: list[list[str]], subtotal: list[str], per_page: int = 14, seller: str = "ISTONE INNOVATION LIMITED",
                header: list[str] | None = None) -> bytes:
    """Page 1 has the title and the first `per_page` lines; later pages repeat the header row."""
    chunks = [lines[i : i + per_page] for i in range(0, len(lines), per_page)] or [[]]
    streams = []
    for p, chunk in enumerate(chunks):
        ops = ["0.5 w"]
        top = 800.0
        if p == 0:
            ops += [_text(200, 800, seller, 14), _text(230, 780, "Commercial Invoice", 12),
                    _text(28, 750, "Invoice No. : T-0001"), _text(330, 750, "Issued Date :2026-10-01")]
            top = 730.0
        rows = [header or HEADER, *chunk] + ([subtotal] if p == len(chunks) - 1 else [])
        ops += _table(rows, top)
        streams.append("\n".join(ops).encode("latin-1"))
    return _document(streams)


def _document(streams: list[bytes], width: int = 595, height: int = 842) -> bytes:
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>"]
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(len(streams)))
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(streams)} >>".encode())
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    for i, s in enumerate(streams):
        objs.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] /Resources << /Font << /F1 3 0 R >> >> /Contents {5 + 2 * i} 0 R >>".encode()
        )
        objs.append(b"<< /Length %d >>\nstream\n" % len(s) + s + b"\nendstream")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for n, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % n + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


def mixed_pdf(pages: list[bytes | tuple[bytes, int, int]], width: int = 595, height: int = 842) -> bytes:
    """Pages of text (content streams) and pictures: (JPEG data, pixel width, pixel height) – a scan filling the page."""
    objs: list[bytes] = [b"<< /Type /Catalog /Pages 2 0 R >>", b"", b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"]
    kids = []
    for page in pages:
        n = len(objs) + 1
        kids.append(f"{n} 0 R")
        if isinstance(page, bytes):
            objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] /Resources << /Font << /F1 3 0 R >> >> /Contents {n + 1} 0 R >>".encode())
            objs.append(b"<< /Length %d >>\nstream\n" % len(page) + page + b"\nendstream")
        else:
            jpeg, w, h = page
            draw = f"q {width} 0 0 {height} 0 0 cm /Im0 Do Q".encode()
            objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] /Resources << /XObject << /Im0 {n + 2} 0 R >> >> /Contents {n + 1} 0 R >>".encode())
            objs.append(b"<< /Length %d >>\nstream\n" % len(draw) + draw + b"\nendstream")
            objs.append(f"<< /Type /XObject /Subtype /Image /Width {w} /Height {h} /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length {len(jpeg)} >>\nstream\n".encode() + jpeg + b"\nendstream")
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>".encode()
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for n, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % n + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


def problem_invoice(seller: str = "ISTONE INNOVATION LIMITED") -> bytes:
    """26 lines over 2 pages with known problems:
    line 3 / line 20: same model, different HS code · line 5: HS code format
    line 9: qty x price != amount · line 12: no description
    """
    lines = []
    for n in range(1, 27):
        model, desc, qty, price, hs = f"CINSM{n:03d}", f"Test Product {n}", n, 2, "9006910001"
        if n in (3, 20):
            model = "CINSDUP(X1)"
            hs = "9006910001" if n == 3 else "8518108030"
        if n == 5:
            hs = "9006.91"
        if n == 12:
            desc = ""
        amount = qty * price + (5 if n == 9 else 0)
        lines.append([model, desc, str(qty), f"US${price}.00", f"US${amount:,}.00", hs])
    qty_total = sum(int(r[2]) for r in lines)
    amount_total = sum(int(r[4][3:-3].replace(",", "")) for r in lines)
    return invoice_pdf(lines, ["Subtotal :", "", str(qty_total), "", f"US${amount_total:,}.00", ""], seller=seller)


def packing_list_pdf(seller: str = "ISTONE INNOVATION LIMITED") -> bytes:
    """The packing list that comes with problem_invoice(): it names the invoice, but has no prices."""
    ops = ["0.5 w", _text(200, 800, seller, 14), _text(250, 780, "PACKING LIST", 12),
           _text(28, 750, "Invoice No. : T-0001"), _text(330, 750, "Date :2026-10-01")]
    ops += _table([["Model", "Description", "Cartons", "Qty", "N.W. (kg)", "G.W. (kg)"],
                   *[[f"CINSM{n:03d}", f"Test Product {n}", "1", str(n), "1.20", "1.50"] for n in range(1, 6)]], 730.0)
    return _document(["\n".join(ops).encode("latin-1")])


# ── a supplier invoice with a ruled table: cells centred over two lines, a totals row without "Total" ──
GRID_COLUMNS = [("No", 30), ("Item code", 62), ("Description", 140), ("Q'ty", 70), ("Unit", 42), ("Unite Price|(USD)", 64), ("Amount|(USD)", 70)]


def _centred(x: float, width: float, y_mid: float, text: str, size: int = 8) -> list[str]:
    """Text centred in a cell; "a|b" = two lines."""
    parts = text.split("|")
    out = []
    for i, part in enumerate(parts):
        y = y_mid - 3 + (len(parts) - 1) * 5 - i * 10
        out.append(_text(x + (width - 0.5 * size * len(part)) / 2, y, part, size))
    return out


def grid_invoice_pdf(rows: list[list[str]] | None = None, totals: list[str] | None = None) -> bytes:
    """Like a Chinese supplier's Commercial Invoice (made in Excel): no HS code column, "Unite Price" / "(USD)"
    over two lines, a product name over two lines next to one-line cells, and a totals row of only the sums –
    the total quantity under "Unit"."""
    rows = rows or [["1", "Wire Mesh|Cage", "1200*1000*1200mm", "358", "(PCS)", "60", "21480"]]
    totals = totals if totals is not None else ["", "", "", "", "358", "", "21480"]
    ops = ["0.5 w", _text(150, 800, "Shenzhen Ali Intelligent Equipment Technology Co., Ltd.", 11),
           _text(230, 760, "COMMERCIAL INVOICE", 13), _text(60, 730, "Name: Speed Xpress Inc"), _text(400, 730, "Invoice No: SPX-443"),
           _text(400, 714, "Invoice Date.: Oct 8th,2026"), _text(60, 698, "Freight Terms: FOB Yantian Port, Shenzhen")]
    top = 680.0
    for cells, height in ([[c for c, _ in GRID_COLUMNS], 24.0], *[[r, 36.0] for r in rows], [totals, 16.0]):
        x = 60.0
        for (_, w), cell in zip(GRID_COLUMNS, cells):
            ops.append(f"{x:.1f} {top - height:.1f} {w} {height} re S")
            if cell:
                ops += _centred(x, w, top - height / 2, cell)
            x += w
        top -= height
    ops.append(_text(60, top - 14, "SAY US DOLLARS TWENTY-ONE THOUSAND FOUR HUNDRED AND EIGHTY ONLY."))
    return _document(["\n".join(ops).encode("latin-1")])


FUYAO_COLUMNS = [("Description", 58), ("Hs Code", 130), ("Specificati|on", 52), ("Quantit|y", 38), ("Unit", 24), ("Delivery Note", 56),
                 ("PO", 54), ("Unit", 66), ("Amount", 74)]


def offset_invoice_pdf(lines: list[list[str]], blank_rows: int = 6) -> bytes:
    """Like Fuyao Glass's invoice: the headers do not say what is under them – the part numbers under
    "Description", the description with the HS code in it under "Hs Code" – header words broken over two
    lines ("Quantit" / "y"), the unit price under a second "Unit", empty ruled rows, then a "total" row with
    freight / FOB / insurance in the other cells."""
    qty = sum(int(r[3]) for r in lines)
    amount = sum(float(r[8].split()[-1]) for r in lines)
    total = ["total", "", "", str(qty), "PCS", "FREIGHT4905.98", "FOB42821.72", "INSURANCE80", f"USD{amount:.2f}"]
    ops = ["0.5 w", _text(230, 800, "INVOICE", 14), _text(30, 780, "Invoice No:SYFY-G06-2026-7-15-1"), _text(300, 780, "Date: 2026-07-15"),
           _text(30, 760, "Sold To: Fuyao Glass America Inc."), _text(300, 745, "Container NO.: OOCU9466731")]
    top = 720.0
    rows = [([c for c, _ in FUYAO_COLUMNS], 26.0), *[(r, 16.0) for r in lines], *[([""] * 9, 16.0)] * blank_rows, (total, 16.0)]
    for cells, height in rows:
        x = 22.0
        for (_, w), cell in zip(FUYAO_COLUMNS, cells):
            ops.append(f"{x:.1f} {top - height:.1f} {w} {height} re S")
            if cell:  # small print where it would not fit, as on the real invoice
                ops += _centred(x, w, top - height / 2, cell, max(3, min(8, int((w - 6) / (0.6 * max(len(p) for p in cell.split("|")))))))
            x += w
        top -= height
    ops.append(_text(30, top - 14, "Net Weight 11358.12KGS   Gross Weight 13152.12KGS"))
    return _document(["\n".join(ops).encode("latin-1")])


# ── another supplier: no ruling lines, like many ERP printouts ──
# (x of each column's left edge; numbers end under the right end of their header)
TEXT_HEADER = [("PO#", 17), ("Item#", 128), ("Description", 213), ("HTS", 368), ("HTS Description", 446),
               ("Qty", 589), ("Unit Price", 642), ("Amount", 724)]


def _right(x_end: float, y: float, text: str, size: int = 8) -> str:
    return _text(x_end - 0.45 * size * len(text), y, text, size)


def text_invoice_pdf(items: list[dict[str, str]], per_page: int = 4, seller: str = "VISTA SPORTS GOODS CO LTD") -> bytes:
    """Page 1 a packing list (no prices, no HS codes); then the Commercial Invoice: the same block of
    addresses on every page, the table header only on its first page, every description wrapped onto
    a second line, the last item's second line moved to the top of the next page, and a "Total :" line
    under each page – the last one is the grand total.

    items: {po, item, desc, more, hts, hts_desc, qty, price, amount}
    """
    w, h = 791, 609
    streams = []
    pl = ["0.5 w", _text(250, h - 40, seller, 12), _text(300, h - 70, "PACKING/WEIGHT LIST", 11),
          _text(14, h - 100, "Packing Slip No: INV-5531"),
          _text(17, h - 140, "PO#"), _text(128, h - 140, "Item#"), _text(213, h - 140, "Material Description"),
          _text(520, h - 140, "Qty"), _text(600, h - 140, "Gross Weight")]
    for i, it in enumerate(items):
        y = h - 160 - 14 * i
        pl += [_text(17, y, it["po"]), _text(128, y, it["item"]), _text(213, y, f"{it['desc']} {it['more']}"),
               _right(540, y, it["qty"]), _right(650, y, "9.62")]
    streams.append("\n".join(pl).encode("latin-1"))

    pages = [items[i : i + per_page] for i in range(0, len(items), per_page)]
    carried = None
    for n, chunk in enumerate(pages):
        ops = ["0.5 w", _text(211, h - 30, seller, 12), _text(312, h - 110, "Commercial Invoice", 12),
               _text(496, h - 128, f"Page#:{n + 1} / {len(pages)}"), _text(14, h - 144, "Invoice Number:INV-5531"),
               _text(496, h - 140, "Date:10/13/2026"), _text(14, h - 168, "Bell Sports, Inc."), _text(14, h - 240, "Text1:")]
        y = h - 276
        if n == 0:
            for label, x in TEXT_HEADER:
                ops.append(_text(x, y, label))
            ops += [_text(651, y - 12, "(USD)"), _text(721, y - 12, "(USD)")]
            y -= 50
        else:
            y = h - 272
        if carried is not None:  # the end of the last description of the page before
            ops += [_text(213, y, carried["more"]), _text(446, y, "lenses")]
            y -= 14
        page_qty = page_amount = 0.0
        for k, it in enumerate(chunk):
            ops += [_text(17, y, it["po"]), _text(128, y, it["item"]), _text(213, y, it["desc"]), _text(368, y, it["hts"]),
                    _text(446, y, it["hts_desc"]), _right(604, y, it["qty"]), _text(608, y, "EA"),
                    _right(691, y, it["price"]), _right(762, y, it["amount"])]
            page_qty += float(it["qty"])
            page_amount += float(it["amount"].replace(",", ""))
            last = k == len(chunk) - 1 and n < len(pages) - 1
            if not last:
                ops += [_text(213, y - 12, it["more"]), _text(446, y - 12, "lenses")]
            carried = it if last else None
            y -= 26
        if n == len(pages) - 1:  # the grand total
            page_qty = sum(float(i["qty"]) for i in items)
            page_amount = sum(float(i["amount"].replace(",", "")) for i in items)
        ops += [_text(213, y - 4, "Total :"), _right(604, y - 4, f"{page_qty:.0f}"), _right(762, y - 4, f"{page_amount:,.2f}")]
        ops += [_text(14, y - 40, "Manufacturing Location: VISTA SPORTS GOODS CO., LTD.")]
        streams.append("\n".join(ops).encode("latin-1"))
    return _document(streams, w, h)
