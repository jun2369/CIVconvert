"""Excel-style formulas for the online sheet (=SUM(G2:G9), =ROUND(G2*H2,2), =IF(F2="","?",F2) …).

The browser has a twin of this engine (static/formula.js) for instant results; this one is
authoritative: values are recomputed here on every save and for the CSV export. Both are
checked against the same cases in tests/formula_cases.json. No eval – a formula is parsed
and evaluated, it can never run code.

Differences from Excel, on purpose: text that looks like a number ("50", "1,600.00") counts
as a number in calculations, because every value read from a PDF is text.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, ROUND_UP, Decimal
from typing import Any, Callable


@dataclass(frozen=True)
class FormulaError:
    code: str  # #DIV/0!  #VALUE!  #REF!  #NAME?  #NUM!  #N/A  #CIRC!  #ERROR!

    def __str__(self) -> str:
        return self.code


DIV0, VALUE, REF, NAME, NUM, NA, CIRC, SYNTAX = (
    FormulaError(c) for c in ("#DIV/0!", "#VALUE!", "#REF!", "#NAME?", "#NUM!", "#N/A", "#CIRC!", "#ERROR!")
)
ERROR_CODES = {e.code: e for e in (DIV0, VALUE, REF, NAME, NUM, NA, CIRC, SYNTAX, FormulaError("#NULL!"))}


class FormulaSyntaxError(ValueError):
    pass


# ───────────────────────────── cell references ─────────────────────────────


def col_letter(index: int) -> str:
    s, n = "", index + 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def col_index(letters: str) -> int:
    n = 0
    for ch in letters.upper():
        n = n * 26 + ord(ch) - 64
    return n - 1


# ───────────────────────────── tokens ─────────────────────────────

_TOKEN = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<num>(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)
  | (?P<str>"(?:[^"]|"")*")
  | (?P<err>\#(?:DIV/0!|VALUE!|REF!|NAME\?|NUM!|N/A|NULL!|CIRC!|ERROR!))
  | (?P<ref>\$?[A-Za-z]{1,3}\$?\d+(?![A-Za-z0-9_.(]))
  | (?P<name>[A-Za-z_][A-Za-z0-9_.]*)
  | (?P<op><=|>=|<>|[-+*/^&=<>%:,()])
    """,
    re.VERBOSE,
)
_REF = re.compile(r"(\$?)([A-Za-z]{1,3})(\$?)(\d+)")


def tokenize(text: str) -> list[tuple[str, str]]:
    tokens, pos = [], 0
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m:
            raise FormulaSyntaxError(f"unexpected character {text[pos]!r}")
        kind = m.lastgroup
        if kind != "ws":
            tokens.append((kind, m.group()))
        pos = m.end()
    return tokens


# ───────────────────────────── parser ─────────────────────────────
# AST: ("num", x) ("str", s) ("bool", b) ("err", code) ("ref", col, row) ("range", c1, r1, c2, r2)
#      ("fn", NAME, [args]) ("neg", x) ("pct", x) ("bin", op, a, b)   – rows/cols are 0-based


class _Parser:
    def __init__(self, tokens: list[tuple[str, str]]):
        self.tokens, self.i = tokens, 0

    def peek(self) -> tuple[str, str] | None:
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def take(self) -> tuple[str, str]:
        tok = self.peek()
        if tok is None:
            raise FormulaSyntaxError("the formula ends too early")
        self.i += 1
        return tok

    def is_op(self, *ops: str) -> bool:
        tok = self.peek()
        return tok is not None and tok[0] == "op" and tok[1] in ops

    def expect(self, op: str) -> None:
        tok = self.take()
        if tok != ("op", op):
            raise FormulaSyntaxError(f"expected {op!r}")

    def parse(self):
        node = self.comparison()
        if self.peek() is not None:
            raise FormulaSyntaxError(f"unexpected {self.peek()[1]!r}")
        return node

    def comparison(self):
        node = self.concat()
        while self.is_op("=", "<>", "<", ">", "<=", ">="):
            node = ("bin", self.take()[1], node, self.concat())
        return node

    def concat(self):
        node = self.additive()
        while self.is_op("&"):
            self.take()
            node = ("bin", "&", node, self.additive())
        return node

    def additive(self):
        node = self.multiplicative()
        while self.is_op("+", "-"):
            node = ("bin", self.take()[1], node, self.multiplicative())
        return node

    def multiplicative(self):
        node = self.power()
        while self.is_op("*", "/"):
            node = ("bin", self.take()[1], node, self.power())
        return node

    def power(self):
        node = self.unary()
        while self.is_op("^"):
            self.take()
            node = ("bin", "^", node, self.unary())
        return node

    def unary(self):  # Excel: -2^2 = 4, negation binds tighter than ^
        if self.is_op("-"):
            self.take()
            return ("neg", self.unary())
        if self.is_op("+"):
            self.take()
            return self.unary()
        return self.postfix()

    def postfix(self):
        node = self.range_()
        while self.is_op("%"):
            self.take()
            node = ("pct", node)
        return node

    def range_(self):
        node = self.primary()
        if self.is_op(":"):
            self.take()
            end = self.primary()
            if node[0] != "ref" or end[0] != "ref":
                raise FormulaSyntaxError("a range needs a cell on both sides of ':'")
            c1, c2 = sorted((node[1], end[1]))
            r1, r2 = sorted((node[2], end[2]))
            node = ("range", c1, r1, c2, r2)
        return node

    def primary(self):
        kind, text = self.take()
        if kind == "num":
            return ("num", float(text))
        if kind == "str":
            return ("str", text[1:-1].replace('""', '"'))
        if kind == "err":
            return ("err", text.upper())
        if kind == "ref":
            m = _REF.fullmatch(text)
            return ("ref", col_index(m.group(2)), int(m.group(4)) - 1)
        if kind == "name":
            name = text.upper()
            if self.is_op("("):
                self.take()
                args = []
                if not self.is_op(")"):
                    args.append(self.argument())
                    while self.is_op(","):
                        self.take()
                        args.append(self.argument())
                self.expect(")")
                return ("fn", name, args)
            if name in ("TRUE", "FALSE"):
                return ("bool", name == "TRUE")
            return ("err", NAME.code)
        if (kind, text) == ("op", "("):
            node = self.comparison()
            self.expect(")")
            return node
        raise FormulaSyntaxError(f"unexpected {text!r}")

    def argument(self):
        if self.is_op(",", ")"):  # empty argument: IF(A1,,1)
            return ("empty",)
        return self.comparison()


def parse(formula: str):
    text = formula[1:] if formula.startswith("=") else formula
    return _Parser(tokenize(text)).parse()


# ───────────────────────────── values ─────────────────────────────

_NUMERIC = re.compile(r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")
_THOUSANDS = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d*)?")


def number_from_text(text: str) -> float | None:
    """'50', '-7.5', '1,600.00', '1e3' – and, as Excel reads a typed value, '$20', '-$1,234.50', '20%'."""
    s = text.strip()
    percent = s.endswith("%")
    if percent:
        s = s[:-1]
    sign = ""
    if s[:1] in ("+", "-"):
        sign, s = s[0], s[1:]
    if s[:1] == "$" and not percent:
        s = s[1:]
        if not sign and s[:1] in ("+", "-"):
            sign, s = s[0], s[1:]
    if _THOUSANDS.fullmatch(s):
        s = s.replace(",", "")
    if not _NUMERIC.fullmatch(s):
        return None
    n = float(sign + s)
    return n / 100 if percent else n


def to_number(value: Any) -> float | FormulaError:
    if isinstance(value, FormulaError):
        return value
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if value is None or value == "":
        return 0.0
    n = number_from_text(str(value))
    return VALUE if n is None else n


def to_text(value: Any) -> str | FormulaError:
    if isinstance(value, FormulaError):
        return value
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return format_number(float(value))
    return str(value)


def to_bool(value: Any) -> bool | FormulaError:
    if isinstance(value, FormulaError):
        return value
    if isinstance(value, bool):
        return value
    if value is None or value == "":
        return False
    if isinstance(value, (int, float)):
        return value != 0
    s = str(value).strip().upper()
    if s in ("TRUE", "FALSE"):
        return s == "TRUE"
    n = number_from_text(s)
    return VALUE if n is None else n != 0


def format_number(x: float) -> str:
    """Like Excel's General format: up to 15 significant digits, no trailing zeros.

    Identical to formatNumber() in static/formula.js (rounding ties away from zero, like
    JavaScript's toExponential).
    """
    if x == 0:
        return "0"
    sign = "-" if x < 0 else ""
    d = Decimal(abs(x))  # the exact binary value
    exp = d.adjusted()
    q = d.scaleb(14 - exp).to_integral_value(rounding=ROUND_HALF_UP)
    if q >= Decimal(10) ** 15:  # 9.99…95 rounded up to 10.0…
        q, exp = (q / 10).to_integral_value(rounding=ROUND_HALF_UP), exp + 1
    digits = str(int(q)).rstrip("0") or "0"
    if exp >= 15 or exp < -9:
        mant = digits[0] + ("." + digits[1:] if len(digits) > 1 else "")
        return f"{sign}{mant}E{'+' if exp >= 0 else '-'}{abs(exp):02d}"
    if exp >= 0:
        whole = digits[: exp + 1].ljust(exp + 1, "0")
        frac = digits[exp + 1 :]
        return sign + whole + ("." + frac if frac else "")
    return sign + "0." + "0" * (-exp - 1) + digits


def format_value(value: Any) -> str:
    if isinstance(value, FormulaError):
        return value.code
    text = to_text(value)
    return text.code if isinstance(text, FormulaError) else text


def _finite(x: float) -> float | FormulaError:
    return x if math.isfinite(x) else NUM


def _round(x: float, digits: float, mode: str) -> float:
    n = int(digits)
    d = Decimal(repr(x)).scaleb(n)
    rounded = d.to_integral_value(rounding=mode).scaleb(-n)
    return float(rounded)


# ───────────────────────────── evaluation ─────────────────────────────

Resolver = Callable[[int, int], Any]  # (col, row) -> cell value


class _Range(list):
    """Values of a range, row by row (only functions accept ranges)."""


def _compare(op: str, a: Any, b: Any) -> bool | FormulaError:
    for v in (a, b):
        if isinstance(v, FormulaError):
            return v
    na = None if isinstance(a, bool) else (0.0 if a is None else (float(a) if isinstance(a, (int, float)) else number_from_text(a)))
    nb = None if isinstance(b, bool) else (0.0 if b is None else (float(b) if isinstance(b, (int, float)) else number_from_text(b)))
    if na is not None and nb is not None:
        x, y = na, nb
    else:
        x, y = str(to_text(a)).lower(), str(to_text(b)).lower()
    return {"=": x == y, "<>": x != y, "<": x < y, ">": x > y, "<=": x <= y, ">=": x >= y}[op]


def _numbers(args: list[Any], strict_scalars: bool = True) -> list[float] | FormulaError:
    """Numbers for SUM/AVERAGE/MIN/MAX: text in ranges is skipped, text typed as an argument must be a number."""
    out: list[float] = []
    for arg in args:
        if isinstance(arg, _Range):
            for v in arg:
                if isinstance(v, FormulaError):
                    return v
                if isinstance(v, bool) or v is None:
                    continue
                n = float(v) if isinstance(v, (int, float)) else number_from_text(str(v))
                if n is not None:
                    out.append(n)
        else:
            if arg is _EMPTY:
                continue
            n = to_number(arg)
            if isinstance(n, FormulaError):
                if strict_scalars:
                    return n
                continue
            out.append(n)
    return out


_EMPTY = object()


def _flat(args: list[Any]) -> list[Any]:
    out = []
    for a in args:
        out.extend(a if isinstance(a, _Range) else [a])
    return out


def _fn_round(mode):
    def f(x, n=0.0):
        x, n = to_number(x), to_number(n)
        for v in (x, n):
            if isinstance(v, FormulaError):
                return v
        return _round(x, n, mode)

    return f


def _text_fn(fn):
    def f(*args):
        texts = [to_text(a) for a in args]
        for t in texts:
            if isinstance(t, FormulaError):
                return t
        return fn(*texts)

    return f


def _left(text, n=1.0):
    n = to_number(n)
    if isinstance(n, FormulaError):
        return n
    return VALUE if n < 0 else text[: int(n)]


def _right(text, n=1.0):
    n = to_number(n)
    if isinstance(n, FormulaError):
        return n
    if n < 0:
        return VALUE
    return text[len(text) - int(n) :] if int(n) else ""


def _mid(text, start, n):
    start, n = to_number(start), to_number(n)
    for v in (start, n):
        if isinstance(v, FormulaError):
            return v
    if start < 1 or n < 0:
        return VALUE
    return text[int(start) - 1 : int(start) - 1 + int(n)]


def _sum(*args):
    nums = _numbers(list(args))
    return nums if isinstance(nums, FormulaError) else _finite(math.fsum(nums))


def _average(*args):
    nums = _numbers(list(args))
    if isinstance(nums, FormulaError):
        return nums
    return DIV0 if not nums else _finite(math.fsum(nums) / len(nums))


def _minmax(pick):
    def f(*args):
        nums = _numbers(list(args))
        if isinstance(nums, FormulaError):
            return nums
        return pick(nums) if nums else 0.0

    return f


def _product(*args):
    nums = _numbers(list(args))
    if isinstance(nums, FormulaError):
        return nums
    if not nums:
        return 0.0
    p = 1.0
    for n in nums:
        p *= n
    return _finite(p)


def _count(*args):
    return float(sum(
        1 for v in _flat(list(args))
        if not isinstance(v, (bool, FormulaError)) and v is not None and v is not _EMPTY
        and (isinstance(v, (int, float)) or number_from_text(str(v)) is not None)
    ))


def _counta(*args):
    return float(sum(1 for v in _flat(list(args)) if v is not None and v != "" and v is not _EMPTY))


def _logic(combine):
    def f(*args):
        values = []
        for a in args:
            for v in (a if isinstance(a, _Range) else [a]):
                if isinstance(v, FormulaError):
                    return v
                if v is None or v is _EMPTY or (isinstance(a, _Range) and isinstance(v, str) and number_from_text(v) is None):
                    continue  # empty cells and text in ranges are ignored
                b = to_bool(v)
                if isinstance(b, FormulaError):
                    return b
                values.append(b)
        return VALUE if not values else combine(values)

    return f


def _num1(fn):
    def f(x):
        x = to_number(x)
        return x if isinstance(x, FormulaError) else fn(x)

    return f


def _mod(a, b):
    a, b = to_number(a), to_number(b)
    for v in (a, b):
        if isinstance(v, FormulaError):
            return v
    if b == 0:
        return DIV0
    return _finite(a - b * math.floor(a / b))


def _power(a, b):
    a, b = to_number(a), to_number(b)
    for v in (a, b):
        if isinstance(v, FormulaError):
            return v
    return _pow(a, b)


def _pow(a: float, b: float) -> float | FormulaError:
    if a == 0 and b < 0:
        return DIV0
    try:
        r = a**b
    except (OverflowError, ZeroDivisionError):
        return NUM
    return NUM if isinstance(r, complex) else _finite(r)


def _value(text):
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        return float(text)
    t = to_text(text)
    if isinstance(t, FormulaError):
        return t
    n = number_from_text(t)
    return VALUE if n is None else n


def _not(x):
    b = to_bool(x)
    return b if isinstance(b, FormulaError) else not b


def _trim(t):  # like Excel: inner runs of spaces become one space
    return re.sub(" +", " ", t).strip(" ")


FUNCTIONS: dict[str, tuple[Callable[..., Any], int, int]] = {  # name -> (function, min args, max args)
    "SUM": (_sum, 1, 255),
    "AVERAGE": (_average, 1, 255),
    "MIN": (_minmax(min), 1, 255),
    "MAX": (_minmax(max), 1, 255),
    "COUNT": (_count, 1, 255),
    "COUNTA": (_counta, 1, 255),
    "PRODUCT": (_product, 1, 255),
    "ROUND": (_fn_round(ROUND_HALF_UP), 1, 2),
    "ROUNDUP": (_fn_round(ROUND_UP), 1, 2),
    "ROUNDDOWN": (_fn_round(ROUND_DOWN), 1, 2),
    "INT": (_num1(lambda x: float(math.floor(x))), 1, 1),
    "ABS": (_num1(abs), 1, 1),
    "SQRT": (_num1(lambda x: NUM if x < 0 else math.sqrt(x)), 1, 1),
    "MOD": (_mod, 2, 2),
    "POWER": (_power, 2, 2),
    "AND": (_logic(all), 1, 255),
    "OR": (_logic(any), 1, 255),
    "NOT": (_not, 1, 1),
    "CONCAT": (lambda *a: _text_fn(lambda *t: "".join(t))(*_flat(list(a))), 1, 255),
    "CONCATENATE": (lambda *a: _text_fn(lambda *t: "".join(t))(*_flat(list(a))), 1, 255),
    "LEFT": (lambda t, n=1.0: _text_fn(lambda s: _left(s, n))(t), 1, 2),
    "RIGHT": (lambda t, n=1.0: _text_fn(lambda s: _right(s, n))(t), 1, 2),
    "MID": (lambda t, s, n: _text_fn(lambda x: _mid(x, s, n))(t), 3, 3),
    "LEN": (_text_fn(lambda s: float(len(s))), 1, 1),
    "UPPER": (_text_fn(str.upper), 1, 1),
    "LOWER": (_text_fn(str.lower), 1, 1),
    "TRIM": (_text_fn(_trim), 1, 1),
    "VALUE": (_value, 1, 1),
}
LAZY = {"IF", "IFERROR"}  # these evaluate only the branch they return
FUNCTION_NAMES = sorted([*FUNCTIONS, *LAZY])


class Evaluator:
    def __init__(self, resolve: Resolver):
        self.resolve = resolve

    def run(self, node) -> Any:
        value = self.eval(node)
        if isinstance(value, _Range):  # =A1:A3 on its own – no spilling
            return VALUE
        return 0.0 if value is None or value is _EMPTY else value  # =Z99 (an empty cell) shows 0, like Excel

    def eval(self, node) -> Any:
        kind = node[0]
        if kind == "num":
            return node[1]
        if kind in ("str", "bool"):
            return node[1]
        if kind == "err":
            return ERROR_CODES.get(node[1], SYNTAX)
        if kind == "empty":
            return _EMPTY
        if kind == "ref":
            return self.resolve(node[1], node[2])
        if kind == "range":
            _, c1, r1, c2, r2 = node
            if (r2 - r1 + 1) * (c2 - c1 + 1) > 1_000_000:
                return VALUE
            return _Range(self.resolve(c, r) for r in range(r1, r2 + 1) for c in range(c1, c2 + 1))
        if kind == "neg":
            x = to_number(self.scalar(node[1]))
            return x if isinstance(x, FormulaError) else -x
        if kind == "pct":
            x = to_number(self.scalar(node[1]))
            return x if isinstance(x, FormulaError) else x / 100
        if kind == "bin":
            return self.binary(node[1], self.scalar(node[2]), self.scalar(node[3]))
        if kind == "fn":
            return self.call(node[1], node[2])
        return SYNTAX

    def scalar(self, node) -> Any:
        v = self.eval(node)
        if isinstance(v, _Range):
            return VALUE
        return None if v is _EMPTY else v

    def binary(self, op: str, a: Any, b: Any) -> Any:
        if op == "&":
            ta, tb = to_text(a), to_text(b)
            if isinstance(ta, FormulaError):
                return ta
            return tb if isinstance(tb, FormulaError) else ta + tb
        if op in ("=", "<>", "<", ">", "<=", ">="):
            return _compare(op, a, b)
        x, y = to_number(a), to_number(b)
        if isinstance(x, FormulaError):
            return x
        if isinstance(y, FormulaError):
            return y
        if op == "+":
            return _finite(x + y)
        if op == "-":
            return _finite(x - y)
        if op == "*":
            return _finite(x * y)
        if op == "/":
            return DIV0 if y == 0 else _finite(x / y)
        if op == "^":
            return _pow(x, y)
        return SYNTAX

    def call(self, name: str, args: list) -> Any:
        if name == "IF":
            if not 2 <= len(args) <= 3:
                return VALUE
            cond = to_bool(self.scalar(args[0]))
            if isinstance(cond, FormulaError):
                return cond
            branch = args[1] if cond else (args[2] if len(args) > 2 else ("bool", False))
            v = self.scalar(branch)
            return 0.0 if v is None and branch == ("empty",) else v  # IF(A1,,1): an empty branch is 0
        if name == "IFERROR":
            if len(args) != 2:
                return VALUE
            v = self.scalar(args[0])
            return self.scalar(args[1]) if isinstance(v, FormulaError) else v
        if name not in FUNCTIONS:
            return NAME
        fn, lo, hi = FUNCTIONS[name]
        if not lo <= len(args) <= hi:
            return VALUE
        values = [None if v is _EMPTY else v for v in (self.eval(a) for a in args)]
        try:
            return fn(*values)
        except (TypeError, ValueError, OverflowError):
            return VALUE


def evaluate(formula: str, resolve: Resolver) -> Any:
    try:
        node = parse(formula)
    except FormulaSyntaxError:
        return SYNTAX
    return Evaluator(resolve).run(node)


# ───────────────────────────── a whole sheet ─────────────────────────────


class SheetEvaluator:
    """Values of every cell of a sheet stored as rows = [{"cells": {"<col>": {"v": .., "f": ..}}}].

    Excel row numbers: the template header is row `header_row`, data row i is header_row + 1 + i.
    """

    def __init__(self, rows: list[dict[str, Any]], header_row: int = 1, headers: list[str] | None = None):
        self.rows = rows
        self.header_row = header_row
        self.headers = headers or []
        self.cache: dict[tuple[int, int], Any] = {}
        self.active: set[tuple[int, int]] = set()

    def cell(self, col: int, excel_row0: int) -> Any:
        """Value at 0-based column / 0-based Excel row (as references are parsed)."""
        r = excel_row0 + 1 - self.header_row - 1  # data index
        if excel_row0 + 1 == self.header_row:
            return self.headers[col] if 0 <= col < len(self.headers) else None
        if r < 0 or r >= len(self.rows) or col < 0:
            return None
        return self.value(r, col)

    def value(self, r: int, col: int) -> Any:
        key = (r, col)
        if key in self.cache:
            return self.cache[key]
        raw = (self.rows[r].get("cells") or {}).get(str(col))
        if not raw:
            return None
        formula = raw.get("f")
        if not formula:
            v = raw.get("v")
            return None if v in (None, "") else v
        if key in self.active:
            return CIRC
        self.active.add(key)
        try:
            result = evaluate(formula, self.cell)
        except RecursionError:
            result = CIRC
        finally:
            self.active.discard(key)
        self.cache[key] = result
        return result

    def compute(self) -> dict[tuple[int, int], Any]:
        """(data row, col) -> value for every formula cell."""
        out = {}
        for r, row in enumerate(self.rows):
            for c, raw in (row.get("cells") or {}).items():
                if raw and raw.get("f"):
                    out[(r, int(c))] = self.value(r, int(c))
        return out


def recalculate(rows: list[dict[str, Any]], header_row: int = 1, headers: list[str] | None = None) -> list[tuple[int, int, str]]:
    """Store the computed value (as text) in "v" of every formula cell; returns the cells that are errors."""
    errors = []
    for (r, c), value in SheetEvaluator(rows, header_row, headers).compute().items():
        text = format_value(value)
        rows[r]["cells"][str(c)]["v"] = text
        if isinstance(value, FormulaError):
            errors.append((r, c, text))
    return errors
