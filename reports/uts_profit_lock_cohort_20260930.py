"""Replay the frozen UTS opportunity cohort locally, without runtime changes.

Every included path must match its recorded pandas input hash, baseline net
P&L and original modeled charges. This evaluates the existing entry cohort;
it does not generate new opportunities or claim an executable portfolio.
"""
from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import os
import socket
import sys
import tempfile
import textwrap
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _summary(rows):
    totals = {
        key: round(sum(row[key] for row in rows), 2)
        for key in (
            "before_net", "verified_fill_only_net", "after_net",
            "verified_fill_only_net_with_estimated_charges",
            "after_net_raw_fill_flat_80", "after_net_with_estimated_charges",
        )
    }
    totals.update(
        trades=len(rows),
        difference=round(totals["after_net"] - totals["before_net"], 2),
        fill_preservation_difference=round(
            totals["verified_fill_only_net"] - totals["before_net"], 2
        ),
        high_peak_difference=round(
            totals["after_net"] - totals["verified_fill_only_net"], 2
        ),
        improved=sum(row["difference"] > .01 for row in rows),
        worsened=sum(row["difference"] < -.01 for row in rows),
        unchanged=sum(abs(row["difference"]) <= .01 for row in rows),
        before_wins=sum(row["before_net"] > 0 for row in rows),
        after_wins=sum(row["after_net"] > 0 for row in rows),
        additional_estimated_cost_vs_flat_80=round(
            sum(row["after_estimated_charges"] - 80. for row in rows), 2
        ),
    )
    return totals


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--frozen", type=Path,
        default=ROOT / "reports/uts_profit_reversal_trades_20260930.json",
    )
    parser.add_argument(
        "--output", type=Path,
        default=Path(__file__).with_suffix(".json"),
    )
    args = parser.parse_args()
    frozen = json.loads(args.frozen.read_text(encoding="utf-8"))["closed"]

    def blocked(*_args, **_kwargs):
        raise RuntimeError("Frozen cohort replay has network disabled")

    socket.socket.connect = blocked
    socket.create_connection = blocked
    from loguru import logger
    logger.remove()

    import pandas as pd
    with tempfile.TemporaryDirectory(prefix="uts_frozen_cohort_") as temporary:
        database = str(Path(temporary) / "isolated.db")
        os.environ["UTBOT_TEST_DB_PATH"] = database
        os.environ["UTBOT_DB_PATH"] = database
        import config.settings as settings_module
        import engine.signal_processor as processor_module
        from data.sqlite_db import DatabaseManager
        from engine.trade_accounting import estimate_trade_charges

        settings = SimpleNamespace(
            options_sl_pct=15., ut_force_exit_time="15:25",
            uts_lane_lock_arm_r=1., uts_lane_lock_giveback_pct=30.,
            opt_cost=80., slippage_opt_pct=.02,
            slippage_auto_calibration_enabled=False,
        )
        settings_module._settings = settings
        processor = processor_module.SignalProcessor.__new__(
            processor_module.SignalProcessor
        )
        source = textwrap.dedent(inspect.getsource(
            processor_module.SignalProcessor._simulate_actual_option_exit
        ))
        high_call = (
            "            uts_lane_policy.exit_decision(\n"
            "                uts_trade, 0.0, high, "
            "now=(ts + pd.Timedelta(minutes=1)).to_pydatetime(),\n"
            "            )\n"
        )
        if source.count(high_call) != 1:
            raise RuntimeError("Production helper changed; review the baseline adapter")
        before_source = source.replace(high_call, "")
        namespace = dict(processor_module.__dict__)
        namespace["get_settings"] = lambda: settings
        exec(compile(before_source, "<close_only_uts_baseline>", "exec"), namespace)
        close_helper = namespace["_simulate_actual_option_exit"]

        cache, rows, unavailable = {}, [], []
        normalizations = Counter()
        try:
            with (
                patch.object(processor_module, "get_settings", lambda: settings),
                patch.object(settings_module, "get_settings", lambda: settings),
            ):
                for original in frozen:
                    identity = original["id"]
                    meta = original.get("replay_input") or {}
                    contract = meta.get("contract") or {}
                    symbol = contract.get("contract_symbol")
                    path = ROOT / "data_store/candles" / f"{symbol}_1min.csv"
                    if not symbol or not path.exists():
                        unavailable.append({"id": identity, "reason": "Contract CSV missing"})
                        continue
                    if symbol not in cache:
                        cache[symbol] = pd.read_csv(path, index_col=0, parse_dates=True)
                    frame = cache[symbol].copy()
                    frame.index = pd.DatetimeIndex([
                        stamp.tz_convert("Asia/Kolkata").tz_localize(None)
                        if stamp.tzinfo else stamp for stamp in frame.index
                    ])
                    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
                    start = pd.Timestamp(meta["eligible_at"])
                    end = pd.Timestamp(meta["last_closed_at"])
                    frame = frame.loc[
                        (frame.index >= start)
                        & (frame.index + pd.Timedelta(minutes=1) <= end)
                    ]
                    matched = None
                    for columns in (
                        (), ("provider_time",), ("receipt_at",),
                        ("provider_time", "receipt_at"),
                    ):
                        candidate = frame.copy()
                        for column in columns:
                            if column in candidate:
                                candidate[column] = pd.to_datetime(candidate[column])
                        digest = hashlib.sha256(
                            pd.util.hash_pandas_object(candidate, index=True).values.tobytes()
                        ).hexdigest()
                        if digest == meta.get("input_sha256"):
                            matched = candidate
                            normalizations[",".join(columns) or "CSV as read"] += 1
                            break
                    if matched is None:
                        unavailable.append({"id": identity, "reason": "Input hash differs"})
                        continue
                    matched.attrs = contract.copy()
                    index = original["instrument"].split()[0]
                    quantity = int(original["lots"]) * int(original["lot_size"])
                    cap = float(meta["option_stop_cap_pct"])
                    setup = SimpleNamespace(
                        source_lane="UTS", instrument=index,
                        timeframe=original["timeframe"],
                        quality_verdict={"policy_version": "uts_native.v1", "checks": {
                            "uts_premium_cap_pct": cap,
                            "uts_premium_cap_profile": (
                                "midcap.v1" if index == "MIDCPNIFTY" else "standard.v1"
                            ),
                        }},
                    )
                    chart_reason = {
                        "UTS_REVERSE": "UT_REVERSE", "UTS_SMART_EXIT": "SMART_EXIT",
                        "UTS_HARD_EXIT": "SMART_HARD_EXIT",
                    }.get(original["exit_reason"], "")
                    arguments = dict(
                        option_df=matched, start_ts=start.to_pydatetime(),
                        end_ts=end.to_pydatetime(), sl_price=original["initial_stop"],
                        rr=3., max_sl_pct=cap / 100., qty=quantity,
                        tf15_instrument=index, tf15_direction=original["direction"],
                        tf15_option_type=original["option_type"],
                        tf15_timeframe=original["timeframe"],
                        replay_grade=original["grade"],
                        replay_confidence=original["confidence"],
                        replay_regime=original["market_regime"],
                        replay_source_lane="UTS", chart_exit_reason=chart_reason,
                        replay_setup=setup,
                    )
                    prior = close_helper(processor, **arguments)
                    final = processor._simulate_actual_option_exit(**arguments)
                    if prior is None or final is None:
                        unavailable.append({"id": identity, "reason": "Helper abstained"})
                        continue
                    legacy_exit = (
                        original["initial_stop"]
                        if prior["reason"] == "SL HIT" else prior["exit"]
                    )
                    baseline = (round(legacy_exit, 2) - round(prior["entry"], 2)) * quantity - 80.
                    prior_charge = estimate_trade_charges(
                        prior["entry"], prior["exit"], quantity, "OPT", settings,
                        instrument=index,
                    )
                    if (abs(baseline - float(original["pnl"])) > .011
                            or abs(prior["entry"] - float(original["entry_price"])) > .001
                            or abs(prior_charge - float(original["charges"])) > .011):
                        unavailable.append({"id": identity, "reason": "Baseline or charges differ"})
                        continue
                    final_charge = estimate_trade_charges(
                        final["entry"], final["exit"], quantity, "OPT", settings,
                        instrument=index,
                    )
                    corrected = (round(prior["exit"], 2) - round(prior["entry"], 2)) * quantity - 80.
                    high_net = (round(final["exit"], 2) - round(final["entry"], 2)) * quantity - 80.
                    rows.append({
                        "id": identity, "index": index, "timeframe": original["timeframe"],
                        "eligible_at": meta["eligible_at"], "input_sha256": digest,
                        "cap_pct": cap, "quantity": quantity,
                        "before_net": round(baseline, 2),
                        "verified_fill_only_net": round(corrected, 2),
                        "after_net": round(high_net, 2),
                        "difference": round(high_net - baseline, 2),
                        "fill_preservation_difference": round(corrected - baseline, 2),
                        "high_peak_difference": round(high_net - corrected, 2),
                        "before_exit": original["exit_reason"],
                        "after_exit": (
                            final["reason"] if final["reason"] != "HISTORICAL"
                            else original["exit_reason"]
                        ),
                        "recorded_exit_price": original["exit_price"],
                        "close_only_verified_exit_price": prior["exit"],
                        "after_exit_price": final["exit"],
                        "after_exit_at": pd.Timestamp(final["exit_time"]).isoformat(),
                        "frozen_estimated_charges": original["charges"],
                        "verified_fill_only_estimated_charges": prior_charge,
                        "after_estimated_charges": final_charge,
                        "verified_fill_only_net_with_estimated_charges": round(
                            (prior["exit"] - prior["entry"]) * quantity - prior_charge, 2
                        ),
                        "after_net_raw_fill_flat_80": round(
                            (final["exit"] - final["entry"]) * quantity - 80., 2
                        ),
                        "after_net_with_estimated_charges": round(
                            (final["exit"] - final["entry"]) * quantity - final_charge, 2
                        ),
                    })
        finally:
            if DatabaseManager._instance:
                DatabaseManager._instance.conn.close()

    result = {
        "captured_date": "2026-09-30", "summary": _summary(rows),
        "scope": (
            "Frozen option opportunities with exact path hashes, contracts, times, "
            "quantities and premium caps. Baseline reproduces legacy SL fill replacement. "
            "Intermediate preserves verified close-only fills. Final also observes "
            "minute highs for UTS premium locks. No new opportunities are generated."
        ),
        "complete": len(rows) == len(frozen), "recorded_cohort_trades": len(frozen),
        "exact_csv_contracts": len(cache),
        "timestamp_dtype_restoration_counts": dict(normalizations),
        "unavailable": unavailable,
        "by_index": {key: _summary([row for row in rows if row["index"] == key])
                     for key in sorted({row["index"] for row in rows})},
        "by_timeframe": {key: _summary([row for row in rows if row["timeframe"] == key])
                         for key in sorted({row["timeframe"] for row in rows})},
        "after_reasons": dict(Counter(row["after_exit"] for row in rows)),
        "round_trip_cost_rs": 80., "fill_price_rounding_decimals": 2,
        "final_pnl_precision": (
            "Final modeled costs use unrounded helper fills and round only net P&L. "
            "Earlier flat-cost stages round fills to reproduce frozen display arithmetic."
        ),
        "cost_model_verification": {
            "frozen_charge_rows_matched": len(rows), "frozen_charge_rows": len(frozen),
            "modeled_per_leg_premium_fraction": .0002, "flat_cost_rs": 80.,
            "auto_calibration_disabled_for_reproduction": True,
            "reason": "Freeze the cost fraction that exactly reproduces recorded charges.",
        },
        "lock_settings": {"uts_lane_lock_arm_r": 1., "uts_lane_lock_giveback_pct": 30.},
        "network_attempts": 0, "production_database_modified": False,
        "runtime_modified": False,
        "source_sha256": {
            path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
            for path in ("engine/signal_processor.py", "engine/uts_lane.py")
        },
        "close_only_helper_sha256": hashlib.sha256(before_source.encode()).hexdigest(),
        "limitations": [
            "Minute OHLC lacks tick order. Existing stops are checked first. New locks "
            "cross at observed close or apply in the next minute.",
            "A frozen opportunity cohort does not simulate changed portfolio overlap, "
            "capital constraints, later admissions or retry opportunities.",
            "Twenty-second buffering and ten-second repaint checks require finer data.",
            "Results do not guarantee profit or executable broker fills.",
        ],
        "rows": rows,
    }
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({"complete": result["complete"], "summary": result["summary"],
                      "unavailable": len(unavailable)}, indent=2))
    return 0 if result["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
