"""Sanitized AngelOne read-only market-data fallback.

Spot indices are computed values and AngelOne reports zero traded volume for them,
so index quotes remain price-only. Option market data is accepted only after the
active Kite contract identity is independently resolved to AngelOne's own token.
"""

from __future__ import annotations

import logging
import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import pyotp
import requests
from loguru import logger


ANGELONE_INDEX_TARGETS = {
    "NIFTY": ("NSE", "NIFTY", "99926000"),
    "BANKNIFTY": ("NSE", "BANKNIFTY", "99926009"),
    "MIDCPNIFTY": ("NSE", "MIDCPNIFTY", "99926074"),
    "SENSEX": ("BSE", "SENSEX", "99919000"),
}

ANGELONE_SCRIP_MASTER_URL = (
    "https://margincalculator.angelbroking.com/"
    "OpenAPI_File/files/OpenAPIScripMaster.json"
)
ANGELONE_OPTION_ROOTS = tuple(ANGELONE_INDEX_TARGETS)


class AngelOneIndexQuoteSource:
    """Small, fail-closed SmartAPI REST client for index quote redundancy."""

    def __init__(
        self,
        *,
        api_key: str = "",
        client_id: str = "",
        password: str = "",
        totp_secret: str = "",
        timeout_seconds: float = 5.0,
    ) -> None:
        self._api_key = str(api_key or "").strip()
        self._client_id = str(client_id or "").strip()
        self._password = str(password or "").strip()
        self._totp_secret = str(totp_secret or "").strip()
        self._timeout_seconds = max(1.0, min(10.0, float(timeout_seconds or 5.0)))
        self._api: Any = None
        self._lock = threading.RLock()
        self.connected = False
        self.last_success_at = 0.0
        self.last_error = ""
        self._retry_after = 0.0
        self._option_master_lock = threading.RLock()
        self._option_master_date = ""
        self._option_master_by_symbol: Dict[Tuple[str, str], Dict[str, str]] = {}
        self._option_master_cache_path = (
            Path(__file__).resolve().parents[1]
            / "data_store"
            / "cache"
            / "angelone_option_master.json"
        )

    @property
    def configured(self) -> bool:
        return all(
            (self._api_key, self._client_id, self._password, self._totp_secret)
        )

    @staticmethod
    def _load_sdk():
        # SmartAPI logs complete request headers on failures, including X-PrivateKey.
        # Silence its logger before constructing the client; this application reports
        # only sanitized source status through loguru.
        import SmartApi.smartConnect as smart_module
        from SmartApi import SmartConnect

        smart_module.logger.setLevel(logging.CRITICAL)
        for handler in list(getattr(smart_module.logger, "handlers", ()) or ()):
            handler.setLevel(logging.CRITICAL)
        return SmartConnect

    def connect(self, *, force: bool = False) -> bool:
        if not self.configured:
            self.connected = False
            self.last_error = "credentials_not_configured"
            return False
        with self._lock:
            if not force and self.connected and self._api is not None:
                return True
            if not force and time.time() < self._retry_after:
                return False
            try:
                smart_connect = self._load_sdk()
                api = smart_connect(
                    api_key=self._api_key,
                    timeout=self._timeout_seconds,
                )
                response = api.generateSession(
                    self._client_id,
                    self._password,
                    pyotp.TOTP(self._totp_secret).now(),
                )
                if not isinstance(response, dict) or not response.get("status"):
                    self._api = None
                    self.connected = False
                    self.last_error = "authentication_rejected"
                    self._retry_after = time.time() + 30.0
                    return False
                self._api = api
                self.connected = True
                self.last_error = ""
                self._retry_after = 0.0
                logger.info("AngelOne read-only market-data fallback connected.")
                return True
            except Exception:
                # SDK exceptions may embed request headers. Never interpolate them.
                self._api = None
                self.connected = False
                self.last_error = "connection_failed"
                self._retry_after = time.time() + 30.0
                logger.warning("AngelOne index quote fallback connection failed.")
                return False

    def prewarm(self) -> bool:
        """Authenticate and load today's filtered option master off the scan loop."""
        if not self.connect():
            return False
        return bool(self._load_option_master())

    @staticmethod
    def _master_day() -> str:
        return datetime.now().date().isoformat()

    @staticmethod
    def _normalize_master_rows(rows: Iterable[Any]) -> Dict[Tuple[str, str], Dict[str, str]]:
        contracts: Dict[Tuple[str, str], Dict[str, str]] = {}
        roots = tuple(f"{name}" for name in ANGELONE_OPTION_ROOTS)
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            exchange = str(raw.get("exch_seg") or raw.get("exchange") or "").upper()
            symbol = str(raw.get("symbol") or raw.get("tradingsymbol") or "").upper()
            token = str(raw.get("token") or raw.get("symboltoken") or raw.get("symbolToken") or "")
            if exchange not in {"NFO", "BFO"} or not token:
                continue
            if not symbol.endswith(("CE", "PE")) or not symbol.startswith(roots):
                continue
            contracts[(exchange, symbol)] = {
                "exchange": exchange,
                "symbol": symbol,
                "token": token,
            }
        return contracts

    def _read_option_master_cache(self, day: str) -> Dict[Tuple[str, str], Dict[str, str]]:
        path = self._option_master_cache_path
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if str(payload.get("date") or "") != day:
                return {}
            return self._normalize_master_rows(payload.get("contracts") or [])
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return {}

    def _write_option_master_cache(
        self,
        day: str,
        contracts: Dict[Tuple[str, str], Dict[str, str]],
    ) -> None:
        path = self._option_master_cache_path
        payload = {
            "date": day,
            "source": ANGELONE_SCRIP_MASTER_URL,
            "contracts": list(contracts.values()),
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(path.suffix + ".tmp")
            temp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
            temp.replace(path)
        except OSError:
            logger.warning("AngelOne filtered option-master cache could not be persisted.")

    def _load_option_master(
        self,
        *,
        force: bool = False,
    ) -> Dict[Tuple[str, str], Dict[str, str]]:
        """Load today's Angel tokens once; stale masters never resolve live contracts."""
        day = self._master_day()
        with self._option_master_lock:
            if (
                not force
                and self._option_master_date == day
                and self._option_master_by_symbol
            ):
                return self._option_master_by_symbol
            if not force:
                cached = self._read_option_master_cache(day)
                if cached:
                    self._option_master_date = day
                    self._option_master_by_symbol = cached
                    return cached
            try:
                response = requests.get(
                    ANGELONE_SCRIP_MASTER_URL,
                    timeout=max(15.0, min(30.0, self._timeout_seconds * 4.0)),
                )
                response.raise_for_status()
                rows = response.json()
                contracts = self._normalize_master_rows(rows if isinstance(rows, list) else [])
            except Exception:
                self.last_error = "option_master_download_failed"
                logger.warning("AngelOne option instrument master download failed.")
                return {}
            if not contracts:
                self.last_error = "option_master_empty"
                return {}
            self._option_master_date = day
            self._option_master_by_symbol = contracts
            self._write_option_master_cache(day, contracts)
            return contracts

    @staticmethod
    def _number(payload: Dict[str, Any], key: str) -> float:
        try:
            return float(payload.get(key) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def get_index_quote(self, symbol: str, *, retry_auth: bool = True) -> Optional[Dict[str, Any]]:
        name = str(symbol or "").upper()
        target = ANGELONE_INDEX_TARGETS.get(name)
        if target is None:
            return None
        if not self.connected or self._api is None:
            if not self.connect():
                return None
        exchange, trading_symbol, token = target
        with self._lock:
            try:
                response = self._api.ltpData(exchange, trading_symbol, token)
            except Exception:
                self.connected = False
                self.last_error = "quote_failed"
                self._retry_after = time.time() + 10.0
                return None
            if not isinstance(response, dict) or not response.get("status"):
                code = str(response.get("errorcode") or response.get("errorCode") or "")
                if retry_auth and code == "AG8001":
                    self.connected = False
                    self._api = None
                    if self.connect(force=True):
                        return self.get_index_quote(name, retry_auth=False)
                self.last_error = "quote_rejected"
                return None
            row = response.get("data") or {}
            ltp = self._number(row, "ltp")
            if ltp <= 0:
                self.last_error = "non_positive_quote"
                return None
            now = time.time()
            self.last_success_at = now
            self.last_error = ""
            return {
                "ltp": ltp,
                "open": self._number(row, "open"),
                "high": self._number(row, "high"),
                "low": self._number(row, "low"),
                "previous_close": self._number(row, "close"),
                "received_at": now,
                "exchange_at": None,
                "source": "angel_rest",
                "volume_available": False,
                "volume": 0.0,
                "volume_unit": "none",
                "volume_unavailable_reason": "spot_index_is_not_traded",
            }

    @staticmethod
    def _option_identity(
        index_symbol: str,
        contract: Dict[str, Any],
        option_type: str,
    ) -> Optional[Dict[str, str]]:
        """Translate a Kite contract identity without ever reusing its token."""
        name = str(index_symbol or "").upper()
        option_type = str(option_type or "").upper()
        exchange = str(contract.get("exch_seg") or "").upper()
        kite_symbol = str(contract.get("symbol") or "").upper()
        if option_type not in {"CE", "PE"} or exchange not in {"NFO", "BFO"}:
            return None
        if exchange == "BFO" and kite_symbol.endswith(option_type):
            angel_symbol = kite_symbol
        else:
            expiry_text = str(
                contract.get("date_aware_expiry") or contract.get("expiry") or ""
            ).upper()
            try:
                expiry = datetime.strptime(expiry_text, "%d%b%Y")
                expiry_code = expiry.strftime("%d%b%y").upper()
                strike = float(contract.get("strike") or 0.0)
                if strike > 100000.0:
                    strike /= 100.0
                strike_text = str(int(round(strike)))
            except (TypeError, ValueError):
                return None
            if not name or float(strike_text) <= 0:
                return None
            angel_symbol = f"{name}{expiry_code}{strike_text}{option_type}"
        return {
            "exchange": exchange,
            "symbol": angel_symbol,
            "query": angel_symbol[:-2],
            "option_type": option_type,
        }

    @staticmethod
    def _contract_strike(contract: Dict[str, Any]) -> float:
        try:
            strike = float(contract.get("strike") or 0.0)
            return strike / 100.0 if strike > 100000.0 else strike
        except (TypeError, ValueError):
            return 0.0

    def get_option_pairs(
        self,
        index_symbol: str,
        contract_pairs: Sequence[Tuple[Dict[str, Any], Dict[str, Any]]],
        *,
        retry_auth: bool = True,
    ) -> List[Dict[str, Any]]:
        """Resolve exact Angel tokens from daily master, then quote all pairs in one call."""
        if not self.connected or self._api is None:
            if not self.connect():
                return []
        master = self._load_option_master()
        if not master:
            return []

        prepared: List[Dict[str, Any]] = []
        for call_contract, put_contract in contract_pairs:
            call_id = self._option_identity(index_symbol, call_contract, "CE")
            put_id = self._option_identity(index_symbol, put_contract, "PE")
            if not call_id or not put_id:
                continue
            if call_id["exchange"] != put_id["exchange"] or call_id["query"] != put_id["query"]:
                continue
            call_master = master.get((call_id["exchange"], call_id["symbol"]))
            put_master = master.get((put_id["exchange"], put_id["symbol"]))
            if not call_master or not put_master:
                continue
            prepared.append(
                {
                    "strike": self._contract_strike(call_contract),
                    "expiry": str(
                        call_contract.get("date_aware_expiry")
                        or call_contract.get("expiry")
                        or ""
                    ),
                    "CE": dict(call_master),
                    "PE": dict(put_master),
                }
            )
        if not prepared:
            self.last_error = "option_contract_not_found"
            return []

        exchange_tokens: Dict[str, List[str]] = {}
        for item in prepared:
            for side in ("CE", "PE"):
                identity = item[side]
                exchange_tokens.setdefault(identity["exchange"], []).append(identity["token"])
        exchange_tokens = {
            exchange: list(dict.fromkeys(tokens))
            for exchange, tokens in exchange_tokens.items()
        }
        with self._lock:
            try:
                response = None
                for attempt in range(2):
                    try:
                        response = self._api.getMarketData(
                            mode="FULL", exchangeTokens=exchange_tokens
                        )
                    except Exception:
                        response = None
                    if isinstance(response, dict) and response.get("status"):
                        break
                    if attempt == 0:
                        time.sleep(1.1)
            except Exception:
                self.last_error = "option_market_data_failed"
                return []

            if not isinstance(response, dict) or not response.get("status"):
                code = str(response.get("errorcode") or response.get("errorCode") or "")
                if retry_auth and code == "AG8001":
                    self.connected = False
                    self._api = None
                    if self.connect(force=True):
                        return self.get_option_pairs(
                            index_symbol, contract_pairs, retry_auth=False
                        )
                self.last_error = "option_market_data_rejected"
                return []

            fetched = (response.get("data") or {}).get("fetched") or []
            by_token = {
                str(row.get("symbolToken") or row.get("symboltoken") or row.get("token") or ""): row
                for row in fetched if isinstance(row, dict)
            }
            result: List[Dict[str, Any]] = []
            for item in prepared:
                quoted = {"strike": item["strike"], "expiry": item["expiry"]}
                complete = True
                for side in ("CE", "PE"):
                    identity = item[side]
                    row = by_token.get(identity["token"])
                    ltp = self._number(row, "ltp") if isinstance(row, dict) else 0.0
                    if ltp <= 0:
                        complete = False
                        break
                    quoted[side] = {
                        **identity,
                        "ltp": ltp,
                        "oi": max(0.0, self._number(row, "opnInterest")),
                        "volume": max(0.0, self._number(row, "totTrdQty")),
                    }
                if complete:
                    result.append(quoted)
            if not result:
                self.last_error = "option_quote_missing"
                return []
            self.last_success_at = time.time()
            self.last_error = ""
            return result

    def get_option_pair(
        self,
        index_symbol: str,
        call_contract: Dict[str, Any],
        put_contract: Dict[str, Any],
        *,
        retry_auth: bool = True,
    ) -> Optional[Dict[str, Dict[str, Any]]]:
        """Compatibility wrapper for callers needing one exact CE/PE pair."""
        pairs = self.get_option_pairs(
            index_symbol,
            [(call_contract, put_contract)],
            retry_auth=retry_auth,
        )
        return pairs[0] if pairs else None

    def health(self) -> Dict[str, Any]:
        age = time.time() - self.last_success_at if self.last_success_at > 0 else None
        return {
            "configured": self.configured,
            "connected": bool(self.connected),
            "last_success_age_seconds": round(age, 2) if age is not None else None,
            "last_error": self.last_error,
            "price_only": False,
            "index_price_only": True,
            "option_market_data": True,
            "index_volume_available": False,
            "option_master_date": self._option_master_date,
            "option_master_contracts": len(self._option_master_by_symbol),
        }
