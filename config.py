"""
Screener configuration.

Dhan credentials (client id, access token, TOTP secret, PIN) come from a
.env next to this file (copy .env.example). Without one, the sibling
trade_ai/.env is reused so we don't duplicate secrets.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
_LOCAL_ENV = ROOT / ".env"
_TRADE_AI_ENV = ROOT.parent / "trade_ai" / ".env"
ENV_PATH = _TRADE_AI_ENV if not _LOCAL_ENV.exists() and _TRADE_AI_ENV.exists() else _LOCAL_ENV
load_dotenv(ENV_PATH, override=True)

CLIENT_ID: str = os.getenv("DHAN_CLIENT_ID", "")
ACCESS_TOKEN: str = os.getenv("DHAN_ACCESS_TOKEN", "")
TOTP_SECRET: str = os.getenv("DHAN_TOTP_SECRET", "")
PIN: str = os.getenv("DHAN_PIN", "")

API_BASE = "https://api.dhan.co/v2"
AUTH_BASE = "https://auth.dhan.co"

# Dhan instrument master (detailed format has readable column names).
SCRIP_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"

# Local data / cache locations.
DATA_DIR = ROOT / "data"
SCRIP_MASTER_CACHE = DATA_DIR / "scrip_master.csv"
MARKET_CAP_CACHE = DATA_DIR / "market_cap_cache.json"
OUTPUT_DIR = DATA_DIR / "results"

# Screen parameters (overridable via CLI). All % are vs previous day close.
UP_PCT = 5.0            # morning move must be at least this %
UP_MODE = "at"          # "at"   = value AT the cutoff (09:45) is >=UP_PCT;
                        # "high" = touched >=UP_PCT any time in [open, cutoff]
# "Faded by noon" band: stock has pulled back into this % range at noon
# (not necessarily negative — e.g. spiked +6% then sits at -2%..+3%).
NOON_LOW = -2.0
NOON_HIGH = 3.0
MCAP_MIN_CR = 500.0     # minimum market cap in ₹ crore
MORNING_CUTOFF = "09:45"  # morning reference time
NOON_TIME = "12:00"       # noon reference time
MARKET_OPEN = "09:15"
INTRADAY_INTERVAL = "1"   # 1-minute candles

# Market-cap cache is considered fresh for this many days.
MCAP_CACHE_DAYS = 7

for _d in (DATA_DIR, OUTPUT_DIR):
    _d.mkdir(parents=True, exist_ok=True)


def headers(token: str | None = None) -> dict:
    """Build Dhan request headers with the given (or current) token."""
    return {
        "access-token": token or ACCESS_TOKEN,
        "client-id": CLIENT_ID,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
