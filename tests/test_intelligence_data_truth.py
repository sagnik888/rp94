from datetime import datetime, timedelta
import json
from pathlib import Path
import threading
from types import SimpleNamespace
import warnings

import pandas as pd

from data.market_data import MarketDataProvider
from intelligence.greeks_engine import GreeksEngine
from intelligence.intelligence_aggregator import IntelligenceAggregator
from intelligence.oi_tracker import OITracker
from intelligence.order_flow import OrderFlowAnalyzer
from intelligence.outcome_calibration import CALIBRATION_FIELDS, OutcomeCalibrationStore
from scanner import Scanner


def candles():
    index = pd.date_range("2026-06-19 10:00", periods=20, freq="min")
    return pd.DataFrame(
        {
            "open": [100.0 + i * 0.1 for i in range(20)],
            "high": [101.0 + i * 0.1 for i in range(20)],
            "low": [99.0 + i * 0.1 for i in range(20)],
            "close": [100.5 + i * 0.1 for i in range(20)],
            "volume": [1000 + i * 10 for i in range(20)],
        },
        index=index,
    )


def option_chain(call_change=100, put_change=80):
    return pd.DataFrame(
        {
            "strike": [100, 110, 120],
            "call_oi": [1000, 1200, 900],
            "put_oi": [900, 1300, 1100],
            "call_oi_change": [call_change, 0, 0],
            "put_oi_change": [put_change, 0, 0],
            "call_volume": [100, 120, 80],
            "put_volume": [90, 130, 100],
            "call_ltp": [8.0, 4.0, 1.5],
            "put_ltp": [1.5, 4.5, 9.0],
            "call_iv": [20.0, 20.0, 20.0],
            "put_iv": [25.0, 25.0, 25.0],
        }
    )


def test_volatility_routing_uses_real_vix_percentile_not_price_velocity():
    aggregator = IntelligenceAggregator()
    common = dict(
        instrument="NIFTY",
        timeframe="5min",
        candle_df=candles(),
        options_chain=option_chain(),
        spot_price=110.0,
        strike_interval=10,
        days_to_expiry=7,
        volatility_value=18.4,
        volatility_percentile=82.0,
        volatility_source="india_vix_daily_percentile",
    )
    low_velocity = aggregator.analyze(price_change_pct=0.0, **common)
    high_velocity = aggregator.analyze(price_change_pct=3.0, **common)

    for result in (low_velocity, high_velocity):
        greeks = result["greeks"]
        assert greeks["iv_percentile"] == 82.0
        assert greeks["volatility_percentile_source"] == "india_vix_daily_percentile"
        assert greeks["volatility_value"] == 18.4
        assert greeks["chain"][0]["call_iv"] == 20.0
        assert greeks["chain"][0]["put_iv"] == 25.0
        assert greeks["chain"][0]["iv_source"] == "chain_iv"


def test_missing_vix_percentile_abstains_and_stays_explicit():
    """An absent percentile publishes None, not a neutral 50.

    The original contract paired a neutral 50.0 with an explicit
    ``volatility_percentile_available: False`` flag, so a consumer COULD tell. None did:
    every downstream gate read the number. 50.0 is also the most permissive value in the
    range -- 50 < 70 chose the looser 120s option-chain age limit over 90s, 50 < 80 kept
    FUT/OPT routing off the high-IV branch, and 50 < the 70.0 special-5min ceiling turned
    that gate into a pass. Meanwhile ``signal_quality_policy`` already carried an
    ``if iv_percentile is None: return False`` branch that could never be reached.

    The explicit half of the contract is unchanged and still asserted below.
    """
    result = IntelligenceAggregator().analyze(
        instrument="NIFTY",
        timeframe="5min",
        candle_df=candles(),
        options_chain=option_chain(),
        spot_price=110.0,
        strike_interval=10,
        days_to_expiry=7,
        price_change_pct=4.0,
    )
    assert result["greeks"]["iv_percentile"] is None
    assert result["greeks"]["volatility_percentile"] is None
    assert result["greeks"]["volatility_percentile_available"] is False
    assert result["greeks"]["volatility_percentile_source"] == "unavailable_neutral"


def test_an_absent_percentile_refuses_the_special_5min_gate():
    """The abstain branch that was unreachable while 50.0 was published."""
    from engine.signal_quality_policy import option_premium_health_ok
    from config.settings import Settings

    settings = Settings()
    assert option_premium_health_ok(iv_percentile=None, settings=settings) is False
    assert option_premium_health_ok(iv_percentile=50.0, settings=settings) is True


def test_divergence_warnings_are_throttled(monkeypatch):
    import intelligence.intelligence_aggregator as aggregator_module

    aggregator = IntelligenceAggregator()
    warnings = []
    now = [1000.0]

    monkeypatch.setattr(aggregator_module.time, "time", lambda: now[0])
    monkeypatch.setattr(aggregator_module.logger, "warning", lambda msg: warnings.append(str(msg)))
    aggregator.order_flow.analyze = lambda *args, **kwargs: {"signal": "BEARISH", "ratio": 0.5}

    for _ in range(3):
        result = aggregator.analyze(
            instrument="BANKNIFTY",
            timeframe="5min",
            candle_df=candles(),
            options_chain=None,
            spot_price=110.0,
            price_change_pct=0.2,
        )
        assert result["divergence_alert"] is True

    assert len(warnings) == 1
    assert "Distribution" in warnings[0]

    # Inside the throttle window (now settings-driven, default 300s = one
    # 5min bar) a repeat must stay suppressed...
    now[0] += 16.0
    aggregator.analyze(
        instrument="BANKNIFTY",
        timeframe="5min",
        candle_df=candles(),
        options_chain=None,
        spot_price=110.0,
        price_change_pct=0.2,
    )
    assert len(warnings) == 1

    # ...and re-fire only after the window elapses.
    now[0] += 300.0
    aggregator.analyze(
        instrument="BANKNIFTY",
        timeframe="5min",
        candle_df=candles(),
        options_chain=None,
        spot_price=110.0,
        price_change_pct=0.2,
    )
    assert len(warnings) == 2


def test_ranging_regime_does_not_erase_measured_order_flow_and_divergence():
    aggregator = IntelligenceAggregator()
    aggregator.volume = SimpleNamespace(
        analyze=lambda *args, **kwargs: {
            "signal": "NEUTRAL",
            "buy_sell_ratio": 1.0,
            "current_volume": 1000,
        }
    )
    aggregator.regime = SimpleNamespace(
        detect=lambda *args, **kwargs: {"regime": "RANGING"}
    )
    aggregator.order_flow = SimpleNamespace(
        analyze=lambda *args, **kwargs: {"signal": "BULLISH", "ratio": 1.5}
    )

    result = aggregator.analyze(
        instrument="NIFTY",
        timeframe="5min",
        candle_df=candles(),
        options_chain=None,
        spot_price=110.0,
        price_change_pct=-0.2,
    )

    aggregate = result["aggregate"]
    # Weight-normalised (2026-08-08): the vote sum is unchanged, the divisor is now the
    # sum of the counted pillars' MAXIMUM votes rather than their count.
    assert aggregate["score_before_regime_dampening"] == 0.5
    assert aggregate["regime_score_multiplier"] == 1.0
    # A non-authoritative option chain (here options_chain=None) does not feed the
    # OI/PCR components into the score, and the published value is rounded to 1dp.
    # RANGING contributes no directional vote. It must not also multiply independent
    # flow/divergence measurements by 0.15; the quality policy applies regime risk once.
    assert aggregate["score"] == 38.5
    assert aggregate["signal"] == "BUY"
    # The evidence this rests on is now stated rather than implied.
    assert aggregate["evidence_mass"] == 0.75
    assert aggregate["evidence_ratio"] == round(0.75 / 1.30, 3)


def test_greeks_engine_keeps_expiry_day_values_nonzero():
    result = GreeksEngine().calculate(
        spot=24000.0,
        strike=24000.0,
        days=0.0,
        iv=0.18,
        opt_type="call",
    )

    assert result["effective_days_to_expiry"] == 0.5
    assert result["price"] > 0
    assert result["gamma"] > 0
    assert result["delta"] > 0


def test_angelone_zero_oi_change_is_derived_from_persisted_snapshot():
    provider = MarketDataProvider.__new__(MarketDataProvider)
    provider._option_oi_snapshots = {}
    provider._save_option_oi_snapshots = lambda: None
    first = pd.DataFrame(
        {
            "strike": [100],
            "call_oi": [1000],
            "put_oi": [900],
            "call_oi_change": [0],
            "put_oi_change": [0],
        }
    )
    warmup = provider._apply_option_oi_snapshot_deltas(
        "NIFTY", first, provider="angelone", expiry="25JUN2026"
    )
    assert warmup.iloc[0]["oi_change_source"] == "angelone_snapshot_warmup"

    second = first.copy()
    second["call_oi"] = [1125]
    second["put_oi"] = [850]
    derived = provider._apply_option_oi_snapshot_deltas(
        "NIFTY", second, provider="angelone", expiry="25JUN2026"
    )
    assert derived.iloc[0]["call_oi_change"] == 125
    assert derived.iloc[0]["put_oi_change"] == -50
    assert derived.iloc[0]["oi_change_source"] == "angelone_snapshot_delta"


def test_provider_reported_oi_change_is_preserved():
    provider = MarketDataProvider.__new__(MarketDataProvider)
    provider._option_oi_snapshots = {}
    provider._save_option_oi_snapshots = lambda: None
    chain = option_chain(call_change=45, put_change=-20)
    normalized = provider._apply_option_oi_snapshot_deltas(
        "BANKNIFTY", chain, provider="fyers", expiry="30JUN2026"
    )
    assert normalized["call_oi_change"].sum() == 45
    assert normalized["put_oi_change"].sum() == -20
    assert set(normalized["oi_change_source"]) == {"fyers_reported"}


def test_option_oi_snapshot_save_is_thread_safe(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    provider = MarketDataProvider.__new__(MarketDataProvider)
    provider._option_oi_snapshots = {}
    errors = []

    def worker(offset):
        chain = pd.DataFrame(
            {
                "strike": [100 + offset],
                "call_oi": [1000 + offset],
                "put_oi": [900 + offset],
                "call_oi_change": [0],
                "put_oi_change": [0],
            }
        )
        try:
            provider._apply_option_oi_snapshot_deltas(
                "NIFTY",
                chain,
                provider="fyers",
                expiry=f"{25 + offset:02d}JUN2026",
            )
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(offset,)) for offset in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    snapshot_path = Path("data_store/option_oi_snapshots.json")
    payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert len(payload) == 8
    assert list(Path("data_store").glob("option_oi_snapshots.json.*.tmp")) == []


def test_option_subscription_uses_full_mode_for_oi():
    """Option streams must carry OI, or the intelligence layer runs on stale REST data.

    AngelOne needed a separate SNAP_QUOTE mode (3) for this; Kite carries OI *and*
    5-level depth in MODE_FULL. The requirement is unchanged, the mechanism is not.
    """
    subscribed, modes = [], []
    provider = MarketDataProvider.__new__(MarketDataProvider)
    provider._active_kite_tokens = set()
    provider._kite_ws_connected = True
    provider.kws = SimpleNamespace(
        MODE_FULL="full",
        subscribe=lambda tokens: subscribed.extend(tokens),
        set_mode=lambda mode, tokens: modes.append((mode, tuple(tokens))),
    )
    provider.subscribe_tokens(["111", "222"], exchange_type=2)

    assert subscribed == [111, 222]
    assert modes == [("full", (111, 222))]


def test_option_subscription_queues_tokens_while_the_socket_is_down():
    """Tokens must survive a disconnect: on_connect replays them.

    Previously subscribe_tokens() was a silent no-op with no socket, so option
    contracts were never streamed at all after the AngelOne retirement.
    """
    provider = MarketDataProvider.__new__(MarketDataProvider)
    provider._active_kite_tokens = set()
    provider._kite_ws_connected = False
    provider.kws = None

    provider.subscribe_tokens(["333"])
    assert provider._active_kite_tokens == {"333"}


def test_zero_oi_delta_never_creates_directional_signal():
    chain = option_chain(call_change=0, put_change=0)
    chain["oi_change_source"] = "angelone_snapshot_warmup"
    result = OITracker().analyze(
        chain,
        price_change=2.0,
        spot_price=110.0,
        strike_interval=10,
    )
    assert result["signal"] == "NEUTRAL"
    assert result["activity"] == "NO_CONFIRMED_OI_CHANGE"
    assert result["score"] == 50
    assert result["oi_change_source"] == "angelone_snapshot_warmup"


def test_oi_tracker_near_money_slice_is_warning_free_and_does_not_mutate_input():
    chain = pd.DataFrame(
        {
            "strike": ["80", "100", "110", "120", "140"],
            "call_oi": ["100", "1000", "1200", "900", "50"],
            "put_oi": ["50", "900", "1300", "1100", "100"],
            "call_oi_change": ["0", "100", "0", "0", "0"],
            "put_oi_change": ["0", "80", "0", "0", "0"],
        }
    )
    original = chain.copy(deep=True)

    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        result = OITracker().analyze(
            chain,
            price_change=0.2,
            spot_price=110.0,
            strike_interval=10,
            near_strikes=1,
        )

    assert result["analysis_basis"] == "NEAR_MONEY"
    assert result["analysis_strike_count"] == 3
    assert not any(
        issubclass(warning.category, pd.errors.SettingWithCopyWarning)
        for warning in captured
    )
    pd.testing.assert_frame_equal(chain, original)


def test_order_flow_discloses_proxy_limitation():
    result = OrderFlowAnalyzer().analyze(candles())
    assert result["true_tick_footprint_available"] is False
    assert "OHLCV/L1 proxy" in result["limitation"]


def test_vix_percentile_uses_prior_daily_observations_only():
    scanner = Scanner.__new__(Scanner)
    start = datetime(2026, 1, 1, 15, 30)
    scanner._vix_data = {
        (start + timedelta(days=i)).strftime("%Y-%m-%d %H:%M:%S"): 10.0 + i
        for i in range(30)
    }
    percentile = scanner._vix_percentile(
        25.0,
        datetime(2026, 2, 15, 10, 0),
    )
    assert percentile == 53.3


def test_instrument_iv_rank_overrides_vix_when_history_is_sufficient():
    aggregator = IntelligenceAggregator()
    # option_chain ATM IV ≈ mean(20%, 25%) = 22.5%; all prior observations are
    # well below it, so the instrument IV-Rank should resolve to 100.
    result = aggregator.analyze(
        instrument="NIFTY",
        timeframe="5min",
        candle_df=candles(),
        options_chain=option_chain(),
        spot_price=110.0,
        strike_interval=10,
        days_to_expiry=7,
        volatility_value=18.4,
        volatility_percentile=82.0,
        volatility_source="india_vix_daily_percentile",
        instrument_iv_observations=[5.0] * 25,
        instrument_iv_min_sessions=20,
    )
    greeks = result["greeks"]
    assert greeks["iv_percentile_basis"] == "instrument_iv_rank"
    assert greeks["iv_percentile"] == 100.0
    assert greeks["instrument_iv"] == 22.5
    # India VIX percentile is still published for transparency, unused for routing.
    assert greeks["volatility_percentile"] == 82.0


def test_instrument_iv_rank_falls_back_to_vix_when_history_is_thin():
    result = IntelligenceAggregator().analyze(
        instrument="NIFTY",
        timeframe="5min",
        candle_df=candles(),
        options_chain=option_chain(),
        spot_price=110.0,
        strike_interval=10,
        days_to_expiry=7,
        volatility_percentile=82.0,
        volatility_source="india_vix_daily_percentile",
        instrument_iv_observations=[5.0] * 5,
        instrument_iv_min_sessions=20,
    )
    greeks = result["greeks"]
    assert greeks["iv_percentile_basis"] == "india_vix_percentile"
    assert greeks["iv_percentile"] == 82.0


def test_instrument_iv_observations_use_prior_sessions_only():
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner._instrument_iv_data = {
        "NIFTY": {"2026-06-18": 18.0, "2026-06-19": 19.0, "2026-06-20": 20.0}
    }
    # as_of excludes the current session and everything after it.
    obs = scanner._instrument_iv_observations("NIFTY", datetime(2026, 6, 20, 10, 0))
    assert sorted(obs) == [18.0, 19.0]


def test_record_instrument_iv_persists_one_real_observation_per_session():
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner._instrument_iv_data = {}
    scanner._persist_instrument_iv_history = lambda: None
    scanner._record_instrument_iv(
        "NIFTY",
        {"greeks": {"instrument_iv": 21.5, "iv_source": "chain_iv"}},
        datetime(2026, 6, 22, 10, 0),
    )
    assert scanner._instrument_iv_data["NIFTY"]["2026-06-22"] == 21.5
    # A default/low-quality IV source must never be recorded.
    scanner._record_instrument_iv(
        "BANKNIFTY",
        {"greeks": {"instrument_iv": 0.0, "iv_source": "default_low_quality"}},
        datetime(2026, 6, 22, 10, 0),
    )
    assert "BANKNIFTY" not in scanner._instrument_iv_data


def test_first_poll_of_new_session_uses_overnight_oi_delta():
    provider = MarketDataProvider.__new__(MarketDataProvider)
    provider._save_option_oi_snapshots = lambda: None
    provider._option_oi_snapshots = {
        "NIFTY|25JUN2026": {
            "provider": "angelone",
            "expiry": "25JUN2026",
            "session": "2026-06-20",
            "captured_at": __import__("time").time(),
            "rows": {"100.0": {"call_oi": 1000.0, "put_oi": 900.0}},
        }
    }
    current = pd.DataFrame(
        {
            "strike": [100],
            "call_oi": [1100],
            "put_oi": [850],
            "call_oi_change": [0],
            "put_oi_change": [0],
        }
    )
    derived = provider._apply_option_oi_snapshot_deltas(
        "NIFTY", current, provider="angelone", expiry="25JUN2026"
    )
    assert derived.iloc[0]["call_oi_change"] == 100
    assert derived.iloc[0]["put_oi_change"] == -50
    assert derived.iloc[0]["oi_change_source"] == "angelone_overnight_delta"


def test_stale_oi_snapshot_falls_back_to_warmup():
    provider = MarketDataProvider.__new__(MarketDataProvider)
    provider._save_option_oi_snapshots = lambda: None
    provider._option_oi_snapshots = {
        "NIFTY|25JUN2026": {
            "provider": "angelone",
            "expiry": "25JUN2026",
            "session": "2026-06-01",
            "captured_at": __import__("time").time() - 10 * 86400,
            "rows": {"100.0": {"call_oi": 1000.0, "put_oi": 900.0}},
        }
    }
    current = pd.DataFrame(
        {"strike": [100], "call_oi": [1100], "put_oi": [850],
         "call_oi_change": [0], "put_oi_change": [0]}
    )
    derived = provider._apply_option_oi_snapshot_deltas(
        "NIFTY", current, provider="angelone", expiry="25JUN2026"
    )
    assert derived.iloc[0]["oi_change_source"] == "angelone_snapshot_warmup"


def test_calibration_remains_advisory_until_validation_thresholds(tmp_path):
    store = OutcomeCalibrationStore(str(tmp_path / "calibration.csv"))
    incomplete = {field: "" for field in CALIBRATION_FIELDS}
    incomplete.update({"trade_id": "T1", "instrument": "NIFTY", "pnl": "100"})
    store.append_rows([incomplete])

    status = store.validation_status(min_samples=2, min_samples_per_instrument=1)
    assert status["state"] == "COLLECTING_EVIDENCE"
    assert status["live_adjustments_enabled"] is False
    assert status["self_learning_claim_allowed"] is False
    assert status["context_coverage_pct"] == 0.0
    assert len(status["validation_plan"]) == 6
