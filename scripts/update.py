#!/usr/bin/env python3
"""Fetch daily market data from FRED and compute the Investing Barometer signals.

Writes:
  data/latest.json    - everything the main page needs (metrics, signals, allocations)
  data/history.json   - one compact entry per day with each category's action
  data/backtest.json  - month-by-month signal history and a backtest of the rules

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
import bisect
import csv
import io
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
START_DATE = "1990-01-01"
HISTORY_MAX_DAYS = 730
TIMELINE_START = date(2010, 1, 1)

# Long-run reference figures (not fetched; widely cited historical averages).
LONG_RUN = {
    "sp500_nominal_annual": 10.0,  # S&P 500 total return, ~1926-present, approx.
    "sp500_real_annual": 7.0,      # after inflation, approx.
    "bonds_nominal_annual": 5.0,   # intermediate US government bonds, approx.
    "tbills_nominal_annual": 3.3,  # 3-month T-bills, approx.
}

# Backtest assumptions.
SP500_DIVIDEND_YIELD = 1.6   # % a year added to FRED's price-only S&P 500
BOND_DURATION = 8.0          # approx. duration of a 10-year Treasury

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
    "T10YIE": "10-year breakeven inflation",
    "T10Y3M": "10-year minus 3-month spread",
    "BAMLH0A0HYM2": "High-yield credit spread",
    "BAMLC0A0CM": "Investment-grade credit spread",
    # Stocks and other markets
    "SP500": "S&P 500",
    "NASDAQCOM": "Nasdaq Composite",
    "DJIA": "Dow Jones Industrial Average",
    "NIKKEI225": "Nikkei 225 (Japan)",
    "VIXCLS": "VIX volatility index",
    "DTWEXBGS": "US dollar index (broad)",
    "DCOILWTICO": "WTI crude oil",
    "PPIACO": "Producer prices: all commodities",
    "IR14270": "Gold import price index",
    "CBBTCUSD": "Bitcoin (Coinbase)",
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

# Category display order. "kind" sets the action labels.
CATEGORIES = [
    ("stocks", "US stocks"),
    ("intl", "International stocks"),
    ("bonds", "Treasury bonds"),
    ("tips", "TIPS"),
    ("corp", "Corporate bonds"),
    ("hy", "High-yield bonds"),
    ("reits", "Real estate (REITs)"),
    ("commodities", "Commodities"),
    ("gold", "Gold"),
    ("cash", "Cash"),
    ("bitcoin", "Bitcoin"),
    ("housing", "Housing"),
]

# Baseline portfolio weights (%) by risk profile. Housing is a personal decision, not a portfolio slice.
PROFILES = {
    "conservative": {
        "label": "Conservative",
        "blurb": "Prioritizes stability and income. Suits shorter timelines or low tolerance for losses.",
        "weights": {"stocks": 20, "intl": 8, "bonds": 30, "tips": 10, "corp": 10, "hy": 0,
                    "reits": 2, "commodities": 0, "gold": 3, "cash": 17, "bitcoin": 0},
        "core": {"stocks": 30, "bonds": 55, "cash": 15},
    },
    "moderate": {
        "label": "Moderate",
        "blurb": "A balance of growth and stability. A common choice for 10+ year goals.",
        "weights": {"stocks": 38, "intl": 15, "bonds": 15, "tips": 5, "corp": 7, "hy": 3,
                    "reits": 5, "commodities": 2, "gold": 3, "cash": 7, "bitcoin": 0},
        "core": {"stocks": 60, "bonds": 30, "cash": 10},
    },
    "aggressive": {
        "label": "Aggressive",
        "blurb": "Maximizes long-run growth and accepts large swings. Suits long timelines.",
        "weights": {"stocks": 50, "intl": 22, "bonds": 5, "tips": 0, "corp": 3, "hy": 5,
                    "reits": 7, "commodities": 3, "gold": 2, "cash": 1, "bitcoin": 2},
        "core": {"stocks": 85, "bonds": 12, "cash": 3},
    },
}
TILT = 0.15            # each net factor point moves a slice's weight by 15%, before re-normalizing
TILT_RANGE = (0.25, 1.75)  # a slice never drops below 25% or rises above 175% of its baseline


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #

def _get(url: str, retries: int = 3) -> bytes:
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "investing-barometer/1.0"})
            with urllib.request.urlopen(req, timeout=60) as resp:
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
        path = fixtures / f"{series_id}.csv"
        return parse_csv(path.read_text()) if path.exists() else []
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

class Series:
    def __init__(self, sid: str, obs: list[tuple[str, float]] | None = None, *, dates=None, vals=None):
        self.id = sid
        if dates is not None:
            self.dates, self.vals = dates, vals
        else:
            obs = sorted(obs or [])
            self.dates = [date.fromisoformat(d) for d, _ in obs]
            self.vals = [v for _, v in obs]

    def __bool__(self) -> bool:
        return bool(self.vals)

    def __len__(self) -> int:
        return len(self.vals)

    @property
    def last(self) -> float | None:
        return self.vals[-1] if self.vals else None

    @property
    def last_date(self) -> str | None:
        return self.dates[-1].isoformat() if self.dates else None

    def publication_lag(self) -> int:
        """Rough days between an observation's date and when it is published."""
        if len(self.dates) < 3:
            return 30
        gaps = sorted((self.dates[i + 1] - self.dates[i]).days for i in range(len(self.dates) - 1))
        gap = gaps[len(gaps) // 2]
        if gap <= 4:
            return 1       # daily
        if gap <= 10:
            return 3       # weekly
        if gap <= 40:
            return 45      # monthly
        if gap <= 100:
            return 120     # quarterly
        return 640         # annual (e.g. Census income, ~21 months later)

    def upto(self, when: date) -> "Series":
        i = bisect.bisect_right(self.dates, when)
        return Series(self.id, dates=self.dates[:i], vals=self.vals[:i])

    def value_on_or_before(self, when: date) -> float | None:
        i = bisect.bisect_right(self.dates, when)
        return self.vals[i - 1] if i else None

    def ago(self, days: int) -> float | None:
        if not self.vals:
            return None
        return self.value_on_or_before(self.dates[-1] - timedelta(days=days))

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
        if self.dates[0] > self.dates[-1] - timedelta(days=int(365.25 * years) - 10):
            return None
        return ((self.last / prev) ** (1 / years) - 1) * 100

    def mean_since(self, days: int) -> float | None:
        if not self.vals:
            return None
        i = bisect.bisect_left(self.dates, self.dates[-1] - timedelta(days=days))
        vals = self.vals[i:]
        return sum(vals) / len(vals) if vals else None

    def moving_average(self, n: int) -> float | None:
        if len(self.vals) < n:
            return None
        return sum(self.vals[-n:]) / n

    def spark(self, days: int = 5 * 365, points: int = 120) -> list[list]:
        """Down-sampled [date, value] pairs for a sparkline."""
        if not self.vals:
            return []
        i = bisect.bisect_left(self.dates, self.dates[-1] - timedelta(days=days))
        sub = list(zip(self.dates[i:], self.vals[i:]))
        if len(sub) > points:
            step = len(sub) / points
            sub = [sub[int(k * step)] for k in range(points)] + [sub[-1]]
        return [[d.isoformat(), round(v, 4)] for d, v in sub]


def rnd(x: float | None, nd: int = 2) -> float | None:
    return None if x is None else round(x, nd)


def fmt(x: float | None, suffix: str = "%", nd: int = 2) -> str:
    return "n/a" if x is None else f"{x:.{nd}f}{suffix}"


def sgn(x: float, nd: int = 2) -> str:
    return f"{'+' if x >= 0 else ''}{x:.{nd}f}"


# --------------------------------------------------------------------------- #
# Strategy rules
# --------------------------------------------------------------------------- #

class Scorer:
    """Collects factors that each score -1, 0 or +1 and turns them into an action."""

    def __init__(self):
        self.factors: list[dict] = []

    def add(self, name: str, score: int | None, detail: str):
        self.factors.append({"name": name, "score": score, "detail": detail})

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


# Shared factor helpers ------------------------------------------------------ #

def trend_factor(sc: Scorer, ser: Series, label: str, band: float = 2.0, name: str = "Trend"):
    ma200 = ser.moving_average(200)
    if ser and ma200:
        gap = (ser.last / ma200 - 1) * 100
        score = 1 if gap > band else (-1 if gap < -band else 0)
        sc.add(name, score, f"{label} is {abs(gap):.1f}% {'above' if gap >= 0 else 'below'} its 200-day average.")
    else:
        sc.add(name, None, f"Not enough {label} history.")


def fed_direction(s: dict[str, Series]) -> float | None:
    return s["DFF"].change(182)


def fed_factor(sc: Scorer, s: dict[str, Series], cutting_is_good: bool, good: str, bad: str):
    chg = fed_direction(s)
    if chg is None:
        sc.add("Fed direction", None, "Fed funds unavailable.")
        return
    cutting = chg <= -0.25
    hiking = chg >= 0.25
    if cutting_is_good:
        score = 1 if cutting else (-1 if hiking else 0)
    else:
        score = -1 if cutting else (1 if hiking else 0)
    msg = f"Fed funds rate changed {sgn(chg)} pts over 6 months. "
    msg += (good if score == 1 else bad if score == -1 else "Roughly steady.")
    sc.add("Fed direction", score, msg)


def dollar_factor(sc: Scorer, s: dict[str, Series], weak_is_good: bool = True, why: str = ""):
    chg = s["DTWEXBGS"].pct_change(182)
    if chg is None:
        sc.add("US dollar", None, "Dollar index unavailable.")
        return
    weak, strong = chg < -3, chg > 3
    score = (1 if weak else -1 if strong else 0) if weak_is_good else (-1 if weak else 1 if strong else 0)
    sc.add("US dollar", score,
           f"The broad dollar index moved {sgn(chg, 1)}% over 6 months. "
           + ("A weakening dollar " + why if weak else "A strengthening dollar works against this." if strong else "Little change."))


def credit_stress_factor(sc: Scorer, s: dict[str, Series]):
    hy = s["BAMLH0A0HYM2"]
    chg = hy.change(91)
    if hy and chg is not None:
        score = -1 if chg >= 1.0 else (1 if chg <= -0.75 else 0)
        sc.add("Credit stress", score,
               f"High-yield spread is {fmt(hy.last, ' pts')} ({sgn(chg)} over 3 months). "
               + ("Rapidly widening spreads signal stress." if score == -1 else "Spreads are easing." if score == 1 else "Stable."))
    else:
        sc.add("Credit stress", None, "Credit spread unavailable (FRED only carries 3 years of ICE data).")


def sahm_like(s: dict[str, Series]) -> float | None:
    """3-month average unemployment minus the lowest 3-month average of the prior 12 months."""
    u = s["UNRATE"].vals
    if len(u) < 15:
        return None
    avgs = [sum(u[i - 2:i + 1]) / 3 for i in range(len(u) - 13, len(u))]
    return avgs[-1] - min(avgs[:-1])


def recession_factor(sc: Scorer, s: dict[str, Series]):
    sahm = sahm_like(s)
    curve = s["T10Y3M"].last
    if sahm is None and curve is None:
        sc.add("Recession risk", None, "Unemployment and yield-curve data unavailable.")
        return
    flags = []
    if sahm is not None and sahm >= 0.5:
        flags.append(f"unemployment has risen {sahm:.1f} pts off its low")
    if curve is not None and curve < 0:
        flags.append("the yield curve is inverted")
    if flags:
        sc.add("Recession risk", -1, "Elevated: " + " and ".join(flags) + ". Defaults rise in recessions.")
    else:
        sc.add("Recession risk", 0, "No recession warning from unemployment or the yield curve.")


# Category rules ------------------------------------------------------------- #

def stocks_signal(s: dict[str, Series], cpi_yoy: float | None) -> dict:
    sc = Scorer()
    sp = s["SP500"]
    # Trend following (e.g. Faber 2007): above the ~10-month / 200-day average is risk-on, below is risk-off.
    trend_factor(sc, sp, "S&P 500", band=0.0)

    # Time-series momentum (Moskowitz, Ooi & Pedersen 2012): 12-month return vs. what cash paid.
    r1 = sp.pct_change(365)
    tb = s["DGS3MO"].mean_since(365)
    if r1 is not None and tb is not None:
        excess = r1 - tb
        score = 1 if excess > 0 else -1
        sc.add("12-month momentum", score,
               f"Over 12 months the S&P 500 returned {r1:.1f}% vs. about {tb:.1f}% from T-bills. "
               + ("Stocks are beating cash, and that tends to persist." if score == 1 else "Stocks are trailing cash, a classic risk-off sign."))
    else:
        sc.add("12-month momentum", None, "1-year return unavailable.")

    # Volatility management (Moreira & Muir 2017): cut risk when volatility is high, since returns
    # don't rise enough to compensate. (Earlier versions treated a high VIX as a contrarian buy.)
    vix = s["VIXCLS"].last
    if vix is not None:
        score = -1 if vix > 30 else 0
        sc.add("Volatility (VIX)", score,
               f"VIX is {vix:.1f}. " + ("Very high volatility: smaller positions keep risk steady." if score == -1
                                        else "Normal or calm (below 30)."))
    else:
        sc.add("Volatility (VIX)", None, "VIX unavailable.")

    # Recession risk: rising unemployment (Sahm-style), or an inverted curve that the market trend confirms.
    # An inverted curve alone was a poor timing tool (inverted for most of 2022-24 while stocks rallied).
    sahm = sahm_like(s)
    curve = s["T10Y3M"].last
    ma = sp.moving_average(200)
    below_trend = bool(sp and ma and sp.last < ma)
    if sahm is None and curve is None:
        sc.add("Recession risk", None, "Unemployment and yield-curve data unavailable.")
    elif sahm is not None and sahm >= 0.5:
        sc.add("Recession risk", -1, f"Unemployment has risen {sahm:.1f} pts off its low, the classic early-recession signal.")
    elif curve is not None and curve < 0 and below_trend:
        sc.add("Recession risk", -1, f"The yield curve is inverted ({fmt(curve, ' pts')}) and stocks have broken below trend.")
    else:
        note = f"Yield curve {fmt(curve, ' pts')}" if curve is not None else "Yield curve n/a"
        sc.add("Recession risk", 0, f"{note}; no confirmed recession warning.")

    credit_stress_factor(sc, s)

    return sc.result({
        "subject": "Broad US stock index funds (S&P 500, total market)",
        "headline_metric": {"label": "S&P 500", "value": rnd(sp.last, 0), "as_of": sp.last_date},
        "returns": {
            "1y": rnd(r1, 1), "3y_annualized": rnd(sp.cagr(3), 1), "5y_annualized": rnd(sp.cagr(5), 1),
            "10y_annualized": rnd(sp.cagr(10), 1), "ytd": rnd(_ytd(sp), 1),
        },
    })


def intl_signal(s: dict[str, Series], cpi_yoy: float | None) -> dict:
    sc = Scorer()
    dollar_factor(sc, s, weak_is_good=True, why="boosts foreign returns for US investors.")
    nk = s["NIKKEI225"]
    trend_factor(sc, nk, "The Nikkei 225 (a developed-market proxy)", name="Foreign trend")

    nk1, sp1 = nk.pct_change(365), s["SP500"].pct_change(365)
    if nk1 is not None and sp1 is not None:
        rel = nk1 - sp1
        score = 1 if rel > 5 else (-1 if rel < -10 else 0)
        sc.add("Relative strength", score,
               f"Over 1 year the Nikkei returned {nk1:.1f}% vs. {sp1:.1f}% for the S&P 500. "
               + ("Foreign markets are leading." if score == 1 else "US markets are leading by a wide margin." if score == -1 else "Roughly even."))
    else:
        sc.add("Relative strength", None, "Index returns unavailable.")

    return sc.result({
        "subject": "Developed and emerging-market index funds outside the US",
        "headline_metric": {"label": "Nikkei 225", "value": rnd(nk.last, 0), "as_of": nk.last_date},
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

    fed_factor(sc, s, True, "Rate cuts tend to lift bond prices.", "Rate hikes tend to push bond prices down.")

    avg1 = t10.mean_since(365)
    if t10 and avg1 is not None:
        diff = t10.last - avg1
        score = 1 if diff < -0.25 else (-1 if diff > 0.25 else 0)
        sc.add("Bond trend", score,
               f"The 10-year yield is {sgn(diff)} pts vs. its 12-month average. "
               + ("Falling yields mean rising bond prices." if score == 1 else "Rising yields mean falling bond prices." if score == -1 else "Sideways."))
    else:
        sc.add("Bond trend", None, "Yield history unavailable.")

    if cpi_yoy is not None:
        score = -1 if cpi_yoy > 4 else (1 if cpi_yoy < 2.5 else 0)
        sc.add("Inflation", score,
               f"CPI inflation is {cpi_yoy:.1f}% year over year. "
               + ("High inflation erodes fixed payments." if score == -1 else "Tame inflation is friendly to bonds." if score == 1 else "Moderately elevated."))
    else:
        sc.add("Inflation", None, "CPI unavailable.")

    return sc.result({
        "subject": "Intermediate-term US Treasury bond funds",
        "headline_metric": {"label": "10-yr Treasury", "value": rnd(t10.last), "unit": "%", "as_of": t10.last_date},
    })


def tips_signal(s: dict[str, Series], cpi_yoy: float | None) -> dict:
    sc = Scorer()
    real = s["DFII10"]
    if real:
        score = 1 if real.last > 2 else (-1 if real.last < 0.5 else 0)
        sc.add("Real yield", score, f"10-year TIPS pay {fmt(real.last)} above inflation. "
               + ("Historically generous." if score == 1 else "Very little real return." if score == -1 else "Reasonable."))
    else:
        sc.add("Real yield", None, "TIPS yield unavailable.")

    be = s["T10YIE"].last
    if be is not None and cpi_yoy is not None:
        gap = be - cpi_yoy
        score = 1 if gap < -0.25 else (-1 if gap > 0.75 else 0)
        sc.add("Inflation protection price", score,
               f"Markets expect {be:.2f}% inflation (breakeven) vs. {cpi_yoy:.1f}% actual. "
               + ("Protection is cheap relative to current inflation." if score == 1 else "Protection is pricey relative to current inflation." if score == -1 else "Fairly priced."))
    else:
        sc.add("Inflation protection price", None, "Breakeven or CPI data unavailable.")

    if be is not None:
        score = 1 if be < 2.0 else (-1 if be > 2.75 else 0)
        sc.add("Breakeven level", score,
               f"10-year breakeven inflation is {be:.2f}%. "
               + ("Below the Fed's 2% goal, so TIPS win if inflation merely hits target." if score == 1 else "Markets already price in high inflation." if score == -1 else "Near normal (2-2.75%)."))
    else:
        sc.add("Breakeven level", None, "Breakeven data unavailable.")

    return sc.result({
        "subject": "Treasury Inflation-Protected Securities funds",
        "headline_metric": {"label": "10-yr real yield", "value": rnd(real.last), "unit": "%", "as_of": real.last_date},
    })


def corp_signal(s: dict[str, Series], cpi_yoy: float | None) -> dict:
    sc = Scorer()
    ig = s["BAMLC0A0CM"]
    if ig:
        score = 1 if ig.last > 1.6 else (-1 if ig.last < 1.0 else 0)
        sc.add("Credit spread", score,
               f"Investment-grade bonds pay {ig.last:.2f} pts over Treasuries. "
               + ("Well paid for the extra risk." if score == 1 else "Tight: little extra pay for credit risk." if score == -1 else "Normal."))
    else:
        sc.add("Credit spread", None, "Spread unavailable.")

    t10 = s["DGS10"]
    avg5 = t10.mean_since(5 * 365)
    if t10 and avg5 is not None:
        diff = t10.last - avg5
        score = 1 if diff > 0.5 else (-1 if diff < -0.5 else 0)
        sc.add("Base yield", score, f"Treasury yields are {sgn(diff)} pts vs. their 5-year average, which sets the floor for corporate yields.")
    else:
        sc.add("Base yield", None, "Treasury yield unavailable.")

    recession_factor(sc, s)
    return sc.result({
        "subject": "Investment-grade corporate bond funds",
        "headline_metric": {"label": "IG spread", "value": rnd(ig.last), "unit": " pts", "as_of": ig.last_date},
    })


def hy_signal(s: dict[str, Series], cpi_yoy: float | None) -> dict:
    sc = Scorer()
    hy = s["BAMLH0A0HYM2"]
    if hy:
        score = 1 if hy.last > 6 else (-1 if hy.last < 3.5 else 0)
        sc.add("Credit spread", score,
               f"High-yield bonds pay {hy.last:.2f} pts over Treasuries. "
               + ("Wide spreads have historically paid off." if score == 1 else "Tight: little cushion against defaults." if score == -1 else "Normal."))
    else:
        sc.add("Credit spread", None, "Spread unavailable.")
    credit_stress_factor(sc, s)
    recession_factor(sc, s)
    return sc.result({
        "subject": "High-yield (junk) corporate bond funds",
        "headline_metric": {"label": "HY spread", "value": rnd(hy.last), "unit": " pts", "as_of": hy.last_date},
    })


def reits_signal(s: dict[str, Series], cpi_yoy: float | None) -> dict:
    sc = Scorer()
    chg = s["DGS10"].change(182)
    if chg is not None:
        score = 1 if chg <= -0.5 else (-1 if chg >= 0.5 else 0)
        sc.add("Rate direction", score,
               f"The 10-year yield moved {sgn(chg)} pts over 6 months. "
               + ("Falling rates lift property values and REIT prices." if score == 1 else "Rising rates weigh on REITs." if score == -1 else "Little change."))
    else:
        sc.add("Rate direction", None, "Treasury yield unavailable.")
    fed_factor(sc, s, True, "Cheaper financing helps property owners.", "Costlier financing hurts property owners.")
    credit_stress_factor(sc, s)
    return sc.result({
        "subject": "Real estate investment trust (REIT) index funds",
        "headline_metric": {"label": "10-yr Treasury", "value": rnd(s["DGS10"].last), "unit": "%", "as_of": s["DGS10"].last_date},
    })


def commodities_signal(s: dict[str, Series], cpi_yoy: float | None) -> dict:
    sc = Scorer()
    oil = s["DCOILWTICO"]
    trend_factor(sc, oil, "WTI crude oil", band=5, name="Oil trend")
    ppi = s["PPIACO"].pct_change(365)
    if ppi is not None:
        score = 1 if ppi > 3 else (-1 if ppi < -3 else 0)
        sc.add("Commodity prices", score, f"Producer prices for all commodities are {sgn(ppi, 1)}% over a year. "
               + ("Rising broadly." if score == 1 else "Falling broadly." if score == -1 else "Flat-ish."))
    else:
        sc.add("Commodity prices", None, "Commodity PPI unavailable.")
    if cpi_yoy is not None:
        score = 1 if cpi_yoy > 3.5 else (-1 if cpi_yoy < 2 else 0)
        sc.add("Inflation hedge demand", score, f"CPI inflation is {cpi_yoy:.1f}%. "
               + ("Commodities tend to shine when inflation runs hot." if score == 1 else "Low inflation reduces the hedge's appeal." if score == -1 else "Moderate."))
    else:
        sc.add("Inflation hedge demand", None, "CPI unavailable.")
    dollar_factor(sc, s, weak_is_good=True, why="makes dollar-priced commodities cheaper globally, supporting prices.")
    return sc.result({
        "subject": "Broad commodity index funds (energy, metals, agriculture)",
        "headline_metric": {"label": "WTI oil", "value": rnd(oil.last), "unit": "$", "as_of": oil.last_date},
    })


def gold_signal(s: dict[str, Series], cpi_yoy: float | None) -> dict:
    sc = Scorer()
    rchg = s["DFII10"].change(182)
    if rchg is not None:
        score = 1 if rchg <= -0.3 else (-1 if rchg >= 0.3 else 0)
        sc.add("Real-yield direction", score, f"10-year real yields moved {sgn(rchg)} pts over 6 months. "
               + ("Falling real yields make non-yielding gold more attractive." if score == 1 else "Rising real yields raise the cost of holding gold." if score == -1 else "Little change."))
    else:
        sc.add("Real-yield direction", None, "TIPS yield unavailable.")
    dollar_factor(sc, s, weak_is_good=True, why="tends to lift gold prices.")
    g = s["IR14270"]
    g1 = g.pct_change(365)
    if g1 is not None:
        score = -1 if g1 > 30 else (1 if g1 < -5 else 0)
        sc.add("Price momentum", score, f"Gold import prices are {sgn(g1, 1)}% over a year. "
               + ("A run this big is often stretched." if score == -1 else "A pullback can be a better entry." if score == 1 else "Normal range."))
    else:
        sc.add("Price momentum", None, "Gold price index unavailable.")
    return sc.result({
        "subject": "Gold funds or bullion",
        "headline_metric": {"label": "Gold price index", "value": rnd(g.last, 1), "as_of": g.last_date},
    })


def cash_signal(s: dict[str, Series], cpi_yoy: float | None) -> dict:
    sc = Scorer()
    tb = s["DGS3MO"]
    if tb and cpi_yoy is not None:
        real = tb.last - cpi_yoy
        score = 1 if real > 1 else (-1 if real < 0 else 0)
        sc.add("Real cash yield", score,
               f"3-month T-bills pay {fmt(tb.last)} vs. {cpi_yoy:.1f}% inflation ({sgn(real)} real). "
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

    fed_factor(sc, s, False, "Rising policy rates push cash yields up.", "Falling policy rates mean cash yields will drift lower.")
    return sc.result({
        "subject": "High-yield savings, money market funds, T-bills",
        "headline_metric": {"label": "3-mo T-bill", "value": rnd(tb.last), "unit": "%", "as_of": tb.last_date},
    }, labels=("Underweight", "Hold", "Overweight"))


def bitcoin_signal(s: dict[str, Series], cpi_yoy: float | None) -> dict:
    sc = Scorer()
    btc = s["CBBTCUSD"]
    trend_factor(sc, btc, "Bitcoin", band=5)
    r1 = btc.pct_change(365)
    if r1 is not None:
        score = -1 if r1 > 150 else (1 if r1 < -50 else 0)
        sc.add("Momentum", score, f"Bitcoin is {sgn(r1, 0)}% over a year. "
               + ("Euphoric runs have often ended in sharp drops." if score == -1 else "Deep drawdowns have preceded past recoveries." if score == 1 else "Not extreme by Bitcoin standards."))
    else:
        sc.add("Momentum", None, "Price history unavailable.")
    fed_factor(sc, s, True, "Easier money has tended to help speculative assets.", "Tighter money has tended to hurt speculative assets.")
    return sc.result({
        "subject": "Speculative. Only money you can afford to lose entirely.",
        "headline_metric": {"label": "Bitcoin", "value": rnd(btc.last, 0), "unit": "$", "as_of": btc.last_date},
    })


def monthly_payment(principal: float, annual_rate_pct: float, years: int = 30) -> float:
    r = annual_rate_pct / 100 / 12
    n = years * 12
    return principal / n if r == 0 else principal * r / (1 - (1 + r) ** -n)


def housing_signal(s: dict[str, Series], cpi_yoy: float | None = None) -> dict:
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
        sc.add("Mortgage rate vs. 10-year average", score, f"30-year rate is {fmt(m.last)} vs. a 10-year average of {fmt(avg10)}.")
    else:
        sc.add("Mortgage rate vs. 10-year average", None, "Mortgage rate unavailable.")

    hpi_yoy = s["CSUSHPINSA"].pct_change(365)
    if hpi_yoy is not None:
        score = -1 if hpi_yoy > 8 else (1 if hpi_yoy < 0 else 0)
        sc.add("Price momentum", score,
               f"Case-Shiller national prices are {sgn(hpi_yoy, 1)}% year over year. "
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
        "subject": "Buying a home to live in (Sell = seller's market)",
        "headline_metric": {"label": "Median home price", "value": rnd(price, 0), "unit": "$", "as_of": s["MSPUS"].last_date},
        "affordability": {"monthly_payment": rnd(payment, 0), "share_of_income": rnd(share, 1)},
        "mortgage_rate": rnd(rate),
        "mortgage_rate_10y_avg": rnd(avg10),
    })


SIGNAL_FUNCS = {
    "stocks": stocks_signal, "intl": intl_signal, "bonds": bonds_signal, "tips": tips_signal,
    "corp": corp_signal, "hy": hy_signal, "reits": reits_signal, "commodities": commodities_signal,
    "gold": gold_signal, "cash": cash_signal, "bitcoin": bitcoin_signal, "housing": housing_signal,
}


def compute_signals(s: dict[str, Series]) -> dict:
    cpi_yoy = s["CPIAUCSL"].pct_change(365)
    out = {}
    for key, title in CATEGORIES:
        sig = SIGNAL_FUNCS[key](s, cpi_yoy)
        sig["title"] = title
        out[key] = sig
    return out


def borrowing_advice(s: dict[str, Series]) -> dict:
    expected = LONG_RUN["sp500_nominal_annual"]
    loans = []
    for sid in ("MORTGAGE30US", "MORTGAGE15US", "TERMCBAUTO48NS", "TERMCBPER24NS", "TERMCBCCALLNS"):
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
        loans.append({"id": sid, "name": SERIES[sid], "rate": rnd(r), "as_of": ser.last_date,
                      "change_1y": rnd(ser.change(365)), "verdict": verdict})
    return {"loans": loans, "expected_stock_return": expected}


# --------------------------------------------------------------------------- #
# Allocation
# --------------------------------------------------------------------------- #



def round_to_100(weights: dict[str, float]) -> dict[str, int]:
    """Round to whole percents that sum to exactly 100 (largest-remainder method)."""
    total = sum(weights.values()) or 1
    scaled = {k: v / total * 100 for k, v in weights.items()}
    floor = {k: int(math.floor(v)) for k, v in scaled.items()}
    short = 100 - sum(floor.values())
    for k in sorted(scaled, key=lambda k: scaled[k] - floor[k], reverse=True)[:short]:
        floor[k] += 1
    return floor


def multiplier(score: int | None) -> float:
    lo, hi = TILT_RANGE
    return min(hi, max(lo, 1 + TILT * (score or 0)))


DEFENSIVE = ("bonds", "tips", "corp", "cash")


def tilted(weights: dict[str, float], scores: dict[str, int | None]) -> dict[str, int]:
    """Scale each growth slice by its net signal score and let the defensive slices absorb the difference.

    Growth slices (stocks, REITs, commodities...) are set to baseline x multiplier. Defensive slices are
    tilted by their own scores and then scaled to fill whatever is left, so cutting stocks really does move
    money into bonds and cash instead of being undone by re-normalizing.
    """
    growth = {k: w * multiplier(scores.get(k)) for k, w in weights.items() if k not in DEFENSIVE}
    defense = {k: w * multiplier(scores.get(k)) for k, w in weights.items() if k in DEFENSIVE}
    g_total, d_total = sum(growth.values()), sum(defense.values())
    if g_total > 100 or d_total == 0:
        return round_to_100({**growth, **defense})
    room = 100 - g_total
    return round_to_100({**growth, **{k: v / d_total * room for k, v in defense.items()}})


def allocations(signals: dict) -> dict:
    scores = {k: (v["score"] if v["action"] != "No signal" else 0) for k, v in signals.items()}
    return {
        name: {"label": p["label"], "blurb": p["blurb"], "baseline": p["weights"],
               "suggested": tilted(p["weights"], scores)}
        for name, p in PROFILES.items()
    }


# --------------------------------------------------------------------------- #
# Backtest and monthly signal history
# --------------------------------------------------------------------------- #

def month_ends(start: date, end: date) -> list[date]:
    out = []
    y, m = start.year, start.month
    while True:
        nxt = date(y + (m == 12), m % 12 + 1, 1)
        d = nxt - timedelta(days=1)
        if d > end:
            break
        out.append(d)
        y, m = nxt.year, nxt.month
    return out


def as_of(series: dict[str, Series], when: date, lags: dict[str, int]) -> dict[str, Series]:
    """What a reader would have seen on `when`, allowing for publication delays."""
    return {sid: ser.upto(when - timedelta(days=lags[sid])) for sid, ser in series.items()}


def max_drawdown(values: list[float]) -> float:
    peak, worst = values[0], 0.0
    for v in values:
        peak = max(peak, v)
        worst = min(worst, v / peak - 1)
    return worst * 100


def perf_stats(values: list[float], monthly: list[float]) -> dict:
    years = len(monthly) / 12
    cagr = ((values[-1] / values[0]) ** (1 / years) - 1) * 100 if years > 0 else None
    mean = sum(monthly) / len(monthly) if monthly else 0
    var = sum((r - mean) ** 2 for r in monthly) / (len(monthly) - 1) if len(monthly) > 1 else 0
    return {"final": round(values[-1]), "cagr": rnd(cagr, 2), "volatility": rnd(math.sqrt(var) * math.sqrt(12) * 100, 2),
            "max_drawdown": rnd(max_drawdown(values), 2)}


def sharpe(monthly: list[float], cash: list[float]) -> float | None:
    if len(monthly) < 12:
        return None
    ex = [r - c for r, c in zip(monthly, cash)]
    mean = sum(ex) / len(ex)
    sd = math.sqrt(sum((x - mean) ** 2 for x in ex) / (len(ex) - 1))
    return None if sd == 0 else mean / sd * math.sqrt(12)


BACKTESTS = {
    # key: (stock index series, dividend yield %, first month, label)
    "recent": ("SP500", SP500_DIVIDEND_YIELD, date(2016, 11, 1), "S&P 500, last ~10 years"),
    "long": ("NASDAQCOM", 1.0, date(1990, 12, 1), "Nasdaq Composite since 1991"),
}


def run_backtest(series: dict[str, Series], lags: dict[str, int], stock_sid: str, div_yield: float,
                 first: date, last: date) -> dict | None:
    """Monthly-rebalanced stocks / 10-year Treasuries / cash, with the stock signal computed from `stock_sid`."""
    sim = dict(series)
    sim["SP500"] = series[stock_sid]
    months = month_ends(first, last)
    stock, t10, t3m = series[stock_sid], series["DGS10"], series["DGS3MO"]
    rows = []
    for i in range(1, len(months)):
        d0, d1 = months[i - 1], months[i]
        seen = as_of(sim, d0, lags)
        cpi_yoy = seen["CPIAUCSL"].pct_change(365)
        sig = {k: SIGNAL_FUNCS[k](seen, cpi_yoy) for k in ("stocks", "bonds", "cash")}
        p0, p1 = stock.value_on_or_before(d0), stock.value_on_or_before(d1)
        y0, y1 = t10.value_on_or_before(d0), t10.value_on_or_before(d1)
        c0 = t3m.value_on_or_before(d0)
        if None in (p0, p1, y0, y1, c0) or sig["stocks"]["action"] == "No signal":
            continue
        r = {"stocks": p1 / p0 - 1 + div_yield / 100 / 12,
             "bonds": y0 / 100 / 12 - BOND_DURATION * (y1 - y0) / 100,
             "cash": c0 / 100 / 12}
        rows.append((d1, {k: v["score"] for k, v in sig.items()}, r))
    if not rows:
        return None

    out = {"stock_index": SERIES[stock_sid], "start": rows[0][0].isoformat(), "end": rows[-1][0].isoformat(),
           "months": len(rows), "profiles": {}}
    cash = [r["cash"] for _, _, r in rows]
    for name, p in PROFILES.items():
        core = p["core"]
        curves = {"strategy": [10000.0], "static": [10000.0], "sp500": [10000.0]}
        rets = {k: [] for k in curves}
        stock_w = []
        for _, scores, r in rows:
            w_tilt = tilted(core, scores)
            stock_w.append(w_tilt["stocks"])
            for key, w in (("strategy", w_tilt), ("static", core), ("sp500", {"stocks": 100, "bonds": 0, "cash": 0})):
                ret = sum(w[k] / 100 * r[k] for k in r)
                rets[key].append(ret)
                curves[key].append(curves[key][-1] * (1 + ret))
        stats = {k: perf_stats(curves[k], rets[k]) for k in curves}
        for k in stats:
            stats[k]["sharpe"] = rnd(sharpe(rets[k], cash), 2)
        out["profiles"][name] = {
            "core": core,
            "stats": stats,
            "stock_weight": {"min": min(stock_w), "max": max(stock_w), "avg": round(sum(stock_w) / len(stock_w))},
            "curve": [[(rows[0][0] - timedelta(days=31)).isoformat()[:7] if j == 0 else rows[j - 1][0].isoformat()[:7],
                       round(curves["strategy"][j]), round(curves["static"][j]), round(curves["sp500"][j]),
                       None if j == 0 else stock_w[j - 1]]
                      for j in range(len(rows) + 1)],
        }
    return out


def backtest(series: dict[str, Series]) -> dict:
    lags = {sid: ser.publication_lag() for sid, ser in series.items()}
    last = max((ser.dates[-1] for ser in series.values() if ser), default=date.today())

    timeline = []
    for d in month_ends(TIMELINE_START, last):
        sigs = compute_signals(as_of(series, d, lags))
        timeline.append({"date": d.isoformat(), **{k: {"a": v["action"], "s": v["score"]} for k, v in sigs.items()}})

    periods = {}
    for key, (sid, dy, first, label) in BACKTESTS.items():
        res = run_backtest(series, lags, sid, dy, first, last)
        if res:
            res["label"] = label
            periods[key] = res

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "timeline": timeline,
        "backtest": {
            "periods": periods,
            "assumptions": {
                "bond_duration": BOND_DURATION, "tilt_per_point": TILT, "tilt_range": TILT_RANGE,
                "notes": [
                    "Rebalanced monthly using only data that would have been published by each month-end.",
                    "The rules use textbook settings (200-day trend, 12-month momentum) and were not fitted to this data. "
                    "The Nasdaq period is an out-of-sample check: a different index and 25 more years, including the 2000-02 and 2008 crashes.",
                    f"Stocks: index price change plus an assumed dividend yield ({SP500_DIVIDEND_YIELD}% S&P 500, 1.0% Nasdaq).",
                    "Bonds: a 10-year Treasury approximation (yield income minus duration times the yield change).",
                    "Cash: the 3-month T-bill yield.",
                    "No taxes, fees or trading costs. Uses today's revised data, not the figures first reported.",
                ],
            },
        },
    }


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def _ytd(ser: Series) -> float | None:
    if not ser:
        return None
    base = ser.value_on_or_before(date(ser.dates[-1].year - 1, 12, 31))
    return None if not base else (ser.last / base - 1) * 100


def build(series: dict[str, Series], failures: list[str]) -> dict:
    cpi = series["CPIAUCSL"]
    cpi_yoy = cpi.pct_change(365)
    signals = compute_signals(series)

    def m(sid, nd=2):
        ser = series[sid]
        return {"id": sid, "name": SERIES[sid], "value": rnd(ser.last, nd), "as_of": ser.last_date,
                "change_1y": rnd(ser.change(365), nd), "spark": ser.spark()}

    markets = []
    for sid in ("SP500", "NASDAQCOM", "DJIA", "NIKKEI225", "DCOILWTICO", "CBBTCUSD"):
        ser = series[sid]
        markets.append({"id": sid, "name": SERIES[sid], "value": rnd(ser.last, 0 if sid != "DCOILWTICO" else 2),
                        "as_of": ser.last_date, "ytd": rnd(_ytd(ser), 1), "1y": rnd(ser.pct_change(365), 1),
                        "3y": rnd(ser.cagr(3), 1), "5y": rnd(ser.cagr(5), 1), "spark": ser.spark()})

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "Federal Reserve Bank of St. Louis (FRED)",
        "missing_series": failures,
        "categories": [k for k, _ in CATEGORIES],
        "signals": signals,
        "profiles": allocations(signals),
        "tilt": TILT,
        "tilt_range": TILT_RANGE,
        "borrowing": borrowing_advice(series),
        "rates": [m(sid) for sid in ("DFF", "DGS3MO", "DGS2", "DGS10", "DGS30", "DFII10", "T10YIE",
                                     "T10Y3M", "BAMLC0A0CM", "BAMLH0A0HYM2")],
        "loan_rates": [m(sid) for sid in ("MORTGAGE30US", "MORTGAGE15US", "TERMCBAUTO48NS",
                                          "TERMCBPER24NS", "TERMCBCCALLNS")],
        "markets": markets,
        "housing": [m(sid, 1) for sid in ("MSPUS", "ASPUS", "CSUSHPINSA", "MSACSR")],
        "economy": {
            "cpi_yoy": rnd(cpi_yoy, 1), "cpi_as_of": cpi.last_date,
            "unemployment": rnd(series["UNRATE"].last, 1), "unemployment_as_of": series["UNRATE"].last_date,
            "vix": rnd(series["VIXCLS"].last, 1),
            "dollar_6m": rnd(series["DTWEXBGS"].pct_change(182), 1),
        },
        "long_run_averages": LONG_RUN,
    }


def update_history(latest: dict) -> list:
    path = DATA_DIR / "history.json"
    history = json.loads(path.read_text()) if path.exists() else []
    today = latest["generated_at"][:10]
    entry = {"date": today,
             **{k: {"action": v["action"], "score": v["score"]} for k, v in latest["signals"].items()},
             "allocation": {name: p["suggested"] for name, p in latest["profiles"].items()}}
    history = [h for h in history if h["date"] != today] + [entry]
    return history[-HISTORY_MAX_DAYS:]


def main() -> int:
    global DATA_DIR
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixtures", type=Path, help="Read <dir>/<SERIES>.csv instead of fetching")
    ap.add_argument("--out", type=Path, default=DATA_DIR, help="Output directory (default: data/)")
    ap.add_argument("--dump", type=Path, help="Also save every fetched series as <dir>/<SERIES>.csv")
    args = ap.parse_args()
    DATA_DIR = args.out

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
        if args.dump and obs:
            args.dump.mkdir(parents=True, exist_ok=True)
            (args.dump / f"{sid}.csv").write_text("observation_date," + sid + "\n" + "".join(f"{d},{v}\n" for d, v in obs))
        if not obs:
            failures.append(sid)
        series[sid] = Series(sid, obs)
        if not args.fixtures:
            time.sleep(0.3)  # be polite

    if failures:
        print(f"::warning::Missing series: {', '.join(failures)}")
    if len(failures) > len(SERIES) // 2:
        print(f"Too many series failed ({len(failures)}/{len(SERIES)}); keeping previous data.", file=sys.stderr)
        return 1

    latest = build(series, failures)
    t0 = time.time()
    bt = backtest(series)
    print(f"Backtests computed in {time.time() - t0:.1f}s")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "latest.json").write_text(json.dumps(latest, indent=1) + "\n")
    (DATA_DIR / "history.json").write_text(json.dumps(update_history(latest), indent=1) + "\n")
    (DATA_DIR / "backtest.json").write_text(json.dumps(bt, separators=(",", ":")) + "\n")
    for k, v in latest["signals"].items():
        print(f"{k:12} -> {v['action']} (score {v['score']})")
    for name, p in latest["profiles"].items():
        print(f"{name:12} {p['suggested']}")
    for period, res in bt["backtest"]["periods"].items():
        print(f"Backtest {period}: {res['label']}, {res['months']} months ({res['start']} to {res['end']})")
        for name, r in res["profiles"].items():
            print(f"  {name:12} stocks {r['stock_weight']['min']}-{r['stock_weight']['max']}%  "
                  + "  ".join(f"{k}: {v['cagr']}%/yr dd {v['max_drawdown']}% sharpe {v['sharpe']}" for k, v in r["stats"].items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
