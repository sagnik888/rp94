"""The anti-repaint buffer must be sized from the repaint distribution, not by feel.

The operator asked why a signal takes so long to enter. Correcting an earlier bad answer
first: 152s was measured ACROSS DIFFERENT SIGNALS -- the first opposite signal after an
exit, and a later, different one that was accepted. Per SIGNAL the real figures are:

    first appearance -> accepted   n=63  min 0s  p50 90s  p90 365s
    bar open -> accepted           5min  min 33s p50 166s
                                   15min min 38s p50 365s

so the floor was already ~33s: the 30s window plus scan time.

WHY 15s. The live logs record every repaint with its elapsed time ("UT1 arrow vanished
after Ns"), and 24 sessions hold 3,604 of them:

    p50 3.7s   p90 16.0s   max 62.9s
     5s catches 61.2%    20s catches 93.6%
    10s catches 80.9%    30s catches 98.8%   <- the old value
    15s catches 88.8%

Every one of the 24 sessions falls between 84% and 94% caught at 15s. No session is an
outlier -- which is the session-clustered stability two earlier threshold proposals in
this repo could not show, and the reason this one is defensible where those were not.

WHAT IT COSTS, stated because only the timing half is measured. Repaints reaching the
downstream gates rise from ~1.9/session at 30s to ~16.8/session at 15s, against roughly
10 accepted entries per session. How many become bad ENTRIES is unknown: no outcome data
exists for signals the repaint gate refused, so the benefit side of the trade is
measured and the cost side is not.

10s was requested. It was not taken: it costs another ~12 slipped repaints per session
to save 5 more seconds, on the unmeasured side of the trade. It is one setting away.

Note .env overrides the dataclass default in this build, so both were changed;
LIVE_SIGNAL_STABILIZATION_SECONDS* is what actually takes effect.
"""
import re
from pathlib import Path

import pytest

from config.settings import Settings, get_settings

# Pooled from 24 sessions of logs/ut1*.log, 3,604 repaint events.
CAUGHT_AT = {5: 61.2, 10: 80.9, 15: 88.8, 20: 93.6, 30: 98.8}


@pytest.mark.parametrize("timeframe", ["5min", "15min"])
def test_the_window_sits_in_the_evidenced_band(timeframe):
    """15-20s keeps ~89-94% of repaint protection; below 15s the measured cost jumps."""
    settings = Settings()
    value = float(getattr(settings, f"live_signal_stabilization_seconds_{timeframe}"))

    assert 10.0 <= value <= 30.0, (
        f"{timeframe} stabilization is {value}s, outside anything the 24-session repaint "
        "distribution can speak to"
    )
    assert value >= 15.0, (
        f"{timeframe} stabilization is {value}s. Below 15s the pooled distribution shows "
        "repaint protection falling under 88.8% while the COST side -- what those "
        "signals do once admitted -- has never been measured"
    )


def test_the_effective_value_is_what_env_says():
    """.env overrides the dataclass default in this build, and .env is what runs.

    A previous incident in this repo pruned .env from 755 keys to 99 and silently
    reverted behaviour to different code defaults. Changing the dataclass alone would
    have left the live system on 30s while every comment claimed 15s.
    """
    env = Path(".env")
    if not env.exists():
        pytest.skip("no .env in this checkout")
    text = env.read_text(encoding="utf-8")
    for key in (
        "LIVE_SIGNAL_STABILIZATION_SECONDS",
        "LIVE_SIGNAL_STABILIZATION_SECONDS_5MIN",
        "LIVE_SIGNAL_STABILIZATION_SECONDS_15MIN",
    ):
        match = re.search(rf"^{key}='?([\d.]+)'?", text, re.M)
        if not match:
            continue
        value = float(match.group(1))
        assert value >= 15.0, f"{key} in .env is {value}s, below the evidenced floor"
        assert value == float(
            getattr(get_settings(), key.lower())
        ), f"{key} in .env disagrees with the loaded settings value"


def test_the_fast_paths_are_untouched():
    """Spikes and market-wide moves already had short buffers; those are separate."""
    settings = Settings()
    assert float(settings.volume_spike_entry_stabilization_seconds) <= 15.0
    assert float(settings.market_ltp_shock_stabilization_seconds) <= 15.0
    assert float(settings.midcap_15min_risky_stabilization_seconds) >= 30.0, (
        "the MIDCPNIFTY 15min risky-option case deliberately waits longer; it must not "
        "be swept along with the general window"
    )


def test_the_window_still_gates_entry_at_all():
    """A zero window would remove repaint protection entirely."""
    settings = Settings()
    for attribute in (
        "live_signal_stabilization_seconds",
        "live_signal_stabilization_seconds_5min",
        "live_signal_stabilization_seconds_15min",
    ):
        assert float(getattr(settings, attribute)) > 0.0, f"{attribute} disables the gate"


def test_the_requirements_helper_returns_the_configured_window():
    """The setting must actually reach the gate, not be shadowed by a literal."""
    import inspect

    from scanner import Scanner

    source = inspect.getsource(Scanner._live_signal_stabilization_requirements)
    assert "live_signal_stabilization_seconds_5min" in source
    assert "live_signal_stabilization_seconds_15min" in source
    # the risky-option floor is a max() against the base and would pin 30s back on
    assert "max(base_seconds, 30.0)" in source, (
        "the risky-option floor changed shape; re-check whether the new window is "
        "actually reachable for risky 5min options"
    )


def test_the_distribution_this_was_sized_from_is_recorded():
    """The numbers must stay auditable next to the value they justify."""
    settings_text = Path("config/settings.py").read_text(encoding="utf-8")
    block = settings_text[
        settings_text.index("live_signal_stabilization_seconds_5min") - 2200 :
        settings_text.index("live_signal_stabilization_seconds_5min") + 200
    ]
    for marker in ("3,604", "24 sessions", "88.8%", "16.8/session"):
        assert marker in block, (
            f"the evidence for this window no longer records {marker!r}; a threshold "
            "without its measurement is how the last two were wrong"
        )
