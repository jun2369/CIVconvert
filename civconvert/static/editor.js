/* The document page: the online sheet, autosave, local backup, export, Confirm, history.

   Nothing typed may get lost:
   - every change is written to localStorage at once (a draft) and autosaved ~1.5 s later
   - the server only accepts a save based on the latest revision (two tabs / two people
     cannot silently overwrite each other); a conflict asks the user what to do
   - offline / server errors: the draft stays and the save is retried; leaving the page with
     unsaved changes asks first; a draft found when the page opens can be restored */
(() => {
  "use strict";
  const { $, el, api, fmtTime, fmtDate, toast, ask, download, statusBadges } = CIV;
  const { t } = CIVi18n;
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
      center(el("div", { class: "panel" }, el("h2", { text: t(e.status === 404 ? "ed.not_found" : "ed.open_failed") }), el("p", { text: e.message }), el("a", { class: "btn", href: "/" }, t("ed.back_dashboard"))));
      return;
    }
    doc = detail.document;
    header();
    if (doc.status === "processing") {
      center(el("div", {}, el("p", {}, el("span", { class: "spin" }), " " + t("ed.processing")), el("p", { class: "hint", text: t("ed.processing_hint") })));
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
    document.title = t("ed.page_title", { name: doc.name });
    $("#title").textContent = doc.name;
    $("#badges").replaceChildren(...statusBadges(doc));
    const ex = detail.extraction || {};
    $("#meta").textContent = [doc.invoice_no && t("ed.meta_invoice", { no: doc.invoice_no }), doc.invoice_date,
      doc.customer_name || (ex.customer && (ex.customer.id === "auto" ? t("list.auto") : ex.customer.name)),
      doc.source === "auto" ? t("ed.meta_auto", { from: (doc.email && doc.email.from) || "–", time: fmtTime(doc.created_at) })
        : doc.created_by && t("ed.meta_uploaded", { name: doc.created_by.name, time: fmtTime(doc.created_at) }),
      doc.last_exported_at && t("ed.meta_exported", { time: fmtTime(doc.last_exported_at) }),
      doc.expires_at && t("ed.meta_expires", { date: fmtDate(doc.expires_at) })].filter(Boolean).join(" · ");
  }

  async function failed() {
    const sel = el("select", { "aria-label": t("upload.customer") }, el("option", { value: "" }, t("ed.auto_customer")));
    try { for (const c of await api("/api/customers")) sel.append(el("option", { value: c.id }, `${c.name} (${c.id})`)); } catch { /* keep auto */ }
    const retry = el("button", { type: "button", class: "btn primary", text: t("ed.retry") });
    retry.onclick = async () => {
      retry.disabled = true;
      try {
        await api(`/api/documents/${docId}/retry`, { method: "POST", json: { customer: sel.value || null } });
        load();
      } catch (e) { toast(e.message, "err"); retry.disabled = false; }
    };
    center(el("div", { class: "panel" },
      el("h2", { text: t("ed.failed_title") }),
      el("p", { class: "msg err", text: doc.error || t("ed.unknown_error") }),
      el("p", { class: "hint", text: t("ed.failed_hint") }),
      el("div", { style: "display:flex;gap:8px;flex-wrap:wrap;align-items:center" }, sel, retry,
        ...doc.files.map((f) => el("a", { class: "btn", href: `/api/documents/${docId}/files/${f.id}`, target: "_blank", rel: "noopener" }, t("ed.view_file", { name: f.filename }))))));
  }

  function open() {
    $("#center").hidden = true;
    for (const id of ["#toolbar", "#fbar", "#gridwrap", "#statusbar"]) $(id).hidden = false;
    const tpl = detail.template;
    const converted = (detail.extraction && detail.extraction.converted) || [];
    if (!sheet) {
      sheet = new CIVSheet($("#grid"), {
        columns: tpl.columns, headerRow: tpl.header_row, roles: tpl.roles,
        readOnly: doc.status !== "editing",
        original: (src, letter) => (converted[src - 1] ? converted[src - 1][letter] : undefined),
        onChange: changed,
        onSelect: selectedCell,
        onEditInput: (text) => { $("#formula").value = text; },
        formulaBar: $("#formula"),
        onContextMenu: contextMenu,
        onReadOnly: () => toast(t("ed.readonly")),
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
      const by = doc.confirmed_by ? t("ed.completed_by", { name: doc.confirmed_by.name, time: fmtTime(doc.confirmed_at) }) : "";
      out.push(banner("ok", t("ed.completed_banner", { by }), [
        el("button", { type: "button", class: "btn small primary", onclick: () => exportFile("csv") }, t("ed.export_final")),
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
    const newer = d.base !== revision ? t("ed.draft_newer") : "";
    banners(banner("warn", t("ed.draft_found", { time: when }) + newer, [
      el("button", { type: "button", class: "btn small primary", onclick: () => {
        sheet.setRows(d.rows);
        banners();
        changed();
        toast(t("ed.restored"));
      } }, t("ed.restore")),
      el("button", { type: "button", class: "btn small", onclick: () => { clearDraft(); banners(); } }, t("ed.discard")),
    ]));
  }

  function saveState(kind, text) {
    const box = $("#savestate");
    box.className = "savestate " + kind;
    const label = {
      saved: () => (lastSavedAt ? t("save.saved", { time: fmtTime(lastSavedAt, false) }) : t("save.saved_plain")),
      dirty: () => t("save.dirty"),
      saving: () => t("save.saving"),
      offline: () => t("save.offline"),
      error: () => t("save.error", { error: text || "" }),
      "": () => (lastSavedAt ? t("save.modified", { time: fmtTime(lastSavedAt) }) : ""),
    }[kind]();
    box.replaceChildren(kind === "saving" ? el("span", { class: "spin" }) : "", label);
    if (kind === "error") box.append(" ", el("button", { type: "button", class: "link", onclick: () => save("manual") }, t("save.retry")));
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
        el("div", { class: "dlg-body" }, el("h3", { text: t("conflict.title") }),
          el("p", { text: t("conflict.body", { who: info.updated_by || t("conflict.someone"), time: fmtTime(info.updated_at), rev: info.revision }) }),
          el("p", { class: "hint", text: t("conflict.hint") })),
        el("div", { class: "dlg-actions" },
          el("button", { type: "button", class: "btn", onclick: () => pick("later") }, t("conflict.later")),
          el("button", { type: "button", class: "btn danger", onclick: () => pick("overwrite") }, t("conflict.overwrite")),
          el("button", { type: "button", class: "btn primary", onclick: () => pick("reload") }, t("conflict.reload"))),
      );
      dlg.addEventListener("cancel", (e) => { e.preventDefault(); pick("later"); });
      document.body.append(dlg);
      dlg.showModal();
    });
    if (choice === "overwrite") { saving = null; return save("manual", true); }
    if (choice === "reload") { savedChanges = changes; await reload(); return false; }
    saveState("error", t("save.conflict_short"));
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
    const text = issueRows.length ? [errors && t("issues.errors", { n: errors }), warnings && t("issues.warnings", { n: warnings })].filter(Boolean).join(" · ") : t("issues.none");
    const prev = el("button", { type: "button", class: "btn small", title: t("issues.prev"), disabled: !issueRows.length, onclick: () => jump(-1) }, "‹");
    const next = el("button", { type: "button", class: "btn small", title: t("issues.next"), disabled: !issueRows.length, onclick: () => jump(1) }, "›");
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
      parts.push(t("sb.selected", { n: info.cells }), t("sb.filled", { n: info.count }));
      if (info.nums) parts.push(t("sb.sum", { v: CIVFormula.formatNumber(info.sum) }), t("sb.avg", { v: CIVFormula.formatNumber(info.sum / info.nums) }));
    }
    parts.push(t("sb.rows", { n: sheet.rowCount }), t("sb.template", { id: detail.template.id, n: detail.template.columns.length }));
    const bits = [el("span", { text: parts.join(" · ") })];
    const flag = info.flag || (info.rowFlag && !info.flag ? info.rowFlag : null);
    if (flag) bits.push(el("span", { class: "msg-cell " + flag.level, text: (flag.level === "error" ? "✗ " : "⚠ ") + flag.msgs.join(t("common.msg_sep")) }));
    const row = info.row;
    const items = detail.extraction && detail.extraction.items;
    if (row && row.src && items && items[row.src - 1]) {
      const it = items[row.src - 1];
      const fields = detail.extraction.fields || [];
      bits.push(el("span", { title: t("sb.pdf_row_tip") }, t("sb.pdf_row", { n: row.src, values: fields.map((f) => `${f.label} ${it[f.name] || "–"}`).join(" · ") })));
    } else if (row && !row.src) bits.push(el("span", { text: t("sb.manual_row") }));
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
    fileType();
    $("#save").textContent = t("tb.save") + (dirty() ? " ●" : "");
  }

  $("#undo").onclick = () => { sheet.undo(); sheet.focus(); };
  $("#redo").onclick = () => { sheet.redo(); sheet.focus(); };
  $("#insert").onclick = (e) => {
    const r = e.currentTarget.getBoundingClientRect();
    CIV.menu(r.left, r.bottom + 4, [
      { label: t("menu.insert_above"), run: () => { sheet.insertAtSelection("above"); sheet.focus(); } },
      { label: t("menu.insert_below"), run: () => { sheet.insertAtSelection("below"); sheet.focus(); } },
      { label: t("menu.append"), run: () => { sheet.insertRows(sheet.rowCount, 1); sheet.focus(); } },
    ]);
  };
  $("#delrows").onclick = () => { sheet.deleteRows(sheet.selectedRows()); sheet.focus(); };
  $("#clear").onclick = () => { sheet.clearSelection(); sheet.focus(); };
  $("#save").onclick = async () => {
    const ok = await save("manual");
    if (ok) toast(t("toast.saved", { rev: revision }));
  };
  $("#csv").onclick = () => exportFile("csv");
  $("#xlsx").onclick = () => exportFile("xlsx");
  $("#confirm").onclick = confirmDoc;
  $("#reopen").onclick = reopen;
  $("#preview").onclick = (e) => preview(e.currentTarget);
  $("#history").onclick = history;
  const fileType = () => { // the original file's type on its button: PDF, XLSX, PNG …
    const m = /\.(pdf|xlsx|xlsm|xls|png|jpe?g|tiff?|bmp|gif|webp|heic|heif|avif)$/i.exec((doc && doc.files[0] && doc.files[0].filename) || "");
    $("#pdf").textContent = m ? m[1].toUpperCase() : "PDF";
  };
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
    if (!ok || dirty()) { toast(t("toast.unsaved_left"), "err"); return false; }
    return true;
  }

  async function exportFile(fmt) {
    if (!(await flush())) return;
    try {
      await download(`/api/documents/${docId}/export?format=${fmt}&revision=${revision}`, "POST", `${doc.name}.${fmt}`);
      toast(fmt === "csv" ? t("toast.exported_csv", { rev: revision, final: doc.status === "confirmed" ? t("toast.final") : "" }) : t("toast.exported_xlsx"));
      const fresh = await api(`/api/documents/${docId}`);
      doc.last_exported_at = fresh.document.last_exported_at;
      doc.export_count = fresh.document.export_count;
      header();
    } catch (e) { toast(t("toast.export_failed", { error: e.message }), "err"); }
  }

  async function confirmDoc() {
    if (!(await flush())) return;
    const errors = flags.filter((f) => f.level === "error").length, warnings = flags.length - errors;
    const pdfFailed = ((detail.extraction && detail.extraction.checks) || []).filter((c) => !c.ok);
    const body = el("div", {},
      el("p", { text: t("confirm.body_editor") }),
      errors || warnings ? el("p", { class: "msg warn", text: t("confirm.issues", { e: errors, w: warnings }) }) : el("p", { class: "msg ok", text: t("confirm.clean") }),
      pdfFailed.length ? el("p", { class: "msg warn", text: t("confirm.pdf_failed", { names: pdfFailed.map((c) => CIVi18n.checkName(c.name)).join(t("list.sep")) }) }) : null);
    if (!(await ask({ title: t("confirm.title"), body, ok: t("confirm.ok"), okClass: "ok" }))) return;
    try {
      const res = await api(`/api/documents/${docId}/confirm`, { method: "POST", json: { revision } });
      await reload();
      toast(t("toast.completed"));
      return res;
    } catch (e) { toast(t("toast.confirm_failed", { error: e.message }), "err"); }
  }

  async function reopen() {
    if (!(await ask({ title: t("reopen.title"), body: t("reopen.body"), ok: t("reopen.ok") }))) return;
    try {
      await api(`/api/documents/${docId}/reopen`, { method: "POST" });
      await reload();
      toast(t("toast.reopened"));
    } catch (e) { toast(e.message, "err"); }
  }

  async function preview(button) {
    if (!(await flush())) return;
    CIVPreview.open({
      title: doc.name,
      load: () => api(`/api/documents/${docId}/preview`),
      returnFocus: button,
      actions: () => [el("button", { type: "button", class: "btn primary", onclick: () => exportFile("csv") }, t("tb.csv"))],
    });
  }

  // ───────────── history ─────────────
  const finalNote = (d) => (d.final ? t("toast.final") : "");
  const ACTIONS = {
    "document.created": (d) => d.source === "auto"
      ? t("act.document.received", { files: (d.files || []).join(t("list.sep")), from: d.email_from || "–", subject: d.email_subject || "–" })
      : t("act.document.created", { files: (d.files || []).join(t("list.sep")) }),
    "conversion.succeeded": (d) => t("act.conversion.succeeded", { lines: d.lines, customer: d.customer }),
    "conversion.failed": (d) => t("act.conversion.failed", { error: d.error }),
    "conversion.retried": (d) => t("act.conversion.retried", { customer: d.customer ? t("act.customer", { customer: d.customer }) : "" }),
    "sheet.saved": (d) => t("act.sheet.saved", {
      kind: d.reason === "manual" ? t("act.saved") : t("act.autosaved") + (d.saves > 1 ? t("act.times", { n: d.saves }) : ""),
      changes: [d.cells && t("act.cells", { n: d.cells }), d.rows_added && t("act.rows_added", { n: d.rows_added }),
        d.rows_deleted && t("act.rows_deleted", { n: d.rows_deleted })].filter(Boolean).join(t("list.sep")),
      rev: d.revision,
    }),
    "document.exported": (d) => t("act.document.exported", { fmt: String(d.format).toUpperCase(), rev: d.revision, final: finalNote(d) }),
    "document.confirmed": (d) => t("act.document.confirmed", { rev: d.revision, issues: d.open_issues ? t("act.open_issues", { n: d.open_issues }) : "" }),
    "document.reopened": () => t("act.document.reopened"),
    "document.edited": (d) => t("act.document.edited", {
      changes: ["invoice_no", "customer_name"].filter((f) => d[f])
        .map((f) => t("act.field." + f, { from: d[f].from || "–", to: d[f].to || "–" })).join(t("list.sep")),
    }),
  };
  const reasonLabel = (r) => (CIVi18n.has("hist.reason." + r) ? t("hist.reason." + r) : r);

  async function history() {
    document.querySelectorAll(".drawer").forEach((d) => d.remove());
    const body = el("div", { class: "body" }, el("p", {}, el("span", { class: "spin" }), " " + t("hist.loading")));
    const close = el("button", { type: "button", class: "btn small", text: t("hist.close") });
    const drawer = el("aside", { class: "drawer", "aria-label": t("hist.title") }, el("header", {}, el("h2", { text: t("hist.title") }), close), body);
    close.onclick = () => drawer.remove();
    document.body.append(drawer);
    try {
      const [{ log }, { versions }] = await Promise.all([api(`/api/documents/${docId}/log`), api(`/api/documents/${docId}/versions`)]);
      body.replaceChildren(
        el("h3", { text: t("hist.log") }),
        el("ol", {}, ...log.map((e) => el("li", {}, el("div", { text: (ACTIONS[e.action] || (() => e.action))(e.detail || {}) }),
          el("div", { class: "t", text: `${e.user ? e.user.name : t("hist.system")} · ${fmtTime(e.at)}${e.detail && e.detail.until && e.detail.saves > 1 ? ` – ${fmtTime(e.detail.until, false)}` : ""}` })))),
        el("h3", { text: t("hist.versions") }),
        el("ol", {}, ...versions.map((v) => el("li", {}, el("div", { text: t("hist.version", { rev: v.revision, reason: reasonLabel(v.reason) }) }),
          el("div", { class: "t", text: `${v.by || t("hist.system")} · ${fmtTime(v.at)}` })))),
        el("h3", { text: t("hist.exports") }),
        detail.exports && detail.exports.length
          ? el("ol", {}, ...detail.exports.map((x) => el("li", {}, el("div", { text: t("hist.export", { file: x.filename, rev: x.revision, rows: x.rows, final: finalNote(x) }) }),
            el("div", { class: "t", text: `${x.exported_by ? x.exported_by.name : ""} · ${fmtTime(x.exported_at)}` }))))
          : el("p", { class: "hint", text: t("hist.no_exports") }));
    } catch (e) { body.replaceChildren(el("p", { class: "msg err", text: e.message })); }
  }

  // ───────────── context menu ─────────────
  function contextMenu(x, y) {
    const editing = doc.status === "editing";
    const rows = sheet.selectedRows().length;
    const run = (cmd) => () => { sheet.focus(); document.execCommand(cmd); };
    CIV.menu(x, y, [
      { label: t("menu.cut"), key: "Ctrl+X", disabled: !editing, run: run("cut") },
      { label: t("menu.copy"), key: "Ctrl+C", run: run("copy") },
      { label: t("menu.paste"), key: "Ctrl+V", disabled: !editing, run: async () => {
        sheet.focus();
        try { sheet.paste(await navigator.clipboard.readText()); } catch { toast(t("menu.paste_blocked")); }
      } },
      "-",
      { label: t("menu.insert_above"), disabled: !editing, run: () => { sheet.insertAtSelection("above"); sheet.focus(); } },
      { label: t("menu.insert_below"), disabled: !editing, run: () => { sheet.insertAtSelection("below"); sheet.focus(); } },
      { label: t("menu.delete_rows", { n: rows }), disabled: !editing || !rows, run: () => { sheet.deleteRows(sheet.selectedRows()); sheet.focus(); } },
      { label: t("menu.clear"), key: "Delete", disabled: !editing, run: () => { sheet.clearSelection(); sheet.focus(); } },
      "-",
      { label: t("menu.undo"), key: "Ctrl+Z", disabled: !sheet.canUndo(), run: () => { sheet.undo(); sheet.focus(); } },
      { label: t("menu.redo"), key: "Ctrl+Y", disabled: !sheet.canRedo(), run: () => { sheet.redo(); sheet.focus(); } },
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
  // the language switch reloads the page: save first (if that fails, the draft and the leave-page question remain)
  CIVi18n.beforeSwitch = async () => {
    if (!sheet || !doc || doc.status !== "editing") return;
    sheet.commitEdit(null);
    if (dirty() || saving) { writeDraft(); await save("auto"); }
  };

  // for the browser console and automated tests
  window.CIVEditor = { get sheet() { return sheet; }, get revision() { return revision; }, get dirty() { return dirty(); }, save };

  (async () => {
    CIVi18n.apply();
    try {
      user = await CIV.currentUser();
      CIV.topbar(user);
    } catch { return; }
    load();
  })();
})();
