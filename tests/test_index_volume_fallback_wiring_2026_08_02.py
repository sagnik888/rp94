"""The decided index-volume policy must actually reach the candles.

Policy: Fyers cash volume is primary; when it is absent or stale, near-month Kite
FUTURES volume (and OI) stands in. `MarketDataProvider.get_index_activity` implemented
exactly that and was tested — but nothing in the live path ever called it, so index
candles carried volume 0 all day. Two visible consequences:

  * the Market Intelligence volume panel had nothing to show, and
  * `Scanner._candle_thrust_metrics` computed volume_ratio 0.0, which can never clear
    power_move_volume_mult (2.5) — and that surge is the ONLY way a 1min signal is
    admitted for entry, so the 1min surge signal could not fire at all.

The unit changes across that fallback (cash shares vs futures contracts, ~1000x apart),
so the switch must REBASE the candle's running baseline. Subtracting a shares baseline
from a contracts total fabricates a spike, and a fabricated spike is read by the 2.5x
gate as a genuine breakout — worse than having no volume.
"""
import threading
import time
from datetime import datetime, timedelta

import pytest

import pandas as pd

from data.candle_builder import CandleBuilder


def _ticks(builder, instrument, start, samples, source):
    for i, (price, volume) in enumerate(samples):
        builder.update_from_tick(
            instrument, price, volume, start + timedelta(seconds=i * 5), source
        )


def test_futures_volume_reaches_the_candle_when_fyers_is_silent():
    """The whole point: a real, non-zero per-candle volume for an index."""
    b = CandleBuilder()
    t0 = datetime(2026, 8, 2, 10, 0, 0)
    _ticks(b, "NIFTY", t0, [
        (24000.0, 100_000), (24005.0, 101_500), (24010.0, 103_000),
    ], "kite_index_futures")

    df = b.get_candles("NIFTY", "1min")
    assert df is not None and not df.empty
    assert float(df["volume"].iloc[-1]) == 3000.0, (
        "futures volume never became a per-candle figure"
    )


def test_a_source_switch_rebases_instead_of_inventing_a_spike():
    """Fyers(shares) -> futures(contracts) is ~1000x apart in the WRONG direction.

    Subtracting the old baseline would post a colossal fake volume bar, and the 2.5x
    surge gate would read it as a breakout and admit a 1min entry on fiction.
    """
    b = CandleBuilder()
    t0 = datetime(2026, 8, 2, 10, 0, 0)
    # Futures contracts first (small numbers), then Fyers cash shares (huge numbers).
    _ticks(b, "NIFTY", t0, [(24000.0, 100_000), (24005.0, 101_000)], "kite_index_futures")
    _ticks(b, "NIFTY", t0 + timedelta(seconds=20),
           [(24010.0, 409_000_000), (24015.0, 409_200_000)], "fyers_index")

    volume = float(b.get_candles("NIFTY", "1min")["volume"].iloc[-1])
    assert volume < 1_000_000, f"fabricated a {volume:,.0f} volume spike on a unit switch"
    # After rebasing, the delta measured within the NEW unit is what survives.
    assert volume == 200_000.0


def test_a_backwards_counter_rebases_rather_than_zeroing_the_rest_of_the_candle():
    """Brokers reset cumulative counters on reconnect; that is not zero participation."""
    b = CandleBuilder()
    t0 = datetime(2026, 8, 2, 10, 0, 0)
    _ticks(b, "NIFTY", t0, [
        (24000.0, 500_000), (24005.0, 520_000),
        (24010.0, 5_000),                        # reconnect: counter restarted
        (24015.0, 9_000),
    ], "kite_index_futures")

    volume = float(b.get_candles("NIFTY", "1min")["volume"].iloc[-1])
    assert volume >= 4_000.0, "post-reset participation was discarded entirely"


def test_a_steady_source_still_measures_a_plain_delta():
    b = CandleBuilder()
    t0 = datetime(2026, 8, 2, 10, 0, 0)
    _ticks(b, "NIFTY", t0, [(24000.0, 10_000), (24005.0, 12_500)], "fyers_index")
    assert float(b.get_candles("NIFTY", "1min")["volume"].iloc[-1]) == 2500.0


def test_an_unlabelled_feed_is_not_treated_as_a_source_change():
    """Legacy/4-arg callers pass no label; they must keep working unchanged."""
    b = CandleBuilder()
    t0 = datetime(2026, 8, 2, 10, 0, 0)
    b.update_from_tick("NIFTY", 24000.0, 10_000, t0)
    b.update_from_tick("NIFTY", 24005.0, 12_000, t0 + timedelta(seconds=5))
    assert float(b.get_candles("NIFTY", "1min")["volume"].iloc[-1]) == 2000.0


# ───────── The fallback must not hide the outage it papers over ─────────
# Before this, index volume could be dead for days with nothing surfacing it. Now the
# futures fallback keeps trading alive when Fyers goes quiet — which removes the only
# symptom anyone would have noticed. So degradation has to announce itself.

from types import SimpleNamespace

from scanner import Scanner


class _Provider:
    def __init__(self, source, fyers_online=True):
        self.source = source
        self.fyers_online = fyers_online
        # The in-memory state the tick path maintains. The watchdog reads THIS rather
        # than calling get_index_activity, because that reaches the broker and cannot
        # run on a timer from the event loop.
        volume = {"fyers_index": 409_000_000.0, "kite_index_futures": 250_000.0}.get(source, 0.0)
        self._index_cum_volume = {"NIFTY": volume} if volume else {}
        self._index_cum_volume_source = {"NIFTY": source} if source != "none" else {}
        self._index_cum_volume_at = {"NIFTY": time.time()} if source != "none" else {}

    def get_index_activity(self, symbol):
        futures = {"available": True, "volume": 250_000.0, "oi": 1_200_000.0,
                   "tradingsymbol": f"{symbol}FUT"}
        if self.source == "fyers_index":
            return {"source": "fyers_index", "unit": "shares", "volume": 409_000_000.0,
                    "comparable_to_thresholds": True, "fyers_online": True,
                    "futures": futures, "confirmation": "broker_confirms"}
        if self.source == "kite_index_futures":
            return {"source": "kite_index_futures", "unit": "contracts", "volume": 250_000.0,
                    "comparable_to_thresholds": False, "fyers_online": False,
                    "futures": futures, "confirmation": "fallback_only"}
        return {"source": "none", "unit": "shares", "volume": 0.0,
                "comparable_to_thresholds": True, "fyers_online": False,
                "futures": {}, "confirmation": "no_volume_source"}


def _scanner(source):
    s = Scanner.__new__(Scanner)
    s.data = _Provider(source)
    s.config = {"indices": {"NIFTY": {}}}
    s.active_indices = ["NIFTY"]
    return s


def test_health_reports_the_primary_feed_as_healthy():
    health = _scanner("fyers_index").volume_source_health()
    assert health["healthy"] is True and health["degraded"] == []
    row = health["instruments"]["NIFTY"]
    assert row["unit"] == "shares" and row["comparable_to_thresholds"] is True


def test_health_flags_the_futures_fallback_as_degraded():
    """Trading continues on the fallback — that is exactly why it needs surfacing."""
    health = _scanner("kite_index_futures").volume_source_health()
    assert health["healthy"] is False and health["degraded"] == ["NIFTY"]
    row = health["instruments"]["NIFTY"]
    assert row["degraded"] is True
    assert row["unit"] == "contracts"
    assert row["comparable_to_thresholds"] is False, (
        "a caller comparing contracts against share-calibrated thresholds is wrong by ~1000x"
    )
    assert row["futures_oi"] == 1_200_000.0, "OI is part of the decided fallback payload"


def test_health_flags_a_total_outage():
    health = _scanner("none").volume_source_health()
    assert health["degraded"] == ["NIFTY"] and "none" in health["summary"].lower()


def test_degradation_alerts_once_per_transition_not_every_pass():
    """A per-pass alert would recreate the CRITICAL flood other latches exist to stop."""
    s = _scanner("kite_index_futures")
    seen = []
    s.on_notification = lambda msg, level="info": seen.append((msg, level))

    for _ in range(5):
        s._alert_volume_source_degradation()
    assert len(seen) == 1, f"alerted {len(seen)} times for one degradation"
    assert "degraded" in seen[0][0].lower() and seen[0][1] == "warning"

    # Recovery, then a second degradation, must alert again.
    s.data = _Provider("fyers_index")
    s._alert_volume_source_degradation()
    s.data = _Provider("kite_index_futures")
    s._alert_volume_source_degradation()
    assert len(seen) == 2, "a genuine re-degradation was swallowed"


def test_a_healthy_feed_never_alerts():
    s = _scanner("fyers_index")
    seen = []
    s.on_notification = lambda msg, level="info": seen.append(msg)
    s._alert_volume_source_degradation()
    assert seen == []


# ───────── A price-only tick must not reset the volume baseline ─────────
# Both feeds publish ticks with an ltp and no volume field, and _emit_index_tick sends
# those as (0, ""). Treating volume==0 as a "backwards counter" rebased the baseline to
# zero, so the NEXT tick's whole day-cumulative total landed as that bar's delta — a
# ~4000x fabricated spike. That clears the 2.5x surge gate, which is the only route a
# 1min signal has into a live entry. This is the steady state of the outage the futures
# fallback exists for, not a rare edge.

def test_a_price_only_tick_does_not_fabricate_a_full_cumulative_bar():
    b = CandleBuilder()
    t0 = datetime(2026, 8, 2, 10, 0, 0)
    _ticks(b, "NIFTY", t0, [
        (24000.0, 409_000_000), (24005.0, 409_050_000),
    ], "fyers_index")
    # Price-only packet: ltp present, no volume key -> emitted as (0, "")
    b.update_from_tick("NIFTY", 24007.0, 0, t0 + timedelta(seconds=15))
    b.update_from_tick("NIFTY", 24010.0, 409_100_000, t0 + timedelta(seconds=20), "fyers_index")

    volume = float(b.get_candles("NIFTY", "1min")["volume"].iloc[-1])
    assert volume == 100_000.0, (
        f"true delta is 100,000 but the bar recorded {volume:,.0f} — a zero-volume tick "
        "reset the baseline and the next cumulative total was booked as one bar"
    )


def test_interleaved_price_only_ticks_keep_the_delta_honest():
    """The live shape: Fyers price ticks interleaved with volume-carrying ticks."""
    b = CandleBuilder()
    t0 = datetime(2026, 8, 2, 10, 0, 0)
    b.update_from_tick("NIFTY", 24000.0, 300_000, t0, "kite_index_futures")
    b.update_from_tick("NIFTY", 24001.0, 0, t0 + timedelta(seconds=5))
    b.update_from_tick("NIFTY", 24002.0, 300_180, t0 + timedelta(seconds=10), "kite_index_futures")
    b.update_from_tick("NIFTY", 24003.0, 0, t0 + timedelta(seconds=15))
    b.update_from_tick("NIFTY", 24004.0, 300_360, t0 + timedelta(seconds=20), "kite_index_futures")

    volume = float(b.get_candles("NIFTY", "1min")["volume"].iloc[-1])
    assert volume == 360.0, f"true delta 360, recorded {volume:,.0f}"


def test_a_candle_that_opens_on_a_price_only_tick_still_measures_correctly():
    """The first-tick-of-minute variant: no baseline exists yet, so 0 must not become one."""
    b = CandleBuilder()
    t0 = datetime(2026, 8, 2, 10, 0, 0)
    b.update_from_tick("NIFTY", 24000.0, 0, t0)                      # opens the candle
    b.update_from_tick("NIFTY", 24005.0, 500_000, t0 + timedelta(seconds=10), "kite_index_futures")
    b.update_from_tick("NIFTY", 24010.0, 500_400, t0 + timedelta(seconds=20), "kite_index_futures")

    volume = float(b.get_candles("NIFTY", "1min")["volume"].iloc[-1])
    assert volume == 400.0, (
        f"recorded {volume:,.0f}; a candle opening on a price-only tick booked the whole "
        "cumulative total as its delta"
    )


def test_a_new_minute_opening_on_a_price_only_tick_is_also_safe():
    b = CandleBuilder()
    t0 = datetime(2026, 8, 2, 10, 0, 0)
    _ticks(b, "NIFTY", t0, [(24000.0, 700_000), (24005.0, 700_500)], "kite_index_futures")
    # Next minute opens with a price-only tick, then real totals arrive.
    b.update_from_tick("NIFTY", 24006.0, 0, t0 + timedelta(minutes=1))
    b.update_from_tick("NIFTY", 24007.0, 700_900, t0 + timedelta(minutes=1, seconds=10), "kite_index_futures")
    b.update_from_tick("NIFTY", 24008.0, 701_100, t0 + timedelta(minutes=1, seconds=20), "kite_index_futures")

    volume = float(b.get_candles("NIFTY", "1min")["volume"].iloc[-1])
    assert volume == 200.0, f"recorded {volume:,.0f} for a 200-contract minute"


# ───────── Arity must be inspected, never inferred from a caught TypeError ─────────

def _provider():
    from data.market_data import MarketDataProvider
    p = MarketDataProvider.__new__(MarketDataProvider)
    p._tick_callback_ref = None
    p._tick_callback_takes_source = None
    return p


def test_a_five_arg_callback_receives_the_source_label():
    p, seen = _provider(), []
    p.on_tick = lambda name, price, vol, ts, src="": seen.append(src)
    p._emit_index_tick("NIFTY", 24000.0, 100, "kite_index_futures")
    assert seen == ["kite_index_futures"]


def test_a_legacy_four_arg_callback_still_works():
    p, seen = _provider(), []
    p.on_tick = lambda name, price, vol, ts: seen.append((name, vol))
    p._emit_index_tick("NIFTY", 24000.0, 100, "fyers_index")
    assert seen == [("NIFTY", 100)]


def test_a_typeerror_inside_the_callback_is_not_retried_as_an_arity_problem():
    """A retry would double-apply the tick AND replay it without the source label —
    i.e. without the rebase protection that stops a fabricated volume spike."""
    p, calls = _provider(), []

    def raising(name, price, vol, ts, src=""):
        calls.append(src)
        raise TypeError("unsupported operand type(s) — raised INSIDE the callback")

    p.on_tick = raising
    with pytest.raises(TypeError):
        p._emit_index_tick("NIFTY", 24000.0, 100, "kite_index_futures")
    assert len(calls) == 1, f"tick was applied {len(calls)} times, not once"


# ───────── A unit change must not read as a surge ACROSS candles ─────────
# The per-candle rebase only protects the delta inside one bar. Every surge gate divides
# the latest bar by the mean of the previous ~20, so a lookback that straddles a
# Fyers(shares) <-> futures(contracts) failover produces a ~100x ratio with no change in
# participation at all — clearing the 2.5x gate and admitting a 1min entry on a feed event.

def _window(bases, volumes):
    import pandas as pd
    idx = pd.date_range("2026-08-02 10:00", periods=len(volumes), freq="1min")
    return pd.DataFrame(
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5,
         "volume": volumes, "volume_basis": bases},
        index=idx,
    )


def test_a_mixed_unit_window_is_not_homogeneous():
    w = _window(["kite_index_futures"] * 20 + ["fyers_index"], [500] * 20 + [409_000_000])
    assert Scanner._volume_window_is_homogeneous(w) is False


def test_a_single_unit_window_is_homogeneous():
    w = _window(["fyers_index"] * 21, [1000] * 21)
    assert Scanner._volume_window_is_homogeneous(w) is True


def test_a_window_with_no_basis_column_is_treated_as_homogeneous():
    """Historical frames and legacy feeds carry no label; they must keep working."""
    import pandas as pd
    idx = pd.date_range("2026-08-02 10:00", periods=21, freq="1min")
    w = pd.DataFrame({"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5,
                      "volume": [1000] * 21}, index=idx)
    assert Scanner._volume_window_is_homogeneous(w) is True


def test_thrust_metrics_refuse_a_ratio_across_a_feed_failover():
    """The whole point: a feed failover must not manufacture a tradable surge."""
    class _Candles:
        def __init__(self, df):
            self.df = df

        def get_candles(self, instrument, timeframe):
            return self.df

    sc = Scanner.__new__(Scanner)
    # 20 quiet futures bars, then one cash-volume bar ~800,000x larger.
    sc.candles = _Candles(_window(
        ["kite_index_futures"] * 20 + ["fyers_index"],
        [500] * 20 + [409_000_000],
    ))
    assert Scanner._candle_thrust_metrics(sc, "NIFTY", "1min", "BUY") is None, (
        "a Fyers recovery was reported as a genuine volume surge"
    )

    # Same shape, one consistent unit -> a real surge is still measured.
    sc.candles = _Candles(_window(["fyers_index"] * 21, [500] * 20 + [5_000]))
    metrics = Scanner._candle_thrust_metrics(sc, "NIFTY", "1min", "BUY")
    assert metrics is not None and metrics["volume_ratio"] == 10.0


# ───────── Health must measure the candles, not what a feed COULD supply ─────────
# The 2026-07-27 shape: the Fyers quote cache looked perfectly healthy while the candle
# series the gates read was all zeros. A check built on the quote alone reports all-green
# straight through the outage it exists to catch.

class _Candles:
    def __init__(self, volumes):
        self.volumes = volumes

    def get_candles(self, instrument, timeframe):
        import pandas as pd
        if self.volumes is None:
            return None
        idx = pd.date_range("2026-08-02 10:00", periods=len(self.volumes), freq="1min")
        return pd.DataFrame({"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5,
                             "volume": self.volumes}, index=idx)


def _scanner_with_candles(source, volumes):
    s = _scanner(source)
    s.candles = _Candles(volumes)
    return s


def test_a_healthy_quote_over_dry_candles_still_reports_degraded():
    """The exact outage: quote cache fine, candles empty."""
    s = _scanner_with_candles("fyers_index", [0.0] * 10)
    health = s.volume_source_health()
    assert health["healthy"] is False, "reported all-green while the gates saw no volume"
    assert health["degraded"] == ["NIFTY"]
    row = health["instruments"]["NIFTY"]
    assert row["candle_volume_dry"] is True
    assert "candles dry" in health["summary"]


def test_live_candles_on_the_primary_feed_are_healthy():
    s = _scanner_with_candles("fyers_index", [1000.0] * 10)
    health = s.volume_source_health()
    assert health["healthy"] is True and health["degraded"] == []
    assert health["instruments"]["NIFTY"]["candle_volume_dry"] is False


def test_a_short_warmup_series_is_not_called_dry():
    """Too few bars is start-up, not an outage — that would alert every morning."""
    s = _scanner_with_candles("fyers_index", [0.0] * 3)
    health = s.volume_source_health()
    assert health["instruments"]["NIFTY"]["candle_volume_dry"] is False
    assert health["healthy"] is True


def test_dry_candles_raise_the_alert_even_when_the_source_looks_primary():
    s = _scanner_with_candles("fyers_index", [0.0] * 10)
    seen = []
    s.on_notification = lambda msg, level="info": seen.append(msg)
    s._alert_volume_source_degradation()
    assert len(seen) == 1, "the outage shape produced no operator alert"


def test_health_covers_active_indices_not_every_configured_one():
    """Warning about an instrument nobody trades is a false alarm."""
    s = _scanner_with_candles("fyers_index", [1000.0] * 10)
    s.config = {"indices": {"NIFTY": {}, "MIDCPNIFTY": {}}}
    s.active_indices = ["NIFTY"]
    assert list(s.volume_source_health()["instruments"]) == ["NIFTY"]


# ───────── The stamp the BUILDER writes must be the stamp the GUARD reads ─────────
# The tests above build volume_basis frames by hand, which proves the guard's logic but
# not the seam. Driving real ticks through CandleBuilder found two holes the hand-built
# frames could never have shown: the column was dropped entirely by resampling (so the
# guard was inert on 5min/15min, exactly where _detect_power_move runs), and NaN from a
# historical backfill counted as a distinct unit (which marked every post-backfill window
# mixed and silently re-killed the 1min surge signal).

def _drive(builder, minutes, source, start_cum, step, t0):
    cum = start_cum
    for m in range(minutes):
        for sec in (0, 20, 40):
            cum += step
            builder.update_from_tick(
                "NIFTY", 24000 + m * 0.5, cum,
                t0 + timedelta(minutes=m, seconds=sec), source,
            )
    return cum


def test_volume_basis_survives_into_every_timeframe():
    """_detect_power_move runs on 5min/15min; a guard absent there guards nothing."""
    b = CandleBuilder()
    t0 = datetime(2026, 8, 2, 10, 0, 0)
    _drive(b, 25, "kite_index_futures", 100_000.0, 100, t0)

    for tf in ("1min", "5min", "15min"):
        df = b.get_candles("NIFTY", tf)
        assert df is not None and not df.empty, f"{tf} produced no candles"
        assert "volume_basis" in df.columns, f"{tf} lost the volume unit in resampling"
        assert Scanner._volume_window_is_homogeneous(df.tail(21)) is True


def test_a_real_feed_failover_trips_the_guard_through_the_builder():
    b = CandleBuilder()
    t0 = datetime(2026, 8, 2, 10, 0, 0)
    _drive(b, 25, "kite_index_futures", 100_000.0, 100, t0)
    _drive(b, 3, "fyers_index", 409_000_000.0, 50_000,
           t0 + timedelta(minutes=25))

    tail = b.get_candles("NIFTY", "1min").tail(21)
    assert Scanner._volume_window_is_homogeneous(tail) is False, (
        "a shares<->contracts failover was not detected end to end"
    )


def test_a_resampled_bucket_spanning_a_failover_is_marked_mixed():
    """A 5min bar built from bars of two different units is itself unusable."""
    b = CandleBuilder()
    t0 = datetime(2026, 8, 2, 10, 0, 0)
    _drive(b, 3, "kite_index_futures", 100_000.0, 100, t0)
    _drive(b, 2, "fyers_index", 409_000_000.0, 50_000, t0 + timedelta(minutes=3))

    df = b.get_candles("NIFTY", "5min")
    assert "MIXED" in {str(v) for v in df["volume_basis"].tolist()}
    assert Scanner._volume_window_is_homogeneous(df) is False


def test_historical_backfill_does_not_permanently_disable_the_surge_gate():
    """NaN is 'unknown', not a third unit.

    float('nan') is truthy, so an `or ""` test let it through as a distinct label —
    marking every window after a backfill mixed, returning None from the thrust metrics,
    and re-killing the 1min signal this guard exists to protect.
    """
    b = CandleBuilder()
    hist = pd.DataFrame(
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 1000.0},
        index=pd.date_range("2026-08-02 09:15", periods=30, freq="1min"),
    )
    b.update_candles("NIFTY", hist, "1min")
    for sec in (0, 20, 40):
        b.update_from_tick("NIFTY", 100.5, 5_000 + sec,
                           datetime(2026, 8, 2, 9, 45, sec), "fyers_index")

    merged = b.get_candles("NIFTY", "1min")
    assert "volume_basis" in merged.columns
    assert Scanner._volume_window_is_homogeneous(merged.tail(21)) is True, (
        "a historical backfill disarmed the surge gate for the rest of the session"
    )


# ───────── Round-6: regressions found auditing the round-5 fixes ─────────

def test_a_source_switch_discards_the_old_units_accumulated_volume():
    """Relabelling a bar without resetting its volume poisons the guard's own input.

    The delta so far was measured in the OLD unit; keeping it while stamping the NEW
    unit hands the surge guard a bar of shares volume labelled as contracts, and max()
    would preserve the larger old figure for the rest of the minute.
    """
    b = CandleBuilder()
    t0 = datetime(2026, 8, 2, 10, 0, 0)
    # Big cash-shares deltas first, then a switch to small futures numbers.
    _ticks(b, "NIFTY", t0, [(24000.0, 409_000_000), (24005.0, 410_000_000)], "fyers_index")
    _ticks(b, "NIFTY", t0 + timedelta(seconds=20),
           [(24010.0, 100_000), (24015.0, 100_540)], "kite_index_futures")

    row = b.get_candles("NIFTY", "1min").iloc[-1]
    assert str(row["volume_basis"]) == "kite_index_futures"
    assert float(row["volume"]) == 540.0, (
        f"bar reports {float(row['volume']):,.0f} in contracts but carries the shares delta"
    )


def test_frozen_fyers_total_does_not_block_the_futures_fallback_forever():
    """A vendor republishing a stale non-zero volume is the 2026-07-27 shape.

    Stamping freshness on every republication made it read as live indefinitely, so the
    fallback never engaged and the candles stayed dry.
    """
    from data.market_data import MarketDataProvider

    p = MarketDataProvider.__new__(MarketDataProvider)
    p._index_cum_volume = {"NIFTY": 409_000_000.0}
    p._index_cum_volume_source = {"NIFTY": "fyers_index"}
    p._index_cum_volume_at = {"NIFTY": 1_000_000.0}      # stamped long ago
    p._index_futures_volume_at = {}
    # Per-index in-flight tracking replaced the single global lock, which starved
    # three of four indices; see test_index_volume_refresh_is_per_index_2026_08_05.
    p._index_volume_refresh_guard = threading.Lock()
    p._index_volume_refresh_inflight = set()
    p.kite = None

    dispatched = []
    p._request_index_volume_refresh = lambda name, ts: dispatched.append(name)

    # 60s later with the SAME frozen total: must be judged stale and trigger a refresh.
    volume, source = p._index_volume_for_tick("NIFTY", 1_000_060.0)
    assert dispatched == ["NIFTY"], "a frozen total was treated as a live feed"


def test_the_tick_path_never_blocks_on_broker_rest():
    """kiteconnect dispatches on_ticks on the socket reader thread.

    A blocking HTTPS round-trip there stalls ticks for every instrument.
    """
    from data.market_data import MarketDataProvider

    p = MarketDataProvider.__new__(MarketDataProvider)
    p._index_cum_volume = {}
    p._index_cum_volume_source = {}
    p._index_cum_volume_at = {}
    p._index_futures_volume_at = {}
    # Per-index in-flight tracking replaced the single global lock, which starved
    # three of four indices; see test_index_volume_refresh_is_per_index_2026_08_05.
    p._index_volume_refresh_guard = threading.Lock()
    p._index_volume_refresh_inflight = set()

    def _slow(symbol):
        time.sleep(2.0)
        return {"source": "kite_index_futures", "volume": 250_000.0}

    p.get_index_activity = _slow
    started = time.monotonic()
    p._index_volume_for_tick("NIFTY", time.time())
    elapsed = time.monotonic() - started
    assert elapsed < 0.5, f"tick path blocked for {elapsed:.2f}s on a broker call"
