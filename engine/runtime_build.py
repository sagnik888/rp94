"""Identify the source snapshot present when a scanner process imports its code."""
import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path

FILES = ('scanner.py','engine/signal_processor.py','engine/signal_manager.py',
         'engine/scanner_recalculation.py','engine/recalculation_worker.py','data/candle_builder.py',
         'data/market_data.py','data/kite_market_data.py','data/derivative_participation.py',
         'intelligence/intelligence_aggregator.py','intelligence/derivative_context.py',
         'intelligence/regime_detector.py','intelligence/session_context.py',
         'intelligence/oi_tracker.py','engine/multi_timeframe.py',
         'intelligence/confidence_calibration.py','engine/expiry_manager.py',
         'trading/trade_manager.py','engine/entry_policy.py','config/settings.py',
         'engine/pre_candle_predictor.py','engine/entry_data_quality.py',
         'engine/runtime_build.py','engine/scanner_telemetry.py',
         'trading/trade_dashboard.py','dashboard/server.py',
         'engine/uts_lane.py','engine/uts_repaint.py','engine/execution_service.py',
         'engine/exit_cycle.py','engine/trade_row_adapter.py','engine/ut_smart_engine.py',
         'engine/s1_lane.py','engine/candidate_selection.py','engine/alt_scanners.py',
         'engine/risk_manager.py','engine/market_regime.py','engine/scanner_chart_mixin.py',
         'engine/scanner_dashboard.py','engine/engine_lab.py','data/sqlite_db.py','main.py')


def capture_build(root):
    root=Path(root)
    files={}
    for name in FILES:
        p=root/name
        if p.exists():
            stat=p.stat()
            files[name]={'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),
                         'mtime_ns':stat.st_mtime_ns,'size':stat.st_size}
    fingerprint=hashlib.sha256(''.join(name+row['sha256'] for name,row in sorted(files.items())).encode()).hexdigest()
    return {'fingerprint':fingerprint,'pid':os.getpid(),'captured_at':datetime.now(timezone.utc).isoformat(),
            'basis':'source_snapshot_at_scanner_import','files':files}


def build_status(build,root):
    changed=[]
    for name,row in build.get('files',{}).items():
        p=Path(root)/name
        if not p.exists():changed.append(name);continue
        stat=p.stat()
        if (stat.st_mtime_ns,stat.st_size)!=(row['mtime_ns'],row['size']):
            if hashlib.sha256(p.read_bytes()).hexdigest()!=row['sha256']:changed.append(name)
    return {**build,'source_files_changed_since_import':changed,'reload_required':bool(changed)}
