"""E-mails passed on by Power Automate Cloud.

    POST /api/intake/email?owner=<OP e-mail>   X-API-Key   body: the e-mail (Outlook trigger output) as JSON
    GET  /api/intake/today?since=<local midnight>          the dashboard line "today received ..." (logged-in user)

Power Automate watches a mailbox (sender, folder ...) and posts every matching e-mail with its
attachments – the trigger's output as it is (triggerBody()), or {subject, from, message_id, attachments:
[{name, contentBytes}]}. Which attachments are the Commercial Invoice (CIV): PDF and Excel files only
(not logos, signatures, Word files ...), and of those the ones that read as an invoice – "Commercial
Invoice" in the text, prices, HS codes – not a packing list, bill of lading, proforma ... (see score()).
An e-mail with a single PDF / Excel file takes it unless it clearly is another document. The same invoice
attached twice (PDF and Excel) is converted once. Each CIV becomes a document in the owner's list,
created and converted in the background exactly like a manual upload (documents.create_document),
marked "auto". Every e-mail is recorded (intake_messages): the dashboard line, a repeated delivery of the
same e-mail by Power Automate creates nothing twice, and what became of each attachment can be looked up.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import auth, documents, extract, generic, sources
from .db import as_utc, get_db, iso, utcnow
from .errors import AppError
from .models import AUTO, Document, IntakeMessage, User
from .pipeline import InputFile
from .profiles import ConfigError, load_generic

log = logging.getLogger("civconvert")
router = APIRouter(prefix="/api/intake", tags=["intake"])

MAX_BODY_MB = 100  # the whole e-mail as JSON (attachments in base64)
MAX_ATTACHMENTS = 50
CIV_SCORE = 4  # at least this: the attachment is a Commercial Invoice
LONE_SCORE = 2  # the e-mail's only PDF / Excel file: this is enough
CIV, SKIPPED = "civ", "skipped"


class IntakeError(AppError):
    """The e-mail cannot be taken (status, code, English message)."""


@dataclass
class Attachment:
    name: str
    data: bytes
    content_type: str = ""
    inline: bool = False
    missing: bool = False  # no content was sent (an attached e-mail)


@dataclass
class Email:
    key: str  # SHA-256 of the Internet message id (or of the e-mail's content when there is none)
    sender: str | None
    subject: str | None
    received_at: datetime | None
    attachments: list[Attachment]


@dataclass
class Decision:
    attachment: Attachment
    kind: str | None  # pdf / xlsx / xls, or None: neither
    score: int = 0
    civ: bool = False
    reason: str = ""  # why, in English (details)
    code: str = ""  # why, for the page: civ, civ_lone, not_civ, not_pdf_excel, inline_picture, no_content, too_large, same_invoice, error
    invoice_no: str | None = None
    document_id: str | None = None

    def json(self) -> dict[str, Any]:
        return {"name": self.attachment.name, "size": len(self.attachment.data), "kind": self.kind, "score": self.score,
                "decision": CIV if self.civ else SKIPPED, "code": self.code, "reason": self.reason, "invoice_no": self.invoice_no,
                "document_id": self.document_id}


# ───────────────────────────── the e-mail ─────────────────────────────


def _pick(d: dict[str, Any], *names: str) -> Any:
    """The first of these keys that has a value (Outlook triggers name them camelCase or PascalCase)."""
    for n in names:
        if d.get(n) not in (None, ""):
            return d[n]
    return None


def _address(value: Any) -> str | None:
    if isinstance(value, dict):  # Microsoft Graph: {"emailAddress": {"name": ..., "address": ...}}
        value = (value.get("emailAddress") or value).get("address")
    return str(value).strip()[:255] if value else None


def _when(value: Any) -> datetime | None:
    try:
        return as_utc(datetime.fromisoformat(str(value).replace("Z", "+00:00"))) if value else None
    except ValueError:
        return None


def _decode(name: str, content: Any) -> bytes | None:
    """The attachment's bytes; None when it has no content (an attached e-mail, or attachments not included)."""
    if isinstance(content, dict):  # a Power Automate file object {"$content-type": ..., "$content": base64}
        content = content.get("$content") or content.get("contentBytes")
    if not isinstance(content, str):
        return None
    if content.startswith("data:") and "," in content:
        content = content.split(",", 1)[1]
    try:
        return base64.b64decode(content, validate=False)
    except (binascii.Error, ValueError) as e:
        raise IntakeError(400, "attachment_not_base64", f"{name}: the content is not base64", {"name": name}) from e


def parse_email(body: Any) -> Email:
    """The e-mail as Power Automate sends it: the Outlook trigger's output, or the short form."""
    if isinstance(body, dict) and isinstance(body.get("body"), dict) and "attachments" not in body:
        body = body["body"]  # the whole trigger output (headers + body) was sent
    if not isinstance(body, dict):
        raise IntakeError(400, "bad_email", "The request body must be the e-mail as a JSON object")
    items = _pick(body, "attachments", "Attachments", "files")
    if isinstance(items, str):  # the array was put in quotes
        try:
            items = json.loads(items)
        except ValueError:
            items = None
    if not isinstance(items, list):
        raise IntakeError(400, "no_attachments", "The e-mail has no 'attachments' list – in the trigger, set 'Include Attachments' to Yes")
    if len(items) > MAX_ATTACHMENTS:
        raise IntakeError(413, "too_many_attachments", f"More than {MAX_ATTACHMENTS} attachments", {"max": MAX_ATTACHMENTS})
    attachments = []
    for n, a in enumerate(items, 1):
        if not isinstance(a, dict):
            raise IntakeError(400, "bad_attachment", f"Attachment {n} is not an object")
        name = str(_pick(a, "name", "Name", "filename", "FileName") or f"attachment-{n}")[:255]
        data = _decode(name, _pick(a, "contentBytes", "ContentBytes", "content_base64", "$content", "content", "Content"))
        inline = _pick(a, "isInline", "IsInline") in (True, "true", "True")
        attachments.append(Attachment(name, data or b"", str(_pick(a, "contentType", "ContentType") or ""), inline, data is None))
    if attachments and all(a.missing for a in attachments):
        raise IntakeError(400, "attachments_without_content", "The attachments have no content – in the trigger, set 'Include Attachments' to Yes")
    sender = _address(_pick(body, "from", "From", "sender", "Sender"))
    subject = _pick(body, "subject", "Subject")
    subject = str(subject).strip()[:500] if subject else None
    received = _when(_pick(body, "receivedDateTime", "DateTimeReceived", "received_at", "receivedAt"))
    message_id = _pick(body, "internetMessageId", "InternetMessageId", "message_id", "messageId", "id", "Id", "MessageId")
    if message_id:
        key = hashlib.sha256(str(message_id).strip().encode()).hexdigest()
    else:  # no id: the same sender, subject, time and files are the same e-mail
        h = hashlib.sha256(f"{sender}|{subject}|{received}".encode())
        for a in attachments:
            h.update(hashlib.sha256(a.data).digest())
        key = h.hexdigest()
    return Email(key, sender, subject, received, attachments)


# ───────────────────────────── which attachments are the CIV ─────────────────────────────

_COMMERCIAL = re.compile(r"commercial\s*invoice|商业发票", re.I)
_INVOICE = re.compile(r"\binvoice\b|发票", re.I)
_INVOICE_TITLE = re.compile(r"\binvoice\b(?!\s*(?:no\b|number|num\b|#|date|value|amount|total|ref))|发票", re.I)  # not "Invoice No.:"
_PROFORMA = re.compile(r"pro\s*-?\s*forma", re.I)
_OTHER = re.compile(
    r"packing\s*(list|slip)|weight\s*(list|note)|装箱单|bill\s*of\s*lading|air\s*way\s*bill|\bawb\b|certificate\s*of\s*origin|"
    r"purchase\s*order|quotation|delivery\s*(note|order)|safety\s*data\s*sheet|\bmsds\b|arrival\s*notice|debit\s*note|"
    r"credit\s*note|statement\s*of\s*account", re.I)
_PRICES = re.compile(r"unit\s*price|\bamount\b|total\s*value|\brate\b|单价|金额", re.I)
_TARIFF = re.compile(r"\bhts\b|\bh\.?\s?s\.?\s*code|\bhsn\b|tariff|海关编码|商品编码", re.I)
_NAME_CIV = re.compile(r"commercial\s*invoice|invoice|\binv\b|\bciv?\b|发票", re.I)
_NAME_OTHER = re.compile(
    r"packing|\bp\s*l\b|装箱|\bb\s*l\b|lading|\bawb\b|waybill|certificate|\bcoo\b|msds|\bsds\b|\bp\s*o\b|purchase|"
    r"pro\s*forma|\bpi\b|quotation|\bquote\b|label|manifest|contract|arrival|debit|credit|statement", re.I)


def document_text(name: str, data: bytes, kind: str) -> tuple[str, str]:
    """The text of the first pages of a PDF / of a workbook's sheets, and (Excel) its labels with the value below."""
    if kind == sources.PDF:
        with extract.open_pdf(data, name, ocr_limit=3) as pdf:  # a scanned PDF: its first pages by OCR
            return "\n".join(page.extract_text() or "" for page in pdf.pages[:3]), ""
    grids = sources.read_workbook(data, name, kind)
    return "\n\n".join(g.text() for g in grids), generic.label_values(grids)


def score(name: str, text: str) -> tuple[int, list[str]]:
    """How much an attachment looks like a Commercial Invoice (>= CIV_SCORE: it is one), and why."""
    flat = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text))
    head, body = flat[:800], flat[:30000]  # the title is at the top
    points, why = 0, []
    if _COMMERCIAL.search(body):  # a packing / weight list in front of it does not matter: the file has the invoice
        points, why = points + 6, why + ["'Commercial Invoice' in the text"]
    elif _PROFORMA.search(head):
        points, why = points - 6, why + ["a proforma invoice"]
    elif _INVOICE_TITLE.search(head):
        points, why = points + 4, why + ["'Invoice' in the title"]
    elif _OTHER.search(head):
        points, why = points - 6, why + [f"the title is '{_OTHER.search(head).group(0)}'"]
    elif _INVOICE.search(body):
        points, why = points + 2, why + ["'Invoice' in the text"]
    elif _OTHER.search(body):
        points, why = points - 3, why + [f"'{_OTHER.search(body).group(0)}' in the text"]
    if _PRICES.search(body):
        points, why = points + 2, why + ["prices / amounts"]
    if _TARIFF.search(body):
        points, why = points + 1, why + ["HS codes"]
    words = re.sub(r"[_\-.()\[\]+,]+", " ", sources.strip_extension(name))
    if _NAME_CIV.search(words):
        points, why = points + 3, why + ["the file name says invoice"]
    elif _NAME_OTHER.search(words):
        points, why = points - 3, why + [f"the file name says '{_NAME_OTHER.search(words).group(0)}'"]
    return points, why


def choose(attachments: list[Attachment]) -> list[Decision]:
    """Which attachments are the e-mail's Commercial Invoice(s)."""
    spec = None
    decisions = []
    for a in attachments:
        found = None if a.missing else sources.kind(a.name, a.data)
        d = Decision(a, None if found == sources.IMAGE else found)  # pictures in e-mails (logos, signatures) are not read
        decisions.append(d)
        if d.kind is None:
            d.code = "no_content" if a.missing else "inline_picture" if a.inline else "not_pdf_excel"
            d.reason = {"no_content": "no content (an attached e-mail?)", "inline_picture": "an inline picture (logo, signature)",
                        "not_pdf_excel": "not a PDF or Excel file"}[d.code]
            continue
        if len(a.data) > documents.MAX_FILE_MB * 1024 * 1024:
            d.kind, d.code, d.reason = None, "too_large", f"larger than {documents.MAX_FILE_MB} MB"
            continue
        try:
            text, more = document_text(a.name, a.data, d.kind)
        except Exception as e:  # noqa: BLE001 – unreadable: judged by its name; the conversion reports the error
            text, more = "", ""
            log.info("intake: %s cannot be read: %s: %s", a.name, e.__class__.__name__, e)
        d.score, why = score(a.name, text)
        if not text.strip():
            why.append("no text (a scan?)")
        d.civ = d.score >= CIV_SCORE
        d.reason = "; ".join(why) or "nothing that looks like an invoice"
        d.code = "civ" if d.civ else "not_civ"
        if d.civ and text:
            try:
                spec = spec or load_generic()
                d.invoice_no = generic.document_fields(text, spec, more).get("invoice_no")
            except ConfigError:
                pass
    files = [d for d in decisions if d.kind]
    if not any(d.civ for d in files) and len(files) == 1 and files[0].score >= LONE_SCORE:
        files[0].civ = True  # the only PDF / Excel file of the e-mail, something looks like an invoice, nothing says it is not
        files[0].reason = f"the only PDF / Excel attachment ({files[0].reason})"
        files[0].code = "civ_lone"
    # the same invoice attached twice (PDF and Excel): converted once – the higher score, else the PDF
    kept: dict[str, Decision] = {}
    for d in sorted((d for d in files if d.civ and d.invoice_no), key=lambda d: (-d.score, d.kind != sources.PDF)):
        if d.invoice_no in kept:
            d.civ, d.code, d.reason = False, "same_invoice", f"the same invoice {d.invoice_no} as {kept[d.invoice_no].attachment.name}"
        else:
            kept[d.invoice_no] = d
    return decisions


# ───────────────────────────── receiving ─────────────────────────────


def owner_user(db: Session, email: str | None) -> User:
    """The OP whose list the documents go to: a CIV Converter user (logged in at least once)."""
    address = (email or "").strip().lower()
    if not address:
        raise IntakeError(400, "owner_required", "Say whose documents these are: ?owner=<the OP's e-mail> in the URL")
    user = db.scalar(select(User).where(User.email == address))
    if user is None or not user.is_active:
        raise IntakeError(400, "unknown_owner", f"{address} is not a CIV Converter user – log in once with this account first",
                          {"owner": address})
    return user


def _summary(row: IntakeMessage, duplicate: bool = False) -> dict[str, Any]:
    status = "duplicate" if duplicate else "ok" if row.civ_count else "no_civ"
    return {
        "status": status,
        "email": {"id": row.id, "from": row.sender, "subject": row.subject, "received_at": iso(row.received_at)},
        "attachments": row.attachments or [],
        "documents": [a["document_id"] for a in row.attachments or [] if a.get("document_id")],
    }


def receive(db: Session, owner: User, email: Email) -> dict[str, Any]:
    """Record the e-mail and turn each of its CIVs into a document (converted in the background)."""
    row = IntakeMessage(owner_id=owner.id, message_key=email.key, sender=email.sender, subject=email.subject,
                        received_at=email.received_at, created_at=utcnow(), attachment_count=len(email.attachments), attachments=[])
    db.add(row)
    try:
        db.flush()
    except IntegrityError:  # Power Automate sent this e-mail before (a retry): nothing is created twice
        db.rollback()
        old = db.scalar(select(IntakeMessage).where(IntakeMessage.owner_id == owner.id, IntakeMessage.message_key == email.key))
        log.info("intake: e-mail %s for %s was received before", old.id if old else "?", owner.email)
        return _summary(old, duplicate=True) if old else {"status": "duplicate"}
    decisions = choose(email.attachments)
    for d in decisions:
        if not d.civ:
            continue
        try:
            doc = documents.create_document(db, owner, [InputFile(d.attachment.name, d.attachment.data)], None, intake=row)
            d.document_id = doc.id
        except documents.DocumentError as e:
            d.civ, d.code, d.reason = False, "error", e.message
    row.attachments = [d.json() for d in decisions]
    row.civ_count = sum(1 for d in decisions if d.document_id)
    db.commit()
    log.info("intake: e-mail %s from %s (%r) for %s: %d attachment(s), %d CIV", row.id, row.sender, row.subject, owner.email,
             row.attachment_count, row.civ_count)
    return _summary(row)


def today(db: Session, user: User, since: datetime | None) -> dict[str, Any]:
    """The e-mails received for this user since the start of their day."""
    rows = db.scalars(select(IntakeMessage).where(IntakeMessage.owner_id == user.id, IntakeMessage.created_at >= since)
                      .order_by(IntakeMessage.id.desc()).limit(200)).all() if since else []
    ever = bool(rows) or db.scalar(select(func.count()).select_from(IntakeMessage).where(IntakeMessage.owner_id == user.id)) > 0
    names = {i: n for i, n in db.execute(select(Document.id, Document.name).where(Document.intake_id.in_([r.id for r in rows])))} if rows else {}
    return {
        "ever": ever,
        "emails": len(rows),
        "attachments": sum(r.attachment_count for r in rows),
        "civ": sum(r.civ_count for r in rows),
        "no_civ": sum(1 for r in rows if not r.civ_count),
        "items": [{**_summary(r), "at": iso(r.created_at),
                   "attachments": [{**a, "document_name": names.get(a.get("document_id")),
                                    "document_deleted": bool(a.get("document_id")) and a.get("document_id") not in names}
                                   for a in r.attachments or []]} for r in rows],
    }


# ───────────────────────────── HTTP ─────────────────────────────


def require_api_key(x_api_key: Annotated[str | None, Header()] = None) -> None:
    """Power Automate: the X-API-Key header must be one of API_KEY (a logged-in browser cannot post e-mails)."""
    keys = auth.api_keys()
    if not keys:
        raise IntakeError(503, "intake_not_configured", "API_KEY is not set on the server")
    if not x_api_key or not any(hmac.compare_digest(x_api_key.encode(), k.encode()) for k in keys):
        raise IntakeError(401, "bad_api_key", "Missing or invalid X-API-Key header")


DB = Annotated[Session, Depends(get_db)]


@router.post("/email", dependencies=[Depends(require_api_key)])
async def receive_email(request: Request, db: DB, owner: str | None = None) -> dict[str, Any]:
    raw = await request.body()
    if len(raw) > MAX_BODY_MB * 1024 * 1024:
        raise IntakeError(413, "email_too_large", f"The e-mail is larger than {MAX_BODY_MB} MB", {"mb": MAX_BODY_MB})
    try:
        body = json.loads(raw or b"null")
    except ValueError as e:
        raise IntakeError(400, "bad_json", "The request body is not JSON – in the HTTP action, put the e-mail in Body") from e
    email = parse_email(body)
    user = owner_user(db, owner or (body.get("owner") if isinstance(body, dict) else None))
    return await run_in_threadpool(receive, db, user, email)


@router.get("/today")
def received_today(user: Annotated[User, Depends(auth.require_user)], db: DB, since: datetime | None = None) -> dict[str, Any]:
    """since: the start of "today" in the user's time zone."""
    return today(db, user, as_utc(since) if since else None)
