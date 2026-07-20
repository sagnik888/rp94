from datetime import datetime

from config.settings import Settings


def _t(text):
    return datetime.strptime(text, "%H:%M").time()


def test_early_session_cutoff_is_explicit_and_aligned_to_session_start():
    s = Settings()
    # The stricter 5min early-morning window must end at the configured cutoff,
    # which is now an explicit setting aligned to the session start (~09:20),
    # NOT the old hidden 09:30 default.
    assert s.ut_early_session_cutoff == "09:20"
    assert _t(s.ut_early_session_cutoff) >= _t(s.ut_session_start)
    assert _t(s.ut_early_session_cutoff) < _t("09:30")


def test_0925_signal_is_outside_the_strict_early_window():
    s = Settings()
    cutoff = _t(s.ut_early_session_cutoff)
    # The reported 09:25 MIDCPNIFTY case: now governed by the normal 5min gate.
    assert _t("09:25") >= cutoff
    # A genuine opening micro-window (session start .. cutoff) still gets the
    # stricter bar.
    assert _t("09:19") < cutoff
