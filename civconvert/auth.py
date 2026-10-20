"""Login with the company Nimbus account, and the session that keeps the user logged in.

Login (same protocol as the HTS rule engine): the user types their Nimbus e-mail and password
on our page, we POST them to {NIMBUS_API_URL}/api/v2/userauth/auth/sign-in and accept the
answer when success is true and data has userId and token. The password is never stored or
logged, and the Nimbus token is not kept.

Session: a random token in an HttpOnly cookie (scripts cannot read it); only its SHA-256 is
stored in user_sessions, so logging out really ends it. Requests that change data must also
send the header X-CIV-Request: 1, which other websites cannot add (CSRF protection).
Failed logins are rate limited per e-mail and per IP address.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import timedelta
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import settings
from .db import as_utc, get_db, iso, utcnow
from .models import AuditLog, User, UserSession

log = logging.getLogger("civconvert")
router = APIRouter(prefix="/api/auth", tags=["auth"])

COOKIE = "civ_session"
COOKIE_MAX_AGE = 30 * 24 * 3600  # the server-side session decides; it expires after SESSION_TTL_HOURS without use
CSRF_HEADER = "X-CIV-Request"
SIGN_IN_PATH = "/api/v2/userauth/auth/sign-in"
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_transport: httpx.BaseTransport | None = None  # tests put a mock Nimbus here


class NimbusUnavailable(Exception):
    """Nimbus could not be reached or answered with a server error."""


# ───────────────────────────── Nimbus ─────────────────────────────


def nimbus_sign_in(email: str, password: str) -> dict[str, Any] | None:
    """The Nimbus account for these credentials, or None when they are wrong."""
    base = settings.nimbus_api_url()
    if not base:
        raise NimbusUnavailable("Nimbus login is not configured (NIMBUS_API_URL)")
    try:
        with httpx.Client(timeout=15, transport=_transport) as client:
            res = client.post(base + SIGN_IN_PATH, json={"email": email, "password": password})
    except httpx.HTTPError as e:
        raise NimbusUnavailable(f"{e.__class__.__name__}") from e
    if res.status_code >= 500:
        raise NimbusUnavailable(f"HTTP {res.status_code}")
    try:
        body = res.json()
    except ValueError:
        body = None
    if not res.is_success or not isinstance(body, dict) or body.get("success") is not True:
        return None
    data = body.get("data")
    if not isinstance(data, dict) or data.get("userId") is None or not isinstance(data.get("token"), str):
        return None
    name = next((data[k] for k in ("name", "userName", "nickName", "realName") if isinstance(data.get(k), str) and data[k].strip()), None)
    role = data.get("role") if isinstance(data.get("role"), str) and data["role"].strip() else None
    return {"user_id": str(data["userId"]), "role": role, "name": name}


# ───────────────────────────── rate limit ─────────────────────────────


class RateLimiter:
    """At most `limit` failures per `window` seconds per key (in this process)."""

    def __init__(self, limit: int, window: float):
        self.limit, self.window = limit, window
        self.hits: dict[str, deque[float]] = defaultdict(deque)
        self.lock = threading.Lock()

    def wait(self, key: str) -> float:
        """Seconds until the next attempt is allowed (0 = now)."""
        now = time.monotonic()
        with self.lock:
            q = self.hits[key]
            while q and q[0] <= now - self.window:
                q.popleft()
            return 0.0 if len(q) < self.limit else q[0] + self.window - now

    def fail(self, key: str) -> None:
        with self.lock:
            self.hits[key].append(time.monotonic())

    def reset(self, key: str) -> None:
        with self.lock:
            self.hits.pop(key, None)


failures_by_email = RateLimiter(5, 15 * 60)
failures_by_ip = RateLimiter(30, 15 * 60)


# ───────────────────────────── sessions ─────────────────────────────


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def client_ip(request: Request) -> str:
    return (request.client.host if request.client else "") or ""


def _https(request: Request) -> bool:
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    return request.url.scheme == "https" or proto == "https"


def _ttl() -> timedelta:
    return timedelta(hours=settings.session_ttl_hours())


def start_session(db: Session, user: User, request: Request, response: Response) -> None:
    token = secrets.token_urlsafe(32)
    now = utcnow()
    db.add(UserSession(
        user_id=user.id, token_hash=_hash(token), created_at=now, last_seen_at=now, expires_at=now + _ttl(),
        ip=client_ip(request)[:64], user_agent=(request.headers.get("user-agent") or "")[:255],
    ))
    response.set_cookie(COOKIE, token, max_age=COOKIE_MAX_AGE, httponly=True, samesite="lax", secure=_https(request), path="/")


def session_user(request: Request, db: Session) -> User | None:
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    s = db.scalar(select(UserSession).where(UserSession.token_hash == _hash(token)))
    now = utcnow()
    if s is None or s.revoked_at is not None or as_utc(s.expires_at) <= now or not s.user.is_active:
        return None
    if now - as_utc(s.last_seen_at) > timedelta(minutes=5):  # still working: keep the session alive
        s.last_seen_at, s.expires_at = now, now + _ttl()
        db.commit()
    return s.user


def require_user(request: Request, db: Annotated[Session, Depends(get_db)]) -> User:
    """FastAPI dependency: the logged-in user (401 if none). Writes need the X-CIV-Request header."""
    user = session_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="请先登录")
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get(CSRF_HEADER) != "1":
        raise HTTPException(status_code=403, detail=f"missing {CSRF_HEADER} header")
    return user


def user_json(user: User) -> dict[str, Any]:
    return {"id": user.id, "email": user.email, "name": user.name, "role": user.role, "last_login_at": iso(user.last_login_at)}


def _upsert_user(db: Session, email: str, name: str | None, nimbus: dict[str, Any] | None) -> User:
    user = db.scalar(select(User).where(User.email == email))
    if user is None:
        user = User(email=email, name=name or _name_from_email(email), role="user", is_active=True, created_at=utcnow())
        db.add(user)
    elif name:
        user.name = name
    if nimbus:
        user.nimbus_user_id, user.nimbus_role = nimbus["user_id"], nimbus.get("role")
    user.last_login_at = utcnow()
    db.flush()
    return user


def _name_from_email(email: str) -> str:
    """junjie.ma@… -> Junjie Ma (Nimbus does not send a name)."""
    local = email.split("@", 1)[0]
    return " ".join(p.capitalize() for p in re.split(r"[._-]+", local) if p) or email


# ───────────────────────────── endpoints ─────────────────────────────


class LoginIn(BaseModel):
    email: str = Field(max_length=255)
    password: str = Field(max_length=500)


class DevLoginIn(BaseModel):
    email: str = Field(max_length=255)
    name: str | None = Field(default=None, max_length=255)


@router.get("/config")
def auth_config() -> dict[str, bool]:
    """What the login page offers."""
    return {"nimbus": bool(settings.nimbus_api_url()), "dev_login": settings.dev_login_enabled()}


@router.post("/login")
def login(body: LoginIn, request: Request, response: Response, db: Annotated[Session, Depends(get_db)]) -> dict[str, Any]:
    email = body.email.strip().lower()
    if not _EMAIL.fullmatch(email) or not body.password:
        raise HTTPException(status_code=400, detail="请输入邮箱和密码")
    ip = client_ip(request)
    wait = max(failures_by_email.wait(email), failures_by_ip.wait(ip))
    if wait > 0:
        raise HTTPException(status_code=429, detail=f"登录失败次数过多，请 {int(wait // 60) + 1} 分钟后再试",
                            headers={"Retry-After": str(int(wait) + 1)})
    try:
        account = nimbus_sign_in(email, body.password)
    except NimbusUnavailable as e:
        log.warning("Nimbus login unavailable: %s", e)
        raise HTTPException(status_code=502, detail="暂时连接不到 Nimbus 账户系统，请稍后再试") from e
    if account is None:
        failures_by_email.fail(email)
        failures_by_ip.fail(ip)
        log.info("failed login for %s from %s", email, ip)
        raise HTTPException(status_code=401, detail="邮箱或密码不正确")
    failures_by_email.reset(email)
    user = _upsert_user(db, email, account.get("name"), account)
    if not user.is_active:
        raise HTTPException(status_code=403, detail="该账户已被停用")
    start_session(db, user, request, response)
    db.add(AuditLog(user_id=user.id, action="user.login", detail={"ip": ip, "via": "nimbus"}, created_at=utcnow()))
    db.commit()
    return {"user": user_json(user)}


@router.post("/dev-login", include_in_schema=False)
def dev_login(body: DevLoginIn, request: Request, response: Response, db: Annotated[Session, Depends(get_db)]) -> dict[str, Any]:
    """Local development only (AUTH_DEV_LOGIN=1): log in with an e-mail address, no password."""
    if not settings.dev_login_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    email = body.email.strip().lower()
    if not _EMAIL.fullmatch(email):
        raise HTTPException(status_code=400, detail="请输入邮箱")
    user = _upsert_user(db, email, (body.name or "").strip() or None, None)
    start_session(db, user, request, response)
    db.add(AuditLog(user_id=user.id, action="user.login", detail={"ip": client_ip(request), "via": "dev"}, created_at=utcnow()))
    db.commit()
    return {"user": user_json(user)}


@router.post("/logout")
def logout(request: Request, response: Response, db: Annotated[Session, Depends(get_db)]) -> dict[str, bool]:
    token = request.cookies.get(COOKIE)
    if token:
        s = db.scalar(select(UserSession).where(UserSession.token_hash == _hash(token)))
        if s is not None and s.revoked_at is None:
            s.revoked_at = utcnow()
            db.add(AuditLog(user_id=s.user_id, action="user.logout", detail={}, created_at=utcnow()))
            db.commit()
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
def me(user: Annotated[User, Depends(require_user)]) -> dict[str, Any]:
    return {"user": user_json(user)}


# ───────────────────────────── API key (Power Automate) ─────────────────────────────


def api_keys() -> list[str]:
    return [k.strip() for k in settings.env("API_KEY").split(",") if k.strip()]


def require_api_access(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    x_api_key: Annotated[str | None, Header()] = None,
) -> None:
    """The conversion API: a valid X-API-Key (Power Automate, scripts) or a logged-in user."""
    keys = api_keys()
    if x_api_key:
        if keys and any(hmac.compare_digest(x_api_key.encode(), k.encode()) for k in keys):
            return
        raise HTTPException(status_code=401, detail="Missing or invalid X-API-Key header")
    if session_user(request, db) is not None:
        if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get(CSRF_HEADER) != "1":
            raise HTTPException(status_code=403, detail=f"missing {CSRF_HEADER} header")
        return
    raise HTTPException(status_code=401, detail="Log in, or send a valid X-API-Key header")
