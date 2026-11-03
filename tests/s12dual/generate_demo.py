"""Synthetic OHLCV for functional testing; NOT representative of trading performance."""
import numpy as np,pandas as pd

def make(n_days=15,seed=88):
    rng=np.random.default_rng(seed)
    ix=[]
    for day in pd.bdate_range('2026-08-03',periods=n_days,tz='Asia/Kolkata'):
        ix.extend(pd.date_range(day+pd.Timedelta(hours=9,minutes=15),periods=375,freq='1min',tz='Asia/Kolkata'))
    index=pd.DatetimeIndex(ix).tz_convert('UTC')
    returns=rng.normal(0,.0014,len(index))+np.sin(np.arange(len(index))/150)*.00010
    close=100*np.exp(np.cumsum(returns));op=np.r_[100.,close[:-1]]
    # New-day open gaps are intentional.
    hi=np.maximum(op,close)+rng.uniform(.01,.14,len(index))
    lo=np.minimum(op,close)-rng.uniform(.01,.14,len(index))
    vol=rng.integers(10000,70000,len(index)).astype(float)
    return pd.DataFrame({'open':op,'high':hi,'low':lo,'close':close,'volume':vol},index=index)

if __name__=='__main__':
    from pathlib import Path
    import sys
    sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
    from engine.s12dual.mtf import aggregate
    x=make(); root=Path(__file__).parent
    x.to_csv(root/'demo_1m.csv', index_label='time')
    aggregate(x,'1','5','Asia/Kolkata',555).to_csv(root/'demo_5m.csv', index_label='time')
    print('created demo OHLCV')
