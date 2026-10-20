/* Review before export: the invoice table (PDF page image) next to the filled template columns (Excel).
   CIVPreview.open({ title, load, returnFocus, download | actions })  where load() resolves to
   { result, preview } (POST /api/preview, or GET /api/documents/{id}/preview for a saved document).
   download(base64, filename, type) saves the result's files; actions(result) returns footer buttons instead. */
(() => {
  "use strict";
  const PT = 96 / 72; // CSS px per PDF point at 100 %
  const READ_LINE = 0.35; // height in a pane where the line being read sits when the panes are aligned
  const STEP = 1.25;
  const STACK_BELOW = 900; // drawer narrower than this: the panes go on top of each other
  const WIDTH_KEY = "civconvert.preview.width";

  const el = (tag, attrs = {}, ...children) => {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k === "class") node.className = v; else if (k === "text") node.textContent = v; else node.setAttribute(k, v);
    }
    for (const c of children) if (c != null) node.append(c);
    return node;
  };
  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
  const store = {
    get: (k) => { try { return localStorage.getItem(k) || ""; } catch { return ""; } },
    set: (k, v) => { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
  };
  const worse = (a, b) => (a === "error" || b === "error" ? "error" : a || b);
  const letter = (i) => { let s = ""; for (i += 1; i > 0; i = Math.floor((i - 1) / 26)) s = String.fromCharCode(65 + ((i - 1) % 26)) + s; return s; };

  const ICONS = {
    prev: '<path d="M10 3.5 5.5 8l4.5 4.5"/>',
    next: '<path d="M6 3.5 10.5 8 6 12.5"/>',
    up: '<path d="M3.5 10 8 5.5l4.5 4.5"/>',
    down: '<path d="M3.5 6 8 10.5 12.5 6"/>',
    minus: '<path d="M3.5 8h9"/>',
    plus: '<path d="M3.5 8h9M8 3.5v9"/>',
    fitw: '<path d="M1.5 8h13M4.5 5 1.5 8l3 3M11.5 5l3 3-3 3"/>',
    fit: '<path d="M5.5 2h-3.5v3.5M10.5 2h3.5v3.5M5.5 14h-3.5v-3.5M10.5 14h3.5v-3.5"/><rect x="5" y="5" width="6" height="6" rx=".8"/>',
    full: '<path d="M2 6V2h4M10 2h4v4M14 10v4h-4M6 14H2v-4"/>',
    unfull: '<path d="M6 2v4H2M10 2v4h4M14 10h-4v4M2 10h4v4"/>',
    side: '<rect x="1.5" y="2.5" width="13" height="11" rx="1.5"/><path d="M8 2.5v11"/>',
    stack: '<rect x="1.5" y="2.5" width="13" height="11" rx="1.5"/><path d="M1.5 8h13"/>',
    close: '<path d="m4 4 8 8M12 4l-8 8"/>',
  };
  const icon = (name) => {
    const s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    s.setAttribute("viewBox", "0 0 16 16");
    s.setAttribute("aria-hidden", "true");
    for (const [k, v] of Object.entries({ fill: "none", stroke: "currentColor", "stroke-width": "1.6", "stroke-linecap": "round", "stroke-linejoin": "round" })) s.setAttribute(k, v);
    s.innerHTML = ICONS[name];
    return s;
  };
  const btn = (name, label, title, onclick) => {
    const b = el("button", { type: "button", class: "pv-btn", title, "aria-label": title }, name ? icon(name) : null, label ? el("span", { class: "lbl", text: label }) : null);
    b.onclick = onclick;
    return b;
  };

  let S = null; // the open preview
  let seq = 0;

  // ───────────────────────────── open / close ─────────────────────────────

  function open(opts) {
    close();
    const token = ++seq;
    S = { opts, sync: true, layout: null, view: "table", page: 0, sel: null, hover: [null, null], driver: null, observers: [] };
    mount();
    Promise.resolve()
      .then(opts.load)
      .then((data) => { if (token === seq && S) render(data); },
            (e) => { if (token === seq && S) fail((e && e.message) || String(e)); });
  }

  function close() {
    if (!S) return;
    const s = S;
    S = null;
    seq++;
    if (document.fullscreenElement === s.root) document.exitFullscreen().catch(() => {});
    s.observers.forEach((o) => o.disconnect());
    document.removeEventListener("keydown", onKey, true);
    document.removeEventListener("fullscreenchange", onFullscreen);
    s.backdrop.remove();
    s.root.remove();
    document.documentElement.style.overflow = s.overflow;
    if (s.opts.returnFocus) s.opts.returnFocus.focus({ preventScroll: true });
  }

  function mount() {
    S.backdrop = el("div", { class: "pv-backdrop" });
    S.backdrop.onclick = close;
    S.root = el("div", { class: "pv", role: "dialog", "aria-modal": "true", "aria-label": "校验与对比预览", tabindex: "-1" });
    const width = parseInt(store.get(WIDTH_KEY), 10);
    if (width > 0) S.root.style.setProperty("--pv-width", width + "px");

    S.title = el("h2", { text: S.opts.title || "Preview" });
    S.meta = el("div", { class: "pv-meta", text: "正在生成预览…" });
    S.issueBox = el("div", { class: "pv-issues" });
    const sync = el("input", { type: "checkbox", checked: "" });
    sync.onchange = () => { S.sync = sync.checked; if (S.sync && S.driver) onScroll(S.driver); };
    S.layoutBtn = btn("stack", null, "", () => { S.layout = S.root.dataset.layout === "stack" ? "side" : "stack"; layout(); });
    S.fullBtn = btn("full", null, "全屏 (F)", toggleFull);
    const tools = el("div", { class: "pv-tools" }, S.issueBox,
      el("label", { class: "pv-check", title: "滚动一侧时，另一侧自动对齐到同一行" }, sync, "同步滚动"),
      S.layoutBtn, S.fullBtn, btn("close", null, "关闭 (Esc)", close));
    S.checks = el("div", { class: "pv-checks", hidden: "" });
    S.body = el("div", { class: "pv-body" }, el("div", { class: "pv-msg" }, el("span", { class: "pv-spin" }), "正在读取 PDF、生成预览…"));
    S.detail = el("div", { class: "pv-detail" });
    S.actions = el("div", { class: "pv-actions" });
    const resize = el("div", { class: "pv-resize", title: "拖动调整宽度" });
    S.root.append(resize,
      el("header", { class: "pv-head" }, el("div", { class: "pv-title" }, S.title, S.meta), tools),
      S.checks, S.body, el("footer", { class: "pv-foot" }, S.detail, S.actions));
    resizable(resize);

    S.overflow = document.documentElement.style.overflow;
    document.documentElement.style.overflow = "hidden";
    document.body.append(S.backdrop, S.root);
    document.addEventListener("keydown", onKey, true);
    document.addEventListener("fullscreenchange", onFullscreen);
    const ro = new ResizeObserver(layout);
    ro.observe(S.root);
    S.observers.push(ro);
    layout();
    S.root.focus({ preventScroll: true });
  }

  function fail(message) {
    S.meta.textContent = "无法生成预览";
    S.body.replaceChildren(el("div", { class: "pv-msg err", text: "预览失败：" + message }));
  }

  function layout() {
    if (!S) return;
    const mode = S.layout || (S.root.clientWidth < STACK_BELOW ? "stack" : "side");
    S.root.dataset.layout = mode;
    const next = mode === "stack" ? "side" : "stack";
    S.layoutBtn.replaceChildren(icon(next));
    S.layoutBtn.title = mode === "stack" ? "改为左右并排" : "改为上下排列";
  }

  function resizable(handle) {
    let drag = null;
    handle.addEventListener("pointerdown", (e) => {
      drag = e.pointerId;
      handle.setPointerCapture(e.pointerId);
      e.preventDefault();
    });
    handle.addEventListener("pointermove", (e) => {
      if (drag !== e.pointerId) return;
      const w = clamp(window.innerWidth - e.clientX, 520, window.innerWidth);
      S.root.style.setProperty("--pv-width", Math.round(w) + "px");
    });
    const end = (e) => {
      if (drag !== e.pointerId) return;
      drag = null;
      store.set(WIDTH_KEY, String(parseInt(S.root.style.getPropertyValue("--pv-width"), 10) || ""));
    };
    handle.addEventListener("pointerup", end);
    handle.addEventListener("pointercancel", end);
  }

  function toggleFull() {
    if (document.fullscreenElement) { document.exitFullscreen().catch(() => {}); return; }
    if (S.root.classList.contains("max")) { S.root.classList.remove("max"); onFullscreen(); return; }
    const fallback = () => { S.root.classList.add("max"); onFullscreen(); };
    if (S.root.requestFullscreen) S.root.requestFullscreen().catch(fallback); else fallback();
  }

  function onFullscreen() {
    if (!S) return;
    const on = document.fullscreenElement === S.root || S.root.classList.contains("max");
    S.fullBtn.replaceChildren(icon(on ? "unfull" : "full"));
    S.fullBtn.title = on ? "退出全屏 (F)" : "全屏 (F)";
    S.fullBtn.classList.toggle("on", on);
  }

  // ───────────────────────────── render ─────────────────────────────

  function render(data) {
    S.data = data;
    S.result = data.result;
    S.pv = data.preview;
    S.n = S.result.lines.length;
    S.issues = collectIssues();
    S.issueLines = [...S.issues.keys()].sort((a, b) => a - b);
    const first = S.pv.pages.findIndex((p) => p.table);
    S.page = first < 0 ? 0 : first;

    const r = S.result;
    S.title.replaceChildren(r.filename + "  ", el("span", { class: "badge " + (r.status === "ok" ? "ok" : "review"), text: r.status === "ok" ? "✓ 校验通过" : "需复核" }));
    S.meta.textContent = [r.customer.name, r.document.invoice_no && "发票号 " + r.document.invoice_no, r.document.invoice_date,
      `${S.n} 行`, r.sources.join(" + ")].filter(Boolean).join(" · ");
    S.checks.hidden = false;
    S.checks.replaceChildren(...r.checks.map((c) => el("span", { class: "pv-chip " + (c.ok ? "pass" : "fail"), title: c.detail },
      (c.ok ? "✓ " : "✗ ") + c.name + " ", el("span", { class: "d", text: c.detail }))));

    S.src = makePane("src");
    S.out = makePane("out");
    S.body.replaceChildren(srcPane(), outPane());
    renderPage();
    renderGrid();
    issueNav();
    footer();
    detail(null);
    requestAnimationFrame(() => {
      if (!S) return;
      fit(S.src, "auto");
      fit(S.out, "auto");
      S.src.view.scrollTop = S.out.view.scrollTop = 0;
    });
  }

  function collectIssues() {
    const map = new Map();
    const add = (n, level, message, fields = [], cols = []) => {
      if (!map.has(n)) map.set(n, { level: null, list: [], fields: new Map(), cols: new Map() });
      const e = map.get(n);
      e.level = worse(e.level, level);
      e.list.push({ level, message });
      for (const f of fields) e.fields.set(f, worse(e.fields.get(f), level));
      for (const c of cols) e.cols.set(c, worse(e.cols.get(c), level));
    };
    for (const f of S.result.flags || []) add(f.line, f.level, f.message, f.fields, f.columns);
    for (const c of S.pv.grid.columns) {
      c.cells.forEach((cell, i) => {
        if (c.field && cell.v === "" && !(map.get(i + 1) && map.get(i + 1).cols.has(c.letter))) {
          add(i + 1, "error", `${c.letter} ${c.header} 为空值`, [c.field], [c.letter]);
        }
        if (cell.csv !== undefined) add(i + 1, "warning", `${c.letter} ${c.header}：Excel 显示 "${cell.v}"，CSV 中是 "${cell.csv}"`, [], [c.letter]);
      });
    }
    return map;
  }

  function makePane(kind) {
    const p = { kind, zoom: 1, mode: "auto", min: kind === "src" ? 0.1 : 0.4, max: kind === "src" ? 8 : 4 };
    p.view = el("div", { class: "pane-view", tabindex: "0" });
    p.zoomLabel = el("span", { class: "pv-zoomval" });
    p.zoomBar = el("span", { class: "pv-group" },
      btn("minus", null, "缩小 (-)", () => zoomBy(p, 1 / STEP)), p.zoomLabel, btn("plus", null, "放大 (+)", () => zoomBy(p, STEP)),
      el("span", { class: "pv-sep" }),
      btn("fitw", "适应宽度", "适应宽度", () => fit(p, "fitw")),
      btn("fit", "适应窗口", "适应窗口（整体可见）", () => fit(p, "fit")),
      btn(null, "1:1", "恢复默认比例 100% (0)", () => { S.driver = p; setZoom(p, 1); }));
    interactive(p);
    const ro = new ResizeObserver(() => { if (p.mode !== "manual") fit(p, p.mode, true); });
    ro.observe(p.view);
    S.observers.push(ro);
    return p;
  }

  // left: the invoice page
  function srcPane() {
    const p = S.src;
    p.pageInfo = el("span", { class: "pv-pageinfo" });
    p.prevBtn = btn("prev", null, "上一页", () => goPage(S.page - 1));
    p.nextBtn = btn("next", null, "下一页", () => goPage(S.page + 1));
    p.tableBtn = el("button", { type: "button", text: "表格区域", title: "只显示明细表格（表头、明细行、Subtotal）" });
    p.pageBtn = el("button", { type: "button", text: "整页", title: "显示整页原始发票" });
    p.tableBtn.onclick = () => setView("table");
    p.pageBtn.onclick = () => setView("page");
    p.img = el("img", { alt: "原始发票" });
    p.layer = el("div");
    p.sheet = el("div", { class: "pv-sheet" }, p.img, p.layer);
    p.gutter = el("div", { class: "pv-gutter" });
    p.doc = el("div", { class: "pv-doc" }, p.gutter, p.sheet);
    p.view.append(el("div", { class: "pv-stage" }, p.doc));
    const bar = el("div", { class: "pane-bar" },
      el("span", { class: "pane-title" }, "原始 Invoice ", el("small", { text: "PDF" })),
      el("span", { class: "pv-group" }, p.prevBtn, p.pageInfo, p.nextBtn),
      el("span", { class: "pv-seg" }, p.tableBtn, p.pageBtn),
      el("span", { class: "pane-sub" }, p.zoomBar));
    return el("section", { class: "pane", "aria-label": "原始 Invoice" }, bar, p.view);
  }

  function viewRect() {
    const pg = S.pv.pages[S.page];
    const t = S.view === "table" && pg.table;
    return t ? { x: t[0], y: t[1], w: t[2] - t[0], h: t[3] - t[1] } : { x: 0, y: 0, w: pg.width, h: pg.height };
  }

  const box = (node, b, prefix = "") => {
    node.style.setProperty(`--${prefix}x0`, b[0]);
    node.style.setProperty(`--${prefix}y0`, b[1]);
    node.style.setProperty(`--${prefix}x1`, b[2]);
    node.style.setProperty(`--${prefix}y1`, b[3]);
    return node;
  };

  function renderPage() {
    const p = S.src, pv = S.pv, pg = pv.pages[S.page], r = viewRect();
    for (const [k, v] of Object.entries({ vx: r.x, vy: r.y, vw: r.w, vh: r.h, pw: pg.width, ph: pg.height })) p.doc.style.setProperty("--" + k, v);
    p.sheet.querySelector(".noimg")?.remove();
    if (pg.image) { p.img.hidden = false; p.img.src = pg.image; } else {
      p.img.hidden = true;
      p.sheet.prepend(el("div", { class: "noimg", text: "这一页无法显示：" + (pg.error || "") }));
    }
    const layer = [], gutter = [];
    // a header gets the anchor of the gap before the first line below it, so scrolling stays in step
    const before = (top) => pv.lines.filter((l) => l.page != null && (l.page < S.page || (l.page === S.page && l.bbox[1] < top))).length;
    for (const h of pv.headers) if (h.page === S.page) layer.push(box(el("div", { class: "ov head", "data-anchor": before(h.bbox[1]) + 0.5 }), h.bbox));
    pv.lines.forEach((l, i) => {
      if (l.page !== S.page) return;
      const n = i + 1, iss = S.issues.get(n);
      const ov = box(el("div", { class: "ov line" + (iss ? " " + iss.level : ""), "data-line": n, "data-anchor": n, title: tip(n) }), l.bbox);
      for (const [f, c] of Object.entries(l.cells)) {
        const level = iss && iss.fields.get(f);
        ov.append(box(el("div", { class: "c" + (level ? " " + level : ""), "data-field": f }), c, "c"));
      }
      layer.push(ov);
      const no = el("span", { class: "pv-no" + (iss ? " " + iss.level : ""), "data-line": n, title: tip(n), text: String(n) });
      no.style.setProperty("--y", (l.bbox[1] + l.bbox[3]) / 2);
      gutter.push(no);
    });
    if (pv.totals && pv.totals.page === S.page) {
      const bad = S.result.checks.filter((c) => !c.ok && c.name.startsWith("Subtotal"));
      const badFields = new Set(bad.map((c) => c.name.replace(/^Subtotal\s*/, "")));
      const t = box(el("div", { class: "ov totals " + (bad.length ? "bad" : "good"), "data-anchor": S.n + 0.5, "data-sum": "1",
        title: S.result.checks.filter((c) => c.name.startsWith("Subtotal")).map((c) => (c.ok ? "✓ " : "✗ ") + c.name + "：" + c.detail).join("\n") }), pv.totals.bbox);
      for (const [f, c] of Object.entries(pv.totals.cells)) if (badFields.has(f)) t.append(box(el("div", { class: "c bad" }), c, "c"));
      layer.push(t);
      const sum = el("span", { class: "pv-no sum " + (bad.length ? "bad" : "good"), "data-sum": "1", title: t.title, text: "Σ" });
      sum.style.setProperty("--y", (pv.totals.bbox[1] + pv.totals.bbox[3]) / 2);
      gutter.push(sum);
    }
    p.layer.replaceChildren(...layer);
    p.gutter.replaceChildren(...gutter);
    const count = pv.pages.length;
    p.pageInfo.textContent = `${S.page + 1} / ${count} 页`;
    p.pageInfo.title = `${pg.source} 第 ${pg.page} / ${pg.page_count} 页` + (pv.skipped_pages ? `\n另有 ${pv.skipped_pages} 页（不含明细表格）未显示` : "");
    p.prevBtn.disabled = S.page <= 0;
    p.nextBtn.disabled = S.page >= count - 1;
    p.tableBtn.disabled = !pg.table;
    p.tableBtn.classList.toggle("on", S.view === "table" && !!pg.table);
    p.pageBtn.classList.toggle("on", S.view !== "table" || !pg.table);
    restyle();
  }

  function tip(n) {
    const iss = S.issues.get(n);
    return `第 ${n} 行` + (iss ? "\n" + iss.list.map((x) => (x.level === "error" ? "✗ " : "⚠ ") + x.message).join("\n") : "");
  }

  function setPage(i) {
    if (i === S.page || i < 0 || i >= S.pv.pages.length) return;
    S.page = i;
    renderPage();
    if (S.src.mode !== "manual") fit(S.src, S.src.mode, true);
  }

  function goPage(i) {
    S.driver = S.src;
    setPage(i);
    S.src.view.scrollTop = 0;
  }

  function setView(v) {
    if (S.view === v) return;
    S.view = v;
    renderPage();
    fit(S.src, S.src.mode === "manual" ? "fitw" : S.src.mode, true);
    if (S.sel) level(S.sel, readHeight()); else S.src.view.scrollTop = 0;
  }

  // right: the template columns, as in the exported Excel file
  function outPane() {
    const p = S.out;
    const orig = el("input", { type: "checkbox" });
    orig.onchange = () => S.root.classList.toggle("show-orig", orig.checked);
    const bar = el("div", { class: "pane-bar" },
      el("span", { class: "pane-title" }, "转换结果 ", el("small", { text: "CargoWise 模板 · 与导出的 Excel / CSV 相同" })),
      el("label", { class: "pv-check", title: "在被转换过的单元格下显示 PDF 里的原文（例如去掉中文之前）" }, orig, "显示 PDF 原文"),
      el("span", { class: "pane-sub" }, p.zoomBar));
    p.table = el("table", { class: "pv-xl" });
    p.view.append(el("div", { class: "pv-stage" }, p.table));
    return el("section", { class: "pane", "aria-label": "转换结果" }, bar, p.view);
  }

  const styled = (node, s) => {
    if (!s) return node;
    node.style.fontFamily = `"${s.font}", Arial, sans-serif`;
    node.style.fontSize = `calc(${s.size}pt * var(--gz))`;
    node.style.fontWeight = s.bold ? "700" : "400";
    if (s.color) node.style.color = s.color;
    if (s.fill) node.style.backgroundColor = s.fill;
    return node;
  };

  function renderGrid() {
    const g = S.pv.grid, p = S.out;
    const segs = [];
    let prev = -1;
    for (const c of g.columns) {
      if (c.index > prev + 1) segs.push({ from: prev + 1, to: c.index - 1 });
      segs.push(c);
      prev = c.index;
    }
    if (prev < g.total_columns - 1) segs.push({ from: prev + 1, to: g.total_columns - 1 });
    const gapName = (s) => letter(s.from) + (s.to > s.from ? ":" + letter(s.to) : "");
    const gapTip = (s) => `${gapName(s)}：${s.to - s.from + 1} 列，导出文件中为空`;

    const widths = [40, ...segs.map((s) => (s.letter ? s.width : 44))];
    const cols = el("colgroup", {}, ...widths.map((w) => el("col", { style: `width: calc(${w}px * var(--gz))` })));
    // a fixed-layout table needs a width, or Chromium makes it as wide as it can (1,000,000 px)
    p.table.style.width = `calc(${widths.reduce((a, b) => a + b, 0)}px * var(--gz))`;
    const letters = el("tr", {}, el("th", { class: "ch rh" }),
      ...segs.map((s) => s.letter
        ? el("th", { class: "ch" + (s.field ? " target" : ""), text: s.letter, title: s.field ? `${s.letter} ${s.header} ← PDF ${fieldLabel(s.field)}` : `${s.letter} ${s.header}（自动生成）` })
        : el("th", { class: "ch gap", text: gapName(s), title: gapTip(s) })));
    const head = el("tr", {}, el("th", { class: "rh", text: String(g.header_row) }),
      ...segs.map((s) => (s.letter ? styled(el("th", { text: s.header }), s.head_style) : el("th", { class: "gap", title: gapTip(s) }))));
    const rows = [];
    for (let i = 0; i < S.n; i++) {
      const n = i + 1, iss = S.issues.get(n), line = S.result.lines[i];
      const tr = el("tr", { "data-line": n, "data-anchor": n, title: tip(n) }, el("td", { class: "rh", text: String(g.header_row + n) }));
      for (const s of segs) {
        if (!s.letter) { tr.append(el("td", { class: "gap" })); continue; }
        const cell = s.cells[i], level = iss && iss.cols.get(s.letter);
        const original = line.original[s.letter];
        const changed = original !== undefined && original !== cell.v;
        const cls = [cell.num && "num", s.field && cell.v === "" && "empty", level, changed && "changed"].filter(Boolean).join(" ");
        const td = styled(el("td", { class: cls || null, "data-field": s.field || null, "data-copy": cell.v }), s.cell_style);
        td.append(cell.v);
        if (changed) {
          td.title = `PDF 原文：${original}`;
          td.append(el("span", { class: "orig", text: "PDF：" + (original || "（空）") }));
        }
        tr.append(td);
      }
      rows.push(tr);
    }
    p.table.replaceChildren(cols, el("thead", {}, letters, head), el("tbody", {}, ...rows));
    restyle();
  }

  const fieldLabel = (f) => (S.pv.fields.find((x) => x.name === f) || { label: f }).label;

  // ───────────────────────────── zoom & pan ─────────────────────────────

  function restyle() {
    for (const p of [S.src, S.out]) {
      if (!p) continue;
      if (p.kind === "src") p.doc.style.setProperty("--z", p.zoom * PT); else p.table.style.setProperty("--gz", p.zoom);
      p.zoomLabel.textContent = Math.round(p.zoom * 100) + "%";
    }
  }

  function fitZoom(p, whole) {
    const w = p.view.clientWidth - 34, h = p.view.clientHeight - 34;
    if (p.kind === "src") {
      const r = viewRect();
      const zw = (w - 60) / (r.w * PT), zh = h / (r.h * PT);
      return whole ? Math.min(zw, zh) : zw;
    }
    const w1 = p.table.offsetWidth / p.zoom, h1 = p.table.offsetHeight / p.zoom;
    return whole ? Math.min(w / w1, h / h1) : w / w1;
  }

  function fit(p, mode, keep = false) {
    if (!p.view.clientWidth) return;
    let z = fitZoom(p, mode === "fit");
    if (mode === "auto") z = p.kind === "src" ? clamp(z, 0.3, 2) : clamp(z, 0.8, 1.6);
    if (!keep) S.driver = p;
    setZoom(p, z, mode);
  }

  function zoomBy(p, factor, at) {
    S.driver = p;
    setZoom(p, p.zoom * factor, "manual", at);
  }

  function setZoom(p, z, mode = "manual", at = null) {
    z = clamp(z, p.min, p.max);
    const target = p.kind === "src" ? p.sheet : p.table;
    const vr = p.view.getBoundingClientRect();
    const ax = at ? at.x : vr.left + p.view.clientWidth / 2;
    const ay = at ? at.y : vr.top + p.view.clientHeight / 2;
    const r = target.getBoundingClientRect();
    const fx = r.width ? (ax - r.left) / r.width : 0, fy = r.height ? (ay - r.top) / r.height : 0;
    p.zoom = z;
    p.mode = mode;
    restyle();
    const r2 = target.getBoundingClientRect();
    p.view.scrollLeft += r2.left + fx * r2.width - ax;
    p.view.scrollTop += r2.top + fy * r2.height - ay;
  }

  const lineOf = (t) => { const e = t && t.closest && t.closest("[data-line]"); return e ? +e.dataset.line : null; };
  const fieldOf = (t) => { const e = t && t.closest && t.closest("[data-field]"); return e ? e.dataset.field : null; };

  function interactive(p) {
    const v = p.view;
    let drag = null;
    // events can still arrive after close() (the Esc that closed it, for one): S is null then
    for (const type of ["wheel", "keydown", "touchstart"]) v.addEventListener(type, () => { if (S) S.driver = p; }, { passive: true });
    v.addEventListener("wheel", (e) => {
      if (!e.ctrlKey && !e.metaKey) return; // plain wheel scrolls; Ctrl + wheel (or pinch) zooms at the pointer
      e.preventDefault();
      zoomBy(p, Math.exp(-e.deltaY * (e.deltaMode === 1 ? 0.05 : 0.0025)), { x: e.clientX, y: e.clientY });
    }, { passive: false });
    v.addEventListener("scroll", () => onScroll(p), { passive: true });
    v.addEventListener("pointerdown", (e) => {
      if (!S) return;
      S.driver = p;
      p.down = e.target;
      if (e.button !== 0 || e.pointerType === "touch") return; // touch: the browser scrolls natively
      const r = v.getBoundingClientRect();
      if (e.clientX - r.left >= v.clientWidth || e.clientY - r.top >= v.clientHeight) return; // on a scrollbar
      drag = { id: e.pointerId, x: e.clientX, y: e.clientY, left: v.scrollLeft, top: v.scrollTop, moved: false, target: e.target };
      v.setPointerCapture(e.pointerId);
      v.focus({ preventScroll: true });
    });
    v.addEventListener("pointermove", (e) => {
      if (!drag || e.pointerId !== drag.id) return;
      const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
      if (!drag.moved && Math.hypot(dx, dy) < 4) return;
      if (!drag.moved) { drag.moved = true; v.classList.add("panning"); hover(null, null); }
      v.scrollLeft = drag.left - dx;
      v.scrollTop = drag.top - dy;
    });
    const end = (e) => {
      if (!drag || e.pointerId !== drag.id) return;
      const d = drag;
      drag = null;
      v.classList.remove("panning");
      if (S && !d.moved && e.type === "pointerup") pick(d.target, p);
    };
    v.addEventListener("pointerup", end);
    v.addEventListener("pointercancel", end);
    v.addEventListener("click", (e) => { if (S && p.lastTouch) pick(e.target, p); });
    v.addEventListener("pointerdown", (e) => { p.lastTouch = e.pointerType === "touch"; }, true);
    v.addEventListener("pointerover", (e) => { if (!drag || !drag.moved) hover(lineOf(e.target), fieldOf(e.target)); });
    v.addEventListener("pointerleave", () => { if (!drag) hover(null, null); });
    v.addEventListener("dblclick", () => {
      if (!S) return;
      const td = p.down && p.down.closest && p.down.closest("td[data-copy]");
      if (td && td.dataset.copy && navigator.clipboard) navigator.clipboard.writeText(td.dataset.copy).then(() => toast("已复制：" + td.dataset.copy), () => {});
    });
  }

  function pick(target, p) {
    if (target && target.closest && target.closest("[data-sum]")) { S.sel = null; mark("sel", null); detailTotals(); return; }
    const n = lineOf(target);
    if (n != null) select(n, p);
  }

  // ───────────────────────────── linking the two sides ─────────────────────────────

  function mark(cls, n) {
    for (const e of S.root.querySelectorAll("." + cls)) e.classList.remove(cls);
    if (n != null) for (const e of S.root.querySelectorAll(`[data-line="${n}"]`)) e.classList.add(cls);
  }

  function hover(n, field) {
    if (!S || !S.src || (S.hover[0] === n && S.hover[1] === field)) return;
    S.hover = [n, field];
    mark("hover", n);
    for (const e of S.root.querySelectorAll(".hot")) e.classList.remove("hot");
    if (n != null && field) for (const e of S.root.querySelectorAll(`[data-line="${n}"] [data-field="${CSS.escape(field)}"]`)) e.classList.add("hot");
  }

  // `from`: the pane that was clicked – the other pane brings the same line level with it;
  // "reveal": scroll only when the line is out of sight; null: both panes show the line at the reading height
  function select(n, from = null) {
    if (n == null || n < 1 || n > S.n) return;
    S.sel = n;
    mark("sel", n);
    detail(n);
    issueNav();
    if (from === "reveal" && visible(S.src, n) && visible(S.out, n)) return;
    const y = from && from !== "reveal" ? anchorY(from, n) : null;
    level(n, y != null ? y - from.view.scrollTop : readHeight());
  }

  const readHeight = () => Math.min(S.src.view.clientHeight, S.out.view.clientHeight) * READ_LINE;

  function visible(p, n) {
    if (p === S.src && S.pv.lines[n - 1].page !== S.page) return false;
    const y = anchorY(p, n);
    return y != null && y > p.view.scrollTop + 30 && y < p.view.scrollTop + p.view.clientHeight - 30;
  }

  // line n at height `at` in both panes (lower, if a pane cannot scroll that far up)
  function level(n, at) {
    const pg = S.pv.lines[n - 1] && S.pv.lines[n - 1].page;
    if (pg == null) return;
    setPage(pg);
    const ys = [S.src, S.out].map((p) => anchorY(p, n));
    if (ys.includes(null)) return;
    at = Math.min(at, ...ys);
    S.driver = null; // these scrolls must not be echoed by the scroll sync
    S.src.view.scrollTop = ys[0] - at;
    S.out.view.scrollTop = ys[1] - at;
  }

  function anchorY(p, key) {
    const e = p.view.querySelector(`[data-anchor="${key}"]`);
    if (!e) return null;
    const r = e.getBoundingClientRect();
    return r.top - p.view.getBoundingClientRect().top + p.view.scrollTop + r.height / 2;
  }

  function anchors(p) {
    const top = p.view.getBoundingClientRect().top - p.view.scrollTop;
    const list = [];
    for (const e of p.view.querySelectorAll("[data-anchor]")) {
      const r = e.getBoundingClientRect();
      list.push({ key: +e.dataset.anchor, y: r.top - top + r.height / 2 });
    }
    return list.sort((a, b) => a.key - b.key);
  }

  // position <-> line number ("key", fractional between lines), straight lines between anchors
  function keyAt(a, y) {
    if (!a.length) return null;
    if (a.length === 1) return a[0].key;
    let i = 0;
    while (i < a.length - 2 && y > a[i + 1].y) i++;
    const p = a[i], q = a[i + 1];
    return p.key + ((y - p.y) / (q.y - p.y || 1)) * (q.key - p.key);
  }

  function yAt(a, k) {
    if (!a.length) return null;
    if (a.length === 1) return a[0].y;
    let i = 0;
    while (i < a.length - 2 && k > a[i + 1].key) i++;
    const p = a[i], q = a[i + 1];
    return p.y + ((k - p.key) / (q.key - p.key || 1)) * (q.y - p.y);
  }

  // only the pane the user is moving drives the other one – programmatic scrolls do not echo back
  function onScroll(p) {
    if (!S || !S.sync || S.driver !== p || !S.src) return;
    const other = p === S.src ? S.out : S.src;
    const k = keyAt(anchors(p), p.view.scrollTop + p.view.clientHeight * READ_LINE);
    if (k == null) return;
    if (other === S.src) {
      const line = S.pv.lines[clamp(Math.round(k), 1, S.n) - 1];
      if (line && line.page != null && line.page !== S.page) setPage(line.page);
    }
    const y = yAt(anchors(other), k);
    if (y != null) other.view.scrollTop = y - other.view.clientHeight * READ_LINE;
  }

  // ───────────────────────────── issues, details, export ─────────────────────────────

  function issueNav() {
    const lines = S.issueLines;
    const errors = lines.filter((n) => S.issues.get(n).level === "error").length;
    const warnings = lines.length - errors;
    const at = S.sel != null ? lines.indexOf(S.sel) : -1;
    const text = !lines.length ? "✓ 未发现问题"
      : (at >= 0 ? `第 ${at + 1}/${lines.length} 处 · ` : "") + [errors && `${errors} 行错误`, warnings && `${warnings} 行警告`].filter(Boolean).join(" · ");
    const prev = btn("up", "上一个", "上一个问题", () => jump(-1));
    const next = btn("down", "下一个", "下一个问题", () => jump(1));
    prev.disabled = next.disabled = !lines.length;
    S.issueBox.replaceChildren(prev, el("span", { class: "count " + (errors ? "error" : warnings ? "warning" : "ok"), text }), next);
  }

  function jump(dir) {
    const lines = S.issueLines;
    if (!lines.length) return;
    const cur = S.sel == null ? (dir > 0 ? 0 : S.n + 1) : S.sel;
    const n = dir > 0 ? lines.find((x) => x > cur) ?? lines[0] : [...lines].reverse().find((x) => x < cur) ?? lines[lines.length - 1];
    S.driver = null;
    select(n);
  }

  function detail(n) {
    if (n == null) {
      S.detail.replaceChildren(el("div", { class: "empty", text: "点击左侧发票或右侧表格的任意一行，查看这一行的逐项对照；↑ ↓ 键逐行移动，Ctrl + 滚轮缩放，按住拖动查看。" }));
      return;
    }
    const line = S.pv.lines[n - 1], pg = line && line.page != null ? S.pv.pages[line.page] : null;
    const item = S.pv.items[n - 1] || {}, iss = S.issues.get(n);
    const value = (v) => el("b", { text: v === "" || v == null ? "（空）" : v });
    const pdf = el("dd", {}, ...S.pv.fields.map((f) => el("span", {}, el("i", { text: f.label + " " }), value(item[f.name]))));
    const out = el("dd", {}, ...S.pv.grid.columns.map((c) => el("span", {}, el("i", { text: `${c.letter} ${c.header} ` }), value(c.cells[n - 1].v))));
    const checks = iss
      ? el("ul", {}, ...iss.list.map((x) => el("li", { class: x.level, text: (x.level === "error" ? "✗ " : "⚠ ") + x.message })))
      : el("span", { text: "✓ 这一行没有发现问题" });
    S.detail.replaceChildren(
      el("h3", {}, `第 ${n} 行 `, el("span", { text: `· Excel 第 ${S.pv.grid.header_row + n} 行` + (pg ? ` · ${pg.source} 第 ${pg.page} 页` : "") })),
      el("dl", {}, el("dt", { text: "PDF 原文" }), pdf, el("dt", { text: "导出结果" }), out, el("dt", { text: "校验" }), el("dd", {}, checks)));
  }

  function detailTotals() {
    const t = S.pv.totals_values || {};
    const checks = S.result.checks.filter((c) => c.name.startsWith("Subtotal"));
    S.detail.replaceChildren(
      el("h3", { text: "Subtotal 合计行" }),
      el("dl", {},
        el("dt", { text: "PDF 合计" }), el("dd", {}, ...S.pv.fields.filter((f) => t[f.name]).map((f) => el("span", {}, el("i", { text: f.label + " " }), el("b", { text: t[f.name] })))),
        el("dt", { text: "校验" }), el("dd", {}, el("ul", {}, ...checks.map((c) => el("li", { class: c.ok ? "" : "error", text: (c.ok ? "✓ " : "✗ ") + c.name + "：" + c.detail }))))));
  }

  function footer() {
    const r = S.result, download = S.opts.download;
    if (S.opts.actions) { // a saved document: the caller's buttons (export through the documents API)
      const open = S.issueLines.length;
      S.actions.replaceChildren(el("span", { class: "hint", text: open ? `还有 ${open} 行需复核` : "已对照：可以导出" }), ...S.opts.actions(r));
      return;
    }
    const csv = el("button", { type: "button", class: "primary", text: "下载 CSV" });
    csv.onclick = () => download(r.content_base64, r.filename, "text/csv");
    const xlsx = el("button", { type: "button", class: "secondary", text: "下载 Excel" });
    xlsx.onclick = () => download(r.xlsx_base64, r.xlsx_filename, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet");
    const open = S.issueLines.length;
    const hint = el("span", { class: "hint", text: open ? `还有 ${open} 行需复核，确认无误后再导出` : "已对照：可以导出" });
    S.actions.replaceChildren(hint, csv, r.xlsx_base64 ? xlsx : null);
  }

  function toast(text) {
    S.root.querySelector(".pv-toast")?.remove();
    const t = el("div", { class: "pv-toast", text });
    S.root.append(t);
    setTimeout(() => t.remove(), 1600);
  }

  function onKey(e) {
    if (!S) return;
    if (e.key === "Escape") {
      if (!document.fullscreenElement) { e.preventDefault(); close(); }
      return;
    }
    if (!S.src || e.ctrlKey || e.metaKey || e.altKey || (e.target.closest && e.target.closest("input, select, textarea"))) return;
    const pane = hoverPane() || S.driver || S.src;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const n = S.sel == null ? (e.key === "ArrowDown" ? 1 : S.n) : clamp(S.sel + (e.key === "ArrowDown" ? 1 : -1), 1, S.n);
      S.driver = null;
      select(n, "reveal");
    } else if (e.key === "+" || e.key === "=") zoomBy(pane, STEP);
    else if (e.key === "-" || e.key === "_") zoomBy(pane, 1 / STEP);
    else if (e.key === "0") { S.driver = pane; setZoom(pane, 1); }
    else if (e.key === "f" || e.key === "F") toggleFull();
    else return;
    e.preventDefault();
  }

  // the pane under the mouse pointer: keyboard zoom applies to it
  function hoverPane() {
    for (const p of [S.src, S.out]) if (p.view.matches(":hover")) return p;
    return null;
  }

  window.CIVPreview = { open, close };
  Object.defineProperty(window.CIVPreview, "state", { get: () => S }); // for debugging in the browser console
})();
