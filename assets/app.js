(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const isNum = (x) => typeof x === "number" && isFinite(x);

  const fmtNum = (x, nd = 2) => (isNum(x) ? x.toLocaleString(undefined, { minimumFractionDigits: nd, maximumFractionDigits: nd }) : "n/a");
  const fmtPct = (x, nd = 2) => (isNum(x) ? fmtNum(x, nd) + "%" : "n/a");
  const fmtMoney = (x) => (isNum(x) ? "$" + Math.round(x).toLocaleString() : "n/a");
  const signed = (x, nd = 2, suffix = "") => {
    if (!isNum(x)) return '<span class="na">n/a</span>';
    const cls = x > 0 ? "up" : x < 0 ? "down" : "";
    return `<span class="${cls}">${x > 0 ? "+" : ""}${fmtNum(x, nd)}${suffix}</span>`;
  };
  const fmtDate = (s) => {
    if (!s) return "n/a";
    const d = new Date(s + (s.length === 10 ? "T00:00:00" : ""));
    return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
  };

  function actionClass(a) {
    if (a === "Buy" || a === "Overweight") return "act-buy";
    if (a === "Sell" || a === "Underweight") return "act-sell";
    if (a === "Hold") return "act-hold";
    return "act-none";
  }

  function spark(points, w = 120, h = 32) {
    if (!points || points.length < 2) return "";
    const ys = points.map((p) => p[1]);
    const min = Math.min(...ys), max = Math.max(...ys);
    const span = max - min || 1;
    const d = points
      .map((p, i) => `${i ? "L" : "M"}${((i / (points.length - 1)) * w).toFixed(1)},${(h - 2 - ((p[1] - min) / span) * (h - 4)).toFixed(1)}`)
      .join("");
    const label = `5-year trend from ${fmtNum(ys[0])} to ${fmtNum(ys[ys.length - 1])}`;
    return `<svg class="spark" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" role="img" aria-label="${esc(label)}"><path d="${d}"/></svg>`;
  }

  function renderSignals(signals) {
    const order = ["stocks", "bonds", "cash", "housing"];
    $("signals").innerHTML = order
      .filter((k) => signals[k])
      .map((k) => {
        const s = signals[k];
        const hm = s.headline_metric || {};
        let hv = hm.unit === "%" ? fmtPct(hm.value) : hm.unit === "$" ? fmtMoney(hm.value) : fmtNum(hm.value, 0);
        const factors = s.factors
          .map((f) => {
            const cls = f.score === null ? "na" : f.score > 0 ? "pos" : f.score < 0 ? "neg" : "zero";
            const pt = f.score === null ? "–" : f.score > 0 ? "+1" : f.score < 0 ? "−1" : "0";
            return `<li><span class="pt ${cls}">${pt}</span><span><span class="fname">${esc(f.name)}.</span> ${esc(f.detail)}</span></li>`;
          })
          .join("");
        return `<article class="card">
          <h3>${esc(s.title)}</h3>
          <p class="subject">${esc(s.subject)}</p>
          <span class="badge ${actionClass(s.action)}">${esc(s.action)}</span>
          <div class="score">Score ${s.score > 0 ? "+" : ""}${s.score} across ${s.max_score} factors</div>
          <div class="headline">${esc(hm.label)}: <b>${hv}</b> <span class="asof">(${fmtDate(hm.as_of)})</span></div>
          <details><summary>Why?</summary><ul class="factors">${factors}</ul></details>
        </article>`;
      })
      .join("");
  }

  function renderAllocation(a) {
    const s = a.suggested;
    const seg = (k, label) => `<div class="seg-${k}" style="width:${s[k]}%">${s[k] >= 8 ? s[k] + "%" : ""}</div>`;
    const b = a.baseline;
    const changed = s.stocks !== b.stocks || s.bonds !== b.bonds || s.cash !== b.cash;
    $("allocation").innerHTML = `<div class="alloc">
      <div class="alloc-label">${changed ? "Tilted from the 60 / 30 / 10 baseline" : "No tilt today: signals are neutral, so the baseline stands"}</div>
      <div class="alloc-bar" role="img" aria-label="Stocks ${s.stocks}%, bonds ${s.bonds}%, cash ${s.cash}%">
        ${seg("stocks")}${seg("bonds")}${seg("cash")}
      </div>
      <div class="alloc-legend">
        <span style="--c:#2f5fd0">Stocks ${s.stocks}%</span>
        <span style="--c:#7b4fc8">Bonds ${s.bonds}%</span>
        <span style="--c:#3c8c7a">Cash ${s.cash}%</span>
      </div>
    </div>`;
  }

  function seriesTable(el, rows, opts = {}) {
    const unit = opts.unit || ((r) => "%");
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

  function renderLoans(b, loanRates) {
    $("expected").textContent = b.expected_stock_return;
    const spk = Object.fromEntries(loanRates.map((r) => [r.id, r.spark]));
    $("loans").innerHTML = `<thead><tr><th>Loan</th><th class="num">Rate</th><th class="num">1-yr change</th><th class="hide-sm">5-yr trend</th><th>What it suggests</th><th>As of</th></tr></thead><tbody>${b.loans
      .map((l) => `<tr><td>${esc(l.name)}</td><td class="num">${fmtPct(l.rate)}</td><td class="num">${signed(l.change_1y, 2, " pts")}</td><td class="hide-sm">${spark(spk[l.id])}</td><td>${esc(l.verdict)}</td><td class="asof">${fmtDate(l.as_of)}</td></tr>`)
      .join("")}</tbody>`;

    const m30 = b.loans.find((l) => l.id === "MORTGAGE30US");
    const input = $("myrate");
    const out = $("refi-out");
    const update = () => {
      const mine = parseFloat(input.value);
      if (!isNum(mine) || !m30 || !isNum(m30.rate)) { out.textContent = ""; return; }
      const gap = mine - m30.rate;
      if (gap >= 1) out.textContent = `Today's 30-year average is ${fmtPct(m30.rate)}, ${gap.toFixed(2)} points below yours. A common rule of thumb says refinancing is worth pricing out when you can cut your rate by about 1 point or more, after closing costs.`;
      else if (gap >= 0.5) out.textContent = `You're ${gap.toFixed(2)} points above today's average of ${fmtPct(m30.rate)}. Refinancing may pay off if you plan to stay a long time and closing costs are low.`;
      else out.textContent = `Your rate is at or near today's average of ${fmtPct(m30.rate)}. Refinancing likely isn't worth it right now.`;
    };
    input.addEventListener("input", update);
    try { const saved = localStorage.getItem("myrate"); if (saved) { input.value = saved; update(); } } catch (e) {}
    input.addEventListener("change", () => { try { localStorage.setItem("myrate", input.value); } catch (e) {} });
  }

  function renderMarkets(markets, lr) {
    $("lr-nom").textContent = lr.sp500_nominal_annual;
    $("lr-real").textContent = lr.sp500_real_annual;
    $("markets").innerHTML = `<thead><tr><th>Index</th><th class="num">Level</th><th class="num">YTD</th><th class="num">1 yr</th><th class="num">3 yr / yr</th><th class="num">5 yr / yr</th><th class="hide-sm">5-yr trend</th><th>As of</th></tr></thead><tbody>${markets
      .map((m) => `<tr><td>${esc(m.name)}</td><td class="num">${fmtNum(m.value, 0)}</td><td class="num">${signed(m.ytd, 1, "%")}</td><td class="num">${signed(m["1y"], 1, "%")}</td><td class="num">${signed(m["3y"], 1, "%")}</td><td class="num">${signed(m["5y"], 1, "%")}</td><td class="hide-sm">${spark(m.spark)}</td><td class="asof">${fmtDate(m.as_of)}</td></tr>`)
      .join("")}</tbody>`;
  }

  function renderEconomy(e, housing) {
    const aff = housing.affordability || {};
    const stats = [
      ["Inflation (CPI, yr/yr)", fmtPct(e.cpi_yoy, 1), e.cpi_as_of],
      ["Unemployment", fmtPct(e.unemployment, 1), e.unemployment_as_of],
      ["VIX", fmtNum(e.vix, 1)],
      ["Median-home payment", isNum(aff.monthly_payment) ? fmtMoney(aff.monthly_payment) + "/mo" : "n/a"],
      ["Payment / income", fmtPct(aff.share_of_income, 0)],
    ];
    $("economy").innerHTML = stats
      .map(([k, v, d]) => `<div class="stat"><div class="k">${esc(k)}</div><div class="v">${v}</div>${d ? `<div class="k">${fmtDate(d)}</div>` : ""}</div>`)
      .join("");
  }

  function renderHistory(history) {
    const rows = history.slice(-30).reverse();
    const cell = (x) => (x ? `<span class="badge small ${actionClass(x.action)}">${esc(x.action)}</span>` : "");
    $("history").innerHTML = `<thead><tr><th>Date</th><th>Stocks</th><th>Bonds</th><th>Cash</th><th>Housing</th><th class="num">Mix (S/B/C)</th></tr></thead><tbody>${rows
      .map((h) => `<tr><td class="asof">${fmtDate(h.date)}</td><td>${cell(h.stocks)}</td><td>${cell(h.bonds)}</td><td>${cell(h.cash)}</td><td>${cell(h.housing)}</td><td class="num">${h.allocation ? `${h.allocation.stocks}/${h.allocation.bonds}/${h.allocation.cash}` : ""}</td></tr>`)
      .join("")}</tbody>`;
  }

  async function main() {
    try {
      const bust = "?t=" + Math.floor(Date.now() / 3.6e6); // hourly cache-buster
      const [latest, history] = await Promise.all([
        fetch("data/latest.json" + bust).then((r) => { if (!r.ok) throw new Error("latest.json " + r.status); return r.json(); }),
        fetch("data/history.json" + bust).then((r) => (r.ok ? r.json() : [])).catch(() => []),
      ]);

      const gen = new Date(latest.generated_at);
      let meta = `Updated ${gen.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })}. Source: ${esc(latest.source)}.`;
      if (latest.missing_series && latest.missing_series.length) meta += ` Some series could not be fetched today: ${esc(latest.missing_series.join(", "))}.`;
      $("updated").innerHTML = meta;

      renderSignals(latest.signals);
      renderAllocation(latest.allocation);
      renderLoans(latest.borrowing, latest.loan_rates);
      renderMarkets(latest.markets, latest.long_run_averages);
      seriesTable("rates", latest.rates, { unit: (r) => "%" });
      seriesTable("housing", latest.housing, {
        nd: 1,
        unit: (r) => (r.id === "MSPUS" || r.id === "ASPUS" ? "$" : r.id === "MSACSR" ? "months" : ""),
      });
      renderEconomy(latest.economy, latest.signals.housing);
      renderHistory(history);
      $("app").hidden = false;
    } catch (err) {
      $("updated").innerHTML = `<div class="error">Data isn't available yet. The daily update job may not have run. (${esc(err.message)})</div>`;
    }
  }

  main();
})();
