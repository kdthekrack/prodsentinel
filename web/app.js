/* ProdSentinel — single-page frontend for api/main.py */
"use strict";

// ------------------------------------------------------------------ utils
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* ignore */ } },
};
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
function parseTs(s) {
  if (s == null || s === "") return null;
  if (typeof s === "number") return new Date(s);
  const m = String(s).match(/(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?/);
  if (!m) { const d = new Date(s); return isNaN(d) ? null : d; }
  return new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +(m[6] || 0));
}
const pad = n => String(n).padStart(2, "0");
function fmtTs(s, withYear = true) {
  const d = parseTs(s); if (!d) return "—";
  return `${d.getDate()} ${MONTHS[d.getMonth()]}${withYear ? " " + d.getFullYear() : ""}, ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
const fmtDay = s => { const d = parseTs(s); return d ? `${DAYS[d.getDay()]} ${d.getDate()} ${MONTHS[d.getMonth()]}` : "—"; };
const toLocalInput = s => { const d = parseTs(s); return d ? `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}` : ""; };
const inr = v => (v == null || isNaN(v)) ? "—" : (v < 0 ? "-" : "") + "₹" + Math.round(Math.abs(v)).toLocaleString("en-IN");
function lakh(v) {
  if (v == null || isNaN(v)) return "—";
  const a = Math.abs(v), s = v < 0 ? "-" : "";
  if (a >= 1e7) return `${s}₹${(a / 1e7).toFixed(2)} Cr`;
  if (a >= 1e5) return `${s}₹${(a / 1e5).toFixed(1)} L`;
  if (a >= 1e3) return `${s}₹${(a / 1e3).toFixed(1)}K`;
  return inr(v);
}
const num = v => (v == null || isNaN(v)) ? "—" : Math.round(v).toLocaleString("en-IN");
const fmtMins = m => { m = Math.max(0, Math.round(m)); return m < 60 ? `${m} min` : m < 1440 ? `${Math.floor(m / 60)} h ${m % 60} min` : `${Math.round(m / 1440)} days`; };
const pct = v => (v == null || isNaN(v)) ? "—" : `${Math.min(99, Math.round(v * 100))}%`;
const hrs = h => h == null ? "—" : h < 1 ? "now" : h < 48 ? `${Math.round(h)}h` : `${(h / 24).toFixed(1)}d`;
const initials = n => String(n || "?").split(/\s+/).map(x => x[0]).slice(0, 2).join("").toUpperCase();
const cap = s => String(s || "").charAt(0).toUpperCase() + String(s || "").slice(1);

function toast(msg, kind = "ok") {
  const c = { ok: "bg-secondary", err: "bg-error", info: "bg-primary-container" }[kind] || "bg-inverse-surface";
  const el = document.createElement("div");
  el.className = `${c} text-white px-4 py-3 rounded-xl shadow-lg text-sm font-medium max-w-sm flex items-start gap-2`;
  el.innerHTML = `<span class="material-symbols-outlined text-[18px]">${kind === "err" ? "error" : "check_circle"}</span><span>${esc(msg)}</span>`;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), 4500);
}

// ------------------------------------------------------------------ api
async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json" };
  if (S.token) headers["X-User-Token"] = S.token;
  const res = await fetch(path, { method: opts.method || "GET", headers, body: opts.body ? JSON.stringify(opts.body) : undefined });
  if (res.status === 401 && !path.startsWith("/ui/") && !opts._retried) {
    await switchTo(store.get("ews_user") || defaultPerson(), true);
    return api(path, { ...opts, _retried: true });
  }
  let data = null;
  try { data = await res.json(); } catch { /* empty */ }
  if (!res.ok) {
    let d = data && data.detail;
    if (Array.isArray(d)) d = d.map(x => x.msg).join("; ");
    throw new Error(d || `${res.status} ${res.statusText}`);
  }
  return data;
}
const post = (path, body = {}) => api(path, { method: "POST", body });

async function guarded(btn, fn) {
  if (btn) { btn.disabled = true; btn.dataset.html = btn.innerHTML; btn.innerHTML = `<span class="material-symbols-outlined text-[18px] spin">progress_activity</span> Working…`; }
  try { return await fn(); }
  catch (e) { toast(e.message, "err"); }
  finally { if (btn && btn.isConnected) { btn.disabled = false; btn.innerHTML = btn.dataset.html; } }
}

// ------------------------------------------------------------------ state
const S = { token: null, user: null, cfg: null, now: null, summary: null, risks: [], alerts: [], notes: [], assistant: false };
const SEV = {
  critical: { label: "Critical", bar: "bg-error", pill: "bg-error-container text-error", text: "text-error", dot: "bg-error", cell: "#ba1a1a" },
  high: { label: "High", bar: "bg-orange-500", pill: "bg-orange-100 text-orange-700", text: "text-orange-700", dot: "bg-orange-500", cell: "#f97316" },
  medium: { label: "Medium", bar: "bg-amber-400", pill: "bg-amber-100 text-amber-800", text: "text-amber-700", dot: "bg-amber-400", cell: "#fbbf24" },
  low: { label: "Low", bar: "bg-slate-300", pill: "bg-slate-100 text-slate-600", text: "text-slate-600", dot: "bg-slate-400", cell: "#94a3b8" },
  ok: { label: "Normal", bar: "bg-secondary", pill: "bg-secondary/10 text-secondary", text: "text-secondary", dot: "bg-secondary", cell: "#10b981" },
};
const SEV_ORDER = ["critical", "high", "medium", "low"];
const CAT = {
  machine: { icon: "precision_manufacturing", big: "vibration", label: "Machine", color: "#ba1a1a" },
  material: { icon: "inventory_2", big: "local_shipping", label: "Inbound", color: "#2563eb" },
  workforce: { icon: "groups", big: "person_alert", label: "Workforce", color: "#7c3aed" },
  quality: { icon: "rule", big: "troubleshoot", label: "Quality", color: "#d97706" },
  orders: { icon: "assignment_late", big: "assignment_late", label: "Orders", color: "#0891b2" },
  demand: { icon: "trending_up", big: "trending_up", label: "Demand", color: "#0d9488" },
  compound: { icon: "hub", big: "hub", label: "Compound", color: "#be123c" },
};
const cat = c => CAT[c] || { icon: "warning", big: "warning", label: cap(c), color: "#737686" };
const sev = s => SEV[s] || SEV.low;
const lineName = id => (S.cfg?.lines.find(l => l.id === id)?.name) || id;
const lineShort = id => lineName(id).replace(/^Line \d+ - /, "");

const approverRole = () => S.cfg?.escalation?.approver_role || "plant_manager";
const isApprover = () => S.user?.role === approverRole();
const approverName = () => (S.cfg?.people.find(p => p.role === approverRole()) || {}).name || "the Plant Manager";
function defaultPerson() {
  const pm = S.cfg?.people.find(p => p.role === "plant_manager");
  return (pm || S.cfg?.people[0] || {}).name;
}
async function switchTo(name, quiet = false) {
  const r = await post("/ui/switch", { name });
  S.token = r.token; S.user = { ...r.person, title: r.title };
  store.set("ews_user", name); store.set("ews_token", r.token);
  renderUser();
  if (!quiet) toast(`Now viewing as ${name} (${r.title})`, "info");
}
async function login(name, pin) {
  const r = await post("/ui/login", { name, pin });
  S.token = r.token; S.user = { ...r.person, title: r.title };
  store.set("ews_user", name); store.set("ews_token", r.token);
  renderUser();
  toast(`Signed in as ${name} (${r.title})`, "info");
}
function signOut() {
  S.token = null; S.user = null;
  try { localStorage.removeItem("ews_token"); } catch { /* ignore */ }
  showSignin();
}
let _signinResolve = null;
async function showSignin() {
  closePops(); closeModal();
  const box = $("#signin"); if (!box.classList.contains("hidden")) return;
  box.classList.remove("hidden");
  let people = [];
  try { people = await api("/ui/people"); } catch (e) { $("#siErr").textContent = e.message; $("#siErr").classList.remove("hidden"); }
  const saved = store.get("ews_user");
  $("#siName").innerHTML = people.map(p => `<option value="${esc(p.name)}" data-pin="${p.has_pin ? 1 : 0}" ${p.name === saved ? "selected" : ""}>${esc(p.name)} — ${esc(p.title)}</option>`).join("");
  const hint = () => {
    const o = $("#siName").selectedOptions[0], first = o && o.dataset.pin === "0";
    $("#siPinLabel").textContent = first ? "Choose a PIN (4–12 digits)" : "PIN";
    $("#siHint").textContent = first ? "First sign-in for this person: the PIN you enter now becomes their PIN." : "";
    $("#siGo").textContent = first ? "Set PIN & sign in" : "Sign in";
  };
  $("#siName").onchange = hint; hint();
  $("#siPin").value = ""; $("#siErr").classList.add("hidden");
  $("#siPin").focus();
}
$("#signinForm").addEventListener("submit", async e => {
  e.preventDefault();
  const pin = $("#siPin").value.trim();
  if (!/^\d{4,12}$/.test(pin)) { $("#siErr").textContent = "PIN must be 4–12 digits."; $("#siErr").classList.remove("hidden"); return; }
  let ok = false;
  await guarded($("#siGo"), async () => {
    try { await login($("#siName").value, pin); ok = true; }
    catch (err) { $("#siErr").textContent = err.message; $("#siErr").classList.remove("hidden"); }
  });
  if (!ok) return;
  $("#signin").classList.add("hidden");
  if (_signinResolve) { const r = _signinResolve; _signinResolve = null; r(); }
  else { await refresh(); render(); }
});
function changePin() {
  modal(`<h3 class="text-headline-sm mb-3">Change PIN</h3>
    <label class="block text-body-sm text-on-surface-variant">Current PIN<input id="cpOld" type="password" inputmode="numeric" class="mt-1 w-full rounded-xl border-slate-200 text-sm"></label>
    <label class="block text-body-sm text-on-surface-variant mt-3">New PIN (4–12 digits)<input id="cpNew" type="password" inputmode="numeric" class="mt-1 w-full rounded-xl border-slate-200 text-sm"></label>
    <div id="cpErr" class="hidden text-error text-body-sm mt-2"></div>
    <div class="flex justify-end gap-2 mt-4"><button data-close class="px-4 py-2 rounded-xl bg-slate-100 hover:bg-slate-200 text-sm font-semibold">Cancel</button><button id="cpOk" class="px-4 py-2 rounded-xl bg-primary-container text-white text-sm font-semibold">Change PIN</button></div>`, el => {
    $("#cpOld", el).focus();
    $("#cpOk", el).onclick = async () => {
      const o = $("#cpOld", el).value.trim(), n = $("#cpNew", el).value.trim();
      if (!/^\d{4,12}$/.test(n)) { $("#cpErr", el).textContent = "New PIN must be 4–12 digits."; $("#cpErr", el).classList.remove("hidden"); return; }
      try { await post("/ui/pin", { old: o, new: n }); closeModal(); toast("PIN changed"); }
      catch (e) { $("#cpErr", el).textContent = e.message; $("#cpErr", el).classList.remove("hidden"); }
    };
  });
}

async function refresh() {
  const [clock, summary, risks, alerts, notes] = await Promise.all([
    api("/clock"), api("/summary"), api("/risks?alerts_only=false&limit=500"),
    api("/alerts?status=open,acknowledged"), api("/notifications?limit=40"),
  ]);
  S.now = clock.now; S.summary = summary; S.risks = risks; S.alerts = alerts; S.notes = notes;
  renderChrome();
}

function setLive(ok, msg) {
  $("#liveping").className = `animate-ping absolute inline-flex h-full w-full rounded-full ${ok ? "bg-secondary" : "bg-error"} opacity-75`;
  $("#livedot").className = `relative inline-flex rounded-full h-2.5 w-2.5 ${ok ? "bg-secondary" : "bg-error"}`;
  $("#livetitle").textContent = ok ? "Facility Online" : "Engine Offline";
  $("#livesub").textContent = msg;
  $("#livesub").className = `text-[10px] font-bold uppercase ${ok ? "text-secondary" : "text-error"}`;
}

// risk card for an alert row (active risk if still present, else stored narration)
function riskFor(key) { return S.risks.find(r => r.item.key === key); }
function alertFor(key) { return S.alerts.find(a => a.key === key); }

// ------------------------------------------------------------------ chrome
const NAV = [
  ["control-room", "tune", "Control Room"],
  ["horizon", "timeline", "Event Horizon"],
  ["playbook", "auto_stories", "Playbook Cascade"],
  ["alert-history", "task_alt", "Alert History"],
  ["machines", "precision_manufacturing", "Machine Telemetry"],
  ["supply", "radar", "Inbound Supply Radar"],
  ["orders", "calendar_clock", "Orders & Schedule"],
  ["history", "history", "Order History"],
  ["settings", "settings", "Settings"],
];

function renderNav() {
  const cur = route().page;
  const unack = S.alerts.filter(a => a.status === "open").length;
  $("#nav").innerHTML = NAV.map(([id, icon, label]) => `
    <a href="#/${id}" class="flex items-center justify-between gap-space-sm px-space-md py-2.5 rounded-lg text-on-surface-variant hover:bg-surface-container-low hover:text-on-surface transition-colors ${cur === id || (cur === "alert" && id === "horizon") || (cur === "risk" && id === "horizon") ? "nav-active" : ""}">
      <span class="flex items-center gap-space-sm"><span class="material-symbols-outlined text-[20px]">${icon}</span><span>${label}</span></span>
      ${id === "horizon" && unack ? `<span class="px-2 py-0.5 rounded-full bg-error text-white text-[11px] font-bold shadow-sm">${unack}</span>` : ""}
    </a>`).join("");
}

function sevCounts() {
  const act = S.risks.filter(r => r.item.alert);
  return {
    critical: act.filter(r => r.item.severity === "critical").length,
    warning: act.filter(r => ["high", "medium"].includes(r.item.severity)).length,
    normal: S.risks.filter(r => !r.item.alert || r.item.severity === "low").length,
  };
}

function renderChrome() {
  $("#clockText").textContent = fmtTs(S.now);
  $("#plantName").textContent = `${S.cfg.company.site} - ${S.cfg.company.industry.split(" ").pop().replace(/^./, c => c.toUpperCase())}`;
  const c = sevCounts();
  $("#sevChips").innerHTML = `
    <a href="#/horizon?severity=critical" class="px-2.5 py-1 rounded-full bg-error/10 text-error text-[11px] font-bold flex items-center gap-1 hover:bg-error/20"><span class="h-2 w-2 rounded-full bg-error animate-pulse"></span> ${c.critical} Critical</a>
    <a href="#/horizon?severity=warning" class="px-2.5 py-1 rounded-full bg-amber-500/10 text-amber-700 text-[11px] font-bold flex items-center gap-1 hover:bg-amber-500/20"><span class="h-2 w-2 rounded-full bg-amber-500"></span> ${c.warning} Warning</a>
    <a href="#/horizon?severity=normal&watch=1" class="px-2.5 py-1 rounded-full bg-secondary/10 text-secondary text-[11px] font-bold flex items-center gap-1 hover:bg-secondary/20"><span class="h-2 w-2 rounded-full bg-secondary"></span> ${c.normal} Normal</a>`;
  const mine = S.notes.filter(n => n.to_name === S.user?.name);
  const seen = +(store.get("ews_seen_note") || 0);
  $("#bellDot").classList.toggle("hidden", !S.notes.some(n => n.id > seen));
  renderNav();
  void mine;
}

function renderUser() {
  if (!S.user) return;
  $("#userName").textContent = S.user.name;
  $("#userRole").textContent = S.user.title;
  $("#userAvatar").textContent = initials(S.user.name);
}

function closePops(except) { ["#plantPop", "#clockPop", "#bellPop", "#userPop"].forEach(id => { if (id !== except) $(id).classList.add("hidden"); }); }
function togglePop(id, fill) {
  const el = $(id), open = el.classList.contains("hidden");
  closePops(id);
  if (open) { fill(el); el.classList.remove("hidden"); } else el.classList.add("hidden");
}

function bindChrome() {
  document.addEventListener("click", e => {
    if (!e.target.closest(".popover") && !e.target.closest("#plantBtn,#clockBtn,#bellBtn,#userBtn")) closePops();
  });
  $("#plantBtn").onclick = () => togglePop("#plantPop", el => {
    const c = S.cfg.company;
    el.innerHTML = `
      <div class="text-label-caps uppercase text-outline mb-2">Active plant</div>
      <div class="flex items-center gap-2 p-2 rounded-xl bg-surface-container-low">
        <span class="material-symbols-outlined text-primary">factory</span>
        <div><div class="font-semibold">${esc(c.name)}</div><div class="text-body-sm text-on-surface-variant">${esc(c.industry)} · ${esc(c.site)}</div></div>
        <span class="material-symbols-outlined text-secondary ml-auto">check_circle</span>
      </div>
      <div class="text-label-caps uppercase text-outline mt-3 mb-1">Lines</div>
      ${S.cfg.lines.map(l => `<a href="#/horizon?line=${l.id}" class="flex justify-between px-2 py-1.5 rounded-lg hover:bg-surface-container-low text-sm"><span>${esc(l.name)}</span><span class="text-outline">${num(l.product.units_per_hour)}/h</span></a>`).join("")}`;
    $$("a", el).forEach(a => a.addEventListener("click", () => closePops()));
  });
  $("#clockBtn").onclick = () => togglePop("#clockPop", el => {
    el.innerHTML = `
      <div class="text-label-caps uppercase text-outline mb-1">Plant clock (replay of 2015 data)</div>
      <p class="text-body-sm text-on-surface-variant mb-3">Advancing runs assess → explain → alert → escalate, like a live plant.</p>
      <div class="grid grid-cols-3 gap-2 mb-3">
        ${[3, 12, 24].map(h => `<button data-adv="${h}" class="px-2 py-2 rounded-xl bg-primary-container text-white text-sm font-semibold hover:bg-primary">+${h}h</button>`).join("")}
      </div>
      <label class="text-label-caps uppercase text-outline">Jump to</label>
      <div class="flex gap-2 mt-1">
        <input id="clockInput" type="datetime-local" step="3600" value="${toLocalInput(S.now)}" min="${toLocalInput(S.cfg.range.start)}" max="${toLocalInput(S.cfg.range.end)}" class="flex-1 rounded-xl border-slate-200 text-sm">
        <button id="clockSet" class="px-3 rounded-xl bg-slate-100 hover:bg-slate-200 text-sm font-semibold">Set</button>
      </div>
      <p class="text-body-sm text-outline mt-2">Going back in time clears alert history after that point.</p>`;
    $$("[data-adv]", el).forEach(b => b.onclick = () => guarded(b, async () => {
      const r = await post("/clock/advance", { hours: +b.dataset.adv });
      toast(`Clock now ${fmtTs(r.now)} · ${r.alerts_created} new alert(s), ${new Set(r.escalations.map(x => x.alert_id)).size} escalated alert(s)`);
      await refresh(); closePops(); render();
    }));
    $("#clockSet", el).onclick = () => guarded($("#clockSet", el), async () => {
      const v = $("#clockInput", el).value;
      if (!v) throw new Error("Pick a date and time");
      const r = await post("/clock/set", { ts: v.replace("T", " ") });
      toast(`Clock set to ${fmtTs(r.now)}`);
      await refresh(); closePops(); render();
    });
  });
  $("#bellBtn").onclick = () => togglePop("#bellPop", el => {
    const mineOnly = store.get("ews_bell_mine") === "1";
    const list = S.notes.filter(n => !mineOnly || n.to_name === S.user.name);
    el.innerHTML = `
      <div class="flex items-center justify-between px-4 py-3 border-b border-slate-100 sticky top-0 bg-white">
        <span class="font-semibold">Notifications</span>
        <label class="text-body-sm flex items-center gap-1.5 cursor-pointer"><input id="bellMine" type="checkbox" class="rounded" ${mineOnly ? "checked" : ""}> Only mine</label>
      </div>
      ${list.length ? list.map(n => `
        <a href="#/alert/${esc(n.alert_id)}" class="block px-4 py-3 hover:bg-surface-container-low border-b border-slate-50">
          <div class="text-sm font-medium">${esc(n.subject)}</div>
          <div class="text-body-sm text-on-surface-variant mt-0.5">To ${esc(n.to_name)} (${esc(n.to_role)}) · ${esc(n.channel)} · ${fmtTs(n.ts, false)}</div>
        </a>`).join("") : `<div class="p-6 text-center text-on-surface-variant text-sm">No notifications yet.</div>`}`;
    $("#bellMine", el).onchange = e => { store.set("ews_bell_mine", e.target.checked ? "1" : "0"); closePops(); $("#bellBtn").click(); };
    $$("a", el).forEach(a => a.addEventListener("click", () => closePops()));
    if (S.notes.length) store.set("ews_seen_note", String(Math.max(...S.notes.map(n => n.id))));
    $("#bellDot").classList.add("hidden");
  });
  $("#userBtn").onclick = () => togglePop("#userPop", el => {
    el.innerHTML = `<div class="px-3 py-2 text-label-caps uppercase text-outline">Switch person</div>` + S.cfg.people.map(p => `
      <button data-name="${esc(p.name)}" class="w-full flex items-center gap-2 px-3 py-2 rounded-xl hover:bg-surface-container-low text-left ${p.name === S.user.name ? "bg-surface-container-low" : ""}">
        <span class="w-8 h-8 rounded-lg bg-primary-container/10 text-primary flex items-center justify-center text-xs font-bold">${initials(p.name)}</span>
        <span><span class="block text-sm font-semibold">${esc(p.name)}</span><span class="block text-body-sm text-on-surface-variant">${esc(S.cfg.roles[p.role])}</span></span>
        ${p.name === S.user.name ? `<span class="material-symbols-outlined text-primary ml-auto text-[18px]">check</span>` : ""}
      </button>`).join("");
    $$("[data-name]", el).forEach(b => b.onclick = async () => {
      closePops();
      if (b.dataset.name === S.user.name) return;
      await switchTo(b.dataset.name);
      await refresh(); render();
    });
  });
  const sb = $("#sidebar"), scrim = $("#scrim");
  const side = open => { sb.classList.toggle("-translate-x-full", !open); scrim.classList.toggle("hidden", !open); };
  $("#menuBtn").onclick = () => side(sb.classList.contains("-translate-x-full"));
  scrim.onclick = () => side(false);
  $("#nav").addEventListener("click", e => { if (e.target.closest("a")) side(false); });
  $("#searchForm").onsubmit = e => {
    e.preventDefault();
    const q = $("#searchInput").value.trim();
    if (!q) return;
    if (/^L\d-[A-Z]{3}-\d{2}$/i.test(q)) location.hash = `#/machines/${q.toUpperCase()}`;
    else if (/^SO\d+$/i.test(q)) location.hash = `#/orders?q=${encodeURIComponent(q.toUpperCase())}`;
    else if (/^ALR-\d+$/i.test(q)) location.hash = `#/alert/${q.toUpperCase()}`;
    else location.hash = `#/horizon?q=${encodeURIComponent(q)}&watch=1`;
  };
}

// ------------------------------------------------------------------ modal
function modal(html, onMount) {
  $("#modalBody").innerHTML = html;
  $("#modal").classList.remove("hidden");
  $$("[data-close]", $("#modalBody")).forEach(b => b.onclick = closeModal);
  onMount && onMount($("#modalBody"));
}
function closeModal() { $("#modal").classList.add("hidden"); $("#modalBody").innerHTML = ""; }
$("#modal").addEventListener("click", e => { if (e.target.id === "modal") closeModal(); });
document.addEventListener("keydown", e => { if (e.key === "Escape") { closeModal(); closePops(); } });

function askText({ title, label, placeholder = "", min = 0, confirm = "Save", danger = false }) {
  return new Promise(resolve => {
    modal(`
      <h3 class="text-headline-sm mb-1">${esc(title)}</h3>
      <label class="text-body-sm text-on-surface-variant">${esc(label)}</label>
      <textarea id="mText" rows="4" class="w-full mt-2 rounded-xl border-slate-200 text-sm" placeholder="${esc(placeholder)}"></textarea>
      <div id="mErr" class="text-error text-body-sm mt-1 hidden"></div>
      <div class="flex justify-end gap-2 mt-4">
        <button data-close class="px-4 py-2 rounded-xl bg-slate-100 hover:bg-slate-200 text-sm font-semibold">Cancel</button>
        <button id="mOk" class="px-4 py-2 rounded-xl ${danger ? "bg-secondary" : "bg-primary-container"} text-white text-sm font-semibold">${esc(confirm)}</button>
      </div>`, el => {
      const t = $("#mText", el); t.focus();
      $$("[data-close]", el).forEach(b => b.addEventListener("click", () => resolve(null)));
      $("#mOk", el).onclick = () => {
        if (t.value.trim().length < min) { $("#mErr", el).textContent = `Please write at least ${min} characters.`; $("#mErr", el).classList.remove("hidden"); return; }
        const v = t.value.trim(); closeModal(); resolve(v);
      };
    });
  });
}
function confirmBox(title, body, ok = "Confirm") {
  return new Promise(resolve => modal(`
    <h3 class="text-headline-sm mb-2">${esc(title)}</h3><div class="text-sm text-on-surface-variant">${body}</div>
    <div class="flex justify-end gap-2 mt-5">
      <button data-close class="px-4 py-2 rounded-xl bg-slate-100 hover:bg-slate-200 text-sm font-semibold">Cancel</button>
      <button id="mOk" class="px-4 py-2 rounded-xl bg-secondary text-white text-sm font-semibold">${esc(ok)}</button>
    </div>`, el => {
    $$("[data-close]", el).forEach(b => b.addEventListener("click", () => resolve(false)));
    $("#mOk", el).onclick = () => { closeModal(); resolve(true); };
  }));
}

// ------------------------------------------------------------------ alert workflow
async function ackAlert(id, btn) {
  const note = await askText({ title: `Acknowledge ${id}`, label: "Optional note for the team", confirm: "Acknowledge" });
  if (note === null) return;
  await guarded(btn, async () => { await post(`/alerts/${id}/ack`, { note }); toast(`${id} acknowledged`); await refresh(); render(); });
}
async function actionDone(id, idx, btn) {
  const note = await askText({ title: "Mark action done", label: "What was done? (optional)", confirm: "Mark done", danger: true });
  if (note === null) return;
  await guarded(btn, async () => { await post(`/alerts/${id}/actions/${idx}/done`, { note }); toast("Action marked done"); await refresh(); render(); });
}
async function dispatchPlan(id, btn) {
  const d = await api(`/alerts/${id}`);
  const pending = d.actions.filter(a => a.status !== "done");
  if (!pending.length) { toast("All actions are already done", "info"); return; }
  const todo = pending.filter(a => a.status === "open");
  if (!todo.length) { toast("This plan is already authorized", "info"); return; }
  const ok = await confirmBox("Authorize & dispatch plan", `You are authorizing the plan for <b>${esc(id)}</b>. Each task below is sent to the person responsible, who must mark it done from their own profile:<ul class="list-disc pl-5 mt-2 space-y-1">${todo.map(a => `<li>${esc(a.text)} <span class="text-outline">— ${esc(a.owner_name)}</span></li>`).join("")}</ul>`, "Authorize & dispatch");
  if (!ok) return;
  await guarded(btn, async () => {
    await post(`/alerts/${id}/authorize`, { note: "" });
    toast(`Plan for ${id} authorized - tasks sent to their owners`); await refresh(); render();
  });
}
async function resolveAlert(id, btn) {
  const r = await askText({ title: `Resolve ${id}`, label: "How was it resolved?", min: 3, confirm: "Resolve", danger: true, placeholder: "e.g. Seal replaced during 14:00 changeover" });
  if (r === null) return;
  await guarded(btn, async () => { await post(`/alerts/${id}/resolve`, { resolution: r }); toast(`${id} resolved`); await refresh(); render(); });
}
async function commentAlert(id, btn) {
  const t = await askText({ title: `Comment on ${id}`, label: "Comment", min: 1, confirm: "Post comment" });
  if (t === null) return;
  await guarded(btn, async () => { await post(`/alerts/${id}/comment`, { note: t }); toast("Comment posted"); render(); });
}

function detailLink(item) {
  const c = item.context || {};
  if (item.category === "machine") return { href: `#/machines/${c.machine}`, icon: "monitoring", label: "View Sensor Telemetry" };
  if (item.category === "material") return { href: `#/supply?m=${encodeURIComponent(c.material || "")}`, icon: "radar", label: "View Supply Radar" };
  if (item.category === "workforce") return { href: `#/orders?tab=workforce&line=${item.line}`, icon: "groups", label: "View Shift Roster" };
  if (item.category === "quality") return { href: `#/orders?tab=quality&line=${item.line}`, icon: "query_stats", label: "View SPC Charts" };
  return { href: `#/orders?line=${item.line}`, icon: "calendar_clock", label: "View Order Schedule" };
}

function bindWorkflow(root) {
  $$("[data-ack]", root).forEach(b => b.onclick = () => ackAlert(b.dataset.ack, b));
  $$("[data-dispatch]", root).forEach(b => b.onclick = () => dispatchPlan(b.dataset.dispatch, b));
  $$("[data-resolve]", root).forEach(b => b.onclick = () => resolveAlert(b.dataset.resolve, b));
  $$("[data-comment]", root).forEach(b => b.onclick = () => commentAlert(b.dataset.comment, b));
  $$("[data-done]", root).forEach(b => b.onclick = () => { const [id, idx] = b.dataset.done.split("|"); actionDone(id, +idx, b); });
}

// ------------------------------------------------------------------ shared pieces
function pageHeader({ tag, title, sub, right = "" }) {
  return `
  <div class="flex flex-col md:flex-row md:items-end justify-between gap-space-md mb-space-xl">
    <div>
      <div class="flex items-center gap-space-sm mb-space-xs">
        <span class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-surface-container-high text-primary text-label-caps uppercase tracking-wider">
          <span class="w-1.5 h-1.5 rounded-full bg-primary animate-pulse"></span>${esc(tag)}</span>
        <span class="text-outline text-body-sm">•</span>
        <span class="text-on-surface-variant text-label-sm">Assessed at ${fmtTs(S.now)}</span>
      </div>
      <h1 class="text-[28px] leading-9 font-bold md:text-headline-xl tracking-tight">${esc(title)}</h1>
      <p class="text-body-lg text-on-surface-variant max-w-2xl mt-1">${esc(sub)}</p>
    </div>${right}
  </div>`;
}
const card = (inner, cls = "") => `<div class="min-w-0 bg-surface-container-lowest rounded-2xl shadow-sm p-space-lg ${cls}">${inner}</div>`;
const sevPill = (s, extra = "") => `<span class="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full ${sev(s).pill} text-label-caps uppercase font-bold">${extra}${sev(s).label}</span>`;
const empty = (msg, icon = "task_alt") => `<div class="py-12 text-center text-on-surface-variant"><span class="material-symbols-outlined text-[40px] text-secondary">${icon}</span><div class="mt-2">${esc(msg)}</div></div>`;
const loading = () => `<div class="py-24 text-center text-on-surface-variant"><span class="material-symbols-outlined spin text-primary text-[32px]">progress_activity</span><div class="mt-2">Loading…</div></div>`;
function segmented(opts, cur, attr) {
  return `<div class="flex items-center bg-surface-container-lowest p-1.5 rounded-xl shadow-sm self-start md:self-auto">${opts.map(([v, l]) => `
    <button ${attr}="${v}" class="px-3.5 py-1.5 rounded-lg text-label-sm ${v === cur ? "bg-primary-container text-white shadow-sm" : "text-on-surface-variant hover:text-on-surface"}">${esc(l)}</button>`).join("")}</div>`;
}
function select(id, opts, cur, label) {
  return `<label class="flex flex-col text-label-caps uppercase text-outline gap-1">${esc(label)}
    <select id="${id}" class="rounded-xl border-slate-200 text-sm normal-case tracking-normal text-on-surface font-normal py-1.5">${opts.map(([v, l]) => `<option value="${esc(v)}" ${String(v) === String(cur) ? "selected" : ""}>${esc(l)}</option>`).join("")}</select></label>`;
}
const lineOpts = () => [["", "All lines"], ...S.cfg.lines.map(l => [l.id, l.name])];

// ------------------------------------------------------------------ SVG charts
function lineChart({ series, w = 640, h = 170, yLines = [], yLabel = "", xFmt = t => fmtDay(t), band = null }) {
  const pts = series.flatMap(s => s.points).filter(p => p[1] != null && !isNaN(p[1]));
  if (!pts.length) return `<div class="h-[${h}px] flex items-center justify-center text-outline text-sm">No data</div>`;
  const P = { l: 44, r: 10, t: 10, b: 22 };
  let xs = pts.map(p => p[0]), ys = pts.map(p => p[1]).concat(yLines.map(l => l.y));
  let x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
  if (x1 === x0) x1 = x0 + 1;
  const padY = (y1 - y0) * 0.08 || 1; y0 -= padY; y1 += padY;
  const X = x => P.l + (x - x0) / (x1 - x0) * (w - P.l - P.r), Y = y => P.t + (1 - (y - y0) / (y1 - y0)) * (h - P.t - P.b);
  const ticks = 4, fy = v => Math.abs(v) >= 1000 ? Math.round(v).toLocaleString("en-IN") : (+v.toFixed(2)).toString();
  let g = "";
  for (let i = 0; i <= ticks; i++) { const v = y0 + (y1 - y0) * i / ticks; g += `<line x1="${P.l}" x2="${w - P.r}" y1="${Y(v)}" y2="${Y(v)}" stroke="#eef1f8"/><text x="${P.l - 6}" y="${Y(v) + 3}" font-size="10" text-anchor="end" fill="#737686">${fy(v)}</text>`; }
  for (let i = 0; i <= 3; i++) { const v = x0 + (x1 - x0) * i / 3; g += `<text x="${X(v)}" y="${h - 6}" font-size="10" text-anchor="${i === 0 ? "start" : i === 3 ? "end" : "middle"}" fill="#737686">${esc(xFmt(v))}</text>`; }
  if (band) g += `<rect x="${P.l}" width="${w - P.l - P.r}" y="${Y(band[1])}" height="${Math.max(0, Y(band[0]) - Y(band[1]))}" fill="#10b981" opacity=".06"/>`;
  yLines.forEach(l => { g += `<line x1="${P.l}" x2="${w - P.r}" y1="${Y(l.y)}" y2="${Y(l.y)}" stroke="${l.color || "#ba1a1a"}" stroke-dasharray="4 3"/><text x="${w - P.r}" y="${Y(l.y) - 3}" font-size="10" text-anchor="end" fill="${l.color || "#ba1a1a"}">${esc(l.label || "")}</text>`; });
  series.forEach(s => {
    const d = s.points.filter(p => p[1] != null && !isNaN(p[1])).map((p, i) => `${i ? "L" : "M"}${X(p[0]).toFixed(1)},${Y(p[1]).toFixed(1)}`).join("");
    g += `<path d="${d}" fill="none" stroke="${s.color || "#2563eb"}" stroke-width="${s.width || 1.6}" stroke-linejoin="round"/>`;
    (s.marks || []).forEach(p => { g += `<circle cx="${X(p[0])}" cy="${Y(p[1])}" r="3.5" fill="${p[2] || "#ba1a1a"}"/>`; });
  });
  return `<svg viewBox="0 0 ${w} ${h}" class="w-full h-auto" role="img" aria-label="${esc(yLabel)}">${g}</svg>`;
}

// ------------------------------------------------------------------ alert card (Control Room)
function actionCards(alertId, actions) {
  return actions.map((a, i) => {
    const done = a.status === "done", sent = a.status === "dispatched", mine = a.owner_name === S.user.name;
    const overdue = sent && parseTs(a.due_by) < parseTs(S.now);
    const pill = done ? `<span class="px-2.5 py-0.5 rounded-full bg-secondary/10 text-secondary text-label-sm font-bold">• Completed</span>`
      : !sent ? `<span class="px-2.5 py-0.5 rounded-full bg-slate-100 text-on-surface-variant text-label-sm font-bold">• Waiting for approval</span>`
      : overdue ? `<span class="px-2.5 py-0.5 rounded-full bg-error-container text-error text-label-sm font-bold">• Overdue</span>`
        : `<span class="px-2.5 py-0.5 rounded-full bg-surface-container-high text-primary text-label-sm font-bold">• Dispatched</span>`;
    return `
    <div class="flex flex-col justify-between p-space-md rounded-xl border border-slate-100 bg-white shadow-[0_1px_2px_rgba(0,0,0,0.03)]">
      <div class="flex gap-3">
        <span class="w-6 h-6 shrink-0 rounded-full ${done ? "bg-secondary text-white" : "bg-surface-container-high text-primary"} flex items-center justify-center text-label-sm font-bold">${done ? `<span class="material-symbols-outlined text-[14px]">check</span>` : i + 1}</span>
        <div>
          <div class="font-semibold leading-snug ${done ? "line-through text-on-surface-variant" : ""}">${esc(a.text)}</div>
          <div class="text-body-md text-on-surface-variant mt-1">${esc(a.owner_name)} · ${esc(S.cfg.roles[a.owner_role] || a.owner_role)}</div>
        </div>
      </div>
      <div class="flex items-center justify-between mt-space-md gap-2">
        ${pill}
        ${done ? `<span class="text-body-sm text-outline">by ${esc(a.done_by)} · ${fmtTs(a.done_at, false)}</span>`
        : `<span class="flex items-center gap-2"><span class="text-body-sm text-outline tracking-wide">due ${fmtTs(a.due_by, false)}</span>
           ${sent && mine ? `<button data-done="${esc(alertId)}|${a.idx}" class="px-2 py-1 rounded-lg bg-secondary text-white hover:bg-[#005236] text-label-sm font-semibold transition-colors">Mark done</button>`
             : sent ? `<span class="text-body-sm text-outline">for ${esc(a.owner_name.split(" ")[0])}</span>` : ""}</span>`}
      </div>
    </div>`;
  }).join("");
}

function alertCard(a, detail) {
  const r = riskFor(a.key), item = (r && r.item) || detail?.item || {}, n = (r && r.narration) || a.narration || {};
  const s = sev(a.severity), c = cat(a.category), link = detailLink(item);
  const actions = detail?.actions || [];
  const allDone = actions.length && actions.every(x => x.status === "done");
  const ackd = a.status === "acknowledged";
  const left = parseTs(a.ack_deadline) - parseTs(S.now);
  const imp = item.impact || {};
  const bits = [["lost output", imp.lost_margin], ["scrap", imp.scrap], ["repair", imp.repair], ["late-order penalties", imp.late_penalty]].filter(x => x[1] > 0);
  return `
  <div class="bg-surface-container-lowest rounded-2xl shadow-md p-space-lg md:p-space-xl relative overflow-hidden transition-all hover:shadow-lg">
    <div class="absolute left-0 top-0 bottom-0 w-2 ${s.bar}"></div>
    <div class="flex flex-col lg:flex-row lg:items-start justify-between gap-space-md pb-space-md">
      <div class="flex flex-wrap items-center gap-space-sm">
        <span class="inline-flex items-center gap-1.5 px-3 py-1 rounded-full ${s.pill} text-label-caps font-bold uppercase tracking-wider">
          <span class="w-2 h-2 rounded-full ${s.dot} ${a.severity === "critical" ? "animate-ping" : ""}"></span>${s.label} ${c.label}</span>
        <a href="#/alert/${esc(a.alert_id)}" class="text-headline-sm hover:text-primary">${esc(lineName(a.line).replace(" - ", " • "))}${item.context?.machine ? " • " + esc(item.context.machine) : ""}</a>
        <span class="text-outline text-body-sm">•</span>
        <span class="inline-flex items-center gap-1 text-on-surface-variant text-label-sm bg-surface-container-low px-2.5 py-1 rounded-lg">
          <span class="material-symbols-outlined text-[16px] ${s.text}">timer</span>${item.starts_in_h != null ? (item.starts_in_h < 1 ? "Impact window: now" : `Impact window: starts in ${hrs(item.starts_in_h)}`) : "Active"}</span>
        <span class="inline-flex items-center gap-1 text-on-surface-variant text-label-sm bg-surface-container-low px-2.5 py-1 rounded-lg">${esc(a.alert_id)} · ${esc(n.likelihood || pct(item.probability))}</span>
      </div>
      <div class="flex items-baseline gap-2 ${a.severity === "critical" ? "bg-error-container/40" : "bg-surface-container-low"} px-4 py-2 rounded-xl self-start shrink-0">
        <span class="text-label-caps ${s.text} font-bold uppercase">Exposure:</span>
        <span class="text-headline-lg ${s.text} font-bold tabular-nums">${inr(a.expected_impact)}</span>
        <span class="text-body-sm ${s.text} font-medium">Potential Loss</span>
      </div>
    </div>
    <div class="my-space-md p-space-md md:p-space-lg rounded-xl bg-surface-container-low">
      <div class="flex items-start gap-space-md">
        <div class="p-2.5 rounded-xl bg-white shadow-sm ${s.text} shrink-0 hidden sm:block"><span class="material-symbols-outlined text-[24px]">${c.big}</span></div>
        <div>
          <span class="text-label-caps text-outline uppercase tracking-wider font-bold block mb-1">Predictive Diagnostics Summary</span>
          <p class="text-body-lg font-semibold mb-1">${esc(n.headline || a.headline)}</p>
          <p class="text-body-lg leading-relaxed">${esc(n.summary || "")}
          ${bits.length ? ` If left unaddressed this costs ${bits.map(([k, v]) => `<span class="font-semibold text-error">${inr(v)} in ${k}</span>`).join(" and ")}.` : ""}</p>
        </div>
      </div>
    </div>
    <div class="my-space-lg">
      <div class="flex items-center justify-between mb-space-sm">
        <span class="text-label-caps uppercase tracking-wider font-bold text-on-surface-variant text-[13px]">Recommended Action Checklist (Sequenced)</span>
        ${allDone ? `<span class="flex items-center gap-1 text-secondary text-label-sm"><span class="material-symbols-outlined text-[18px]">verified</span> All actions completed</span>`
      : `<span class="flex items-center gap-1 text-secondary text-label-sm"><span class="material-symbols-outlined text-[18px]">check_circle</span> Owner: ${esc(a.owner_name)}</span>`}
      </div>
      <div class="grid grid-cols-1 md:grid-cols-${Math.min(3, Math.max(1, actions.length))} gap-space-md">${actions.length ? actionCards(a.alert_id, actions) : `<div class="text-on-surface-variant text-sm">No playbook actions.</div>`}</div>
    </div>
    <div class="flex flex-wrap items-center justify-between gap-space-md pt-space-sm">
      <div class="flex flex-wrap items-center gap-space-sm">
        ${actions.length && actions.every(x => x.status !== "open") ? `<span class="flex items-center gap-2 px-5 py-2.5 rounded-lg bg-secondary/10 text-secondary font-semibold"><span class="material-symbols-outlined text-[18px]">check</span> Plan authorized · ${actions.filter(x => x.status === "done").length}/${actions.length} tasks done</span>`
      : isApprover() ? `<button data-dispatch="${esc(a.alert_id)}" class="flex items-center gap-2 px-5 py-2.5 rounded-lg bg-secondary hover:bg-[#005236] text-white font-semibold shadow-sm transition-colors"><span class="material-symbols-outlined text-[18px]">send</span> Authorize &amp; Dispatch Plan</button>`
      : `<span class="flex items-center gap-2 px-5 py-2.5 rounded-lg bg-slate-100 text-on-surface-variant font-semibold"><span class="material-symbols-outlined text-[18px]">hourglass_top</span> Waiting for ${esc(approverName())} to authorize</span>`}
        ${ackd ? `<span class="flex items-center gap-2 px-4 py-2.5 rounded-lg bg-surface-container-low text-on-surface-variant font-medium"><span class="material-symbols-outlined text-[18px] text-secondary">thumb_up</span> Acknowledged by ${esc(a.acknowledged_by)}</span>`
      : `<button data-ack="${esc(a.alert_id)}" class="flex items-center gap-2 px-4 py-2.5 rounded-lg bg-white border border-slate-100 shadow-sm hover:bg-surface-container-low font-medium transition-colors"><span class="material-symbols-outlined text-[18px]">thumb_up</span> Acknowledge Alert</button>`}
        <a href="${link.href}" class="flex items-center gap-2 px-4 py-2.5 rounded-lg bg-white border border-slate-100 shadow-sm hover:bg-surface-container-low font-medium transition-colors"><span class="material-symbols-outlined text-[18px]">${link.icon}</span> ${link.label}</a>
        <a href="#/alert/${esc(a.alert_id)}" class="flex items-center gap-2 px-4 py-2.5 rounded-lg text-primary hover:bg-surface-container-low font-medium transition-colors"><span class="material-symbols-outlined text-[18px]">open_in_new</span> Full Details</a>
      </div>
      <div class="flex items-center gap-2 text-on-surface-variant text-body-md">
        <span class="material-symbols-outlined text-[18px] ${ackd ? "text-secondary" : left < 0 ? "text-error" : "text-secondary"}">${ackd ? "verified_user" : "shield"}</span>
        ${ackd ? `Held by ${esc(a.holder_name || a.owner_name)}` : String(a.ack_deadline).startsWith("9999") ? `No response yet - with ${esc(a.holder_name)} (escalated ${a.level}x, reminders sent)` : left < 0 ? `Escalated to ${esc(a.holder_name)} (level ${a.level})` : `Acknowledge within ${fmtMins(left / 60000)} · ${esc(a.holder_name || a.owner_name)}`}
        ${isApprover() ? `<button data-resolve="${esc(a.alert_id)}" class="ml-2 px-3 py-1.5 rounded-lg bg-slate-100 hover:bg-slate-200 text-label-sm font-semibold">Resolve</button>` : ""}
      </div>
    </div>
  </div>`;
}

// ------------------------------------------------------------------ pages
const pages = {};

pages["control-room"] = async (el, q) => {
  const hz = q.get("h") || "72";
  const right = segmented([["72", "Next 72 Hours"], ["168", "Next 7 Days"], ["audit", "Historical Audit"]], hz, "data-hz");
  const sm = S.summary || {};
  const act = S.risks.filter(r => r.item.alert);
  const worst = Object.entries(sm.lines || {}).sort((a, b) => b[1].money_at_risk - a[1].money_at_risk)[0];
  const linesAtRisk = Object.values(sm.lines || {}).filter(l => l.status !== "ok").length;
  const flagged = S.risks.filter(r => r.item.category === "machine" && !((r.item.mitigation || {}).factor >= 1)).length;
  const c = sevCounts();
  const kpis = `
  <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-space-md mb-space-xl">
    <a href="#/horizon" class="bg-surface-container-lowest p-space-lg rounded-2xl shadow-sm hover:shadow-md flex flex-col justify-between">
      <div class="flex items-center justify-between mb-space-sm"><span class="text-label-caps text-on-surface-variant uppercase tracking-wider">Projected Risk (72h)</span>
        <span class="inline-flex items-center justify-center w-7 h-7 rounded-lg bg-error-container text-error"><span class="material-symbols-outlined text-[18px]">warning</span></span></div>
      <div><div class="flex items-baseline gap-2"><span class="text-headline-xl text-error font-bold tabular-nums">${lakh(sm.money_at_risk)}</span><span class="text-label-sm text-error font-semibold">${sm.critical || 0} Critical</span></div>
      <p class="text-body-sm text-on-surface-variant mt-1.5 leading-relaxed">${worst && worst[1].money_at_risk > 0 ? `Highest exposure on ${esc(worst[1].name)} (${lakh(worst[1].money_at_risk)}, ${worst[1].alerts} alert${worst[1].alerts === 1 ? "" : "s"}).` : "No material exposure across the plant."}</p>
      ${sm.money_protected > 0 ? `<p class="text-body-sm text-emerald-700 font-semibold mt-1">${lakh(sm.money_protected)} protected by actions taken (${sm.risks_acted_on} risk${sm.risks_acted_on === 1 ? "" : "s"})</p>` : ""}</div>
    </a>
    <a href="#/horizon" class="bg-surface-container-lowest p-space-lg rounded-2xl shadow-sm hover:shadow-md flex flex-col justify-between">
      <div class="flex items-center justify-between mb-space-sm"><span class="text-label-caps text-on-surface-variant uppercase tracking-wider">Active Alert Queue</span>
        <span class="inline-flex items-center justify-center w-7 h-7 rounded-lg bg-surface-container-high text-primary"><span class="material-symbols-outlined text-[18px]">notifications_active</span></span></div>
      <div><div class="flex items-center gap-space-sm text-label-numeric tabular-nums mb-1">
        <span class="inline-flex items-center gap-1.5 text-error"><span class="w-2.5 h-2.5 rounded-full bg-error animate-pulse"></span>${c.critical}</span><span class="text-outline-variant font-normal">/</span>
        <span class="inline-flex items-center gap-1.5 text-tertiary"><span class="w-2.5 h-2.5 rounded-full bg-tertiary-fixed-dim"></span>${c.warning}</span><span class="text-outline-variant font-normal">/</span>
        <span class="inline-flex items-center gap-1.5 text-primary"><span class="w-2.5 h-2.5 rounded-full bg-primary-container"></span>${c.normal}</span></div>
      <div class="flex items-center gap-2 text-on-surface-variant text-body-sm">Critical • Warning • Normal Safe</div></div>
    </a>
    <a href="#/orders?late=1" class="bg-surface-container-lowest p-space-lg rounded-2xl shadow-sm hover:shadow-md flex flex-col justify-between">
      <div class="flex items-center justify-between mb-space-sm"><span class="text-label-caps text-on-surface-variant uppercase tracking-wider">Orders At Risk</span>
        <span class="inline-flex items-center justify-center w-7 h-7 rounded-lg bg-amber-100 text-amber-700"><span class="material-symbols-outlined text-[18px]">assignment_late</span></span></div>
      <div><span class="text-headline-xl font-bold tabular-nums">${sm.orders_at_risk ?? 0}</span>
      <p class="text-body-sm text-on-surface-variant mt-1.5">Customer orders projected to ship late because of active risks.</p></div>
    </a>
    <a href="#/machines" class="bg-surface-container-lowest p-space-lg rounded-2xl shadow-sm hover:shadow-md flex flex-col justify-between">
      <div class="flex items-center justify-between mb-space-sm"><span class="text-label-caps text-on-surface-variant uppercase tracking-wider">Fleet Status</span>
        <span class="inline-flex items-center justify-center w-7 h-7 rounded-lg bg-secondary/10 text-secondary"><span class="material-symbols-outlined text-[18px]">precision_manufacturing</span></span></div>
      <div><div class="flex items-baseline gap-2"><span class="text-headline-xl font-bold tabular-nums">${flagged}</span><span class="text-label-sm text-on-surface-variant">machines flagged</span></div>
      <p class="text-body-sm text-on-surface-variant mt-1.5">${linesAtRisk} of ${S.cfg.lines.length} lines carry an active alert.</p></div>
    </a>
  </div>`;

  const lanes = `
  <div class="bg-surface-container-lowest rounded-2xl shadow-sm p-space-lg mb-space-xl">
    <div class="flex items-center justify-between mb-space-md"><span class="text-label-caps uppercase tracking-wider font-bold text-on-surface-variant">Line Health</span><a href="#/horizon" class="text-primary text-label-sm font-semibold">Open Event Horizon →</a></div>
    <div class="grid grid-cols-1 md:grid-cols-5 gap-space-sm">
    ${S.cfg.lines.map(l => { const d = (sm.lines || {})[l.id] || { status: "ok", alerts: 0, money_at_risk: 0 }; const s = sev(d.status); return `
      <a href="#/horizon?line=${l.id}" class="p-space-md rounded-xl border border-slate-100 hover:shadow-md transition-shadow relative overflow-hidden">
        <div class="absolute left-0 top-0 bottom-0 w-1 ${s.bar}"></div>
        <div class="text-label-caps uppercase text-outline">${l.id}</div>
        <div class="font-semibold">${esc(l.name.replace(/^Line \d+ - /, ""))}</div>
        <div class="flex items-center justify-between mt-2"><span class="px-2 py-0.5 rounded-full ${s.pill} text-[11px] font-bold">${d.status === "ok" ? "Normal" : s.label}</span><span class="text-body-sm tabular-nums ${d.money_at_risk ? s.text : "text-outline"}">${d.money_at_risk ? lakh(d.money_at_risk) : "—"}</span></div>
      </a>`; }).join("")}
    </div>
  </div>`;

  let body;
  if (hz === "audit") { location.hash = "#/alert-history"; return; }
  {
    const lim = +hz;
    const list = S.alerts.filter(a => { const r = riskFor(a.key); return !r || (r.item.starts_in_h ?? 0) <= lim; });
    const details = await Promise.all(list.slice(0, 12).map(a => api(`/alerts/${a.alert_id}`).catch(() => null)));
    body = list.length ? `<div class="space-y-space-lg">${list.slice(0, 12).map((a, i) => alertCard(a, details[i])).join("")}</div>
      ${list.length > 12 ? `<div class="text-center mt-space-lg"><a href="#/horizon" class="text-primary font-semibold">View all ${list.length} alerts →</a></div>` : ""}`
      : card(empty(`No open alerts in the next ${lim === 72 ? "72 hours" : "7 days"}.`));
  }
  el.innerHTML = pageHeader({ tag: `${S.cfg.company.site} Continuous Intelligence`, title: "Alerts & Recommendations", sub: "Predictive disruption intelligence, real-time loss exposure, and pre-verified mitigation playbooks.", right }) + kpis + lanes + body;
  $$("[data-hz]", el).forEach(b => b.onclick = () => { location.hash = `#/control-room?h=${b.dataset.hz}`; });
  bindWorkflow(el);
  void act;
};

pages.horizon = async (el, q) => {
  const f = { severity: q.get("severity") || "", category: q.get("category") || "", line: q.get("line") || "", q: q.get("q") || "", watch: q.get("watch") === "1", span: +(q.get("span") || 72) };
  const text = x => `${x.item.title} ${x.narration.headline} ${x.item.line} ${x.item.line_name} ${x.item.key} ${x.item.category} ${JSON.stringify(x.item.context)}`.toLowerCase();
  const rows = S.risks.filter(r => {
    const s = r.item.severity;
    if (!f.watch && !r.item.alert) return false;
    if (f.severity === "warning" ? !["high", "medium"].includes(s) : f.severity === "normal" ? (r.item.alert && s !== "low") : f.severity && s !== f.severity) return false;
    if (f.category && r.item.category !== f.category) return false;
    if (f.line && r.item.line !== f.line) return false;
    if (f.q && !text(r).includes(f.q.toLowerCase())) return false;
    return true;
  });
  const set = (k, v) => { const p = new URLSearchParams(q); v ? p.set(k, v) : p.delete(k); location.hash = `#/horizon?${p}`; };
  const W = f.span;
  const gantt = S.cfg.lines.map(l => {
    const lr = rows.filter(r => r.item.line === l.id);
    const bars = lr.map((r, i) => {
      const s0 = Math.min(W, r.item.starts_in_h || 0), e0 = Math.min(W, r.item.ends_in_h ?? W);
      const left = s0 / W * 100, width = Math.max(1.5, (Math.max(e0, s0 + 1) - s0) / W * 100);
      const a = alertFor(r.item.key);
      return `<a href="${a ? `#/alert/${a.alert_id}` : `#/risk/${encodeURIComponent(r.item.key)}`}" title="${esc(r.item.title)} — ${inr(r.item.expected_impact)}"
        class="absolute h-5 rounded-md text-[10px] text-white font-semibold px-1.5 truncate leading-5 hover:ring-2 ring-offset-1 ring-primary ${r.item.category === "compound" ? "ring-2 ring-error" : ""}"
        style="left:${left}%;width:${Math.min(width, 100 - left)}%;top:${4 + (i % 4) * 24}px;background:${cat(r.item.category).color};opacity:${r.item.alert ? 1 : .55}">${esc(cat(r.item.category).label)} · ${lakh(r.item.expected_impact)}</a>`;
    }).join("");
    const hgt = Math.max(32, Math.min(4, lr.length) * 24 + 8);
    return `<div class="flex items-stretch border-t border-slate-100"><div class="w-40 shrink-0 py-2 pr-2 text-sm font-medium">${esc(l.name.replace(" - ", " · "))}</div><div class="relative flex-1" style="height:${hgt}px">${bars}</div></div>`;
  }).join("");
  const ticks = [0, .25, .5, .75, 1].map(t => `<span class="absolute text-[10px] text-outline" style="left:${t * 100}%;transform:translateX(${t === 1 ? "-100%" : t ? "-50%" : "0"})">${t === 0 ? "now" : "+" + Math.round(W * t) + "h"}</span>`).join("");
  el.innerHTML = pageHeader({ tag: "Event Horizon", title: "Disruption Timeline", sub: "Every predicted risk on a shared clock — overlapping bars on one line compound into bigger losses.", right: segmented([["72", "72 Hours"], ["168", "7 Days"]], String(W), "data-span") }) + `
  <div class="bg-surface-container-lowest rounded-2xl shadow-sm p-space-lg mb-space-lg">
    <div class="flex flex-wrap gap-space-md items-end mb-space-md">
      ${select("fSev", [["", "All severities"], ["critical", "Critical"], ["high", "High"], ["medium", "Medium"], ["low", "Low"], ["warning", "Warning (high+medium)"], ["normal", "Normal / watch"]], f.severity, "Severity")}
      ${select("fCat", [["", "All categories"], ...Object.entries(CAT).map(([k, v]) => [k, v.label])], f.category, "Category")}
      ${select("fLine", lineOpts(), f.line, "Line")}
      <label class="flex flex-col text-label-caps uppercase text-outline gap-1">Search<input id="fQ" value="${esc(f.q)}" class="rounded-xl border-slate-200 text-sm normal-case tracking-normal text-on-surface font-normal py-1.5" placeholder="machine, customer, material…"></label>
      <label class="flex items-center gap-2 text-sm pb-2 cursor-pointer"><input id="fWatch" type="checkbox" class="rounded" ${f.watch ? "checked" : ""}> Include watch list (below alert threshold)</label>
      <button id="fClear" class="px-3 py-2 rounded-xl bg-slate-100 hover:bg-slate-200 text-sm font-semibold">Clear</button>
    </div>
    <div class="flex"><div class="w-40 shrink-0"></div><div class="relative flex-1 h-4">${ticks}</div></div>
    ${gantt}
    <div class="flex flex-wrap gap-3 mt-space-md">${Object.entries(CAT).map(([, v]) => `<span class="flex items-center gap-1.5 text-body-sm text-on-surface-variant"><span class="w-3 h-3 rounded" style="background:${v.color}"></span>${v.label}</span>`).join("")}</div>
  </div>` + card(rows.length ? `<div class="overflow-auto"><table class="data w-full"><thead><tr><th>Severity</th><th>Risk</th><th>Line</th><th>Starts in</th><th>Likelihood</th><th>Exposure</th><th>Owner</th><th>Status</th></tr></thead><tbody>
    ${rows.map(r => { const a = alertFor(r.item.key); return `<tr class="click" data-href="${a ? `#/alert/${a.alert_id}` : `#/risk/${encodeURIComponent(r.item.key)}`}">
      <td>${sevPill(r.item.severity)}</td>
      <td><div class="flex items-center gap-2"><span class="material-symbols-outlined text-[18px]" style="color:${cat(r.item.category).color}">${cat(r.item.category).icon}</span><span class="font-medium">${esc(r.narration.headline)}</span></div><div class="text-body-sm text-outline ml-7">${esc(r.item.title)}</div></td>
      <td>${esc(r.item.line)}</td><td class="tabular-nums">${hrs(r.item.starts_in_h)}</td><td class="tabular-nums">${pct(r.item.probability)}</td>
      <td class="tabular-nums font-semibold ${sev(r.item.severity).text}">${inr(r.item.expected_impact)}</td>
      <td>${esc(a?.holder_name || a?.owner_name || S.cfg.roles[r.item.owner_role])}</td>
      <td>${a ? `<span class="px-2 py-0.5 rounded-full text-[11px] font-bold ${a.status === "open" ? "bg-error-container text-error" : "bg-secondary/10 text-secondary"}">${a.status === "open" ? "Unacknowledged" : "Acknowledged"}</span>` : `<span class="text-outline text-body-sm">Watch list</span>`}</td></tr>`; }).join("")}
    </tbody></table></div>` : empty("No risks match these filters."), "p-0 overflow-hidden");
  $("#fSev").onchange = e => set("severity", e.target.value);
  $("#fCat").onchange = e => set("category", e.target.value);
  $("#fLine").onchange = e => set("line", e.target.value);
  $("#fQ").onchange = e => set("q", e.target.value.trim());
  $("#fWatch").onchange = e => set("watch", e.target.checked ? "1" : "");
  $("#fClear").onclick = () => { location.hash = "#/horizon"; };
  $$("[data-span]", el).forEach(b => b.onclick = () => set("span", b.dataset.span === "72" ? "" : b.dataset.span));
};

// ---- alert / risk detail
function evidenceBlock(item) {
  const e = item.evidence || {}, c = item.context || {};
  const kv = (k, v) => `<div class="flex justify-between gap-4 py-1.5 border-b border-slate-50 text-sm"><span class="text-on-surface-variant">${esc(k)}</span><span class="font-medium text-right">${v}</span></div>`;
  let h = "";
  if (item.category === "machine") {
    h += kv("Machine", `<a class="text-primary" href="#/machines/${esc(c.machine)}">${esc(c.machine)}</a> (${esc(cap(c.station))})`) + kv("Likely component", esc(e.likely_component)) +
      kv("Failure probability", pct(e.failure_probability)) + kv("Expected within", `${e.expected_within_h}h`) + kv("Repair time", `${e.repair_hours}h`) +
      kv("Line stops", String(e.line_stops) === "True" || e.line_stops === true ? "Yes — no backup machine" : `No — ${pct(1 - e.capacity_loss_share)} speed`) +
      kv("Maintenance tech short", e.maintenance_tech_short ? "Yes" : "No");
    if (e.sensor_trends) h += `<div class="mt-3 text-label-caps uppercase text-outline">24h sensor change</div>` + Object.entries(e.sensor_trends).map(([s, v]) => kv(S.cfg.sensors[s]?.label || s, `<span class="${Math.abs(v) >= .5 ? "text-error" : ""}">${v > 0 ? "+" : ""}${v} ${esc(S.cfg.sensors[s]?.unit || "")}</span>`)).join("");
    if (e.alarms_24h && Object.keys(e.alarms_24h).length) h += `<div class="mt-3 text-label-caps uppercase text-outline">Alarms (24h)</div>` + Object.entries(e.alarms_24h).map(([k, v]) => kv(k, v)).join("");
    if (e.similar_past_cases?.length) h += `<div class="mt-3 text-label-caps uppercase text-outline mb-1">Similar past breakdowns</div>` + e.similar_past_cases.map(x => `<blockquote class="text-body-sm bg-surface-container-low rounded-lg p-2 mb-1.5">${esc(x)}</blockquote>`).join("");
  } else if (item.category === "material") {
    h += kv("Material", esc(e.material)) + kv("Supplier", esc(e.supplier)) + kv("Stock", `${num(e.stock_units)} ${esc(e.unit || "")}`) + kv("Days of cover", e.days_of_cover) +
      kv("Stock-out in", hrs(e.stockout_in_h)) + kv("Starved for", `${e.stockout_hours}h`) + kv("Lines using it", esc((e.lines_using_it || []).join(", "))) +
      kv("Supplier delay rate", pct(e.supplier_delay_rate));
    if (e.open_purchase_orders?.length) h += `<div class="mt-3 text-label-caps uppercase text-outline">Open purchase orders</div>` + e.open_purchase_orders.map(p => kv(p.po, `${num(p.qty)} · ETA ${esc(p.eta)} ${p.revised ? `<span class="text-error">(revised)</span>` : ""}`)).join("");
  } else if (item.category === "quality") {
    h += kv("Metric", `${esc(e.metric)} (${esc(e.unit)})`) + kv("Station", esc(e.station)) + kv("Last shift out of spec", esc(e.last_shift_out_of_spec)) + kv("Normal out of spec", esc(e.normal_out_of_spec)) +
      kv("Projected out of spec", esc(e.projected_out_of_spec)) + kv("Trend per shift", e.trend_per_shift) + kv("SPC signal", e.spc_signal ? "Yes" : "No");
  } else if (item.category === "workforce") {
    h += kv("Role", esc(cap(String(e.role).replace(/_/g, " ")))) + kv("Certified role", e.certified ? "Yes" : "No") + kv("Speed when short", esc(e.speed_when_short)) + kv("Notice given", esc(e.notice_given));
    (e.shifts || []).forEach(s => { h += kv("Shift", esc(s)); });
  } else {
    Object.entries(e).filter(([, v]) => typeof v !== "object").forEach(([k, v]) => { h += kv(cap(k.replace(/_/g, " ")), esc(typeof v === "number" ? num(v) : v)); });
  }
  const orders = e.orders || e.orders_made_late || [];
  if (orders.length) h += `<div class="mt-4 text-label-caps uppercase text-outline mb-1">Orders affected</div><div class="overflow-auto"><table class="data w-full"><thead><tr><th>Order</th><th>Customer</th><th>Due</th><th>Days late</th><th>Penalty</th></tr></thead><tbody>
    ${orders.map(o => `<tr><td><a class="text-primary" href="#/orders?q=${esc(o.order)}">${esc(o.order)}</a></td><td>${esc(o.customer)} ${o.tier ? `<span class="text-outline">(${esc(o.tier)})</span>` : ""}</td><td>${esc(o.due)}</td><td>${o.days_late ?? o.extra_days_late ?? "—"}</td><td class="tabular-nums">${inr(o.penalty ?? o.extra_penalty)}</td></tr>`).join("")}</tbody></table></div>`;
  return h || `<div class="text-outline text-sm">No additional evidence.</div>`;
}

function impactBars(imp) {
  const parts = [["Lost output", imp.lost_margin, "#2563eb"], ["Scrap", imp.scrap, "#d97706"], ["Repair", imp.repair, "#7c3aed"], ["Late penalties", imp.late_penalty, "#ba1a1a"]];
  const tot = parts.reduce((s, p) => s + (p[1] || 0), 0) || 1;
  return `<div class="flex h-4 rounded-full overflow-hidden bg-slate-100">${parts.map(p => p[1] ? `<div style="width:${p[1] / tot * 100}%;background:${p[2]}" title="${p[0]}: ${inr(p[1])}"></div>` : "").join("")}</div>
  <div class="grid grid-cols-2 gap-2 mt-3">${parts.map(p => `<div class="flex items-center justify-between text-sm"><span class="flex items-center gap-1.5"><span class="w-2.5 h-2.5 rounded-sm" style="background:${p[2]}"></span>${p[0]}</span><span class="tabular-nums font-medium">${inr(p[1] || 0)}</span></div>`).join("")}</div>
  <div class="flex justify-between border-t border-slate-100 mt-3 pt-2 font-semibold"><span>Total expected impact</span><span class="tabular-nums">${inr(imp.total)}</span></div>`;
}

function whatifBlock(item) {
  const keys = item.category === "compound" ? item.related : ["machine", "material", "workforce", "quality"].includes(item.category) ? [item.key] : [];
  if (!keys.length) return "";
  return card(`<div class="text-label-caps uppercase tracking-wider font-bold text-on-surface-variant mb-2">What-if: prevent this risk</div>
    <p class="text-body-sm text-on-surface-variant mb-3">Re-prices the line with this risk reduced, including knock-on late-order penalties.</p>
    <div class="flex items-center gap-3"><input id="wiMit" type="range" min="0" max="100" step="10" value="100" class="flex-1"><span id="wiVal" class="w-12 text-right tabular-nums font-semibold">100%</span>
    <button id="wiRun" data-keys="${esc(JSON.stringify(keys))}" class="px-3 py-1.5 rounded-lg bg-primary-container text-white text-label-sm font-semibold">Simulate</button></div>
    <div id="wiOut" class="mt-3"></div>`);
}
function bindWhatif(el) {
  const b = $("#wiRun", el); if (!b) return;
  $("#wiMit", el).oninput = e => { $("#wiVal", el).textContent = e.target.value + "%"; };
  b.onclick = () => guarded(b, async () => {
    const r = await post("/whatif", { keys: JSON.parse(b.dataset.keys), mitigation: +$("#wiMit", el).value / 100 });
    const rows = Object.entries(r.lines || {});
    $("#wiOut", el).innerHTML = rows.length ? rows.map(([l, v]) => `<div class="grid grid-cols-4 gap-2 text-sm py-1.5 border-b border-slate-50"><span class="font-medium">${esc(l)}</span><span>Before ${inr(v.before)}</span><span>After ${inr(v.after)}</span><span class="text-secondary font-semibold">Saves ${inr(v.saved)}</span></div>`).join("") + `<div class="text-right font-semibold text-secondary mt-2">Total saved: ${inr(r.saved)}</div>`
      : `<pre class="text-body-sm">${esc(JSON.stringify(r, null, 1))}</pre>`;
  });
}

async function renderDetail(el, { item, narration: n, alert: a, actions = [], history = [], notifications = [] }) {
  const s = sev(item.severity || a?.severity), c = cat(item.category);
  const steps = ["open", "acknowledged", "resolved"], cur = a ? (a.status === "auto_resolved" ? 2 : steps.indexOf(a.status)) : -1;
  const closed = a && ["resolved", "auto_resolved"].includes(a.status);
  const left = a ? parseTs(a.ack_deadline) - parseTs(S.now) : 0;
  el.innerHTML = `
  <a href="javascript:history.back()" class="inline-flex items-center gap-1 text-primary text-sm font-semibold mb-space-md"><span class="material-symbols-outlined text-[18px]">arrow_back</span> Back</a>
  <div class="bg-surface-container-lowest rounded-2xl shadow-md p-space-lg md:p-space-xl relative overflow-hidden mb-space-lg">
    <div class="absolute left-0 top-0 bottom-0 w-2 ${s.bar}"></div>
    <div class="flex flex-col lg:flex-row justify-between gap-space-md">
      <div>
        <div class="flex flex-wrap items-center gap-2 mb-2">${sevPill(item.severity || a.severity)}
          <span class="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full bg-surface-container-low text-label-sm"><span class="material-symbols-outlined text-[16px]" style="color:${c.color}">${c.icon}</span>${c.label}</span>
          <span class="text-label-sm text-outline">${a ? esc(a.alert_id) + " · " : "Watch list · "}${esc(item.key)}</span></div>
        <h1 class="text-headline-lg">${esc(n.headline)}</h1>
        <div class="text-on-surface-variant mt-1">${esc(lineName(item.line))} · ${esc(item.title)}</div>
      </div>
      <div class="flex flex-col items-start lg:items-end gap-2 shrink-0">
        <div class="flex items-baseline gap-2 bg-error-container/40 px-4 py-2 rounded-xl"><span class="text-label-caps ${s.text} font-bold uppercase">Exposure:</span><span class="text-headline-lg ${s.text} font-bold tabular-nums">${inr(item.expected_impact ?? a?.expected_impact)}</span></div>
        <div class="text-body-sm text-on-surface-variant">${esc(n.likelihood || "")} · starts ${item.starts_in_h != null ? "in " + hrs(item.starts_in_h) : "—"}${item.ends_in_h != null ? ` · ends in ${hrs(item.ends_in_h)}` : ""}</div>
      </div>
    </div>
    ${a ? `<div class="flex flex-wrap items-center justify-between gap-space-md mt-space-lg pt-space-md border-t border-slate-100">
      <div class="flex flex-wrap items-center gap-2">${["New", "Acknowledged", "Resolved"].map((l, i) => `<span class="flex items-center gap-1.5 text-label-sm font-semibold ${i <= cur ? "text-secondary" : "text-outline"}"><span class="w-6 h-6 rounded-full flex items-center justify-center ${i <= cur ? "bg-secondary text-white" : "bg-slate-100"}">${i < cur || (i === cur && closed) ? `<span class="material-symbols-outlined text-[14px]">check</span>` : i + 1}</span>${l}</span>${i < 2 ? `<span class="w-8 h-px bg-slate-200"></span>` : ""}`).join("")}
        ${!closed && a.status === "open" ? `<span class="ml-3 text-label-sm ${left < 0 ? "text-error" : "text-on-surface-variant"}">${left < 0 ? `Escalated to ${esc(a.holder_name)} (level ${a.level})` : `${Math.round(left / 60000)} min left before escalation`}</span>` : ""}</div>
      <div class="flex flex-wrap gap-2">
        ${!closed && a.status === "open" ? `<button data-ack="${esc(a.alert_id)}" class="flex items-center gap-2 px-4 py-2 rounded-lg bg-white border border-slate-200 hover:bg-surface-container-low font-medium"><span class="material-symbols-outlined text-[18px]">thumb_up</span>Acknowledge</button>` : ""}
        ${!closed && isApprover() && actions.some(x => x.status === "open") ? `<button data-dispatch="${esc(a.alert_id)}" class="flex items-center gap-2 px-4 py-2 rounded-lg bg-secondary text-white font-semibold"><span class="material-symbols-outlined text-[18px]">send</span>Authorize &amp; Dispatch Plan</button>`
          : !closed && actions.some(x => x.status === "open") ? `<span class="px-3 py-2 rounded-lg bg-slate-100 text-on-surface-variant text-sm font-semibold">Waiting for ${esc(approverName())} to authorize</span>` : ""}
        <button data-comment="${esc(a.alert_id)}" class="flex items-center gap-2 px-4 py-2 rounded-lg bg-white border border-slate-200 hover:bg-surface-container-low font-medium"><span class="material-symbols-outlined text-[18px]">chat</span>Comment</button>
        ${!closed ? (isApprover() ? `<button data-resolve="${esc(a.alert_id)}" class="flex items-center gap-2 px-4 py-2 rounded-lg bg-primary-container text-white font-semibold"><span class="material-symbols-outlined text-[18px]">task_alt</span>Resolve</button>` : "") : `<span class="px-3 py-2 rounded-lg bg-secondary/10 text-secondary text-sm font-semibold">${a.status === "auto_resolved" ? "Auto-resolved: condition cleared" : `Resolved by ${esc(a.resolved_by)} — ${esc(a.resolution)}`}</span>`}
      </div></div>` : ""}
  </div>
  <div class="grid grid-cols-1 xl:grid-cols-3 gap-space-lg">
    <div class="min-w-0 xl:col-span-2 space-y-space-lg">
      ${card(`<div class="text-label-caps uppercase tracking-wider font-bold text-outline mb-2">What will happen</div><p class="text-body-lg leading-relaxed">${esc(n.summary)}</p><p class="text-body-md text-on-surface-variant mt-2">${esc(n.cost || "")}</p>
        ${n.why?.length ? `<div class="text-label-caps uppercase tracking-wider font-bold text-outline mt-space-lg mb-2">Why we think so</div><ul class="space-y-2">${n.why.map(w => `<li class="flex gap-2"><span class="material-symbols-outlined text-[18px] text-primary">insights</span><span>${esc(w)}</span></li>`).join("")}</ul>` : ""}`)}
      ${card(`<div class="text-label-caps uppercase tracking-wider font-bold text-outline mb-3">Recommended action checklist</div>
        ${a ? `<div class="grid grid-cols-1 md:grid-cols-2 gap-space-md">${actionCards(a.alert_id, actions)}</div>` : (n.actions || []).map((x, i) => `<div class="flex gap-3 py-2 border-b border-slate-50"><span class="w-6 h-6 rounded-full bg-surface-container-high text-primary flex items-center justify-center text-label-sm font-bold">${i + 1}</span><div><div class="font-medium">${esc(x.text)}</div><div class="text-body-sm text-on-surface-variant">${esc(x.owner_name)} · ${esc(x.owner_title)} · ${esc(x.due_text)}</div></div></div>`).join("") + `<p class="text-body-sm text-outline mt-2">Below the alert threshold — no alert was raised, so actions are advisory.</p>`}`)}
      ${card(`<div class="text-label-caps uppercase tracking-wider font-bold text-outline mb-2">Evidence</div>${evidenceBlock(item)}`)}
    </div>
    <div class="space-y-space-lg">
      ${card(`<div class="text-label-caps uppercase tracking-wider font-bold text-outline mb-3">What it costs</div>${impactBars(item.impact || { total: a?.expected_impact })}`)}
      ${!closed && riskFor(item.key) ? whatifBlock(item) : ""}
      ${a ? card(`<div class="text-label-caps uppercase tracking-wider font-bold text-outline mb-3">Activity</div>
        <ol class="relative border-l border-slate-200 ml-2 space-y-3">${history.map(h => `<li class="ml-4"><span class="absolute -left-1.5 w-3 h-3 rounded-full bg-primary-container ring-2 ring-white"></span><div class="text-sm"><b>${esc(cap(h.type.replace(/_/g, " ")))}</b> · ${esc(h.actor)}</div><div class="text-body-sm text-on-surface-variant">${esc(h.detail)}</div><div class="text-body-sm text-outline">${fmtTs(h.ts)}</div></li>`).join("")}</ol>`) : ""}
      ${a && notifications.length ? card(`<div class="text-label-caps uppercase tracking-wider font-bold text-outline mb-3">Notifications sent</div>${notifications.map(x => `<div class="py-2 border-b border-slate-50"><div class="text-sm font-medium">${esc(x.to_name)} <span class="text-outline font-normal">(${esc(x.channel)} · ${esc(x.status)})</span></div><div class="text-body-sm text-on-surface-variant">${esc(x.subject)}</div><div class="text-body-sm text-outline">${fmtTs(x.ts)}</div></div>`).join("")}`) : ""}
    </div>
  </div>`;
  bindWorkflow(el); bindWhatif(el);
}

pages.alert = async (el, q, id) => {
  const d = await api(`/alerts/${encodeURIComponent(id)}`);
  const live = riskFor(d.alert.key);
  await renderDetail(el, { ...d, item: live ? live.item : d.item, narration: live ? live.narration : d.narration });
};
pages.risk = async (el, q, key) => {
  const k = decodeURIComponent(key);
  const a = alertFor(k);
  if (a) { location.replace(`#/alert/${a.alert_id}`); return; }
  const d = await api(`/risks/${encodeURIComponent(k)}`);
  await renderDetail(el, { item: d.item, narration: d.narration });
};

// ---- playbook
pages.playbook = async (el, q) => {
  const f = q.get("f") || "mine", owner = q.get("owner") || "";
  const all = await api("/alerts?status=open,acknowledged");
  const det = await Promise.all(all.map(a => api(`/alerts/${a.alert_id}`).catch(() => null)));
  let acts = [];
  det.forEach((d, i) => d && d.actions.forEach(x => acts.push({ ...x, alert: all[i] })));
  const total = acts.length, doneN = acts.filter(x => x.status === "done").length;
  acts = acts.filter(x => (f === "mine" ? x.owner_name === S.user.name && x.status !== "done" : f === "pending" ? x.status !== "done" : f === "done" ? x.status === "done" : true) && (!owner || x.owner_name === owner));
  acts.sort((x, y) => (x.status === "done") - (y.status === "done") || SEV_ORDER.indexOf(x.alert.severity) - SEV_ORDER.indexOf(y.alert.severity) || String(x.due_by).localeCompare(String(y.due_by)));
  const set = (k, v) => { const p = new URLSearchParams(q); v ? p.set(k, v) : p.delete(k); location.hash = `#/playbook?${p}`; };
  el.innerHTML = pageHeader({ tag: "Playbook Cascade", title: "Action Playbook", sub: "Every recommended action across open alerts, routed to the person who owns it.", right: segmented([["mine", "My Actions"], ["pending", "All Pending"], ["done", "Completed"], ["all", "Everything"]], f, "data-f") }) + `
  <div class="grid grid-cols-1 md:grid-cols-3 gap-space-md mb-space-lg">
    ${card(`<div class="text-label-caps uppercase text-on-surface-variant">Actions on open alerts</div><div class="text-headline-xl tabular-nums">${total}</div>`)}
    ${card(`<div class="text-label-caps uppercase text-on-surface-variant">Completed</div><div class="text-headline-xl tabular-nums text-secondary">${doneN}</div><div class="h-2 rounded-full bg-slate-100 mt-2"><div class="h-2 rounded-full bg-secondary" style="width:${total ? doneN / total * 100 : 0}%"></div></div>`)}
    ${card(`<div class="text-label-caps uppercase text-on-surface-variant">Assigned to ${esc(S.user.name)}</div><div class="text-headline-xl tabular-nums text-primary">${det.flatMap(d => d ? d.actions : []).filter(x => x.owner_name === S.user.name && x.status !== "done").length}</div><div class="text-body-sm text-on-surface-variant">pending</div>`)}
  </div>
  <div class="flex items-end gap-space-md mb-space-md">${select("fOwner", [["", "All owners"], ...S.cfg.people.map(p => [p.name, `${p.name} — ${S.cfg.roles[p.role]}`])], owner, "Owner")}</div>` +
    card(acts.length ? `<div class="overflow-auto"><table class="data w-full"><thead><tr><th></th><th>Action</th><th>Owner</th><th>Alert</th><th>Due</th><th>Status</th><th></th></tr></thead><tbody>
    ${acts.map(x => { const overdue = x.status !== "done" && parseTs(x.due_by) < parseTs(S.now); return `<tr>
      <td>${sevPill(x.alert.severity)}</td><td class="font-medium max-w-xl">${esc(x.text)}</td>
      <td>${esc(x.owner_name)}<div class="text-body-sm text-outline">${esc(S.cfg.roles[x.owner_role] || x.owner_role)}</div></td>
      <td><a class="text-primary" href="#/alert/${esc(x.alert.alert_id)}">${esc(x.alert.alert_id)}</a><div class="text-body-sm text-outline max-w-xs truncate">${esc(x.alert.headline)}</div></td>
      <td class="${overdue ? "text-error font-semibold" : ""}">${fmtTs(x.due_by, false)}${overdue ? "<div class='text-body-sm'>Overdue</div>" : ""}</td>
      <td>${x.status === "done" ? `<span class="text-secondary font-semibold">Done</span><div class="text-body-sm text-outline">${esc(x.done_by)}</div>`
        : x.status === "dispatched" ? `<span class="text-primary font-semibold">Dispatched</span><div class="text-body-sm text-outline">to do</div>`
        : `<span class="text-on-surface-variant font-semibold">Waiting for approval</span><div class="text-body-sm text-outline">${esc(approverName())}</div>`}</td>
      <td>${x.status === "dispatched" && x.owner_name === S.user.name ? `<button data-done="${esc(x.alert.alert_id)}|${x.idx}" class="px-3 py-1.5 rounded-lg bg-secondary text-white text-label-sm font-semibold whitespace-nowrap">Mark done</button>` : ""}</td></tr>`; }).join("")}
    </tbody></table></div>` : empty(f === "mine" ? "Nothing assigned to you right now." : "No actions match."), "p-0 overflow-hidden");
  $$("[data-f]", el).forEach(b => b.onclick = () => set("f", b.dataset.f));
  $("#fOwner").onchange = e => set("owner", e.target.value);
  bindWorkflow(el);
};

// ---- machines
pages.machines = async (el, q, id) => {
  const ms = await api("/ui/machines");
  const stationsOrder = [...new Set(ms.map(m => m.station))];
  const color = p => p == null ? "#e2e8f0" : p >= .6 ? "#ba1a1a" : p >= .35 ? "#f97316" : p >= .15 ? "#fbbf24" : "#10b981";
  const grid = `<div class="overflow-auto"><table class="w-full text-sm"><thead><tr><th class="text-left text-label-caps uppercase text-outline p-2">Line</th>${stationsOrder.map(s => `<th class="text-label-caps uppercase text-outline p-2 font-semibold">${esc(s)}</th>`).join("")}</tr></thead><tbody>
    ${S.cfg.lines.map(l => `<tr><td class="p-2 font-medium whitespace-nowrap">${esc(l.id)} · ${esc(lineShort(l.id))}</td>${stationsOrder.map(st => `<td class="p-1.5 text-center"><div class="flex gap-1 justify-center">${ms.filter(m => m.line === l.id && m.station === st).map(m => `
      <a href="#/machines/${esc(m.machine)}" title="${esc(m.machine)} — ${m.probability != null ? pct(m.probability) + " failure risk" : "no data"}${m.component ? " (" + esc(m.component) + ")" : ""}${m.fix_note ? " · " + esc(m.fix_note) : ""}"
        class="w-7 h-7 rounded-md flex items-center justify-center text-[9px] font-bold text-white hover:scale-110 transition-transform ${m.machine === id ? "ring-2 ring-primary ring-offset-2" : ""}" style="background:${color(m.probability)}">${m.fixed ? "✓" : m.flagged ? "!" : esc(m.machine.slice(-2))}</a>`).join("")}</div></td>`).join("")}</tr>`).join("")}
  </tbody></table></div>
  <div class="flex flex-wrap gap-4 mt-3 text-body-sm text-on-surface-variant">${[["#10b981", "< 15%"], ["#fbbf24", "15–35%"], ["#f97316", "35–60%"], ["#ba1a1a", "≥ 60%"]].map(([c, l]) => `<span class="flex items-center gap-1.5"><span class="w-3 h-3 rounded" style="background:${c}"></span>${l} failure probability (48h)</span>`).join("")}<span>“!” = flagged by the AI model</span><span>“✓” = repaired after an alert</span></div>`;
  const top = ms.filter(m => m.probability != null && !m.fixed).sort((a, b) => b.probability - a.probability).slice(0, 8);
  const fixedList = ms.filter(m => m.fix_note);
  el.innerHTML = pageHeader({ tag: "Machine Telemetry", title: "Fleet Health", sub: `${ms.length} machines scored by the failure model every 3 hours. Click a machine for sensors, alarms and history.` }) +
    `<div class="grid grid-cols-1 xl:grid-cols-4 gap-space-lg mb-space-lg"><div class="min-w-0 xl:col-span-3">${card(grid)}</div>
     <div>${card(`<div class="text-label-caps uppercase tracking-wider font-bold text-outline mb-2">Highest risk now</div>${top.map(m => `<a href="#/machines/${esc(m.machine)}" class="flex items-center justify-between py-2 border-b border-slate-50 hover:text-primary"><span><span class="font-medium">${esc(m.machine)}</span><span class="block text-body-sm text-outline">${esc(m.station)} · ${esc(m.component || "")}</span></span><span class="tabular-nums font-semibold" style="color:${color(m.probability)}">${pct(m.probability)}</span></a>`).join("")}`)}</div></div>
     ${fixedList.length ? `<div class="mb-space-lg">${card(`<div class="text-label-caps uppercase tracking-wider font-bold text-outline mb-2">Repairs after alerts</div>${fixedList.map(m => `<a href="#/machines/${esc(m.machine)}" class="flex items-center justify-between py-2 border-b border-slate-50 hover:text-primary"><span class="font-medium">${esc(m.machine)} <span class="text-body-sm text-outline">${esc(m.station)}</span></span><span class="text-body-sm ${m.fixed ? "text-emerald-700" : "text-amber-700"} font-semibold">${esc(m.fix_note)}</span></a>`).join("")}`)}</div>` : ""}
     <div id="mDetail">${id ? loading() : card(empty("Select a machine above to see its telemetry.", "touch_app"))}</div>`;
  if (!id) return;
  const days = +(q.get("days") || 7);
  const d = await api(`/ui/machines/${encodeURIComponent(id)}?days=${days}`);
  const i = d.info, t = d.telemetry.map(x => [parseTs(x.ts).getTime(), x]);
  const sColors = ["#2563eb", "#7c3aed", "#0891b2", "#ba1a1a"];
  const charts = Object.entries(d.sensors).map(([k, s], j) => card(`<div class="flex justify-between mb-1"><span class="text-label-caps uppercase tracking-wider font-bold text-outline">${esc(s.label)}</span><span class="text-body-sm text-outline">${esc(s.unit)}</span></div>
    ${lineChart({ series: [{ points: t.map(([x, r]) => [x, r[k]]), color: sColors[j % 4] }], h: 150, yLabel: s.label })}`)).join("");
  const riskPts = d.risk_history.map(r => [parseTs(r.ts).getTime(), r.p_failure]);
  const alertsHere = S.alerts.filter(a => riskFor(a.key)?.item.context?.machine === id);
  $("#mDetail").innerHTML = `
    <div class="bg-surface-container-lowest rounded-2xl shadow-md p-space-lg mb-space-lg flex flex-col lg:flex-row justify-between gap-space-md">
      <div><div class="text-label-caps uppercase text-outline">${esc(lineName(i.line))}</div><h2 class="text-headline-lg">${esc(i.machine)} · ${esc(i.station)}</h2>
        <div class="text-on-surface-variant">Model ${esc(i.model)} · ${esc(i.age)} years · ${esc(i.mode)} station · criticality ${esc(i.criticality)}</div>
        ${i.fix_note ? `<div class="mt-2 inline-flex items-center gap-1 px-3 py-1 rounded-full ${i.fixed ? "bg-emerald-50 text-emerald-800" : "bg-amber-50 text-amber-800"} text-label-sm font-bold"><span class="material-symbols-outlined text-[16px]">build</span>${esc(i.fix_note)}</div>` : ""}
        ${i.driver && !i.fixed ? `<div class="mt-2 text-sm"><span class="material-symbols-outlined text-[16px] text-primary">insights</span> Main driver: ${esc(i.driver)}</div>` : ""}
        ${alertsHere.map(a => `<a href="#/alert/${esc(a.alert_id)}" class="inline-flex mt-2 mr-2 items-center gap-1 px-3 py-1 rounded-full ${sev(a.severity).pill} text-label-sm font-bold">${esc(a.alert_id)} · ${esc(a.headline)} →</a>`).join("")}</div>
      <div class="flex items-center gap-space-lg">
        <div class="text-center"><div class="text-headline-xl tabular-nums" style="color:${color(i.probability)}">${pct(i.probability)}</div><div class="text-body-sm text-outline">failure risk (48h)</div></div>
        <div class="text-center"><div class="text-headline-sm">${esc(i.component || "—")}</div><div class="text-body-sm text-outline">likely component</div></div>
        ${segmented([["3", "3d"], ["7", "7d"], ["14", "14d"]], String(days), "data-days")}
      </div>
    </div>
    <div class="grid grid-cols-1 lg:grid-cols-2 gap-space-lg mb-space-lg">${charts}</div>
    <div class="grid grid-cols-1 lg:grid-cols-3 gap-space-lg">
      ${card(`<div class="text-label-caps uppercase tracking-wider font-bold text-outline mb-1">Model score history</div>${lineChart({ series: [{ points: riskPts, color: "#ba1a1a" }], yLines: [{ y: d.threshold, label: "alert threshold", color: "#f97316" }], h: 150 })}`)}
      ${card(`<div class="text-label-caps uppercase tracking-wider font-bold text-outline mb-2">Days since component replaced</div>${Object.entries(d.days_since).map(([k, v]) => `<div class="flex justify-between py-1.5 border-b border-slate-50 text-sm"><span>${esc(cap(k))}</span><span class="tabular-nums font-medium">${v == null ? "never" : v + " d"}</span></div>`).join("")}
        <div class="text-label-caps uppercase tracking-wider font-bold text-outline mt-4 mb-2">Alarms (${days}d)</div>${d.alarms.length ? d.alarms.slice(-10).reverse().map(x => `<div class="flex justify-between py-1 text-sm"><span>${esc(x.name)}</span><span class="text-outline">${fmtTs(x.ts, false)}</span></div>`).join("") : `<div class="text-outline text-sm">No alarms.</div>`}`)}
      ${card(`<div class="text-label-caps uppercase tracking-wider font-bold text-outline mb-2">Maintenance notes</div>${d.notes.length ? d.notes.slice().reverse().map(x => `<div class="py-2 border-b border-slate-50"><div class="text-sm">${esc(x.note)}</div><div class="text-body-sm text-outline">${esc(x.author)} · ${fmtTs(x.ts)}</div></div>`).join("") : `<div class="text-outline text-sm">No notes.</div>`}`)}
    </div>`;
  $$("[data-days]", el).forEach(b => b.onclick = () => { location.hash = `#/machines/${id}?days=${b.dataset.days}`; });
};

// ---- supply
pages.supply = async (el, q) => {
  const mats = await api("/ui/materials");
  const sel = q.get("m") || "";
  const matRisks = S.risks.filter(r => r.item.category === "material");
  const riskOf = m => matRisks.find(r => r.item.context.material === m);
  const low = mats.filter(m => m.days_of_cover != null && m.days_of_cover < m.reorder_days).length;
  const revised = mats.reduce((s, m) => s + m.open_pos.filter(p => p.revised).length, 0);
  el.innerHTML = pageHeader({ tag: "Inbound Supply Radar", title: "Materials & Suppliers", sub: "Live stock cover for every material in the bill of materials, open purchase orders and supplier delay notices." }) + `
  <div class="grid grid-cols-1 md:grid-cols-3 gap-space-md mb-space-lg">
    ${card(`<div class="text-label-caps uppercase text-on-surface-variant">Materials tracked</div><div class="text-headline-xl tabular-nums">${mats.length}</div>`)}
    ${card(`<div class="text-label-caps uppercase text-on-surface-variant">Below reorder cover</div><div class="text-headline-xl tabular-nums ${low ? "text-error" : "text-secondary"}">${low}</div>`)}
    ${card(`<div class="text-label-caps uppercase text-on-surface-variant">Supplier delay notices</div><div class="text-headline-xl tabular-nums ${revised ? "text-amber-700" : "text-secondary"}">${revised}</div>`)}
  </div>` + card(`<div class="overflow-auto"><table class="data w-full"><thead><tr><th>Material</th><th>Supplier</th><th>Stock</th><th style="min-width:200px">Days of cover</th><th>Use / h</th><th>Lines</th><th>Open POs</th><th>Risk</th></tr></thead><tbody>
    ${mats.map(m => { const r = riskOf(m.material), a = r && alertFor(r.item.key); const pc = m.days_of_cover == null ? 0 : Math.min(100, m.days_of_cover / (m.reorder_days * 2) * 100);
      const col = m.days_of_cover == null ? "#cbd5e1" : m.days_of_cover < m.reorder_days * .5 ? "#ba1a1a" : m.days_of_cover < m.reorder_days ? "#f97316" : "#10b981";
      return `<tr class="click ${sel === m.material ? "bg-surface-container-low" : ""}" data-mat="${esc(m.material)}">
      <td class="font-medium">${esc(m.material)}</td><td>${esc(m.supplier)}<div class="text-body-sm text-outline">lead ${m.lead_time_days}d</div></td><td class="tabular-nums">${num(m.stock)}</td>
      <td><div class="flex items-center gap-2"><div class="flex-1 h-2 rounded-full bg-slate-100 relative"><div class="h-2 rounded-full" style="width:${pc}%;background:${col}"></div><div class="absolute top-[-3px] h-3.5 w-px bg-slate-500" style="left:50%" title="reorder level ${m.reorder_days}d"></div></div><span class="tabular-nums w-12 text-right">${m.days_of_cover ?? "—"}d</span></div></td>
      <td class="tabular-nums">${num(m.hourly_use)}</td><td>${esc(m.lines.join(", "))}</td>
      <td>${m.open_pos.length ? m.open_pos.map(p => `<div class="text-body-sm whitespace-nowrap">${esc(p.po)} · ETA ${fmtTs(p.eta, false)} ${p.revised ? `<span class="px-1.5 rounded bg-amber-100 text-amber-800 font-bold">delayed</span>` : ""}</div>`).join("") : `<span class="text-outline text-body-sm">none</span>`}</td>
      <td>${r ? `<a href="${a ? `#/alert/${a.alert_id}` : `#/risk/${encodeURIComponent(r.item.key)}`}" class="px-2 py-0.5 rounded-full ${sev(r.item.severity).pill} text-[11px] font-bold whitespace-nowrap">${sev(r.item.severity).label} · ${lakh(r.item.expected_impact)}</a>` : `<span class="text-secondary text-body-sm">OK</span>`}</td></tr>`; }).join("")}
  </tbody></table></div>`, "p-0 overflow-hidden") + `<div id="matDetail" class="mt-space-lg"></div>`;
  $$("[data-mat]", el).forEach(tr => tr.addEventListener("click", e => { if (e.target.closest("a")) return; location.hash = `#/supply?m=${encodeURIComponent(tr.dataset.mat)}`; }));
  const m = mats.find(x => x.material === sel);
  if (m) {
    const t0 = parseTs(S.now).getTime(), H = 24 * 10, pts = [];
    let stock = m.stock || 0;
    const arr = m.open_pos.map(p => ({ t: parseTs(p.eta).getTime(), q: p.qty }));
    for (let h = 0; h <= H; h += 3) { const t = t0 + h * 3600e3; arr.forEach(a => { if (!a.done && a.t <= t) { stock += a.q; a.done = true; } }); pts.push([t, Math.max(0, stock)]); stock -= m.hourly_use * 3; }
    $("#matDetail").innerHTML = card(`<div class="flex justify-between items-center mb-2"><div><div class="text-label-caps uppercase text-outline">Projected stock — next 10 days (normal usage, arrivals at ETA)</div><div class="text-headline-sm">${esc(m.material)} · ${esc(m.supplier)}</div></div><a href="#/supply" class="text-primary text-sm font-semibold">Close</a></div>
      ${lineChart({ series: [{ points: pts, color: "#2563eb", width: 2 }], yLines: [{ y: m.hourly_use * 24 * m.reorder_days, label: `reorder level (${m.reorder_days}d)`, color: "#f97316" }], h: 200 })}`);
    setTimeout(() => $("#matDetail")?.scrollIntoView({ behavior: "smooth", block: "start" }), 50);
  }
};

// ---- orders / workforce / quality
pages.orders = async (el, q) => {
  const tab = q.get("tab") || "orders", line = q.get("line") || "";
  const set = (k, v) => { const p = new URLSearchParams(q); v ? p.set(k, v) : p.delete(k); location.hash = `#/orders?${p}`; };
  const head = pageHeader({ tag: "Orders & Schedule", title: tab === "orders" ? "Customer Orders" : tab === "workforce" ? "Shift Roster Gaps" : "Quality SPC", sub: tab === "orders" ? "Open orders dispatched earliest-due-date first, projected against normal line capacity." : tab === "workforce" ? "Staffing gaps known for the next 72 hours, with certified roles highlighted." : "Shift-level control charts for every line and quality metric.", right: segmented([["orders", "Open Orders"], ["workforce", "Workforce"], ["quality", "Quality"]], tab, "data-tab") });
  let body = "";
  if (tab === "orders") {
    const qq = (q.get("q") || "").toLowerCase(), late = q.get("late") === "1", tier = q.get("tier") || "";
    const atRisk = new Set(S.risks.flatMap(r => (r.item.evidence?.orders || r.item.evidence?.orders_made_late || []).map(o => o.order)));
    let os = await api("/ui/orders");
    os = os.filter(o => (!line || o.line === line) && (!tier || o.tier === tier) && (!late || o.proj_late_days > 0 || atRisk.has(o.order_id)) && (!qq || `${o.order_id} ${o.customer} ${o.sku}`.toLowerCase().includes(qq)));
    const pen = os.reduce((s, o) => s + (o.proj_penalty || 0), 0);
    const byCust = {}; os.forEach(o => { byCust[o.customer] = (byCust[o.customer] || 0) + (o.proj_penalty || 0); });
    const maxC = Math.max(1, ...Object.values(byCust));
    body = `<div class="flex flex-wrap items-end gap-space-md mb-space-md">${select("oLine", lineOpts(), line, "Line")}${select("oTier", [["", "All tiers"], ["A", "Tier A"], ["B", "Tier B"], ["C", "Tier C"]], tier, "Customer tier")}
      <label class="flex flex-col text-label-caps uppercase text-outline gap-1">Search<input id="oQ" value="${esc(q.get("q") || "")}" class="rounded-xl border-slate-200 text-sm normal-case tracking-normal text-on-surface font-normal py-1.5" placeholder="order, customer, SKU"></label>
      <label class="flex items-center gap-2 text-sm pb-2 cursor-pointer"><input id="oLate" type="checkbox" class="rounded" ${late ? "checked" : ""}> Late or at risk only</label></div>
    <div class="grid grid-cols-1 xl:grid-cols-4 gap-space-lg">
      <div class="min-w-0 xl:col-span-3">${card(os.length ? `<div class="overflow-auto max-h-[70vh]"><table class="data w-full"><thead><tr><th>Order</th><th>Line</th><th>Customer</th><th>Qty</th><th>Remaining</th><th>Due</th><th>Projected finish</th><th>Days late</th><th>Penalty</th></tr></thead><tbody>
        ${os.map(o => `<tr class="${atRisk.has(o.order_id) ? "bg-error-container/20" : ""}"><td class="font-medium whitespace-nowrap">${esc(o.order_id)} ${o.rush ? `<span class="px-1.5 rounded bg-orange-100 text-orange-700 text-[10px] font-bold">RUSH</span>` : ""} ${atRisk.has(o.order_id) ? `<span class="px-1.5 rounded bg-error-container text-error text-[10px] font-bold">AT RISK</span>` : ""}</td>
        <td>${esc(o.line)}</td><td>${esc(o.customer)} <span class="px-1.5 rounded bg-surface-container text-[10px] font-bold">${esc(o.tier)}</span></td><td class="tabular-nums">${num(o.qty)}</td><td class="tabular-nums">${num(o.remaining)}</td>
        <td class="whitespace-nowrap">${fmtTs(o.due_ts, false)}</td><td class="whitespace-nowrap">${fmtTs(o.proj_done, false)}</td><td class="tabular-nums ${o.proj_late_days > 0 ? "text-error font-semibold" : "text-secondary"}">${o.proj_late_days > 0 ? (+o.proj_late_days).toFixed(1) : "on time"}</td><td class="tabular-nums">${o.proj_penalty ? inr(o.proj_penalty) : "—"}</td></tr>`).join("")}
      </tbody></table></div>` : empty("No open orders match."), "p-0 overflow-hidden")}</div>
      <div>${card(`<div class="text-label-caps uppercase tracking-wider font-bold text-outline">Projected penalties</div><div class="text-headline-lg text-error tabular-nums mb-3">${inr(pen)}</div>
        ${Object.entries(byCust).sort((a, b) => b[1] - a[1]).map(([c, v]) => `<div class="mb-2"><div class="flex justify-between text-sm"><span>${esc(c)}</span><span class="tabular-nums">${lakh(v)}</span></div><div class="h-2 rounded-full bg-slate-100"><div class="h-2 rounded-full bg-error" style="width:${v / maxC * 100}%"></div></div></div>`).join("")}
        <p class="text-body-sm text-outline mt-3">${os.length} open orders shown. Baseline projection at normal capacity; risk-driven delays are flagged “AT RISK”.</p>`)}</div>
    </div>`;
  } else if (tab === "workforce") {
    const ws = (await api("/ui/workforce")).filter(w => !line || w.line === line);
    body = `<div class="flex items-end gap-space-md mb-space-md">${select("oLine", lineOpts(), line, "Line")}</div>` + card(ws.length ? `<div class="overflow-auto"><table class="data w-full"><thead><tr><th>Shift</th><th>Line</th><th>Role</th><th>Planned</th><th>Present</th><th>Missing</th><th>Notice</th><th></th></tr></thead><tbody>
      ${ws.map(w => { const r = S.risks.find(x => x.item.category === "workforce" && x.item.line === w.line); const a = r && alertFor(r.item.key); return `<tr><td class="whitespace-nowrap">${fmtDay(w.shift_start)} · ${esc(w.shift)}</td><td>${esc(lineName(w.line))}</td>
      <td>${esc(cap(w.role.replace(/_/g, " ")))} ${w.certified ? `<span class="px-1.5 rounded bg-error-container text-error text-[10px] font-bold">CERTIFIED</span>` : ""}</td><td class="tabular-nums">${w.planned}</td><td class="tabular-nums">${w.present_known}</td>
      <td class="tabular-nums text-error font-semibold">${w.planned - w.present_known}</td><td class="whitespace-nowrap">${fmtTs(w.notice_ts, false)}</td>
      <td>${r ? `<a class="text-primary text-sm font-semibold whitespace-nowrap" href="${a ? `#/alert/${a.alert_id}` : `#/risk/${encodeURIComponent(r.item.key)}`}">View risk →</a>` : ""}</td></tr>`; }).join("")}
    </tbody></table></div>` : empty("No known staffing gaps in the next 72 hours."), "p-0 overflow-hidden");
  } else {
    const qs = (await api("/ui/quality")).filter(x => !line || x.line === line);
    body = `<div class="flex items-end gap-space-md mb-space-md">${select("oLine", lineOpts(), line, "Line")}</div><div class="grid grid-cols-1 lg:grid-cols-2 xl:grid-cols-3 gap-space-lg">` + qs.map(x => {
      const pts = x.points.map(p => [parseTs(p.shift_start).getTime(), p.mean]);
      const marks = x.points.filter(p => p.mean < x.lsl || p.mean > x.usl || p.out_of_spec > .05).map(p => [parseTs(p.shift_start).getTime(), p.mean]);
      const last = x.points[x.points.length - 1] || {};
      const r = S.risks.find(z => z.item.category === "quality" && z.item.line === x.line && (z.item.evidence?.metric || "") === x.label);
      return card(`<div class="flex justify-between items-start mb-1"><div><div class="text-label-caps uppercase text-outline">${esc(x.line)} · ${esc(x.station)}</div><div class="font-semibold">${esc(x.label)} <span class="text-outline font-normal">(${esc(x.unit)})</span></div></div>
        ${r ? `<a href="${alertFor(r.item.key) ? `#/alert/${alertFor(r.item.key).alert_id}` : `#/risk/${encodeURIComponent(r.item.key)}`}" class="px-2 py-0.5 rounded-full ${sev(r.item.severity).pill} text-[11px] font-bold">Drift · ${lakh(r.item.expected_impact)}</a>` : `<span class="px-2 py-0.5 rounded-full bg-secondary/10 text-secondary text-[11px] font-bold">In control</span>`}</div>
        ${lineChart({ series: [{ points: pts, color: "#2563eb", marks }], yLines: [{ y: x.usl, label: "USL" }, { y: x.lsl, label: "LSL" }, { y: x.target, label: "target", color: "#10b981" }], h: 150 })}
        <div class="text-body-sm text-on-surface-variant">Last shift: mean ${last.mean != null ? (+last.mean).toFixed(2) : "—"} · ${pct(last.out_of_spec)} out of spec</div>`);
    }).join("") + `</div>`;
  }
  el.innerHTML = head + body;
  $$("[data-tab]", el).forEach(b => b.onclick = () => { location.hash = `#/orders?tab=${b.dataset.tab}${line ? "&line=" + line : ""}`; });
  $("#oLine") && ($("#oLine").onchange = e => set("line", e.target.value));
  $("#oTier") && ($("#oTier").onchange = e => set("tier", e.target.value));
  $("#oQ") && ($("#oQ").onchange = e => set("q", e.target.value.trim()));
  $("#oLate") && ($("#oLate").onchange = e => set("late", e.target.checked ? "1" : ""));
};

// ---- alert history (closed alerts)
pages["alert-history"] = async (el, q) => {
  const line = q.get("line") || "", catF = q.get("cat") || "", who = q.get("who") || "", qq = (q.get("q") || "").toLowerCase();
  const set = (k, v) => { const p = new URLSearchParams(q); v ? p.set(k, v) : p.delete(k); location.hash = `#/alert-history?${p}`; };
  const all = await api("/ui/alert-history?limit=5000");
  const rows = all.filter(a => (!line || a.line === line) && (!catF || a.category === catF) &&
    (!who || (who === "people" ? a.status === "resolved" : a.status === "auto_resolved")) &&
    (!qq || `${a.alert_id} ${a.headline} ${a.resolved_by} ${a.resolution}`.toLowerCase().includes(qq)));
  const byPeople = rows.filter(a => a.status === "resolved");
  const protectedSum = byPeople.reduce((s, a) => s + (a.money_protected || 0), 0);
  const avgH = byPeople.length ? byPeople.reduce((s, a) => s + a.hours_to_close, 0) / byPeople.length : 0;
  const dur = h => h < 1 ? `${Math.max(1, Math.round(h * 60))} min` : h < 48 ? `${h.toFixed(1)} h` : `${(h / 24).toFixed(1)} days`;
  const stat = (label, value, sub = "", cls = "") => card(`<div class="text-label-caps uppercase tracking-wider text-on-surface-variant">${label}</div><div class="text-headline-xl font-bold tabular-nums ${cls}">${value}</div><div class="text-body-sm text-on-surface-variant">${sub}</div>`);
  const cats = [...new Set(all.map(a => a.category))];
  el.innerHTML = pageHeader({ tag: "Alert History", title: "Closed Alerts", sub: "Every alert that has been resolved - by the team or because the problem cleared - with who closed it, how long it took and the money protected." }) + `
  <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-space-md mb-space-lg">
    ${stat("Alerts closed", rows.length.toLocaleString("en-IN"), `${byPeople.length} by the team · ${rows.length - byPeople.length} cleared by themselves`)}
    ${stat("Money protected", lakh(protectedSum), "risk removed by actions the team took", "text-emerald-700")}
    ${stat("Average time to close", byPeople.length ? dur(avgH) : "—", "from alert raised to resolved")}
    ${stat("Escalated before closing", String(rows.filter(a => a.escalations > 0).length), "alerts that needed a manager")}
  </div>
  <div class="flex flex-wrap items-end gap-3 mb-space-md">
    ${select("ahLine", [["", "All lines"], ...S.cfg.lines.map(l => [l.id, l.name])], line, "Line")}
    ${select("ahCat", [["", "All categories"], ...cats.map(c => [c, cat(c).label])], catF, "Category")}
    ${select("ahWho", [["", "Everyone"], ["people", "Resolved by the team"], ["auto", "Cleared by itself"]], who, "Closed by")}
    <label class="text-body-sm text-on-surface-variant">Search<input id="ahQ" value="${esc(q.get("q") || "")}" placeholder="alert, person, note" class="mt-1 block rounded-xl border-slate-200 text-sm"></label>
  </div>` + card(rows.length ? `<div class="overflow-auto"><table class="data w-full"><thead><tr>
      <th>Closed</th><th>Alert</th><th>What happened</th><th>Line</th><th>At risk when raised</th><th>Protected</th><th>Closed by</th><th>Time to close</th><th>Actions</th></tr></thead><tbody>
    ${rows.map(a => `<tr class="click" data-href="#/alert/${esc(a.alert_id)}">
      <td class="whitespace-nowrap">${fmtTs(a.resolved_at, false)}</td>
      <td><div class="font-semibold">${esc(a.alert_id)}</div>${sevPill(a.severity)}</td>
      <td class="min-w-[280px] max-w-md"><div class="font-medium">${esc(a.headline)}</div><div class="text-body-sm text-outline">${esc(cat(a.category).label)}</div></td>
      <td>${esc(a.line)}</td>
      <td class="tabular-nums">${inr(a.impact_when_raised)}</td>
      <td class="tabular-nums ${a.money_protected ? "text-emerald-700 font-semibold" : "text-outline"}">${a.money_protected ? inr(a.money_protected) : "—"}</td>
      <td>${a.status === "resolved" ? `<div class="font-medium">${esc(a.resolved_by)}</div><div class="text-body-sm text-outline">${esc(a.resolution || "")}</div>` : `<span class="text-outline">Problem cleared by itself</span>`}</td>
      <td class="whitespace-nowrap">${dur(a.hours_to_close)}${a.escalations ? `<div class="text-body-sm text-amber-700">escalated ${a.escalations}x</div>` : ""}</td>
      <td class="tabular-nums">${a.actions_done}/${a.actions_total}</td></tr>`).join("")}
    </tbody></table></div>` : empty(all.length ? "No closed alerts match these filters." : "No closed alerts yet. Resolve an alert, or complete all its actions, and it appears here."), "p-0 overflow-hidden");
  $("#ahLine").onchange = e => set("line", e.target.value);
  $("#ahCat").onchange = e => set("cat", e.target.value);
  $("#ahWho").onchange = e => set("who", e.target.value);
  $("#ahQ").onchange = e => set("q", e.target.value.trim());
};

// ---- history
pages.history = async (el, q) => {
  const line = q.get("line") || "", late = q.get("late") === "1", qq = (q.get("q") || "").toLowerCase(), page = +(q.get("p") || 1);
  const set = (k, v) => { const p = new URLSearchParams(q); v ? p.set(k, v) : p.delete(k); if (k !== "p") p.delete("p"); location.hash = `#/history?${p}`; };
  const all = await api("/ui/history?limit=3000");
  const rows = all.filter(o => (!line || o.line === line) && (!late || o.days_late > 0) && (!qq || `${o.order_id} ${o.customer} ${o.sku}`.toLowerCase().includes(qq)));
  const onTime = rows.length ? rows.filter(o => !(o.days_late > 0)).length / rows.length : 0;
  const pen = rows.reduce((s, o) => s + (o.penalty || 0), 0);
  const PER = 50, pages_ = Math.max(1, Math.ceil(rows.length / PER)), pg = Math.min(page, pages_);
  el.innerHTML = pageHeader({ tag: "Order History", title: "Delivered Orders", sub: "Completed customer orders up to the plant clock, with lateness and contract penalties paid." }) + `
  <div class="grid grid-cols-1 md:grid-cols-3 gap-space-md mb-space-lg">
    ${card(`<div class="text-label-caps uppercase text-on-surface-variant">Orders delivered</div><div class="text-headline-xl tabular-nums">${num(rows.length)}</div>`)}
    ${card(`<div class="text-label-caps uppercase text-on-surface-variant">On-time delivery</div><div class="text-headline-xl tabular-nums ${onTime > .9 ? "text-secondary" : "text-amber-700"}">${pct(onTime)}</div>`)}
    ${card(`<div class="text-label-caps uppercase text-on-surface-variant">Penalties paid</div><div class="text-headline-xl tabular-nums text-error">${lakh(pen)}</div>`)}
  </div>
  <div class="flex flex-wrap items-end gap-space-md mb-space-md">${select("hLine", lineOpts(), line, "Line")}
    <label class="flex flex-col text-label-caps uppercase text-outline gap-1">Search<input id="hQ" value="${esc(q.get("q") || "")}" class="rounded-xl border-slate-200 text-sm normal-case tracking-normal text-on-surface font-normal py-1.5" placeholder="order, customer, SKU"></label>
    <label class="flex items-center gap-2 text-sm pb-2 cursor-pointer"><input id="hLate" type="checkbox" class="rounded" ${late ? "checked" : ""}> Late only</label></div>` +
    card(rows.length ? `<div class="overflow-auto"><table class="data w-full"><thead><tr><th>Order</th><th>Line</th><th>SKU</th><th>Customer</th><th>Qty</th><th>Due</th><th>Completed</th><th>Days late</th><th>Penalty</th></tr></thead><tbody>
    ${rows.slice((pg - 1) * PER, pg * PER).map(o => `<tr><td class="font-medium">${esc(o.order_id)} ${o.rush ? `<span class="px-1.5 rounded bg-orange-100 text-orange-700 text-[10px] font-bold">RUSH</span>` : ""}</td><td>${esc(o.line)}</td><td>${esc(o.sku)}</td><td>${esc(o.customer)} <span class="px-1.5 rounded bg-surface-container text-[10px] font-bold">${esc(o.tier)}</span></td>
      <td class="tabular-nums">${num(o.qty)}</td><td class="whitespace-nowrap">${fmtTs(o.due_ts)}</td><td class="whitespace-nowrap">${fmtTs(o.completed_ts)}</td><td class="tabular-nums ${o.days_late > 0 ? "text-error font-semibold" : "text-secondary"}">${o.days_late > 0 ? (+o.days_late).toFixed(1) : "on time"}</td><td class="tabular-nums">${o.penalty ? inr(o.penalty) : "—"}</td></tr>`).join("")}
    </tbody></table></div>
    <div class="flex items-center justify-between px-4 py-3 border-t border-slate-100"><span class="text-body-sm text-outline">Page ${pg} of ${pages_}</span><div class="flex gap-2">
      <button id="hPrev" ${pg <= 1 ? "disabled" : ""} class="px-3 py-1.5 rounded-lg bg-slate-100 hover:bg-slate-200 disabled:opacity-40 text-sm font-semibold">Previous</button>
      <button id="hNext" ${pg >= pages_ ? "disabled" : ""} class="px-3 py-1.5 rounded-lg bg-slate-100 hover:bg-slate-200 disabled:opacity-40 text-sm font-semibold">Next</button></div></div>` : empty("No delivered orders match."), "p-0 overflow-hidden");
  $("#hLine").onchange = e => set("line", e.target.value);
  $("#hQ").onchange = e => set("q", e.target.value.trim());
  $("#hLate").onchange = e => set("late", e.target.checked ? "1" : "");
  $("#hPrev") && ($("#hPrev").onclick = () => set("p", String(pg - 1)));
  $("#hNext") && ($("#hNext").onclick = () => set("p", String(pg + 1)));
};

// ---- settings
pages.settings = async (el) => {
  const [rep, ev] = await Promise.all([api("/model/report").catch(() => ({})), api("/evaluation").catch(() => ({}))]);
  const t = rep.test || {}, m = t.model || {}, b = t.baseline || t.baseline_vibration_threshold || {};
  m.component_accuracy_when_alerted = m.component_accuracy_when_alerted ?? m.component_accuracy;
  const cfg = S.cfg, esc_ = cfg.escalation, pm = S.user.role === "plant_manager", dis = pm ? "" : "disabled";
  const cmp = (label, mv, bv, fmt = v => v, better = "high") => `<div class="py-2 border-b border-slate-50"><div class="flex justify-between text-sm"><span>${label}</span><span class="tabular-nums"><b>${fmt(mv)}</b> <span class="text-outline">vs ${fmt(bv)}</span></span></div>
    <div class="flex gap-1 mt-1"><div class="h-1.5 rounded-full bg-primary-container" style="width:${Math.min(100, (better === "high" ? mv / Math.max(mv, bv) : bv / Math.max(mv, bv)) * 100)}%"></div></div></div>`;
  el.innerHTML = pageHeader({ tag: "Settings", title: "Plant Configuration", sub: "People, ownership, escalation rules and model accuracy. Changes apply immediately and re-assess the plant." }) + `
  <div class="grid grid-cols-1 xl:grid-cols-2 gap-space-lg">
    ${card(`<div class="text-label-caps uppercase tracking-wider font-bold text-outline mb-3">Viewing as</div>
      <div class="flex items-center gap-3 mb-3"><div class="w-11 h-11 rounded-xl bg-gradient-to-tr from-primary to-blue-500 text-white flex items-center justify-center font-bold">${initials(S.user.name)}</div><div><div class="font-semibold">${esc(S.user.name)}</div><div class="text-body-sm text-on-surface-variant">${esc(S.user.title)} · ${esc(S.user.email)} · ${esc(S.user.phone)}</div></div></div>
      <div class="flex flex-wrap items-end gap-2">${select("sUser", cfg.people.map(p => [p.name, `${p.name} — ${cfg.roles[p.role]}`]), S.user.name, "View as")}</div>
      <div class="text-label-caps uppercase tracking-wider font-bold text-outline mt-space-lg mb-2">Plant clock</div>
      <div class="flex items-center gap-2"><input id="sClock" type="datetime-local" step="3600" value="${toLocalInput(S.now)}" min="${toLocalInput(cfg.range.start)}" max="${toLocalInput(cfg.range.end)}" class="rounded-xl border-slate-200 text-sm"><button id="sClockSet" class="px-3 py-2 rounded-xl bg-primary-container text-white text-sm font-semibold">Set clock</button>
        <button id="sDemoReset" class="px-3 py-2 rounded-xl bg-amber-100 text-amber-900 hover:bg-amber-200 text-sm font-semibold">Reset demo</button></div>
      <div class="text-body-sm text-outline mt-1">Reset demo clears all alerts and returns to the demo moment with live issues.</div>`)}
    ${card(`<div class="text-label-caps uppercase tracking-wider font-bold text-outline mb-1">How accurate is the AI (test: Oct–Dec 2015, unseen)</div>
      ${m.failures ? `<div class="grid grid-cols-3 gap-3 my-3">
        <div class="p-3 rounded-xl bg-surface-container-low"><div class="text-headline-lg tabular-nums">${m.detected}/${m.failures}</div><div class="text-body-sm text-on-surface-variant">failures warned early (simple alarm rule: ${b.detected ?? "—"}/${b.failures ?? "—"})</div></div>
        <div class="p-3 rounded-xl bg-surface-container-low"><div class="text-headline-lg tabular-nums">${m.median_lead_h}h</div><div class="text-body-sm text-on-surface-variant">median warning lead</div></div>
        <div class="p-3 rounded-xl bg-surface-container-low"><div class="text-headline-lg tabular-nums">${pct(m.component_accuracy_when_alerted)}</div><div class="text-body-sm text-on-surface-variant">correct component</div></div></div>
        ${cmp("Precision", m.precision, b.precision, pct)}${cmp("Recall", m.recall, b.recall, pct)}${cmp("Failures caught", m.event_recall, b.event_recall, pct)}${cmp("False alarms / day", m.false_alarm_episodes_per_day, b.false_alarm_episodes_per_day, v => v, "low")}
        <p class="text-body-sm text-outline mt-2">AI model (bold) vs the old vibration-threshold rule. Alert threshold score ${(+rep.threshold).toFixed(3)}.</p>` : `<div class="text-outline text-sm">No model report found. Run <code>python run.py train</code>.</div>`}
      ${ev && !ev.hint ? `<details class="mt-3"><summary class="cursor-pointer text-primary text-sm font-semibold">End-to-end evaluation</summary><pre class="text-body-sm bg-surface-container-low rounded-lg p-3 mt-2 overflow-auto max-h-80">${esc(JSON.stringify(ev, null, 1))}</pre></details>` : `<p class="text-body-sm text-outline mt-2">End-to-end evaluation: ${esc(ev.hint || "not available")}</p>`}`)}
    <form id="setForm" class="min-w-0 xl:col-span-2 grid grid-cols-1 xl:grid-cols-2 gap-space-lg">
    ${card(`<div class="flex items-center justify-between mb-3"><span class="text-label-caps uppercase tracking-wider font-bold text-outline">People &amp; contacts</span>${pm ? "" : `<span class="text-body-sm text-outline">Read-only — only the Plant Manager can edit</span>`}</div>
      <div class="overflow-auto"><table class="data w-full"><thead><tr><th>Name</th><th>Role</th><th>Lines</th><th>Email</th><th>Phone</th></tr></thead><tbody>
      ${cfg.people.map(p => `<tr data-contact="${esc(p.name)}"><td class="font-medium whitespace-nowrap">${esc(p.name)}</td><td>${esc(cfg.roles[p.role])}</td><td>${esc(Array.isArray(p.lines) ? p.lines.join(", ") : "All")}</td>
        <td><input data-email type="email" required value="${esc(p.email)}" ${dis} class="w-56 rounded-lg border-slate-200 text-sm py-1"></td>
        <td><input data-phone required value="${esc(p.phone)}" ${dis} class="w-40 rounded-lg border-slate-200 text-sm py-1"></td></tr>`).join("")}</tbody></table></div>`)}
    ${card(`<div class="text-label-caps uppercase tracking-wider font-bold text-outline mb-3">Ownership &amp; escalation</div>
      <div class="grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-2">${Object.entries(cfg.owners).map(([c, r]) => `<label class="flex items-center justify-between gap-2 text-sm"><span class="flex items-center gap-1.5"><span class="material-symbols-outlined text-[16px]" style="color:${cat(c).color}">${cat(c).icon}</span>${esc(cat(c).label)}</span>
        <select data-owner="${esc(c)}" ${dis} class="rounded-lg border-slate-200 text-sm py-1">${Object.entries(cfg.roles).map(([k, v]) => `<option value="${esc(k)}" ${k === r ? "selected" : ""}>${esc(v)}</option>`).join("")}</select></label>`).join("")}</div>
      <div class="text-label-caps uppercase tracking-wider font-bold text-outline mt-space-lg mb-2">Severity bands (₹) &amp; acknowledge timeouts (min)</div>
      ${SEV_ORDER.map(k => `<div class="grid grid-cols-3 items-center gap-2 py-1.5 border-b border-slate-50 text-sm">${sevPill(k)}
        ${k === "low" ? `<span class="text-on-surface-variant">below medium</span>` : `<label class="flex items-center gap-1">≥ ₹<input id="stB_${k}" type="number" min="1" step="1000" required value="${esc_.severity_bands[k]}" ${dis} class="w-full rounded-lg border-slate-200 text-sm py-1"></label>`}
        <label class="flex items-center gap-1"><input id="stT_${k}" type="number" min="1" step="1" required value="${esc_.ack_timeout_min[k]}" ${dis} class="w-full rounded-lg border-slate-200 text-sm py-1"> min</label></div>`).join("")}
      <div class="grid grid-cols-1 sm:grid-cols-2 gap-3 mt-space-md text-sm">
        <label class="p-3 rounded-xl bg-surface-container-low">Alert threshold (₹)<input id="stThr" type="number" min="1" step="1000" required value="${cfg.risk.alert_threshold}" ${dis} class="mt-1 w-full rounded-lg border-slate-200 text-sm py-1"></label>
        <label class="p-3 rounded-xl bg-surface-container-low">Auto-resolve after (hours clear)<input id="stAuto" type="number" min="1" step="1" required value="${esc_.auto_resolve_after_h}" ${dis} class="mt-1 w-full rounded-lg border-slate-200 text-sm py-1"></label>
        <div class="p-3 rounded-xl bg-surface-container-low">Look-ahead window<div class="font-semibold">${cfg.risk.horizon_h} hours</div><div class="text-body-sm text-outline">set in the industry pack</div></div>
        <div class="p-3 rounded-xl bg-surface-container-low">Channels<div class="font-semibold">${esc(esc_.channels.join(", "))}</div><div class="text-body-sm text-outline">set in the industry pack</div></div>
      </div>
      ${pm ? `<div class="flex justify-end mt-space-md"><button id="setSave" type="submit" class="flex items-center gap-2 px-5 py-2.5 rounded-lg bg-secondary text-white font-semibold"><span class="material-symbols-outlined text-[18px]">save</span>Save settings</button></div>` : ""}`)}
    </form>
    ${card(`<div class="text-label-caps uppercase tracking-wider font-bold text-outline mb-2">Ask the plant assistant</div>
      <div class="text-body-sm text-on-surface-variant mb-2">${S.assistant ? "Powered by Claude." : "Built-in mode: answers from live plant data (no API key needed). Try: \"Which line is most at risk?\", \"Why is line 5 at risk?\", \"What should I do?\", \"Who do I call about stock?\", \"What if we fix line 5?\""}</div>
      ${true ? `<div class="flex flex-col sm:flex-row gap-2"><input id="askQ" class="flex-1 rounded-xl border-slate-200 text-sm" placeholder="e.g. What should maintenance do first today?"><select id="askLang" class="hidden"><option>English</option></select><button id="askGo" type="button" class="px-4 py-2 rounded-xl bg-primary-container text-white text-sm font-semibold">Ask</button></div>
      <div id="askOut" class="mt-3 text-sm whitespace-pre-wrap"></div>`
      : `<div class="flex items-start gap-3 p-3 rounded-xl bg-amber-50 text-amber-900 text-sm"><span class="material-symbols-outlined">key</span><div>The assistant is switched off because no Anthropic API key was found. Set <code>ANTHROPIC_API_KEY</code> in the environment and restart the server:<pre class="mt-2 bg-white/70 rounded p-2 text-body-sm">set ANTHROPIC_API_KEY=your-key
py -3.13 -m uvicorn api.main:app --port 8000</pre></div></div>`}`, "xl:col-span-2")}
  </div>`;
  $("#sUser").onchange = async e => { await switchTo(e.target.value); await refresh(); render(); };
  $("#sDemoReset").onclick = async ev => {
    const ok = await confirmBox("Reset demo", "This clears every alert, action and history entry and goes back to the demo moment.", "Reset demo");
    if (!ok) return;
    await guarded(ev.currentTarget, async () => { const r = await post("/demo/reset", {}); toast(`Demo reset - ${r.open_alerts} live alerts`); await refresh(); location.hash = "#/control-room"; render(); });
  };
  const f = $("#setForm");
  if (f) f.onsubmit = ev => { ev.preventDefault(); guarded($("#setSave"), async () => {
    const n = id => { const v = parseFloat($("#" + id).value); if (!(v > 0)) throw new Error("All numbers must be greater than zero"); return v; };
    const body = {
      alert_threshold: n("stThr"), auto_resolve_after_h: n("stAuto"),
      severity_bands: { critical: n("stB_critical"), high: n("stB_high"), medium: n("stB_medium") },
      ack_timeout_min: Object.fromEntries(SEV_ORDER.map(k => [k, Math.round(n("stT_" + k))])),
      owners: Object.fromEntries($$("[data-owner]").map(x => [x.dataset.owner, x.value])),
      contacts: Object.fromEntries($$("[data-contact]").map(x => [x.dataset.contact, { email: x.querySelector("[data-email]").value.trim(), phone: x.querySelector("[data-phone]").value.trim() }])),
    };
    await api("/ui/settings", { method: "PUT", body });
    S.cfg = await api("/ui/config"); await refresh(); toast("Settings saved and plant re-assessed"); render();
  }); };
  $("#sClockSet").onclick = () => guarded($("#sClockSet"), async () => {
    const v = $("#sClock").value; if (!v) throw new Error("Pick a date and time");
    const r = await post("/clock/set", { ts: v.replace("T", " ") }); toast(`Clock set to ${fmtTs(r.now)}`); await refresh(); render();
  });
  const go = $("#askGo");
  if (go) {
    const ask = () => guarded(go, async () => {
      const qv = $("#askQ").value.trim(); if (qv.length < 3) throw new Error("Type a longer question");
      const r = await post("/ask", { question: qv, language: $("#askLang").value });
      $("#askOut").textContent = r.answer || r.text || JSON.stringify(r, null, 1);
    });
    go.onclick = ask; $("#askQ").onkeydown = e => { if (e.key === "Enter") ask(); };
  }
};

// ------------------------------------------------------------------ router
function route() {
  const h = location.hash.replace(/^#\/?/, "") || "control-room";
  const [path, qs] = h.split("?");
  const [page, ...rest] = path.split("/");
  return { page: pages[page] ? page : "control-room", arg: rest.join("/") || null, q: new URLSearchParams(qs || "") };
}
let renderSeq = 0;
async function render() {
  const { page, arg, q } = route(), seq = ++renderSeq;
  renderNav();
  const main = $("#main");
  $$(".render-tmp", main).forEach(x => x.remove());           // drop any stale in-flight render
  if (!main.dataset.page || main.dataset.page !== page + arg) main.innerHTML = loading();
  const tmp = document.createElement("div");
  tmp.className = "render-tmp"; tmp.hidden = true;
  main.prepend(tmp);                                           // attached and first, so page code finds its own ids
  try {
    await pages[page](tmp, q, arg);
    if (seq !== renderSeq) { tmp.remove(); return; }
    main.innerHTML = ""; tmp.hidden = false; tmp.className = ""; main.appendChild(tmp);   // keep node so page handlers stay valid
    main.dataset.page = page + arg;
    $$("tr[data-href]", main).forEach(tr => tr.addEventListener("click", e => { if (!e.target.closest("a,button")) location.hash = tr.dataset.href; }));
  } catch (e) {
    tmp.remove();
    if (seq !== renderSeq) return;
    main.innerHTML = card(`<div class="text-center py-10"><span class="material-symbols-outlined text-error text-[40px]">error</span><div class="font-semibold mt-2">Could not load this page</div><div class="text-on-surface-variant text-sm mt-1">${esc(e.message)}</div><button id="retry" class="mt-4 px-4 py-2 rounded-xl bg-primary-container text-white text-sm font-semibold">Retry</button></div>`);
    $("#retry").onclick = render;
  }
}
window.addEventListener("hashchange", () => { closePops(); render(); window.scrollTo(0, 0); });

async function boot() {
  bindChrome();
  try {
    S.cfg = await api("/ui/config");
    S.token = store.get("ews_token");
    let me = null;
    if (S.token) { try { me = await api("/ui/me"); } catch { me = null; } }
    $("#signin").classList.add("hidden");
    if (me) { S.user = { ...me.person, title: me.title }; renderUser(); }
    else {
      const saved = store.get("ews_user");
      const name = S.cfg.people.some(p => p.name === saved) ? saved : defaultPerson();
      await switchTo(name, true);
    }
    await refresh();
    S.assistant = (await api("/assistant/status").catch(() => ({}))).available === true;
    setLive(true, "Engine Synchronized");
  } catch (e) {
    setLive(false, "API unreachable");
    $("#main").innerHTML = card(`<div class="text-center py-10"><div class="font-semibold">Cannot reach the early-warning API</div><div class="text-sm text-on-surface-variant mt-1">${esc(e.message)}</div><button onclick="location.reload()" class="mt-4 px-4 py-2 rounded-xl bg-primary-container text-white text-sm font-semibold">Reload</button></div>`);
    return;
  }
  render();
  setInterval(async () => {           // keep counters and notifications fresh
    try { await refresh(); setLive(true, "Engine Synchronized"); } catch { setLive(false, "API unreachable"); }
  }, 30000);
}
boot();
