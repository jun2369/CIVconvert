/* The dashboard: counts, upload (each invoice becomes a document) and the list of one's own documents. */
(() => {
  "use strict";
  const { $, el, api, fmtTime, fmtSize, statusBadges, toast, ask, download } = CIV;
  const { t } = CIVi18n;
  const files = [];
  const state = { status: "", q: "", from: "", to: "", page: 1, pageSize: 25 };
  let customers = [];
  let timer = null;

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

  // ───────────── list ─────────────
  async function loadList() {
    const p = new URLSearchParams({ page: state.page, page_size: state.pageSize });
    if (state.status) p.set("status", state.status);
    if (state.q.trim()) p.set("q", state.q.trim());
    if (state.from) p.set("created_from", localDay(state.from, false));
    if (state.to) p.set("created_to", localDay(state.to, true));
    const data = await api("/api/documents?" + p);
    $("#listError").replaceChildren();
    const tbody = $("#rows");
    if (!data.items.length) {
      tbody.replaceChildren(el("tr", {}, el("td", { colspan: "10", class: "empty" },
        t(state.status || state.q || state.from || state.to ? "list.empty_filtered" : "list.empty"))));
    } else {
      tbody.replaceChildren(...data.items.map(row));
    }
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
    return el("tr", { onclick: open, title: t("row.open_tip") },
      name,
      el("td", { text: d.invoice_no || "–" }),
      el("td", {}, ...statusBadges(d)),
      el("td", { class: "num", text: ready ? String(d.line_count) : "–" }),
      el("td", { text: d.customer_id || "–" }),
      el("td", { text: d.created_by ? d.created_by.name : "" }),
      el("td", { text: fmtTime(d.created_at) }),
      el("td", { text: fmtTime(d.updated_at) }),
      el("td", {}, d.last_exported_at
        ? el("span", { class: "badge ok", title: t("row.export_count", { n: d.export_count }) }, "✓ ", fmtTime(d.last_exported_at))
        : el("span", { class: "hint", text: t("row.not_exported") })),
      el("td", {}, acts),
    );
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
    try {
      await Promise.all([loadStats(), loadList()]);
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
    if (!/\.pdf$/i.test(f.name)) { toast(t("upload.not_pdf", { name: f.name }), "err"); return; }
    files.splice(0, files.length, f); // a new choice replaces the previous one
    renderFiles();
  }
  async function upload() {
    const fd = new FormData();
    for (const f of files) fd.append("files", f, f.name);
    if ($("#customer").value) fd.append("customer", $("#customer").value);
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
  $("#customer").onchange = (e) => {
    try { localStorage.setItem("civconvert.customer", e.target.value); } catch { /* private mode */ }
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
      customers = await api("/api/customers");
      const sel = $("#customer");
      for (const c of customers) sel.append(el("option", { value: c.id, text: `${c.name} (${c.id})` }));
      try { sel.value = localStorage.getItem("civconvert.customer") || ""; } catch { /* private mode */ }
      if (sel.selectedIndex < 0) sel.value = "";
      const info = await api("/api/info");
      if (info.retention_months) $("#retention").textContent = t("list.retention", { months: info.retention_months });
    } catch (e) {
      if (e.status !== 401) toast(e.message, "err");
      return;
    }
    refresh();
  })();
})();
