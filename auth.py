"""
Dhan token management.

Verifies the saved access token and, if expired, regenerates one via TOTP
(same flow trade_ai uses). The fresh token is written back to trade_ai/.env
so every tool stays in sync.
"""

import re
import time

import pyotp
import requests

import config as cfg


def check_token(token: str) -> bool:
    """Return True if the access token is currently valid."""
    try:
        r = requests.get(
            f"{cfg.API_BASE}/profile",
            headers={"access-token": token},
            timeout=10,
        )
        return r.status_code == 200
    except requests.RequestException:
        return False


def _generate_token_totp() -> str | None:
    """Generate a fresh 24h token using TOTP + PIN. Returns token or None."""
    if not cfg.TOTP_SECRET or not cfg.PIN:
        return None

    totp = pyotp.TOTP(cfg.TOTP_SECRET)
    last_code = None
    for _ in range(3):
        # The TOTP code is valid for a 30s window; start of a fresh window
        # avoids the code expiring mid-request.
        offset = int(time.time()) % 30
        if offset > 5:
            time.sleep(30 - offset + 2)

        code = totp.now()
        if code == last_code:
            time.sleep(31)
            code = totp.now()
        last_code = code

        url = (
            f"{cfg.AUTH_BASE}/app/generateAccessToken"
            f"?dhanClientId={cfg.CLIENT_ID}&pin={cfg.PIN}&totp={code}"
        )
        try:
            r = requests.post(url, timeout=15)
        except requests.RequestException:
            continue
        if r.status_code == 200:
            tok = r.json().get("accessToken", "")
            if tok:
                return tok
    return None


def _save_token(token: str) -> None:
    """Persist a new token into trade_ai/.env and refresh in-memory copy."""
    content = cfg.ENV_PATH.read_text()
    updated = re.sub(r"(DHAN_ACCESS_TOKEN=).*", rf"\g<1>{token}", content)
    if updated == content:
        updated += f"\nDHAN_ACCESS_TOKEN={token}\n"
    cfg.ENV_PATH.write_text(updated)
    cfg.ACCESS_TOKEN = token


def ensure_token() -> str:
    """Return a valid access token, regenerating via TOTP if needed.

    Raises RuntimeError with actionable guidance if no valid token can be
    obtained (e.g. expired token + stale TOTP secret).
    """
    if cfg.ACCESS_TOKEN and check_token(cfg.ACCESS_TOKEN):
        return cfg.ACCESS_TOKEN

    token = _generate_token_totp()
    if token and check_token(token):
        _save_token(token)
        return token

    raise RuntimeError(
        "No valid Dhan access token.\n"
        "  The saved token is expired and TOTP regeneration failed.\n"
        f"  Fix one of these in {cfg.ENV_PATH}:\n"
        "    - Paste a fresh DHAN_ACCESS_TOKEN (Dhan web > Profile > "
        "DhanHQ Trading APIs > generate), or\n"
        "    - Correct DHAN_TOTP_SECRET so auto-generation works."
    )
