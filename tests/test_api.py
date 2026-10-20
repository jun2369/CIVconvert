import base64

from fastapi.testclient import TestClient

from civconvert.api import app
from conftest import SAMPLE, needs_sample

client = TestClient(app)


def test_health_and_customers(api_key):
    assert client.get("/healthz").json()["status"] == "ok"
    assert any(c["id"] == "istone" for c in client.get("/api/customers", headers=api_key).json())


def test_conversion_api_is_not_anonymous(monkeypatch):
    monkeypatch.delenv("API_KEY", raising=False)
    assert client.get("/api/customers").status_code == 401
    assert client.post("/api/convert", files={"files": ("x.pdf", b"%PDF-1.4", "application/pdf")}).status_code == 401


def test_api_key_is_enforced_when_set(monkeypatch):
    monkeypatch.setenv("API_KEY", "secret-1, secret-2")
    assert client.get("/api/info").json()["auth_required"] is True
    assert client.get("/api/customers").status_code == 401
    assert client.get("/api/customers", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/api/customers", headers={"X-API-Key": "secret-2"}).status_code == 200


def test_power_automate_excel_attachment(api_key):
    from test_generic import invoice_xlsx

    body = {"files": [{"name": "invoice.xlsx", "contentBytes": base64.b64encode(invoice_xlsx()).decode()},
                      {"name": "logo.png", "contentBytes": base64.b64encode(b"\x89PNG\r\n").decode()}]}
    data = client.post("/api/convert/json", json=body, headers=api_key).json()
    assert data["skipped"] == ["logo.png"]
    [result] = data["results"]
    assert result["filename"] == "CargoWise Input - invoice.csv" and result["customer"]["id"] == "auto"
    assert base64.b64decode(result["content_base64"]).decode().splitlines()[1].startswith("1,,,MLG0603P2N2BTZ10,INDUCTOR,8504500000,")


def test_not_a_pdf_is_a_clear_error(api_key):
    r = client.post("/api/convert", files={"files": ("x.pdf", b"hello", "application/pdf")}, headers=api_key)
    assert r.status_code == 422 and "not a PDF" in r.json()["error"]


@needs_sample
def test_upload_returns_csv_file(api_key):
    r = client.post("/api/convert", files={"files": (SAMPLE.name, SAMPLE.read_bytes(), "application/pdf")}, headers=api_key)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "CargoWise Input - 8100800394 Commercial Invoice.csv" in r.headers["content-disposition"]
    assert r.headers["x-status"] == "ok" and r.headers["x-line-count"] == "20"


@needs_sample
def test_power_automate_json_with_outlook_attachments(api_key):
    body = {
        "sender": "pako@example.com",
        "files": [  # shape of the Outlook trigger's attachments array
            {"name": SAMPLE.name, "contentBytes": base64.b64encode(SAMPLE.read_bytes()).decode(),
             "contentType": "application/pdf", "isInline": False, "size": 1},
            {"name": "logo.png", "contentBytes": base64.b64encode(b"\x89PNG\r\n").decode(), "isInline": True},
        ],
    }
    data = client.post("/api/convert/json", json=body, headers=api_key).json()
    assert data["status"] == "ok" and data["skipped"] == ["logo.png"]
    [result] = data["results"]
    assert result["filename"] == "CargoWise Input - 8100800394 Commercial Invoice.csv"
    csv_text = base64.b64decode(result["content_base64"]).decode()
    assert csv_text.splitlines()[1].startswith("1,,,CINSFAVS(CINSFAVS-01),Selfie Stick + Throwpod,9006910001,")
