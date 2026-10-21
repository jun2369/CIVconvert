import json

import httpx
import pytest
from sqlalchemy import select

from civconvert import auth, db
from civconvert.models import User, UserSession
from conftest import login

GOOD = {"email": "junjie.ma@agslogistics.com", "password": "right-password"}


@pytest.fixture
def nimbus(monkeypatch):
    """A fake Nimbus sign-in endpoint; `calls` records what our server sent."""
    calls = []
    state = {"mode": "ok", "accounts": [GOOD]}  # Nimbus compares the e-mail as it was registered

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append({"url": str(request.url), "body": body})
        if state["mode"] == "down":
            raise httpx.ConnectError("no route to host")
        if state["mode"] == "500":
            return httpx.Response(500, text="oops")
        if body in state["accounts"]:
            return httpx.Response(200, json={"success": True, "data": {"userId": 4711, "token": "nimbus-jwt", "role": "OP", "refreshAt": 1, "expireAt": 2}})
        return httpx.Response(200, json={"success": False, "message": "wrong password"})

    monkeypatch.setenv("NIMBUS_API_URL", "https://nimbus.example")
    monkeypatch.setattr(auth, "_transport", httpx.MockTransport(handler))
    auth.failures_by_email.hits.clear()
    auth.failures_by_ip.hits.clear()
    return {"calls": calls, "state": state}


def test_nimbus_login_creates_the_user_and_a_session(app_client, nimbus):
    res = app_client.post("/api/auth/login", json={**GOOD, "email": "Junjie.Ma@agslogistics.com"})
    assert res.status_code == 200, res.text
    assert res.json()["user"]["email"] == "junjie.ma@agslogistics.com"
    assert res.json()["user"]["name"] == "Junjie Ma"
    url = "https://nimbus.example/api/v2/userauth/auth/sign-in"
    # as typed first (Nimbus does not know "Junjie.Ma@..."), then in lower case
    assert nimbus["calls"] == [{"url": url, "body": {**GOOD, "email": "Junjie.Ma@agslogistics.com"}}, {"url": url, "body": GOOD}]
    cookie = res.headers["set-cookie"]
    assert "civ_session=" in cookie and "HttpOnly" in cookie and "samesite=lax" in cookie.lower()
    assert app_client.get("/api/auth/me").json()["user"]["email"] == GOOD["email"]
    with db.session_scope() as s:
        user = s.scalar(select(User).where(User.email == GOOD["email"]))
        assert (user.nimbus_user_id, user.nimbus_role) == ("4711", "OP")
        session = s.scalar(select(UserSession).where(UserSession.user_id == user.id))
        token = app_client.cookies.get("civ_session")
        assert session.token_hash != token and len(session.token_hash) == 64  # only the hash is stored


def test_wrong_password_and_rate_limit(app_client, nimbus):
    for _ in range(5):
        res = app_client.post("/api/auth/login", json={**GOOD, "password": "nope"})
        assert res.status_code == 401 and res.json()["code"] == "wrong_credentials"
    res = app_client.post("/api/auth/login", json=GOOD)  # even the right password waits now
    assert res.status_code == 429 and "Retry-After" in res.headers
    assert len(nimbus["calls"]) == 5


def test_an_account_registered_with_capitals(app_client, nimbus):
    """Nimbus knows the account as "Cindy.guan@...": the e-mail is sent as typed, like the Nimbus login page
    sends it (it used to be sent in lower case, which Nimbus refused); our user is the lower-case address."""
    cindy = {"email": "Cindy.guan@agslogistics.com", "password": "her-password"}
    nimbus["state"]["accounts"].append(cindy)
    res = app_client.post("/api/auth/login", json=cindy)
    assert res.status_code == 200, res.text
    assert res.json()["user"]["email"] == "cindy.guan@agslogistics.com"
    assert [c["body"] for c in nimbus["calls"]] == [cindy]  # one call, nothing changed


def test_wrong_password_typed_with_capitals_is_one_failed_attempt(app_client, nimbus):
    res = app_client.post("/api/auth/login", json={"email": "Junjie.Ma@agslogistics.com", "password": "nope"})
    assert res.status_code == 401 and res.json()["code"] == "wrong_credentials"
    assert [c["body"]["email"] for c in nimbus["calls"]] == ["Junjie.Ma@agslogistics.com", "junjie.ma@agslogistics.com"]
    assert len(auth.failures_by_email.hits["junjie.ma@agslogistics.com"]) == 1


def test_nimbus_unreachable_is_not_a_wrong_password(app_client, nimbus):
    nimbus["state"]["mode"] = "down"
    assert app_client.post("/api/auth/login", json=GOOD).status_code == 502
    nimbus["state"]["mode"] = "500"
    assert app_client.post("/api/auth/login", json=GOOD).status_code == 502
    assert auth.failures_by_email.wait(GOOD["email"]) == 0  # not counted as failed attempts


def test_logout_ends_the_session(user_client):
    assert user_client.get("/api/auth/me").status_code == 200
    token = user_client.cookies.get("civ_session")
    user_client.post("/api/auth/logout")
    user_client.cookies.set("civ_session", token)  # the old cookie no longer works
    assert user_client.get("/api/auth/me").status_code == 401


def test_writes_need_the_csrf_header(user_client):
    del user_client.headers["X-CIV-Request"]
    res = user_client.post("/api/documents", files={"files": ("a.pdf", b"%PDF-1.4", "application/pdf")})
    assert res.status_code == 403


def test_pages_need_login(app_client):
    res = app_client.get("/", follow_redirects=False)
    assert res.status_code == 303 and res.headers["location"] == "/login?next=/"
    res = app_client.get("/documents/abc", follow_redirects=False)
    assert res.headers["location"] == "/login?next=/documents/abc"
    assert app_client.get("/login").status_code == 200
    login(app_client)
    assert app_client.get("/").status_code == 200
    assert app_client.get("/login?next=//evil.example", follow_redirects=False).headers["location"] == "/"


def test_dev_login_is_off_unless_enabled(app_client, monkeypatch):
    monkeypatch.delenv("AUTH_DEV_LOGIN")
    assert app_client.post("/api/auth/dev-login", json={"email": "x@y.com"}).status_code == 404
    assert app_client.get("/api/auth/config").json()["dev_login"] is False


def test_expired_session(user_client):
    with db.session_scope() as s:
        for sess in s.scalars(select(UserSession)):
            sess.expires_at = db.utcnow().replace(year=2000)
    assert user_client.get("/api/auth/me").status_code == 401
