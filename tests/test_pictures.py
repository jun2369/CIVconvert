"""Invoices as pictures (PNG, JPG, TIFF …) and scanned PDFs: read by OCR, then like any PDF."""

import base64
import io

import pdfplumber
import pytest
from PIL import Image, ImageDraw

from civconvert import extract, ocr, sources
from civconvert.pipeline import ConversionError, InputFile, convert
from conftest import API_KEY
from pdfgen import _document, _right, _text, grid_invoice_pdf, mixed_pdf, problem_invoice, text_invoice_pdf
from test_documents import detail, upload
from test_generic import _text_items

needs_ocr = pytest.mark.skipif(not ocr.available(), reason="OCR packages not installed")
# its "Description" column holds only the size, so the name of the goods is put in front of it (tidy_items)
WIRE_MESH = {"model": "Wire Mesh Cage", "description": "Wire Mesh Cage 1200*1000*1200mm", "qty": "358",
             "unit_price": "60", "amount": "21480"}


def render(pdf: bytes, dpi: int = 200, fmt: str = "PNG", **save) -> bytes:
    """The pages of a PDF as a picture file (all pages: TIFF)."""
    with pdfplumber.open(io.BytesIO(pdf)) as doc:
        pages = [p.to_image(resolution=dpi).original.convert("RGB") for p in doc.pages]
    buf = io.BytesIO()
    pages[0].save(buf, fmt, save_all=len(pages) > 1, append_images=pages[1:], **save)
    return buf.getvalue()


def picture(fmt: str, size=(40, 30), mode="RGB", **save) -> bytes:
    buf = io.BytesIO()
    Image.new(mode, size, "white").save(buf, fmt, **save)
    return buf.getvalue()


def fields(item: dict) -> dict:
    return {k: item[k] for k in WIRE_MESH}


# ───────────────────────────── pictures as files ─────────────────────────────


@pytest.mark.parametrize("name, fmt, kind", [
    ("a.png", "PNG", "png"), ("a.jpg", "JPEG", "jpeg"), ("a.tif", "TIFF", "tiff"), ("a.bmp", "BMP", "bmp"),
    ("a.gif", "GIF", "gif"), ("a.webp", "WEBP", "webp"),
])
def test_picture_formats(name, fmt, kind):
    data = picture(fmt)
    assert sources.kind(name, data) == sources.IMAGE and sources.picture_format(name, data) == kind
    assert sources.content_type(name, data) == sources.PICTURE_TYPES[kind]
    assert sources.strip_extension(name) == "a"


def test_not_pictures():
    assert sources.kind("notes.txt", b"BM is not a bitmap") is None  # "BM" only counts for a .bmp
    assert sources.kind("x.png", b"\x89PNX not really") is None
    assert sources.kind("invoice.pdf", problem_invoice()) == sources.PDF  # PDFs and Excel files as before
    assert sources.content_type("invoice.pdf", problem_invoice()) == "application/pdf"


def test_heic_photos():
    pillow_heif = pytest.importorskip("pillow_heif")
    try:
        img = pillow_heif.from_pillow(Image.new("RGB", (64, 48), "white"))
        buf = io.BytesIO()
        img.save(buf, quality=90)
    except Exception as e:  # no HEIF encoder in this build: only reading is needed
        pytest.skip(f"cannot write HEIC here: {e}")
    data = buf.getvalue()
    assert sources.picture_format("IMG_0001.HEIC", data) == "heic"
    [page] = sources.picture_pages(data, "IMG_0001.HEIC")
    assert page.size == (64, 48)


def test_a_picture_as_a_pdf_page():
    """Portrait pictures become A4-wide pages, landscape ones as wide as A4 is high; a TIFF keeps its pages;
    a phone photo is turned as it was taken; transparency becomes white."""
    tall, wide = picture("PNG", (1000, 1400)), picture("PNG", (1400, 1000))
    for data, width in ((tall, 595), (wide, 842)):
        with extract.open_pdf(data, "x.png", ocr.NONE) as pdf:
            assert len(pdf.pages) == 1 and round(pdf.pages[0].width) == width
    pages = [Image.new("RGB", (800, 1100), "white") for _ in range(3)]
    buf = io.BytesIO()
    pages[0].save(buf, "TIFF", save_all=True, append_images=pages[1:])
    with extract.open_pdf(buf.getvalue(), "scan.tif", ocr.NONE) as pdf:
        assert len(pdf.pages) == 3
    exif = Image.Exif()
    exif[0x0112] = 6  # taken with the phone held upright: the pixels are stored on their side
    buf = io.BytesIO()
    Image.new("RGB", (1200, 900), "white").save(buf, "JPEG", exif=exif.tobytes())
    assert sources.picture_pages(buf.getvalue(), "photo.jpg")[0].size == (900, 1200)
    [clear] = sources.picture_pages(picture("PNG", (20, 20), "RGBA"), "logo.png")
    assert clear.mode == "RGB" and clear.getpixel((5, 5)) == (255, 255, 255)
    with pytest.raises(extract.PdfError, match="cannot read the picture"):
        extract.open_pdf(b"\x89PNG\r\n\x1a\n broken", "broken.png")


# ───────────────────────────── table rulings ─────────────────────────────


def test_rulings_under_a_stamp():
    """A table's lines are found; where a red stamp is printed over them, they go on under it. A gap a table
    really has (no line there) is not filled, and the stamp's own ring is not a line."""
    img = Image.new("RGB", (900, 400), "white")
    d = ImageDraw.Draw(img)
    for y in (100, 140, 200):
        d.line([(50, y), (850, y)], fill="black", width=2)
    d.line([(50, 250), (400, 250)], fill="black", width=2)  # a line with a real gap …
    d.line([(600, 250), (850, 250)], fill="black", width=2)
    for x in (50, 300, 600, 850):
        d.line([(x, 100), (x, 250)], fill="black", width=2)
    d.ellipse([(520, 160), (680, 240)], fill=(240, 120, 120), outline=(220, 30, 30), width=6)  # … and a stamp over two lines
    hs, vs = ocr.rulings(img, 20.0)

    def at(y: int) -> list[tuple[int, int]]:  # the pieces of the line drawn at y (2 px wide)
        return sorted((round(a), round(b)) for p, a, b in hs if abs(p - y) <= 2)

    def near(got: list[tuple[int, int]], want: list[tuple[int, int]]) -> bool:
        return len(got) == len(want) and all(abs(g[0] - w[0]) <= 3 and abs(g[1] - w[1]) <= 3 for g, w in zip(got, want))

    assert near(at(100), [(50, 850)]) and near(at(140), [(50, 850)])
    assert near(at(200), [(50, 850)])  # under the stamp
    assert near(at(250), [(50, 400), (600, 850)])  # the real gap stays
    assert [(round(x / 5) * 5, round(a / 5) * 5, round(b / 5) * 5) for x, a, b in sorted(vs)] == [
        (50, 100, 250), (300, 100, 250), (600, 100, 250), (850, 100, 250)]  # x = 600 under the stamp too


# ───────────────────────────── reading pictures ─────────────────────────────


@needs_ocr
@pytest.mark.parametrize("name, data", [
    ("invoice.png", lambda: render(grid_invoice_pdf(), 150)),
    ("invoice.jpg", lambda: render(grid_invoice_pdf(), 200, "JPEG", quality=75)),
    ("scan.pdf", lambda: sources.picture_pdf(render(grid_invoice_pdf()), "x.png")[0]),
])
def test_a_ruled_invoice_as_a_picture(name, data):
    """The Speed Xpress invoice as a screenshot, a photo-like JPEG and a scanned PDF: the product name over
    two lines in its cell, "Unite Price (USD)" over two lines, the totals row without a "Total" label."""
    r = convert([InputFile(name, data())])
    assert [fields(i) for i in r.items] == [WIRE_MESH]
    assert r.document["invoice_no"] == "SPX-443"
    assert {c.name: c.ok for c in r.checks} == {"Line items": True, "Subtotal qty": True, "Subtotal amount": True, "Line math": True, "OCR": False}
    ocr_check = next(c for c in r.checks if c.name == "OCR")
    assert ocr_check.code == "ocr" and ocr_check.params == {"pages": f"{name} p.1"}
    assert r.parts[0].ocr_pages == [1] and r.needs_review
    assert [row[3:6] for row in r.rows] == [["Wire Mesh Cage", "Wire Mesh Cage 1200*1000*1200mm", ""]]  # no HS code on it


@needs_ocr
def test_a_scan_over_several_pages():
    """A multi-page TIFF of an invoice without ruling lines (a packing list first, wrapped descriptions,
    the header only on the first invoice page): every line, item number, HS code and amount is read."""
    items = _text_items()
    r = convert([InputFile("scan.tif", render(text_invoice_pdf(items, per_page=4), 200, "TIFF", compression="tiff_deflate"))])
    assert [(i["model"], i["hs_code"], i["unit_price"], i["amount"]) for i in r.items] == [
        (i["item"], i["hts"], i["price"], i["amount"]) for i in items]
    assert {c.name: c.ok for c in r.checks} == {"Line items": True, "Subtotal qty": True, "Subtotal amount": True, "Line math": True, "OCR": False}
    assert r.parts[0].ocr_pages == [1, 2, 3]


@needs_ocr
def test_a_text_pdf_is_not_read_by_ocr():
    r = convert([InputFile("invoice.pdf", grid_invoice_pdf())])
    assert [fields(i) for i in r.items] == [WIRE_MESH] and r.parts[0].ocr_pages == []
    assert all(c.ok for c in r.checks) and "OCR" not in {c.name for c in r.checks}


def _jpeg_page(pdf: bytes) -> tuple[bytes, int, int]:
    data = render(pdf, 150, "JPEG", quality=85)
    with Image.open(io.BytesIO(data)) as im:
        return data, im.width, im.height


def _streams(pdf: bytes) -> list[bytes]:
    """The content streams of a pdfgen PDF, page by page."""
    out, rest = [], pdf
    while b"stream\n" in rest:
        rest = rest.split(b"stream\n", 1)[1]
        body, rest = rest.split(b"\nendstream", 1)
        out.append(body)
    return out


@needs_ocr
def test_a_text_invoice_with_a_picture_page_is_read_as_before():
    """A picture page (a signed declaration, a logo page) next to a text invoice: no OCR, the same result."""
    items = _text_items()
    text = text_invoice_pdf(items, per_page=4)
    plain = convert([InputFile("vista.pdf", text)])
    mixed = convert([InputFile("vista.pdf", mixed_pdf([*_streams(text), _jpeg_page(grid_invoice_pdf())], 791, 609))])
    assert mixed.rows == plain.rows and [c.ok for c in mixed.checks] == [c.ok for c in plain.checks]
    assert mixed.parts[0].ocr_pages == [] and not mixed.needs_review


@needs_ocr
def test_a_scanned_invoice_behind_a_text_page_is_read_by_ocr():
    cover = "\n".join([_text(60, 760, "Dear Sirs, please find the invoice for shipment SPX-443 attached.", 10)]).encode("latin-1")
    r = convert([InputFile("mail.pdf", mixed_pdf([cover, _jpeg_page(grid_invoice_pdf())]))])
    assert [fields(i) for i in r.items] == [WIRE_MESH] and r.parts[0].ocr_pages == [2]
    assert {c.name: c.ok for c in r.checks}["OCR"] is False


def test_without_ocr_a_picture_is_a_clear_error(monkeypatch):
    monkeypatch.setattr(ocr, "_available", False)
    with pytest.raises(ConversionError, match="OCR is not installed"):
        convert([InputFile("invoice.png", render(grid_invoice_pdf(), 100))])


# ───────────────────────────── totals rows without a "Total" label ─────────────────────────────


def test_a_totals_row_of_only_the_sums_in_a_ruled_table():
    """It used to become a second line with only an amount; now it is the totals row – the total quantity
    printed under "Unit" counts, as it is exactly the sum."""
    r = convert([InputFile("invoice.pdf", grid_invoice_pdf())])
    assert len(r.items) == 1 and r.parts[0].totals["amount"] == "21480" and r.parts[0].totals["qty"] == "358"
    two = grid_invoice_pdf([["1", "Wire Mesh|Cage", "1200*1000*1200mm", "358", "(PCS)", "60", "21480"],
                            ["2", "Tray", "800*600mm", "42", "(PCS)", "10", "420"]], ["", "", "", "400", "", "", "21900"])
    r = convert([InputFile("invoice.pdf", two)])
    assert [i["model"] for i in r.items] == ["Wire Mesh Cage", "Tray"] and all(c.ok for c in r.checks)


def test_a_row_of_numbers_that_are_not_the_sums_stays_a_line():
    pdf = grid_invoice_pdf(totals=["", "", "", "999", "", "", "1"])
    r = convert([InputFile("invoice.pdf", pdf)])
    assert len(r.items) == 2  # as before: not taken for the totals


def test_a_totals_line_of_only_the_sums_without_ruling_lines():
    ops = ["0.5 w", _text(220, 800, "COMMERCIAL INVOICE", 12), _text(40, 770, "Invoice No.: NL-77")]
    for label, x in (("Item No.", 40), ("Description", 120), ("HS Code", 300), ("Qty", 400), ("Unit Price", 450), ("Amount", 520)):
        ops.append(_text(x, 740, label))
    lines = [("A-100", "STEEL SHELF", "9403.20.0080", "10", "5.00", "50.00"), ("A-200", "WIRE BASKET", "7326.20.0070", "4", "2.50", "10.00")]
    y = 720
    for item, desc, hs, qty, price, amount in lines:
        ops += [_text(40, y, item), _text(120, y, desc), _text(300, y, hs), _right(418, y, qty), _right(490, y, price), _right(560, y, amount)]
        y -= 16
    ops += [_right(418, y - 4, "14"), _right(560, y - 4, "60.00"), _text(40, y - 40, "Made in China")]
    r = convert([InputFile("nolines.pdf", _document(["\n".join(ops).encode("latin-1")]))])
    assert [i["model"] for i in r.items] == ["A-100", "A-200"]
    assert r.parts[0].totals["qty"] == "14" and all(c.ok for c in r.checks)


# ───────────────────────────── on the web page and from Power Automate ─────────────────────────────


@needs_ocr
def test_uploading_a_picture(user_client):
    png = render(grid_invoice_pdf(), 150)
    doc = upload(user_client, "Speed Xpress Inc-INVOICE.png", png)
    d = detail(user_client, doc["id"])["document"]
    assert (d["status"], d["name"], d["line_count"], d["invoice_no"], d["needs_review"]) == ("editing", "Speed Xpress Inc-INVOICE", 1, "SPX-443", True)
    file_id = d["files"][0]["id"]
    res = user_client.get(f"/api/documents/{doc['id']}/files/{file_id}")
    assert res.content == png and res.headers["content-type"] == "image/png" and res.headers["content-disposition"].startswith("inline")
    pv = user_client.get(f"/api/documents/{doc['id']}/preview").json()["preview"]  # the picture on the left, the line marked on it
    assert len(pv["pages"]) == 1 and pv["pages"][0]["image"].startswith("data:image/") and pv["lines"][0]["page"] == 0
    assert pv["lines"][0]["cells"]["model"] and pv["totals"]["page"] == 0


def test_pictures_in_emails_and_the_json_api_are_skipped_as_before(user_client, monkeypatch):
    from test_intake import att, outlook_email, post

    monkeypatch.setenv("API_KEY", API_KEY)
    png = render(grid_invoice_pdf(), 100)
    r = post(user_client, outlook_email(att("invoice.png", png)), owner="op.one@agslogistics.com").json()
    assert [(a["name"], a["decision"], a["code"]) for a in r["attachments"]] == [("invoice.png", "skipped", "not_pdf_excel")]
    res = user_client.post("/api/convert/json", json={"files": [{"name": "invoice.png", "content_base64": base64.b64encode(png).decode()}]},
                           headers={"X-API-Key": API_KEY})
    assert res.status_code == 422 and "Skipped: invoice.png" in res.json()["error"]
