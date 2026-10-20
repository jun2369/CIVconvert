"""A tiny PDF writer for tests: an ISTONE-style invoice table with ruling lines, split over pages."""

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


def invoice_pdf(lines: list[list[str]], subtotal: list[str], per_page: int = 14) -> bytes:
    """Page 1 has the title and the first `per_page` lines; later pages repeat the header row."""
    chunks = [lines[i : i + per_page] for i in range(0, len(lines), per_page)] or [[]]
    streams = []
    for p, chunk in enumerate(chunks):
        ops = ["0.5 w"]
        top = 800.0
        if p == 0:
            ops += [_text(200, 800, "ISTONE INNOVATION LIMITED", 14), _text(230, 780, "Commercial Invoice", 12),
                    _text(28, 750, "Invoice No. : T-0001"), _text(330, 750, "Issued Date :2026-10-01")]
            top = 730.0
        rows = [HEADER, *chunk] + ([subtotal] if p == len(chunks) - 1 else [])
        ops += _table(rows, top)
        streams.append("\n".join(ops).encode("latin-1"))

    objs = [b"<< /Type /Catalog /Pages 2 0 R >>"]
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(len(streams)))
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(streams)} >>".encode())
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    for i, s in enumerate(streams):
        objs.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 3 0 R >> >> /Contents {5 + 2 * i} 0 R >>".encode()
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


def problem_invoice() -> bytes:
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
    return invoice_pdf(lines, ["Subtotal :", "", str(qty_total), "", f"US${amount_total:,}.00", ""])
