"""
Market-cap lookup.

Dhan exposes no fundamental data, so market cap comes from yfinance.
Because the screener only needs it for the handful of stocks that survive
the price filter, lookups are cheap and cached on disk.

Values are returned in ₹ crore (1 crore = 1e7).
"""

import json
import logging
from datetime import datetime, timezone

import config as cfg

log = logging.getLogger("screener.marketcap")

_CRORE = 1e7


def _load_cache() -> dict:
    if cfg.MARKET_CAP_CACHE.exists():
        try:
            return json.loads(cfg.MARKET_CAP_CACHE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_cache(cache: dict) -> None:
    cfg.MARKET_CAP_CACHE.write_text(json.dumps(cache, indent=2))


def _is_fresh(entry: dict) -> bool:
    try:
        asof = datetime.fromisoformat(entry["asof"])
    except (KeyError, ValueError):
        return False
    age_days = (datetime.now(timezone.utc) - asof).total_seconds() / 86400
    return age_days < cfg.MCAP_CACHE_DAYS


def _fetch_one(symbol: str) -> float | None:
    """Market cap (₹ crore) for an NSE symbol via yfinance, or None."""
    import yfinance as yf

    ticker = yf.Ticker(f"{symbol}.NS")
    cap = None
    try:
        cap = getattr(ticker.fast_info, "market_cap", None)
    except Exception:  # noqa: BLE001 - yfinance raises many ad-hoc errors
        cap = None
    if not cap:
        try:
            cap = ticker.info.get("marketCap")
        except Exception:  # noqa: BLE001
            cap = None
    if not cap:
        return None
    return round(float(cap) / _CRORE, 1)


def get_market_caps(symbols: list[str]) -> dict[str, float | None]:
    """Return {symbol: market_cap_in_crore_or_None}, using/refreshing cache.

    Failed lookups are never cached — a rate-limited fetch must not stick
    as None for MCAP_CACHE_DAYS.
    """
    cache = _load_cache()
    result: dict[str, float | None] = {}
    fetched = 0
    for sym in symbols:
        entry = cache.get(sym)
        if entry and entry.get("mcap_cr") is not None and _is_fresh(entry):
            result[sym] = entry["mcap_cr"]
            continue
        mcap = _fetch_one(sym)
        if mcap is not None:
            cache[sym] = {"mcap_cr": mcap, "asof": datetime.now(timezone.utc).isoformat()}
        result[sym] = mcap
        fetched += 1
    if fetched:
        _save_cache(cache)
        log.debug("Fetched market cap for %d symbol(s) via yfinance", fetched)
    return result
