"""Database tables. Everything about one invoice hangs off documents.id.

Only portable column types (String, Text, Integer, Boolean, DateTime, JSON, LargeBinary),
so the schema works the same on PostgreSQL, MySQL and SQLite.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, LargeBinary, MetaData, String, Text
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .db import utcnow

# document status flow:  processing -> editing -> confirmed  (confirmed -> editing when reopened)
#                         processing -> failed -> processing (retry)
PROCESSING, EDITING, CONFIRMED, FAILED = "processing", "editing", "confirmed", "failed"
STATUSES = (PROCESSING, EDITING, CONFIRMED, FAILED)


def new_id() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    # fixed constraint names, the same on every database – later migrations can refer to them
    metadata = MetaData(naming_convention={
        "ix": "ix_%(column_0_label)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    })
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)  # lower case
    name: Mapped[str] = mapped_column(String(255))
    nimbus_user_id: Mapped[str | None] = mapped_column(String(64))
    nimbus_role: Mapped[str | None] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(32), default="user")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UserSession(Base):
    """A login. The browser holds a random token in an HttpOnly cookie; only its SHA-256 is stored."""

    __tablename__ = "user_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(255))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(lazy="joined")


class Document(Base):
    """One uploaded invoice (one or several PDF parts) and its CargoWise sheet."""

    __tablename__ = "documents"
    __table_args__ = (Index("ix_documents_status_updated", "status", "updated_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(255))
    invoice_no: Mapped[str | None] = mapped_column(String(100), index=True)
    invoice_date: Mapped[str | None] = mapped_column(String(40))
    customer_id: Mapped[str | None] = mapped_column(String(100))
    template_id: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20), default=PROCESSING, index=True)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text)
    line_count: Mapped[int] = mapped_column(Integer, default=0)
    options: Mapped[dict[str, Any]] = mapped_column(default=dict)  # upload options (customer, merge)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    confirmed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_exported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    export_count: Mapped[int] = mapped_column(Integer, default=0)

    creator: Mapped[User] = relationship(foreign_keys=[created_by], lazy="joined")
    files: Mapped[list[DocumentFile]] = relationship(
        back_populates="document", order_by="DocumentFile.position", cascade="all, delete-orphan"
    )


class DocumentFile(Base):
    """An original PDF. The bytes are in `data` (FILE_STORAGE=db) or in external storage under `storage_key`."""

    __tablename__ = "document_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(100), default="application/pdf")
    size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    storage: Mapped[str] = mapped_column(String(20), default="db")
    storage_key: Mapped[str | None] = mapped_column(String(500))
    data: Mapped[bytes | None] = mapped_column(LargeBinary().with_variant(mysql.LONGBLOB(), "mysql"), deferred=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    document: Mapped[Document] = relationship(back_populates="files")


class DocumentExtraction(Base):
    """What was read from the PDF: line items, totals, checks, where each line is on the page."""

    __tablename__ = "document_extractions"

    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True)
    data: Mapped[dict[str, Any]] = mapped_column(default=dict)
    engine_version: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DocumentSheet(Base):
    """The editable CargoWise sheet. rows = [{id, src, cells: {"<column index>": {"v": value, "f": formula}}}]."""

    __tablename__ = "document_sheets"

    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    rows: Mapped[list[Any]] = mapped_column(default=list)
    flags: Mapped[list[Any]] = mapped_column(default=list)  # validation results of this revision
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SheetVersion(Base):
    """Snapshots: the converted sheet, manual saves, confirmations and every 10 minutes of autosaving."""

    __tablename__ = "sheet_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    rows: Mapped[list[Any]] = mapped_column(default=list)
    reason: Mapped[str] = mapped_column(String(20))  # initial | manual | auto | confirm
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DocumentExport(Base):
    __tablename__ = "document_exports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    format: Mapped[str] = mapped_column(String(10))  # csv | xlsx
    filename: Mapped[str] = mapped_column(String(255))
    sha256: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(Integer)
    row_count: Mapped[int] = mapped_column(Integer)
    final: Mapped[bool] = mapped_column(Boolean, default=False)  # exported after Confirm
    exported_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    exported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[str | None] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(40), index=True)
    detail: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    user: Mapped[User | None] = relationship(lazy="joined")
