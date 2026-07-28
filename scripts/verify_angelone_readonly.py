"""Non-mutating AngelOne login/data/account-read capability probe."""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.settings import get_settings
from data.market_data import INDEX_SPOT_TOKENS, INDEX_STRIKE_INTERVALS, MarketDataProvider, SmartWebSocketV2


def _summary(response):
    if not isinstance(response, dict):
        return {"transport_ok": False, "type": type(response).__name__}
    data = response.get("data")
    rows = len(data) if isinstance(data, list) else (
        len(data.get("fetched"))
        if isinstance(data, dict) and isinstance(data.get("fetched"), list)
        else None
    )
    return {
        "transport_ok": True,
        "status": response.get("status"),
        "error_code": response.get("errorCode") or response.get("errorcode") or "",
        "message": str(response.get("message") or "")[:160],
        "rows": rows,
    }


def main() -> int:
    settings = get_settings()
    report = {
        "probe": "angelone_readonly",
        "mutations_attempted": 0,
        "login": False,
        "tls_verification": True,
        "websocket_sdk_available": bool(SmartWebSocketV2),
        "instrument_master_rows": 0,
        "spot_quotes": {},
        "historical_candles": {},
        "option_contracts": {},
        "option_market_data": {},
        "execution_methods_available": {},
        "positions": {},
        "order_book": {},
    }
    provider = MarketDataProvider(
        api_key=settings.angelone_api_key,
        client_id=settings.angelone_client_id,
        password=settings.angelone_password,
        totp_secret=settings.angelone_totp_secret,
        start_streams=False,
    )
    if not provider.connect(force_reauth=True):
        report["fatal"] = "AngelOne login failed"
        print(json.dumps(report, indent=2, sort_keys=True))
        return 1
    report["login"] = True
    api = provider.smart_api
    report["execution_methods_available"] = {
        method: callable(getattr(api, method, None))
        for method in (
            "placeOrder", "modifyOrder", "cancelOrder", "orderBook", "position"
        )
    }
    try:
        provider.fetch_instruments()
        from engine.expiry_manager import expiry_manager
        master = expiry_manager.df
        report["instrument_master_rows"] = int(len(master)) if master is not None else 0
    except Exception as exc:
        report["instrument_master_error"] = str(exc)[:160]
    for instrument in list(settings.active_indices or []):
        token_info = INDEX_SPOT_TOKENS.get(instrument) or ("", "NSE")
        token, spot_exchange = str(token_info[0]), str(token_info[1])
        try:
            quote = api.ltpData(spot_exchange, "INDEX", token)
            quote_summary = _summary(quote)
            if isinstance(quote, dict) and isinstance(quote.get("data"), dict):
                quote_summary["ltp_positive"] = float(quote["data"].get("ltp") or 0) > 0
            report["spot_quotes"][instrument] = quote_summary
        except Exception as exc:
            quote = {}
            report["spot_quotes"][instrument] = {"transport_ok": False, "error": str(exc)[:160]}
        time.sleep(0.25)
        try:
            now = datetime.now()
            candles = api.getCandleData({
                "exchange": spot_exchange, "symboltoken": token, "interval": "FIVE_MINUTE",
                "fromdate": (now - timedelta(days=7)).strftime("%Y-%m-%d 09:15"),
                "todate": now.strftime("%Y-%m-%d %H:%M"),
            })
            report["historical_candles"][instrument] = _summary(candles)
        except Exception as exc:
            report["historical_candles"][instrument] = {"transport_ok": False, "error": str(exc)[:160]}
        time.sleep(0.25)
        try:
            quote_data = quote.get("data", {}) if isinstance(quote, dict) else {}
            spot = float(quote_data.get("ltp") or quote_data.get("close") or 0.0)
            interval = float(INDEX_STRIKE_INTERVALS.get(instrument, 50) or 50)
            strike = round(spot / interval) * interval if spot > 0 else 0
            ce = provider.get_option_token(instrument, strike, "CE") if strike else None
            pe = provider.get_option_token(instrument, strike, "PE") if strike else None
            resolved = bool(ce and pe and ce.get("symbol") and ce.get("token") and pe.get("symbol") and pe.get("token"))
            report["option_contracts"][instrument] = {
                "resolved": resolved, "strike_positive": strike > 0,
                "ce_suffix": bool(ce and str(ce.get("symbol") or "").endswith("CE")),
                "pe_suffix": bool(pe and str(pe.get("symbol") or "").endswith("PE")),
            }
            if resolved:
                exchange = str(ce.get("exch_seg") or "NFO")
                market = api.getMarketData(mode="FULL", exchangeTokens={exchange: [str(ce["token"]), str(pe["token"])]})
                report["option_market_data"][instrument] = _summary(market)
            else:
                report["option_market_data"][instrument] = {"transport_ok": False, "reason": "contracts unresolved"}
        except Exception as exc:
            report["option_contracts"][instrument] = {"resolved": False, "error": str(exc)[:160]}
            report["option_market_data"][instrument] = {"transport_ok": False, "error": str(exc)[:160]}
        time.sleep(0.25)
    try:
        report["positions"] = _summary(api.position())
    except Exception as exc:
        report["positions"] = {"transport_ok": False, "error": str(exc)[:160]}
    time.sleep(0.25)
    try:
        report["order_book"] = _summary(api.orderBook())
    except Exception as exc:
        report["order_book"] = {"transport_ok": False, "error": str(exc)[:160]}
    print(json.dumps(report, indent=2, sort_keys=True))
    failures = []
    if not report["login"] or report["instrument_master_rows"] <= 0:
        failures.append("login/instrument master")
    for section in ("spot_quotes", "historical_candles", "option_market_data"):
        for instrument, result in report[section].items():
            if not result.get("transport_ok") or result.get("error_code") == "AG8001":
                failures.append(f"{section}:{instrument}")
    for section in ("positions", "order_book"):
        result = report[section]
        if not result.get("transport_ok") or result.get("error_code") == "AG8001":
            failures.append(section)
    if not all(report["execution_methods_available"].values()):
        failures.append("execution SDK methods")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
