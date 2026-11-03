"""An explicit zero FUT allocation must survive mixed-book slot normalization."""
from datetime import datetime

import pytest

from config.settings import Settings
from engine.scanner_modes import COMBINED_MODES
from tests.test_multi_signal_mixed_book import _cand, _scanner


def test_settings_accept_explicit_zero_future_slots():
    settings = Settings(_env_file=None, multi_signal_max_fut=0)
    assert settings.multi_signal_max_fut == 0


@pytest.mark.parametrize("mode", ["quad_prime", "ut_smart", "s1", *COMBINED_MODES])
@pytest.mark.parametrize("preference", ["AUTO", "OPT", "FUT"])
def test_zero_future_allocation_keeps_existing_preference_reallocation(monkeypatch, mode, preference):
    settings = Settings(_env_file=None, scanner_engine=mode,
        multi_signal_mixed_book_enabled=True, multi_signal_max_fut=0,
        multi_signal_max_opt_instruments=2, multi_signal_min_instruments=3,
        multi_signal_min_timeframes=2, multi_signal_min_engines=2)
    monkeypatch.setattr("config.settings._settings", settings)
    scanner = _scanner()
    scanner.inst_pref = preference
    native = COMBINED_MODES.get(mode, mode)
    inst_type = "OPT" if preference == "OPT" else "FUT"
    candidates = [_cand(inst, inst_type=inst_type, source_engine=native,
        source_lane="S1" if native == "s1" else ("UTS" if native == "ut_smart" else "QP"),
        signal_timestamp=datetime(2026, 10, 5, 10))
        for inst in ("NIFTY", "BANKNIFTY", "SENSEX")]
    assert scanner._multi_signal_mixed_book_trigger(candidates)
    result = scanner._apply_multi_signal_mixed_book(candidates)
    futures = sum(c.inst_type == "FUT" for c in result)
    options = sum(c.inst_type == "OPT" for c in result)
    if preference == "AUTO":
        assert futures == 0
        assert options <= 2
    elif preference == "OPT":
        # OPT preference still receives OPT+FUT allocation; zero adds no slot.
        assert futures == 0
        assert options == 2
    else:
        # FUT preference still receives OPT+FUT allocation, capped by active indices.
        assert futures == 2
        assert options == 0
