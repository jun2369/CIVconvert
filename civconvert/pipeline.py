"""Commercial Invoice PDF(s) -> rows of the CargoWise template -> CSV.

    profile  = which customer (explicit, by sender e-mail, or by text in the PDF)
    extract  = line items from the PDF table, located by header text
    mapping  = template column <- item field / value, plus transforms (strip_chinese …)
    checks   = subtotal, qty x price = amount, formats – anything odd becomes a warning
"""

from __future__ import annotations

import base64
import re
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from openpyxl.utils import get_column_letter

from . import extract
from .export import to_csv, to_xlsx
from .profiles import ColumnSpec, ConfigError, CustomerProfile, load_customers, load_template_profile
from .template import Template, load_template, normalize_header
from .transforms import apply_transforms, parse_spec


class ConversionError(Exception):
    """The input cannot be converted (unknown customer, no line items, not a PDF …)."""


@dataclass
class InputFile:
    name: str
    data: bytes


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


@dataclass
class OutputColumn:
    index: int
    letter: str
    header: str
    spec: ColumnSpec


@dataclass
class Result:
    customer: CustomerProfile
    template: Template
    sources: list[str]
    document: dict[str, str]
    filename: str
    columns: list[OutputColumn]
    rows: list[list[str]]
    items: list[dict[str, str]]
    checks: list[Check] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def needs_review(self) -> bool:
        return bool(self.warnings) or not all(c.ok for c in self.checks)

    @property
    def xlsx_filename(self) -> str:
        return re.sub(r"\.csv$", "", self.filename, flags=re.IGNORECASE) + ".xlsx"

    def csv_bytes(self) -> bytes:
        return to_csv(self.template, self.rows)

    def xlsx_bytes(self) -> bytes:
        return to_xlsx(self.template, self.rows, frozenset(c.index for c in self.columns if c.spec.number))

    def to_dict(self, content: bool = False, xlsx: bool = False) -> dict[str, Any]:
        d: dict[str, Any] = {
            "status": "review" if self.needs_review else "ok",
            "filename": self.filename,
            "customer": {"id": self.customer.id, "name": self.customer.name},
            "template": self.template.id,
            "sources": self.sources,
            "document": self.document,
            "line_count": len(self.rows),
            "checks": [asdict(c) for c in self.checks],
            "warnings": self.warnings,
            "columns": [{"letter": c.letter, "header": c.header, "field": c.spec.field} for c in self.columns],
            "lines": [
                {
                    "line_no": n,
                    "source": item.get("_source", ""),
                    "values": {c.letter: row[c.index] for c in self.columns},
                    "original": {c.letter: item.get(c.spec.field, "") for c in self.columns if c.spec.field},
                }
                for n, (row, item) in enumerate(zip(self.rows, self.items), 1)
            ],
        }
        if content:
            d["content_base64"] = base64.b64encode(self.csv_bytes()).decode("ascii")
        if xlsx:
            d["xlsx_filename"] = self.xlsx_filename
            d["xlsx_base64"] = base64.b64encode(self.xlsx_bytes()).decode("ascii")
        return d


@dataclass
class Failure:
    sources: list[str]
    error: str

    def to_dict(self, **_: Any) -> dict[str, Any]:
        return {"status": "error", "sources": self.sources, "error": self.error}


class _Values(dict):
    """format_map() context that names the missing placeholder instead of a bare KeyError."""

    def __init__(self, profile_id: str, values: dict[str, Any]):
        super().__init__(values)
        self.profile_id = profile_id

    def __missing__(self, key: str) -> Any:
        raise ConversionError(f"'{{{key}}}' is used in profile '{self.profile_id}' but has no value for this invoice")


def _render(pattern: str, values: _Values) -> str:
    try:
        return pattern.format_map(values)
    except (ValueError, IndexError) as e:
        raise ConfigError(f"profile '{values.profile_id}': invalid placeholder text '{pattern}': {e}") from e


def _stem(name: str) -> str:
    base = re.split(r"[\\/]", name)[-1]
    return re.sub(r"\.pdf$", "", base, flags=re.IGNORECASE)


def _safe_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip().rstrip(".")
    return name if name.lower().endswith(".csv") else f"{name}.csv"


def _decimal(text: str | None) -> Decimal | None:
    if not text:
        return None
    digits = re.sub(r"[^\d.\-]", "", text.replace(",", ""))
    try:
        return Decimal(digits) if re.search(r"\d", digits) else None
    except InvalidOperation:
        return None


def _fmt(value: Decimal) -> str:
    return f"{value.quantize(Decimal(1)) if value == value.to_integral_value() else value:,}"


def resolve_customer(
    profiles: dict[str, CustomerProfile], customer: str | None, sender: str | None, text: str
) -> CustomerProfile:
    if customer:
        if customer in profiles:
            return profiles[customer]
        raise ConversionError(f"Unknown customer '{customer}'. Configured customers: {', '.join(profiles) or 'none'}")
    pool = list(profiles.values())
    if sender:
        by_sender = [p for p in pool if p.matches_sender(sender)]
        if len(by_sender) == 1:
            return by_sender[0]
        pool = by_sender or pool
    by_text = [p for p in pool if p.matches_text(text)]
    if len(by_text) == 1:
        return by_text[0]
    if not by_text:
        raise ConversionError(
            "Could not identify the customer from this PDF. Choose the customer explicitly, "
            "or add detection text to the customer's profile."
        )
    raise ConversionError(
        f"This PDF matches several customer profiles ({', '.join(p.id for p in by_text)}); choose the customer explicitly."
    )


def resolve_columns(profile: CustomerProfile, template: Template) -> list[OutputColumn]:
    columns = []
    for spec in profile.mapping:
        idx = template.resolve(spec.target)
        header = template.headers[idx]
        if spec.expect and normalize_header(spec.expect) != normalize_header(header):
            raise ConfigError(
                f"{profile.path.name}: column {spec.target} of template '{template.id}' is '{header.strip()}', "
                f"but the profile expects '{spec.expect}'. Was the template changed?"
            )
        columns.append(OutputColumn(idx, get_column_letter(idx + 1), header, spec))
    seen: set[int] = set()
    for c in columns:
        if c.index in seen:
            raise ConfigError(f"{profile.path.name}: template column {c.letter} is mapped twice")
        seen.add(c.index)
    return columns


def _run_checks(
    profile: CustomerProfile,
    items: list[dict[str, str]],
    totals: dict[str, str] | None,
    rows: list[list[str]],
    columns: list[OutputColumn],
) -> tuple[list[Check], list[str]]:
    cfg = profile.checks
    checks = [Check("Line items", True, f"{len(items)} lines read")]
    warnings: list[str] = []

    if cfg["totals"]:
        if totals is None:
            checks.append(Check("Subtotal", False, "subtotal row not found – cannot confirm that every line was read"))
        for f in cfg["totals"] if totals is not None else []:
            expected = _decimal(totals.get(f))
            values = [_decimal(i.get(f)) for i in items]
            if expected is None:
                checks.append(Check(f"Subtotal {f}", False, f"no {f} value in the subtotal row"))
                continue
            got = sum((v for v in values if v is not None), Decimal(0))
            unreadable = sum(v is None for v in values)
            ok = unreadable == 0 and abs(got - expected) <= Decimal("0.01")
            detail = f"lines {_fmt(got)} vs subtotal {_fmt(expected)}"
            if unreadable:
                detail += f" ({unreadable} line(s) without a readable {f})"
            checks.append(Check(f"Subtotal {f}", ok, detail))

    if cfg["line_math"]:
        q, p, a = cfg["line_math"]
        bad = []
        for n, item in enumerate(items, 1):
            qty, price, amount = (_decimal(item.get(x)) for x in (q, p, a))
            # allow rounding of the printed unit price: half a cent per unit
            if None in (qty, price, amount) or abs(qty * price - amount) > max(Decimal("0.01"), abs(qty) * Decimal("0.005")):
                bad.append(str(n))
        detail = f"{q} x {p} = {a} on every line" if not bad else f"does not add up on line(s) {', '.join(bad)}"
        checks.append(Check("Line math", not bad, detail))

    for n, item in enumerate(items, 1):
        missing = [f for f in profile.table.required if not item.get(f)]
        if missing:
            warnings.append(f"Line {n}: {', '.join(missing)} is empty")
        for f, rx in cfg["patterns"].items():
            if item.get(f) and not rx.search(item[f]):
                warnings.append(f"Line {n}: {f} '{item[f]}' has an unexpected format")

    for n, (row, item) in enumerate(zip(rows, items), 1):
        for col in columns:
            value = row[col.index]
            raw = item.get(col.spec.field, "") if col.spec.field else ""
            if any(parse_spec(t)[0] == "strip_chinese" for t in col.spec.transforms) and raw:
                if not value:
                    warnings.append(f"Line {n}: {col.header} is empty after removing Chinese ('{raw}') – needs an English description")
                elif value != raw and len(re.findall(r"[A-Za-z]", value)) < cfg["min_letters_after_strip"]:
                    warnings.append(f"Line {n}: {col.header} '{value}' looks too short after removing Chinese ('{raw}')")
            if cfg["ascii_only"] and value and not value.isascii():
                warnings.append(f"Line {n}: {col.header} contains non-English characters: '{value}'")
    return checks, warnings


def convert(files: list[InputFile], customer: str | None = None, sender: str | None = None) -> Result:
    """One invoice: a single PDF, or several PDFs that are parts of the same invoice."""
    if not files:
        raise ConversionError("No PDF file provided.")
    for f in files:
        if b"%PDF" not in f.data[:1024]:
            raise ConversionError(f"{f.name}: not a PDF file")
    profiles = load_customers()
    try:
        first_text = extract.read_text(files[0].data, files[0].name)
    except extract.PdfError as e:
        raise ConversionError(str(e)) from e
    profile = resolve_customer(profiles, customer, sender, first_text)
    template = load_template(load_template_profile(profile.template))
    columns = resolve_columns(profile, template)

    items: list[dict[str, str]] = []
    totals: dict[str, str] | None = None
    document: dict[str, str] = {}
    warnings: list[str] = []
    carry, header_seen = None, False
    for f in files:
        try:
            part, carry = extract.extract(f.data, f.name, profile.table, carry)
        except extract.PdfError as e:
            raise ConversionError(str(e)) from e
        if not part.text.strip():
            raise ConversionError(f"{f.name}: the PDF has no text layer (scanned image?) – OCR is not supported yet")
        header_seen = header_seen or part.header_found
        items.extend(part.items)
        warnings.extend(part.notes)
        if part.totals is not None:
            if totals is not None:
                warnings.append(f"{f.name}: another subtotal row was found; the last one is used for the check")
            totals = part.totals
        for key, rx in profile.document_fields.items():
            m = rx.search(part.text)
            if m:
                value = m.group(1).strip()
                if key in document and document[key] != value:
                    warnings.append(f"{key} differs between the PDF files: '{document[key]}' / '{value}' ({f.name})")
                document.setdefault(key, value)

    names = ", ".join(f.name for f in files)
    if not header_seen:
        raise ConversionError(
            f"{names}: the item table header ({', '.join(profile.table.required)}) of profile "
            f"'{profile.id}' was not found – the PDF layout does not match this customer's profile"
        )
    if not items:
        raise ConversionError(f"{names}: the item table was found but it has no line items")

    base = {"source_name": _stem(files[0].name), "customer": profile.id, **document}
    rows = []
    for n, item in enumerate(items, 1):
        values = _Values(profile.id, {**base, **{k: v for k, v in item.items() if not k.startswith("_")}, "line_no": n})
        row = [""] * len(template.headers)
        for col in columns:
            raw = item.get(col.spec.field, "") if col.spec.field else _render(col.spec.value or "", values)
            row[col.index] = apply_transforms(raw, col.spec.transforms)
        rows.append(row)

    checks, check_warnings = _run_checks(profile, items, totals, rows, columns)
    return Result(
        customer=profile,
        template=template,
        sources=[f.name for f in files],
        document=document,
        filename=_safe_filename(_render(profile.output_filename, _Values(profile.id, base))),
        columns=columns,
        rows=rows,
        items=items,
        checks=checks,
        warnings=warnings + check_warnings,
    )


def convert_many(
    files: list[InputFile], customer: str | None = None, sender: str | None = None, merge: bool | None = None
) -> list[Result | Failure]:
    """Several PDFs: one merged invoice (merge=True) or one result per PDF (merge=False).

    merge=None uses the customer's profile setting (output.multi_file).
    A single PDF that fails raises ConversionError; in separate mode each failure is reported per file.
    """
    if not files:
        raise ConversionError("No PDF file provided.")
    if merge is None and len(files) > 1:
        try:
            text = extract.read_text(files[0].data, files[0].name)
            merge = resolve_customer(load_customers(), customer, sender, text).multi_file == "merge"
        except (extract.PdfError, ConversionError):
            merge = False
    if merge or len(files) == 1:
        return [convert(files, customer, sender)]
    results: list[Result | Failure] = []
    for f in files:
        try:
            results.append(convert([f], customer, sender))
        except ConversionError as e:
            results.append(Failure([f.name], str(e)))
    return results
