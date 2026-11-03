"""Research estimates survive the real candidate ledger without changing policy."""
from copy import deepcopy
from datetime import datetime
import json

import pytest

from config.settings import get_settings
from intelligence.signal_learning import freeze_decision, valid_evidence
from tests.test_grading_telemetry_seam_2026_07_31 import _candidate
from tests.test_signal_learning_redesign_2026_10_04 import candidate as evidence_candidate
from tests.test_workflow_buffer_consistency_2026_09_23 import _scanner


@pytest.mark.parametrize('mode,native,lane', [
    ('quad_prime_s1', 'utbot', 'QP'), ('quad_prime_s1', 's1', 'S1'),
    ('ut_smart_s1', 'ut_smart', 'UTS'), ('ut_smart_s1', 's1', 'S1'),
])
def test_research_outputs_survive_dashboard_and_restart_without_replacing_setup(monkeypatch, mode, native, lane):
    monkeypatch.setattr(get_settings(), 'scanner_engine', mode)
    scanner = _scanner(-.6, 'RANGING')
    scanner._manual_signal_exit_state = lambda c: {}
    holder = _candidate(source_engine=native, source_engines=[native], source_lane=lane)
    scanner._serialize_trade_candidate(holder)  # Establish its production lifecycle identity.
    holder.grade_pillars = evidence_candidate().grade_pillars
    holder.grading_inputs = evidence_candidate().grading_inputs
    holder.market_regime = 'TRENDING_UP'
    holder.setup_score = .71
    holder.calibrated_win_probability = .62
    holder.expected_net_r = -.15
    holder.signal_learning = freeze_decision(holder, 'REJECTED', datetime(2026, 10, 2, 10))
    holder.signal_learning['assessment'] = {
        'status': 'SHADOW_VALIDATED', 'mode': 'SHADOW_ONLY', 'live_eligible': False,
        'calibrated_win_probability': .62, 'expected_net_r': -.15, 'research_grade': 'C',
    }
    original = deepcopy(holder.signal_learning)
    payload = scanner._serialize_trade_candidate(holder)
    assert payload['setup_score'] == .71
    assert payload['grade_basis'] == 'HEURISTIC_SETUP'
    assert payload['calibrated_win_probability'] == .62
    assert payload['expected_net_r'] == -.15
    assert payload['signal_learning'] == original
    payload['signal_learning']['eligibility']['allowed'] = True
    assert holder.signal_learning == original
    restored = scanner._candidate_from_payload(json.loads(json.dumps(scanner._serialize_trade_candidate(holder))))
    assert restored is not None and valid_evidence(restored.signal_learning)
    assert restored.signal_learning == original
    assert (restored.setup_score, restored.calibrated_win_probability, restored.expected_net_r) == (.71, .62, -.15)
    assert (restored.grade, restored.confidence) == (holder.grade, holder.confidence)
    assert restored.source_lane == lane


def test_legacy_candidate_has_setup_score_but_no_invented_probability():
    scanner = _scanner(-.6, 'RANGING')
    scanner._manual_signal_exit_state = lambda c: {}
    holder = _candidate()
    payload = scanner._serialize_trade_candidate(holder)
    assert payload['setup_score'] == holder.confidence
    assert payload['calibrated_win_probability'] is None
    assert payload['expected_net_r'] is None
    assert payload['signal_learning'] == {}


@pytest.mark.parametrize('probability,expected', [('bad', 'bad'), (float('nan'), float('inf')), (1.2, -.1)])
def test_invalid_optional_research_never_discards_a_restored_candidate(probability, expected):
    scanner = _scanner(-.6, 'RANGING')
    scanner._manual_signal_exit_state = lambda c: {}
    holder = _candidate()
    payload = scanner._serialize_trade_candidate(holder)
    payload.update(calibrated_win_probability=probability, expected_net_r=expected)
    restored = scanner._candidate_from_payload(payload)
    assert restored is not None
    assert (restored.grade, restored.confidence, restored.price, restored.stop) == (
        holder.grade, holder.confidence, holder.price, holder.stop)
    assert restored.calibrated_win_probability is None
