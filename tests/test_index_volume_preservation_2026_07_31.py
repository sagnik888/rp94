"""Index volume must survive the Yahoo tertiary fallback (live audit, 2026-07-31).

Index volume died on 2026-07-27 and every 5-min bar since has been zero across all four
indices. Chain: Fyers index history stopped returning current-day candles -> the frame is
judged stale -> Yahoo fallback -> Yahoo reports volume=0 for index tickers -> the code did
a blind `df = yahoo_df`, discarding the real volume it already had.

That matters because Scanner._candle_thrust_metrics computes
`volume_ratio = last/prior_mean if prior_mean > 0 else 0.0` — zero, NOT a neutral 1.0. So
every volume-gated path evaluates False and is silently dead: volume_spike_entry (>=2.5x),
quality_hybrid thrust (>=1.6x), one_way_cascade (>=1.40x), market_ltp_shock and
late_session_reclaim (>=1.10x), the 15m choppy scalp (>=1.05x), and the fast-opposite
thrust EXIT guard.
"""
import pandas as pd
import pytest

from data.market_data import MarketDataProvider

merge = MarketDataProvider._merge_yahoo_preserving_volume


def _frame(vols, start="2026-07-31 09:15", periods=None, price=100.0):
    n = periods or len(vols)
    idx = pd.date_range(start, periods=n, freq="5min")
    return pd.DataFrame(
        {"open": price, "high": price + 1, "low": price - 1, "close": price, "volume": vols},
        index=idx,
    )


def test_real_volume_is_preserved_when_yahoo_reports_zero():
    prior = _frame([1000, 2000, 3000])
    yahoo = _frame([0, 0, 0], price=101.0)
    out = merge(prior, yahoo, "NIFTY")
    assert out["volume"].tolist() == [1000, 2000, 3000], "the exact bug: volume was zeroed"
    # Yahoo still wins on price — that is why we fell back at all.
    assert out["close"].tolist() == [101.0, 101.0, 101.0]


def test_yahoo_volume_wins_when_it_actually_has_some():
    prior = _frame([1000, 2000, 3000])
    yahoo = _frame([11, 22, 33], price=101.0)
    out = merge(prior, yahoo, "NIFTY")
    assert out["volume"].tolist() == [11, 22, 33]


def test_bars_only_yahoo_has_keep_zero_rather_than_inventing_volume():
    prior = _frame([1000, 2000])                       # 09:15, 09:20
    yahoo = _frame([0, 0, 0, 0], price=101.0)          # 09:15..09:30
    out = merge(prior, yahoo, "NIFTY")
    assert out["volume"].tolist() == [1000, 2000, 0, 0]


def test_no_prior_volume_means_nothing_to_preserve():
    assert merge(_frame([0, 0, 0]), _frame([0, 0, 0]), "NIFTY")["volume"].tolist() == [0, 0, 0]
    assert merge(pd.DataFrame(), _frame([0, 0]), "NIFTY")["volume"].tolist() == [0, 0]


def test_survives_missing_or_dirty_volume_columns():
    yahoo_no_vol = _frame([0, 0]).drop(columns=["volume"])
    assert "volume" in merge(_frame([5, 6]), yahoo_no_vol, "NIFTY").columns
    prior_no_vol = _frame([0, 0]).drop(columns=["volume"])
    assert merge(prior_no_vol, _frame([0, 0]), "NIFTY")["volume"].tolist() == [0, 0]
    dirty = _frame([0, 0]); dirty["volume"] = ["x", None]
    assert merge(_frame([7, 8]), dirty, "NIFTY")["volume"].tolist() == [7, 8]


def test_duplicate_prior_timestamps_do_not_explode():
    prior = _frame([1000, 2000, 3000])
    prior = pd.concat([prior, prior.iloc[[0]]])        # duplicated 09:15
    out = merge(prior, _frame([0, 0, 0], price=101.0), "NIFTY")
    assert out["volume"].tolist() == [1000, 2000, 3000]


def test_missing_volume_never_yields_a_usable_ratio():
    """Missing volume must fail CLOSED — it must never read as a neutral 1.0.

    Originally asserted on the source text 'if prior_vol > 0 else 0.0'. That sentinel has
    been replaced by returning None (no metrics) when volume cannot be measured, which
    fails closed identically and is more honest: 0.0 asserts "measured, no surge", while
    None says "cannot measure". The old assertion invited exactly this re-check, so the
    invariant is now pinned by BEHAVIOUR rather than by a string that goes stale.

    Zero-volume bars are additionally excluded from the average rather than counted as
    measurements of zero — averaging them in dragged the denominator down and fabricated
    a 4x-10x ratio out of identical live bars at session open.
    """
    import numpy as np
    import pandas as pd

    from scanner import Scanner

    class _Candles:
        def __init__(self, df):
            self.df = df

        def get_candles(self, instrument, timeframe):
            return self.df

    def metrics(volumes):
        n = len(volumes)
        idx = pd.date_range("2026-07-31 09:15", periods=n, freq="5min")
        base = 24000 + np.arange(n) * 0.5
        df = pd.DataFrame(
            {"open": base, "high": base + 4, "low": base - 4, "close": base + 2,
             "volume": volumes},
            index=idx,
        )
        scanner = Scanner.__new__(Scanner)
        scanner.candles = _Candles(df)
        return Scanner._candle_thrust_metrics(scanner, "NIFTY", "5min", "BUY")

    # The 2026-07-27 shape: Yahoo zeroed the volume for every bar.
    assert metrics([0.0] * 21) is None, "all-zero volume produced a usable ratio"

    # Too few measured bars is also unmeasurable, not a breakout.
    assert metrics([0.0] * 18 + [500.0] * 3) is None

    # And a real, measurable surge must still come through.
    live = metrics([500.0] * 20 + [3000.0])
    assert live is not None and live["volume_ratio"] == pytest.approx(6.0)
