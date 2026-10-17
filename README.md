# Fade Screener

Finds NSE stocks (market cap > ₹500 cr) that were **> 5% up at 09:45 but had
faded back to roughly flat (−2% to +3%) by noon** — the classic morning-spike
fade. All percentages are vs the **previous day's close**.

## Logic

| Gate | Rule |
|------|------|
| Up   | value **at 09:45** ≥ **+5%** vs prev close (`--up-mode high` instead uses the intraday high anytime in 09:15–09:45) |
| Fade | price at `--noon` (12:00) is within the band **−2% … +3%** vs prev close (not necessarily negative) |
| Size | market cap **> ₹500 cr** |

Tune the band with `--noon-low` / `--noon-high` and the morning bar with
`--up-pct` / `--up-mode` / `--cutoff`.

## How it works

1. **Universe** — NSE EQ stocks from the Dhan instrument master (~2,400).
2. **Previous-close baseline** — from the **NSE daily bhavcopy**
   (`sec_bhavdata_full`), cached in `data/`. This is the correct reference for
   every percentage and is run-time independent (see the gotcha below).
3. **Pre-filter** — keep only stocks whose **day high** already cleared +5% vs
   prev close (cheap; avoids thousands of intraday calls). Day high comes from
   the bhavcopy when the session is done, or live Dhan `/marketfeed/ohlc` for an
   intraday run today.
4. **Confirm** — `/charts/intraday` 1-min candles for survivors verify the
   +5% at 09:45 **and** the noon level is inside the fade band.
5. **Market cap** — looked up (yfinance, cached) only for the final survivors.

### Two gotchas this handles

- **Dhan has no market cap.** Verified against the scrip master, market quote,
  historical, intraday and live-feed docs. So market cap is sourced via
  yfinance and cached in `data/market_cap_cache.json`.
- **Dhan's `ohlc.close` is NOT a reliable previous close.** During market hours
  it is the previous close, but after the session it rolls to *today's* close —
  which silently inflates every percentage if used as the baseline. The
  previous close therefore comes from the NSE bhavcopy, not from Dhan.

## Setup

```bash
conda activate trade_feb
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in your Dhan credentials
(`DHAN_CLIENT_ID`, `DHAN_ACCESS_TOKEN`, `DHAN_TOTP_SECRET`, `DHAN_PIN`).
If there is no local `.env`, `../trade_ai/.env` is used instead. An expired
token is auto-regenerated via TOTP; if that fails, paste a fresh
`DHAN_ACCESS_TOKEN`. `.env` and `data/` are git-ignored.

## Run

```bash
python screener.py                      # today, defaults (>5% @09:45, fade to -2%..+3% @noon)
python screener.py --date 2026-06-02    # a specific trading day
python screener.py --noon-low -2 --noon-high 3 --up-pct 5 --cutoff 09:45 --noon 12:00
python screener.py --up-mode high       # use the 09:15-09:45 high instead of the 09:45 value
python screener.py --limit 200          # scan only first 200 (debug)
python screener.py --no-mcap            # skip market-cap gate (debug)
```

Results print to console and are written to `data/results/screener_<date>.csv`.
