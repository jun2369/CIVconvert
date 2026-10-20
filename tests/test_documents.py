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
    assert save(user_client, doc["id"], 2, rows, status=409)["error"].startswith("文档已确认")
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


def test_list_filters_and_dashboard(app_client):
    login(app_client, "op.one@agslogistics.com")
    a = upload(app_client, "problem invoice.pdf", problem_invoice())
    upload(app_client, "unknown seller.pdf", problem_invoice(seller="SOMEONE ELSE LTD"))
    app_client.post(f"/api/documents/{a['id']}/confirm", json={})
    login(app_client, "op.two@agslogistics.com")
    upload(app_client, "second.pdf", problem_invoice())

    def ids(**params):
        return [d["name"] for d in app_client.get("/api/documents", params=params).json()["items"]]

    assert ids() == ["second"]  # mine
    assert sorted(ids(mine=False)) == ["problem invoice", "second", "unknown seller"]
    assert ids(mine=False, status="confirmed") == ["problem invoice"]
    assert sorted(ids(mine=False, status="attention")) == ["second", "unknown seller"]  # failed + needs review
    assert ids(mine=False, q="T-0001") == ["second", "problem invoice"]  # invoice number
    assert ids(mine=False, q="SELLER") == ["unknown seller"]
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    assert ids(mine=False, created_from=tomorrow) == []

    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    mine = app_client.get("/api/documents/stats", params={"since": today}).json()
    assert (mine["today"], mine["total"], mine["editing"], mine["attention"]) == (1, 1, 1, 1)
    everyone = app_client.get("/api/documents/stats", params={"since": today, "mine": False}).json()
    assert (everyone["total"], everyone["confirmed"], everyone["failed"], everyone["attention"]) == (3, 1, 1, 2)
