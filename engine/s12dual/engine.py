"""Dual independent S1/S2 Pine-v5.4 signal scanner (closed-bar, no brokerage orders).

Input index = timezone-aware candle OPEN times. run() returns events, bars, ledger.
Chart representations, Pine UI, and theoretical broker fills are intentionally omitted.
"""
from __future__ import annotations
from dataclasses import dataclass,field,asdict
from typing import Optional
import math
import re
import numpy as np
import pandas as pd
from .config import Config,MACD_BY_TF,tf_minutes,auto_mode,mode_cap
from .indicators import calculate,adaptive_macd
from .mtf import build_mtf,require_candles
from .timing import chart_closes, candle_closes
from .generated_rules import s1_pattern,s2_pattern


def valid(x): return x is not None and not pd.isna(x)
def v(x,d=0.): return x if valid(x) else d
def gt(x,y): return valid(x) and valid(y) and x>y
def lt(x,y): return valid(x) and valid(y) and x<y
def r2(x): return float(x)

def nearest_stop(px,tick,long):
    if tick<=0:return px
    z=px/tick
    return (math.ceil(z-1e-10) if long else math.floor(z+1e-10))*tick

@dataclass
class Trade:
    side: str
    entry: float
    entry_bar: int
    trade_id: str
    stop: float
    trailing: float
    t1: float
    t2: float
    t3: float
    setup: str
    subtype: str='NONE'
    t1_hit: bool=False
    t2_hit: bool=False
    t3_hit: bool=False
    locked: bool=False
    early_be: bool=False
    early_loss_armed: bool=False
    peak: float=0.
    trough: float=0.
    exit_count: int=0
    exit_long_count: int=0
    exit_short_count: int=0

    @property
    def long(self):return self.side=='LONG'

@dataclass
class EngineState:
    active: Optional[Trade]=None
    pending: Optional[dict]=None
    last_signal_bar: int=0
    last_signal_direction: int=0
    losses: int=0
    pause_until: int=0
    total: int=0
    wins: int=0
    flat: int=0
    net_pct: float=0.
    trades: list=field(default_factory=list)


def hard_stop_price(ep,long,pct):
    return ep*(1-pct/100 if long else 1+pct/100)

def targets(ep,stop,long,t1,t2,t3,floor=0.):
    risk=max(abs(ep-stop),floor)
    sign=1 if long else -1
    return (ep+sign*risk*t1,ep+sign*risk*t2,ep+sign*risk*t3)

def pnl(trade,px,friction):
    return (px-trade.entry)*(1 if trade.long else -1)/trade.entry*100-friction


# App-owned exit policy layered on the imported Pine scanner. Winners use an R-based
# ratchet after T1 so an ATR wider than the user's 0.1% spot risk cannot leave the stop
# parked at the original loss level. A weak, still-unproven trade can tighten to -0.5R.
RUNNER_GIVEBACK_R=1.0
EARLY_LOSS_ARM_R=.25
EARLY_LOSS_STOP_R=.75
EARLY_LOSS_MAX_FAVORABLE_R=.25


def runner_stop_price(entry,long,peak_or_trough,risk,current_stop,close,tick,lock_floor_r):
    """Ratchet a proven runner by R; milestones never become terminal price targets."""
    if risk<=0:return current_stop
    peak_r=((peak_or_trough-entry) if long else (entry-peak_or_trough))/risk
    lock_r=max(float(lock_floor_r),peak_r-RUNNER_GIVEBACK_R)
    desired=entry+(1 if long else -1)*risk*lock_r
    # A closed bar may only publish a stop that was not already crossed on that bar.
    desired=min(desired,close-tick) if long else max(desired,close+tick)
    desired=nearest_stop(desired,tick,long)
    return max(current_stop,desired) if long else min(current_stop,desired)


def early_loss_action(entry,long,close,risk,best_price,current_stop,weakness_count,t1_hit,tick=.05):
    """Return (ratcheted stop, exit now) for a weak trade before it proves itself.

    Two independent weakness clues plus at least -0.25R arm a -0.75R stop for the next
    bar. If the confirmed bar already closes through that level, exit at its close.
    A trade that reached T1 or had +0.25R favorable excursion is left to runner logic.
    """
    if risk<=0 or t1_hit or weakness_count<2:return current_stop,False
    best_r=((best_price-entry) if long else (entry-best_price))/risk
    loss_r=((close-entry) if long else (entry-close))/risk
    if best_r>=EARLY_LOSS_MAX_FAVORABLE_R or loss_r>-EARLY_LOSS_ARM_R:
        return current_stop,False
    cut=nearest_stop(entry-(1 if long else -1)*risk*EARLY_LOSS_STOP_R,tick,long)
    if loss_r<=-EARLY_LOSS_STOP_R:return current_stop,True
    return (max(current_stop,cut) if long else min(current_stop,cut)),False

class DualScanner:
    def __init__(self, candles:pd.DataFrame, *, symbol:str, timeframe:str='5',
                 mtf:Optional[dict[str,pd.DataFrame]]=None, config:Config=None,
                 tick_size:float=.05, instrument_type:Optional[str]=None,
                 security_type:Optional[str]=None, session_timezone:Optional[str]=None, native_session:Optional[tuple]=None):
        self.cfg=config or Config()
        self._mtf_raw={k:v.copy() for k,v in (mtf or {}).items()}
        self._processed=0
        self.cfg.validate()
        self.chart_tf=timeframe
        self.minutes=tf_minutes(timeframe)
        self.symbol=symbol
        self.exchange=symbol.split(':')[0].upper() if ':' in symbol else ''
        self.instrument_type=self._detect_instrument(instrument_type,security_type)  # S1 manual classification only
        self.security_type=self._source_security_type(security_type)
        self.s2_is_index,self.s2_is_commodity=self._s2_source_types()
        self.tick=tick_size
        if tick_size<=0: raise ValueError('tick_size must be positive')
        # The chart's EXCHANGE clock is independent of the S2 user-selected
        # market region. This is essential for S1 NSE/BSE EOD parity.
        self.native_region=self._native_region()
        self.region=self._region()  # S2-only market-region override
        self.tz=session_timezone or ('Asia/Kolkata' if self.native_region=='India'
                 else 'America/New_York' if self.native_region=='US' else None)
        if self.tz is None:
            raise ValueError('Unknown exchange: supply session_timezone and native_session=(open,close)')
        self.s2_tz=('Asia/Kolkata' if self.region=='India' else
                    'America/New_York' if self.region=='US' else self.tz)
        self.native_session=native_session
        self.session_offset=(540 if self.native_region=='India' and self.s2_is_commodity
                       else 555 if self.native_region=='India' else 570 if self.native_region=='US'
                       else native_session[0] if native_session else 0)
        self.session_end=(1410 if self.native_region=='India' and self.s2_is_commodity
                     else 930 if self.native_region=='India' else 960 if self.native_region=='US'
                     else native_session[1] if native_session else 0)
        require_candles(candles,'chart')
        for k,d in (mtf or {}).items(): require_candles(d,f'MTF {k}')
        self._bar_closes=chart_closes(candles,timeframe,self.tz,self.session_end)
        self._explicit_bar_closes='bar_close' in candles
        self._mtf_cache={}
        self.data=calculate(candles,self.tz)
        m=MACD_BY_TF.get(timeframe,(12,26,9,.45))
        self.macd_ratio_threshold=m[3]
        self.data=self.data.join(adaptive_macd(self.data,*m[:3]))
        self.mtf=build_mtf(candles,timeframe,mtf or {},timezone=self.tz,session_offset=self.session_offset,session_end_min=self.session_end,stable=self.cfg.s2_confirmed_mtf,cache=self._mtf_cache)
        self.india_cash=self.exchange in {'NSE','BSE'}
        self.s1=EngineState()
        self.s2=EngineState()
        self.events=[]
        self.snapshots=[]
        self.div_piv_low=[]
        self.div_piv_high=[]
        self.orb_high=np.nan
        self.orb_low=np.nan
        self.orb_start=0
        self.orb_formed=False
        self._arr={k:self.data[k].to_numpy() for k in self.data.columns}
        self._times=self.data.index
        self._local=self.data.index.tz_convert(self.tz)
        self._s2_local=self.data.index.tz_convert(self.s2_tz)
        self._tf_duration=pd.Timedelta(minutes=self.minutes)
        self._s1_mode=self.cfg.s1_trade_mode if self.cfg.s1_trade_mode!='Auto' else auto_mode(self.minutes)
        if self.cfg.s2_trade_mode!='Auto': self._s2_mode=self.cfg.s2_trade_mode
        elif self.cfg.share_auto_mode:self._s2_mode=self._s1_mode
        else:self._s2_mode=('Intraday' if self.minutes<=30 else 'Swing' if self.minutes<=240 else 'Positional')

    def _detect_instrument(self, selected, security_type):
        """Pine's chart metadata cannot be inferred reliably from broker tickers.

        `security_type` accepts broker categories stock/index/futures/commodity/
        crypto/forex. Explicit `instrument_type` overrides Config and metadata.
        Automatic guesses are intentionally narrow to protect ETFs such as
        NIFTYBEES from being mistaken for NIFTY index underlying.
        """
        original=selected if selected is not None else self.cfg.instrument_type
        aliases={'Nifty 50':'index','Bank Nifty':'index','FinNifty':'index',
                 'Midcap':'index','Sensex':'index','Index':'index','Stock':'stock',
                 'Commodity':'commodity','Crypto':'crypto','Forex':'forex',
                 'Futures':'futures',
                 'stock':'stock','index':'index','commodity':'commodity',
                 'futures':'futures','crypto':'crypto','forex':'forex'}
        if original!='Auto-Detect':
            if original not in aliases:raise ValueError('instrument_type invalid')
            return aliases[original]
        typ=(security_type or '').lower()
        if typ and typ not in {'stock','index','futures','commodity','crypto','forex','cfd'}:
            raise ValueError('Unknown security_type: provide a supported broker security category')
        root=self.symbol.split(':')[-1].upper()
        if self.exchange=='MCX':return 'commodity'
        if typ in {'index','crypto','forex','commodity'}:return typ
        if typ=='futures':return 'futures'
        if typ=='cfd' and any(z in root for z in ('CRUDE','GOLD','SILVER','OIL','NATGAS')):return 'commodity'
        if (root in {'NIFTY','NIFTY50','NIFTY 50','BANKNIFTY','BANK NIFTY',
                     'FINNIFTY','MIDCPNIFTY','MIDCAPNIFTY','SENSEX','BANKEX'}
            or re.fullmatch(r'(NIFTY|BANKNIFTY|FINNIFTY|SENSEX|MIDCPNIFTY)\d{2}[A-Z]{3}\d{2,4}.*',root)):
            return 'index' if typ!='futures' else 'futures'
        if typ=='stock':return 'stock'
        if self.exchange in {'OANDA','FX','FOREX'}:return 'forex'
        if self.exchange in {'BINANCE','COINBASE','KRAKEN','BYBIT','BITSTAMP','OKX'}:return 'crypto'
        return 'stock'

    def _source_security_type(self, security_type):
        """Best available syminfo.type; NEVER use S1 manual instrument override.

        Supply broker security_type for futures/CFDs and ambiguous instruments.
        """
        if security_type:
            return security_type.lower()
        # If the caller omits metadata, only unambiguous chart tokens are guessed.
        root=self.symbol.split(':')[-1].upper()
        if root in {'NIFTY','NIFTY50','BANKNIFTY','FINNIFTY','MIDCAPNIFTY',
                    'MIDCPNIFTY','SENSEX','BANKEX'}:return 'index'
        if self.exchange in {'MCX'}:return 'futures'
        if self.exchange in {'BINANCE','COINBASE','KRAKEN','BYBIT','BITSTAMP','OKX'}:return 'crypto'
        if self.exchange in {'OANDA','FX','FOREX'}:return 'forex'
        return 'stock'

    def _s2_source_types(self):
        """Recreate Pine v5.4 s2_isIndex/isComm using source symbol/type flags.

        Pine instrumentType dropdown is not an input to either S2 flag.
        """
        symbol=self.symbol.lower()
        root=symbol.split(':')[-1]
        typ=self.security_type
        bank=any(s in symbol for s in ('banknifty','bank_nifty','banknf'))
        fin=any(s in symbol for s in ('finnifty','fin_nifty','finnf'))
        mid='midcap' in symbol
        sensex=any(s in symbol for s in ('sensex','bankex'))
        omitted=('100','200','500','smallcap','it','auto','pharma','metal',
                 'realty','energy','infra','media','fmcg','psu','next')
        nifty=('nifty50' in symbol or
               ('nifty' in symbol and not (bank or fin or mid) and
                not any(token in symbol for token in omitted)))
        is_crypto=(typ=='crypto' or self.exchange in {'BINANCE','COINBASE','KRAKEN','BYBIT','BITSTAMP','OKX'})
        is_forex=(typ=='forex' or self.exchange in {'OANDA','PEPPERSTONE','FX','FOREX'} or
                  any(tok in symbol for tok in ('eurusd','gbpusd','usdjpy','audusd','usdcad','usdchf','nzdusd','usdinr','eurgbp','eurjpy','gbpjpy','usdsgd','usdmxn','usdtry','usdzar','usdhkd')))
        commodity_names=('gold','silver','crude','crudeoil','copper','naturalgas',
            'natgas','xauusd','xagusd','xptusd','xpdusd','usoil','ukoil',
            'wti','brent','aluminum','aluminium','nickel','zinc','lead',
            'cotton','mentha','palmolein','castor','rubber','wheat','corn','soybean')
        is_commodity=(self.exchange=='MCX' or
                      (typ in {'futures','cfd','commodity'} and
                       any(term in root for term in commodity_names) and
                       not is_crypto and not is_forex))
        is_index=(typ=='index' or (typ=='futures' and
                                   (bank or nifty or fin or mid or sensex)))
        return bool(is_index),bool(is_commodity)

    def _native_region(self):
        if self.exchange in {'NSE','BSE','MCX'}:return 'India'
        if self.exchange in {'NASDAQ','NYSE','NYSEARCA','AMEX'}:return 'US'
        return 'Native'

    def _region(self):
        # Pine S2 setting forces an S2-only session rule; S1 is unaffected.
        if self.cfg.s2_market_region=='India (NSE/BSE)':return 'India'
        if self.cfg.s2_market_region=='US (NYSE/NASDAQ)':return 'US'
        return self._native_region()

    def lag(self,k,offset=0):
        j=self.i-offset
        if j<0 or k not in self._arr:return np.nan
        return self._arr[k][j]

    def event(self,engine,event,**kwargs):
        dt=self._times[self.i].isoformat()
        ev=dict(engine=engine,event=event,id=f'{engine}|{self.symbol}|{self.chart_tf}|{dt}|{event}',symbol=self.symbol,timeframe=self.chart_tf,bar_open=dt,bar_close=self._bar_closes[self.i].isoformat(),bar_close_estimated=(not self._explicit_bar_closes and self.minutes>=1440),confirmed_bar=True,**kwargs)
        self.events.append(ev)
        return ev

    def _new_trade_id(self,name):
        return f'{name}|{self.symbol}|{self.chart_tf}|{self._times[self.i].isoformat()}|TRADE'

    def _session(self,engine='S2'):
        """Exchange-native S1 session or S2's independently configured clock."""
        if engine not in {'S1','S2'}:raise ValueError('engine must be S1 or S2')
        region=self.native_region if engine=='S1' else self.region
        local=self._local if engine=='S1' else self._s2_local
        loc=local[self.i]
        minute=loc.hour*60+loc.minute
        end=minute+self.minutes
        prev=local[self.i-1] if self.i else None
        prev_min=(prev.hour*60+prev.minute) if prev is not None else None
        commodity=(self.s2_is_commodity if engine=='S2' else self.s2_is_commodity)
        if region=='India':
            start,stop=(540,1410) if commodity else (555,930)
        elif region=='US':start,stop=570,960
        elif self.native_session:start,stop=self.native_session
        else:raise ValueError('Native session hours required; avoid guessing exchange schedule')
        in_session=(start<=minute<stop) if self.minutes<1440 else True
        prev_in=(prev_min is not None and start<=prev_min<stop) if self.minutes<1440 else False
        first=(prev is None or prev.date()!=loc.date() or (in_session and not prev_in))
        last=(in_session and end>=stop and self.minutes<1440)
        return dict(first=first,minute=minute,end=end,market=in_session,
                    session_start=start,session_end=stop,last=last,
                    region=region,is_commodity=commodity if region=='India' else False)

    def _s2_close_minute(self):
        """Minute of the bar's real close on S2's clock (Pine s2_istCloseDecimal/s2_sessionCloseDecimal)."""
        loc=self._bar_closes[self.i].tz_convert(self.s2_tz)
        return loc.hour*60+loc.minute

    def _mtf_regime(self,tf):
        a=self.mtf[f'{tf}_close'][self.i]
        b=self.mtf[f'{tf}_sma20'][self.i]
        if not valid(a) or not valid(b) or b==0:return 0
        return (1 if a>b else -1) if abs(a-b)/b>.005 else 0

    def _features(self):
        i=self.i;c=self.cfg
        ctx={k:self.lag(k) for k in ('open','high','low','close','volume','ema9','ema13','ema21','ema50','ema200','rsi14','adx','di_plus','di_minus','atr14','atrma50','atr_exp','vwap','volsma20','relvol','macd12_26','macdsig9','macdhist','hi_prev20','lo_prev20','hi_prev5','lo_prev5','sma20','hlc3std20','avg_body14','avg_range14','s2_macd','s2_macd_sig','s2_macdhist','s2_macdhist_avgabs14')}
        ctx.update(atrV=ctx['atr14'],ema9=ctx['ema9'],ema21=ctx['ema21'],bar_index=i)
        ctx['bullTrend']=ctx['close']>ctx['ema50']>ctx['ema200'] and ctx['adx']>20
        ctx['bearTrend']=ctx['close']<ctx['ema50']<ctx['ema200'] and ctx['adx']>20
        ctx['usableVolume']=valid(ctx['volume']) and valid(ctx['volsma20']) and ctx['volsma20']>0
        ctx['volSpike']=ctx['usableVolume'] and ctx['volume']>ctx['volsma20']*1.5
        ctx['volAbove']=ctx['usableVolume'] and ctx['relvol']>1.2
        ctx['highVol']=ctx['atr_exp']>1.25
        ctx['priceProxy']=(c.no_volume_policy=='Price proxy' and not ctx['usableVolume'] and ctx['atr_exp']>1 and ctx['adx']>=c.min_adx and abs(ctx['close']-ctx['open'])>=ctx['atrV']*.35)
        ctx['bodyPct']=(ctx['close']-ctx['low'])/(ctx['high']-ctx['low']) if ctx['high']>ctx['low'] else .5
        ctx['macdAccBull']=ctx['macdhist']>self.lag('macdhist',1)
        ctx['macdAccBear']=ctx['macdhist']<self.lag('macdhist',1)
        ctx['rsi5m']=self.mtf['5_rsi14'][i]
        ctx['ema9_15']=self.mtf['15_ema9'][i]
        ctx['ema21_15']=self.mtf['15_ema21'][i]
        ctx['ema9_15_slope']=self.mtf['15_ema9_slope'][i]
        ctx['mtf5']=self._mtf_regime('5')
        ctx['mtf15']=self._mtf_regime('15')
        ctx['mtf60']=self._mtf_regime('60')
        ctx['mtfD']=self._mtf_regime('D')
        ctx['new_day']=i>0 and self._local[i].date()!=self._local[i-1].date()
        return ctx

    def _record_exit(self,state,name,trade,price,reason):
        net=pnl(trade,price,self.cfg.round_trip_cost_pct)
        rec=dict(engine=name,trade_id=trade.trade_id,side=trade.side,entry=trade.entry,exit=float(price),entry_bar=trade.entry_bar,exit_bar=self.i,setup=trade.setup,reason=reason,net_pct=float(net))
        state.trades.append(rec)
        state.net_pct+=net
        if net>0:state.wins+=1;state.losses=0
        elif net<0:
            state.losses+=1
            if state.losses>=self.cfg.consecutive_loss_limit and (name=='S1' or self.cfg.s2_circuit):
                state.pause_until=self.i+self.cfg.circuit_pause_bars
        else:state.flat+=1;state.losses=0
        if reason in {'EARLY LOSS CUT','EARLY LOSS STOP'} and (name=='S1' or self.cfg.s2_circuit):
            # Give the failed setup time to reset; a 1m lane otherwise immediately
            # re-enters the same chop after being cut. Keep the longer configured pause.
            pause_bars=max(self.cfg.circuit_pause_bars,int(math.ceil(15/self.minutes)))
            state.pause_until=max(state.pause_until,self.i+pause_bars)
        if name=='S2':state.total+=1
        state.active=None
        self.event(name,'EXIT',trade_id=trade.trade_id,signal='EXIT_LONG' if trade.long else 'EXIT_SHORT',price=float(price),reason=reason,simulated=True,net_pct=float(net))

    def _milestones(self,name,tr,h,l):
        newly_hit=set()
        for k in ('t1','t2','t3'):
            level=getattr(tr,k)
            hit=(h>=level if tr.long else l<=level)
            if hit and not getattr(tr,k+'_hit'):
                setattr(tr,k+'_hit',True)
                newly_hit.add(k)
                self.event(name,k.upper(),trade_id=tr.trade_id,price=float(level),milestone_only=True)
        return newly_hit

    def run(self):
        for i in range(self._processed,len(self.data)):
            self.i=i
            ctx=self._features()
            self._s1_bar(ctx,self._session("S1"))
            self._s2_bar(ctx,self._session("S2"))
            self.snapshots.append(dict(time=self._times[i].isoformat(),symbol=self.symbol,
                                       close=float(ctx['close']),s1_side=self.s1.active.side if self.s1.active else 'NONE',
                                       s2_side=self.s2.active.side if self.s2.active else 'NONE',
                                       s1_stop=self.s1.active.trailing if self.s1.active else None,
                                       s2_stop=self.s2.active.trailing if self.s2.active else None,
                                       s1_bull=ctx.get('s1_bull'),s1_bear=ctx.get('s1_bear'),
                                       s2_bull=ctx.get('s2_bull'),s2_bear=ctx.get('s2_bear')))
        self._processed=len(self.data)
        ledger=sorted(self.s1.trades+self.s2.trades,key=lambda x:(x['exit_bar'],0 if x['engine']=='S2' else 1),reverse=True)
        return dict(events=list(self.events),bars=list(self.snapshots),ledger=ledger,positions=self.current_positions(),statistics={name:dict(total=s.total,wins=s.wins,flat=s.flat,loss_streak=s.losses,net_pct=float(s.net_pct)) for name,s in [('S1',self.s1),('S2',self.s2)]})

    def current_positions(self):
        """One actionable, effective protective stop per engine position.

        Initial risk stays internal for R-based targets and stop-vs-trail exit
        accounting. The broker-facing position contract has only ONE `stop`.
        """
        result={}
        for name,st in (('S1',self.s1),('S2',self.s2)):
            if st.active is None:
                result[name]=None
                continue
            tr=st.active
            details=asdict(tr)
            details.pop('trailing')
            details['stop']=float(tr.trailing)
            details['initial_risk_per_unit']=float(abs(tr.entry-tr.stop))
            result[name]=details
        return result

    def update(self, new_closed_candles:pd.DataFrame, *, mtf:Optional[dict[str,pd.DataFrame]]=None):
        """Continue from the previous state without replaying signals/ledger.

        `new_closed_candles` is strictly NEW closed bars (no mutable in-progress bar).
        `mtf` supplies UPDATED full auxiliary timeframe histories when external MTF
        data are used. Indicator arrays are recomputed: convenient, not O(1) ingest.
        The returned list contains ONLY events from the newly supplied candles.
        """
        if not self._processed:
            raise RuntimeError('run() historical warm-up before update()')
        require_candles(new_closed_candles,'new_closed_candles')
        if len(new_closed_candles)==0:return []
        if new_closed_candles.index[0]<=self._times[-1]:
            raise ValueError('update() must receive strictly later, non-overlapping closed candles')
        if mtf is not None:
            for tf,df in mtf.items():require_candles(df,'MTF '+tf)
            self._mtf_raw.update({k:v.copy() for k,v in mtf.items()})
            self._mtf_cache.clear()
        # An auxiliary timeframe finer than the chart MUST be current enough for
        # any lower-TF last intrabar values; stale data are not silently used.
        last_close=chart_closes(new_closed_candles,self.chart_tf,self.tz,self.session_end)[-1]
        for tf,df in self._mtf_raw.items():
            if tf_minutes(tf)<self.minutes and len(df) and candle_closes(df.index,tf,self.tz,self.session_end,df['bar_close'] if 'bar_close' in df else None)[-1]<last_close:
                raise ValueError(f'Stale {tf} MTF feed: provide updated full {tf} history')
        cols=['open','high','low','close']+(['volume'] if 'volume' in self.data else [])+(['bar_close'] if 'bar_close' in self.data else [])
        if ('bar_close' in new_closed_candles)!=('bar_close' in self.data):
            raise ValueError('bar_close metadata must be supplied consistently across run/update')
        extended=pd.concat([self.data[cols],new_closed_candles],axis=0)
        self._bar_closes=chart_closes(extended,self.chart_tf,self.tz,self.session_end)
        x=calculate(extended,self.tz)
        params=MACD_BY_TF.get(self.chart_tf,(12,26,9,.45))
        self.data=x.join(adaptive_macd(x,*params[:3]))
        self.mtf=build_mtf(extended,self.chart_tf,self._mtf_raw,timezone=self.tz,session_offset=self.session_offset,session_end_min=self.session_end,stable=self.cfg.s2_confirmed_mtf,cache=self._mtf_cache,reuse_prefix=self.mtf if mtf is None else None)
        self._arr={k:self.data[k].to_numpy() for k in self.data.columns}
        self._times=self.data.index
        self._local=self.data.index.tz_convert(self.tz)
        self._s2_local=self.data.index.tz_convert(self.s2_tz)
        n=len(self.events)
        self.run()
        return self.events[n:]

    def _s1_reason(self,x,pat,long):
        mac=x['macd12_26']; sig=x['macdsig9'];prev_m=self.lag('macd12_26',1);prev_s=self.lag('macdsig9',1)
        cross_up=valid(prev_m) and prev_m<=prev_s and mac>sig
        cross_dn=valid(prev_m) and prev_m>=prev_s and mac<sig
        breakout=x['high']>x['hi_prev20'] and x['volSpike'] and x['close']>x['open']
        breakdown=x['low']<x['lo_prev20'] and x['volSpike'] and x['close']<x['open']
        pullbuy=x['bullTrend'] and x['close']<x['ema21'] and x['close']>x['ema50'] and 40<x['rsi14']<60
        pullsell=x['bearTrend'] and x['close']>x['ema21'] and x['close']<x['ema50'] and 40<x['rsi14']<60
        mom_buy=cross_up and x['volSpike'] and x['close']>x['ema21']
        mom_sell=cross_dn and x['volSpike'] and x['close']<x['ema21']
        revbuy=x['rsi14']<35 and x['close']>x['open'] and x['close']>self.lag('high',1) and mac>sig
        revsell=x['rsi14']>65 and x['close']<x['open'] and x['close']<self.lag('low',1) and mac<sig
        if long:return 'Breakout' if breakout else 'Pullback' if pullbuy else 'Momentum' if mom_buy else 'Reversal' if revbuy else pat['bull_name'] if pat['bull'] and pat['bull_name'] else 'Trend'
        return 'Breakdown' if breakdown else 'Pullback' if pullsell else 'Momentum' if mom_sell else 'Reversal' if revsell else pat['bear_name'] if pat['bear'] and pat['bear_name'] else 'Trend'

    def _s1_new_trade(self,pending,x):
        c=self.cfg;st=self.s1;long=pending['long'];ep=float(x['open']);mode=self._s1_mode
        subtype=('BTST' if long else 'STBT') if mode=='BTST/STBT' else 'NONE'
        cap=mode_cap(c,mode,subtype)
        pct=min(c.s1_stop_pct,cap) if c.hard_stop_enabled else c.s1_stop_pct
        stop=nearest_stop(hard_stop_price(ep,long,pct),self.tick,long)
        t1,t2,t3=targets(ep,stop,long,c.t1_r,max(c.t1_r+.25,c.t2_r),max(max(c.t1_r+.25,c.t2_r)+.25,c.t3_r))
        trade=Trade('LONG' if long else 'SHORT',ep,self.i,pending['id'],stop,stop,t1,t2,t3,pending['reason'],subtype,peak=ep,trough=ep)
        st.active=trade;st.pending=None;st.total+=1
        self.event('S1','FILL_ESTIMATE',trade_id=trade.trade_id,simulated=True,execution='NEXT_OPEN_RECONSTRUCTED_ON_NEXT_CLOSE',price=ep,signal='BUY' if long else 'SELL',broker_fill_confirmed=False,stop=stop,t1=t1,t2=t2,t3=t3,setup=trade.setup,hard_stop_pct=float(cap),hard_stop_px=hard_stop_price(ep,long,cap))

    def _s1_bar(self,x,ses):
        c=self.cfg;s=self.s1;i=self.i;close=x['close'];o=x['open'];h=x['high'];l=x['low'];atr=x['atrV'];rsi=x['rsi14']
        s1_close_ist=self._bar_closes[i].tz_convert('Asia/Kolkata')
        s1_close_min=s1_close_ist.hour*60+s1_close_ist.minute
        s1_cash_eod=(self.india_cash and 555<=s1_close_min<=930 and
                     (s1_close_min>=c.s1_eod_cutoff_ist_min or
                      s1_close_min+self.minutes>c.s1_eod_cutoff_ist_min))
        s1_eod=(c.s1_force_eod and self._s1_mode=='Intraday' and self.minutes<1440 and
                (s1_cash_eod or (not self.india_cash and ses['last'])))
        pat=s1_pattern(x,self.lag,c) if valid(atr) and i>=5 else {'bull':False,'bear':False,'strong_bull':False,'strong_bear':False,'bull_name':'','bear_name':''}
        vwap=x['vwap'];dev=abs(close-vwap)/vwap if valid(vwap) and vwap>0 else 0.
        vwap_bull=(14 if dev>.003 else 7) if valid(vwap) and close>vwap else 0
        vwap_bear=(14 if dev>.003 else 7) if valid(vwap) and close<vwap else 0
        adx=x['adx'];minadx=c.min_adx;diP=x['di_plus'];diM=x['di_minus']; mac=x['macd12_26'];sig=x['macdsig9'];ema9=x['ema9'];ema21=x['ema21'];ema50=x['ema50']
        htfB=x['ema9_15']>x['ema21_15'] and x['ema9_15_slope']>0
        htfS=x['ema9_15']<x['ema21_15'] and x['ema9_15_slope']<0
        body=x['bodyPct'];volSpike=x['volSpike'];volAbove=x['volAbove']
        adxpts=20 if adx>25 else 10+(adx-minadx)*(10/max(1,25-minadx))
        b= (adxpts if adx>=minadx and diP>diM else 0)
        b+=(18 if x['macdAccBull'] else 10) if mac>sig else 0
        b+=vwap_bull
        b+=14 if ema9>ema21>ema50 else 7 if ema9>ema21 else 0
        b+=12 if volSpike and body>.6 else 6 if volAbove and body>.5 else 0
        b+=max(0,min(10,(rsi-50)*.5)) if valid(rsi) else 0
        b+=(8 if htfB else 0) if c.use_15m_gate else 4
        b+=4 if x['rsi5m']>50 else 0
        b+=4 if close>x['hi_prev5'] else 0
        b+=15 if c.s1_pattern_scoring and pat['strong_bull'] else 10 if c.s1_pattern_scoring and pat['bull'] else 0
        r=(adxpts if adx>=minadx and diM>diP else 0)
        r+=(18 if x['macdAccBear'] else 10) if mac<sig else 0
        r+=vwap_bear
        r+=14 if ema9<ema21<ema50 else 7 if ema9<ema21 else 0
        r+=12 if volSpike and body<.4 else 6 if volAbove and body<.5 else 0
        r+=max(0,min(10,(50-rsi)*.5)) if valid(rsi) else 0
        r+=(8 if htfS else 0) if c.use_15m_gate else 4
        r+=4 if x['rsi5m']<50 else 0
        r+=4 if close<x['lo_prev5'] else 0
        r+=15 if c.s1_pattern_scoring and pat['strong_bear'] else 10 if c.s1_pattern_scoring and pat['bear'] else 0
        b=min(b,100.);r=min(r,100.)
        x['s1_bull'],x['s1_bear']=float(b),float(r)
        risk_pct=min(c.s1_stop_pct,mode_cap(c,self._s1_mode)) if c.hard_stop_enabled else c.s1_stop_pct
        dyn_min=c.min_score+(16 if s.losses>=3 else 12 if s.losses==2 else 8 if s.losses==1 else 0)
        vb=(x['volAbove'] or x['volSpike']) if x['usableVolume'] else x['priceProxy']
        body_ok=abs(close-o)>=atr*.15 if valid(atr) else False
        can_signal=(close>0 and close*risk_pct/100>=self.tick and i-s.last_signal_bar>=c.s1_cooldown_bars and i>s.pause_until
                    and not s1_eod and (not c.block_conflicting_entries or self.s2.active is None))
        basic=(c.enable_s1 and valid(adx) and valid(atr) and i>=200 and adx>=minadx and vb and body_ok and can_signal and s.active is None and s.pending is None)
        buy=(basic and (not c.use_15m_gate or htfB) and b>=dyn_min and b-r>=c.conflict_margin and r<45 and body>.5 and s.last_signal_direction<=0)
        sell=(basic and (not c.use_15m_gate or htfS) and r>=dyn_min and r-b>=c.conflict_margin and b<45 and body<.5 and s.last_signal_direction>=0)
        # The Pine evaluation order computes S1 setups BEFORE reconciling a pending next-open entry.
        if buy or sell:
            pending={'long':bool(buy),'bar':i,'reason':self._s1_reason(x,pat,bool(buy)),'id':self._new_trade_id('S1')}
            s.pending=pending
            s.last_signal_bar=i;s.last_signal_direction=1 if buy else -1
            self.event('S1','SETUP',trade_id=pending['id'],signal='BUY' if buy else 'SELL',setup=pending['reason'],signal_close=float(close),expected_entry='NEXT_BAR_OPEN',broker_fill_confirmed=False,stop_pct=c.s1_stop_pct,hard_stop_pct=float(mode_cap(c,self._s1_mode,'BTST' if self._s1_mode=='BTST/STBT' else 'NONE')))
        if s.pending and i>s.pending['bar']:
            if s1_eod and self._s1_mode=='Intraday':
                pen=s.pending;s.pending=None;s.last_signal_direction=0
                self.event('S1','SETUP_CANCELLED',trade_id=pen['id'],reason='INTRADAY_CUTOFF')
            elif c.s1_cancel_overnight_cash and self.india_cash and self._s1_mode=='Intraday' and x['new_day']:
                pen=s.pending;s.pending=None;s.last_signal_direction=0
                self.event('S1','SETUP_CANCELLED',trade_id=pen['id'],reason='INTRADAY_NEW_DAY')
            else:self._s1_new_trade(s.pending,x)
        tr=s.active
        if tr is None:return
        long=tr.long;old=tr.trailing if c.s1_trail else tr.stop
        gap_stop=o<=old if long else o>=old
        touch_stop=l<=old if long else h>=old
        gap_t3=o>=tr.t3 if long else o<=tr.t3
        reason=None;px=None
        if gap_stop:
            reason='STOP GAP';px=o
        elif x['new_day'] and self._s1_mode=='Intraday' and not s1_eod:
            reason='ORPHAN INTRADAY NEXT OPEN';px=o
        elif gap_t3 and c.s1_exit_at_t3:
            self._milestones('S1',tr,h,l)
            reason='OPEN GAP THROUGH T3';px=o
        elif touch_stop:
            if gap_t3:self._milestones('S1',tr,h,l)
            reason='EARLY LOSS STOP' if tr.early_loss_armed else 'TRAIL/LOCK STOP' if old!=tr.stop else 'INITIAL STOP';px=old
        else:
            new_milestones=self._milestones('S1',tr,h,l)
            if c.s1_exit_at_t3 and tr.t3_hit:
                reason='T3 AUTO EXIT';px=tr.t3
            elif s1_eod and self._s1_mode=='Intraday':
                reason='INTRADAY EOD (CLOSE EST)';px=close
            else:
                risk=abs(tr.entry-tr.stop)
                if risk>0:
                    tr.peak=max(tr.peak,h)
                    tr.trough=min(tr.trough,l)
                ca=(mac<sig and x['macdAccBear']) if long else (mac>sig and x['macdAccBull'])
                cb=close<x['ema13'] if long else close>x['ema13']
                cc=(rsi<45 and rsi<self.lag('rsi14',1)) if long else (rsi>55 and rsi>self.lag('rsi14',1))
                if c.s1_trail and risk>0:
                    early_stop,early_exit=early_loss_action(
                        tr.entry,long,close,risk,tr.peak if long else tr.trough,tr.trailing,
                        int(ca)+int(cb)+int(cc),tr.t1_hit,self.tick)
                    if early_exit:
                        reason='EARLY LOSS CUT';px=close
                    elif early_stop!=tr.trailing:
                        tr.trailing=early_stop;tr.early_loss_armed=True
                if c.s1_trail and risk>0 and tr.t1_hit:
                    tr.trailing=runner_stop_price(tr.entry,long,tr.peak if long else tr.trough,
                                                  risk,tr.trailing,close,self.tick,.4)
                    tr.locked=True
                    tr.early_loss_armed=False
                met=int(ca)+int(cb)+int(cc)
                winner_runner=tr.t1_hit and (close>tr.entry if long else close<tr.entry)
                if reason is None:
                    if met>=(3 if tr.t1_hit else 2):tr.exit_count+=1
                    elif (close>ema9 if long else close<ema9):tr.exit_count=max(0,tr.exit_count-1)
                    if tr.exit_count>=c.s1_exit_confirm_bars and 't2' not in new_milestones and not winner_runner:
                        reason='MOMENTUM EXIT';px=close
                if reason is None and c.s1_trail:
                    if risk>0:
                        rr=((close-tr.entry) if long else (tr.entry-close))/risk
                        if rr>=c.s1_trail_start_r:
                            mul=c.s1_trail_atr*(1.5 if x['highVol'] and not tr.t2_hit else 1.3 if tr.t2_hit else 1.)
                            proposal=(tr.peak-atr*mul) if long else (tr.trough+atr*mul)
                            tr.trailing=max(tr.trailing,min(close-self.tick,proposal)) if long else min(tr.trailing,max(close+self.tick,proposal))
        if reason is None and (tr.trailing>=tr.entry if long else tr.trailing<=tr.entry):
            tr.early_loss_armed=False
        if reason:
            self._record_exit(s,'S1',tr,px,reason)
            s.last_signal_direction=0
        elif c.s1_trail and tr.trailing!=old and c.emit_stop_updates:
            self.event('S1','STOP_UPDATED',trade_id=tr.trade_id,stop=float(tr.trailing),effective='NEXT_BAR',simulated=True)

    def _s2_stop(self,x,long,entry,subtype):
        c=self.cfg;mode=self._s2_mode;cap=mode_cap(c,mode,subtype)
        atr=x['atrV'];lo=self.lag('low',1);hi=self.lag('high',1)
        struct=lo if long else hi
        for j in range(1,6):
            lj=self.lag('low',j);hj=self.lag('high',j)
            if long and lj<struct+atr*3:struct=min(struct,lj)
            if not long and hj>struct-atr*3:struct=max(struct,hj)
        swing=struct-atr*.2 if long else struct+atr*.2
        if c.s2_risk_mode=='Dynamic':
            vf=max(.7,min(atr/x['atrma50'],2.2))
            tf=.70 if mode=='Intraday' else 1. if mode in {'Swing','BTST/STBT'} else 1.40
            dmul=c.s2_atr_stop_mult*vf*tf*c.s2_dynamic_sensitivity
            atr_stop=entry-atr*dmul if long else entry+atr*dmul
            raw=min(atr_stop,swing) if long else max(atr_stop,swing)
        elif c.s2_risk_mode=='ATR':raw=entry-atr*c.s2_atr_stop_mult if long else entry+atr*c.s2_atr_stop_mult
        elif c.s2_risk_mode=='Percentage':raw=hard_stop_price(entry,long,c.s2_percent_stop_pct)
        else:raw=swing
        minpct=min(c.s2_min_stop_pct,cap) if c.hard_stop_enabled else c.s2_min_stop_pct
        mind=entry*minpct/100
        if minpct>0 and abs(raw-entry)<mind:raw=entry-mind if long else entry+mind
        upct=c.s2_max_stop_pct
        maxpct=min(cap,upct) if c.hard_stop_enabled and upct>0 else cap if c.hard_stop_enabled else upct
        maxd=entry*maxpct/100
        if maxpct>0 and abs(raw-entry)>maxd:raw=entry-maxd if long else entry+maxd
        if c.hard_stop_enabled:
            h=hard_stop_price(entry,long,cap)
            raw=max(raw,h) if long else min(raw,h)
        if long and raw>=entry:raw=entry-max(self.tick,mind)
        if not long and raw<=entry:raw=entry+max(self.tick,mind)
        # Pine tick-snap: toward entry, but at least one tick off entry.
        raw=nearest_stop(raw,self.tick,long)
        return min(entry-self.tick,raw) if long else max(entry+self.tick,raw)

    def _s2_new_trade(self,x,long,btst_window,ratios,slip,conf,mtfscore,burst):
        c=self.cfg;s=self.s2;ep=x['close']*(1+slip/100 if long else 1-slip/100)
        subtype=(('BTST' if long else 'STBT') if (self._s2_mode=='BTST/STBT' or (c.s2_enable_btst and btst_window)) else 'NONE')
        sl=self._s2_stop(x,long,ep,subtype)
        t1,t2,t3=targets(ep,sl,long,*ratios,floor=.0001)
        tr=Trade('LONG' if long else 'SHORT',ep,self.i,self._new_trade_id('S2'),sl,sl,t1,t2,t3,'S2 CONFLUENCE',subtype,peak=x['close'],trough=x['close'])
        s.active=tr;s.last_signal_bar=self.i
        self.event('S2','ENTRY',trade_id=tr.trade_id,signal=('BTST_LONG' if subtype=='BTST' else 'BUY') if long else ('STBT_SHORT' if subtype=='STBT' else 'SELL'),action='BUY' if long else 'SELL',
                   burst=bool(burst),reference_close=float(x['close']),confidence=float(conf),adx=float(x['adx']),htf_score=float(mtfscore),
                   price=float(ep),theoretical_slippage_pct=float(slip),broker_fill_confirmed=False,hard_stop_pct=float(mode_cap(c,self._s2_mode,subtype)),
                   hard_stop_px=float(hard_stop_price(ep,long,mode_cap(c,self._s2_mode,subtype))),
                   stop=float(sl),t1=float(t1),t2=float(t2),t3=float(t3),subtype=subtype,simulated=True)

    def _s2_bar(self,x,ses):
        c=self.cfg;s=self.s2;i=self.i;close=x['close'];o=x['open'];h=x['high'];l=x['low'];atr=x['atrV'];adx=x['adx'];rsi=x['rsi14']
        is_intra=self._s2_mode=='Intraday'
        is_swing=self._s2_mode in {'Swing','BTST/STBT'}
        mac=x['s2_macd'];ms=x['s2_macd_sig'];mh=x['s2_macdhist']
        prev_m=self.lag('s2_macd',1);prev_ms=self.lag('s2_macd_sig',1);prev_h=self.lag('s2_macdhist',1);prev_h2=self.lag('s2_macdhist',2)
        ratio=abs(mh)/max(x['s2_macdhist_avgabs14'],1e-12)
        bull_mac=mac>ms and mh>0 and ratio>self.macd_ratio_threshold
        bear_mac=mac<ms and mh<0 and ratio>self.macd_ratio_threshold
        bull_relax=mac>ms or (mh>prev_h and mh>prev_h2)
        bear_relax=mac<ms or (mh<prev_h and mh<prev_h2)
        cross_up=valid(prev_m) and prev_m<=prev_ms and mac>ms
        cross_dn=valid(prev_m) and prev_m>=prev_ms and mac<ms
        mac_mom_up=mh>0 and mh>prev_h
        mac_mom_dn=mh<0 and mh<prev_h
        turn_up=mh>prev_h>prev_h2 and prev_h<0
        turn_dn=mh<prev_h<prev_h2 and prev_h>0
        ris_rsi=rsi>self.lag('rsi14',1)>self.lag('rsi14',2)
        fall_rsi=rsi<self.lag('rsi14',1)<self.lag('rsi14',2)
        adx_thresh= (18 if self.minutes<=3 else 20 if self.minutes<=15 else 22) if c.s2_settings_mode=='Auto' else 20
        vol_mult=(1.2 if self.minutes<=5 else 1.3 if self.minutes<=15 else 1.4 if self.minutes<=60 else 1.6) if c.s2_settings_mode=='Auto' else 1.3
        gap_skip_pct=(.3 if self.minutes<=5 else .5 if self.minutes<=15 else .7 if self.minutes<=60 else 1.) if c.s2_settings_mode=='Auto' else .5
        vol_bull=(x['volume']>x['volsma20']*vol_mult if x['usableVolume'] else x['priceProxy']) and close>=o
        vol_bear=(x['volume']>x['volsma20']*vol_mult if x['usableVolume'] else x['priceProxy']) and close<=o
        vol_burst=(x['volume']>x['volsma20']*c.s2_burst_volume_mult) if x['usableVolume'] else x['priceProxy']
        index_type=self.s2_is_index
        commodity=self.s2_is_commodity
        thr=c.s2_burst_index_pct if index_type else c.s2_burst_commodity_pct if commodity else c.s2_burst_stock_pct
        burst_bars=c.s2_burst_bars or (5 if self.minutes<=1 else 4 if self.minutes<=3 else 3 if self.minutes<=5 else 2 if self.minutes<=30 else 1)
        price_chgn=(close-self.lag('close',burst_bars))/self.lag('close',burst_bars)*100 if i>=burst_bars and self.lag('close',burst_bars)>0 else 0.
        body_chg=(close-o)/o*100 if o>0 else 0.
        range_chg=(h-l)/l*100 if l>0 else 0.
        chg2=(close-self.lag('close',2))/self.lag('close',2)*100 if i>=2 and self.lag('close',2)>0 else 0.
        bull_burst=c.s2_burst and vol_burst and (price_chgn>=thr or (body_chg>=thr*.8 and range_chg>=thr) or chg2>=thr*1.2)
        bear_burst=c.s2_burst and vol_burst and (price_chgn<=-thr or (body_chg<=-thr*.8 and range_chg>=thr) or chg2<=-thr*1.2)
        recent_hi=max(self._arr['high'][max(0,i-4):i+1])
        recent_lo=min(self._arr['low'][max(0,i-4):i+1])
        drop=(recent_hi-close)/recent_hi*100 if recent_hi>0 else 0
        rise=(close-recent_lo)/recent_lo*100 if recent_lo>0 else 0
        rapid_bull=rise>=thr and close>self.lag('close',1) and self.lag('close',1)<=self.lag('close',2) and vol_burst
        rapid_bear=drop>=thr and close<self.lag('close',1) and self.lag('close',1)>=self.lag('close',2) and vol_burst
        bull_momentum=bull_burst or rapid_bull
        bear_momentum=bear_burst or rapid_bear
        reg5,reg15,reg60,regD=(x[k] for k in ('mtf5','mtf15','mtf60','mtfD'))
        if is_intra:
            mtfscore=reg15*2. if self.minutes<=5 else reg5*1.5+reg60*1.5 if self.minutes<=15 else reg15*1.5+reg60*1.5
        elif is_swing:mtfscore=reg15+reg60*2.5+regD*2.
        else:mtfscore=reg60+regD*4.
        mtfbull=mtfscore>=(1.5 if is_intra else 3.)
        mtfbear=mtfscore<=(-1.5 if is_intra else -3.)
        dailybull=not c.s2_daily_trend_lock or regD>=0
        dailybear=not c.s2_daily_trend_lock or regD<=0
        mtfbullok=bull_momentum or (mtfscore>=0 if is_intra else mtfbull)
        mtfbearok=bear_momentum or (mtfscore<=0 if is_intra else mtfbear)
        sessionok=(not is_intra or ses['market'])
        first_session=is_intra and sessionok and ses['first']
        if first_session:
            self.orb_high=h;self.orb_low=l;self.orb_formed=False;self.orb_start=i
        orb_bars=max(1,math.floor(c.s2_orb_minutes/self.minutes+0.5))
        if is_intra and sessionok and not self.orb_formed and valid(self.orb_high):
            if i-self.orb_start<orb_bars:
                self.orb_high=max(self.orb_high,h);self.orb_low=min(self.orb_low,l)
            else:self.orb_formed=True
        orb_bull=c.s2_orb and sessionok and self.orb_formed and is_intra and close>self.orb_high and vol_bull
        orb_bear=c.s2_orb and sessionok and self.orb_formed and is_intra and close<self.orb_low and vol_bear
        orb_active=c.s2_orb and sessionok and not self.orb_formed and valid(self.orb_high) and is_intra
        # Divergences are scored only after 2 RIGHT bars confirm a pivot.
        bull_div=bear_div=False
        if i>=4:
            j=i-2
            lows=self._arr['low'];highs=self._arr['high']
            pivotlow=lows[j]<lows[j-1] and lows[j]<=lows[j-2] and lows[j]<lows[j+1] and lows[j]<=lows[j+2]
            pivothigh=highs[j]>highs[j-1] and highs[j]>=highs[j-2] and highs[j]>highs[j+1] and highs[j]>=highs[j+2]
            if pivotlow:
                prev=self.div_piv_low[-1] if self.div_piv_low else None
                rv=self.lag('rsi14',2)
                if prev is not None and valid(rv):
                    bull_div=(c.s2_divergence and i-prev[0]<=c.s2_div_lookback and lows[j]<prev[1] and rv>prev[2]+2 and rv<50)
                self.div_piv_low.append((i,lows[j],rv))
            if pivothigh:
                prev=self.div_piv_high[-1] if self.div_piv_high else None
                rv=self.lag('rsi14',2)
                if prev is not None and valid(rv):
                    bear_div=(c.s2_divergence and i-prev[0]<=c.s2_div_lookback and highs[j]>prev[1] and rv<prev[2]-2 and rv>50)
                self.div_piv_high.append((i,highs[j],rv))
        pb,pr=s2_pattern(x,self.lag,c) if valid(atr) and i>=14 else (0.,0.)
        gap_pct=(o-self.lag('close',1))/self.lag('close',1)*100 if i and self.lag('close',1)>0 else 0
        gap_up=gap_pct>gap_skip_pct;gap_down=gap_pct<-gap_skip_pct
        near_gap=close>0 and abs(close-self.lag('close',1))/close*100<.3 if i else False
        isgapbar=c.s2_gap_skip and first_session and abs(gap_pct)>=gap_skip_pct
        # S2 confluence score: literal source weights, non-probabilistic points.
        cb=22 if close>x['ema9']>x['ema21']>x['ema50'] else 10 if close>x['ema21'] else 5 if close>x['ema9'] else 0
        cs=22 if close<x['ema9']<x['ema21']<x['ema50'] else 10 if close<x['ema21'] else 5 if close<x['ema9'] else 0
        cb+=8 if regD>0 else 0;cs+=8 if regD<0 else 0
        if adx>self.lag('adx',1) and adx>adx_thresh:cb+=5;cs+=5
        cb+=12 if bull_mac else 6 if bull_relax else 0
        cs+=12 if bear_mac else 6 if bear_relax else 0
        cb+=6 if mac_mom_up else 0;cs+=6 if mac_mom_dn else 0
        cb+=5 if cross_up else 0;cs+=5 if cross_dn else 0
        cb+=4 if turn_up else 0;cs+=4 if turn_dn else 0
        buy_rsi=(55<rsi<75) or (45<=rsi<=55 and ris_rsi)
        sell_rsi=(25<rsi<45) or (45<=rsi<=55 and fall_rsi)
        cb+=6 if 55<rsi<75 else 3 if 45<=rsi<=55 and ris_rsi else 0
        cs+=6 if 25<rsi<45 else 3 if 45<=rsi<=55 and fall_rsi else 0
        cb+=8 if vol_bull else 0;cs+=8 if vol_bear else 0
        cb+=8 if mtfbull else 4 if mtfscore>0 else 0
        cs+=8 if mtfbear else 4 if mtfscore<0 else 0
        cb+=c.s2_burst_score_boost if bull_momentum else 0
        cs+=c.s2_burst_score_boost if bear_momentum else 0
        cb+=c.s2_div_score_boost if bull_div else 0
        cs+=c.s2_div_score_boost if bear_div else 0
        cb+=10 if orb_bull else 0;cs+=10 if orb_bear else 0
        cb+=pb;cs+=pr
        cb+=4 if gap_up and not near_gap else 0
        cs+=4 if gap_down and not near_gap else 0
        cb+=3 if valid(x['vwap']) and valid(x['hlc3std20']) and close>x['vwap']+x['hlc3std20'] else 0
        cs+=3 if valid(x['vwap']) and valid(x['hlc3std20']) and close<x['vwap']-x['hlc3std20'] else 0
        cb=min(cb,100.);cs=min(cs,100.)
        x['s2_bull'],x['s2_bear']=float(cb),float(cs)
        trend_b=close>x['sma20'] and x['ema9']>x['ema21'];trend_s=close<x['sma20'] and x['ema9']<x['ema21']
        strongb=trend_b and bull_mac and buy_rsi and adx>adx_thresh and x['di_plus']>x['di_minus'] and mtfbullok and dailybull
        strongs=trend_s and bear_mac and sell_rsi and adx>adx_thresh and x['di_minus']>x['di_plus'] and mtfbearok and dailybear
        medb=trend_b and bull_mac and buy_rsi and mtfscore>=0 and dailybull
        meds=trend_s and bear_mac and sell_rsi and mtfscore<=0 and dailybear
        patb=pb>=15 and cb>=c.s2_confidence_floor and dailybull and (bull_mac or bull_relax or cross_up)
        pats=pr>=15 and cs>=c.s2_confidence_floor and dailybear and (bear_mac or bear_relax or cross_dn)
        orbb=orb_bull and dailybull and cb>=c.s2_confidence_floor-5
        orbs=orb_bear and dailybear and cs>=c.s2_confidence_floor-5
        burstb=bull_momentum and cb>=c.s2_confidence_floor and bull_relax
        bursts=bear_momentum and cs>=c.s2_confidence_floor and bear_relax
        revb=rapid_bull and cb>=c.s2_confidence_floor and (turn_up or cross_up or bull_relax)
        revs=rapid_bear and cs>=c.s2_confidence_floor and (turn_dn or cross_dn or bear_relax)
        filterb=not c.s2_macd_entry_filter or (bull_relax if bull_momentum else bull_mac)
        filters=not c.s2_macd_entry_filter or (bear_relax if bear_momentum else bear_mac)
        rawb=(strongb or medb or patb or orbb or burstb or revb or (cross_up and cb>=c.s2_confidence_floor and dailybull)) and cb>=c.s2_confidence_floor and filterb and not isgapbar and not orb_active
        raws=(strongs or meds or pats or orbs or bursts or revs or (cross_dn and cs>=c.s2_confidence_floor and dailybear)) and cs>=c.s2_confidence_floor and filters and not isgapbar and not orb_active
        if c.s2_settings_mode=='Auto' and is_intra:
            effgap=max(c.s2_signal_gap,3 if self.minutes<=1 else 2 if self.minutes<=5 else 1)
        else:effgap=c.s2_signal_gap
        # Source time gates: NSE 15.40 decimal means 15:24 not 15:40.
        use_native_last=(ses['region']=='Native' or (ses['region']=='India' and ses['is_commodity']))
        last_force=(c.s2_force_exit_ist_decimal*60 if ses['region']=='India' and not ses['is_commodity']
                    else 930 if ses['region']=='US' else ses['session_end'])
        # Pine 1411/1413 test the bar's real time_close, not open+TF: a session-truncated
        # last bar (30m 15:15 closes 15:30) must still force-exit and block entries.
        close_min=self._s2_close_minute() if is_intra and ses['market'] and not use_native_last else ses['end']
        force_exit=is_intra and ses['market'] and (ses['last'] if use_native_last else
                  (close_min>=last_force or close_min+self.minutes>last_force) and close_min<=ses['session_end'])
        btst_window=c.s2_enable_btst and (is_intra or is_swing) and ses['market'] and (
            ses['last'] if use_native_last else last_force-c.s2_btst_window_min<=ses['minute']<last_force)
        risk_valid=(c.s2_max_stop_pct<=0 or (c.s2_min_stop_pct<=c.s2_max_stop_pct and close*c.s2_max_stop_pct/100>=self.tick)) and close>0 and (not c.hard_stop_enabled or close*mode_cap(c,self._s2_mode)/100>=self.tick)
        ready=risk_valid and i>=65 and valid(atr) and valid(x['atrma50']) and valid(adx) and valid(x['sma20']) and valid(mac) and atr>0 and x['atrma50']>0
        may_signal=(not c.s2_circuit or i>s.pause_until) and ready and c.enable_s2 and not force_exit and sessionok
        normal=may_signal and i-s.last_signal_bar>=effgap
        burst=may_signal and i-s.last_signal_bar>=max(1,effgap-1)
        conflict=(not c.block_conflicting_entries or (self.s1.active is None and self.s1.pending is None and self.s1.last_signal_bar!=i))
        cbuy=(not c.s2_require_15m_alignment or (x['ema9_15']>x['ema21_15'] and x['ema9_15_slope']>0)) and rawb and conflict and s.active is None and cb>=c.s2_confidence_floor and (normal or (bull_momentum and burst))
        csell=(not c.s2_require_15m_alignment or (x['ema9_15']<x['ema21_15'] and x['ema9_15_slope']<0)) and raws and conflict and s.active is None and cs>=c.s2_confidence_floor and (normal or (bear_momentum and burst))
        buy=cbuy and (not csell or cb>cs)
        sell=csell and (not cbuy or cs>cb)
        if buy or sell:
            tr_start_r=(1. if self.minutes<=15 else 1.3 if self.minutes<=60 else 1.8) if c.s2_settings_mode=='Auto' else c.s2_trail_start_manual
            tr_offset=(1.8 if self.minutes<=15 else 2.2 if self.minutes<=60 else 2.8) if c.s2_settings_mode=='Auto' else c.s2_trail_atr_manual
            trmode=(1.,1.8,3.) if is_intra else (1.2,2.5,4.5) if is_swing else (1.5,3.,6.)
            r1,r2,r3=(c.t1_r,c.t2_r,c.t3_r) if c.share_target_ladder else trmode if c.s2_settings_mode=='Auto' else (c.s2_t1_manual,c.s2_t2_manual,c.s2_t3_manual)
            ratios=(r1,max(r1+.25,r2),max(max(r1+.25,r2)+.25,r3))
            slip=.15 if atr>x['atrma50']*1.3 else .08 if atr>x['atrma50'] else .04
            self._s2_new_trade(x,bool(buy),btst_window,ratios,slip,cb if buy else cs,mtfscore,bull_momentum if buy else bear_momentum)
        tr=s.active
        # S2 checks an existing position only from NEXT candle (Pine source).
        if tr is None or i<=tr.entry_bar:return
        long=tr.long;old=tr.trailing
        stopgap=o<=old if long else o>=old
        stoptouch=l<=old if long else h>=old
        orphan=x['new_day'] and is_intra and tr.subtype=='NONE'
        reason=None;px=None
        if stopgap:reason='STOP GAP';px=o
        elif orphan:reason='NEW DAY GAP CLOSE';px=o
        elif stoptouch:reason='EARLY LOSS STOP' if tr.early_loss_armed else 'TRAIL/LOCK STOP' if old!=tr.stop else 'INITIAL STOP';px=old
        else:
            self._milestones('S2',tr,h,l)
            risk=max(abs(tr.entry-tr.stop),.0001)
            if long:
                tr.peak=max(tr.peak,close if c.s2_tsl_use_close else h)
                best=tr.peak
            else:
                tr.trough=min(tr.trough,close if c.s2_tsl_use_close else l)
                best=tr.trough
            weak_count=(sum((close<x['ema21'],close<self.lag('close',1),mh<prev_h,mac<ms,rsi<self.lag('rsi14',1)))
                        if long else sum((close>x['ema21'],close>self.lag('close',1),mh>prev_h,mac>ms,rsi>self.lag('rsi14',1))))
            early_stop,early_exit=early_loss_action(tr.entry,long,close,risk,best,tr.trailing,weak_count,tr.t1_hit,self.tick)
            if early_exit:
                reason='EARLY LOSS CUT';px=close
            elif early_stop!=tr.trailing:
                tr.trailing=early_stop;tr.early_loss_armed=True
            if c.s2_trailing and tr.t1_hit and reason is None:
                tr.trailing=runner_stop_price(tr.entry,long,best,risk,tr.trailing,close,self.tick,c.s2_profit_lock_r)
                tr.locked=True
                tr.early_loss_armed=False
            if reason is None and force_exit and is_intra and tr.subtype=='NONE':
                reason='INTRADAY EOD';px=close
            elif reason is None:
                exitlm=bear_mac and mh<prev_h and mh<prev_h2
                exitlt=close<x['ema21'] and close<self.lag('close',1)
                exitlc=cross_dn and mh<0
                exitlr=rsi>80 and rsi<self.lag('rsi14',1)
                exitsm=bull_mac and mh>prev_h and mh>prev_h2
                exitst=close>x['ema21'] and close>self.lag('close',1)
                exitsc=cross_up and mh>0
                exitsr=rsi<20 and rsi>self.lag('rsi14',1)
                stillbull=close>x['ema50'] and adx>25 and x['di_plus']>x['di_minus'] and bull_mac
                stillbear=close<x['ema50'] and adx>25 and x['di_minus']>x['di_plus'] and bear_mac
                if long:tr.exit_long_count=tr.exit_long_count+1 if sum((exitlm,exitlt,exitlc,exitlr))>=2 else 0
                else:tr.exit_short_count=tr.exit_short_count+1 if sum((exitsm,exitst,exitsc,exitsr))>=2 else 0
                exitconf=(5 if c.s2_exit_mode=='Conservative' else 4 if c.s2_exit_mode=='Balanced' else 3)
                if c.s2_settings_mode=='Manual' and c.s2_exit_confirm_input>0:exitconf=c.s2_exit_confirm_input
                smart=c.s2_smart_exit and i-tr.entry_bar>5 and ((exitlm and exitlt and exitlc) if long else (exitsm and exitst and exitsc))
                quick=c.s2_exit_weakness and ((exitlm and exitlt and close<x['ema9']) if long else (exitsm and exitst and close>x['ema9']))
                trend_still=stillbull if long else stillbear
                momentum=(not trend_still and (tr.exit_long_count if long else tr.exit_short_count)>=exitconf) or smart or quick
                winner_runner=tr.t1_hit and (close>tr.entry if long else close<tr.entry)
                if i-tr.entry_bar>=c.s2_max_bars_in_trade or (momentum and not winner_runner):
                    reason='MAX BARS FAILSAFE' if i-tr.entry_bar>=c.s2_max_bars_in_trade else 'SMART EXIT' if smart else 'WEAKNESS EXIT' if quick else 'MOMENTUM EXIT'
                    px=close
                elif c.s2_trailing:
                    trailstart=(1. if self.minutes<=15 else 1.3 if self.minutes<=60 else 1.8) if c.s2_settings_mode=='Auto' else c.s2_trail_start_manual
                    trailoffset=(1.8 if self.minutes<=15 else 2.2 if self.minutes<=60 else 2.8) if c.s2_settings_mode=='Auto' else c.s2_trail_atr_manual
                    rr=((close-tr.entry) if long else (tr.entry-close))/risk
                    if c.s2_early_be and not tr.early_be and rr>=c.s2_early_be_r and (close>tr.entry if long else close<tr.entry):
                        tr.trailing=max(tr.trailing,tr.entry) if long else min(tr.trailing,tr.entry)
                        tr.early_be=True
                    if rr>=trailstart:
                        mult=trailoffset*(.7 if c.s2_aggressive_trail else 1.)*(1.3 if tr.t2_hit else 1.)
                        proposal=(tr.peak-atr*mult) if long else (tr.trough+atr*mult)
                        tr.trailing=max(tr.trailing,min(close-self.tick,proposal)) if long else min(tr.trailing,max(close+self.tick,proposal))
        if reason is None and (tr.trailing>=tr.entry if long else tr.trailing<=tr.entry):
            tr.early_loss_armed=False
        if reason:
            self._record_exit(s,'S2',tr,px,reason)
        elif tr.trailing!=old and c.emit_stop_updates:
            self.event('S2','STOP_UPDATED',trade_id=tr.trade_id,stop=float(tr.trailing),effective='NEXT_BAR',simulated=True)
