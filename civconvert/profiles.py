"""Customer profiles (config/customers/*.yaml) and template profiles (config/templates/*.yaml).

Adding a customer means adding a YAML file – no code change. Files whose name starts
with "_" (such as _example_new_customer.yaml) are documentation and are not loaded.
"""

from __future__ import annotations

import codecs
import fnmatch
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .settings import config_dir
from .transforms import TRANSFORMS, parse_spec


class ConfigError(Exception):
    """A configuration file is missing or invalid."""


@dataclass
class TemplateProfile:
    id: str
    workbook: Path
    sheet: str | None
    header_row: int
    csv_encoding: str
    csv_delimiter: str
    csv_line_terminator: str
    header_control_chars: str  # keep | strip


@dataclass
class TableSpec:
    strategy: str  # lines | text
    columns: dict[str, re.Pattern]  # field -> header-cell pattern
    required: list[str]
    stop_row: re.Pattern | None
    totals_row: re.Pattern | None
    continuation_anchor: str | None
    continuation_fields: list[str]
    pdfplumber: dict[str, Any]


@dataclass
class ColumnSpec:
    target: str  # column letter ("D") or template header text ("Product Code")
    field: str | None
    value: str | None
    transforms: list[Any]
    expect: str | None
    number: bool


@dataclass
class CustomerProfile:
    id: str
    name: str
    template: str
    detect_all: list[str]
    detect_any: list[str]
    senders: list[str]
    document_fields: dict[str, re.Pattern]
    table: TableSpec
    mapping: list[ColumnSpec]
    checks: dict[str, Any]
    output_filename: str
    multi_file: str  # separate | merge
    path: Path

    def matches_text(self, text: str) -> bool:
        if not (self.detect_all or self.detect_any):
            return False
        hay = _squash(text)
        if any(_squash(s) not in hay for s in self.detect_all):
            return False
        return not self.detect_any or any(_squash(s) in hay for s in self.detect_any)

    def matches_sender(self, sender: str) -> bool:
        address = sender.strip().lower()
        return any(fnmatch.fnmatch(address, p.lower()) for p in self.senders)


def _squash(text: str) -> str:
    return " ".join(text.split()).lower()


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        raise ConfigError(f"{path.name}: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name}: expected key: value settings at the top level")
    return data


def _regex(path: Path, where: str, pattern: Any) -> re.Pattern:
    if not isinstance(pattern, str) or not pattern:
        raise ConfigError(f"{path.name}: {where} must be a regular expression (text)")
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        raise ConfigError(f"{path.name}: invalid regular expression in {where}: {e}") from e


def _texts(path: Path, where: str, value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return value
    raise ConfigError(f"{path.name}: {where} must be a text or a list of texts")


def _fields(path: Path, where: str, value: Any, known: dict[str, Any]) -> list[str]:
    names = _texts(path, where, value)
    unknown = [n for n in names if n not in known]
    if unknown:
        raise ConfigError(f"{path.name}: {where} refers to {', '.join(unknown)}, not defined under table.columns")
    return names


def _table(path: Path, d: Any) -> TableSpec:
    if not isinstance(d, dict):
        raise ConfigError(f"{path.name}: 'table' settings are missing")
    cols = d.get("columns")
    if not isinstance(cols, dict) or not cols:
        raise ConfigError(f"{path.name}: table.columns must map field names to header patterns")
    columns = {str(f): _regex(path, f"table.columns.{f}", p) for f, p in cols.items()}
    required = _fields(path, "table.required", d.get("required"), columns) or list(columns)
    strategy = d.get("strategy", "lines")
    if strategy not in ("lines", "text"):
        raise ConfigError(f"{path.name}: table.strategy must be 'lines' or 'text'")
    cont = d.get("continuation_rows", {})
    if cont is False:
        anchor, cont_fields = None, []
    elif isinstance(cont, dict):
        anchor = cont.get("when_empty", required[0])
        _fields(path, "table.continuation_rows.when_empty", anchor, columns)
        default = ["description"] if "description" in columns else []
        cont_fields = _fields(path, "table.continuation_rows.append", cont.get("append", default), columns)
    else:
        raise ConfigError(f"{path.name}: table.continuation_rows must be settings or false")
    settings = d.get("pdfplumber") or {}
    if not isinstance(settings, dict):
        raise ConfigError(f"{path.name}: table.pdfplumber must be key: value settings")
    return TableSpec(
        strategy=strategy,
        columns=columns,
        required=required,
        stop_row=_regex(path, "table.stop_row", d["stop_row"]) if d.get("stop_row") else None,
        totals_row=_regex(path, "table.totals_row", d["totals_row"]) if d.get("totals_row") else None,
        continuation_anchor=anchor,
        continuation_fields=cont_fields,
        pdfplumber=settings,
    )


def _mapping(path: Path, d: Any, columns: dict[str, Any]) -> list[ColumnSpec]:
    if not isinstance(d, dict) or not d:
        raise ConfigError(f"{path.name}: 'mapping' must list the template columns to fill")
    specs = []
    for key, spec in d.items():
        if isinstance(key, bool) or not isinstance(key, str):
            raise ConfigError(f"{path.name}: mapping key {key!r} – put column letters in quotes, e.g. \"N\":")
        if not isinstance(spec, dict):
            raise ConfigError(f"{path.name}: mapping.{key} must be settings like {{ field: model }}")
        fld, value = spec.get("field"), spec.get("value")
        if (fld is None) == (value is None):
            raise ConfigError(f"{path.name}: mapping.{key} needs exactly one of 'field' or 'value'")
        if fld is not None and fld not in columns:
            raise ConfigError(f"{path.name}: mapping.{key} uses field '{fld}', not defined under table.columns")
        transforms = spec.get("transforms") or []
        if not isinstance(transforms, list):
            transforms = [transforms]
        for t in transforms:
            try:
                name, options = parse_spec(t)
                if name not in TRANSFORMS:
                    raise ValueError(f"unknown transform '{name}' (available: {', '.join(TRANSFORMS)})")
                TRANSFORMS[name]("x", **options)  # rejects unknown options early
            except (TypeError, ValueError, re.error) as e:
                raise ConfigError(f"{path.name}: mapping.{key}: {e}") from e
        expect = spec.get("expect")
        specs.append(
            ColumnSpec(
                target=key,
                field=fld,
                value=None if value is None else str(value),
                transforms=transforms,
                expect=None if expect is None else str(expect),
                number=bool(spec.get("number", False)),
            )
        )
    return specs


def _checks(path: Path, d: Any, columns: dict[str, Any]) -> dict[str, Any]:
    d = d or {}
    if not isinstance(d, dict):
        raise ConfigError(f"{path.name}: 'checks' must be key: value settings")
    line_math = _fields(path, "checks.line_math", d.get("line_math"), columns)
    if line_math and len(line_math) != 3:
        raise ConfigError(f"{path.name}: checks.line_math must be [quantity, unit price, amount]")
    patterns = d.get("patterns") or {}
    if not isinstance(patterns, dict):
        raise ConfigError(f"{path.name}: checks.patterns must map field names to patterns")
    _fields(path, "checks.patterns", list(patterns), columns)
    return {
        "totals": _fields(path, "checks.totals", d.get("totals"), columns),
        "line_math": line_math,
        "patterns": {f: _regex(path, f"checks.patterns.{f}", p) for f, p in patterns.items()},
        "min_letters_after_strip": int(d.get("min_letters_after_strip", 3)),
        "ascii_only": bool(d.get("ascii_only", True)),
    }


def load_customer(path: Path) -> CustomerProfile:
    d = _load_yaml(path)
    detect = d.get("detect") or {}
    table = _table(path, d.get("table"))
    doc_fields = {}
    for key, pattern in (d.get("document_fields") or {}).items():
        rx = _regex(path, f"document_fields.{key}", pattern)
        if rx.groups < 1:
            raise ConfigError(f"{path.name}: document_fields.{key} needs a (group) around the value")
        doc_fields[str(key)] = rx
    output = d.get("output") or {}
    multi_file = output.get("multi_file", "separate")
    if multi_file not in ("separate", "merge"):
        raise ConfigError(f"{path.name}: output.multi_file must be 'separate' or 'merge'")
    template = d.get("template")
    if not isinstance(template, str) or not template:
        raise ConfigError(f"{path.name}: 'template' (name of a file in config/templates) is required")
    return CustomerProfile(
        id=str(d.get("id") or path.stem),
        name=str(d.get("name") or d.get("id") or path.stem),
        template=template,
        detect_all=_texts(path, "detect.all", detect.get("all")),
        detect_any=_texts(path, "detect.any", detect.get("any")),
        senders=_texts(path, "detect.senders", detect.get("senders")),
        document_fields=doc_fields,
        table=table,
        mapping=_mapping(path, d.get("mapping"), table.columns),
        checks=_checks(path, d.get("checks"), table.columns),
        output_filename=str(output.get("filename") or "CargoWise Input - {source_name}.csv"),
        multi_file=multi_file,
        path=path,
    )


def load_customers() -> dict[str, CustomerProfile]:
    folder = config_dir() / "customers"
    if not folder.is_dir():
        raise ConfigError(f"customer profile folder not found: {folder}")
    profiles: dict[str, CustomerProfile] = {}
    for path in sorted([*folder.glob("*.yaml"), *folder.glob("*.yml")]):
        if path.name.startswith("_"):
            continue
        profile = load_customer(path)
        if profile.id in profiles:
            raise ConfigError(f"customer id '{profile.id}' is used by {profiles[profile.id].path.name} and {path.name}")
        profiles[profile.id] = profile
    return profiles


def load_template_profile(template_id: str) -> TemplateProfile:
    folder = config_dir() / "templates"
    path = folder / f"{template_id}.yaml"
    if not path.is_file():
        raise ConfigError(f"template profile '{template_id}' not found ({path})")
    d = _load_yaml(path)
    csv_d = d.get("csv") or {}
    encoding = str(csv_d.get("encoding", "utf-8"))
    try:
        codecs.lookup(encoding)
    except LookupError as e:
        raise ConfigError(f"{path.name}: unknown csv.encoding '{encoding}'") from e
    control = csv_d.get("header_control_chars", "keep")
    if control not in ("keep", "strip"):
        raise ConfigError(f"{path.name}: csv.header_control_chars must be 'keep' or 'strip'")
    return TemplateProfile(
        id=str(d.get("id") or template_id),
        workbook=folder / str(d.get("workbook") or f"{template_id}.xlsx"),
        sheet=d.get("sheet") or None,
        header_row=int(d.get("header_row", 1)),
        csv_encoding=encoding,
        csv_delimiter=str(csv_d.get("delimiter", ",")),
        csv_line_terminator=str(csv_d.get("line_terminator", "\r\n")),
        header_control_chars=control,
    )
