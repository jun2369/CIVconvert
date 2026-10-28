"""Commercial Invoice PDF(s) or Excel file -> rows of the CargoWise template -> CSV.

    profile  = which customer (explicit, by sender e-mail, or by text in the file); no customer
               profile, or its table is not in the file: the general reader (generic.py)
    extract  = line items from the invoice table, located by header text
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

from . import extract, generic, ocr, sources
from .export import to_csv, to_xlsx
from .profiles import AUTO_ID, ColumnSpec, ConfigError, CustomerProfile, load_customers, load_generic, load_template_profile
from .template import Template, load_template, normalize_header
from .transforms import apply_transforms, parse_spec


class ConversionError(Exception):
    """The input cannot be converted (unknown customer, no line items, not a PDF …)."""


class NoCustomerMatch(ConversionError):
    """No customer profile recognises the file (the general reader takes it)."""


class TableNotFound(ConversionError):
    """The customer profile's item table is not in the file (the general reader tries)."""


@dataclass
class InputFile:
    name: str
    data: bytes


@dataclass
class Check:
    name: str
    ok: bool
    detail: str  # English; the web page translates `code` with `params`
    code: str = ""
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class Flag:
    """A problem on one line item, located so the preview can mark the cells."""

    line: int
    level: str  # error | warning
    message: str  # English; the web page translates `code` with `params`
    fields: list[str] = field(default_factory=list)  # PDF table fields (model, hs_code …)
    columns: list[str] = field(default_factory=list)  # template column letters (D, F …)
    code: str = ""
    params: dict[str, Any] = field(default_factory=dict)


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
    flags: list[Flag] = field(default_factory=list)
    parts: list[extract.PdfExtract] = field(default_factory=list, repr=False)  # one per PDF, with row positions

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
            "flags": [asdict(f) for f in self.flags],
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
    return sources.strip_extension(re.split(r"[\\/]", name)[-1])


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
        raise NoCustomerMatch(
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
    joined: list[int],
) -> tuple[list[Check], list[str], list[Flag]]:
    cfg = profile.checks
    checks = [Check("Line items", True, f"{len(items)} lines read", "lines_read", {"n": len(items)})]
    warnings: list[str] = []
    flags: list[Flag] = []
    letters: dict[str, list[str]] = {}  # field -> template columns filled from it
    for c in columns:
        if c.spec.field:
            letters.setdefault(c.spec.field, []).append(c.letter)

    def flag(
        n: int, level: str, message: str, fields: list[str], cols: list[str] | None = None, warn: bool = True,
        code: str = "", **params: Any,
    ) -> None:
        if cols is None:
            cols = [letter for f in fields for letter in letters.get(f, [])]
        flags.append(Flag(n, level, message, fields, cols, code, params))
        if warn:
            warnings.append(f"Line {n}: {message}")

    if cfg["totals"]:
        if totals is None:
            checks.append(Check("Subtotal", False, "subtotal row not found – cannot confirm that every line was read", "no_subtotal"))
        for f in cfg["totals"] if totals is not None else []:
            expected = _decimal(totals.get(f))
            values = [_decimal(i.get(f)) for i in items]
            if expected is None:
                checks.append(Check(f"Subtotal {f}", False, f"no {f} value in the subtotal row", "no_total", {"field": f}))
                continue
            got = sum((v for v in values if v is not None), Decimal(0))
            unreadable = sum(v is None for v in values)
            ok = unreadable == 0 and abs(got - expected) <= Decimal("0.01")
            detail = f"lines {_fmt(got)} vs subtotal {_fmt(expected)}"
            params: dict[str, Any] = {"field": f, "got": _fmt(got), "expected": _fmt(expected)}
            if unreadable:
                detail += f" ({unreadable} line(s) without a readable {f})"
                params["unreadable"] = unreadable
            checks.append(Check(f"Subtotal {f}", ok, detail, "total_unreadable" if unreadable else "total", params))

    if cfg["line_math"]:
        q, p, a = cfg["line_math"]
        bad = []
        for n, item in enumerate(items, 1):
            qty, price, amount = (_decimal(item.get(x)) for x in (q, p, a))
            if None in (qty, price, amount):
                bad.append(str(n))
                flag(n, "error", f"{q} x {p} = {a} cannot be checked: a value is missing or unreadable", [q, p, a], [], warn=False,
                     code="line_math_unreadable", q=q, p=p, a=a)
            # allow rounding of the printed unit price: half a cent per unit
            elif abs(qty * price - amount) > max(Decimal("0.01"), abs(qty) * Decimal("0.005")):
                bad.append(str(n))
                flag(n, "error", f"{q} x {p} = {_fmt(qty * price)}, but {a} is {_fmt(amount)}", [q, p, a], [], warn=False,
                     code="line_math", q=q, p=p, a=a, calc=_fmt(qty * price), amount=_fmt(amount))
        detail = f"{q} x {p} = {a} on every line" if not bad else f"does not add up on line(s) {', '.join(bad)}"
        params = {"q": q, "p": p, "a": a, "lines": bad}
        checks.append(Check("Line math", not bad, detail, "line_math_bad" if bad else "line_math_ok", params))

    for n, item in enumerate(items, 1):
        missing = [f for f in profile.table.required if not item.get(f)]
        if missing:
            flag(n, "error", f"{', '.join(missing)} is empty", missing, code="empty", names=missing)
        for f, rx in cfg["patterns"].items():
            if item.get(f) and not rx.search(item[f]):
                flag(n, "warning", f"{f} '{item[f]}' has an unexpected format", [f], code="format", field=f, value=item[f])

    for n, (row, item) in enumerate(zip(rows, items), 1):
        for col in columns:
            value = row[col.index]
            fields = [col.spec.field] if col.spec.field else []
            raw = item.get(col.spec.field, "") if col.spec.field else ""
            if any(parse_spec(t)[0] == "strip_chinese" for t in col.spec.transforms) and raw:
                if not value:
                    flag(n, "error", f"{col.header} is empty after removing Chinese ('{raw}') – needs an English description", fields, [col.letter],
                         code="empty_after_strip", header=col.header, raw=raw)
                elif value != raw and len(re.findall(r"[A-Za-z]", value)) < cfg["min_letters_after_strip"]:
                    flag(n, "warning", f"{col.header} '{value}' looks too short after removing Chinese ('{raw}')", fields, [col.letter],
                         code="too_short", header=col.header, value=value, raw=raw)
            if cfg["ascii_only"] and value and not value.isascii():
                flag(n, "warning", f"{col.header} contains non-English characters: '{value}'", fields, [col.letter],
                     code="non_ascii", header=col.header, value=value)

    # the same model on several lines must have the same HS code (checks.consistent)
    for key, dependents in cfg["consistent"].items():
        lines: dict[str, list[int]] = {}
        for n, item in enumerate(items, 1):
            if item.get(key):
                lines.setdefault(item[key], []).append(n)
        for value, group in lines.items():
            for f in dependents:
                for n in group:
                    mine = items[n - 1].get(f, "")
                    differ = [m for m in group if items[m - 1].get(f, "") != mine]
                    if differ:
                        other = ", ".join(f"'{items[m - 1].get(f, '')}' on line {m}" for m in differ)
                        flag(n, "warning", f"{key} '{value}' has {f} '{mine}' here but {other}", [key, f],
                             code="inconsistent", key=key, value=value, field=f, mine=mine,
                             others=[{"line": m, "value": items[m - 1].get(f, "")} for m in differ])

    for n, count in enumerate(joined, 1):
        if count:  # already reported by extract as a note
            flag(n, "warning", f"{count} wrapped row(s) below were joined to this line", profile.table.continuation_fields, warn=False,
                 code="joined", count=count)
    return checks, warnings, flags


def _file_kinds(files: list[InputFile]) -> list[str]:
    if not files:
        raise ConversionError("No PDF file provided.")
    kinds = []
    for f in files:
        kind = sources.kind(f.name, f.data)
        if kind is None:
            raise ConversionError(f"{f.name}: not a PDF, Excel or picture file")
        kinds.append(kind)
    return kinds


def _no_text(name: str) -> ConversionError:
    if not ocr.available():
        return ConversionError(f"{name}: the PDF has no text layer (scanned image?) and OCR is not installed on this server")
    return ConversionError(f"{name}: no text could be read – the page is empty, or the picture is too unclear for OCR")


def file_text(f: InputFile, kind: str) -> str:
    """The text of a PDF or picture / the cells of a workbook (to recognise the customer)."""
    try:
        if kind in (sources.PDF, sources.IMAGE):
            return extract.read_text(f.data, f.name)
        return "\n\n".join(g.text() for g in sources.read_workbook(f.data, f.name, kind))
    except (extract.PdfError, sources.SourceError) as e:
        raise ConversionError(str(e)) from e


def convert(files: list[InputFile], customer: str | None = None, sender: str | None = None) -> Result:
    """One invoice: a single PDF or Excel file, or several PDFs that are parts of the same invoice.

    A customer profile that recognises the file reads it as before. Without one – another supplier,
    an Excel file, or a layout the profile does not know – the general reader finds the item table.
    """
    kinds = _file_kinds(files)
    profiles = load_customers()
    first_text = file_text(files[0], kinds[0])
    profile: CustomerProfile | None
    if customer == AUTO_ID:
        profile = None
    else:
        try:
            profile = resolve_customer(profiles, customer, sender, first_text)
        except NoCustomerMatch:
            profile = None
    notes: list[str] = []
    if profile is not None and all(k == sources.PDF for k in kinds):
        try:
            return _convert_with_profile(files, profile)
        except TableNotFound as e:
            notes.append(f"{e}; it was read with the general reader instead")
    return _convert_general(files, kinds, profile, notes)


def _convert_with_profile(files: list[InputFile], profile: CustomerProfile) -> Result:
    """PDFs read with a customer profile (its header texts, its table strategy)."""
    template = load_template(load_template_profile(profile.template))
    columns = resolve_columns(profile, template)

    items: list[dict[str, str]] = []
    totals: dict[str, str] | None = None
    document: dict[str, str] = {}
    warnings: list[str] = []
    parts: list[extract.PdfExtract] = []
    carry, header_seen = None, False
    for f in files:
        try:
            part, carry = extract.extract(f.data, f.name, profile.table, carry)
        except extract.PdfError as e:
            raise ConversionError(str(e)) from e
        if not part.text.strip():
            raise _no_text(f.name)
        parts.append(part)
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
        raise TableNotFound(
            f"{names}: the item table header ({', '.join(profile.table.required)}) of profile "
            f"'{profile.id}' was not found – the PDF layout does not match this customer's profile"
        )
    if not items:
        raise TableNotFound(f"{names}: the item table was found but it has no line items")
    return _finish(profile, template, columns, files, items, totals, document, warnings, parts)


def _convert_general(files: list[InputFile], kinds: list[str], hint: CustomerProfile | None, notes: list[str]) -> Result:
    """The general reader (generic.py). `hint`: a customer profile that recognised an Excel file –
    its header texts are tried first, and its mapping and checks are used."""
    spec = load_generic()
    items: list[dict[str, str]] = []
    totals: dict[str, str] | None = None
    document: dict[str, str] = {}
    warnings: list[str] = list(notes)
    parts: list[extract.PdfExtract] = []
    carry = None
    for f, kind in zip(files, kinds):
        try:
            if kind in (sources.PDF, sources.IMAGE):
                part, carry = generic.read_pdf(f.data, f.name, spec, carry)
            else:
                part = generic.read_excel(f.data, f.name, kind, spec, hint)
        except (extract.PdfError, sources.SourceError) as e:
            raise ConversionError(str(e)) from e
        if kind in (sources.PDF, sources.IMAGE) and not part.text.strip():
            raise _no_text(f.name)
        parts.append(part)
        items.extend(part.items)
        warnings.extend(part.notes)
        if part.totals is not None:
            totals = part.totals
        found = generic.document_fields(part.text, spec, part.label_values)
        if hint is not None:
            for key, rx in hint.document_fields.items():
                m = rx.search(part.text)
                if m:
                    found[key] = m.group(1).strip()
        for key, value in found.items():
            if key in document and document[key] != value:
                warnings.append(f"{key} differs between the files: '{document[key]}' / '{value}' ({f.name})")
            document.setdefault(key, value)

    # no invoice number in the text, but the item table has a column for it (packing list + invoice in
    # one document): the number of its lines, when they all say the same
    if "invoice_no" not in document:
        on_lines = {i["line_invoice_no"].strip() for i in items if i.get("line_invoice_no", "").strip()}
        if len(on_lines) == 1:
            document["invoice_no"] = on_lines.pop()

    names = ", ".join(f.name for f in files)
    if not any(p.header_found for p in parts):
        what = generic.other_document("\n".join(p.text for p in parts), spec)
        if what:  # an arrival notice, an HS-7 form …: say so
            raise ConversionError(f"{names}: this is {what}, not a Commercial Invoice – it has no invoice lines to convert. Upload the Commercial Invoice")
        raise ConversionError(
            f"{names}: no line-item table was found – looked for columns such as Description, HS Code / HTS, "
            "Qty, Unit Price, Amount (the general reader's settings: config/generic.yaml)"
        )
    if not items:
        raise ConversionError(f"{names}: the item table was found but it has no line items")
    found_fields = {f for p in parts for f in p.labels}
    # the customer's (still recognised) or the general profile, with the checks this invoice's columns allow
    profile = generic.effective(hint if hint is not None else spec.profile, found_fields, totals is not None)
    template = load_template(load_template_profile(profile.template))
    columns = resolve_columns(profile, template)
    return _finish(profile, template, columns, files, items, totals, document, warnings, parts)


def _finish(
    profile: CustomerProfile,
    template: Template,
    columns: list[OutputColumn],
    files: list[InputFile],
    items: list[dict[str, str]],
    totals: dict[str, str] | None,
    document: dict[str, str],
    warnings: list[str],
    parts: list[extract.PdfExtract],
) -> Result:
    """The template rows of the line items, the checks, and the result."""
    base = {"source_name": _stem(files[0].name), "customer": profile.id, **document}
    rows = []
    for n, item in enumerate(items, 1):
        values = _Values(profile.id, {**base, **{k: v for k, v in item.items() if not k.startswith("_")}, "line_no": n})
        row = [""] * len(template.headers)
        for col in columns:
            raw = item.get(col.spec.field, "") if col.spec.field else _render(col.spec.value or "", values)
            row[col.index] = apply_transforms(raw, col.spec.transforms)
        rows.append(row)

    joined = [box.joined for part in parts for box in part.rows]
    checks, check_warnings, flags = _run_checks(profile, items, totals, rows, columns, joined)
    read_by_ocr = [f"{p.name} p.{', '.join(map(str, p.ocr_pages))}" for p in parts if p.ocr_pages]
    if read_by_ocr:  # letters can be misread: a person compares the values with the picture
        pages = "; ".join(read_by_ocr)
        checks.append(Check("OCR", False, f"read from a picture by OCR ({pages}) – compare the values with the picture", "ocr", {"pages": pages}))
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
        flags=sorted(flags, key=lambda x: x.line),
        parts=parts,
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
            kind = sources.kind(files[0].name, files[0].data) or sources.PDF
            text = file_text(files[0], kind)
            merge = resolve_customer(load_customers(), customer, sender, text).multi_file == "merge"
        except ConversionError:
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
