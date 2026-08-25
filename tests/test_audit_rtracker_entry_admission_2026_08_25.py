from scripts.audit_rtracker_entry_admission import (
    build_audit,
    dedupe_exits,
    summarize_archived_post_exit_coverage,
    summarize_outcomes,
)


def _exit(*, event_id, exit_time, pnl, peak, contract=None):
    return {
        "id": event_id,
        "date": "2026-08-21",
        "decision": "EXIT",
        "instrument": "NIFTY",
        "timeframe": "5min",
        "direction": "LONG",
        "entry_timestamp": "2026-08-21T09:15:00",
        "exit_timestamp": exit_time,
        "pnl": pnl,
        "peak_pnl": peak,
        "contract": contract or {},
    }


def test_exit_collisions_are_exposed_and_retention_is_explicit():
    first = _exit(
        event_id="first",
        exit_time="2026-08-21T09:15:10",
        pnl=-10,
        peak=0,
    )
    last = _exit(
        event_id="last",
        exit_time="2026-08-21T09:15:20",
        pnl=-20,
        peak=0,
    )

    earliest, earliest_collisions = dedupe_exits([last, first], keep="earliest")
    latest, latest_collisions = dedupe_exits([last, first], keep="latest")

    assert earliest[0]["id"] == "first"
    assert latest[0]["id"] == "last"
    assert earliest_collisions == latest_collisions
    assert earliest_collisions[0]["row_count"] == 2


def test_exit_dedupe_accepts_mixed_naive_and_offset_timestamps():
    naive = _exit(
        event_id="first",
        exit_time="2026-08-21T09:15:10",
        pnl=-10,
        peak=0,
    )
    offset = _exit(
        event_id="last",
        exit_time="2026-08-21T09:15:20+05:30",
        pnl=-20,
        peak=0,
    )

    latest, _ = dedupe_exits([offset, naive], keep="latest")

    assert latest[0]["id"] == "last"


def test_explicit_lifecycle_and_parallel_legacy_contracts_do_not_collapse():
    explicit_a = {**_exit(event_id="a", exit_time="2026-08-21T09:16:00", pnl=1, peak=2), "lifecycle_id": "life-a"}
    explicit_b = {**_exit(event_id="b", exit_time="2026-08-21T09:16:00", pnl=1, peak=2), "lifecycle_id": "life-b"}
    legacy_a = _exit(
        event_id="legacy-a",
        exit_time="2026-08-21T09:16:00",
        pnl=1,
        peak=2,
        contract={"inst_type": "OPT", "trading_symbol": "NIFTY-A"},
    )
    legacy_b = {
        **legacy_a,
        "id": "legacy-b",
        "source_lane": "B",
        "contract": {"inst_type": "OPT", "trading_symbol": "NIFTY-B"},
    }

    selected, collisions = dedupe_exits(
        [explicit_a, explicit_b, legacy_a, legacy_b], keep="latest"
    )

    assert len(selected) == 4
    assert collisions == []


def test_never_green_and_mixed_basis_capture_are_reproduced_without_relabeling():
    rows = [
        _exit(event_id="loss", exit_time="2026-08-21T09:15:10", pnl=-40, peak=0),
        {
            **_exit(event_id="win", exit_time="2026-08-21T09:15:30", pnl=20, peak=100),
            "entry_timestamp": "2026-08-21T09:15:10",
        },
    ]

    summary = summarize_outcomes(rows)

    assert summary["outcomes"] == 2
    assert summary["never_green"] == 1
    assert summary["never_green_pnl"] == -40.0
    assert summary["never_green_share_of_loser_pot_pct"] == 100.0
    assert summary["never_green_median_hold_seconds"] == 10.0
    assert summary["published_mixed_basis_capture_pct"] == -20.0


def test_build_audit_separates_explicit_options_from_identity_less_rows():
    option = _exit(
        event_id="option",
        exit_time="2026-08-21T09:15:10",
        pnl=-10,
        peak=0,
        contract={"inst_type": "OPT", "trading_symbol": "NIFTYOPT"},
    )
    legacy = {
        **_exit(
            event_id="legacy",
            exit_time="2026-08-22T09:15:10",
            pnl=30,
            peak=50,
        ),
        "date": "2026-08-22",
        "entry_timestamp": "2026-08-22T09:15:00",
    }

    result = build_audit([option, legacy], through="2026-08-21")

    assert result["snapshot"]["latest_exit_retention"]["outcomes"] == 1
    assert (
        result["snapshot"]["schema_era_cohorts_latest"]["explicit_option_contract"]["outcomes"]
        == 1
    )
    assert (
        result["snapshot"]["schema_era_cohorts_latest"]["legacy_identity_less"]["outcomes"]
        == 0
    )
    cohorts = result["schema_era_cohorts_latest"]
    assert cohorts["explicit_option_contract"]["outcomes"]["outcomes"] == 1
    assert cohorts["legacy_identity_less"]["outcomes"]["outcomes"] == 1


def test_sparse_post_exit_endpoints_are_not_called_continuous_coverage(tmp_path):
    row = {
        **_exit(
            event_id="option",
            exit_time="2026-08-21T09:16:00",
            pnl=-10,
            peak=50,
            contract={"inst_type": "OPT", "trading_symbol": "NIFTYOPT"},
        ),
        "pnl_path": [[1.0, 10.0]],
    }
    candles = tmp_path / "2026-08-21" / "candles"
    candles.mkdir(parents=True)
    (candles / "NIFTYOPT_1min.csv").write_text(
        "timestamp,open,high,low,close,volume\n"
        "2026-08-21 09:16:00,100,101,99,100,1\n"
        "2026-08-21 09:46:00,100,101,99,100,1\n",
        encoding="utf-8",
    )

    coverage = summarize_archived_post_exit_coverage([row], tmp_path)

    assert coverage["exact_contract_path_matches"] == 1
    assert coverage["files_with_any_post_exit_bar"] == 1
    assert coverage["latest_post_exit_timestamp_at_least_minutes"]["30"] == 1
    assert coverage["continuous_valid_one_minute_ohlc_through_minutes"]["30"] == 0
    assert coverage["latest_post_exit_timestamp_at_least_minutes"]["60"] == 0
    assert "not an executable quote path" in coverage["warning"]
