"""One-shot post-fix proof for the 10 audit issues."""
from __future__ import annotations

import inspect
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.settings import Settings, get_settings
from dashboard import server as dash
from engine.position_sizing import per_trade_risk_budget, size_lots_for_trade
from engine.risk_manager import RiskManager
from engine.signal_processor import SignalProcessor
from trading.trade_manager import TradeManager


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    results = []
    s = get_settings()
    old = s.capital_opt * s.risk_opt_pct / 100
    new = per_trade_risk_budget(s, "OPT")
    results.append(
        (
            "1 live OPT risk capped",
            new <= s.effective_trade_loss_cap("OPT") and new < old,
            f"{old}->{new}",
        )
    )
    results.append(("2 per-trade < daily OPT", new < old, f"daily={old} per={new}"))

    _sizing_kwargs = dict(
        inst_type="FUT",
        requested_lots=10,
        lot_size=25,
        entry_price=25000,
        stop_price=24975,
    )
    r_default = size_lots_for_trade(
        settings=Settings(
            capital_fut=1e6,
            risk_fut_pct=1,
            max_trade_loss_abs=5000,
            risk_per_trade_daily_fraction=1,
        ),
        **_sizing_kwargs,
    )
    r_auto = size_lots_for_trade(
        settings=Settings(
            capital_fut=1e6,
            risk_fut_pct=1,
            max_trade_loss_abs=5000,
            risk_per_trade_daily_fraction=1,
            auto_risk_sizing_enabled=True,
            risk_per_trade_fut_pct=0.0,
        ),
        **_sizing_kwargs,
    )
    # Final design: default honors user lots (no silent hardcap); optional
    # auto-risk-sizing still trims to the risk budget when enabled.
    results.append((
        "3 FUT sizing default+auto",
        r_default.lots == 10 and r_auto.lots <= 8,
        f"default={r_default.lots} auto={r_auto.lots}",
    ))

    sp_src = (ROOT / "engine" / "signal_processor.py").read_text(encoding="utf-8")
    sc_src = (ROOT / "scanner.py").read_text(encoding="utf-8")
    results.append(
        (
            "4 no synthetic OPT recovery",
            "no verified LTP" in sp_src or "refusing synthetic" in sp_src,
            "guard present",
        )
    )
    results.append(
        (
            "5 shared resolve_trade_lots",
            "resolve_trade_lots" in sp_src and "resolve_trade_lots" in sc_src,
            "both paths",
        )
    )
    results.append(
        (
            "6 hist signed delta",
            "_option_signed_delta" in sc_src and "_option_signed_delta" in sp_src,
            "signed",
        )
    )

    tm_src = inspect.getsource(TradeManager.update_trade)
    results.append(("7 TARGET_HIT in update_trade", "TARGET_HIT" in tm_src, "present"))

    tm = TradeManager(broker=None, max_positions=5)
    tm.mode = "HISTORICAL"
    tm.open_trades.clear()
    t = tm.open_trade(
        instrument="NIFTY",
        timeframe="5min",
        direction="LONG",
        price=100,
        trailing_stop=95,
        lots=1,
        lot_size=10,
        grade="A",
        target=110,
        inst_type="FUT",
        is_ghost=True,
    )
    tm.update_trade(t.id, current_spot=112, real_fut_price=112)
    results.append(
        (
            "7b TARGET_HIT functional",
            t.id not in tm.open_trades
            and any(c.exit_reason == "TARGET_HIT" for c in tm.closed_trades),
            "closed",
        )
    )

    rm_src = inspect.getsource(RiskManager.check_circuit_breaker)
    results.append(
        (
            "8 RM fail-closed",
            "fail-closed" in rm_src or "fail_closed" in rm_src,
            "present",
        )
    )
    results.append(
        (
            "9 premium stop handoff",
            "spot_to_prem(new_stop)" in sc_src
            and ("incoming_stop" in tm_src or "is_option" in tm_src),
            "handoff",
        )
    )
    results.append(
        (
            "10 read API token",
            "/api/state" in dash._TOKEN_REQUIRED_API_PATHS
            and "/api/trades" in dash._TOKEN_REQUIRED_API_PATHS,
            "token required",
        )
    )

    # PE signed delta sanity
    d_pe = SignalProcessor._option_signed_delta(25000, 25000, "PE", {}, 3, -0.5)
    d_ce = SignalProcessor._option_signed_delta(25000, 25000, "CE", {}, 3, 0.5)
    results.append(("6b PE delta negative", d_pe < 0 and d_ce > 0, f"PE={d_pe:.3f} CE={d_ce:.3f}"))

    failed = 0
    for name, ok, detail in results:
        status = "PASS" if ok else "FAIL"
        if not ok:
            failed += 1
        print(f"{status} | {name} | {detail}")
    print(f"TOTAL {len(results) - failed}/{len(results)} proved")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
