"""
Order Flow Approximation — Buy/Sell Volume Ratio & Cumulative Delta
"""
import pandas as pd
import numpy as np
from typing import Dict, Optional
from loguru import logger


class OrderFlowAnalyzer:
    @staticmethod
    def _current_session(df: pd.DataFrame) -> pd.DataFrame:
        """Rows belonging to the latest calendar date in the frame.

        Falls through unchanged for a non-datetime index (tests and any RangeIndex
        caller), so nothing that already worked starts behaving differently.
        """
        try:
            dates = pd.Series(df.index.date, index=df.index)
            return df[dates == dates.iloc[-1]]
        except Exception:
            return df

    def analyze(
        self,
        df: pd.DataFrame,
        options_chain: Optional[pd.DataFrame] = None,
        spot_price: float = 0,
        strike_interval: int = 50,
        market_context: Optional[Dict] = None,
    ) -> Dict:
        # NOTE ON THE WINDOW: callers hand this a warm-up frame.
        # scanner._live_analysis_window prepends up to 180 prior-session 1min bars,
        # which the rolling-window consumers (regime detector, volume profiler) need.
        # Order flow is an INTRADAY statistic, so summing the whole frame described
        # yesterday as much as today: buy/sell pressure was diluted toward 1.0 and the
        # aggregator's divergence gate disagreed with a clean read on ~33% of samples.
        # The trim below is deliberately inside this analyzer rather than in
        # _live_analysis_window, so live, historical replay and re-simulation all get
        # the same correction.
        if df is None or len(df) < 5:
            return self._empty_result(market_context)
        df = self._current_session(df)
        # Re-checked AFTER trimming. This is the sharp case: in the first four minutes
        # of the session the frame still has hundreds of rows, so the guard above passes,
        # but `pos.iloc[-5:]` is 1-4 of today's bars padded with YESTERDAY'S CLOSING
        # bars — and that emitted BULLISH/BEARISH readings built from the prior session
        # where a clean read is NEUTRAL. The flow signal is a hard entry veto and a
        # loss-guard vote, so fewer than five live bars must mean "no reading".
        if len(df) < 5:
            return self._empty_result(market_context)
        try:
            depth_flow = self._depth_flow(market_context)
            c, h, l = df['close'], df['high'], df['low']
            v = pd.to_numeric(df['volume'], errors='coerce').fillna(0.0).clip(lower=0.0)
            usable_volume = pd.Series(True, index=df.index)
            if "volume_basis" in df.columns:
                bases = df["volume_basis"].fillna("").astype(str)
                active_bases = bases.loc[v > 0]
                latest_basis = active_bases.iloc[-1] if not active_bases.empty else ""
                usable_volume &= bases.eq(latest_basis)
                usable_volume &= ~bases.str.upper().isin({"MIXED", "MIXED_UNITS", "TICK_ACTIVITY", "INDEX_TICK_ACTIVITY", "UNAVAILABLE"})
            if "volume_complete" in df.columns:
                usable_volume &= ~df["volume_complete"].eq(False).fillna(False)
            # Exclude invalid quantities without letting an old partial bar veto
            # today's valid observations or an independent usable depth snapshot.
            partial_coverage = bool((~usable_volume & v.gt(0)).any())
            v = v.where(usable_volume, 0.0)
            # Candle location without measured volume is price action, not order flow.
            # A usable depth snapshot remains legitimate flow evidence on its own.
            if float(v.sum()) <= 0.0 and not depth_flow.get("usable"):
                return self._empty_result(market_context)
            rng = h - l
            rng = rng.replace(0, np.nan)
            pos = (c - l) / rng
            pos = pos.fillna(0.5).clip(0, 1)
            buy_vol = (v * pos).sum()
            sell_vol = (v * (1 - pos)).sum()
            ratio = float(buy_vol / sell_vol) if sell_vol > 0 else 1.0
            cum_delta = float(buy_vol - sell_vol)
            # Recent momentum is only flow when recent bars actually carry measured
            # volume.  An older active session followed by a dead right edge used to
            # publish a 95% "buyers" signal from candle geometry alone.
            recent_v = v.iloc[-5:]
            if float(recent_v.sum()) <= 0.0 and not depth_flow.get("usable"):
                return self._empty_result(market_context)
            recent_pos = (
                float((pos.iloc[-5:] * recent_v).sum() / recent_v.sum())
                if float(recent_v.sum()) > 0.0
                else 0.5
            )
            if recent_pos > 0.6:
                signal, aggressor = "BULLISH", "BUYERS"
            elif recent_pos < 0.4:
                signal, aggressor = "BEARISH", "SELLERS"
            else:
                signal, aggressor = "NEUTRAL", "NONE"
            result = {
                "buy_volume": int(buy_vol), "sell_volume": int(sell_vol),
                "ratio": round(ratio, 2), "buy_sell_ratio": round(ratio, 2), "cum_delta": int(cum_delta),
                "buy_pct": round(float(recent_pos * 100), 1),
                "signal": signal, "aggressor": aggressor,
                "source": "ohlcv_close_position",
                "session_volume_complete": not partial_coverage,
                "true_tick_footprint_available": False,
                "limitation": "Broker feed does not provide aggressor-side tick footprint; using OHLCV/L1 proxy",
            }
            if depth_flow.get("usable"):
                result.update(
                    {
                        "buy_volume": int(depth_flow.get("bid_qty", 0)),
                        "sell_volume": int(depth_flow.get("ask_qty", 0)),
                        "ratio": round(float(depth_flow.get("ofr", 1.0)), 2),
                        "buy_sell_ratio": round(float(depth_flow.get("ofr", 1.0)), 2),
                        "buy_pct": round(float(depth_flow.get("buy_pct", 50.0)), 1),
                        "signal": depth_flow.get("signal", signal),
                        "aggressor": depth_flow.get("aggressor", aggressor),
                        "source": depth_flow.get("source", "depth"),
                    }
                )
            # Only the COMPUTED path is available. Volume, OI and PCR all publish this
            # key and the aggregator gates both their vote and their component on it;
            # order flow published nothing, so an absent reading was scored as a neutral
            # observation AND counted in the denominator.
            result["available"] = True
            result.update(self._source_context(market_context))
            result["options_flow"] = self._options_flow(options_chain, spot_price, strike_interval)
            confirmation = result["signal"]
            # CE/PE quantity has no aggressor identity and cannot vote direction.
            result["multi_source_flow"]["confirmation"] = confirmation
            return result
        except Exception as e:
            logger.error(f"Order flow error: {e}")
            return self._empty_result(market_context)

    def _depth_flow(self, market_context: Optional[Dict]) -> Dict:
        depth = (market_context or {}).get("depth") if isinstance(market_context, dict) else {}
        if not isinstance(depth, dict) or not depth.get("usable"):
            return {"usable": False}
        bid_qty = float(depth.get("bid_qty") or 0.0)
        ask_qty = float(depth.get("ask_qty") or 0.0)
        if bid_qty <= 0 and ask_qty <= 0:
            return {"usable": False}
        ofr = float(depth.get("ofr") or (bid_qty / ask_qty if ask_qty > 0 else 1.0))
        total = bid_qty + ask_qty
        buy_pct = (bid_qty / total * 100.0) if total > 0 else 50.0
        if ofr >= 1.08:
            signal, aggressor = "BULLISH", "BUYERS"
        elif ofr <= 0.92:
            signal, aggressor = "BEARISH", "SELLERS"
        else:
            signal, aggressor = "NEUTRAL", "NONE"
        return {
            "usable": True,
            "bid_qty": bid_qty,
            "ask_qty": ask_qty,
            "ofr": ofr,
            "buy_pct": buy_pct,
            "signal": signal,
            "aggressor": aggressor,
            "source": depth.get("source", "depth"),
        }

    def _source_context(self, market_context: Optional[Dict]) -> Dict:
        context = market_context or {}
        sources = list(context.get("sources") or [])
        delayed = [
            str(src.get("source"))
            for src in sources
            if str(src.get("latency_class") or "").upper() == "DELAYED"
            and not bool(src.get("entry_eligible", False))
            and src.get("source")
        ]
        entry_eligible = bool(context.get("entry_eligible", True))
        return {
            "usable_for_entry": entry_eligible,
            "multi_source_flow": {
                "entry_eligible": entry_eligible,
                "freshest": context.get("freshest", {}),
                "sources": sources,
                "delayed_sources_ignored_for_entry": delayed,
                "confirmation": "NEUTRAL",
            },
        }

    def _options_flow(self, options_chain: Optional[pd.DataFrame], spot_price: float, strike_interval: int) -> Dict:
        if options_chain is None or options_chain.empty:
            return {"top_ce": [], "top_pe": [], "ce_pe_activity_ratio": 1.0}
        try:
            # Only the three columns read below. copy() + sort_values + two nlargest on
            # the whole chain moved its symbol/token string columns through four
            # take()s per call; copy only this three-column numeric projection so the
            # derived distance column never writes through a pandas slice.
            chain = options_chain[
                [c for c in ("strike", "call_volume", "put_volume") if c in options_chain.columns]
            ].copy()
            if spot_price and "strike" in chain.columns:
                interval = max(float(strike_interval or 0), 1.0)
                atm = round(float(spot_price) / interval) * interval
                chain["_distance"] = (chain["strike"].astype(float) - atm).abs()
                chain = chain.sort_values(["_distance", "strike"]).head(9)
            top_ce = chain.nlargest(4, "call_volume")[["strike", "call_volume"]].to_dict("records")
            top_pe = chain.nlargest(4, "put_volume")[["strike", "put_volume"]].to_dict("records")
            ce_activity = float(chain.get("call_volume", pd.Series(dtype=float)).sum() or 0.0)
            pe_activity = float(chain.get("put_volume", pd.Series(dtype=float)).sum() or 0.0)
            ratio = ce_activity / pe_activity if pe_activity > 0 else (ce_activity if ce_activity > 0 else 1.0)
            return {
                "top_ce": top_ce, "top_pe": top_pe,
                "ce_pe_activity_ratio": round(float(ratio), 3),
                "basis": "cumulative_option_volume_only",
            }
        except Exception as e:
            logger.error(f"Options flow error: {e}")
            return {"top_ce": [], "top_pe": [], "ce_pe_activity_ratio": 1.0}

    def _empty_result(self, market_context: Optional[Dict] = None) -> Dict:
        result = {
            "buy_volume": 0, "sell_volume": 0, "ratio": 1.0, "buy_sell_ratio": 1.0,
            "cum_delta": 0, "buy_pct": 50.0, "signal": "NEUTRAL", "aggressor": "NONE",
            # "cannot say", not "balanced". The aggregator must abstain, not average a
            # neutral in — fewer than five live bars is the normal 09:15-09:19 condition.
            "available": False,
            "source": "unavailable",
            "true_tick_footprint_available": False,
            "limitation": "Broker feed does not provide aggressor-side tick footprint; using OHLCV/L1 proxy",
            "options_flow": {"top_ce": [], "top_pe": [], "ce_pe_activity_ratio": 1.0},
        }
        result.update(self._source_context(market_context))
        return result
