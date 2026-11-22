# Investing Barometer

A static web page, hosted on GitHub Pages, that reads current interest rates, bond yields, stock index returns and
housing data once a day and turns them into a simple **Buy / Hold / Sell** read for stocks, bonds, cash and housing,
plus borrowing guidance and an illustrative portfolio tilt.

> **Not financial advice.** This is an educational project built on a few transparent rules. It does not know your
> situation. Consult a licensed professional before investing.

## How it works

1. `.github/workflows/update.yml` runs every day at 14:30 UTC (and on every push to `main`, or by hand).
2. `scripts/update.py` downloads about 25 series from [FRED](https://fred.stlouisfed.org/) (St. Louis Fed),
   scores each category, and writes `data/latest.json` and `data/history.json`.
3. The workflow commits the new data and deploys the site to GitHub Pages.
4. `index.html` + `assets/app.js` render the JSON in the browser. No build step and no dependencies.

### Signals

Each category is scored on 3–5 factors worth +1, 0 or −1. A total of +2 or more is **Buy** (cash: *Overweight*),
−2 or less is **Sell** (cash: *Underweight*), anything else is **Hold**.

| Category | Factors |
| --- | --- |
| Stocks | S&P 500 vs. 200-day average, 1-year return vs. history, VIX, yield-curve inversion, 3-month change in high-yield spreads |
| Bonds | 10-year yield vs. 5-year average, 10-year TIPS real yield, 6-month Fed funds direction, CPI inflation |
| Cash | T-bill yield minus inflation, yield curve, Fed funds direction |
| Housing (buyer's view) | Payment-to-income on the median home, mortgage rate vs. 10-year average, Case-Shiller YoY, months' supply |
| Borrowing | Each loan rate vs. a ~10% long-run stock return, plus a refinance check against your own mortgage rate |

## Setup (one time)

1. **Enable Pages:** *Settings → Pages → Build and deployment → Source: **GitHub Actions***.
2. **(Recommended) Add a FRED API key.** Without one, the script uses FRED's public CSV download, which works but is
   less reliable for automated jobs. Get a free key at <https://fredaccount.stlouisfed.org/apikeys>, then add it under
   *Settings → Secrets and variables → Actions → New repository secret* with the name `FRED_API_KEY`.
3. Run the workflow once: *Actions → Update data and deploy → Run workflow*.

The site will be at `https://mendus59.github.io/Investing-Barometer/`.

## Run locally

```sh
python3 scripts/update.py          # optional: FRED_API_KEY=... python3 scripts/update.py
python3 -m http.server 8000        # then open http://localhost:8000
```
