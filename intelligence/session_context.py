"""Session-level intraday context for index intelligence.

The scanner already has the raw candles needed to describe the trading day.
This module turns that stream into compact, testable market context:
gap, opening range, VWAP behavior, day type, and session cumulative delta.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import pandas as pd


class SessionContextAnalyzer:
    """Build a data-driven day hypothesis from intraday candles."""

    def analyze(
        self,
        candle_df: Optional[pd.DataFrame],
        *,
        instrument: str = "",
        spot_price: float = 0.0,
        previous_close: float = 0.0,
        vix_value: Optional[float] = None,
        vix_open: Optional[float] = None,
    ) -> Dict[str, Any]:
        if candle_df is None or candle_df.empty or not isinstance(candle_df.index, pd.DatetimeIndex):
            return self._empty(instrument, "NO_CANDLES")

        try:
            df = candle_df.copy().sort_index()
            required = {"open", "high", "low", "close"}
            if not required.issubset(set(df.columns)):
                return self._empty(instrument, "MISSING_OHLC")

            session_date = max(df.index.date)
            dates = pd.Series(df.index.date, index=df.index)
            session = df[dates == session_date].copy()
            prior = df[dates < session_date].copy()
            if session.empty:
                return self._empty(instrument, "NO_SESSION_CANDLES")

            open_price = self._safe_float(session["open"].iloc[0])
            close_price = self._safe_float(spot_price) or self._safe_float(session["close"].iloc[-1])
            high_price = self._safe_float(session["high"].max())
            low_price = self._safe_float(session["low"].min())
            day_range = max(0.0, high_price - low_price)
            # A small *absolute* session range is a different risk condition from
            # a low range versus the ten-day average.  Keep this independently
            # observable so entry/sizing policy never has to infer it from a
            # historical statistic.
            reference_price = open_price or close_price
            reference_source = "SESSION_OPEN" if open_price > 0 else "SESSION_CLOSE_FALLBACK"
            session_range_pct = (day_range / reference_price * 100.0) if reference_price > 0 else 0.0
            compression_threshold_pct = 0.20

            pdc = self._safe_float(previous_close)
            if pdc <= 0 and not prior.empty:
                pdc = self._safe_float(prior["close"].iloc[-1])
            pdh = self._safe_float(prior["high"].max()) if not prior.empty else 0.0
            pdl = self._safe_float(prior["low"].min()) if not prior.empty else 0.0

            gap_pct = ((open_price - pdc) / pdc * 100.0) if pdc > 0 and open_price > 0 else 0.0
            gap_state = self._gap_fill_state(gap_pct, pdc, high_price, low_price)

            opening = session.iloc[: min(len(session), 6)]
            opening_high = self._safe_float(opening["high"].max()) if not opening.empty else 0.0
            opening_low = self._safe_float(opening["low"].min()) if not opening.empty else 0.0
            opening_range = max(0.0, opening_high - opening_low)
            opening_status = "FORMING"
            if len(session) >= 6 and opening_range > 0:
                if close_price > opening_high:
                    opening_status = "BREAK_UP"
                elif close_price < opening_low:
                    opening_status = "BREAK_DOWN"
                else:
                    opening_status = "INSIDE"

            typical = (session["high"] + session["low"] + session["close"]) / 3.0
            volume = pd.to_numeric(session.get("volume", pd.Series(0, index=session.index)), errors="coerce").fillna(0.0)
            if float(volume.sum()) > 0:
                vwap_series = (typical * volume).cumsum() / volume.cumsum().replace(0.0, pd.NA)
                vwap_source = "volume"
            else:
                vwap_series = typical.expanding().mean()
                vwap_source = "price_fallback"
            vwap_series = vwap_series.ffill().fillna(typical)
            vwap = self._safe_float(vwap_series.iloc[-1])
            vwap_dev_pct = ((close_price - vwap) / vwap * 100.0) if vwap > 0 else 0.0
            above_vwap = (session["close"] >= vwap_series).astype(int)
            above_ratio = float(above_vwap.mean()) if len(above_vwap) else 0.5
            vwap_cross_count = int((above_vwap.diff().abs().fillna(0) > 0).sum())

            avg_range = self._average_prior_day_range(df, session_date)
            range_ratio = (day_range / avg_range) if avg_range > 0 else 0.0

            delta_ctx = self._session_cumulative_delta(session)
            day_type, bias, confidence = self._classify_day(
                bars=len(session),
                gap_pct=gap_pct,
                opening_status=opening_status,
                above_vwap_ratio=above_ratio,
                vwap_cross_count=vwap_cross_count,
                range_ratio=range_ratio,
                close_price=close_price,
                vwap=vwap,
                delta_bias=str(delta_ctx.get("bias", "NEUTRAL")),
            )

            # Multi-horizon moods: pulse (~1–5m), 15m, 1h, plus full-day label.
            bar_minutes = self._infer_bar_minutes(session)
            horizons = self._mood_horizons(session, bar_minutes=bar_minutes)
            # Primary rolling mood = hour pocket (or richest available).
            mood = dict(horizons.get("hour") or horizons.get("quarter") or horizons.get("minute") or {})
            if not mood:
                mood = self._rolling_intraday_mood(session, lookback_bars=12)
            # Effective phase for guards: prefer rolling mood when it disagrees
            # with a stale opening day-type at sufficient confidence.
            effective_day_type = day_type
            effective_bias = bias
            effective_conf = confidence
            mood_type = str(mood.get("day_type") or "")
            mood_bias = str(mood.get("bias") or "NEUTRAL")
            mood_conf = float(mood.get("confidence") or 0.0)
            if (
                mood_type
                and mood_type not in {"UNDECIDED", ""}
                and mood_conf >= 0.55
                and (
                    (day_type.startswith("TREND_DAY") and mood_type != day_type)
                    or (day_type == "RANGE_DAY" and mood_type.startswith("TREND_DAY"))
                    or mood_type in {"MOOD_TRANSITION", "MOOD_DUMP", "MOOD_RALLY"}
                )
            ):
                effective_day_type = mood_type if mood_type.startswith("TREND") else (
                    "TREND_DAY_DOWN" if mood_bias == "BEARISH" else (
                        "TREND_DAY_UP" if mood_bias == "BULLISH" else "RANGE_DAY"
                    )
                )
                effective_bias = mood_bias
                effective_conf = max(0.55, min(0.88, mood_conf))

            vix_change_pct = 0.0
            if vix_open and vix_value and float(vix_open) > 0 and float(vix_value) > 0:
                vix_change_pct = (float(vix_value) - float(vix_open)) / float(vix_open) * 100.0

            day_mood = {
                "day_type": day_type,
                "bias": bias,
                "confidence": round(confidence, 3),
                "horizon": "day",
                "lookback_minutes": None,
            }
            horizons_out = {
                "day": day_mood,
                "minute": horizons.get("minute") or {},
                "quarter": horizons.get("quarter") or {},
                "hour": horizons.get("hour") or {},
            }

            return {
                "instrument": str(instrument or "").upper(),
                "session_date": str(session_date),
                "bars": int(len(session)),
                "bar_minutes": round(float(bar_minutes), 3),
                # Full-session opening classification (compatibility)
                "day_type": day_type,
                "bias": bias,
                "confidence": round(confidence, 3),
                "opening_day_type": day_type,
                "opening_bias": bias,
                # Dynamic phase used by late-session / shock guards
                "intraday_mood": mood,
                "mood_horizons": horizons_out,
                "intraday_phase": effective_day_type,
                "market_phase": effective_day_type,
                "phase_confidence": round(effective_conf, 3),
                "effective_day_type": effective_day_type,
                "effective_bias": effective_bias,
                "gap_pct": round(gap_pct, 3),
                "gap_fill_state": gap_state,
                "previous_close": round(pdc, 2) if pdc > 0 else 0.0,
                "pdh": round(pdh, 2) if pdh > 0 else 0.0,
                "pdl": round(pdl, 2) if pdl > 0 else 0.0,
                "distance_from_pdh_pct": self._distance_pct(close_price, pdh),
                "distance_from_pdl_pct": self._distance_pct(close_price, pdl),
                "distance_from_pdc_pct": self._distance_pct(close_price, pdc),
                "opening_range_high": round(opening_high, 2),
                "opening_range_low": round(opening_low, 2),
                "opening_range_points": round(opening_range, 2),
                "opening_range_pct": round((opening_range / open_price * 100.0), 3) if open_price > 0 else 0.0,
                "opening_range_status": opening_status,
                "vwap": round(vwap, 2),
                "vwap_source": vwap_source,
                "vwap_deviation_pct": round(vwap_dev_pct, 3),
                "vwap_cross_count": vwap_cross_count,
                "close_above_vwap_pct": round(above_ratio * 100.0, 1),
                "day_range_points": round(day_range, 2),
                "session_high": round(high_price, 2),
                "session_low": round(low_price, 2),
                "session_reference_price": round(reference_price, 2),
                "session_reference_source": reference_source,
                "session_range_pct": round(session_range_pct, 4),
                "compression_threshold_pct": compression_threshold_pct,
                "is_session_compressed": bool(
                    reference_price > 0
                    and session_range_pct <= compression_threshold_pct + 1e-9
                ),
                "avg_10day_range_points": round(avg_range, 2),
                "range_vs_10day": round(range_ratio, 3),
                "session_delta": delta_ctx,
                "vix_value": round(float(vix_value), 3) if vix_value else None,
                "vix_open": round(float(vix_open), 3) if vix_open else None,
                "vix_change_pct": round(vix_change_pct, 3),
            }
        except Exception as exc:
            return self._empty(instrument, f"ERROR: {exc}")

    @staticmethod
    def _safe_float(value: Any) -> float:
        try:
            if pd.isna(value):
                return 0.0
            return float(value)
        except Exception:
            return 0.0

    @staticmethod
    def _distance_pct(price: float, level: float) -> float:
        if price <= 0 or level <= 0:
            return 0.0
        return round((price - level) / level * 100.0, 3)

    @staticmethod
    def _gap_fill_state(gap_pct: float, pdc: float, high_price: float, low_price: float) -> str:
        if pdc <= 0 or abs(gap_pct) < 0.05:
            return "NO_GAP"
        if gap_pct > 0:
            return "FILLED" if low_price <= pdc else "OPEN_UP_GAP"
        return "FILLED" if high_price >= pdc else "OPEN_DOWN_GAP"

    @staticmethod
    def _average_prior_day_range(df: pd.DataFrame, session_date) -> float:
        try:
            prior = df[pd.Series(df.index.date, index=df.index) < session_date]
            if prior.empty:
                return 0.0
            daily = prior.groupby(prior.index.date).agg({"high": "max", "low": "min"})
            ranges = (daily["high"] - daily["low"]).dropna()
            ranges = ranges[ranges > 0].tail(10)
            return float(ranges.mean()) if len(ranges) else 0.0
        except Exception:
            return 0.0

    def _session_cumulative_delta(self, session: pd.DataFrame) -> Dict[str, Any]:
        try:
            high = pd.to_numeric(session["high"], errors="coerce")
            low = pd.to_numeric(session["low"], errors="coerce")
            close = pd.to_numeric(session["close"], errors="coerce")
            rng = (high - low).replace(0.0, pd.NA)
            close_pos = ((close - low) / rng).clip(0.0, 1.0).fillna(0.5)
            volume = pd.to_numeric(session.get("volume", pd.Series(0, index=session.index)), errors="coerce").fillna(0.0)
            if float(volume.sum()) <= 0:
                volume = pd.Series(1.0, index=session.index)
                source = "bar_pressure"
            else:
                source = "volume"
            delta = (close_pos - 0.5) * 2.0 * volume
            total_weight = float(volume.sum())
            cum_delta = float(delta.sum())
            delta_ratio = (cum_delta / total_weight) if total_weight > 0 else 0.0
            if delta_ratio > 0.15:
                bias = "BULLISH"
            elif delta_ratio < -0.15:
                bias = "BEARISH"
            else:
                bias = "NEUTRAL"
            return {
                "source": source,
                "cumulative_delta": round(cum_delta, 2),
                "delta_ratio": round(delta_ratio, 3),
                "bias": bias,
            }
        except Exception:
            return {
                "source": "unavailable",
                "cumulative_delta": 0.0,
                "delta_ratio": 0.0,
                "bias": "NEUTRAL",
            }

    @staticmethod
    def _classify_day(
        *,
        bars: int,
        gap_pct: float,
        opening_status: str,
        above_vwap_ratio: float,
        vwap_cross_count: int,
        range_ratio: float,
        close_price: float,
        vwap: float,
        delta_bias: str,
    ) -> tuple[str, str, float]:
        if bars < 6:
            return "UNDECIDED", "NEUTRAL", 0.25

        close_above = close_price >= vwap if vwap > 0 else above_vwap_ratio >= 0.5
        delta_bias = str(delta_bias or "NEUTRAL").upper()
        volatile_open = bars <= 12 and abs(gap_pct) >= 0.25 and range_ratio >= 0.35

        if (
            opening_status == "BREAK_UP"
            and above_vwap_ratio >= 0.68
            and vwap_cross_count <= 2
            and close_above
        ):
            confidence = 0.72 + min(0.18, max(0.0, range_ratio - 0.5) * 0.15)
            if delta_bias == "BULLISH":
                confidence += 0.05
            return "TREND_DAY_UP", "BULLISH", min(0.92, confidence)

        if (
            opening_status == "BREAK_DOWN"
            and above_vwap_ratio <= 0.32
            and vwap_cross_count <= 2
            and not close_above
        ):
            confidence = 0.72 + min(0.18, max(0.0, range_ratio - 0.5) * 0.15)
            if delta_bias == "BEARISH":
                confidence += 0.05
            return "TREND_DAY_DOWN", "BEARISH", min(0.92, confidence)

        if volatile_open:
            bias = "BULLISH" if above_vwap_ratio > 0.58 else ("BEARISH" if above_vwap_ratio < 0.42 else "NEUTRAL")
            return "VOLATILE_OPEN", bias, 0.55

        if vwap_cross_count >= 3 or 0.35 <= above_vwap_ratio <= 0.65:
            return "RANGE_DAY", "NEUTRAL", 0.62

        bias = "BULLISH" if above_vwap_ratio > 0.58 else ("BEARISH" if above_vwap_ratio < 0.42 else "NEUTRAL")
        return "UNDECIDED", bias, 0.4

    @staticmethod
    def _infer_bar_minutes(session: pd.DataFrame) -> float:
        try:
            if session is None or len(session) < 2:
                return 5.0
            delta = (session.index[-1] - session.index[-2]).total_seconds() / 60.0
            if delta > 0:
                return float(delta)
        except Exception:
            pass
        return 5.0

    def _mood_horizons(self, session: pd.DataFrame, *, bar_minutes: float = 5.0) -> Dict[str, Any]:
        """Build minute / 15m / 1h mood pockets from the active bar stream."""
        bm = max(0.5, float(bar_minutes or 5.0))

        def bars_for(minutes: float) -> int:
            return max(1, int(round(float(minutes) / bm)))

        # Pulse ≈ 1–5 minutes of tape; quarter ≈ 15m; hour ≈ 60m.
        specs = {
            "minute": max(1, min(5, bars_for(max(1.0, min(5.0, bm))))),
            "quarter": bars_for(15.0),
            "hour": bars_for(60.0),
        }
        out: Dict[str, Any] = {}
        for name, n_bars in specs.items():
            m = self._rolling_intraday_mood(
                session,
                lookback_bars=n_bars,
                min_bars=1 if name == "minute" else 2,
            )
            m = dict(m or {})
            m["horizon"] = name
            m["lookback_minutes"] = round(n_bars * bm, 2)
            out[name] = m
        return out

    def _rolling_intraday_mood(
        self,
        session: pd.DataFrame,
        *,
        lookback_bars: int = 12,
        min_bars: int = 2,
    ) -> Dict[str, Any]:
        """Classify the *current* intraday pocket from the last N bars.

        Full-day day_type can stay TREND_DAY_UP after a late dump. Guards that
        need a live phase should read this mood (or effective_day_type derived
        from it) so the panel flips every few bars rather than locking one label
        for the whole session.
        """
        empty = {
            "day_type": "UNDECIDED",
            "bias": "NEUTRAL",
            "confidence": 0.0,
            "move_pct": 0.0,
            "lookback_bars": 0,
            "volume_ratio": 0.0,
            "close_location": 0.5,
        }
        try:
            if session is None or session.empty:
                return empty
            floor = max(1, int(min_bars or 1))
            n = min(int(len(session)), max(floor, int(lookback_bars or 12)))
            if n < 1:
                return empty
            window = session.iloc[-n:]
            open0 = self._safe_float(window["open"].iloc[0])
            close1 = self._safe_float(window["close"].iloc[-1])
            high = self._safe_float(window["high"].max())
            low = self._safe_float(window["low"].min())
            if open0 <= 0 or close1 <= 0:
                return empty
            move_pct = (close1 - open0) / open0 * 100.0
            rng = max(0.0, high - low)
            close_loc = ((close1 - low) / rng) if rng > 0 else 0.5

            # Recent half vs first half of the window — detects accelerating dump/rally.
            half = max(1, n // 2)
            first_half = window.iloc[:half]
            second_half = window.iloc[-half:]
            first_mid = self._safe_float(first_half["close"].mean())
            second_mid = self._safe_float(second_half["close"].mean())
            accel_pct = (
                ((second_mid - first_mid) / first_mid * 100.0) if first_mid > 0 else 0.0
            )

            vol = pd.to_numeric(
                window.get("volume", pd.Series(0.0, index=window.index)),
                errors="coerce",
            ).fillna(0.0)
            prior = session.iloc[max(0, len(session) - n * 3) : max(0, len(session) - n)]
            prior_vol = pd.to_numeric(
                prior.get("volume", pd.Series(0.0, index=prior.index)) if not prior.empty else pd.Series(dtype=float),
                errors="coerce",
            ).fillna(0.0)
            avg_recent = float(vol.mean()) if len(vol) else 0.0
            avg_prior = float(prior_vol.mean()) if len(prior_vol) else 0.0
            volume_ratio = (avg_recent / avg_prior) if avg_prior > 0 else (1.0 if avg_recent > 0 else 0.0)

            # Last 3 bars short shock (≈15m on 5min bars, ≈3m on 1min).
            short_n = min(n, 3)
            short = window.iloc[-short_n:]
            short_open = self._safe_float(short["open"].iloc[0])
            short_close = self._safe_float(short["close"].iloc[-1])
            short_move = (
                ((short_close - short_open) / short_open * 100.0) if short_open > 0 else 0.0
            )

            bias = "NEUTRAL"
            day_type = "UNDECIDED"
            confidence = 0.35

            dump = (
                (move_pct <= -0.18 and close_loc <= 0.40)
                or (short_move <= -0.20 and close_loc <= 0.35)
                or (accel_pct <= -0.12 and move_pct <= -0.10 and close_loc <= 0.42)
            )
            rally = (
                (move_pct >= 0.18 and close_loc >= 0.60)
                or (short_move >= 0.20 and close_loc >= 0.65)
                or (accel_pct >= 0.12 and move_pct >= 0.10 and close_loc >= 0.58)
            )

            if dump and not rally:
                day_type = "TREND_DAY_DOWN"
                bias = "BEARISH"
                # Strong short dump with volume → explicit mood label for guards.
                if short_move <= -0.25 or (move_pct <= -0.30 and volume_ratio >= 1.1):
                    day_type = "MOOD_DUMP"
                confidence = min(
                    0.90,
                    0.55
                    + min(0.25, abs(min(move_pct, short_move)) * 0.35)
                    + (0.08 if volume_ratio >= 1.15 else 0.0),
                )
            elif rally and not dump:
                day_type = "TREND_DAY_UP"
                bias = "BULLISH"
                if short_move >= 0.25 or (move_pct >= 0.30 and volume_ratio >= 1.1):
                    day_type = "MOOD_RALLY"
                confidence = min(
                    0.90,
                    0.55
                    + min(0.25, max(move_pct, short_move) * 0.35)
                    + (0.08 if volume_ratio >= 1.15 else 0.0),
                )
            elif dump and rally:
                # Conflicting halves — transition / chop pocket.
                day_type = "MOOD_TRANSITION"
                bias = "BEARISH" if short_move < 0 else ("BULLISH" if short_move > 0 else "NEUTRAL")
                confidence = 0.50
            elif abs(move_pct) < 0.08 and 0.35 <= close_loc <= 0.65:
                day_type = "RANGE_DAY"
                bias = "NEUTRAL"
                confidence = 0.55
            else:
                bias = "BULLISH" if move_pct > 0.05 else ("BEARISH" if move_pct < -0.05 else "NEUTRAL")
                day_type = "UNDECIDED"
                confidence = 0.40

            return {
                "day_type": day_type,
                "bias": bias,
                "confidence": round(confidence, 3),
                "move_pct": round(move_pct, 3),
                "short_move_pct": round(short_move, 3),
                "accel_pct": round(accel_pct, 3),
                "lookback_bars": int(n),
                "volume_ratio": round(volume_ratio, 3),
                "close_location": round(float(close_loc), 3),
            }
        except Exception:
            return empty

    @staticmethod
    def _empty(instrument: str, reason: str) -> Dict[str, Any]:
        pocket = {
            "day_type": "UNDECIDED",
            "bias": "NEUTRAL",
            "confidence": 0.0,
            "move_pct": 0.0,
            "lookback_bars": 0,
            "volume_ratio": 0.0,
            "close_location": 0.5,
        }
        day_mood = {
            "day_type": "UNDECIDED",
            "bias": "NEUTRAL",
            "confidence": 0.0,
            "horizon": "day",
            "lookback_minutes": None,
        }
        return {
            "instrument": str(instrument or "").upper(),
            "session_date": "",
            "bars": 0,
            "bar_minutes": 5.0,
            "day_type": "UNDECIDED",
            "bias": "NEUTRAL",
            "confidence": 0.0,
            "opening_day_type": "UNDECIDED",
            "opening_bias": "NEUTRAL",
            "intraday_mood": dict(pocket),
            # Always emit horizons so the dashboard mood grid has stable keys
            # (never silent zeros / missing DOM updates).
            "mood_horizons": {
                "day": dict(day_mood),
                "minute": {**pocket, "horizon": "minute", "lookback_minutes": 5.0},
                "quarter": {**pocket, "horizon": "quarter", "lookback_minutes": 15.0},
                "hour": {**pocket, "horizon": "hour", "lookback_minutes": 60.0},
            },
            "intraday_phase": "UNDECIDED",
            "market_phase": "UNDECIDED",
            "phase_confidence": 0.0,
            "effective_day_type": "UNDECIDED",
            "effective_bias": "NEUTRAL",
            "reason": reason,
            "session_delta": {
                "source": "unavailable",
                "cumulative_delta": 0.0,
                "delta_ratio": 0.0,
                "bias": "NEUTRAL",
            },
        }
