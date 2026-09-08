"""Session archiver + contract-identity capture (2026-07-24).

The offline exit backtester replays each session's REAL option-premium path. The live
candle cache only holds last-fetched contracts, so traded strikes were lost/overwritten
(only ~40% of 2026-07-24 trades were replayable). Two fixes are covered here:

  1. rtracker records the traded CONTRACT IDENTITY, so replay matches exactly instead
     of guessing by premium proximity.
  2. scripts/archive_session_data.py snapshots each session's trades + day-sliced
     candles into data_store/session_archive/<date>/, and refuses heavy IO during
     market hours unless forced.
"""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]

_SPEC = importlib.util.spec_from_file_location(
    "arch", ROOT / "scripts" / "archive_session_data.py"
)
arch = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(arch)


# ── 1. contract identity in rtracker events ──────────────────────────────────

def test_rtracker_event_records_contract_identity():
    from intelligence.rtracker import RegimeTracker

    tracker = RegimeTracker.__new__(RegimeTracker)
    candidate = SimpleNamespace(
        instrument="NIFTY", timeframe="5min", direction="LONG", price=183.7,
        stop=169.9, target=204.3, confidence=0.8, grade="A",
        trading_symbol="NIFTY28JUL2623600CE", symbol_token="12345",
        inst_type="OPT", option_type="CE", atm_strike=23600.0, lots=1, lot_size=65,
    )
    event = tracker._event_from_candidate(
        candidate, decision="ACCEPTED", reason="", source="test", extra=None
    )
    c = event.get("contract")
    assert isinstance(c, dict)
    assert c["trading_symbol"] == "NIFTY28JUL2623600CE"
    assert c["symbol_token"] == "12345"
    assert c["inst_type"] == "OPT"
    assert c["option_type"] == "CE"
    assert c["atm_strike"] == 23600.0
    assert c["lot_size"] == 65


def test_backtester_matches_recorded_contract_exactly():
    spec = importlib.util.spec_from_file_location(
        "bt2", ROOT / "scripts" / "backtest_exit_lifecycle.py"
    )
    bt = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bt)

    df = pd.DataFrame([{"timestamp": pd.Timestamp(2026, 7, 24, 9, 20),
                        "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}])
    contracts = {
        "NIFTY28JUL2623600CE_1min.csv": df,   # the recorded contract
        "NIFTY28JUL2623800CE_1min.csv": df,   # a decoy with closer premium
    }
    trade = {"trading_symbol": "NIFTY28JUL2623600CE", "instrument": "NIFTY",
             "option_type": "CE", "entry": 999.0,
             "entry_ts": pd.Timestamp(2026, 7, 24, 9, 25)}
    name, _ = bt.match_contract(trade, contracts)
    # exact identity wins even though the entry premium matches neither
    assert name == "NIFTY28JUL2623600CE_1min.csv"


# ── 2. archiver behaviour ────────────────────────────────────────────────────

def test_traded_symbols_extracted_from_events():
    events = [
        {"contract": {"trading_symbol": "NIFTY28JUL2623600CE"}},
        {"contract": {"trading_symbol": "BANKNIFTY28JUL2656600CE"}},
        {"contract": {"trading_symbol": ""}},
        {"no_contract": True},
    ]
    assert arch.traded_symbols(events) == {
        "NIFTY28JUL2623600CE", "BANKNIFTY28JUL2656600CE"
    }


def test_next_day_shadow_outcome_does_not_demand_prior_day_contract_candles():
    events = [
        {
            "decision": "ACCEPTED",
            "contract": {
                "trading_symbol": "NIFTY08SEP2624000CE",
                "symbol_token": "current-token",
            },
        },
        {
            "decision": "SHADOW_OUTCOME",
            "shadow_outcome": {"session_date": "2026-09-03"},
            "contract": {
                "trading_symbol": "SENSEX03SEP2676500PE",
                "symbol_token": "expired-token",
            },
        },
    ]

    assert arch.traded_symbols(events, "2026-09-04") == {"NIFTY08SEP2624000CE"}
    assert arch.contract_tokens(events, "2026-09-04") == {
        "NIFTY08SEP2624000CE": "current-token"
    }


def test_same_day_or_unowned_shadow_outcome_remains_fail_closed_contract_evidence():
    events = [
        {
            "decision": "SHADOW_OUTCOME",
            "shadow_outcome": {"session_date": "2026-09-04"},
            "contract": {"trading_symbol": "SAME_DAY_CE"},
        },
        {
            "decision": "SHADOW_OUTCOME",
            "shadow_outcome": {},
            "contract": {"trading_symbol": "UNKNOWN_DAY_PE"},
        },
    ]

    assert arch.traded_symbols(events, "2026-09-04") == {
        "SAME_DAY_CE",
        "UNKNOWN_DAY_PE",
    }


def test_day_slice_filters_to_the_session(tmp_path):
    p = tmp_path / "X_1min.csv"
    pd.DataFrame([
        {"timestamp": "2026-07-23 09:20:00", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
        {"timestamp": "2026-07-24 09:20:00", "open": 2, "high": 2, "low": 2, "close": 2, "volume": 1},
        {"timestamp": "2026-07-24 09:21:00", "open": 3, "high": 3, "low": 3, "close": 3, "volume": 1},
    ]).to_csv(p, index=False)
    sub = arch.day_slice(p, "2026-07-24")
    assert sub is not None and len(sub) == 2
    assert arch.day_slice(p, "2026-07-25") is None


def test_archive_skips_during_market_hours(monkeypatch, tmp_path):
    monkeypatch.setattr(arch, "market_hours_now", lambda: True)
    out = arch.archive_day("2026-07-24", force=False, quiet=True)
    assert out.get("skipped") == "market_hours"


def test_archive_skips_when_no_events(monkeypatch):
    monkeypatch.setattr(arch, "market_hours_now", lambda: False)
    monkeypatch.setattr(arch, "load_events", lambda day: [])
    out = arch.archive_day("1999-01-01", force=True, quiet=True)
    assert out.get("skipped") == "no_events"


def test_archive_writes_trades_and_manifest(monkeypatch, tmp_path):
    monkeypatch.setattr(arch, "market_hours_now", lambda: False)
    monkeypatch.setattr(arch, "ARCHIVE", tmp_path / "session_archive")
    monkeypatch.setattr(arch, "CANDLES", tmp_path / "candles")
    (tmp_path / "candles").mkdir()
    # >120 bytes so the archiver's empty-file guard does not skip it (realistic session)
    pd.DataFrame([
        {"timestamp": f"2026-07-24 09:{20 + i:02d}:00",
         "open": 100 + i, "high": 102 + i, "low": 99 + i, "close": 101 + i, "volume": 500 + i}
        for i in range(10)
    ]).to_csv(tmp_path / "candles" / "NIFTY28JUL2623600CE_1min.csv", index=False)
    events = [{"decision": "ACCEPTED", "instrument": "NIFTY",
               "contract": {"trading_symbol": "NIFTY28JUL2623600CE"}}]
    monkeypatch.setattr(arch, "load_events", lambda day: events)

    man = arch.archive_day("2026-07-24", force=True, quiet=True)

    out = tmp_path / "session_archive" / "2026-07-24"
    assert json.loads((out / "trades.json").read_text())[0]["decision"] == "ACCEPTED"
    assert man["has_contract_identity"] is True
    assert man["contract_files_archived"] == 1
    assert (out / "candles" / "NIFTY28JUL2623600CE_1min.csv").exists()


def test_archive_preserves_prior_day_shadow_row_without_poisoning_coverage(monkeypatch, tmp_path):
    monkeypatch.setattr(arch, "market_hours_now", lambda: False)
    monkeypatch.setattr(arch, "ARCHIVE", tmp_path / "session_archive")
    monkeypatch.setattr(arch, "CANDLES", tmp_path / "candles")
    (tmp_path / "candles").mkdir()
    rows = [
        {
            "decision": "SHADOW_OUTCOME",
            "shadow_outcome": {"session_date": "2026-09-03"},
            "contract": {"trading_symbol": "SENSEX03SEP2676500PE"},
        }
    ]
    monkeypatch.setattr(arch, "load_events", lambda day: rows)

    manifest = arch.archive_day("2026-09-04", force=True, quiet=True)

    saved = json.loads(
        (tmp_path / "session_archive" / "2026-09-04" / "trades.json").read_text()
    )
    assert saved == rows
    assert manifest["traded_symbols_recorded"] == []
    assert manifest["missing_contract_symbols"] == []
    assert manifest["cross_session_contract_events_excluded"] == 1
    assert manifest["decisions"] == {"SHADOW_OUTCOME": 1}


def test_ownership_migration_reuses_frozen_candles_without_live_cache(monkeypatch, tmp_path):
    day = "2026-08-31"
    candles = tmp_path / "live-candles"
    events_dir = tmp_path / "events"
    archive_dir = tmp_path / "session-archive"
    frozen = archive_dir / day / "candles"
    candles.mkdir()
    events_dir.mkdir()
    frozen.mkdir(parents=True)

    rows = [
        {
            "decision": "ACCEPTED",
            "contract": {"trading_symbol": "NIFTY08SEP2624000CE"},
        },
        {
            "decision": "SHADOW_OUTCOME",
            "shadow_outcome": {"session_date": "2026-08-28"},
            "contract": {"trading_symbol": "NIFTY01SEP2624100CE"},
        },
    ]
    (events_dir / f"{day}.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
    )
    frame = pd.DataFrame(
        [
            {
                "timestamp": f"{day} 09:{15 + i:02d}:00",
                "open": 100 + i,
                "high": 102 + i,
                "low": 99 + i,
                "close": 101 + i,
                "volume": 500 + i,
            }
            for i in range(10)
        ]
    )
    frame.to_csv(frozen / "NIFTY08SEP2624000CE_1min.csv", index=False)
    for index in arch.INDICES:
        for timeframe in ("1min", "5min", "15min"):
            frame.to_csv(frozen / f"{index}_{timeframe}.csv", index=False)

    monkeypatch.setattr(arch, "market_hours_now", lambda: False)
    monkeypatch.setattr(arch, "CANDLES", candles)
    monkeypatch.setattr(arch, "EVENTS", events_dir)
    monkeypatch.setattr(arch, "ARCHIVE", archive_dir)

    manifest = arch.archive_day(day, force=True, quiet=True)

    assert manifest["complete"] is True
    assert manifest["schema_version"] == arch.ARCHIVE_SCHEMA_VERSION
    assert manifest["contract_ownership_version"] == arch.CONTRACT_OWNERSHIP_VERSION
    assert manifest["events"] == 2
    assert manifest["cross_session_contract_events_excluded"] == 1
    assert manifest["traded_symbols_recorded"] == ["NIFTY08SEP2624000CE"]
    assert manifest["contract_symbols_archived"] == ["NIFTY08SEP2624000CE"]
    assert manifest["contract_files_archived"] == 1
    assert manifest["spot_files_archived"] == 12
    assert json.loads((archive_dir / day / "trades.json").read_text()) == rows


def test_main_py_wires_the_archiver_scheduler():
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    # Takes the authenticated data provider since 2026-08-14: the archiver child has no
    # broker session, so contract premium is back-filled in the parent before it runs.
    #
    # As a GETTER, not the object. create_system() builds the provider on the dashboard
    # bootstrap task, which runs after this scheduler is wired, so there is nothing to
    # pass at wiring time -- naming the object here crashed startup with NameError.
    #
    # This assertion was previously a substring match on the exact call text, which
    # matched the crashing line perfectly: it proved main.py CONTAINED the wiring, never
    # that the name resolved. test_main_startup_has_no_unbound_names_2026_08_14.py now
    # carries that half by walking main()'s bytecode.
    assert "def start_session_archiver_scheduler(data_provider=None)" in src
    main_src = src.split("def main()")[-1]
    i = main_src.index("start_session_archiver_scheduler(")
    call = main_src[i: i + 200]
    assert "data_provider=" in call
    assert "lambda" in call, "the provider must be resolved at fire time, not wiring time"
    assert "SESSION_ARCHIVE_ENABLED" in src
