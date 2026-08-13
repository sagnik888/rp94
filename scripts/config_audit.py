"""What the running system is actually configured to do, versus what the code says.

Two silent failure modes this makes visible.

1. DIVERGENCE. A .env value overrides a code default and nothing announces it. On
   2026-08-13, 50 of 96 .env keys diverged. Most are legitimate — credentials,
   ports, capital, trading mode. A handful change trading behaviour, and the only
   reason one of them surfaced at all is that a test happened to read the live
   settings singleton and failed.

2. COLLAPSE. Some settings are only meaningful relative to each other. Set equal,
   the distinction they encode disappears without any single value looking wrong:

       power_move_range_atr_mult        2.0   ordinary range expansion, a GATE
       power_move_spike_range_atr_mult  2.0   explosive spike, WAIVES 15m alignment

   Both plausible in isolation; together the 15-minute alignment requirement
   became unreachable for power moves. That pair is now derived in code
   (power_move_spike_threshold), and the remaining ordered pairs are checked here.

Usage:
    python scripts/config_audit.py            # behaviour-relevant divergence + collapse
    python scripts/config_audit.py --all      # include credentials, ports, capital
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Dict, List, Tuple

# Keys whose divergence is expected operator setup, not a behaviour surprise.
_INFRA_HINTS = (
    "api_key", "api_secret", "secret", "password", "totp", "client_id", "user_id",
    "redirect_uri", "app_id", "port", "dir", "path", "url", "token",
    "capital", "trading_mode", "backtest_days", "dashboard",
)

#: Pairs where the SECOND must stay strictly above the FIRST, with the meaning that
#: is lost when they meet. Add to this list, never silently reorder it.
_ORDERED_PAIRS: Tuple[Tuple[str, str, str], ...] = (
    (
        "power_move_range_atr_mult",
        "power_move_spike_range_atr_mult",
        "explosive spike must be a higher bar than range expansion, else the 15m "
        "alignment waiver applies to every power move",
    ),
    (
        "profit_lock_min_ratio",
        "profit_lock_max_ratio",
        "the lock ladder's floor must sit below its ceiling or the band inverts",
    ),
)

#: Pairs whose ordering the CODE now derives rather than trusting. The raw
#: settings may still disagree — the divergence table above shows that — but the
#: value actually used at runtime is corrected, so these are not live collapses.
_DERIVED: Dict[str, str] = {
    "power_move_spike_range_atr_mult": "engine.signal_quality_policy.power_move_spike_threshold",
}


def _is_infra(key: str) -> bool:
    return any(hint in key for hint in _INFRA_HINTS)


def read_env(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip().lower()] = value.strip().strip("'").strip('"')
    return out


def _matches(value: str, default: Any) -> bool:
    try:
        if isinstance(default, bool):
            return (value.lower() in ("true", "1", "yes", "on")) == default
        if isinstance(default, (int, float)) and default is not None:
            return abs(float(value) - float(default)) < 1e-9
        return str(value) == str(default)
    except (TypeError, ValueError):
        return False


def divergences(env: Dict[str, str], fields) -> List[Tuple[str, str, Any]]:
    rows = []
    for key, value in sorted(env.items()):
        if key not in fields:
            continue
        default = fields[key].default
        if not _matches(value, default):
            rows.append((key, value, default))
    return rows


def effective(settings, name: str) -> float:
    """The value the system actually uses, not the raw setting.

    A pair the code derives cannot collapse at runtime however the .env is set,
    and reporting the raw number as a live collapse would be false.
    """
    if name == "power_move_spike_range_atr_mult":
        from engine.signal_quality_policy import power_move_spike_threshold

        return float(power_move_spike_threshold(settings))
    return float(getattr(settings, name))


def collapses(settings) -> List[Tuple[str, str, float, float, str]]:
    """Ordered pairs the live configuration does not honour, after derivation."""
    found = []
    for lower, upper, why in _ORDERED_PAIRS:
        try:
            lo = effective(settings, lower)
            hi = effective(settings, upper)
        except (TypeError, ValueError, AttributeError):
            continue
        if hi <= lo:
            found.append((lower, upper, lo, hi, why))
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true", help="include infrastructure keys")
    parser.add_argument("--env", default=".env")
    args = parser.parse_args()

    from config.settings import Settings, get_settings

    env = read_env(Path(args.env))
    rows = divergences(env, Settings.model_fields)
    behaviour = [r for r in rows if not _is_infra(r[0])]
    shown = rows if args.all else behaviour

    print(f"config audit — {len(env)} .env keys, {len(rows)} diverge from code default")
    print(f"  {len(behaviour)} of those change trading behaviour\n")
    print(f"  {'setting':52} {'live':<14} code")
    for key, value, default in shown:
        print(f"  {key:52} {value:<14} {default}")

    settings = get_settings()
    bad = collapses(settings)
    print()
    for raw, helper in _DERIVED.items():
        try:
            live = float(getattr(settings, raw)); used = effective(settings, raw)
        except Exception:
            continue
        if abs(live - used) > 1e-9:
            print(f"  note: {raw} is set to {live} but {used} is used — derived by {helper}")
    if bad:
        print("COLLAPSED ORDERED PAIRS — a distinction the code relies on is gone:")
        for lower, upper, lo, hi, why in bad:
            print(f"  {upper} ({hi}) is not above {lower} ({lo})")
            print(f"      {why}")
    else:
        print("ordered pairs: all intact")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
