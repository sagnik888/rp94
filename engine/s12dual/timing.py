"""Exchange-local candle clocks and calendar-boundary-safe as-of alignment.

Index values are candle OPEN timestamps. Explicit `bar_close` inputs are preferred
for holidays, early closes, overnight and nonstandard sessions. Defaults model
regular NSE/BSE (09:15–15:30 IST), MCX (09:00–23:30 IST), US cash
(09:30–16:00 America/New_York); holidays/half-days are not inferred.
"""
from __future__ import annotations
import re
import pandas as pd
import numpy as np


def _components(tf):
    match=re.fullmatch(r'(\d+)?([DWM])',str(tf))
    if match:return int(match.group(1) or 1), match.group(2)
    if str(tf).isdigit() and int(tf)>0:return int(tf),'min'
    raise ValueError(f'Unsupported candle timeframe {tf!r}')


def _last_weekday(ts):
    while ts.weekday()>=5:ts-=pd.Timedelta(days=1)
    return ts


def candle_closes(index, tf, timezone='Asia/Kolkata', session_end_min=930,
                  explicit=None):
    """Timezone-aware close stamps for complete bars, no fixed 30-day months.

    Weekly/monthly derived closes are estimates unless explicit is supplied;
    the caller should use true broker/exchange bar_close values in production.
    """
    index=pd.DatetimeIndex(index)
    if index.tz is None:raise ValueError('Candle OPEN index must be timezone-aware')
    if explicit is not None:
        closes=pd.DatetimeIndex(pd.to_datetime(explicit,utc=True))
        if len(closes)!=len(index) or closes.isna().any() or (closes<=index.tz_convert('UTC')).any():
            raise ValueError('bar_close must be after each corresponding bar_open')
        return closes.tz_convert('UTC')
    n,unit=_components(tf)
    local=index.tz_convert(timezone)
    if unit=='min':
        closes=local+pd.Timedelta(minutes=n)
        cutoff=local.normalize()+pd.Timedelta(minutes=session_end_min)
        mask=(local<cutoff)&(closes>cutoff)
        closes=closes.where(~mask,cutoff)
    elif unit=='D':
        if n!=1:raise ValueError('Multi-day chart requires actual bar_close timestamps')
        closes=local.normalize()+pd.Timedelta(minutes=session_end_min)
    elif unit=='W':
        if n!=1:raise ValueError('Multi-week chart requires actual bar_close timestamps')
        days=4-local.weekday
        closes=(local.normalize()+pd.to_timedelta(days,unit='D')
                +pd.Timedelta(minutes=session_end_min))
    else:
        if n!=1:raise ValueError('Multi-month chart requires actual bar_close timestamps')
        # MonthBegin respects 28/29/30/31-day months; BDay skips weekends.
        first_next=local.normalize()+pd.offsets.MonthBegin(1)
        closes=first_next-pd.offsets.BDay(1)+pd.Timedelta(minutes=session_end_min)
    if (closes<=local).any():
        raise ValueError('Estimated candle close precedes its OPEN; supply explicit bar_close')
    return closes.tz_convert('UTC')


def chart_closes(chart, tf, timezone, session_end_min):
    supplied=chart['bar_close'] if 'bar_close' in chart else None
    return candle_closes(chart.index,tf,timezone,session_end_min,supplied)
