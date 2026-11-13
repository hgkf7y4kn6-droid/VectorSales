/* VectorSales rep workspace: pipeline, lead detail, settings and the guided demo. */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const money = (n) => (n == null ? "–" : "$" + Number(n).toLocaleString("en-US", { maximumFractionDigits: Number(n) % 1 ? 2 : 0, minimumFractionDigits: Number(n) % 1 ? 2 : 0 }));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const STAGES = [
  ["new", "New"], ["review", "Needs review"], ["sent", "Quote out"],
  ["replied", "Replied"], ["won", "Won"], ["lost", "Lost"],
];
const TIER_ORDER = ["good", "better", "best"];
const AUTO_STEPS = [
  ["acknowledgement", "Instant reply", "The thank-you email within seconds of a new lead."],
  ["quote", "Send the quote", "Three fixed-price options with the cost comparison."],
  ["follow_up", "Follow-ups", "Day 2, day 5 and day 9, written when each is due."],
  ["invoice", "Invoice and welcome email", "Issued the moment the customer accepts."],
];

let S = null;            // last /api/state
let CONFIG = {};
let openLeadId = null;
let openTab = "quote";
let lastLeadIds = new Set();

// ---------- API ----------
function token() { try { return localStorage.getItem("vs_token") || ""; } catch { return ""; } }
async function api(path, opts = {}) {
  const headers = { "content-type": "application/json" };
  if (token()) headers.authorization = "Bearer " + token();
  const res = await fetch(path, { ...opts, headers, body: opts.body ? JSON.stringify(opts.body) : undefined });
  if (res.status === 401 && !path.startsWith("/auth")) { showLogin(); throw new Error("Please sign in."); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || "Something went wrong.");
  return data;
}
function toast(msg) {
  const t = document.createElement("div");
  t.className = "toast"; t.textContent = msg; document.body.append(t);
  setTimeout(() => t.remove(), 3200);
}
function showLogin() {
  if ($("#login-form")) return;
  $("#overlay-root").append($("#login-tpl").content.cloneNode(true));
  $("#login-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    try {
      const r = await api("/auth/login", { method: "POST", body: { email: f.email.value, password: f.password.value } });
      localStorage.setItem("vs_token", r.token);
      $("#overlay-root").innerHTML = "";
      refresh();
    } catch (err) { $("#login-err").textContent = err.message; }
  });
}

// ---------- time ----------
const nowDate = () => new Date(S ? S.now : Date.now());
function fmtWhen(ts) {
  const d = new Date(ts), n = nowDate();
  const days = Math.round((d - n) / 86400000);
  const time = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  if (Math.abs(d - n) < 60000) return "just now";
  if (days === 0 && d.toDateString() === n.toDateString()) return "today " + time;
  if (days < 0) return `${-days || 1}d ago`;
  return `in ${days || 1}d`;
}
function fmtSecs(s) {
  if (s == null) return "–";
  if (s < 60) return `${Math.round(s)} sec`;
  if (s < 3600) return `${Math.round(s / 60)} min`;
  if (s < 86400) return `${(s / 3600).toFixed(1)} hr`;
  return `${(s / 86400).toFixed(1)} days`;
}

// ---------- dashboard ----------
async function refresh() {
  S = await api("/api/state");
  renderClock(); renderKpis(); renderApprovals(); renderBoard(); renderGuide();
  if (openLeadId) await renderDrawer();
}
function renderClock() {
  const d = nowDate();
  const label = d.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" }) + " " +
    d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  $("#clock-text").innerHTML = (S.clock_offset_days ? `Clock <b>+${S.clock_offset_days}d</b> · ` : "Today · ") + esc(label);
}
function renderKpis() {
  const m = S.metrics;
  const tiles = [
    ["Speed to lead", fmtSecs(m.median_response_seconds), "median first reply"],
    ["Lead to quote", fmtSecs(m.median_quote_seconds), "median, priced and sent"],
    ["Open pipeline", money(m.open_pipeline), "per month, quotes out"],
    ["Won", money(m.won_mrr), "new monthly recurring"],
    ["Follow-ups sent", m.follow_ups_sent, "automatically, none by hand"],
    ["Collected", money(m.collected), m.win_rate == null ? "first invoices" : `win rate ${m.win_rate}%`],
  ];
  const prev = [...document.querySelectorAll(".kpi .v")].map((e) => e.textContent);
  $("#kpis").innerHTML = tiles.map(([l, v, s]) =>
    `<div class="card kpi"><div class="l">${esc(l)}</div><div class="v num">${esc(v)}</div><div class="s">${esc(s)}</div></div>`).join("");
  document.querySelectorAll(".kpi").forEach((el, i) => { if (prev.length && prev[i] !== $(".v", el).textContent) el.classList.add("flash"); });
}
function renderApprovals() {
  const el = $("#approvals"), items = S.approvals || [];
  el.hidden = !items.length;
  if (!items.length) return;
  const rows = items.map((it) => it.type === "quote"
    ? `<div class="ap-row"><div class="ap-main"><span><b>${esc(it.company)}</b> <span class="pill warn">Quote</span></span>
        <span class="muted small">${esc(it.number)} · ${money(it.monthly)}/mo · held: ${esc(it.why)}</span></div>
        <div class="ap-actions"><button class="btn sm" data-act="open-lead" data-open="${esc(it.lead_id)}" data-opentab="quote" type="button">Review</button>
        <button class="btn sm brass" data-act="approve-quote" data-q="${esc(it.id)}" type="button">Approve &amp; send</button></div></div>`
    : `<div class="ap-row"><div class="ap-main"><span><b>${esc(it.company)}</b> <span class="pill info">${esc(it.label)}</span></span>
        <span class="muted small">${esc(it.subject)}</span></div>
        <div class="ap-actions"><button class="btn sm" data-act="open-lead" data-open="${esc(it.lead_id)}" data-opentab="emails" type="button">Review &amp; edit</button>
        <button class="btn sm brass" data-act="approve-msg" data-id="${esc(it.id)}" type="button">Approve &amp; send</button>
        <button class="btn sm ghost" data-act="skip-msg" data-id="${esc(it.id)}" type="button">Skip</button></div></div>`).join("");
  el.innerHTML = `<div class="ap-h"><h2>Needs your approval</h2><span class="pill warn">${items.length}</span>
    <span class="muted small">Nothing here goes out until you approve it.</span></div>${rows}`;
}
function leadCard(l) {
  const q = l.quote;
  const tags = [];
  if (l.sample) tags.push('<span class="pill sample">Sample</span>');
  if (q && q.status === "sent" && q.viewed) tags.push('<span class="pill internal">Viewed</span>');
  if (l.next_follow_up) tags.push(`<span class="pill info">Follow-up ${esc(fmtWhen(l.next_follow_up))}</span>`);
  if (l.stage === "review") tags.push('<span class="pill warn">Approve quote</span>');
  if (l.pending_approvals) tags.push('<span class="pill warn">Approve email</span>');
  if (l.stage === "replied") tags.push('<span class="pill brass">Call back</span>');
  const fresh = !lastLeadIds.has(l.id) && lastLeadIds.size ? " new-arrival" : "";
  return `<button class="lead${fresh}" data-lead="${esc(l.id)}" type="button">
    <div class="co">${esc(l.company)}</div>
    <div class="meta">${esc(l.contact || "")}${l.city ? " · " + esc(l.city) : ""}</div>
    <div class="meta num">${q && q.monthly ? `${esc(q.number)} · ${money(q.monthly)}/mo` : esc(l.source || "")}</div>
    <div class="tags">${tags.join("")}</div></button>`;
}
function renderBoard() {
  const by = Object.fromEntries(STAGES.map(([k]) => [k, []]));
  S.leads.forEach((l) => (by[l.stage] || by.new).push(l));
  $("#board").innerHTML = STAGES.map(([k, label]) => `<div class="col"><div class="col-h"><span>${label}</span><span>${by[k].length}</span></div>
    ${by[k].map(leadCard).join("")}</div>`).join("");
  $("#board-sub").textContent = `${S.leads.length} leads`;
  lastLeadIds = new Set(S.leads.map((l) => l.id));
  $("#sample-bar").hidden = !S.leads.some((l) => l.sample);
}
document.addEventListener("click", (e) => {
  const card = e.target.closest("[data-lead]");
  if (card) openLead(card.dataset.lead);
});

// ---------- lead drawer ----------
async function openLead(id, tab = "quote") { openLeadId = id; openTab = tab; await renderDrawer(); }
function closeDrawer() { openLeadId = null; $("#overlay-root").innerHTML = ""; }
document.addEventListener("keydown", (e) => { if (e.key === "Escape") { if ($(".modal iframe")) closeCustomer(); else closeDrawer(); } });

function optionCard(o, q, { internal = true } = {}) {
  const rec = o.tier === q.recommended;
  let save = "";
  if (o.savings_monthly != null) {
    save = o.savings_monthly > 0
      ? `<div class="save pos">Saves ${money(o.savings_monthly)}/mo vs. today</div>`
      : `<div class="save neg">${money(-o.savings_monthly)}/mo more than today${o.tier === "best" ? ", includes floor care" : ""}</div>`;
  }
  return `<div class="opt${rec ? " rec" : ""}">
    ${rec ? '<span class="pill brass badge-rec">Recommended</span>' : ""}
    <div class="tier">${esc(o.label)}</div><div class="nm">${esc(o.name)}</div>
    <div class="price num">${money(o.monthly)}<small>/month</small></div>
    <div class="muted small num">${money(o.per_sqft)}/sq ft · ${money(o.annual)}/yr</div>
    ${save}
    <ul>${o.includes.map((i) => `<li>${esc(i)}</li>`).join("")}</ul>
    ${internal ? `<div class="internal-box">🔒 Cost ${money(o.cost)} · margin ${o.margin_pct}% · ${o.labor_hours} labor hrs/mo</div>` : ""}
  </div>`;
}
function comparisonTable(q) {
  if (!q.current_monthly) return '<p class="muted small">No current vendor price on file, so there\'s nothing to compare against. Add it in the lead details to show savings.</p>';
  const rows = q.options.map((o) => `<tr><td>${esc(o.label)} · ${esc(o.name)}</td><td class="num">${money(o.compare_to)}</td>
    <td class="num">${money(o.monthly)}</td><td class="num" style="color:var(${o.savings_monthly > 0 ? "--ok" : "--muted"})">${money(o.savings_monthly)}</td>
    <td class="num">${money(o.savings_annual)}</td></tr>`).join("");
  const sc = q.supplies_compare.formats.map((f) => `<tr><td>${esc(f.label)}</td><td class="num">$${f.cost_per_use.toFixed(4)}</td><td class="num">${money(f.monthly)}</td></tr>`).join("");
  return `<table class="compare"><thead><tr><th>Option</th><th>Pays today</th><th>Our price</th><th>Saves /mo</th><th>Saves /yr</th></tr></thead><tbody>${rows}</tbody></table>
    <p class="muted small">"Pays today" is the current vendor (${money(q.current_monthly)}/mo). For Best, it adds the restroom supplies they buy now (${money(q.current_supplies_monthly)}/mo est.), since Best includes them.</p>
    <h3>Restroom supplies, cost per use (${q.supplies_compare.occupants} occupants)</h3>
    <table class="compare"><thead><tr><th>Format</th><th>Per use</th><th>Per month</th></tr></thead><tbody>${sc}</tbody></table>`;
}
function mailItem(m) {
  if (m.status === "pending") {
    const kind = { acknowledgement: "Instant reply", follow_up: "Follow-up", welcome: "Welcome + invoice" }[m.kind] || m.kind;
    return `<details class="mail pending" open><summary><span class="pill">${esc(kind)}</span><span class="subj">${esc(m.subject)}</span><span class="pill warn">Waiting for you</span></summary>
      <div class="mail-edit"><div class="muted small">To: ${esc(m.to)}. Edit anything, then approve.</div>
        <label>Subject <input data-f="subject" value="${esc(m.subject)}"></label>
        <label>Message <textarea data-f="body" rows="10">${esc(m.body)}</textarea></label>
        <div class="actions" style="margin:0"><button class="btn brass" data-act="approve-msg" data-id="${esc(m.id)}" type="button">Approve &amp; send</button>
        <button class="btn" data-act="skip-msg" data-id="${esc(m.id)}" type="button">Skip this email</button></div></div></details>`;
  }
  const pending = m.status === "scheduled";
  const label = pending ? `<span class="pill info">Scheduled ${esc(fmtWhen(m.scheduled_for))}</span>`
    : m.status === "cancelled" ? '<span class="pill">Not sent</span>'
    : m.status === "failed" ? '<span class="pill err">Failed</span>'
    : `<span class="pill ok">${m.delivery === "outbox" ? "Sent to outbox" : "Emailed"}</span>`;
  const kind = { acknowledgement: "Instant reply", quote: "Quote", follow_up: "Follow-up", welcome: "Welcome + invoice" }[m.kind] || m.kind;
  const subject = m.subject || (pending ? "Written when it goes out, from what the prospect has done by then" : "");
  return `<details class="mail${pending ? " scheduled" : ""}"><summary><span class="pill">${esc(kind)}</span><span class="subj">${esc(subject)}</span>${label}</summary>
    ${m.body ? `<pre>To: ${esc(m.to)}\n\n${esc(m.body)}</pre>` : ""}</details>`;
}
async function renderDrawer() {
  const d = await api(`/api/leads/${openLeadId}`);
  const l = d.lead, q = d.quotes[d.quotes.length - 1], inv = d.invoices[0];
  const stage = Object.fromEntries(STAGES)[l.stage];
  const facts = [["Facility", S.facility_types[l.facility_type] || "–"], ["Size", l.sqft ? Number(l.sqft).toLocaleString() + " sq ft" : "–"],
    ["Visits", l.visits_per_week ? l.visits_per_week + "/week" : "–"], ["Pays today", l.current_monthly ? money(l.current_monthly) + "/mo" : "–"],
    ["Contact", `${l.contact || ""}${l.title ? ", " + l.title : ""}`], ["Email", l.email], ["Phone", l.phone || "–"], ["Source", l.source]];
  const tabs = [["quote", "Quote"], ["emails", `Emails (${d.messages.length})`], ["timeline", "Timeline"], ["details", "Lead details"]];
  if (inv) tabs.splice(1, 0, ["invoice", "Invoice"]);
  if (!tabs.some(([k]) => k === openTab)) openTab = "quote";

  let body = "";
  if (openTab === "quote") {
    if (!q) body = '<div class="notice">This lead didn\'t include size and frequency, so it got the instant reply asking for them. Add them under Lead details to price it.</div>';
    else {
      const notes = [];
      if (q.status === "draft") notes.push(`<div class="notice">${{ approval: "Held for your approval: your settings have a person approve every quote first. Check it, then send.",
        limit: "Held for review: this job is over the auto-send limit or under the target margin. Check it, then send.",
        revised: "Re-priced. Review it, then send." }[q.hold_reason] || (q.needs_review ? "Held for review: over the auto-send limit or under target margin." : "Draft. Not sent yet.")}</div>`);
      if (q.status === "sent") notes.push(`<div class="notice info">Sent ${esc(fmtWhen(q.sent_at))} by ${esc(q.sent_by)}. ${q.viewed_at ? "Opened by the prospect " + esc(fmtWhen(q.viewed_at)) + "." : "Not opened yet."} Valid until ${new Date(q.valid_until).toLocaleDateString()}.</div>`);
      if (q.status === "won") notes.push(`<div class="notice ok">Accepted online by ${esc(q.signed_by)} ${esc(fmtWhen(q.accepted_at))}: ${esc(q.options.find((o) => o.tier === q.selected).name)}.</div>`);
      body = `${notes.join("")}
        <div class="options">${TIER_ORDER.map((t) => optionCard(q.options.find((o) => o.tier === t), q)).join("")}</div>
        <p class="muted small">Plus a one-time initial deep clean: ${money(q.deep_clean)}. 🔒 lines are internal and never appear on the customer's proposal.</p>
        <div class="actions">
          ${q.status === "draft" ? `<select id="send-tier" style="width:auto">${TIER_ORDER.map((t) => `<option value="${t}"${t === q.selected ? " selected" : ""}>Recommend ${esc(q.options.find((o) => o.tier === t).name)}</option>`).join("")}</select>
            <button class="btn brass" data-act="send" data-q="${esc(q.id)}" type="button">Approve &amp; send</button>` : ""}
          ${["sent", "won"].includes(q.status) ? `<button class="btn primary" data-act="customer" data-token="${esc(q.token)}" type="button">Open customer's view</button>` : ""}
          ${q.status === "sent" ? `<button class="btn" data-act="reply" type="button">Log a reply from them</button>` : ""}
        </div>
        <h3>Cost comparison</h3>${comparisonTable(q)}`;
    }
  } else if (openTab === "emails") {
    body = d.messages.length ? d.messages.map(mailItem).join("") : '<p class="muted">No emails yet.</p>';
  } else if (openTab === "timeline") {
    body = `<ul class="timeline">${d.events.slice().reverse().map((e) => `<li><span class="dot ${esc(e.kind)}"></span><span>${esc(e.text)}</span><time>${esc(fmtWhen(e.created_at))}</time></li>`).join("")}</ul>`;
  } else if (openTab === "invoice" && inv) {
    body = `<div class="notice ${inv.status === "paid" ? "ok" : "info"}">${esc(inv.number)} · ${inv.status === "paid" ? `Paid ${esc(fmtWhen(inv.paid_at))} (${esc(inv.method)})` : `Open, due ${new Date(inv.due).toLocaleDateString()}`}</div>
      <table class="invoice-table">${inv.lines.map((x) => `<tr><td>${esc(x.description)}</td><td class="num">${money(x.amount)}</td></tr>`).join("")}
      <tr class="total"><td>Total</td><td class="num">${money(inv.total)}</td></tr></table>
      <p class="muted small">Then ${money(l.mrr)}/month on the same day each month.</p>`;
  } else if (openTab === "details") {
    const ft = Object.entries(S.facility_types).map(([k, v]) => `<option value="${k}"${k === l.facility_type ? " selected" : ""}>${esc(v)}</option>`).join("");
    body = `${l.notes ? `<p><b>Their note:</b> ${esc(l.notes)}</p>` : ""}
      <form id="requote-form"><div class="grid2">
        <label>Facility type <select name="facility_type">${ft}</select></label>
        <label>Square feet <input name="sqft" type="number" min="500" value="${esc(l.sqft || "")}"></label>
        <label>Visits per week <input name="visits_per_week" type="number" min="1" max="7" value="${esc(l.visits_per_week || "")}"></label>
        <label>Occupants <input name="occupants" type="number" min="0" value="${esc(l.occupants || "")}"></label>
        <label>Pays current vendor ($/mo) <input name="current_monthly" type="number" min="0" value="${esc(l.current_monthly || "")}"></label>
      </div><div class="actions"><button class="btn primary" type="submit">Re-price quote</button></div>
      <p class="muted small">Re-pricing makes a new draft for review. An open quote is withdrawn and its follow-ups stop.</p></form>`;
  }

  $("#overlay-root").innerHTML = `<div class="scrim" data-act="close"></div>
    <aside class="drawer" role="dialog" aria-label="${esc(l.company)}">
      <div class="drawer-h"><div class="top"><div style="flex:1">
        <div style="display:flex;gap:6px;margin-bottom:6px"><span class="pill">${esc(stage)}</span>${l.sample ? '<span class="pill sample">Sample</span>' : ""}
        ${l.first_response_seconds ? `<span class="pill ok">Replied in ${fmtSecs(l.first_response_seconds)}</span>` : ""}</div>
        <h2>${esc(l.company)}</h2></div><button class="btn ghost" data-act="close" type="button" aria-label="Close">✕</button></div>
        <div class="facts">${facts.map(([k, v]) => `<div><span>${esc(k)}</span>${esc(v)}</div>`).join("")}</div>
        <div class="tabs" role="tablist">${tabs.map(([k, v]) => `<button class="tab" role="tab" data-tab="${k}" aria-selected="${k === openTab}" type="button">${esc(v)}</button>`).join("")}</div>
      </div>
      <div class="drawer-b">${body}</div>
    </aside>`;
}
document.addEventListener("click", async (e) => {
  const t = e.target.closest("[data-tab],[data-act]");
  if (!t) return;
  if (t.dataset.tab) { openTab = t.dataset.tab; return renderDrawer(); }
  const act = t.dataset.act;
  try {
    if (act === "close") closeDrawer();
    if (act === "send") { await api(`/api/quotes/${t.dataset.q}/send`, { method: "POST", body: { tier: $("#send-tier").value } }); toast("Quote sent. Follow-ups scheduled."); await refresh(); }
    if (act === "customer") openCustomer(t.dataset.token);
    if (act === "open-lead") openLead(t.dataset.open, t.dataset.opentab || "quote");
    if (act === "approve-quote") { await api(`/api/quotes/${t.dataset.q}/send`, { method: "POST", body: {} }); toast("Quote approved and sent. Follow-ups scheduled."); await refresh(); }
    if (act === "approve-msg") {
      const box = t.closest(".mail"), body = box ? { subject: $("[data-f=subject]", box).value, body: $("[data-f=body]", box).value } : {};
      await api(`/api/messages/${t.dataset.id}/approve`, { method: "POST", body }); toast("Approved and sent."); await refresh();
    }
    if (act === "skip-msg") { await api(`/api/messages/${t.dataset.id}/skip`, { method: "POST" }); toast("Skipped. It won't be sent."); await refresh(); }
    if (act === "reply") {
      const text = prompt("What did they say? (Logging a reply stops the automatic follow-ups.)", "");
      if (text !== null) { await api(`/api/leads/${openLeadId}/reply`, { method: "POST", body: { text } }); toast("Reply logged. Follow-ups stopped."); await refresh(); }
    }
  } catch (err) { toast(err.message); }
});
document.addEventListener("submit", async (e) => {
  if (e.target.id === "requote-form") {
    e.preventDefault();
    try { await api(`/api/leads/${openLeadId}/requote`, { method: "POST", body: Object.fromEntries(new FormData(e.target)) }); openTab = "quote"; toast("Re-priced. Review and send."); await refresh(); }
    catch (err) { toast(err.message); }
  }
});

// ---------- customer's view (the proposal page in a frame) ----------
function openCustomer(tok) {
  const wrap = document.createElement("div");
  wrap.id = "customer-view";
  wrap.innerHTML = `<div class="scrim top" data-close-customer></div><div class="modal"><div class="modal-h">
    <b>Customer's view</b><span style="opacity:.7">what the prospect sees when they open the link in the email</span><span class="spacer"></span>
    <a class="btn sm" href="/p/${esc(tok)}" target="_blank" rel="noopener">Open in new tab ↗</a>
    <button class="btn sm" data-close-customer type="button">Back to VectorSales</button></div>
    <iframe src="/p/${esc(tok)}?embed=1" title="Customer proposal"></iframe></div>`;
  document.body.append(wrap);
}
function closeCustomer() { $("#customer-view")?.remove(); refresh(); }
document.addEventListener("click", (e) => { if (e.target.closest("[data-close-customer]")) closeCustomer(); });
window.addEventListener("message", (e) => { if (e.origin === location.origin && e.data?.vs === "changed") refresh(); });

// ---------- new lead / settings ----------
$("#open-new").addEventListener("click", () => {
  const ft = Object.entries(S.facility_types).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("");
  $("#overlay-root").innerHTML = `<div class="scrim" data-act="close"></div><aside class="drawer"><div class="drawer-h" style="padding-bottom:14px"><div class="top"><h2 style="flex:1">New lead</h2><button class="btn ghost" data-act="close" type="button">✕</button></div></div>
    <form class="drawer-b" id="new-form"><p class="muted small">Same as a website or CRM lead: it gets the instant reply, and a quote if size and frequency are filled in.</p><div class="grid2">
    <label>Company <input name="company" required></label><label>Contact name <input name="contact"></label>
    <label>Email <input name="email" type="email" required></label><label>Phone <input name="phone"></label>
    <label>Facility type <select name="facility_type">${ft}</select></label><label>Square feet <input name="sqft" type="number" min="500"></label>
    <label>Visits per week <input name="visits_per_week" type="number" min="1" max="7"></label><label>Pays current vendor ($/mo) <input name="current_monthly" type="number" min="0"></label>
    </div><label style="margin-top:12px">Notes <textarea name="notes" rows="3"></textarea></label>
    <label style="margin-top:12px;display:flex;gap:8px;align-items:center"><input type="checkbox" name="sample" checked style="width:auto"> Sample lead (stays in the outbox, never emailed)</label>
    <div class="actions"><button class="btn brass" type="submit">Add lead</button></div></form></aside>`;
  $("#new-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = Object.fromEntries(new FormData(e.target));
    body.sample = !!body.sample;
    try { const l = await api("/api/leads", { method: "POST", body }); await refresh(); openLead(l.id); } catch (err) { toast(err.message); }
  });
});
$("#open-settings").addEventListener("click", () => {
  const p = S.profile, r = S.rates;
  const rate = (k, label, step = "0.01") => `<label>${label} <input name="${k}" type="number" step="${step}" value="${r[k]}"></label>`;
  $("#overlay-root").innerHTML = `<div class="scrim" data-act="close"></div><aside class="drawer"><div class="drawer-h" style="padding-bottom:14px"><div class="top"><h2 style="flex:1">Settings</h2><button class="btn ghost" data-act="close" type="button">✕</button></div></div>
    <form class="drawer-b" id="settings-form">
    <h3 style="margin-top:0">Your company (on every email and proposal)</h3><div class="grid2">
      <label>Company <input name="p_company" value="${esc(p.company)}"></label><label>Sender name <input name="p_sender" value="${esc(p.sender)}"></label>
      <label>Phone <input name="p_phone" value="${esc(p.phone)}"></label><label>Reply-to email <input name="p_email" value="${esc(p.email)}"></label></div>
    <h3>Pricing</h3><div class="grid3">
      ${rate("wage", "Cleaner wage ($/hr)")}${rate("burden_pct", "Labor burden %", "0.1")}${rate("supplies_pct", "Supplies % of labor", "0.1")}
      ${rate("overhead_pct", "Overhead %", "0.1")}${rate("margin_pct", "Target margin %", "0.1")}${rate("min_hours_per_visit", "Min hours per visit", "0.25")}
      ${rate("deep_clean_per_sqft", "Deep clean $/sq ft", "0.005")}${rate("deep_clean_min", "Deep clean minimum $", "1")}${rate("floor_care_per_sqft", "Floor care $/sq ft", "0.005")}</div>
    <h3>Who runs each step?</h3>
    <p class="muted small" style="margin-top:0">Choose per step. "I approve first" writes the email or quote and holds it in your approvals queue until you send it. You can edit it first.</p>
    ${AUTO_STEPS.map(([k, label, hint]) => `<div class="auto-row"><div><b>${esc(label)}</b><div class="muted small">${esc(hint)}</div></div>
      <select name="a_${k}"><option value="auto"${S.automation[k] === "auto" ? " selected" : ""}>Automatic</option>
      <option value="approve"${S.automation[k] === "approve" ? " selected" : ""}>I approve first</option></select></div>`).join("")}
    <div class="grid2" style="margin-top:12px">${rate("auto_send_under", "Auto-send quotes under ($/mo)", "100")}</div>
    <p class="muted small">Even on automatic, quotes at or above that amount, or under your target margin, wait for your approval. Follow-ups run on day 2, 5 and 9 and stop when the prospect replies, accepts or declines.</p>
    <p class="muted small">Email: ${CONFIG.email ? "SMTP connected" : "not connected (messages stay in the outbox)"} · AI personalization: ${CONFIG.ai ? "on" : "off (templates)"}</p>
    <div class="actions"><button class="btn brass" type="submit">Save</button></div></form></aside>`;
  $("#settings-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = Object.fromEntries(new FormData(e.target));
    const profile = {}, rates = {}, automation = {};
    for (const [k, v] of Object.entries(f)) (k.startsWith("p_") ? (profile[k.slice(2)] = v) : k.startsWith("a_") ? (automation[k.slice(2)] = v) : (rates[k] = v));
    try { await api("/api/settings", { method: "PUT", body: { profile, rates, automation } }); toast("Saved. New quotes use these rates."); closeDrawer(); refresh(); } catch (err) { toast(err.message); }
  });
});
$("#clock-advance").addEventListener("click", async () => {
  const r = await api("/api/clock/advance", { method: "POST", body: { days: 1 } });
  const out = r.sent.filter((m) => m.status === "sent").length, wait = r.sent.length - out;
  toast(!r.sent.length ? "A day passes. Nothing due."
    : `A day passes. ${out ? `${out} follow-up${out > 1 ? "s" : ""} sent automatically. ` : ""}${wait ? `${wait} waiting for your approval.` : ""}`);
  refresh();
});

// ---------- the guided demo ----------
const demo = { leadId: null, step: 0, followups: 0, busy: false };
try { Object.assign(demo, JSON.parse(sessionStorage.getItem("vs_demo") || "{}"), { busy: false }); } catch { /* fresh */ }
const saveDemo = () => { try { sessionStorage.setItem("vs_demo", JSON.stringify(demo)); } catch { /* fine */ } };

const demoLead = () => S && S.leads.find((l) => l.id === demo.leadId);
const STEPS = [
  { title: "A lead comes in", text: "A practice manager fills in the quote form at 9:14 pm. Nobody is in the office.", btn: "Send the sample request" },
  { title: "Instant reply", text: "Answered in seconds, day or night, from your company's name.", btn: "Show the emails" },
  { title: "Quote priced and sent", text: "Three fixed-price options, priced from labor, with a cost comparison against their current vendor.", btn: "Open the quote" },
  { title: "Follow-up on autopilot", text: "Day 2, 5 and 9. Each one is written when it goes out, from what the prospect has done.", btn: "Skip ahead 2 days" },
  { title: "Customer accepts online", text: "They pick an option and accept from the link. Follow-ups stop on their own.", btn: "Open the customer's view" },
  { title: "Invoice and cash", text: "First invoice goes out with the welcome email and can be paid on the spot.", btn: "Show the invoice" },
];
const waitingFor = (label) => S.approvals.some((a) => a.lead_id === demo.leadId && a.label === label);
function stepBtn(i, s) {
  const l = demoLead(), A = S.automation;
  if (i === 1 && A.acknowledgement === "approve" && waitingFor("Instant reply")) return "Approve the instant reply";
  if (i === 2 && l && l.quote && l.quote.status === "draft") return "Approve & send the quote";
  if (i === 3 && demo.followups === 1) return "Skip ahead 3 more days";
  if (i === 3 && demo.followups >= 2) return "Done: move on";
  if (i === 4 && demo.step === 4) return "Accept for her";
  if (i === 5 && waitingFor("Invoice + welcome email")) return "Approve the invoice & pay it";
  return s.btn;
}
function narration() {
  const l = demoLead(), q = l && l.quote, A = S.automation;
  const name = "Priya";
  const held = q && q.status === "draft";
  switch (demo.step) {
    case 0: return "<b>The pitch:</b> cleaning companies lose jobs to whoever answers first and follows up longest. VectorSales does both on its own. Press <b>Send the sample request</b> to submit a made-up lead through the real website form endpoint.";
    case 1: {
      const reply = A.acknowledgement === "approve" && waitingFor("Instant reply")
        ? "Your settings have you approve every step, so the instant reply is written and waiting in the queue above. Nothing has reached " + name + " yet."
        : `The instant reply went out in <b>${fmtSecs(l.first_response_seconds)}</b>.`;
      const quote = held ? " The quote is priced and held for you too." : " The quote was priced and sent right behind it, because it's under the auto-send limit. Larger or low-margin jobs wait for one click.";
      return `<b>${esc(l.company)}</b> just arrived. ${reply}${quote}`;
    }
    case 2: return held
      ? `The quote is priced, and nothing has gone to ${name}. Three fixed options, recommended <b>Smart schedule</b> at <b>${money(q.monthly)}/mo</b>. The purple 🔒 lines are your cost and margin. Review them, then approve and send.`
      : `${name} has three fixed prices in her inbox, ${fmtSecs(l.first_quote_seconds)} after asking. The recommended <b>Smart schedule</b> option comes in at <b>${money(q && q.monthly)}/mo</b>. The purple 🔒 lines are your cost and margin, kept off her copy.`;
    case 3: {
      const ask = A.follow_up === "approve";
      if (demo.followups === 0) return ask
        ? `${name} hasn't answered. Day 2, 5 and 9 follow-ups are scheduled. Each is written when it's due, then waits in your queue for your OK. Skip ahead to see the first one arrive.`
        : `${name} hasn't answered. Nobody on your team has to remember her: day 2, 5 and 9 follow-ups are already scheduled. Skip ahead to watch them go out.`;
      return ask
        ? `A follow-up is waiting in the queue above, written from what ${name} has done so far. Approve it, skip ahead again, or move on. Anything still waiting is cancelled when she accepts.`
        : `The day-${demo.followups === 1 ? "2 check-in" : "5 value email"} went out automatically${demo.followups === 2 ? ". It leads with her own savings number" : ""}. ${demo.followups === 1 ? "Skip ahead again, or move on." : "Now let's have her say yes."}`;
    }
    case 4: return `This is ${name}'s side: the link from her email. She can compare options, see her savings, and accept by typing her name. Try it, or press <b>Accept for her</b>.`;
    case 5: return waitingFor("Invoice + welcome email")
      ? `Accepted, and the deal is Won. The day-9 email was cancelled. The invoice and welcome email are drafted and waiting for you, and ${name} can't see or pay the invoice until you approve it.`
      : `Accepted. The day-9 email was cancelled, the deal moved to Won, and invoice + welcome email went out together. <b>From form to signed contract, no one typed a quote or a follow-up.</b>`;
    default: return "<b>That's quote-to-cash:</b> lead in, quote out, follow-up on autopilot, contract signed, invoice paid. Reset and run it again any time, or turn on Autoplay.";
  }
}
function renderGuide() {
  if (demo.leadId && !demoLead()) Object.assign(demo, { leadId: null, step: 0, followups: 0 });
  const l = demoLead();
  if (l && demo.step < 5 && l.stage === "won") demo.step = 5;
  const modes = Object.values(S.automation);
  const preset = modes.every((m) => m === "auto") ? "auto" : modes.every((m) => m === "approve") ? "approve" : "custom";
  document.querySelectorAll("[data-preset]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.preset === preset)));
  $("#mode-note").textContent = preset === "auto" ? "Big or low-margin quotes still wait for you."
    : preset === "approve" ? "Nothing goes out until you approve it." : "Custom mix. Set each step in Settings.";
  $("#steps").innerHTML = STEPS.map((s, i) => {
    const btn = stepBtn(i, s);
    const state = i < demo.step ? "done" : i === demo.step ? "current" : "";
    return `<li class="step ${state}"><span class="n">${i < demo.step ? "✓" : i + 1}</span><h4>${esc(s.title)}</h4><p>${esc(s.text)}</p>
      ${i === demo.step || (i === 4 && demo.step === 4) ? `<button class="btn sm ${i === demo.step ? "brass" : ""}" data-step="${i}" type="button"${demo.busy ? " disabled" : ""}>${esc(btn)}</button>` : ""}
      ${i === 4 && demo.step === 4 ? `<button class="btn sm" data-step="4.5" type="button">Open the customer's view</button>` : ""}</li>`;
  }).join("");
  $("#narration").innerHTML = narration();
  saveDemo();
}
async function runStep(i) {
  if (demo.busy) return;
  demo.busy = true; renderGuide();
  try {
    if (i === 0) {
      // Same endpoint the public quote form posts to.
      const l = await api("/api/demo/lead", { method: "POST" });
      Object.assign(demo, { leadId: l.id, step: 1, followups: 0 });
      closeDrawer(); await refresh();
    } else if (i === 1) {
      const d = await api(`/api/leads/${demo.leadId}`);
      const ack = d.messages.find((m) => m.kind === "acknowledgement" && m.status === "pending");
      if (ack) { await api(`/api/messages/${ack.id}/approve`, { method: "POST", body: {} }); toast("Approved. The instant reply goes out now."); }
      demo.step = 2; await openLead(demo.leadId, "emails");
    } else if (i === 2) {
      const d = await api(`/api/leads/${demo.leadId}`);
      const dq = d.quotes[d.quotes.length - 1];
      if (dq && dq.status === "draft") { await api(`/api/quotes/${dq.id}/send`, { method: "POST", body: {} }); toast("Quote approved and sent. Follow-ups scheduled."); }
      demo.step = 3; await openLead(demo.leadId, "quote");
    } else if (i === 3) {
      if (demo.followups >= 2) { demo.step = 4; }
      else {
        const r = await api("/api/clock/advance", { method: "POST", body: { days: demo.followups === 0 ? 2 : 3 } });
        demo.followups++;
        const out = r.sent.filter((m) => m.status === "sent").length, wait = r.sent.length - out;
        toast(`${demo.followups === 1 ? "2 days" : "3 more days"} later: ` + ((out ? `${out} follow-up${out > 1 ? "s" : ""} sent automatically. ` : "") + (wait ? `${wait} waiting for your approval.` : "") || "nothing due."));
        await openLead(demo.leadId, "emails");
      }
    } else if (i === 4.5) {
      demo.busy = false;
      const d = await api(`/api/leads/${demo.leadId}`);
      openCustomer(d.quotes[d.quotes.length - 1].token);
    } else if (i === 4) {
      const d = await api(`/api/leads/${demo.leadId}`);
      const q = d.quotes[d.quotes.length - 1];
      if (q.status === "sent") {
        await fetch(`/api/public/${q.token}`); // she opens the link first
        await api(`/api/public/${q.token}/accept`, { method: "POST", body: { tier: q.recommended, name: "Priya Shah" } });
      }
      demo.step = 5; await openLead(demo.leadId, "timeline");
    } else if (i === 5) {
      const d = await api(`/api/leads/${demo.leadId}`);
      const q = d.quotes[d.quotes.length - 1];
      const welcome = d.messages.find((m) => m.kind === "welcome" && m.status === "pending");
      if (welcome) await api(`/api/messages/${welcome.id}/approve`, { method: "POST", body: {} });
      if (d.invoices[0] && d.invoices[0].status !== "paid") await api(`/api/public/${q.token}/pay`, { method: "POST" });
      demo.step = 6; await openLead(demo.leadId, "invoice");
      toast("Invoice paid (sample, no charge). Won and Collected updated.");
    }
  } catch (err) { toast(err.message); }
  demo.busy = false;
  await refresh();
}
document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-step]");
  if (b) runStep(Number(b.dataset.step));
});
document.addEventListener("click", async (e) => {
  const b = e.target.closest("[data-preset]");
  if (!b) return;
  const automation = Object.fromEntries(Object.keys(S.automation).map((k) => [k, b.dataset.preset]));
  try {
    await api("/api/settings", { method: "PUT", body: { automation } });
    toast(b.dataset.preset === "auto" ? "Autopilot: every step runs on its own. Big or low-margin quotes still wait."
      : "Approval mode: nothing goes out until you approve it.");
    await refresh();
  } catch (err) { toast(err.message); }
});
$("#demo-reset").addEventListener("click", async () => {
  if (!confirm("Reset to the sample pipeline? This clears everything, including leads you added.")) return;
  await api("/api/demo/reset", { method: "POST" });
  Object.assign(demo, { leadId: null, step: 0, followups: 0 }); closeDrawer(); $("#customer-view")?.remove();
  lastLeadIds = new Set(); await refresh(); toast("Sample data reset.");
});
$("#demo-autoplay").addEventListener("click", async () => {
  const btn = $("#demo-autoplay");
  if (btn.dataset.playing) { btn.dataset.playing = ""; btn.textContent = "▶ Autoplay"; return; }
  btn.dataset.playing = "1"; btn.textContent = "■ Stop";
  if (demo.step >= 6) Object.assign(demo, { leadId: null, step: 0, followups: 0 });
  // Step 3 runs three times: two skips ahead, then move on.
  while (btn.dataset.playing && demo.step < 6) {
    const i = demo.step;
    await runStep(i);
    await sleep(i === 0 ? 4500 : 6500);
  }
  btn.dataset.playing = ""; btn.textContent = "▶ Autoplay";
});

// ---------- start ----------
(async () => {
  try { CONFIG = await api("/api/config"); } catch { /* shown on first call */ }
  if (CONFIG.auth && !token()) return showLogin();
  if (!CONFIG.demo) $("#guide").hidden = true;
  try { await refresh(); } catch (err) { if (err.message !== "Please sign in.") toast(err.message); }
  setInterval(() => {
    const a = document.activeElement;
    const typing = a && a.closest && a.closest(".drawer") && ["INPUT", "TEXTAREA", "SELECT"].includes(a.tagName);
    if (!document.hidden && !$("#customer-view") && !typing) refresh().catch(() => {});
  }, 15000);
})();
