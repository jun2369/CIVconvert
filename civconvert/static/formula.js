/* Excel-style formulas for the online sheet – the browser twin of civconvert/formula.py.
   Same grammar, functions and number format; both are checked against tests/formula_cases.json.
   The server recomputes every value on save, so this one only has to be fast and agree.
   No eval: formulas are parsed and evaluated, they can never run code.
   Also: how references move when rows are deleted / inserted or a formula is copied. */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.CIVFormula = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  class FormulaError {
    constructor(code) { this.code = code; }
    toString() { return this.code; }
  }
  const ERRORS = {};
  for (const c of ["#DIV/0!", "#VALUE!", "#REF!", "#NAME?", "#NUM!", "#N/A", "#NULL!", "#CIRC!", "#ERROR!"]) ERRORS[c] = new FormulaError(c);
  const DIV0 = ERRORS["#DIV/0!"], VALUE = ERRORS["#VALUE!"], NAME = ERRORS["#NAME?"], NUM = ERRORS["#NUM!"];
  const CIRC = ERRORS["#CIRC!"], SYNTAX = ERRORS["#ERROR!"];
  const isError = (v) => v instanceof FormulaError;
  class SyntaxErr extends Error {}

  // ───────────── references ─────────────
  function colLetter(index) {
    let s = "";
    for (let n = index + 1; n > 0; n = Math.floor((n - 1) / 26)) s = String.fromCharCode(65 + ((n - 1) % 26)) + s;
    return s;
  }
  function colIndex(letters) {
    let n = 0;
    for (const ch of letters.toUpperCase()) n = n * 26 + ch.charCodeAt(0) - 64;
    return n - 1;
  }
  const REF = /^(\$?)([A-Za-z]{1,3})(\$?)(\d+)$/;
  function parseRef(text) {
    const m = REF.exec(text);
    return { absCol: !!m[1], col: colIndex(m[2]), absRow: !!m[3], row: Number(m[4]) }; // row: Excel row number
  }
  const refText = (r) => `${r.absCol ? "$" : ""}${colLetter(r.col)}${r.absRow ? "$" : ""}${r.row}`;

  // ───────────── tokens ─────────────
  const TOKEN = /(\s+)|((?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)|("(?:[^"]|"")*")|(#(?:DIV\/0!|VALUE!|REF!|NAME\?|NUM!|N\/A|NULL!|CIRC!|ERROR!))|(\$?[A-Za-z]{1,3}\$?\d+(?![A-Za-z0-9_.(]))|([A-Za-z_][A-Za-z0-9_.]*)|(<=|>=|<>|[-+*/^&=<>%:,()])/y;
  const KINDS = [null, "ws", "num", "str", "err", "ref", "name", "op"];

  function tokenize(text) {
    const tokens = [];
    TOKEN.lastIndex = 0;
    while (TOKEN.lastIndex < text.length) {
      const start = TOKEN.lastIndex;
      const m = TOKEN.exec(text);
      if (!m) throw new SyntaxErr(`unexpected character ${JSON.stringify(text[start])}`);
      const kind = KINDS[m.findIndex((g, i) => i > 0 && g !== undefined)];
      if (kind !== "ws") tokens.push({ kind, text: m[0], start, end: TOKEN.lastIndex });
    }
    return tokens;
  }

  /** The cell references in a formula, also while it is still being typed (what cannot be read yet is skipped):
      [{ c1, r1, c2, r2 }] – columns 0-based, rows = Excel row numbers. For outlining them while the formula is typed. */
  function references(formula) {
    const text = formula.startsWith("=") ? formula.slice(1) : formula;
    const tokens = [];
    TOKEN.lastIndex = 0;
    while (TOKEN.lastIndex < text.length) {
      const start = TOKEN.lastIndex;
      const m = TOKEN.exec(text);
      if (!m) {
        if (text[start] === '"') break; // an unfinished "text": the rest is inside it
        TOKEN.lastIndex = start + 1;
        continue;
      }
      const kind = KINDS[m.findIndex((g, i) => i > 0 && g !== undefined)];
      if (kind !== "ws") tokens.push({ kind, text: m[0] });
    }
    const out = [];
    for (let i = 0; i < tokens.length; i++) {
      if (tokens[i].kind !== "ref") continue;
      const a = parseRef(tokens[i].text);
      let b = a;
      if (tokens[i + 1] && tokens[i + 1].text === ":" && tokens[i + 2] && tokens[i + 2].kind === "ref") { b = parseRef(tokens[i + 2].text); i += 2; }
      out.push({ c1: Math.min(a.col, b.col), r1: Math.min(a.row, b.row), c2: Math.max(a.col, b.col), r2: Math.max(a.row, b.row) });
    }
    return out;
  }

  // ───────────── parser (AST like formula.py: ["num", x], ["ref", col, row0], ["range", c1, r1, c2, r2] …) ─────────────
  function parse(formula) {
    const text = formula.startsWith("=") ? formula.slice(1) : formula;
    const t = tokenize(text);
    let i = 0;
    const peek = () => t[i];
    const take = () => { if (i >= t.length) throw new SyntaxErr("the formula ends too early"); return t[i++]; };
    const isOp = (...ops) => i < t.length && t[i].kind === "op" && ops.includes(t[i].text);
    const expect = (op) => { const tok = take(); if (tok.kind !== "op" || tok.text !== op) throw new SyntaxErr(`expected ${op}`); };

    function comparison() {
      let node = concat();
      while (isOp("=", "<>", "<", ">", "<=", ">=")) node = ["bin", take().text, node, concat()];
      return node;
    }
    function concat() {
      let node = additive();
      while (isOp("&")) { take(); node = ["bin", "&", node, additive()]; }
      return node;
    }
    function additive() {
      let node = multiplicative();
      while (isOp("+", "-")) node = ["bin", take().text, node, multiplicative()];
      return node;
    }
    function multiplicative() {
      let node = power();
      while (isOp("*", "/")) node = ["bin", take().text, node, power()];
      return node;
    }
    function power() {
      let node = unary();
      while (isOp("^")) { take(); node = ["bin", "^", node, unary()]; }
      return node;
    }
    function unary() { // Excel: -2^2 = 4
      if (isOp("-")) { take(); return ["neg", unary()]; }
      if (isOp("+")) { take(); return unary(); }
      return postfix();
    }
    function postfix() {
      let node = range();
      while (isOp("%")) { take(); node = ["pct", node]; }
      return node;
    }
    function range() {
      let node = primary();
      if (isOp(":")) {
        take();
        const end = primary();
        if (node[0] !== "ref" || end[0] !== "ref") throw new SyntaxErr("a range needs a cell on both sides of ':'");
        node = ["range", Math.min(node[1], end[1]), Math.min(node[2], end[2]), Math.max(node[1], end[1]), Math.max(node[2], end[2])];
      }
      return node;
    }
    function argument() {
      if (isOp(",", ")")) return ["empty"];
      return comparison();
    }
    function primary() {
      const tok = take();
      if (tok.kind === "num") return ["num", Number(tok.text)];
      if (tok.kind === "str") return ["str", tok.text.slice(1, -1).replace(/""/g, '"')];
      if (tok.kind === "err") return ["err", tok.text.toUpperCase()];
      if (tok.kind === "ref") { const r = parseRef(tok.text); return ["ref", r.col, r.row - 1]; }
      if (tok.kind === "name") {
        const name = tok.text.toUpperCase();
        if (isOp("(")) {
          take();
          const args = [];
          if (!isOp(")")) {
            args.push(argument());
            while (isOp(",")) { take(); args.push(argument()); }
          }
          expect(")");
          return ["fn", name, args];
        }
        if (name === "TRUE" || name === "FALSE") return ["bool", name === "TRUE"];
        return ["err", "#NAME?"];
      }
      if (tok.kind === "op" && tok.text === "(") {
        const node = comparison();
        expect(")");
        return node;
      }
      throw new SyntaxErr(`unexpected ${tok.text}`);
    }

    const node = comparison();
    if (i < t.length) throw new SyntaxErr(`unexpected ${peek().text}`);
    return node;
  }

  // ───────────── values ─────────────
  const NUMERIC = /^(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/;
  const THOUSANDS = /^\d{1,3}(?:,\d{3})+(?:\.\d*)?$/;
  // "50", "-7.5", "1,600.00", "1e3" – and, as Excel reads a typed value, "$20", "-$1,234.50", "20%" (= number_from_text)
  function numberFromText(text) {
    let s = String(text).trim();
    const percent = s.endsWith("%");
    if (percent) s = s.slice(0, -1);
    let sign = "";
    if (s[0] === "+" || s[0] === "-") { sign = s[0]; s = s.slice(1); }
    if (s[0] === "$" && !percent) {
      s = s.slice(1);
      if (!sign && (s[0] === "+" || s[0] === "-")) { sign = s[0]; s = s.slice(1); }
    }
    if (THOUSANDS.test(s)) s = s.replace(/,/g, "");
    if (!NUMERIC.test(s)) return null;
    const n = Number(sign + s);
    return percent ? n / 100 : n;
  }
  function toNumber(v) {
    if (isError(v)) return v;
    if (typeof v === "boolean") return v ? 1 : 0;
    if (typeof v === "number") return v;
    if (v === null || v === undefined || v === "") return 0;
    const n = numberFromText(v);
    return n === null ? VALUE : n;
  }
  function toText(v) {
    if (isError(v)) return v;
    if (v === null || v === undefined) return "";
    if (typeof v === "boolean") return v ? "TRUE" : "FALSE";
    if (typeof v === "number") return formatNumber(v);
    return String(v);
  }
  function toBool(v) {
    if (isError(v)) return v;
    if (typeof v === "boolean") return v;
    if (v === null || v === undefined || v === "") return false;
    if (typeof v === "number") return v !== 0;
    const s = String(v).trim().toUpperCase();
    if (s === "TRUE" || s === "FALSE") return s === "TRUE";
    const n = numberFromText(s);
    return n === null ? VALUE : n !== 0;
  }

  // Excel's General format: up to 15 significant digits, no trailing zeros (= format_number in formula.py)
  function formatNumber(x) {
    if (x === 0) return "0";
    const sign = x < 0 ? "-" : "";
    const [m, e] = Math.abs(x).toExponential(14).split("e");
    const exp = Number(e);
    const digits = m.replace(".", "").replace(/0+$/, "") || "0";
    if (exp >= 15 || exp < -9) {
      const mant = digits[0] + (digits.length > 1 ? "." + digits.slice(1) : "");
      return `${sign}${mant}E${exp >= 0 ? "+" : "-"}${String(Math.abs(exp)).padStart(2, "0")}`;
    }
    if (exp >= 0) {
      const frac = digits.slice(exp + 1);
      return sign + digits.slice(0, exp + 1).padEnd(exp + 1, "0") + (frac ? "." + frac : "");
    }
    return sign + "0." + "0".repeat(-exp - 1) + digits;
  }
  function formatValue(v) {
    if (isError(v)) return v.code;
    const t = toText(v);
    return isError(t) ? t.code : t;
  }
  const finite = (x) => (Number.isFinite(x) ? x : NUM);

  // ROUND / ROUNDUP / ROUNDDOWN on the decimal digits (1.005 -> 1.01, like Excel)
  function shift(v, k) {
    const [m, e] = v.toExponential().split("e");
    return Number(`${m}e${Number(e) + k}`);
  }
  function roundTo(x, n, mode) {
    n = Math.trunc(n);
    const sign = x < 0 ? -1 : 1;
    const a = shift(Math.abs(x), n);
    const f = Math.floor(a);
    const r = mode === "half" ? (a - f >= 0.5 ? f + 1 : f) : mode === "up" ? Math.ceil(a) : f;
    const out = sign * shift(r, -n);
    return out === 0 ? 0 : out;
  }

  // ───────────── evaluation ─────────────
  const EMPTY = { empty: true };
  class Range extends Array {}

  function compare(op, a, b) {
    if (isError(a)) return a;
    if (isError(b)) return b;
    const num = (v) => (typeof v === "boolean" ? null : v === null || v === undefined ? 0 : typeof v === "number" ? v : numberFromText(v));
    let x = num(a), y = num(b);
    if (x === null || y === null) { x = String(toText(a)).toLowerCase(); y = String(toText(b)).toLowerCase(); }
    switch (op) {
      case "=": return x === y;
      case "<>": return x !== y;
      case "<": return x < y;
      case ">": return x > y;
      case "<=": return x <= y;
      default: return x >= y;
    }
  }

  function numbers(args) {
    const out = [];
    for (const arg of args) {
      if (arg instanceof Range) {
        for (const v of arg) {
          if (isError(v)) return v;
          if (typeof v === "boolean" || v === null || v === undefined) continue;
          const n = typeof v === "number" ? v : numberFromText(v);
          if (n !== null) out.push(n);
        }
      } else {
        const n = toNumber(arg);
        if (isError(n)) return n;
        out.push(n);
      }
    }
    return out;
  }
  const flat = (args) => args.flatMap((a) => (a instanceof Range ? [...a] : [a]));
  const sumOf = (nums) => { // Neumaier summation, close to Python's math.fsum
    let s = 0, c = 0;
    for (const x of nums) {
      const t = s + x;
      c += Math.abs(s) >= Math.abs(x) ? (s - t) + x : (x - t) + s;
      s = t;
    }
    return s + c;
  };
  const textArgs = (fn) => (...args) => {
    const texts = args.map(toText);
    const bad = texts.find(isError);
    return bad || fn(...texts);
  };
  const chars = (s) => Array.from(s);
  const num1 = (fn) => (x) => { x = toNumber(x); return isError(x) ? x : fn(x); };
  const nums2 = (fn) => (a, b) => {
    a = toNumber(a); b = toNumber(b);
    if (isError(a)) return a;
    if (isError(b)) return b;
    return fn(a, b);
  };
  function pow(a, b) {
    if (a === 0 && b < 0) return DIV0;
    const r = Math.pow(a, b);
    return Number.isNaN(r) ? NUM : finite(r);
  }
  const roundFn = (mode) => (x, n = 0) => {
    x = toNumber(x); n = toNumber(n);
    if (isError(x)) return x;
    if (isError(n)) return n;
    return roundTo(x, n, mode);
  };
  const logic = (combine) => (...args) => {
    const values = [];
    for (const a of args) {
      for (const v of a instanceof Range ? a : [a]) {
        if (isError(v)) return v;
        if (v === null || v === undefined || (a instanceof Range && typeof v === "string" && numberFromText(v) === null)) continue;
        const b = toBool(v);
        if (isError(b)) return b;
        values.push(b);
      }
    }
    return values.length ? combine(values) : VALUE;
  };
  const minmax = (pick) => (...args) => {
    const n = numbers(args);
    if (isError(n)) return n;
    return n.length ? n.reduce((a, b) => pick(a, b)) : 0; // no spread: ranges can be long
  };

  const FUNCTIONS = { // name: [function, min args, max args]
    SUM: [(...a) => { const n = numbers(a); return isError(n) ? n : finite(sumOf(n)); }, 1, 255],
    AVERAGE: [(...a) => { const n = numbers(a); return isError(n) ? n : n.length ? finite(sumOf(n) / n.length) : DIV0; }, 1, 255],
    MIN: [minmax(Math.min), 1, 255],
    MAX: [minmax(Math.max), 1, 255],
    COUNT: [(...a) => flat(a).filter((v) => !isError(v) && typeof v !== "boolean" && v !== null && v !== undefined && (typeof v === "number" || numberFromText(v) !== null)).length, 1, 255],
    COUNTA: [(...a) => flat(a).filter((v) => v !== null && v !== undefined && v !== "").length, 1, 255],
    PRODUCT: [(...a) => { const n = numbers(a); return isError(n) ? n : n.length ? finite(n.reduce((p, x) => p * x, 1)) : 0; }, 1, 255],
    ROUND: [roundFn("half"), 1, 2],
    ROUNDUP: [roundFn("up"), 1, 2],
    ROUNDDOWN: [roundFn("down"), 1, 2],
    INT: [num1((x) => Math.floor(x)), 1, 1],
    ABS: [num1((x) => Math.abs(x)), 1, 1],
    SQRT: [num1((x) => (x < 0 ? NUM : Math.sqrt(x))), 1, 1],
    MOD: [nums2((a, b) => (b === 0 ? DIV0 : finite(a - b * Math.floor(a / b)))), 2, 2],
    POWER: [nums2(pow), 2, 2],
    AND: [logic((v) => v.every(Boolean)), 1, 255],
    OR: [logic((v) => v.some(Boolean)), 1, 255],
    NOT: [(x) => { const b = toBool(x); return isError(b) ? b : !b; }, 1, 1],
    CONCAT: [(...a) => textArgs((...t) => t.join(""))(...flat(a)), 1, 255],
    CONCATENATE: [(...a) => textArgs((...t) => t.join(""))(...flat(a)), 1, 255],
    LEFT: [(t, n = 1) => textArgs((s) => { n = toNumber(n); if (isError(n)) return n; return n < 0 ? VALUE : chars(s).slice(0, Math.trunc(n)).join(""); })(t), 1, 2],
    RIGHT: [(t, n = 1) => textArgs((s) => { n = toNumber(n); if (isError(n)) return n; if (n < 0) return VALUE; const c = chars(s); return Math.trunc(n) ? c.slice(Math.max(0, c.length - Math.trunc(n))).join("") : ""; })(t), 1, 2],
    MID: [(t, st, n) => textArgs((s) => {
      st = toNumber(st); n = toNumber(n);
      if (isError(st)) return st;
      if (isError(n)) return n;
      if (st < 1 || n < 0) return VALUE;
      return chars(s).slice(Math.trunc(st) - 1, Math.trunc(st) - 1 + Math.trunc(n)).join("");
    })(t), 3, 3],
    LEN: [textArgs((s) => chars(s).length), 1, 1],
    UPPER: [textArgs((s) => s.toUpperCase()), 1, 1],
    LOWER: [textArgs((s) => s.toLowerCase()), 1, 1],
    TRIM: [textArgs((s) => s.replace(/ +/g, " ").replace(/^ +| +$/g, "")), 1, 1],
    VALUE: [(v) => { if (typeof v === "number") return v; const t = toText(v); if (isError(t)) return t; const n = numberFromText(t); return n === null ? VALUE : n; }, 1, 1],
  };
  const FUNCTION_NAMES = [...Object.keys(FUNCTIONS), "IF", "IFERROR"].sort();

  function evaluateNode(node, resolve) {
    const scalar = (n) => {
      const v = ev(n);
      if (v instanceof Range) return VALUE;
      return v === EMPTY ? null : v;
    };
    function binary(op, a, b) {
      if (op === "&") {
        const ta = toText(a), tb = toText(b);
        if (isError(ta)) return ta;
        return isError(tb) ? tb : ta + tb;
      }
      if (["=", "<>", "<", ">", "<=", ">="].includes(op)) return compare(op, a, b);
      const x = toNumber(a), y = toNumber(b);
      if (isError(x)) return x;
      if (isError(y)) return y;
      switch (op) {
        case "+": return finite(x + y);
        case "-": return finite(x - y);
        case "*": return finite(x * y);
        case "/": return y === 0 ? DIV0 : finite(x / y);
        case "^": return pow(x, y);
        default: return SYNTAX;
      }
    }
    function call(name, args) {
      if (name === "IF") {
        if (args.length < 2 || args.length > 3) return VALUE;
        const cond = toBool(scalar(args[0]));
        if (isError(cond)) return cond;
        const branch = cond ? args[1] : args.length > 2 ? args[2] : ["bool", false];
        const v = scalar(branch);
        return v === null && branch[0] === "empty" ? 0 : v;
      }
      if (name === "IFERROR") {
        if (args.length !== 2) return VALUE;
        const v = scalar(args[0]);
        return isError(v) ? scalar(args[1]) : v;
      }
      const spec = FUNCTIONS[name];
      if (!spec) return NAME;
      if (args.length < spec[1] || args.length > spec[2]) return VALUE;
      const values = args.map((a) => { const v = ev(a); return v === EMPTY ? null : v; });
      try { return spec[0](...values); } catch { return VALUE; }
    }
    function ev(n) {
      switch (n[0]) {
        case "num": case "str": case "bool": return n[1];
        case "err": return ERRORS[n[1]] || SYNTAX;
        case "empty": return EMPTY;
        case "ref": return resolve(n[1], n[2]);
        case "range": {
          const [, c1, r1, c2, r2] = n;
          if ((r2 - r1 + 1) * (c2 - c1 + 1) > 1e6) return VALUE;
          const out = new Range();
          for (let r = r1; r <= r2; r++) for (let c = c1; c <= c2; c++) out.push(resolve(c, r));
          return out;
        }
        case "neg": { const x = toNumber(scalar(n[1])); return isError(x) ? x : -x; }
        case "pct": { const x = toNumber(scalar(n[1])); return isError(x) ? x : x / 100; }
        case "bin": return binary(n[1], scalar(n[2]), scalar(n[3]));
        case "fn": return call(n[1], n[2]);
        default: return SYNTAX;
      }
    }
    const value = ev(node);
    if (value instanceof Range) return VALUE;
    return value === null || value === undefined || value === EMPTY ? 0 : value;
  }

  function evaluate(formula, resolve) {
    let node;
    try { node = parse(formula); } catch { return SYNTAX; }
    return evaluateNode(node, resolve);
  }

  // A sheet: rows = [{cells: {"<col>": {v, f}}}]; the template header is Excel row headerRow,
  // data row i is Excel row headerRow + 1 + i.
  class SheetEvaluator {
    constructor(rows, headerRow = 1, headers = []) {
      this.rows = rows; this.headerRow = headerRow; this.headers = headers;
      this.cache = new Map(); this.active = new Set(); this.parsed = new Map();
      this.cell = (col, row0) => this.at(col, row0);
    }
    at(col, excelRow0) {
      if (excelRow0 + 1 === this.headerRow) return col >= 0 && col < this.headers.length ? this.headers[col] : null;
      const r = excelRow0 - this.headerRow;
      if (r < 0 || r >= this.rows.length || col < 0) return null;
      return this.value(r, col);
    }
    value(r, col) {
      const key = r * 100000 + col;
      if (this.cache.has(key)) return this.cache.get(key);
      const raw = this.rows[r].cells && this.rows[r].cells[col];
      if (!raw) return null;
      if (!raw.f) return raw.v === undefined || raw.v === "" ? null : raw.v;
      if (this.active.has(key)) return CIRC;
      this.active.add(key);
      let result;
      try {
        let node = this.parsed.get(raw.f);
        if (node === undefined) {
          try { node = parse(raw.f); } catch { node = null; }
          this.parsed.set(raw.f, node);
        }
        result = node ? evaluateNode(node, this.cell) : SYNTAX;
      } catch (e) {
        result = e instanceof RangeError ? CIRC : VALUE; // RangeError: the call stack is exhausted
      } finally {
        this.active.delete(key);
      }
      this.cache.set(key, result);
      return result;
    }
  }

  // ───────────── moving references (rows deleted / inserted, formula copied) ─────────────
  // Rewrites the reference tokens and keeps every other character as typed.
  function rewrite(formula, fix) {
    if (!formula.startsWith("=")) return formula;
    let tokens;
    try { tokens = tokenize(formula.slice(1)); } catch { return formula; }
    let out = "=", pos = 0;
    const body = formula.slice(1);
    for (let i = 0; i < tokens.length; i++) {
      const t = tokens[i];
      if (t.kind !== "ref") continue;
      const isRange = tokens[i + 1] && tokens[i + 1].kind === "op" && tokens[i + 1].text === ":" && tokens[i + 2] && tokens[i + 2].kind === "ref";
      const end = isRange ? tokens[i + 2] : t;
      const replacement = fix(parseRef(t.text), isRange ? parseRef(end.text) : null);
      out += body.slice(pos, t.start) + replacement;
      pos = end.end;
      if (isRange) i += 2;
    }
    return out + body.slice(pos);
  }
  const join = (a, b) => (b ? `${refText(a)}:${refText(b)}` : refText(a));

  /** deleted: Excel row numbers that were removed. */
  function deleteRows(formula, deleted) {
    const gone = [...deleted].sort((a, b) => a - b);
    const below = (row, inclusive) => gone.filter((d) => (inclusive ? d <= row : d < row)).length;
    return rewrite(formula, (a, b) => {
      if (!b) return gone.includes(a.row) ? "#REF!" : refText({ ...a, row: a.row - below(a.row, false) });
      const top = Math.min(a.row, b.row), bottom = Math.max(a.row, b.row);
      const nTop = top - below(top, false), nBottom = bottom - below(bottom, true);
      if (nBottom < nTop) return "#REF!";
      return join({ ...a, row: a.row === top ? nTop : nBottom }, { ...b, row: b.row === top && a.row !== top ? nTop : nBottom });
    });
  }

  /** count rows inserted so that the first new row has Excel row number `at`. */
  function insertRows(formula, at, count) {
    return rewrite(formula, (a, b) => {
      if (!b) return refText({ ...a, row: a.row >= at ? a.row + count : a.row });
      const top = Math.min(a.row, b.row), bottom = Math.max(a.row, b.row);
      const nTop = top >= at ? top + count : top, nBottom = bottom >= at ? bottom + count : bottom;
      return join({ ...a, row: a.row === top ? nTop : nBottom }, { ...b, row: b.row === top && a.row !== top ? nTop : nBottom });
    });
  }

  /** A formula copied dRows down / dCols right: relative references move, $ references stay. */
  function shiftFormula(formula, dRows, dCols) {
    const move = (r) => {
      const row = r.absRow ? r.row : r.row + dRows, col = r.absCol ? r.col : r.col + dCols;
      return row < 1 || col < 0 ? null : { ...r, row, col };
    };
    return rewrite(formula, (a, b) => {
      const ma = move(a), mb = b && move(b);
      if (!ma || (b && !mb)) return "#REF!";
      return join(ma, mb);
    });
  }

  return {
    FormulaError, ERRORS, isError, parse, tokenize, references, evaluate, SheetEvaluator,
    formatNumber, formatValue, toNumber, numberFromText, colLetter, colIndex,
    deleteRows, insertRows, shiftFormula, FUNCTION_NAMES,
  };
});
