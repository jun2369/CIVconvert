// Checks static/formula.js against tests/formula_cases.json (run by tests/test_formula.py, or: node tests/formula_check.js)
"use strict";
const path = require("path");
const F = require(path.join(__dirname, "..", "civconvert", "static", "formula.js"));
const data = require(path.join(__dirname, "formula_cases.json"));

function sheetFrom(cells, headerRow) {
  const rows = [];
  for (const [ref, text] of Object.entries(cells)) {
    const m = /^([A-Z]+)(\d+)$/.exec(ref);
    const r = Number(m[2]) - headerRow - 1, c = F.colIndex(m[1]);
    while (rows.length <= r) rows.push({ cells: {} });
    rows[r].cells[c] = text.startsWith("=") ? { f: text } : { v: text };
  }
  return rows;
}

const failures = [];
const rows = sheetFrom(data.cells, data.header_row);
for (const [formula, expected] of data.cases) {
  const sheet = new F.SheetEvaluator(rows, data.header_row, data.headers);
  const got = F.formatValue(F.evaluate(formula, sheet.cell));
  if (got !== expected) failures.push(`${formula}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(got)}`);
}
for (const [x, expected] of data.format) {
  const got = F.formatNumber(x);
  if (got !== expected) failures.push(`format ${x}: expected ${expected}, got ${got}`);
}
for (const [kind, formula, args, expected] of data.move) {
  const got = kind === "delete" ? F.deleteRows(formula, args) : kind === "insert" ? F.insertRows(formula, ...args) : F.shiftFormula(formula, ...args);
  if (got !== expected) failures.push(`${kind} ${formula} ${JSON.stringify(args)}: expected ${expected}, got ${got}`);
}
if (failures.length) {
  console.log(failures.join("\n"));
  process.exit(1);
}
console.log(`ok: ${data.cases.length} formulas, ${data.format.length} formats, ${data.move.length} reference moves`);
