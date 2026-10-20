"""HTTP API of the documents (logged-in users; all users see all documents, the list defaults to "mine").

    POST /api/documents                  upload PDF(s) -> documents (status processing)
    GET  /api/documents                  list: status, q, created_from/to, mine, page
    GET  /api/documents/stats            dashboard counts
    GET  /api/documents/{id}             document, template columns, sheet, extraction, exports
    PUT  /api/documents/{id}/sheet       save the sheet {revision, rows, reason}
    POST /api/documents/{id}/export      CSV (or format=xlsx) of the saved sheet, recorded
    POST /api/documents/{id}/confirm     editing -> confirmed
    POST /api/documents/{id}/reopen      confirmed -> editing
    POST /api/documents/{id}/retry       failed -> processing (convert again)
    GET  /api/documents/{id}/files/{fid} original PDF
    GET  /api/documents/{id}/preview     data for the side-by-side Preview
    GET  /api/documents/{id}/log         audit log;  GET .../versions: saved versions
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from . import documents as docs
from . import storage
from .auth import require_user
from .db import get_db
from .models import DocumentFile, User
from .pipeline import InputFile

router = APIRouter(prefix="/api/documents", tags=["documents"])
CurrentUser = Annotated[User, Depends(require_user)]
DB = Annotated[Session, Depends(get_db)]


def _attachment(filename: str, inline: bool = False) -> dict[str, str]:
    fallback = filename.encode("ascii", "replace").decode().replace("?", "_").replace('"', "_")
    kind = "inline" if inline else "attachment"
    return {"Content-Disposition": f"{kind}; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename)}"}


@router.post("")
async def upload(
    user: CurrentUser,
    db: DB,
    files: Annotated[list[UploadFile], File(description="Commercial Invoice PDF(s)")],
    customer: Annotated[str | None, Form()] = None,
    merge: Annotated[bool | None, Form(description="true = the PDFs are parts of one invoice")] = None,
) -> dict[str, Any]:
    inputs = [InputFile(f.filename or "invoice.pdf", await f.read()) for f in files]

    def create() -> list[dict[str, Any]]:
        created = docs.create_documents(db, user, inputs, customer or None, merge)
        for d in created:
            db.refresh(d)  # the conversion may already have finished
        return [docs.doc_json(d) for d in created]

    return {"documents": await run_in_threadpool(create)}


@router.get("")
def list_documents(
    user: CurrentUser,
    db: DB,
    status: str | None = None,
    q: str | None = Query(default=None, max_length=200),
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    mine: bool = True,
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    docs.expire_stuck(db)
    return docs.list_documents(db, user, status=status, q=q, created_from=created_from, created_to=created_to, mine=mine, page=page, page_size=page_size)


@router.get("/stats")
def stats(user: CurrentUser, db: DB, mine: bool = True, since: datetime | None = None) -> dict[str, int]:
    """since: the start of "today" in the user's time zone."""
    return docs.stats(db, user, mine, since)


@router.get("/{doc_id}")
def detail(doc_id: str, user: CurrentUser, db: DB) -> dict[str, Any]:
    return docs.detail_json(db, docs.get_document(db, doc_id))


class SheetIn(BaseModel):
    revision: int | None = None
    rows: list[Any]
    reason: str = Field(default="auto", pattern="^(auto|manual)$")
    force: bool = False  # overwrite a newer version saved elsewhere (after the user chose to)


@router.put("/{doc_id}/sheet")
def save_sheet(doc_id: str, body: SheetIn, user: CurrentUser, db: DB) -> dict[str, Any]:
    doc = docs.get_document(db, doc_id)
    return docs.save_sheet(db, doc, user, body.rows, body.revision, body.reason, body.force)


@router.post("/{doc_id}/export")
def export(doc_id: str, user: CurrentUser, db: DB, format: str = "csv", revision: int | None = None) -> Response:
    filename, data, media = docs.export(db, docs.get_document(db, doc_id), user, format, revision)
    return Response(data, media_type=media, headers=_attachment(filename))


class ConfirmIn(BaseModel):
    revision: int | None = None


@router.post("/{doc_id}/confirm")
def confirm(doc_id: str, body: ConfirmIn, user: CurrentUser, db: DB) -> dict[str, Any]:
    return {"document": docs.doc_json(docs.confirm(db, docs.get_document(db, doc_id), user, body.revision))}


@router.post("/{doc_id}/reopen")
def reopen(doc_id: str, user: CurrentUser, db: DB) -> dict[str, Any]:
    return {"document": docs.doc_json(docs.reopen(db, docs.get_document(db, doc_id), user))}


class RetryIn(BaseModel):
    customer: str | None = None


@router.post("/{doc_id}/retry")
def retry(doc_id: str, body: RetryIn, user: CurrentUser, db: DB) -> dict[str, Any]:
    doc = docs.retry(db, docs.get_document(db, doc_id), user, body.customer or None)
    db.refresh(doc)
    return {"document": docs.doc_json(doc)}


@router.get("/{doc_id}/files/{file_id}")
def original(doc_id: str, file_id: int, user: CurrentUser, db: DB) -> Response:
    row = db.get(DocumentFile, file_id)
    if row is None or row.document_id != doc_id:
        raise docs.DocumentError(404, "文件不存在")
    try:
        data = storage.get(row)
    except storage.StorageError as e:
        raise docs.DocumentError(500, str(e)) from e
    return Response(data, media_type="application/pdf", headers=_attachment(row.filename, inline=True))


@router.get("/{doc_id}/preview")
def document_preview(doc_id: str, user: CurrentUser, db: DB) -> dict[str, Any]:
    return docs.preview_payload(db, docs.get_document(db, doc_id))


@router.get("/{doc_id}/log")
def audit_log(doc_id: str, user: CurrentUser, db: DB) -> dict[str, Any]:
    return {"log": docs.log_json(db, docs.get_document(db, doc_id))}


@router.get("/{doc_id}/versions")
def versions(doc_id: str, user: CurrentUser, db: DB) -> dict[str, Any]:
    return {"versions": docs.versions_json(db, docs.get_document(db, doc_id))}
