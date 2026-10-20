/* The document page: the online sheet, autosave, local backup, export, Confirm, history.

   Nothing typed may get lost:
   - every change is written to localStorage at once (a draft) and autosaved ~1.5 s later
   - the server only accepts a save based on the latest revision (two tabs / two people
     cannot silently overwrite each other); a conflict asks the user what to do
   - offline / server errors: the draft stays and the save is retried; leaving the page with
     unsaved changes asks first; a draft found when the page opens can be restored */
(() => {
  "use strict";
  const { $, el, api, fmtTime, toast, ask, download, statusBadges } = CIV;
  const docId = decodeURIComponent(location.pathname.split("/").pop());
  const DRAFT = `civconvert.draft.${docId}`;
  const AUTOSAVE_MS = 1500;

  let detail = null, doc = null, sheet = null, user = null;
  let revision = 0, changes = 0, savedChanges = 0;
  let saving = null, saveToken = null, saveTimer = null, retryTimer = null, retryDelay = 2000, lastSavedAt = null;
  let flags = [], issueRows = [], selected = null;

  // ───────────── load ─────────────
  async function load() {
    try {
      detail = await api(`/api/documents/${encodeURIComponent(docId)}`);
    } catch (e) {
      if (e.status === 401) return;
      center(el("div", { class: "panel" }, el("h2", { text: e.status === 404 ? "文档不存在" : "打开文档失败" }), el("p", { text: e.message }), el("a", { class: "btn", href: "/" }, "返回工作面板")));
      return;
    }
    doc = detail.document;
    header();
    if (doc.status === "processing") {
      center(el("div", {}, el("p", {}, el("span", { class: "spin" }), " 正在识别和转换 PDF…"), el("p", { class: "hint", text: "通常只需几秒钟，完成后自动打开表格。" })));
      setTimeout(load, 1500);
      return;
    }
    if (doc.status === "failed") { failed(); return; }
    open();
  }

  function center(node) {
    $("#center").hidden = false;
    $("#center").replaceChildren(node);
    for (const id of ["#toolbar", "#fbar", "#gridwrap", "#statusbar"]) $(id).hidden = true;
  }

  function header() {
    document.title = `${doc.name} · CIVconvert`;
    $("#title").textContent = doc.name;
    $("#badges").replaceChildren(...statusBadges(doc));
    const ex = detail.extraction || {};
    $("#meta").textContent = [doc.invoice_no && `发票号 ${doc.invoice_no}`, doc.invoice_date, ex.customer && ex.customer.name,
      doc.created_by && `${doc.created_by.name} 上传于 ${fmtTime(doc.created_at)}`,
      doc.last_exported_at && `最后导出 ${fmtTime(doc.last_exported_at)}`].filter(Boolean).join(" · ");
  }

  async function failed() {
    const sel = el("select", { "aria-label": "客户" }, el("option", { value: "" }, "自动识别"));
    try { for (const c of await api("/api/customers")) sel.append(el("option", { value: c.id }, `${c.name}（${c.id}）`)); } catch { /* keep auto */ }
    const retry = el("button", { type: "button", class: "btn primary", text: "重新转换" });
    retry.onclick = async () => {
      retry.disabled = true;
      try {
        await api(`/api/documents/${docId}/retry`, { method: "POST", json: { customer: sel.value || null } });
        load();
      } catch (e) { toast(e.message, "err"); retry.disabled = false; }
    };
    center(el("div", { class: "panel" },
      el("h2", { text: "这份 PDF 没有转换成功" }),
      el("p", { class: "msg err", text: doc.error || "未知错误" }),
      el("p", { class: "hint", text: "常见原因：客户没有被识别（请在下面选择客户）、PDF 是扫描件（没有文字层）、表格格式与客户配置不一致。" }),
      el("div", { style: "display:flex;gap:8px;flex-wrap:wrap;align-items:center" }, sel, retry,
        ...doc.files.map((f) => el("a", { class: "btn", href: `/api/documents/${docId}/files/${f.id}`, target: "_blank", rel: "noopener" }, `查看 ${f.filename}`)))));
  }

  function open() {
    $("#center").hidden = true;
    for (const id of ["#toolbar", "#fbar", "#gridwrap", "#statusbar"]) $(id).hidden = false;
    const t = detail.template;
    const converted = (detail.extraction && detail.extraction.converted) || [];
    if (!sheet) {
      sheet = new CIVSheet($("#grid"), {
        columns: t.columns, headerRow: t.header_row, roles: t.roles,
        readOnly: doc.status !== "editing",
        original: (src, letter) => (converted[src - 1] ? converted[src - 1][letter] : undefined),
        onChange: changed,
        onSelect: selectedCell,
        onEditInput: (text) => { $("#formula").value = text; },
        onContextMenu: contextMenu,
        onReadOnly: () => toast("文档已确认（只读）。需要修改请点“重新编辑”"),
      });
    }
    revision = detail.sheet.revision;
    lastSavedAt = detail.sheet.updated_at;
    sheet.setRows(detail.sheet.rows);
    setFlags(detail.sheet.flags);
    sheet.setReadOnly(doc.status !== "editing");
    banners();
    toolbar();
    saveState(doc.status === "editing" ? "saved" : "");
    restoreDraft();
    sheet.focus();
  }

  // ───────────── banners ─────────────
  function banner(kind, text, actions = []) {
    return el("div", { class: "banner " + kind }, el("span", { class: "grow", text }), ...actions);
  }
  function banners(extra) {
    const out = [];
    if (doc.status === "confirmed") {
      const by = doc.confirmed_by ? doc.confirmed_by.name : "";
      out.push(banner("ok", `✓ 已确认 / Ready${by ? `（${by}，${fmtTime(doc.confirmed_at)}）` : ""}。表格为只读，可以导出最终版 CSV 交付 CargoWise。`, [
        el("button", { type: "button", class: "btn small primary", onclick: () => exportFile("csv") }, "导出最终版 CSV"),
      ]));
    }
    if (extra) out.push(extra);
    $("#banners").replaceChildren(...out);
  }

  // ───────────── changes & saving ─────────────
  function changed() {
    changes++;
    writeDraft();
    saveState("dirty");
    toolbar();
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => save("auto"), AUTOSAVE_MS);
  }
  const dirty = () => changes !== savedChanges;

  function writeDraft() {
    try { localStorage.setItem(DRAFT, JSON.stringify({ base: revision, at: Date.now(), rows: sheet.getRows() })); } catch { /* storage full or private mode */ }
  }
  function readDraft() {
    try { return JSON.parse(localStorage.getItem(DRAFT) || "null"); } catch { return null; }
  }
  function clearDraft() {
    try { localStorage.removeItem(DRAFT); } catch { /* ignore */ }
  }

  function restoreDraft() {
    const d = readDraft();
    if (!d || doc.status !== "editing") return;
    if (JSON.stringify(d.rows) === JSON.stringify(sheet.getRows())) { clearDraft(); return; }
    const when = fmtTime(new Date(d.at).toISOString());
    const newer = d.base !== revision ? "服务器上的文档在那之后又被保存过；恢复会用本机的版本替换它。" : "";
    banners(banner("warn", `发现本机暂存的未保存修改（${when}），可能是上次关闭页面或断网时留下的。${newer}`, [
      el("button", { type: "button", class: "btn small primary", onclick: () => {
        sheet.setRows(d.rows);
        banners();
        changed();
        toast("已恢复本机暂存的修改");
      } }, "恢复这些修改"),
      el("button", { type: "button", class: "btn small", onclick: () => { clearDraft(); banners(); } }, "丢弃"),
    ]));
  }

  function saveState(kind, text) {
    const box = $("#savestate");
    box.className = "savestate " + kind;
    const label = {
      saved: () => (lastSavedAt ? `✓ 已保存 ${fmtTime(lastSavedAt, false)}` : "✓ 已保存"),
      dirty: () => "● 有未保存的修改",
      saving: () => "正在保存…",
      offline: () => "⚠ 网络中断：修改已暂存在本机，恢复后自动保存",
      error: () => `⚠ 保存失败：${text || ""}`,
      "": () => (lastSavedAt ? `最后修改 ${fmtTime(lastSavedAt)}` : ""),
    }[kind]();
    box.replaceChildren(kind === "saving" ? el("span", { class: "spin" }) : "", label);
    if (kind === "error") box.append(" ", el("button", { type: "button", class: "link", onclick: () => save("manual") }, "重试"));
  }

  /** Save the sheet. Resolves true when everything typed so far is on the server.
      keepalive: the page may be closing – the request still reaches the server (bodies up to 64 KB). */
  async function save(reason = "auto", force = false, keepalive = false) {
    if (!sheet || doc.status !== "editing") return true;
    clearTimeout(saveTimer);
    if (reason === "auto" && sheet.editing && !keepalive) { saveTimer = setTimeout(() => save("auto"), AUTOSAVE_MS); return false; } // typing in a cell
    if (reason !== "auto") sheet.commitEdit(null);
    if (saving) { await saving; if (!dirty() && reason === "auto") return true; }
    if (!dirty() && reason === "auto") return true;
    const sent = changes;
    const rows = sheet.getRows();
    const body = { revision, rows, reason, force };
    keepalive = keepalive && JSON.stringify(body).length < 60000;
    saveState("saving");
    const token = {};
    saveToken = token;
    saving = (async () => {
      try {
        const res = await api(`/api/documents/${docId}/sheet`, { method: "PUT", json: body, keepalive });
        revision = res.revision;
        lastSavedAt = res.saved_at;
        savedChanges = sent;
        retryDelay = 2000;
        doc.needs_review = res.needs_review;
        doc.line_count = res.line_count;
        $("#badges").replaceChildren(...statusBadges(doc));
        setFlags(res.flags);
        if (dirty()) { saveState("dirty"); saveTimer = setTimeout(() => save("auto"), AUTOSAVE_MS); } else { clearDraft(); saveState("saved"); }
        return true;
      } catch (e) {
        if (e.status === 409 && e.data && e.data.conflict) return conflict(e.data);
        if (e.status === 409) { toast(e.message, "err"); await reload(); return false; }
        if (e.offline || e.status >= 500 || e.status === 0) {
          saveState("offline");
          clearTimeout(retryTimer);
          retryTimer = setTimeout(() => save("auto"), retryDelay);
          retryDelay = Math.min(retryDelay * 2, 30000);
          return false;
        }
        saveState("error", e.message);
        return false;
      } finally {
        if (saveToken === token) saving = null; // not when a newer save (after a conflict) has started
        toolbar();
      }
    })();
    return saving;
  }

  async function conflict(info) {
    const choice = await new Promise((resolve) => {
      const dlg = el("dialog", { class: "dlg" });
      const pick = (v) => { dlg.close(); dlg.remove(); resolve(v); };
      dlg.append(
        el("div", { class: "dlg-body" }, el("h3", { text: "这份文档已在别处被修改" }),
          el("p", { text: `${info.updated_by || "其他人"} 在 ${fmtTime(info.updated_at)} 保存了新版本（第 ${info.revision} 版）。你在这里的修改还没有保存（已暂存在本机）。` }),
          el("p", { class: "hint", text: "重新加载：打开对方的最新版本（你的修改可在打开后选择“恢复”）。覆盖：用你的版本替换对方的修改。" })),
        el("div", { class: "dlg-actions" },
          el("button", { type: "button", class: "btn", onclick: () => pick("later") }, "稍后决定"),
          el("button", { type: "button", class: "btn danger", onclick: () => pick("overwrite") }, "用我的版本覆盖"),
          el("button", { type: "button", class: "btn primary", onclick: () => pick("reload") }, "重新加载")),
      );
      dlg.addEventListener("cancel", (e) => { e.preventDefault(); pick("later"); });
      document.body.append(dlg);
      dlg.showModal();
    });
    if (choice === "overwrite") { saving = null; return save("manual", true); }
    if (choice === "reload") { savedChanges = changes; await reload(); return false; }
    saveState("error", "与服务器上的新版本冲突");
    return false;
  }

  async function reload() {
    detail = await api(`/api/documents/${docId}`);
    doc = detail.document;
    header();
    changes = savedChanges = 0;
    open();
  }

  // ───────────── flags / issues ─────────────
  function setFlags(list) {
    flags = list || [];
    sheet.setFlags(flags);
    const worst = new Map();
    for (const f of flags) worst.set(f.line, worst.get(f.line) === "error" || f.level === "error" ? "error" : "warning");
    issueRows = [...worst.keys()].sort((a, b) => a - b);
    const errors = issueRows.filter((n) => worst.get(n) === "error").length;
    const warnings = issueRows.length - errors;
    const text = issueRows.length ? [errors && `${errors} 行错误`, warnings && `${warnings} 行警告`].filter(Boolean).join(" · ") : "✓ 未发现问题";
    const prev = el("button", { type: "button", class: "btn small", title: "上一个问题", disabled: !issueRows.length, onclick: () => jump(-1) }, "‹");
    const next = el("button", { type: "button", class: "btn small", title: "下一个问题", disabled: !issueRows.length, onclick: () => jump(1) }, "›");
    $("#issues").replaceChildren(prev, el("span", { class: "count " + (errors ? "error" : warnings ? "warning" : "ok"), text }), next);
  }

  function jump(dir) {
    if (!issueRows.length) return;
    const cur = selected ? selected.r + 1 : 0;
    const n = dir > 0 ? (issueRows.find((x) => x > cur) ?? issueRows[0]) : ([...issueRows].reverse().find((x) => x < cur) ?? issueRows[issueRows.length - 1]);
    const f = flags.find((x) => x.line === n && x.columns && x.columns.length);
    const col = f ? detail.template.columns.findIndex((c) => c.letter === f.columns[0]) : 0;
    sheet.selectCell(n - 1, Math.max(col, 0));
    sheet.focus();
  }

  // ───────────── formula bar & status bar ─────────────
  function selectedCell(info) {
    selected = info;
    $("#addr").textContent = info.address;
    const input = $("#formula");
    if (document.activeElement !== input) input.value = info.raw;
    input.readOnly = doc.status !== "editing";
    const parts = [];
    if (info.cells > 1) {
      parts.push(`选中 ${info.cells} 个单元格`, `非空 ${info.count}`);
      if (info.nums) parts.push(`求和 ${CIVFormula.formatNumber(info.sum)}`, `平均 ${CIVFormula.formatNumber(info.sum / info.nums)}`);
    }
    parts.push(`${sheet.rowCount} 行`, `模板 ${detail.template.id}（${detail.template.columns.length} 列，列不可改）`);
    const bits = [el("span", { text: parts.join(" · ") })];
    const flag = info.flag || (info.rowFlag && !info.flag ? info.rowFlag : null);
    if (flag) bits.push(el("span", { class: "msg-cell " + flag.level, text: (flag.level === "error" ? "✗ " : "⚠ ") + flag.msgs.join("；") }));
    const row = info.row;
    const items = detail.extraction && detail.extraction.items;
    if (row && row.src && items && items[row.src - 1]) {
      const it = items[row.src - 1];
      const fields = detail.extraction.fields || [];
      bits.push(el("span", { title: "这一行在 PDF 中的内容" }, `PDF 第 ${row.src} 行：` + fields.map((f) => `${f.label} ${it[f.name] || "–"}`).join(" · ")));
    } else if (row && !row.src) bits.push(el("span", { text: "手动添加的行（不在 PDF 中）" }));
    $("#statusbar").replaceChildren(...bits);
  }

  const formula = $("#formula");
  formula.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); sheet.setActiveContent(formula.value); sheet.moveBy(1, 0, false); sheet.focus(); }
    else if (e.key === "Escape") { e.preventDefault(); formula.value = selected ? selected.raw : ""; sheet.focus(); }
    else if (e.key === "Tab") { e.preventDefault(); sheet.setActiveContent(formula.value); sheet.moveBy(0, e.shiftKey ? -1 : 1, false); sheet.focus(); }
  });

  // ───────────── toolbar ─────────────
  function toolbar() {
    if (!sheet) return;
    const editing = doc.status === "editing";
    $("#undo").disabled = !sheet.canUndo();
    $("#redo").disabled = !sheet.canRedo();
    for (const id of ["#insert", "#delrows", "#clear", "#save", "#confirm"]) $(id).disabled = !editing;
    $("#confirm").hidden = !editing;
    $("#reopen").hidden = doc.status !== "confirmed";
    $("#save").textContent = dirty() ? "Save ●" : "Save";
  }

  $("#undo").onclick = () => { sheet.undo(); sheet.focus(); };
  $("#redo").onclick = () => { sheet.redo(); sheet.focus(); };
  $("#insert").onclick = (e) => {
    const r = e.currentTarget.getBoundingClientRect();
    CIV.menu(r.left, r.bottom + 4, [
      { label: "在上方插入行", run: () => { sheet.insertAtSelection("above"); sheet.focus(); } },
      { label: "在下方插入行", run: () => { sheet.insertAtSelection("below"); sheet.focus(); } },
      { label: "在末尾添加一行", run: () => { sheet.insertRows(sheet.rowCount, 1); sheet.focus(); } },
    ]);
  };
  $("#delrows").onclick = () => { sheet.deleteRows(sheet.selectedRows()); sheet.focus(); };
  $("#clear").onclick = () => { sheet.clearSelection(); sheet.focus(); };
  $("#save").onclick = async () => {
    const ok = await save("manual");
    if (ok) toast(`已保存（第 ${revision} 版）`);
  };
  $("#csv").onclick = () => exportFile("csv");
  $("#xlsx").onclick = () => exportFile("xlsx");
  $("#confirm").onclick = confirmDoc;
  $("#reopen").onclick = reopen;
  $("#preview").onclick = (e) => preview(e.currentTarget);
  $("#history").onclick = history;
  $("#pdf").onclick = (e) => {
    const files = doc.files;
    const go = (f) => window.open(`/api/documents/${docId}/files/${f.id}`, "_blank", "noopener");
    if (files.length === 1) { go(files[0]); return; }
    const r = e.currentTarget.getBoundingClientRect();
    CIV.menu(r.left, r.bottom + 4, files.map((f) => ({ label: f.filename, run: () => go(f) })));
  };

  async function flush() {
    if (doc.status !== "editing") return true;
    sheet.commitEdit(null);
    if (!dirty()) return true;
    const ok = await save("manual");
    if (!ok || dirty()) { toast("还有修改没有保存成功，请稍后再试", "err"); return false; }
    return true;
  }

  async function exportFile(fmt) {
    if (!(await flush())) return;
    try {
      await download(`/api/documents/${docId}/export?format=${fmt}&revision=${revision}`, "POST", `${doc.name}.${fmt}`);
      toast(fmt === "csv" ? `已导出 CSV（第 ${revision} 版${doc.status === "confirmed" ? "，最终版" : ""}）` : "已导出 Excel");
      const fresh = await api(`/api/documents/${docId}`);
      doc.last_exported_at = fresh.document.last_exported_at;
      doc.export_count = fresh.document.export_count;
      header();
    } catch (e) { toast("导出失败：" + e.message, "err"); }
  }

  async function confirmDoc() {
    if (!(await flush())) return;
    const errors = flags.filter((f) => f.level === "error").length, warnings = flags.length - errors;
    const pdfFailed = ((detail.extraction && detail.extraction.checks) || []).filter((c) => !c.ok);
    const body = el("div", {},
      el("p", { text: "确认后，这份文档将标记为“已确认 / Ready”，表示信息已经完整，可以交付 CargoWise 使用。表格将变为只读（需要时可以重新编辑）。" }),
      errors || warnings ? el("p", { class: "msg warn", text: `表格中还有 ${errors} 个错误、${warnings} 个警告。` }) : el("p", { class: "msg ok", text: "✓ 表格校验没有发现问题" }),
      pdfFailed.length ? el("p", { class: "msg warn", text: "PDF 校验未通过：" + pdfFailed.map((c) => c.name).join("、") }) : null);
    if (!(await ask({ title: "确认这份文档？", body, ok: "确认 Confirm", okClass: "ok" }))) return;
    try {
      const res = await api(`/api/documents/${docId}/confirm`, { method: "POST", json: { revision } });
      await reload();
      toast("已确认 / Ready");
      return res;
    } catch (e) { toast("确认失败：" + e.message, "err"); }
  }

  async function reopen() {
    if (!(await ask({ title: "重新编辑这份文档？", body: "文档会回到“编辑中”，需要修改完成后再次 Confirm。操作会记录在历史中。", ok: "重新编辑" }))) return;
    try {
      await api(`/api/documents/${docId}/reopen`, { method: "POST" });
      await reload();
      toast("已回到编辑中");
    } catch (e) { toast(e.message, "err"); }
  }

  async function preview(button) {
    if (!(await flush())) return;
    CIVPreview.open({
      title: doc.name,
      load: () => api(`/api/documents/${docId}/preview`),
      returnFocus: button,
      actions: () => [el("button", { type: "button", class: "btn primary", onclick: () => exportFile("csv") }, "导出 CSV")],
    });
  }

  // ───────────── history ─────────────
  const ACTIONS = {
    "document.created": (d) => `上传了 ${(d.files || []).join("、")}`,
    "conversion.succeeded": (d) => `转换完成：${d.lines} 行（客户 ${d.customer}）`,
    "conversion.failed": (d) => `转换失败：${d.error}`,
    "conversion.retried": (d) => `重新转换${d.customer ? `（客户 ${d.customer}）` : ""}`,
    "sheet.saved": (d) => `${d.reason === "manual" ? "保存" : `自动保存${d.saves > 1 ? ` ${d.saves} 次` : ""}`}：` +
      [d.cells && `修改 ${d.cells} 个单元格`, d.rows_added && `新增 ${d.rows_added} 行`, d.rows_deleted && `删除 ${d.rows_deleted} 行`].filter(Boolean).join("，") + ` · 第 ${d.revision} 版`,
    "document.exported": (d) => `导出 ${String(d.format).toUpperCase()}（第 ${d.revision} 版${d.final ? "，最终版" : ""}）`,
    "document.confirmed": (d) => `确认 Confirm（第 ${d.revision} 版${d.open_issues ? `，当时还有 ${d.open_issues} 个提示` : ""}）`,
    "document.reopened": () => "重新编辑",
  };
  const REASONS = { initial: "转换结果", manual: "手动保存", auto: "自动保存", confirm: "确认时" };

  async function history() {
    document.querySelectorAll(".drawer").forEach((d) => d.remove());
    const body = el("div", { class: "body" }, el("p", {}, el("span", { class: "spin" }), " 读取中…"));
    const close = el("button", { type: "button", class: "btn small", text: "关闭" });
    const drawer = el("aside", { class: "drawer", "aria-label": "历史记录" }, el("header", {}, el("h2", { text: "历史记录" }), close), body);
    close.onclick = () => drawer.remove();
    document.body.append(drawer);
    try {
      const [{ log }, { versions }] = await Promise.all([api(`/api/documents/${docId}/log`), api(`/api/documents/${docId}/versions`)]);
      body.replaceChildren(
        el("h3", { text: "操作记录" }),
        el("ol", {}, ...log.map((e) => el("li", {}, el("div", { text: (ACTIONS[e.action] || (() => e.action))(e.detail || {}) }),
          el("div", { class: "t", text: `${e.user ? e.user.name : "系统"} · ${fmtTime(e.at)}${e.detail && e.detail.until && e.detail.saves > 1 ? ` – ${fmtTime(e.detail.until, false)}` : ""}` })))),
        el("h3", { text: "保存的版本（快照）" }),
        el("ol", {}, ...versions.map((v) => el("li", {}, el("div", { text: `第 ${v.revision} 版 · ${REASONS[v.reason] || v.reason}` }),
          el("div", { class: "t", text: `${v.by || "系统"} · ${fmtTime(v.at)}` })))),
        el("h3", { text: "导出记录" }),
        detail.exports && detail.exports.length
          ? el("ol", {}, ...detail.exports.map((x) => el("li", {}, el("div", { text: `${x.filename}（第 ${x.revision} 版，${x.rows} 行${x.final ? "，最终版" : ""}）` }),
            el("div", { class: "t", text: `${x.exported_by ? x.exported_by.name : ""} · ${fmtTime(x.exported_at)}` }))))
          : el("p", { class: "hint", text: "还没有导出过" }));
    } catch (e) { body.replaceChildren(el("p", { class: "msg err", text: e.message })); }
  }

  // ───────────── context menu ─────────────
  function contextMenu(x, y) {
    const editing = doc.status === "editing";
    const rows = sheet.selectedRows().length;
    const run = (cmd) => () => { sheet.focus(); document.execCommand(cmd); };
    CIV.menu(x, y, [
      { label: "剪切", key: "Ctrl+X", disabled: !editing, run: run("cut") },
      { label: "复制", key: "Ctrl+C", run: run("copy") },
      { label: "粘贴", key: "Ctrl+V", disabled: !editing, run: async () => {
        sheet.focus();
        try { sheet.paste(await navigator.clipboard.readText()); } catch { toast("浏览器不允许从菜单读取剪贴板，请按 Ctrl+V 粘贴"); }
      } },
      "-",
      { label: "在上方插入行", disabled: !editing, run: () => { sheet.insertAtSelection("above"); sheet.focus(); } },
      { label: "在下方插入行", disabled: !editing, run: () => { sheet.insertAtSelection("below"); sheet.focus(); } },
      { label: `删除 ${rows} 行`, disabled: !editing || !rows, run: () => { sheet.deleteRows(sheet.selectedRows()); sheet.focus(); } },
      { label: "清除内容", key: "Delete", disabled: !editing, run: () => { sheet.clearSelection(); sheet.focus(); } },
      "-",
      { label: "撤销", key: "Ctrl+Z", disabled: !sheet.canUndo(), run: () => { sheet.undo(); sheet.focus(); } },
      { label: "重做", key: "Ctrl+Y", disabled: !sheet.canRedo(), run: () => { sheet.redo(); sheet.focus(); } },
    ]);
  }

  // ───────────── leaving the page ─────────────
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && (e.key === "s" || e.key === "S")) { e.preventDefault(); $("#save").click(); }
  });
  window.addEventListener("beforeunload", (e) => {
    if (sheet && (dirty() || saving)) { writeDraft(); e.preventDefault(); e.returnValue = ""; }
  });
  document.addEventListener("visibilitychange", () => { // tab hidden / window closing: save now (the draft covers the rest)
    if (document.visibilityState !== "hidden" || !sheet || !dirty() || doc.status !== "editing") return;
    writeDraft();
    sheet.commitEdit(null);
    save("auto", false, true);
  });
  window.addEventListener("online", () => { if (sheet && dirty()) save("auto"); });

  // for the browser console and automated tests
  window.CIVEditor = { get sheet() { return sheet; }, get revision() { return revision; }, get dirty() { return dirty(); }, save };

  (async () => {
    try {
      user = await CIV.currentUser();
      CIV.topbar(user);
    } catch { return; }
    load();
  })();
})();
