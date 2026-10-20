/* Shared by every page: API calls (session cookie + CSRF header), the top bar, dialogs, toasts. */
(() => {
  "use strict";

  const el = (tag, attrs = {}, ...children) => {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? "" : v);
    }
    for (const c of children) if (c != null && c !== false) node.append(c);
    return node;
  };
  const $ = (sel, root = document) => root.querySelector(sel);

  function toLogin() {
    location.href = "/login?next=" + encodeURIComponent(location.pathname + location.search);
  }

  class ApiError extends Error {
    constructor(message, status, data) { super(message); this.status = status; this.data = data || {}; }
  }

  /** fetch + JSON. Writes send X-CIV-Request (CSRF). 401 -> login page (unless quiet401). */
  async function api(path, { method = "GET", json, form, raw = false, keepalive = false, quiet401 = false } = {}) {
    const headers = {};
    let body;
    if (method !== "GET") headers["X-CIV-Request"] = "1";
    if (json !== undefined) { headers["Content-Type"] = "application/json"; body = JSON.stringify(json); }
    else if (form) body = form;
    let res;
    try {
      res = await fetch(path, { method, headers, body, credentials: "same-origin", keepalive });
    } catch {
      const err = new ApiError("连接不到服务器（网络中断或服务没有运行）", 0);
      err.offline = true;
      throw err;
    }
    if (res.status === 401 && !quiet401) {
      toLogin();
      throw new ApiError("请先登录", 401);
    }
    if (raw && res.ok) return res;
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new ApiError(data.error || data.detail || `HTTP ${res.status}`, res.status, data);
    return data;
  }

  const pad = (n) => String(n).padStart(2, "0");
  function fmtTime(iso, withDate = true) {
    if (!iso) return "";
    const d = new Date(iso);
    const time = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
    if (!withDate) return `${time}:${pad(d.getSeconds())}`;
    const today = new Date();
    if (d.toDateString() === today.toDateString()) return `今天 ${time}`;
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${time}`;
  }
  const fmtSize = (n) => (n >= 1048576 ? (n / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(n / 1024)) + " KB");
  const startOfToday = () => { const d = new Date(); d.setHours(0, 0, 0, 0); return d; };

  const STATUS = {
    processing: "处理中",
    editing: "编辑中",
    confirmed: "已确认",
    failed: "失败 / 需处理",
  };
  function statusBadges(doc) {
    const out = [el("span", { class: `badge st-${doc.status}` }, doc.status === "processing" ? el("span", { class: "spin" }) : null, STATUS[doc.status] || doc.status)];
    if (doc.status === "editing" && doc.needs_review) out.push(el("span", { class: "badge st-review", title: "有校验问题，请检查" }, "需复核"));
    return out;
  }

  function toast(text, kind = "") {
    let box = $(".toasts");
    if (!box) { box = el("div", { class: "toasts", role: "status", "aria-live": "polite" }); document.body.append(box); }
    const t = el("div", { class: "toast " + kind, text });
    box.append(t);
    setTimeout(() => t.remove(), kind === "err" ? 6000 : 2600);
  }

  /** A modal question; resolves true (OK) / false. body: string or nodes. */
  function ask({ title, body, ok = "确定", cancel = "取消", okClass = "primary", danger = false }) {
    return new Promise((resolve) => {
      const dlg = el("dialog", { class: "dlg", "aria-label": title });
      const okBtn = el("button", { type: "button", class: `btn ${danger ? "danger" : okClass}`, text: ok });
      const cancelBtn = cancel ? el("button", { type: "button", class: "btn", text: cancel }) : null;
      dlg.append(
        el("div", { class: "dlg-body" }, el("h3", { text: title }), typeof body === "string" ? el("p", { text: body }) : body),
        el("div", { class: "dlg-actions" }, cancelBtn, okBtn),
      );
      const done = (v) => { dlg.close(); dlg.remove(); resolve(v); };
      okBtn.onclick = () => done(true);
      if (cancelBtn) cancelBtn.onclick = () => done(false);
      dlg.addEventListener("cancel", (e) => { e.preventDefault(); done(false); });
      document.body.append(dlg);
      dlg.showModal();
      okBtn.focus();
    });
  }

  function saveBlob(blob, filename) {
    const url = URL.createObjectURL(blob);
    const a = el("a", { href: url, download: filename });
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
  }

  function filenameFrom(res, fallback) {
    const cd = res.headers.get("Content-Disposition") || "";
    const star = /filename\*=UTF-8''([^;]+)/i.exec(cd);
    if (star) return decodeURIComponent(star[1]);
    const plain = /filename="([^"]+)"/i.exec(cd);
    return plain ? plain[1] : fallback;
  }

  /** POST/GET a file endpoint and save the response. */
  async function download(path, method = "POST", fallback = "download") {
    const res = await api(path, { method, raw: true });
    saveBlob(await res.blob(), filenameFrom(res, fallback));
  }

  /** Top bar: brand, page title, the user and log out. */
  async function topbar(user) {
    const bar = $(".topbar");
    const initials = (user.name || user.email).split(/\s+/).map((p) => p[0]).join("").slice(0, 2).toUpperCase();
    const logout = el("button", { type: "button", class: "btn small", text: "退出登录" });
    logout.onclick = async () => {
      logout.disabled = true;
      await api("/api/auth/logout", { method: "POST" }).catch(() => {});
      location.href = "/login";
    };
    bar.append(el("div", { class: "userbox" },
      el("span", { class: "avatar", text: initials, "aria-hidden": "true" }),
      el("span", { class: "who" }, el("b", { text: user.name }), " ", user.email),
      logout));
  }

  async function currentUser() {
    const { user } = await api("/api/auth/me");
    return user;
  }

  function menu(x, y, items) {
    document.querySelectorAll(".menu").forEach((m) => m.remove());
    const box = el("div", { class: "menu", role: "menu" });
    for (const it of items) {
      if (it === "-") { box.append(el("hr")); continue; }
      const b = el("button", { type: "button", role: "menuitem", disabled: it.disabled }, el("span", { text: it.label }), it.key ? el("kbd", { text: it.key }) : null);
      b.onclick = () => { close(); it.run(); };
      box.append(b);
    }
    document.body.append(box);
    const r = box.getBoundingClientRect();
    box.style.left = Math.min(x, innerWidth - r.width - 8) + "px";
    box.style.top = Math.min(y, innerHeight - r.height - 8) + "px";
    const close = () => { box.remove(); document.removeEventListener("mousedown", away, true); document.removeEventListener("keydown", esc, true); };
    const away = (e) => { if (!box.contains(e.target)) close(); };
    const esc = (e) => { if (e.key === "Escape") { e.stopPropagation(); close(); } };
    setTimeout(() => { document.addEventListener("mousedown", away, true); document.addEventListener("keydown", esc, true); });
    const first = box.querySelector("button:not(:disabled)");
    if (first) first.focus();
  }

  window.CIV = { el, $, api, ApiError, toLogin, fmtTime, fmtSize, startOfToday, STATUS, statusBadges, toast, ask, saveBlob, download, topbar, currentUser, menu };
})();
