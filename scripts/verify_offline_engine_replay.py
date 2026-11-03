"""Run the production historical candidate/exit path on frozen local inputs.

All persistence goes to a fresh workspace beneath reports. Broker networking is
disabled before application imports. No production settings or app state change.
"""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import sys
from types import SimpleNamespace
from unittest.mock import patch


def metrics(rows):
    values = [float(r.get('pnl') or 0) for r in rows]
    win = sum(v for v in values if v > 0)
    loss = -sum(v for v in values if v < 0)
    equity = peak = dd = 0.0
    for row in sorted(rows, key=lambda r:str(r.get('exit_time') or '')):
        equity += float(row.get('pnl') or 0)
        peak = max(peak,equity); dd=max(dd,peak-equity)
    return dict(trades=len(values),wins=sum(v>0 for v in values),losses=sum(v<0 for v in values),
                net=round(sum(values),2),profit_factor=round(win/loss,4) if loss else None,
                max_drawdown=round(dd,2),expectancy=round(sum(values)/len(values),2) if values else 0)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--source',required=True)
    parser.add_argument('--inputs',required=True)
    parser.add_argument('--output',required=True)
    parser.add_argument('--indices',default='NIFTY,BANKNIFTY,SENSEX,MIDCPNIFTY')
    args=parser.parse_args()
    repo=Path(__file__).resolve().parents[1]
    source=Path(args.source).resolve(); inputs=Path(args.inputs).resolve();output=Path(args.output).resolve()
    source_hashes={str(p.relative_to(source)):hashlib.sha256(p.read_bytes()).hexdigest()
        for p in source.rglob('*.py') if '__pycache__' not in p.parts and 'reports' not in p.relative_to(source).parts}
    input_manifest=json.loads((inputs/'manifest.json').read_text())
    for relative,expected in input_manifest.items():
        if hashlib.sha256((inputs/relative).read_bytes()).hexdigest()!=expected:
            raise RuntimeError(f'Frozen input checksum mismatch: {relative}')
    workspace=output.with_suffix('.workspace')
    if workspace.exists(): raise RuntimeError('Use a new output path; replay workspace already exists')
    workspace.mkdir(parents=True)
    shutil.copytree(inputs/'data_store',workspace/'data_store')
    os.chdir(workspace)
    os.environ['UTBOT_DB_PATH']=str(workspace/'data_store/replay.db')
    sys.path.insert(0,str(repo));sys.path.insert(0,str(source))
    network_attempts=[]
    def block_network(*a,**kw):
        network_attempts.append('network_request_blocked')
        raise RuntimeError('Offline replay has no network authority')
    socket.socket.connect=block_network
    socket.create_connection=block_network
    import pandas as pd
    from loguru import logger
    logger.remove(); logger.add(workspace/'replay.log',level='WARNING')
    import config.settings as settings_module
    settings_data=json.loads((inputs/'settings.json').read_text(encoding='utf-8'))
    settings_module._settings=settings_module.Settings(_env_file=None,**settings_data)
    settings=settings_module.get_settings()
    settings.trading_mode='HISTORICAL'
    settings.auto_mode=False
    # Only disable outbound notifications/collection, not decision settings.
    if hasattr(settings,'telegram_enabled'):settings.telegram_enabled=False
    from scanner import Scanner
    from data.candle_builder import CandleBuilder
    from engine.multi_timeframe import MultiTimeframeEngine
    from engine.signal_manager import SignalManager
    from intelligence.intelligence_aggregator import IntelligenceAggregator
    from trading.trade_manager import TradeManager
    from engine.expiry_manager import expiry_manager
    from engine.intelligence_score import aggregate_intelligence_score

    # Use the same saved public instrument master for both builds. Do not fetch
    # today's broker master or infer the expiry day from a monthly symbol.
    for master_name in ('kite_instruments.json','instruments.json'):
        master_path=workspace/'data_store'/master_name
        if master_path.exists():
            expiry_manager.df=pd.DataFrame(json.loads(master_path.read_text()))
            expiry_manager._ensure_expiry_dates()
            expiry_manager.analyze_expiries()
            break

    class LocalData:
        def get_option_token(self,name,strike,option_type,trade_date=None,now_dt=None):
            expiry_type='active_monthly' if name in {'BANKNIFTY','MIDCPNIFTY'} else 'active_weekly'
            if trade_date is None: return None
            return expiry_manager.get_token_for_date(name,strike,option_type,trade_date,expiry_type,now_dt=now_dt)
        def get_option_chain(self,*a,**kw): return None
        def get_derivative_participation(self,*a,**kw): return {}
        def get_recorded_option_chain(self,*a,**kw): return pd.DataFrame(), {'available':False,'reason':'no_recorded_historical_chain'}
        def get_latest_price(self,*a,**kw): return 0.0
        def get_ltp(self,*a,**kw): return 0.0
        def is_market_open(self,*a,**kw): return False
        def get_historical_candles(self,*a,**kw): return pd.DataFrame()

    indices=args.indices.split(',')
    config=json.loads((inputs/'config/instruments.json').read_text())
    frames={}
    candles=CandleBuilder(max_candles=100000)
    mtf=MultiTimeframeEngine(settings.get_ut_engine_params())
    for name in indices:
        for tf in ('1min','5min','15min'):
            frame=pd.read_csv(workspace/f'data_store/candles/{name}_{tf}.csv',index_col=0,parse_dates=True)
            frame=frame.sort_index()
            if frame.index.tz:frame.index=frame.index.tz_convert('Asia/Kolkata').tz_localize(None)
            frames[(name,tf)]=frame
            # Keep exact cached OHLCV and missingness; no resampling or fabricated history.
            getattr(candles,'_candles_'+tf)[name]=frame
    with patch.object(TradeManager,'load_state',lambda self:None),patch('threading.Thread.start',lambda self:None):
        trades=TradeManager(broker=None)
    trades._sync_active=False
    with patch.object(expiry_manager,'pre_market_check',return_value={}):
        scanner=Scanner(LocalData(),candles,mtf,SignalManager(),IntelligenceAggregator(),trades,
                        SimpleNamespace(),config,trading_mode='HISTORICAL')
    scanner.backtest_days=30
    scanner.active_indices=indices
    scanner._diagnostics.setdefault('simulation',{})['backtest_days']=30
    runtime_path=inputs/'runtime_config.json'
    if runtime_path.exists():
        runtime=json.loads(runtime_path.read_text())
        scanner.user_lots.update(runtime.get('lots',{}))
        scanner.user_lots_fut.update(runtime.get('lots_fut',{}))
        scanner.futures_sl_pct=float(runtime.get('fut_sl',scanner.futures_sl_pct))
        scanner.options_sl_pct=float(runtime.get('opt_sl',scanner.options_sl_pct))
    scanner.log_event=lambda *a,**kw:None
    scanner._restore_warm_memory=lambda:None
    provenance={}
    for name in indices:
        cfg=config['indices'][name]
        lot_size=int(cfg['lot_size']);lots=int(scanner.user_lots.get(name,1))
        for tf in ('1min','5min','15min'):
            mtf.update_data(name,tf,scanner._prepare_live_signal_data(name,tf,frames[name,tf]))
        result=mtf.process_instrument(name,lot_size=lot_size,lots=lots,
                                      capital=scanner.capital_fut+scanner.capital_opt,risk_pct=scanner.risk_fut_pct)
        for tf in ('1min','5min','15min'):
            tf_result=getattr(result,'results_'+tf,None)
            if tf_result:
                scanner._cached_results[f'{name}_{tf}']=tf_result
        df=frames[(name,'5min')]
        spot=float(df['close'].iloc[-1]);step=int(cfg['strike_interval'])
        # Historical branch computes each decision's own intel. The last-bar bundle
        # is only the normal outer call contract, retained equally for both builds.
        intel=scanner.intel.analyze(name,'5min',df,options_chain=None,spot_price=spot)
        score=aggregate_intelligence_score(intel.get('aggregate',{}))
        regime=intel.get('regime',{}).get('regime','UNKNOWN')
        scanner.intel.regime.reset_history(name)
        scanner.signal_processor.process_best_signal(name,result,intel,score,regime,lots,lot_size,cfg,spot,round(spot/step)*step)
        provenance[name]={tf:dict(rows=len(frames[name,tf]),first=str(frames[name,tf].index[0]),last=str(frames[name,tf].index[-1])) for tf in ('1min','5min','15min')}
        print(json.dumps(dict(index=name,completed_trades=len(trades.closed_trades))),flush=True)
        output.with_suffix('.progress.json').write_text(json.dumps(dict(
            index=name,summary=metrics([t.to_dict() for t in trades.closed_trades])),default=str))
    rows=[t.to_dict() for t in trades.closed_trades]
    groups=defaultdict(list)
    for row in rows:groups[str(row['timeframe'])].append(row)
    price_groups=defaultdict(list)
    for row in rows:
        trace=row.get('replay_exit_trace',[])
        replay_input=next((x for x in trace if x.get('guard')=='replay_input'),{})
        price_groups[replay_input.get('price_basis','UNSPECIFIED_LEGACY_BACKTEST')].append(row)
    report=dict(source=str(source),source_hashes=source_hashes,
        inputs_manifest_sha256=hashlib.sha256((inputs/'manifest.json').read_bytes()).hexdigest(),
        settings_sha256=hashlib.sha256((inputs/'settings.json').read_bytes()).hexdigest(),
        summary=metrics(rows),timeframes={key:metrics(group) for key,group in groups.items()},
        price_basis={key:metrics(group) for key,group in price_groups.items()},
        historical_intelligence_coverage=getattr(scanner,'_historical_intel_coverage',{}),
        data_coverage=provenance,diagnostics=scanner._diagnostics,trades=rows,network_attempts=network_attempts,
        limitations=['Archived contract prices only; unavailable historical derivative/OI inputs abstain.',
                      'Current source baseline may differ from the earlier app process; captured96trade result retained separately.',
                      'No broker liquidity/queue/slippage validation; no profitability certification.'])
    output.write_text(json.dumps(report,indent=2,default=str),encoding='utf-8')
    print(json.dumps(report['summary']),flush=True)
    for key in ('_scanner_live_executor','_engine_lab_executor'):
        getattr(scanner,key).shutdown(wait=False)


if __name__=='__main__':main()
