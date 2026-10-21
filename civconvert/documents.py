"""Documents: one uploaded invoice (one or several PDF parts) -> conversion -> editable sheet -> CSV.

    status:  processing -> editing -> confirmed      (confirmed -> editing: reopened for changes)
             processing -> failed  -> processing     (converted again)

The original PDFs, what was read from them, the sheet, its versions, the exports and the audit
log all hang off documents.id.
"""

from __future__ import annotations

import calendar
import copy
import hashlib
import logging
import re
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.orm import Session, selectinload

from . import __version__, extract, jobs, preview, settings, sources, storage
from . import sheet as sheets
from .db import as_utc, iso, session_scope, utcnow
from .errors import AppError
from .models import (
    AUTO,
    CONFIRMED,
    EDITING,
    FAILED,
    MANUAL,
    PROCESSING,
    STATUSES,
    AuditLog,
    Document,
    DocumentExport,
    DocumentExtraction,
    DocumentFile,
    DocumentSheet,
    IntakeMessage,
    SheetVersion,
    User,
)
from .pipeline import ConversionError, InputFile, Result, convert
from .profiles import ConfigError, CustomerProfile, find_profile, load_template_profile
from .template import Template, load_template

log = logging.getLogger("civconvert")

MAX_FILE_MB = 25
VERSION_EVERY = timedelta(minutes=10)  # autosaves keep a snapshot at most this often
LOG_MERGE = timedelta(minutes=10)  # autosaves by the same user within this time are one log entry
STUCK_AFTER = timedelta(minutes=10)  # a conversion never takes this long: the server was restarted
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class DocumentError(AppError):
    """A document action that cannot be done (status, code for the page, English message)."""


def audit(db: Session, action: str, user_id: int | None, document: Document | None = None, **detail: Any) -> None:
    db.add(AuditLog(document_id=document.id if document else None, user_id=user_id, action=action, detail=detail, created_at=utcnow()))


def _stem(name: str) -> str:
    return sources.strip_extension(re.split(r"[\\/]", name)[-1])[:255] or "invoice"


# ───────────────────────────── upload & conversion ─────────────────────────────


def check_upload(files: list[InputFile]) -> None:
    if not files:
        raise DocumentError(422, "no_files", "No file uploaded")
    if len(files) > 1:  # one invoice file per upload, so files of different invoices are never mixed up
        raise DocumentError(422, "one_file_only", "Upload one file at a time")
    for f in files:
        if len(f.data) > MAX_FILE_MB * 1024 * 1024:
            raise DocumentError(422, "file_too_large", f"{f.name}: larger than {MAX_FILE_MB} MB", {"name": f.name, "mb": MAX_FILE_MB})
        if sources.kind(f.name, f.data) is None:
            raise DocumentError(422, "not_pdf", f"{f.name}: not a PDF or Excel file", {"name": f.name})


def create_document(db: Session, user: User, files: list[InputFile], customer: str | None,
                    intake: IntakeMessage | None = None) -> Document:
    """The uploaded invoice (PDF or Excel) becomes a document; the conversion runs in the background.
    `intake`: the e-mail it came from (Power Automate Cloud) – the document is then "auto" and in `user`'s list."""
    check_upload(files)
    [f] = files
    now = utcnow()
    doc = Document(
        name=_stem(f.name)[:255], status=PROCESSING, options={"customer": customer},
        created_by=user.id, created_at=now, updated_by=user.id, updated_at=now,
        source=AUTO if intake else MANUAL, intake_id=intake.id if intake else None,
    )
    db.add(doc)
    db.flush()
    content_type = sources.CONTENT_TYPES[sources.kind(f.name, f.data) or sources.PDF]
    row = DocumentFile(document_id=doc.id, position=0, filename=f.name[:255], content_type=content_type, created_at=now)
    storage.put(row, f.data)
    db.add(row)
    if intake:  # no person did it: received by e-mail
        audit(db, "document.created", None, doc, files=[f.name], customer=customer, source=AUTO, email_from=intake.sender, email_subject=intake.subject)
    else:
        audit(db, "document.created", user.id, doc, files=[f.name], customer=customer)
    db.commit()
    jobs.submit(process, doc.id)
    return doc


def extraction_data(result: Result) -> dict[str, Any]:
    """What was read from the PDF – kept so the document can be checked and previewed later."""
    geo = preview.geometry(result)
    fields = list(result.customer.table.columns)
    labels = next((p["labels"] for p in geo if p["labels"]), {})
    return {
        "customer": {"id": result.customer.id, "name": result.customer.name},
        "template": result.template.id,
        "document": result.document,
        "csv_filename": result.filename,
        "sources": result.sources,
        "checks": [asdict(c) for c in result.checks],
        "warnings": result.warnings,
        "flags": [asdict(f) for f in result.flags],
        "fields": [{"name": f, "label": labels.get(f) or f} for f in fields],
        "items": [{f: item.get(f, "") for f in fields} for item in result.items],
        "converted": [{c.letter: row[c.index] for c in result.columns} for row in result.rows],
        "geometry": geo,
    }


def process(doc_id: str) -> None:
    """Background job: convert the stored PDF(s) of a document."""
    with session_scope() as db:
        doc = db.get(Document, doc_id)
        if doc is None or doc.status != PROCESSING:
            return
        try:
            files = [InputFile(f.filename, storage.get(f)) for f in doc.files]
            result = convert(files, (doc.options or {}).get("customer") or None)
        except (ConversionError, ConfigError, storage.StorageError) as e:
            _fail(db, doc, str(e))
            return
        except Exception as e:  # never leave a document "processing"
            log.exception("converting document %s failed", doc_id)
            _fail(db, doc, f"Internal error {e.__class__.__name__}: {e}")
            return
        _store(db, doc, result)
        log.info("document %s converted: %s, %d lines", doc.id, result.customer.id, len(result.rows))


def _fail(db: Session, doc: Document, message: str) -> None:
    doc.status, doc.error, doc.updated_at = FAILED, message[:2000], utcnow()
    audit(db, "conversion.failed", doc.created_by, doc, error=message[:500])


def _store(db: Session, doc: Document, result: Result) -> None:
    template = result.template
    extraction = extraction_data(result)
    rows = sheets.rows_from_result(result)
    sheets.compute(rows, template)
    flags = sheets.validate(rows, template, result.customer, result.columns, extraction)
    now = utcnow()
    for model in (DocumentExtraction, DocumentSheet):  # an earlier attempt
        old = db.get(model, doc.id)
        if old is not None:
            db.delete(old)
    db.flush()
    db.add(DocumentExtraction(document_id=doc.id, data=extraction, engine_version=__version__, created_at=now))
    db.add(DocumentSheet(document_id=doc.id, revision=1, rows=rows, flags=flags, updated_by=doc.created_by, updated_at=now))
    db.add(SheetVersion(document_id=doc.id, revision=1, rows=rows, reason="initial", created_by=doc.created_by, created_at=now))
    doc.status, doc.error = EDITING, None
    doc.customer_id, doc.template_id = result.customer.id, template.id
    doc.invoice_no = (result.document.get("invoice_no") or None) and result.document["invoice_no"][:100]
    doc.invoice_date = (result.document.get("invoice_date") or None) and result.document["invoice_date"][:40]
    doc.line_count = len(rows)
    doc.needs_review = sheets.needs_review(flags, extraction)
    doc.updated_at = now
    audit(db, "conversion.succeeded", doc.created_by, doc, lines=len(rows), customer=result.customer.id, warnings=len(result.warnings))


def expire_stuck(db: Session) -> int:
    """Documents left 'processing' by a restarted server become 'failed' (they can be converted again)."""
    res = db.execute(
        update(Document)
        .where(Document.status == PROCESSING, Document.updated_at < utcnow() - STUCK_AFTER)
        .values(status=FAILED, error="Processing was interrupted (server restart) – convert it again", updated_at=utcnow())
    )
    db.commit()
    return res.rowcount or 0


def retry(db: Session, doc: Document, user: User, customer: str | None) -> Document:
    if doc.status != FAILED:
        raise DocumentError(409, "retry_only_failed", "Only a failed document can be converted again")
    doc.status, doc.error, doc.updated_at, doc.updated_by = PROCESSING, None, utcnow(), user.id
    doc.options = {**(doc.options or {}), "customer": customer}
    audit(db, "conversion.retried", user.id, doc, customer=customer)
    db.commit()
    jobs.submit(process, doc.id)
    return doc


# ───────────────────────────── the sheet ─────────────────────────────


def get_document(db: Session, doc_id: str, user: User) -> Document:
    """A document of this user – everyone only sees and changes their own documents."""
    doc = db.get(Document, doc_id)
    if doc is None or doc.created_by != user.id:
        raise DocumentError(404, "not_found", "Document not found")
    return doc


def _template(doc: Document) -> Template:
    try:
        return load_template(load_template_profile(doc.template_id or ""))
    except ConfigError as e:
        raise DocumentError(500, "template_unreadable", f"Template '{doc.template_id}' cannot be read: {e}", {"template": doc.template_id, "error": str(e)}) from e


def _profile(doc: Document, template: Template) -> tuple[CustomerProfile | None, list]:
    """The customer's profile – or the general reader's – and its mapped columns (None if the profile was removed since)."""
    try:
        profile = find_profile(doc.customer_id)
        return (profile, sheets.mapped_columns(profile, template)) if profile else (None, [])
    except ConfigError:
        return None, []


def _extraction(db: Session, doc: Document) -> dict[str, Any]:
    ex = db.get(DocumentExtraction, doc.id)
    return ex.data if ex else {}


def _sheet(db: Session, doc: Document, lock: bool = False) -> DocumentSheet:
    query = select(DocumentSheet).where(DocumentSheet.document_id == doc.id)
    sh = db.scalar(query.with_for_update() if lock else query)
    if sh is None:
        raise DocumentError(409, "no_sheet", "This document has no sheet yet")
    return sh


def save_sheet(db: Session, doc: Document, user: User, rows_in: Any, revision: int | None, reason: str = "auto", force: bool = False) -> dict[str, Any]:
    if doc.status == CONFIRMED:
        raise DocumentError(409, "completed_readonly", "The document is completed (read-only) – reopen it to make changes", extra={"status": doc.status})
    if doc.status != EDITING:
        raise DocumentError(409, "not_editable", "The document cannot be edited now", extra={"status": doc.status})
    sh = _sheet(db, doc, lock=True)  # one save at a time per document
    if not force and revision != sh.revision:
        who = db.get(User, sh.updated_by) if sh.updated_by else None
        raise DocumentError(409, "conflict", "This document was changed elsewhere", extra={
            "conflict": True, "revision": sh.revision, "updated_by": who.name if who else None, "updated_at": iso(sh.updated_at),
        })
    template = _template(doc)
    try:
        rows = sheets.clean_rows(rows_in, len(template.headers))
    except sheets.SheetError as e:
        raise DocumentError(400, "invalid_sheet", f"Invalid sheet data: {e}", {"error": str(e)}) from e
    sheets.compute(rows, template)
    change = sheets.diff(sh.rows or [], rows)
    if rows == sh.rows:  # nothing changed (e.g. Save pressed twice)
        return _saved(doc, sh)
    profile, mapped = _profile(doc, template)
    extraction = _extraction(db, doc)
    flags = sheets.validate(rows, template, profile, mapped, extraction)
    now = utcnow()
    sh.rows, sh.flags, sh.revision, sh.updated_by, sh.updated_at = rows, flags, sh.revision + 1, user.id, now
    doc.updated_at, doc.updated_by, doc.line_count = now, user.id, len(rows)
    doc.needs_review = sheets.needs_review(flags, extraction)
    last = db.scalar(select(func.max(SheetVersion.created_at)).where(SheetVersion.document_id == doc.id))
    if reason == "manual" or last is None or now - as_utc(last) >= VERSION_EVERY:
        db.add(SheetVersion(document_id=doc.id, revision=sh.revision, rows=rows, reason=reason, created_by=user.id, created_at=now))
    _log_save(db, doc, user, reason, change, sh.revision, now)
    db.commit()
    return _saved(doc, sh)


def _saved(doc: Document, sh: DocumentSheet) -> dict[str, Any]:
    return {
        "revision": sh.revision, "saved_at": iso(sh.updated_at), "rows": sh.rows, "flags": sh.flags,
        "needs_review": doc.needs_review, "line_count": len(sh.rows or []),
    }


def _log_save(db: Session, doc: Document, user: User, reason: str, change: dict[str, int], revision: int, now: datetime) -> None:
    last = db.scalar(select(AuditLog).where(AuditLog.document_id == doc.id).order_by(AuditLog.id.desc()).limit(1))
    if (
        reason == "auto" and last is not None and last.action == "sheet.saved" and last.user_id == user.id
        and last.detail.get("reason") == "auto" and now - as_utc(last.created_at) < LOG_MERGE
    ):
        d = dict(last.detail)
        for k in ("cells", "rows_added", "rows_deleted"):
            d[k] = d.get(k, 0) + change[k]
        d.update(revision=revision, saves=d.get("saves", 1) + 1, until=iso(now))
        last.detail = d
        return
    audit(db, "sheet.saved", user.id, doc, reason=reason, revision=revision, saves=1, until=iso(now), **change)


def export(db: Session, doc: Document, user: User, fmt: str = "csv", revision: int | None = None) -> tuple[str, bytes, str]:
    """The current sheet as the CargoWise CSV (or Excel) – same writer, headers and column order as before."""
    if doc.status not in (EDITING, CONFIRMED):
        raise DocumentError(409, "nothing_to_export", "This document has nothing to export yet")
    if fmt not in ("csv", "xlsx"):
        raise DocumentError(400, "bad_format", "format must be csv or xlsx")
    sh = _sheet(db, doc)
    if revision is not None and revision != sh.revision:
        raise DocumentError(409, "unsaved_export", "The sheet has unsaved changes – save before exporting", extra={"revision": sh.revision})
    template = _template(doc)
    _, mapped = _profile(doc, template)
    rows = copy.deepcopy(sh.rows or [])
    sheets.compute(rows, template)
    view = sheets.export_view(template, mapped, rows)
    csv_name = _extraction(db, doc).get("csv_filename") or f"CargoWise Input - {doc.name}.csv"
    if fmt == "csv":
        data, filename, media = view.csv_bytes(), csv_name, "text/csv; charset=utf-8"
    else:
        data, filename, media = view.xlsx_bytes(), re.sub(r"\.csv$", "", csv_name, flags=re.IGNORECASE) + ".xlsx", XLSX
    now = utcnow()
    final = doc.status == CONFIRMED
    db.add(DocumentExport(
        document_id=doc.id, revision=sh.revision, format=fmt, filename=filename[:255], sha256=hashlib.sha256(data).hexdigest(),
        size=len(data), row_count=len(rows), final=final, exported_by=user.id, exported_at=now,
    ))
    doc.last_exported_at = now
    doc.export_count = (doc.export_count or 0) + 1
    audit(db, "document.exported", user.id, doc, format=fmt, revision=sh.revision, final=final, filename=filename)
    db.commit()
    return filename, data, media


def confirm(db: Session, doc: Document, user: User, revision: int | None) -> Document:
    if doc.status != EDITING:
        raise DocumentError(409, "confirm_only_editing", "Only a document being edited can be completed")
    sh = _sheet(db, doc)
    if revision is not None and revision != sh.revision:
        raise DocumentError(409, "unsaved_confirm", "The sheet has unsaved changes – save before completing it", extra={"revision": sh.revision})
    now = utcnow()
    doc.status, doc.confirmed_at, doc.confirmed_by, doc.updated_at, doc.updated_by = CONFIRMED, now, user.id, now, user.id
    db.add(SheetVersion(document_id=doc.id, revision=sh.revision, rows=sh.rows, reason="confirm", created_by=user.id, created_at=now))
    audit(db, "document.confirmed", user.id, doc, revision=sh.revision, open_issues=len(sh.flags or []))
    db.commit()
    return doc


def reopen(db: Session, doc: Document, user: User) -> Document:
    if doc.status != CONFIRMED:
        raise DocumentError(409, "reopen_only_completed", "Only a completed document can be reopened")
    now = utcnow()
    doc.status, doc.confirmed_at, doc.confirmed_by, doc.updated_at, doc.updated_by = EDITING, None, None, now, user.id
    audit(db, "document.reopened", user.id, doc)
    db.commit()
    return doc


def delete_documents(db: Session, user: User, ids: list[str]) -> dict[str, Any]:
    """Delete some of the user's own documents for good – the original files, the sheet, its versions, exports and
    history (the database cascades). Documents of other users are not touched. Who deleted what is logged."""
    wanted = list(dict.fromkeys(ids))
    docs = db.scalars(select(Document).where(Document.id.in_(wanted), Document.created_by == user.id)).all() if wanted else []
    for d in docs:
        for f in d.files:
            if f.storage != "db":
                storage.delete(f)
    if docs:
        db.execute(delete(Document).where(Document.id.in_([d.id for d in docs])))
        audit(db, "documents.deleted", user.id, None, count=len(docs), documents=[{"id": d.id, "name": d.name} for d in docs][:100])
        db.commit()
        log.info("%s deleted %d document(s)", user.email, len(docs))
    found = {d.id for d in docs}
    return {"deleted": len(docs), "not_found": [i for i in wanted if i not in found]}


DETAILS = {"invoice_no": 100, "customer_name": 255}  # what the user may change in the list, and its longest length


def update_details(db: Session, doc: Document, user: User, changes: dict[str, str | None]) -> Document:
    """The invoice number and the customer name shown in the list, as the user writes them (empty = none;
    an empty customer name shows the detected customer again). The sheet and the export do not change."""
    if doc.status not in (EDITING, CONFIRMED):
        raise DocumentError(409, "details_not_editable", "The invoice number and customer can be changed once the invoice is converted")
    changed = {}
    for field, value in changes.items():
        value = " ".join((value or "").split()) or None
        if value and len(value) > DETAILS[field]:
            raise DocumentError(400, "detail_too_long", f"At most {DETAILS[field]} characters", {"max": DETAILS[field]})
        if value != getattr(doc, field):
            changed[field] = {"from": getattr(doc, field), "to": value}
            setattr(doc, field, value)
    if changed:
        doc.updated_at, doc.updated_by = utcnow(), user.id
        audit(db, "document.edited", user.id, doc, **changed)
        db.commit()
    return doc


# ───────────────────────────── lists & dashboard ─────────────────────────────


def _like(q: str) -> str:
    return "%" + q.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def list_documents(
    db: Session, user: User, *, status: str | None = None, q: str | None = None, created_from: datetime | None = None,
    created_to: datetime | None = None, page: int = 1, page_size: int = 50,
) -> dict[str, Any]:
    query = select(Document).where(Document.created_by == user.id)  # only one's own documents
    if status == "attention":  # failed, or needs a check
        query = query.where(or_(Document.status == FAILED, and_(Document.status == EDITING, Document.needs_review.is_(True))))
    elif status == "review":
        query = query.where(Document.status == EDITING, Document.needs_review.is_(True))
    elif status in STATUSES:
        query = query.where(Document.status == status)
    if q and q.strip():
        like = _like(q.strip())
        query = query.where(or_(func.lower(Document.name).like(like, escape="\\"), func.lower(Document.invoice_no).like(like, escape="\\")))
    if created_from:
        query = query.where(Document.created_at >= created_from)
    if created_to:
        query = query.where(Document.created_at < created_to)
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    page_size = max(1, min(page_size, 200))
    page = max(1, page)
    docs = db.scalars(
        query.options(selectinload(Document.files)).order_by(Document.updated_at.desc(), Document.id).offset((page - 1) * page_size).limit(page_size)
    ).all()
    return {"items": [doc_json(d) for d in docs], "total": total, "page": page, "page_size": page_size}


def stats(db: Session, user: User, since: datetime | None = None) -> dict[str, int]:
    scope = [Document.created_by == user.id]
    counts = {s: 0 for s in STATUSES}
    review = 0
    for status, flagged, n in db.execute(
        select(Document.status, Document.needs_review, func.count()).where(*scope).group_by(Document.status, Document.needs_review)
    ):
        counts[status] = counts.get(status, 0) + n
        if status == EDITING and flagged:
            review += n
    today = db.scalar(select(func.count()).select_from(Document).where(*scope, Document.created_at >= since)) if since else 0
    exported = db.scalar(select(func.count()).select_from(Document).where(*scope, Document.last_exported_at.is_not(None)))
    return {
        "today": today or 0,
        "total": sum(counts.values()),
        **counts,
        "review": review,
        "attention": counts[FAILED] + review,
        "exported": exported or 0,
    }


# ───────────────────────────── retention ─────────────────────────────


def months_before(when: datetime, months: int) -> datetime:
    """The same day `months` calendar months earlier (later for negative months), clamped to the month's end."""
    y, m = divmod(when.month - 1 - months, 12)
    year, month = when.year + y, m + 1
    return when.replace(year=year, month=month, day=min(when.day, calendar.monthrange(year, month)[1]))


def expires_at(doc: Document) -> datetime | None:
    months = settings.retention_months()
    return months_before(as_utc(doc.created_at), -months) if months and doc.created_at else None


def purge_expired(db: Session, now: datetime | None = None) -> int:
    """Delete documents uploaded more than DOCUMENT_RETENTION_MONTHS ago, with everything that belongs to
    them (PDFs, sheet, versions, exports, log – the database cascades), to keep the database small."""
    months = settings.retention_months()
    if not months:
        return 0
    cutoff = months_before(now or utcnow(), months)
    ids = list(db.scalars(select(Document.id).where(Document.created_at < cutoff)))
    for start in range(0, len(ids), 500):
        batch = ids[start : start + 500]
        for f in db.scalars(select(DocumentFile).where(DocumentFile.document_id.in_(batch), DocumentFile.storage != "db")):
            storage.delete(f)
        db.execute(delete(Document).where(Document.id.in_(batch)))
    if ids:
        audit(db, "documents.purged", None, None, count=len(ids), uploaded_before=iso(cutoff))
        log.info("deleted %d document(s) uploaded before %s (kept %d months)", len(ids), iso(cutoff), months)
    # the e-mails received by Power Automate Cloud are kept as long as the documents
    emails = db.execute(delete(IntakeMessage).where(IntakeMessage.created_at < cutoff)).rowcount or 0
    if emails:
        log.info("deleted %d received e-mail record(s) from before %s", emails, iso(cutoff))
    db.commit()
    return len(ids)


# ───────────────────────────── JSON ─────────────────────────────


def _user_brief(user: User | None) -> dict[str, Any] | None:
    return {"id": user.id, "name": user.name, "email": user.email} if user else None


def doc_json(doc: Document) -> dict[str, Any]:
    return {
        "id": doc.id,
        "name": doc.name,
        "invoice_no": doc.invoice_no,
        "invoice_date": doc.invoice_date,
        "customer_id": doc.customer_id,
        "customer_name": doc.customer_name,
        "source": doc.source or MANUAL,
        "email": {"from": doc.intake.sender, "subject": doc.intake.subject, "received_at": iso(doc.intake.received_at or doc.intake.created_at)}
        if doc.intake else None,
        "status": doc.status,
        "needs_review": doc.needs_review,
        "error": doc.error,
        "line_count": doc.line_count,
        "created_by": _user_brief(doc.creator),
        "created_at": iso(doc.created_at),
        "updated_at": iso(doc.updated_at),
        "confirmed_at": iso(doc.confirmed_at),
        "last_exported_at": iso(doc.last_exported_at),
        "export_count": doc.export_count,
        "expires_at": iso(expires_at(doc)),
        "files": [{"id": f.id, "filename": f.filename, "size": f.size} for f in doc.files],
    }


def detail_json(db: Session, doc: Document) -> dict[str, Any]:
    out: dict[str, Any] = {"document": doc_json(doc)}
    if doc.confirmed_by:
        out["document"]["confirmed_by"] = _user_brief(db.get(User, doc.confirmed_by))
    sh = db.get(DocumentSheet, doc.id)
    if sh is None:  # processing or failed
        return out
    template = _template(doc)
    _, mapped = _profile(doc, template)
    ex = _extraction(db, doc)
    info = sheets.template_info(template.id)
    out["template"] = {**info, "roles": sheets.column_roles(mapped)}
    out["sheet"] = {
        "revision": sh.revision, "rows": sh.rows, "flags": sh.flags, "updated_at": iso(sh.updated_at),
        "updated_by": _user_brief(db.get(User, sh.updated_by)) if sh.updated_by else None,
    }
    out["extraction"] = {k: ex.get(k) for k in ("customer", "document", "csv_filename", "checks", "warnings", "fields", "items", "converted")}
    exports = db.scalars(select(DocumentExport).where(DocumentExport.document_id == doc.id).order_by(DocumentExport.id.desc()).limit(20)).all()
    users = {u.id: u for u in db.scalars(select(User).where(User.id.in_({e.exported_by for e in exports})))} if exports else {}
    out["exports"] = [
        {"format": e.format, "filename": e.filename, "revision": e.revision, "final": e.final, "rows": e.row_count,
         "exported_at": iso(e.exported_at), "exported_by": _user_brief(users.get(e.exported_by))}
        for e in exports
    ]
    return out


def log_json(db: Session, doc: Document, limit: int = 200) -> list[dict[str, Any]]:
    entries = db.scalars(select(AuditLog).where(AuditLog.document_id == doc.id).order_by(AuditLog.id.desc()).limit(limit)).all()
    return [{"action": e.action, "detail": e.detail, "at": iso(e.created_at), "user": _user_brief(e.user)} for e in entries]


def versions_json(db: Session, doc: Document) -> list[dict[str, Any]]:
    rows = db.execute(
        select(SheetVersion.id, SheetVersion.revision, SheetVersion.reason, SheetVersion.created_at, User.name)
        .join(User, User.id == SheetVersion.created_by, isouter=True)
        .where(SheetVersion.document_id == doc.id).order_by(SheetVersion.id.desc()).limit(100)
    ).all()
    return [{"id": r.id, "revision": r.revision, "reason": r.reason, "at": iso(r.created_at), "by": r.name} for r in rows]


def preview_payload(db: Session, doc: Document) -> dict[str, Any]:
    """The Preview page for a saved document: the stored PDF on the left, the current sheet on the right."""
    sh = _sheet(db, doc)
    template = _template(doc)
    _, mapped = _profile(doc, template)
    ex = _extraction(db, doc)
    rows = copy.deepcopy(sh.rows or [])
    sheets.compute(rows, template)
    view = sheets.export_view(template, mapped, rows)
    try:
        files = [InputFile(f.filename, storage.get(f)) for f in doc.files]
        pg = preview.pages(files, ex.get("geometry") or [])
    except (storage.StorageError, extract.PdfError) as e:
        raise DocumentError(500, "pdf_unreadable", f"The original PDF cannot be read: {e}", {"error": str(e)}) from e
    src_lines, items, converted = pg["lines"], ex.get("items") or [], ex.get("converted") or []
    nowhere = {"page": None, "bbox": None, "cells": {}, "joined": 0}

    def src(row: dict[str, Any]) -> int | None:
        s = row.get("src")
        return s if isinstance(s, int) and 1 <= s <= len(src_lines) else None

    document = dict(ex.get("document") or {})  # the invoice number as it is now (it may have been changed in the list)
    if not doc.invoice_no:
        document.pop("invoice_no", None)
    elif doc.invoice_no != (document.get("invoice_no") or "")[:100]:
        document["invoice_no"] = doc.invoice_no
    customer = ex.get("customer") or {"id": doc.customer_id, "name": doc.customer_id}
    result = {
        "status": "review" if doc.needs_review else "ok",
        "filename": doc.name,
        "customer": {**customer, "label": doc.customer_name} if doc.customer_name else customer,
        "document": document,
        "sources": [f.filename for f in doc.files],
        "checks": ex.get("checks") or [],
        "warnings": [],
        "flags": sh.flags or [],
        "line_count": len(rows),
        "lines": [
            {
                "line_no": n,
                "values": {c.letter: view.rows[n - 1][c.index] for c in view.columns},
                "original": converted[src(r) - 1] if src(r) and src(r) <= len(converted) else {},
            }
            for n, r in enumerate(rows, 1)
        ],
    }
    return {
        "result": result,
        "preview": {
            **pg,
            "lines": [src_lines[src(r) - 1] if src(r) else nowhere for r in rows],
            "items": [items[src(r) - 1] if src(r) and src(r) <= len(items) else {} for r in rows],
            "fields": ex.get("fields") or [],
            "grid": preview.grid(view),
        },
    }
