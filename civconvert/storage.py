"""Where the original PDFs are kept.

FILE_STORAGE=db (default): inside the database, so one database backup holds everything.
FILE_STORAGE=local: files under LOCAL_FILE_DIR (e.g. a mounted volume).
Each file row records where its bytes are, so changing FILE_STORAGE later still reads older
files. Object storage (S3 …) would be one more branch in put() / get().
"""

from __future__ import annotations

import hashlib

from .models import DocumentFile
from .settings import file_storage, local_file_dir


class StorageError(Exception):
    pass


def put(row: DocumentFile, data: bytes) -> None:
    """Store the bytes of a new file row (document_id and position must be set)."""
    row.size = len(data)
    row.sha256 = hashlib.sha256(data).hexdigest()
    kind = file_storage()
    if kind == "db":
        row.storage, row.storage_key, row.data = "db", None, data
    elif kind == "local":
        key = f"{row.document_id}/{row.position:03d}-{row.sha256[:16]}.pdf"
        path = local_file_dir() / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        row.storage, row.storage_key, row.data = "local", key, None
    else:
        raise StorageError(f"unknown FILE_STORAGE '{kind}' – use 'db' or 'local'")


def get(row: DocumentFile) -> bytes:
    if row.storage == "db":
        data = row.data or b""
    elif row.storage == "local" and row.storage_key:
        path = local_file_dir() / row.storage_key
        if not path.is_file():
            raise StorageError(f"{row.filename}: file not found in {local_file_dir()}")
        data = path.read_bytes()
    else:
        raise StorageError(f"{row.filename}: unknown storage '{row.storage}'")
    if hashlib.sha256(data).hexdigest() != row.sha256:
        raise StorageError(f"{row.filename}: the stored file is damaged (checksum mismatch)")
    return data
