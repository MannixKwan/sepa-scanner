"use strict";
/* SEPA Scanner dashboard - vanilla JS, reads data/summary.json and data/t/<TICKER>.json */

const $ = (s, el = document) => el.querySelector(s);
const app = $("#app");
let S = null;                       // summary.json
const details = new Map();          // ticker -> detail json
let charts = [];                    // live chart instances (disposed on navigation)
const sortState = {};               // table id -> {key, asc}

// ------------------------------------------------------------------ helpers
const css = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = {
  px: (v) => (v == null ? "–" : Number(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })),
  pct: (v, d = 1, sign = true) => (v == null ? "–" : Math.abs(v) >= 10 ? (v > 0 ? ">+999%" : "<−999%")
    : (sign && v > 0 ? "+" : "") + (v * 100).toFixed(d) + "%"),
  int: (v) => (v == null ? "–" : Math.round(v).toLocaleString("en-US")),
  big: (v) => {
    if (v == null) return "–";
    const a = Math.abs(v);
    if (a >= 1e12) return (v / 1e12).toFixed(2) + "T";
    if (a >= 1e9) return (v / 1e9).toFixed(a >= 1e10 ? 1 : 2) + "B";
    if (a >= 1e6) return (v / 1e6).toFixed(0) + "M";
    return Math.round(v).toLocaleString("en-US");
  },
  usd: (v) => (v == null ? "–" : "$" + Math.round(v).toLocaleString("en-US")),
};
const dir = (v) => (v == null ? "" : v > 0 ? "up" : v < 0 ? "down" : "");

const STATUS = {
  BREAKOUT: ["good", "Breakout"], "BUY ZONE": ["good", "Buy zone"], READY: ["info", "Ready"],
  FORMING: ["", "Forming"], EXTENDED: ["warning", "Extended"], TREND: ["", "Trend only"], FAILED: ["critical", "Failed"],
};
const ACTION = { EXIT: "critical", "SELL PARTIAL": "good", TIGHTEN: "serious", HOLD: "" };
const REGIME = { uptrend: "good", pressure: "warning", correction: "critical" };
const MODE_KIND = { defensive: "critical", pilot: "info", normal: "good", full: "good" };
const chip = (kind, text, title = "") => `<span class="chip ${kind}" title="${esc(title)}"><span class="dot"></span>${esc(text)}</span>`;
const statusChip = (s) => chip((STATUS[s] || [""])[0], (STATUS[s] || [s, s])[1], S?.status_help?.[s] || "");

const ICON = {
  ok: `<svg class="ic" viewBox="0 0 16 16" aria-label="pass"><circle cx="8" cy="8" r="7" fill="var(--good)"/><path d="M4.8 8.2l2.1 2.1 4.3-4.6" stroke="#fff" stroke-width="1.8" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
  no: `<svg class="ic" viewBox="0 0 16 16" aria-label="fail"><circle cx="8" cy="8" r="7" fill="var(--critical)"/><path d="M5.5 5.5l5 5M10.5 5.5l-5 5" stroke="#fff" stroke-width="1.8" stroke-linecap="round"/></svg>`,
  plus: `<svg class="ic" viewBox="0 0 16 16" aria-label="positive"><circle cx="8" cy="8" r="7" fill="var(--series-1)"/><path d="M8 4.6v6.8M4.6 8h6.8" stroke="#fff" stroke-width="1.8" stroke-linecap="round"/></svg>`,
  warn: `<svg class="ic" viewBox="0 0 16 16" aria-label="warning"><path d="M8 1.5l7 12.5H1z" fill="var(--warning)"/><path d="M8 6v4M8 11.8v.4" stroke="#0b0b0b" stroke-width="1.6" stroke-linecap="round"/></svg>`,
  dot: `<svg class="ic" viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="3" fill="var(--muted)"/></svg>`,
};

async function getJSON(url) {
  const r = await fetch(url + (url.includes("?") ? "&" : "?") + "_=" + Date.now(), { cache: "no-store" });
  if (!r.ok) throw new Error(`${url}: ${r.status}`);
  return r.json();
}
async function loadDetail(t) {
  if (!details.has(t)) details.set(t, await getJSON(`data/t/${encodeURIComponent(t)}.json`));
  return details.get(t);
}

function timeLabel(iso) {
  const d = new Date(iso);
  const et = d.toLocaleString("en-US", { timeZone: "America/New_York", weekday: "short", hour: "2-digit", minute: "2-digit", hour12: false });
  const tz = S?.settings?.local_timezone;
  let local = "";
  try {
    local = d.toLocaleString("en-US", { timeZone: tz || undefined, hour: "2-digit", minute: "2-digit", hour12: false, timeZoneName: "short" });
  } catch { /* unknown zone */ }
  return `${et} ET${local ? " · " + local : ""}`;
}
function renderHeader() {
  const mode = S.mode === "full" ? "full scan" : "15-min refresh";
  $("#updated").textContent = `Updated ${timeLabel(S.generated_at)} (${mode})`;
}

// ------------------------------------------------------------------ tooltip
const tip = $("#tip");
function showTip(e, html) {
  tip.innerHTML = html;
  tip.hidden = false;
  const pad = 14, w = tip.offsetWidth, h = tip.offsetHeight;
  let x = e.clientX + pad, y = e.clientY + pad;
  if (x + w > innerWidth - 8) x = e.clientX - w - pad;
  if (y + h > innerHeight - 8) y = e.clientY - h - pad;
  tip.style.left = Math.max(8, x) + "px";
  tip.style.top = Math.max(8, y) + "px";
}
document.addEventListener("pointermove", (e) => {
  const t = e.target.closest?.("[data-tip]");
  if (t) showTip(e, t.getAttribute("data-tip"));
  else tip.hidden = true;
});
document.addEventListener("pointerdown", (e) => {
  const t = e.target.closest?.("[data-tip]");
  if (t) showTip(e, t.getAttribute("data-tip"));
});

// ------------------------------------------------------------------ pieces
function tile(label, value, delta = "", cls = "") {
  return `<div class="tile"><div class="label">${esc(label)}</div><div class="value ${cls}">${value}</div><div class="delta">${delta}</div></div>`;
}

/* Where price sits between the stop, the pivot and the top of the buy zone. */
function meter(stop, pivot, zoneHi, price, labels = ["Stop", "Pivot", "+5%"]) {
  if ([stop, pivot, zoneHi, price].some((v) => v == null)) return "";
  const lo = Math.min(stop, pivot, price) * 0.985, hi = Math.max(zoneHi, pivot, price) * 1.01;
  const x = (v) => ((v - lo) / (hi - lo)) * 100;
  const riskW = Math.max(0, x(pivot) - x(stop));   // a stop raised above entry has no risk band
  return `<div class="meter" aria-label="price ${fmt.px(price)}, stop ${fmt.px(stop)}, pivot ${fmt.px(pivot)}">
    <div class="track"></div>
    <div class="risk" style="left:${x(stop)}%;width:${riskW}%"></div>
    <div class="zone" style="left:${x(pivot)}%;width:${x(zoneHi) - x(pivot)}%"></div>
    <div class="mark stop" style="left:${x(stop)}%"></div>
    <div class="mark" style="left:${x(pivot)}%"></div>
    <div class="px" style="left:${x(price)}%" data-tip="Price ${fmt.px(price)}"></div>
    <div class="lbl" style="left:${x(stop)}%">${labels[0]}</div>
    <div class="lbl" style="left:${x(pivot)}%">${labels[1]}</div>
    <div class="lbl" style="left:${x(zoneHi)}%">${labels[2]}</div>
  </div>`;
}

function pivotText(r) {
  if (r.pivot_dist == null) return "";
  return r.pivot_dist < 0 ? `${fmt.pct(-r.pivot_dist, 1, false)} below pivot` : `${fmt.pct(r.pivot_dist, 1, false)} above pivot`;
}

function pickCard(r) {
  const p = r.plan || {}, m = r.fund || {}, s = r.setup || {};
  const flags = (r.flags || []).slice(0, 2).map((f) => `<div class="warnline">${ICON.warn}<span>${esc(f)}</span></div>`).join("");
  return `<a class="panel card" href="#/t/${encodeURIComponent(r.ticker)}">
    <div class="row1">
      <div><div class="tk">${esc(r.ticker)}</div><div class="nm" title="${esc(r.name)}">${esc(r.name)}</div></div>
      <div class="px"><b class="num">${fmt.px(r.price)}</b><div class="num ${dir(r.change_pct)}">${fmt.pct(r.change_pct)}</div></div>
    </div>
    <div class="facts">${statusChip(r.status)}<span class="tag">${pivotText(r)}</span><span class="tag">Score ${r.score}</span></div>
    ${meter(p.stop, p.entry, p.buy_zone_hi, r.price)}
    <div class="kv num">
      <div><div class="k">Buy above</div><div class="v">${fmt.px(p.entry)}</div></div>
      <div><div class="k">Stop</div><div class="v">${fmt.px(p.stop)}</div></div>
      <div><div class="k">Risk</div><div class="v">${fmt.pct(p.risk_pct, 1, false)}</div></div>
      <div><div class="k">Shares</div><div class="v">${fmt.int(p.shares)}</div></div>
      <div><div class="k">RS rating</div><div class="v">${r.rs ?? "–"}</div></div>
      <div><div class="k">EPS (qtr)</div><div class="v">${fmt.pct(m.eps_q_yoy, 0)}</div></div>
      <div><div class="k">Sales (qtr)</div><div class="v">${fmt.pct(m.sales_q_yoy, 0)}</div></div>
      <div><div class="k">VCP</div><div class="v">${s.count ? s.count + "T · " + fmt.pct(s.final_depth, 0, false) : "–"}</div></div>
    </div>
    ${flags}
  </a>`;
}

function positionCard(p, compact = false) {
  const t = p.targets || {};
  const sig = (p.signals || []).map((s) => `<li>${s.action === "HOLD" ? ICON.dot : s.action === "SELL PARTIAL" ? ICON.plus : ICON.warn}<span>${esc(s.text)}</span></li>`).join("");
  return `<div class="panel card" onclick="location.hash='#/t/${encodeURIComponent(p.ticker)}'">
    <div class="row1">
      <div><div class="tk">${esc(p.ticker)}</div><div class="nm">${fmt.int(p.shares)} sh @ ${fmt.px(p.entry)}${p.entry_date ? " · " + esc(p.entry_date) : ""}</div></div>
      <div class="px"><b class="num">${fmt.px(p.price)}</b><div class="num ${dir(p.gain_pct)}">${fmt.pct(p.gain_pct)} · ${p.r_multiple >= 0 ? "+" : ""}${p.r_multiple}R</div></div>
    </div>
    <div class="facts">${chip(ACTION[p.action] || "", p.action)}<span class="tag">Stop ${fmt.px(p.stop)} · ${esc(p.stop_reason)}</span></div>
    ${meter(p.stop, p.entry, t["+20%"], p.price, ["Stop", "Entry", "+20%"])}
    <div class="kv num">
      <div><div class="k">To stop</div><div class="v">${fmt.pct(p.stop_dist_pct, 1, false)}</div></div>
      <div><div class="k">P&amp;L</div><div class="v ${dir(p.pnl)}">${fmt.usd(p.pnl)}</div></div>
      <div><div class="k">50-day MA</div><div class="v">${fmt.px(p.sma50)}</div></div>
      <div><div class="k">Earnings</div><div class="v">${p.days_to_earnings != null ? p.days_to_earnings + "d" : "–"}</div></div>
    </div>
    ${compact ? "" : `<div class="kv num">
      <div><div class="k">2R</div><div class="v">${fmt.px(t["2R"])}</div></div>
      <div><div class="k">3R</div><div class="v">${fmt.px(t["3R"])}</div></div>
      <div><div class="k">+20%</div><div class="v">${fmt.px(t["+20%"])}</div></div>
      <div><div class="k">+25%</div><div class="v">${fmt.px(t["+25%"])}</div></div>
    </div>`}
    ${sig ? `<ul class="list">${sig}</ul>` : `<div class="warnline">${ICON.dot}<span>No exit signals - hold and let it work.</span></div>`}
  </div>`;
}

// sortable table -------------------------------------------------------------
function table(id, rows, cols, defSort) {
  sortState[id] = sortState[id] || { key: defSort.key, asc: defSort.asc };
  const st = sortState[id];
  const col = cols.find((c) => c.key === st.key) || cols[0];
  const val = col.sort || ((r) => r[col.key]);
  const sorted = [...rows].sort((a, b) => {
    const x = val(a), y = val(b);
    if (x == null && y == null) return 0;
    if (x == null) return 1;
    if (y == null) return -1;
    return (x < y ? -1 : x > y ? 1 : 0) * (st.asc ? 1 : -1);
  });
  const head = cols.map((c) => `<th class="${c.l ? "l" : ""} ${c.key === st.key ? "sorted" + (st.asc ? " asc" : "") : ""}" data-k="${c.key}" data-t="${id}">${c.label}</th>`).join("");
  const body = sorted.map((r) => `<tr data-go="${esc(r.ticker)}">${cols.map((c) => `<td class="${c.l ? "l" : ""}">${c.f(r)}</td>`).join("")}</tr>`).join("");
  return `<div class="tablewrap"><table id="${id}"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}
document.addEventListener("click", (e) => {
  const th = e.target.closest("th[data-k]");
  if (th) {
    const st = sortState[th.dataset.t];
    if (st.key === th.dataset.k) st.asc = !st.asc;
    else { st.key = th.dataset.k; st.asc = false; }
    route(true);
    return;
  }
  const tr = e.target.closest("tr[data-go]");
  if (tr) location.hash = "#/t/" + encodeURIComponent(tr.dataset.go);
});

const STATUS_RANK = { BREAKOUT: 0, "BUY ZONE": 1, READY: 2, FORMING: 3, EXTENDED: 4, TREND: 5, FAILED: 6 };
const COLS = {
  ticker: { key: "ticker", label: "Stock", l: true, f: (r) => `<span class="t">${esc(r.ticker)}</span> <span class="n">${esc(r.name)}</span>` },
  status: { key: "status", label: "Status", l: true, sort: (r) => STATUS_RANK[r.status], f: (r) => statusChip(r.status) },
  price: { key: "price", label: "Price", f: (r) => fmt.px(r.price) },
  chg: { key: "change_pct", label: "Today", f: (r) => `<span class="${dir(r.change_pct)}">${fmt.pct(r.change_pct)}</span>` },
  pivot: { key: "pivot_dist", label: "To pivot", f: (r) => (r.pivot_dist == null ? "–" : fmt.pct(r.pivot_dist)) },
  rs: { key: "rs", label: "RS", f: (r) => (r.rs ?? "–") + (r.rs_new_high ? " ●" : "") },
  eps: { key: "eps_q_yoy", label: "EPS qtr", sort: (r) => (r.fund ? r.fund.eps_q_yoy : r.eps_q_yoy), f: (r) => fmt.pct(r.fund ? r.fund.eps_q_yoy : r.eps_q_yoy, 0) },
  sales: { key: "sales_q_yoy", label: "Sales qtr", sort: (r) => (r.fund ? r.fund.sales_q_yoy : r.sales_q_yoy), f: (r) => fmt.pct(r.fund ? r.fund.sales_q_yoy : r.sales_q_yoy, 0) },
  vcp: { key: "vcp", label: "VCP", sort: (r) => (r.setup?.valid ? r.setup.count : r.vcp_count), f: (r) => { const c = r.setup?.valid ? r.setup.count : r.vcp_count; const d = r.setup?.valid ? r.setup.final_depth : r.final_depth; return c ? `${c}T · ${fmt.pct(d, 0, false)}` : "–"; } },
  earn: { key: "days_to_earnings", label: "Earnings", f: (r) => (r.days_to_earnings == null ? "–" : r.days_to_earnings < 0 ? "–" : r.days_to_earnings + "d") },
  score: { key: "score", label: "Score", f: (r) => `<b>${r.score}</b>` },
  offhigh: { key: "pct_below_high", label: "Off high", f: (r) => fmt.pct(-(r.pct_below_high ?? 0), 1) },
  ind: { key: "industry", label: "Industry", l: true, f: (r) => `<span class="n">${esc(r.industry || "–")}</span>` },
};

// ------------------------------------------------------------------ views
function viewHome() {
  const m = S.market, b = m.benchmark, br = m.breadth || {}, c = S.counts || {};
  const pos = S.positions || [];
  const act = pos.filter((p) => p.action !== "HOLD");
  app.innerHTML = `
  <section class="panel market">
    <div>
      <div class="headline">${chip(REGIME[m.regime], m.label)}<span class="muted">as of ${esc(m.as_of)}</span></div>
      <p>${esc(m.guidance)}</p>
      <ul class="list">${m.reasons.map((x) => `<li>${ICON.dot}<span class="ink2">${esc(x)}</span></li>`).join("")}</ul>
    </div>
    <div class="tiles num">
      ${tile(b.symbol, fmt.px(b.close), `<span class="${dir(b.change_pct)}">${fmt.pct(b.change_pct)}</span> today`)}
      ${tile("vs 50 / 200-day", fmt.pct(b.pct_from_sma50), `${fmt.pct(b.pct_from_sma200)} vs 200-day`)}
      ${tile("Distribution days", m.distribution_days.length, "last 25 sessions")}
      ${tile("Above 50-day MA", fmt.pct(br.pct_above_50, 0, false), "of liquid NASDAQ stocks")}
      ${tile("Trend Template", fmt.int(c.tt_pass), `of ${fmt.int(c.liquid)} liquid stocks`)}
      ${tile("New highs / lows", `${br.new_highs ?? "–"} / ${br.new_lows ?? "–"}`, "52-week, last session")}
    </div>
  </section>

  ${portfolioStrip(S.portfolio)}

  ${act.length ? `<h2>Action needed on your positions</h2><div class="grid cards">${act.map((p) => positionCard(p, true)).join("")}</div>` : ""}

  <h2>Stocks to watch <span class="sub">actionable now · fundamentals pass · best first</span></h2>
  ${S.top_picks.length ? `<div class="grid cards">${S.top_picks.map(pickCard).join("")}</div>`
    : `<div class="panel empty">No actionable VCP setups that also pass the earnings/sales filters right now.<br>The watchlist below shows bases that are still forming.</div>`}

  ${pos.length && !act.length ? `<h2>Your positions <span class="sub">no exit signals</span></h2><div class="grid cards">${pos.map((p) => positionCard(p, true)).join("")}</div>` : ""}

  <h2>Watchlist <span class="sub">VCP bases - breakouts, ready and forming (${S.watchlist.length})</span></h2>
  ${S.watchlist.length ? table("wl", S.watchlist, [COLS.ticker, COLS.status, COLS.price, COLS.chg, COLS.pivot, COLS.rs, COLS.eps, COLS.sales, COLS.vcp, COLS.earn, COLS.score], { key: "status", asc: true })
    : `<div class="panel empty">No VCP bases right now.</div>`}

  <h2>Leading industries <span class="sub">most stocks passing the Trend Template</span></h2>
  ${industryBars(S.industries || [])}
  `;
}

function industryBars(rows) {
  if (!rows.length) return `<div class="panel empty">Industry data appears after fundamentals are fetched.</div>`;
  const max = Math.max(...rows.map((r) => r.count));
  return `<div class="panel bars">${rows.slice(0, 10).map((r) => `
    <div class="row" data-tip="${esc(r.industry)}: ${r.count} stocks, avg RS ${r.avg_rs}<br>${esc(r.tickers.join(", "))}">
      <div><div class="name">${esc(r.industry)}</div><div class="tks">${esc(r.tickers.join(" · "))}</div></div>
      <div><div class="bt" style="width:${Math.max(4, (r.count / max) * 100)}%"></div></div>
      <div class="num" style="text-align:right;font-weight:600">${r.count}</div>
    </div>`).join("")}</div>`;
}

function portfolioStrip(pf) {
  if (!pf) return "";
  return `<a class="panel strip" href="#/portfolio">
    ${chip(MODE_KIND[pf.mode] || "", pf.mode_label + " mode")}
    <span><b>${fmt.pct(pf.exposure_pct, 0, false)}</b> invested of ${fmt.usd(pf.capital)} <span class="muted">(max ${fmt.pct(pf.target_exposure_pct, 0, false)})</span></span>
    <span>Room <b>${fmt.usd(pf.budget)}</b> · ${pf.slots} slot${pf.slots === 1 ? "" : "s"}</span>
    <span>Open risk <b>${fmt.pct(pf.open_risk_pct, 1, false)}</b></span>
    <span class="muted">${pf.ibkr_stale ? "IBKR offline (last snapshot)" : pf.ibkr ? "IBKR live" : "IBKR not connected"} →</span>
  </a>`;
}

function exposureBar(pf) {
  const scale = Math.max(1, pf.exposure_pct + pf.reserved_for_orders / pf.capital, pf.target_exposure_pct);
  const w = (x) => Math.max(0, Math.min(100, (x / scale) * 100));
  const inv = w(pf.exposure_pct), res = w(pf.reserved_for_orders / (pf.capital || 1)), tgt = w(pf.target_exposure_pct);
  return `<div class="xbar" aria-label="invested ${fmt.pct(pf.exposure_pct, 0, false)}, target ${fmt.pct(pf.target_exposure_pct, 0, false)}">
    <div class="xtrack">
      <div class="xinv" style="width:${inv}%" data-tip="Invested in US stocks: ${fmt.usd(pf.invested)}"></div>
      <div class="xres" style="left:${inv}%;width:${res}%" data-tip="Reserved by open buy orders: ${fmt.usd(pf.reserved_for_orders)}"></div>
      <div class="xtgt" style="left:${tgt}%" data-tip="Max exposure for this market and mode: ${fmt.pct(pf.target_exposure_pct, 0, false)}"></div>
    </div>
    <div class="xlab"><span>Invested ${fmt.pct(pf.exposure_pct, 0, false)}</span><span>Max ${fmt.pct(pf.target_exposure_pct, 0, false)}</span></div>
  </div>`;
}

function viewPortfolio() {
  const pf = S.portfolio, pos = S.positions || [], st = S.settings || {}, pr = st.positions_rules || {};
  if (!pf) { app.innerHTML = `<div class="panel empty">No portfolio data yet - run a scan.</div>`; return; }
  const plan = pf.plan || { rows: [] };
  const conv = pf.base_currency && pf.base_currency !== "USD" ? ` · converted to USD from ${esc(pf.base_currency)}` : "";
  const planCols = [
    { key: "rank", label: "#", l: true, f: (r) => `<span class="muted">${r.rank}</span>` },
    COLS.ticker, COLS.status,
    { key: "entry", label: "Buy at", f: (r) => fmt.px(r.entry) },
    { key: "stop", label: "Stop", f: (r) => fmt.px(r.stop) },
    { key: "shares", label: "Shares", f: (r) => (r.shares ? fmt.int(r.shares) : "–") },
    { key: "value", label: "Amount", f: (r) => (r.shares ? fmt.usd(r.value) : "–") },
    { key: "pct_capital", label: "% capital", f: (r) => (r.shares ? fmt.pct(r.pct_capital, 1, false) : "–") },
    { key: "risk_amount", label: "Risk $", f: (r) => (r.shares ? fmt.usd(r.risk_amount) : "–") },
    { key: "risk_pct_capital", label: "Risk %", f: (r) => (r.shares ? fmt.pct(r.risk_pct_capital, 2, false) : "–") },
    { key: "limit", label: "Limited by", l: true, f: (r) => `<span class="n" style="max-width:260px">${esc(r.limit)}</span>` },
  ];
  const other = (pf.other_holdings || []).length ? `<h2>Other holdings <span class="sub">count toward capital, not managed by the scanner</span></h2>
    <div class="tablewrap"><table><thead><tr><th class="l">Holding</th><th>Type</th><th>Qty</th><th>Value (USD)</th></tr></thead><tbody>
    ${pf.other_holdings.map((o) => `<tr><td class="l">${esc(o.symbol)} <span class="n">${esc(o.currency)}</span></td><td>${esc(o.type)}</td><td>${fmt.int(o.qty)}</td><td>${fmt.usd(o.value_usd)}</td></tr>`).join("")}</tbody></table></div>` : "";
  const pending = (pf.pending_buys || []).length ? `<h2>Open buy orders <span class="sub">cash reserved</span></h2>
    <div class="tablewrap"><table><thead><tr><th class="l">Stock</th><th>Type</th><th>Qty</th><th>Price</th><th>Reserved</th></tr></thead><tbody>
    ${pf.pending_buys.map((b) => `<tr><td class="l"><span class="t">${esc(b.ticker)}</span></td><td>${esc(b.type)}</td><td>${fmt.int(b.qty)}</td><td>${fmt.px(b.price)}</td><td>${fmt.usd(b.value_usd)}</td></tr>`).join("")}</tbody></table></div>` : "";
  const conc = (pf.concentration || []).length ? `<h2>Concentration by industry</h2><div class="panel bars">${pf.concentration.map((c) => `
    <div class="row"><div class="name">${esc(c.industry)}</div><div><div class="bt" style="width:${Math.max(3, c.pct * 100 / Math.max(...pf.concentration.map((x) => x.pct)))}%"></div></div><div class="num" style="text-align:right">${fmt.pct(c.pct, 0, false)}</div></div>`).join("")}</div>` : "";

  app.innerHTML = `
    <h1>Portfolio &amp; allocation</h1>
    <p class="muted">${pf.ibkr_stale ? `<b>IBKR offline</b> - showing the last snapshot from ${esc((pf.ibkr_as_of || "").replace("T", " ").slice(0, 16))} ET. If IBKR Mobile shows a login request, approve it.` : pf.ibkr ? `Live from Interactive Brokers (read-only)${conv}.` : "IBKR not connected - using positions.yaml and risk.account_equity from config.yaml."}
      Sizing follows Minervini's rules with the parameters in config.yaml → risk. Mechanical output, not advice.</p>
    <section class="panel">
      <div class="headline" style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:10px">${chip(MODE_KIND[pf.mode] || "", pf.mode_label + " mode")}<span class="ink2">${esc(pf.mode_reason)}</span></div>
      ${exposureBar(pf)}
      <div class="tiles num" style="margin-top:12px">
        ${tile("Capital", fmt.usd(pf.capital), esc(pf.capital_source))}
        ${tile("Invested", fmt.pct(pf.exposure_pct, 0, false), `${fmt.usd(pf.invested)} · max ${fmt.pct(pf.target_exposure_pct, 0, false)} now`)}
        ${tile("Cash", fmt.usd(pf.cash), pf.use_margin ? `buying power ${fmt.usd(pf.buying_power)}` : "no margin")}
        ${tile("Room for new buys", fmt.usd(pf.budget), `${pf.slots} of ${pf.max_positions} slots free`)}
        ${tile("Open risk", fmt.pct(pf.open_risk_pct, 1, false), `if every stop hits · cap ${fmt.pct(pf.max_open_risk_pct, 0, false)}`)}
        ${tile("Per new trade", fmt.pct(pf.risk_per_trade_pct, 2, false) + " risk", `position ≤ ${fmt.pct(pf.max_position_pct, 1, false)} of capital`)}
      </div>
    </section>

    <h2>If the best setups trigger <span class="sub">sized in priority order (breakouts first, then best score); each one uses up cash, slots and risk</span></h2>
    ${plan.rows.length ? table("plan", plan.rows, planCols, { key: "rank", asc: true }) : `<div class="panel empty">No actionable setups right now.</div>`}
    ${plan.rows.length ? `<p class="muted" style="margin-top:8px">Buying every sized row: ${fmt.usd(plan.total_value)} → invested ${fmt.pct(plan.exposure_after_pct, 0, false)} of capital, open risk ${fmt.pct(plan.open_risk_after_pct, 1, false)}. Only buy the ones that actually break out on volume.</p>` : ""}

    <h2>Open positions <span class="sub">${pos.length} · exit plan per Minervini's sell rules</span></h2>
    ${pos.length ? `<div class="grid cards">${pos.map((p) => positionCard(p)).join("")}</div>`
      : `<div class="panel empty">No open US stock positions${pf.ibkr ? " in IBKR" : ""}.${pf.ibkr ? "" : "<br>Connect IBKR or add them to <code>positions.yaml</code>."}</div>`}
    ${pending}${other}${conc}

    <h2>How the allocation works</h2>
    <div class="panel prose"><ul>
      <li><b>Size off the stop.</b> Shares = capital × risk per trade ÷ (entry − stop). A tight stop allows more shares; a wide one, fewer.</li>
      <li><b>Cap each position</b> at ${fmt.pct(st.max_position_pct, 0, false)} of capital (half in pilot mode) and hold at most ${pf.max_positions} names, so winners can matter.</li>
      <li><b>Progressive exposure.</b> <i>Pilot</i> (half size, ≤ 50% invested) until trades work → <i>Normal</i> → <i>Full</i> (${fmt.pct(st.full_risk_per_trade_pct, 1, false)} risk vs ${fmt.pct(st.normal_risk_per_trade_pct, 2, false)} normal) only when several positions are up ≥ 1R and none are losing. Back to pilot when most are under water.</li>
      <li><b>Market sets the ceiling:</b> uptrend 100%, under pressure 50%, correction 0% (no new buys).</li>
      <li><b>Total open risk</b> (every stop hit at once) stays under ${fmt.pct(pf.max_open_risk_pct, 0, false)} of capital.</li>
      <li>Never average down; add only to positions that are working. Exits: stop → breakeven at +${pr.breakeven_at_r}R → +1R locked at +${pr.lock_in_at_r}R, sell ⅓–½ into strength, rest below the 50-day MA.</li>
    </ul></div>`;
}

let allFilter = { q: "", f: "all" };
function viewAll() {
  const F = {
    all: ["All", () => true],
    act: ["Actionable", (r) => ["BREAKOUT", "BUY ZONE", "READY"].includes(r.status)],
    vcp: ["Has VCP", (r) => r.vcp_count],
    fund: ["Fundamentals pass", (r) => r.fund_pass],
    rs90: ["RS ≥ 90", (r) => r.rs >= 90],
  };
  const q = allFilter.q.trim().toUpperCase();
  const rows = (S.tt_passers || []).filter(F[allFilter.f][1]).filter((r) => !q || r.ticker.includes(q) || (r.name || "").toUpperCase().includes(q) || (r.industry || "").toUpperCase().includes(q));
  app.innerHTML = `
    <h1>All Trend-Template stocks</h1>
    <p class="muted">${(S.tt_passers || []).length} NASDAQ stocks pass all 8 Trend Template criteria (Stage 2 uptrends). Tap a row for the full analysis.</p>
    <div class="filters">
      <input id="q" type="search" placeholder="Search ticker, name, industry" value="${esc(allFilter.q)}">
      <div class="seg">${Object.entries(F).map(([k, [lab]]) => `<button data-f="${k}" class="${allFilter.f === k ? "on" : ""}">${lab}</button>`).join("")}</div>
      <span class="muted">${rows.length} shown</span>
    </div>
    ${table("all", rows, [COLS.ticker, COLS.status, COLS.price, COLS.chg, COLS.rs, COLS.offhigh, COLS.eps, COLS.sales, COLS.vcp, COLS.earn, COLS.score, COLS.ind], { key: "score", asc: false })}`;
  const qi = $("#q");
  qi.addEventListener("input", () => { allFilter.q = qi.value; const pos = qi.selectionStart; viewAll(); const n = $("#q"); n.focus(); n.setSelectionRange(pos, pos); });
  app.querySelectorAll("[data-f]").forEach((b) => b.addEventListener("click", () => { allFilter.f = b.dataset.f; viewAll(); }));
}

function viewRules() {
  const st = S.settings || {}, pr = st.positions_rules || {};
  app.innerHTML = `<div class="prose">
  <h1>How the scanner works</h1>
  <p class="ink2">An automated version of Mark Minervini's SEPA® approach (<i>Trade Like a Stock Market Wizard</i>, <i>Think &amp; Trade Like a Champion</i>). It narrows ~3,000 NASDAQ stocks to a handful of low-risk entry points. It is a decision aid, not advice - you place every order.</p>
  <h2>1 · Market first</h2>
  <ul><li>QQQ above its rising 50-day MA, 50-day above 200-day, and ≤ 4 distribution days in 25 sessions = <b>Confirmed uptrend</b>.</li>
  <li>Breadth: share of stocks above their 50-day MA, new highs vs lows, and the number of Trend Template stocks (leaders expand in healthy markets).</li></ul>
  <h2>2 · Trend Template (all 8 must pass)</h2>
  <ol><li>Price above the 150-day and 200-day MA</li><li>150-day MA above the 200-day MA</li><li>200-day MA rising for at least 1 month</li><li>50-day MA above the 150 and 200-day MA</li><li>Price above the 50-day MA</li><li>Price ≥ 30% above its 52-week low</li><li>Price within 25% of its 52-week high</li><li>Relative Strength rating ≥ 70 (IBD-style: 40% weight on the last quarter, 20% on each of the prior three, ranked against all NASDAQ stocks)</li></ol>
  <h2>3 · Volatility Contraction Pattern (VCP)</h2>
  <ul><li>From the base high, each pullback must be shallower than the one before (e.g. 25% → 12% → 5%) - 2 to 6 contractions.</li>
  <li>First contraction ≤ 40%, last contraction ≤ 12% (tight), base ≥ 3 weeks; volume should dry up in the final contraction.</li>
  <li><b>Pivot</b> = high of the last contraction. That's the buy point.</li></ul>
  <h2>4 · Fundamentals (SEPA)</h2>
  <ul><li>Latest quarter EPS up ≥ ${fmt.pct(st.min_eps_growth, 0, false)} vs a year ago (or a loss-to-profit turnaround), and sales up ≥ ${fmt.pct(st.min_sales_growth, 0, false)} or accelerating.</li>
  <li>Bonus for acceleration (growth rate rising quarter after quarter), expanding margins, positive earnings surprises, and "Code 33" (EPS, sales and margins accelerating 3 quarters).</li>
  <li>Sources: Yahoo Finance (adjusted EPS, estimates, next report date) and SEC EDGAR filings (revenue and net income history).</li></ul>
  <h2>5 · Buying</h2>
  <ul><li>Buy as the stock trades through the pivot, ideally on volume ≥ 140% of its 50-day average; never more than ${fmt.pct(st.buy_zone_pct, 0, false)} above the pivot.</li>
  <li>Initial stop just below the low of the last contraction, capped at ${fmt.pct(st.max_stop_pct, 0, false)} below the entry.</li>
  <li>Position size = equity × ${fmt.pct(st.risk_per_trade_pct, 2, false)} ÷ (entry − stop), max 25% of equity in one stock. Equity used: ${fmt.usd(st.equity)}.</li>
  <li>Avoid opening new positions in the days right before an earnings report.</li></ul>
  <h2>6 · Selling</h2>
  <ul><li>Sell at the stop, no exceptions.</li><li>Once up ${pr.breakeven_at_r}× your initial risk, raise the stop to breakeven; at ${pr.lock_in_at_r}R lock in +1R.</li>
  <li>Sell ⅓–½ into strength at +${pr.partial_profit_at_r}R or ${fmt.pct(pr.partial_profit_at_pct, 0)}, or on a climax run (${fmt.pct(pr.climax_gain_pct, 0)} in ${pr.climax_days} sessions).</li>
  <li>Sell the rest on a close below the 50-day MA; a close below the 21-day EMA is a warning to tighten.</li></ul>
  <h2>Status labels</h2>
  <ul>${Object.entries(S.status_help || {}).map(([k, v]) => `<li>${statusChip(k)} ${esc(v)}</li>`).join("")}</ul>
  <h2>Schedule</h2>
  <ul><li>Full NASDAQ scan every trading day after the close (~45 min after 4 pm ET), followed by a "plan for next session" message.</li>
  <li>Every 15 minutes while the market is open: live prices for the watchlist and your positions → breakout, near-pivot, stop and exit alerts. A full digest goes out at the times set in config.yaml.</li></ul>
  </div>`;
}

// ------------------------------------------------------------------ detail
async function viewDetail(t) {
  app.innerHTML = `<div class="loading">Loading ${esc(t)}…</div>`;
  let d;
  try { d = await loadDetail(t); } catch {
    app.innerHTML = `<a class="back" href="#/">← Back</a><div class="panel empty">No analysis for ${esc(t)}. Detail pages exist for Trend Template stocks and your positions.</div>`;
    return;
  }
  const r = d.row, f = d.fundamentals;
  app.innerHTML = `
    <a class="back" href="javascript:history.length>1?history.back():location.hash='#/'">← Back</a>
    <div class="dhead">
      <div class="who">
        <h1>${esc(r.ticker)} ${statusChip(r.status)}</h1>
        <div class="ink2">${esc(r.name)}${r.industry ? " · " + esc(r.industry) : ""}${r.market_cap ? " · Mkt cap " + fmt.big(r.market_cap) : ""}${r.inst_pct != null ? " · Inst. own " + fmt.pct(Math.min(r.inst_pct, 1), 0, false) : ""}</div>
      </div>
      <div class="price"><b class="num">${fmt.px(r.price)}</b> <span class="num ${dir(r.change_pct)}">${fmt.pct(r.change_pct)}</span>
        <div class="muted">${esc(r.date)} · RS ${r.rs ?? "–"} · score ${r.score}</div></div>
    </div>
    <div class="grid two">
      <div class="panel chartbox">
        <div class="legend" id="lg"></div>
        <div id="pc" class="chart"></div>
        <div class="legend" style="margin-top:8px"><span>RS line: price relative to QQQ (rising = outperforming the market)${r.rs_new_high ? " · <b>at a new high</b>" : ""}</span></div>
        <div id="rc" class="chart rs"></div>
      </div>
      <div class="grid">
        ${d.position ? positionCard(d.position) : ""}
        ${planPanel(r)}
        ${whyPanel(r)}
      </div>
    </div>
    <div class="grid two" style="margin-top:12px">
      <div class="panel">${fundPanel(f, r)}</div>
      <div class="grid">
        <div class="panel">${ttPanel(r.tt)}</div>
        <div class="panel">${vcpPanel(r.setup)}</div>
      </div>
    </div>`;
  drawCharts(d);
}

function planPanel(r) {
  const p = r.plan, st = S.settings || {};
  if (!p) return `<div class="panel plan"><h3>Trade plan</h3><p class="ink2">${esc(S.status_help?.[r.status] || "")}</p><p class="muted">No proper VCP base yet, so there is no low-risk buy point. Let it build a base.</p></div>`;
  return `<div class="panel plan">
    <h3>Trade plan</h3>
    <p class="ink2">${esc(S.status_help?.[r.status] || "")}</p>
    <dl class="num">
      <dt>Buy-stop at pivot</dt><dd class="big">${fmt.px(p.entry)}</dd>
      <dt>Buy zone</dt><dd>${fmt.px(p.entry)} – ${fmt.px(p.buy_zone_hi)}</dd>
      <dt>Initial stop</dt><dd class="down">${fmt.px(p.stop)} (−${fmt.pct(p.risk_pct, 1, false)})</dd>
      ${r.price > p.entry ? `<dt>Risk if bought now</dt><dd>−${fmt.pct(p.risk_pct_now, 1, false)}</dd>` : ""}
      <dt>Position size</dt><dd>${p.shares ? `${fmt.int(p.shares)} sh ≈ ${fmt.usd(p.position_value)}` : "0 sh"}</dd>
      ${p.sizing ? `<dt></dt><dd class="muted" style="font-weight:400">${p.shares ? fmt.pct(p.sizing.pct_capital, 1, false) + " of capital · " : ""}limited by ${esc(p.sizing.limit)}</dd>` : ""}
      <dt>Risk at stop</dt><dd>${fmt.usd(p.risk_amount)}${p.sizing ? ` (${fmt.pct(p.sizing.risk_pct_capital, 2, false)})` : ""}</dd>
      <dt>Targets</dt><dd>2R ${fmt.px(p.targets["2R"])} · 3R ${fmt.px(p.targets["3R"])}</dd>
      <dt></dt><dd>+20% ${fmt.px(p.targets["+20%"])} · +25% ${fmt.px(p.targets["+25%"])}</dd>
      <dt>Volume pace today</dt><dd>${r.vol_ratio}× avg</dd>
      ${r.next_earnings ? `<dt>Next earnings</dt><dd>${esc(r.next_earnings)}${r.days_to_earnings != null ? ` (${r.days_to_earnings}d)` : ""}</dd>` : ""}
    </dl>
    <p class="muted" style="margin-top:8px">Sized for your portfolio: ${fmt.usd(st.equity)} capital, ${esc(S.portfolio?.mode_label || "")} mode (${fmt.pct(st.risk_per_trade_pct, 2, false)} risk per trade). <a href="#/portfolio">Portfolio →</a></p>
  </div>`;
}

function whyPanel(r) {
  const why = (r.reasons || []).map((x) => `<li>${ICON.plus}<span>${esc(x)}</span></li>`).join("");
  const flags = (r.flags || []).map((x) => `<li>${ICON.warn}<span>${esc(x)}</span></li>`).join("");
  return `<div class="panel"><h3>Why it's on the list</h3><ul class="list">${why || "<li class='muted'>–</li>"}</ul>
    ${flags ? `<h3 style="margin-top:12px">Watch out</h3><ul class="list">${flags}</ul>` : ""}</div>`;
}

function ttPanel(tt) {
  if (!tt || !tt.checks?.length) return `<h3>Trend Template</h3><p class="muted">${esc(tt?.reason || "Not enough history.")}</p>`;
  return `<h3>Trend Template · ${tt.score}/8</h3><ul class="list checks">${tt.checks.map((c) => `<li>${c.pass ? ICON.ok : ICON.no}<span>${esc(c.label)}</span><span class="d num">${esc(c.detail)}</span></li>`).join("")}</ul>`;
}

function vcpPanel(s) {
  if (!s) return `<h3>VCP</h3><p class="muted">No base detected (stock is making new highs or the base is under 3 weeks old).</p>`;
  const rows = s.contractions.map((c, i) => `<tr><td class="l">C${i + 1}</td><td>${esc(c.peak_date)}</td><td>${fmt.px(c.peak)}</td><td>${fmt.px(c.trough)}</td><td><b>−${fmt.pct(c.depth, 1, false)}</b></td></tr>`).join("");
  return `<h3>Volatility contraction · ${s.valid ? chip("good", "Valid VCP") : chip("", "Not a valid VCP yet")}</h3>
    <div class="tiles num" style="margin-bottom:10px">
      ${tile("Contractions", s.count + "T", s.depths.map((x) => fmt.pct(x, 0, false)).join(" → "))}
      ${tile("Base", s.base_weeks + " wks", "depth " + fmt.pct(s.base_depth, 0, false))}
      ${tile("Volume dry-up", fmt.pct(s.vol_dryup, 0, false), "of 50-day avg")}
      ${tile("Quality", s.quality + "/100", "up/down vol " + s.updown_volume)}
    </div>
    <div class="tablewrap"><table><thead><tr><th class="l">#</th><th>Started</th><th>High</th><th>Low</th><th>Depth</th></tr></thead><tbody>${rows}</tbody></table></div>
    ${s.notes?.length ? `<ul class="list" style="margin-top:8px">${s.notes.map((n) => `<li>${ICON.dot}<span class="ink2">${esc(n)}</span></li>`).join("")}</ul>` : ""}`;
}

/* Quarterly columns (one series) with a YoY-growth row beneath, like an earnings table. */
function barChart(items, o) {
  const n = items.length;
  if (!n) return `<p class="muted">No data.</p>`;
  const W = 560, gut = 44, top = 18, plotH = 118, rowY = top + plotH + 16;
  const vals = items.map(o.value);
  const maxV = Math.max(0, ...vals), minV = Math.min(0, ...vals);
  const span = maxV - minV || 1;
  const y = (v) => top + ((maxV - v) / span) * plotH;
  const band = (W - gut) / n, bw = Math.min(24, band * 0.5), r = 4;
  const rows = o.rows || [];
  const H = rowY + 6 + rows.length * 17;
  let svg = `<svg class="fchart" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(o.title)}">`;
  svg += `<line class="base" x1="${gut}" x2="${W}" y1="${y(0)}" y2="${y(0)}"/>`;
  rows.forEach((row, k) => { svg += `<text class="lab" x="0" y="${rowY + 17 + k * 17}">${row.label}</text>`; });
  items.forEach((it, i) => {
    const v = vals[i], cx = gut + band * i + band / 2, x = cx - bw / 2;
    const y0 = y(0), y1 = y(v), h = Math.max(Math.abs(y1 - y0), 1);
    const rr = Math.min(r, h, bw / 2);
    let d;
    if (v >= 0) d = `M${x},${y0} V${y1 + rr} Q${x},${y1} ${x + rr},${y1} H${x + bw - rr} Q${x + bw},${y1} ${x + bw},${y1 + rr} V${y0} Z`;
    else d = `M${x},${y0} V${y1 - rr} Q${x},${y1} ${x + rr},${y1} H${x + bw - rr} Q${x + bw},${y1} ${x + bw},${y1 - rr} V${y0} Z`;
    svg += `<path class="bar ${v < 0 ? "neg" : ""}" d="${d}"/>`;
    if (i === n - 1) svg += `<text class="gv" x="${cx}" y="${v >= 0 ? y1 - 5 : y1 + 13}" text-anchor="middle">${o.fmtV(v)}</text>`;
    svg += `<text x="${cx}" y="${rowY}" text-anchor="middle">${esc(it.label || "")}</text>`;
    rows.forEach((row, k) => {
      const txt = row.f(it, i);
      svg += `<text class="gv ${row.bold && row.bold(it, i) ? "acc" : ""}" x="${cx}" y="${rowY + 17 + k * 17}" text-anchor="middle" style="${row.bold && !row.bold(it, i) ? "font-weight:400" : ""}">${txt}</text>`;
    });
    svg += `<rect class="hit" x="${gut + band * i}" y="0" width="${band}" height="${H}" data-tip="${esc(o.tip(it))}"/>`;
  });
  return svg + `</svg>`;
}

function fundPanel(f, r) {
  if (!f) return `<h3>Fundamentals</h3><p class="muted">Not fetched for this stock.</p>`;
  const m = f.metrics || {};
  const eps = (f.eps || []).slice(-8), rev = (f.revenue || []).slice(-8);
  const accel = (arr, i) => i > 0 && arr[i].yoy != null && arr[i - 1].yoy != null && arr[i].yoy > arr[i - 1].yoy;
  const g = (it) => (it.turnaround ? "turn" : it.yoy == null ? "–" : fmt.pct(it.yoy, 0));
  const epsChart = barChart(eps, {
    title: "Quarterly EPS", value: (e) => e.value, fmtV: (v) => "$" + v.toFixed(2),
    rows: [{ label: "YoY", f: (e) => g(e), bold: (e, i) => accel(eps, i) }],
    tip: (e) => `<b>${e.label}</b> (reported ${e.date})<br>EPS $${e.value.toFixed(2)} · YoY ${g(e)}${e.est != null ? `<br>Estimate $${e.est.toFixed(2)} · surprise ${e.surprise != null ? e.surprise.toFixed(1) + "%" : "–"}` : ""}`,
  });
  const revChart = barChart(rev, {
    title: "Quarterly revenue", value: (x) => x.value, fmtV: (v) => fmt.big(v),
    rows: [{ label: "YoY", f: (x) => g(x), bold: (x, i) => accel(rev, i) },
      { label: "Margin", f: (x) => (x.margin == null ? "–" : fmt.pct(x.margin, 0, false)) }],
    tip: (x) => `<b>Quarter ending ${x.date}</b><br>Revenue ${fmt.big(x.value)} · YoY ${g(x)}<br>Net margin ${x.margin == null ? "–" : fmt.pct(x.margin, 1, false)}`,
  });
  const badges = [
    m.pass ? chip("good", "Passes SEPA earnings/sales filter") : chip("warning", "Below SEPA earnings/sales thresholds"),
    m.code33 ? chip("good", "Code 33") : "",
    m.eps_accel_qtrs >= 2 ? chip("info", `EPS accelerating ${m.eps_accel_qtrs} qtrs`) : "",
    m.sales_accel_qtrs >= 2 ? chip("info", `Sales accelerating ${m.sales_accel_qtrs} qtrs`) : "",
  ].join(" ");
  const tbl = `<details><summary>Show as table</summary><div class="tablewrap" style="margin-top:8px"><table>
    <thead><tr><th class="l">Quarter</th><th>EPS</th><th>EPS YoY</th><th>Surprise</th></tr></thead>
    <tbody>${eps.map((e) => `<tr><td class="l">${esc(e.label)}</td><td>${e.value.toFixed(2)}</td><td>${g(e)}</td><td>${e.surprise != null ? e.surprise.toFixed(1) + "%" : "–"}</td></tr>`).join("")}</tbody></table></div>
    <div class="tablewrap" style="margin-top:8px"><table><thead><tr><th class="l">Quarter end</th><th>Revenue</th><th>YoY</th><th>Net margin</th></tr></thead>
    <tbody>${rev.map((x) => `<tr><td class="l">${esc(x.date)}</td><td>${fmt.big(x.value)}</td><td>${g(x)}</td><td>${x.margin == null ? "–" : fmt.pct(x.margin, 1, false)}</td></tr>`).join("")}</tbody></table></div></details>`;
  return `<h3>Fundamentals · score ${m.score ?? "–"}/100</h3>
    <div class="facts" style="margin-bottom:10px">${badges}</div>
    <div class="tiles num" style="margin-bottom:12px">
      ${tile("EPS, latest qtr", fmt.pct(m.eps_q_yoy, 0), "prior qtr " + fmt.pct(m.eps_q_yoy_prev, 0))}
      ${tile("Sales, latest qtr", fmt.pct(m.sales_q_yoy, 0), "prior qtr " + fmt.pct(m.sales_q_yoy_prev, 0))}
      ${tile("EPS, trailing 4Q", fmt.pct(m.eps_ttm_yoy, 0), "vs prior 4 quarters")}
      ${tile("Net margin", fmt.pct(m.margin_last, 1, false), m.margin_expanding_qtrs ? `up YoY ${m.margin_expanding_qtrs} qtrs` : "not expanding")}
      ${tile("Beat estimates", `${m.positive_surprises_4q ?? 0}/4`, m.surprise_last != null ? `last ${m.surprise_last.toFixed(1)}%` : "")}
      ${tile("Next earnings", f.next_earnings ? f.next_earnings.slice(5) : "–", r.days_to_earnings != null ? `in ${r.days_to_earnings} days` : "")}
    </div>
    <h3>Quarterly EPS <span class="muted" style="font-weight:400">· growth vs same quarter last year (bold = accelerating)</span></h3>
    ${epsChart}
    <h3 style="margin-top:14px">Quarterly revenue <span class="muted" style="font-weight:400">· with net margin</span></h3>
    ${revChart}
    ${tbl}
    <p class="muted" style="margin-top:8px;font-size:12px">EPS: ${m.eps_source === "yahoo" ? "adjusted, from Yahoo Finance" : "GAAP diluted, from SEC filings"} · revenue/margins: SEC EDGAR (Yahoo for non-US filers) · fetched ${esc((f.fetched_at || "").slice(0, 10))}</p>`;
}

// ------------------------------------------------------------------ charts
function disposeCharts() {
  charts.forEach((c) => { try { c.remove(); } catch { /* already gone */ } });
  charts = [];
}

function drawCharts(d) {
  const LW = window.LightweightCharts;
  const c = d.chart, r = d.row, p = r.plan, s = r.setup, pos = d.position;
  if (!LW) { $("#pc").innerHTML = `<div class="empty">Chart library failed to load (offline?).</div>`; return; }
  const th = {
    bg: css("--surface"), muted: css("--muted"), grid: css("--grid"), axis: css("--axis"), ink: css("--ink"), ink2: css("--ink-2"),
    s1: css("--series-1"), s2: css("--series-2"), s3: css("--series-3"), s4: css("--series-4"), crit: css("--critical"), good: css("--good"),
  };
  const base = {
    autoSize: true,
    layout: { background: { type: "solid", color: th.bg }, textColor: th.muted, fontSize: 11, fontFamily: "system-ui, -apple-system, Segoe UI, sans-serif" },
    grid: { vertLines: { visible: false }, horzLines: { color: th.grid } },
    rightPriceScale: { borderColor: th.axis },
    timeScale: { borderColor: th.axis, rightOffset: 4 },
    crosshair: { mode: LW.CrosshairMode.Normal },
  };
  const n = c.d.length;
  const chart = LW.createChart($("#pc"), { ...base, rightPriceScale: { ...base.rightPriceScale, scaleMargins: { top: 0.06, bottom: 0.26 } } });
  charts.push(chart);

  const candles = chart.addCandlestickSeries({
    upColor: th.bg, downColor: th.ink2, borderUpColor: th.ink2, borderDownColor: th.ink2,
    wickUpColor: th.ink2, wickDownColor: th.ink2, priceLineVisible: false,
  });
  candles.setData(c.d.map((t, i) => ({ time: t, open: c.o[i], high: c.h[i], low: c.l[i], close: c.c[i] })));

  const lineData = (arr) => c.d.map((t, i) => (arr[i] == null ? { time: t } : { time: t, value: arr[i] }));
  const MA = [
    ["sma50", "50-day", th.s1, true], ["sma150", "150-day", th.s2, true],
    ["sma200", "200-day", th.s3, true], ["ema21", "21-day EMA", th.s4, false],
  ];
  const series = {};
  MA.forEach(([k, , color, vis]) => {
    series[k] = chart.addLineSeries({ color, lineWidth: 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false, visible: vis });
    series[k].setData(lineData(c[k]));
  });

  // volume: neutral bars, heavy days (>= 1.4x 50-day avg) highlighted
  const vol = chart.addHistogramSeries({ priceScaleId: "vol", priceFormat: { type: "volume" }, lastValueVisible: false, priceLineVisible: false });
  chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } });
  vol.setData(c.d.map((t, i) => {
    const heavy = c.vol50[i] && c.v[i] >= 1.4 * c.vol50[i];
    const upDay = i === 0 ? c.c[i] >= c.o[i] : c.c[i] >= c.c[i - 1];
    return { time: t, value: c.v[i], color: heavy ? (upDay ? th.s1 : th.crit) : th.axis };
  }));
  const v50 = chart.addLineSeries({ priceScaleId: "vol", color: th.muted, lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
  v50.setData(lineData(c.vol50));

  // key levels
  const lvl = (price, color, title, style = LW.LineStyle.Dashed) => price && candles.createPriceLine({ price, color, lineWidth: 1, lineStyle: style, axisLabelVisible: true, title });
  if (pos) {
    lvl(pos.entry, th.s1, "Entry", LW.LineStyle.Solid);
    lvl(pos.stop, th.crit, "Stop");
  } else if (p) {
    lvl(p.entry, th.ink, "Pivot");
    lvl(p.buy_zone_hi, th.good, "Max buy");
    lvl(p.stop, th.crit, "Stop");
  }

  // contraction markers
  const inRange = new Set(c.d);
  const marks = [];
  if (s) {
    s.contractions.forEach((k, i) => {
      if (inRange.has(k.trough_date)) marks.push({ time: k.trough_date, position: "belowBar", color: th.ink2, shape: "arrowUp", text: `C${i + 1} −${Math.round(k.depth * 100)}%` });
    });
    if (inRange.has(s.pivot_date)) marks.push({ time: s.pivot_date, position: "aboveBar", color: th.ink, shape: "arrowDown", text: "Pivot" });
    if (inRange.has(s.base_start) && s.base_start !== s.pivot_date) marks.push({ time: s.base_start, position: "aboveBar", color: th.muted, shape: "circle", text: "Base high" });
  }
  if (pos?.entry_date) {
    const ed = c.d.find((t) => t >= pos.entry_date);
    if (ed) marks.push({ time: ed, position: "aboveBar", color: th.s1, shape: "arrowDown", text: "Bought" });
  }
  marks.sort((a, b) => (a.time < b.time ? -1 : a.time > b.time ? 1 : 0));
  candles.setMarkers(marks);

  // legend: OHLC readout + MA toggles + ranges
  const lg = $("#lg");
  const swatch = (k, label, color, vis) => `<button data-ma="${k}" class="${vis ? "" : "off"}" aria-pressed="${vis}"><span class="sw" style="background:${color}"></span>${label}</button>`;
  lg.innerHTML = `<span class="ohlc num" id="ohlc"></span>
    ${MA.map(([k, l, col, vis]) => swatch(k, l, col, vis)).join("")}
    <span class="seg ranges">${[["3M", 63], ["6M", 126], ["1Y", 260]].map(([l, b]) => `<button data-rg="${b}" class="${b === 126 ? "on" : ""}">${l}</button>`).join("")}</span>`;
  lg.querySelectorAll("[data-ma]").forEach((b) => b.addEventListener("click", () => {
    const on = b.classList.toggle("off") === false;
    b.setAttribute("aria-pressed", on);
    series[b.dataset.ma].applyOptions({ visible: on });
  }));
  const ohlc = $("#ohlc");
  const readout = (i) => {
    if (i == null || i < 0 || i >= n) i = n - 1;
    const chg = i > 0 ? c.c[i] / c.c[i - 1] - 1 : 0;
    ohlc.innerHTML = `${c.d[i]} O ${fmt.px(c.o[i])} H ${fmt.px(c.h[i])} L ${fmt.px(c.l[i])} C <b>${fmt.px(c.c[i])}</b> <span class="${dir(chg)}">${fmt.pct(chg)}</span> · Vol ${fmt.big(c.v[i])}${c.vol50[i] ? ` (${(c.v[i] / c.vol50[i]).toFixed(1)}× avg)` : ""}`;
  };
  readout(n - 1);
  const idx = new Map(c.d.map((t, i) => [t, i]));
  chart.subscribeCrosshairMove((pt) => readout(pt?.time ? idx.get(typeof pt.time === "string" ? pt.time : `${pt.time.year}-${String(pt.time.month).padStart(2, "0")}-${String(pt.time.day).padStart(2, "0")}`) : null));

  // RS line chart, time-synced
  let rsChart = null;
  if (c.rs) {
    rsChart = LW.createChart($("#rc"), { ...base, timeScale: { ...base.timeScale, visible: false }, rightPriceScale: { ...base.rightPriceScale, scaleMargins: { top: 0.1, bottom: 0.1 } }, handleScroll: false, handleScale: false });
    charts.push(rsChart);
    const rl = rsChart.addLineSeries({ color: th.s1, lineWidth: 2, priceLineVisible: false, lastValueVisible: false });
    rl.setData(lineData(c.rs));
    if (r.rs_new_high) rl.setMarkers([{ time: c.d[n - 1], position: "aboveBar", color: th.s1, shape: "circle", text: "New high" }]);
    chart.timeScale().subscribeVisibleLogicalRangeChange((rg) => rg && rsChart.timeScale().setVisibleLogicalRange(rg));
  } else {
    $("#rc").style.display = "none";
  }

  const setRange = (bars) => chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, n - bars), to: n + 3 });
  lg.querySelectorAll("[data-rg]").forEach((b) => b.addEventListener("click", () => {
    lg.querySelectorAll("[data-rg]").forEach((x) => x.classList.toggle("on", x === b));
    setRange(+b.dataset.rg);
  }));
  setRange(126);
}

// ------------------------------------------------------------------ router
function route(keepScroll = false) {
  if (!S) return;
  const y = scrollY;
  disposeCharts();
  const h = location.hash || "#/";
  const key = h.startsWith("#/t/") ? "" : (h === "#/portfolio" || h === "#/positions") ? "portfolio" : h === "#/all" ? "all" : h === "#/rules" ? "rules" : "home";
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.r === key));
  if (h.startsWith("#/t/")) viewDetail(decodeURIComponent(h.slice(4)).toUpperCase());
  else if (key === "portfolio") viewPortfolio();
  else if (key === "all") viewAll();
  else if (key === "rules") viewRules();
  else viewHome();
  if (keepScroll) scrollTo(0, y);
}
window.addEventListener("hashchange", () => { route(); scrollTo(0, 0); });
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => route(true));

async function boot() {
  try {
    S = await getJSON("data/summary.json");
  } catch (e) {
    app.innerHTML = `<div class="panel empty">No scan results yet. Run <code>python -m minervini scan</code> first.<br><span class="muted">${esc(e.message)}</span></div>`;
    return;
  }
  renderHeader();
  route();
  // pick up new 15-minute refreshes automatically (detail pages keep their zoom until you navigate)
  setInterval(async () => {
    try {
      const n = await getJSON("data/summary.json");
      if (n.generated_at !== S.generated_at) {
        S = n;
        details.clear();
        renderHeader();
        if (!location.hash.startsWith("#/t/")) route(true);
      }
    } catch { /* offline - try again next minute */ }
  }, 60000);
}
boot();
