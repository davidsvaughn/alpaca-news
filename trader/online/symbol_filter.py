"""Symbol-level filtering: blacklist + positive US equity/ETF validation.

Maintains a persistent JSON file (``data/knowledge/symbol_lists.json``) with:
- **blacklist**: manually curated symbols that should never be traded (e.g. SPY, VIX)
- **verified**: auto-populated symbols confirmed as tradeable US equities/ETFs
- **crypto**: auto-populated cryptocurrency tickers
- **rejected**: auto-populated non-tradeable symbols (indices, futures, forex, etc.)

Unknown symbols are checked eagerly via ``yfinance.Ticker(sym).info``.
Results are cached so subsequent encounters are instant (no yfinance call).

Positive filter: ``quoteType in ("EQUITY", "ETF") and market == "us_market"``
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time

log = logging.getLogger(__name__)
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trader.market.alpaca_env import get_alpaca_account_env

_file_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Alpaca tradeable-symbol cache (bulk-loaded, refreshed periodically)
# ---------------------------------------------------------------------------
_alpaca_tradeable: set[str] | None = None
_alpaca_loaded_at: float = 0.0
_ALPACA_REFRESH_SECONDS = 24 * 3600  # refresh once per day


def _load_alpaca_tradeable() -> set[str] | None:
    """Bulk-fetch all active tradeable US equity symbols from Alpaca.

    Returns None if credentials are unavailable or the call fails.
    """
    try:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.enums import AssetClass, AssetStatus
        from alpaca.trading.requests import GetAssetsRequest

        api_key = get_alpaca_account_env("ALPACA_API_KEY", 1)
        secret_key = get_alpaca_account_env("ALPACA_SECRET_KEY", 1)
        if not api_key or not secret_key:
            return None

        client = TradingClient(api_key=api_key, secret_key=secret_key, paper=True)
        assets = client.get_all_assets(
            filter=GetAssetsRequest(
                status=AssetStatus.ACTIVE,
                asset_class=AssetClass.US_EQUITY,
            )
        )
        tradeable = {a.symbol for a in assets if a.tradable}
        log.info("ALPACA ASSETS: loaded %d tradeable US equity symbols", len(tradeable))
        return tradeable
    except Exception as e:
        log.warning("ALPACA ASSETS: could not load tradeable symbols: %s", e)
        return None


def get_alpaca_tradeable() -> set[str] | None:
    """Return cached set of Alpaca-tradeable symbols, refreshing if stale."""
    global _alpaca_tradeable, _alpaca_loaded_at
    if _alpaca_tradeable is None or (time.monotonic() - _alpaca_loaded_at) > _ALPACA_REFRESH_SECONDS:
        result = _load_alpaca_tradeable()
        if result is not None:
            _alpaca_tradeable = result
            _alpaca_loaded_at = time.monotonic()
    return _alpaca_tradeable

DEBUG = os.getenv("DEBUG", "false").lower() in ("true", "1")

# Seed blacklist — symbols that should never be considered for buying.
_DEFAULT_BLACKLIST = ["SPY", "VIX"]

# Positive filter: only these quoteTypes on us_market are tradeable.
_ALLOWED_QUOTE_TYPES = {"EQUITY", "ETF"}
_ALLOWED_MARKET = "us_market"

# Known US exchanges — symbols tagged with these can be auto-verified.
_US_EXCHANGES = {"NYSE", "NASDAQ", "AMEX", "ARCA", "BATS", "CBOE", "OTC"}

# Regex for symbol formats that are obviously not US equities/ETFs.
# Matches: crypto pairs (BTCUSD, DOGEUSD), futures (NG1!, CL1!),
# indices/economics with underscores (SP_IPSA, FED30D), numeric-only (603993).
_NON_EQUITY_PATTERN = re.compile(
    r"^[A-Z]{2,10}USD$"   # crypto pair: BTCUSD, ETHUSD, DOGEUSD
    r"|!$"                 # futures: NG1!, CL1!
    r"|_"                  # indices/economics: SP_IPSA, FED30D
    r"|^\d+$"              # numeric-only (Chinese exchanges, etc.)
)

_SYMBOL_LISTS_FILENAME = "symbol_lists.json"


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _lists_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / "knowledge" / _SYMBOL_LISTS_FILENAME


def load_symbol_lists(data_dir: str | Path) -> dict[str, Any]:
    """Load symbol lists from disk, creating defaults if missing."""
    path = _lists_path(data_dir)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    # Create defaults
    data: dict[str, Any] = {
        "blacklist": list(_DEFAULT_BLACKLIST),
        "verified": [],
        "crypto": [],
        "rejected": [],
        "last_updated": _utc_now_iso(),
    }
    _save_symbol_lists(data_dir, data)
    return data


def _save_symbol_lists(data_dir: str | Path, data: dict[str, Any]) -> None:
    path = _lists_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    data["last_updated"] = _utc_now_iso()
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _add_to_list(data_dir: str | Path, list_name: str, symbol: str) -> None:
    """Thread-safe append of a symbol to a named list."""
    with _file_lock:
        data = load_symbol_lists(data_dir)
        lst = data.get(list_name, [])
        sym = symbol.upper()
        if sym not in lst:
            lst.append(sym)
            data[list_name] = lst
            _save_symbol_lists(data_dir, data)


def _classify_symbol(symbol: str, data_dir: str | Path) -> str:
    """Query yfinance to classify a symbol. Returns a reason string.

    Returns:
        "verified" if tradeable US equity/ETF,
        "crypto" if cryptocurrency,
        "not_us_tradeable" for anything else (index, futures, forex, foreign, etc.),
        "lookup_failed" if yfinance call fails.
    """
    sym = symbol.upper()
    try:
        import yfinance as yf

        ticker = yf.Ticker(sym)
        info = ticker.info or {}
        quote_type = info.get("quoteType", "")
        market = info.get("market", "")

        if quote_type == "CRYPTOCURRENCY":
            _add_to_list(data_dir, "crypto", sym)
            return "crypto"

        if quote_type in _ALLOWED_QUOTE_TYPES and market == _ALLOWED_MARKET:
            _add_to_list(data_dir, "verified", sym)
            return "verified"

        # Not tradeable — cache it so we don't re-check
        _add_to_list(data_dir, "rejected", sym)
        return "not_us_tradeable"

    except Exception:
        if DEBUG:
            raise
        # yfinance failure → allow through (conservative: don't block unknowns)
        return "lookup_failed"


def filter_symbols(
    symbols: list[str],
    data_dir: str | Path,
    symbol_exchanges: dict[str, str] | None = None,
) -> tuple[list[str], list[dict[str, str]]]:
    """Filter symbols to only tradeable US equities/ETFs.

    Checks in order: blacklist → explicit non-US exchange (reject)
    → explicit US exchange (auto-verify) → verified cache → crypto cache
    → rejected cache → non-equity format (reject) → yfinance lookup (result cached).

    Exchange info from the news source takes priority over cached classifications
    because a bare ticker like "RS" may be verified as a US equity (Reliance Steel)
    but the *current* article may refer to RS PCL on Thailand's SET exchange.

    Args:
        symbol_exchanges: optional mapping of symbol → exchange (e.g. {"BRK.A": "NYSE"}).
            Symbols tagged with a known US exchange are auto-verified without yfinance.

    Returns:
        (kept_symbols, filtered_reasons) where filtered_reasons is a list of
        ``{"symbol": ..., "reason": ...}`` dicts for each removed symbol.
    """
    data = load_symbol_lists(data_dir)
    blacklist = set(s.upper() for s in data.get("blacklist", []))
    verified = set(s.upper() for s in data.get("verified", []))
    crypto_set = set(s.upper() for s in data.get("crypto", []))
    rejected = set(s.upper() for s in data.get("rejected", []))
    exchanges = symbol_exchanges or {}

    kept: list[str] = []
    filtered: list[dict[str, str]] = []

    for sym in symbols:
        s = sym.upper()

        # 1. Blacklist (manual, highest priority)
        if s in blacklist:
            filtered.append({"symbol": s, "reason": "blacklisted"})
            continue

        # 2. Explicit exchange from news source — takes priority over caches.
        #    A bare ticker like "RS" may be cached as verified (Reliance Steel)
        #    but this article's SET:RS refers to RS PCL in Thailand.
        exchange = exchanges.get(s, exchanges.get(sym, "")).upper()
        if exchange:
            if exchange in _US_EXCHANGES:
                # Known US exchange → auto-verify and cache
                _add_to_list(data_dir, "verified", s)
                kept.append(sym)
            else:
                # Non-US exchange → reject immediately (no yfinance call)
                filtered.append({"symbol": s, "reason": f"non_us_exchange:{exchange}"})
            continue

        # 3. Already verified as tradeable
        if s in verified:
            kept.append(sym)
            continue

        # 4. Already known crypto
        if s in crypto_set:
            filtered.append({"symbol": s, "reason": "crypto"})
            continue

        # 5. Already known non-tradeable
        if s in rejected:
            filtered.append({"symbol": s, "reason": "not_us_tradeable"})
            continue

        # 6. Obviously non-equity format (crypto pairs, futures, indices, numeric)
        if _NON_EQUITY_PATTERN.search(s):
            _add_to_list(data_dir, "rejected", s)
            filtered.append({"symbol": s, "reason": "non_equity_format"})
            continue

        # 7. Unknown — classify via yfinance
        result = _classify_symbol(s, data_dir)
        if result == "verified":
            kept.append(sym)
        elif result == "lookup_failed":
            # Conservative: allow through if we can't check
            kept.append(sym)
        else:
            filtered.append({"symbol": s, "reason": result})

    # 8. Alpaca tradeable check — reject symbols not active on Alpaca.
    #    This is a bulk-cached set (refreshed daily), so no per-symbol API call.
    alpaca_set = get_alpaca_tradeable()
    if alpaca_set is not None:
        final_kept: list[str] = []
        for sym in kept:
            if sym.upper() in alpaca_set:
                final_kept.append(sym)
            else:
                filtered.append({"symbol": sym.upper(), "reason": "not_alpaca_tradeable"})
        kept = final_kept

    if filtered:
        reasons = ", ".join(f"{f['symbol']}({f['reason']})" for f in filtered)
        log.debug("SYMBOL FILTER: removed %s", reasons)

    return kept, filtered
