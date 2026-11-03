import csv
from intelligence.confidence_calibration import load_calibrator


def test_calibration_uses_only_outcomes_known_before_decision_and_cache_keeps_cutoff(tmp_path):
    path=tmp_path/'outcomes.csv'
    rows=[
        (.6,100,'2026-09-01T09:30:00+05:30'),
        (.7,-100,'2026-09-01T09:31:00+05:30'),
        (.8,100,'2026-09-01T10:00:00+05:30'),
        (.9,-100,'2026-09-01T10:01:00+05:30'),
        (.9,100,''),
    ]
    with path.open('w',newline='') as f:
        w=csv.writer(f);w.writerow(['confidence','pnl','exit_time']);w.writerows(rows)
    assert load_calibrator(str(path),min_samples=2,as_of='2026-09-01T09:31:00+05:30') is None
    early=load_calibrator(str(path),min_samples=2,as_of='2026-09-01T04:02:00Z')
    assert early.n==2
    later=load_calibrator(str(path),min_samples=2,as_of='2026-09-01T10:02:00+05:30')
    assert later.n==4
    assert load_calibrator(str(path),min_samples=2,as_of='2026-09-01T09:32:00+05:30').n==2
