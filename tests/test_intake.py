"""E-mails passed on by Power Automate Cloud: the CIV among the attachments becomes an "auto" document."""

import base64
from datetime import datetime, timedelta, timezone

import pytest

from civconvert import db, documents, intake
from civconvert.models import IntakeMessage, User
from conftest import API_KEY, login
from pdfgen import packing_list_pdf, problem_invoice
from test_documents import upload
from test_generic import size_rows_xlsx

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
OWNER = "op.one@agslogistics.com"


def att(name: str, data: bytes, **extra) -> dict:
    """An attachment as the Outlook trigger "When a new email arrives (V3)" gives it."""
    return {"@odata.type": "#microsoft.graph.fileAttachment", "id": "AAMk-" + name, "name": name, "contentType": "application/octet-stream",
            "size": len(data), "isInline": False, "contentBytes": base64.b64encode(data).decode(), **extra}


def outlook_email(*attachments: dict, message_id: str = "<m1@speedx.io>", subject: str = "CIV for shipment 123") -> dict:
    """The trigger's output (triggerBody()), as Power Automate posts it."""
    return {"id": "AAMkAGI2-" + message_id, "receivedDateTime": "2026-10-21T08:15:00+00:00", "hasAttachments": True,
            "internetMessageId": message_id, "subject": subject, "bodyPreview": "Please see attached", "importance": "normal",
            "from": "junjie.ma@speedx.io", "toRecipients": OWNER, "isHtml": True, "body": "<p>Hi</p>", "attachments": list(attachments)}


def post(client, body, owner: str | None = OWNER, key: str | None = API_KEY):
    return client.post("/api/intake/email", params={"owner": owner} if owner else None, json=body,
                       headers={"X-API-Key": key} if key else {})


@pytest.fixture
def client(app_client, monkeypatch):
    monkeypatch.setenv("API_KEY", API_KEY)
    login(app_client, OWNER)  # the OP has used CIV Converter before
    return app_client


def listed(client) -> dict[str, dict]:
    return {d["id"]: d for d in client.get("/api/documents").json()["items"]}


def test_the_civ_of_an_email_becomes_an_auto_document(client):
    email = outlook_email(att("CI-T-0001.pdf", problem_invoice()), att("PL-T-0001.pdf", packing_list_pdf()),
                          att("image001.png", PNG, isInline=True, contentType="image/png"))
    res = post(client, email)
    assert res.status_code == 200, res.text
    r = res.json()
    assert r["status"] == "ok" and r["email"]["from"] == "junjie.ma@speedx.io" and r["email"]["subject"] == "CIV for shipment 123"
    assert [(a["name"], a["decision"], a["code"]) for a in r["attachments"]] == [
        ("CI-T-0001.pdf", "civ", "civ"), ("PL-T-0001.pdf", "skipped", "not_civ"), ("image001.png", "skipped", "inline_picture")]
    [doc_id] = r["documents"]
    d = listed(client)[doc_id]  # in the OP's list, converted like an upload
    assert (d["source"], d["status"], d["line_count"], d["invoice_no"], d["customer_id"]) == ("auto", "editing", 26, "T-0001", "istone")
    assert d["email"]["from"] == "junjie.ma@speedx.io" and d["email"]["subject"] == "CIV for shipment 123"
    [created] = [e for e in client.get(f"/api/documents/{doc_id}/log").json()["log"] if e["action"] == "document.created"]
    assert created["user"] is None and created["detail"]["source"] == "auto" and created["detail"]["email_from"] == "junjie.ma@speedx.io"
    # a manual upload is still "manual"
    manual = upload(client, "problem invoice.pdf", problem_invoice())
    assert listed(client)[manual["id"]]["source"] == "manual"

    # Power Automate delivers the same e-mail again (a retry): nothing is created twice
    again = post(client, email).json()
    assert again["status"] == "duplicate" and again["documents"] == [doc_id]
    assert len(listed(client)) == 2

    # an e-mail without a CIV is recorded, so the OP can see it and upload by hand
    r = post(client, outlook_email(att("PL-T-0001.pdf", packing_list_pdf()), att("logo.png", PNG), message_id="<m2@speedx.io>")).json()
    assert r["status"] == "no_civ" and r["documents"] == []
    assert [a["code"] for a in r["attachments"]] == ["not_civ", "not_pdf_excel"]

    since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    s = client.get("/api/intake/today", params={"since": since}).json()
    assert (s["ever"], s["emails"], s["attachments"], s["civ"], s["no_civ"]) == (True, 2, 5, 1, 1)
    assert s["items"][1]["attachments"][0]["document_name"] == "CI-T-0001"  # the newest e-mail first
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    assert client.get("/api/intake/today", params={"since": tomorrow}).json()["emails"] == 0

    # another OP sees neither the documents nor the e-mails
    login(client, "op.two@agslogistics.com")
    assert listed(client) == {}
    assert client.get("/api/intake/today", params={"since": since}).json()["ever"] is False


def test_older_outlook_triggers_and_excel(client):
    """PascalCase names (shared mailbox / V2 triggers), an Excel CIV, the same invoice attached twice."""
    data = size_rows_xlsx()
    email = {"Subject": "Invoice AE-017", "From": "junjie.ma@speedx.io", "InternetMessageId": "<m3@speedx.io>",
             "DateTimeReceived": "2026-10-21T09:00:00Z",
             "Attachments": [{"Name": "AE-017-26-27.xlsx", "ContentBytes": base64.b64encode(data).decode(), "IsInline": False},
                             {"Name": "AE-017-26-27 (copy).xlsx", "ContentBytes": base64.b64encode(data).decode(), "IsInline": False}]}
    r = post(client, email).json()
    assert [(a["decision"], a["code"]) for a in r["attachments"]] == [("civ", "civ"), ("skipped", "same_invoice")]
    [doc_id] = r["documents"]
    d = listed(client)[doc_id]
    assert (d["source"], d["status"], d["line_count"], d["invoice_no"]) == ("auto", "editing", 6, "AE-017-26-27")


def test_what_power_automate_is_told_when_something_is_wrong(client, monkeypatch):
    email = outlook_email(att("CI.pdf", problem_invoice()))
    assert post(client, email, key=None).json()["code"] == "bad_api_key"
    assert post(client, email, key="wrong").status_code == 401
    assert post(client, email, owner=None).json()["code"] == "owner_required"
    res = post(client, email, owner="nobody@agslogistics.com")
    assert res.status_code == 400 and res.json()["code"] == "unknown_owner"
    no_content = outlook_email({"name": "CI.pdf", "isInline": False})  # 'Include Attachments' was off
    assert post(client, no_content).json()["code"] == "attachments_without_content"
    assert post(client, {"subject": "no attachments"}).json()["code"] == "no_attachments"
    res = client.post("/api/intake/email", params={"owner": OWNER}, content=b"not json", headers={"X-API-Key": API_KEY})
    assert res.status_code == 400 and res.json()["code"] == "bad_json"
    monkeypatch.delenv("API_KEY")
    assert post(client, email).status_code == 503  # not set up on the server
    assert listed(client) == {}  # nothing was created


@pytest.mark.parametrize("name, text, civ", [
    ("CI-1.pdf", "ACME LTD COMMERCIAL INVOICE Invoice No.: 1 Description Qty Unit Price Amount HS Code", True),
    ("doc.pdf", "WEIGHT LIST ... (page 2) COMMERCIAL INVOICE Model Qty Amount", True),  # a weight list in front of the invoice
    ("export.xlsx", "EXPORT INVOICE Exporter: Invoice no & date Description Rate Amount HTS CODE", True),
    ("PL-1.pdf", "PACKING LIST Invoice No. : T-0001 Model Cartons Qty N.W. G.W.", False),  # names the invoice, is not one
    ("PI-7.pdf", "PROFORMA INVOICE Description Qty Unit Price Amount", False),
    ("AWB.pdf", "AIR WAYBILL Shipper Consignee", False),
    ("scan.pdf", "", False),
])
def test_which_attachment_is_the_commercial_invoice(name, text, civ):
    points, why = intake.score(name, text)
    assert (points >= intake.CIV_SCORE) is civ, (points, why)


def test_received_emails_are_deleted_with_the_documents(client):
    with db.session_scope() as s:
        owner = s.query(User).filter_by(email=OWNER).one()
        s.add(IntakeMessage(owner_id=owner.id, message_key="old", created_at=datetime.now(timezone.utc) - timedelta(days=200), attachments=[]))
        s.add(IntakeMessage(owner_id=owner.id, message_key="new", created_at=datetime.now(timezone.utc), attachments=[]))
        s.commit()
        documents.purge_expired(s)
        assert [m.message_key for m in s.query(IntakeMessage).all()] == ["new"]
