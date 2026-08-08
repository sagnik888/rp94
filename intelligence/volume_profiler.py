"""
Volume Profiler — Intraday Volume Analysis
═══════════════════════════════════════════════════════════════

Tracks:
- Cumulative intraday volume vs previous day
- Volume surge detection (>1.5x average)
- VWAP calculation and deviation
- Buy/sell volume ratio estimation
"""

import pandas as pd
import numpy as np
from typing import Dict, Optional
from loguru import logger
from intelligence.price_action import (
    aroon_context,
    market_structure_profile,
    wavetrend_context,
)


# A final bar holding less than this fraction of the recent median is a STUB — a bar cut
# short by the session ending, the feed dropping, or the process stopping mid-bar — not a
# quiet one. Measured stubs on 2026-08-05 ran at 0.003-0.01 of the median across all four
# indices; an ordinary quiet bar runs at tens of percent. 0.05 sits well clear of both.
STUB_BAR_FRACTION = 0.05

# How many trailing stub bars to step back over before giving up. Index candles carry no
# traded volume on the live tick feed; it arrives only with the periodic Fyers history
# backfill, which lags the tape by a few bars, so the dead zone at the right edge is
# several bars deep rather than one. A few bars of lag is backfill and worth stepping
# over; a longer run is a dead feed and must be reported as absent, not measured.
MAX_STUB_WALKBACK = 4


def volume_window_is_homogeneous(frame) -> bool:
    """False when the window mixes volume UNITS, so no ratio across it is meaningful.

    Index volume switches mid-session between Fyers cash-market shares and the broker's
    near-month FUTURES contracts when Fyers drops. Both are legitimate; they are simply
    not the same quantity, and they differ by roughly 1000x (a measured 0.006 for NIFTY,
    0.001 for SENSEX). Comparing a bar in one against a 20-bar mean of the other
    manufactures a surge out of a feed failover.

    Deliberately mirrors Scanner._volume_window_is_homogeneous rather than importing it:
    intelligence must not depend on scanner. Its NaN rule is copied exactly and was
    learned the hard way — historical backfill rows carry no basis, float('nan') is
    TRUTHY, and treating it as a distinct unit marked every post-backfill window mixed
    and silently killed the surge signal the guard existed to protect. Unknown is not a
    unit. A test asserts the two implementations still agree.
    """
    columns = getattr(frame, "columns", [])
    if "volume_basis" not in columns:
        return True
    try:
        seen = set()
        for value in frame["volume_basis"].tolist():
            text = str(value).strip()
            if not text or text.lower() in {"nan", "none"}:
                continue
            if text == "MIXED":
                return False
            seen.add(text)
    except Exception:
        return True
    return len(seen) <= 1


class VolumeProfiler:
    """Intraday volume analysis engine"""

    def __init__(self):
        self._prev_day_volumes: Dict[str, pd.Series] = {}
        self._today_volumes: Dict[str, float] = {}

    def analyze(self, df: pd.DataFrame, instrument: str = "") -> Dict:
        """
        Analyze volume patterns in OHLCV data.

        Returns:
            Dict with volume metrics and signals
        """
        if df is None or len(df) < 10:
            return self._empty_result()

        try:
            raw_volume = pd.to_numeric(
                df.get('volume', pd.Series(0.0, index=df.index)),
                errors='coerce',
            ).fillna(0)
            close = df['close']
            high = df['high']
            low = df['low']
            if isinstance(df.index, pd.DatetimeIndex):
                active_date = df.index[-1].date()
                active_mask = pd.Series(
                    df.index.date == active_date, index=df.index
                )
            else:
                active_mask = pd.Series(True, index=df.index)
            real_nonzero = raw_volume.where(raw_volume > 0)
            real_nonzero_count = int(real_nonzero.count())
            active_real_nonzero_count = int(
                raw_volume.loc[active_mask][raw_volume.loc[active_mask] > 0].count()
            )

            # Cash-index ticks do not carry traded quantity: zero is a valid
            # upstream absence marker, not a measured volume observation.  When
            # the live 1m candle builder has enough samples, expose relative tick
            # activity as an explicitly labelled proxy.  This gives the operator
            # useful participation telemetry without inventing exchange volume.
            tick_activity = pd.to_numeric(
                df.get('tick_count', pd.Series(0.0, index=df.index)),
                errors='coerce',
            ).fillna(0)
            tick_nonzero = tick_activity.where(tick_activity > 0)
            tick_nonzero_count = int(tick_nonzero.count())
            active_tick_nonzero_count = int(
                tick_activity.loc[active_mask][tick_activity.loc[active_mask] > 0].count()
            )
            is_proxy = (
                active_real_nonzero_count < 5
                and active_tick_nonzero_count >= 5
            )
            if active_real_nonzero_count >= 5:
                volume = raw_volume
                nonzero_volume = real_nonzero
                nonzero_count = active_real_nonzero_count
                metric_source = "EXCHANGE_VOLUME"
            elif is_proxy:
                volume = tick_activity
                nonzero_volume = tick_nonzero
                nonzero_count = active_tick_nonzero_count
                metric_source = "INDEX_TICK_ACTIVITY"
            else:
                volume = raw_volume
                nonzero_volume = real_nonzero
                nonzero_count = active_real_nonzero_count
                metric_source = "UNAVAILABLE"
            current_vol = float(volume.iloc[-1])
            # Historical sessions may provide a comparison baseline, but they
            # cannot qualify today's source.  The latest bar must also contain a
            # positive observation; otherwise 0.0x is a data gap, not a neutral
            # market measurement.
            #
            # "Latest" means the last bar that has actually had a chance to trade.
            # Index volume is a CUMULATIVE total and the candle stores the delta, so a
            # bar that is still forming legitimately reads 0 for the first ticks of every
            # minute. Testing only volume.iloc[-1] therefore reported the whole feed
            # unavailable for part of EVERY minute — which is what showed zero on all four
            # indices in the intel panel while the candles behind it held 700k+ of real
            # volume, and what starved both the confidence score and the exit volume test.
            #
            # The dead-feed case this guard exists for is still caught: nonzero_count >= 5
            # requires five measured bars in the active session, and a genuinely dead feed
            # has none. Looking one bar back only forgives the forming bar.
            # Measure on the last bar that has actually FINISHED.
            #
            # The final bar is still forming unless a full interval has elapsed since it
            # opened, and a partial bar compared against an average of complete bars
            # understates the ratio by exactly the fraction of the bar that has not
            # happened yet. Observed live: SENSEX 51 seconds into a 60-second bar read
            # 3,112 against a 48,034 predecessor -- 0.09x on an ordinary tape, which the
            # panel renders as "0.0x" after .toFixed(1). Every volume gate saw that same
            # 0.09x and refused (2.5x power move, 1.6x thrust, 1.40x cascade, 1.05x scalp).
            #
            # An empty final bar is the same problem at its limit, so the zero check stays
            # as a fallback for frames whose index carries no usable interval.
            recent_vol = current_vol
            measured_index = -1
            if len(volume) >= 2:
                forming = False
                try:
                    interval = volume.index[-1] - volume.index[-2]
                    now_ts = pd.Timestamp.now(tz=volume.index.tz)
                    forming = (now_ts - volume.index[-1]) < interval
                except (TypeError, ValueError, AttributeError):
                    forming = False
                # The clock test only catches a bar that is forming RIGHT NOW. A final bar
                # can also be truncated and STALE: the session ended part-way through it,
                # the feed dropped, or the process was stopped mid-bar. Hours later the
                # clock says "long since closed" and the stub gets measured as if complete.
                #
                # Captured live on 2026-08-05 at 19:12, on all four indices at once —
                #   NIFTY      tail=[..., 14980267, 13736088, 40189]  ratio 0.008x
                #   BANKNIFTY  tail=[...,  6512759,  8209167, 15712]  ratio 0.005x
                #   SENSEX     tail=[...,   210651,  1195852,  1792]  ratio 0.010x
                #   MIDCPNIFTY tail=[...,  6854073,  8315127, 14961]  ratio 0.005x
                # — the final bar holding a few tenths of a percent of its predecessors,
                # which the panel renders as "0.0x" and every volume gate refuses. This is
                # the zero-volume report that was chased for three sessions.
                #
                # So completeness is judged on the DATA as well as the clock: a final bar
                # holding a tiny fraction of the recent median is a stub, whatever the time
                # says. The threshold is deliberately low — a genuinely quiet bar runs at
                # tens of percent of the median, not at one.
                # The dead zone is not one bar deep. Index candles carry NO traded volume
                # on the live tick feed — it arrives only with the periodic Fyers history
                # backfill ("Prioritizing Fyers history for ... to get volume"), which lags
                # the tape. Captured live on 2026-08-07 at 10:15, 1min frame, all four
                # indices at once:
                #   NIFTY tail=[515102, 478060, 210220, 6760, 5655, 0]  avg=495,263
                # — bars -1, -2 AND -3 are all stubs. Stepping back a single bar lands
                # still inside the zone and measures 5,655 as if complete: 0.011x, which
                # the panel renders "0.0x". That reading is NOT inert downstream: it
                # reports available=True, so the volume pillar scores a phantom "no
                # participation" into every candidate instead of abstaining.
                #
                # So step back until a bar is genuinely complete, and judge each candidate
                # by the same rule rather than trusting the one behind a stub.
                def _stub_at(pos: int) -> bool:
                    """True when the bar at `pos` cannot be a complete observation."""
                    try:
                        bar_vol = float(volume.iloc[pos])
                    except (IndexError, TypeError, ValueError):
                        return False
                    if bar_vol <= 0:
                        return True
                    try:
                        prior = volume.iloc[pos - 6:pos]
                        prior = prior[prior > 0]
                        if len(prior) < 3:
                            return False
                        median_prior = float(prior.median())
                    except (IndexError, TypeError, ValueError):
                        return False
                    return median_prior > 0 and bar_vol < STUB_BAR_FRACTION * median_prior

                measured_index = None
                for step in range(MAX_STUB_WALKBACK + 1):
                    pos = -1 - step
                    if len(volume) + pos < 0:
                        break
                    if (step == 0 and forming) or _stub_at(pos):
                        continue
                    measured_index = pos
                    recent_vol = float(volume.iloc[pos])
                    break
                if measured_index is None:
                    # Nothing complete within reach. Report absence so the pillar abstains;
                    # a phantom 0.0x that claims to be a measurement is strictly worse.
                    measured_index = -1
                    recent_vol = 0.0
            volume_available = bool(nonzero_count >= 5 and recent_vol > 0)
            # Every RATIO below is measured against recent_vol for the same reason. Using
            # the forming bar made vol_ratio 0.0/avg = 0.0x, which is what the intel panel
            # displayed on all four indices while the candles held real volume -- and 0.0x
            # is not a neutral reading downstream: it fails every surge gate outright
            # (2.5x power move, 1.6x thrust, 1.40x cascade, 1.05-1.10x scalps) and drags
            # the volume component of the confidence score to its floor.
            measured_vol = recent_vol if recent_vol > 0 else current_vol
            volume_for_stats = volume.loc[active_mask]

            # ── Intraday Daily VWAP (Resets every day at 09:15) ──
            df_copy = df.copy()
            df_copy['volume'] = volume
            df_copy['date'] = df_copy.index.date
            # Group by date to calculate daily cumulative values
            grouped = df_copy.groupby('date')
            
            typical_price = (high + low + close) / 3
            cum_vol = grouped['volume'].cumsum()
            cum_pv = (typical_price * df_copy['volume']).groupby(df_copy.index.date).cumsum()
            vwap = cum_pv / cum_vol
            vwap_current = float(vwap.iloc[-1]) if not pd.isna(vwap.iloc[-1]) else 0
            vwap_deviation = ((close.iloc[-1] - vwap_current) / vwap_current * 100) if vwap_current > 0 else 0

            # ── Volume Moving Average ──
            # Anchored to the bar the ratio is MEASURED on. Taking the average at the
            # last index while the numerator came from the one before it compares a bar
            # against a window that includes the next bar's empty slot, which quietly
            # inflates the ratio for part of every minute.
            current_idx = volume.index[measured_index]

            vol_ma = volume_for_stats.rolling(20, min_periods=5).mean()
            avg_at_current = vol_ma.loc[current_idx] if current_idx in vol_ma.index else vol_ma.iloc[-1]
            if pd.isna(avg_at_current) or avg_at_current <= 0:
                fallback_avg = volume_for_stats.tail(20).mean()
                avg_vol = float(fallback_avg) if not pd.isna(fallback_avg) and fallback_avg > 0 else max(measured_vol, 1.0)
            else:
                avg_vol = float(avg_at_current)

            vol_ratio = measured_vol / avg_vol if avg_vol > 0 else 1.0

            tod_avg = 0.0
            relative_volume_ratio = 1.0
            volume_confidence = 50
            if isinstance(df.index, pd.DatetimeIndex) and len(df) >= 30:
                last_ts = current_idx
                same_slot = df[(df.index.time == last_ts.time()) & (df.index.date != last_ts.date())]
                if len(same_slot) >= 2:
                    same_slot_column = 'tick_count' if is_proxy else 'volume'
                    same_slot_volume = pd.to_numeric(
                        same_slot.get(same_slot_column, pd.Series(0.0, index=same_slot.index)),
                        errors='coerce',
                    ).fillna(0)
                    same_slot_nonzero = same_slot_volume[same_slot_volume > 0]
                    tod_source = same_slot_nonzero if len(same_slot_nonzero) >= 2 else same_slot_volume
                    tod_avg = float(tod_source.tail(10).mean())
                    if tod_avg > 0:
                        relative_volume_ratio = measured_vol / tod_avg
                        volume_confidence = 65 if is_proxy else 75

            # ── Volume Surge Detection ──
            tod_confident = volume_confidence >= (65 if is_proxy else 75) and tod_avg > 0
            effective_ratio = max(vol_ratio, relative_volume_ratio if tod_confident else 1.0)
            display_ratio = relative_volume_ratio if tod_confident else vol_ratio
            ratio_basis = "TIME_OF_DAY" if tod_confident else "ROLLING_AVG"
            if is_proxy:
                ratio_basis = f"TICK_{ratio_basis}"
            # ── diagnostic for an implausibly low ratio ──
            # A live feed reading near-zero is the symptom that has now been chased three
            # times from stale CSV dumps, whose trailing rows are always partial and so
            # cannot show what the running process actually saw. Emit the profiler's real
            # INPUTS when the ratio is low enough to fail every gate, throttled per
            # instrument so it cannot flood a session.
            if volume_available and vol_ratio < 0.25:
                marker = f"{instrument}|{int(now_bucket)}" if (now_bucket := len(volume) // 5) else instrument
                if getattr(self, "_last_low_ratio_marker", None) != marker:
                    self._last_low_ratio_marker = marker
                    try:
                        tail = [round(float(x)) for x in volume.iloc[-6:]]
                        stamps = [str(t)[-8:] for t in volume.index[-3:]]
                    except Exception:
                        tail, stamps = [], []
                    logger.warning(
                        f"VOLUME RATIO LOW {instrument}: ratio={vol_ratio:.3f}x "
                        f"measured_bar={measured_index} measured_vol={measured_vol:,.0f} "
                        f"last_bar={current_vol:,.0f} avg={avg_vol:,.0f} "
                        f"nonzero={nonzero_count} src={metric_source} "
                        f"tail={tail} stamps={stamps}"
                    )

            # The walk-back above judges each bar against its immediate NEIGHBOURS, which
            # is what identifies a stub at the right edge of an otherwise healthy series.
            # It cannot identify a feed that has been dead for a while: by then the
            # neighbours are dead too, the local median collapses, and a dribble of 30
            # against a 300,000 average looks locally consistent and passes as real.
            #
            # So make the final call against the AVERAGE the ratio is actually reported
            # on. A bar under a twentieth of its own 20-bar mean is not a quiet bar — this
            # module's own threshold says quiet runs at tens of percent — it is an absence
            # wearing a number. Say so, and let the aggregator's volume pillar abstain
            # instead of scoring a phantom into every candidate.
            if volume_available and vol_ratio < STUB_BAR_FRACTION:
                volume_available = False

            # A ratio across MIXED UNITS is not a measurement. On a Fyers recovery the
            # numerator flips from futures CONTRACTS back to cash SHARES while the 20-bar
            # mean is still contracts, so vol_ratio reads in the hundreds: is_surge fires,
            # surge_level goes EXTREME, and +/-0.20 of intelligence score is injected in
            # whatever direction the last bar happened to close. Two homogeneity guards
            # already existed for exactly this and neither was wired to the pillar that
            # feeds the score.
            if volume_available and not volume_window_is_homogeneous(df):
                volume_available = False
                is_proxy = False
                metric_source = "MIXED_UNITS"

            is_surge = bool(volume_available and effective_ratio > 1.5)
            surge_level = "EXTREME" if effective_ratio > 3.0 else ("HIGH" if effective_ratio > 2.0 else ("MODERATE" if effective_ratio > 1.5 else "NORMAL"))
            if not volume_available:
                surge_level = "NORMAL"
                effective_ratio = 1.0
                display_ratio = 1.0
                vol_ratio = 1.0
                relative_volume_ratio = 1.0
                ratio_basis = "INSUFFICIENT_CURRENT_SESSION"
                volume_confidence = 0

            # ── Cumulative Volume Today ──
            total_volume = float(volume.loc[active_mask].sum())

            # ── Volume Trend (last 10 bars) ──
            trend_volume = volume_for_stats.dropna()
            if len(trend_volume) >= 10:
                recent_avg = float(trend_volume.iloc[-5:].mean())
                prior_avg = float(trend_volume.iloc[-10:-5].mean())
                vol_trend = "INCREASING" if recent_avg > prior_avg * 1.2 else (
                    "DECREASING" if recent_avg < prior_avg * 0.8 else "STABLE"
                )
            else:
                vol_trend = "INSUFFICIENT"

            # ── Buy/Sell Volume Estimation ──
            # Approximate using close position within high-low range
            close_position = (close - low) / (high - low)
            close_position = close_position.fillna(0.5)
            buy_volume = (volume.loc[active_mask] * close_position.loc[active_mask]).sum()
            sell_volume = (
                volume.loc[active_mask] * (1 - close_position.loc[active_mask])
            ).sum()
            buy_sell_ratio = (
                float(buy_volume / sell_volume)
                if volume_available and sell_volume > 0
                else 1.0
            )

            # ── Signal Generation ──
            signal = "NEUTRAL"
            if is_surge:
                if close.iloc[-1] > close.iloc[-2]:
                    signal = "BULLISH_VOLUME"
                else:
                    signal = "BEARISH_VOLUME"

            score = 50
            if signal == "BULLISH_VOLUME":
                score += 20
            elif signal == "BEARISH_VOLUME":
                score -= 20
            if buy_sell_ratio > 1.5:
                score += 15
            elif buy_sell_ratio < 0.6:
                score -= 15
            if effective_ratio > 2.0:
                score += 5 if signal == "BULLISH_VOLUME" else -5

            # ── Market Structure Profile v2 + Aroon/WaveTrend telemetry ──
            # These are context layers. They can slightly refine the volume score,
            # but they do not create a standalone trade signal.
            profile = market_structure_profile(df_copy, lookback=200, bins=40)
            aroon = aroon_context(high.to_numpy(dtype=float), low.to_numpy(dtype=float))
            wavetrend = wavetrend_context(
                high.to_numpy(dtype=float),
                low.to_numpy(dtype=float),
                close.to_numpy(dtype=float),
            )
            profile_signal = str((profile or {}).get("signal") or "")
            if profile_signal in {"ACCEPTING_HIGHER", "UPSIDE_ACCEPTANCE"}:
                score += 5
            elif profile_signal in {"ACCEPTING_LOWER", "DOWNSIDE_ACCEPTANCE"}:
                score -= 5
            elif profile_signal == "REJECTING_HIGHER":
                score -= 3
            elif profile_signal == "REJECTING_LOWER":
                score += 3

            aroon_trend = str((aroon or {}).get("trend") or "")
            if aroon_trend == "UP" and not bool((aroon or {}).get("late_trend_caution")):
                score += 3
            elif aroon_trend == "DOWN" and not bool((aroon or {}).get("late_trend_caution")):
                score -= 3
            score = int(max(0, min(100, score)))

            return {
                "vwap": round(vwap_current, 2),
                "vwap_deviation_pct": round(float(vwap_deviation), 2),
                "current_volume": int(measured_vol),
                "avg_volume": int(avg_vol),
                "volume_ratio": round(vol_ratio, 2),
                "relative_volume_ratio": round(relative_volume_ratio, 2),
                "display_volume_ratio": round(display_ratio, 2),
                "effective_volume_ratio": round(effective_ratio, 2),
                "volume_ratio_basis": ratio_basis,
                "time_of_day_avg_volume": int(tod_avg),
                "volume_confidence": volume_confidence,
                "available": volume_available,
                "status": (
                    ("PROXY" if is_proxy else "OK")
                    if volume_available
                    else "UNAVAILABLE"
                ),
                "reason": (
                    "Index traded volume unavailable; using relative 1m tick activity"
                    if is_proxy and volume_available
                    else (
                        ""
                        if volume_available
                        # `source` collapses to UNAVAILABLE for every cause and other
                        # code keys off that marker, so the specific cause is carried
                        # here instead. A mixed-unit window is a very different problem
                        # from a dead feed and the operator should be able to tell them
                        # apart.
                        else (
                            "Volume window mixes units (cash shares and futures "
                            "contracts); no ratio across it is meaningful"
                            if metric_source == "MIXED_UNITS"
                            else "Current-session OHLCV/tick activity is missing, zero, or insufficient"
                        )
                    )
                ),
                "source": metric_source if volume_available else "UNAVAILABLE",
                "metric_kind": "TICK_ACTIVITY" if is_proxy else "TRADED_VOLUME",
                "is_proxy": bool(is_proxy and volume_available),
                "sample_count": int(len(df)),
                "nonzero_bar_count": nonzero_count,
                "real_volume_nonzero_bar_count": real_nonzero_count,
                "tick_activity_nonzero_bar_count": tick_nonzero_count,
                "active_session_real_volume_nonzero_bar_count": active_real_nonzero_count,
                "active_session_tick_activity_nonzero_bar_count": active_tick_nonzero_count,
                "is_surge": is_surge,
                "surge_level": surge_level,
                "total_volume": int(total_volume),
                "volume_trend": vol_trend,
                "buy_sell_ratio": round(buy_sell_ratio, 2),
                "buy_pct": round(buy_sell_ratio / (1 + buy_sell_ratio) * 100, 1),
                "market_structure_profile": profile or {},
                "aroon": aroon or {"available": False},
                "wavetrend": wavetrend or {"available": False},
                "signal": signal,
                "score": score,
            }

        except Exception as e:
            logger.error(f"Volume analysis error: {e}")
            return self._empty_result()

    def _empty_result(self) -> Dict:
        return {
            "vwap": 0, "vwap_deviation_pct": 0, "current_volume": 0,
            "available": False, "status": "UNAVAILABLE", "reason": "Insufficient OHLCV bars",
            "source": "UNAVAILABLE", "metric_kind": "TRADED_VOLUME", "is_proxy": False,
            "sample_count": 0, "nonzero_bar_count": 0,
            "real_volume_nonzero_bar_count": 0, "tick_activity_nonzero_bar_count": 0,
            "avg_volume": 0, "volume_ratio": 1.0, "relative_volume_ratio": 1.0,
            "display_volume_ratio": 1.0, "effective_volume_ratio": 1.0,
            "volume_ratio_basis": "INSUFFICIENT",
            "time_of_day_avg_volume": 0, "volume_confidence": 0, "is_surge": False,
            "surge_level": "NORMAL", "total_volume": 0, "volume_trend": "INSUFFICIENT",
            "buy_sell_ratio": 1.0, "buy_pct": 50.0,
            "market_structure_profile": {}, "aroon": {"available": False},
            "wavetrend": {"available": False},
            "signal": "NEUTRAL", "score": 50,
        }
