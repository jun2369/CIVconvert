"""HTTP API and upload page.

    local:      python -m civconvert serve
    container:  uvicorn civconvert.api:app --host 0.0.0.0 --port 8080

Set API_KEY (several keys: comma-separated) to require an X-API-Key header.

    POST /api/convert        multipart upload (web page, curl) -> CSV file, or JSON with format=json
    POST /api/convert/json   base64 JSON in/out, accepts Outlook attachments as-is (Power Automate)
    POST /api/preview        the PDF(s) of one invoice -> result + page images for the review page
"""

import base64
import binascii
import hmac
import io
import logging
import os
import zipfile
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from . import __version__, preview
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

app = FastAPI(title="CIVconvert", version=__version__, description="Commercial Invoice PDF -> CargoWise CSV")
NO_CACHE = {"Cache-Control": "no-cache"}  # revalidate the page and its scripts, so an update is never half-loaded


class _Static(StaticFiles):
    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers.update(NO_CACHE)
        return response


app.mount("/static", _Static(directory=STATIC_DIR), name="static")


def _api_keys() -> list[str]:
    return [k.strip() for k in os.environ.get("API_KEY", "").split(",") if k.strip()]


if not _api_keys():
    log.warning("API_KEY is not set - anyone who can reach this server can use it (fine for local use only)")


def require_api_key(x_api_key: Annotated[str | None, Header()] = None) -> None:
    keys = _api_keys()
    if keys and not (x_api_key and any(hmac.compare_digest(x_api_key.encode(), k.encode()) for k in keys)):
        raise HTTPException(status_code=401, detail="Missing or invalid X-API-Key header")


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


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", headers=NO_CACHE)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@app.get("/api/info")
def info() -> dict[str, Any]:
    return {"version": __version__, "auth_required": bool(_api_keys())}


@app.get("/api/customers", dependencies=[Depends(require_api_key)])
def customers() -> list[dict[str, str]]:
    return [{"id": p.id, "name": p.name, "template": p.template, "multi_file": p.multi_file} for p in load_customers().values()]


@app.post("/api/convert", dependencies=[Depends(require_api_key)])
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


@app.post("/api/preview", dependencies=[Depends(require_api_key)])
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


@app.post("/api/convert/json", dependencies=[Depends(require_api_key)])
def convert_json(body: ConvertIn) -> dict[str, Any]:
    """Power Automate friendly: pass the Outlook trigger's attachments array as `files`.

    Attachments that are not PDFs (logos, signatures) are skipped and listed in `skipped`.
    Each result has `filename` and `content_base64` (the CSV) – use base64ToBinary() to save it.
    """
    inputs, skipped = [], []
    for f in body.files:
        try:
            data = base64.b64decode(f.content, validate=False)
        except (binascii.Error, ValueError) as e:
            raise ConversionError(f"{f.name}: content is not valid base64") from e
        if b"%PDF" in data[:1024]:
            inputs.append(InputFile(f.name, data))
        else:
            skipped.append(f.name)
    if not inputs:
        raise ConversionError("No PDF attachment found." + (f" Skipped: {', '.join(skipped)}" if skipped else ""))
    _check_limits(inputs)
    results = convert_many(inputs, body.customer or None, body.sender or None, body.merge)
    _log(results)
    return {**_payload(results, body.include_xlsx), "skipped": skipped}
