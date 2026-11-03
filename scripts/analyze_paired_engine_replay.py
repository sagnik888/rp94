"""Reconcile paired offline reports without changing decisions or tuning policy."""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import re
import pandas as pd


def money(row):
    sign=1 if row['inst_type']=='OPT' or row['direction']=='LONG' else -1
    return (float(row['exit_price'])-float(row['entry_price']))*int(row['lots'])*int(row['lot_size'])*sign-float(row.get('charges') or 0)


def stats(rows):
    ordered=sorted(rows,key=lambda r:str(r.get('exit_timestamp') or ''))
    values=[money(r) for r in ordered]
    gains=sum(v for v in values if v>0); losses=-sum(v for v in values if v<0)
    equity=peak=dd=0.0
    for v in values:
        equity+=v;peak=max(peak,equity);dd=max(dd,peak-equity)
    return dict(trades=len(rows),net=round(sum(values),2),wins=sum(v>0 for v in values),
        losses=sum(v<0 for v in values),win_rate=round(100*sum(v>0 for v in values)/len(values),2) if values else 0,
        profit_factor=round(gains/losses,4) if losses else None,max_drawdown=round(dd,2),
        charges=round(sum(float(r.get('charges') or 0) for r in rows),2))


def group(rows,key):
    groups=defaultdict(list)
    for row in rows:groups[key(row)].append(row)
    return {k:stats(v) for k,v in sorted(groups.items())}


def input_meta(row):
    return next((t for t in row.get('replay_exit_trace',[]) if t.get('guard')=='replay_input'),{})


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def market_time(value):
    ts=pd.Timestamp(value)
    return ts.tz_convert('Asia/Kolkata').tz_localize(None) if ts.tzinfo else ts


def audit_prices(rows,inputs,require_expiry_evidence=False):
    issues=[]; errors=[]; verified=0; sources={}; no_index_history=[]
    bundle_path=inputs/'data_store/option_history_bundle.parquet'
    bundle_manifest=read(str(bundle_path)+'.manifest.json') if Path(str(bundle_path)+'.manifest.json').exists() else {}
    bundle=pd.read_parquet(bundle_path) if bundle_path.exists() else None
    bundle_matches=0; expiry_checks=0
    for row in rows:
        ident=row['id'];meta=input_meta(row);contract=meta.get('contract',{})
        for item in row.get('replay_exit_trace',[]):
            if item.get('state')=='ERROR_ABSTAIN':errors.append({'id':ident,**item})
        if meta.get('price_basis')!='OBSERVED_OPTION_OHLC':
            issues.append({'id':ident,'issue':'not_observed_option_price'});continue
        if not contract.get('contract_verified') or not meta.get('input_sha256'):
            issues.append({'id':ident,'issue':'missing_verified_contract_or_input_hash'})
        symbol=contract.get('contract_symbol') or row.get('trading_symbol')
        if symbol!=row.get('trading_symbol'):
            issues.append({'id':ident,'issue':'reported_contract_mismatch'})
        path=inputs/'data_store/candles'/f'{symbol}_1min.csv'
        if not path.exists():
            issues.append({'id':ident,'issue':'contract_csv_unavailable'});continue
        if symbol not in sources:
            frame=pd.read_csv(path,index_col=0,parse_dates=True).sort_index()
            if frame.index.tz:frame.index=frame.index.tz_convert('Asia/Kolkata').tz_localize(None)
            sources[symbol]=frame
        frame=sources[symbol]
        entry=market_time(meta['entry_time']);eligible=market_time(meta['eligible_at'])
        end=market_time(meta['last_closed_at']);exit_at=market_time(row['exit_timestamp'])
        if require_expiry_evidence:
            expiry=meta.get('expiry_context') or {}
            try:
                contract_expiry=market_time(contract['contract_expiry']).date()
                expected_dte=(contract_expiry-entry.date()).days
                assert expected_dte>=0
                assert expiry.get('available') is True
                assert market_time(expiry['contract_expiry']).date()==contract_expiry
                assert float(expiry['days_to_expiry'])==expected_dte
                delta=float(meta['natural_stop_delta'])
                assert 0<abs(delta)<=1
                assert (delta>0)==(contract['contract_option_type']=='CE')
                assert meta['natural_stop_delta_source']=='estimated_black_scholes_iv_15pct'
                assert abs(float(meta['natural_stop_iv'])-.15)<1e-9
                mapped=abs(float(meta['underlying_stop_distance'])*delta)
                assert abs(mapped-float(meta['natural_sl_distance']))<1e-8
                assert abs(float(meta['sizing_entry_price'])-float(row['entry_price']))<1e-8
                assert int(meta['sizing_lots'])==int(row['lots'])
                sized_risk=abs(float(meta['sizing_entry_price'])-float(meta['sizing_stop_price']))
                assert abs(sized_risk*int(row['lot_size'])-float(meta['sizing_risk_per_lot']))<1e-7
                expiry_checks+=1
            except (AssertionError,KeyError,TypeError,ValueError):
                issues.append({'id':ident,'issue':'contract_expiry_or_stop_mapping_mismatch'})
        if entry<eligible:issues.append({'id':ident,'issue':'entry_precedes_signal_availability'})
        available=frame.loc[(frame.index>=eligible)&(frame.index<end)]
        if len(available)!=meta.get('rows'):
            issues.append({'id':ident,'issue':'input_window_row_count_mismatch'})
        # Parser units are normalized by the simulator; use the same normalized
        # naive timestamp index before independently recomputing the input digest.
        hash_frame=available.copy()
        hash_frame.index=pd.DatetimeIndex([market_time(t) for t in hash_frame.index])
        digest=hashlib.sha256(pd.util.hash_pandas_object(hash_frame,index=True).values.tobytes()).hexdigest()
        if digest!=meta.get('input_sha256'):
            # The bundle has union columns: a plain OHLCV CSV can acquire all-NaN
            # telemetry columns without changing a price. Validate its identity,
            # compare original OHLCV, then check the actual full-frame digest.
            signature=list(bundle_manifest.get('signatures',{}).get(path.name,[]))
            stat=path.stat();matched=False
            if bundle is not None and signature==[stat.st_mtime_ns,stat.st_size]:
                try:
                    bundled=bundle.xs(path.name,level='contract').copy()
                    bundled.index=pd.DatetimeIndex([market_time(t) for t in bundled.index])
                    bundled=bundled.loc[(bundled.index>=eligible)&(bundled.index<end)]
                    cols=['open','high','low','close','volume']
                    same_prices=hash_frame[cols].equals(bundled[cols])
                    bundle_digest=hashlib.sha256(pd.util.hash_pandas_object(bundled,index=True).values.tobytes()).hexdigest()
                    matched=same_prices and bundle_digest==meta.get('input_sha256')
                except (KeyError,ValueError):pass
            if matched:bundle_matches+=1
            else:issues.append({'id':ident,'issue':'input_hash_mismatch'})
        if available.empty or entry!=available.index[0] or abs(float(available.iloc[0]['open'])-float(row['entry_price']))>1e-6:
            issues.append({'id':ident,'issue':'entry_not_first_eligible_open'})
        # Ordinary exits are observed at bar close; gap stops may fill at its open.
        candidates=frame.loc[frame.index.isin([exit_at,exit_at-pd.Timedelta(minutes=1)])]
        px=float(row['exit_price'])
        if candidates.empty or not any(float(r.low)-1e-6<=px<=float(r.high)+1e-6 for r in candidates.itertuples()):
            issues.append({'id':ident,'issue':'exit_outside_observed_ohlc','exit':px,'at':str(exit_at)})
        index=row['instrument'].split()[0]
        idx_path=inputs/'data_store/candles'/f'{index}_1min.csv'
        if index not in sources:
            index_frame=pd.read_csv(idx_path,index_col=0,parse_dates=True).sort_index()
            index_frame.index=pd.DatetimeIndex([market_time(t) for t in index_frame.index])
            sources[index]=index_frame
        index_frame=sources[index]
        window=index_frame.loc[(index_frame.index>=entry)&(index_frame.index<exit_at)]
        expected=max(0,int((exit_at-entry).total_seconds()//60))
        if len(window)<expected:no_index_history.append(ident)
        verified+=1
    return dict(observed_option_rows_checked=verified,bundle_union_column_digests_verified=bundle_matches,
                contract_expiry_stop_mappings_checked=expiry_checks,expiry_evidence_required=require_expiry_evidence,
                issues=issues,guard_errors=errors,
                rows_missing_complete_index_1m_management_window=no_index_history)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--baseline',default='reports/offline_baseline_paired_20260913.json')
    parser.add_argument('--corrected',default='reports/offline_corrected_paired_20260913.json')
    parser.add_argument('--inputs',default='reports/replay_frozen_inputs_v2_20260913')
    parser.add_argument('--output',default='reports/paired_replay_analysis_20260913.json')
    parser.add_argument('--require-expiry-evidence',action='store_true')
    args=parser.parse_args();before=read(args.baseline);after=read(args.corrected);inputs=Path(args.inputs)
    assert before['inputs_manifest_sha256']==after['inputs_manifest_sha256'], 'Input manifests differ'
    assert before['settings_sha256']==after['settings_sha256'], 'Settings differ'
    assert not before['network_attempts'] and not after['network_attempts'], 'Network attempted'
    b=before['trades'];a=after['trades']
    rounding=[r['id'] for r in b+a if abs(money(r)-float(r['pnl']))>.51]
    assert not rounding, f'Unreconciled accounting: {rounding}'
    by_before={r['id']:r for r in b};by_after={r['id']:r for r in a}
    common=sorted(by_before.keys()&by_after.keys())
    unchanged=[key for key in common if by_before[key].get('trading_symbol')==by_after[key].get('trading_symbol')]
    all_dates=sorted(set(pd.Timestamp(r['exit_timestamp']).date().isoformat() for r in b+a))
    split=all_dates[len(all_dates)//2] if all_dates else ''
    report={'input_manifest_sha256':after['inputs_manifest_sha256'],'settings_sha256':after['settings_sha256'],
        'baseline':stats(b),'corrected':stats(a),'net_change':round(stats(a)['net']-stats(b)['net'],2),
        'chronological_split':split,'comparison_scope':'saved_source_before_repairs_vs_corrected_source; original96tradeprocessnotreconstructable',
        'source_hashes':{'baseline':before['source_hashes'],'corrected':after['source_hashes']},
        'matched_ids':{'count':len(common),'same_contract':len(unchanged),'changed_contract':len(common)-len(unchanged),
            'baseline_same_contract':stats([by_before[k] for k in unchanged]),'corrected_same_contract':stats([by_after[k] for k in unchanged]),
            'removed':sorted(by_before.keys()-by_after.keys()),'added':sorted(by_after.keys()-by_before.keys())},
        'corrected_price_audit':audit_prices(a,inputs,args.require_expiry_evidence),
        'historical_intelligence_coverage':after.get('historical_intelligence_coverage',{}),
        'limitations':['OHLC cannot establish intrabar event order; stop-first convention remains conservative.',
            'Historical derivative observations and option-chain snapshots are unavailable for these sessions.',
            'No out-of-sample profitability claim: chronological slices are descriptive and no policy was fitted.',
            'Missing archive coverage changes the executed sample; matched contract comparison is included.',
            'Original dashboard96trade loss is retained separately; the earlier process source/input trace was not archived.']}
    for label,rows,data in [('baseline',b,before),('corrected',a,after)]:
        report[label+'_groups']={
            'index':group(rows,lambda r:r['instrument'].split()[0]),'timeframe':group(rows,lambda r:r['timeframe']),
            'grade':group(rows,lambda r:r['grade']),'exit':group(rows,lambda r:r['exit_reason']),
            'price_basis':group(rows,lambda r:input_meta(r).get('price_basis','LEGACY_UNSPECIFIED')),
            'chronological':group(rows,lambda r:'earlier' if r['exit_timestamp'][:10]<split else 'later'),
            'last_five_sessions':stats([r for r in rows if r['exit_timestamp'][:10] in all_dates[-5:]])}
        report[label+'_diagnostics']=data['diagnostics']
    output=Path(args.output);output.write_text(json.dumps(report,indent=2,default=str),encoding='utf-8')
    print(json.dumps({k:report[k] for k in ['baseline','corrected','net_change']},indent=2))
    print('price issues:',len(report['corrected_price_audit']['issues']),'guard errors:',len(report['corrected_price_audit']['guard_errors']))


if __name__=='__main__':main()
