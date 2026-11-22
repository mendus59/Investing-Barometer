(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const isNum = (x) => typeof x === "number" && isFinite(x);
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) {} },
  };

  const fmtNum = (x, nd = 2) => (isNum(x) ? x.toLocaleString(undefined, { minimumFractionDigits: nd, maximumFractionDigits: nd }) : "n/a");
  const fmtPct = (x, nd = 2) => (isNum(x) ? fmtNum(x, nd) + "%" : "n/a");
  const fmtMoney = (x) => (isNum(x) ? "$" + Math.round(x).toLocaleString() : "n/a");
  const fmtCompactMoney = (x) => (x >= 1e6 ? "$" + (x / 1e6).toFixed(1) + "M" : x >= 1e3 ? "$" + Math.round(x / 1e3) + "k" : "$" + Math.round(x));
  const signed = (x, nd = 2, suffix = "") => {
    if (!isNum(x)) return '<span class="na">n/a</span>';
    const cls = x > 0 ? "up" : x < 0 ? "down" : "";
    return `<span class="${cls}">${x > 0 ? "+" : ""}${fmtNum(x, nd)}${suffix}</span>`;
  };
  const fmtDate = (s) => {
    if (!s) return "n/a";
    const d = new Date(s.length === 7 ? s + "-01T00:00:00" : s + (s.length === 10 ? "T00:00:00" : ""));
    return s.length === 7
      ? d.toLocaleDateString(undefined, { year: "numeric", month: "short" })
      : d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
  };

  function actionClass(a) {
    if (a === "Buy" || a === "Overweight") return "act-buy";
    if (a === "Sell" || a === "Underweight") return "act-sell";
    if (a === "Hold") return "act-hold";
    return "act-none";
  }

  // ------------------------------------------------------------------ tooltip
  const tip = $("tooltip");
  function showTip(html, x, y) {
    tip.innerHTML = html;
    tip.hidden = false;
    const r = tip.getBoundingClientRect();
    let left = x + 14, top = y + 14;
    if (left + r.width > window.innerWidth - 8) left = x - r.width - 14;
    if (top + r.height > window.innerHeight - 8) top = y - r.height - 14;
    tip.style.left = Math.max(8, left) + "px";
    tip.style.top = Math.max(8, top) + "px";
  }
  const hideTip = () => { tip.hidden = true; };

  function spark(points, w = 120, h = 32) {
    if (!points || points.length < 2) return "";
    const ys = points.map((p) => p[1]);
    const min = Math.min(...ys), max = Math.max(...ys);
    const span = max - min || 1;
    const d = points
      .map((p, i) => `${i ? "L" : "M"}${((i / (points.length - 1)) * w).toFixed(1)},${(h - 2 - ((p[1] - min) / span) * (h - 4)).toFixed(1)}`)
      .join("");
    const label = `Trend from ${fmtNum(ys[0])} to ${fmtNum(ys[ys.length - 1])}`;
    return `<svg class="spark" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" role="img" aria-label="${esc(label)}"><path d="${d}"/></svg>`;
  }

  function factorList(factors) {
    return factors
      .map((f) => {
        const cls = f.score === null ? "na" : f.score > 0 ? "pos" : f.score < 0 ? "neg" : "zero";
        const pt = f.score === null ? "–" : f.score > 0 ? "+1" : f.score < 0 ? "−1" : "0";
        return `<li><span class="pt ${cls}">${pt}</span><span><span class="fname">${esc(f.name)}.</span> ${esc(f.detail)}</span></li>`;
      })
      .join("");
  }

  function headlineValue(hm) {
    if (!hm || !isNum(hm.value)) return "n/a";
    if (hm.unit === "%") return fmtPct(hm.value);
    if (hm.unit === "$") return hm.value >= 1000 ? fmtMoney(hm.value) : "$" + fmtNum(hm.value);
    if (hm.unit) return fmtNum(hm.value) + hm.unit;
    return fmtNum(hm.value, hm.value >= 1000 ? 0 : 1);
  }

  // ------------------------------------------------------------------ signals
  function renderSignals(latest) {
    $("signals").innerHTML = latest.categories
      .map((k) => {
        const s = latest.signals[k];
        const hm = s.headline_metric || {};
        return `<article class="card">
          <h3>${esc(s.title)}</h3>
          <p class="subject">${esc(s.subject)}</p>
          <span class="badge ${actionClass(s.action)}">${esc(s.action)}</span>
          <div class="score">Score ${s.score > 0 ? "+" : ""}${s.score} across ${s.max_score} factors</div>
          <div class="headline">${esc(hm.label)}: <b>${headlineValue(hm)}</b> <span class="asof">(${fmtDate(hm.as_of)})</span></div>
          <details><summary>Why?</summary><ul class="factors">${factorList(s.factors)}</ul></details>
        </article>`;
      })
      .join("");
  }

  // ------------------------------------------------------------------ profiles + allocation
  let state = { profile: "moderate" };
  let LATEST, BACKTEST;

  function renderProfilePicker() {
    const profiles = LATEST.profiles;
    const saved = store.get("profile");
    if (saved && profiles[saved]) state.profile = saved;
    $("profiles").innerHTML = Object.entries(profiles)
      .map(([k, p]) => `<button type="button" role="radio" data-profile="${k}" aria-checked="${k === state.profile}">${esc(p.label)}</button>`)
      .join("");
    $("profiles").addEventListener("click", (e) => {
      const b = e.target.closest("button[data-profile]");
      if (!b) return;
      state.profile = b.dataset.profile;
      store.set("profile", state.profile);
      renderProfileDependent();
    });
  }

  function renderProfileDependent() {
    document.querySelectorAll("#profiles button").forEach((b) => b.setAttribute("aria-checked", String(b.dataset.profile === state.profile)));
    const p = LATEST.profiles[state.profile];
    $("profile-blurb").textContent = p.blurb;
    document.querySelectorAll(".profile-name").forEach((el) => (el.textContent = p.label.toLowerCase()));
    renderAllocation(p);
    renderBacktest();
  }

  function renderAllocation(p) {
    $("allocation").innerHTML = `<thead><tr><th>Category</th><th>Signal</th><th class="num">Baseline</th><th class="num">Suggested</th><th class="hide-sm" style="width:30%">Weight</th></tr></thead><tbody>${LATEST.categories
      .filter((k) => k in p.baseline)
      .map((k) => {
        const s = LATEST.signals[k];
        const b = p.baseline[k], w = p.suggested[k];
        const d = w - b;
        const delta = d ? ` <span class="${d > 0 ? "delta-up" : "delta-down"}">(${d > 0 ? "+" : ""}${d})</span>` : "";
        return `<tr><td>${esc(s.title)}</td><td><span class="badge small ${actionClass(s.action)}">${esc(s.action)}</span></td>
          <td class="num">${b}%</td><td class="num"><b>${w}%</b>${delta}</td>
          <td class="hide-sm"><div class="weight-bar" title="Suggested ${w}%, baseline ${b}%"><span style="width:${Math.min(100, w * 1.6)}%"></span><i style="left:${Math.min(100, b * 1.6)}%"></i></div></td></tr>`;
      })
      .join("")}</tbody>`;
  }

  // ------------------------------------------------------------------ housing / ZIP
  function monthlyPI(principal, ratePct, years = 30) {
    const r = ratePct / 100 / 12, n = years * 12;
    return r === 0 ? principal / n : (principal * r) / (1 - Math.pow(1 + r, -n));
  }

  function scoreToAction(score, available) {
    if (available < 2) return "No signal";
    return score >= 2 ? "Buy" : score <= -2 ? "Sell" : "Hold";
  }

  function zipAnalysis(z, rec) {
    const h = LATEST.signals.housing;
    const rate = h.mortgage_rate, avg10 = h.mortgage_rate_10y_avg;
    const factors = [];
    const add = (name, score, detail) => factors.push({ name, score, detail });
    const payment = isNum(rate) ? monthlyPI(rec.v * 0.8, rate) : null;

    if (isNum(payment) && isNum(rec.i)) {
      const share = (payment / (rec.i / 12)) * 100;
      const s = share > 35 ? -1 : share < 25 ? 1 : 0;
      add("Affordability", s, `Principal & interest on a typical home here (20% down at ${fmtPct(rate)}) is about ${fmtMoney(payment)}/mo, ${share.toFixed(0)}% of the local median household income of ${fmtMoney(rec.i)}. ` +
        (s === -1 ? "Well above the ~25% norm." : s === 1 ? "Comfortably affordable." : "Somewhat stretched."));
    } else add("Affordability", null, "Local income data isn't available for this ZIP.");

    if (isNum(rec.r)) {
      const ratio = rec.v / (rec.r * 12);
      const s = ratio > 20 ? -1 : ratio < 15 ? 1 : 0;
      add("Price-to-rent", s, `Homes sell for ${ratio.toFixed(1)}× a year's rent (${fmtMoney(rec.r)}/mo). ` +
        (s === -1 ? "Above 20×, renting is usually cheaper than buying." : s === 1 ? "Below 15×, buying usually beats renting." : "In the 15-20× middle ground."));
    } else add("Price-to-rent", null, "Zillow doesn't track rents for this ZIP.");

    if (isNum(rec.y1)) {
      const s = rec.y1 > 8 ? -1 : rec.y1 < 0 ? 1 : 0;
      add("Local price momentum", s, `Typical values are ${rec.y1 > 0 ? "+" : ""}${rec.y1.toFixed(1)}% over a year. ` +
        (s === -1 ? "Running hot." : s === 1 ? "Falling prices give buyers leverage." : "Moderate."));
    } else add("Local price momentum", null, "Not enough history.");

    if (isNum(rate) && isNum(avg10)) {
      const d = rate - avg10;
      const s = d > 1 ? -1 : d < -0.5 ? 1 : 0;
      add("Mortgage rates (national)", s, `30-year rate is ${fmtPct(rate)} vs. a 10-year average of ${fmtPct(avg10)}.`);
    } else add("Mortgage rates (national)", null, "Mortgage data unavailable.");

    const avail = factors.filter((f) => f.score !== null).length;
    const score = factors.reduce((t, f) => t + (f.score || 0), 0);
    const ownCost = isNum(payment) ? payment + (rec.v * 0.015) / 12 : null;
    return { factors, score, action: scoreToAction(score, avail), payment, ownCost };
  }

  // ------------------------------------------------------------------ nearby Buy search
  const SEARCH_RADII = [50, 150, 400]; // miles; widen until something turns up
  const MOVE_COST = 0.08;              // ~8% of the sale price for agent fees, closing costs and moving

  function miles(la1, lo1, la2, lo2) {
    const R = 3958.8, rad = Math.PI / 180;
    const dLa = (la2 - la1) * rad, dLo = (lo2 - lo1) * rad;
    const a = Math.sin(dLa / 2) ** 2 + Math.cos(la1 * rad) * Math.cos(la2 * rad) * Math.sin(dLo / 2) ** 2;
    return 2 * R * Math.asin(Math.sqrt(a));
  }

  let nearIndex = null;
  async function loadNear() {
    if (!nearIndex) {
      const r = await fetch("data/zip/near.json");
      if (!r.ok) throw new Error("the nearby-ZIP index hasn't been built yet");
      nearIndex = await r.json();
    }
    return nearIndex;
  }

  async function findNearbyBuys(z, rec, el) {
    if (!isNum(rec.la)) { el.innerHTML = `<p class="hint">No location on file for ${esc(z)}, so a nearby search isn't possible.</p>`; return; }
    el.innerHTML = `<p class="hint">Searching nearby ZIP codes…</p>`;
    let idx;
    try { idx = await loadNear(); } catch (err) { el.innerHTML = `<p class="hint">Nearby search isn't available yet: ${esc(err.message)}.</p>`; return; }
    const cands = [];
    for (let k = 0; k < idx.z.length; k++) {
      if (idx.z[k] === z) continue;
      const d = miles(rec.la, rec.lo, idx.la[k], idx.lo[k]);
      if (d <= SEARCH_RADII[SEARCH_RADII.length - 1]) cands.push([d, k]);
    }
    cands.sort((a, b) => a[0] - b[0]);
    const buys = [];
    let holds = 0, radius = SEARCH_RADII[0];
    for (const r of SEARCH_RADII) {
      radius = r;
      for (const [d, k] of cands) {
        if (d > r) break;
        if (buys.some((b) => b.k === k)) continue;
        const other = { v: idx.v[k], y1: idx.y1[k], r: idx.r[k], i: idx.i[k] };
        const a = zipAnalysis(idx.z[k], other);
        if (a.action === "Buy") buys.push({ k, d, a, rec: other });
        else if (a.action === "Hold" && r === SEARCH_RADII[0]) holds++;
        if (buys.length >= 5) break;
      }
      if (buys.length) break;
    }
    if (!buys.length) {
      el.innerHTML = `<p class="hint">No ZIP within ${radius} miles of ${esc(z)} scores Buy right now. Rates and prices are the main obstacles nationally, so this can change as mortgage rates move.</p>`;
      return;
    }
    const net = (b) => rec.v * (1 - MOVE_COST) - b.rec.v;
    el.innerHTML = `<h4>Nearest Buy ZIP codes</h4>
      <p class="hint" style="margin:0 0 8px">Sorted by distance from ${esc(z)}, searched out to ${radius} miles. "Cash freed up" is a typical home's value here, minus ~${Math.round(MOVE_COST * 100)}% for selling, buying and moving costs, minus a typical home's value there.</p>
      <div class="table-wrap"><table><thead><tr><th>ZIP</th><th class="num">Distance</th><th class="num">Typical value</th><th class="num">1-yr change</th><th class="num">Price-to-rent</th><th class="num">Cash freed up</th><th class="num">Score</th></tr></thead><tbody>${buys
        .map((b) => {
          const p2r = isNum(b.rec.r) ? (b.rec.v / (b.rec.r * 12)).toFixed(1) + "×" : "n/a";
          const place = [idx.c[b.k], idx.s[b.k]].filter(Boolean).join(", ");
          return `<tr><td><button type="button" class="linklike" data-zip="${esc(idx.z[b.k])}">${esc(idx.z[b.k])}</button> <span class="asof">${esc(place)}</span></td>
            <td class="num">${b.d.toFixed(0)} mi</td><td class="num">${fmtMoney(b.rec.v)}</td><td class="num">${signed(b.rec.y1, 1, "%")}</td>
            <td class="num">${p2r}</td><td class="num">${(() => { const n = net(b); return `<span class="${n >= 0 ? "up" : "down"}">${n >= 0 ? "+" : "−"}${fmtCompactMoney(Math.abs(n))}</span>`; })()}</td><td class="num">+${b.a.score}</td></tr>`;
        })
        .join("")}</tbody></table></div>
      <p class="hint" style="margin-top:8px">A Buy here means local conditions favor buyers (affordability, rent comparison, cooling prices), not a guaranteed profit. Check commute, schools and taxes before moving, and remember that Zillow values are area-wide typical values, not your home's price.</p>`;
    el.querySelectorAll("button[data-zip]").forEach((btn) => btn.addEventListener("click", () => {
      $("zip").value = btn.dataset.zip;
      lookupZip(btn.dataset.zip);
      $("zip").scrollIntoView({ behavior: "smooth", block: "center" });
    }));
  }

  const zipCache = {};
  async function lookupZip(z) {
    const out = $("zip-out");
    if (!/^\d{5}$/.test(z)) { out.innerHTML = `<p class="hint">Enter a 5-digit ZIP code.</p>`; return; }
    out.innerHTML = `<p class="hint">Looking up ${esc(z)}…</p>`;
    try {
      const prefix = z.slice(0, 3);
      if (!(prefix in zipCache)) {
        const r = await fetch(`data/zip/${prefix}.json`);
        zipCache[prefix] = r.ok ? await r.json() : {};
      }
      const rec = zipCache[prefix][z];
      if (!rec) { out.innerHTML = `<p class="hint">No Zillow home value data for ${esc(z)}. It may be a PO-box-only or non-residential ZIP. Try a neighboring one.</p>`; return; }
      store.set("zip", z);
      const a = zipAnalysis(z, rec);
      const place = [rec.c, rec.s].filter(Boolean).join(", ");
      out.innerHTML = `<div class="zip-result">
        <div class="zip-head"><h3>${esc(z)}${place ? " · " + esc(place) : ""}</h3><span class="badge ${actionClass(a.action)}">${esc(a.action)}</span>
          <span class="score">Score ${a.score > 0 ? "+" : ""}${a.score} across ${a.factors.length} factors</span></div>
        ${rec.m ? `<div class="zip-note">${esc(rec.m)} metro${rec.co ? " · " + esc(rec.co) : ""}</div>` : ""}
        <div class="stats">
          <div class="stat"><div class="k">Typical home value</div><div class="v">${fmtMoney(rec.v)}</div><div class="k">${fmtDate(rec.d)}</div></div>
          <div class="stat"><div class="k">1-year change</div><div class="v">${signed(rec.y1, 1, "%")}</div></div>
          <div class="stat"><div class="k">5-year, per year</div><div class="v">${signed(rec.y5, 1, "%")}</div></div>
          <div class="stat"><div class="k">Typical rent</div><div class="v">${isNum(rec.r) ? fmtMoney(rec.r) : "n/a"}</div>${isNum(rec.ry1) ? `<div class="k">${rec.ry1 > 0 ? "+" : ""}${rec.ry1}% yr/yr</div>` : ""}</div>
          <div class="stat"><div class="k">Median household income</div><div class="v">${isNum(rec.i) ? fmtMoney(rec.i) : "n/a"}</div></div>
          <div class="stat"><div class="k">10-year trend</div><div class="v">${spark(rec.h, 140, 36)}</div></div>
        </div>
        ${isNum(a.ownCost) && isNum(rec.r) ? `<p class="hint" style="margin:0">Owning a typical home here costs roughly <b>${fmtMoney(a.ownCost)}/mo</b> (principal & interest plus ~1.5% a year for taxes, insurance and upkeep) vs. <b>${fmtMoney(rec.r)}/mo</b> to rent. This ignores the down payment's opportunity cost, tax breaks and appreciation.</p>` : ""}
        <ul class="factors">${factorList(a.factors)}</ul>
        <div class="nearby" id="nearby">${a.action === "Sell" ? "" : `<button type="button" class="linkbtn" id="nearby-btn">Find nearby Buy ZIP codes</button>`}</div>
      </div>`;
      const nearbyEl = $("nearby");
      if (a.action === "Sell") findNearbyBuys(z, rec, nearbyEl);
      else $("nearby-btn").addEventListener("click", () => findNearbyBuys(z, rec, nearbyEl));
    } catch (err) {
      out.innerHTML = `<p class="hint">ZIP data isn't available yet. It builds once a week. (${esc(err.message)})</p>`;
    }
  }

  function setupZip() {
    $("zip-form").addEventListener("submit", (e) => { e.preventDefault(); lookupZip($("zip").value.trim()); });
    const saved = store.get("zip");
    if (saved) { $("zip").value = saved; lookupZip(saved); }
  }

  function seriesTable(el, rows, opts = {}) {
    const unit = opts.unit || (() => "%");
    const nd = opts.nd ?? 2;
    $(el).innerHTML = `<thead><tr><th>Series</th><th class="num">Latest</th><th class="num">1-yr change</th><th class="hide-sm">5-yr trend</th><th>As of</th></tr></thead><tbody>${rows
      .map((r) => {
        const u = unit(r);
        const v = u === "$" ? fmtMoney(r.value) : u === "%" ? fmtPct(r.value, nd) : fmtNum(r.value, nd) + (u ? " " + u : "");
        const chg = u === "%" ? signed(r.change_1y, 2, " pts") : u === "$" ? signed(r.change_1y, 0) : signed(r.change_1y, 1);
        return `<tr><td>${esc(r.name)}</td><td class="num">${v}</td><td class="num">${chg}</td><td class="hide-sm">${spark(r.spark)}</td><td class="asof">${fmtDate(r.as_of)}</td></tr>`;
      })
      .join("")}</tbody>`;
  }

  // ------------------------------------------------------------------ borrowing
  function renderLoans(b, loanRates) {
    $("expected").textContent = b.expected_stock_return;
    const spk = Object.fromEntries(loanRates.map((r) => [r.id, r.spark]));
    $("loans").innerHTML = `<thead><tr><th>Loan</th><th class="num">Rate</th><th class="num">1-yr change</th><th class="hide-sm">5-yr trend</th><th>What it suggests</th><th>As of</th></tr></thead><tbody>${b.loans
      .map((l) => `<tr><td>${esc(l.name)}</td><td class="num">${fmtPct(l.rate)}</td><td class="num">${signed(l.change_1y, 2, " pts")}</td><td class="hide-sm">${spark(spk[l.id])}</td><td>${esc(l.verdict)}</td><td class="asof">${fmtDate(l.as_of)}</td></tr>`)
      .join("")}</tbody>`;

    const m30 = b.loans.find((l) => l.id === "MORTGAGE30US");
    const input = $("myrate"), out = $("refi-out");
    const update = () => {
      const mine = parseFloat(input.value);
      if (!isNum(mine) || !m30 || !isNum(m30.rate)) { out.textContent = ""; return; }
      const gap = mine - m30.rate;
      if (gap >= 1) out.textContent = `Today's 30-year average is ${fmtPct(m30.rate)}, ${gap.toFixed(2)} points below yours. A common rule of thumb says refinancing is worth pricing out when you can cut your rate by about 1 point or more, after closing costs.`;
      else if (gap >= 0.5) out.textContent = `You're ${gap.toFixed(2)} points above today's average of ${fmtPct(m30.rate)}. Refinancing may pay off if you plan to stay a long time and closing costs are low.`;
      else out.textContent = `Your rate is at or near today's average of ${fmtPct(m30.rate)}. Refinancing likely isn't worth it right now.`;
    };
    input.addEventListener("input", update);
    input.addEventListener("change", () => store.set("myrate", input.value));
    const saved = store.get("myrate");
    if (saved) { input.value = saved; update(); }
  }

  // ------------------------------------------------------------------ backtest chart
  state.period = store.get("period") || "long";

  function btSeries(res) {
    const idx = res.stock_index === "S&P 500" ? "S&P 500" : res.stock_index.replace(" Composite", "");
    return [
      { key: 1, name: "Signal-tilted", color: "var(--series-1)" },
      { key: 2, name: "Static mix", color: "var(--series-2)" },
      { key: 3, name: `100% ${idx}`, color: "var(--series-3)" },
    ];
  }

  function renderBacktest() {
    const bt = BACKTEST && BACKTEST.backtest;
    const periods = (bt && bt.periods) || {};
    if (!periods[state.period]) state.period = Object.keys(periods)[0];
    const per = periods[state.period];
    const res = per && per.profiles[state.profile];
    if (!res) {
      $("bt-intro").textContent = "Backtest data isn't available yet.";
      $("bt-chart").innerHTML = ""; $("bt-stats").innerHTML = ""; $("bt-weight").innerHTML = "";
      return;
    }
    $("bt-periods").innerHTML = Object.entries(periods)
      .map(([k, p]) => `<button type="button" role="radio" data-period="${k}" aria-checked="${k === state.period}">${esc(p.label)}</button>`).join("");
    const c = res.core;
    const series = btSeries(per);
    const longRun = per.months > 240;
    $("bt-intro").textContent = `Growth of $10,000 from ${fmtDate(per.start.slice(0, 7))} to ${fmtDate(per.end.slice(0, 7))}` +
      (longRun ? " (log scale, so equal percentage moves look the same size)" : "") + `. ` +
      `The ${LATEST.profiles[state.profile].label.toLowerCase()} core mix is ${c.stocks}% stocks (${per.stock_index}), ${c.bonds}% Treasuries and ${c.cash}% cash. ` +
      `"Signal-tilted" re-weights that mix every month from the stocks, bonds and cash scores; "Static mix" never changes.`;
    $("bt-legend").innerHTML = series.map((s) => `<span><i class="sw" style="--c:${s.color}"></i>${s.name}</span>`).join("");
    lineChart($("bt-chart"), res.curve, { series, log: longRun, fmt: fmtCompactMoney, tipFmt: fmtMoney, height: 300 });
    lineChart($("bt-weight"), res.curve.slice(1), {
      series: [{ key: 4, name: "Stock allocation", color: "var(--series-1)" }],
      lo: 0, hi: 100, step: 25, fmt: (v) => v + "%", tipFmt: (v) => v + "%", height: 140,
    });
    $("bt-weight-note").textContent = `Stock allocation in the signal-tilted mix: ${res.stock_weight.min}% to ${res.stock_weight.max}%, ${res.stock_weight.avg}% on average (static mix: ${c.stocks}%).`;

    const st = res.stats;
    const rows = [["strategy", series[0].name], ["static", series[1].name], ["sp500", series[2].name]];
    $("bt-stats").innerHTML = `<thead><tr><th>Portfolio</th><th class="num">Ending value</th><th class="num">Return / yr</th><th class="num">Volatility</th><th class="num">Worst drop</th><th class="num">Sharpe</th></tr></thead><tbody>${rows
      .map(([k, n]) => `<tr><td>${esc(n)}</td><td class="num">${fmtMoney(st[k].final)}</td><td class="num">${fmtPct(st[k].cagr)}</td><td class="num">${fmtPct(st[k].volatility, 1)}</td><td class="num">${signed(st[k].max_drawdown, 1, "%")}</td><td class="num">${fmtNum(st[k].sharpe)}</td></tr>`)
      .join("")}</tbody>`;
    $("bt-notes").innerHTML = bt.assumptions.notes.map((n) => `<li>${esc(n)}</li>`).join("") +
      `<li>Sharpe ratio: return above cash per unit of volatility. Higher is better; it rewards getting similar returns with smaller swings.</li>`;
  }

  function setupPeriodPicker() {
    $("bt-periods").addEventListener("click", (e) => {
      const b = e.target.closest("button[data-period]");
      if (!b) return;
      state.period = b.dataset.period;
      store.set("period", state.period);
      renderBacktest();
    });
  }

  function lineChart(el, rows, opt) {
    const W = 800, H = opt.height || 300, m = { l: 56, r: 12, t: 10, b: 26 };
    const series = opt.series;
    rows = rows.filter((r) => series.every((s) => isNum(r[s.key])));
    const n = rows.length;
    if (n < 2) { el.innerHTML = ""; return; }
    const vals = rows.flatMap((r) => series.map((s) => r[s.key]));
    const tf = opt.log ? Math.log10 : (v) => v;
    let lo, hi, ticks = [];
    if (opt.log) {
      lo = Math.min(...vals); hi = Math.max(...vals);
      for (let e = Math.floor(Math.log10(lo)); e <= Math.ceil(Math.log10(hi)); e++)
        for (const mlt of [1, 2, 5]) { const v = mlt * 10 ** e; if (v >= lo * 0.9 && v <= hi * 1.1) ticks.push(v); }
      lo = Math.min(lo, ticks[0] || lo); hi = Math.max(hi, ticks[ticks.length - 1] || hi);
    } else {
      lo = opt.lo ?? Math.min(...vals); hi = opt.hi ?? Math.max(...vals);
      const step = opt.step || niceStep((hi - lo) / 5);
      lo = Math.floor(lo / step) * step; hi = Math.ceil(hi / step) * step;
      for (let v = lo; v <= hi + 1e-9; v += step) ticks.push(v);
    }
    const x = (i) => m.l + (i / (n - 1)) * (W - m.l - m.r);
    const y = (v) => m.t + (1 - (tf(v) - tf(lo)) / (tf(hi) - tf(lo))) * (H - m.t - m.b);

    let grid = ticks.map((v) => `<line class="gridline" x1="${m.l}" x2="${W - m.r}" y1="${y(v)}" y2="${y(v)}"/><text x="${m.l - 8}" y="${y(v) + 4}" text-anchor="end">${opt.fmt(v)}</text>`).join("");
    const every = n > 300 ? 5 : n > 90 ? 2 : 1;
    let xl = "";
    rows.forEach((r, i) => {
      if (r[0].endsWith("-01") && Number(r[0].slice(0, 4)) % every === 0) xl += `<text x="${x(i)}" y="${H - 6}" text-anchor="middle">${r[0].slice(0, 4)}</text>`;
    });
    const paths = series.map((s) => `<path class="line" style="stroke:${s.color}" d="${rows.map((r, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(r[s.key]).toFixed(1)}`).join("")}"/>`).join("");
    const last = rows[n - 1];
    const label = series.map((s) => `${s.name} ${opt.tipFmt(last[s.key])}`).join(", ");
    el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(label)}">
      ${grid}${xl}${paths}<g class="hover" visibility="hidden"><line class="crosshair" y1="${m.t}" y2="${H - m.b}"/>${series.map((s) => `<circle class="dot" r="4.5" style="fill:${s.color}"/>`).join("")}</g>
      <rect x="${m.l}" y="0" width="${W - m.l - m.r}" height="${H}" fill="transparent"/></svg>`;
    const svg = el.querySelector("svg"), g = svg.querySelector(".hover");
    const move = (ev) => {
      const pt = ev.touches ? ev.touches[0] : ev;
      const rect = svg.getBoundingClientRect();
      const px = ((pt.clientX - rect.left) / rect.width) * W;
      const i = Math.max(0, Math.min(n - 1, Math.round(((px - m.l) / (W - m.l - m.r)) * (n - 1))));
      const r = rows[i];
      g.setAttribute("visibility", "visible");
      g.querySelector("line").setAttribute("x1", x(i)); g.querySelector("line").setAttribute("x2", x(i));
      g.querySelectorAll("circle").forEach((c, k) => { c.setAttribute("cx", x(i)); c.setAttribute("cy", y(r[series[k].key])); });
      showTip(`<div><b>${fmtDate(r[0])}</b></div>` + series.map((s) => `<div class="row"><i class="sw" style="--c:${s.color}"></i>${esc(s.name)}: <b>${opt.tipFmt(r[s.key])}</b></div>`).join(""), pt.clientX, pt.clientY);
    };
    const leave = () => { g.setAttribute("visibility", "hidden"); hideTip(); };
    svg.addEventListener("pointermove", move);
    svg.addEventListener("pointerleave", leave);
    svg.addEventListener("touchmove", move, { passive: true });
  }

  function niceStep(raw) {
    const p = Math.pow(10, Math.floor(Math.log10(raw)));
    const f = raw / p;
    return (f < 1.5 ? 1 : f < 3 ? 2 : f < 7 ? 5 : 10) * p;
  }

  // ------------------------------------------------------------------ signal timeline
  function renderTimeline() {
    const tl = BACKTEST && BACKTEST.timeline;
    if (!tl || !tl.length) { $("timeline").innerHTML = `<p class="hint">Not available yet.</p>`; return; }
    const cats = LATEST.categories;
    const labelW = 150, cellH = 18, gap = 2, top = 18;
    const W = 900, n = tl.length;
    const cw = (W - labelW) / n;
    const H = top + cats.length * (cellH + gap);
    let s = "";
    tl.forEach((t, i) => {
      if (t.date.slice(5, 7) === "01") s += `<text x="${labelW + i * cw}" y="12">${t.date.slice(0, 4)}</text>`;
    });
    cats.forEach((k, r) => {
      const yy = top + r * (cellH + gap);
      s += `<text x="0" y="${yy + cellH / 2 + 4}">${esc(LATEST.signals[k].title)}</text>`;
      tl.forEach((t, i) => {
        const a = (t[k] && t[k].a) || "No signal";
        s += `<rect class="tl-cell ${actionClass(a)}" x="${(labelW + i * cw).toFixed(2)}" y="${yy}" width="${Math.max(cw - 0.5, 0.5).toFixed(2)}" height="${cellH}" data-i="${i}" data-k="${k}"/>`;
      });
    });
    $("timeline").innerHTML = `<div style="overflow-x:auto"><svg viewBox="0 0 ${W} ${H}" style="min-width:640px" role="img" aria-label="Monthly signal history for ${cats.length} categories">${s}</svg></div>`;
    $("tl-legend").innerHTML = [["Buy / Overweight", "act-buy"], ["Hold", "act-hold"], ["Sell / Underweight", "act-sell"], ["No signal", "act-none"]]
      .map(([l, c]) => `<span><i class="sq ${c}"></i>${l}</span>`).join("");
    const svg = $("timeline").querySelector("svg");
    svg.addEventListener("pointermove", (e) => {
      const c = e.target.closest(".tl-cell");
      if (!c) return hideTip();
      const t = tl[+c.dataset.i], k = c.dataset.k, v = t[k] || {};
      showTip(`<b>${esc(LATEST.signals[k].title)}</b><br>${fmtDate(t.date)}: ${esc(v.a || "No signal")}${isNum(v.s) ? ` (score ${v.s > 0 ? "+" : ""}${v.s})` : ""}`, e.clientX, e.clientY);
    });
    svg.addEventListener("pointerleave", hideTip);
  }

  function renderHistory(history) {
    const cats = LATEST.categories;
    const rows = history.slice(-30).reverse();
    const cell = (x) => (x ? `<span class="badge small ${actionClass(x.action)}">${esc(x.action)}</span>` : "");
    $("history").innerHTML = `<thead><tr><th>Date</th>${cats.map((k) => `<th>${esc(LATEST.signals[k].title)}</th>`).join("")}</tr></thead><tbody>${rows
      .map((h) => `<tr><td class="asof">${fmtDate(h.date)}</td>${cats.map((k) => `<td>${cell(h[k])}</td>`).join("")}</tr>`)
      .join("")}</tbody>`;
  }

  // ------------------------------------------------------------------ markets / economy
  function renderMarkets(markets, lr) {
    $("lr-nom").textContent = lr.sp500_nominal_annual;
    $("lr-real").textContent = lr.sp500_real_annual;
    $("markets").innerHTML = `<thead><tr><th>Market</th><th class="num">Level</th><th class="num">YTD</th><th class="num">1 yr</th><th class="num">3 yr / yr</th><th class="num">5 yr / yr</th><th class="hide-sm">5-yr trend</th><th>As of</th></tr></thead><tbody>${markets
      .map((m) => `<tr><td>${esc(m.name)}</td><td class="num">${fmtNum(m.value, m.value < 1000 ? 2 : 0)}</td><td class="num">${signed(m.ytd, 1, "%")}</td><td class="num">${signed(m["1y"], 1, "%")}</td><td class="num">${signed(m["3y"], 1, "%")}</td><td class="num">${signed(m["5y"], 1, "%")}</td><td class="hide-sm">${spark(m.spark)}</td><td class="asof">${fmtDate(m.as_of)}</td></tr>`)
      .join("")}</tbody>`;
  }

  function renderEconomy(e, housing) {
    const aff = housing.affordability || {};
    const stats = [
      ["Inflation (CPI, yr/yr)", fmtPct(e.cpi_yoy, 1), e.cpi_as_of],
      ["Unemployment", fmtPct(e.unemployment, 1), e.unemployment_as_of],
      ["VIX", fmtNum(e.vix, 1)],
      ["Dollar, 6-month change", isNum(e.dollar_6m) ? (e.dollar_6m > 0 ? "+" : "") + fmtPct(e.dollar_6m, 1) : "n/a"],
      ["Median-home payment", isNum(aff.monthly_payment) ? fmtMoney(aff.monthly_payment) + "/mo" : "n/a"],
      ["Payment / income", fmtPct(aff.share_of_income, 0)],
    ];
    $("economy").innerHTML = stats
      .map(([k, v, d]) => `<div class="stat"><div class="k">${esc(k)}</div><div class="v">${v}</div>${d ? `<div class="k">${fmtDate(d)}</div>` : ""}</div>`)
      .join("");
  }

  // ------------------------------------------------------------------ main
  async function main() {
    try {
      const bust = "?t=" + Math.floor(Date.now() / 3.6e6); // hourly cache-buster
      const getJson = (p, fallback) => fetch(p + bust).then((r) => (r.ok ? r.json() : fallback)).catch(() => fallback);
      const [latest, history, backtest] = await Promise.all([
        fetch("data/latest.json" + bust).then((r) => { if (!r.ok) throw new Error("latest.json " + r.status); return r.json(); }),
        getJson("data/history.json", []),
        getJson("data/backtest.json", null),
      ]);
      LATEST = latest; BACKTEST = backtest;
      if (!latest.categories) throw new Error("data is from an older version; the next daily update will fix this");

      const gen = new Date(latest.generated_at);
      let meta = `Updated ${gen.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })}. Source: ${esc(latest.source)}.`;
      if (latest.missing_series && latest.missing_series.length) meta += ` Some series could not be fetched today: ${esc(latest.missing_series.join(", "))}.`;
      $("updated").innerHTML = meta;
      document.querySelectorAll(".tilt").forEach((el) => (el.textContent = Math.round(latest.tilt * 100)));
      document.querySelectorAll(".tilt-lo").forEach((el) => (el.textContent = Math.round((latest.tilt_range || [0.25])[0] * 100)));
      document.querySelectorAll(".tilt-hi").forEach((el) => (el.textContent = Math.round((latest.tilt_range || [0, 1.75])[1] * 100)));

      renderSignals(latest);
      setupPeriodPicker();
      renderProfilePicker();
      renderProfileDependent();
      renderLoans(latest.borrowing, latest.loan_rates);
      renderMarkets(latest.markets, latest.long_run_averages);
      seriesTable("rates", latest.rates);
      seriesTable("housing", latest.housing, {
        nd: 1,
        unit: (r) => (r.id === "MSPUS" || r.id === "ASPUS" ? "$" : r.id === "MSACSR" ? "months" : ""),
      });
      renderEconomy(latest.economy, latest.signals.housing);
      renderTimeline();
      renderHistory(history);
      setupZip();
      $("app").hidden = false;
    } catch (err) {
      $("updated").innerHTML = `<div class="error">Data isn't available yet. The daily update job may not have run. (${esc(err.message)})</div>`;
    }
  }

  main();
})();
