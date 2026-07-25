"""
Market Data Provider — Institutional Broker-Master Integration
═══════════════════════════════════════════════════════════════
Primary: AngelOne SmartAPI for 100% accurate Index Spot (LTT)
Secondary: Yahoo Finance for backfill & historical candles
"""

import os
import time
import json
import threading
import logging
import pandas as pd
import numpy as np
import yfinance as yf
from datetime import datetime, timedelta, time as dtime, date
from typing import Dict, List, Optional, Tuple, Any
from pathlib import Path
from loguru import logger
from fyers_apiv3 import fyersModel
from fyers_apiv3.FyersWebsocket import data_ws
from data.market_calendar import (
    load_nse_calendar_coverage,
    load_nse_holidays,
    load_nse_special_trading_days,
)
from data.market_source_health import build_market_source_snapshot, build_source_health
from engine.option_history_store import get_option_history_store
from trading.api_rate_limiter import get_shared_api_limiter
from trading.secure_logging import install_smartapi_log_redaction, redact_sensitive_text

try:
    import pyotp
    from SmartApi import SmartConnect
    from SmartApi.smartWebSocketV2 import SmartWebSocketV2
    install_smartapi_log_redaction()
    try:
        import logzero
        logzero.logger.setLevel(logging.CRITICAL)
    except Exception as exc:
        logger.debug(f"logzero logger level adjustment skipped: {exc}")
    HAS_SMARTAPI = True
except ImportError as e:
    import traceback
    with open("import_err.log", "a") as f:
        f.write("FATAL IMPORT ERROR: " + str(e) + "\n")
        f.write(traceback.format_exc())
    SmartConnect = None
    SmartWebSocketV2 = None
    HAS_SMARTAPI = False

YAHOO_TICKERS = {
    "NIFTY": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "SENSEX": "^BSESN",
    "MIDCPNIFTY": "NIFTY_MID_SELECT.NS",
}
INDEX_SPOT_TOKENS = {
    "NIFTY": ("99926000", "NSE"),
    "BANKNIFTY": ("99926009", "NSE"),
    "SENSEX": ("99919000", "BSE"),
    "MIDCPNIFTY": ("99926074", "NSE"),
}
INDEX_SHORT_TOKENS = {
    "26000": "NIFTY",
    "26009": "BANKNIFTY",
    "19000": "SENSEX",
    "26074": "MIDCPNIFTY",
}
FYERS_INDEX_SYMBOLS = {
    "NIFTY": "NSE:NIFTY50-INDEX",
    "BANKNIFTY": "NSE:NIFTYBANK-INDEX",
    "SENSEX": "BSE:SENSEX-INDEX",
    "MIDCPNIFTY": "NSE:MIDCPNIFTY-INDEX",
}
FYERS_SYMBOL_TO_INDEX = {v: k for k, v in FYERS_INDEX_SYMBOLS.items()}
INDEX_STRIKE_INTERVALS = {"NIFTY": 50, "BANKNIFTY": 100, "SENSEX": 100, "MIDCPNIFTY": 25}
YAHOO_INTERVALS = {"1min": "1m", "5min": "5m", "15min": "15m"}
YAHOO_MAX_DAYS = {"1m": 7, "5m": 60, "15m": 60}
PREVIOUS_CLOSE_KEYS = ("prev_close_price", "previous_close", "prev_close", "close_previous", "close")
FYERS_LOG_FILENAMES = ("fyersApi.log", "fyersRequests.log", "fyersDataSocket.log")
FYERS_LOGS_READY = False


def _positive_int_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
        return value if value > 0 else default
    except (TypeError, ValueError):
        return default


def _fyers_log_dir(log_dir: Optional[Path] = None) -> Path:
    configured = log_dir or Path(os.getenv("UT1_FYERS_LOG_DIR", "logs/fyers"))
    configured.mkdir(parents=True, exist_ok=True)
    return configured


def _archive_fyers_log(src: Path, archive_dir: Path, *, now: Optional[datetime] = None) -> bool:
    if not src.exists() or not src.is_file():
        return False
    archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    dest = archive_dir / f"{src.stem}.{stamp}{src.suffix}"
    counter = 1
    while dest.exists():
        dest = archive_dir / f"{src.stem}.{stamp}.{counter}{src.suffix}"
        counter += 1
    src.replace(dest)
    return True


def _prune_fyers_archives(archive_dir: Path, *, retention: int) -> None:
    if retention <= 0 or not archive_dir.exists():
        return
    for name in FYERS_LOG_FILENAMES:
        archives = sorted(
            archive_dir.glob(f"{Path(name).stem}.*{Path(name).suffix}"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for old in archives[retention:]:
            try:
                old.unlink()
            except OSError as exc:
                logger.debug(f"Unable to prune old Fyers log archive {old}: {exc}")


def _rotate_fyers_logs(
    *,
    root_dir: Optional[Path] = None,
    log_dir: Optional[Path] = None,
    max_bytes: Optional[int] = None,
    retention: Optional[int] = None,
) -> Path:
    """Keep SDK-created Fyers logs out of the project root before clients open them."""
    root = root_dir or Path(".")
    managed_dir = _fyers_log_dir(log_dir)
    archive_dir = managed_dir / "archive"
    root_archive_dir = managed_dir / "root_archive"
    size_limit = max_bytes or _positive_int_env("UT1_FYERS_LOG_ROTATE_BYTES", 25 * 1024 * 1024)
    keep = retention if retention is not None else _positive_int_env("UT1_FYERS_LOG_RETENTION", 10)

    for name in FYERS_LOG_FILENAMES:
        root_log = root / name
        try:
            if root_log.exists() and root_log.is_file():
                _archive_fyers_log(root_log, root_archive_dir)
        except OSError as exc:
            logger.warning(f"Unable to archive root Fyers log {root_log}: {exc}")

        managed_log = managed_dir / name
        try:
            if managed_log.exists() and managed_log.is_file() and managed_log.stat().st_size >= size_limit:
                _archive_fyers_log(managed_log, archive_dir)
        except OSError as exc:
            logger.warning(f"Unable to rotate Fyers log {managed_log}: {exc}")

    _prune_fyers_archives(archive_dir, retention=keep)
    _prune_fyers_archives(root_archive_dir, retention=keep)
    return managed_dir


def _fyers_sdk_log_path() -> str:
    global FYERS_LOGS_READY
    if not FYERS_LOGS_READY:
        _rotate_fyers_logs()
        FYERS_LOGS_READY = True
    # FyersDataSocket concatenates log_path + filename, so keep the trailing slash.
    return _fyers_log_dir().as_posix().rstrip("/") + "/"

import functools

def retry_on_exception(retries: int = 3, delay: float = 1.0, backoff: float = 2.0):
    """Exponential backoff retry decorator"""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            m_delay = delay
            for i in range(retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if i == retries - 1: raise e
                    logger.warning(f"Retry {i+1}/{retries} for {func.__name__} after {m_delay}s: {e}")
                    time.sleep(m_delay)
                    m_delay *= backoff
            return func(*args, **kwargs)
        return wrapper
    return decorator

class MarketDataProvider:
    _option_oi_snapshot_file_lock = threading.RLock()

    def __init__(
        self,
        api_key: str = "",
        client_id: str = "",
        password: str = "",
        totp_secret: str = "",
        start_streams: bool = True,
    ):
        self.api_key = api_key
        self.client_id = client_id
        self.password = password
        self.totp_secret = totp_secret
        self.start_streams = bool(start_streams)
        self.smart_api: Optional[SmartConnect] = None
        self.is_connected = False
        self._rest_lock = threading.Lock()
        self._angel_quote_lease_state = threading.local()
        self._angel_rest_backoff_until: float = 0.0
        self._angel_rest_backoff_failures: int = 0
        self._last_angel_rest_backoff_reason: str = ""
        self._fyers_chain_failure_streak: int = 0
        self._last_fyers_reinit_ts: float = 0.0
        self._ltp_cache: Dict[str, Dict] = {}
        
        # Versioned exchange calendar with optional data_store overrides.
        self.nse_holidays = load_nse_holidays()
        self.nse_special_trading_days = load_nse_special_trading_days()
        self.nse_calendar_coverage_years = load_nse_calendar_coverage()

        self._yf_cache: Dict[str, Dict] = {}
        self._cache_ttl = 5
        self._ltp_cache: Dict[str, Dict[str, Any]] = {}
        self._ws_cache: Dict[str, Dict[str, Any]] = {}
        self._touch_quote_cache: Dict[str, Dict[str, Any]] = {}
        self._index_tick_volume_diag: Dict[str, Dict[str, Any]] = {}
        self._active_angel_tokens = {1: set(["26000", "26009", "26017", "26074"]), 3: set(["19000"])}
        self._last_ltp_time: float = 0
        self._last_ws_tick: float = 0.0  # Compatibility aggregate; provider health uses separate clocks.
        self._last_angel_ws_tick: float = 0.0
        self._last_fyers_ws_tick: float = 0.0
        self._fyers_ws_connected: bool = False
        self._fyers_ws_connected_since: float = 0.0
        self._fyers_ws_retry_count: int = 0
        self._fyers_ws_gateway_failures: int = 0
        self._fyers_ws_last_error: str = ""
        self._yahoo_request_count: int = 0
        self._yahoo_last_used_at: Optional[float] = None
        self._instruments_df: Optional[pd.DataFrame] = None
        self._inst_cache_path = Path("data_store/instruments.csv")
        self._option_chain_meta: Dict[str, Dict[str, Any]] = {}
        self._option_oi_snapshot_lock = self._option_oi_snapshot_file_lock
        self._fyers_refresh_disabled = False
        self._fyers_auth_required = False
        self._fyers_auth_reason = ""
        self._fyers_token_updated_at = None
        self.fyers: Optional[fyersModel.FyersModel] = None
        self._load_fyers_token()

    def _angel_rest_backoff_config(self) -> Tuple[float, float]:
        try:
            from config.settings import get_settings
            settings = get_settings()
            base = float(getattr(settings, "angel_rest_backoff_seconds", 12.0) or 12.0)
            max_delay = float(getattr(settings, "angel_rest_backoff_max_seconds", 60.0) or 60.0)
        except Exception:
            base, max_delay = 12.0, 60.0
        return max(1.0, base), max(1.0, max_delay)

    def _maybe_self_heal_fyers(self, context: str) -> None:
        """Re-create the Fyers client after a persistent chain-failure streak.

        The 2026-07-03 session needed a manual midday restart to clear ~1600
        chain errors/stale fallbacks; re-initialising the client from the
        stored token is what that restart effectively did, so do it
        automatically instead of waiting for an operator."""
        try:
            from config.settings import get_settings
            settings = get_settings()
            if not bool(getattr(settings, "fyers_chain_self_heal_enabled", True)):
                return
            threshold = max(3, int(getattr(settings, "fyers_chain_reinit_failure_threshold", 8) or 8))
            cooldown = max(60.0, float(getattr(settings, "fyers_chain_reinit_cooldown_seconds", 300.0) or 300.0))
        except Exception:
            threshold, cooldown = 8, 300.0
        self._fyers_chain_failure_streak = int(getattr(self, "_fyers_chain_failure_streak", 0) or 0) + 1
        if self._fyers_chain_failure_streak < threshold:
            return
        now = time.time()
        if now - float(getattr(self, "_last_fyers_reinit_ts", 0.0) or 0.0) < cooldown:
            return
        self._last_fyers_reinit_ts = now
        self._fyers_chain_failure_streak = 0
        logger.warning(
            f"Fyers self-heal: re-initialising client after chain-failure streak ({context})"
        )
        try:
            self._load_fyers_token()
        except Exception as exc:
            logger.error(f"Fyers self-heal reinit failed: {exc}")

    def _angel_rest_error_text(self, payload: Any) -> str:
        if isinstance(payload, BaseException):
            return str(payload)
        if isinstance(payload, dict):
            try:
                return json.dumps(payload, default=str)
            except Exception:
                return str(payload)
        return str(payload or "")

    def _angel_rest_should_backoff(self, payload: Any) -> bool:
        text = self._angel_rest_error_text(payload).lower()
        markers = (
            "exceeding access rate",
            "access rate",
            "rate limit",
            "too many requests",
            "read timed out",
            "connect timeout",
            "connection timed out",
            "timed out",
            "timeout",
        )
        return any(marker in text for marker in markers)

    def _mark_angel_rest_backoff(self, context: str, payload: Any) -> None:
        base, max_delay = self._angel_rest_backoff_config()
        failures = int(getattr(self, "_angel_rest_backoff_failures", 0) or 0) + 1
        delay = min(max_delay, base * (2 ** max(0, failures - 1)))
        self._angel_rest_backoff_failures = failures
        self._angel_rest_backoff_until = time.time() + delay
        reason = redact_sensitive_text(self._angel_rest_error_text(payload))
        self._last_angel_rest_backoff_reason = f"{context}: {reason}"[:300]
        logger.warning(
            f"Angel REST backoff {delay:.1f}s after {context} failure "
            f"({self._last_angel_rest_backoff_reason})"
        )

    def _mark_angel_rest_success(self) -> None:
        self._angel_rest_backoff_failures = 0
        self._angel_rest_backoff_until = 0.0
        self._last_angel_rest_backoff_reason = ""

    def _angel_rest_ready(self, context: str) -> bool:
        until = float(getattr(self, "_angel_rest_backoff_until", 0.0) or 0.0)
        remaining = until - time.time()
        if remaining > 0:
            logger.debug(f"Skipping Angel REST {context}; backoff active for {remaining:.1f}s")
            return False
        return True

    def _throttle_angel_rest(self, context: str) -> bool:
        if not self._angel_rest_ready(context):
            return False
        if self.smart_api is not None:
            # Best-effort quotes yield immediately so they never queue ahead of
            # order status, protective stops, exits, or position reconciliation.
            limiter = get_shared_api_limiter(self.smart_api)
            acquired = limiter.try_begin_best_effort_quote()
            if acquired:
                lease_state = getattr(self, "_angel_quote_lease_state", None)
                if lease_state is None:
                    lease_state = threading.local()
                    self._angel_quote_lease_state = lease_state
                lease_state.active = True
            return acquired
        lock = getattr(self, "_rest_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._rest_lock = lock
        with lock:
            now = time.time()
            last_rest_time = float(getattr(self, "_last_rest_time", 0.0) or 0.0)
            wait = 0.5 - (now - last_rest_time)
            if wait > 0:
                time.sleep(wait)
                self._last_rest_time = time.time()
            else:
                self._last_rest_time = now
        return True

    def _release_angel_rest_quote(self) -> None:
        lease_state = getattr(self, "_angel_quote_lease_state", None)
        if lease_state is None or not bool(getattr(lease_state, "active", False)):
            return
        lease_state.active = False
        if self.smart_api is not None:
            get_shared_api_limiter(self.smart_api).end_best_effort_quote()

    def _best_effort_angel_rest_call(self, context: str, fn):
        """Run one non-critical REST request without competing with broker calls."""
        if not self._throttle_angel_rest(context):
            return None
        try:
            response = fn()
            if isinstance(response, dict) and response.get("status"):
                self._mark_angel_rest_success()
            elif isinstance(response, dict) and self._angel_rest_should_backoff(response):
                self._mark_angel_rest_backoff(context, response)
            return response
        except Exception as exc:
            if self._angel_rest_should_backoff(exc):
                self._mark_angel_rest_backoff(context, exc)
            logger.debug(f"Angel REST {context} failed: {redact_sensitive_text(exc)}")
            return None
        finally:
            self._release_angel_rest_quote()

    @staticmethod
    def _read_fyers_token_file() -> Dict[str, Any]:
        token_path = Path("fyers_token.json")
        if not token_path.exists():
            return {}
        try:
            return json.loads(token_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(f"⚠️ Failed to read fyers_token.json: {exc}")
            return {}

    @staticmethod
    def _fyers_profile_token_error(profile: Any) -> bool:
        if not isinstance(profile, dict):
            return False
        try:
            code = int(profile.get("code"))
        except (TypeError, ValueError):
            code = profile.get("code")
        return profile.get("s") == "error" and code in {-8, -17, -300}

    @staticmethod
    def _fyers_ws_gateway_timeout(error: Any) -> bool:
        text = str(error or "").lower()
        return (
            "504" in text
            or "gateway time-out" in text
            or "gateway timeout" in text
            or "cloudflare" in text
        )

    @staticmethod
    def _fyers_ws_backoff_seconds(retry_count: int) -> int:
        return min(60, 2 ** max(1, int(retry_count or 1)))

    @staticmethod
    def _quote_float(values: Dict[str, Any], *keys: str) -> float:
        for key in keys:
            try:
                value = float((values or {}).get(key) or 0.0)
            except (TypeError, ValueError, AttributeError):
                value = 0.0
            if value > 0:
                return value
        return 0.0

    def _remember_previous_close(self, symbol: str, previous_close: float, source: str) -> float:
        previous_close = float(previous_close or 0.0)
        if previous_close <= 0:
            return 0.0
        if not hasattr(self, "_prev_close_global_cache"):
            self._prev_close_global_cache = {}
        if not hasattr(self, "_prev_close_cache_time"):
            self._prev_close_cache_time = {}
        if not hasattr(self, "_prev_close_cache_source"):
            self._prev_close_cache_source = {}
        self._prev_close_global_cache[symbol] = previous_close
        self._prev_close_cache_time[symbol] = time.time()
        self._prev_close_cache_source[symbol] = str(source or "")
        return previous_close

    def _fyers_index_quote_values(self, symbol: str) -> Dict[str, Any]:
        fyers_symbol = FYERS_INDEX_SYMBOLS.get(symbol)
        if not getattr(self, "fyers", None) or not fyers_symbol:
            return {}
        resp = self.fyers.quotes({"symbols": fyers_symbol})
        if not isinstance(resp, dict) or resp.get("s") != "ok":
            return {}
        values = (resp.get("d") or [{}])[0].get("v", {})
        return dict(values or {}) if isinstance(values, dict) else {}

    def _fyers_ws_token_ready_for_reconnect(self, access_token: str, ws_generation: int) -> bool:
        token_data = self._read_fyers_token_file()
        file_token = str(token_data.get("access_token") or "")
        if file_token and file_token != access_token:
            logger.info("Fyers token file changed; restarting WebSocket with refreshed token.")
            self.start_fyers_websocket(file_token)
            return False

        token_to_check = file_token or access_token
        if not token_to_check:
            self._mark_fyers_auth_required("Fyers WebSocket reconnect has no access token")
            self._fyers_ws_auth_failed = True
            return False

        try:
            from config.settings import get_settings
            settings = get_settings()
            model = fyersModel.FyersModel(
                client_id=settings.fyers_app_id,
                token=token_to_check,
                log_path=_fyers_sdk_log_path(),
            )
            profile = model.get_profile()
            if self._fyers_profile_token_error(profile):
                logger.warning(
                    f"Fyers token failed pre-reconnect validation ({profile}); attempting refresh."
                )
                if token_data and self._refresh_fyers_token(token_data):
                    return False
                self._fyers_ws_auth_failed = True
                self._mark_fyers_auth_required("Fyers WebSocket token invalid and refresh failed")
                return False
            if getattr(self, "_fyers_ws_generation", 0) == ws_generation:
                self.fyers = model
                self._clear_fyers_auth_required()
            return True
        except Exception as exc:
            logger.warning(f"Fyers token pre-reconnect validation failed; retrying existing WebSocket token: {exc}")
            return True

    def _load_fyers_token(self):
        try:
            token_data = self._read_fyers_token_file()
            if not token_data:
                logger.warning("⚠️ fyers_token.json not found")
                return

            access_token = token_data.get("access_token")
                
            if access_token:
                from config.settings import get_settings
                settings = get_settings()
                self.fyers = fyersModel.FyersModel(
                    client_id=settings.fyers_app_id,
                    token=access_token,
                    log_path=_fyers_sdk_log_path(),
                )
                
                # Verify token status
                profile = self.fyers.get_profile()
                if self._fyers_profile_token_error(profile):
                    logger.warning(f"🔄 Fyers Token Issue (Code {profile.get('code')}). Attempting refresh...")
                    if not self._refresh_fyers_token(token_data):
                        self.fyers = None
                        if not self._fyers_auth_reason:
                            self._mark_fyers_auth_required("Fyers token expired and manual login is required")
                else:
                    self._clear_fyers_auth_required()
                    logger.info("✅ Fyers API Connected for Data!")
                    if self.start_streams:
                        self.start_fyers_websocket(access_token)
                    else:
                        logger.info("Fyers WebSocket skipped for history-only provider.")
            else:
                logger.warning("⚠️ Fyers access token missing in fyers_token.json")
                self._mark_fyers_auth_required("Fyers access token missing")
        except Exception as e:
            self.fyers = None
            self._mark_fyers_auth_required(f"Failed to load Fyers token: {e}")
            logger.warning(f"⚠️ Failed to load Fyers token: {e}")

    @staticmethod
    def _close_ws_socket(sock, context: str) -> bool:
        """Tear down a broker websocket via the SDK's REAL close method.

        Neither FyersDataSocket nor SmartWebSocketV2 has `.close()` — both
        expose `close_connection()`. Calling `.close()` raised AttributeError
        on every silent-disconnect/auth/restart teardown (20x on 2026-07-08),
        so stale sockets were never closed before reconnecting. Tries the real
        method first, tolerates SDK variants, and never raises."""
        if not sock:
            return False
        for method_name in ("close_connection", "close", "stop"):
            method = getattr(sock, method_name, None)
            if callable(method):
                try:
                    method()
                    return True
                except Exception as exc:
                    logger.debug(f"WS {method_name}() during {context} failed: {exc}")
                    return False
        logger.debug(f"WS teardown during {context}: socket has no close method")
        return False

    @staticmethod
    def _fyers_ws_tick_stalled(last_tick: float, connected_since: float,
                               now_ts: float, stall_seconds: float) -> bool:
        """True when the WS is connected but ticks have stalled past stall_seconds.

        Covers both 'connected but never delivered a tick' (last_tick<=0 after
        open) and 'ticks went silent' (last_tick aged out). Pure/ testable so the
        detection threshold can be tuned and regression-guarded independently of
        the reconnect loop and the market-hours guard around it."""
        try:
            stall = max(5.0, float(stall_seconds))
        except (TypeError, ValueError):
            stall = 60.0
        no_tick_after_open = (
            last_tick <= 0 and connected_since > 0 and (now_ts - connected_since) > stall
        )
        stale_tick_after_open = last_tick > 0 and (now_ts - last_tick) > stall
        return bool(no_tick_after_open or stale_tick_after_open)

    def start_fyers_websocket(self, access_token: str):
        """Start Fyers WebSocket in a separate thread"""
        self._fyers_ws_generation = getattr(self, "_fyers_ws_generation", 0) + 1
        ws_generation = self._fyers_ws_generation
        self._fyers_ws_connected = False
        self._fyers_ws_connected_since = 0.0
        # A socket-open callback is not a market-data tick. Keep feed freshness false
        # until an actual quote arrives, otherwise startup DNS failures can look fresh.
        self._last_fyers_ws_tick = 0.0
        if hasattr(self, "_fyers_ws") and self._fyers_ws:
            self._close_ws_socket(self._fyers_ws, "pre-restart teardown")
            
        try:
            def on_message(msg):
                if getattr(self, "_fyers_ws_generation", 0) != ws_generation:
                    return
                if isinstance(msg, list):
                    messages = msg
                else:
                    messages = [msg]
                    
                for m in messages:
                    symbol = m.get('symbol')
                    ltp = m.get('ltp')
                    oi = m.get('oi')
                    vol, vol_key, vol_fields = self._extract_index_tick_volume(
                        m,
                        (
                            "vol_traded_today",
                            "volume_traded_today",
                            "volume",
                            "vol",
                            "last_traded_qty",
                            "last_traded_quantity",
                            "ltq",
                            "qty",
                            "quantity",
                        ),
                    )
                    
                    if symbol and ltp:
                        last_ws_debug = getattr(self, "_last_ws_debug_log", {})
                        now_ts = time.time()
                        self._ws_cache[symbol] = {
                            'ltp': float(ltp),
                            'oi': int(oi) if oi else 0,
                            'vol': int(vol) if vol else 0,
                            'volume_key': vol_key,
                            'volume_fields': dict(vol_fields or {}),
                            'change': float(m.get('ch') or 0.0),
                            'change_pct': float(m.get('chp') or 0.0),
                            'previous_close': self._quote_float(m, *PREVIOUS_CLOSE_KEYS),
                            'time': now_ts,
                            'source': 'fyers_ws',
                        }
                        if now_ts - last_ws_debug.get(symbol, 0) >= 10:
                            logger.debug(f"Fyers WS Tick: {symbol} -> {ltp}")
                            last_ws_debug[symbol] = now_ts
                            self._last_ws_debug_log = last_ws_debug
                        
                        self._last_fyers_ws_tick = time.time()
                        self._last_ws_tick = self._last_fyers_ws_tick
                        self._fyers_ws_retry_count = 0
                        self._fyers_ws_gateway_failures = 0
                        self._fyers_ws_last_error = ""
                        
                        if hasattr(self, 'on_tick') and self.on_tick:
                            if symbol in FYERS_SYMBOL_TO_INDEX:
                                index_name = FYERS_SYMBOL_TO_INDEX[symbol]
                                self._record_index_tick_volume_diag(
                                    index_name,
                                    "fyers_ws",
                                    float(vol or 0.0),
                                    vol_key,
                                    vol_fields,
                                    now_ts,
                                )
                                self.on_tick(index_name, float(ltp), int(vol) if vol else 0, datetime.now())

            def on_open():
                if getattr(self, "_fyers_ws_generation", 0) != ws_generation:
                    return
                logger.info("🔌 Fyers WebSocket Connected")
                self._fyers_ws_connected = True
                self._fyers_ws_connected_since = time.time()
                # CRITICAL (2026-07-09): reset the stall clock on every (re)open.
                # The silent-disconnect detector fires when now-last_tick > stall.
                # On a RECONNECT, _last_fyers_ws_tick still held the pre-disconnect
                # value (already >stall old), so the first monitor iteration
                # instantly re-declared "silent disconnect" (~0.8s after subscribe)
                # and tore the fresh socket down again — a self-perpetuating
                # reconnect storm (43 drops / 64 reconnects on 2026-07-09, each
                # spawning WinError 10054 duplicate-connection fights + re-auth
                # bursts). Zeroing it hands the reconnected socket a full `stall`
                # grace window to deliver its first tick (no_tick_after_open branch,
                # measured from the fresh connected_since).
                self._last_fyers_ws_tick = 0.0
                symbols = list(FYERS_INDEX_SYMBOLS.values())
                self._fyers_ws.subscribe(symbols=symbols)
                logger.info(f"Subscribed to Fyers symbols: {symbols}")

            def on_error(e):
                if getattr(self, "_fyers_ws_generation", 0) != ws_generation:
                    return
                self._fyers_ws_connected = False
                self._fyers_ws_connected_since = 0.0
                self._fyers_ws_last_error = str(e)
                err_text = str(e).lower()
                if (
                    "valid token" in err_text
                    or "invalid token" in err_text
                    or "'code': -300" in err_text
                    or '"code": -300' in err_text
                ):
                    self._fyers_ws_auth_failed = True
                    self._mark_fyers_auth_required("Fyers WebSocket token invalid; refresh Fyers token before reconnecting")
                    logger.error(f"Fyers WS auth failed; reconnect loop stopped: {e}")
                    self._close_ws_socket(self._fyers_ws, "auth failure")
                    return
                if self._fyers_ws_gateway_timeout(e):
                    self._fyers_ws_gateway_failures = getattr(self, "_fyers_ws_gateway_failures", 0) + 1
                    logger.warning(
                        "Fyers WS transient gateway timeout "
                        f"(count={self._fyers_ws_gateway_failures}); reconnect will back off: {e}"
                    )
                else:
                    logger.error(f"Fyers WS Error: {e}")

            def on_close(message=None):
                # Fyers SDK invokes on_close with a close-message argument. The
                # old zero-arg signature raised "takes 0 positional arguments but
                # 1 was given", which crashed BEFORE the cleanup below ran — so
                # _fyers_ws_connected was never reset and the reconnect/state
                # machine desynced (app reported the broker offline). Accept the
                # arg so the cleanup always runs.
                if getattr(self, "_fyers_ws_generation", 0) != ws_generation:
                    return
                self._fyers_ws_connected = False
                self._fyers_ws_connected_since = 0.0
                logger.warning(f"🔌 Fyers WebSocket Closed{f': {message}' if message else ''}")

            def _build_fyers_socket():
                socket = data_ws.FyersDataSocket(
                    access_token=access_token,
                    log_path=_fyers_sdk_log_path(),
                    reconnect=False,
                    on_message=on_message,
                    on_error=on_error,
                    on_close=on_close
                )
                socket.on_open = on_open
                return socket

            self._fyers_ws_auth_failed = False
            self._fyers_ws = _build_fyers_socket()

            def _ws_runner():
                self._fyers_ws_retry_count = 0
                first_connect = True
                use_prebuilt_socket = True
                try:
                    from config.settings import get_settings
                    stall_seconds = float(
                        getattr(get_settings(), "fyers_ws_silent_disconnect_seconds", 90.0) or 90.0
                    )
                except Exception:
                    stall_seconds = 90.0
                while (
                    getattr(self, "_fyers_ws_generation", 0) == ws_generation
                    and not getattr(self, "_fyers_ws_auth_failed", False)
                ):
                    try:
                        if not first_connect and not self._fyers_ws_token_ready_for_reconnect(
                            access_token,
                            ws_generation,
                        ):
                            break
                        first_connect = False
                        
                        if use_prebuilt_socket and getattr(self, "_fyers_ws", None) is not None:
                            use_prebuilt_socket = False
                        else:
                            if hasattr(self, "_fyers_ws") and self._fyers_ws:
                                self._close_ws_socket(self._fyers_ws, "reconnect teardown")
                                time.sleep(2.0)  # Let Fyers server register the disconnection before reconnecting
                            self._fyers_ws = _build_fyers_socket()

                        if getattr(self, "_fyers_ws", None) is None:
                            raise RuntimeError("Fyers socket was not constructed")

                        self._fyers_ws.connect()
                    except Exception as e:
                        if getattr(self, "_fyers_ws_generation", 0) != ws_generation:
                            break
                        logger.error(f"Fyers WS Runner error: {e}")

                    if (
                        getattr(self, "_fyers_ws_generation", 0) != ws_generation
                        or getattr(self, "_fyers_ws_auth_failed", False)
                    ):
                        break
                    
                    while (
                        getattr(self, "_fyers_ws_generation", 0) == ws_generation
                        and getattr(self, "_fyers_ws_connected", False)
                        and not getattr(self, "_fyers_ws_auth_failed", False)
                    ):
                        last_tick = float(getattr(self, "_last_fyers_ws_tick", 0.0) or 0.0)
                        connected_since = float(getattr(self, "_fyers_ws_connected_since", 0.0) or 0.0)
                        now_ts = time.time()
                        if self._fyers_ws_tick_stalled(
                            last_tick, connected_since, now_ts, stall_seconds
                        ):
                            if not self.is_market_open():
                                time.sleep(10)
                                continue
                            logger.warning(
                                f"Fyers WS Silent Disconnect Detected (No ticks > {int(stall_seconds)}s)."
                            )
                            self._close_ws_socket(self._fyers_ws, "silent disconnect")
                            break
                        if connected_since and now_ts - connected_since >= 30:
                            if getattr(self, "_fyers_ws_retry_count", 0):
                                logger.info("Fyers WS stable for 30s; resetting reconnect backoff.")
                            self._fyers_ws_retry_count = 0
                            self._fyers_ws_gateway_failures = 0
                            self._fyers_ws_last_error = ""
                        time.sleep(1)

                    self._fyers_ws_retry_count = min(6, getattr(self, "_fyers_ws_retry_count", 0) + 1)
                    backoff = self._fyers_ws_backoff_seconds(self._fyers_ws_retry_count)
                    if getattr(self, "_fyers_ws_auth_failed", False):
                        break
                    logger.warning(f"🔄 Fyers WS Reconnecting in {backoff}s...")
                    time.sleep(backoff)

                if getattr(self, "_fyers_ws_auth_failed", False):
                    logger.warning("Fyers WS reconnect paused until token refresh.")

            import threading
            t = threading.Thread(target=_ws_runner, daemon=True)
            t.start()
            logger.info("🚀 Fyers WebSocket Thread Started")
            
        except Exception as e:
            logger.error(f"Failed to start Fyers WebSocket: {e}")

    def _mark_fyers_auth_required(self, reason: str):
        self._fyers_auth_required = True
        self._fyers_auth_reason = reason

    def _clear_fyers_auth_required(self):
        self._fyers_auth_required = False
        self._fyers_auth_reason = ""

    @staticmethod
    def _fyers_missing_credentials(settings, require_pin: bool = False) -> List[str]:
        missing = []
        if not str(getattr(settings, "fyers_app_id", "") or "").strip():
            missing.append("FYERS_APP_ID")
        if not str(getattr(settings, "fyers_secret_key", "") or "").strip():
            missing.append("FYERS_SECRET_KEY")
        if require_pin and not str(getattr(settings, "fyers_password", "") or "").strip():
            missing.append("FYERS_PASSWORD")
        return missing

    def _refresh_fyers_token(self, token_data: Dict) -> bool:
        """Automatically refresh Fyers access token using refresh_token"""
        if self._fyers_refresh_disabled:
            return False
        try:
            from config.settings import get_settings
            settings = get_settings()
            refresh_token = token_data.get("refresh_token")
            
            if not refresh_token:
                logger.error("❌ Cannot refresh Fyers token: refresh_token missing")
                self._mark_fyers_auth_required("Fyers refresh token missing")
                return False

            missing = self._fyers_missing_credentials(settings, require_pin=True)
            if missing:
                message = f"Missing Fyers credentials in .env: {', '.join(missing)}"
                logger.error(f"❌ Cannot refresh Fyers token: {message}")
                self._mark_fyers_auth_required(message)
                return False

            # Fyers refresh requires a SHA256 hash of app_id + ":" + secret_key
            import requests
            import hashlib
            app_id = settings.fyers_app_id
            secret = settings.fyers_secret_key
            app_id_hash = hashlib.sha256((app_id + ":" + secret).encode()).hexdigest()
            
            # Pin must also be hashed
            pin = settings.fyers_password
            pin_hash = hashlib.sha256(pin.encode()).hexdigest()
            
            payload = {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "appIdHash": app_id_hash,
                "pin": pin_hash
            }
            
            url = "https://api-t1.fyers.in/api/v3/validate-refresh-token"
            response_raw = requests.post(url, json=payload, timeout=20)
            response = response_raw.json()
            if response and response.get('s') == 'ok' and response.get('access_token'):
                new_access_token = response.get('access_token')
                token_data['access_token'] = new_access_token
                
                # Update file
                with open("fyers_token.json", "w") as f:
                    json.dump(token_data, f)
                
                # Update current instance
                self.fyers = fyersModel.FyersModel(
                    client_id=settings.fyers_app_id,
                    token=new_access_token,
                    log_path=_fyers_sdk_log_path(),
                )
                self._fyers_token_updated_at = datetime.now().isoformat()
                self._fyers_refresh_disabled = False
                self._clear_fyers_auth_required()
                logger.success("✅ Fyers Token Refreshed Successfully!")
                self.start_fyers_websocket(new_access_token)
                return True
            else:
                if response and response.get("code") == -16:
                    self._fyers_refresh_disabled = True
                    self._mark_fyers_auth_required("Fyers automatic refresh is disabled by broker policy. Manual login is required.")
                    logger.warning("Fyers token refresh is disabled by broker policy; using available cache/fallback data.")
                else:
                    self._mark_fyers_auth_required(f"Fyers token refresh failed: {response}")
                    logger.error(f"❌ Fyers Token Refresh Failed: {response}")
                return False
        except Exception as e:
            self._mark_fyers_auth_required(f"Error during Fyers token refresh: {e}")
            logger.error(f"❌ Error during Fyers token refresh: {e}")
            return False

    def get_fyers_auth_status(self) -> Dict[str, Any]:
        token_path = Path("fyers_token.json")
        token_mtime = None
        if token_path.exists():
            token_mtime = datetime.fromtimestamp(token_path.stat().st_mtime).isoformat()
        return {
            "connected": self.fyers is not None,
            "auth_required": self._fyers_auth_required,
            "reason": self._fyers_auth_reason,
            "refresh_disabled": self._fyers_refresh_disabled,
            "token_updated_at": self._fyers_token_updated_at or token_mtime,
        }

    def get_fyers_auth_url(self) -> str:
        from config.settings import get_settings
        settings = get_settings()
        missing = self._fyers_missing_credentials(settings)
        if missing:
            message = f"Missing Fyers credentials in .env: {', '.join(missing)}"
            self._mark_fyers_auth_required(message)
            raise ValueError(message)
        session = fyersModel.SessionModel(
            client_id=settings.fyers_app_id,
            secret_key=settings.fyers_secret_key,
            redirect_uri=settings.fyers_redirect_uri,
            response_type="code",
            grant_type="authorization_code",
        )
        return session.generate_authcode()

    def update_fyers_token_from_auth_code(self, auth_code: str) -> Dict[str, Any]:
        """Complete manual Fyers login after the user pastes auth_code or the redirect URL."""
        from config.settings import get_settings
        settings = get_settings()
        missing = self._fyers_missing_credentials(settings)
        if missing:
            message = f"Missing Fyers credentials in .env: {', '.join(missing)}"
            self._mark_fyers_auth_required(message)
            return {"status": "error", "message": message, "missing": missing}
        code = (auth_code or "").strip()
        
        # Robust parsing for both full URL and raw code
        if "auth_code=" in code or "code=" in code:
            from urllib.parse import parse_qs, urlparse
            parsed = urlparse(code)
            query = parse_qs(parsed.query)
            if not query and parsed.fragment:
                query = parse_qs(parsed.fragment)
            code = query.get("auth_code", query.get("code", [code]))[0]
            
        if not code:
            return {"status": "error", "message": "Missing Fyers auth_code"}

        session = fyersModel.SessionModel(
            client_id=settings.fyers_app_id,
            secret_key=settings.fyers_secret_key,
            redirect_uri=settings.fyers_redirect_uri,
            response_type="code",
            grant_type="authorization_code",
        )
        session.set_token(code)
        response = session.generate_token()
        if not response or response.get("s") != "ok" or not response.get("access_token"):
            self._mark_fyers_auth_required(f"Manual Fyers token generation failed: {response}")
            return {"status": "error", "message": "Fyers token generation failed", "response": response}

        token_data = {
            "access_token": response.get("access_token"),
            "refresh_token": response.get("refresh_token"),
            "timestamp": datetime.now().isoformat(),
        }
        with open("fyers_token.json", "w") as f:
            json.dump(token_data, f, indent=2)

        self.fyers = fyersModel.FyersModel(
            client_id=settings.fyers_app_id,
            token=token_data["access_token"],
            log_path=_fyers_sdk_log_path(),
        )
        profile = self.fyers.get_profile()
        if profile and profile.get("s") == "error":
            self.fyers = None
            self._mark_fyers_auth_required(f"New Fyers token validation failed: {profile}")
            return {"status": "error", "message": "New Fyers token did not validate", "response": profile}

        self._fyers_token_updated_at = token_data["timestamp"]
        self._fyers_refresh_disabled = False
        self._clear_fyers_auth_required()
        logger.success("✅ Fyers manual authorization completed successfully.")
        if getattr(self, "start_streams", True):
            self.start_fyers_websocket(token_data["access_token"])
        return {"status": "ok", "message": "Fyers token updated", "profile": profile}

    def is_market_holiday(self, dt: datetime = None) -> bool:
        """Check if current or given date is an NSE market holiday"""
        dt = dt or datetime.now()
        date_str = dt.strftime("%Y-%m-%d")
        coverage_years = getattr(self, "nse_calendar_coverage_years", None)
        if coverage_years is None:
            coverage_years = {
                int(str(day)[:4])
                for day in getattr(self, "nse_holidays", set())
                if str(day)[:4].isdigit()
            }
        if coverage_years and dt.year not in coverage_years:
            # Fail CLOSED: an uncovered year is treated as non-trading until its
            # calendar is provided (deliberate safety — do not trade a year whose
            # holidays we cannot verify; see test_unknown_future_calendar_year_*).
            # Log CRITICAL only ONCE per uncovered year rather than on every call, so
            # a look-ahead probe (e.g. a 2026 session touching 2027) does not spam the
            # log ×N — today that produced 16 identical CRITICALs.
            warned = getattr(self, "_holiday_coverage_warned_years", None)
            if warned is None:
                warned = set()
                self._holiday_coverage_warned_years = warned
            if dt.year not in warned:
                warned.add(dt.year)
                logger.critical(
                    f"NSE holiday calendar has no explicit coverage for {dt.year}; "
                    "treating that year's sessions as CLOSED until data_store/market_holidays.json "
                    "(or the NSE holiday list) is updated."
                )
            return True
        return date_str in self.nse_holidays

    def is_special_trading_session(self, dt: datetime = None) -> bool:
        dt = dt or datetime.now()
        return dt.strftime("%Y-%m-%d") in self.nse_special_trading_days

    def is_market_open(self, dt: datetime = None) -> bool:
        """Robust check for NSE Market Status (09:15 - 15:30, Mon-Fri, No Holidays)"""
        dt = dt or datetime.now()
        special_session = self.is_special_trading_session(dt)
        # 1. Weekend check
        if dt.weekday() >= 5 and not special_session: return False
        # 2. Holiday check
        if self.is_market_holiday(dt) and not special_session: return False
        # 3. Session time check (09:15 - 15:30)
        curr_time = dt.time()
        start_time = dtime(9, 15)
        end_time = dtime(15, 30)
        return start_time <= curr_time <= end_time

    def get_market_session_label(self, dt: datetime = None) -> str:
        """Return the display session for index quotes."""
        dt = dt or datetime.now()
        special_session = self.is_special_trading_session(dt)
        if (dt.weekday() >= 5 or self.is_market_holiday(dt)) and not special_session:
            return "CLOSED"
        curr_time = dt.time()
        preopen_start = self._clock_setting_time("real_mode_premarket_arm_start", "09:00:00")
        live_start = self._clock_setting_time("real_mode_live_start", "09:15:00")
        live_end = self._clock_setting_time("real_mode_live_end", "15:30:00")
        if preopen_start <= curr_time < live_start:
            return "PRE-OPEN"
        if live_start <= curr_time <= live_end:
            return "LIVE"
        return "CLOSED"

    @staticmethod
    def _clock_setting_time(setting_name: str, default: str) -> dtime:
        try:
            from config.settings import get_settings

            label = str(getattr(get_settings(), setting_name, default) or default).strip()
        except Exception:
            label = default
        for fmt in ("%H:%M:%S", "%H:%M"):
            try:
                return datetime.strptime(label, fmt).time()
            except ValueError:
                continue
        return datetime.strptime(default, "%H:%M:%S" if len(default.split(":")) == 3 else "%H:%M").time()

    def _preopen_rest_quote_allowed(self, dt: datetime = None, session_label: str = "") -> bool:
        """Use pre-open REST only during the configured settlement window."""
        if str(session_label or "").upper() != "PRE-OPEN":
            return False
        dt = dt or datetime.now()
        settle_end = self._clock_setting_time("real_mode_premarket_settle_end", "09:08:00")
        return dt.time() <= settle_end

    def _quote_label_for_session(self, session: str) -> str:
        session = str(session or "").upper()
        if session in {"LIVE", "PRE-OPEN"}:
            return session
        return "REF"

    def _connect_attempt(self) -> bool:
        if not HAS_SMARTAPI or not self.api_key:
            return False
            
        import threading
        if not hasattr(self, "_connect_lock"):
            self._connect_lock = threading.Lock()
            
        with self._connect_lock:
            # Check if another thread already connected while we were waiting
            if getattr(self, "is_connected", False) and getattr(self, "smart_api", None):
                # Optionally check if token is valid, but returning True is safe
                return True
                
            self.smart_api = SmartConnect(api_key=self.api_key)
            # SmartConnect configures logzero handlers inside its constructor;
            # install again afterwards so every future SDK record is redacted.
            install_smartapi_log_redaction()
            totp = pyotp.TOTP(self.totp_secret).now()
            
            import requests
            import json
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            totp = pyotp.TOTP(self.totp_secret).now()
            
            import requests
            import json
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            
            clean_client_id = self.client_id.strip("'").strip('"')
            clean_password = self.password.strip("'").strip('"')
            clean_api_key = self.api_key.strip("'").strip('"')
            
            url = "https://apiconnect.angelbroking.com/rest/auth/angelbroking/user/v1/loginByPassword"
            payload = {"clientcode": clean_client_id, "password": clean_password, "totp": totp}
            headers = {
                "X-UserType": "USER",
                "X-SourceID": "WEB",
                "X-ClientLocalIP": "127.0.0.1",
                "X-ClientPublicIP": "127.0.0.1",
                "X-MACAddress": "00:00:00:00:00:00",
                "X-PrivateKey": clean_api_key,
                "Content-Type": "application/json",
                "Accept": "application/json"
            }
            
            try:
                response = requests.post(url, json=payload, headers=headers, timeout=10, verify=False)
                data = response.json()
            except Exception as e:
                data = {"status": False, "message": f"HTTP Error: {str(e)}"}
                
            if not data.get('status'):
                raise Exception(f"Login failed: {data.get('message', 'Unknown error')} - {data}")
                
            jwt_token = data.get('data', {}).get('jwtToken')
            feed_token = data.get('data', {}).get('feedToken')
            refresh_token = data.get('data', {}).get('refreshToken')
            
            self.smart_api.setAccessToken(jwt_token)
            self.smart_api.setRefreshToken(refresh_token)
            self.smart_api.setFeedToken(feed_token)
            self.smart_api.setUserId(clean_client_id)
            self._angel_jwt_token = jwt_token
            self._angel_feed_token = feed_token
            
        self.is_connected = True
        logger.success("✅ AngelOne Connected")
        # Start WebSocket only for the live provider. History workers use REST only.
        if self.start_streams:
            jwt_token = data.get('data', {}).get('jwtToken')
            feed_token = self.smart_api.getfeedToken()
            self.start_websocket(jwt_token, feed_token)
        return True

    @retry_on_exception(retries=3, delay=1.0)
    def _connect_with_retry(self) -> bool:
        return self._connect_attempt()

    def connect(self) -> bool:
        try:
            logger.info(
                f"Trying to connect AngelOne. HAS_SMARTAPI: {HAS_SMARTAPI}, "
                f"api_key_configured={bool(self.api_key)}"
            )
            return self._connect_with_retry()
        except Exception as e:
            logger.error(f"❌ AngelOne connection failed after retries: {e}")
            return False

    def start_websocket(self, jwt_token: str, feed_token: str):
        """Start AngelOne WebSocket in a separate thread"""
        if not SmartWebSocketV2:
            logger.warning("SmartWebSocketV2 not available")
            return
            
        try:
            def on_data(ws, tick_data):
                token = tick_data.get('token')
                ltp = tick_data.get('last_traded_price')
                oi = tick_data.get('open_interest', 0)
                oi_change_pct = tick_data.get('open_interest_change_percentage', 0)
                vol, vol_key, vol_fields = self._extract_index_tick_volume(
                    tick_data,
                    (
                        "volume_trade_for_the_day",
                        "vol_traded_today",
                        "volume_traded_today",
                        "volume",
                        "vol",
                        "last_traded_quantity",
                        "last_traded_qty",
                        "ltq",
                        "qty",
                        "quantity",
                    ),
                )
                
                if token and ltp:
                    now_ts = time.time()
                    self._last_angel_ws_tick = now_ts
                    self._last_ws_tick = now_ts
                    self._last_ltp_time = now_ts
                    price = float(ltp) / 100.0
                    # Store as dict instead of float
                    self._ws_cache[token] = {
                        'ltp': price,
                        'oi': int(oi),
                        'oi_change_pct': float(oi_change_pct or 0.0),
                        'vol': int(vol),
                        'volume_key': vol_key,
                        'volume_fields': dict(vol_fields or {}),
                        'previous_close': self._quote_float(tick_data, *PREVIOUS_CLOSE_KEYS),
                        'time': now_ts,
                        'source': 'angel_ws',
                    }
                    last_ws_debug = getattr(self, "_last_ws_debug_log", {})
                    if now_ts - last_ws_debug.get(token, 0) >= 10:
                        logger.debug(f"WS Tick: {token} -> {price} | OI: {oi} | Vol: {vol}")
                        last_ws_debug[token] = now_ts
                        self._last_ws_debug_log = last_ws_debug
                    
                    if hasattr(self, 'on_tick') and self.on_tick:
                        if token in INDEX_SHORT_TOKENS:
                            index_name = INDEX_SHORT_TOKENS[token]
                            self._record_index_tick_volume_diag(
                                index_name,
                                "angel_ws",
                                float(vol or 0.0),
                                vol_key,
                                vol_fields,
                                now_ts,
                            )
                            self.on_tick(index_name, price, int(vol), datetime.now())

            def on_open(ws):
                logger.info("🔌 AngelOne WebSocket Connected")
                if hasattr(self.sws, "current_retry_attempt"):
                    self.sws.current_retry_attempt = 0
                self._angel_ws_retry_count = 0
                self._angel_ws_connected = True
                self._last_angel_ws_tick = time.time()
                self._last_ws_tick = self._last_angel_ws_tick
                
                token_list = [{"exchangeType": exch, "tokens": list(toks)} for exch, toks in self._active_angel_tokens.items() if toks]
                if token_list:
                    self.sws.subscribe(correlation_id="ut1_default", mode=2, token_list=token_list)
                    logger.info(f"Subscribed to tokens (Mode 2): {token_list}")

            def on_error(ws, error):
                self._angel_ws_connected = False
                logger.error(f"WS Error: {error}")

            def on_close(ws):
                self._angel_ws_connected = False
                logger.warning("🔌 AngelOne WebSocket Closed")

            def _build_and_bind_sws(curr_jwt, curr_feed):
                sws_inst = SmartWebSocketV2(
                    curr_jwt,
                    self.api_key,
                    self.client_id,
                    curr_feed,
                    max_retry_attempt=2,
                    retry_strategy=1,
                    retry_delay=2,
                    retry_multiplier=1.5,
                    retry_duration=10,
                )
                sws_inst.on_data = on_data
                sws_inst.on_open = on_open
                sws_inst.on_error = on_error
                sws_inst.on_close = on_close
                return sws_inst

            self.sws = _build_and_bind_sws(jwt_token, feed_token)

            def _ws_runner():
                self._angel_ws_retry_count = 0
                
                def watchdog():
                    while True:
                        if self._last_angel_ws_tick > 0 and getattr(self, '_angel_ws_connected', False) and (time.time() - self._last_angel_ws_tick > 60):
                            if self.is_market_open():
                                logger.warning("AngelOne WS Silent Disconnect Detected (No ticks > 60s).")
                                self._close_ws_socket(getattr(self, "sws", None), "angel silent disconnect")
                        time.sleep(10)
                
                import threading
                threading.Thread(target=watchdog, daemon=True, name="AngelWSWatchdog").start()

                while True:
                    try:
                        if getattr(self, "_angel_ws_retry_count", 0) > 0 or not getattr(self, "sws", None):
                            if getattr(self, "_angel_ws_retry_count", 0) >= 3 or not getattr(self, "is_connected", False):
                                try:
                                    logger.info("Refreshing AngelOne REST login before WS reconnect...")
                                    self._connect_with_retry()
                                except Exception as ex:
                                    logger.debug(f"AngelOne login refresh during WS reconnect failed: {ex}")
                            curr_jwt = getattr(self, "_angel_jwt_token", None) or jwt_token
                            curr_feed = getattr(self, "_angel_feed_token", None) or feed_token
                            if hasattr(self.smart_api, "getfeedToken"):
                                try:
                                    ft = self.smart_api.getfeedToken()
                                    if ft:
                                        curr_feed = ft
                                except Exception as ex:
                                    logger.debug(
                                        f"AngelOne getfeedToken() during WS reconnect failed; "
                                        f"reusing prior feed token: {ex}"
                                    )
                            self.sws = _build_and_bind_sws(curr_jwt, curr_feed)
                        
                        self.sws.connect()
                    except Exception as e:
                        logger.error(f"AngelOne WS Runner error: {e}")
                    
                    self._angel_ws_retry_count += 1
                    backoff = min(60, 2 ** self._angel_ws_retry_count)
                    logger.warning(f"🔄 AngelOne WS Reconnecting in {backoff}s (Attempt {self._angel_ws_retry_count})...")
                    time.sleep(backoff)

            import threading
            t = threading.Thread(target=_ws_runner, daemon=True, name="AngelWSRunner")
            t.start()
            logger.info("🚀 WebSocket Thread Started")
            
        except Exception as e:
            logger.error(f"Failed to start WebSocket: {e}")

    def subscribe_tokens(self, tokens: List[str], exchange_type: int = 1):
        """Dynamically subscribe to more tokens"""
        active = self._active_angel_tokens.setdefault(exchange_type, set())
        new_tokens = [str(token) for token in tokens if str(token) and str(token) not in active]
        if not new_tokens:
            return
        active.update(new_tokens)
        if hasattr(self, 'sws') and self.sws:
            if not getattr(self, '_angel_ws_connected', False):
                logger.warning(f"WebSocket is closed. Token subscription delayed until reconnection.")
                return
            try:
                # AngelOne exposes OI only in SNAP_QUOTE mode (3), not QUOTE (2).
                self.sws.subscribe(correlation_id="ut1_dynamic", mode=3, token_list=[{"exchangeType": exchange_type, "tokens": new_tokens}])
                logger.info(f"📡 AngelOne: Dynamically subscribed to {len(new_tokens)} option tokens (SNAP_QUOTE mode)")
            except Exception as e:
                logger.error(f"Failed to subscribe to tokens: {e}")

    def get_source_health(self) -> Dict[str, Any]:
        """Check each provider independently; Yahoo is an idle emergency source."""
        health = build_source_health(self)
        remaining = max(
            0.0,
            float(getattr(self, "_angel_rest_backoff_until", 0.0) or 0.0) - time.time(),
        )
        health.update({
            "angel_rest_backoff_active": remaining > 0.0,
            "angel_rest_backoff_seconds_remaining": round(remaining, 2),
            "angel_rest_backoff_failures": int(
                getattr(self, "_angel_rest_backoff_failures", 0) or 0
            ),
        })
        if self.smart_api is not None:
            try:
                limiter = get_shared_api_limiter(self.smart_api)
                health["angel_rest_quote_gate"] = {
                    **limiter.snapshot(),
                    "paced_calls": int(limiter.calls),
                    "pacing_timeouts": int(limiter.timeouts),
                }
            except (AttributeError, TypeError):
                # Lightweight test/dry-run stubs may be immutable bare objects.
                health["angel_rest_quote_gate"] = {"available": False}
        return health

    @staticmethod
    def _numeric_tick_fields(payload: Dict[str, Any]) -> Dict[str, float]:
        fields: Dict[str, float] = {}
        for key, value in (payload or {}).items():
            key_text = str(key or "").lower()
            if not any(part in key_text for part in ("vol", "qty", "quantity")):
                continue
            try:
                numeric = float(value or 0.0)
            except Exception:
                continue
            fields[str(key)] = numeric
        return fields

    def _extract_index_tick_volume(
        self,
        payload: Dict[str, Any],
        preferred_keys: Tuple[str, ...],
    ) -> Tuple[float, str, Dict[str, float]]:
        fields = self._numeric_tick_fields(payload)
        for key in preferred_keys:
            if key in fields and fields[key] > 0:
                return fields[key], key, fields
        for key, value in fields.items():
            if value > 0:
                return value, key, fields
        first_key = next((key for key in preferred_keys if key in fields), "")
        return (fields.get(first_key, 0.0) if first_key else 0.0), first_key, fields

    def _record_index_tick_volume_diag(
        self,
        instrument: str,
        source: str,
        volume: float,
        volume_key: str,
        volume_fields: Dict[str, float],
        now_ts: Optional[float] = None,
    ) -> None:
        name = str(instrument or "").upper()
        if not name:
            return
        now = time.time() if now_ts is None else float(now_ts)
        minute_bucket = int(now // 60)
        previous = self._index_tick_volume_diag.get(name, {})
        tick_count = int(previous.get("tick_count_current_minute", 0) or 0)
        if int(previous.get("minute_bucket", -1) or -1) != minute_bucket:
            tick_count = 0
        tick_count += 1
        self._index_tick_volume_diag[name] = {
            "instrument": name,
            "source": str(source or ""),
            "volume_available": bool(float(volume or 0.0) > 0.0),
            "volume_reliable": bool(float(volume or 0.0) > 0.0),
            "volume": float(volume or 0.0),
            "volume_key": str(volume_key or ""),
            "volume_fields": dict(volume_fields or {}),
            "tick_count_current_minute": tick_count,
            "minute_bucket": minute_bucket,
            "updated_at": now,
        }

    def get_index_tick_volume_diagnostics(self) -> Dict[str, Dict[str, Any]]:
        return {
            str(inst): dict(payload)
            for inst, payload in (getattr(self, "_index_tick_volume_diag", {}) or {}).items()
            if isinstance(payload, dict)
        }

    def get_market_source_snapshot(self, symbol: str) -> Dict[str, Any]:
        """Return source latency metadata for live-vs-delayed signal weighting."""
        return build_market_source_snapshot(
            self,
            symbol,
            INDEX_SPOT_TOKENS,
            FYERS_INDEX_SYMBOLS,
        )

    # ═══════════════════════════════════════════════════════════
    # UNIFIED PRICE BRIDGE (The "Master" Lock)
    # ═══════════════════════════════════════════════════════════

    def _select_index_ws_quote(
        self,
        symbol: str,
        token: str = "",
        now_ts: Optional[float] = None,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[float]]:
        """Select one stable live source instead of oscillating between brokers.

        AngelOne is the order-authoritative index feed. Keep using it while its
        tick is entry-eligible; only fail over to Fyers when AngelOne is stale.
        Choosing the newest tick across both feeds made dashboard LTPs jump
        backward and forward whenever their arrival order changed.
        """
        now = time.time() if now_ts is None else float(now_ts)
        configured_token = INDEX_SPOT_TOKENS.get(symbol, ("", ""))[0]
        angel_token = token or configured_token
        short_token = angel_token[3:] if angel_token.startswith("999") else angel_token

        def freshest(keys: Tuple[str, ...], max_age: float):
            candidates = []
            for key in dict.fromkeys(key for key in keys if key):
                cached = self._ws_cache.get(key)
                if not isinstance(cached, dict):
                    continue
                if float(cached.get("ltp", 0.0) or 0.0) <= 0:
                    continue
                age = max(0.0, now - float(cached.get("time") or 0.0))
                if age <= max_age:
                    candidates.append((age, cached))
            return min(candidates, key=lambda item: item[0]) if candidates else None

        # Match market_source_health: Angel ticks remain authoritative for six
        # seconds. Fyers is a live display/analysis failover, not a competing
        # tick that should replace AngelOne several times per second.
        selected = freshest((angel_token, short_token), 6.0)
        if selected is None:
            selected = freshest((FYERS_INDEX_SYMBOLS.get(symbol, ""),), 15.0)
        if selected is None:
            return None, None
        return selected[1], selected[0]

    @staticmethod
    def _cache_epoch(value: Any) -> float:
        if isinstance(value, datetime):
            return value.timestamp()
        try:
            return float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def get_cached_contract_ltp(
        self,
        exchange: str,
        symbol: str,
        token: str,
        max_age_seconds: float = 10.0,
    ) -> Optional[float]:
        """Return a fresh contract mark without starting any REST request."""
        del exchange  # Cache keys are broker token/symbol based.
        now_ts = time.time()
        max_age = max(0.0, float(max_age_seconds or 0.0))
        token = str(token or "")
        symbol = str(symbol or "")
        short_token = token[3:] if token.startswith("999") else token
        for cache, keys in (
            (getattr(self, "_ws_cache", {}) or {}, (token, short_token, symbol)),
            (getattr(self, "_ltp_cache", {}) or {}, (symbol, token, short_token)),
        ):
            for key in dict.fromkeys(key for key in keys if key):
                payload = cache.get(key)
                if not isinstance(payload, dict):
                    continue
                price = float(payload.get("ltp", 0.0) or 0.0)
                cache_ts = self._cache_epoch(payload.get("time"))
                age = now_ts - cache_ts if cache_ts > 0 else float("inf")
                if price > 0 and 0.0 <= age <= max_age:
                    self._last_ltp_time = cache_ts
                    return price
        return None
    
    @retry_on_exception(retries=1, delay=0.1)
    def get_latest_price_by_token(self, token: str, symbol: str, exchange: str = "NSE") -> float:
        """The Master Source for Index Spot Prices — Weekend Precision Fix"""
        def _remember_ltp_time(val: Any) -> float:
            if isinstance(val, dict):
                self._last_ltp_time = float(val.get("time") or time.time())
                return val.get('ltp', 0.0)
            self._last_ltp_time = time.time()
            return val

        if symbol in INDEX_SPOT_TOKENS:
            now_ts = time.time()
            cached, _age = self._select_index_ws_quote(symbol, token, now_ts)
            if cached is not None:
                return _remember_ltp_time(cached)

        # Non-index contracts may not have redundant source metadata. Index
        # prices already passed the freshness gate above and must fall through
        # to REST/Yahoo when every websocket tick is stale.
        if symbol not in INDEX_SPOT_TOKENS:
            now_ts = time.time()

            def _is_valid_cache(cache_obj: Any) -> bool:
                if not isinstance(cache_obj, dict): return False
                age = now_ts - float(cache_obj.get("time") or 0.0)
                return age <= 29.0

            if token in self._ws_cache and _is_valid_cache(self._ws_cache[token]):
                return _remember_ltp_time(self._ws_cache[token])

            short_token = token[3:] if token.startswith('999') else token
            if short_token in self._ws_cache and _is_valid_cache(self._ws_cache[short_token]):
                return _remember_ltp_time(self._ws_cache[short_token])

            fyers_symbol = FYERS_INDEX_SYMBOLS.get(symbol, symbol)
            if fyers_symbol in self._ws_cache and _is_valid_cache(self._ws_cache[fyers_symbol]):
                return _remember_ltp_time(self._ws_cache[fyers_symbol])
            
        # 2. BROKER LIVE FEED (REST Fallback - TIER 2: FYERS)
        if getattr(self, 'fyers', None):
            try:
                f_sym = FYERS_INDEX_SYMBOLS.get(symbol)
                if f_sym:
                    f_resp = self.fyers.quotes({"symbols": f_sym})
                    if f_resp and f_resp.get('s') == 'ok':
                        f_ltp = f_resp.get('d', [{}])[0].get('v', {}).get('lp', 0)
                        if f_ltp > 0:
                            self._last_ltp_time = time.time()
                            self._ltp_cache[symbol] = {'ltp': f_ltp, 'time': datetime.now(), 'source': 'fyers'}
                            return f_ltp
            except Exception as exc:
                logger.debug(f"Fyers REST quote fallback failed for {symbol}: {exc}")

        # 3. BROKER LIVE FEED (REST Fallback - TIER 1: AngelOne)
        if self.is_connected and self.smart_api:
            if not self._throttle_angel_rest("ltpData"):
                return self._ltp_cache.get(symbol, {}).get('ltp', 0.0)
            try:
                # Double-check cache after acquiring the single-flight lease.
                with self._rest_lock:
                    cached = self._ltp_cache.get(symbol)
                    cached_time = cached.get('time', datetime.min) if isinstance(cached, dict) else datetime.min
                    if isinstance(cached_time, datetime):
                        cached_age = (datetime.now() - cached_time).total_seconds()
                    elif isinstance(cached_time, (int, float)):
                        cached_age = time.time() - float(cached_time)
                    else:
                        cached_age = float("inf")
                    if isinstance(cached, dict) and 0.0 <= cached_age < 5.0:
                        return cached['ltp']

                try:
                    trading_symbol = "INDEX" if symbol in INDEX_SPOT_TOKENS else symbol
                    resp = self.smart_api.ltpData(exchange, trading_symbol, token)
                    if not isinstance(resp, dict):
                        raise RuntimeError(f"Angel REST returned invalid {type(resp).__name__} response")

                    # Check for Invalid Token error
                    if not resp.get('status') and resp.get('errorCode') == 'AG8001':
                        logger.warning("🔄 AngelOne Session Expired (AG8001). Re-authenticating...")
                        if self.connect():
                            resp = self.smart_api.ltpData(exchange, trading_symbol, token)
                        else:
                            return self._ltp_cache.get(symbol, {}).get('ltp', 0.0)

                    if isinstance(resp, dict) and resp.get('status'):
                        self._mark_angel_rest_success()
                    elif isinstance(resp, dict):
                        if self._angel_rest_should_backoff(resp):
                            self._mark_angel_rest_backoff("ltpData", resp)
                        else:
                            logger.warning(f"Angel REST failure for {symbol}: {resp}")

                    if isinstance(resp, dict) and resp.get('status') and resp.get('data'):
                        ltp = float(resp['data'].get('ltp', 0.0))
                        close_px = float(resp['data'].get('close', 0.0))

                        if close_px > 0:
                            self._remember_previous_close(symbol, close_px, "angel_rest")

                        if ltp > 0:
                            self._last_ltp_time = time.time()
                            self._ltp_cache[symbol] = {'ltp': ltp, 'time': datetime.now(), 'source': 'angel'}
                            return ltp
                except Exception as e:
                    if "Invalid Token" in str(e) or "AG8001" in str(e):
                        logger.warning("🔄 AngelOne Exception: Invalid Token. Re-authenticating...")
                        self.connect()
                    else:
                        if self._angel_rest_should_backoff(e):
                            self._mark_angel_rest_backoff("ltpData", e)
                        logger.debug(f"Angel REST LTP fetch failed for {symbol}: {e}")
            finally:
                self._release_angel_rest_quote()
        
        # 4. LAST RESORT: YAHOO (TIER 3 - 15min DELAYED)
        ticker = YAHOO_TICKERS.get(symbol)
        if ticker:
            try:
                self._yahoo_request_count += 1
                y_ticker = yf.Ticker(ticker)
                y_data = y_ticker.fast_info
                y_ltp = y_data.last_price
                if y_ltp > 0:
                    logger.warning(f"⚠️ EMERGENCY: Using 15min delayed Yahoo data for {symbol}")
                    self._last_ltp_time = time.time()
                    self._yahoo_last_used_at = self._last_ltp_time
                    self._ltp_cache[symbol] = {'ltp': y_ltp, 'time': datetime.now(), 'source': 'yahoo'}
                    return y_ltp
            except Exception as exc:
                logger.debug(f"Yahoo fast_info fallback failed for {symbol}: {exc}")

        # 5. INTERNAL CACHE FALLBACK
        return self._ltp_cache.get(symbol, {}).get('ltp', 0.0)

    def _get_preopen_fyers_index_quote(
        self,
        symbol: str,
        fyers_symbol: str,
        now_ts: float,
        ttl_seconds: float = 5.0,
    ) -> Optional[Dict[str, Any]]:
        """Use Fyers REST as the pre-open index source.

        AngelOne index WS can emit fresh timestamps with reference/previous
        close values during pre-open. Fyers REST is slower, so this path is
        short-cached and only used before the regular session opens.
        """
        if not getattr(self, "fyers", None) or not fyers_symbol:
            return None

        cache = getattr(self, "_preopen_index_quote_cache", None)
        if cache is None:
            cache = {}
            self._preopen_index_quote_cache = cache

        cached = cache.get(symbol)
        if isinstance(cached, dict):
            age = now_ts - float(cached.get("time") or 0.0)
            if 0 <= age <= ttl_seconds and isinstance(cached.get("quote"), dict):
                quote = dict(cached["quote"])
                quote["quote_age_seconds"] = age
                return quote

        try:
            values = self._fyers_index_quote_values(symbol)
            ltp = float(values.get("lp", 0.0) or 0.0)
            if ltp > 0:
                previous_close = self._quote_float(values, *PREVIOUS_CLOSE_KEYS)
                if previous_close > 0:
                    self._remember_previous_close(symbol, previous_close, "fyers_rest")
                quote = {
                    "ltp": ltp,
                    "change": float(values.get("ch", 0.0) or 0.0),
                    "change_pct": float(values.get("chp", 0.0) or 0.0),
                    "previous_close": previous_close,
                    "prev_close": previous_close,
                    "source": "fyers_rest_preopen",
                    "quote_age_seconds": 0.0,
                }
                cache[symbol] = {"time": now_ts, "quote": quote}
                self._last_ltp_time = now_ts
                self._ltp_cache[symbol] = {"ltp": ltp, "time": datetime.now(), "source": "fyers_rest_preopen"}
                return dict(quote)
        except Exception as exc:
            logger.debug(f"Pre-open Fyers quote fetch failed for {symbol}: {exc}")
        return None

    def get_index_quote(self, symbol: str) -> Dict[str, Any]:
        """Return index LTP plus broker-provided change fields when available."""
        now_dt = datetime.now()
        session_label = self.get_market_session_label(now_dt)
        quote = {
            "ltp": 0.0, 
            "change": 0.0, 
            "change_pct": 0.0, 
            "source": "unknown", 
            "quote_label": session_label,
            "session": session_label,
            "is_preopen": session_label == "PRE-OPEN"
        }
        if symbol not in INDEX_SPOT_TOKENS:
            return quote

        now_ts = time.time()
        token, exchange = INDEX_SPOT_TOKENS.get(symbol, ("", "NSE"))
        short_token = token[3:] if token.startswith("999") else token
        fyers_symbol = FYERS_INDEX_SYMBOLS.get(symbol)

        preopen_rest_allowed = self._preopen_rest_quote_allowed(
            dt=now_dt,
            session_label=session_label,
        )
        quote["preopen_settlement"] = preopen_rest_allowed

        if preopen_rest_allowed and fyers_symbol:
            preopen_quote = self._get_preopen_fyers_index_quote(symbol, fyers_symbol, now_ts)
            if preopen_quote:
                quote.update(preopen_quote)
                return quote

        cached, quote_age = self._select_index_ws_quote(symbol, token, now_ts)
        if cached is not None:
            previous_close = float(cached.get("previous_close", 0.0) or 0.0)
            if previous_close <= 0:
                previous_close = self.get_previous_close(symbol)
            quote.update({
                "ltp": float(cached.get("ltp", 0.0) or 0.0),
                "change": float(cached.get("change", 0.0) or 0.0),
                "change_pct": float(cached.get("change_pct", 0.0) or 0.0),
                "previous_close": previous_close,
                "prev_close": previous_close,
                "source": str(cached.get("source") or "ws"),
                "quote_age_seconds": quote_age,
            })
            self._last_ltp_time = float(cached.get("time") or now_ts)
            # A live WebSocket LTP must never be replaced by a slower REST
            # quote merely because the WS payload omitted day-change fields.
            # The scanner uses LTP to build the forming candle and detect UT
            # crossovers, so waiting for/restoring an older REST value here
            # can delay an otherwise real-time intrabar signal.
            if previous_close > 0:
                if not quote["change"]:
                    quote["change"] = quote["ltp"] - previous_close
                if not quote["change_pct"]:
                    quote["change_pct"] = quote["change"] / previous_close * 100.0
            return quote

        if getattr(self, "fyers", None) and fyers_symbol:
            try:
                values = self._fyers_index_quote_values(symbol)
                ltp = float(values.get("lp", 0.0) or 0.0)
                if ltp > 0:
                    previous_close = self._quote_float(values, *PREVIOUS_CLOSE_KEYS)
                    if previous_close > 0:
                        self._remember_previous_close(symbol, previous_close, "fyers_rest")
                    quote.update({
                        "ltp": ltp,
                        "change": float(values.get("ch", 0.0) or 0.0),
                        "change_pct": float(values.get("chp", 0.0) or 0.0),
                        "previous_close": previous_close,
                        "prev_close": previous_close,
                        "source": "fyers_rest",
                        "quote_age_seconds": 0.0,
                    })
                    self._last_ltp_time = now_ts
                    self._ltp_cache[symbol] = {"ltp": ltp, "time": datetime.now(), "source": "fyers"}
                    return quote
            except Exception as exc:
                logger.debug(f"Fyers quote fetch failed for {symbol}: {exc}")

        ltp = self.get_latest_price_by_token(token, symbol, exchange)
        quote["ltp"] = float(ltp or 0.0)
        cached_source = self._ltp_cache.get(symbol, {})
        quote["source"] = str(cached_source.get("source") or "fallback")
        if "time" in cached_source:
            quote["quote_age_seconds"] = (datetime.now() - cached_source["time"]).total_seconds()
        else:
            quote["quote_age_seconds"] = 0.0
        prev_close = self.get_previous_close(symbol)
        quote["previous_close"] = prev_close
        quote["prev_close"] = prev_close
        if quote["ltp"] > 0 and prev_close > 0:
            quote["change"] = quote["ltp"] - prev_close
            quote["change_pct"] = quote["change"] / prev_close * 100.0
        return quote

    def get_ltp(self, exchange: str, symbol: str, token: str) -> Optional[float]:
        """Unified LTP method used by entire system - Always fetches from AngelOne"""
        price = self.get_latest_price_by_token(token, symbol, exchange)
        return price if price > 0 else None

    @staticmethod
    def _first_positive(values: Dict[str, Any], keys: Tuple[str, ...]) -> float:
        for key in keys:
            try:
                value = float(values.get(key) or 0.0)
            except (TypeError, ValueError, AttributeError):
                value = 0.0
            if value > 0:
                return value
        return 0.0

    def _touch_quote_from_payload(
        self,
        payload: Dict[str, Any],
        source: str,
        now_ts: float,
        max_age_seconds: float,
    ) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            return {"usable": False, "source": source, "reason": "invalid_payload"}
        quote_ts = payload.get("time") or payload.get("timestamp") or payload.get("last_update")
        try:
            quote_ts = float(quote_ts or now_ts)
        except (TypeError, ValueError):
            quote_ts = now_ts
        age = max(0.0, now_ts - quote_ts)
        bid = self._first_positive(
            payload,
            (
                "bid",
                "best_bid",
                "bid_price",
                "best_bid_price",
                "bp",
                "bidRate",
                "bidrate",
            ),
        )
        ask = self._first_positive(
            payload,
            (
                "ask",
                "best_ask",
                "ask_price",
                "best_ask_price",
                "ap",
                "askRate",
                "askrate",
                "offer",
                "offer_price",
            ),
        )
        depth = payload.get("depth") or payload.get("market_depth") or payload.get("best5") or payload.get("bidask")
        if (bid <= 0 or ask <= 0) and depth:
            parsed_depth = self._parse_depth_payload(depth, source)
            bid = bid or float(parsed_depth.get("bid", 0.0) or 0.0)
            ask = ask or float(parsed_depth.get("ask", 0.0) or 0.0)
        result = {
            "bid": bid,
            "ask": ask,
            "ltp": float(payload.get("ltp") or payload.get("lp") or payload.get("last_traded_price") or 0.0),
            "bid_qty": int(float(payload.get("bid_qty") or payload.get("bid_size") or payload.get("bidsize") or 0.0)),
            "ask_qty": int(float(payload.get("ask_qty") or payload.get("ask_size") or payload.get("asksize") or 0.0)),
            "source": source,
            "age_seconds": age,
            "usable": False,
        }
        if bid <= 0 or ask <= 0:
            result["reason"] = "missing_bid_ask"
            return result
        if ask < bid:
            result["reason"] = "crossed_book"
            return result
        if age > max_age_seconds:
            result["reason"] = "stale"
            return result
        spread = ask - bid
        mid = (ask + bid) / 2.0
        result.update({
            "spread": spread,
            "spread_pct": (spread / mid * 100.0) if mid > 0 else 0.0,
            "usable": True,
            "reason": "ok",
        })
        return result

    def get_contract_touch_quote(
        self,
        exchange: str,
        symbol: str,
        token: str,
        max_age_seconds: float = 1.5,
    ) -> Dict[str, Any]:
        """Return best-effort executable touch quote for an option/future contract.

        This is deliberately separate from signal LTP. It is used only by the
        execution layer to choose or shadow a marketable-limit cap.
        """
        now_ts = time.time()
        cache_key = f"{exchange}|{symbol}|{token}"
        cached = self._touch_quote_cache.get(cache_key)
        if isinstance(cached, dict):
            age = now_ts - float(cached.get("time") or 0.0)
            if 0 <= age <= max_age_seconds and isinstance(cached.get("quote"), dict):
                quote = dict(cached["quote"])
                quote["age_seconds"] = age
                return quote

        keys = [token, str(token), symbol]
        if symbol in FYERS_INDEX_SYMBOLS:
            keys.append(FYERS_INDEX_SYMBOLS.get(symbol, ""))
        for key in keys:
            cached_tick = self._ws_cache.get(key) if key else None
            if not isinstance(cached_tick, dict):
                continue
            quote = self._touch_quote_from_payload(
                cached_tick,
                str(cached_tick.get("source") or "ws_touch"),
                now_ts,
                max_age_seconds,
            )
            if quote.get("usable"):
                self._touch_quote_cache[cache_key] = {"time": now_ts, "quote": quote}
                return quote

        if getattr(self, "fyers", None):
            # Current instrument master is Angel-token based; option Fyers symbols
            # are used only when the caller/cache already provides one.
            fyers_symbol = FYERS_INDEX_SYMBOLS.get(symbol, symbol if ":" in str(symbol) else "")
            if fyers_symbol:
                try:
                    resp = self.fyers.quotes({"symbols": fyers_symbol})
                    if resp and resp.get("s") == "ok":
                        values = (resp.get("d") or [{}])[0].get("v", {})
                        quote = self._touch_quote_from_payload(
                            values,
                            "fyers_rest_touch",
                            now_ts,
                            max_age_seconds,
                        )
                        self._touch_quote_cache[cache_key] = {"time": now_ts, "quote": quote}
                        return quote
                except Exception as exc:
                    logger.debug(f"Fyers touch quote fetch failed for {symbol}: {exc}")

        quote = {
            "bid": 0.0,
            "ask": 0.0,
            "ltp": 0.0,
            "source": "unavailable",
            "age_seconds": 0.0,
            "usable": False,
            "reason": "unavailable",
        }
        self._touch_quote_cache[cache_key] = {"time": now_ts, "quote": quote}
        return quote

    def get_order_book_snapshot(self, exchange: str, symbol: str, token: str) -> Dict[str, Any]:
        """
        Best-effort bid/ask depth snapshot for OFR.

        This intentionally uses cached feed fields only. If the broker payload
        does not expose depth, callers receive a neutral unavailable result and
        keep using candle-pressure order flow.
        """
        keys = [token, str(token), symbol, FYERS_INDEX_SYMBOLS.get(symbol, "")]
        for key in keys:
            cached = self._ws_cache.get(key) if key else None
            if not isinstance(cached, dict):
                continue
            depth = cached.get("depth") or cached.get("market_depth") or cached.get("best5") or cached.get("bidask")
            parsed = self._parse_depth_payload(depth, cached.get("source", "ws_depth"))
            if parsed.get("usable"):
                return parsed
            bid_qty = cached.get("bid_qty") or cached.get("bid_size") or cached.get("bidsize")
            ask_qty = cached.get("ask_qty") or cached.get("ask_size") or cached.get("asksize")
            parsed = self._depth_from_qty(bid_qty, ask_qty, cached.get("source", "ws_depth"))
            if parsed.get("usable"):
                return parsed
        return {"ofr": 1.0, "bid_qty": 0, "ask_qty": 0, "source": "unavailable", "usable": False}

    def _parse_depth_payload(self, depth: Any, source: str = "depth") -> Dict[str, Any]:
        if not depth:
            return {"usable": False}
        bids, asks = [], []
        if isinstance(depth, dict):
            bids = depth.get("buy") or depth.get("bids") or depth.get("bid") or []
            asks = depth.get("sell") or depth.get("asks") or depth.get("ask") or []
        elif isinstance(depth, list):
            for row in depth:
                if not isinstance(row, dict):
                    continue
                side = str(row.get("side") or row.get("type") or "").lower()
                if side.startswith("b"):
                    bids.append(row)
                elif side.startswith("a") or side.startswith("s"):
                    asks.append(row)
        bid_qty = self._sum_depth_qty(bids)
        ask_qty = self._sum_depth_qty(asks)
        parsed = self._depth_from_qty(bid_qty, ask_qty, source)
        parsed["bid"] = self._best_depth_price(bids, "bid")
        parsed["ask"] = self._best_depth_price(asks, "ask")
        if parsed.get("bid", 0.0) > 0 and parsed.get("ask", 0.0) > 0:
            parsed["usable"] = True
        return parsed

    @staticmethod
    def _sum_depth_qty(rows: Any) -> float:
        if isinstance(rows, dict):
            rows = [rows]
        total = 0.0
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            qty = row.get("quantity", row.get("qty", row.get("size", row.get("volume", 0))))
            try:
                total += float(qty or 0)
            except (TypeError, ValueError):
                continue
        return total

    @staticmethod
    def _best_depth_price(rows: Any, side: str) -> float:
        if isinstance(rows, dict):
            rows = [rows]
        prices = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            price = row.get("price", row.get("rate", row.get("bp", row.get("ap", 0))))
            try:
                price = float(price or 0.0)
            except (TypeError, ValueError):
                price = 0.0
            if price > 0:
                prices.append(price)
        if not prices:
            return 0.0
        return max(prices) if side == "bid" else min(prices)

    @staticmethod
    def _depth_from_qty(bid_qty: Any, ask_qty: Any, source: str = "depth") -> Dict[str, Any]:
        try:
            bid = float(bid_qty or 0.0)
            ask = float(ask_qty or 0.0)
        except Exception:
            return {"usable": False}
        if bid <= 0 and ask <= 0:
            return {"usable": False}
        ofr = bid / ask if ask > 0 else (bid if bid > 0 else 1.0)
        return {
            "ofr": round(float(ofr), 3),
            "bid_qty": int(bid),
            "ask_qty": int(ask),
            "source": source,
            "usable": True,
        }

    def get_previous_close(self, symbol: str) -> float:
        """Fetch official closing price with persistent session caching"""
        if not hasattr(self, "_prev_close_global_cache"):
            self._prev_close_global_cache = {}
            self._prev_close_cache_time = {}
            
        if symbol in self._prev_close_global_cache:
            cache_source = getattr(self, "_prev_close_cache_source", {}).get(symbol, "")
            cache_age = time.time() - getattr(self, "_prev_close_cache_time", {}).get(symbol, 0)
            if (
                cache_age < 300
                and (
                    cache_source == "fyers_rest"
                    or symbol not in INDEX_SPOT_TOKENS
                    or not getattr(self, "fyers", None)
                )
            ):
                return self._prev_close_global_cache[symbol]

        if symbol in INDEX_SPOT_TOKENS and getattr(self, "fyers", None):
            try:
                values = self._fyers_index_quote_values(symbol)
                previous_close = self._quote_float(values, *PREVIOUS_CLOSE_KEYS)
                if previous_close > 0:
                    return self._remember_previous_close(symbol, previous_close, "fyers_rest")
            except Exception as exc:
                logger.debug(f"Fyers prev_close fetch error for {symbol}: {exc}")

        # 1. BROKER LIVE FEED (Primary Source)
        if self.is_connected and self.smart_api:
            if symbol in INDEX_SPOT_TOKENS:
                token, exchange = INDEX_SPOT_TOKENS[symbol]
                try:
                    trading_symbol = "INDEX" if symbol in INDEX_SPOT_TOKENS else symbol
                    
                    resp = self._best_effort_angel_rest_call(
                        "previousClose",
                        lambda: self.smart_api.ltpData(exchange, trading_symbol, token),
                    )
                    if isinstance(resp, dict) and resp.get('status') and resp.get('data'):
                        close_price = float(resp['data'].get('close', 0.0))
                        if close_price > 0:
                            return self._remember_previous_close(symbol, close_price, "angel_rest")
                except Exception as e:
                    logger.debug(f"AngelOne prev_close fetch error for {symbol}: {e}")

        return 0.0

    def get_latest_price(self, symbol: str) -> float:
        """Alias for convenience, attempts to find correct token"""
        if symbol in INDEX_SPOT_TOKENS:
            t, e = INDEX_SPOT_TOKENS[symbol]
            return self.get_latest_price_by_token(t, symbol, e)
        return self._ltp_cache.get(symbol, {}).get('ltp', 0.0)

    # ═══════════════════════════════════════════════════════════
    # HISTORICAL & OPTIONS
    # ═══════════════════════════════════════════════════════════

    def _angel_history_rate_limited(self) -> bool:
        """True while the AngelOne history endpoint is inside a rate-limit cooldown."""
        return time.time() < float(getattr(self, "_angel_hist_rate_limited_until", 0.0) or 0.0)

    def _note_angel_history_rate_limit(self, context: str) -> None:
        """Escalating cooldown: 10s doubling per consecutive hit, capped at 120s."""
        streak = int(getattr(self, "_angel_hist_rate_limit_streak", 0) or 0) + 1
        self._angel_hist_rate_limit_streak = streak
        cooldown = min(120.0, 10.0 * (2 ** (streak - 1)))
        self._angel_hist_rate_limited_until = time.time() + cooldown
        logger.warning(
            f"🚨 AngelOne history rate limit ({context}); cooling down {cooldown:.0f}s "
            f"(consecutive hit #{streak})"
        )

    def get_historical_candles_angel(self, token, exchange, interval, days_back=15) -> pd.DataFrame:
        """Fetch historical candles directly from AngelOne SmartApi"""
        if not self.is_connected or not self.smart_api:
            return pd.DataFrame()
        if self._angel_history_rate_limited():
            logger.debug(f"AngelOne history cooldown active; skipping fetch for {token}")
            return pd.DataFrame()

        try:
            # Map interval to AngelOne format
            api_interval = {
                "1min": "ONE_MINUTE",
                "5min": "FIVE_MINUTE",
                "15min": "FIFTEEN_MINUTE"
            }.get(interval, "FIVE_MINUTE")
            
            to_date = datetime.now().strftime("%Y-%m-%d %H:%M")
            from_date = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d 09:15")
            
            params = {
                "exchange": exchange,
                "symboltoken": token,
                "interval": api_interval,
                "fromdate": from_date,
                "todate": to_date
            }
            
            # ── Institutional Grade: Direct Async-ready Fetch ──
            # (Removed time.sleep to eliminate 300ms bottleneck)
            resp = self.smart_api.getCandleData(params)
            
            # Check for Invalid Token error
            if not resp.get('status') and resp.get('errorCode') == 'AG8001':
                logger.warning("🔄 AngelOne History Session Expired (AG8001). Re-authenticating...")
                if self.connect():
                    resp = self.smart_api.getCandleData(params)
                else:
                    return pd.DataFrame()

            if resp.get('status') and resp.get('data'):
                df = pd.DataFrame(resp['data'], columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                df['timestamp'] = pd.to_datetime(df['timestamp'])
                df.set_index('timestamp', inplace=True)

                # Force IST Normalization (Broker → IST Naive)
                # This ensures '03:45 UTC' becomes '09:15 Naive'
                if df.index.tz is not None:
                    df.index = df.index.tz_convert('Asia/Kolkata').tz_localize(None)

                self._angel_hist_rate_limit_streak = 0
                return df
            else:
                err = resp.get('message', 'Unknown Error')
                if "exceeding access rate" in err.lower():
                    self._note_angel_history_rate_limit(f"response for {token}")
                else:
                    logger.debug(f"AngelOne Empty Response for {token}: {err}")
        except Exception as e:
            # The SDK surfaces the plain-text rate-limit body as a JSON parse
            # failure, so the rate-limit signal usually arrives via this branch.
            if "exceeding access rate" in str(e).lower():
                self._note_angel_history_rate_limit(f"exception for {token}")
            else:
                logger.error(f"❌ AngelOne History Fetch Error: {e}")
        return pd.DataFrame()

    def get_historical_candles(self, token, exchange, interval, days_back=15, instrument_name="") -> pd.DataFrame:
        """
        Unified Historical Data Fetcher — Strictly prioritized for AngelOne.
        """
        cache_dir = Path("data_store/candles")
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / f"{instrument_name}_{interval}.csv"
        df = pd.DataFrame()

        # 0. GLOBAL API CACHE CHECK (Applies to both Fyers and AngelOne)
        now = time.time()
        cache_key = (
            f"hist_api_{instrument_name}_{exchange}_{token}_{interval}_{int(days_back)}"
        )
        if not hasattr(self, "_api_hist_cache"): self._api_hist_cache = {}
        if not hasattr(self, "_last_api_hist_fetch"): self._last_api_hist_fetch = {}
        
        from config.settings import get_settings
        history_ttl = float(getattr(get_settings(), "history_cache_ttl_seconds", 60.0))
        last_fetch = self._last_api_hist_fetch.get(cache_key, 0)
        if isinstance(last_fetch, datetime):
            last_fetch = last_fetch.timestamp()
        elif not isinstance(last_fetch, (int, float)):
            last_fetch = 0
        if now - last_fetch < history_ttl:
            cached_df = self._api_hist_cache.get(cache_key)
            if cached_df is not None:
                logger.debug(f"Using cached API history for {cache_key}")
                return cached_df

        # Historical option candles are heavily reused during backtests. Prefer the
        # local per-symbol CSV cache before touching broker APIs to avoid rate-limit
        # stalls and session-token noise for expired option contracts.
        is_option_symbol = bool(instrument_name) and str(instrument_name).upper().endswith(("CE", "PE"))
        if is_option_symbol and cache_file.exists():
            try:
                cached_df = get_option_history_store().load(cache_file)
                if not cached_df.empty:
                    self._api_hist_cache[cache_key] = cached_df
                    self._last_api_hist_fetch[cache_key] = now
                    logger.info(f"📦 Using Local Option History Cache for {instrument_name}")
                    return cached_df
            except Exception as exc:
                logger.debug(f"Option history cache read failed for {instrument_name}: {exc}")

        # 1. PRIMARY SOURCE FOR INDICES: FYERS (To get Volume which AngelOne lacks)
        if getattr(self, 'fyers', None) and instrument_name in FYERS_INDEX_SYMBOLS:
            try:
                fyers_symbol = FYERS_INDEX_SYMBOLS.get(instrument_name)
                
                if fyers_symbol:
                    logger.info(f"Prioritizing Fyers history for {fyers_symbol} to get volume...")
                    res = "1" if interval == "1min" else ("5" if interval == "5min" else "15")
                    range_from = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
                    range_to = datetime.now().strftime("%Y-%m-%d")
                    
                    data = {
                        "symbol": fyers_symbol,
                        "resolution": res,
                        "date_format": "1",
                        "range_from": range_from,
                        "range_to": range_to,
                        "cont_flag": "1"
                    }
                    response = self.fyers.history(data=data)
                    if response and response.get('s') == 'ok':
                        candles = response.get('candles', [])
                        if candles:
                            fyers_df = pd.DataFrame(candles, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                            fyers_df['timestamp'] = pd.to_datetime(fyers_df['timestamp'], unit='s')
                            fyers_df['timestamp'] = fyers_df['timestamp'].dt.tz_localize('UTC').dt.tz_convert('Asia/Kolkata').dt.tz_localize(None)
                            fyers_df.set_index('timestamp', inplace=True)
                            
                            if not fyers_df.empty:
                                df = fyers_df
                                df = self._save_and_merge_candles(df, cache_file)
                                # Cache update moved to bottom
            except Exception as e:
                logger.warning(f"⚠️ Fyers history fetch failed for {instrument_name}: {e}")

        # 2. FALLBACK/PRIMARY FOR OTHERS: BROKER (AngelOne)
        if df.empty and self.is_connected and self.smart_api and token:
            try:
                df = self.get_historical_candles_angel(token, exchange, interval, days_back)
                if not df.empty:
                    df = self._save_and_merge_candles(df, cache_file)
                    # Cache update moved to bottom
            except Exception as e:
                logger.warning(f"⚠️ AngelOne fetch failed for {instrument_name}: {e}")

        # 3. FALLBACK FOR OTHERS: FYERS (If AngelOne fails)
        if df.empty and getattr(self, 'fyers', None):
            fyers_symbol = FYERS_INDEX_SYMBOLS.get(instrument_name)
            
            if fyers_symbol:
                try:
                    logger.info(f"Fetching Fyers history for {fyers_symbol}...")
                    # Map interval to Fyers resolution
                    res = "1" if interval == "1min" else ("5" if interval == "5min" else "15")
                    
                    range_from = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
                    range_to = datetime.now().strftime("%Y-%m-%d")
                    
                    data = {
                        "symbol": fyers_symbol,
                        "resolution": res,
                        "date_format": "1",
                        "range_from": range_from,
                        "range_to": range_to,
                        "cont_flag": "1"
                    }
                    response = self.fyers.history(data=data)
                    if response and response.get('s') == 'ok':
                        candles = response.get('candles', [])
                        if candles:
                            fyers_df = pd.DataFrame(candles, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                            fyers_df['timestamp'] = pd.to_datetime(fyers_df['timestamp'], unit='s')
                            fyers_df['timestamp'] = fyers_df['timestamp'].dt.tz_localize('UTC').dt.tz_convert('Asia/Kolkata').dt.tz_localize(None)
                            fyers_df.set_index('timestamp', inplace=True)
                            
                            if not fyers_df.empty:
                                df = fyers_df
                                df = self._save_and_merge_candles(df, cache_file)
                except Exception as e:
                    logger.warning(f"⚠️ Fyers history fetch failed for {instrument_name}: {e}")

        # 2. SECONDARY: LOCAL CACHE (If Broker fails or Market is closed)
        if df.empty and cache_file.exists():
            try:
                cached_df = pd.read_csv(cache_file, index_col=0, parse_dates=True)
                if not cached_df.empty:
                    df = cached_df
                    logger.info(f"📦 Using Local Cache for {instrument_name}")
            except Exception as exc:
                logger.debug(f"Local candle cache read failed for {instrument_name}: {exc}")

        # 3. TERTIARY: YAHOO FALLBACK
        # If no data or data is from a previous day during a weekday, fallback to Yahoo
        today = datetime.now().date()
        is_stale = False
        if not df.empty:
            last_timestamp = df.index[-1]
            last_date = last_timestamp.date()
            if last_date < today and self.is_market_open():
                is_stale = True
                logger.info(f"📆 Cache data for {instrument_name} is stale (Last: {last_date}). Triggering Yahoo fallback.")
            elif self.is_market_open() and last_date == today:
                now = datetime.now()
                try:
                    int_mins = int(interval.replace('min', ''))
                except (AttributeError, TypeError, ValueError):
                    int_mins = 5
                threshold_minutes = max(15, 3 * int_mins)
                if (now - last_timestamp).total_seconds() / 60 > threshold_minutes:
                    is_stale = True
                    logger.info(f"🕒 Cache data for {instrument_name} is stale (Last: {last_timestamp}). Triggering Yahoo fallback.")
                
        if df.empty or is_stale:
            ticker = YAHOO_TICKERS.get(instrument_name, "")
            if ticker:
                try:
                    self._yahoo_request_count += 1
                    logger.info(f"📡 Falling back to Yahoo for {instrument_name}...")
                    yahoo_df = yf.Ticker(ticker).history(period=f"{days_back}d", interval=YAHOO_INTERVALS.get(interval, "5m"))
                    if not yahoo_df.empty:
                        self._yahoo_last_used_at = time.time()
                        df = yahoo_df
                except Exception as e:
                    logger.error(f"❌ Yahoo Fetch Error for {instrument_name}: {e}")

        if df.empty:
            empty_df = pd.DataFrame()
            self._api_hist_cache[cache_key] = empty_df
            self._last_api_hist_fetch[cache_key] = now
            return empty_df
        
        # --- NORMALIZATION & SESSION FILTERING ---
        df.columns = [c.lower() for c in df.columns]
        if 'adj close' in df.columns: df['close'] = df['adj close']
        
        if df.index.tz is not None:
            df.index = df.index.tz_convert('Asia/Kolkata').tz_localize(None)
        else:
            if instrument_name in YAHOO_TICKERS and df.index[-1].hour < 6:
                df.index = df.index.tz_localize('UTC').tz_convert('Asia/Kolkata').tz_localize(None)

        df = df.between_time('09:15', '15:30')
        for col in ['open', 'high', 'low', 'close', 'volume']:
            if col not in df.columns: df[col] = df['close'] if col != 'volume' else 0
            
        final_df = df[['open', 'high', 'low', 'close', 'volume']].copy()
        
        # ALWAYS update cache timestamp to prevent rate-limit spam loops on failures
        self._api_hist_cache[cache_key] = final_df
        self._last_api_hist_fetch[cache_key] = now
        
        return final_df

    def _save_and_merge_candles(self, df: pd.DataFrame, cache_file: Path):
        """Merge new candles with existing cache to prevent data loss and fill gaps"""
        if df.empty:
            return
            
        try:
            if cache_file.exists():
                existing_df = pd.read_csv(cache_file, index_col=0, parse_dates=True)
                if not existing_df.empty:
                    for frame in (existing_df, df):
                        frame.columns = [c.lower() for c in frame.columns]
                    existing_df = existing_df[~existing_df.index.duplicated(keep='last')].sort_index()
                    df = df[~df.index.duplicated(keep='last')].sort_index()
                    if "volume" in existing_df.columns and "volume" in df.columns:
                        existing_vol = pd.to_numeric(existing_df["volume"], errors="coerce").fillna(0)
                        new_vol = pd.to_numeric(df["volume"], errors="coerce").fillna(0)
                        shared_idx = df.index.intersection(existing_df.index)
                        preserve_idx = [
                            idx for idx in shared_idx
                            if new_vol.get(idx, 0) <= 0 and existing_vol.get(idx, 0) > 0
                        ]
                        if preserve_idx:
                            df.loc[preserve_idx, "volume"] = existing_vol.loc[preserve_idx]
                    # Drop duplicates keeping the LATEST data (important for updates!)
                    merged_df = pd.concat([existing_df, df])
                    merged_df = merged_df[~merged_df.index.duplicated(keep='last')]
                    merged_df.sort_index(inplace=True)
                    merged_df = merged_df.tail(5000)
                    compare_cols = [c for c in ['open', 'high', 'low', 'close', 'volume'] if c in existing_df.columns and c in merged_df.columns]
                    if (
                        compare_cols
                        and len(merged_df) == len(existing_df)
                        and len(merged_df.index.difference(existing_df.index)) == 0
                        and existing_df[compare_cols].tail(3).equals(merged_df[compare_cols].tail(3))
                    ):
                        logger.debug(f"Skipping unchanged candle cache write for {cache_file.name}")
                        return existing_df
                    df = merged_df
                    
            # Save the full merged dataset
            df.to_csv(cache_file)
            get_option_history_store().invalidate(cache_file)
            logger.info(f"💾 Merged and saved data to {cache_file.name} (Total candles: {len(df)})")
        except Exception as e:
            logger.error(f"❌ Failed to merge/save candles to {cache_file.name}: {e}")
            # Fallback to just saving the new data
            try:
                df.to_csv(cache_file)
                get_option_history_store().invalidate(cache_file)
            except Exception as fallback_exc:
                logger.warning(f"Fallback candle save failed for {cache_file.name}: {fallback_exc}")
        return df

    def get_cached_option_chain(
        self,
        index_symbol: str,
        max_age_seconds: float = 10.0,
    ) -> Optional[pd.DataFrame]:
        """Return only a fresh, active-expiry Fyers chain without network I/O."""
        cache_key = f"opt_chain_{index_symbol}"
        cached = (getattr(self, "_opt_chain_cache", {}) or {}).get(cache_key)
        meta = (getattr(self, "_option_chain_meta", {}) or {}).get(index_symbol, {})
        if cached is None or not isinstance(meta, dict):
            return None
        if str(meta.get("source") or "").lower() != "fyers" or not bool(meta.get("expiry_aligned", False)):
            return None
        fetched_at = float(meta.get("fetched_at") or 0.0)
        age = time.time() - fetched_at if fetched_at > 0 else float("inf")
        if not (0.0 <= age <= max(0.0, float(max_age_seconds or 0.0))):
            return None
        return cached

    def get_option_chain(self, index_symbol: str, exchange: str = "NFO") -> Optional[pd.DataFrame]:
        """Fetch real options data using Fyers (Primary) or AngelOne (Fallback)"""
        now = time.time()
        cache_key = f"opt_chain_{index_symbol}"
        if not hasattr(self, "_opt_chain_cache"): self._opt_chain_cache = {}
        if not hasattr(self, "_last_opt_chain_fetch"): self._last_opt_chain_fetch = {}
        if not hasattr(self, "_last_opt_chain_error"): self._last_opt_chain_error = {}
        
        from config.settings import get_settings
        chain_ttl = max(90.0, float(getattr(get_settings(), "intelligence_cache_ttl_seconds", 90.0)))
        error_cooldown = max(chain_ttl, 180.0)

        # Cache option chain aggressively during live scans to prevent API spam.
        last_fetch = self._last_opt_chain_fetch.get(cache_key, 0)
        if now - last_fetch < chain_ttl:
            cached_data = self._opt_chain_cache.get(cache_key)
            if cached_data is not None:
                logger.debug(f"Using cached option chain for {index_symbol}")
                return cached_data
            else:
                # In-flight fetch by another thread, return empty to prevent queueing
                return pd.DataFrame()

        last_error = self._last_opt_chain_error.get(cache_key, 0)
        if now - last_error < error_cooldown:
            cached_data = self._opt_chain_cache.get(cache_key)
            if cached_data is not None:
                logger.info(f"Using stale option chain for {index_symbol} during error cooldown")
                return cached_data
            return pd.DataFrame()

        # Mark as in-flight immediately to prevent concurrent threads from piling up
        self._last_opt_chain_fetch[cache_key] = now

        # ══ TRY FYERS FIRST ══
        if self.fyers:
            fyers_symbol = FYERS_INDEX_SYMBOLS.get(index_symbol)
            
            if fyers_symbol:
                try:
                    logger.info(f"Fetching Fyers option chain for {fyers_symbol}...")
                    
                    # Try to center around ATM strike if we have spot price
                    spot = self.get_latest_price(index_symbol)
                    data = {"symbol": fyers_symbol, "strikecount": 30, "timestamp": ""}
                    
                    if spot > 0:
                        strike_interval = INDEX_STRIKE_INTERVALS.get(index_symbol, 50)
                        atm_strike = round(spot / strike_interval) * strike_interval
                        logger.info(f"Fyers option chain requested for {fyers_symbol} (ATM: {atm_strike})")
                    
                    response = self.fyers.optionchain(data=data)
                    logger.info(f"Fyers optionchain response status: {response.get('s') if response else 'None'}")

                    # Align intelligence with the exact active expiry used by execution.
                    if response and response.get("s") == "ok":
                        from engine.expiry_manager import expiry_manager
                        if not expiry_manager.expiries:
                            expiry_manager.pre_market_check()
                        expiry_key = (
                            "active_monthly"
                            if index_symbol in {"BANKNIFTY", "MIDCPNIFTY"}
                            else "active_weekly"
                        )
                        active_label = (
                            expiry_manager.expiries.get(index_symbol, {}).get(expiry_key)
                        )
                        expiry_rows = response.get("data", {}).get("expiryData", []) or []
                        active_timestamp = ""
                        active_date = ""
                        if active_label:
                            try:
                                active_date = datetime.strptime(
                                    active_label, "%d%b%Y"
                                ).strftime("%d-%m-%Y")
                            except ValueError:
                                active_date = ""
                        for expiry_row in expiry_rows:
                            if str(expiry_row.get("date") or "") == active_date:
                                active_timestamp = str(expiry_row.get("expiry") or "")
                                break
                        if active_timestamp:
                            data["timestamp"] = active_timestamp
                            response = self.fyers.optionchain(data=data)
                            logger.info(
                                f"Fyers option chain aligned to active expiry "
                                f"{active_label} for {index_symbol}."
                            )
                    
                    if response and response.get('s') == 'ok':
                        chain_data = response.get('data', {}).get('optionsChain', [])
                        logger.info(f"Fyers optionsChain length: {len(chain_data)}")
                        df = pd.DataFrame(chain_data)
                        # Filter out index row
                        df = df[df['strike_price'] > 0]
                        
                        ce_df = df[df['option_type'] == 'CE'].rename(columns={
                            'oi': 'call_oi', 'volume': 'call_volume', 'strike_price': 'strike',
                            'oich': 'call_oi_change', 'ltp': 'call_ltp',
                            'symbol': 'call_symbol', 'fyToken': 'call_token',
                        })
                        pe_df = df[df['option_type'] == 'PE'].rename(columns={
                            'oi': 'put_oi', 'volume': 'put_volume', 'strike_price': 'strike',
                            'oich': 'put_oi_change', 'ltp': 'put_ltp',
                            'symbol': 'put_symbol', 'fyToken': 'put_token',
                        })
                        
                        # Merge on strike
                        ce_cols = [
                            col for col in (
                                'strike', 'call_oi', 'call_volume', 'call_oi_change',
                                'call_ltp', 'call_symbol', 'call_token',
                            ) if col in ce_df.columns
                        ]
                        pe_cols = [
                            col for col in (
                                'strike', 'put_oi', 'put_volume', 'put_oi_change',
                                'put_ltp', 'put_symbol', 'put_token',
                            ) if col in pe_df.columns
                        ]
                        chain = pd.merge(ce_df[ce_cols], pe_df[pe_cols], on='strike', how='outer')
                        chain = chain.fillna(0)
                        chain = self._apply_option_oi_snapshot_deltas(
                            index_symbol,
                            chain,
                            provider="fyers",
                            expiry=str(active_label or "") if "active_label" in locals() else "",
                        )
                        self._option_chain_meta[index_symbol] = {
                            "source": "fyers",
                            "fetched_at": now,
                            "strike_count": int(len(chain)),
                            "fallback": False,
                            "active_expiry": active_label if "active_label" in locals() else "",
                            "expiry_aligned": bool(active_timestamp) if "active_timestamp" in locals() else False,
                            "oi_change_source": str(chain["oi_change_source"].iloc[0]) if not chain.empty else "none",
                        }
                        
                        logger.info(f"✅ Fyers Option Chain fetched successfully for {index_symbol}")
                        self._opt_chain_cache[cache_key] = chain
                        self._last_opt_chain_fetch[cache_key] = now
                        self._fyers_chain_failure_streak = 0
                        return chain
                    self._last_opt_chain_fetch[cache_key] = now
                    self._last_opt_chain_error[cache_key] = now
                    self._maybe_self_heal_fyers(f"optionchain error status for {index_symbol}")
                except Exception as e:
                    self._last_opt_chain_fetch[cache_key] = now
                    self._last_opt_chain_error[cache_key] = now
                    self._maybe_self_heal_fyers(f"optionchain exception for {index_symbol}")
                    logger.warning(f"⚠️ Fyers Option Chain fetch failed: {e}. Falling back to AngelOne...")

        # ══ FALLBACK TO ANGELONE (Single ATM Row - Like older versions) ══
        spot = self.get_latest_price(index_symbol)
        if spot <= 0:
            self._last_opt_chain_error[cache_key] = now
            return pd.DataFrame()
        
        interval = INDEX_STRIKE_INTERVALS.get(index_symbol, 50)
        atm = round(spot / interval) * interval
        
        from engine.expiry_manager import expiry_manager
        
        # Get tokens for ATM CE and PE using the same active rollover logic as trades.
        ce_token_dict = self.get_option_token(index_symbol, atm, "CE")
        pe_token_dict = self.get_option_token(index_symbol, atm, "PE")
        
        if not ce_token_dict or not pe_token_dict:
            logger.warning(f"Tokens not found for ATM {atm} on {index_symbol}.")
            self._last_opt_chain_error[cache_key] = now
            return pd.DataFrame()
            
        ce_token = ce_token_dict['token']
        pe_token = pe_token_dict['token']
        
        ce_ltp, ce_oi, ce_vol = 0.0, 100000, 0
        pe_ltp, pe_oi, pe_vol = 0.0, 80000, 0
        
        if self.is_connected and self.smart_api:
            try:
                # 1. Check WebSocket Cache First
                ce_cached = self._ws_cache.get(ce_token, {})
                pe_cached = self._ws_cache.get(pe_token, {})
                
                ce_ltp = ce_cached.get('ltp', 0.0)
                ce_oi = ce_cached.get('oi', 0)
                ce_vol = ce_cached.get('vol', 0)
                
                pe_ltp = pe_cached.get('ltp', 0.0)
                pe_oi = pe_cached.get('oi', 0)
                pe_vol = pe_cached.get('vol', 0)
                
                # 2. If Cache is empty, use REST Fallback
                if ce_ltp <= 0 or pe_ltp <= 0:
                    logger.debug(f"ATM Cache empty for {index_symbol}, fetching via REST")
                    exch_seg = ce_token_dict.get('exch_seg', 'NFO')
                    exch_type = 4 if exch_seg == 'BFO' else 2
                    self.subscribe_tokens([ce_token, pe_token], exchange_type=exch_type)
                    
                    market_resp = self._best_effort_angel_rest_call(
                        "optionChainMarketData",
                        lambda: self.smart_api.getMarketData(
                            mode="FULL",
                            exchangeTokens={exch_seg: [ce_token, pe_token]},
                        ),
                    )
                    if isinstance(market_resp, dict) and (
                        not market_resp.get('status') and market_resp.get('errorCode') == 'AG8001'
                    ):
                        logger.warning("🔄 Option Chain Session Expired (AG8001); deferring refresh.")

                    fetched_rows = (
                        market_resp.get('data', {}).get('fetched', [])
                        if isinstance(market_resp, dict) and market_resp.get('status')
                        else []
                    )

                    def _row_token(row):
                        return str(
                            row.get('symbolToken')
                            or row.get('symboltoken')
                            or row.get('token')
                            or ''
                        )

                    by_token = {_row_token(row): row for row in fetched_rows if isinstance(row, dict)}
                    ce_fetched = by_token.get(str(ce_token), fetched_rows[0] if fetched_rows else {})
                    ce_ltp = float(ce_fetched.get('ltp', 0.0))
                    ce_oi = int(ce_fetched.get('opnInterest', 100000))
                    ce_vol = int(ce_fetched.get('totTrdQty', 0))

                    pe_fetched = by_token.get(
                        str(pe_token),
                        fetched_rows[1] if len(fetched_rows) > 1 else {},
                    )
                    pe_ltp = float(pe_fetched.get('ltp', 0.0))
                    pe_oi = int(pe_fetched.get('opnInterest', 80000))
                    pe_vol = int(pe_fetched.get('totTrdQty', 0))
                
            except Exception as e:
                logger.error(f"❌ Failed to fetch option Market Data: {e}")
                self._last_opt_chain_error[cache_key] = now
                if 'ce_ltp' not in locals() or ce_ltp <= 0:
                    ce_ltp, ce_oi, ce_vol = 0.0, 100000, 0
                    pe_ltp, pe_oi, pe_vol = 0.0, 80000, 0
                
        # Build chain with 1 ATM row
        rows = [{
            'strike': atm, 
            'call_oi': ce_oi, 
            'call_oi_change': 0,
            'call_volume': ce_vol,
            'call_ltp': ce_ltp,
            'put_oi': pe_oi, 
            'put_oi_change': 0,
            'put_volume': pe_vol,
            'put_ltp': pe_ltp,
            'call_iv': 15.0, 'put_iv': 15.0, 
            'call_gamma': 0.0004, 'put_gamma': 0.0004,
            'call_theta': -10.0, 'put_theta': -10.0, 
            'is_atm': True
        }]
        df = self._apply_option_oi_snapshot_deltas(
            index_symbol,
            pd.DataFrame(rows),
            provider="angelone",
            expiry=str(
                ce_token_dict.get("expiry")
                or ce_token_dict.get("expiry_date")
                or ce_token_dict.get("symbol", "")
            ),
        )
        self._option_chain_meta[index_symbol] = {
            "source": "angelone_atm_fallback",
            "fetched_at": now,
            "strike_count": 1,
            "fallback": True,
            "oi_change_source": str(df["oi_change_source"].iloc[0]) if not df.empty else "none",
        }
        
        # Cache the result so we don't REST-fetch again within 60s
        self._opt_chain_cache[cache_key] = df
        self._last_opt_chain_fetch[cache_key] = now
        
        logger.debug(f"✅ AngelOne Option Chain built for {index_symbol} (ATM={atm})")
        return df

    def _option_oi_snapshot_guard(self) -> Any:
        lock = getattr(self, "_option_oi_snapshot_lock", None)
        if lock is None:
            lock = self._option_oi_snapshot_file_lock
            self._option_oi_snapshot_lock = lock
        return lock

    def _load_option_oi_snapshots(self) -> Dict[str, Any]:
        with self._option_oi_snapshot_guard():
            snapshots = getattr(self, "_option_oi_snapshots", None)
            if isinstance(snapshots, dict):
                return snapshots
            path = Path("data_store/option_oi_snapshots.json")
            try:
                snapshots = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            except Exception as exc:
                logger.warning(f"Option OI snapshot history could not be loaded: {exc}")
                snapshots = {}
            self._option_oi_snapshots = snapshots if isinstance(snapshots, dict) else {}
            return self._option_oi_snapshots

    def _save_option_oi_snapshots(self) -> None:
        with self._option_oi_snapshot_guard():
            path = Path("data_store/option_oi_snapshots.json")
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_name(
                f"{path.name}.{threading.get_ident()}.{time.time_ns()}.tmp"
            )
            try:
                temp.write_text(
                    json.dumps(self._option_oi_snapshots, separators=(",", ":")),
                    encoding="utf-8",
                )
                # Windows raises PermissionError (WinError 32) on os.replace when a
                # reader (another thread or a concurrent process such as an audit/
                # replay run) still holds the destination open. Retry with capped
                # exponential backoff so the brief lock window is absorbed rather
                # than dropping the snapshot tick. Non-order-path.
                attempts = 6
                for attempt in range(attempts):
                    try:
                        temp.replace(path)
                        break
                    except OSError:
                        if attempt == attempts - 1:
                            raise
                        time.sleep(min(0.4, 0.05 * (2 ** attempt)))
            except Exception as exc:
                logger.warning(f"Option OI snapshot history could not be saved: {exc}")
                try:
                    temp.unlink(missing_ok=True)
                except Exception as cleanup_exc:
                    logger.debug(f"Option OI temp cleanup failed for {temp}: {cleanup_exc}")

    def _apply_option_oi_snapshot_deltas(
        self,
        index_symbol: str,
        chain: pd.DataFrame,
        provider: str,
        expiry: str = "",
    ) -> pd.DataFrame:
        with self._option_oi_snapshot_guard():
            return self._apply_option_oi_snapshot_deltas_locked(
                index_symbol,
                chain,
                provider,
                expiry=expiry,
            )

    def _apply_option_oi_snapshot_deltas_locked(
        self,
        index_symbol: str,
        chain: pd.DataFrame,
        provider: str,
        expiry: str = "",
    ) -> pd.DataFrame:
        """Use provider OI-change when real; otherwise derive it from snapshots."""
        if chain is None or chain.empty:
            return chain
        normalized = chain.copy()
        for column in ("strike", "call_oi", "put_oi", "call_oi_change", "put_oi_change"):
            if column not in normalized.columns:
                normalized[column] = 0
            normalized[column] = pd.to_numeric(normalized[column], errors="coerce").fillna(0)

        reported_abs = float(
            normalized["call_oi_change"].abs().sum()
            + normalized["put_oi_change"].abs().sum()
        )
        session_key = datetime.now().date().isoformat()
        captured_now = time.time()
        # Key by instrument+expiry only (no session): the most recent snapshot is
        # the baseline for the next poll regardless of session boundary. OI is
        # cumulative and carries overnight, so the first poll of a new session
        # deltas against the prior session's close (day-over-day OI change)
        # instead of producing a neutral warmup. A staleness window guards
        # against deltas across long downtime (system off > a weekend/holiday).
        snapshot_key = f"{str(index_symbol).upper()}|{str(expiry or 'ACTIVE').upper()}"
        snapshots = self._load_option_oi_snapshots()
        previous = snapshots.get(snapshot_key, {})
        previous_rows = previous.get("rows", {}) if isinstance(previous, dict) else {}
        previous_session = str(previous.get("session", "")) if isinstance(previous, dict) else ""
        previous_captured = (
            float(previous.get("captured_at", 0.0) or 0.0) if isinstance(previous, dict) else 0.0
        )
        max_age = float(getattr(self, "_oi_snapshot_max_age_seconds", 4 * 86400))
        snapshot_fresh = bool(previous_rows) and (captured_now - previous_captured) <= max_age

        if reported_abs > 0:
            source = f"{provider}_reported"
        elif snapshot_fresh:
            call_changes = []
            put_changes = []
            for _, row in normalized.iterrows():
                strike_key = str(float(row["strike"]))
                prior = previous_rows.get(strike_key, {})
                call_changes.append(float(row["call_oi"]) - float(prior.get("call_oi", row["call_oi"])))
                put_changes.append(float(row["put_oi"]) - float(prior.get("put_oi", row["put_oi"])))
            normalized["call_oi_change"] = call_changes
            normalized["put_oi_change"] = put_changes
            if previous_session and previous_session != session_key:
                source = f"{provider}_overnight_delta"
            else:
                source = f"{provider}_snapshot_delta"
        else:
            normalized["call_oi_change"] = 0
            normalized["put_oi_change"] = 0
            source = f"{provider}_snapshot_warmup"

        normalized["oi_change_source"] = source
        snapshots[snapshot_key] = {
            "provider": provider,
            "expiry": expiry,
            "session": session_key,
            "captured_at": captured_now,
            "rows": {
                str(float(row["strike"])): {
                    "call_oi": float(row["call_oi"]),
                    "put_oi": float(row["put_oi"]),
                }
                for _, row in normalized.iterrows()
            },
        }
        self._option_oi_snapshots = snapshots
        self._save_option_oi_snapshots()
        normalized.attrs["oi_change_source"] = source
        return normalized

    def fetch_instruments(self):
        from engine.expiry_manager import expiry_manager
        return expiry_manager.pre_market_check()

    def get_option_token(self, name: str, strike: float, option_type: str, trade_date: Optional[date] = None, now_dt=None) -> Optional[Dict]:
        from engine.expiry_manager import expiry_manager

        # Expiry Selection Logic:
        # BANKNIFTY/MIDCPNIFTY: Monthly (active_monthly handles T-2 rollover)
        # NIFTY/SENSEX: Always use Weekly (active_weekly handles 2-day rollover)
        if name in {"BANKNIFTY", "MIDCPNIFTY"}:
            expiry_type = "active_monthly"
        else:
            expiry_type = "active_weekly"

        if trade_date is not None:
            # now_dt (full timestamp) lets expiry-day gamma mode apply its intraday cutoff.
            dated = expiry_manager.get_token_for_date(name, strike, option_type, trade_date, expiry_type, now_dt=now_dt)
            if dated:
                return dated

        return expiry_manager.get_token(name, strike, option_type, expiry_type)
