"""Confirmed multi-timeframe mapping; no HTF future-candle leakage."""
import pandas as pd
import numpy as np
from .config import tf_minutes
from .indicators import calculate
from .timing import candle_closes


def require_candles(df, name):
    if not isinstance(df.index,pd.DatetimeIndex) or df.index.tz is None:
        raise ValueError(f'{name} index must have timezone-aware bar OPEN timestamps')
    if not df.index.is_monotonic_increasing or df.index.has_duplicates:
        raise ValueError(f'{name} requires sorted unique timestamps')
    for col in ('open','high','low','close'):
        if col not in df: raise ValueError(f'{name} missing {col}')
    if df[list(('open','high','low','close'))].isna().any().any():
        raise ValueError(f'{name} has missing OHLC values')
    if (df['low']>df['high']).any() or (df['high']<df[['open','close']].max(axis=1)).any() or (df['low']>df[['open','close']].min(axis=1)).any():
        raise ValueError(f'{name} invalid OHLC')
    if 'bar_close' in df:
        ends=pd.to_datetime(df['bar_close'],utc=True)
        if ends.isna().any() or (ends.array <= df.index.tz_convert('UTC').array).any():
            raise ValueError(f'{name} bar_close must be after candle OPEN')


def aggregate(src, src_tf, dst_tf, timezone, offset):
    """Resample without fabricating monthly/weekly periods from 24h durations.

    W/M open labels use the first observed trading candle in the group, not
    the calendar's midnight; `bar_close` is the final supporting bar close.
    Do not aggregate a timeframe with missing component bars expecting parity.
    """
    a,b=tf_minutes(src_tf),tf_minutes(dst_tf)
    if a>b or (b%a != 0 and b<1440):
        raise ValueError(f'Cannot reconstruct exact {dst_tf} from {src_tf}; supply finer source candles.')
    src=src.tz_convert(timezone)
    agg={'open':'first','high':'max','low':'min','close':'last'}
    if 'volume' in src:agg['volume']='sum'
    if dst_tf.endswith(('W','M')):
        # Calendar weeks and months, never a fixed 30-day approximation.
        n=int(dst_tf[:-1] or '1')
        if n!=1:raise ValueError('Multi-week/month aggregation needs exchange calendar input')
        groups=src.index.tz_localize(None).to_period('W-SUN' if dst_tf.endswith('W') else 'M')
        out=src.groupby(groups,sort=True).agg(agg)
        opening=src.groupby(groups,sort=True).apply(lambda group: group.index[0])
        out.index=pd.DatetimeIndex(opening.to_list())
    elif b>=1440:
        if dst_tf!='D':raise ValueError('Multi-day aggregation needs exchange calendar input')
        groups=(src.index-pd.Timedelta(minutes=offset)).normalize()
        out=src.groupby(groups,sort=True).agg(agg)
        # Label by the actual first supporting bar on each trading day.
        opening=src.groupby(groups,sort=True).apply(lambda group:group.index[0])
        out.index=pd.DatetimeIndex(opening.to_list())
    else:
        out=src.resample(f'{b}min',origin='start_day',offset=f'{offset}min',label='left',closed='left').agg(agg)
    out=out.dropna(subset=['open','close'])
    return out.tz_convert('UTC')


def aligned_feature(chart_idx, chart_tf, df, target_tf, column, *, stable=True,
                    timezone='Asia/Kolkata', session_end_min=930, chart_bar_close=None,
                    target_bar_close=None):
    """Align sealed Pine MTF features to chart bars without cross-period lookahead.

    True HTF: previous target candle containing the host OPEN.
    Same TF: current target candle.
    LTF: last target candle closed within [host open, host real close].
    """
    cm,tm=tf_minutes(chart_tf),tf_minutes(target_tf)
    host=pd.DatetimeIndex(chart_idx).tz_convert('UTC')
    target=df.index.tz_convert('UTC')
    values=df[column].to_numpy(dtype=float)
    if tm==cm:
        return df[column].reindex(host).to_numpy(dtype=float)
    chart=pd.DataFrame({'at':host,'_order':np.arange(len(host))})
    if tm>cm:
        # A higher-TF candle is NEVER visible to shorter bars until it seals.
        vals=pd.DataFrame({'at':target,'v':np.r_[np.nan,values[:-1]]})
        merged=pd.merge_asof(chart.sort_values('at'),vals.sort_values('at'),on='at',direction='backward')
    else:
        host_ends=candle_closes(host,chart_tf,timezone,session_end_min,chart_bar_close)
        low_ends=candle_closes(target,target_tf,timezone,session_end_min,target_bar_close)
        # Restrict selection to a host's interval; prior month/day is not an
        # acceptable fallback if no matching lower candle exists.
        chart['at']=host_ends
        vals=pd.DataFrame({'at':low_ends,'v':values,'start':target})
        merged=pd.merge_asof(chart.sort_values('at'),vals.sort_values('at'),on='at',direction='backward')
        merged.loc[merged['start']<host.take(merged['_order'].to_numpy()),'v']=np.nan
    return merged.sort_values('_order')['v'].to_numpy(dtype=float)


def build_mtf(chart, chart_tf, mtf, *, timezone='Asia/Kolkata', session_offset=555,
              session_end_min=930, stable=True, cache=None, reuse_prefix=None):
    if not stable:
        raise NotImplementedError('S2 developing-HTF Pine mode needs historical developing candle snapshots.')
    sources={chart_tf:chart}
    sources.update(mtf or {})
    needed=['5','15','60','D']
    charts={}
    for target in needed:
        from_chart=(target==chart_tf)
        if target in sources:
            f=sources[target].copy()
        else:
            candidates=[]
            for t,d in sources.items():
                try:
                    if tf_minutes(t)<tf_minutes(target) and (tf_minutes(target)%tf_minutes(t)==0 or target=='D'):
                        candidates.append((tf_minutes(t),t,d))
                except ValueError:pass
            if not candidates:
                raise ValueError(f'Need true {target} OHLCV candles or finer source for chart TF={chart_tf}.')
            # Prefer an explicit auxiliary feed over rebuilding from the
            # mutable chart history; it can be safely cached until replaced.
            external=[z for z in candidates if z[1] in (mtf or {})]
            _,base_tf,base=max(external or candidates,key=lambda z:z[0])
            from_chart=(base_tf==chart_tf)
            if cache is not None and not from_chart and target in cache:
                charts[target]=cache[target]
                continue
            f=aggregate(base,base_tf,target,timezone,session_offset)
        if cache is not None and not from_chart and target in cache:
            charts[target]=cache[target]
            continue
        require_candles(f,target)
        charts[target]=calculate(f,timezone)
        if cache is not None and not from_chart:
            cache[target]=charts[target]
    result={}

    def align(key, tf, d, column):
        past=reuse_prefix.get(key) if reuse_prefix is not None else None
        skip=len(past) if past is not None and len(past)<=len(chart) else 0
        if past is not None and skip==len(chart):
            result[key]=past
            return
        host_idx=chart.index[skip:]
        host_close=chart['bar_close'].iloc[skip:] if 'bar_close' in chart else None
        tail=aligned_feature(host_idx,chart_tf,d,tf,column,stable=stable,
                             timezone=timezone,session_end_min=session_end_min,
                             chart_bar_close=host_close,
                             target_bar_close=d['bar_close'] if 'bar_close' in d else None)
        result[key]=np.concatenate([past,tail]) if skip else tail

    for tf,d in charts.items():
        for key, column in [('close','close'),('sma20','sma20')]:
            align(f'{tf}_{key}',tf,d,column)
    d=charts['5']
    align('5_rsi14','5',d,'rsi14')
    d=charts['15'].copy()
    d['ema9_slope']=d['ema9'].diff()
    for col in ('ema9','ema21','ema9_slope'):
        align(f'15_{col}','15',d,col)
    return result
