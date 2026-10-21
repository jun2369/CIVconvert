/* The dashboard: counts, upload (each invoice becomes a document) and the list of one's own documents. */
(() => {
  "use strict";
  const { $, el, api, fmtTime, fmtSize, statusBadges, toast, ask, download } = CIV;
  const { t } = CIVi18n;
  const files = [];
  const state = { status: "", q: "", from: "", to: "", page: 1, pageSize: 25 };
  let timer = null;
  let editing = 0; // an invoice number / customer being typed: the list is not reloaded under it
  let shown = []; // the documents on this page
  const picked = new Set(); // ticked to be deleted (ids, on this page)

  // ───────────── filters live in the URL, so refresh / back keep them ─────────────
  function readUrl() {
    const p = new URLSearchParams(location.search);
    state.status = ["editing", "confirmed", "attention"].includes(p.get("status")) ? p.get("status") : "";
    state.q = p.get("q") || "";
    state.from = p.get("from") || "";
    state.to = p.get("to") || "";
    state.page = Math.max(1, Number(p.get("page")) || 1);
  }
  function writeUrl() {
    const p = new URLSearchParams();
    for (const k of ["status", "q", "from", "to"]) if (state[k]) p.set(k, state[k]);
    if (state.page > 1) p.set("page", state.page);
    history.replaceState(null, "", p.toString() ? "?" + p : location.pathname);
  }
  function syncControls() {
    $("#status").value = state.status;
    $("#q").value = state.q;
    $("#from").value = state.from;
    $("#to").value = state.to;
  }
  const localDay = (value, endOfDay) => { // "2026-10-19" in the user's time zone -> ISO
    if (!value) return null;
    const [y, m, d] = value.split("-").map(Number);
    return new Date(y, m - 1, d + (endOfDay ? 1 : 0)).toISOString();
  };

  // ───────────── counts ─────────────
  const CARDS = [
    { key: "total", label: "card.total", cls: "", status: "" },
    { key: "editing", label: "card.editing", cls: "", status: "editing" },
    { key: "confirmed", label: "card.completed", cls: "ok", status: "confirmed" },
    { key: "attention", label: "card.attention", cls: "err", status: "attention" },
  ];

  async function loadStats() {
    const s = await api("/api/documents/stats");
    const plain = !state.from && !state.to && !state.q;
    $("#stats").replaceChildren(...CARDS.map((c) => {
      const active = state.status === c.status && (c.status || plain);
      const card = el("button", { type: "button", class: `stat ${c.cls}${active ? " on" : ""}`, title: t("card.tip") },
        el("span", { class: "n", text: String(s[c.key] ?? 0) }), el("span", { class: "l", text: t(c.label) }));
      card.onclick = () => {
        Object.assign(state, c.status ? { status: c.status, page: 1 } : { status: "", q: "", from: "", to: "", page: 1 });
        syncControls();
        refresh();
      };
      return card;
    }));
  }

  // ───────────── e-mails received today by Power Automate Cloud ─────────────
  async function loadIntake() {
    const midnight = new Date();
    midnight.setHours(0, 0, 0, 0);
    const line = $("#intake");
    let s;
    try {
      s = await api("/api/intake/today?since=" + encodeURIComponent(midnight.toISOString()));
    } catch { line.hidden = true; return; } // never in the way of the list
    line.hidden = !s.ever; // only for users whose mailbox is connected
    if (!s.ever) return;
    line.replaceChildren(el("span", { text: t("intake.today", { emails: s.emails, attachments: s.attachments, civ: s.civ }) }),
      s.no_civ ? el("span", { class: "warn", text: t("intake.no_civ", { n: s.no_civ }) }) : null,
      s.emails ? el("button", { type: "button", class: "link", onclick: () => mails(s.items) }, t("intake.details")) : null);
  }

  function mails(items) {
    const why = (a) => t("intake.code." + (a.code || (a.decision === "civ" ? "civ" : "not_civ")), { other: a.reason || "" });
    ask({
      title: t("intake.title"),
      body: el("div", { class: "mails" }, ...items.map((m) => el("section", {},
        el("div", { class: "when", text: [fmtTime(m.at), m.email.from].filter(Boolean).join(" · ") }),
        el("b", { text: m.email.subject || t("intake.no_subject") }),
        el("ul", {}, ...m.attachments.map((a) => el("li", { class: a.decision === "civ" ? "" : "skip", title: a.reason || "" },
          el("span", { text: `${a.name} — ${why(a)}` }),
          a.document_id && a.document_deleted ? el("span", { class: "skip", text: " → " + t("intake.deleted") }) : null,
          a.document_id && !a.document_deleted ? el("span", {}, " → ", el("a", { href: `/documents/${a.document_id}` }, a.document_name || t("intake.open"))) : null)))))),
      ok: t("intake.close"),
      cancel: null,
    });
  }

  // ───────────── list ─────────────
  async function loadList() {
    const p = new URLSearchParams({ page: state.page, page_size: state.pageSize });
    if (state.status) p.set("status", state.status);
    if (state.q.trim()) p.set("q", state.q.trim());
    if (state.from) p.set("created_from", localDay(state.from, false));
    if (state.to) p.set("created_to", localDay(state.to, true));
    const data = await api("/api/documents?" + p);
    $("#listError").replaceChildren();
    shown = data.items;
    for (const id of [...picked]) if (!shown.some((d) => d.id === id)) picked.delete(id); // ticks are for this page only
    const tbody = $("#rows");
    if (!data.items.length) {
      tbody.replaceChildren(el("tr", {}, el("td", { colspan: "12", class: "empty" },
        t(state.status || state.q || state.from || state.to ? "list.empty_filtered" : "list.empty"))));
    } else {
      tbody.replaceChildren(...data.items.map(row));
    }
    syncPicks();
    const pages = Math.max(1, Math.ceil(data.total / data.page_size));
    $("#count").textContent = t("list.count", { total: data.total, page: data.page, pages });
    $("#prev").disabled = data.page <= 1;
    $("#next").disabled = data.page >= pages;
    clearTimeout(timer);
    if (data.items.some((d) => d.status === "processing")) timer = setTimeout(refresh, 2000);
  }

  function row(d) {
    const open = () => { location.href = `/documents/${d.id}`; };
    const stop = (fn) => (e) => { e.stopPropagation(); fn(e); };
    const ready = d.status === "editing" || d.status === "confirmed";
    const acts = el("div", { class: "acts" },
      el("a", { class: "btn small", href: `/documents/${d.id}`, onclick: (e) => e.stopPropagation() }, t(d.status === "failed" ? "row.fix" : "row.open")),
      el("button", { type: "button", class: "btn small", disabled: !ready, onclick: stop((e) => preview(d, e.currentTarget)) }, t("row.preview")),
      el("button", { type: "button", class: "btn small", disabled: !ready, onclick: stop(() => exportCsv(d)) }, t("row.export")),
      d.status === "editing" ? el("button", { type: "button", class: "btn small", onclick: stop(() => confirmDoc(d)) }, t("row.confirm")) : null,
    );
    const name = el("td", { class: "name" }, el("b", { text: d.name }),
      el("span", { class: "hint", text: [d.invoice_no && t("row.invoice", { no: d.invoice_no }), d.files.length > 1 && t("row.pdfs", { n: d.files.length })].filter(Boolean).join(" · ") }));
    if (d.status === "failed" && d.error) name.append(el("span", { class: "err-line", text: d.error }));
    const auto = d.customer_id === "auto";
    const tick = el("input", { type: "checkbox", checked: picked.has(d.id), "aria-label": t("row.select", { name: d.name }),
      onchange: (e) => { if (e.target.checked) picked.add(d.id); else picked.delete(d.id); syncPicks(); } });
    return el("tr", { onclick: open, title: t("row.open_tip"), "data-id": d.id, class: picked.has(d.id) ? "picked" : "" },
      el("td", { class: "pick", title: "", onclick: (e) => e.stopPropagation() }, tick),
      name,
      detail(d, "invoice_no", { shown: d.invoice_no, start: d.invoice_no }, "row.edit_invoice", 100),
      el("td", {}, ...statusBadges(d)),
      el("td", { class: "num", text: ready ? String(d.line_count) : "–" }),
      detail(d, "customer_name", {
        shown: d.customer_name || (auto ? t("list.auto") : d.customer_id), // empty: the detected customer
        start: d.customer_name || (auto ? "" : d.customer_id),
        placeholder: auto ? t("list.auto") : d.customer_id,
      }, "row.edit_customer", 255),
      el("td", {}, d.source === "auto"
        ? el("span", { class: "badge auto", title: d.email ? t("src.auto_tip", { subject: d.email.subject || "–", from: d.email.from || "–",
          time: fmtTime(d.email.received_at) }) : t("src.auto_tip_short") }, t("src.auto"))
        : el("span", { class: "src-manual", text: t("src.manual") })),
      el("td", { text: d.source === "auto" ? "Power Automate" : d.created_by ? d.created_by.name : "" }),
      el("td", { text: fmtTime(d.created_at) }),
      el("td", { text: fmtTime(d.updated_at) }),
      el("td", {}, d.last_exported_at
        ? el("span", { class: "badge ok", title: t("row.export_count", { n: d.export_count }) }, "✓ ", fmtTime(d.last_exported_at))
        : el("span", { class: "hint", text: t("row.not_exported") })),
      el("td", {}, acts),
    );
  }

  /** A cell the user can change (invoice number, customer): click, type, Enter or click elsewhere saves, Esc cancels.
   *  shown: the text in the cell; start: the text to edit; placeholder: what an empty value means. */
  function detail(d, field, { shown, start, placeholder }, tip, max) {
    const td = el("td");
    if (d.status !== "editing" && d.status !== "confirmed") { td.textContent = shown || "–"; return td; }
    const view = () => td.replaceChildren(el("button", { type: "button", class: "cell-edit", title: t(tip), "aria-label": t(tip),
      onclick: (e) => { e.stopPropagation(); edit(); } }, el("span", { text: shown || "–" }), el("span", { class: "pen", "aria-hidden": "true", text: "✎" })));
    const edit = () => {
      const before = start || "";
      const input = el("input", { type: "text", class: "cell-input", value: before, maxlength: String(max), placeholder, "aria-label": t(tip) });
      let done = false;
      const finish = async (save) => {
        if (done) return;
        done = true;
        editing--;
        const value = input.value.trim();
        if (!save || value === before) { view(); return; }
        input.disabled = true;
        try {
          const { document } = await api(`/api/documents/${d.id}`, { method: "PATCH", json: { [field]: value || null } });
          td.closest("tr").replaceWith(row(document)); // the new value, "Last modified" – the row stays where it is
          toast(t("toast.detail_saved"));
        } catch (e) {
          toast(t("toast.detail_failed", { error: e.message }), "err");
          view();
        }
      };
      input.onclick = (e) => e.stopPropagation();
      input.onkeydown = (e) => {
        if (e.key === "Enter") { e.preventDefault(); finish(true); }
        else if (e.key === "Escape") { e.preventDefault(); finish(false); }
      };
      input.onblur = () => finish(true);
      editing++;
      td.replaceChildren(input);
      input.focus();
      input.select();
    };
    view();
    return td;
  }

  // ───────────── ticking documents and deleting them ─────────────
  function syncPicks() {
    for (const tr of document.querySelectorAll("#rows tr[data-id]")) {
      const on = picked.has(tr.dataset.id);
      tr.classList.toggle("picked", on);
      const box = tr.querySelector(".pick input");
      if (box) box.checked = on;
    }
    const n = picked.size;
    $("#delSel").disabled = n === 0;
    $("#delSel").textContent = n ? t("del.button_n", { n }) : t("del.button");
    $("#pickAll").checked = n > 0 && n === shown.length;
    $("#pickAll").indeterminate = n > 0 && n < shown.length;
  }

  async function deleteSelected() {
    const docs = shown.filter((d) => picked.has(d.id));
    if (!docs.length) return;
    const ok = await ask({
      title: t("del.title", { n: docs.length }),
      body: el("div", {}, el("p", { class: "msg err", text: t("del.warn") }),
        el("ul", { class: "del-list" }, ...docs.slice(0, 10).map((d) => el("li", { text: d.invoice_no ? `${d.name} (${d.invoice_no})` : d.name })),
          docs.length > 10 ? el("li", { class: "hint", text: t("del.more", { n: docs.length - 10 }) }) : null)),
      ok: t("del.ok"),
      danger: true,
    });
    if (!ok) return;
    try {
      const r = await api("/api/documents/delete", { method: "POST", json: { ids: docs.map((d) => d.id) } });
      picked.clear();
      toast(t("toast.deleted", { n: r.deleted }));
    } catch (e) {
      toast(t("toast.delete_failed", { error: e.message }), "err");
    }
    refresh();
  }

  async function exportCsv(d) {
    try {
      await download(`/api/documents/${d.id}/export?format=csv`, "POST", `${d.name}.csv`);
      toast(t("toast.exported"));
      refresh();
    } catch (e) { toast(t("toast.export_failed", { error: e.message }), "err"); }
  }

  async function confirmDoc(d) {
    const ok = await ask({
      title: t("confirm.title"),
      body: el("div", {}, el("p", { text: t("confirm.body_list", { name: d.name }) }),
        d.needs_review ? el("p", { class: "msg warn", text: t("confirm.review_warn") }) : null),
      ok: t("confirm.ok"), okClass: "ok",
    });
    if (!ok) return;
    try {
      await api(`/api/documents/${d.id}/confirm`, { method: "POST", json: {} });
      toast(t("toast.completed"));
      refresh();
    } catch (e) { toast(t("toast.confirm_failed", { error: e.message }), "err"); }
  }

  function preview(d, button) {
    CIVPreview.open({
      title: d.name,
      load: () => api(`/api/documents/${d.id}/preview`),
      returnFocus: button,
      actions: () => [
        el("a", { class: "btn", href: `/documents/${d.id}` }, t("preview.open_edit")),
        el("button", { type: "button", class: "btn primary", onclick: () => exportCsv(d) }, t("row.export")),
      ],
    });
  }

  async function refresh() {
    writeUrl();
    if (editing) { // not under a cell being typed in: later
      clearTimeout(timer);
      timer = setTimeout(refresh, 2000);
      return;
    }
    try {
      await Promise.all([loadStats(), loadList(), loadIntake()]);
    } catch (e) {
      $("#listError").replaceChildren(el("div", { class: "msg err", text: t("list.load_failed", { error: e.message }) }));
    }
  }

  // ───────────── upload ─────────────
  function renderFiles() {
    $("#filelist").replaceChildren(...files.map((f, i) => el("li", {}, el("span", { text: f.name }),
      el("span", { class: "hint" }, fmtSize(f.size) + " ", el("button", { type: "button", class: "link", onclick: () => { files.splice(i, 1); renderFiles(); } }, t("upload.remove"))))));
    $("#go").disabled = files.length === 0;
  }
  function addFiles(list) {
    if (!list.length) return;
    if (list.length > 1) { toast(t("upload.one_only"), "err"); return; } // one invoice PDF per upload
    const f = list[0];
    if (!/\.(pdf|xlsx|xlsm|xls|png|jpe?g|tiff?|bmp|gif|webp|heic|heif|avif)$/i.test(f.name)) { toast(t("upload.not_pdf", { name: f.name }), "err"); return; }
    files.splice(0, files.length, f); // a new choice replaces the previous one
    renderFiles();
  }
  async function upload() {
    const fd = new FormData();
    for (const f of files) fd.append("files", f, f.name); // the customer is always detected
    $("#go").disabled = true;
    $("#upStatus").replaceChildren(el("span", { class: "spin" }), " " + t("upload.uploading"));
    try {
      const { documents } = await api("/api/documents", { method: "POST", form: fd });
      files.length = 0;
      renderFiles();
      $("#upStatus").textContent = "";
      location.href = `/documents/${documents[0].id}`;
    } catch (e) {
      $("#upStatus").textContent = "";
      toast(t("upload.failed", { error: e.message }), "err");
      renderFiles();
    }
  }

  // ───────────── wiring ─────────────
  $("#pick").onclick = () => $("#file").click();
  $("#file").onchange = (e) => { addFiles(e.target.files); e.target.value = ""; };
  const drop = $("#drop");
  drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("over"));
  drop.addEventListener("drop", (e) => { e.preventDefault(); drop.classList.remove("over"); addFiles(e.dataTransfer.files); });
  $("#go").onclick = upload;
  $("#delSel").onclick = deleteSelected;
  $("#pickAll").onchange = (e) => {
    if (e.target.checked) for (const d of shown) picked.add(d.id);
    else picked.clear();
    syncPicks();
  };
  let typing;
  $("#q").addEventListener("input", (e) => { clearTimeout(typing); typing = setTimeout(() => { state.q = e.target.value; state.page = 1; refresh(); }, 300); });
  $("#status").onchange = (e) => { state.status = e.target.value; state.page = 1; refresh(); };
  $("#from").onchange = (e) => { state.from = e.target.value; state.page = 1; refresh(); };
  $("#to").onchange = (e) => { state.to = e.target.value; state.page = 1; refresh(); };
  $("#reset").onclick = () => { Object.assign(state, { status: "", q: "", from: "", to: "", page: 1 }); syncControls(); refresh(); };
  $("#prev").onclick = () => { state.page--; refresh(); };
  $("#next").onclick = () => { state.page++; refresh(); };
  window.addEventListener("focus", () => refresh());
  setInterval(() => { if (!document.hidden) refresh(); }, 60000); // documents arriving by e-mail show up by themselves

  (async () => {
    CIVi18n.apply();
    document.title = t("dash.page_title");
    readUrl();
    syncControls();
    try {
      const user = await CIV.currentUser();
      CIV.topbar(user);
      const hour = new Date().getHours();
      $("#greeting").textContent = t(hour < 12 ? "dash.morning" : hour < 18 ? "dash.afternoon" : "dash.evening", { name: user.name });
      const info = await api("/api/info");
      if (info.retention_months) $("#retention").textContent = t("list.retention", { months: info.retention_months });
    } catch (e) {
      if (e.status !== 401) toast(e.message, "err");
      return;
    }
    refresh();
  })();
})();
