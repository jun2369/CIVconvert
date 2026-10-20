"""Errors the API reports to the web page.

Each has an HTTP status, a stable `code` that the page translates (English / Chinese) with
`params`, and an English `message` that is shown as is when the page has no translation.
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    def __init__(
        self, status: int, code: str, message: str, params: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None, headers: dict[str, str] | None = None,
    ):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message
        self.params, self.extra, self.headers = params or {}, extra or {}, headers or {}

    def payload(self) -> dict[str, Any]:
        return {"status": "error", "code": self.code, "error": self.message, "params": self.params, **self.extra}
