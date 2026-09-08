from types import SimpleNamespace
import importlib.util
from pathlib import Path

import pandas as pd

from data.market_data import MarketDataProvider
from engine.expiry_manager import expiry_manager

_AUDIT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "full_system_audit.py"
_SPEC = importlib.util.spec_from_file_location("live_readiness_full_system_audit", _AUDIT_PATH)
audit = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(audit)


def _bare_provider():
    provider = MarketDataProvider.__new__(MarketDataProvider)
    provider.start_streams = False
    provider.allow_auth_refresh = False
    provider._fyers_refresh_disabled = False
    provider._fyers_auth_required = False
    provider._fyers_auth_reason = ""
    provider._fyers_token_updated_at = None
    provider.fyers = None
    return provider


def test_missing_fyers_cache_is_an_explicit_manual_login_blocker(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    provider = _bare_provider()

    provider._load_fyers_token()

    status = provider.get_fyers_auth_status()
    assert status["connected"] is False
    assert status["auth_required"] is True
    assert status["reason"] == "no cached Fyers token"
    assert status["recovery"] == "manual_login"
    assert status["login_path"] == "/api/fyers_login"
    assert status["token_cache_present"] is False


def test_read_only_provider_never_refreshes_an_invalid_fyers_token(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "fyers_token.json").write_text(
        '{"access_token":"redacted-test-token","refresh_token":"redacted-refresh"}',
        encoding="utf-8",
    )

    class FyersClient:
        def __init__(self, **_kwargs):
            pass

        def get_profile(self):
            return {"s": "error", "code": -8, "message": "invalid token"}

    monkeypatch.setattr("data.market_data.fyersModel.FyersModel", FyersClient)
    provider = _bare_provider()
    monkeypatch.setattr(
        provider,
        "_refresh_fyers_token",
        lambda _token: (_ for _ in ()).throw(AssertionError("refresh was attempted")),
    )

    provider._load_fyers_token()

    status = provider.get_fyers_auth_status()
    assert status["connected"] is False
    assert status["auth_required"] is True
    assert "read-only validation did not refresh" in status["reason"]
    assert status["access_token_present"] is True
    assert status["refresh_token_present"] is True


def test_malformed_profile_never_counts_as_connected(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "fyers_token.json").write_text(
        '{"access_token":"redacted-test-token"}', encoding="utf-8"
    )

    class FyersClient:
        def __init__(self, **_kwargs):
            pass

        def get_profile(self):
            return None

    monkeypatch.setattr("data.market_data.fyersModel.FyersModel", FyersClient)
    provider = _bare_provider()

    provider._load_fyers_token()

    status = provider.get_fyers_auth_status()
    assert status["connected"] is False
    assert status["auth_required"] is False
    assert "could not be validated" in status["reason"]
    assert status["recovery"] == "retry_validation"


def test_audit_status_payload_never_exposes_token_values():
    public = audit._public_status(
        {
            "connected": False,
            "access_token": "secret-one",
            "refresh_token": "secret-two",
            "reason": "access_token=secret-three invalid",
        }
    )

    rendered = str(public)
    assert "access_token" not in public
    assert "refresh_token" not in public
    assert "secret-one" not in rendered
    assert "secret-two" not in rendered
    assert "secret-three" not in rendered
    assert "<redacted>" in rendered


def test_audit_disables_refresh_and_classifies_auth_caused_chain_failures(
    monkeypatch,
):
    captured_kwargs = {}

    class Provider:
        def __init__(self, *args, **kwargs):
            captured_kwargs.update(kwargs)
            self.fyers = None
            self.kite = SimpleNamespace(
                _auth=SimpleNamespace(
                    status=lambda: {
                        "connected": False,
                        "auth_required": True,
                        "reason": "no cached Kite token",
                    }
                )
            )
            self._option_chain_meta = {}

        def connect(self):
            return False

        def get_fyers_auth_status(self):
            return {
                "connected": False,
                "auth_required": True,
                "reason": "Fyers manual login required",
            }

        def get_historical_candles(self, *args, **kwargs):
            return pd.DataFrame({"close": [1.0]}, index=pd.to_datetime(["2026-08-28"]))

        def get_index_quote(self, _symbol):
            return {"ltp": 1.0, "source": "cache", "quote_label": "CLOSED"}

        def get_market_source_snapshot(self, _symbol):
            return {"freshest": {"source": "cache"}, "entry_eligible": False}

        def get_latest_price(self, _symbol):
            return 1.0

        def get_option_chain(self, _symbol):
            return pd.DataFrame()

        def is_market_open(self):
            return False

    monkeypatch.setattr(
        audit,
        "get_settings",
        lambda: SimpleNamespace(
            angelone_api_key="",
            angelone_client_id="",
            angelone_password="",
            angelone_totp_secret="",
            active_indices=["NIFTY"],
        ),
    )
    monkeypatch.setattr(audit, "MarketDataProvider", Provider)
    monkeypatch.setattr(audit.time, "sleep", lambda _seconds: None)

    result = audit.run_end_to_end_audit(days=1)

    assert captured_kwargs["start_streams"] is False
    assert captured_kwargs["allow_auth_refresh"] is False
    assert result["live_ready"] is False
    assert {item["component"] for item in result["external_blockers"]} == {
        "kite",
        "fyers",
    }
    assert result["option_chains"]["NIFTY"]["classification"] == (
        "external_session_required"
    )
    assert result["software_failures"] == []
    assert result["session_limitations"]


def test_history_only_provider_does_not_start_kite_websocket(monkeypatch):
    """The audit's no-stream promise must also hold when Kite is authenticated."""

    api = object()

    class Auth:
        kite = api

        def load_cached_session(self):
            raise AssertionError("already-authenticated client was needlessly reloaded")

        def status(self):
            return {"connected": True, "user_id": "test-user", "reason": ""}

    provider = _bare_provider()
    provider.kite = None
    provider.start_streams = False
    provider.is_connected = False
    provider._active_kite_tokens = set()
    provider._kite_index_names = {}
    monkeypatch.setattr("data.kite_auth.get_kite_auth", lambda: Auth())
    monkeypatch.setattr("data.market_data.KiteDataSource.load_instruments", lambda *_a, **_k: [])
    monkeypatch.setattr(provider, "_load_kite_index_tokens", lambda: None)
    monkeypatch.setattr(
        provider,
        "start_kite_ticker",
        lambda: (_ for _ in ()).throw(AssertionError("Kite WebSocket was started")),
    )

    assert provider.connect() is True
    assert provider.is_connected is True


def _fyers_chain_response(*, default_date, active_date, active_timestamp="1770000000"):
    return {
        "s": "ok",
        "data": {
            "expiryData": [
                {"date": default_date, "expiry": "1760000000"},
                *(
                    [{"date": active_date, "expiry": active_timestamp}]
                    if active_date != default_date
                    else []
                ),
            ],
            "optionsChain": [
                {
                    "strike_price": 80000,
                    "option_type": "CE",
                    "oi": 100,
                    "volume": 10,
                    "oich": 5,
                    "ltp": 100.0,
                    "symbol": "BSE:SENSEX2690380000CE",
                    "fyToken": "ce-token",
                },
                {
                    "strike_price": 80000,
                    "option_type": "PE",
                    "oi": 120,
                    "volume": 12,
                    "oich": 6,
                    "ltp": 110.0,
                    "symbol": "BSE:SENSEX2690380000PE",
                    "fyToken": "pe-token",
                },
            ],
        },
    }


def _chain_provider(responses):
    class Fyers:
        def __init__(self, queued):
            self.queued = list(queued)
            self.calls = []

        def optionchain(self, *, data):
            self.calls.append(dict(data))
            return self.queued.pop(0)

    provider = _bare_provider()
    provider.fyers = Fyers(responses)
    provider._opt_chain_cache = {}
    provider._last_opt_chain_fetch = {}
    provider._last_opt_chain_error = {}
    provider._option_chain_meta = {}
    provider._fyers_chain_failure_streak = 0
    provider.get_latest_price = lambda _symbol: 80000.0
    provider._augment_chain_oi_from_kite = lambda _symbol, chain: chain

    def add_snapshot_source(_symbol, chain, **_kwargs):
        result = chain.copy()
        result["oi_change_source"] = "fyers"
        return result

    provider._apply_option_oi_snapshot_deltas = add_snapshot_source
    provider._maybe_self_heal_fyers = lambda _reason: None
    return provider


def test_fyers_default_active_expiry_reuses_first_valid_chain(monkeypatch):
    monkeypatch.setattr(
        expiry_manager,
        "expiries",
        {"SENSEX": {"active_weekly": "03SEP2026"}},
    )
    provider = _chain_provider(
        [_fyers_chain_response(default_date="03-09-2026", active_date="03-09-2026")]
    )

    chain = provider.get_option_chain("SENSEX", "BFO")

    assert len(chain) == 1
    assert len(provider.fyers.calls) == 1
    assert provider.fyers.calls[0]["timestamp"] == ""
    assert provider._option_chain_meta["SENSEX"]["expiry_aligned"] is True
    assert provider._option_chain_meta["SENSEX"]["active_expiry"] == "03SEP2026"


def test_failed_nondefault_active_expiry_refetch_tries_verified_fallback_then_fails_closed(monkeypatch):
    monkeypatch.setattr(
        expiry_manager,
        "expiries",
        {"SENSEX": {"active_weekly": "03SEP2026"}},
    )
    provider = _chain_provider(
        [
            _fyers_chain_response(
                default_date="10-09-2026",
                active_date="03-09-2026",
                active_timestamp="1777777777",
            ),
            {"s": "error", "message": "temporary provider failure"},
        ]
    )

    chain = provider.get_option_chain("SENSEX", "BFO")

    assert chain.empty
    assert [call["timestamp"] for call in provider.fyers.calls] == ["", "1777777777"]
    assert "opt_chain_SENSEX" not in provider._opt_chain_cache
    meta = provider._option_chain_meta["SENSEX"]
    assert meta["fallback"] is True
    assert meta["strike_count"] == 0
    assert meta["source"] == "angelone"


def test_failed_fyers_expiry_alignment_uses_verified_angel_chain(monkeypatch):
    monkeypatch.setattr(
        expiry_manager,
        "expiries",
        {"SENSEX": {"active_weekly": "03SEP2026"}},
    )
    provider = _chain_provider(
        [
            _fyers_chain_response(
                default_date="10-09-2026",
                active_date="03-09-2026",
                active_timestamp="1777777777",
            ),
            {"s": "error", "message": "temporary provider failure"},
        ]
    )
    provider.get_option_token = lambda _name, strike, side: {
        "symbol": f"SENSEX26903{int(strike)}{side}",
        "expiry": "03SEP2026",
        "strike": int(strike) * 100,
        "exch_seg": "BFO",
    }

    def pairs_for(_name, pairs):
        rows = []
        for call, _put in pairs:
            strike = call["strike"] / 100
            rows.append({
                "strike": strike,
                "expiry": "03SEP2026",
                "CE": {"symbol": f"SENSEX26903{int(strike)}CE", "token": f"c-{strike}", "ltp": 100.0, "oi": 1000.0, "volume": 2000.0},
                "PE": {"symbol": f"SENSEX26903{int(strike)}PE", "token": f"p-{strike}", "ltp": 110.0, "oi": 1200.0, "volume": 2200.0},
            })
        return rows

    provider.angel_index = SimpleNamespace(last_error="", get_option_pairs=pairs_for)

    chain = provider.get_option_chain("SENSEX", "BFO")

    assert len(chain) == 5
    meta = provider._option_chain_meta["SENSEX"]
    assert meta["source"] == "angelone_near_atm_realtime"
    assert meta["auto_certified"] is True
    assert meta["active_expiry"] == "03SEP2026"
