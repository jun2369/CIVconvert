"""Command line:  python -m civconvert <command>

  convert <pdf>...   convert PDF(s); the CSV is saved next to each PDF (or --out-dir)
  customers          list the configured customers
  check-config       validate every customer profile against its template
  serve              start the web page / API locally
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import webbrowser
from pathlib import Path

from . import __version__
from .pipeline import ConversionError, Failure, InputFile, Result, convert_many, resolve_columns
from .profiles import ConfigError, load_customers, load_template_profile
from .template import load_template


def _print_result(r: Result, saved: list[Path]) -> None:
    print(f"[{'REVIEW' if r.needs_review else 'OK'}] {', '.join(r.sources)}  ->  {r.filename}")
    info = "  ".join(f"{k}: {v}" for k, v in r.document.items())
    print(f"  customer: {r.customer.id}  lines: {len(r.rows)}  {info}")
    for c in r.checks:
        print(f"  {'ok  ' if c.ok else 'FAIL'} {c.name}: {c.detail}")
    for w in r.warnings:
        print(f"  !    {w}")
    for p in saved:
        print(f"  saved: {p}")


def cmd_convert(args: argparse.Namespace) -> int:
    files, folders = [], {}
    for p in args.pdf:
        if not p.is_file():
            print(f"error: file not found: {p}", file=sys.stderr)
            return 1
        files.append(InputFile(p.name, p.read_bytes()))
        folders.setdefault(p.name, p.resolve().parent)
    merge = True if args.merge else False if args.separate else None
    try:
        results = convert_many(files, args.customer, args.sender, merge)
    except (ConversionError, ConfigError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    failed, report = 0, []
    for r in results:
        if isinstance(r, Failure):
            failed += 1
            print(f"[ERROR] {', '.join(r.sources)}: {r.error}", file=sys.stderr)
            report.append(r.to_dict())
            continue
        folder = args.out_dir or folders[r.sources[0]]
        folder.mkdir(parents=True, exist_ok=True)
        saved = [folder / r.filename]
        saved[0].write_bytes(r.csv_bytes())
        if args.xlsx:
            saved.append(folder / r.xlsx_filename)
            saved[1].write_bytes(r.xlsx_bytes())
        report.append({**r.to_dict(), "saved": [str(p) for p in saved]})
        if not args.json:
            _print_result(r, saved)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if failed else 0


def cmd_customers(_: argparse.Namespace) -> int:
    try:
        profiles = load_customers()
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    for p in profiles.values():
        print(f"{p.id:<16} {p.name}  (template: {p.template}, file: {p.path.name})")
    return 0


def cmd_check_config(_: argparse.Namespace) -> int:
    try:
        profiles = load_customers()
    except ConfigError as e:
        print(f"FAIL {e}", file=sys.stderr)
        return 1
    bad = 0
    for p in profiles.values():
        try:
            template = load_template(load_template_profile(p.template))
            cols = resolve_columns(p, template)
            print(f"ok   {p.path.name}: {len(cols)} columns -> {', '.join(f'{c.letter} {c.header}' for c in cols)}")
        except ConfigError as e:
            bad += 1
            print(f"FAIL {p.path.name}: {e}", file=sys.stderr)
    if not profiles:
        print("no customer profiles found", file=sys.stderr)
    return 1 if bad or not profiles else 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    url = f"http://{host}:{args.port}"
    if args.open:
        threading.Timer(1.5, webbrowser.open, [url]).start()
    print(f"CIVconvert {__version__}: {url}   (Ctrl+C to stop)")
    uvicorn.run("civconvert.api:app", host=args.host, port=args.port)
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:  # Windows consoles default to cp1252, which cannot print Chinese
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(prog="civconvert", description="Commercial Invoice PDF -> CargoWise CSV")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    c = sub.add_parser("convert", help="convert PDF file(s) to CargoWise CSV")
    c.add_argument("pdf", nargs="+", type=Path)
    c.add_argument("-c", "--customer", help="customer id (default: detect from the PDF)")
    c.add_argument("--sender", help="sender e-mail, used to detect the customer")
    group = c.add_mutually_exclusive_group()
    group.add_argument("--merge", action="store_true", help="the PDFs are parts of ONE invoice")
    group.add_argument("--separate", action="store_true", help="each PDF is its own invoice")
    c.add_argument("-o", "--out-dir", type=Path, help="folder for the CSV (default: next to the PDF)")
    c.add_argument("--xlsx", action="store_true", help="also save the filled template as .xlsx")
    c.add_argument("--json", action="store_true", help="print the result as JSON")
    c.set_defaults(func=cmd_convert)

    sub.add_parser("customers", help="list configured customers").set_defaults(func=cmd_customers)
    sub.add_parser("check-config", help="validate customer profiles and templates").set_defaults(func=cmd_check_config)

    s = sub.add_parser("serve", help="run the web page / API")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--open", action="store_true", help="open the page in the browser")
    s.set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)
    return args.func(args)
