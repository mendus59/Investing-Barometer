"""Sanity checks for the scoring rules. Run: python -m unittest discover tests"""
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import update as u  # noqa: E402


def flat(sid, value, days=4000, end=date(2026, 11, 20)):
    return u.Series(sid, [((end - timedelta(days=i)).isoformat(), value) for i in range(days)])


def base():
    s = {sid: u.Series(sid, []) for sid in u.SERIES}
    return s


class Rules(unittest.TestCase):
    def test_monthly_payment(self):
        self.assertAlmostEqual(u.monthly_payment(300000, 6.0), 1798.65, places=1)

    def test_bonds_buy_when_yields_high_and_fed_cutting(self):
        s = base()
        end = date(2026, 11, 20)
        # 10-year yield jumps from 3% to 5% in the last 30 days.
        s["DGS10"] = u.Series("DGS10", [((end - timedelta(days=i)).isoformat(), 5.0 if i < 30 else 3.0) for i in range(2000)])
        s["DFII10"] = flat("DFII10", 2.0)
        s["DFF"] = u.Series("DFF", [((end - timedelta(days=i)).isoformat(), 3.5 if i < 60 else 4.5) for i in range(400)])
        r = u.bonds_signal(s, cpi_yoy=2.0)
        self.assertEqual(r["action"], "Buy")

    def test_no_signal_without_data(self):
        r = u.stocks_signal(base(), None)
        self.assertEqual(r["action"], "No signal")

    def test_housing_sell_when_unaffordable_and_tight(self):
        s = base()
        s["MSPUS"] = flat("MSPUS", 420000)
        s["MORTGAGE30US"] = u.Series("MORTGAGE30US", [((date(2026, 11, 20) - timedelta(days=i)).isoformat(), 7.5 if i < 300 else 4.0) for i in range(3650)])
        s["MEHOINUSA672N"] = flat("MEHOINUSA672N", 80000)
        s["MSACSR"] = flat("MSACSR", 3.5)
        r = u.housing_signal(s)
        self.assertEqual(r["action"], "Sell")

    def test_profiles_sum_to_100(self):
        for name, p in u.PROFILES.items():
            self.assertEqual(sum(p["weights"].values()), 100, name)
            self.assertEqual(sum(p["core"].values()), 100, name)

    def test_allocation_tilts_and_sums_to_100(self):
        sig = {k: {"action": "Hold"} for k, _ in u.CATEGORIES}
        sig["stocks"]["action"] = "Buy"
        sig["cash"]["action"] = "Underweight"
        for name, a in u.allocations(sig).items():
            self.assertEqual(sum(a["suggested"].values()), 100, name)
            self.assertGreaterEqual(a["suggested"]["stocks"], a["baseline"]["stocks"], name)
            self.assertLessEqual(a["suggested"]["cash"], a["baseline"]["cash"], name)

    def test_every_category_handles_missing_data(self):
        sigs = u.compute_signals(base())
        self.assertEqual([k for k, _ in u.CATEGORIES], list(sigs))
        for k, v in sigs.items():
            self.assertEqual(v["action"], "No signal", k)

    def test_backtest_has_no_lookahead(self):
        end = date(2026, 11, 20)
        s = base()
        s["CPIAUCSL"] = u.Series("CPIAUCSL", [((end - timedelta(days=30 * i)).isoformat(), 300 - i * 0.5) for i in range(60)])
        lag = s["CPIAUCSL"].publication_lag()
        self.assertEqual(lag, 45)
        seen = u.as_of(s, date(2026, 10, 31), {k: v.publication_lag() for k, v in s.items()})["CPIAUCSL"]
        self.assertLessEqual(seen.dates[-1], date(2026, 10, 31) - timedelta(days=45))

    def test_backtest_runs_on_synthetic_data(self):
        end = date(2026, 10, 31)
        days = [end - timedelta(days=i) for i in range(4000)][::-1]
        s = base()
        s["SP500"] = u.Series("SP500", [(d.isoformat(), 1000 * 1.0003 ** i) for i, d in enumerate(days)])
        s["DGS10"] = u.Series("DGS10", [(d.isoformat(), 4.0) for d in days])
        s["DGS3MO"] = u.Series("DGS3MO", [(d.isoformat(), 2.0) for d in days])
        s["VIXCLS"] = u.Series("VIXCLS", [(d.isoformat(), 20.0) for d in days])
        bt = u.backtest(s)["backtest"]
        self.assertGreater(bt["months"], 24)
        mod = bt["profiles"]["moderate"]
        self.assertEqual(len(mod["curve"]), bt["months"] + 1)
        # A constant 4% Treasury yield and 2% cash with steadily rising stocks: everything grows.
        self.assertGreater(mod["stats"]["static"]["final"], 10000)


if __name__ == "__main__":
    unittest.main()
