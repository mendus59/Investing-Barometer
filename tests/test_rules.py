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

    def test_allocation_sums_to_100(self):
        sig = {"stocks": {"action": "Buy"}, "bonds": {"action": "Sell"}, "cash": {"action": "Overweight"}}
        a = u.allocation(sig)["suggested"]
        self.assertEqual(sum(a.values()), 100)
        self.assertGreater(a["stocks"], 60)


if __name__ == "__main__":
    unittest.main()
