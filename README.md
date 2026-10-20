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

## Data sources (all public, no credentials)

| What | Source |
|------|--------|
| Universe | The public scrip-master CSV (`api-scrip-master-detailed.csv` on Dhan's CDN — a plain download, no auth), filtered to NSE EQ equities (~2,700); cached in `data/scrip_master.csv` for the day. |
| Previous close | The NSE daily bhavcopy (`sec_bhavdata_full` from nsearchives.nseindia.com), cached in `data/bhav_*.csv`. A broker live feed's "close" rolls to the current day after the session, which silently corrupts every percentage — the bhavcopy is run-time independent. Files whose internal DATE1 does not match the requested day are refused (the archive serves the latest file for missing dates). |
| Day high | The same bhavcopy once the session is done; for a live run today (before publication) Yahoo daily bars via yfinance, keeping only bars actually dated today. A 30-symbol probe skips the full sweep when the market is closed. |
| Intraday bars | Yahoo Finance 5-minute bars (yfinance), timestamps shifted to bar **end** time (see the convention below). Yahoo keeps ~60 days. |
| Market cap | Yahoo via yfinance (`fast_info`/`info`), cached in `data/market_cap_cache.json`. Failed lookups are never cached. |

## Error behaviour

- Weekends and future dates raise immediately ("2026-10-18 is a Sunday — the
  NSE does not trade or publish bhavcopy on weekends").
- A past weekday with no bhavcopy of its own (NSE holiday) raises instead of
  silently serving an older day's file.
- Today before publication reports "not published yet"; the day-high pre-filter
  falls back to live Yahoo bars, probing a 30-symbol sample first.
- The screener and backtest raise a no-data error when coverage collapses
  (closed market, rate-limited downloads) — never a silent "zero matches" or
  "zero trades".

## Ask command

Plain-English questions answered by DeepSeek tool calling: the model picks the
tools (`run_intraday_screener`, `run_fade_backtest`, `get_market_caps`,
`load_nse_bhavcopy`, `get_prev_close_map`, `list_nse_universe`), the tool layer
executes them, and results feed back until the model answers. The loop stops
after 8 rounds.

```bash
export DEEPSEEK_API_KEY=...   # your key, in your terminal only
python screener.py --ask "show me stocks that spiked 5% or more by 9:45am and faded back to flat by noon on 16 October"
python screener.py --ask "how did the fade setup do over the last 60 days?"
```

- `DEEPSEEK_API_KEY` is read from the environment at run time and only ever
  sent in the Authorization header — never printed, logged, or stored in this
  folder.
- Answers state only figures a tool returned, open backtest answers with one
  plain sentence on how the setup did (no trading advice), then a table with
  plain column names, then the list of tool calls made.

### Bar time convention

Intraday data is 5-minute bars labelled by bar **end** time: the "09:45" price
is the close of the 09:40–09:45 bar, the "noon" price is the close of the
11:55–12:00 bar, and `morning_high_time` is the end time of the bar that set
the morning high.

### Screener rule vs backtest rule

- **Screener**: the price **at 09:45** ≥ +5% and the price **at 12:00** inside
  −2…+3% vs prev close.
- **Backtest**: a 5-minute candle **close in the 09:30–09:45 window** ≥ +5%
  (Chartink-style), the fade checked at the **12:30 close** (< +3%), entry at
  the 12:30 bar open; forward returns to 13:00…15:15 and close, plus MFE/MAE.

The rules differ, so a stock the screener flags can be absent from the
backtest (e.g. still above +3% at the 12:30 close) — backtest answers state
the rule they used.

### Verbosity

Default logging shows warnings and errors (from any logger) plus one INFO line
per stage — e.g. where the CSV was written. `--verbose` (on both
`screener.py` and `fade_backtest.py`) adds this project's debug-level progress
(tool rounds, chunk-by-chunk downloads) without third-party debug noise.

## Setup

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
```

Copy `.env.example` to `.env`. The only variable the ask command needs is
`DEEPSEEK_API_KEY`. The Dhan lines are read only by legacy code paths
(`fade_backtest.py`'s universe loader reads the token but ignores its value —
the stock list comes from the public CSV), so they can stay blank. `.env` and
`data/` are git-ignored.

## Run

```bash
python screener.py                      # today, defaults (>5% @09:45, fade to -2%..+3% @noon)
python screener.py --date 2026-10-16    # a specific trading day
python screener.py --up-mode high       # use the 09:15-09:45 high instead of the 09:45 value
python screener.py --limit 200          # scan only first 200 (debug)
python screener.py --no-mcap            # skip market-cap gate (debug)
python fade_backtest.py                 # 58-day backtest (capped: Yahoo 5m limit)
python fade_backtest.py --refresh       # ignore caches, re-download
```

Results print to console and are written to `data/results/screener_<date>.csv`
and `data/results/fade_backtest_trades.csv`.

## Tests

```bash
.venv/bin/python3 test_tools.py
```

Stdlib `unittest` against the tool layer only — never DeepSeek. Covers the
known screener days (15/16/19 October), weekend/holiday errors, unknown-symbol
reasons, and the backtest window/stats rules. The hard-coded dates depend on
5-minute data still being available (~60 days on Yahoo).
