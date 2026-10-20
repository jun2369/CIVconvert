/* The dashboard: counts, upload (each invoice becomes a document) and the document list. */
(() => {
  "use strict";
  const { $, el, api, fmtTime, fmtSize, statusBadges, toast, ask, download } = CIV;
  const files = [];
  const state = { scope: "mine", status: "", q: "", from: "", to: "", page: 1, pageSize: 25 };
  let customers = [];
  let timer = null;

  // ───────────── filters live in the URL, so refresh / back keep them ─────────────
  function readUrl() {
    const p = new URLSearchParams(location.search);
    state.scope = p.get("scope") === "all" ? "all" : "mine";
    state.status = p.get("status") || "";
    state.q = p.get("q") || "";
    state.from = p.get("from") || "";
    state.to = p.get("to") || "";
    state.page = Math.max(1, Number(p.get("page")) || 1);
  }
  function writeUrl() {
    const p = new URLSearchParams();
    if (state.scope === "all") p.set("scope", "all");
    for (const k of ["status", "q", "from", "to"]) if (state[k]) p.set(k, state[k]);
    if (state.page > 1) p.set("page", state.page);
    history.replaceState(null, "", p.toString() ? "?" + p : location.pathname);
  }
  function syncControls() {
    document.querySelectorAll(".seg button").forEach((b) => b.classList.toggle("on", b.dataset.scope === state.scope));
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
    { key: "today", label: "今天上传的 Invoice", cls: "info", apply: () => ({ from: todayStr(), to: "", status: "" }) },
    { key: "total", label: "文档总数", cls: "", apply: () => ({ from: "", to: "", status: "" }) },
    { key: "editing", label: "编辑中", cls: "", apply: () => ({ status: "editing" }) },
    { key: "confirmed", label: "已确认 / Ready", cls: "ok", apply: () => ({ status: "confirmed" }) },
    { key: "attention", label: "失败或需人工检查", cls: "err", apply: () => ({ status: "attention" }) },
    { key: "exported", label: "已导出 CSV", cls: "", apply: null },
  ];
  const todayStr = () => { const d = new Date(); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`; };

  async function loadStats() {
    const p = new URLSearchParams({ mine: state.scope === "mine", since: CIV.startOfToday().toISOString() });
    const s = await api("/api/documents/stats?" + p);
    $("#stats").replaceChildren(...CARDS.map((c) => {
      const active = c.key === "today" ? state.from === todayStr() && !state.to && !state.status
        : c.key === "total" ? !state.status && !state.from && !state.to && !state.q
          : c.apply && state.status === c.apply().status;
      const card = el("button", { type: "button", class: `stat ${c.cls}${active ? " on" : ""}`, disabled: !c.apply,
        title: c.apply ? "点击筛选列表" : "" }, el("span", { class: "n", text: String(s[c.key] ?? 0) }), el("span", { class: "l", text: c.label }));
      if (c.apply) card.onclick = () => { Object.assign(state, { q: "", ...c.apply(), page: 1 }); syncControls(); refresh(); };
      return card;
    }));
  }

  // ───────────── list ─────────────
  async function loadList() {
    const p = new URLSearchParams({ mine: state.scope === "mine", page: state.page, page_size: state.pageSize });
    if (state.status) p.set("status", state.status);
    if (state.q.trim()) p.set("q", state.q.trim());
    if (state.from) p.set("created_from", localDay(state.from, false));
    if (state.to) p.set("created_to", localDay(state.to, true));
    const data = await api("/api/documents?" + p);
    $("#listError").replaceChildren();
    const tbody = $("#rows");
    if (!data.items.length) {
      tbody.replaceChildren(el("tr", {}, el("td", { colspan: "9", class: "empty" },
        state.status || state.q || state.from || state.to ? "没有符合条件的文档" : "还没有文档。上传一份 Commercial Invoice PDF 开始。")));
    } else {
      tbody.replaceChildren(...data.items.map(row));
    }
    const pages = Math.max(1, Math.ceil(data.total / data.page_size));
    $("#count").textContent = `共 ${data.total} 份 · 第 ${data.page} / ${pages} 页`;
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
      el("a", { class: "btn small", href: `/documents/${d.id}`, onclick: (e) => e.stopPropagation() }, d.status === "failed" ? "处理" : "打开"),
      el("button", { type: "button", class: "btn small", disabled: !ready, onclick: stop((e) => preview(d, e.currentTarget)) }, "预览"),
      el("button", { type: "button", class: "btn small", disabled: !ready, onclick: stop(() => exportCsv(d)) }, "导出 CSV"),
      d.status === "editing" ? el("button", { type: "button", class: "btn small", onclick: stop(() => confirmDoc(d)) }, "Confirm") : null,
    );
    const name = el("td", { class: "name" }, el("b", { text: d.name }),
      el("span", { class: "hint", text: [d.invoice_no && `发票号 ${d.invoice_no}`, d.files.length > 1 && `${d.files.length} 个 PDF`].filter(Boolean).join(" · ") }));
    if (d.status === "failed" && d.error) name.append(el("span", { class: "err-line", text: d.error }));
    return el("tr", { onclick: open, title: "打开文档" },
      name,
      el("td", {}, ...statusBadges(d)),
      el("td", { class: "num", text: d.status === "editing" || d.status === "confirmed" ? String(d.line_count) : "–" }),
      el("td", { text: d.customer_id || "–" }),
      el("td", { text: d.created_by ? d.created_by.name : "" }),
      el("td", { text: fmtTime(d.created_at) }),
      el("td", { text: fmtTime(d.updated_at) }),
      el("td", {}, d.last_exported_at ? el("span", { class: "badge ok", title: `共导出 ${d.export_count} 次` }, "✓ ", fmtTime(d.last_exported_at)) : el("span", { class: "hint", text: "未导出" })),
      el("td", {}, acts),
    );
  }

  async function exportCsv(d) {
    try {
      await download(`/api/documents/${d.id}/export?format=csv`, "POST", `${d.name}.csv`);
      toast("已导出 CSV");
      refresh();
    } catch (e) { toast("导出失败：" + e.message, "err"); }
  }

  async function confirmDoc(d) {
    const ok = await ask({
      title: "确认这份文档？",
      body: el("div", {}, el("p", { text: `「${d.name}」将标记为“已确认 / Ready”，可以交付 CargoWise 使用。确认后表格变为只读（需要时可以重新编辑）。` }),
        d.needs_review ? el("p", { class: "msg warn", text: "这份文档还有校验问题（需复核）。建议先打开检查。" }) : null),
      ok: "确认 Confirm", okClass: "ok",
    });
    if (!ok) return;
    try {
      await api(`/api/documents/${d.id}/confirm`, { method: "POST", json: {} });
      toast("已确认");
      refresh();
    } catch (e) { toast("确认失败：" + e.message, "err"); }
  }

  function preview(d, button) {
    CIVPreview.open({
      title: d.name,
      load: () => api(`/api/documents/${d.id}/preview`),
      returnFocus: button,
      actions: () => [
        el("a", { class: "btn", href: `/documents/${d.id}` }, "打开编辑"),
        el("button", { type: "button", class: "btn primary", onclick: () => exportCsv(d) }, "导出 CSV"),
      ],
    });
  }

  async function refresh() {
    writeUrl();
    try {
      await Promise.all([loadStats(), loadList()]);
    } catch (e) {
      $("#listError").replaceChildren(el("div", { class: "msg err", text: "读取文档失败：" + e.message }));
    }
  }

  // ───────────── upload ─────────────
  function renderFiles() {
    $("#filelist").replaceChildren(...files.map((f, i) => el("li", {}, el("span", { text: f.name }),
      el("span", { class: "hint" }, fmtSize(f.size) + " ", el("button", { type: "button", class: "link", onclick: () => { files.splice(i, 1); renderFiles(); } }, "移除")))));
    $("#mergeRow").hidden = files.length < 2;
    $("#go").disabled = files.length === 0;
  }
  function addFiles(list) {
    for (const f of list) {
      if (!/\.pdf$/i.test(f.name)) { toast(`${f.name} 不是 PDF，已忽略`, "err"); continue; }
      if (!files.some((x) => x.name === f.name && x.size === f.size)) files.push(f);
    }
    renderFiles();
  }
  async function upload() {
    const fd = new FormData();
    for (const f of files) fd.append("files", f, f.name);
    if ($("#customer").value) fd.append("customer", $("#customer").value);
    if (files.length > 1) fd.append("merge", $("#merge").checked ? "true" : "false");
    $("#go").disabled = true;
    $("#upStatus").replaceChildren(el("span", { class: "spin" }), " 正在上传…");
    try {
      const { documents } = await api("/api/documents", { method: "POST", form: fd });
      files.length = 0;
      renderFiles();
      $("#upStatus").textContent = "";
      if (documents.length === 1) { location.href = `/documents/${documents[0].id}`; return; }
      toast(`已上传 ${documents.length} 份文档，正在转换`);
      Object.assign(state, { status: "", q: "", from: "", to: "", page: 1 });
      syncControls();
      refresh();
    } catch (e) {
      $("#upStatus").textContent = "";
      toast("上传失败：" + e.message, "err");
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
    const c = customers.find((x) => x.id === e.target.value);
    $("#merge").checked = !!c && c.multi_file === "merge";
  };
  document.querySelectorAll(".seg button").forEach((b) => { b.onclick = () => { state.scope = b.dataset.scope; state.page = 1; syncControls(); refresh(); }; });
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
    readUrl();
    syncControls();
    try {
      const user = await CIV.currentUser();
      CIV.topbar(user);
      const hour = new Date().getHours();
      $("#greeting").textContent = `${hour < 12 ? "上午好" : hour < 18 ? "下午好" : "晚上好"}，${user.name}`;
      customers = await api("/api/customers");
      const sel = $("#customer");
      for (const c of customers) sel.append(el("option", { value: c.id, text: `${c.name}（${c.id}）` }));
      try { sel.value = localStorage.getItem("civconvert.customer") || ""; } catch { /* private mode */ }
      if (sel.selectedIndex < 0) sel.value = "";
    } catch (e) {
      if (e.status !== 401) toast(e.message, "err");
      return;
    }
    refresh();
  })();
})();
