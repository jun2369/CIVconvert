/* CIVSheet – the online CargoWise sheet.
   An Excel-like grid whose columns are the template's columns: they cannot be renamed, deleted
   or moved. Rows can be edited, inserted and deleted. Formulas are computed with CIVFormula.
   Every change of data is one undoable action:
     { label, cells: [{id, c, before, after}], del: [{index, row}], ins: [{index, row}] }
   (cells by row id, so actions stay valid while rows move). Only the visible rows are drawn.
   While a formula is typed (in the cell or in opts.formulaBar), clicking / dragging over cells or the arrow
   keys put their reference in, like in Excel, and the cells it refers to are outlined in colour. */
(() => {
  "use strict";
  const F = window.CIVFormula;
  const tr = (key, params) => (window.CIVi18n ? window.CIVi18n.t(key, params) : key); // texts: i18n.js
  const flagText = (f) => (window.CIVi18n ? window.CIVi18n.flag(f) : f.message);
  const ROW_H = 24, LETTER_H = 22, HEADER_H = 30, HEAD_H = LETTER_H + HEADER_H, RH_W = 56;
  const OVERSCAN = 8, MAX_UNDO = 300, BOTTOM_SPACE = 120;

  const esc = (s) => String(s).replace(/[&<>"]/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[ch]));
  const clone = (o) => (o == null ? o : JSON.parse(JSON.stringify(o)));
  const same = (a, b) => JSON.stringify(a || null) === JSON.stringify(b || null);
  const newId = () => Math.random().toString(36).slice(2, 10) + Date.now().toString(36).slice(-4);
  const div = (cls) => { const d = document.createElement("div"); d.className = cls; return d; };
  const norm = (g) => ({ r1: Math.min(g.r1, g.r2), r2: Math.max(g.r1, g.r2), c1: Math.min(g.c1, g.c2), c2: Math.max(g.c1, g.c2) });
  const content = (text) => (text === "" || text == null ? null : text.startsWith("=") && text.length > 1 ? { f: text } : { v: text });

  // Excel's clipboard text: tab-separated, cells with tabs / line breaks / quotes are quoted
  function toTSV(grid) {
    const q = (s) => (/[\t\n\r"]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s);
    return grid.map((row) => row.map(q).join("\t")).join("\r\n");
  }
  function parseTSV(text) {
    const rows = [[]];
    let cell = "", i = 0, quoted = false;
    text = text.replace(/\r\n?/g, "\n");
    if (text.endsWith("\n")) text = text.slice(0, -1);
    while (i < text.length) {
      const ch = text[i];
      if (quoted) {
        if (ch === '"' && text[i + 1] === '"') { cell += '"'; i += 2; continue; }
        if (ch === '"') { quoted = false; i++; continue; }
        cell += ch; i++; continue;
      }
      if (ch === '"' && cell === "") { quoted = true; i++; continue; }
      if (ch === "\t") { rows[rows.length - 1].push(cell); cell = ""; i++; continue; }
      if (ch === "\n") { rows[rows.length - 1].push(cell); rows.push([]); cell = ""; i++; continue; }
      cell += ch; i++;
    }
    rows[rows.length - 1].push(cell);
    return rows;
  }

  class Sheet {
    constructor(host, opts) {
      this.opts = opts;
      this.headerRow = opts.headerRow || 1;
      this.roles = opts.roles || {};
      let x = 0;
      this.columns = opts.columns.map((c) => { const col = { ...c, x }; x += c.width; return col; });
      this.width = this.columns.length;
      this.totalW = x;
      this.lnoCol = this.columns.findIndex((c) => this.roles[c.letter] && this.roles[c.letter].line_no);
      this.headers = this.columns.map((c) => c.header);
      this.colByLetter = new Map(this.columns.map((c, i) => [c.letter, i]));
      this.readOnly = !!opts.readOnly;
      this.rows = [];
      this.calc = new Map();
      this.cellFlags = new Map();
      this.rowFlags = new Map();
      this.active = { r: 0, c: 0 };
      this.anchor = { r: 0, c: 0 };
      this.ranges = [{ r1: 0, c1: 0, r2: 0, c2: 0 }];
      this.undoStack = [];
      this.redoStack = [];
      this.editing = null;
      this.point = null; // the reference just put into a formula by pointing at cells
      this.clipboard = null;
      this.marquee = null;
      this.build(host);
    }

    // ───────────── DOM ─────────────
    build(host) {
      host.classList.add("sh");
      this.view = div("sh-view");
      this.content = div("sh-content");
      const head = div("sh-head");
      this.corner = div("sh-corner");
      this.colHead = div("sh-cols");
      head.append(this.corner, this.colHead);
      const body = div("sh-body");
      this.rowHead = div("sh-rh");
      this.cellLayer = div("sh-cells");
      this.cellsBox = div("sh-box");
      this.selBox = div("sh-box sh-selbox");
      this.input = document.createElement("textarea");
      this.input.className = "sh-input";
      this.input.setAttribute("spellcheck", "false");
      this.input.setAttribute("autocomplete", "off");
      this.input.setAttribute("aria-label", tr("sheet.cell_aria"));
      this.cellLayer.append(this.cellsBox, this.selBox, this.input);
      body.append(this.rowHead, this.cellLayer);
      this.content.append(head, body);
      this.view.append(this.content);
      this.empty = div("sh-empty");
      host.append(this.view, this.empty);
      this.colHead.style.width = this.cellLayer.style.width = this.totalW + "px";
      this.colHead.innerHTML = this.columns.map((c, i) => {
        const role = this.roles[c.letter];
        const target = role && role.field ? " target" : "";
        const tip = role && role.field ? tr("sheet.from_pdf", { field: role.field }) : role && role.line_no ? tr("sheet.auto_no") : "";
        return `<div class="sh-l${target}" data-c="${i}" style="left:${c.x}px;width:${c.width}px">${c.letter}</div>` +
          `<div class="sh-h${target}" data-c="${i}" style="left:${c.x}px;width:${c.width}px" title="${esc(`${c.letter}: ${c.header}${tip}`)}">${esc(c.header)}</div>`;
      }).join("");
      this.corner.title = tr("sheet.select_all");
      this.events();
      new ResizeObserver(() => this.render()).observe(this.view);
    }

    events() {
      const v = this.view;
      v.addEventListener("scroll", () => this.schedule(), { passive: true });
      v.addEventListener("mousedown", (e) => this.onMouseDown(e));
      v.addEventListener("dblclick", (e) => {
        const hit = this.hit(e);
        if (hit && hit.kind === "cell") this.beginEdit("edit");
      });
      v.addEventListener("contextmenu", (e) => this.onContextMenu(e));
      v.addEventListener("mousemove", (e) => this.onHover(e));
      const inp = this.input;
      inp.addEventListener("keydown", (e) => this.onKey(e));
      inp.addEventListener("input", () => {
        if (!this.editing && inp.value) this.beginEdit("enter", true);
        else if (this.editing) { this.fitEditor(); this.opts.onEditInput && this.opts.onEditInput(inp.value); this.renderSelection(); }
      });
      inp.addEventListener("compositionstart", () => { if (!this.editing) this.beginEdit("enter", true); });
      inp.addEventListener("copy", (e) => { if (!this.editing) { e.preventDefault(); this.copy(e.clipboardData, false); } });
      inp.addEventListener("cut", (e) => { if (!this.editing) { e.preventDefault(); this.copy(e.clipboardData, true); } });
      inp.addEventListener("paste", (e) => {
        if (this.editing) return;
        e.preventDefault();
        this.paste(e.clipboardData.getData("text/plain"));
      });
      inp.addEventListener("blur", () => { if (this.editing && !this.keepEditing) this.commitEdit(null); });
      const bar = this.opts.formulaBar; // a formula typed there: its references are outlined too
      if (bar) for (const type of ["input", "focus", "blur"]) bar.addEventListener(type, () => this.renderSelection());
    }

    // ───────────── data ─────────────
    setRows(rows) {
      this.cancelEdit();
      this.rows = clone(rows || []);
      this.undoStack = [];
      this.redoStack = [];
      this.marquee = null;
      this.recalc();
      this.clampSelection();
      this.layout();
      this.render();
      this.emitSelect();
    }

    getRows() {
      return this.rows.map((r) => ({
        id: r.id,
        src: r.src == null ? null : r.src,
        cells: Object.fromEntries(Object.entries(r.cells || {}).map(([k, c]) => [k, c.f ? { f: c.f } : { v: c.v }])),
      }));
    }

    setFlags(flags) {
      this.cellFlags = new Map();
      this.rowFlags = new Map();
      const letters = new Map(this.columns.map((c, i) => [c.letter, i]));
      for (const f of flags || []) {
        const r = f.line - 1;
        const add = (key, map) => {
          const e = map.get(key) || { level: null, msgs: [] };
          e.level = e.level === "error" || f.level === "error" ? "error" : "warning";
          e.msgs.push(flagText(f));
          map.set(key, e);
        };
        if (f.columns && f.columns.length) for (const L of f.columns) { if (letters.has(L)) add(`${r},${letters.get(L)}`, this.cellFlags); }
        add(r, this.rowFlags);
      }
      this.render();
      this.emitSelect();
    }

    setReadOnly(ro) {
      this.readOnly = ro;
      if (ro) this.cancelEdit();
      this.view.classList.toggle("ro", ro);
    }

    get rowCount() { return this.rows.length; }
    excelRow(r) { return this.headerRow + 1 + r; }
    address(r, c) { return `${this.columns[c].letter}${this.excelRow(r)}`; }
    raw(r, c) {
      const cell = this.rows[r] && this.rows[r].cells[c];
      return cell ? (cell.f || cell.v || "") : "";
    }
    value(r, c) {
      const cell = this.rows[r] && this.rows[r].cells[c];
      if (!cell) return null;
      return cell.f ? this.calc.get(r * 100000 + c) : cell.v;
    }
    display(r, c) {
      const cell = this.rows[r] && this.rows[r].cells[c];
      if (!cell) return "";
      return cell.f ? F.formatValue(this.calc.get(r * 100000 + c)) : cell.v;
    }

    recalc() {
      const ev = new F.SheetEvaluator(this.rows, this.headerRow, this.headers);
      this.calc = new Map();
      this.rows.forEach((row, r) => {
        for (const [k, cell] of Object.entries(row.cells || {})) if (cell.f) this.calc.set(r * 100000 + Number(k), ev.value(r, Number(k)));
      });
    }

    // ───────────── actions (undo / redo) ─────────────
    apply(action, record = true) {
      this.doApply(action, false);
      if (record) {
        this.undoStack.push(action);
        if (this.undoStack.length > MAX_UNDO) this.undoStack.shift();
        this.redoStack = [];
      }
      this.marquee = null;
      this.afterChange(action);
    }
    undo() {
      if (this.readOnly || !this.undoStack.length) return;
      this.cancelEdit();
      const a = this.undoStack.pop();
      this.doApply(a, true);
      this.redoStack.push(a);
      this.afterChange(a, "undo");
    }
    redo() {
      if (this.readOnly || !this.redoStack.length) return;
      this.cancelEdit();
      const a = this.redoStack.pop();
      this.doApply(a, false);
      this.undoStack.push(a);
      this.afterChange(a, "redo");
    }
    canUndo() { return !this.readOnly && this.undoStack.length > 0; }
    canRedo() { return !this.readOnly && this.redoStack.length > 0; }

    doApply(a, reverse) {
      const set = (byId, ch, val) => {
        const row = byId.get(ch.id);
        if (!row) return;
        if (val == null) delete row.cells[ch.c]; else row.cells[ch.c] = clone(val);
      };
      if (!reverse) {
        for (const { index, row } of [...(a.ins || [])].sort((x, y) => x.index - y.index)) this.rows.splice(index, 0, clone(row));
        for (const { index } of [...(a.del || [])].sort((x, y) => y.index - x.index)) this.rows.splice(index, 1);
        const byId = new Map(this.rows.map((r) => [r.id, r]));
        for (const ch of a.cells || []) set(byId, ch, ch.after);
      } else {
        const byId = new Map(this.rows.map((r) => [r.id, r]));
        for (const ch of [...(a.cells || [])].reverse()) set(byId, ch, ch.before);
        for (const { index, row } of [...(a.del || [])].sort((x, y) => x.index - y.index)) this.rows.splice(index, 0, clone(row));
        for (const { index } of [...(a.ins || [])].sort((x, y) => y.index - x.index)) this.rows.splice(index, 1);
      }
    }

    afterChange(action, how) {
      this.recalc();
      this.clampSelection();
      this.layout();
      this.render();
      this.emitSelect();
      this.opts.onChange && this.opts.onChange(action, how || "do");
    }

    /** LNO (the line-number column) follows the row order – if it was 1, 2, 3 … before the change. */
    renumber(before, after) {
      const c = this.lnoCol;
      if (c < 0) return [];
      const plain = (row, i) => { const cell = row.cells[c]; return cell && !cell.f && cell.v === String(i + 1); };
      if (!before.every(plain)) return [];
      const out = [];
      after.forEach((row, i) => {
        const want = { v: String(i + 1) };
        if (!same(row.cells[c], want)) out.push({ id: row.id, c, before: clone(row.cells[c]) || null, after: want });
      });
      return out;
    }

    setCells(changes, label) {
      // changes: [{r, c, after}] -> one action (rows past the end are added)
      if (this.readOnly) return;
      const maxR = Math.max(...changes.map((x) => x.r), -1);
      const ins = [];
      const rows = [...this.rows];
      while (rows.length <= maxR) { const row = { id: newId(), src: null, cells: {} }; ins.push({ index: rows.length, row }); rows.push(row); }
      const cells = [];
      for (const { r, c, after } of changes) {
        const row = rows[r];
        if (c < 0 || c >= this.width || !row) continue;
        const before = clone(row.cells[c]) || null;
        if (!same(before, after)) cells.push({ id: row.id, c, before, after });
      }
      if (ins.length) cells.push(...this.renumber(this.rows, rows));
      if (!cells.length && !ins.length) return;
      this.apply({ label, cells, ins });
    }

    clearSelection() {
      if (this.readOnly) return;
      const changes = [];
      for (const g of this.ranges.map(norm)) {
        for (let r = g.r1; r <= Math.min(g.r2, this.rows.length - 1); r++) {
          for (let c = g.c1; c <= g.c2; c++) if (this.rows[r].cells[c]) changes.push({ r, c, after: null });
        }
      }
      if (changes.length) this.setCells(changes, `Clear ${changes.length} cells`);
    }

    selectedRows() {
      const set = new Set();
      for (const g of this.ranges.map(norm)) for (let r = g.r1; r <= Math.min(g.r2, this.rows.length - 1); r++) set.add(r);
      return [...set].sort((a, b) => a - b);
    }

    deleteRows(indices) {
      if (this.readOnly) return;
      const idx = [...new Set(indices)].filter((i) => i >= 0 && i < this.rows.length).sort((a, b) => a - b);
      if (!idx.length) return;
      const gone = new Set(idx);
      const del = idx.map((i) => ({ index: i, row: clone(this.rows[i]) }));
      const excelRows = idx.map((i) => this.excelRow(i));
      const rest = this.rows.filter((_, i) => !gone.has(i));
      const cells = [];
      for (const row of rest) {
        for (const [k, cell] of Object.entries(row.cells)) {
          if (!cell.f) continue;
          const f = F.deleteRows(cell.f, excelRows);
          if (f !== cell.f) cells.push({ id: row.id, c: Number(k), before: clone(cell), after: { f } });
        }
      }
      cells.push(...this.renumber(this.rows, rest));
      const first = idx[0];
      this.apply({ label: `Delete ${idx.length} rows`, del, cells });
      const r = Math.min(first, this.rows.length - 1);
      this.selectCell(Math.max(r, 0), this.active.c);
    }

    insertRows(at, count) {
      if (this.readOnly) return;
      at = Math.max(0, Math.min(at, this.rows.length));
      const fresh = Array.from({ length: count }, () => ({ id: newId(), src: null, cells: {} }));
      const ins = fresh.map((row, k) => ({ index: at + k, row }));
      const cells = [];
      for (const row of this.rows) {
        for (const [k, cell] of Object.entries(row.cells)) {
          if (!cell.f) continue;
          const f = F.insertRows(cell.f, this.excelRow(at), count);
          if (f !== cell.f) cells.push({ id: row.id, c: Number(k), before: clone(cell), after: { f } });
        }
      }
      const after = [...this.rows.slice(0, at), ...fresh, ...this.rows.slice(at)];
      cells.push(...this.renumber(this.rows, after));
      this.apply({ label: `Insert ${count} rows`, ins, cells });
      this.selectCell(at, this.active.c);
    }

    insertAtSelection(where) {
      const rows = this.selectedRows();
      if (!rows.length) { this.insertRows(this.rows.length, 1); return; }
      const count = rows.length;
      this.insertRows(where === "above" ? rows[0] : rows[rows.length - 1] + 1, count);
    }

    // ───────────── clipboard ─────────────
    copy(clipboardData, cut) {
      const g = norm(this.ranges[this.ranges.length - 1]);
      const r2 = Math.min(g.r2, this.rows.length - 1);
      if (r2 < g.r1) return;
      const shown = [], raw = [];
      for (let r = g.r1; r <= r2; r++) {
        const s = [], w = [];
        for (let c = g.c1; c <= g.c2; c++) { s.push(this.display(r, c)); w.push(clone(this.rows[r].cells[c]) || null); }
        shown.push(s); raw.push(w);
      }
      const text = toTSV(shown);
      clipboardData.setData("text/plain", text);
      this.clipboard = { text, raw, origin: { r: g.r1, c: g.c1 } };
      this.marquee = cut ? null : { r1: g.r1, c1: g.c1, r2, c2: g.c2 };
      if (cut && !this.readOnly) {
        const changes = [];
        for (let r = g.r1; r <= r2; r++) for (let c = g.c1; c <= g.c2; c++) if (this.rows[r].cells[c]) changes.push({ r, c, after: null });
        if (changes.length) this.setCells(changes, "Cut");
      }
      this.renderSelection();
    }

    paste(text) {
      if (this.readOnly || text == null) return;
      let block;
      const target = norm(this.ranges[this.ranges.length - 1]);
      const inner = this.clipboard && this.clipboard.text.replace(/\r\n/g, "\n") === text.replace(/\r\n/g, "\n").replace(/\n$/, "");
      if (inner) { // copied in this sheet: formulas move with the cells, like in Excel
        const { raw, origin } = this.clipboard;
        const dr = target.r1 - origin.r, dc = target.c1 - origin.c;
        block = raw.map((row) => row.map((cell) => (cell && cell.f ? { f: F.shiftFormula(cell.f, dr, dc) } : clone(cell))));
      } else {
        block = parseTSV(text).map((row) => row.map(content));
      }
      const h = block.length, w = Math.max(...block.map((r) => r.length));
      const changes = [];
      const one = h === 1 && w === 1;
      const fill = one && (target.r2 > target.r1 || target.c2 > target.c1);
      if (fill) { // one value into every selected cell
        for (const g of this.ranges.map(norm)) {
          for (let r = g.r1; r <= Math.min(g.r2, this.rows.length - 1); r++) {
            for (let c = g.c1; c <= g.c2; c++) {
              const cell = block[0][0];
              const moved = inner && cell && cell.f ? { f: F.shiftFormula(this.clipboard.raw[0][0].f, r - this.clipboard.origin.r, c - this.clipboard.origin.c) } : cell;
              changes.push({ r, c, after: moved });
            }
          }
        }
      } else {
        block.forEach((row, i) => row.forEach((cell, j) => {
          const c = target.c1 + j;
          if (c < this.width) changes.push({ r: target.r1 + i, c, after: cell });
        }));
      }
      this.setCells(changes, `Paste ${changes.length} cells`);
      if (!fill) this.selectRange(target.r1, target.c1, Math.min(target.r1 + h - 1, this.rows.length - 1), Math.min(target.c1 + w - 1, this.width - 1));
    }

    // ───────────── editing ─────────────
    beginEdit(mode, typed = false) {
      if (this.readOnly) { this.input.value = ""; this.opts.onReadOnly && this.opts.onReadOnly(); return; }
      if (!this.rows.length) { this.input.value = ""; return; }
      const { r, c } = this.active;
      this.editing = { r, c, mode, original: this.raw(r, c) };
      this.point = null;
      if (!typed) this.input.value = this.raw(r, c);
      this.marquee = null;
      this.ensureVisible(r, c);
      this.input.classList.add("editing");
      this.placeEditor();
      this.input.focus({ preventScroll: true });
      if (!typed) this.input.setSelectionRange(this.input.value.length, this.input.value.length);
      this.opts.onEditInput && this.opts.onEditInput(this.input.value);
      this.renderSelection();
    }

    commitEdit(move) {
      if (!this.editing) return;
      const { r, c } = this.editing;
      const text = this.input.value.replace(/\r?\n/g, " ");
      this.editing = null;
      this.point = null;
      this.input.classList.remove("editing");
      this.input.value = "";
      this.setCells([{ r, c, after: content(text) }], `Edit ${this.address(r, c)}`);
      if (move) this.moveBy(move[0], move[1], false);
      else { this.renderSelection(); this.emitSelect(); }
    }

    cancelEdit() {
      if (!this.editing) return;
      this.editing = null;
      this.point = null;
      this.input.classList.remove("editing");
      this.input.value = "";
      this.placeEditor();
      this.renderSelection();
      this.emitSelect();
    }

    /** Set the active cell from the formula bar. */
    setActiveContent(text) {
      if (this.readOnly || !this.rows.length) return;
      this.cancelEdit();
      this.point = null;
      const { r, c } = this.active;
      this.setCells([{ r, c, after: content(text) }], `Edit ${this.address(r, c)}`);
    }

    placeEditor() {
      const { r, c } = this.editing || this.active;
      const col = this.columns[c];
      if (!col) return;
      this.input.style.left = col.x + "px";
      this.input.style.top = r * ROW_H + "px";
      this.input.style.width = col.width + "px";
      if (this.editing) this.fitEditor();
    }

    fitEditor() {
      const col = this.columns[this.editing.c];
      this.input.style.width = col.width + "px";
      const need = Math.min(Math.max(col.width, this.input.scrollWidth + 8), 520);
      this.input.style.width = need + "px";
    }

    // ───────────── formulas: pointing at cells, like in Excel ─────────────
    /** The text box a formula is being typed in: the cell editor, or the formula bar (opts.formulaBar). */
    formulaInput() {
      if (this.editing) return this.input.value.startsWith("=") ? this.input : null;
      const bar = this.opts.formulaBar;
      return bar && !this.readOnly && !bar.readOnly && document.activeElement === bar && bar.value.startsWith("=") ? bar : null;
    }

    /** Is the caret still right after the reference just put in by pointing? */
    pointActive(input) {
      const p = this.point;
      return !!p && p.input === input && input.selectionStart === p.end && input.selectionEnd === p.end && input.value.slice(p.start, p.end) === p.text;
    }

    /** A reference can go in right after "=", an operator, "(" or "," (not inside "text"), or replace the one just put in. */
    canPoint(input) {
      if (this.pointActive(input)) return true;
      const before = input.value.slice(0, input.selectionStart);
      if ((before.match(/"/g) || []).length % 2) return false;
      return /[=+\-*\/^&(,:<>]\s*$/.test(before);
    }

    /** Put the reference of anchor..focus (a cell or a range) at the caret, or in place of the one just put in. */
    pointAt(input, anchor, focus) {
      const r1 = Math.min(anchor.r, focus.r), r2 = Math.max(anchor.r, focus.r), c1 = Math.min(anchor.c, focus.c), c2 = Math.max(anchor.c, focus.c);
      const text = r1 === r2 && c1 === c2 ? this.address(r1, c1) : `${this.address(r1, c1)}:${this.address(r2, c2)}`;
      const again = this.pointActive(input);
      const start = again ? this.point.start : input.selectionStart;
      const end = again ? this.point.end : input.selectionEnd;
      input.value = input.value.slice(0, start) + text + input.value.slice(end);
      input.setSelectionRange(start + text.length, start + text.length);
      this.point = { input, start, end: start + text.length, text, anchor, focus };
      if (input === this.input) { this.fitEditor(); this.opts.onEditInput && this.opts.onEditInput(input.value); }
      this.renderSelection();
    }

    /** Arrow keys while a formula is typed in the cell: move (Shift: stretch) the reference. */
    pointMove(d, extend) {
      const p = this.pointActive(this.input) ? this.point : null;
      const from = p ? p.focus : { r: this.editing.r, c: this.editing.c };
      const focus = { r: Math.max(0, Math.min(from.r + d[0], this.rows.length - 1)), c: Math.max(0, Math.min(from.c + d[1], this.width - 1)) };
      this.pointAt(this.input, extend && p ? p.anchor : focus, focus);
      this.ensureVisible(focus.r, focus.c);
    }

    // ───────────── selection ─────────────
    clampSelection() {
      const maxR = Math.max(this.rows.length - 1, 0), maxC = this.width - 1;
      const cl = (p) => ({ r: Math.max(0, Math.min(p.r, maxR)), c: Math.max(0, Math.min(p.c, maxC)) });
      this.active = cl(this.active);
      this.anchor = cl(this.anchor);
      this.ranges = this.ranges.map((g) => ({ r1: Math.min(g.r1, maxR), r2: Math.min(g.r2, maxR), c1: Math.min(g.c1, maxC), c2: Math.min(g.c2, maxC) }));
    }

    selectCell(r, c, scroll = true) {
      this.active = { r, c };
      this.anchor = { r, c };
      this.focusEnd = { r, c };
      this.ranges = [{ r1: r, c1: c, r2: r, c2: c }];
      this.clampSelection();
      if (scroll) this.ensureVisible(this.active.r, this.active.c);
      this.placeEditor();
      this.renderSelection();
      this.emitSelect();
    }

    selectRange(r1, c1, r2, c2) {
      this.active = { r: r1, c: c1 };
      this.anchor = { r: r1, c: c1 };
      this.focusEnd = { r: r2, c: c2 };
      this.ranges = [{ r1, c1, r2, c2 }];
      this.clampSelection();
      this.placeEditor();
      this.renderSelection();
      this.emitSelect();
    }

    moveBy(dr, dc, extend) {
      if (!this.rows.length) return;
      if (extend) {
        const end = this.focusEnd || { ...this.active };
        const next = { r: Math.max(0, Math.min(end.r + dr, this.rows.length - 1)), c: Math.max(0, Math.min(end.c + dc, this.width - 1)) };
        this.focusEnd = next;
        this.ranges = [{ r1: this.anchor.r, c1: this.anchor.c, r2: next.r, c2: next.c }];
        this.ensureVisible(next.r, next.c);
        this.renderSelection();
        this.emitSelect();
        return;
      }
      this.selectCell(Math.max(0, Math.min(this.active.r + dr, this.rows.length - 1)), Math.max(0, Math.min(this.active.c + dc, this.width - 1)));
    }

    lastUsedCol(r) {
      const keys = Object.keys((this.rows[r] && this.rows[r].cells) || {}).map(Number);
      return keys.length ? Math.max(...keys) : 0;
    }

    emitSelect() {
      if (!this.opts.onSelect) return;
      const { r, c } = this.active;
      let count = 0, nums = 0, sum = 0, cells = 0;
      for (const g of this.ranges.map(norm)) {
        for (let rr = g.r1; rr <= Math.min(g.r2, this.rows.length - 1); rr++) {
          for (let cc = g.c1; cc <= g.c2; cc++) {
            cells++;
            const v = this.value(rr, cc);
            if (v === null || v === undefined || v === "") continue;
            count++;
            const n = typeof v === "number" ? v : typeof v === "string" ? F.numberFromText(v) : null;
            if (n !== null && !Number.isNaN(n)) { nums++; sum += n; }
          }
        }
      }
      const flag = this.cellFlags.get(`${r},${c}`) || null;
      this.opts.onSelect({
        r, c, address: this.rows.length ? this.address(r, c) : "", raw: this.raw(r, c), display: this.display(r, c),
        cells, count, nums, sum, flag, rowFlag: this.rowFlags.get(r) || null, row: this.rows[r] || null,
      });
    }

    // ───────────── mouse ─────────────
    hit(e) {
      const t = e.target;
      if (t.closest(".sh-corner")) return { kind: "all" };
      const inCols = t.closest(".sh-cols");
      if (inCols) {
        const x = e.clientX - this.colHead.getBoundingClientRect().left;
        return { kind: "col", c: this.colAt(x) };
      }
      const rect = this.cellLayer.getBoundingClientRect();
      const y = e.clientY - rect.top;
      const r = Math.floor(y / ROW_H);
      if (t.closest(".sh-rh")) return r >= 0 && r < this.rows.length ? { kind: "row", r } : null;
      if (t.closest(".sh-cells")) {
        if (r < 0 || r >= this.rows.length) return null;
        return { kind: "cell", r, c: this.colAt(e.clientX - rect.left) };
      }
      return null;
    }

    colAt(x) {
      let lo = 0, hi = this.width - 1;
      while (lo < hi) {
        const mid = (lo + hi + 1) >> 1;
        if (this.columns[mid].x <= x) lo = mid; else hi = mid - 1;
      }
      return lo;
    }

    onMouseDown(e) {
      if (e.button !== 0 && e.button !== 2) return;
      const vr = this.view.getBoundingClientRect();
      if (e.clientX - vr.left >= this.view.clientWidth || e.clientY - vr.top >= this.view.clientHeight) return; // scrollbar
      const hit = this.hit(e);
      if (e.target === this.input && this.editing) return; // clicking inside the cell editor
      const fx = hit && hit.kind === "cell" && e.button === 0 ? this.formulaInput() : null;
      if (fx && this.canPoint(fx)) { // a formula is being typed: the cell's reference goes into it (drag = a range)
        e.preventDefault(); // the text box keeps the focus
        const anchor = e.shiftKey && this.pointActive(fx) ? this.point.anchor : { r: hit.r, c: hit.c };
        this.pointAt(fx, anchor, { r: hit.r, c: hit.c });
        this.drag = { kind: "point", input: fx, anchor };
        this.trackDrag();
        return;
      }
      e.preventDefault();
      if (this.editing) this.commitEdit(null);
      this.input.focus({ preventScroll: true });
      if (!hit) return;
      if (e.button === 2) { // right click outside the selection selects the cell first
        if (hit.kind === "cell" && !this.inSelection(hit.r, hit.c)) this.selectCell(hit.r, hit.c, false);
        if (hit.kind === "row" && !this.selectedRows().includes(hit.r)) this.selectRowsRange(hit.r, hit.r, false);
        return;
      }
      const add = e.ctrlKey || e.metaKey, extend = e.shiftKey;
      if (hit.kind === "all") { this.selectAll(); return; }
      if (hit.kind === "row") {
        if (extend) this.selectRowsRange(this.anchor.r, hit.r, false);
        else this.selectRowsRange(hit.r, hit.r, add);
        this.drag = { kind: "row", from: extend ? this.anchor.r : hit.r };
      } else if (hit.kind === "col") {
        const last = Math.max(this.rows.length - 1, 0);
        const c1 = extend ? this.anchor.c : hit.c;
        const g = { r1: 0, c1, r2: last, c2: hit.c };
        if (!extend) { this.active = { r: 0, c: hit.c }; this.anchor = { r: 0, c: hit.c }; }
        this.ranges = add ? [...this.ranges, g] : [g];
        this.drag = { kind: "col", from: c1 };
        this.renderSelection();
        this.emitSelect();
      } else {
        if (extend) {
          this.focusEnd = { r: hit.r, c: hit.c };
          this.ranges[this.ranges.length - 1] = { r1: this.anchor.r, c1: this.anchor.c, r2: hit.r, c2: hit.c };
        } else {
          this.active = { r: hit.r, c: hit.c };
          this.anchor = { r: hit.r, c: hit.c };
          this.focusEnd = { r: hit.r, c: hit.c };
          const g = { r1: hit.r, c1: hit.c, r2: hit.r, c2: hit.c };
          this.ranges = add ? [...this.ranges, g] : [g];
        }
        this.drag = { kind: "cell" };
        this.placeEditor();
        this.renderSelection();
        this.emitSelect();
      }
      this.trackDrag();
    }

    trackDrag() {
      const move = (ev) => this.onDrag(ev);
      const up = () => {
        this.drag = null;
        cancelAnimationFrame(this.autoScroll);
        window.removeEventListener("mousemove", move);
        window.removeEventListener("mouseup", up);
      };
      window.addEventListener("mousemove", move);
      window.addEventListener("mouseup", up);
    }

    onDrag(e) {
      if (!this.drag) return;
      this.lastDrag = e;
      const rect = this.cellLayer.getBoundingClientRect();
      const r = Math.max(0, Math.min(Math.floor((e.clientY - rect.top) / ROW_H), this.rows.length - 1));
      const c = this.colAt(e.clientX - rect.left);
      if (this.drag.kind === "point") this.pointAt(this.drag.input, this.drag.anchor, { r, c });
      else {
        const g = this.ranges[this.ranges.length - 1];
        if (this.drag.kind === "cell") { g.r2 = r; g.c2 = c; this.focusEnd = { r, c }; }
        else if (this.drag.kind === "row") { g.r1 = this.drag.from; g.r2 = r; }
        else { g.c1 = this.drag.from; g.c2 = c; }
        this.renderSelection();
        this.emitSelect();
      }
      // keep scrolling while the pointer is outside the grid
      cancelAnimationFrame(this.autoScroll);
      const vr = this.view.getBoundingClientRect();
      const dy = e.clientY < vr.top + HEAD_H ? -20 : e.clientY > vr.bottom ? 20 : 0;
      const dx = e.clientX < vr.left + RH_W ? -30 : e.clientX > vr.right ? 30 : 0;
      if (dx || dy) {
        this.autoScroll = requestAnimationFrame(() => {
          this.view.scrollTop += dy;
          this.view.scrollLeft += dx;
          if (this.drag) this.onDrag(this.lastDrag);
        });
      }
    }

    onHover(e) {
      if (this.drag) return;
      const t = e.target.closest(".sh-c");
      this.view.title = t && t.dataset.tip ? t.dataset.tip : "";
    }

    inSelection(r, c) {
      return this.ranges.map(norm).some((g) => r >= g.r1 && r <= g.r2 && c >= g.c1 && c <= g.c2);
    }

    selectRowsRange(a, b, add) {
      const g = { r1: Math.min(a, b), c1: 0, r2: Math.max(a, b), c2: this.width - 1 };
      if (!add || !this.ranges.length) { this.active = { r: a, c: 0 }; this.anchor = { r: a, c: 0 }; }
      this.ranges = add ? [...this.ranges, g] : [g];
      this.renderSelection();
      this.emitSelect();
    }

    selectAll() {
      if (!this.rows.length) return;
      this.ranges = [{ r1: 0, c1: 0, r2: this.rows.length - 1, c2: this.width - 1 }];
      this.renderSelection();
      this.emitSelect();
    }

    onContextMenu(e) {
      e.preventDefault();
      const hit = this.hit(e);
      if (!hit || hit.kind === "col" || hit.kind === "all") return;
      this.opts.onContextMenu && this.opts.onContextMenu(e.clientX, e.clientY);
    }

    // ───────────── keyboard ─────────────
    onKey(e) {
      const ctrl = e.ctrlKey || e.metaKey;
      if (e.isComposing || e.keyCode === 229) return;
      if (this.editing) {
        const enterMode = this.editing.mode === "enter";
        if (e.key === "Enter" && !e.altKey) { e.preventDefault(); this.commitEdit([e.shiftKey ? -1 : 1, 0]); }
        else if (e.key === "Tab") { e.preventDefault(); this.commitEdit([0, e.shiftKey ? -1 : 1]); }
        else if (e.key === "Escape") { e.preventDefault(); this.cancelEdit(); }
        else if (enterMode && e.key.startsWith("Arrow")) {
          e.preventDefault();
          const d = { ArrowUp: [-1, 0], ArrowDown: [1, 0], ArrowLeft: [0, -1], ArrowRight: [0, 1] }[e.key];
          if (this.input.value.startsWith("=") && this.canPoint(this.input)) this.pointMove(d, e.shiftKey);
          else this.commitEdit(d);
        }
        return;
      }
      const shift = e.shiftKey;
      const pageRows = Math.max(1, Math.floor((this.view.clientHeight - HEAD_H) / ROW_H) - 1);
      const k = e.key;
      let handled = true;
      if (k === "ArrowUp") ctrl ? this.selectCell(0, this.active.c) : this.moveBy(-1, 0, shift);
      else if (k === "ArrowDown") ctrl ? this.selectCell(this.rows.length - 1, this.active.c) : this.moveBy(1, 0, shift);
      else if (k === "ArrowLeft") ctrl ? this.selectCell(this.active.r, 0) : this.moveBy(0, -1, shift);
      else if (k === "ArrowRight") ctrl ? this.selectCell(this.active.r, this.lastUsedCol(this.active.r)) : this.moveBy(0, 1, shift);
      else if (k === "Tab") this.moveBy(0, shift ? -1 : 1, false);
      else if (k === "Enter") this.moveBy(shift ? -1 : 1, 0, false);
      else if (k === "PageDown") this.moveBy(pageRows, 0, shift);
      else if (k === "PageUp") this.moveBy(-pageRows, 0, shift);
      else if (k === "Home") ctrl ? this.selectCell(0, 0) : this.selectCell(this.active.r, 0);
      else if (k === "End") ctrl ? this.selectCell(this.rows.length - 1, this.lastUsedCol(this.rows.length - 1)) : this.selectCell(this.active.r, this.lastUsedCol(this.active.r));
      else if (k === "F2") this.beginEdit("edit");
      else if (k === "Delete" || k === "Backspace") this.clearSelection();
      else if (k === "Escape") { this.marquee = null; this.renderSelection(); }
      else if (ctrl && (k === "z" || k === "Z") && !shift) this.undo();
      else if (ctrl && (k === "y" || k === "Y" || ((k === "z" || k === "Z") && shift))) this.redo();
      else if (ctrl && (k === "a" || k === "A")) this.selectAll();
      else handled = false;
      if (handled) e.preventDefault();
    }

    focus() { this.input.focus({ preventScroll: true }); }

    // ───────────── drawing ─────────────
    layout() {
      const h = this.rows.length * ROW_H;
      this.rowHead.style.height = this.cellLayer.style.height = h + BOTTOM_SPACE + "px";
      this.empty.hidden = this.rows.length > 0;
      this.empty.textContent = this.rows.length ? "" : tr("sheet.empty");
    }

    schedule() {
      if (this.frame) return;
      this.frame = requestAnimationFrame(() => { this.frame = null; this.render(); });
    }

    ensureVisible(r, c) {
      const v = this.view;
      const top = r * ROW_H, bottom = top + ROW_H;
      const viewH = v.clientHeight - HEAD_H;
      if (top < v.scrollTop) v.scrollTop = top;
      else if (bottom > v.scrollTop + viewH) v.scrollTop = bottom - viewH;
      const col = this.columns[c];
      if (!col) return;
      const viewW = v.clientWidth - RH_W;
      if (col.x < v.scrollLeft) v.scrollLeft = col.x;
      else if (col.x + col.width > v.scrollLeft + viewW) v.scrollLeft = col.x + col.width - viewW;
    }

    render() {
      const v = this.view;
      if (!v.clientHeight) return;
      const r0 = Math.max(0, Math.floor(v.scrollTop / ROW_H) - OVERSCAN);
      const r1 = Math.min(this.rows.length - 1, Math.ceil((v.scrollTop + v.clientHeight) / ROW_H) + OVERSCAN);
      const x0 = v.scrollLeft - 200, x1 = v.scrollLeft + v.clientWidth + 200;
      const c0 = this.colAt(Math.max(0, x0));
      let c1 = c0;
      while (c1 < this.width - 1 && this.columns[c1 + 1].x < x1) c1++;
      const original = this.opts.original;
      let html = "";
      for (let r = r0; r <= r1; r++) {
        const row = this.rows[r];
        const top = r * ROW_H;
        for (let c = c0; c <= c1; c++) {
          const col = this.columns[c];
          const cell = row.cells[c];
          let text = "", cls = "sh-c", tip = "";
          if (cell) {
            if (cell.f) {
              const val = this.calc.get(r * 100000 + c);
              text = F.formatValue(val);
              cls += " fx";
              if (typeof val === "number") cls += " num";
              if (F.isError(val)) cls += " err";
              tip = `${cell.f}`;
            } else text = cell.v;
          }
          const role = this.roles[col.letter];
          if (role && role.number) cls += " num";
          const flag = this.cellFlags.get(`${r},${c}`);
          if (flag) { cls += " " + flag.level; tip = flag.msgs.join("\n") + (tip ? "\n" + tip : ""); }
          if (original && row.src && role && role.field) {
            const was = original(row.src, col.letter);
            if (was !== undefined && was !== null && was !== text) { cls += " edited"; tip = (tip ? tip + "\n" : "") + tr("sheet.was", { v: was === "" ? tr("common.empty") : was }); }
          }
          html += `<div class="${cls}" style="left:${col.x}px;top:${top}px;width:${col.width}px"${tip ? ` data-tip="${esc(tip)}"` : ""}>${esc(text)}</div>`;
        }
      }
      this.cellsBox.innerHTML = html;
      let rh = "";
      for (let r = r0; r <= r1; r++) {
        const f = this.rowFlags.get(r);
        const tip = f ? f.msgs.join("\n") : "";
        rh += `<div class="sh-rn${f ? " " + f.level : ""}" data-r="${r}" style="top:${r * ROW_H}px"${tip ? ` title="${esc(tip)}"` : ""}>${this.excelRow(r)}</div>`;
      }
      this.rowHead.innerHTML = rh;
      this.renderSelection();
    }

    renderSelection() {
      const parts = [];
      const rect = (g, cls) => {
        const n = norm(g);
        const r2 = Math.min(n.r2, this.rows.length - 1);
        if (r2 < n.r1 || !this.rows.length) return;
        const a = this.columns[n.c1], b = this.columns[n.c2];
        parts.push(`<div class="${cls}" style="left:${a.x}px;top:${n.r1 * ROW_H}px;width:${b.x + b.width - a.x}px;height:${(r2 - n.r1 + 1) * ROW_H}px"></div>`);
      };
      const multi = this.ranges.length > 1 || this.ranges.some((g) => g.r1 !== g.r2 || g.c1 !== g.c2);
      if (multi) for (const g of this.ranges) rect(g, "sh-sel");
      if (this.marquee) rect(this.marquee, "sh-ants");
      const fx = this.formulaInput();
      if (fx) F.references(fx.value).forEach((g, i) => { // the cells the formula being typed refers to
        const c1 = this.colByLetter.get(F.colLetter(g.c1)), c2 = this.colByLetter.get(F.colLetter(g.c2));
        const r1 = Math.max(g.r1 - this.headerRow - 1, 0), r2 = g.r2 - this.headerRow - 1;
        if (c1 !== undefined && c2 !== undefined && r2 >= 0) rect({ r1, c1, r2, c2 }, `sh-ref k${i % 6}`);
      });
      if (this.rows.length) rect({ r1: this.active.r, c1: this.active.c, r2: this.active.r, c2: this.active.c }, "sh-act");
      this.selBox.innerHTML = parts.join("");
      // header highlights
      const rows = new Set(), cols = new Set();
      for (const g of this.ranges.map(norm)) {
        for (let r = g.r1; r <= g.r2; r++) rows.add(r);
        for (let c = g.c1; c <= g.c2; c++) cols.add(c);
      }
      for (const elx of this.rowHead.children) elx.classList.toggle("on", rows.has(Number(elx.dataset.r)));
      for (const elx of this.colHead.children) elx.classList.toggle("on", cols.has(Number(elx.dataset.c)));
      if (!this.editing) this.placeEditor();
    }
  }

  Sheet.ROW_H = ROW_H;
  Sheet.parseTSV = parseTSV;
  Sheet.toTSV = toTSV;
  window.CIVSheet = Sheet;
})();
