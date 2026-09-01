"""An empty forensics tab must say why it is empty.

2026-09-01. The operator reported the Settings > Signal Forensics tab as "not working at
all". It was working. The API was healthy:

    {"items": [], "total": 1, "signal_total": 1, "synthetic_rows_hidden": 1,
     "status": "ok", "session_date": "2026-08-30",
     "available_dates": ["2026-08-30"]}

The whole signal_lifecycle_events table held ONE row -- the LFC-TEST-12345 fixture --
which the server deliberately hides as synthetic (a forensics surface showing fabricated
history is worse than one showing nothing). So the table was correctly empty, and the UI
rendered the words "No items found", which is indistinguishable from a broken tab.

Both halves of the pipeline were verified working during the investigation:

  * _record_trade_forensics() produces events (probed directly: one event on a new
    trade, a second on an OPEN -> CLOSED transition),
  * db.append_signal_lifecycle_events() persists them (probed against the live DB and
    the probe row removed afterwards).

Nothing reaches them because forensics deliberately records REAL-mode trades only, and
no real trade has been taken -- Auto Mode is Manual, the Kite account holds Rs 0.90, and
Kite reports zero orders. The trade manager's newest row is 2026-08-24.

So the defect is one of explanation, not of function. Two things are fixed here: the
empty state now names its cause, and a failed fetch -- previously console.error only --
becomes visible instead of masquerading as "no data".
"""
from pathlib import Path

import pytest

SCRIPT = Path("dashboard/static/js/signal-forensics.js")


@pytest.fixture(scope="module")
def source():
    return SCRIPT.read_text(encoding="utf-8")


def test_the_bare_no_items_found_message_is_gone(source):
    """It was true, useless, and read as a fault."""
    assert ">No items found<" not in source, (
        "the forensics tab still renders a bare 'No items found', which cannot be told "
        "apart from a broken surface"
    )


def test_hidden_test_rows_are_explained(source):
    """The exact 2026-09-01 case: total 1, all synthetic, nothing shown."""
    assert "syntheticHidden" in source, (
        "the UI does not read synthetic_rows_hidden, so it cannot explain the one case "
        "that actually occurred"
    )
    assert "synthetic_rows_hidden" in source, "the API field is never captured"
    assert "REAL-mode trades only" in source, (
        "the empty state does not say that forensics only records REAL-mode trades, "
        "which is the reason the table is empty"
    )


def test_the_other_empty_reasons_are_distinguished(source):
    """Filters, wrong date, and genuinely-no-data are different problems."""
    for phrase in (
        "No rows match the current filters",
        "Data exists for",
        "No forensics recorded yet",
    ):
        assert phrase in source, f"the empty state cannot report: {phrase!r}"


def test_a_failed_fetch_is_shown_not_just_logged(source):
    """A console-only error looks exactly like an empty table."""
    assert "model.loadError" in source, "fetch failures are not carried to the render"
    assert "Could not load forensics" in source, (
        "a fetch failure still renders as an ordinary empty state"
    )
    catch_at = source.index("catch (err)")
    window = source[catch_at : catch_at + 600]
    assert "renderRows(false)" in window, (
        "the error path does not re-render, so the message never reaches the table"
    )


def test_the_explanation_is_escaped(source):
    """The date and error text reach innerHTML; both are interpolated values."""
    empty_at = source.index("model.items.length === 0 && !append")
    window = source[empty_at : empty_at + 1800]
    assert "esc(model.sessionDate" in window, "session date is interpolated unescaped"
    assert "esc(model.loadError)" in window, "error text is interpolated unescaped"


def test_the_server_still_hides_synthetic_rows(source):
    """The UI change must not become a reason to start showing fabricated history."""
    server = Path("dashboard/server.py").read_text(encoding="utf-8")
    assert "_is_synthetic_forensic_row" in server
    assert "_drop_synthetic_forensic_rows" in server
    assert "LFC-TEST" in server, (
        "the synthetic-row guard no longer recognises the test fixture"
    )
