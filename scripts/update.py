#!/usr/bin/env python3
"""Fetch daily market data from FRED and compute the Investing Barometer signals.

Writes:
  data/latest.json   - everything the web page needs (metrics, signals, sparklines)
  data/history.json  - one compact entry per day with each category's action

Data source: Federal Reserve Bank of St. Louis (FRED). If the FRED_API_KEY
environment variable is set, the official API is used; otherwise the public,
keyless CSV download endpoint is used.

Only the Python standard library is needed.

Usage:
  python scripts/update.py                     # live fetch
  python scripts/update.py --fixtures DIR      # read DIR/<SERIES>.csv instead (for testing)
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
START_DATE = "2010-01-01"
HISTORY_MAX_DAYS = 730

# Long-run reference figures (not fetched; widely cited historical averages).
LONG_RUN = {
    "sp500_nominal_annual": 10.0,  # S&P 500 total return, ~1926-present, approx.
    "sp500_real_annual": 7.0,      # after inflation, approx.
    "bonds_nominal_annual": 5.0,   # intermediate US government bonds, approx.
    "tbills_nominal_annual": 3.3,  # 3-month T-bills, approx.
}

SERIES = {
    # Loans
    "MORTGAGE30US": "30-year fixed mortgage",
    "MORTGAGE15US": "15-year fixed mortgage",
    "TERMCBAUTO48NS": "48-month new auto loan",
    "TERMCBPER24NS": "24-month personal loan",
    "TERMCBCCALLNS": "Credit card (all accounts)",
    # Rates / bonds
    "DFF": "Federal funds rate",
    "DGS3MO": "3-month Treasury bill",
    "DGS2": "2-year Treasury",
    "DGS10": "10-year Treasury",
    "DGS30": "30-year Treasury",
    "DFII10": "10-year TIPS (real yield)",
    "T10Y3M": "10-year minus 3-month spread",
    "BAMLH0A0HYM2": "High-yield credit spread",
    "BAMLC0A0CM": "Investment-grade credit spread",
    # Stocks
    "SP500": "S&P 500",
    "NASDAQCOM": "Nasdaq Composite",
    "DJIA": "Dow Jones Industrial Average",
    "VIXCLS": "VIX volatility index",
    # Housing
    "MSPUS": "Median home sales price",
    "ASPUS": "Average home sales price",
    "CSUSHPINSA": "Case-Shiller US home price index",
    "MSACSR": "Months' supply of new homes",
    "MEHOINUSA672N": "Median household income",
    # Economy
    "CPIAUCSL": "Consumer Price Index",
    "UNRATE": "Unemployment rate",
}


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #

def _get(url: str, retries: int = 3) -> bytes:
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "investing-barometer/1.0"})
            with urllib.request.urlopen(req, timeout=45) as resp:
                return resp.read()
        except Exception as err:  # noqa: BLE001 - retry any network failure
            last_err = err
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"GET failed after {retries} attempts: {last_err}")


def _parse_value(raw: str) -> float | None:
    raw = (raw or "").strip()
    if raw in ("", "."):
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def parse_csv(text: str) -> list[tuple[str, float]]:
    rows = list(csv.reader(io.StringIO(text)))
    out = []
    for row in rows[1:]:
        if len(row) < 2:
            continue
        v = _parse_value(row[1])
        if v is not None:
            out.append((row[0], v))
    return out


def fetch_series(series_id: str, api_key: str | None, fixtures: Path | None) -> list[tuple[str, float]]:
    if fixtures is not None:
        return parse_csv((fixtures / f"{series_id}.csv").read_text())
    if api_key:
        qs = urllib.parse.urlencode({
            "series_id": series_id,
            "api_key": api_key,
            "file_type": "json",
            "observation_start": START_DATE,
        })
        payload = json.loads(_get(f"https://api.stlouisfed.org/fred/series/observations?{qs}"))
        return [(o["date"], v) for o in payload["observations"] if (v := _parse_value(o["value"])) is not None]
    qs = urllib.parse.urlencode({"id": series_id, "cosd": START_DATE})
    return parse_csv(_get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?{qs}").decode("utf-8"))


# --------------------------------------------------------------------------- #
# Series helpers
# --------------------------------------------------------------------------- #

def _d(s: str) -> date:
    return date.fromisoformat(s)


class Series:
    def __init__(self, sid: str, obs: list[tuple[str, float]]):
        self.id = sid
        self.obs = sorted(obs)

    def __bool__(self) -> bool:
        return bool(self.obs)

    @property
    def last(self) -> float | None:
        return self.obs[-1][1] if self.obs else None

    @property
    def last_date(self) -> str | None:
        return self.obs[-1][0] if self.obs else None

    def value_on_or_before(self, when: date) -> float | None:
        best = None
        for d, v in self.obs:
            if _d(d) <= when:
                best = v
            else:
                break
        return best

    def ago(self, days: int) -> float | None:
        if not self.obs:
            return None
        return self.value_on_or_before(_d(self.last_date) - timedelta(days=days))

    def change(self, days: int) -> float | None:
        prev = self.ago(days)
        return None if prev is None or self.last is None else self.last - prev

    def pct_change(self, days: int) -> float | None:
        prev = self.ago(days)
        if prev in (None, 0) or self.last is None:
            return None
        return (self.last / prev - 1) * 100

    def cagr(self, years: int) -> float | None:
        prev = self.ago(int(round(365.25 * years)))
        if prev in (None, 0) or self.last is None:
            return None
        # Require the series to actually reach back that far.
        if _d(self.obs[0][0]) > _d(self.last_date) - timedelta(days=int(365.25 * years) - 10):
            return None
        return ((self.last / prev) ** (1 / years) - 1) * 100

    def mean_since(self, days: int) -> float | None:
        if not self.obs:
            return None
        cutoff = _d(self.last_date) - timedelta(days=days)
        vals = [v for d, v in self.obs if _d(d) >= cutoff]
        return sum(vals) / len(vals) if vals else None

    def moving_average(self, n: int) -> float | None:
        if len(self.obs) < n:
            return None
        return sum(v for _, v in self.obs[-n:]) / n

    def spark(self, days: int = 5 * 365, points: int = 120) -> list[list]:
        """Down-sampled [date, value] pairs for a sparkline."""
        if not self.obs:
            return []
        cutoff = _d(self.last_date) - timedelta(days=days)
        sub = [(d, v) for d, v in self.obs if _d(d) >= cutoff]
        if len(sub) > points:
            step = len(sub) / points
            sub = [sub[int(i * step)] for i in range(points)] + [sub[-1]]
        return [[d, round(v, 4)] for d, v in sub]


def rnd(x: float | None, nd: int = 2) -> float | None:
    return None if x is None else round(x, nd)


# --------------------------------------------------------------------------- #
# Strategy rules
# --------------------------------------------------------------------------- #

class Scorer:
    """Collects factors that each score -1, 0 or +1 and turns them into an action."""

    def __init__(self):
        self.factors: list[dict] = []

    def add(self, name: str, score: int | None, detail: str):
        self.factors.append({
            "name": name,
            "score": score,          # None means data unavailable
            "detail": detail,
        })

    @property
    def total(self) -> int:
        return sum(f["score"] or 0 for f in self.factors)

    @property
    def available(self) -> int:
        return sum(1 for f in self.factors if f["score"] is not None)

    def action(self, labels=("Sell", "Hold", "Buy")) -> str:
        if self.available < 2:
            return "No signal"
        t = self.total
        if t >= 2:
            return labels[2]
        if t <= -2:
            return labels[0]
        return labels[1]

    def result(self, summary: dict, labels=("Sell", "Hold", "Buy")) -> dict:
        return {
            "action": self.action(labels),
            "score": self.total,
            "max_score": len(self.factors),
            "factors": self.factors,
            **summary,
        }


def fmt(x: float | None, suffix: str = "%", nd: int = 2) -> str:
    return "n/a" if x is None else f"{x:.{nd}f}{suffix}"


def stocks_signal(s: dict[str, Series], cpi_yoy: float | None) -> dict:
    sc = Scorer()
    sp = s["SP500"]

    ma200 = sp.moving_average(200)
    if sp and ma200:
        gap = (sp.last / ma200 - 1) * 100
        score = 1 if gap > 2 else (-1 if gap < -2 else 0)
        sc.add("Trend", score, f"S&P 500 is {fmt(abs(gap), '%', 1)} {'above' if gap >= 0 else 'below'} its 200-day average.")
    else:
        sc.add("Trend", None, "Not enough S&P 500 history.")

    r1 = sp.pct_change(365)
    if r1 is not None:
        score = -1 if r1 > 25 else (1 if r1 < -10 else 0)
        sc.add("Momentum vs. history", score,
               f"Trailing 1-year price return is {fmt(r1, '%', 1)} vs. a long-run average near {LONG_RUN['sp500_nominal_annual']:.0f}%. "
               + ("Gains this large often cool off." if score == -1 else "A deep drawdown has historically been a better entry point." if score == 1 else "Within a normal range."))
    else:
        sc.add("Momentum vs. history", None, "1-year return unavailable.")

    vix = s["VIXCLS"].last
    if vix is not None:
        score = 1 if vix > 30 else (-1 if vix < 13 else 0)
        sc.add("Fear gauge (VIX)", score,
               f"VIX is {vix:.1f}. " + ("High fear has often marked good long-term entry points." if score == 1
                                        else "Very low volatility can signal complacency." if score == -1 else "Normal range (13-30)."))
    else:
        sc.add("Fear gauge (VIX)", None, "VIX unavailable.")

    curve = s["T10Y3M"].last
    if curve is not None:
        score = -1 if curve < 0 else 0
        sc.add("Yield curve", score,
               f"10-year minus 3-month spread is {fmt(curve, ' pts')}. " + ("Inverted curves have preceded most US recessions." if curve < 0 else "Not inverted."))
    else:
        sc.add("Yield curve", None, "Spread unavailable.")

    hy = s["BAMLH0A0HYM2"]
    hy_chg = hy.change(91)
    if hy and hy_chg is not None:
        score = -1 if hy_chg >= 1.0 else (1 if hy_chg <= -0.75 else 0)
        sc.add("Credit stress", score,
               f"High-yield spread is {fmt(hy.last, ' pts')} ({'+' if hy_chg >= 0 else ''}{hy_chg:.2f} over 3 months). "
               + ("Rapidly widening spreads signal stress." if score == -1 else "Spreads are easing." if score == 1 else "Stable."))
    else:
        sc.add("Credit stress", None, "Credit spread unavailable.")

    returns = {
        "1y": rnd(r1, 1),
        "3y_annualized": rnd(sp.cagr(3), 1),
        "5y_annualized": rnd(sp.cagr(5), 1),
        "10y_annualized": rnd(sp.cagr(10), 1),
        "ytd": rnd(_ytd(sp), 1),
    }
    return sc.result({
        "title": "Stocks",
        "subject": "Broad US stock index funds (e.g. S&P 500)",
        "headline_metric": {"label": "S&P 500", "value": rnd(sp.last, 0), "as_of": sp.last_date},
        "returns": returns,
    })


def bonds_signal(s: dict[str, Series], cpi_yoy: float | None) -> dict:
    sc = Scorer()
    t10 = s["DGS10"]

    avg5 = t10.mean_since(5 * 365)
    if t10 and avg5 is not None:
        diff = t10.last - avg5
        score = 1 if diff > 0.5 else (-1 if diff < -0.5 else 0)
        sc.add("Yield vs. 5-year average", score,
               f"10-year yield is {fmt(t10.last)} vs. a 5-year average of {fmt(avg5)}. "
               + ("Higher-than-usual yields lock in more income." if score == 1 else "Yields are low relative to recent history." if score == -1 else "Close to average."))
    else:
        sc.add("Yield vs. 5-year average", None, "10-year yield unavailable.")

    real = s["DFII10"].last
    if real is not None:
        score = 1 if real > 1.5 else (-1 if real < 0.5 else 0)
        sc.add("Real yield", score,
               f"10-year TIPS real yield is {fmt(real)}. " + ("Strong after-inflation return." if score == 1 else "Little return after inflation." if score == -1 else "Moderate."))
    else:
        sc.add("Real yield", None, "TIPS yield unavailable.")

    ff_chg = s["DFF"].change(182)
    if ff_chg is not None:
        score = 1 if ff_chg <= -0.25 else (-1 if ff_chg >= 0.25 else 0)
        sc.add("Fed direction", score,
               f"Fed funds rate changed {'+' if ff_chg >= 0 else ''}{ff_chg:.2f} pts over 6 months. "
               + ("Rate cuts tend to lift bond prices." if score == 1 else "Rate hikes tend to push bond prices down." if score == -1 else "Roughly steady."))
    else:
        sc.add("Fed direction", None, "Fed funds unavailable.")

    if cpi_yoy is not None:
        score = -1 if cpi_yoy > 4 else (1 if cpi_yoy < 2.5 else 0)
        sc.add("Inflation", score,
               f"CPI inflation is {fmt(cpi_yoy, '%', 1)} year over year. "
               + ("High inflation erodes fixed payments." if score == -1 else "Tame inflation is friendly to bonds." if score == 1 else "Moderately elevated."))
    else:
        sc.add("Inflation", None, "CPI unavailable.")

    return sc.result({
        "title": "Bonds",
        "subject": "Intermediate-term US Treasury / high-quality bond funds",
        "headline_metric": {"label": "10-yr Treasury", "value": rnd(t10.last), "unit": "%", "as_of": t10.last_date},
    })


def cash_signal(s: dict[str, Series], cpi_yoy: float | None) -> dict:
    sc = Scorer()
    tb = s["DGS3MO"]

    if tb and cpi_yoy is not None:
        real = tb.last - cpi_yoy
        score = 1 if real > 1 else (-1 if real < 0 else 0)
        sc.add("Real cash yield", score,
               f"3-month T-bills pay {fmt(tb.last)} vs. {fmt(cpi_yoy, '%', 1)} inflation ({'+' if real >= 0 else ''}{real:.2f} real). "
               + ("Cash is beating inflation comfortably." if score == 1 else "Cash is losing to inflation." if score == -1 else "Roughly keeping pace."))
    else:
        sc.add("Real cash yield", None, "T-bill or CPI data unavailable.")

    curve = s["T10Y3M"].last
    if curve is not None:
        score = 1 if curve < 0 else (-1 if curve > 1.5 else 0)
        sc.add("Cash vs. long bonds", score,
               f"10-year minus 3-month spread is {fmt(curve, ' pts')}. "
               + ("Short-term cash yields more than long bonds." if score == 1 else "Long bonds pay a healthy premium over cash." if score == -1 else "Small difference."))
    else:
        sc.add("Cash vs. long bonds", None, "Spread unavailable.")

    ff_chg = s["DFF"].change(182)
    if ff_chg is not None:
        score = -1 if ff_chg <= -0.25 else (1 if ff_chg >= 0.25 else 0)
        sc.add("Fed direction", score,
               "Falling policy rates mean cash yields will drift lower." if score == -1
               else "Rising policy rates push cash yields up." if score == 1 else "Policy rate roughly steady.")
    else:
        sc.add("Fed direction", None, "Fed funds unavailable.")

    return sc.result({
        "title": "Cash",
        "subject": "High-yield savings, money market funds, T-bills",
        "headline_metric": {"label": "3-mo T-bill", "value": rnd(tb.last), "unit": "%", "as_of": tb.last_date},
    }, labels=("Underweight", "Hold", "Overweight"))


def monthly_payment(principal: float, annual_rate_pct: float, years: int = 30) -> float:
    r = annual_rate_pct / 100 / 12
    n = years * 12
    return principal / n if r == 0 else principal * r / (1 - (1 + r) ** -n)


def housing_signal(s: dict[str, Series]) -> dict:
    sc = Scorer()
    price = s["MSPUS"].last
    rate = s["MORTGAGE30US"].last
    income = s["MEHOINUSA672N"].last

    payment = share = None
    if price and rate and income:
        payment = monthly_payment(price * 0.8, rate)
        share = payment / (income / 12) * 100
        score = -1 if share > 35 else (1 if share < 25 else 0)
        sc.add("Affordability", score,
               f"Principal & interest on the median home (20% down) is about ${payment:,.0f}/mo, {share:.0f}% of median household income. "
               + ("Well above the ~25% historical norm." if score == -1 else "Historically affordable." if score == 1 else "Somewhat stretched."))
    else:
        sc.add("Affordability", None, "Price, rate or income data unavailable.")

    m = s["MORTGAGE30US"]
    avg10 = m.mean_since(10 * 365)
    if m and avg10 is not None:
        diff = m.last - avg10
        score = -1 if diff > 1 else (1 if diff < -0.5 else 0)
        sc.add("Mortgage rate vs. 10-year average", score,
               f"30-year rate is {fmt(m.last)} vs. a 10-year average of {fmt(avg10)}.")
    else:
        sc.add("Mortgage rate vs. 10-year average", None, "Mortgage rate unavailable.")

    hpi_yoy = s["CSUSHPINSA"].pct_change(365)
    if hpi_yoy is not None:
        score = -1 if hpi_yoy > 8 else (1 if hpi_yoy < 0 else 0)
        sc.add("Price momentum", score,
               f"Case-Shiller national prices are {'+' if hpi_yoy >= 0 else ''}{hpi_yoy:.1f}% year over year. "
               + ("Prices are running hot." if score == -1 else "Falling prices give buyers leverage." if score == 1 else "Moderate growth."))
    else:
        sc.add("Price momentum", None, "Home price index unavailable.")

    supply = s["MSACSR"].last
    if supply is not None:
        score = 1 if supply > 7 else (-1 if supply < 4 else 0)
        sc.add("Inventory", score,
               f"{supply:.1f} months' supply of new homes. "
               + ("A buyer's market (above ~7 months)." if score == 1 else "A tight seller's market (below ~4 months)." if score == -1 else "Balanced."))
    else:
        sc.add("Inventory", None, "Inventory data unavailable.")

    return sc.result({
        "title": "Housing",
        "subject": "Buying a home to live in (Sell = good time to sell / seller's market)",
        "headline_metric": {"label": "Median home price", "value": rnd(price, 0), "unit": "$", "as_of": s["MSPUS"].last_date},
        "affordability": {"monthly_payment": rnd(payment, 0), "share_of_income": rnd(share, 1)},
    })


def borrowing_advice(s: dict[str, Series]) -> dict:
    expected = LONG_RUN["sp500_nominal_annual"]
    loans = []
    for sid, kind in [("MORTGAGE30US", "mortgage"), ("MORTGAGE15US", "mortgage"),
                      ("TERMCBAUTO48NS", "auto"), ("TERMCBPER24NS", "personal"), ("TERMCBCCALLNS", "card")]:
        ser = s[sid]
        r = ser.last
        if r is None:
            verdict = "Data unavailable"
        elif r >= expected:
            verdict = "Pay this debt down before investing: the rate beats long-run stock returns."
        elif r >= 7:
            verdict = "Expensive: borrow only if needed, prefer shorter terms, prepay when you can."
        elif r >= 5:
            verdict = "Moderate: a reasonable mix of paying down and investing."
        else:
            verdict = "Cheap: no rush to prepay; investing extra cash has historically come out ahead."
        loans.append({
            "id": sid, "name": SERIES[sid], "rate": rnd(r), "as_of": ser.last_date,
            "change_1y": rnd(ser.change(365)), "verdict": verdict,
        })
    return {"title": "Borrowing", "loans": loans, "expected_stock_return": expected}


def allocation(signals: dict) -> dict:
    """Illustrative tilt for a moderate 60/30/10 investor."""
    stocks, bonds, cash = 60.0, 30.0, 10.0
    tilt = {"Buy": 1, "Overweight": 1, "Hold": 0, "No signal": 0, "Sell": -1, "Underweight": -1}
    stocks += 10 * tilt[signals["stocks"]["action"]]
    bonds += 5 * tilt[signals["bonds"]["action"]]
    cash += 5 * tilt[signals["cash"]["action"]]
    total = stocks + bonds + cash
    alloc = {k: round(v / total * 100) for k, v in (("stocks", stocks), ("bonds", bonds), ("cash", cash))}
    alloc["cash"] = 100 - alloc["stocks"] - alloc["bonds"]
    return {"baseline": {"stocks": 60, "bonds": 30, "cash": 10}, "suggested": alloc}


def _ytd(ser: Series) -> float | None:
    if not ser:
        return None
    last = _d(ser.last_date)
    base = ser.value_on_or_before(date(last.year - 1, 12, 31))
    return None if not base else (ser.last / base - 1) * 100


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def build(series: dict[str, Series], failures: list[str]) -> dict:
    cpi = series["CPIAUCSL"]
    cpi_yoy = cpi.pct_change(365)

    signals = {
        "stocks": stocks_signal(series, cpi_yoy),
        "bonds": bonds_signal(series, cpi_yoy),
        "cash": cash_signal(series, cpi_yoy),
        "housing": housing_signal(series),
    }

    def m(sid, nd=2):
        ser = series[sid]
        return {"id": sid, "name": SERIES[sid], "value": rnd(ser.last, nd), "as_of": ser.last_date,
                "change_1y": rnd(ser.change(365), nd), "spark": ser.spark()}

    markets = []
    for sid in ("SP500", "NASDAQCOM", "DJIA"):
        ser = series[sid]
        markets.append({"id": sid, "name": SERIES[sid], "value": rnd(ser.last, 0), "as_of": ser.last_date,
                        "ytd": rnd(_ytd(ser), 1), "1y": rnd(ser.pct_change(365), 1),
                        "3y": rnd(ser.cagr(3), 1), "5y": rnd(ser.cagr(5), 1),
                        "spark": ser.spark()})

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "Federal Reserve Bank of St. Louis (FRED)",
        "missing_series": failures,
        "signals": signals,
        "allocation": allocation(signals),
        "borrowing": borrowing_advice(series),
        "rates": [m(sid) for sid in ("DFF", "DGS3MO", "DGS2", "DGS10", "DGS30", "DFII10",
                                     "T10Y3M", "BAMLC0A0CM", "BAMLH0A0HYM2")],
        "loan_rates": [m(sid) for sid in ("MORTGAGE30US", "MORTGAGE15US", "TERMCBAUTO48NS",
                                          "TERMCBPER24NS", "TERMCBCCALLNS")],
        "markets": markets,
        "housing": [m(sid, 1) for sid in ("MSPUS", "ASPUS", "CSUSHPINSA", "MSACSR")],
        "economy": {
            "cpi_yoy": rnd(cpi_yoy, 1), "cpi_as_of": cpi.last_date,
            "unemployment": rnd(series["UNRATE"].last, 1), "unemployment_as_of": series["UNRATE"].last_date,
            "vix": rnd(series["VIXCLS"].last, 1),
        },
        "long_run_averages": LONG_RUN,
    }


def update_history(latest: dict) -> list:
    path = DATA_DIR / "history.json"
    history = json.loads(path.read_text()) if path.exists() else []
    today = latest["generated_at"][:10]
    entry = {"date": today, **{k: {"action": v["action"], "score": v["score"]} for k, v in latest["signals"].items()},
             "allocation": latest["allocation"]["suggested"]}
    history = [h for h in history if h["date"] != today] + [entry]
    return history[-HISTORY_MAX_DAYS:]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixtures", type=Path, help="Read <dir>/<SERIES>.csv instead of fetching")
    args = ap.parse_args()

    api_key = os.environ.get("FRED_API_KEY") or None
    print(f"Mode: {'fixtures' if args.fixtures else 'FRED API' if api_key else 'FRED public CSV'}")

    series: dict[str, Series] = {}
    failures: list[str] = []
    for sid in SERIES:
        try:
            obs = fetch_series(sid, api_key, args.fixtures)
            print(f"  {sid:15} {len(obs):6} obs, last {obs[-1] if obs else None}")
        except Exception as err:  # noqa: BLE001 - one bad series shouldn't sink the run
            print(f"  {sid:15} FAILED: {err}", file=sys.stderr)
            obs = []
        if not obs:
            failures.append(sid)
        series[sid] = Series(sid, obs)
        if not args.fixtures:
            time.sleep(0.3)  # be polite

    if len(failures) > len(SERIES) // 2:
        print(f"Too many series failed ({len(failures)}/{len(SERIES)}); keeping previous data.", file=sys.stderr)
        return 1

    latest = build(series, failures)
    DATA_DIR.mkdir(exist_ok=True)
    (DATA_DIR / "latest.json").write_text(json.dumps(latest, indent=1) + "\n")
    (DATA_DIR / "history.json").write_text(json.dumps(update_history(latest), indent=1) + "\n")
    for k, v in latest["signals"].items():
        print(f"{k:8} -> {v['action']} (score {v['score']})")
    print(f"Allocation: {latest['allocation']['suggested']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
