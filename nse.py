"""
NSE daily bhavcopy — the previous-close baseline.

Dhan's marketfeed `ohlc.close` is the *previous* close only during market
hours; after the session it rolls to *today's* close, which silently corrupts
every percentage. The NSE "security-wise full bhavdata" file gives the correct
PREV_CLOSE and full OHLC for every symbol in one download, independent of when
the screener is run, and works for past dates too.
"""

import logging
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import requests

import config as cfg

log = logging.getLogger("screener.nse")

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_BHAV_URL = "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{d}.csv"
_session: requests.Session | None = None


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        s = requests.Session()
        s.headers.update({"User-Agent": _UA, "Accept": "text/csv,*/*",
                          "Referer": "https://www.nseindia.com/all-reports"})
        try:  # prime cookies; NSE rejects cookieless archive requests
            s.get("https://www.nseindia.com", timeout=15)
        except requests.RequestException:
            pass
        _session = s
    return _session


def _download(d: date) -> Path | None:
    path = cfg.DATA_DIR / f"bhav_{d:%d%m%Y}.csv"
    if path.exists() and path.stat().st_size > 1000:
        return path
    url = _BHAV_URL.format(d=f"{d:%d%m%Y}")
    try:
        r = _get_session().get(url, timeout=30)
    except requests.RequestException:
        return None
    if r.status_code != 200 or len(r.content) < 1000 or b"<html" in r.content[:200].lower():
        return None
    path.write_bytes(r.content)
    return path


def load_bhavcopy(d: date) -> dict[str, dict] | None:
    """Return {symbol: {prev_close, open, high, low, close}} for EQ series.

    Raises ValueError for weekends, future dates, and past weekdays with no
    bhavcopy of their own (NSE holidays): the NSE archive serves the most
    recently published file for any missing date, so a cached/downloaded
    file whose internal DATE1 does not match `d` is refused and deleted.
    Returns None only when `d` is today and today's file is not published
    yet (the archive then serves an older file).
    """
    if d.weekday() >= 5:
        raise ValueError(
            f"{d:%Y-%m-%d} is a {d.strftime('%A')} — the NSE does not trade "
            "or publish bhavcopy on weekends. Use a weekday."
        )
    if d > date.today():
        raise ValueError(f"{d:%Y-%m-%d} is in the future — no bhavcopy exists yet.")
    path = _download(d)
    if not path:
        if d == date.today():
            return None  # today's file not published yet
        raise ValueError(
            f"No bhavcopy available for {d:%Y-%m-%d} (past weekday — NSE "
            "holiday, or the download failed)."
        )
    df = pd.read_csv(path, skipinitialspace=True)
    df.columns = [c.strip() for c in df.columns]
    if "DATE1" in df.columns and not df.empty:
        try:
            file_date = pd.to_datetime(str(df["DATE1"].iloc[0]).strip(),
                                       format="%d-%b-%Y").date()
        except ValueError:
            file_date = None
        if file_date is not None and file_date != d:
            path.unlink(missing_ok=True)
            if d == date.today():
                return None  # archive served an older file: not published yet
            raise ValueError(
                f"No bhavcopy for {d:%Y-%m-%d} — the NSE archive served "
                f"{file_date:%Y-%m-%d}'s file (likely an NSE holiday)."
            )
    elif "DATE1" not in df.columns:
        log.warning("bhavcopy %s has no DATE1 column — cannot verify its date",
                    path.name)
    df = df[df["SERIES"].astype(str).str.strip() == "EQ"]
    out: dict[str, dict] = {}
    for r in df.itertuples(index=False):
        try:
            out[str(r.SYMBOL).strip()] = {
                "prev_close": float(r.PREV_CLOSE),
                "open": float(r.OPEN_PRICE),
                "high": float(r.HIGH_PRICE),
                "low": float(r.LOW_PRICE),
                "close": float(r.CLOSE_PRICE),
            }
        except (ValueError, TypeError):
            continue
    return out


def prev_close_map(d: date, lookback: int = 7) -> tuple[date, dict[str, float]]:
    """Previous-close per symbol for trading day `d`.

    Uses the close of the most recent bhavcopy strictly before `d`, walking
    back over weekends/holidays. Returns (date_used, {symbol: close}).
    """
    probe = d - timedelta(days=1)
    for _ in range(lookback):
        if probe.weekday() < 5:
            try:
                data = load_bhavcopy(probe)
            except ValueError:
                data = None  # NSE holiday or unavailable — keep walking back
            if data:
                return probe, {s: v["close"] for s, v in data.items()}
        probe -= timedelta(days=1)
    raise RuntimeError(
        f"No NSE bhavcopy found within {lookback} days before {d} — "
        "cannot establish previous-close baseline."
    )


def is_trading_day(d: date) -> bool:
    """True if a completed NSE session exists for `d` (published bhavcopy).

    Today returns False until its bhavcopy is published, so a live session
    in progress does not count as a completed trading day.
    """
    if d.weekday() >= 5:
        return False
    try:
        return bool(load_bhavcopy(d))
    except ValueError:
        return False


def last_trading_day(d: date, lookback: int = 10) -> date:
    """Most recent completed NSE trading day on or before `d`."""
    probe = d
    for _ in range(lookback):
        if is_trading_day(probe):
            return probe
        probe -= timedelta(days=1)
    raise RuntimeError(f"No NSE trading day within {lookback} days up to {d}")


def next_trading_day(d: date, lookback: int = 10) -> date:
    """First completed NSE trading day on or after `d`."""
    probe = d
    for _ in range(lookback):
        if is_trading_day(probe):
            return probe
        probe += timedelta(days=1)
    raise RuntimeError(f"No NSE trading day within {lookback} days after {d}")
