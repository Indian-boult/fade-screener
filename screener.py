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
"""

import argparse
import csv
import logging
from datetime import datetime, time, timedelta, timezone

import auth
import config as cfg
import marketcap
import nse
from dhan import DhanAPI

IST = timezone(timedelta(hours=5, minutes=30))
log = logging.getLogger("screener")


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


def run(args) -> None:
    open_t, cutoff_t, noon_t = _hhmm(cfg.MARKET_OPEN), _hhmm(args.cutoff), _hhmm(args.noon)
    target = datetime.strptime(args.date, "%Y-%m-%d").date()

    token = auth.ensure_token()
    api = DhanAPI(token)

    universe = api.get_universe(force=args.refresh_master)
    if args.limit:
        universe = universe.head(args.limit)
    log.info("Universe: %d NSE EQ stocks", len(universe))

    # Correct previous-close baseline (run-time independent) from NSE bhavcopy.
    prev_dt, prev_close = nse.prev_close_map(target)
    log.info("Prev-close baseline: NSE bhavcopy %s (%d symbols)", prev_dt, len(prev_close))

    # Day-high for the cheap pre-filter: bhavcopy if the day is done, else
    # live Dhan OHLC (today, intraday).
    day_bhav = nse.load_bhavcopy(target)
    live = {}
    if day_bhav:
        log.info("Day-high source: NSE bhavcopy %s (%d symbols)", target, len(day_bhav))
    else:
        log.info("Bhavcopy for %s not published yet — using live Dhan OHLC for day high", target)
        live = api.ohlc_batch([int(r.security_id) for r in universe.itertuples(index=False)])

    # Pre-filter: day high already >= +UP_PCT% vs (correct) prev close.
    candidates = []
    no_base = 0
    for r in universe.itertuples(index=False):
        sym, sid = r.symbol, int(r.security_id)
        pc = prev_close.get(sym)
        if not pc or pc <= 0:
            no_base += 1
            continue
        if day_bhav:
            row = day_bhav.get(sym)
            day_high = row["high"] if row else None
        else:
            snap = live.get(sid)
            day_high = snap["day_high"] if snap else None
        if not day_high:
            continue
        day_high_pct = (day_high / pc - 1) * 100
        if day_high_pct >= args.up_pct:
            candidates.append((sid, r, pc, round(day_high_pct, 2)))
    candidates.sort(key=lambda x: x[3], reverse=True)
    log.info("Pre-filter (day high >= +%.1f%%): %d candidates (%d had no prev close)",
             args.up_pct, len(candidates), no_base)

    # Confirm via intraday candles.
    confirmed = []
    for n, (sid, row, pc, day_high_pct) in enumerate(candidates, 1):
        try:
            candles = api.intraday(sid, args.date)
        except Exception as e:  # noqa: BLE001
            log.warning("intraday failed for %s: %s", row.symbol, e)
            continue
        res = analyze_intraday(candles, pc, open_t, cutoff_t, noon_t)
        if not res:
            continue
        morning_pct = res["at_cutoff_pct"] if args.up_mode == "at" else res["morning_high_pct"]
        up_ok = morning_pct >= args.up_pct
        noon_ok = args.noon_low <= res["noon_pct"] <= args.noon_high
        if up_ok and noon_ok:
            confirmed.append({
                "symbol": row.symbol, "name": row.name, "security_id": sid,
                "prev_close": round(pc, 2), **res,
            })
        if n % 25 == 0:
            log.info("  confirmed %d / scanned %d", len(confirmed), n)
    log.info("Confirmed morning-spike + noon-fade: %d", len(confirmed))

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
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    run(args)


if __name__ == "__main__":
    main()
