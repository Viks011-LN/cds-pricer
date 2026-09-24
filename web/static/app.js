"use strict";

// ---------------------------------------------------------------------------
// Formatting helpers. All numbers shown come from the engine via the API;
// the browser only formats and (for curve trades) displays the server's net.
// ---------------------------------------------------------------------------

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const todayIso = () => {
  const d = new Date();
  return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
};

/** "10,000,000", "10mm", "7.5m", "250k" -> number (NaN if unparseable). */
function parseNum(raw) {
  const s = String(raw ?? "").trim().toLowerCase().replace(/[,\s_]/g, "");
  const m = s.match(/^(-?\d*\.?\d+)(mm|m|k|bn|b)?$/);
  if (!m) return NaN;
  const mult = { mm: 1e6, m: 1e6, k: 1e3, bn: 1e9, b: 1e9 }[m[2]] || 1;
  return parseFloat(m[1]) * mult;
}

const nf = (d) => new Intl.NumberFormat("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
const f0 = nf(0), f2 = nf(2), f4 = nf(4);
const money = (v, ccy) => `${ccy} ${f2.format(Math.abs(v))}`;
const signed = (v, fmt = f2) => (v < 0 ? "−" : v > 0 ? "+" : "") + fmt.format(Math.abs(v));
const compact = (v) => {
  const a = Math.abs(v);
  if (a >= 1e9) return `${+(a / 1e9).toFixed(2)}bn`;
  if (a >= 1e6) return `${+(a / 1e6).toFixed(2)}mm`;
  if (a >= 1e3) return `${+(a / 1e3).toFixed(1)}k`;
  return f0.format(a);
};

// Direction words, so a sign never has to be interpreted by the reader.
const DIR = {
  cash: (v) => (v > 0 ? ["Pay", "bad"] : v < 0 ? ["Receive", "good"] : ["Nil", "flat"]),
  gain: (v) => (v > 0 ? ["Gain", "good"] : v < 0 ? ["Loss", "bad"] : ["Flat", "flat"]),
  cs01: (v) => (v > 0 ? ["Gain if wider", "good"] : v < 0 ? ["Loss if wider", "bad"] : ["Flat", "flat"]),
};
const dirTag = (kind, v) => {
  const [w, cls] = DIR[kind](Math.round(v * 100) / 100);
  return `<span class="dir ${cls}">${w}</span>`;
};
const numCell = (v) => `<td class="num ${v < 0 ? "neg-num" : ""}">${signed(v)}</td>`;

// ---------------------------------------------------------------------------
// Tabs
// ---------------------------------------------------------------------------

$$(".tab").forEach((tab) =>
  tab.addEventListener("click", () => {
    $$(".tab").forEach((t) => {
      t.classList.toggle("active", t === tab);
      t.setAttribute("aria-selected", t === tab);
    });
    $$(".view").forEach((v) => (v.hidden = v.id !== `view-${tab.dataset.view}`));
    try { localStorage.setItem("cds-tab", tab.dataset.view); } catch (_) { /* storage unavailable */ }
  }),
);
try {
  const saved = localStorage.getItem("cds-tab");
  if (saved) $(`.tab[data-view="${saved}"]`)?.click();
} catch (_) { /* storage unavailable */ }

// ---------------------------------------------------------------------------
// Reusable editors
// ---------------------------------------------------------------------------

/** Tenor/value row editor. Returns { get(): [{tenor, value}], set(rows) }. */
function curveEditor(root, rows, valueLabel) {
  root.innerHTML = "";
  const list = document.createElement("div");
  list.className = "curve-editor";
  const add = document.createElement("button");
  add.type = "button";
  add.className = "ghost";
  add.textContent = "+ Add point";
  root.append(list, add);

  const addRow = (tenor = "", value = "") => {
    const row = document.createElement("div");
    row.className = "curve-row";
    row.innerHTML = `
      <input aria-label="Tenor" placeholder="Tenor e.g. 5Y" value="${esc(tenor)}">
      <input aria-label="${esc(valueLabel)}" inputmode="decimal" placeholder="${esc(valueLabel)}" value="${esc(value)}">
      <button type="button" class="icon-btn" aria-label="Remove point">×</button>`;
    row.querySelector("button").onclick = () => list.children.length > 1 && row.remove();
    list.append(row);
  };
  add.onclick = () => addRow();
  const set = (rs) => { list.innerHTML = ""; rs.forEach(([t, v]) => addRow(t, v)); };
  set(rows);
  return {
    set,
    get() {
      return $$(".curve-row", list).map((r) => {
        const [t, v] = $$("input", r);
        const value = parseNum(v.value);
        v.classList.toggle("invalid", !Number.isFinite(value));
        t.classList.toggle("invalid", !/^\d+(\.\d+)?\s*[MY]$/i.test(t.value.trim()));
        return { tenor: t.value.trim().toUpperCase(), value };
      });
    },
  };
}

/** Flat-rate / zero-curve discount input bound to a .disc container. */
function discountEditor(scope) {
  const root = $(`.disc[data-scope="${scope}"]`);
  root.append($("#tpl-disc").content.cloneNode(true));
  $$(".seg input", root).forEach((inp, i) => {
    inp.name = `disc-${scope}`;
    inp.id = `disc-${scope}-${i}`;
    inp.nextElementSibling.htmlFor = inp.id;
    inp.onchange = () => {
      $(".disc-flat", root).hidden = inp.value !== "flat";
      $(".disc-curve", root).hidden = inp.value !== "curve";
    };
  });
  const curve = curveEditor($(".disc-curve .curve-editor", root),
    [["1Y", "3.50"], ["2Y", "3.45"], ["5Y", "3.50"], ["10Y", "3.70"]], "Zero %");
  return () => {
    const mode = $(".seg input:checked", root).value;
    if (mode === "flat") {
      const inp = $("input[name=flat_rate]", root);
      const r = parseNum(inp.value);
      inp.classList.toggle("invalid", !Number.isFinite(r));
      return { mode, flat_rate: r };
    }
    return { mode, curve: curve.get() };
  };
}

// ---------------------------------------------------------------------------
// API
// ---------------------------------------------------------------------------

async function post(url, body) {
  const res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  let data;
  try { data = await res.json(); } catch (_) { throw new Error(`Server error (${res.status})`); }
  if (!res.ok) throw new Error(data.error || `Server error (${res.status})`);
  return data;
}

function bindSubmit(form, out, build, render) {
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = $("button.primary", form);
    let payload;
    try {
      payload = build();
    } catch (err) {
      out.innerHTML = `<div class="error">${esc(err.message)}</div>`;
      return;
    }
    btn.disabled = true;
    const label = btn.textContent;
    btn.textContent = "Pricing…";
    try {
      render(await post(...payload));
    } catch (err) {
      out.innerHTML = `<div class="error"><strong>Could not price:</strong> ${esc(err.message)}</div>`;
    } finally {
      btn.disabled = false;
      btn.textContent = label;
    }
  });
}

function num(form, name, label) {
  const inp = form.elements[name];
  const v = parseNum(inp.value);
  inp.classList.toggle("invalid", !Number.isFinite(v));
  if (!Number.isFinite(v)) throw new Error(`${label} is not a number.`);
  return v;
}

// ---------------------------------------------------------------------------
// Charts (inline SVG, single series each — no legend needed)
// ---------------------------------------------------------------------------

const tip = $("#tooltip");
function bindTips(svg) {
  $$("[data-tip]", svg).forEach((el) => {
    el.addEventListener("mousemove", (e) => {
      tip.innerHTML = el.dataset.tip;
      tip.hidden = false;
      const x = Math.min(e.clientX + 14, window.innerWidth - tip.offsetWidth - 8);
      tip.style.left = `${x}px`;
      tip.style.top = `${e.clientY - tip.offsetHeight - 10}px`;
    });
    el.addEventListener("mouseleave", () => (tip.hidden = true));
  });
}

/** Signed bar chart (e.g. CS01 by tenor). Positive and negative bars get the two poles. */
function barChart(items, ccy) {
  const W = 520, H = 230, L = 12, R = 12, T = 22, B = 44;
  const vals = items.map((d) => d.v);
  const hi = Math.max(0, ...vals), lo = Math.min(0, ...vals);
  const span = hi - lo || 1;
  const y = (v) => T + ((hi - v) / span) * (H - T - B);
  const band = (W - L - R) / items.length;
  const bw = Math.min(46, band * 0.56);
  const y0 = y(0);
  let g = `<line class="axis" x1="${L}" x2="${W - R}" y1="${y0}" y2="${y0}"/>`;
  items.forEach((d, i) => {
    const cx = L + band * i + band / 2;
    const top = Math.min(y(d.v), y0), h = Math.max(1, Math.abs(y(d.v) - y0));
    const fill = d.v >= 0 ? "var(--series-1)" : "var(--series-2)";
    const labelY = d.v >= 0 ? top - 6 : top + h + 13;
    g += `<rect class="bar" x="${cx - bw / 2}" y="${top}" width="${bw}" height="${h}" rx="3" fill="${fill}"/>`;
    g += `<text class="val" x="${cx}" y="${labelY}" text-anchor="middle">${signed(d.v, f0)}</text>`;
    g += `<text x="${cx}" y="${H - 8}" text-anchor="middle">${esc(d.label)}</text>`;
    g += `<rect class="hit" x="${cx - band / 2}" y="${T - 16}" width="${band}" height="${H - T - B + 32}"
            data-tip="${esc(d.label)} · CS01 <b>${esc(ccy)} ${signed(d.v)}</b>"/>`;
  });
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="CS01 by tenor">${g}</svg>`;
}

/** Survival probability line from t=0 through each bootstrapped pillar. */
function survivalChart(nodes) {
  const W = 520, H = 220, L = 40, R = 16, T = 14, B = 28;
  const pts = [{ t: 0, s: 100, tenor: "0" }, ...nodes.map((n) => ({ t: n.t, s: n.survival_pct, tenor: n.tenor, h: n.hazard_pct }))];
  const tMax = Math.max(...pts.map((p) => p.t)) || 1;
  const sMin = Math.floor(Math.min(...pts.map((p) => p.s)) / 5) * 5;
  const lo = Math.min(sMin, 95);
  const x = (t) => L + (t / tMax) * (W - L - R);
  const y = (s) => T + ((100 - s) / (100 - lo)) * (H - T - B);
  let g = "";
  const step = (100 - lo) / 4;
  for (let k = 0; k <= 4; k++) {
    const v = lo + step * k;
    g += `<line class="grid" x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}"/>`;
    g += `<text x="${L - 6}" y="${y(v) + 4}" text-anchor="end">${+v.toFixed(1)}%</text>`;
  }
  g += `<polyline fill="none" stroke="var(--series-1)" stroke-width="2" stroke-linejoin="round"
          points="${pts.map((p) => `${x(p.t)},${y(p.s)}`).join(" ")}"/>`;
  pts.slice(1).forEach((p) => {
    g += `<circle cx="${x(p.t)}" cy="${y(p.s)}" r="4" fill="var(--series-1)" stroke="var(--surface)" stroke-width="2"/>`;
    g += `<text x="${x(p.t)}" y="${H - 8}" text-anchor="middle">${esc(p.tenor)}</text>`;
    g += `<circle class="hit" cx="${x(p.t)}" cy="${y(p.s)}" r="14"
            data-tip="${esc(p.tenor)} · survival <b>${f2.format(p.s)}%</b> · fwd hazard <b>${f4.format(p.h)}%</b>"/>`;
  });
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="Survival probability by tenor">${g}</svg>`;
}

// ---------------------------------------------------------------------------
// Single trade
// ---------------------------------------------------------------------------

const sForm = $("#single-form");
const sOut = $("#single-results");
sForm.elements.trade_date.value = todayIso();
const sDisc = discountEditor("single");
sForm.elements.tenor.addEventListener("change", (e) => {
  $(".maturity-field", sForm).hidden = e.target.value !== "";
});
bindSubmit(sForm, sOut, () => {
  const f = sForm.elements;
  const body = {
    trade_date: f.trade_date.value,
    tenor: f.tenor.value || null,
    maturity_date: f.tenor.value ? null : f.maturity_date.value,
    notional: num(sForm, "notional", "Notional"),
    traded_spread_bps: num(sForm, "traded_spread_bps", "Traded spread"),
    coupon_bps: Number(f.coupon_bps.value),
    recovery_pct: num(sForm, "recovery_pct", "Recovery"),
    buy_protection: $("input[name=side]:checked", sForm).value === "buy",
    discount: sDisc(),
  };
  sForm._ctx = { ccy: f.currency.value, body };
  return ["/api/price", body];
}, (data) => renderSingle(data, sForm._ctx));

function renderSingle({ result: r, curve_used }, { ccy, body }) {
  const side = body.buy_protection ? "buy" : "sell";
  const cs01Rows = r.cs01_per_tenor.map((c) => ({ label: c.tenor, v: c.cs01 }));
  const curveTxt = curve_used.length === 1
    ? `${f2.format(curve_used[0].value)}bp flat`
    : curve_used.map((p) => `${p.tenor} ${p.value}`).join(" / ");
  const d = body.discount;
  const discTxt = d.mode === "flat" ? `${d.flat_rate}% flat` : d.curve.map((p) => `${p.tenor} ${p.value}%`).join(" / ");

  sOut.innerHTML = `
    <div class="panel summary">
      <div>
        <div class="title"><span class="badge ${side}">${side === "buy" ? "BUY PROTECTION" : "SELL PROTECTION"}</span>
          &nbsp;${ccy} ${compact(body.notional)} ${esc(body.tenor || "")} · matures ${esc(r.maturity_date)}</div>
        <div class="meta">Coupon ${body.coupon_bps}bp · recovery ${body.recovery_pct}% · credit ${esc(curveTxt)} · rates ${esc(discTxt)} · trade date ${esc(body.trade_date)}</div>
      </div>
    </div>

    <div class="kpis">
      <div class="panel kpi"><div class="label">Upfront (clean, cash)</div>
        <div class="value">${money(r.upfront_amount, ccy)}${dirTag("cash", r.upfront_amount)}</div>
        <div class="foot">${f4.format(Math.abs(r.upfront_pct))}% of notional · price ${f4.format(r.clean_price)}</div></div>
      <div class="panel kpi"><div class="label">Par spread</div>
        <div class="value">${f2.format(r.par_spread_bps)} bp</div>
        <div class="foot">traded ${f2.format(body.traded_spread_bps)}bp · coupon ${body.coupon_bps}bp</div></div>
      <div class="panel kpi"><div class="label">CS01 (per +1bp parallel)</div>
        <div class="value">${money(r.cs01_total, ccy)}${dirTag("cs01", r.cs01_total)}</div>
        <div class="foot">bump all pillars +1bp, re-bootstrap</div></div>
      <div class="panel kpi"><div class="label">RPV01 (clean)</div>
        <div class="value">${f4.format(r.clean_rpv01)}</div>
        <div class="foot">dirty ${f4.format(r.rpv01)} · years</div></div>
    </div>

    <div class="cards">
      <div class="panel card"><h3>Pricing</h3>
        <table class="kv">
          <tr><td>Upfront clean</td><td>${money(r.upfront_amount, ccy)} ${dirTag("cash", r.upfront_amount)}</td></tr>
          <tr><td>Upfront dirty</td><td>${money(r.dirty_upfront_amount, ccy)} ${dirTag("cash", r.dirty_upfront_amount)}</td></tr>
          <tr><td>Accrued (${r.accrued_days} days)</td><td>${money(r.accrued_amount, ccy)} ${dirTag("cash", r.accrued_amount)}</td></tr>
          <tr><td>Clean / dirty price</td><td>${f4.format(r.clean_price)} / ${f4.format(r.dirty_price)}</td></tr>
          <tr><td>Par spread</td><td>${f2.format(r.par_spread_bps)} bp</td></tr>
          <tr><td>RPV01 clean / dirty</td><td>${f4.format(r.clean_rpv01)} / ${f4.format(r.rpv01)}</td></tr>
          <tr><td>Protection leg PV</td><td>${f4.format(r.protection_leg_pct)}%</td></tr>
          <tr><td>Premium leg PV</td><td>${f4.format(r.premium_leg_pct)}%</td></tr>
          <tr><td>Survival to maturity</td><td>${f2.format(r.survival_to_maturity)}%</td></tr>
        </table>
      </div>

      <div class="panel card"><h3>Carry &amp; rolldown <span class="muted">· static curves, no default</span></h3>
        <table class="kv">
          <tr><td>Carry, 1 day</td><td>${money(r.carry_daily, ccy)} ${dirTag("gain", r.carry_daily)}</td></tr>
          <tr><td>Carry, 30 days</td><td>${money(r.carry_monthly, ccy)} ${dirTag("gain", r.carry_monthly)}</td></tr>
          <tr><td>Rolldown, 1 day</td><td>${money(r.rolldown_1d, ccy)} ${dirTag("gain", r.rolldown_1d)}</td></tr>
          <tr><td>Rolldown, 1 week</td><td>${money(r.rolldown_1w, ccy)} ${dirTag("gain", r.rolldown_1w)}</td></tr>
          <tr><td>Rolldown, 1 month</td><td>${money(r.rolldown_1m, ccy)} ${dirTag("gain", r.rolldown_1m)}</td></tr>
          <tr><td>Carry + roll, 1 day</td><td>${money(r.carry_daily + r.rolldown_1d, ccy)} ${dirTag("gain", r.carry_daily + r.rolldown_1d)}</td></tr>
          <tr><td>Carry + roll, 1 month</td><td>${money(r.carry_monthly + r.rolldown_1m, ccy)} ${dirTag("gain", r.carry_monthly + r.rolldown_1m)}</td></tr>
        </table>
        <p class="fineprint">${esc(r.carry_note)} Rolldown re-anchors both curves at the horizon (same tenor quotes) and reprices; carry excluded.</p>
      </div>

      <div class="panel card"><h3>CS01 by tenor <span class="muted">· ${ccy} per +1bp on each pillar</span></h3>
        ${barChart(cs01Rows, ccy)}
        <table class="data"><thead><tr><th>Tenor</th><th class="num">Spread bp</th><th class="num">CS01</th></tr></thead><tbody>
          ${r.cs01_per_tenor.map((c) => `<tr><td>${esc(c.tenor)}</td><td class="num">${f2.format(c.spread_bps)}</td>${numCell(c.cs01)}</tr>`).join("")}
        </tbody></table>
      </div>

      <div class="panel card"><h3>Credit curve <span class="muted">· bootstrapped survival</span></h3>
        ${survivalChart(r.hazard_nodes)}
        <table class="data"><thead><tr><th>Pillar</th><th class="num">t (yrs)</th><th class="num">Fwd hazard</th><th class="num">Survival</th></tr></thead><tbody>
          ${r.hazard_nodes.map((n) => `<tr><td>${esc(n.tenor)}</td><td class="num">${f4.format(n.t)}</td><td class="num">${f4.format(n.hazard_pct)}%</td><td class="num">${f2.format(n.survival_pct)}%</td></tr>`).join("")}
        </tbody></table>
      </div>
    </div>`;
  $$("svg.chart", sOut).forEach(bindTips);
}

// ---------------------------------------------------------------------------
// Curve trade
// ---------------------------------------------------------------------------

const cForm = $("#curve-form");
const cOut = $("#curve-results");
cForm.elements.trade_date.value = todayIso();
const cDisc = discountEditor("curve");
const cCredit = curveEditor($("#curve-credit"),
  [["1Y", "80"], ["3Y", "120"], ["5Y", "160"], ["7Y", "180"], ["10Y", "200"]], "Spread bp");

const legsRoot = $("#legs");
let legSeq = 0;
function addLeg({ side = "buy", notional = "10,000,000", tenor = "5Y", coupon = "100" } = {}) {
  const id = ++legSeq;
  const el = document.createElement("div");
  el.className = "leg";
  el.innerHTML = `
    <div class="leg-head"><span class="leg-title"></span></div>
    <div class="seg small" role="radiogroup" aria-label="Side">
      <input type="radio" name="leg${id}-side" id="leg${id}-b" value="buy" ${side === "buy" ? "checked" : ""}><label for="leg${id}-b">Buy protection</label>
      <input type="radio" name="leg${id}-side" id="leg${id}-s" value="sell" ${side === "sell" ? "checked" : ""}><label for="leg${id}-s">Sell protection</label>
    </div>
    <div class="grid3">
      <label>Notional<input class="l-notional" inputmode="decimal" value="${esc(notional)}"></label>
      <label>Tenor<input class="l-tenor" value="${esc(tenor)}"></label>
      <label>Coupon bp<input class="l-coupon" inputmode="decimal" value="${esc(coupon)}"></label>
    </div>`;
  legsRoot.append(el);
  renumber();
}
function renumber() { $$(".leg", legsRoot).forEach((l, i) => ($(".leg-title", l).textContent = `Leg ${i + 1}`)); }
function setLegs(legs) { legsRoot.innerHTML = ""; legs.forEach(addLeg); }

// Notionals chosen so the trades are roughly CS01-neutral on the default curve.
const PRESETS = {
  steepener: [{ side: "sell", notional: "10,000,000", tenor: "5Y" }, { side: "buy", notional: "6,400,000", tenor: "10Y" }],
  flattener: [{ side: "buy", notional: "10,000,000", tenor: "5Y" }, { side: "sell", notional: "6,400,000", tenor: "10Y" }],
};
$$("[data-preset]").forEach((b) => (b.onclick = () => setLegs(PRESETS[b.dataset.preset])));
setLegs(PRESETS.steepener);

bindSubmit(cForm, cOut, () => {
  const legs = $$(".leg", legsRoot).map((l, i) => {
    const read = (cls, label) => {
      const inp = $(cls, l);
      const v = parseNum(inp.value);
      inp.classList.toggle("invalid", !Number.isFinite(v));
      if (!Number.isFinite(v)) throw new Error(`Leg ${i + 1}: ${label} is not a number.`);
      return v;
    };
    return {
      buy_protection: $("input:checked", l).value === "buy",
      notional: read(".l-notional", "notional"),
      tenor: $(".l-tenor", l).value.trim().toUpperCase(),
      coupon_bps: read(".l-coupon", "coupon"),
    };
  });
  const body = {
    trade_date: cForm.elements.trade_date.value,
    recovery_pct: num(cForm, "recovery_pct", "Recovery"),
    credit_curve: cCredit.get(),
    discount: cDisc(),
    legs,
  };
  cForm._ctx = { ccy: cForm.elements.currency.value, body };
  return ["/api/curve-trade", body];
}, (data) => renderCurve(data, cForm._ctx));

/** One block of risk figures — used for every leg and, with the same layout, for the net. */
function riskCard(title, sub, x, ccy, isNet) {
  const row = (label, v, kind) => `<tr><td>${label}</td><td>${money(v, ccy)} ${dirTag(kind, v)}</td></tr>`;
  return `<div class="panel card leg-card${isNet ? " net-card" : ""}">
    <h3>${title}</h3>
    <div class="leg-sub">${sub}</div>
    <table class="kv">
      ${row("Upfront (clean, cash)", x.upfront_clean_amount, "cash")}
      ${row("CS01, per +1bp parallel", x.cs01_total_per_1bp, "cs01")}
      ${row("Carry, 1 day", x.carry_daily, "gain")}
      ${row("Carry, 30 days", x.carry_monthly_30d, "gain")}
      ${row("Rolldown, 1 day", x.rolldown_1d, "gain")}
      ${row("Rolldown, 1 week", x.rolldown_1w, "gain")}
      ${row("Rolldown, 1 month", x.rolldown_1m, "gain")}
      ${row("Carry + roll, 1 day", x.carry_plus_roll_1d, "gain")}
      ${row("Carry + roll, 1 month", x.carry_plus_roll_1m, "gain")}
    </table>
    <div class="leg-sub" style="margin-top:12px">CS01 by tenor (${ccy} per +1bp)</div>
    <table class="data compact"><tbody>
      ${x.cs01_per_tenor.map((c) => `<tr><td>${esc(c.tenor)}</td>${numCell(c.cs01)}</tr>`).join("")}
    </tbody></table>
  </div>`;
}

function renderCurve({ per_leg, net, inputs_echo }, { ccy, body }) {
  const d = body.discount;
  const discTxt = d.mode === "flat" ? `${d.flat_rate}% flat` : d.curve.map((p) => `${p.tenor} ${p.value}%`).join(" / ");
  const curveTxt = inputs_echo.credit_curve_bps.map((p) => `${p.tenor} ${p.spread_bps}`).join(" / ");
  const tenors = net.cs01_per_tenor.map((c) => c.tenor);
  const legName = (l) => `${l.side.startsWith("BUY") ? "Buy" : "Sell"} ${compact(inputs_echo.legs[l.leg - 1].notional)} ${l.tenor || l.maturity_date}`;

  const legRows = per_leg.map((l) => {
    const e = inputs_echo.legs[l.leg - 1];
    return `<tr>
      <td>${l.leg}</td>
      <td><span class="badge ${l.side.startsWith("BUY") ? "buy" : "sell"}">${l.side.startsWith("BUY") ? "BUY" : "SELL"}</span></td>
      <td class="num">${f0.format(e.notional)}</td><td>${esc(l.tenor || "")}</td><td>${esc(l.maturity_date)}</td>
      <td class="num">${f0.format(e.coupon_bps)}</td><td class="num">${f2.format(l.par_spread_bps)}</td>
      ${numCell(l.upfront_clean_amount)}${numCell(l.cs01_total_per_1bp)}
      ${numCell(l.carry_daily)}${numCell(l.carry_monthly_30d)}
      ${numCell(l.rolldown_1d)}${numCell(l.rolldown_1w)}${numCell(l.rolldown_1m)}
      <td class="num">${f4.format(l.rpv01_clean_years)}</td>
    </tr>`;
  }).join("");

  const bucketRows = per_leg.map((l) => {
    const by = Object.fromEntries(l.cs01_per_tenor.map((c) => [c.tenor, c.cs01]));
    return `<tr><td>Leg ${l.leg} · ${esc(legName(l))}</td>${tenors.map((t) => numCell(by[t] ?? 0)).join("")}${numCell(l.cs01_total_per_1bp)}</tr>`;
  }).join("");

  cOut.innerHTML = `
    <div class="panel summary">
      <div>
        <div class="title">${per_leg.length}-leg curve trade · ${per_leg.map((l) => esc(legName(l))).join(" / ")}</div>
        <div class="meta">Credit ${esc(curveTxt)} bp · recovery ${body.recovery_pct}% · rates ${esc(discTxt)} · trade date ${esc(body.trade_date)} · amounts in ${ccy}</div>
      </div>
    </div>

    <div class="kpis">
      <div class="panel kpi"><div class="label">Net upfront (clean, cash)</div>
        <div class="value">${money(net.upfront_clean_amount, ccy)}${dirTag("cash", net.upfront_clean_amount)}</div>
        <div class="foot">dirty ${money(net.upfront_dirty_amount, ccy)}</div></div>
      <div class="panel kpi"><div class="label">Net CS01 (per +1bp parallel)</div>
        <div class="value">${money(net.cs01_total_per_1bp, ccy)}${dirTag("cs01", net.cs01_total_per_1bp)}</div>
        <div class="foot">sum of leg CS01s</div></div>
      <div class="panel kpi"><div class="label">Net carry (30 days)</div>
        <div class="value">${money(net.carry_monthly_30d, ccy)}${dirTag("gain", net.carry_monthly_30d)}</div>
        <div class="foot">${money(net.carry_daily, ccy)} per day</div></div>
      <div class="panel kpi"><div class="label">Net rolldown (1 month)</div>
        <div class="value">${money(net.rolldown_1m, ccy)}${dirTag("gain", net.rolldown_1m)}</div>
        <div class="foot">carry + roll 1m ${money(net.carry_plus_roll_1m, ccy)}</div></div>
    </div>

    <h2 class="section">Per leg and net <span class="muted">· each leg priced on its own on the shared curve, as the CLI prints it; net = sum of the legs</span></h2>
    <div class="leg-grid">
      ${per_leg.map((l) => riskCard(`Leg ${l.leg}`, esc(l.display_heading.replace(/^Leg \d+: /, "")), l, ccy, false)).join("")}
      ${riskCard("Net", `Sum of ${per_leg.length} legs`, net, ccy, true)}
    </div>

    <div class="panel card"><h3>Side by side <span class="muted">· ${ccy}, signed (+ pay / gain)</span></h3>
      <div class="scroll-x"><table class="data">
        <thead><tr><th>#</th><th>Side</th><th class="num">Notional</th><th>Tenor</th><th>Maturity</th><th class="num">Cpn</th>
          <th class="num">Par bp</th><th class="num">Upfront</th><th class="num">CS01</th><th class="num">Carry 1d</th><th class="num">Carry 30d</th>
          <th class="num">Roll 1d</th><th class="num">Roll 1w</th><th class="num">Roll 1m</th><th class="num">RPV01</th></tr></thead>
        <tbody>${legRows}
          <tr class="net"><td colspan="7">Net</td>${numCell(net.upfront_clean_amount)}${numCell(net.cs01_total_per_1bp)}
            ${numCell(net.carry_daily)}${numCell(net.carry_monthly_30d)}${numCell(net.rolldown_1d)}${numCell(net.rolldown_1w)}${numCell(net.rolldown_1m)}<td></td></tr>
        </tbody>
      </table></div>
      <p class="fineprint">Upfront: + = cash this side pays. CS01: + = gains if spreads widen 1bp. Carry / rolldown: + = gain. Net figures are simple sums of the legs.</p>
    </div>

    <div class="panel card"><h3>Net CS01 by tenor <span class="muted">· ${ccy} per +1bp on each pillar</span></h3>
      ${barChart(net.cs01_per_tenor.map((c) => ({ label: c.tenor, v: c.cs01 })), ccy)}
    </div>

    <div class="panel card"><h3>CS01 bucket matrix <span class="muted">· ${ccy} per +1bp</span></h3>
      <div class="scroll-x"><table class="data">
        <thead><tr><th>Leg</th>${tenors.map((t) => `<th class="num">${esc(t)}</th>`).join("")}<th class="num">Total</th></tr></thead>
        <tbody>${bucketRows}
          <tr class="net"><td>Net</td>${net.cs01_per_tenor.map((c) => numCell(c.cs01)).join("")}${numCell(net.cs01_total_per_1bp)}</tr>
        </tbody>
      </table></div>
    </div>`;
  $$("svg.chart", cOut).forEach(bindTips);
}
