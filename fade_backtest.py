#!/usr/bin/env python3
"""
Backtest the "morning-spike -> noon-fade -> buy at 12:30" idea over ~60 days.

Reproduces the Chartink scan from the screenshot, using yfinance 5-min data
(works without the Dhan Data API plan, which only covers ~60 calendar days):

  Scan (per stock, per day), all % vs previous-day close:
    spike : a 5-min candle CLOSED >= +UP%   in the 09:30-09:45 window
            (Chartink candles [=4],[=5],[=6])
    fade  : the candle that closed at 12:30  is  <  +NOON_HIGH%
            (Chartink [0] at the 12:30 scan)
    cap   : market cap > MCAP_MIN cr

  Trade: BUY equal-weight at 12:30 (open of the 12:30 bar), measure forward
         returns to 13:00/13:30/14:00/14:30/15:00/15:15/close and the
         max favourable / adverse excursion to close.

Data is cached on disk so re-runs are instant.

Usage:
  python fade_backtest.py                 # last 60 days, defaults
  python fade_backtest.py --days 45 --up-pct 5 --noon-high 3 --mcap-min 500
  python fade_backtest.py --refresh       # ignore cache, re-download
"""
from __future__ import annotations

import argparse
import json
import logging
import pickle
import time as _time
from datetime import date, datetime, time, timedelta

import pandas as pd
import yfinance as yf

import config as cfg
from dhan import DhanAPI

log = logging.getLogger("fade_bt")

CACHE = cfg.DATA_DIR / "yf_cache"
CACHE_5M = CACHE / "5m"
DAILY_PKL = CACHE / "daily.pkl"
MCAP_JSON = CACHE / "mcap.json"
for _d in (CACHE, CACHE_5M):
    _d.mkdir(parents=True, exist_ok=True)

MORNING_BARS = [time(9, 30), time(9, 35), time(9, 40)]  # [=4],[=5],[=6]
FADE_BAR = time(12, 25)      # candle that closes at 12:30 = [0] at the scan
ENTRY_BAR = time(12, 30)     # we buy at its open
HORIZONS = [time(13, 0), time(13, 30), time(14, 0), time(14, 30),
            time(15, 0), time(15, 15)]


# --------------------------------------------------------------------------- #
# Universe
# --------------------------------------------------------------------------- #
def load_universe(limit: int = 0) -> pd.DataFrame:
    """NSE EQ universe from the Dhan scrip master (a static CSV, no data plan)."""
    uni = DhanAPI(cfg.ACCESS_TOKEN).get_universe()
    uni = uni[["symbol", "name"]].drop_duplicates("symbol").reset_index(drop=True)
    if limit:
        uni = uni.head(limit)
    return uni


# --------------------------------------------------------------------------- #
# Downloads (cached)
# --------------------------------------------------------------------------- #
def _yf_download(tickers: list[str], **kw) -> pd.DataFrame:
    for attempt in range(3):
        try:
            df = yf.download(tickers, group_by="ticker", auto_adjust=False,
                             threads=True, progress=False, **kw)
            if df is not None and not df.empty:
                return df
        except Exception as e:  # noqa: BLE001
            log.warning("yf batch failed (%s), retry", e)
        _time.sleep(2)
    return pd.DataFrame()


def download_daily(symbols: list[str], start: date, end: date,
                   refresh: bool) -> dict[str, pd.DataFrame]:
    """Daily OHLC per symbol (for prev-close + cheap spike pre-filter)."""
    if DAILY_PKL.exists() and not refresh:
        return pickle.loads(DAILY_PKL.read_bytes())

    out: dict[str, pd.DataFrame] = {}
    tickers = [s + ".NS" for s in symbols]
    for i in range(0, len(tickers), 200):
        chunk = tickers[i:i + 200]
        log.info("daily %d-%d / %d", i, i + len(chunk), len(tickers))
        df = _yf_download(chunk, start=start, end=end, interval="1d")
        for t in chunk:
            sym = t[:-3]
            try:
                sub = df[t] if isinstance(df.columns, pd.MultiIndex) else df
                sub = sub.dropna(how="all")
                if not sub.empty:
                    out[sym] = sub[["Open", "High", "Low", "Close"]]
            except Exception:  # noqa: BLE001
                pass
    DAILY_PKL.write_bytes(pickle.dumps(out))
    return out


def download_5m(symbols: list[str], start: date, end: date,
                refresh: bool) -> dict[str, pd.DataFrame]:
    """5-min OHLC per symbol, cached one pickle per ticker (resumable)."""
    out: dict[str, pd.DataFrame] = {}
    todo: list[str] = []
    for s in symbols:
        f = CACHE_5M / f"{s}.pkl"
        if f.exists() and not refresh:
            try:
                out[s] = pickle.loads(f.read_bytes())
                continue
            except Exception:  # noqa: BLE001
                pass
        todo.append(s)

    tickers = [s + ".NS" for s in todo]
    for i in range(0, len(tickers), 60):
        chunk = tickers[i:i + 60]
        log.info("5m %d-%d / %d (have %d cached)", i, i + len(chunk),
                 len(tickers), len(out))
        df = _yf_download(chunk, start=start, end=end, interval="5m")
        for t in chunk:
            sym = t[:-3]
            try:
                sub = df[t] if isinstance(df.columns, pd.MultiIndex) else df
                sub = sub.dropna(how="all")
            except Exception:  # noqa: BLE001
                sub = pd.DataFrame()
            if not sub.empty:
                sub = sub.copy()
                sub.index = sub.index.tz_convert("Asia/Kolkata")
                sub = sub[["Open", "High", "Low", "Close"]]
            (CACHE_5M / f"{sym}.pkl").write_bytes(pickle.dumps(sub))
            out[sym] = sub
        _time.sleep(1)
    return out


# --------------------------------------------------------------------------- #
# Scan + trade
# --------------------------------------------------------------------------- #
def prev_close_by_day(daily: pd.DataFrame) -> dict[date, float]:
    """Map trading day -> previous day's close."""
    closes = daily["Close"].dropna()
    days = [ix.date() if hasattr(ix, "date") else ix for ix in closes.index]
    vals = list(closes.values)
    return {days[i]: float(vals[i - 1]) for i in range(1, len(days))}


def spike_candidates(daily: dict[str, pd.DataFrame], up_pct: float
                     ) -> dict[str, set[date]]:
    """Cheap pre-filter: days where the daily HIGH reached >= +up% vs prev close
    (necessary condition for an intraday 5-min close to breach it)."""
    out: dict[str, set[date]] = {}
    for sym, d in daily.items():
        pc = prev_close_by_day(d)
        hi = d["High"]
        days = set()
        for ix, h in hi.items():
            dd = ix.date() if hasattr(ix, "date") else ix
            p = pc.get(dd)
            if p and p > 0 and (float(h) / p - 1) * 100 >= up_pct:
                days.add(dd)
        if days:
            out[sym] = days
    return out


def _bar(day_df: pd.DataFrame, t: time, field: str):
    for ix, row in day_df.iterrows():
        if ix.time() == t:
            return float(row[field])
    return None


def _last_close(day_df: pd.DataFrame, upto: time):
    val = None
    for ix, row in day_df.iterrows():
        if ix.time() <= upto:
            val = float(row["Close"])
    return val


def scan_and_trade(sym: str, df5: pd.DataFrame, daily: pd.DataFrame,
                   up_pct: float, noon_high: float) -> list[dict]:
    """Return one trade dict per (sym, day) that passes spike+fade."""
    if df5 is None or df5.empty:
        return []
    pc_map = prev_close_by_day(daily)
    trades = []
    for d, day in df5.groupby(df5.index.date):
        pc = pc_map.get(d)
        if not pc or pc <= 0:
            continue
        morning = [_bar(day, t, "Close") for t in MORNING_BARS]
        morning = [m for m in morning if m is not None]
        if not morning or max(morning) < pc * (1 + up_pct / 100):
            continue
        fade_px = _bar(day, FADE_BAR, "Close")
        if fade_px is None:
            fade_px = _last_close(day, time(12, 30))
        if fade_px is None or fade_px >= pc * (1 + noon_high / 100):
            continue
        entry = _bar(day, ENTRY_BAR, "Open") or _last_close(day, ENTRY_BAR)
        if not entry or entry <= 0:
            continue

        rec = {"date": d, "symbol": sym, "prev_close": round(pc, 2),
               "morning_high_pct": round((max(morning) / pc - 1) * 100, 2),
               "entry": round(entry, 2),
               "fade_pct_vs_pc": round((fade_px / pc - 1) * 100, 2)}
        for H in HORIZONS:
            p = _last_close(day, H)
            rec[H.strftime("%H:%M")] = round((p / entry - 1) * 100, 2) if p else None
        rec["close"] = round((float(day.iloc[-1]["Close"]) / entry - 1) * 100, 2)
        aft = day[[ix.time() >= ENTRY_BAR for ix in day.index]]
        if not aft.empty:
            rec["MFE"] = round((float(aft["High"].max()) / entry - 1) * 100, 2)
            rec["MAE"] = round((float(aft["Low"].min()) / entry - 1) * 100, 2)
        trades.append(rec)
    return trades


# --------------------------------------------------------------------------- #
# Market cap (signals only)
# --------------------------------------------------------------------------- #
def market_caps(symbols: list[str], refresh: bool) -> dict[str, float]:
    cache = json.loads(MCAP_JSON.read_text()) if MCAP_JSON.exists() and not refresh else {}
    for s in symbols:
        if s in cache:
            continue
        cap = None
        try:
            info = yf.Ticker(s + ".NS").get_info()
            cap = info.get("marketCap")
        except Exception:  # noqa: BLE001
            cap = None
        cache[s] = (cap / 1e7) if cap else None  # INR -> crore
    MCAP_JSON.write_text(json.dumps(cache))
    return cache


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
def summarize(trades: list[dict], label: str) -> None:
    import statistics as st
    print(f"\n=== {label}: {len(trades)} trades ===")
    if not trades:
        return
    rows = ["13:00", "13:30", "14:00", "14:30", "15:00", "15:15", "close", "MFE", "MAE"]
    print(f"  {'horizon':>8} {'avg%':>8} {'median%':>9} {'win%':>6}")
    for H in rows:
        v = [t[H] for t in trades if t.get(H) is not None]
        if not v:
            continue
        print(f"  {H:>8} {sum(v)/len(v):>+7.2f} {st.median(v):>+8.2f} "
              f"{sum(1 for x in v if x > 0)/len(v)*100:>5.0f}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=58,
                   help="lookback calendar days (Yahoo 5m caps at ~60)")
    p.add_argument("--up-pct", type=float, default=5.0)
    p.add_argument("--noon-high", type=float, default=3.0)
    p.add_argument("--mcap-min", type=float, default=500.0)
    p.add_argument("--limit", type=int, default=0, help="cap universe (debug)")
    p.add_argument("--refresh", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S")

    end = date.today() + timedelta(days=1)
    start = date.today() - timedelta(days=args.days)

    uni = load_universe(args.limit)
    syms = uni["symbol"].tolist()
    log.info("Universe: %d NSE EQ symbols", len(syms))

    daily = download_daily(syms, start, end, args.refresh)
    log.info("Daily data: %d symbols", len(daily))

    cand = spike_candidates(daily, args.up_pct)
    cand_syms = sorted(cand)
    log.info("Spike pre-filter (daily high >= +%.0f%%): %d symbols, %d sym-days",
             args.up_pct, len(cand_syms), sum(len(v) for v in cand.values()))

    df5 = download_5m(cand_syms, start, end, args.refresh)
    log.info("5m data: %d symbols", len(df5))

    all_trades: list[dict] = []
    for sym in cand_syms:
        all_trades += scan_and_trade(sym, df5.get(sym), daily[sym],
                                     args.up_pct, args.noon_high)
    log.info("Signals (spike+fade, pre-mcap): %d", len(all_trades))

    caps = market_caps(sorted({t["symbol"] for t in all_trades}), args.refresh)
    for t in all_trades:
        t["mcap_cr"] = caps.get(t["symbol"])
    passed = [t for t in all_trades if t["mcap_cr"] and t["mcap_cr"] >= args.mcap_min]
    log.info("After mcap > %.0f cr: %d trades", args.mcap_min, len(passed))

    out = cfg.OUTPUT_DIR / "fade_backtest_trades.csv"
    if passed:
        pd.DataFrame(passed).sort_values(["date", "symbol"]).to_csv(out, index=False)
        log.info("Wrote %s", out)
    else:
        log.warning("No trades passed all filters — nothing written")

    summarize(passed, f"BUY 12:30, mcap>{args.mcap_min:.0f}cr, last {args.days}d")
    summarize(all_trades, "(reference) all spike+fade incl. sub-500cr")

    byday = {}
    for t in passed:
        byday.setdefault(t["date"], []).append(t["close"])
    if byday:
        import statistics as st
        dr = [sum(v)/len(v) for v in byday.values()]
        print(f"\nDays with >=1 signal: {len(byday)} | "
              f"avg day return: {sum(dr)/len(dr):+.2f}% | "
              f"positive days: {sum(1 for x in dr if x > 0)}/{len(dr)}")
        allc = [t["close"] for t in passed]
        print(f"Per-trade to close: avg {sum(allc)/len(allc):+.2f}% | "
              f"median {st.median(allc):+.2f}% | "
              f"best {max(allc):+.1f}% | worst {min(allc):+.1f}%")


if __name__ == "__main__":
    main()
