# Investing Barometer

A static web page, hosted on GitHub Pages, that reads current interest rates, bond yields, market index returns and
housing data once a day and turns them into a simple **Buy / Hold / Sell** read for 12 categories, plus:

- suggested allocations for **conservative, moderate and aggressive** risk profiles
- a **backtest** of the rules and a month-by-month **signal history** since 2010
- a **ZIP code** housing lookup (local home values, rents, income, buy-vs-rent)
- borrowing guidance and a refinance check

> **Not financial advice.** This is an educational project built on a few transparent rules. It does not know your
> situation. Consult a licensed professional before investing.

## How it works

1. `.github/workflows/update.yml` runs every day at 14:30 UTC (and on every push to `main`, or by hand).
2. `scripts/update.py` downloads about 30 series from [FRED](https://fred.stlouisfed.org/) (St. Louis Fed),
   scores each category, and writes `data/latest.json`, `data/history.json` and `data/backtest.json`.
3. `scripts/update_zip.py` (weekly) builds `data/zip/*.json` from Zillow's ZIP-level home value and rent indexes and
   Census median household income.
4. The workflow commits the new data and deploys the site to GitHub Pages. Pull requests run `check.yml`, which runs
   the tests and a dry run against live data.
5. `index.html` + `assets/app.js` render the JSON in the browser. No build step and no dependencies.

### Signals

Each category is scored on 3–5 factors worth +1, 0 or −1. A total of +2 or more is **Buy** (cash: *Overweight*),
−2 or less is **Sell** (cash: *Underweight*), anything else is **Hold**.

| Category | Factors |
| --- | --- |
| US stocks | S&P 500 vs. 200-day average, 1-year return vs. history, VIX, yield-curve inversion, 3-month change in high-yield spreads |
| International stocks | Dollar 6-month trend, Nikkei 225 vs. 200-day average, 1-year Nikkei vs. S&P 500 |
| Treasury bonds | 10-year yield vs. 5-year average, 10-year TIPS real yield, Fed funds direction, CPI inflation |
| TIPS | Real yield level, breakeven vs. actual inflation, breakeven level |
| Corporate bonds | Investment-grade spread, Treasury yield vs. 5-year average, recession risk (unemployment rise, inverted curve) |
| High-yield bonds | High-yield spread level, 3-month spread change, recession risk |
| REITs | 10-year yield 6-month change, Fed funds direction, credit stress |
| Commodities | Oil vs. 200-day average, commodity PPI trend, inflation, dollar trend |
| Gold | Real-yield direction, dollar trend, gold import price momentum |
| Cash | T-bill yield minus inflation, yield curve, Fed funds direction |
| Bitcoin (speculative) | Trend vs. 200-day average, 1-year momentum, Fed funds direction |
| Housing (buyer's view) | Payment-to-income on the median home, mortgage rate vs. 10-year average, Case-Shiller YoY, months' supply |
| ZIP code housing | Local payment-to-income, price-to-rent, local 1-year price change, mortgage rate vs. 10-year average |
| Borrowing | Each loan rate vs. a ~10% long-run stock return, plus a refinance check against your own mortgage rate |

Risk profiles start from fixed baseline weights (see `PROFILES` in `scripts/update.py`). Each Buy raises a slice by
30% and each Sell cuts it by 30%, then weights are scaled back to 100%.

The backtest rebalances a stocks/Treasuries/cash core mix monthly using only data published by each month-end. It
covers about 10 years because FRED only carries 10 years of S&P 500 history. No taxes or fees.

## Setup (one time)

1. **Enable Pages:** *Settings → Pages → Build and deployment → Source: **GitHub Actions***.
2. **(Recommended) Add a FRED API key.** Without one, the script uses FRED's public CSV download, which works but is
   less reliable for automated jobs. Get a free key at <https://fredaccount.stlouisfed.org/apikeys>, then add it under
   *Settings → Secrets and variables → Actions → New repository secret* with the name `FRED_API_KEY`.
3. **(Recommended) Add a Census API key** for the ZIP lookup's local income and affordability figures. The Census API
   now refuses keyless downloads of this size. Get a free key at <https://api.census.gov/data/key_signup.html> and save
   it as the repository secret `CENSUS_API_KEY`. Without it, ZIP lookups still work but skip the income-based factor.
4. Run the workflow once: *Actions → Update data and deploy → Run workflow*.

The site will be at `https://mendus59.github.io/Investing-Barometer/`.

## Run locally

```sh
python3 scripts/update.py          # optional: FRED_API_KEY=... python3 scripts/update.py
python3 scripts/update_zip.py --force
python3 -m unittest discover tests
python3 -m http.server 8000        # then open http://localhost:8000
```

## Data credits

FRED (Federal Reserve Bank of St. Louis); Zillow Home Value Index and Zillow Observed Rent Index (Zillow Research);
US Census Bureau American Community Survey.
