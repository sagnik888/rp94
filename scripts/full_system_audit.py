
import sys
import os
import math
from pathlib import Path
import pandas as pd
from datetime import datetime, timedelta
from loguru import logger
import queue
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data.market_data import FYERS_INDEX_SYMBOLS, INDEX_SPOT_TOKENS, MarketDataProvider
from config.settings import get_settings

def run_with_timeout(_runner, label, fn, timeout=20, default=None):
    result_queue = queue.Queue(maxsize=1)

    def target():
        try:
            result_queue.put((True, fn()))
        except Exception as e:
            result_queue.put((False, e))

    worker = threading.Thread(target=target, name=f"audit-{label}", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        logger.error(f"    - {label}: timed out after {timeout}s")
        return default

    ok, result = result_queue.get()
    if ok:
        return result
    logger.error(f"    - {label}: {result}")
    return default

def run_end_to_end_audit(days=7):
    logger.info(f"🚀 Starting Full System Audit (A-Z) for last {days} days")
    settings = get_settings()
    provider = MarketDataProvider(
        api_key=settings.angelone_api_key,
        client_id=settings.angelone_client_id,
        password=settings.angelone_password,
        totp_secret=settings.angelone_totp_secret
    )

    failures = []

    # 1. TEST CONNECTIVITY (read-only: authentication + market-data streams only)
    logger.info("Step 1: Connectivity Handshake...")
    kite_conn = run_with_timeout(
        None,
        "Kite connectivity",
        provider.connect,
        timeout=20,
        default=False,
    )
    fyers_conn = provider.fyers is not None
    logger.info(f"  - Kite: {'🟢 Connected' if kite_conn else '🔴 Failed'}")
    logger.info(f"  - Fyers: {'🟢 Connected' if fyers_conn else '🔴 Failed'}")
    if not kite_conn:
        failures.append("Kite connectivity")
    if not fyers_conn:
        failures.append("Fyers connectivity")

    executor = None

    # 2. TEST HISTORICAL DATA INTEGRITY (Last 7 Days)
    logger.info(f"Step 2: Historical Audit ({days} Days)...")
    indices = list(settings.active_indices)

    for symbol in indices:
        token, exchange = INDEX_SPOT_TOKENS.get(symbol, ("", "NSE"))
        logger.info(f"  🔍 Auditing {symbol} (Token: {token})")

        # A. Historical data integrity through the unified fetcher. For index
        # candles this prefers cache/Fyers and avoids competing with the live
        # scanner for AngelOne history quota.
        unified_history = run_with_timeout(
            executor,
            f"Unified {symbol} history",
            lambda token=token, symbol=symbol: provider.get_historical_candles(
                token, exchange, "5min", days_back=days, instrument_name=symbol
            ),
            timeout=25,
            default=pd.DataFrame(),
        )
        if not unified_history.empty:
            logger.info(f"    - Unified history: 🟢 Found {len(unified_history)} candles")
            # Timezone Check
            last_time = unified_history.index[-1]
            logger.info(f"    - IST Alignment: 🟢 Last candle at {last_time}")
        else:
            logger.error(f"    - Unified history: 🔴 No historical data found!")
            failures.append(f"{symbol} historical candles")

        # B. Fyers Volume Alignment
        f_sym = FYERS_INDEX_SYMBOLS.get(symbol)
        if provider.fyers:
            try:
                from_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
                to_date = datetime.now().strftime("%Y-%m-%d")
                f_data = run_with_timeout(
                    executor,
                    f"Fyers {symbol} history",
                    lambda f_sym=f_sym, from_date=from_date, to_date=to_date: provider.fyers.history({
                        "symbol": f_sym, "resolution": "5", "date_format": "1",
                        "range_from": from_date, "range_to": to_date, "cont_flag": "1"
                    }),
                    timeout=20,
                    default={"s": "timeout", "message": "timed out"},
                )
                if not isinstance(f_data, dict):
                    logger.warning(
                        f"    - Fyers History: 🔴 Malformed response "
                        f"({type(f_data).__name__})"
                    )
                    failures.append(f"{symbol} Fyers volume history")
                elif f_data.get('s') == 'ok':
                    f_df = pd.DataFrame(f_data.get('candles', []))
                    vol_sum = (
                        float(pd.to_numeric(f_df[5], errors="coerce").sum(min_count=1))
                        if not f_df.empty and 5 in f_df.columns
                        else float("nan")
                    )
                    volume_ok = math.isfinite(vol_sum) and vol_sum > 0
                    logger.info(
                        f"    - Fyers Volume: "
                        f"{'🟢 Sync Valid' if volume_ok else '🔴 Missing/Invalid Volume'}"
                    )
                    if not volume_ok:
                        failures.append(f"{symbol} Fyers volume history")
                else:
                    logger.warning(f"    - Fyers History: 🔴 Failed ({f_data.get('message')})")
                    failures.append(f"{symbol} Fyers volume history")
            except Exception as e:
                logger.error(f"    - Fyers Audit Error: {e}")
                failures.append(f"{symbol} Fyers volume history")
        time.sleep(0.35)

    # 3. TEST LIVE PRICE & FAILOVER
    logger.info("Step 3: Live Price & Failover Logic...")
    for symbol in indices:
        quote = run_with_timeout(
            executor,
            f"{symbol} latest price",
            lambda symbol=symbol: provider.get_index_quote(symbol),
            timeout=8,
            default={},
        )
        if not isinstance(quote, dict):
            quote = {}
        price = float(quote.get("ltp") or 0.0)
        source = str(quote.get("source") or "unknown")
        snapshot = provider.get_market_source_snapshot(symbol)
        freshest = snapshot.get("freshest") or {}
        freshest_label = str(freshest.get("source") or source)
        age = freshest.get("age_seconds")
        age_label = f", age={age}s" if age is not None else ""
        quote_session = str(
            quote.get("quote_label") or quote.get("quote_session") or quote.get("session") or "UNKNOWN"
        ).upper()
        try:
            market_open = bool(provider.is_market_open())
        except Exception:
            market_open = quote_session in {"LIVE", "OPEN"}
        entry_ok = bool(
            market_open
            and quote_session in {"LIVE", "OPEN"}
            and snapshot.get("entry_eligible")
        )
        entry_label = "entry-ok" if entry_ok else f"context-only ({quote_session})"
        logger.info(
            f"  - {symbol}: ₹{price} (Quote: {source}; Freshest: "
            f"{freshest_label}{age_label}; {entry_label})"
        )
        if price <= 0:
            logger.error(f"  - {symbol}: 🔴 CRITICAL PRICE ERROR")
            failures.append(f"{symbol} index quote")

    # 4. TEST OPTION CHAIN & PCR
    logger.info("Step 4: Option Intelligence Audit...")
    for symbol in indices:
        chain = run_with_timeout(
            executor,
            f"{symbol} option-chain",
            lambda symbol=symbol: provider.get_option_chain(symbol),
            timeout=20,
            default=pd.DataFrame(),
        )
        if chain is not None and not chain.empty:
            meta = getattr(provider, "_option_chain_meta", {}).get(symbol, {})
            logger.info(f"    - Option-chain source: {meta.get('source', 'unknown')}, fallback={bool(meta.get('fallback'))}")
            spot = provider.get_latest_price(symbol) or 0
            if "strike" in chain.columns and spot:
                atm_idx = (chain["strike"].astype(float) - float(spot)).abs().idxmin()
                atm_row = chain.loc[atm_idx]
            else:
                atm_row = chain.iloc[len(chain) // 2]
            logger.info(
                f"  - {symbol} Chain: 🟢 {len(chain)} strikes found. "
                f"ATM/nearest LTP: CE {atm_row.get('call_ltp')}, PE {atm_row.get('put_ltp')}"
            )
        else:
            logger.error(f"  - {symbol} Chain: 🔴 Failed to fetch!")
            failures.append(f"{symbol} option chain")

    result = {
        "ok": not failures,
        "failures": sorted(set(failures)),
        "indices": indices,
        "days": int(days),
        "read_only": True,
    }
    if failures:
        logger.error("🔚 Audit FAILED: " + "; ".join(result["failures"]))
    else:
        logger.info("🔚 Audit PASSED (read-only data/intelligence checks).")
    shutdown = getattr(provider, "shutdown_streams", None)
    if callable(shutdown):
        try:
            shutdown()
        except Exception as exc:
            logger.warning(f"Audit stream shutdown skipped: {exc}")
    return result

if __name__ == "__main__":
    result = run_end_to_end_audit(days=7)
    try:
        logger.complete()
    except Exception:
        pass
    raise SystemExit(0 if result.get("ok") else 1)
