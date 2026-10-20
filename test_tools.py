#!/usr/bin/env python3
"""
Regression tests for the ask tool layer (screener._execute_tool).

NOTE: the hard-coded dates (2026-10-15/16/19) depend on 5-minute data
still being available — Yahoo only keeps about 60 days of it — and on the
cached NSE bhavcopies in data/. When that window moves past those days,
refresh these tests with newer known trading days.

The tests exercise the tool layer directly and never call DeepSeek:
ask() is never invoked, and no test may be added that calls it. They
assert on returned data only, never on the wording of a model answer.

Run:  .venv/bin/python3 test_tools.py
"""

import logging
import os
import unittest
from datetime import date, timedelta

import config
import screener

logging.basicConfig(level=logging.WARNING,
                    format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")


class EnvBlankLine(unittest.TestCase):
    """A blank line in .env must never clobber an exported value."""

    def test_blank_env_value_never_overrides_an_exported_key(self):
        keys = ("DEEPSEEK_API_KEY", "DHAN_ACCESS_TOKEN")
        saved = {k: os.environ.get(k) for k in keys}
        try:
            os.environ["DEEPSEEK_API_KEY"] = "fake-exported-key-1234"
            os.environ["DHAN_ACCESS_TOKEN"] = "stale-exported-token"
            config._apply_env_values({
                "DEEPSEEK_API_KEY": "",              # blank line in .env
                "DHAN_ACCESS_TOKEN": "fresh-token",  # non-blank line in .env
            })
            # Blank line: the exported key survives.
            self.assertEqual(os.environ["DEEPSEEK_API_KEY"],
                             "fake-exported-key-1234")
            # Non-blank line: the file still wins (Dhan token behaviour).
            self.assertEqual(os.environ["DHAN_ACCESS_TOKEN"], "fresh-token")
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


class ScreenerKnownDays(unittest.TestCase):
    """The screener tool must return exactly the known stocks on these days."""

    def _run_day(self, d):
        result = screener._execute_tool("run_intraday_screener", {"date": d})
        self.assertEqual(result["status"], "ok")
        return result

    def test_2026_10_16_orientelec_avtnpl(self):
        r = self._run_day("2026-10-16")
        self.assertEqual({s["symbol"] for s in r["stocks"]},
                         {"ORIENTELEC", "AVTNPL"})
        self.assertEqual(r["matched"], 2)

    def test_2026_10_15_godrejind_southwest(self):
        r = self._run_day("2026-10-15")
        self.assertEqual({s["symbol"] for s in r["stocks"]},
                         {"GODREJIND", "SOUTHWEST"})
        self.assertEqual(r["matched"], 2)

    def test_2026_10_19_ramcoind(self):
        r = self._run_day("2026-10-19")
        self.assertEqual({s["symbol"] for s in r["stocks"]}, {"RAMCOIND"})
        self.assertEqual(r["matched"], 1)


class ScreenerNonTradingDays(unittest.TestCase):
    """Weekend/holiday dates must give a clear error, not a quiet empty list."""

    def test_sunday_2026_10_18(self):
        with self.assertRaises(ValueError) as cm:
            screener._execute_tool("run_intraday_screener",
                                   {"date": "2026-10-18"})
        self.assertIn("2026-10-18", str(cm.exception))
        self.assertIn("Sunday", str(cm.exception))

    def test_holiday_2026_10_20(self):
        # While 2026-10-20 is today (an NSE holiday) this raises the
        # no-data RuntimeError; once it is a past weekday it raises the
        # bhavcopy loader's ValueError instead. Either way: a clear error.
        with self.assertRaises((ValueError, RuntimeError)) as cm:
            screener._execute_tool("run_intraday_screener",
                                   {"date": "2026-10-20"})
        self.assertIn("2026-10-20", str(cm.exception))


class MarketCapsUnknownSymbol(unittest.TestCase):
    def test_unknown_symbol_gets_a_reason_not_a_silent_null(self):
        r = screener._execute_tool("get_market_caps", {"symbols": ["ZZZQQ"]})
        self.assertEqual(r["status"], "ok")
        entry = r["caps"]["ZZZQQ"]
        self.assertIsNone(entry["market_cap_cr"])
        self.assertIn("reason", entry)
        self.assertIn("not found on NSE", entry["reason"])


class BacktestWindowAndStats(unittest.TestCase):
    def test_window_end_and_horizon_stats(self):
        r = screener._execute_tool("run_fade_backtest", {"days": 60})
        self.assertEqual(r["status"], "ok")
        # Lookback capped to the Yahoo 5-minute limit.
        self.assertEqual(r["params"]["days"], 58)
        # End date is a past trading day, never tomorrow.
        end = date.fromisoformat(r["end_date"])
        today = date.today()
        self.assertLessEqual(end, today)
        self.assertNotEqual(end, today + timedelta(days=1))
        self.assertLess(end.weekday(), 5)
        # MFE/MAE rows carry no win rate; return rows keep it.
        for row in ("MFE", "MAE"):
            self.assertNotIn("win_pct", r["horizon_stats"][row])
            self.assertIn("avg_pct", r["horizon_stats"][row])
            self.assertIn("median_pct", r["horizon_stats"][row])
        for row in ("13:00", "close"):
            self.assertIn("win_pct", r["horizon_stats"][row])
        # A healthy cached run must report trades, not a zero-trade lie.
        self.assertGreater(r["trades_after_mcap"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
