"""
apex_emacross/config.py
Configuration dataclass for APEX Hybrid Pro v5.5 — EMA Cross Edition.
Mirrors ALL Pine Script input parameters (Section 1) exactly.

Signal entry: Pure EMA9/21 crossover.
Confluence score is computed for display/context only — not an entry gate.
"""

from dataclasses import dataclass, field
from typing import List, Tuple


@dataclass
class ApexConfig:
    """
    All configurable parameters for the APEX EMA Cross Python scanner.

    Quick-start defaults match the Pine Script defaults exactly.
    """

    # ═══════════════════════════════════════════════════════════════════
    # INSTRUMENT
    # ═══════════════════════════════════════════════════════════════════
    instrument_type: str = "Auto-Detect"
    # Options: Auto-Detect | Nifty 50 | Bank Nifty | FinNifty | Midcap |
    #          Sensex | Index | Stock | Commodity | Crypto | Forex | Futures

    # ═══════════════════════════════════════════════════════════════════
    # SIGNAL GATES  (scoring / dashboard context — NOT entry conditions)
    # ═══════════════════════════════════════════════════════════════════
    min_score: int       = 70    # Min confluence score displayed on dashboard
    conflict_margin: int = 20    # Min bull-bear divergence %
    min_adx: float       = 20.0  # ADX threshold used in score component C1
    use_htf: bool        = True  # Use 15m EMA alignment in score component C7
    signal_cooldown: int = 2     # Min bars between successive signals

    # ═══════════════════════════════════════════════════════════════════
    # DYNAMIC RISK SIZING
    # ═══════════════════════════════════════════════════════════════════
    atr_mult: float     = 1.5    # ATR multiplier for SL1
    fixed_sl_pct: float = 0.2    # Fallback fixed SL (%) when not trending/volatile

    # ═══════════════════════════════════════════════════════════════════
    # R-MULTIPLE TARGETS  (1:2 / 1:3 / 1:4)
    # ═══════════════════════════════════════════════════════════════════
    t1r: float = 2.0
    t2r: float = 3.0
    t3r: float = 4.0

    # ═══════════════════════════════════════════════════════════════════
    # TRAILING STOP
    # ═══════════════════════════════════════════════════════════════════
    use_trail: bool      = True
    trail_start_r: float = 1.5   # Activate trail once R:R reaches this
    trail_mult: float    = 1.8   # ATR offset multiplier for trailing stop
    lock_at_t1: bool     = True  # Lock entry + buffer once T1 is hit

    # ═══════════════════════════════════════════════════════════════════
    # EXIT LOGIC
    # ═══════════════════════════════════════════════════════════════════
    exit_conf_bars: int  = 3     # Momentum-exit confirmation bars required
    max_consec_loss: int = 3     # Consecutive losses before circuit breaker
    circuit_pause: int   = 10    # Bars to pause after circuit breaker fires

    # ═══════════════════════════════════════════════════════════════════
    # NSE SESSION FILTER  (IST timezone)
    # ═══════════════════════════════════════════════════════════════════
    use_session: bool  = True
    block_open: bool   = True    # Block 09:15–09:30  (noise zone)
    block_close: bool  = True    # Block 15:00–15:30  (close zone)

    # ═══════════════════════════════════════════════════════════════════
    # OPTIONS MODULE
    # ═══════════════════════════════════════════════════════════════════
    enable_opt: bool   = True
    strike_mode: str   = "Smart Auto"
    # Options: Smart Auto | Always ATM | Always OTM1 | Always ITM1

    # ═══════════════════════════════════════════════════════════════════
    # CANDLESTICK PATTERN RECOGNITION
    # ═══════════════════════════════════════════════════════════════════
    show_patterns: bool   = True
    use_pat_score: bool   = True   # Add pattern bonus to confluence score (C10)
    pat_single_grp: bool  = True   # Enable single-bar patterns
    pat_double_grp: bool  = True   # Enable double-bar patterns
    pat_triple_grp: bool  = True   # Enable triple-bar patterns
    pat_cont_grp: bool    = True   # Enable continuation patterns

    # ═══════════════════════════════════════════════════════════════════
    # SWING FORECAST
    # ═══════════════════════════════════════════════════════════════════
    sf_swing_len: int  = 16
    sf_samples: int    = 20
    sf_method: str     = "Weighted"   # Weighted | Average | Median
    sf_fwd_bars: int   = 5

    # Fib extension levels: (active, ratio)
    sf_fib_levels: List[Tuple[bool, float]] = field(
        default_factory=lambda: [
            (True,  1.0),
            (True,  1.272),
            (True,  1.618),
            (False, 2.0),
            (False, 2.618),
        ]
    )

    # ═══════════════════════════════════════════════════════════════════
    # RUNTIME FIELDS  (set by scanner after auto-detection)
    # ═══════════════════════════════════════════════════════════════════
    detected_instrument: str = ""   # Filled by ApexScanner.__init__

    # ─── Derived Properties ───────────────────────────────────────────
    @property
    def supports_options(self) -> bool:
        """True for NSE index instruments that have liquid options."""
        return self.detected_instrument in (
            "Nifty 50", "Bank Nifty", "FinNifty", "Midcap"
        )

    @property
    def strike_interval(self) -> float:
        """Option strike spacing for the detected instrument."""
        _map = {
            "Bank Nifty": 100.0,
            "Nifty 50":   50.0,
            "FinNifty":   50.0,
            "Midcap":     25.0,
        }
        return _map.get(self.detected_instrument, 50.0)

    @property
    def min_bars_required(self) -> int:
        """Warm-up bars needed before reliable signals (mirrors Pine's bar_index>=200)."""
        return 200
