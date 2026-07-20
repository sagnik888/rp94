"""
apex_emacross
─────────────
Python conversion of APEX Hybrid Pro v5.5 | EMA Cross Edition
(Pine Script → Python production package)

Public API
──────────
    from apex_emacross import ApexScanner, ApexConfig

    scanner = ApexScanner(symbol="NIFTY50")
    signals = scanner.scan(ohlcv_df)          # batch / backtest
    events  = scanner.update(bar, history_df) # live / streaming
"""

from .config        import ApexConfig
from .scanner       import ApexScanner
from .instruments   import detect_instrument
from .indicators    import (
    compute_all_indicators,
    compute_emas,
    compute_atr,
    compute_rsi,
    compute_macd,
    compute_dmi_adx,
    compute_vwap,
    crossover,
    crossunder,
    rma,
)
from .patterns      import compute_patterns
from .confluence    import compute_confluence
from .session       import compute_session_mask, is_session_ok
from .risk          import calc_sl, calc_targets, update_trailing_stop
from .options_module import get_strike
from .trade_manager import TradeManager, LiveTrade, TradeRecord

__version__  = "5.5.0"
__all__ = [
    "ApexConfig",
    "ApexScanner",
    "TradeManager",
    "LiveTrade",
    "TradeRecord",
    "detect_instrument",
    "compute_all_indicators",
    "compute_emas",
    "compute_atr",
    "compute_rsi",
    "compute_macd",
    "compute_dmi_adx",
    "compute_vwap",
    "crossover",
    "crossunder",
    "rma",
    "compute_patterns",
    "compute_confluence",
    "compute_session_mask",
    "is_session_ok",
    "calc_sl",
    "calc_targets",
    "update_trailing_stop",
    "get_strike",
]
