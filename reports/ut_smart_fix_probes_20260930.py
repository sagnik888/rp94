"""Read-only verification of the actual NIFTY stop mismatch from the audit snapshot."""
import json
from datetime import datetime
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from engine.entry_policy import ExecutionGeometry, ExpectedMoveForecast, evaluate_final_entry_policy

snapshot = json.loads((ROOT/'reports/ut_smart_loss_audit_20260930.json').read_text())
case = snapshot['today_stop_analysis']
trade = case['trade']
clock = datetime.fromisoformat(trade['entry_filled_at']).replace(tzinfo=None)
entry, stop = trade['entry_price'], trade['initial_stop']
underlying, underlying_stop = trade['entry_spot'], trade['spot_stop']
premium_target = entry + 3*(entry-stop)
underlying_target = underlying + 3*abs(underlying-underlying_stop)
geometry = ExecutionGeometry(
    instrument='NIFTY', direction='LONG', inst_type='OPT', underlying_entry=underlying,
    underlying_stop=underlying_stop, underlying_target=underlying_target,
    premium_entry=entry, premium_stop=stop, premium_target=premium_target,
    contract_entry=entry, contract_stop=stop, contract_target=premium_target,
    observed_at=clock.isoformat(), source='audit_snapshot.actual_fill', verified=True,
    conversion_ratio=abs(case['entry_intel']['delta']),
)
forecast = ExpectedMoveForecast(
    instrument='NIFTY', direction='LONG', timeframe='15min', horizon_minutes=30, horizon_bars=2,
    expected_favorable_move_pct=0, q10_pct=0, q25_pct=0, q50_pct=0, uncertainty_pct=0,
    probability_meeting_floor=0, floor_pct=0, sample_count=0, generated_at='', data_as_of='',
)
common = dict(min_rr=.5, min_rr_override=.5, expected_move_floor_pct=.2,
              max_underlying_stop_pct=1., underlying_stop_cap_override_pct=1., require_forecast=False,
              max_forecast_age_seconds=90, max_geometry_age_seconds=10, checked_at=clock,
              phase='audit_actual_fill', lane='UTS', supported_instruments=['NIFTY'])
verdict = evaluate_final_entry_policy(geometry,forecast,require_premium_stop_coverage=True,**common)
assert not verdict.allowed and dict(verdict.checks)['option_stop_coverage'] is False
recovery = evaluate_final_entry_policy(geometry,forecast,require_premium_stop_coverage=True,
                                       fresh_exposure=False,**common)
assert recovery.allowed
result = {'actual_fill_rejected':not verdict.allowed,
          'delta_mapped_risk':case['delta_mapped_index_risk'],
          'actual_premium_risk':case['actual_premium_risk'],
          'recovery_exposure_preserved':recovery.allowed,'verdict':verdict.to_dict(),
          'scope':'Archived fill prices and delta; nominal targets reconstructed only to isolate stop coverage. No orders or runtime writes.'}
(ROOT/'reports/ut_smart_fix_probes_20260930.json').write_text(json.dumps(result,indent=2))
print(json.dumps({k:result[k] for k in ('actual_fill_rejected','delta_mapped_risk','actual_premium_risk','recovery_exposure_preserved')}))
