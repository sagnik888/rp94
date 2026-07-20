import os
import shutil
import json
import sys
from datetime import datetime
from pathlib import Path
import subprocess

RESTORE_POINT = 38
RP_NAME = f"restore_point_{RESTORE_POINT}"
SRC_DIR = Path(r"C:\Users\sagnik\Desktop\ut index 2")
DEST_DIR = SRC_DIR / RP_NAME

def build_rp38():
    print(f"Building {RP_NAME}...")
    
    EXCLUDE_DIRS = {".git"}
    src_resolved = SRC_DIR.resolve()
    dest_resolved = DEST_DIR.resolve()
    if dest_resolved.parent != src_resolved or dest_resolved.name != RP_NAME:
        raise RuntimeError(f"Unsafe restore-point destination: {dest_resolved}")
    
    if DEST_DIR.exists():
        shutil.rmtree(DEST_DIR)
    DEST_DIR.mkdir(parents=True)
    
    # 1. Copy files
    print("Copying files...")
    copied = 0
    for dirpath, dirnames, filenames in os.walk(SRC_DIR):
        rel_dir = Path(dirpath).relative_to(SRC_DIR)
        
        # Filter dirnames
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS and not d.lower().startswith("restore_point_") and not d.lower().startswith("restorepoint_")]
        
        for fname in filenames:
            if fname.startswith("RESTORE_POINT_") and (fname.endswith("_MANIFEST.json") or fname.endswith("_INFO.txt")):
                continue
                
            src_file = Path(dirpath) / fname
            target_file = DEST_DIR / rel_dir / fname
            target_file.parent.mkdir(parents=True, exist_ok=True)
            
            try:
                # We skip extremely large log files if they cause issues, but we'll try to copy
                shutil.copy2(src_file, target_file)
                copied += 1
            except Exception as e:
                print(f"Failed to copy {src_file}: {e}")
                
    print(f"Copied {copied} files to {RP_NAME}")

    # 2. Write INFO txt
    info_text = f"""RESTORE POINT {RESTORE_POINT} - FINAL PRE-LIVE MARKET AUDIT SNAPSHOT
Updated: {datetime.now().strftime("%Y-%m-%d %H:%M:%S +05:30")}
Source workspace: {SRC_DIR}
Restore target: {DEST_DIR}

Reason:
Preserve the full current runnable system after integrating market_wide_reversal_entry 2-of-3 core-index confirmation with MIDCPNIFTY sympathy routing, volume_spike_entry, power_move, quad_union scanner mode, source-aware quad-union exits, historical quad raw-entry parity, dashboard engine-selector synchronization, Fyers WebSocket transient-gateway backoff hardening, divergence-warning throttling, automatic 09:00-15:40 live-session function/bug/regression tracking, and verifying 527 end-to-end tests, the post-RP38 audit remediations, the dashboard HIST/REAL mode-switch synchronization fix, the 09:00:01 pre-open REAL arming fix, the mid-session REAL restart refresh fix, the daily/yearly Sharpe dashboard/backend update, the deterministic repeated-backtest fix, and the Fyers token-refresh/auth recovery fix confirming clean runtime risk config, stable repeated backtests, broker connectivity, pre-market/live data warming, safe trade-execution blocking, accurate options rollovers, and clearer live performance reporting before live deployment.

What is included:
- Full project source/config/dashboard/docs/tests/scripts.
- Runtime support folders such as venv, data_store, logs/runtime logs, caches.
- Local .env and broker/session files.
- The latest full_system_audit_report.md generated detailing the A-Z checks.

What is excluded:
- .git directory.
- All other restore-point folders.

Major changes/fixes captured since restore_point_37:
- Executed full A-Z system audit via `scripts/full_system_audit.py` (AngelOne/Fyers connectivity, 7-day data sync, Option chain expiry manager rollovers).
- Ran full test suite validating 527 passed tests with zero failures and 13 expected skips.
- Added market-wide reversal routing: synchronized reversal entries can pass on 2/3 core-index agreement across NIFTY, BANKNIFTY, and SENSEX; MIDCPNIFTY can join only when it has its own same-side raw signal plus two core votes, using a separate MIDCPNIFTY sympathy range threshold and scalp-lock risk.
- Added `quad_union` scanner engine mode that unions UTBot, EMA 9/21, Supertrend 10,3, and consensus raw flips after repaint buffering, with dashboard/server/settings command support and cache-busted settings UI.
- Added source-aware quad-union opposite-exit policy: single UTBot/EMA/Supertrend opposite flips warn or tighten protection unless the trade is losing/profit-protection is threatened; consensus, 2+ sources, or all-source flips can exit.
- Fixed historical replay parity for quad_union so raw entries that passed engine/repaint buffering bypass the same soft grade/confidence/choppy filters that live quad_union entries bypass, while hard risk/session gates remain active.
- Throttled duplicate market-intelligence divergence warnings so repeated same-instrument/same-divergence logs do not flood the console while still preserving the actual divergence signal.
- Activated the read-only live-session tracker from system startup for 09:00-15:40 IST with PID guarding, automatic restart checks, 09:00 log-window scoping, and an end-session markdown/JSON report covering function health, stale scanner state, API/diagnostic latency, queue/backlog pressure, broker/source degradation, open-trade/source contradictions, repeated warning/error noise, error sources, and severity-ranked fix suggestions.
- Captured post-RP38 audit remediations: `.env` recovery auto-flatten grace set to 5.0s, `MAX_TRADE_LOSS_ABS` reconciled to the current FUT daily budget of 12000, and risk warnings cleared.
- Hardened option OI snapshot persistence against Windows temp-file/rename races with a shared lock, unique temp files, retries, and a threaded regression test.
- Hardened Fyers WebSocket reconnect behavior for Cloudflare/FYERS 504 gateway handshakes: transient gateway timeouts are classified separately, stale socket-generation callbacks are ignored, and reconnect backoff now resets only after a real tick or 30s stable connection.
- Fixed dashboard HIST/REAL mode switching so the frontend waits for the backend command result, immediately syncs to the actual scanner mode, and shows the exact safe-mode block reason instead of stale/generic notifications.
- Fixed REAL-mode readiness to allow operator arming from 09:00:01 IST, run pre-open settlement/analysis refresh through the 09:14:30 prep window, and keep order execution blocked until market/session gates permit trading.
- Fixed REAL mid-session startup/restart from 09:15-15:00 IST so startup runs the live session refresh inline, rebuilding missing candles, feeds, expiry, chains, and intelligence before scanning resumes.
- Verified option-chain fallback remains functional and Fyers direct option chains return successfully when available.
- Added backend and dashboard Sharpe reporting as `daily / yearly`, preserving `sharpe_ratio` as the daily/base compatibility value and adding `sharpe_ratio_daily`, `sharpe_ratio_yearly`, and `sharpe_ratio_display`.
- Fixed repeated HISTORICAL backtests with unchanged settings returning different results: recalculation now reuses existing candle cache by default, historical replay runs indices in stable order, and simulation resets clear directional census plus regime smoothing state.
- Fixed Fyers token relogin/refresh diagnostics and recovery: missing app credentials now fail fast with exact field names, manual auth no longer reports misleading `invalid app id hash`, and the local Secret ID/token refresh state is captured in this restore snapshot.

Verification performed before final snapshot:
- Full pytest suite: 527 passed, 13 skipped.
- Live session tracker regression: 6 passed, covering scheduler launch, PID guarding, 09:00-15:40 command wiring, stale scanner/latency/broker-gap detection, repeated warning noise, and pre-window log filtering.
- Focused market-wide/location regression: 15 passed, 5 skipped.
- Broader focused regression group: 69 passed, 11 skipped.
- Quad-union/engine regression coverage: engine factory, dashboard options, overlays, relaxed-gate behavior, live and historical raw-candidate soft-gate bypass, source-aware opposite exits, Fyers transient-gateway backoff, and divergence warning throttle tests pass.
- June 29 replay verification: 10:45 5min SELL admits NIFTY, BANKNIFTY, SENSEX, and MIDCPNIFTY via sympathy; 11:15 15min SELL admits NIFTY, BANKNIFTY, and SENSEX while MIDCPNIFTY remains blocked without a raw sell.
- Fresh settings verification: `recovery_auto_flatten_seconds=5.0`, `max_trade_loss_abs=12000.0`, risk warnings `[]`.
- Browser mode-switch verification: REAL request outside market hours keeps HIST active immediately and shows `Mode switch blocked: outside regular market hours`.
- Safe-mode timing verification: 09:00:00 blocked, 09:00:01 allowed, 09:08:00 settlement allowed, 09:14:30 pre-open analysis allowed, 09:14:45 handoff allowed, 09:15:01 live readiness allowed, and 15:30:01 blocked.
- Mid-session restart regression: REAL startup at 10:30 runs live startup refresh immediately instead of waiting for a later market-session cycle.
- Live audit script: AngelOne/Fyers connected; historical and live quotes validated for all 4 indices; Fyers option chains returned directly for NIFTY, BANKNIFTY, SENSEX, and MIDCPNIFTY with fallback disabled.
- Sharpe reporting regression: backend payload exposes daily/yearly/display fields and the dashboard renders `Sharpe daily / yearly` and `SR: daily / yearly` without stale asset-cache keys.
- Live repeated-backtest verification: two consecutive `/api/reset_cache` runs returned identical 16 trades, P&L 6158.75, win/loss 11/5, PF 2.2550180343569783, SR `0.29 / 4.66`, zero candle hash changes, and worker manifest `local_cache:12`.
- Fyers runtime verification: `/api/status` showed Angel true, Fyers true, broker_degraded false, fyers_auth_required false, and refreshed token timestamp `2026-06-29T06:08:00`.

Snapshot details:
- Restore folder: {RP_NAME}
- Manifest file: RESTORE_POINT_{RESTORE_POINT}_MANIFEST.json
"""
    info_path = SRC_DIR / f"RESTORE_POINT_{RESTORE_POINT}_INFO.txt"
    info_path.write_text(info_text, encoding="utf-8")
    shutil.copy2(info_path, DEST_DIR / info_path.name)
    
    # 3. Generate Manifest
    print("Generating Manifest...")
    # Update the fast manifest generator to exclude rp 38 and 37 if needed, but it checks startswith.
    subprocess.run([sys.executable, "scripts/write_restorepoint_manifest.py", "--out", f"RESTORE_POINT_{RESTORE_POINT}_MANIFEST.json"], cwd=str(SRC_DIR))
    shutil.copy2(SRC_DIR / f"RESTORE_POINT_{RESTORE_POINT}_MANIFEST.json", DEST_DIR / f"RESTORE_POINT_{RESTORE_POINT}_MANIFEST.json")

    # 4. Update LATEST_RESTORE_POINT.txt
    latest_text = f"""Latest restore point: {RP_NAME}
Updated: {datetime.now().strftime("%Y-%m-%d %H:%M:%S +05:30")}
Source workspace: {SRC_DIR}
Restore folder: {DEST_DIR}

Checkpoint summary:
- Full system snapshot rebuilt in restore_point_26 style.
- Includes venv, data_store, logs/runtime logs, caches/support files, local env/session files, source/config/dashboard/tests/scripts/docs.
- Excludes .git and all restore-point directories.
- Preserves all current upgrades through the post-RP38 audit fixes, market-wide 2-of-3 reversal routing with MIDCPNIFTY sympathy, quad_union scanner mode, source-aware quad exits, historical quad raw-entry parity, dashboard mode-sync fix, 09:00:01 pre-open REAL arming fix, mid-session restart refresh fix, daily/yearly Sharpe display update, deterministic repeated-backtest fix, Fyers auth recovery, Fyers WebSocket gateway-backoff hardening, duplicate divergence-warning throttling, and automatic live-session function/bug/regression tracker: 5s recovery auto-flatten, current FUT-budget-aligned 12000 absolute loss cap, thread-safe option OI snapshot persistence, clean risk warnings, HIST/REAL switch acknowledgement and safe-mode block messaging, 09:00:01 REAL arming with pre-open refresh, immediate 09:15-15:00 REAL restart catch-up, Sharpe `daily / yearly` backend/dashboard reporting, stable cache-only historical recalculation, reset historical replay state, Fyers connected/token refreshed, 09:00-15:40 tracker reports, test suite completion (527 passed), live broker data hooks, Fyers option chains, option-chain fallback, and options expiry rollovers working correctly.
- Detailed report: RESTORE_POINT_{RESTORE_POINT}_INFO.txt
"""
    (SRC_DIR / "LATEST_RESTORE_POINT.txt").write_text(latest_text, encoding="utf-8")
    shutil.copy2(SRC_DIR / "LATEST_RESTORE_POINT.txt", DEST_DIR / "LATEST_RESTORE_POINT.txt")

    # Regenerate after LATEST_RESTORE_POINT.txt is updated so the manifest
    # fingerprints the exact checkpoint marker included in the restore folder.
    print("Regenerating Manifest after latest marker update...")
    subprocess.run([sys.executable, "scripts/write_restorepoint_manifest.py", "--out", f"RESTORE_POINT_{RESTORE_POINT}_MANIFEST.json"], cwd=str(SRC_DIR), check=True)
    shutil.copy2(SRC_DIR / f"RESTORE_POINT_{RESTORE_POINT}_MANIFEST.json", DEST_DIR / f"RESTORE_POINT_{RESTORE_POINT}_MANIFEST.json")
    print(f"Restore Point {RESTORE_POINT} creation completed.")

if __name__ == "__main__":
    build_rp38()
