"""HTTP API and web pages.

    local:      python -m civconvert serve
    container:  uvicorn civconvert.api:app --host 0.0.0.0 --port 8080

Pages (login with the Nimbus account): /login, / (dashboard), /documents/{id} (online sheet).
The schema of the database (DATABASE_URL) is migrated on start.

Conversion API – a logged-in user, or X-API-Key = one of API_KEY (comma-separated):
    POST /api/convert        multipart upload -> CSV file, or JSON with format=json
    POST /api/convert/json   base64 JSON in/out, accepts Outlook attachments as-is (Power Automate)
    POST /api/preview        the PDF(s) of one invoice -> result + page images for the review page
E-mails from Power Automate Cloud (X-API-Key): see intake.py.  Documents API: see documents_api.py.  Login: see auth.py.
"""

import asyncio
import base64
import binascii
import io
import logging
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import AliasChoices, BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from . import __version__, auth, db, documents, documents_api, intake, preview, settings, sources
from .errors import AppError
from .pipeline import ConversionError, Failure, InputFile, Result, convert, convert_many
from .profiles import ConfigError, load_customers

STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_FILES = 20
MAX_FILE_MB = 25

log = logging.getLogger("civconvert")
if not log.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s civconvert: %(message)s"))
    log.addHandler(_handler)
    log.setLevel(logging.INFO)



HOUSEKEEPING_EVERY = 6 * 3600  # seconds


def _housekeeping_once() -> None:
    with db.session_scope() as session:
        documents.purge_expired(session)  # documents older than DOCUMENT_RETENTION_MONTHS


async def _housekeeping() -> None:
    while True:
        try:
            await run_in_threadpool(_housekeeping_once)
        except Exception:
            log.exception("housekeeping failed")
        await asyncio.sleep(HOUSEKEEPING_EVERY)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await run_in_threadpool(db.migrate)
    with db.session_scope() as session:
        if n := documents.expire_stuck(session):
            log.warning("%d document(s) were left 'processing' by a restart and are now 'failed'", n)
    if not auth.api_keys():
        log.info("API_KEY is not set: the conversion API is only open to logged-in users")
    if settings.dev_login_enabled():
        log.warning("AUTH_DEV_LOGIN is on: anyone can log in with just an e-mail address (development only)")
    housekeeping = asyncio.create_task(_housekeeping())
    yield
    housekeeping.cancel()


app = FastAPI(title="CIVconvert", version=__version__, description="Commercial Invoice PDF -> CargoWise CSV", lifespan=lifespan)
app.include_router(auth.router)
app.include_router(documents_api.router)
app.include_router(intake.router)
NO_CACHE = {"Cache-Control": "no-cache"}  # revalidate the page and its scripts, so an update is never half-loaded


class _Static(StaticFiles):
    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers.update(NO_CACHE)
        return response


app.mount("/static", _Static(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    return response


@app.exception_handler(AppError)
async def _app_error(_: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content=exc.payload(), headers=exc.headers or None)


@app.exception_handler(ConversionError)
async def _conversion_error(_: Request, exc: ConversionError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"status": "error", "error": str(exc)})


@app.exception_handler(ConfigError)
async def _config_error(_: Request, exc: ConfigError) -> JSONResponse:
    log.error("configuration error: %s", exc)
    return JSONResponse(status_code=500, content={"status": "error", "error": f"Configuration error: {exc}"})


def _check_limits(files: list[InputFile]) -> None:
    if not files:
        raise ConversionError("No file uploaded.")
    if len(files) > MAX_FILES:
        raise ConversionError(f"Too many files (at most {MAX_FILES}).")
    for f in files:
        if len(f.data) > MAX_FILE_MB * 1024 * 1024:
            raise ConversionError(f"{f.name}: larger than {MAX_FILE_MB} MB.")


def _log(results: list[Result | Failure]) -> None:
    for r in results:
        if isinstance(r, Failure):
            log.warning("failed %s: %s", r.sources, r.error)
        else:
            log.info("converted %s -> %s (%s, %d lines, %d warnings)", r.sources, r.filename, r.customer.id, len(r.rows), len(r.warnings))


def _payload(results: list[Result | Failure], include_xlsx: bool) -> dict[str, Any]:
    failures = sum(isinstance(r, Failure) for r in results)
    if failures:
        status = "error" if failures == len(results) else "partial"
    else:
        status = "review" if any(r.needs_review for r in results) else "ok"
    return {"status": status, "results": [r.to_dict(content=True, xlsx=include_xlsx) for r in results]}


def _attachment(filename: str) -> dict[str, str]:
    fallback = filename.encode("ascii", "replace").decode().replace("?", "_").replace('"', "_")
    return {"Content-Disposition": f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename)}"}


Db = Annotated[Session, Depends(db.get_db)]


def _page(name: str) -> FileResponse:
    return FileResponse(STATIC_DIR / name, headers=NO_CACHE)


def _safe_next(target: str | None) -> str:
    """Only paths on this site (no //other.host, no backslashes)."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return "/"


def _to_login(request: Request) -> RedirectResponse:
    here = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    return RedirectResponse(f"/login?next={quote(here, safe='/')}", status_code=303)


@app.get("/", include_in_schema=False)
def index(request: Request, session: Db) -> Response:
    """The dashboard: upload, counts and the document list."""
    return _page("index.html") if auth.session_user(request, session) else _to_login(request)


@app.get("/documents/{doc_id}", include_in_schema=False)
def document_page(doc_id: str, request: Request, session: Db) -> Response:
    """The online sheet of one document."""
    return _page("editor.html") if auth.session_user(request, session) else _to_login(request)


@app.get("/login", include_in_schema=False)
def login_page(request: Request, session: Db, next: str | None = None) -> Response:
    if auth.session_user(request, session):
        return RedirectResponse(_safe_next(next), status_code=303)
    return _page("login.html")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@app.get("/api/info")
def info() -> dict[str, Any]:
    return {"version": __version__, "auth_required": True, "login": auth.auth_config(), "retention_months": settings.retention_months()}


@app.get("/api/customers", dependencies=[Depends(auth.require_api_access)])
def customers() -> list[dict[str, str]]:
    return [{"id": p.id, "name": p.name, "template": p.template, "multi_file": p.multi_file} for p in load_customers().values()]


@app.post("/api/convert", dependencies=[Depends(auth.require_api_access)])
async def convert_upload(
    files: Annotated[list[UploadFile], File(description="Commercial Invoice PDF(s)")],
    customer: Annotated[str | None, Form()] = None,
    sender: Annotated[str | None, Form()] = None,
    merge: Annotated[bool | None, Form(description="true = the PDFs are parts of one invoice")] = None,
    response_format: Annotated[str, Form(alias="format", description="csv (default) or json")] = "csv",
    include_xlsx: Annotated[bool, Form()] = False,
):
    inputs = [InputFile(f.filename or "invoice.pdf", await f.read()) for f in files]
    _check_limits(inputs)
    results = await run_in_threadpool(convert_many, inputs, customer or None, sender or None, merge)
    _log(results)
    if response_format == "json":
        return await run_in_threadpool(_payload, results, include_xlsx)
    done = [r for r in results if isinstance(r, Result)]
    if len(results) == 1 and done:
        r = done[0]
        headers = {**_attachment(r.filename), "X-Line-Count": str(len(r.rows)), "X-Status": "review" if r.needs_review else "ok"}
        return Response(r.csv_bytes(), media_type="text/csv; charset=utf-8", headers=headers)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for r in done:
            z.writestr(r.filename, r.csv_bytes())
        errors = [f"{', '.join(r.sources)}: {r.error}" for r in results if isinstance(r, Failure)]
        if errors:
            z.writestr("errors.txt", "\r\n".join(errors))
    return Response(buf.getvalue(), media_type="application/zip", headers=_attachment("CargoWise Input.zip"))


@app.post("/api/preview", dependencies=[Depends(auth.require_api_access)])
async def preview_upload(
    files: Annotated[list[UploadFile], File(description="the PDF(s) of ONE invoice")],
    customer: Annotated[str | None, Form()] = None,
    sender: Annotated[str | None, Form()] = None,
) -> dict[str, Any]:
    """Review before export: the conversion result, the invoice pages as images and where each line is on them."""
    inputs = [InputFile(f.filename or "invoice.pdf", await f.read()) for f in files]
    _check_limits(inputs)
    result = await run_in_threadpool(convert, inputs, customer or None, sender or None)
    _log([result])

    def payload() -> dict[str, Any]:
        return {"result": result.to_dict(content=True, xlsx=True), "preview": preview.build(result, inputs)}

    return await run_in_threadpool(payload)


class FileIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str = Field(validation_alias=AliasChoices("name", "Name", "filename", "FileName"))
    content: str = Field(validation_alias=AliasChoices("content_base64", "contentBytes", "ContentBytes", "$content", "content"))


class ConvertIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    files: list[FileIn]
    customer: str | None = None
    sender: str | None = None
    merge: bool | None = None
    include_xlsx: bool = False


@app.post("/api/convert/json", dependencies=[Depends(auth.require_api_access)])
def convert_json(body: ConvertIn) -> dict[str, Any]:
    """Power Automate friendly: pass the Outlook trigger's attachments array as `files`.

    Attachments that are neither PDF nor Excel (logos, signatures) are skipped and listed in `skipped`.
    Each result has `filename` and `content_base64` (the CSV) – use base64ToBinary() to save it.
    """
    inputs, skipped = [], []
    for f in body.files:
        try:
            data = base64.b64decode(f.content, validate=False)
        except (binascii.Error, ValueError) as e:
            raise ConversionError(f"{f.name}: content is not valid base64") from e
        if sources.kind(f.name, data):
            inputs.append(InputFile(f.name, data))
        else:
            skipped.append(f.name)
    if not inputs:
        raise ConversionError("No PDF or Excel attachment found." + (f" Skipped: {', '.join(skipped)}" if skipped else ""))
    _check_limits(inputs)
    results = convert_many(inputs, body.customer or None, body.sender or None, body.merge)
    _log(results)
    return {**_payload(results, body.include_xlsx), "skipped": skipped}
