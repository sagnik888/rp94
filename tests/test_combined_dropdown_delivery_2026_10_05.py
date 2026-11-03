"""Combined engine selection survives stale or unavailable dashboard scripts."""
from html.parser import HTMLParser
import json
from pathlib import Path
import shutil
import subprocess
from urllib.parse import parse_qs, urlsplit

import pytest


class DashboardMarkup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_engine = False
        self.current_option = None
        self.options = []
        self.scripts = {}
        self.feed(Path('dashboard/static/index.html').read_text(encoding='utf-8'))

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag == 'select' and attrs.get('id') == 'scannerEngineSelect':
            self.in_engine = True
        elif tag == 'option' and self.in_engine:
            self.current_option = {'value': attrs['value'], 'label': '', 'selected': 'selected' in attrs}
            self.options.append(self.current_option)
        elif tag == 'script' and attrs.get('src'):
            url = urlsplit(attrs['src'])
            self.scripts[Path(url.path).name] = parse_qs(url.query).get('v', [''])[0]

    def handle_data(self, data):
        if self.current_option is not None:
            self.current_option['label'] += data

    def handle_endtag(self, tag):
        if tag == 'option':
            self.current_option = None
        elif tag == 'select':
            self.in_engine = False


@pytest.mark.parametrize('mode,label', [
    ('quad_prime_s1', 'QUAD PRIME + S1'),
    ('ut_smart_s1', 'UT SMART EXIT + S1'),
])
def test_combined_mode_is_selectable_before_any_script_runs(mode, label):
    from dashboard.server import _valid_command_payload
    page = DashboardMarkup()
    matches = [option for option in page.options if option['value'] == mode]
    assert len(matches) == 1, f'{mode} missing or duplicated in initial ENGINE dropdown'
    assert matches[0]['label'].strip() == label
    assert [option['value'] for option in page.options if option['selected']] == ['quad_prime']
    assert _valid_command_payload({'cmd': 'set_scanner_engine', 'state': matches[0]['value']}) is None


def test_s12_dual_is_selectable_and_command_is_accepted():
    from dashboard.server import _valid_command_payload
    page = DashboardMarkup()
    matches = [option for option in page.options if option['value'] == 's1s2_dual']
    assert len(matches) == 1, 'S1+S2 Dual missing or duplicated in the initial ENGINE dropdown'
    assert matches[0]['label'].strip() == 'S1+S2 DUAL'
    assert _valid_command_payload({'cmd': 'set_scanner_engine', 'state': 's1s2_dual'}) is None


def test_initial_and_settings_options_have_identical_order_without_duplicates():
    node = shutil.which('node') or r'C:\Program Files\nodejs\node.exe'
    script = """
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync('dashboard/static/js/dashboard-settings.js', 'utf8');
const prefix = source.slice(0, source.indexOf('const S1_SETTINGS_FIELDS'));
process.stdout.write(vm.runInNewContext(prefix + '\\nJSON.stringify(SCANNER_ENGINE_OPTIONS.map(row => row[0]))'));
"""
    result = subprocess.run([node, '-e', script], capture_output=True, text=True, check=True)
    initial = [option['value'] for option in DashboardMarkup().options]
    assert initial == json.loads(result.stdout)
    assert len(initial) == len(set(initial))


def test_combined_upgrade_does_not_request_pre_upgrade_script_cache_keys():
    previous = {
        'dashboard-charts.js': '5.2.0-s1-candles',
        'dashboard-settings.js': '52-last-entry-combined-s1',
        'app.js': 'oct2026-v51-blocks-waits',
    }
    current = DashboardMarkup().scripts
    for filename, old_cache_key in previous.items():
        assert current[filename] and current[filename] != old_cache_key, f'{filename} still requests cached pre-upgrade code'
