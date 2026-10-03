const $ = (id) => document.getElementById(id);

const state = {
  profile: null,
  approved: false,
  holdings: [],
  quotes: {},
  signals: {},
  selected: null,
  range: "6mo",
  history: null,
  pollTimer: null,
  signalTimer: null,
  alertTimer: null,
  settings: { cash_available: null, max_position_pct: null, alerts_enabled: true, browser_notifications: true, sound: true },
  alerts: [],
  seenAlertIds: null,
  priceAlerts: [],
};

const VIEWS = {
  profile: ["Client Profile", "Step 1: paste the case study, review what was extracted, then approve it."],
  portfolio: ["Live Portfolio", "Step 2: track what you invested in with live prices and a signal for each holding."],
  stock: ["Stock Signal", "Step 3: live chart and rule-based buy / hold / sell timing tailored to the approved client profile."],
  alerts: ["Alerts", "Automatic buy / sell notifications for your holdings, with how many shares to trade."],
  analysis: ["Deep AI Analysis", "Full multi-agent research report for one security, using the approved client profile."],
  scanner: ["Opportunity Scanner", "Rank several companies by how well they fit the client right now."],
  backtest: ["Backtest", "Replay the AI agents over past dates and score their calls."],
  settings: ["Settings", "Agent team, live refresh rate and model overrides."],
};

// ---------- utils ----------
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (v, cur = "USD") =>
  v == null || Number.isNaN(v) ? "—" : new Intl.NumberFormat("en-US", { style: "currency", currency: cur, maximumFractionDigits: 2 }).format(v);
const num = (v, d = 2) => (v == null || Number.isNaN(v) ? "—" : Number(v).toLocaleString("en-US", { maximumFractionDigits: d, minimumFractionDigits: d }));
const pct = (v) => (v == null || Number.isNaN(v) ? "—" : `${v >= 0 ? "+" : ""}${v.toFixed(2)}%`);
const signed = (v, cur) => (v == null ? "—" : `${v >= 0 ? "+" : "−"}${money(Math.abs(v), cur)}`);
const dirClass = (v) => (v == null ? "" : v >= 0 ? "up" : "down");
const pillClass = (s) => {
  const k = String(s || "").toLowerCase().split(/[\s/]/)[0];
  return ["buy", "add", "hold", "wait", "trim", "sell", "avoid", "overweight", "underweight"].includes(k) ? k : "review";
};

let toastTimer;
function toast(msg, cls = "") {
  const el = $("toast");
  el.textContent = msg;
  el.className = `toast ${cls}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), cls === "err" ? 8000 : 4000);
}

async function api(method, path, body = null) {
  const res = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401) {
    window.location.replace("/login");
    throw new Error("Signed out.");
  }
  if (!res.ok) {
    const detail = Array.isArray(data.detail) ? data.detail.map((d) => d.msg).join("; ") : data.detail;
    throw new Error(detail || `${res.status} ${res.statusText}`);
  }
  return data;
}

function parseJsonOrNull(text, name) {
  const t = (text || "").trim();
  if (!t) return null;
  try {
    return JSON.parse(t);
  } catch (err) {
    throw new Error(`${name} is not valid JSON: ${err.message}`);
  }
}

async function busy(btn, fn) {
  const label = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = `<span class="spinner"></span>${label}`;
  try {
    return await fn();
  } finally {
    btn.disabled = false;
    btn.innerHTML = label;
  }
}

// ---------- navigation ----------
function showView(name) {
  document.querySelectorAll(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${name}`));
  document.querySelectorAll(".nav-item").forEach((b) => b.classList.toggle("active", b.dataset.view === name));
  $("viewTitle").textContent = VIEWS[name][0];
  $("viewSub").textContent = VIEWS[name][1];
  if (name === "stock") ensureStockSelected();
  if (name === "alerts") loadPriceAlerts();
}
document.querySelectorAll(".nav-item").forEach((b) => b.addEventListener("click", () => showView(b.dataset.view)));

function updateStepBadges() {
  $("stepBadge1").classList.toggle("done", state.approved);
  $("stepBadge2").classList.toggle("done", state.holdings.length > 0);
  $("stepBadge3").classList.toggle("done", Boolean(state.selected));
  const chip = $("profileChip");
  if (state.approved) {
    const name = state.profile?.client_name?.value;
    chip.textContent = `Profile: ${name || "approved"}`;
    chip.className = "chip ok";
  } else {
    chip.textContent = "No approved profile";
    chip.className = "chip warn";
  }
  $("profileState").textContent = state.approved ? "Approved" : "Not approved";
  $("profileState").className = `tag ${state.approved ? "ok" : "warn"}`;
}

// ---------- step 1: profile ----------
function statusBadge(ev) {
  if (!ev || !ev.status) return "";
  return `<span class="status-badge ${esc(ev.status)}">${esc(ev.status)}</span>`;
}

function renderProfileReview(p) {
  const box = $("profileReview");
  if (!p) {
    box.innerHTML = '<div class="empty-state">Extract a profile from the case study to review it here.</div>';
    return;
  }
  const ev = (x) => (x && x.value != null ? esc(x.value) : '<span class="muted">Not stated</span>');
  const list = (items, cls, get = (x) => x) =>
    items && items.length
      ? `<ul class="pr-list">${items.map((i) => `<li class="${cls}">${esc(get(i))}</li>`).join("")}</ul>`
      : '<div class="muted small">None extracted.</div>';

  const flows = (p.cash_flows || []).slice().sort((a, b) => (a.year || 0) - (b.year || 0));
  const timeline = flows.length
    ? `<div class="timeline">${flows
        .map((cf) => {
          const out = cf.event_type === "payment" || cf.event_type === "withdrawal";
          const freq = cf.frequency === "annual" && cf.count ? ` × ${cf.count} years` : "";
          return `<div class="tl-row"><span class="tl-year">${esc(cf.year ?? "?")}</span>
            <span>${esc(cf.event_type)}${freq}${cf.timing === "beginning_of_year" ? " · start of year" : ""}${cf.notes ? ` · ${esc(cf.notes)}` : ""}</span>
            <span class="tl-amt ${out ? "out" : "in"}">${out ? "−" : "+"}${money(cf.amount, cf.currency)}</span></div>`;
        })
        .join("")}</div>`
    : '<div class="muted small">No cash flows extracted.</div>';

  box.innerHTML = `
    <div class="pr-grid">
      <div class="pr-item"><div class="k">Client ${statusBadge(p.client_name)}</div><div class="v">${ev(p.client_name)}</div></div>
      <div class="pr-item"><div class="k">Funding certainty target</div><div class="v">${Math.round((p.funding_certainty_target || 0) * 100)}%</div></div>
      <div class="pr-item" style="grid-column: span 2"><div class="k">Risk tolerance ${statusBadge(p.risk_tolerance)}</div><div class="v">${ev(p.risk_tolerance)}</div></div>
    </div>
    <div class="pr-section"><h3>Goals</h3>${list(p.investment_goals, "pref")}</div>
    <div class="pr-section"><h3>Cash-flow timeline</h3>${timeline}</div>
    <div class="pr-section"><h3>Hard constraints (must not break)</h3>${list(p.hard_constraints, "hard", (c) => c.description)}</div>
    <div class="pr-section"><h3>Preferences (soft)</h3>${list(p.preferences, "pref", (c) => c.description)}</div>
    <div class="pr-section"><h3>Unknown / ambiguous — not assumed</h3>${list([...(p.uncertainties || []), ...(p.ambiguous_items || [])], "unk")}</div>
    <div class="pr-section"><h3>Tradable universe</h3><div class="muted small">${esc(p.wins_universe_rule || "")}</div></div>`;
}

function setProfile(p, approved) {
  state.profile = p;
  state.approved = approved;
  $("clientProfileJson").value = p ? JSON.stringify(p, null, 2) : "";
  if (p && p.raw_case_study_text && !$("caseStudyText").value.trim()) $("caseStudyText").value = p.raw_case_study_text;
  renderProfileReview(p);
  updateStepBadges();
}

async function extractProfile() {
  const text = $("caseStudyText").value.trim();
  if (!text) return toast("Paste a case study first.", "warn");
  try {
    const p = await api("POST", "/client-profile/extract", { case_study_text: text });
    setProfile(p, false);
    toast("Profile extracted. Review it, edit the JSON if needed, then approve.", "ok");
  } catch (err) {
    toast(err.message, "err");
  }
}

async function approveProfile() {
  try {
    const p = parseJsonOrNull($("clientProfileJson").value, "Client profile");
    if (!p) return toast("Extract or paste a profile first.", "warn");
    const res = await api("POST", "/api/profile", { client_profile: p });
    setProfile(res.client_profile, true);
    state.signals = {};
    refreshSignals();
    toast("Profile approved. All signals and AI runs now use it.", "ok");
    showView("portfolio");
  } catch (err) {
    toast(err.message, "err");
  }
}

async function revokeProfile() {
  await api("DELETE", "/api/profile");
  setProfile(state.profile, false);
  state.signals = {};
  refreshSignals();
  toast("Approval revoked. Signals fall back to generic defaults.", "warn");
}

function reloadReviewFromJson() {
  try {
    const p = parseJsonOrNull($("clientProfileJson").value, "Client profile");
    state.profile = p;
    renderProfileReview(p);
    if (state.approved) toast("Edits are not saved until you approve again.", "warn");
  } catch (err) {
    toast(err.message, "err");
  }
}

// ---------- step 2: holdings + live quotes ----------
async function loadHoldings() {
  state.holdings = await api("GET", "/api/holdings");
  renderHoldings();
  renderPicker();
  updateStepBadges();
}

async function addHolding() {
  const ticker = $("hTicker").value.trim().toUpperCase();
  const shares = Number($("hShares").value || 0);
  const cost = $("hCost").value.trim();
  const stop = $("hStop").value.trim();
  if (!ticker) return toast("Enter a ticker.", "warn");
  try {
    state.holdings = await api("POST", "/api/holdings", {
      ticker,
      shares,
      cost_basis: cost ? Number(cost) : null,
      stop_loss_pct: stop ? Number(stop) / 100 : null,
    });
    ["hTicker", "hShares", "hCost", "hStop"].forEach((id) => ($(id).value = ""));
    delete state.signals[ticker];
    renderHoldings();
    renderPicker();
    updateStepBadges();
    await pollQuotes();
    refreshSignals([ticker]);
    toast(`${ticker} saved. It is now monitored for buy / sell alerts.`, "ok");
    checkAlertsNow(true);
  } catch (err) {
    toast(err.message, "err");
  }
}

async function removeHolding(ticker) {
  state.holdings = await api("DELETE", `/api/holdings/${encodeURIComponent(ticker)}`);
  delete state.signals[ticker];
  if (state.selected === ticker) state.selected = null;
  renderHoldings();
  renderPicker();
  updateStepBadges();
}

function holdingCalc(h) {
  const q = state.quotes[h.ticker];
  const price = q && q.available ? q.price : null;
  const value = price != null ? price * h.shares : null;
  const pl = price != null && h.cost_basis ? (price - h.cost_basis) * h.shares : null;
  const plPct = price != null && h.cost_basis ? (price / h.cost_basis - 1) * 100 : null;
  const day = q && q.available && q.change != null ? q.change * h.shares : null;
  return { q, price, value, pl, plPct, day };
}

function renderHoldings(flash = {}) {
  const tbody = document.querySelector("#holdingsTable tbody");
  if (!state.holdings.length) {
    tbody.innerHTML = '<tr><td colspan="9" class="empty">No holdings yet. Add one above.</td></tr>';
  } else {
    tbody.innerHTML = state.holdings
      .map((h) => {
        const c = holdingCalc(h);
        const sig = state.signals[h.ticker];
        const sigHtml = sig ? `<span class="pill ${pillClass(sig.action)}">${esc(sig.action)}</span>` : '<span class="muted small">…</span>';
        const priceCell = c.q && !c.q.available ? '<span class="muted small">unavailable</span>' : money(c.price, c.q?.currency);
        return `<tr class="click" data-t="${esc(h.ticker)}">
          <td class="sym">${esc(h.ticker)}<small>${h.shares > 0 ? "Holding" : "Watchlist"}</small></td>
          <td class="num ${flash[h.ticker] || ""}">${priceCell}</td>
          <td class="num ${dirClass(c.q?.change_pct)}">${pct(c.q?.change_pct)}</td>
          <td class="num">${num(h.shares, h.shares % 1 ? 3 : 0)}</td>
          <td class="num">${money(c.value)}</td>
          <td class="num ${dirClass(c.pl)}">${c.pl == null ? "—" : `${signed(c.pl)}<br><small>${pct(c.plPct)}</small>`}</td>
          <td>${sigHtml}</td>
          <td class="trade-cell">${tradeLabel(sig)}</td>
          <td><button class="btn ghost small danger" data-remove="${esc(h.ticker)}">Remove</button></td>
        </tr>`;
      })
      .join("");
  }
  let total = 0, day = 0, pl = 0, hasPl = false;
  state.holdings.forEach((h) => {
    const c = holdingCalc(h);
    total += c.value || 0;
    day += c.day || 0;
    if (c.pl != null) { pl += c.pl; hasPl = true; }
  });
  $("totValue").textContent = money(total);
  $("totDay").innerHTML = `<span class="${dirClass(day)}">${signed(day)}</span>`;
  $("totPL").innerHTML = hasPl ? `<span class="${dirClass(pl)}">${signed(pl)}</span>` : "—";
  $("totCash").textContent = state.settings.cash_available == null ? "Not set" : money(state.settings.cash_available);
}

function tradeLabel(sig) {
  const t = sig?.trade_suggestion;
  if (!t || !t.side) return '<span class="muted">—</span>';
  if (!t.shares) return `<span class="muted small" title="${esc(t.rationale)}">${esc(t.side)} · size n/a</span>`;
  const cls = t.side === "BUY" ? "up" : "down";
  return `<span class="${cls}" title="${esc(t.rationale)}">${esc(t.side)} ${num(t.shares, t.shares % 1 ? 3 : 0)}</span>`;
}

async function applySuggestedBuy() {
  const tkr = state.selected;
  const sig = state.signals[tkr];
  const t = sig?.trade_suggestion || {};
  if (!tkr || t.side !== "BUY" || !t.shares || t.shares <= 0) {
    return toast("No buy suggestion is available for this stock right now.", "warn");
  }
  const existing = state.holdings.find((h) => h.ticker === tkr);
  const q = state.quotes[tkr];
  const px = q?.available ? Number(q.price) : Number(sig?.indicators?.price);
  if (!px || px <= 0) {
    return toast("Price unavailable, so buy autofill cannot run.", "warn");
  }
  const oldShares = Number(existing?.shares || 0);
  const oldCost = existing?.cost_basis != null ? Number(existing.cost_basis) : null;
  const newShares = oldShares + Number(t.shares);
  const newCost = (oldCost != null && oldShares > 0)
    ? ((oldShares * oldCost + Number(t.shares) * px) / newShares)
    : px;
  try {
    state.holdings = await api("POST", "/api/holdings", {
      ticker: tkr,
      shares: newShares,
      cost_basis: Number(newCost.toFixed(4)),
      stop_loss_pct: existing?.stop_loss_pct ?? null,
      notes: existing?.notes || "",
    });
    renderHoldings();
    renderPicker();
    updateStepBadges();
    await pollQuotes();
    await refreshSignals([tkr]);
    await checkAlertsNow(true);
    toast(`Added ${num(t.shares, t.shares % 1 ? 3 : 0)} ${tkr} share(s) to holdings from the buy suggestion.`, "ok");
  } catch (err) {
    toast(err.message, "err");
  }
}

document.querySelector("#holdingsTable tbody").addEventListener("click", (e) => {
  const rm = e.target.closest("[data-remove]");
  if (rm) {
    e.stopPropagation();
    return removeHolding(rm.dataset.remove);
  }
  const row = e.target.closest("tr[data-t]");
  if (row) selectStock(row.dataset.t);
});

function trackedTickers() {
  const set = new Set(state.holdings.map((h) => h.ticker));
  if (state.selected) set.add(state.selected);
  return [...set];
}

async function pollQuotes() {
  const tickers = trackedTickers();
  if (!tickers.length) return;
  try {
    const quotes = await api("GET", `/api/quotes?tickers=${encodeURIComponent(tickers.join(","))}`);
    const flash = {};
    quotes.forEach((q) => {
      const prev = state.quotes[q.ticker];
      if (prev && prev.price != null && q.price != null && prev.price !== q.price) flash[q.ticker] = q.price > prev.price ? "flash-up" : "flash-down";
      state.quotes[q.ticker] = q;
    });
    renderHoldings(flash);
    renderStockHeader();
    appendLivePoint();
    const ok = quotes.filter((q) => q.available).length;
    $("quotesUpdated").textContent = `Updated ${new Date().toLocaleTimeString()} · ${ok}/${quotes.length} live`;
    setLive(ok > 0);
  } catch (err) {
    setLive(false, "Quotes offline");
  }
}

function setLive(on, text) {
  const secs = Number($("refreshInterval").value);
  $("liveChip").classList.toggle("on", on && secs > 0);
  $("liveText").textContent = text || (secs > 0 ? (on ? `Live · ${secs}s` : "Waiting for data") : "Live off");
}

function startPolling() {
  clearInterval(state.pollTimer);
  clearInterval(state.signalTimer);
  const secs = Number($("refreshInterval").value);
  if (secs > 0) state.pollTimer = setInterval(pollQuotes, secs * 1000);
  state.signalTimer = setInterval(() => refreshSignals(), 5 * 60 * 1000);
  setLive(Object.values(state.quotes).some((q) => q.available));
}

async function refreshSignals(only) {
  const tickers = only || trackedTickers();
  for (const t of tickers) {
    try {
      state.signals[t] = await api("GET", `/api/signal/${encodeURIComponent(t)}`);
    } catch (err) {
      state.signals[t] = { action: "UNAVAILABLE", reasons: [err.message] };
    }
    renderHoldings();
    if (t === state.selected) renderSignal();
  }
}

// ---------- step 3: stock detail ----------
function renderPicker() {
  const sel = $("stockPicker");
  const opts = trackedTickers();
  sel.innerHTML = opts.length
    ? opts.map((t) => `<option value="${esc(t)}" ${t === state.selected ? "selected" : ""}>${esc(t)}</option>`).join("")
    : '<option value="">No holdings</option>';
}

function ensureStockSelected() {
  if (!state.selected && state.holdings.length) selectStock(state.holdings[0].ticker);
}

async function selectStock(ticker) {
  state.selected = ticker.toUpperCase();
  renderPicker();
  updateStepBadges();
  showView("stock");
  renderStockHeader();
  $("signalBody").innerHTML = '<div class="empty-state"><span class="spinner"></span>Computing signal…</div>';
  $("signalWhy").innerHTML = "";
  await Promise.all([loadHistory(), pollQuotes()]);
  await refreshSignals([state.selected]);
}

async function loadHistory() {
  if (!state.selected) return;
  const t = state.selected;
  try {
    const h = await api("GET", `/api/history/${encodeURIComponent(t)}?range=${state.range}`);
    if (t !== state.selected) return;
    state.history = h;
  } catch (err) {
    state.history = { available: false, error: err.message, points: [] };
  }
  drawChart();
}

function appendLivePoint() {
  const h = state.history;
  const q = state.quotes[state.selected];
  if (!h || !h.available || !q || !q.available || !h.points.length) return;
  if (["1d", "5d"].includes(state.range)) {
    h.points[h.points.length - 1].c = q.price;
  } else {
    const last = h.points[h.points.length - 1];
    if (new Date(last.t).toDateString() === new Date().toDateString()) last.c = q.price;
    else h.points.push({ t: new Date().toISOString(), c: q.price });
  }
  drawChart();
}

function renderStockHeader() {
  const t = state.selected;
  if (!t) return;
  const q = state.quotes[t];
  if (!q) {
    $("sPrice").textContent = "—";
    $("sChange").textContent = "";
    $("sMeta").textContent = `${t} · loading live quote…`;
    return;
  }
  if (!q.available) {
    $("sPrice").textContent = "Unavailable";
    $("sChange").textContent = "";
    $("sMeta").textContent = `${t} · ${q.error || "no quote"} (no price is shown rather than a guess)`;
    return;
  }
  $("sPrice").textContent = money(q.price, q.currency);
  $("sChange").innerHTML = `<span class="${dirClass(q.change)}">${signed(q.change, q.currency)} (${pct(q.change_pct)})</span>`;
  $("sMeta").textContent = `${t} · Day ${money(q.day_low, q.currency)}–${money(q.day_high, q.currency)} · 52w ${money(q.year_low, q.currency)}–${money(q.year_high, q.currency)} · ${q.source} · ${new Date(q.as_of).toLocaleTimeString()}`;
}

function drawChart() {
  const svg = $("chart");
  const W = svg.clientWidth || 1000;
  const H = svg.clientHeight || 340;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  const h = state.history;
  if (!h || !h.available || h.points.length < 2) {
    svg.innerHTML = `<text x="${W / 2}" y="${H / 2}" fill="#8a94a8" text-anchor="middle" font-size="13">${esc(h?.error ? `Chart unavailable: ${h.error}` : "No chart data")}</text>`;
    return;
  }
  const pts = h.points;
  const holding = state.holdings.find((x) => x.ticker === state.selected);
  const sig = state.signals[state.selected];
  const refs = [];
  if (holding?.cost_basis) refs.push({ v: holding.cost_basis, cls: "#a78bfa", dash: "6 4", label: "Cost" });
  if (sig?.levels?.stop_loss_price) refs.push({ v: sig.levels.stop_loss_price, cls: "#ef4444", dash: "6 4", label: "Stop" });
  if (sig?.levels?.trend_break_price) refs.push({ v: sig.levels.trend_break_price, cls: "#f59e0b", dash: "2 4", label: "200d" });

  const vals = pts.map((p) => p.c);
  let lo = Math.min(...vals), hi = Math.max(...vals);
  refs.forEach((r) => {
    if (r.v > lo * 0.7 && r.v < hi * 1.3) { lo = Math.min(lo, r.v); hi = Math.max(hi, r.v); }
  });
  const pad = (hi - lo) * 0.08 || 1;
  lo -= pad; hi += pad;
  const L = 8, R = 64, T = 12, B = 26;
  const x = (i) => L + (i / (pts.length - 1)) * (W - L - R);
  const y = (v) => T + (1 - (v - lo) / (hi - lo)) * (H - T - B);
  const up = vals[vals.length - 1] >= vals[0];
  const color = up ? "#22c55e" : "#ef4444";

  const line = pts.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.c).toFixed(1)}`).join("");
  const area = `${line}L${x(pts.length - 1).toFixed(1)},${H - B}L${L},${H - B}Z`;

  let grid = "";
  for (let i = 0; i <= 4; i++) {
    const v = lo + ((hi - lo) * i) / 4;
    grid += `<line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}" stroke="#1f2635"/>
      <text x="${W - R + 8}" y="${y(v) + 4}" fill="#8a94a8" font-size="11" font-family="Consolas,monospace">${num(v)}</text>`;
  }
  const intraday = ["1d", "5d"].includes(state.range);
  for (let i = 0; i <= 4; i++) {
    const idx = Math.round(((pts.length - 1) * i) / 4);
    const d = new Date(pts[idx].t);
    const label = intraday ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : d.toLocaleDateString([], { month: "short", day: "numeric", year: state.range === "5y" ? "2-digit" : undefined });
    grid += `<text x="${x(idx)}" y="${H - 6}" fill="#8a94a8" font-size="11" text-anchor="${i === 0 ? "start" : i === 4 ? "end" : "middle"}">${esc(label)}</text>`;
  }
  const refLines = refs
    .filter((r) => r.v >= lo && r.v <= hi)
    .map((r) => `<line x1="${L}" x2="${W - R}" y1="${y(r.v)}" y2="${y(r.v)}" stroke="${r.cls}" stroke-dasharray="${r.dash}" stroke-width="1.5"/>
      <rect x="${W - R + 2}" y="${y(r.v) - 9}" width="${R - 4}" height="18" rx="4" fill="${r.cls}"/>
      <text x="${W - R + 6}" y="${y(r.v) + 4}" fill="#0a0d14" font-size="10.5" font-weight="700">${r.label} ${num(r.v, 0)}</text>`)
    .join("");

  const lastX = x(pts.length - 1), lastY = y(vals[vals.length - 1]);
  svg.innerHTML = `
    <defs><linearGradient id="g" x1="0" x2="0" y1="0" y2="1">
      <stop offset="0%" stop-color="${color}" stop-opacity=".28"/><stop offset="100%" stop-color="${color}" stop-opacity="0"/>
    </linearGradient></defs>
    ${grid}
    <path d="${area}" fill="url(#g)"/>
    <path d="${line}" fill="none" stroke="${color}" stroke-width="2" stroke-linejoin="round"/>
    ${refLines}
    <circle cx="${lastX}" cy="${lastY}" r="4" fill="${color}"/>
    <circle cx="${lastX}" cy="${lastY}" r="9" fill="${color}" opacity=".25"><animate attributeName="r" values="4;12;4" dur="2s" repeatCount="indefinite"/></circle>
    <g id="hover" style="display:none"><line id="hvLine" y1="${T}" y2="${H - B}" stroke="#8a94a8" stroke-dasharray="3 3"/><circle id="hvDot" r="4" fill="#fff"/></g>
    <rect x="${L}" y="${T}" width="${W - L - R}" height="${H - T - B}" fill="transparent" id="hvZone"/>`;

  const tip = $("chartTip");
  const zone = $("hvZone");
  zone.addEventListener("mousemove", (e) => {
    const rect = svg.getBoundingClientRect();
    const px = e.clientX - rect.left;
    const i = Math.max(0, Math.min(pts.length - 1, Math.round(((px - L) / (W - L - R)) * (pts.length - 1))));
    $("hover").style.display = "";
    $("hvLine").setAttribute("x1", x(i));
    $("hvLine").setAttribute("x2", x(i));
    $("hvDot").setAttribute("cx", x(i));
    $("hvDot").setAttribute("cy", y(pts[i].c));
    const d = new Date(pts[i].t);
    tip.textContent = `${intraday ? d.toLocaleString() : d.toLocaleDateString()} · ${money(pts[i].c)}`;
    tip.classList.remove("hidden");
    tip.style.left = `${Math.min(px + 12, rect.width - tip.offsetWidth - 8)}px`;
  });
  zone.addEventListener("mouseleave", () => {
    $("hover").style.display = "none";
    tip.classList.add("hidden");
  });
}

function renderSignal() {
  const s = state.signals[state.selected];
  if (!s) return;
  const q = state.quotes[state.selected];
  const cur = q?.currency || "USD";
  const ind = s.indicators || {};
  const lv = s.levels || {};
  const pos = s.position;
  const actionText = {
    BUY: "Conditions favour opening a position.",
    ADD: "Conditions favour adding to your position.",
    HOLD: "Keep the position; no strong reason to act.",
    WAIT: "No clear entry yet; wait for better conditions.",
    TRIM: "Consider reducing the position size.",
    SELL: "Consider exiting the position.",
    AVOID: "Conditions do not favour buying now.",
  }[s.action] || "No signal could be produced from the available data.";

  const t = s.trade_suggestion || {};
  const tCls = t.side === "BUY" ? "buy" : t.side === "SELL" ? "sell" : "";
  const tradeBox = t.side
    ? `<div class="trade-box ${tCls}">
        <div>
          <div class="big ${tCls}">${t.shares ? `${esc(t.side)} ${num(t.shares, t.shares % 1 ? 3 : 0)} shares` : `${esc(t.side)} — size not available`}</div>
          <div class="sub">${esc(t.rationale)}${t.est_value ? ` · about ${money(t.est_value, cur)}` : ""}</div>
          ${(t.assumptions || []).map((a) => `<div class="sub">Assumption: ${esc(a)}</div>`).join("")}
        </div>
        <div class="row tight">
          ${t.side === "BUY" && t.shares ? `<button id="buySuggestedBtn" class="btn primary small">Buy ${num(t.shares, t.shares % 1 ? 3 : 0)} shares</button>` : ""}
          <span class="tag">Suggestion only · you place the trade</span>
        </div>
      </div>`
    : `<div class="trade-box"><div><div class="big">No trade now</div><div class="sub">${esc(t.rationale || "Keep monitoring; you will get an alert if this changes.")}</div></div></div>`;

  $("signalBody").innerHTML = `
    ${tradeBox}
    <div class="signal-hero">
      <div class="signal-action ${pillClass(s.action)}">${esc(s.action)}</div>
      <div class="signal-meta">
        <div><b>${esc(actionText)}</b></div>
        <div>Confidence: <b>${esc(s.confidence || "—")}</b>${s.rule_agreement != null ? ` · rule agreement ${Math.round(s.rule_agreement * 100)}%` : ""}${s.score != null ? ` · score ${s.score > 0 ? "+" : ""}${s.score}` : ""}</div>
        <div>${s.generated_at ? `Computed ${new Date(s.generated_at).toLocaleTimeString()}` : ""}</div>
      </div>
    </div>
    ${pos ? `<div class="kv-grid" style="margin-bottom:10px">
      <div class="kv"><div class="k">Market value</div><div class="v">${money(pos.market_value, cur)}</div></div>
      <div class="kv"><div class="k">Unrealized P&amp;L</div><div class="v ${dirClass(pos.unrealized_pl)}">${pos.unrealized_pl == null ? "—" : signed(pos.unrealized_pl, cur)}</div></div>
      <div class="kv"><div class="k">Return</div><div class="v ${dirClass(pos.unrealized_pl_pct)}">${pct(pos.unrealized_pl_pct)}</div></div>
    </div>` : ""}
    <div class="kv-grid">
      <div class="kv"><div class="k">Sell / stop-loss below</div><div class="v down">${money(lv.stop_loss_price, cur)}</div></div>
      <div class="kv"><div class="k">Trend breaks below (200d)</div><div class="v">${money(lv.trend_break_price, cur)}</div></div>
      <div class="kv"><div class="k">Buy / add zone</div><div class="v up">${lv.add_zone_low ? `${num(lv.add_zone_low)}–${num(lv.add_zone_high)}` : "—"}</div></div>
      <div class="kv"><div class="k">RSI (14)</div><div class="v">${num(ind.rsi14, 0)}</div></div>
      <div class="kv"><div class="k">50-day avg</div><div class="v">${num(ind.sma50)}</div></div>
      <div class="kv"><div class="k">From 52w high</div><div class="v ${dirClass(ind.drawdown_from_high_pct)}">${pct(ind.drawdown_from_high_pct)}</div></div>
    </div>
    <p class="hint">${esc(s.disclaimer || "")}</p>`;

  const li = (arr) => (arr && arr.length ? `<ul class="why-list">${arr.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>` : '<div class="muted small" style="margin-bottom:14px">None.</div>');
  $("signalWhy").innerHTML = `
    <div class="why-h">Market evidence</div>${li(s.reasons)}
    <div class="why-h">Client fit (from Step 1)</div>${li(s.client_fit)}
    <div class="why-h">Assumptions to confirm</div>${li(s.assumptions)}`;
  const tag = $("signalProfileTag");
  tag.textContent = s.profile_applied ? "Profile applied" : "No approved profile";
  tag.className = `tag ${s.profile_applied ? "ok" : "warn"}`;
  const buyBtn = $("buySuggestedBtn");
  if (buyBtn) buyBtn.addEventListener("click", applySuggestedBuy);
  renderQuickPriceAlerts(s);
  drawChart();
}

function renderQuickPriceAlerts(s) {
  const lv = s.levels || {};
  const price = s.indicators?.price;
  const quick = [];
  if (lv.stop_loss_price) quick.push({ direction: "below", price: lv.stop_loss_price, note: "Stop-loss level: consider selling" });
  if (lv.trend_break_price) quick.push({ direction: "below", price: lv.trend_break_price, note: "Broke the 200-day trend" });
  if (lv.add_zone_high) quick.push({ direction: "below", price: lv.add_zone_high, note: "Entered the buy / add zone" });
  if (price) quick.push({ direction: "above", price: +(price * 1.1).toFixed(2), note: "Up 10%: review taking profit" });
  $("paQuick").innerHTML = quick.length
    ? `<span class="muted small">Quick add:</span>${quick.map((q, i) => `<button class="btn ghost small" data-quick="${i}">${q.direction === "below" ? "Below" : "Above"} ${num(q.price)} · ${esc(q.note.split(":")[0])}</button>`).join("")}`
    : "";
  $("paQuick").querySelectorAll("[data-quick]").forEach((b) =>
    b.addEventListener("click", () => addPriceAlert(quick[Number(b.dataset.quick)]))
  );
}

$("stockPicker").addEventListener("change", (e) => e.target.value && selectStock(e.target.value));
$("stockLookupBtn").addEventListener("click", () => {
  const t = $("stockLookup").value.trim();
  if (t) selectStock(t);
});
$("stockLookup").addEventListener("keydown", (e) => e.key === "Enter" && $("stockLookupBtn").click());
document.querySelectorAll("#rangeTabs button").forEach((b) =>
  b.addEventListener("click", () => {
    document.querySelectorAll("#rangeTabs button").forEach((x) => x.classList.toggle("active", x === b));
    state.range = b.dataset.range;
    loadHistory();
  })
);
window.addEventListener("resize", () => drawChart());
$("deepFromStockBtn").addEventListener("click", () => {
  if (!state.selected) return toast("Pick a stock first.", "warn");
  $("ticker").value = state.selected;
  showView("analysis");
  $("runAnalysisBtn").click();
});

// ---------- deep AI analysis ----------
function selectedAnalysts() {
  return Array.from(document.querySelectorAll(".checks input:checked")).map((e) => e.value);
}

function sharedRunPayload() {
  const portfolio = $("useHoldings").checked && state.holdings.length
    ? { currency: "USD", positions: state.holdings.filter((h) => h.shares > 0).map((h) => ({ ticker: h.ticker, quantity: h.shares, average_price: h.cost_basis })) }
    : null;
  return {
    trade_date: $("tradeDate").value,
    asset_type: $("assetType").value,
    selected_analysts: selectedAnalysts(),
    config_overrides: parseJsonOrNull($("configOverridesJson").value, "Config overrides") || {},
    portfolio,
    client_profile: state.approved ? state.profile : null,
    case_study_text: null,
  };
}

function renderAnalysis(data) {
  const r = data.structured_recommendation_report || {};
  const sec = (title, body, cls = "") => `<div class="report-sec ${cls}"><h3>${title}</h3><div class="body">${esc(body || "Not provided.")}</div></div>`;
  const violations = (data.client_constraint_violations || [])
    .map((v) => `<div class="violation ${esc(v.severity)}"><b>${esc(v.severity?.toUpperCase())}: ${esc(v.code)}</b><br>${esc(v.message)}</div>`)
    .join("");
  $("analysisResult").innerHTML = `
    <div class="card">
      <div class="report-hero">
        <div>
          <div class="muted small">${esc(r.security || data.ticker || "")} · ${esc(r.timestamp_utc || "")}</div>
          <div class="signal-hero" style="margin:10px 0 0">
            <div class="signal-action ${pillClass(data.signal)}">${esc(data.signal)}</div>
            <div class="signal-meta"><div>Confidence: <b>${esc(r.confidence || "—")}</b></div><div>${esc(r.uncertainty || "")}</div></div>
          </div>
        </div>
        <span class="tag">AI-generated analysis · verify sources</span>
      </div>
    </div>
    ${violations ? `<div class="card"><div class="card-head"><h2>Client constraint checks</h2></div>${violations}</div>` : ""}
    <div class="report-sections">
      ${sec("Client fit", r.client_fit)}${sec("Portfolio impact", r.portfolio_impact)}
      ${sec("Bull case", r.bull_case, "bull")}${sec("Bear case", r.bear_case, "bear")}
      ${sec("Fundamentals", r.fundamental_analysis)}${sec("Technicals", r.technical_analysis)}
      ${sec("News & sentiment", r.news_sentiment_analysis)}${sec("Key risks", r.key_risks)}
      ${sec("What would invalidate this", r.thesis_invalidation)}${sec("Evidence & sources", (r.evidence_sources || []).join("\n"))}
    </div>
    <div class="card" style="margin-top:18px"><details><summary class="muted small">Raw API response</summary><pre class="out">${esc(JSON.stringify(data, null, 2))}</pre></details></div>`;
}

async function runAnalysis() {
  if (!state.approved) toast("No approved profile; analysis will not be client-aware.", "warn");
  await busy($("runAnalysisBtn"), async () => {
    $("analysisResult").innerHTML = '<div class="card"><div class="empty-state"><span class="spinner"></span>Agents are researching… this can take several minutes.</div></div>';
    try {
      const data = await api("POST", "/analyze", { ...sharedRunPayload(), ticker: $("ticker").value.trim().toUpperCase() });
      renderAnalysis(data);
      toast(`Analysis complete: ${data.signal}`, "ok");
    } catch (err) {
      $("analysisResult").innerHTML = `<div class="card"><div class="violation">${esc(err.message)}</div><p class="hint">AI analysis needs an LLM API key (e.g. OPENAI_API_KEY) set before starting the server. Live prices and rule-based signals work without one.</p></div>`;
      toast(err.message, "err");
    }
  });
}

// ---------- scanner ----------
const signalRank = { BUY: 6, ADD: 6, OVERWEIGHT: 5, HOLD: 4, WAIT: 3, UNDERWEIGHT: 2, TRIM: 2, AVOID: 1, SELL: 1 };
const confRank = { high: 3, medium: 2, low: 1 };

function isProfitSeekingSignal(s) {
  return ["BUY", "ADD", "OVERWEIGHT"].includes(String(s || "").toUpperCase());
}

function isConfidenceGood(conf) {
  return ["high", "medium"].includes(String(conf || "").toLowerCase());
}

function looksClientAlignedFromNotes(notes) {
  const t = String(notes || "").toLowerCase();
  if (!t) return true;
  return !(/not provided|uncertain suitability|outside funding|constraint violation|violat/i.test(t));
}

function explainRejectedQuick(s) {
  if (!isProfitSeekingSignal(s.action)) return "Not a profit-seeking signal (BUY/ADD only).";
  if (!isConfidenceGood(s.confidence)) return "Signal confidence is low.";
  if (!s.profile_applied) return "No approved client profile applied.";
  if (!looksClientAlignedFromNotes((s.client_fit || []).join(" "))) return "Signal does not clearly align with client-fit notes.";
  return "Filtered by suitability gate.";
}

function explainRejectedDeep(row) {
  if (!isProfitSeekingSignal(row.signal)) return "Not a profit-seeking recommendation (BUY/OVERWEIGHT/ADD only).";
  if (!isConfidenceGood(row.confidence)) return "Recommendation confidence is low.";
  if (!looksClientAlignedFromNotes(row.notes)) return "Client-fit notes show suitability concerns.";
  if (row.blockingViolations > 0) return "Blocking client-constraint violation detected.";
  return "Filtered by suitability gate.";
}

function renderScan(rows, rejected = []) {
  const tbody = document.querySelector("#scanTable tbody");
  if (!rows.length && !rejected.length) {
    tbody.innerHTML = '<tr><td colspan="7" class="empty">No scan results yet.</td></tr>';
    return;
  }
  if (!rows.length && rejected.length) {
    tbody.innerHTML = `<tr><td colspan="7" class="empty">No candidates passed the profit + client-alignment gate.\n${esc(rejected[0].reason || "")}</td></tr>`;
    return;
  }
  tbody.innerHTML = rows
    .map((r, i) => `<tr class="click" data-t="${esc(r.ticker)}">
      <td>${i + 1}</td><td class="sym">${esc(r.ticker)}</td><td class="num">${money(r.price)}</td>
      <td><span class="pill ${pillClass(r.signal)}">${esc(r.signal)}</span></td>
      <td>${esc(r.confidence || "—")}</td><td class="small">${esc(r.notes || "")}</td>
      <td><button class="btn ghost small" data-open="${esc(r.ticker)}">Open</button></td></tr>`)
    .join("");
}
document.querySelector("#scanTable tbody").addEventListener("click", (e) => {
  const row = e.target.closest("tr[data-t]");
  if (row) selectStock(row.dataset.t);
});

function scanTickers() {
  return ($("scanTickers").value || "").split(",").map((x) => x.trim().toUpperCase()).filter(Boolean);
}

function sortScan(rows) {
  return rows.sort((a, b) => (signalRank[b.signal] || 0) - (signalRank[a.signal] || 0)
    || (b.score || 0) - (a.score || 0)
    || (confRank[(b.confidence || "").toLowerCase()] || 0) - (confRank[(a.confidence || "").toLowerCase()] || 0));
}

async function runQuickScan() {
  const tickers = scanTickers();
  if (!state.approved) return toast("Approve a client profile first; scan gating depends on client beliefs.", "warn");
  if (!tickers.length) return toast("Enter at least one ticker.", "warn");
  await busy($("runQuickScanBtn"), async () => {
    const rows = [];
    const rejected = [];
    const queue = [...tickers];
    const worker = async () => {
      while (queue.length) {
        const t = queue.shift();
        try {
          const s = await api("GET", `/api/signal/${encodeURIComponent(t)}`);
          const row = { ticker: t, signal: s.action, score: s.score, confidence: s.confidence, price: s.quote?.price ?? s.indicators?.price, notes: (s.reasons || [])[0] || "" };
          if (isProfitSeekingSignal(s.action) && isConfidenceGood(s.confidence) && s.profile_applied && looksClientAlignedFromNotes((s.client_fit || []).join(" "))) rows.push(row);
          else rejected.push({ ticker: t, reason: explainRejectedQuick(s) });
        } catch (err) {
          rejected.push({ ticker: t, reason: err.message });
        }
        renderScan(sortScan([...rows]), rejected);
      }
    };
    await Promise.all([worker(), worker(), worker()]);
    const ordered = sortScan(rows);
    if (!ordered.length) {
      toast("No candidate passed the profit + client-alignment gate. Try different tickers or relax constraints.", "warn");
      return;
    }
    const top = ordered[0];
    toast(`Quick scan done. Top aligned candidate: ${top.ticker} (${top.signal}).`, "ok");
  });
}

async function runDeepScan() {
  const tickers = scanTickers();
  if (!state.approved) return toast("Approve a client profile first; deep scan gating depends on client beliefs.", "warn");
  if (!tickers.length) return toast("Enter at least one ticker.", "warn");
  await busy($("runScanBtn"), async () => {
    const rows = [];
    const rejected = [];
    for (let i = 0; i < tickers.length; i++) {
      const t = tickers[i];
      toast(`Deep scanning ${t} (${i + 1}/${tickers.length})…`);
      try {
        const data = await api("POST", "/analyze", { ...sharedRunPayload(), ticker: t });
        const r = data.structured_recommendation_report || {};
        const blockingViolations = (data.client_constraint_violations || []).filter((v) => String(v.severity || "").toLowerCase() === "blocking").length;
        const row = { ticker: t, signal: String(data.signal || "REVIEW").toUpperCase(), confidence: r.confidence, notes: String(r.client_fit || "").slice(0, 140), blockingViolations };
        if (isProfitSeekingSignal(row.signal) && isConfidenceGood(row.confidence) && looksClientAlignedFromNotes(row.notes) && blockingViolations === 0) rows.push(row);
        else rejected.push({ ticker: t, reason: explainRejectedDeep(row) });
      } catch (err) {
        rejected.push({ ticker: t, reason: `Error: ${err.message}` });
      }
      renderScan(sortScan([...rows]), rejected);
    }
    if (!rows.length) {
      toast("Deep scan found no candidates that passed the profit + client-alignment gate.", "warn");
      return;
    }
    toast("Deep AI scan complete with client-aligned candidates only.", "ok");
  });
}

// ---------- backtest ----------
async function runBacktest() {
  await busy($("runBacktestBtn"), async () => {
    $("backtestOut").textContent = "Running backtest… this may take several minutes.";
    try {
      const data = await api("POST", "/backtest", {
        ...sharedRunPayload(),
        tickers: $("backtestTickers").value.split(",").map((x) => x.trim()).filter(Boolean),
        start_date: $("backtestStart").value,
        end_date: $("backtestEnd").value,
        every_n_days: Number($("backtestEvery").value || 7),
      });
      $("backtestOut").textContent = `${data.summary}\n\n${JSON.stringify(data, null, 2)}`;
      toast(`Backtest complete: ${data.cells_run} cells run`, "ok");
    } catch (err) {
      $("backtestOut").textContent = err.message;
      toast(err.message, "err");
    }
  });
}

// ---------- alerts ----------
function timeAgo(iso) {
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return new Date(iso).toLocaleString();
}

function alertClass(e) {
  return e.kind === "price" ? "price" : pillClass(e.action);
}

function renderAlerts() {
  const unread = state.alerts.filter((e) => !e.is_read).length;
  [$("bellCount"), $("navAlertBadge")].forEach((el) => {
    el.textContent = unread > 99 ? "99+" : unread;
    el.classList.toggle("hidden", unread === 0);
  });
  const box = $("alertList");
  if (!state.alerts.length) {
    box.innerHTML = '<div class="empty-state">No alerts yet. Add holdings and alerts will appear here automatically.</div>';
    return;
  }
  box.innerHTML = state.alerts
    .map((e) => {
      const trade = e.payload?.trade_suggestion;
      const side = trade?.side;
      const ticket = side && e.suggested_shares
        ? `<span class="order-ticket ${side === "BUY" ? "buy" : "sell"}">${esc(side)} ${num(e.suggested_shares, e.suggested_shares % 1 ? 3 : 0)} sh</span>`
        : "";
      const icon = e.kind === "price" ? "$" : esc((e.action || "?").slice(0, 1));
      return `<div class="alert-item ${alertClass(e)} ${e.is_read ? "" : "unread"}" data-id="${e.id}">
        <div class="alert-icon">${icon}</div>
        <div>
          <div class="alert-title">${esc(e.title)}</div>
          <div class="alert-msg">${esc(e.message)}</div>
          <div class="alert-meta">${timeAgo(e.created_at)}${e.payload?.confidence ? ` · confidence ${esc(e.payload.confidence)}` : ""}${e.is_read ? "" : " · new"}</div>
        </div>
        <div class="alert-side">
          ${ticket}
          <button class="btn ghost small" data-open="${esc(e.ticker)}">Open ${esc(e.ticker)}</button>
          ${e.is_read ? "" : `<button class="btn ghost small" data-read="${e.id}">Mark read</button>`}
        </div>
      </div>`;
    })
    .join("");
}

$("alertList").addEventListener("click", async (e) => {
  const open = e.target.closest("[data-open]");
  if (open) return selectStock(open.dataset.open);
  const read = e.target.closest("[data-read]");
  if (read) {
    await api("POST", "/api/alerts/read", { ids: [Number(read.dataset.read)] });
    loadAlerts();
  }
});

function beep() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    [880, 1175].forEach((f, i) => {
      const o = ctx.createOscillator();
      const g = ctx.createGain();
      o.frequency.value = f;
      g.gain.setValueAtTime(0.0001, ctx.currentTime + i * 0.18);
      g.gain.exponentialRampToValueAtTime(0.15, ctx.currentTime + i * 0.18 + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + i * 0.18 + 0.16);
      o.connect(g).connect(ctx.destination);
      o.start(ctx.currentTime + i * 0.18);
      o.stop(ctx.currentTime + i * 0.18 + 0.18);
    });
  } catch { /* audio unavailable */ }
}

function notifyNew(events) {
  if (!events.length) return;
  const bell = $("bellBtn");
  bell.classList.remove("ring");
  void bell.offsetWidth;
  bell.classList.add("ring");
  if (state.settings.sound) beep();
  const top = events[0];
  toast(events.length > 1 ? `${events.length} new alerts. Latest: ${top.title}` : `${top.title}. ${top.message}`, top.action === "SELL" || top.action === "TRIM" ? "warn" : "ok");
  if (state.settings.browser_notifications && "Notification" in window && Notification.permission === "granted") {
    events.slice(0, 3).forEach((e) => {
      const n = new Notification(e.title, { body: e.message, tag: `ta-${e.id}` });
      n.onclick = () => { window.focus(); selectStock(e.ticker); };
    });
  }
}

async function loadAlerts() {
  try {
    const data = await api("GET", "/api/alerts?limit=100");
    const fresh = state.seenAlertIds ? data.events.filter((e) => !state.seenAlertIds.has(e.id) && !e.is_read) : [];
    state.seenAlertIds = new Set(data.events.map((e) => e.id));
    state.alerts = data.events;
    renderAlerts();
    const m = data.monitor || {};
    $("monitorStatus").textContent = !m.running
      ? "Automatic monitor is off"
      : m.last_error
        ? `Monitor error: ${m.last_error}`
        : `Checking every ${Math.round(m.interval_seconds)}s · last check ${m.last_run ? new Date(m.last_run * 1000).toLocaleTimeString() : "pending"}`;
    if (fresh.length) {
      notifyNew(fresh);
      fresh.forEach((e) => delete state.signals[e.ticker]);
      refreshSignals(fresh.map((e) => e.ticker).filter((t) => trackedTickers().includes(t)));
    }
  } catch { /* shown via other calls */ }
}

async function checkAlertsNow(quiet = false) {
  try {
    const res = await api("POST", "/api/alerts/check");
    await loadAlerts();
    loadPriceAlerts();
    if (!quiet) toast(res.created.length ? `${res.created.length} new alert(s).` : "Checked: no new buy / sell changes.", "ok");
  } catch (err) {
    if (!quiet) toast(err.message, "err");
  }
}

// ---------- price alerts ----------
async function loadPriceAlerts() {
  try {
    state.priceAlerts = await api("GET", "/api/price-alerts");
  } catch { return; }
  const tbody = document.querySelector("#priceAlertTable tbody");
  tbody.innerHTML = state.priceAlerts.length
    ? state.priceAlerts.map((a) => `<tr>
        <td class="sym">${esc(a.ticker)}</td>
        <td>Price ${esc(a.direction)} ${money(a.price)}</td>
        <td class="small">${esc(a.note || "")}</td>
        <td>${a.active ? '<span class="pill hold">Watching</span>' : `<span class="pill review">Triggered ${a.triggered_at ? timeAgo(a.triggered_at) : ""}</span>`}</td>
        <td><button class="btn ghost small danger" data-del-pa="${a.id}">Delete</button></td></tr>`).join("")
    : '<tr><td colspan="5" class="empty">No price alerts. Add one from a stock\'s page.</td></tr>';
}

document.querySelector("#priceAlertTable tbody").addEventListener("click", async (e) => {
  const del = e.target.closest("[data-del-pa]");
  if (!del) return;
  await api("DELETE", `/api/price-alerts/${del.dataset.delPa}`);
  loadPriceAlerts();
});

async function addPriceAlert(preset) {
  if (!state.selected) return toast("Pick a stock first.", "warn");
  const body = preset
    ? { ticker: state.selected, ...preset }
    : { ticker: state.selected, direction: $("paDirection").value, price: Number($("paPrice").value), note: $("paNote").value.trim() };
  if (!body.price || body.price <= 0) return toast("Enter a price for the alert.", "warn");
  try {
    await api("POST", "/api/price-alerts", body);
    $("paPrice").value = "";
    $("paNote").value = "";
    loadPriceAlerts();
    toast(`Alert set: ${body.ticker} ${body.direction} ${money(body.price)}.`, "ok");
  } catch (err) {
    toast(err.message, "err");
  }
}

// ---------- settings / session ----------
function fillSettings() {
  const s = state.settings;
  $("setCash").value = s.cash_available ?? "";
  $("setMaxPct").value = s.max_position_pct != null ? +(s.max_position_pct * 100).toFixed(2) : "";
  $("setAlerts").checked = s.alerts_enabled !== false;
  $("setNotif").checked = s.browser_notifications !== false;
  $("setSound").checked = s.sound !== false;
}

async function loadSettings() {
  try {
    state.settings = await api("GET", "/api/settings");
    fillSettings();
  } catch { /* defaults */ }
}

async function saveSettings() {
  const cash = $("setCash").value.trim();
  const maxPct = $("setMaxPct").value.trim();
  try {
    state.settings = await api("POST", "/api/settings", {
      cash_available: cash ? Number(cash) : null,
      max_position_pct: maxPct ? Number(maxPct) / 100 : null,
      alerts_enabled: $("setAlerts").checked,
      browser_notifications: $("setNotif").checked,
      sound: $("setSound").checked,
    });
    if (state.settings.browser_notifications) requestNotifications(true);
    renderHoldings();
    state.signals = {};
    refreshSignals();
    toast("Settings saved.", "ok");
  } catch (err) {
    toast(err.message, "err");
  }
}

async function requestNotifications(quiet = false) {
  if (!("Notification" in window)) return quiet || toast("This browser does not support desktop notifications.", "warn");
  if (Notification.permission === "granted") return quiet || toast("Desktop notifications are already on.", "ok");
  const res = await Notification.requestPermission();
  if (!quiet) toast(res === "granted" ? "Desktop notifications enabled." : "Notifications were blocked in the browser.", res === "granted" ? "ok" : "warn");
  updateNotifButton();
}

function updateNotifButton() {
  const btn = $("enableNotifBtn");
  const granted = "Notification" in window && Notification.permission === "granted";
  btn.textContent = granted ? "Desktop notifications on" : "Enable desktop notifications";
  btn.disabled = granted;
}

async function logout() {
  await fetch("/auth/logout", { method: "POST" }).catch(() => {});
  window.location.replace("/login");
}

// ---------- wiring ----------
$("bellBtn").addEventListener("click", () => showView("alerts"));
$("checkNowBtn").addEventListener("click", (e) => busy(e.currentTarget, () => checkAlertsNow()));
$("markAllReadBtn").addEventListener("click", async () => {
  await api("POST", "/api/alerts/read", { ids: null });
  loadAlerts();
});
$("enableNotifBtn").addEventListener("click", () => requestNotifications());
$("addPriceAlertBtn").addEventListener("click", () => addPriceAlert());
$("saveSettingsBtn").addEventListener("click", saveSettings);
$("logoutBtn").addEventListener("click", logout);
$("extractProfileBtn").addEventListener("click", extractProfile);
$("approveProfileBtn").addEventListener("click", approveProfile);
$("revokeProfileBtn").addEventListener("click", revokeProfile);
$("reloadReviewBtn").addEventListener("click", reloadReviewFromJson);
$("clearProfileBtn").addEventListener("click", () => {
  $("caseStudyText").value = "";
  $("clientProfileJson").value = "";
  state.profile = state.approved ? state.profile : null;
  renderProfileReview(null);
});
$("addHoldingBtn").addEventListener("click", addHolding);
$("hTicker").addEventListener("keydown", (e) => e.key === "Enter" && addHolding());
$("refreshQuotesBtn").addEventListener("click", async () => { await pollQuotes(); refreshSignals(); });
$("runAnalysisBtn").addEventListener("click", runAnalysis);
$("runQuickScanBtn").addEventListener("click", runQuickScan);
$("runScanBtn").addEventListener("click", runDeepScan);
$("runBacktestBtn").addEventListener("click", runBacktest);
$("refreshInterval").addEventListener("change", startPolling);

async function init() {
  $("tradeDate").value = new Date().toISOString().slice(0, 10);
  try {
    const me = await api("GET", "/auth/me");
    $("userName").textContent = me.username || "—";
  } catch { return; }
  updateNotifButton();
  await loadSettings();
  try {
    const h = await api("GET", "/health");
    $("healthBadge").textContent = `API ${h.status.toUpperCase()}`;
    $("healthBadge").className = "chip ok";
  } catch {
    $("healthBadge").textContent = "API offline";
    $("healthBadge").className = "chip err";
  }
  try {
    const p = await api("GET", "/api/profile");
    if (p.approved) setProfile(p.client_profile, true);
  } catch { /* profile store unavailable */ }
  updateStepBadges();
  await loadHoldings().catch(() => {});
  startPolling();
  await pollQuotes();
  refreshSignals();
  await loadAlerts();
  loadPriceAlerts();
  state.alertTimer = setInterval(loadAlerts, 20000);
  if (state.approved && state.holdings.length) showView("portfolio");
}

init();
