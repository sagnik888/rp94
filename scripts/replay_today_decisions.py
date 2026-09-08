from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime
import json
from pathlib import Path
import sys
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.settings import get_settings
from engine.quality_verdict import (
    POLICY_VERSION,
    QualityInputs,
    evaluate_quality,
    policy_configuration,
    policy_configuration_hash,
)
from engine.signal_manager import SignalManager
from engine.ut_bot_core import UTBotEngine
from intelligence.regime_detector import RegimeDetector


LOT_SIZES = {"NIFTY": 65, "BANKNIFTY": 30, "SENSEX": 20, "MIDCPNIFTY": 120}
TF_MINUTES = {"1min": 1, "5min": 5, "15min": 15}


@dataclass
class Decision:
    instrument: str
    timeframe: str
    time: str
    signal: str
    price: float
    grade: str
    confidence: float
    adx: float
    regime: str
    confluence: float
    direction_15m: int
    decision: str
    route: str
    reason_code: str
    reason: str


def load_candles(instrument: str, timeframe: str) -> pd.DataFrame:
    path = Path("data_store") / "candles" / f"{instrument}_{timeframe}.csv"
    df = pd.read_csv(path)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df.set_index("timestamp").sort_index()


def ut_params(settings) -> dict:
    return {
        "key_value": settings.ut_atr_multiplier,
        "atr_period": settings.ut_atr_period,
        "use_heikin_ashi": settings.ut_use_heikin_ashi,
        "signal_mode": settings.ut_signal_mode,
        "adx_filter": settings.ut_adx_filter,
        "adx_period": settings.ut_adx_period,
        "adx_threshold": settings.ut_adx_threshold,
        "strict_adx": settings.ut_strict_adx,
        "session_filter": settings.ut_session_filter,
        "session_start": settings.ut_session_start,
        "session_end": settings.ut_session_end,
    }


def pos_at(result: dict, df: pd.DataFrame, ts: pd.Timestamp) -> tuple[int, bool]:
    if result is None or df is None or df.empty:
        return 0, False
    idx = df.index.searchsorted(ts, side="right") - 1
    if idx < 0:
        return 0, False
    idx = min(idx, len(result.get("positions", [])) - 1)
    if idx < 0:
        return 0, False
    pos = int(result["positions"][idx])
    adx_arr = result.get("adx", [])
    adx_val = float(adx_arr[idx]) if idx < len(adx_arr) and pd.notna(adx_arr[idx]) else 0.0
    return pos, adx_val > get_settings().ut_adx_threshold


def confluence_at(results: dict, candles: dict, instrument: str, ts: pd.Timestamp) -> tuple[float, str, int]:
    weights = {"1min": 0.15, "5min": 0.40, "15min": 0.45}
    total = 0.0
    score = 0.0
    pos15 = 0
    for tf, weight in weights.items():
        pos, trending = pos_at(results.get(tf), candles.get(tf), ts)
        if tf == "15min":
            pos15 = pos
        eff = weight * (1.3 if trending else 0.7)
        score += pos * eff
        total += eff
    score = round(score / total, 3) if total else 0.0
    label = "BUY" if score > 0.35 else ("SELL" if score < -0.35 else "HOLD")
    return score, label, pos15


def is_choppy(regime: str) -> bool:
    return str(regime or "UNKNOWN").upper() in {
        "CHOPPY",
        "SIDEWAYS",
        "MEAN_REVERTING",
        "RANGING",
        "RANGEBOUND",
        "VOLATILE",
        "UNKNOWN",
    }


def setup_evidence(sig, frame: pd.DataFrame) -> tuple[bool, float]:
    """Return structural impulse and measured volume ratio without ADX double-counting."""
    candle = getattr(sig, "raw_candle", {}) or {}
    atr = max(float(getattr(sig, "atr_value", 0.0) or 0.0), 1e-9)
    open_px = float(candle.get("open") or 0.0)
    high_px = float(candle.get("high") or 0.0)
    low_px = float(candle.get("low") or 0.0)
    close_px = float(candle.get("close") or getattr(sig, "price", 0.0) or 0.0)
    body_atr = abs(close_px - open_px) / atr
    range_atr = abs(high_px - low_px) / atr
    close_near_extreme = (
        close_px >= high_px - (0.25 * atr)
        if sig.signal_type == "BUY"
        else close_px <= low_px + (0.25 * atr)
    )
    impulse_body = body_atr >= 0.80 and range_atr >= 1.05 and close_near_extreme
    extreme_range = range_atr >= 1.45 and close_near_extreme

    volume_ratio = 0.0
    idx = int(getattr(sig, "bar_index", -1) or -1)
    if 0 <= idx < len(frame):
        current_vol = float(frame.iloc[idx].get("volume", 0.0) or 0.0)
        lookback = frame.iloc[max(0, idx - 12):idx]
        vols = [float(v) for v in lookback.get("volume", pd.Series(dtype=float)).tolist() if float(v or 0) > 0]
        avg_vol = sum(vols) / len(vols) if vols else 0.0
        volume_ratio = current_vol / avg_vol if avg_vol > 0 else 0.0

    volume_confirmed = volume_ratio >= 1.35
    return bool((impulse_body or extreme_range) and volume_confirmed), volume_ratio


def main() -> None:
    settings = get_settings()
    params = ut_params(settings)
    today = date.today()
    signal_manager = SignalManager()
    regime_detector = RegimeDetector()

    decisions: list[Decision] = []
    raw_counts = defaultdict(dict)

    for instrument in settings.active_indices:
        candles = {tf: load_candles(instrument, tf) for tf in ["1min", "5min", "15min"]}
        results = {}
        for tf, df in candles.items():
            engine = UTBotEngine(**params)
            results[tf] = engine.process(
                df=df,
                instrument=instrument,
                timeframe=tf,
                capital=settings.capital_fut,
                risk_pct=settings.risk_fut_pct,
                lots=1,
                lot_size=LOT_SIZES[instrument],
            )
        for tf in ["5min", "15min"]:
            today_sigs = [s for s in results[tf].get("signals", []) if s.timestamp.date() == today]
            raw_counts[instrument][tf] = len(today_sigs)
            for sig in today_sigs:
                sig_ts = pd.Timestamp(sig.timestamp)
                own_frame = candles[tf]
                own_cut = own_frame[own_frame.index <= sig_ts]
                regime = regime_detector.detect(own_cut, instrument, tf).get("regime", "UNKNOWN")
                confluence, _, pos15 = confluence_at(results, candles, instrument, sig_ts)
                grade, conf, _ = signal_manager._grade_signal(sig, confluence, 0.0, regime)
                direction_pos = 1 if sig.signal_type == "BUY" else -1
                trend_15m_agrees = pos15 == direction_pos
                impulse_confirmed, volume_ratio = setup_evidence(sig, own_frame)
                risky = is_choppy(regime)
                verdict = evaluate_quality(
                    QualityInputs(
                        timeframe=tf,
                        inst_type=str(getattr(settings, "inst_pref", "AUTO") or "AUTO"),
                        confidence=conf,
                        regime=regime,
                        adx_value=float(getattr(sig, "adx_value", 0.0) or 0.0),
                        trend_15m_agrees=trend_15m_agrees,
                        impulse_confirmed=impulse_confirmed,
                        risky_regime=risky,
                        is_choppy=risky and not impulse_confirmed,
                        grade=grade,
                        source_count=1,
                        volume_ratio=volume_ratio,
                        lane="REPLAY_TODAY",
                        direction="LONG" if sig.signal_type == "BUY" else "SHORT",
                    ),
                    settings,
                )
                decisions.append(
                    Decision(
                        instrument=instrument,
                        timeframe=tf,
                        time=sig.timestamp.strftime("%H:%M"),
                        signal=sig.signal_type,
                        price=round(float(sig.price), 2),
                        grade=grade,
                        confidence=round(float(conf), 3),
                        adx=round(float(sig.adx_value), 1),
                        regime=regime,
                        confluence=confluence,
                        direction_15m=pos15,
                        decision="ACCEPT" if verdict.allowed else "REJECT",
                        route=verdict.route,
                        reason_code=verdict.reason_code,
                        reason=verdict.reason,
                    )
                )

    decision_counts = Counter(d.decision for d in decisions)
    reject_reasons = Counter(d.reason_code for d in decisions if d.decision == "REJECT")

    accepted = [d.__dict__ for d in decisions if d.decision == "ACCEPT"]
    nearest = sorted(
        [d for d in decisions if d.decision == "REJECT"],
        key=lambda d: (d.confidence, d.adx),
        reverse=True,
    )[:12]

    out = {
        "date": str(today),
        "quality_policy": {
            "version": POLICY_VERSION,
            "configuration_hash": policy_configuration_hash(settings),
            "configuration": policy_configuration(settings),
        },
        "raw_signal_counts": raw_counts,
        "decision_counts": decision_counts,
        "reject_reasons_current": reject_reasons,
        "accepted_current": accepted,
        "nearest_rejected_current": [d.__dict__ for d in nearest],
    }
    open('output.json', 'w').write(json.dumps(out, indent=2, default=dict))


if __name__ == "__main__":
    main()
