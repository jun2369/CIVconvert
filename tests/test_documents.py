import copy
from datetime import datetime, timedelta, timezone

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

from civconvert import db
from civconvert.models import Base
from civconvert.pipeline import InputFile, convert
from conftest import SAMPLE, login, needs_sample
from pdfgen import problem_invoice


def upload(client, name: str, data: bytes, **form) -> dict:
    res = client.post("/api/documents", files={"files": (name, data, "application/pdf")}, data=form)
    assert res.status_code == 200, res.text
    [doc] = res.json()["documents"]
    return doc


def detail(client, doc_id: str) -> dict:
    res = client.get(f"/api/documents/{doc_id}")
    assert res.status_code == 200, res.text
    return res.json()


def save(client, doc_id: str, revision: int, rows: list, reason: str = "auto", status: int = 200) -> dict:
    res = client.put(f"/api/documents/{doc_id}/sheet", json={"revision": revision, "rows": rows, "reason": reason})
    assert res.status_code == status, res.text
    return res.json()


def test_migrations_match_the_models(database):
    with db.engine().connect() as conn:
        assert compare_metadata(MigrationContext.configure(conn), Base.metadata) == []


@needs_sample
def test_unedited_document_exports_exactly_the_converted_csv(user_client):
    doc = upload(user_client, SAMPLE.name, SAMPLE.read_bytes())
    assert doc["status"] == "editing" and doc["invoice_no"] == "8100800394" and doc["line_count"] == 20
    d = detail(user_client, doc["id"])
    assert len(d["template"]["columns"]) == 210 and d["template"]["roles"]["E"]["field"] == "description"
    assert d["template"]["roles"]["A"]["line_no"] is True
    res = user_client.post(f"/api/documents/{doc['id']}/export?revision=1")
    direct = convert([InputFile(SAMPLE.name, SAMPLE.read_bytes())]).csv_bytes()
    assert res.content == direct  # byte for byte: headers, order, encoding, line endings
    assert 'filename="CargoWise Input - 8100800394 Commercial Invoice.csv"' in res.headers["content-disposition"]


def test_edit_save_export_confirm(user_client):
    doc = upload(user_client, "problem invoice.pdf", problem_invoice())
    assert doc["status"] == "editing" and doc["needs_review"] is True and doc["line_count"] == 26
    d = detail(user_client, doc["id"])
    sheet = d["sheet"]
    assert sheet["revision"] == 1
    flagged = {(f["line"], f["level"]) for f in sheet["flags"]}
    assert (12, "error") in flagged and (5, "warning") in flagged and (9, "error") in flagged

    rows = copy.deepcopy(sheet["rows"])
    rows[11]["cells"]["4"] = {"v": "Repaired Description"}  # E13: fill the empty description
    rows[4]["cells"]["5"] = {"v": "9006910001"}  # F6: fix the HS code format
    rows[0]["cells"]["9"] = {"f": "=ROUND(LEN(D2)*1.5,1)"}  # J2: a formula
    del rows[25]  # delete the last row
    saved = save(user_client, doc["id"], 1, rows, "manual")
    assert saved["revision"] == 2 and saved["line_count"] == 25
    assert saved["rows"][0]["cells"]["9"] == {"f": "=ROUND(LEN(D2)*1.5,1)", "v": "12"}  # computed by the server
    lines_flagged = {f["line"] for f in saved["flags"]}
    assert 12 not in lines_flagged and 5 not in lines_flagged and 9 in lines_flagged  # qty x price stays (PDF)

    # a second editor still on revision 1 must not overwrite revision 2
    res = user_client.put(f"/api/documents/{doc['id']}/sheet", json={"revision": 1, "rows": rows})
    assert res.status_code == 409 and res.json()["conflict"] is True and res.json()["revision"] == 2

    csv_text = user_client.post(f"/api/documents/{doc['id']}/export").content.decode()
    lines = csv_text.splitlines()
    assert len(lines) == 1 + 25 and len(lines[1].split(",")) == 210
    assert lines[12].split(",")[4] == "Repaired Description"  # data row 12 (line 0 is the header)
    assert lines[1].split(",")[9] == "12"  # the formula's value, not the formula
    assert user_client.post(f"/api/documents/{doc['id']}/export?revision=1").status_code == 409  # stale

    res = user_client.post(f"/api/documents/{doc['id']}/confirm", json={"revision": 2})
    assert res.json()["document"]["status"] == "confirmed"
    assert save(user_client, doc["id"], 2, rows, status=409)["code"] == "completed_readonly"
    assert user_client.post(f"/api/documents/{doc['id']}/export").status_code == 200  # final export
    assert user_client.post(f"/api/documents/{doc['id']}/reopen").json()["document"]["status"] == "editing"

    d = detail(user_client, doc["id"])
    assert d["document"]["export_count"] == 2 and d["document"]["last_exported_at"]
    assert [e["final"] for e in d["exports"]] == [True, False]
    log = [e["action"] for e in user_client.get(f"/api/documents/{doc['id']}/log").json()["log"]]
    assert log == ["document.reopened", "document.exported", "document.confirmed", "document.exported",
                   "sheet.saved", "conversion.succeeded", "document.created"]
    reasons = [v["reason"] for v in user_client.get(f"/api/documents/{doc['id']}/versions").json()["versions"]]
    assert reasons == ["confirm", "manual", "initial"]


def test_sheet_structure_is_protected(user_client):
    doc = upload(user_client, "problem invoice.pdf", problem_invoice())
    rows = detail(user_client, doc["id"])["sheet"]["rows"]
    bad = copy.deepcopy(rows)
    bad[0]["cells"]["210"] = {"v": "outside the template"}  # column 211 does not exist
    assert "outside the template" in save(user_client, doc["id"], 1, bad, status=400)["error"]
    bad = copy.deepcopy(rows)
    bad[0]["cells"]["3"] = {"v": "<script>x</script>", "x": "ignored"}
    assert save(user_client, doc["id"], 1, bad)["rows"][0]["cells"]["3"] == {"v": "<script>x</script>"}  # stored as text


def test_failed_conversion_and_retry(user_client):
    doc = upload(user_client, "unknown seller.pdf", problem_invoice(seller="SOMEONE ELSE LTD"))
    assert doc["status"] == "failed" and "identify the customer" in doc["error"]
    res = user_client.post(f"/api/documents/{doc['id']}/retry", json={"customer": "istone"})
    assert res.json()["document"]["status"] == "editing"


def test_preview_follows_the_sheet_rows(user_client):
    doc = upload(user_client, "problem invoice.pdf", problem_invoice())
    rows = detail(user_client, doc["id"])["sheet"]["rows"]
    rows = rows[1:]  # delete line 1: sheet row 1 is now PDF line 2
    rows.insert(5, {"id": "handmade1", "src": None, "cells": {"3": {"v": "EXTRA"}}})
    save(user_client, doc["id"], 1, rows)
    pv = user_client.get(f"/api/documents/{doc['id']}/preview").json()
    assert len(pv["result"]["lines"]) == len(pv["preview"]["lines"]) == 26
    assert pv["preview"]["items"][0]["model"] == "CINSM002"  # follows src, not the position
    assert pv["preview"]["lines"][5]["page"] is None  # the row added by hand is not in the PDF
    assert pv["result"]["lines"][0]["original"]["D"] == "CINSM002"
    assert [p["page"] for p in pv["preview"]["pages"]] == [1, 2]


def test_original_pdf_is_kept(user_client):
    data = problem_invoice()
    doc = upload(user_client, "problem invoice.pdf", data)
    file_id = doc["files"][0]["id"]
    res = user_client.get(f"/api/documents/{doc['id']}/files/{file_id}")
    assert res.content == data and res.headers["content-type"] == "application/pdf"


def test_everyone_only_sees_their_own_documents(app_client):
    login(app_client, "op.one@agslogistics.com")
    a = upload(app_client, "problem invoice.pdf", problem_invoice())
    upload(app_client, "unknown seller.pdf", problem_invoice(seller="SOMEONE ELSE LTD"))
    app_client.post(f"/api/documents/{a['id']}/confirm", json={})
    login(app_client, "op.two@agslogistics.com")
    upload(app_client, "second.pdf", problem_invoice())

    def names(**params):
        return [d["name"] for d in app_client.get("/api/documents", params=params).json()["items"]]

    assert names() == ["second"]
    assert names(mine=False) == ["second"]  # there is no "all documents"
    stats = app_client.get("/api/documents/stats").json()
    assert (stats["total"], stats["editing"], stats["confirmed"]) == (1, 1, 0)
    # op.one's document cannot be opened, changed, exported or previewed by op.two
    for method, path in [("get", ""), ("get", "/preview"), ("get", "/log"), ("post", "/export"), ("post", "/reopen"),
                         ("get", f"/files/{a['files'][0]['id']}")]:
        res = getattr(app_client, method)(f"/api/documents/{a['id']}{path}")
        assert res.status_code == 404 and res.json()["code"] == "not_found", (path, res.text)
    res = app_client.put(f"/api/documents/{a['id']}/sheet", json={"revision": 1, "rows": []})
    assert res.status_code == 404

    login(app_client, "op.one@agslogistics.com")
    assert sorted(names()) == ["problem invoice", "unknown seller"]
    assert names(status="confirmed") == ["problem invoice"]
    assert names(status="editing") == []
    assert names(status="attention") == ["unknown seller"]  # failed
    assert names(q="SELLER") == ["unknown seller"]
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    assert names(created_from=tomorrow) == []


def test_documents_are_deleted_four_months_after_upload(user_client):
    from sqlalchemy import func, select

    from civconvert import documents
    from civconvert.models import AuditLog, Document, DocumentFile, DocumentSheet, SheetVersion

    old = upload(user_client, "old.pdf", problem_invoice())
    recent = upload(user_client, "recent.pdf", problem_invoice())
    now = datetime.now(timezone.utc)
    with db.session_scope() as s:
        s.get(Document, old["id"]).created_at = documents.months_before(now, 4) - timedelta(days=1)
        s.get(Document, recent["id"]).created_at = documents.months_before(now, 4) + timedelta(days=1)
    expires = datetime.fromisoformat(detail(user_client, recent["id"])["document"]["expires_at"].replace("Z", "+00:00"))
    assert abs((expires - now).days) <= 2  # the recent one is about to expire

    with db.session_scope() as s:
        assert documents.purge_expired(s) == 1
    with db.session_scope() as s:
        assert s.get(Document, old["id"]) is None and s.get(Document, recent["id"]) is not None
        for model in (DocumentFile, DocumentSheet, SheetVersion):  # everything of the old document is gone
            assert s.scalar(select(func.count()).select_from(model).where(model.document_id == old["id"])) == 0
        assert s.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action == "documents.purged")) == 1
    assert user_client.get(f"/api/documents/{old['id']}").status_code == 404


def test_months_before():
    from civconvert.documents import months_before

    assert months_before(datetime(2026, 10, 20, tzinfo=timezone.utc), 4) == datetime(2026, 6, 20, tzinfo=timezone.utc)
    assert months_before(datetime(2027, 1, 31, tzinfo=timezone.utc), 4) == datetime(2026, 9, 30, tzinfo=timezone.utc)
    assert months_before(datetime(2026, 10, 31, tzinfo=timezone.utc), -4) == datetime(2027, 2, 28, tzinfo=timezone.utc)


def test_flags_and_errors_carry_codes_for_translation(user_client):
    doc = upload(user_client, "problem invoice.pdf", problem_invoice())
    flags = detail(user_client, doc["id"])["sheet"]["flags"]
    codes = {(f["line"], f["code"]) for f in flags}
    assert (12, "required_empty") in codes and (5, "bad_format") in codes and (3, "inconsistent_cells") in codes
    [math] = [f for f in flags if f["code"] == "pdf_line"]
    assert math["line"] == 9 and math["params"]["inner"]["code"] == "line_math"
    assert math["params"]["inner"]["params"] == {"q": "qty", "p": "unit_price", "a": "amount", "calc": "18", "amount": "23"}
    assert all(f["message"] and f["message"].isascii() for f in flags)  # English text for anyone without a translation
    checks = {c["name"]: c for c in detail(user_client, doc["id"])["extraction"]["checks"]}
    assert checks["Line items"]["code"] == "lines_read" and checks["Line items"]["params"] == {"n": 26}
    assert checks["Line math"]["code"] == "line_math_bad" and checks["Line math"]["params"]["lines"] == ["9"]
    assert all(c["code"] and c["detail"].isascii() for c in checks.values())
    res = user_client.post("/api/documents", files={"files": ("x.pdf", b"hello", "application/pdf")})
    assert res.status_code == 422 and res.json()["code"] == "not_pdf" and res.json()["params"] == {"name": "x.pdf"}
