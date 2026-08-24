"""An alarm that fires 1,815 times a day is not an alarm.

2026-08-18 logged 1,815 CRITICAL "BAD MARKET DATA" lines — 1,815 of the session's
1,817 critical events, i.e. 99.9% of the critical channel was one message. Everything
else in the log was buried underneath it.

Two independent defects produced that, and both are fixed here.

1. THE COOLDOWN COULD NOT HOLD. The 300s suppression timestamp lived INSIDE the
   divergence record, and that record is popped the moment the two feeds agree again.
   A quote flickering across the threshold therefore cleared its own cooldown and
   re-alerted on the very next crossing. Measured on the day's own lines:

       BANKNIFTY   524 alerts   513 (98%) inside the 300s window   median gap 13s
       MIDCPNIFTY  456 alerts   444 (98%)                          median gap 16s
       NIFTY       411 alerts   403 (98%)                          median gap 16s
       SENSEX      424 alerts   414 (98%)                          median gap 21s

   The timestamp now lives in a map that survives recovery.

2. THE BAR WAS SET INSIDE THE NOISE. One threshold (0.015%) decided both "is this
   worth tracking" and "is this worth waking someone for". The day's distribution:
   p50 0.020%, p90 0.020%, p99 0.040%, max 0.150%. Normal tick-timing jitter tops out
   near 0.04%, so every ordinary tick cleared the alarm bar. Tracking stays at 0.015%
   so the dashboard still sees drift; CRITICAL now needs 0.05%, which would have kept
   10 of the 1,815 lines.

What is deliberately NOT changed: the handler still resolves to the execution broker's
price, and get_quote_divergences() still reports every disagreement. This is a change
to what is shouted about, not to what is believed.
"""
import time

import pytest
from loguru import logger

from config.settings import Settings, get_settings
from data.market_data import MarketDataProvider


class _Probe(MarketDataProvider):
    """Bare provider: __init__ opens broker sessions we neither have nor want here."""

    def __init__(self):
        self._quote_divergence = {}
        self._quote_divergence_alerted_at = {}


@pytest.fixture
def sink():
    """This project logs through loguru, which pytest's caplog does not see."""
    captured = []
    sink_id = logger.add(
        lambda m: captured.append(m.record), level="DEBUG", format="{message}"
    )
    yield captured
    logger.remove(sink_id)


@pytest.fixture
def probe(monkeypatch):
    p = _Probe()
    st = get_settings()
    monkeypatch.setattr(st, "quote_divergence_alert_pct", 0.015)
    monkeypatch.setattr(st, "quote_divergence_critical_pct", 0.05)
    monkeypatch.setattr(
        "notifications.send_desktop_notification", lambda *a, **k: None, raising=False
    )
    return p


def _flag(p, symbol, primary_ltp, secondary_ltp):
    return MarketDataProvider._flag_quote_divergence(
        p, symbol, "kite", float(primary_ltp), "fyers", float(secondary_ltp)
    )


def _criticals(sink):
    return [r for r in sink if r["level"].name == "CRITICAL"]


# ── the shipped values ───────────────────────────────────────────────────────

def test_the_critical_bar_sits_above_the_measured_noise_floor():
    """p99 of the 2026-08-18 divergences was 0.040%."""
    assert Settings.model_fields["quote_divergence_critical_pct"].default == 0.05


def test_tracking_stays_sensitive():
    """The dashboard must not go blind as a side effect of quieting the log."""
    assert Settings.model_fields["quote_divergence_alert_pct"].default == 0.015


def test_the_critical_bar_can_never_fall_below_the_tracking_bar(probe, monkeypatch):
    """A critical threshold under the threshold that gates it is silently dead code."""
    st = get_settings()
    monkeypatch.setattr(st, "quote_divergence_alert_pct", 0.20)
    monkeypatch.setattr(st, "quote_divergence_critical_pct", 0.01)
    assert probe._quote_divergence_critical_pct() >= probe._quote_divergence_alert_pct()


# ── ordinary jitter is tracked but not shouted about ─────────────────────────

def test_normal_jitter_is_recorded_but_not_critical(probe, sink):
    """The 2026-08-18 median: 0.02% on BANKNIFTY."""
    dev = _flag(probe, "BANKNIFTY", 57561.90, 57551.60)
    assert 0.015 <= dev < 0.05
    assert probe.get_quote_divergences().get("BANKNIFTY"), "must still be tracked"
    assert not _criticals(sink), "ordinary jitter must not be CRITICAL"


def test_a_real_disagreement_is_still_critical(probe, sink):
    dev = _flag(probe, "NIFTY", 24000.0, 24048.0)   # 0.20%
    assert dev >= 0.05
    assert len(_criticals(sink)) == 1
    assert "BAD MARKET DATA" in _criticals(sink)[0]["message"]


def test_agreement_clears_the_tracking_record(probe):
    _flag(probe, "NIFTY", 24000.0, 24048.0)
    assert probe.get_quote_divergences().get("NIFTY")
    _flag(probe, "NIFTY", 24000.0, 24000.1)
    assert not probe.get_quote_divergences().get("NIFTY")


# ── the cooldown, which is the actual 1,815-line bug ─────────────────────────

def test_a_sustained_disagreement_alerts_once(probe, sink):
    for _ in range(50):
        _flag(probe, "NIFTY", 24000.0, 24048.0)
    assert len(_criticals(sink)) == 1, "the 300s cooldown is not holding"


def test_a_flickering_quote_cannot_reset_its_own_cooldown(probe, sink):
    """THE BUG. Alternating across the threshold used to clear the cooldown every time,
    because the timestamp lived in the record that recovery pops."""
    for _ in range(50):
        _flag(probe, "NIFTY", 24000.0, 24048.0)    # diverged, critical-worthy
        _flag(probe, "NIFTY", 24000.0, 24000.1)    # agreed again -> record popped
    assert len(_criticals(sink)) == 1, (
        f"{len(_criticals(sink))} alerts from one flickering quote — the cooldown "
        f"is still stored somewhere that recovery erases"
    )


def test_the_cooldown_does_expire(probe, sink, monkeypatch):
    """Suppression must not become permanent silence."""
    _flag(probe, "NIFTY", 24000.0, 24048.0)
    assert len(_criticals(sink)) == 1
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 301.0)
    _flag(probe, "NIFTY", 24000.0, 24048.0)
    assert len(_criticals(sink)) == 2


def test_each_symbol_keeps_its_own_cooldown(probe, sink):
    for sym, px in (("NIFTY", 24000.0), ("BANKNIFTY", 57000.0), ("SENSEX", 77000.0)):
        _flag(probe, sym, px, px * 1.002)
    assert len(_criticals(sink)) == 3


# ── the measured claim, against the real log ─────────────────────────────────

def test_the_new_bar_would_have_silenced_the_2026_08_18_flood():
    """Replays the day's actual divergence percentages through the new threshold."""
    import glob
    import io
    import re

    logs = glob.glob("logs/ut1.2026-08-18*.log")
    if not logs:
        pytest.skip("2026-08-18 log not present")
    pat = re.compile(r"BAD MARKET DATA: \w+ .*? by ([0-9.]+)%")
    devs = []
    for f in logs:
        for line in io.open(f, encoding="utf-8", errors="replace"):
            if "BAD MARKET DATA" not in line:
                continue
            m = pat.search(line)
            if m:
                devs.append(float(m.group(1)))
    if len(devs) < 100:
        pytest.skip(f"only {len(devs)} divergence lines")

    bar = Settings.model_fields["quote_divergence_critical_pct"].default
    kept = [d for d in devs if d >= bar]
    assert len(devs) > 1000, "expected the flood to be present in this log"
    assert len(kept) < len(devs) * 0.02, (
        f"{len(kept)} of {len(devs)} lines still critical at {bar}%"
    )
    # But it must not silence everything: the genuine outliers survive.
    assert kept, "the new bar suppressed even the largest divergence of the day"
