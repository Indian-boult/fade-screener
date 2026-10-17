"""
Thin Dhan API client for the screener.

Only the three things the screener needs:
  - the NSE equity universe (from the instrument master)
  - bulk OHLC snapshots (prev close + day high + LTP) for the pre-filter
  - 1-minute intraday candles to confirm the morning move and the noon price
"""

import time
import logging

import pandas as pd
import requests

import auth
import config as cfg

log = logging.getLogger("screener.dhan")

# Per-endpoint minimum spacing (seconds). Dhan: marketfeed 1/s, charts 5/s.
_RATE = {"marketfeed": 1.1, "charts": 0.25}


class DhanAPI:
    def __init__(self, token: str):
        self.token = token
        self._last_call: dict[str, float] = {}

    # ------------------------------------------------------------------ #
    # HTTP
    # ------------------------------------------------------------------ #
    def _throttle(self, key: str) -> None:
        gap = _RATE.get(key, 0.0)
        last = self._last_call.get(key)
        if last is not None:
            wait = gap - (time.time() - last)
            if wait > 0:
                time.sleep(wait)
        self._last_call[key] = time.time()

    def _post(self, endpoint: str, payload: dict, rate_key: str, _retried: bool = False) -> dict:
        self._throttle(rate_key)
        r = requests.post(
            f"{cfg.API_BASE}/{endpoint}",
            headers=cfg.headers(self.token),
            json=payload,
            timeout=20,
        )
        if r.status_code == 401 and not _retried:
            log.warning("Token rejected mid-run — refreshing and retrying once")
            self.token = auth.ensure_token()
            return self._post(endpoint, payload, rate_key, _retried=True)
        if r.status_code == 429:
            time.sleep(2)
            return self._post(endpoint, payload, rate_key, _retried=True)
        r.raise_for_status()
        return r.json()

    # ------------------------------------------------------------------ #
    # Universe
    # ------------------------------------------------------------------ #
    def get_universe(self, force: bool = False) -> pd.DataFrame:
        """Return NSE EQ-series equities as a DataFrame.

        Columns: security_id (int), symbol, isin, name.
        The instrument master is cached locally for the day.
        """
        cache = cfg.SCRIP_MASTER_CACHE
        fresh = cache.exists() and (time.time() - cache.stat().st_mtime) < 86400
        if force or not fresh:
            log.info("Downloading Dhan instrument master ...")
            resp = requests.get(cfg.SCRIP_MASTER_URL, timeout=60)
            resp.raise_for_status()
            cache.write_bytes(resp.content)

        df = pd.read_csv(
            cache,
            usecols=[
                "EXCH_ID", "SECURITY_ID", "ISIN", "INSTRUMENT",
                "UNDERLYING_SYMBOL", "DISPLAY_NAME", "SERIES",
            ],
            dtype=str,
            low_memory=False,
        )
        eq = df[
            (df["EXCH_ID"] == "NSE")
            & (df["INSTRUMENT"] == "EQUITY")
            & (df["SERIES"] == "EQ")
            & df["UNDERLYING_SYMBOL"].notna()
        ].copy()
        # UNDERLYING_SYMBOL is the NSE trading ticker (e.g. RELIANCE); the
        # NSE_EQ security id is what the market-data endpoints expect.
        eq["security_id"] = eq["SECURITY_ID"].astype(int)
        eq = eq.rename(columns={"UNDERLYING_SYMBOL": "symbol", "ISIN": "isin", "DISPLAY_NAME": "name"})
        eq = eq[eq["symbol"].str.strip() != ""]
        return eq[["security_id", "symbol", "isin", "name"]].reset_index(drop=True)

    # ------------------------------------------------------------------ #
    # Market data
    # ------------------------------------------------------------------ #
    def ohlc_batch(self, security_ids: list[int]) -> dict[int, dict]:
        """Bulk today's OHLC + LTP for NSE_EQ ids. Returns {security_id: dict}.

        NOTE: `ohlc.close` here is the previous close ONLY during market hours;
        after the session it becomes today's close. Do NOT use it as the
        previous-close baseline — use nse.prev_close_map() for that. Only
        `day_high`/`ltp` are used (live intraday day-high for the pre-filter).
        """
        out: dict[int, dict] = {}
        for i in range(0, len(security_ids), 1000):
            chunk = security_ids[i:i + 1000]
            data = self._post("marketfeed/ohlc", {"NSE_EQ": chunk}, "marketfeed")
            eq = data.get("data", {}).get("NSE_EQ", {}) or {}
            for sid_str, info in eq.items():
                ohlc = info.get("ohlc", {}) or {}
                out[int(sid_str)] = {
                    "ohlc_close": float(ohlc.get("close", 0) or 0),
                    "day_open": float(ohlc.get("open", 0) or 0),
                    "day_high": float(ohlc.get("high", 0) or 0),
                    "day_low": float(ohlc.get("low", 0) or 0),
                    "ltp": float(info.get("last_price", 0) or 0),
                }
        return out

    def intraday(self, security_id: int, date_str: str) -> dict:
        """1-minute candles for one equity on `date_str` (YYYY-MM-DD).

        Returns dict with parallel arrays: timestamp, open, high, low, close, volume.
        """
        payload = {
            "securityId": str(security_id),
            "exchangeSegment": "NSE_EQ",
            "instrument": "EQUITY",
            "interval": cfg.INTRADAY_INTERVAL,
            "oi": False,
            "fromDate": date_str,
            "toDate": date_str,
        }
        return self._post("charts/intraday", payload, "charts")
