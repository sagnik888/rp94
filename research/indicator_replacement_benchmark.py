"""Research-only ATR replacement benchmark.

This module compares UTBot's ATR-trail trigger with common non-ATR and mixed
indicator triggers on the same cached spot candles. It is intentionally
standalone: importing it does not change scanner settings, live engines, or
dashboard behavior.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable

import numpy as np
import pandas as pd


DEFAULT_INSTRUMENTS = ("NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY")
DEFAULT_TIMEFRAMES = ("5min", "15min")
BASELINE_STRATEGY = "utbot_atr_trail"


@dataclass(frozen=True)
class Trade:
    strategy: str
    instrument: str
    timeframe: str
    entry_time: str
    exit_time: str
    direction: str
    entry: float
    exit: float
    points: float


@dataclass(frozen=True)
class StrategyMetrics:
    strategy: str
    instrument: str
    timeframe: str
    trades: int
    wins: int
    losses: int
    win_rate: float
    net_points: float
    gross_win_points: float
    gross_loss_points: float
    profit_factor: float | None
    max_drawdown_points: float
    avg_points: float
    sharpe_per_trade: float
    trades_per_day: float


def load_candles(path: str | Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    if "timestamp" not in df.columns:
        raise ValueError(f"{path} has no timestamp column")
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df = df.dropna(subset=["timestamp"]).sort_values("timestamp")
    df = df.set_index("timestamp")
    required = ["open", "high", "low", "close"]
    missing = [col for col in required if col not in df.columns]
    if missing:
        raise ValueError(f"{path} missing columns: {', '.join(missing)}")
    for col in [*required, "volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    if "volume" not in df.columns:
        df["volume"] = 1.0
    df = df.dropna(subset=required)
    return df[["open", "high", "low", "close", "volume"]]


def trim_lookback_days(df: pd.DataFrame, lookback_days: int | None) -> pd.DataFrame:
    if not lookback_days or df.empty:
        return df
    last = pd.Timestamp(df.index.max()).normalize()
    first = last - pd.Timedelta(days=max(1, int(lookback_days)) - 1)
    return df.loc[df.index.normalize() >= first]


def _true_range(df: pd.DataFrame) -> pd.Series:
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    prev_close = close.shift(1)
    return pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)


def _wilder_atr(df: pd.DataFrame, period: int = 10) -> pd.Series:
    return _true_range(df).ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()


def _hold_through_zeros(values: np.ndarray) -> np.ndarray:
    out = np.zeros(len(values), dtype=int)
    last = 0
    for i, value in enumerate(values):
        if value:
            last = int(value)
        out[i] = last
    return out


def _positions_from_line_cross(close: pd.Series, line: pd.Series) -> np.ndarray:
    c = close.to_numpy(dtype=float)
    x = line.to_numpy(dtype=float)
    pos = np.zeros(len(close), dtype=int)
    last = 0
    for i in range(1, len(c)):
        if not (np.isfinite(c[i - 1]) and np.isfinite(c[i]) and np.isfinite(x[i - 1]) and np.isfinite(x[i])):
            pos[i] = last
            continue
        if c[i - 1] <= x[i - 1] and c[i] > x[i]:
            last = 1
        elif c[i - 1] >= x[i - 1] and c[i] < x[i]:
            last = -1
        pos[i] = last
    return pos


def utbot_atr_trail_positions(df: pd.DataFrame, period: int = 10, mult: float = 1.0) -> np.ndarray:
    close = df["close"].astype(float)
    atr = _wilder_atr(df, period).bfill()
    n_loss = atr.to_numpy(dtype=float) * float(mult)
    src = close.to_numpy(dtype=float)
    trail = np.empty(len(src), dtype=float)
    if not len(src):
        return np.array([], dtype=int)
    trail[0] = src[0] - n_loss[0] if np.isfinite(n_loss[0]) else src[0]
    for i in range(1, len(src)):
        prev_trail = trail[i - 1]
        if src[i] > prev_trail and src[i - 1] > prev_trail:
            trail[i] = max(prev_trail, src[i] - n_loss[i])
        elif src[i] < prev_trail and src[i - 1] < prev_trail:
            trail[i] = min(prev_trail, src[i] + n_loss[i])
        else:
            trail[i] = src[i] - n_loss[i] if src[i] > prev_trail else src[i] + n_loss[i]
    return _positions_from_line_cross(close, pd.Series(trail, index=df.index))


def ema_crossover_positions(df: pd.DataFrame, fast: int = 9, slow: int = 21) -> np.ndarray:
    close = df["close"].astype(float)
    fast_line = close.ewm(span=fast, adjust=False).mean()
    slow_line = close.ewm(span=slow, adjust=False).mean()
    raw = np.where(fast_line > slow_line, 1, -1).astype(int)
    raw[: max(fast, slow)] = 0
    return raw


def supertrend_positions(df: pd.DataFrame, period: int = 10, mult: float = 3.0) -> np.ndarray:
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    atr = _true_range(df).rolling(period, min_periods=period).mean()
    hl2 = (high + low) / 2.0
    upper = (hl2 + mult * atr).to_numpy(dtype=float)
    lower = (hl2 - mult * atr).to_numpy(dtype=float)
    c = close.to_numpy(dtype=float)
    trend = np.ones(len(c), dtype=int)
    final_upper = upper.copy()
    final_lower = lower.copy()
    for i in range(1, len(c)):
        if not (np.isfinite(upper[i]) and np.isfinite(lower[i])):
            trend[i] = trend[i - 1]
            final_upper[i] = final_upper[i - 1]
            final_lower[i] = final_lower[i - 1]
            continue
        final_upper[i] = min(upper[i], final_upper[i - 1]) if c[i - 1] <= final_upper[i - 1] else upper[i]
        final_lower[i] = max(lower[i], final_lower[i - 1]) if c[i - 1] >= final_lower[i - 1] else lower[i]
        if c[i] > final_upper[i - 1]:
            trend[i] = 1
        elif c[i] < final_lower[i - 1]:
            trend[i] = -1
        else:
            trend[i] = trend[i - 1]
    trend[:period] = 0
    return trend


def consensus_positions(df: pd.DataFrame) -> np.ndarray:
    ema = ema_crossover_positions(df)
    st = supertrend_positions(df)
    return _hold_through_zeros(np.where(ema == st, ema, 0))


def aroon_positions(df: pd.DataFrame, period: int = 25) -> np.ndarray:
    high = df["high"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)
    out = np.zeros(len(df), dtype=int)
    for i in range(period - 1, len(df)):
        hi_win = high[i - period + 1 : i + 1]
        lo_win = low[i - period + 1 : i + 1]
        if not (np.isfinite(hi_win).all() and np.isfinite(lo_win).all()):
            continue
        periods_since_high = period - 1 - int(np.argmax(hi_win))
        periods_since_low = period - 1 - int(np.argmin(lo_win))
        aroon_up = ((period - periods_since_high) / period) * 100.0
        aroon_down = ((period - periods_since_low) / period) * 100.0
        if aroon_up >= 70.0 and aroon_down <= 40.0:
            out[i] = 1
        elif aroon_down >= 70.0 and aroon_up <= 40.0:
            out[i] = -1
    return _hold_through_zeros(out)


def donchian_breakout_positions(df: pd.DataFrame, period: int = 20) -> np.ndarray:
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    prior_high = high.shift(1).rolling(period, min_periods=period).max()
    prior_low = low.shift(1).rolling(period, min_periods=period).min()
    raw = np.where(close > prior_high, 1, np.where(close < prior_low, -1, 0)).astype(int)
    return _hold_through_zeros(raw)


def kama_positions(df: pd.DataFrame, er_period: int = 10, fast: int = 2, slow: int = 30) -> np.ndarray:
    close = df["close"].astype(float).to_numpy()
    out = np.zeros(len(close), dtype=int)
    if not len(close):
        return out
    fast_sc = 2.0 / (fast + 1.0)
    slow_sc = 2.0 / (slow + 1.0)
    kama = np.full(len(close), np.nan, dtype=float)
    kama[0] = close[0]
    for i in range(1, len(close)):
        if i < er_period:
            kama[i] = close[i]
            continue
        change = abs(close[i] - close[i - er_period])
        path = np.sum(np.abs(np.diff(close[i - er_period : i + 1])))
        er = 0.0 if path <= 0 else change / path
        sc = (er * (fast_sc - slow_sc) + slow_sc) ** 2
        kama[i] = kama[i - 1] + sc * (close[i] - kama[i - 1])
        out[i] = 1 if close[i] > kama[i] else -1
    return out


def session_vwap_positions(df: pd.DataFrame) -> np.ndarray:
    typical = (df["high"].astype(float) + df["low"].astype(float) + df["close"].astype(float)) / 3.0
    volume = df["volume"].astype(float).replace(0, np.nan).fillna(1.0)
    dates = df.index.normalize()
    vp = typical * volume
    cum_vp = vp.groupby(dates).cumsum()
    cum_vol = volume.groupby(dates).cumsum().replace(0, np.nan)
    vwap = cum_vp / cum_vol
    raw = np.where(df["close"].astype(float) > vwap, 1, -1).astype(int)
    first_bar = np.r_[True, dates[1:] != dates[:-1]]
    raw[first_bar] = 0
    return raw


def bollinger_breakout_positions(df: pd.DataFrame, period: int = 20, width: float = 2.0) -> np.ndarray:
    close = df["close"].astype(float)
    mid = close.rolling(period, min_periods=period).mean()
    std = close.rolling(period, min_periods=period).std(ddof=0)
    upper = mid + width * std
    lower = mid - width * std
    raw = np.where(close > upper, 1, np.where(close < lower, -1, 0)).astype(int)
    return _hold_through_zeros(raw)


def non_atr_majority_positions(df: pd.DataFrame) -> np.ndarray:
    members = np.vstack(
        [
            ema_crossover_positions(df),
            aroon_positions(df),
            donchian_breakout_positions(df),
            kama_positions(df),
            session_vwap_positions(df),
        ]
    )
    summed = np.sum(members, axis=0)
    raw = np.where(summed >= 2, 1, np.where(summed <= -2, -1, 0)).astype(int)
    return _hold_through_zeros(raw)


STRATEGIES: dict[str, Callable[[pd.DataFrame], np.ndarray]] = {
    BASELINE_STRATEGY: utbot_atr_trail_positions,
    "ema_9_21": ema_crossover_positions,
    "consensus_st_ema": consensus_positions,
    "aroon_25": aroon_positions,
    "donchian_20": donchian_breakout_positions,
    "kama_er": kama_positions,
    "session_vwap": session_vwap_positions,
    "bollinger_breakout": bollinger_breakout_positions,
    "non_atr_majority": non_atr_majority_positions,
}


def trades_from_positions(
    df: pd.DataFrame,
    positions: np.ndarray,
    *,
    strategy: str,
    instrument: str,
    timeframe: str,
    friction_bps_per_side: float = 0.0,
) -> list[Trade]:
    if len(df) != len(positions):
        raise ValueError("positions length must match dataframe length")
    trades: list[Trade] = []
    close = df["close"].to_numpy(dtype=float)
    times = list(df.index)
    dates = df.index.normalize().to_numpy()
    friction = max(0.0, float(friction_bps_per_side)) / 10000.0
    for day in pd.unique(dates):
        indices = np.flatnonzero(dates == day)
        open_dir = 0
        entry_price = 0.0
        entry_time = None
        for i in indices:
            want = int(positions[i])
            if want == 0:
                continue
            if open_dir == 0:
                open_dir = want
                entry_price = float(close[i])
                entry_time = times[i]
                continue
            if want == open_dir:
                continue
            exit_price = float(close[i])
            gross = (exit_price - entry_price) * open_dir
            cost = (entry_price + exit_price) * friction
            trades.append(
                Trade(
                    strategy=strategy,
                    instrument=instrument,
                    timeframe=timeframe,
                    entry_time=str(entry_time),
                    exit_time=str(times[i]),
                    direction="LONG" if open_dir > 0 else "SHORT",
                    entry=round(entry_price, 4),
                    exit=round(exit_price, 4),
                    points=round(gross - cost, 4),
                )
            )
            open_dir = want
            entry_price = exit_price
            entry_time = times[i]
        if open_dir and entry_time is not None and len(indices):
            i = int(indices[-1])
            exit_price = float(close[i])
            if times[i] != entry_time:
                gross = (exit_price - entry_price) * open_dir
                cost = (entry_price + exit_price) * friction
                trades.append(
                    Trade(
                        strategy=strategy,
                        instrument=instrument,
                        timeframe=timeframe,
                        entry_time=str(entry_time),
                        exit_time=str(times[i]),
                        direction="LONG" if open_dir > 0 else "SHORT",
                        entry=round(entry_price, 4),
                        exit=round(exit_price, 4),
                        points=round(gross - cost, 4),
                    )
                )
    return trades


def summarize_trades(
    trades: list[Trade],
    *,
    strategy: str,
    instrument: str,
    timeframe: str,
    day_count: int,
) -> StrategyMetrics:
    points = np.asarray([t.points for t in trades], dtype=float)
    wins = int(np.sum(points > 0))
    losses = int(np.sum(points <= 0))
    gross_win = float(points[points > 0].sum()) if len(points) else 0.0
    gross_loss = float(-points[points <= 0].sum()) if len(points) else 0.0
    equity = np.cumsum(points) if len(points) else np.asarray([], dtype=float)
    peaks = np.maximum.accumulate(equity) if len(equity) else equity
    drawdown = peaks - equity if len(equity) else np.asarray([], dtype=float)
    sharpe = 0.0
    if len(points) > 1 and float(points.std(ddof=1)) > 0:
        sharpe = float(np.sqrt(len(points)) * points.mean() / points.std(ddof=1))
    return StrategyMetrics(
        strategy=strategy,
        instrument=instrument,
        timeframe=timeframe,
        trades=int(len(points)),
        wins=wins,
        losses=losses,
        win_rate=round((wins / len(points)) * 100.0, 2) if len(points) else 0.0,
        net_points=round(float(points.sum()) if len(points) else 0.0, 2),
        gross_win_points=round(gross_win, 2),
        gross_loss_points=round(gross_loss, 2),
        profit_factor=round(gross_win / gross_loss, 3) if gross_loss > 0 else (None if gross_win == 0 else 999.0),
        max_drawdown_points=round(float(drawdown.max()) if len(drawdown) else 0.0, 2),
        avg_points=round(float(points.mean()) if len(points) else 0.0, 3),
        sharpe_per_trade=round(sharpe, 3),
        trades_per_day=round(len(points) / max(1, int(day_count)), 2),
    )


def evaluate_strategy(
    df: pd.DataFrame,
    strategy: str,
    position_fn: Callable[[pd.DataFrame], np.ndarray],
    *,
    instrument: str,
    timeframe: str,
    friction_bps_per_side: float = 0.0,
) -> tuple[StrategyMetrics, list[Trade]]:
    positions = position_fn(df)
    trades = trades_from_positions(
        df,
        positions,
        strategy=strategy,
        instrument=instrument,
        timeframe=timeframe,
        friction_bps_per_side=friction_bps_per_side,
    )
    day_count = int(pd.Series(df.index.normalize()).nunique()) if len(df) else 0
    return summarize_trades(
        trades,
        strategy=strategy,
        instrument=instrument,
        timeframe=timeframe,
        day_count=day_count,
    ), trades


def aggregate_metrics(rows: Iterable[StrategyMetrics]) -> list[dict]:
    grouped: dict[str, list[StrategyMetrics]] = {}
    for row in rows:
        grouped.setdefault(row.strategy, []).append(row)
    aggregate: list[dict] = []
    for strategy, items in grouped.items():
        trades = sum(x.trades for x in items)
        wins = sum(x.wins for x in items)
        losses = sum(x.losses for x in items)
        gross_win = sum(x.gross_win_points for x in items)
        gross_loss = sum(x.gross_loss_points for x in items)
        net = sum(x.net_points for x in items)
        dd = sum(x.max_drawdown_points for x in items)
        aggregate.append(
            {
                "strategy": strategy,
                "trades": trades,
                "wins": wins,
                "losses": losses,
                "win_rate": round((wins / trades) * 100.0, 2) if trades else 0.0,
                "net_points": round(net, 2),
                "gross_win_points": round(gross_win, 2),
                "gross_loss_points": round(gross_loss, 2),
                "profit_factor": round(gross_win / gross_loss, 3) if gross_loss > 0 else (None if gross_win == 0 else 999.0),
                "max_drawdown_points_sum": round(dd, 2),
                "net_to_drawdown": round(net / dd, 3) if dd > 0 else None,
            }
        )
    aggregate.sort(key=lambda item: (item["net_points"], item["profit_factor"] or 0), reverse=True)
    baseline = next((item for item in aggregate if item["strategy"] == BASELINE_STRATEGY), None)
    if baseline:
        base_net = float(baseline["net_points"])
        for item in aggregate:
            item["improvement_vs_baseline_points"] = round(float(item["net_points"]) - base_net, 2)
    return aggregate


def run_benchmark(
    data_dir: str | Path = "data_store/candles",
    *,
    instruments: Iterable[str] = DEFAULT_INSTRUMENTS,
    timeframes: Iterable[str] = DEFAULT_TIMEFRAMES,
    lookback_days: int | None = None,
    friction_bps_per_side: float = 0.5,
    strategies: dict[str, Callable[[pd.DataFrame], np.ndarray]] | None = None,
) -> dict:
    data_dir = Path(data_dir)
    active_strategies = strategies or STRATEGIES
    rows: list[StrategyMetrics] = []
    trades_by_strategy: dict[str, int] = {name: 0 for name in active_strategies}
    files: list[str] = []
    skipped: list[str] = []
    for instrument in instruments:
        for timeframe in timeframes:
            path = data_dir / f"{instrument}_{timeframe}.csv"
            if not path.exists():
                skipped.append(str(path))
                continue
            df = trim_lookback_days(load_candles(path), lookback_days)
            if len(df) < 60:
                skipped.append(f"{path}: insufficient rows")
                continue
            files.append(str(path))
            for name, fn in active_strategies.items():
                metrics, trades = evaluate_strategy(
                    df,
                    name,
                    fn,
                    instrument=instrument,
                    timeframe=timeframe,
                    friction_bps_per_side=friction_bps_per_side,
                )
                rows.append(metrics)
                trades_by_strategy[name] += len(trades)
    aggregate = aggregate_metrics(rows)
    return {
        "baseline": BASELINE_STRATEGY,
        "friction_bps_per_side": float(friction_bps_per_side),
        "lookback_days": lookback_days,
        "files": files,
        "skipped": skipped,
        "rows": [asdict(row) for row in rows],
        "aggregate": aggregate,
        "best_by_net": aggregate[0] if aggregate else None,
    }
