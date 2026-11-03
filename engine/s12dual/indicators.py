"""Pine-like closed-bar indicators. One vectorized calculation per shared series."""
import numpy as np
import pandas as pd


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    # Pine ta.ema seeds with first non-NaN; pandas does the same with adjust=False.
    return s.ewm(span=n, adjust=False, ignore_na=True).mean()


def rma(s: pd.Series, n: int) -> pd.Series:
    """Wilder RMA seeded by SMA of first n valid readings, as Pine ta.rma."""
    a = s.astype(float).to_numpy()
    out = np.full(len(a), np.nan)
    valid = 0
    acc = 0.0
    last = np.nan
    for i, x in enumerate(a):
        if np.isnan(x):
            continue
        if valid < n:
            valid += 1
            acc += x
            if valid == n:
                last = acc / n
                out[i] = last
        else:
            last += (x - last) / n
            out[i] = last
    return pd.Series(out, index=s.index)


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    change = close.diff()
    up = rma(change.clip(lower=0), n)
    down = rma((-change).clip(lower=0), n)
    rs = up / down
    result = 100 - 100 / (1 + rs)
    return result.mask((down == 0) & (up > 0), 100.0).mask((up == 0) & (down > 0), 0.0).mask((up == 0) & (down == 0), 50.0)


def calculate(df: pd.DataFrame, session_tz: str = 'Asia/Kolkata') -> pd.DataFrame:
    x = df.copy()
    o, h, l, c = (x[z].astype(float) for z in ('open', 'high', 'low', 'close'))
    for n in (9, 13, 21, 50, 200):
        x[f'ema{n}'] = ema(c, n)
    x['sma20'] = sma(c, 20)
    x['rsi14'] = rsi(c)
    prev = c.shift(1)
    tr = pd.concat([h-l,(h-prev).abs(),(l-prev).abs()],axis=1).max(axis=1)
    tr.iloc[0] = np.nan  # Pine first ATR uses high-low via ta.tr(true)
    tr.iloc[0] = (h-l).iloc[0]
    x['atr14'] = rma(tr,14)
    x['atrma50'] = sma(x['atr14'],50)
    x['adx_tr'] = tr
    up = h.diff()
    down = -l.diff()
    dmplus = pd.Series(np.where((up>down)&(up>0),up,0.0),index=x.index)
    dmminus = pd.Series(np.where((down>up)&(down>0),down,0.0),index=x.index)
    dmplus.iloc[0] = np.nan
    dmminus.iloc[0] = np.nan
    str14 = rma(tr,14)
    dip = 100*rma(dmplus,14)/str14
    dim = 100*rma(dmminus,14)/str14
    denom = dip+dim
    dx = 100*(dip-dim).abs()/denom
    dx = dx.mask(denom==0,0.0)
    x['di_plus'],x['di_minus'],x['adx'] = dip,dim,rma(dx,14)
    mac = ema(c,12)-ema(c,26)
    x['macd12_26'],x['macdsig9'] = mac,ema(mac,9)
    x['macdhist'] = x['macd12_26']-x['macdsig9']
    x['volsma20'] = sma(x['volume'],20) if 'volume' in x else np.nan
    x['relvol'] = x['volume']/x['volsma20'] if 'volume' in x else np.nan
    x['atr_exp'] = (x['atr14']/x['atrma50']).fillna(1.)
    # Session VWAP: known exchange session/date from each candle's timestamp.
    typical = (h+l+c)/3
    vol = x['volume'] if 'volume' in x else pd.Series(np.nan,index=x.index)
    datekeys = x.index.tz_convert(session_tz).date
    num=(typical*vol).groupby(datekeys).cumsum()
    den=vol.groupby(datekeys).cumsum()
    x['vwap']=(num/den).where(den>0)
    x['hlc3std20'] = ((h+l+c)/3).rolling(20,min_periods=20).std(ddof=0)
    x['avg_body14'] = (c-o).abs().rolling(14,min_periods=14).mean()
    x['avg_range14'] = (h-l).rolling(14,min_periods=14).mean()
    # Previous-20 and 5-bar extremes, strictly offset, avoids lookahead.
    x['hi_prev20'] = h.shift(1).rolling(20,min_periods=20).max()
    x['lo_prev20'] = l.shift(1).rolling(20,min_periods=20).min()
    x['hi_prev5'] = h.shift(1).rolling(5,min_periods=5).max()
    x['lo_prev5'] = l.shift(1).rolling(5,min_periods=5).min()
    x['hi20'] = h.rolling(20,min_periods=20).max()
    x['lo20'] = l.rolling(20,min_periods=20).min()
    return x


def adaptive_macd(df: pd.DataFrame, fast: int, slow: int, sig: int) -> pd.DataFrame:
    m = ema(df['close'],fast) - ema(df['close'],slow)
    ms = ema(m,sig)
    hh = m-ms
    r = pd.DataFrame(index=df.index)
    r['s2_macd']=m
    r['s2_macd_sig']=ms
    r['s2_macdhist']=hh
    r['s2_macdhist_avgabs14']=sma(hh.abs(),14).fillna(.0001)
    return r
