import csv
import io

import pytest

from civconvert.pipeline import ConversionError, Failure, InputFile, convert, convert_many
from conftest import SAMPLE, needs_sample


def _csv_rows(data: bytes) -> list[list[str]]:
    return list(csv.reader(io.StringIO(data.decode("utf-8"), newline="")))


@needs_sample
def test_sample_invoice_end_to_end():
    r = convert([InputFile(SAMPLE.name, SAMPLE.read_bytes())])
    assert r.customer.id == "istone"
    assert r.filename == "CargoWise Input - 8100800394 Commercial Invoice.csv"
    assert r.document == {"invoice_no": "8100800394", "invoice_date": "2026-09-30"}
    assert all(c.ok for c in r.checks), r.checks
    assert r.warnings == []

    data = r.csv_bytes()
    assert not data.startswith(b"\xef\xbb\xbf") and b"\r\n" in data
    rows = _csv_rows(data)
    assert len(rows) == 21 and {len(row) for row in rows} == {210}
    assert rows[0][:6] == ["LNO", "Entry/Line Nos.", "Inv. # for Line", "Product Code", "Goods Description", "Tariff"]
    assert rows[1][:6] == ["1", "", "", "CINSFAVS(CINSFAVS-01)", "Selfie Stick + Throwpod", "9006910001"]
    assert rows[5][3:6] == ["CINSBAVE", '3-prong to 1/4"Adapter', "9006910001"]
    assert rows[20][:6] == ["20", "", "", "CINSABQB(Flow203)", "Insta360 Flow2 Stabilizer", "9006910001"]
    assert [row[0] for row in rows[1:]] == [str(n) for n in range(1, 21)]
    # only A, D, E, F are filled
    assert all(not row[1] and not row[2] and not any(row[6:]) for row in rows[1:])


@needs_sample
def test_sample_template_sample_rows_are_not_copied():
    r = convert([InputFile(SAMPLE.name, SAMPLE.read_bytes())])
    assert len(r.rows) == 20  # the template's 1-4 sample LNOs are not part of the output


@needs_sample
def test_parts_of_one_invoice_are_numbered_continuously():
    part = SAMPLE.read_bytes()
    [r] = convert_many([InputFile("part 1.pdf", part), InputFile("part 2.pdf", part)], merge=True)
    assert [row[0] for row in r.rows] == [str(n) for n in range(1, 41)]
    assert r.filename == "CargoWise Input - part 1.csv"
    assert r.sources == ["part 1.pdf", "part 2.pdf"]
    # the same invoice twice does not add up to one subtotal - that must be flagged
    assert r.needs_review and not all(c.ok for c in r.checks)


@needs_sample
def test_separate_files_give_separate_results():
    data = SAMPLE.read_bytes()
    results = convert_many([InputFile("a.pdf", data), InputFile("b.pdf", b"not a pdf")], merge=False)
    assert results[0].filename == "CargoWise Input - a.csv"
    assert isinstance(results[1], Failure) and "not a PDF" in results[1].error


def test_rejects_non_pdf():
    with pytest.raises(ConversionError, match="not a PDF"):
        convert([InputFile("x.pdf", b"hello")])


@needs_sample
def test_unknown_customer():
    with pytest.raises(ConversionError, match="Unknown customer"):
        convert([InputFile(SAMPLE.name, SAMPLE.read_bytes())], customer="nobody")
