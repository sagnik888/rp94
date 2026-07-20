import os
import shutil
import json
from datetime import datetime
from pathlib import Path
import subprocess

RESTORE_POINT = 37
RP_NAME = f"restore_point_{RESTORE_POINT}"
SRC_DIR = Path(r"C:\Users\sagnik\Desktop\ut index 2")
DEST_DIR = SRC_DIR / RP_NAME

def build_rp37():
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
Preserve the full current runnable system after the final end-to-end A-Z system audit, the post-RP37 audit remediations, the dashboard HIST/REAL mode-switch synchronization fix, the 09:00:01 pre-open REAL arming fix, the mid-session REAL restart refresh fix, the daily/yearly Sharpe dashboard/backend update, the deterministic repeated-backtest fix, and the Fyers token-refresh/auth recovery fix confirming clean runtime risk config, stable repeated backtests, broker connectivity, pre-market/live data warming, safe trade-execution blocking, accurate options rollovers, and clearer live performance reporting before live deployment.

What is included:
- Full project source/config/dashboard/docs/tests/scripts.
- Runtime support folders such as venv, data_store, logs/runtime logs, caches.
- Local .env and broker/session files.
- The latest full_system_audit_report.md generated detailing the A-Z checks.

What is excluded:
- .git directory.
- All other restore-point folders.

Major changes/fixes captured since restore_point_36:
- Executed full A-Z system audit via `scripts/full_system_audit.py` (AngelOne/Fyers connectivity, 7-day data sync, Option chain expiry manager rollovers).
- Ran full test suite validating 468 passed tests with zero failures and 13 expected skips.
- Captured post-RP37 audit remediations: `.env` recovery auto-flatten grace set to 5.0s, `MAX_TRADE_LOSS_ABS` reconciled to the current FUT daily budget of 12000, and risk warnings cleared.
- Hardened option OI snapshot persistence against Windows temp-file/rename races with a shared lock, unique temp files, retries, and a threaded regression test.
- Fixed dashboard HIST/REAL mode switching so the frontend waits for the backend command result, immediately syncs to the actual scanner mode, and shows the exact safe-mode block reason instead of stale/generic notifications.
- Fixed REAL-mode readiness to allow operator arming from 09:00:01 IST, run pre-open settlement/analysis refresh through the 09:14:30 prep window, and keep order execution blocked until market/session gates permit trading.
- Fixed REAL mid-session startup/restart from 09:15-15:00 IST so startup runs the live session refresh inline, rebuilding missing candles, feeds, expiry, chains, and intelligence before scanning resumes.
- Verified option-chain fallback remains functional and Fyers direct option chains return successfully when available.
- Added backend and dashboard Sharpe reporting as `daily / yearly`, preserving `sharpe_ratio` as the daily/base compatibility value and adding `sharpe_ratio_daily`, `sharpe_ratio_yearly`, and `sharpe_ratio_display`.
- Fixed repeated HISTORICAL backtests with unchanged settings returning different results: recalculation now reuses existing candle cache by default, historical replay runs indices in stable order, and simulation resets clear directional census plus regime smoothing state.
- Fixed Fyers token relogin/refresh diagnostics and recovery: missing app credentials now fail fast with exact field names, manual auth no longer reports misleading `invalid app id hash`, and the local Secret ID/token refresh state is captured in this restore snapshot.

Verification performed before final snapshot:
- Full pytest suite: 474 passed, 13 skipped.
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
    # Update the fast manifest generator to exclude rp 37 and 36 if needed, but it checks startswith.
    subprocess.run(["python", "scripts/write_restorepoint_manifest.py", "--out", f"RESTORE_POINT_{RESTORE_POINT}_MANIFEST.json"], cwd=str(SRC_DIR))
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
- Preserves all current upgrades through the post-RP37 audit fixes, dashboard mode-sync fix, 09:00:01 pre-open REAL arming fix, mid-session restart refresh fix, daily/yearly Sharpe display update, deterministic repeated-backtest fix, and Fyers auth recovery: 5s recovery auto-flatten, current FUT-budget-aligned 12000 absolute loss cap, thread-safe option OI snapshot persistence, clean risk warnings, HIST/REAL switch acknowledgement and safe-mode block messaging, 09:00:01 REAL arming with pre-open refresh, immediate 09:15-15:00 REAL restart catch-up, Sharpe `daily / yearly` backend/dashboard reporting, stable cache-only historical recalculation, reset historical replay state, Fyers connected/token refreshed, test suite completion (474 passed), live broker data hooks, Fyers option chains, option-chain fallback, and options expiry rollovers working correctly.
- Detailed report: RESTORE_POINT_{RESTORE_POINT}_INFO.txt
"""
    (SRC_DIR / "LATEST_RESTORE_POINT.txt").write_text(latest_text, encoding="utf-8")
    shutil.copy2(SRC_DIR / "LATEST_RESTORE_POINT.txt", DEST_DIR / "LATEST_RESTORE_POINT.txt")

    # Regenerate after LATEST_RESTORE_POINT.txt is updated so the manifest
    # fingerprints the exact checkpoint marker included in the restore folder.
    print("Regenerating Manifest after latest marker update...")
    subprocess.run(["python", "scripts/write_restorepoint_manifest.py", "--out", f"RESTORE_POINT_{RESTORE_POINT}_MANIFEST.json"], cwd=str(SRC_DIR), check=True)
    shutil.copy2(SRC_DIR / f"RESTORE_POINT_{RESTORE_POINT}_MANIFEST.json", DEST_DIR / f"RESTORE_POINT_{RESTORE_POINT}_MANIFEST.json")
    print(f"Restore Point {RESTORE_POINT} creation completed.")

if __name__ == "__main__":
    build_rp37()
