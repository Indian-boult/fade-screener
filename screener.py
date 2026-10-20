#!/usr/bin/env python3
"""
Intraday fade screener.

Finds NSE stocks (market cap > ₹500 cr) that spiked > 5% in the morning
(by 09:45) but were down on the day by noon.

  Up  : intraday HIGH between 09:15 and the cutoff >= +UP_PCT% vs prev close
  Down: price at NOON < previous close (red for the day)

Usage:
  python screener.py                      # today, defaults
  python screener.py --date 2026-06-02    # a specific trading day
  python screener.py --up-pct 5 --mcap-min 500 --cutoff 09:45 --noon 12:00
  python screener.py --limit 200          # only scan first 200 (debug)
  python screener.py ask "your question"  # LLM-augmented natural-language query
"""

import argparse
import csv
import json
import logging
import os
from datetime import date, datetime, time, timedelta, timezone

import pandas as pd
import requests

import config as cfg
import marketcap
import nse

IST = timezone(timedelta(hours=5, minutes=30))
log = logging.getLogger("screener")

# --------------------------------------------------------------------------- #
# Tool schemas for LLM function calling
# --------------------------------------------------------------------------- #
TOOL_SCHEMAS = {
    "load_nse_bhavcopy": {
        "description": "Download NSE security-wise full bhavdata for a date from the public NSE archive (no auth). Returns per-symbol {prev_close, open, high, low, close}. Pass `symbols` to fetch only those rows; omitting `symbols` returns the full dataset for the date (~2700 symbols — large payload).",
        "parameters": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "Trading day YYYY-MM-DD"},
                "symbols": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional NSE symbols (e.g. ['RELIANCE','TCS']) to return rows for",
                },
            },
            "required": ["date"],
            "additionalProperties": False,
        },
    },
    "get_prev_close_map": {
        "description": "Previous-close-per-symbol for a trading day, from the most recent NSE bhavcopy strictly before that date (walks back over weekends/holidays). Returns {symbol: close}. Pass `symbols` to fetch only those closes; omitting `symbols` returns the full map (~2700 symbols).",
        "parameters": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "Target trading day YYYY-MM-DD"},
                "lookback": {"type": "integer", "description": "Max days to walk back for a bhavcopy (default 7)"},
                "symbols": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional NSE symbols to return closes for",
                },
            },
            "required": ["date"],
            "additionalProperties": False,
        },
    },
    "list_nse_universe": {
        "description": "List the NSE EQ universe from the public scrip-master CSV (no auth needed). Returns symbol + name pairs, capped at 200 per call.",
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Cap universe to first N symbols (0 = all, default 0)"},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    "get_market_caps": {
        "description": "Get market caps (₹ crore) for a list of NSE symbols. Each symbol is first checked against the NSE EQ scrip master: a symbol not in it returns market_cap_cr null with reason 'not found on NSE' and is not fetched. Listed symbols are fetched from Yahoo (cached) and return market_cap_cr, or null with reason 'temporary fetch error' when Yahoo returns no market cap.",
        "parameters": {
            "type": "object",
            "properties": {
                "symbols": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of NSE symbols (e.g. ['RELIANCE','TCS'])",
                },
                "refresh": {
                    "type": "boolean",
                    "description": "Ignore cache and re-download (default False).",
                },
            },
            "required": ["symbols"],
            "additionalProperties": False,
        },
    },
    "run_intraday_screener": {
        "description": "Run the intraday fade screener for a specific date. Finds stocks that spiked >= up_pct% by cutoff and faded to within [noon_low, noon_high]% by noon. Public sources only: scrip-master universe, NSE bhavcopy prev closes and day highs, yfinance 5-minute candles, yfinance market caps. Yahoo keeps ~60 days of 5m data. Returns matched stocks plus coverage counts (universe_symbols, day_high_coverage, candidates, symbols_with_intraday_data); returns a no-data error when the market may be closed or the data source has no data for the date.",
        "parameters": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "Trading day YYYY-MM-DD"},
                "up_pct": {"type": "number", "description": "Minimum morning spike % (default 5.0)"},
                "cutoff": {"type": "string", "description": "Morning cutoff HH:MM (default 09:45)"},
                "noon": {"type": "string", "description": "Noon time HH:MM (default 12:00)"},
                "noon_low": {"type": "number", "description": "Noon band low % vs prev close (default -2.0)"},
                "noon_high": {"type": "number", "description": "Noon band high % vs prev close (default 3.0)"},
                "mcap_min": {"type": "number", "description": "Minimum market cap ₹ crore (default 500)"},
                "limit": {"type": "integer", "description": "Scan only first N symbols (0 = all, default 0)"},
                "up_mode": {"type": "string", "description": "'high' = touched up-pct in window, 'at' = value at cutoff (default 'at')"},
                "no_mcap": {"type": "boolean", "description": "Skip market-cap filter (default False)"},
            },
            "required": ["date"],
            "additionalProperties": False,
        },
    },
    "run_fade_backtest": {
        "description": "Run the morning-spike-noon-fade backtest using yfinance (no auth). The lookback is capped at 58 days (Yahoo rejects longer 5-minute ranges); the result reports the actual start and end trading days (today counts only once its session is complete). Cached on disk for instant re-runs. Returns coverage counts (universe_symbols, daily_data_symbols, spike_candidate_symbols, symbols_with_5m_data, signals_pre_mcap, market_cap_failures, trades_after_mcap) and returns a no-data error when most symbols have no data or the market cap lookups failed — never a silent zero-trade result.",
        "parameters": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "Lookback calendar days, capped at 58 (Yahoo 5-minute limit; default 58)"},
                "up_pct": {"type": "number", "description": "Spike threshold % (default 5.0)"},
                "noon_high": {"type": "number", "description": "Fade threshold at 12:30 % (default 3.0)"},
                "mcap_min": {"type": "number", "description": "Minimum market cap ₹ crore (default 500)"},
                "limit": {"type": "integer", "description": "Cap universe to first N symbols (default 0)"},
                "refresh": {"type": "boolean", "description": "Ignore cache, re-download data (default False)"},
            },
            "required": ["days"],
            "additionalProperties": False,
        },
    },
}


# --------------------------------------------------------------------------- #
# DeepSeek tool-calling configuration
# --------------------------------------------------------------------------- #
DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = "deepseek-flash"
MAX_TOOL_ROUNDS = 8


def _hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def _candle_times(ts_list: list[int]) -> list[time]:
    return [datetime.fromtimestamp(ts, IST).time() for ts in ts_list]


def analyze_intraday(candles: dict, prev_close: float, open_t: time,
                     cutoff_t: time, noon_t: time) -> dict | None:
    """Compute morning move and noon level from 1-min candles.

    Returns dict with morning_high_pct (+time), at_cutoff_pct (value at the
    09:45 snapshot), and noon_pct/noon_price — all vs previous close.
    None if the data is unusable.
    """
    ts = candles.get("timestamp") or []
    highs = candles.get("high") or []
    closes = candles.get("close") or []
    if not ts or prev_close <= 0:
        return None

    times = _candle_times(ts)

    morning_high = 0.0
    morning_high_time = None
    cutoff_price = None  # close of last candle at/just before the cutoff
    noon_price = None    # close of last candle at/just before noon
    for t, h, c in zip(times, highs, closes):
        if open_t <= t <= cutoff_t and h > morning_high:
            morning_high = h
            morning_high_time = t
        if t <= cutoff_t:
            cutoff_price = c
        if t <= noon_t:
            noon_price = c

    if morning_high <= 0 or noon_price is None or cutoff_price is None:
        return None

    def pct(p):
        return round((p / prev_close - 1) * 100, 2)

    return {
        "morning_high_pct": pct(morning_high),
        "morning_high_time": morning_high_time.strftime("%H:%M") if morning_high_time else "",
        "at_cutoff_pct": pct(cutoff_price),
        "noon_price": round(noon_price, 2),
        "noon_pct": pct(noon_price),
    }


def _load_universe(limit: int = 0, force: bool = False) -> pd.DataFrame:
    """NSE EQ universe from the public scrip-master CSV (no auth needed).

    Columns: security_id (int), symbol, name. Cached locally for the day.
    """
    cache = cfg.SCRIP_MASTER_CACHE
    fresh = cache.exists() and (datetime.now().timestamp() - cache.stat().st_mtime) < 86400
    if force or not fresh:
        log.debug("Downloading instrument master (public CSV) ...")
        resp = requests.get(cfg.SCRIP_MASTER_URL, timeout=60)
        resp.raise_for_status()
        cache.write_bytes(resp.content)
    df = pd.read_csv(
        cache,
        usecols=["EXCH_ID", "SECURITY_ID", "INSTRUMENT",
                 "UNDERLYING_SYMBOL", "DISPLAY_NAME", "SERIES"],
        dtype=str,
        low_memory=False,
    )
    eq = df[
        (df["EXCH_ID"] == "NSE")
        & (df["INSTRUMENT"] == "EQUITY")
        & (df["SERIES"] == "EQ")
        & df["UNDERLYING_SYMBOL"].notna()
    ].copy()
    eq = eq[eq["UNDERLYING_SYMBOL"].str.strip() != ""]
    eq["security_id"] = eq["SECURITY_ID"].astype(int)
    eq = eq.rename(columns={"UNDERLYING_SYMBOL": "symbol", "DISPLAY_NAME": "name"})
    eq = eq[["security_id", "symbol", "name"]].reset_index(drop=True)
    if limit:
        eq = eq.head(limit)
    return eq


def _yf_day_highs(symbols: list[str], day: date) -> dict[str, float]:
    """Running day-high per symbol from Yahoo daily bars (public, no auth).

    Used only when the bhavcopy for `day` is not published yet (i.e. today,
    live): the current day's bar updates during the session. No disk cache
    because the values keep changing intraday.

    Yahoo serves the most recent bar BEFORE the requested range when the day
    has no bar of its own (holiday, pre-open), so any bar whose index date
    is not `day` is dropped — an older day's high must never pass as
    `day`'s.
    """
    import yfinance as yf

    out: dict[str, float] = {}
    end = day + timedelta(days=1)
    tickers = [s + ".NS" for s in symbols]
    for i in range(0, len(tickers), 200):
        chunk = tickers[i:i + 200]
        try:
            df = yf.download(chunk, start=day.isoformat(), end=end.isoformat(),
                             interval="1d", group_by="ticker", auto_adjust=False,
                             threads=True, progress=False)
        except Exception:  # noqa: BLE001
            continue
        if df is None or df.empty:
            continue
        for t in chunk:
            try:
                sub = df[t] if isinstance(df.columns, pd.MultiIndex) else df
                sub = sub.dropna(how="all")
                # Keep only bars actually dated `day`.
                highs = [float(h) for ix, h in sub["High"].items()
                         if ix.date() == day and pd.notna(h)]
                if highs:
                    out[t[:-3]] = max(highs)
            except Exception:  # noqa: BLE001
                continue
    return out


def _yf_intraday_5m(symbol: str, day: date) -> dict:
    """5-minute candles for one symbol on `day`, from Yahoo (public, no auth).

    Timestamps are shifted to the bar END time so analyze_intraday's
    `t <= cutoff` logic reads "price as of that clock time" (the 09:40 bar
    ends at 09:45, so its close IS the 09:45 price).
    """
    import yfinance as yf

    end = day + timedelta(days=1)
    df = yf.Ticker(symbol + ".NS").history(interval="5m",
                                           start=day.isoformat(),
                                           end=end.isoformat(),
                                           auto_adjust=False)
    if df is None or df.empty:
        return {}
    return {
        "timestamp": [int((ix + timedelta(minutes=5)).timestamp()) for ix in df.index],
        "high": [float(x) for x in df["High"]],
        "close": [float(x) for x in df["Close"]],
    }


def run(args) -> dict:
    open_t, cutoff_t, noon_t = _hhmm(cfg.MARKET_OPEN), _hhmm(args.cutoff), _hhmm(args.noon)
    target = datetime.strptime(args.date, "%Y-%m-%d").date()

    universe = _load_universe(limit=args.limit, force=args.refresh_master)
    log.debug("Universe: %d NSE EQ stocks", len(universe))

    # Correct previous-close baseline (run-time independent) from NSE bhavcopy.
    prev_dt, prev_close = nse.prev_close_map(target)
    log.debug("Prev-close baseline: NSE bhavcopy %s (%d symbols)", prev_dt, len(prev_close))

    # Day-high for the cheap pre-filter: bhavcopy if the day is done, else
    # running Yahoo daily bars (today, live). Probe a 30-symbol sample
    # first: if not even those have a bar for today, the market is closed
    # (holiday/weekend/pre-open) or Yahoo has no data — skip the full sweep.
    day_bhav = nse.load_bhavcopy(target)
    yf_highs = {}
    if day_bhav:
        log.debug("Day-high source: NSE bhavcopy %s (%d symbols)", target, len(day_bhav))
    else:
        probe = _yf_day_highs(universe["symbol"].head(30).tolist(), target)
        if not probe:
            log.warning("No Yahoo daily bars for a 30-symbol sample on %s — "
                        "skipping the full day-high sweep", target)
        else:
            log.debug("Bhavcopy for %s not published yet — fetching live day highs from Yahoo (slow, no cache)", target)
            yf_highs = _yf_day_highs(universe["symbol"].tolist(), target)

    # Pre-filter: day high already >= +UP_PCT% vs (correct) prev close.
    candidates = []
    no_base = 0
    day_high_coverage = 0
    for r in universe.itertuples(index=False):
        sym = r.symbol
        pc = prev_close.get(sym)
        if not pc or pc <= 0:
            no_base += 1
            continue
        if day_bhav:
            row = day_bhav.get(sym)
            day_high = row["high"] if row else None
        else:
            day_high = yf_highs.get(sym)
        if not day_high:
            continue
        day_high_coverage += 1
        day_high_pct = (day_high / pc - 1) * 100
        if day_high_pct >= args.up_pct:
            candidates.append((r, pc, round(day_high_pct, 2)))
    candidates.sort(key=lambda x: x[2], reverse=True)
    log.debug("Pre-filter (day high >= +%.1f%%): %d candidates (%d had no prev close)",
             args.up_pct, len(candidates), no_base)

    # Confirm via 5-minute Yahoo candles.
    confirmed = []
    intraday_checked = 0
    intraday_with_data = 0
    for n, (row, pc, day_high_pct) in enumerate(candidates, 1):
        intraday_checked += 1
        try:
            candles = _yf_intraday_5m(row.symbol, target)
        except Exception as e:  # noqa: BLE001
            log.warning("intraday failed for %s: %s", row.symbol, e)
            continue
        if not candles:
            log.warning("no 5m data for %s on %s (Yahoo keeps ~60 days)", row.symbol, target)
            continue
        intraday_with_data += 1
        res = analyze_intraday(candles, pc, open_t, cutoff_t, noon_t)
        if not res:
            continue
        morning_pct = res["at_cutoff_pct"] if args.up_mode == "at" else res["morning_high_pct"]
        up_ok = morning_pct >= args.up_pct
        noon_ok = args.noon_low <= res["noon_pct"] <= args.noon_high
        if up_ok and noon_ok:
            confirmed.append({
                "symbol": row.symbol, "name": row.name,
                "security_id": int(row.security_id),
                "prev_close": round(pc, 2), **res,
            })
        if n % 25 == 0:
            log.debug("  confirmed %d / scanned %d", len(confirmed), n)
    log.debug("Confirmed morning-spike + noon-fade: %d", len(confirmed))

    # No-data guard: "no matches" is only meaningful when there was data to
    # check. Never let a data-less run look like a normal empty result.
    almost_none = (intraday_checked >= 5
                   and intraday_with_data * 5 < intraday_checked)  # <20% had data
    if (day_high_coverage == 0
            or (intraday_checked and intraday_with_data == 0)
            or almost_none):
        raise RuntimeError(
            f"No usable market data for {target}: day highs available for "
            f"{day_high_coverage} of {len(universe)} universe symbols, "
            f"5-minute bars for {intraday_with_data} of {intraday_checked} "
            "candidates. The market may be closed (NSE holiday, weekend, or "
            "before open) or the data source has no data for this date — "
            "this is not a zero-match result."
        )

    # Market-cap gate (only on survivors).
    if confirmed and not args.no_mcap:
        caps = marketcap.get_market_caps([c["symbol"] for c in confirmed])
        for c in confirmed:
            c["mcap_cr"] = caps.get(c["symbol"])
        passed = [c for c in confirmed if c["mcap_cr"] and c["mcap_cr"] >= args.mcap_min]
        unknown = [c for c in confirmed if not c["mcap_cr"]]
    else:
        for c in confirmed:
            c["mcap_cr"] = None
        passed, unknown = confirmed, []

    passed.sort(key=lambda c: c["morning_high_pct"], reverse=True)
    _report(args, passed, unknown)
    _write_csv(args, passed)
    return {
        "passed": passed,
        "universe_symbols": len(universe),
        "day_high_coverage": day_high_coverage,
        "candidates": len(candidates),
        "intraday_checked": intraday_checked,
        "intraday_with_data": intraday_with_data,
    }


def _report(args, passed: list[dict], unknown: list[dict]) -> None:
    gate = f"@{args.cutoff}" if args.up_mode == "at" else f"high by {args.cutoff}"
    print("\n" + "=" * 96)
    print(f"  FADE SCREENER — {args.date}")
    print(f"  up > {args.up_pct}% ({gate})  →  faded to {args.noon_low}%..{args.noon_high}% "
          f"at {args.noon}  |  mcap > ₹{args.mcap_min:.0f} cr  (all % vs prev close)")
    print("=" * 96)
    if not passed:
        print("  No stocks matched.")
    else:
        print(f"  {'SYMBOL':<14}{'MCAP(cr)':>10}  {'HIGH%':>7} {'@time':>6}  "
              f"{'@9:45%':>7}  {'NOON%':>7}  {'PREVCL':>9}  NAME")
        print("  " + "-" * 92)
        for c in passed:
            mcap = f"{c['mcap_cr']:.0f}" if c.get("mcap_cr") else "n/a"
            print(f"  {c['symbol']:<14}{mcap:>10}  "
                  f"{c['morning_high_pct']:>6.2f}% {c['morning_high_time']:>6}  "
                  f"{c['at_cutoff_pct']:>6.2f}%  {c['noon_pct']:>6.2f}%  "
                  f"{c['prev_close']:>9.2f}  {c['name'][:28]}")
    if unknown:
        print(f"\n  Matched price criteria but market cap unknown (not in result): "
              f"{', '.join(c['symbol'] for c in unknown)}")
    print("=" * 88 + "\n")


def _write_csv(args, passed: list[dict]) -> None:
    out = cfg.OUTPUT_DIR / f"screener_{args.date}.csv"
    cols = ["symbol", "name", "security_id", "prev_close",
            "morning_high_pct", "morning_high_time", "at_cutoff_pct",
            "noon_price", "noon_pct", "mcap_cr"]
    with out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for c in passed:
            w.writerow({k: c.get(k) for k in cols})
    log.info("Wrote %d rows -> %s", len(passed), out)


def ask(question: str) -> str:
    """Answer a plain-English question via DeepSeek tool calling.

    Sends the question and the TOOL_SCHEMAS definitions to DeepSeek, lets the
    model choose tools and arguments, executes each chosen tool through
    _execute_tool(), and feeds results back until the model returns a final
    text answer. The loop stops after MAX_TOOL_ROUNDS rounds.

    The API key is read from the DEEPSEEK_API_KEY environment variable and is
    only ever used in the Authorization header — never printed or logged.
    """
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        return ("DEEPSEEK_API_KEY is not set in the environment. Export it "
                "in your terminal (it is not stored in this folder) and re-run.")

    tools = [
        {"type": "function", "function": {"name": name, **spec}}
        for name, spec in TOOL_SCHEMAS.items()
    ]
    today_str = datetime.now(IST).strftime("%A, %Y-%m-%d")
    system_prompt = (
        "You are an assistant for an NSE intraday fade screener. Answer the "
        "user's question by calling the provided tools with arguments that "
        "match each tool's schema, in whatever order you need. If a tool "
        "errors because your arguments were malformed, fix the arguments and "
        "retry. Once you have enough information, give a concise plain-text "
        "answer.\n"
        "\n"
        f"Today is {today_str} (IST).\n"
        "\n"
        "Date and parameter rules:\n"
        "- Before calling a tool with a date, check that the date is a "
        "weekday (Monday-Friday); the NSE does not trade on weekends.\n"
        "- If the user asks about a weekend date, or a tool reports that a "
        "date is an NSE holiday or not published, say so and ask the user "
        "for a trading day. Do not run any tool for that date.\n"
        "- If a screener or backtest tool returns a no-data error, tell "
        "the user exactly what it says: the market may be closed (NSE "
        "holiday, weekend, or before open), the data source has no data "
        "for the date or range, or Yahoo downloads were rate-limited. "
        "Never present that case as 'no stocks matched' or 'zero trades'; "
        "report zero matches or zero trades only when the tool returned "
        "status ok.\n"
        "- The backtest caps its lookback at 58 days; when the user asked "
        "for a longer window, state the actual start and end dates the "
        "tool returned.\n"
        "- Never change, loosen, or extend the dates, filters, or thresholds "
        "the user asked for, and never substitute a different date or year "
        "on your own. If a run errors or returns nothing, report that "
        "plainly and stop.\n"
        "\n"
        "Grounding rules for the final answer:\n"
        "- State only figures and facts that appear in the tool results. "
        "Quote the exact numbers the tools returned; never round, band, or "
        "generalize them (7.6% must not become '8 to 9%').\n"
        "- Never mention an attribute that no tool returned, such as "
        "liquidity, volume, sector, news, or why a move happened.\n"
        "- If a tool returns a missing value with a reason (e.g. a market "
        "cap with reason 'not found on NSE' or 'temporary fetch error'), "
        "state that reason for that symbol in the answer — name symbols "
        "that are not found on the NSE explicitly; never silently drop a "
        "symbol or report it as just null.\n"
        "- No commentary, interpretation, advice, or prediction, and no "
        "offers of further help. The only question you may ask the user is "
        "the trading-day question above; otherwise answer, then stop.\n"
        "\n"
        "Bar time convention used by the screener: intraday data is "
        "5-minute bars labeled by bar END time. A time T in a result is the "
        "bar covering T-5m to T: the 09:45 price is the close of the "
        "09:40-09:45 bar, the noon price is the close of the 11:55-12:00 "
        "bar, and morning_high_time is the end time of the bar that set the "
        "morning high. Whenever the answer cites the 09:45 price, the noon "
        "price, or a spike/high time, state this convention.\n"
        "\n"
        "Backtest rule, which differs from the screener rule: the backtest "
        "flags a spike when a 5-minute candle close in the 09:30-09:45 "
        "window reaches the spike threshold versus the previous close, "
        "checks the fade at the 12:30 close, and buys at the open of the "
        "12:30 bar; the screener instead uses the price at 09:45 and the "
        "price at noon. Whenever the answer reports backtest results, "
        "state this backtest rule.\n"
        "\n"
        "Answer format:\n"
        "- Present result data as a short markdown table with plain column "
        "names — never raw field names such as at_cutoff_pct or "
        "morning_high_pct — and the exact numbers the tool returned. Rename "
        "fields like: symbol -> Symbol, name -> Name, mcap_cr -> Market cap "
        "(₹ cr), market_cap_cr -> Market cap (₹ cr), reason -> Reason, "
        "prev_close -> Prev close, morning_high_pct -> Morning high "
        "%, morning_high_time -> High time, at_cutoff_pct -> At 09:45 %, "
        "noon_pct -> Noon %, noon_price -> Noon price, entry -> Entry "
        "price, fade_pct_vs_pc -> Fade % vs prev close, avg_pct -> Avg %, "
        "median_pct -> Median %, win_pct -> Win %, start_date -> Start "
        "date, end_date -> End date, universe_symbols -> Universe symbols, "
        "daily_data_symbols -> Symbols with daily data, "
        "spike_candidate_symbols -> Spike candidates, "
        "symbols_with_5m_data -> Symbols with 5m data, signals_pre_mcap -> "
        "Signals before market cap filter, trades_after_mcap -> Trades "
        "after market cap filter, market_cap_failures -> Market cap "
        "failures.\n"
        "- Before the table, at most one short plain-text line saying what "
        "it shows, using only parameters the tool returned (e.g. the date "
        "and thresholds of the run).\n"
        "- For a backtest answer, that line is exactly one plain sentence "
        "stating how the setup did, using only numbers the tool returned: "
        "whether buying the fades at 12:30 made or lost money on average "
        "(per_trade_close avg_pct), the share of winning trades "
        "(per_trade_close win_pct), and how many of the signal days were "
        "positive (day_stats positive_days out of days_with_signal). It "
        "may also name the window (start_date to end_date) and the number "
        "of trades (trades_after_mcap). Never include advice on what to "
        "trade.\n"
        "- No wording about the stocks beyond that line and the table: no "
        "descriptions, comparisons, rankings, or summaries of the rows.\n"
        "- If the answer has no table (a definition, an explanation, or an "
        "error relay), it is plain text only: no markdown symbols of any "
        "kind (no **, no *, no backticks, no headers, no bullet lists) and "
        "at most three sentences.\n"
        "- After the answer, give the 'Tool calls' list of every tool call "
        "you made in this conversation, in order, each with its arguments; "
        "write 'none' if you called no tool."
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": question},
    ]
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    for round_num in range(1, MAX_TOOL_ROUNDS + 1):
        try:
            resp = requests.post(
                DEEPSEEK_API_URL,
                headers=headers,
                json={"model": DEEPSEEK_MODEL, "messages": messages, "tools": tools},
                timeout=60,
            )
            resp.raise_for_status()
            payload = resp.json()
        except requests.RequestException as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            return (f"DeepSeek request failed "
                    f"(HTTP {status if status is not None else 'n/a'}, "
                    f"{e.__class__.__name__}).")
        except ValueError as e:
            return f"DeepSeek returned invalid JSON: {e}"

        try:
            msg = payload["choices"][0]["message"]
        except (KeyError, IndexError):
            return ("Unexpected DeepSeek response shape: "
                    + json.dumps(payload, default=str)[:300])

        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            return msg.get("content") or "(model returned no content)"

        messages.append({
            "role": "assistant",
            "content": msg.get("content"),
            "tool_calls": tool_calls,
        })
        for tc in tool_calls:
            fn = tc.get("function") or {}
            name = fn.get("name", "")
            try:
                tool_args = json.loads(fn.get("arguments") or "{}")
                log.debug("ask round %d: %s(%s)", round_num, name, tool_args)
                result = _execute_tool(name, tool_args)
            except Exception as e:  # noqa: BLE001
                result = {"status": "error",
                          "error": f"{e.__class__.__name__}: {e}"}
            messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id", ""),
                "content": json.dumps(result, default=str),
            })

    return (f"Stopped after {MAX_TOOL_ROUNDS} tool-calling rounds without a "
            "final answer from the model.")


def _execute_tool(name: str, arguments: dict) -> dict:
    """Execute a tool by name with arguments, returning the result dict."""
    from datetime import date, datetime, timedelta

    def _parse_date():
        s = arguments.get("date")
        if not s:
            return None, {"status": "error",
                          "error": "missing required argument: date (YYYY-MM-DD)"}
        try:
            return datetime.strptime(str(s), "%Y-%m-%d").date(), None
        except ValueError:
            return None, {"status": "error",
                          "error": f"invalid date {s!r}; expected YYYY-MM-DD"}

    if name == "load_nse_bhavcopy":
        d, err = _parse_date()
        if err:
            return err
        data = nse.load_bhavcopy(d)
        if not data:
            return {"status": "unavailable", "date": str(d),
                    "note": "no bhavcopy published for this date "
                            "(weekend/holiday or not yet published)"}
        wanted = [str(s).upper().strip() for s in (arguments.get("symbols") or [])]
        if wanted:
            result = {"status": "ok", "date": str(d),
                      "bhavcopy": {s: data[s] for s in wanted if s in data}}
            missing = [s for s in wanted if s not in data]
            if missing:
                result["missing_symbols"] = missing
            return result
        return {"status": "ok", "date": str(d), "symbols_total": len(data),
                "bhavcopy": data}

    if name == "get_prev_close_map":
        d, err = _parse_date()
        if err:
            return err
        lookback = int(arguments.get("lookback", 7))
        used, pc_map = nse.prev_close_map(d, lookback)
        wanted = [str(s).upper().strip() for s in (arguments.get("symbols") or [])]
        if wanted:
            result = {"status": "ok", "date_requested": str(d), "date_used": str(used),
                      "prev_close": {s: pc_map[s] for s in wanted if s in pc_map}}
            missing = [s for s in wanted if s not in pc_map]
            if missing:
                result["missing_symbols"] = missing
            return result
        return {"status": "ok", "date_requested": str(d), "date_used": str(used),
                "symbols_total": len(pc_map), "prev_close": pc_map}

    if name == "list_nse_universe":
        limit = int(arguments.get("limit", 0) or 0)
        uni = _load_universe()
        n = min(limit, 200) if limit > 0 else 100
        rows = uni.head(n)
        return {"status": "ok", "total_nse_eq_symbols": len(uni),
                "returned": len(rows),
                "note": f"showing first {len(rows)} of {len(uni)} NSE EQ symbols "
                        "(scrip-master order); pass a larger limit (max 200 "
                        "per call) to page through",
                "symbols": [{"symbol": r.symbol, "name": r.name}
                            for r in rows.itertuples(index=False)]}

    if name == "get_market_caps":
        symbols = [str(s).upper().strip() for s in (arguments.get("symbols") or [])]
        if not symbols:
            return {"status": "error",
                    "error": "missing required argument: symbols (list of NSE symbols)"}
        if len(symbols) > 50:
            return {"status": "error",
                    "error": f"{len(symbols)} symbols requested; max 50 per call"}
        # Check the NSE EQ scrip master first: it is the authoritative symbol
        # list, so not-found is decided locally instead of guessing from
        # yfinance's undifferentiated failures. Unlisted symbols are not
        # fetched at all.
        uni_syms = set(_load_universe()["symbol"].tolist())
        listed = [s for s in symbols if s in uni_syms]
        caps = marketcap.get_market_caps(listed) if listed else {}
        out: dict = {}
        for s in symbols:
            if s not in uni_syms:
                out[s] = {"market_cap_cr": None,
                          "reason": "not found on NSE (not in the NSE EQ scrip master)"}
            elif caps.get(s) is not None:
                out[s] = {"market_cap_cr": caps[s]}
            else:
                out[s] = {"market_cap_cr": None,
                          "reason": "temporary fetch error (no market cap from "
                                    "Yahoo for a listed NSE symbol)"}
        return {"status": "ok", "caps": out}

    if name == "run_intraday_screener":
        up_mode = arguments.get("up_mode", cfg.UP_MODE)
        if up_mode not in ("at", "high"):
            return {"status": "error", "error": "up_mode must be 'at' or 'high'"}
        ns = argparse.Namespace(
            date=str(arguments.get("date") or datetime.now(IST).strftime("%Y-%m-%d")),
            up_pct=float(arguments.get("up_pct", cfg.UP_PCT)),
            up_mode=up_mode,
            noon_low=float(arguments.get("noon_low", cfg.NOON_LOW)),
            noon_high=float(arguments.get("noon_high", cfg.NOON_HIGH)),
            mcap_min=float(arguments.get("mcap_min", cfg.MCAP_MIN_CR)),
            cutoff=str(arguments.get("cutoff", cfg.MORNING_CUTOFF)),
            noon=str(arguments.get("noon", cfg.NOON_TIME)),
            limit=int(arguments.get("limit", 0) or 0),
            refresh_master=bool(arguments.get("refresh_master", False)),
            no_mcap=bool(arguments.get("no_mcap", False)),
        )
        result = run(ns)
        return {"status": "ok",
                "params": {"date": ns.date, "up_pct": ns.up_pct,
                           "up_mode": ns.up_mode, "cutoff": ns.cutoff,
                           "noon": ns.noon, "noon_low": ns.noon_low,
                           "noon_high": ns.noon_high, "mcap_min": ns.mcap_min,
                           "limit": ns.limit, "no_mcap": ns.no_mcap},
                "matched": len(result["passed"]),
                "universe_symbols": result["universe_symbols"],
                "day_high_coverage": result["day_high_coverage"],
                "candidates": result["candidates"],
                "symbols_with_intraday_data": result["intraday_with_data"],
                "stocks": result["passed"]}

    if name == "run_fade_backtest":
        import fade_backtest as fb
        import pandas as pd
        import statistics as st
        days_requested = int(arguments.get("days", 58))
        up_pct = float(arguments.get("up_pct", 5.0))
        noon_high = float(arguments.get("noon_high", 3.0))
        mcap_min = float(arguments.get("mcap_min", 500.0))
        limit = int(arguments.get("limit", 0) or 0)
        refresh = bool(arguments.get("refresh", False))

        # Yahoo rejects 5m ranges of 60+ days (end is +1 day), so cap the
        # lookback. Downloads use the raw calendar bounds; the reported
        # window is snapped to actual NSE trading days (today only counts
        # once its session is complete).
        days = min(days_requested, 58)
        dl_start = date.today() - timedelta(days=days)
        dl_end = date.today() + timedelta(days=1)
        start = nse.next_trading_day(dl_start)
        end = nse.last_trading_day(date.today())
        syms = _load_universe(limit=limit)["symbol"].tolist()
        daily = fb.download_daily(syms, dl_start, dl_end, refresh)
        cand_syms = sorted(fb.spike_candidates(daily, up_pct))
        df5 = fb.download_5m(cand_syms, dl_start, dl_end, refresh)
        all_trades: list[dict] = []
        for sym in cand_syms:
            all_trades += fb.scan_and_trade(sym, df5.get(sym), daily[sym],
                                            up_pct, noon_high)
        caps = fb.market_caps(sorted({t["symbol"] for t in all_trades}), refresh)
        for t in all_trades:
            t["mcap_cr"] = caps.get(t["symbol"])

        # Coverage counts + no-data guard (mirror of the screener's): a
        # degraded run must error, never look like a valid zero-trade result.
        signal_syms = sorted({t["symbol"] for t in all_trades})
        mcap_failed = sum(1 for s in signal_syms if caps.get(s) is None)
        with_5m = sum(1 for s in cand_syms if s in df5 and not df5[s].empty)
        if ((syms and len(daily) * 2 < len(syms))
                or (cand_syms and with_5m == 0)
                or (len(cand_syms) >= 5 and with_5m * 2 < len(cand_syms))
                or (signal_syms and mcap_failed == len(signal_syms))
                or (len(signal_syms) >= 5 and mcap_failed * 2 > len(signal_syms))):
            raise RuntimeError(
                f"Backtest data incomplete for {start} to {end} ({days} days): "
                f"daily data for {len(daily)} of {len(syms)} universe symbols, "
                f"5-minute data for {with_5m} of {len(cand_syms)} spike-candidate "
                f"symbols, market cap failed for {mcap_failed} of "
                f"{len(signal_syms)} signal symbols. Yahoo downloads were "
                "likely rate-limited or rejected the range — this is not a "
                "zero-trade result. Re-run later, with refresh=true, or with "
                "a smaller limit."
            )
        passed = [t for t in all_trades
                  if t["mcap_cr"] and t["mcap_cr"] >= mcap_min]

        out = cfg.OUTPUT_DIR / "fade_backtest_trades.csv"
        if passed:
            pd.DataFrame(passed).sort_values(["date", "symbol"]).to_csv(out, index=False)

        def _stats(values, include_win=True):
            if not values:
                return None
            row = {"avg_pct": round(sum(values) / len(values), 2),
                   "median_pct": round(st.median(values), 2)}
            if include_win:  # win % is meaningless for MFE/MAE rows
                row["win_pct"] = round(100 * sum(1 for v in values if v > 0) / len(values), 1)
            return row

        horizons = ["13:00", "13:30", "14:00", "14:30", "15:00", "15:15",
                    "close", "MFE", "MAE"]
        summary = {h: _stats([t[h] for t in passed if t.get(h) is not None],
                             include_win=h not in ("MFE", "MAE"))
                   for h in horizons}
        closes = [t["close"] for t in passed]
        per_trade = _stats(closes) or {}
        if closes:
            per_trade["best_pct"] = max(closes)
            per_trade["worst_pct"] = min(closes)
        byday: dict = {}
        for t in passed:
            byday.setdefault(t["date"], []).append(t["close"])
        day_returns = [sum(v) / len(v) for v in byday.values()]
        day_stats = {"days_with_signal": len(byday),
                     "avg_day_return_pct": round(sum(day_returns) / len(day_returns), 2)
                     if day_returns else None,
                     "positive_days": f"{sum(1 for x in day_returns if x > 0)}/{len(day_returns)}"}
        keep = ("date", "symbol", "mcap_cr", "prev_close", "morning_high_pct",
                "entry", "fade_pct_vs_pc", "close")
        sample = [{k: t[k] for k in keep if k in t}
                  for t in sorted(passed, key=lambda t: (t["date"], t["symbol"]))[:20]]
        result = {"status": "ok",
                  "params": {"days": days, "up_pct": up_pct,
                             "noon_high": noon_high, "mcap_min": mcap_min,
                             "limit": limit, "refresh": refresh},
                  "start_date": str(start),
                  "end_date": str(end),
                  "universe_symbols": len(syms),
                  "daily_data_symbols": len(daily),
                  "spike_candidate_symbols": len(cand_syms),
                  "symbols_with_5m_data": with_5m,
                  "signals_pre_mcap": len(all_trades),
                  "market_cap_failures": mcap_failed,
                  "trades_after_mcap": len(passed),
                  "horizon_stats": summary,
                  "per_trade_close": per_trade,
                  "day_stats": day_stats,
                  "sample_trades_first_20": sample,
                  "trades_csv": str(out) if passed else None}
        if days_requested != days:
            result["note"] = (f"lookback capped from {days_requested} to "
                              f"{days} days (Yahoo 5-minute data limit)")
        elif not passed:
            result["note"] = "no trades passed all filters"
        return result

    return {"status": "unknown_tool", "name": name,
            "error": f"unknown tool {name!r}"}


def main() -> None:
    p = argparse.ArgumentParser(description="Intraday morning-spike / noon-fade screener")
    p.add_argument("--date", default=datetime.now(IST).strftime("%Y-%m-%d"),
                   help="Trading day YYYY-MM-DD (default: today)")
    p.add_argument("--up-pct", type=float, default=cfg.UP_PCT)
    p.add_argument("--up-mode", choices=["high", "at"], default=cfg.UP_MODE,
                   help="'high' = touched up-pct in 09:15-cutoff; 'at' = value at cutoff")
    p.add_argument("--noon-low", type=float, default=cfg.NOON_LOW,
                   help="noon band low %% vs prev close (default -2)")
    p.add_argument("--noon-high", type=float, default=cfg.NOON_HIGH,
                   help="noon band high %% vs prev close (default 3)")
    p.add_argument("--mcap-min", type=float, default=cfg.MCAP_MIN_CR, help="₹ crore")
    p.add_argument("--cutoff", default=cfg.MORNING_CUTOFF, help="morning HH:MM")
    p.add_argument("--noon", default=cfg.NOON_TIME, help="noon HH:MM")
    p.add_argument("--limit", type=int, default=0, help="scan only first N (debug)")
    p.add_argument("--refresh-master", action="store_true", help="re-download instrument master")
    p.add_argument("--no-mcap", action="store_true", help="skip market-cap filter (debug)")
    p.add_argument("--ask", dest="ask_question", default=None, help="Ask a plain-English question for LLM-augmented analysis")
    p.add_argument("--verbose", action="store_true", help="show debug-level progress lines (default: warnings and errors only)")
    args = p.parse_args()

    # Default: warnings and errors from any logger, plus this project's INFO
    # stage lines (e.g. where the CSV was written). --verbose adds this
    # project's DEBUG progress lines; third-party DEBUG noise (yfinance,
    # urllib3, ...) stays hidden either way.
    logging.basicConfig(level=logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    for _name in ("screener", "fade_bt"):
        logging.getLogger(_name).setLevel(logging.DEBUG if args.verbose else logging.INFO)

    if args.ask_question:
        answer = ask(args.ask_question)
        print("\n" + "=" * 96)
        print("  ASK ANSWER")
        print("=" * 96)
        print(answer)
        print("=" * 98 + "\n")
    else:
        run(args)


if __name__ == "__main__":
    main()
