"""
apex_emacross/session.py
NSE session filter — mirrors Pine Script Section 5.

Blocks:
  09:15–09:30  (opening noise zone)
  15:00–15:30  (closing noise zone)

All checks use IST time extracted from the DataFrame's DatetimeIndex.
"""

import pandas as pd
import numpy as np
from datetime import time


def is_session_ok(dt: pd.Timestamp, config) -> bool:
    """
    Single-bar session check (for live / streaming use).
    Returns True if the bar's time is allowed to trade.
    """
    if not config.use_session:
        return True
    hhmm = dt.hour * 100 + dt.minute
    if config.block_open  and 915  <= hhmm < 930:
        return False
    if config.block_close and 1500 <= hhmm < 1530:
        return False
    return True


def compute_session_mask(df: pd.DataFrame, config) -> pd.Series:
    """
    Vectorised session mask for a full DataFrame.
    Returns bool Series: True = bar is tradeable.
    """
    if not config.use_session:
        return pd.Series(True, index=df.index)

    hhmm = df.index.hour * 100 + df.index.minute
    mask = pd.Series(True, index=df.index)

    if config.block_open:
        mask &= ~((hhmm >= 915) & (hhmm < 930))
    if config.block_close:
        mask &= ~((hhmm >= 1500) & (hhmm < 1530))

    return mask
