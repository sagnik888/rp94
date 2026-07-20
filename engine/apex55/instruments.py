"""
apex_emacross/instruments.py
Instrument auto-detection from symbol strings.
Mirrors Pine Script Section 2 exactly.
"""


def detect_instrument(symbol: str, sym_type: str = "") -> str:
    """
    Auto-detect instrument class from a symbol ticker string.

    Args:
        symbol:   Ticker string (e.g. "BANKNIFTY", "RELIANCE", "BTCUSDT").
        sym_type: Optional asset class hint ("futures", "forex", "crypto",
                  "index", "equity") — matches Pine's syminfo.type.

    Returns:
        One of: Bank Nifty | Nifty 50 | FinNifty | Midcap | Sensex |
                Index | Stock | Crypto | Forex | Commodity | Futures
    """
    tic = symbol.lower()
    pfx = sym_type.lower()

    # ── NSE Indices ───────────────────────────────────────────────────
    if "banknifty" in tic or "bank_nifty" in tic or "banknf" in tic:
        return "Bank Nifty"

    if "finnifty" in tic or "fin_nifty" in tic or "finnf" in tic:
        return "FinNifty"

    if "midcap" in tic:
        return "Midcap"

    if "sensex" in tic or "bankex" in tic:
        return "Sensex"

    # Nifty 50 — exclude all sector / thematic Nifty variants
    _nifty_sector_kw = [
        "100", "200", "500", "smallcap", "it", "auto", "pharma",
        "metal", "realty", "energy", "infra", "media", "fmcg",
        "psu", "next",
    ]
    if "nifty50" in tic or (
        "nifty" in tic and not any(kw in tic for kw in _nifty_sector_kw)
    ):
        return "Nifty 50"

    # ── Crypto ────────────────────────────────────────────────────────
    _crypto_kw = [
        "btc", "eth", "bnb", "sol", "ada", "xrp", "doge", "shib",
        "usdt", "usdc", "ltc", "dot",
    ]
    if sym_type == "crypto" or any(kw in tic for kw in _crypto_kw):
        return "Crypto"

    # ── Forex ─────────────────────────────────────────────────────────
    _forex_kw = [
        "eurusd", "gbpusd", "usdjpy", "audusd", "usdcad", "usdchf",
        "nzdusd", "usdinr", "eurgbp", "eurjpy", "gbpjpy", "usdsgd",
        "usdmxn", "usdtry", "usdzar", "usdhkd",
    ]
    _forex_brokers = ["oanda", "pepperstone"]
    if (
        sym_type == "forex"
        or any(b in pfx for b in _forex_brokers)
        or any(kw in tic for kw in _forex_kw)
    ):
        return "Forex"

    # ── Commodities / MCX ─────────────────────────────────────────────
    _comm_kw = [
        "gold", "silver", "crude", "crudeoil", "copper", "naturalgas",
        "natgas", "xauusd", "xagusd", "xptusd", "xpdusd", "usoil",
        "ukoil", "wti", "brent", "aluminum", "aluminium", "nickel",
        "zinc", "lead", "cotton", "mentha", "palmolein", "castor",
        "rubber", "wheat", "corn", "soybean",
    ]
    if "mcx" in pfx or any(kw in tic for kw in _comm_kw):
        return "Commodity"

    # ── Futures ───────────────────────────────────────────────────────
    if sym_type == "futures":
        return "Futures"

    # ── Generic Index ────────────────────────────────────────────────
    if sym_type == "index":
        return "Index"

    # ── Default: Equity Stock ─────────────────────────────────────────
    return "Stock"
