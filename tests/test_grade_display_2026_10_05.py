"""Rendered setup scores never masquerade as calibrated win probabilities."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest


def render(row):
    node = shutil.which('node') or r'C:\Program Files\nodejs\node.exe'
    script = r'''
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync('dashboard/static/js/trade-table.js', 'utf8');
const prefix = source.slice(0, source.indexOf('function formatTradeTableSharpeDailyYearly'));
const row = JSON.parse(fs.readFileSync(0, 'utf8'));
process.stdout.write(vm.runInNewContext(prefix + '\nrenderSignalScore(row)', {row}));
'''
    result = subprocess.run([node, '-e', script], input=json.dumps(row), text=True,
                            capture_output=True, check=True, cwd=Path(__file__).resolve().parents[1])
    return result.stdout


def validated(probability=.62, expected=-.15, **values):
    return {'confidence': .85, 'setup_score': .71, 'grade': 'A+', 'timeframe': '5min', 'learning_evidence_valid': True,
            'signal_learning': {'assessment': {'status': 'SHADOW_VALIDATED',
                'calibrated_win_probability': probability, 'expected_net_r': expected, 'research_grade': 'C'}}, **values}


def test_setup_and_research_scores_are_rendered_separately():
    rendered = render(validated())
    assert '71/100' in rendered and 'A+' in rendered and '5m' in rendered
    assert rendered.count('71/100') == 1 and rendered.count('5m') == 1
    assert 'Setup A+' not in rendered
    assert 'Research C' in rendered and 'Win 62%' in rendered and 'Net -0.15R' in rendered
    assert 'not a win probability' in rendered
    assert '85%' not in rendered


@pytest.mark.parametrize('row', [
    {'confidence': .85, 'grade': 'A+'},
    {'confidence': .85, 'grade': 'A+', 'calibrated_win_probability': .99, 'expected_net_r': 3.},
    validated(learning_evidence_valid=False),
    validated(signal_learning={'assessment': {'status': 'UNVALIDATED', 'calibrated_win_probability': .99, 'expected_net_r': 3.}}),
    validated(probability=None), validated(probability=1.1), validated(expected='not a number'),
])
def test_missing_invalid_or_unvalidated_research_never_invents_a_win_chance(row):
    rendered = render(row)
    assert 'Win chance unavailable' not in rendered
    assert 'Research ' not in rendered and 'Net ' not in rendered and 'Win 99%' not in rendered


@pytest.mark.parametrize('probability,expected,text', [(0, 0, 'Win 0%'), (1, .15, 'Win 100%')])
def test_valid_zero_and_boundary_probability_are_not_treated_as_missing(probability, expected, text):
    rendered = render(validated(probability, expected))
    assert text in rendered and 'unavailable' not in rendered


def test_broker_or_archive_grade_text_is_escaped():
    rendered = render({'confidence': .8, 'grade': '<img src=x onerror=alert(1)>'})
    assert '<img' not in rendered and '&lt;img' in rendered


@pytest.mark.parametrize(('raw', 'compact'), [('1min', '1m'), ('5min', '5m'), ('15min', '15m')])
def test_unavailable_win_probability_keeps_compact_score_grade_timeframe(raw, compact):
    rendered = render({'confidence': .82, 'grade': 'B+', 'timeframe': raw})
    assert '82/100' in rendered and 'B+' in rendered and compact in rendered
    assert rendered.count('82/100') == 1 and rendered.count(compact) == 1
    assert 'Win chance unavailable' not in rendered
    assert 'Research ' not in rendered and 'Win ' not in rendered and 'Net ' not in rendered


def test_both_table_paths_use_the_shared_score_renderer_and_keep_learning_fields():
    source = Path('dashboard/static/js/trade-table.js').read_text(encoding='utf-8')
    assert 'renderSignalScore(t)' in source and 'renderSignalScore({ ...sig, grade })' in source
    assert "${t.timeframe || '5min'}" not in source
    assert "${sig.timeframe || '5min'}" not in source
    assert 'signal_learning: sig.signal_learning' in source
    assert 'learning_evidence_valid: sig.learning_evidence_valid === true' in source
