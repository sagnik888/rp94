"""
UT1 Index Trading System — Main Entry Point
═══════════════════════════════════════════════════════════════
Run: python main.py
Dashboard: http://localhost:8001
"""

import sys
import asyncio
import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime, time as dt_time, timedelta
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

import uvicorn
from loguru import logger

from config.settings import get_settings
from data.market_data import MarketDataProvider
from data.candle_builder import CandleBuilder
from engine.multi_timeframe import MultiTimeframeEngine
from engine.signal_manager import SignalManager
from intelligence.intelligence_aggregator import IntelligenceAggregator
from trading.trade_manager import TradeManager, IST
from trading.performance_tracker import PerformanceTracker
from trading.broker import SignalSimBroker
from scanner import Scanner
from notifications import send_desktop_notification
from dashboard.server import app, broadcast
from engine.safe_mode import validate_real_mode_readiness
import dashboard.server as dashboard_module

# Configure logging
app_log_level = os.getenv("LOG_LEVEL", "INFO").upper()
file_log_level = os.getenv("LOG_FILE_LEVEL", app_log_level).upper()
_TRUTHY_ENV = {"1", "true", "yes", "on"}
_FALSEY_ENV = {"0", "false", "no", "off"}
RUNNING_UNDER_PYTEST = "pytest" in sys.modules or "PYTEST_CURRENT_TEST" in os.environ
FILE_LOG_ENABLED = (
    os.getenv("UT1_DISABLE_FILE_LOG", "").strip().lower() not in _TRUTHY_ENV
    and (
        not RUNNING_UNDER_PYTEST
        or os.getenv("UT1_ENABLE_TEST_FILE_LOGS", "").strip().lower() in _TRUTHY_ENV
    )
)
# The launcher tees STDERR to a file so a MemoryError traceback or a Windows fatal
# message outlives the console window. That redirect also swallowed every log line:
# the console went silent right after "Starting Backend Server" while the system was
# in fact scanning normally. Console logging goes to STDOUT, so the operator sees it;
# raw interpreter stderr still lands in the file for the post-mortem.
for _console_stream in (sys.stdout, sys.stderr):
    try:
        _console_stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    except Exception:
        # Not a real console (pytest capture, pipe wrapper). Nothing to widen.
        pass
logger.remove()
logger.add(
    sys.stdout,
    format="<green>{time:HH:mm:ss}</green> | <level>{level:<7}</level> | <level>{message}</level>",
    level=app_log_level,
)
if FILE_LOG_ENABLED:
    logger.add(
        os.getenv("UT1_LOG_FILE", "logs/ut1.log"),
        rotation="00:00",
        retention="3 months",
        level=file_log_level,
    )

def start_angelone_log_cleaner():
    def _cleaner_loop():
        import glob
        import zipfile
        import shutil
        import datetime
        # Folders that failed removal because a file inside is still open
        # (e.g. the AngelOne SDK logger keeps yesterday's app.log handle until
        # restart). Warn ONCE per folder, then skip quietly each hour instead
        # of raising WinError 32 noise all night; retried after next restart.
        locked_skip: set = set()
        while True:
            try:
                now = datetime.datetime.now(IST)
                # Do not run heavy zip operations during active market hours (09:00 to 15:45)
                market_start = datetime.time(9, 0)
                market_end = datetime.time(15, 45)

                if not (market_start <= now.time() <= market_end):
                    today_str = now.strftime("%Y-%m-%d")
                    logs_dir = os.path.join(os.getcwd(), "logs")
                    for folder_path in glob.glob(os.path.join(logs_dir, "202*")):
                        if os.path.isdir(folder_path):
                            folder_name = os.path.basename(folder_path)
                            # Ensure it's a valid date folder and older than today
                            if len(folder_name) == 10 and folder_name < today_str:
                                if folder_path in locked_skip:
                                    continue
                                try:
                                    app_log_path = os.path.join(folder_path, "app.log")
                                    if os.path.exists(app_log_path):
                                        zip_name = f"smartconnect_{folder_name}.log.zip"
                                        zip_path = os.path.join(logs_dir, zip_name)
                                        # Run compression in a separate process to avoid GIL-locking Uvicorn for minutes
                                        import subprocess
                                        import sys
                                        zip_script = f"import zipfile\nwith zipfile.ZipFile(r'{zip_path}', 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as zipf:\n    zipf.write(r'{app_log_path}', arcname=r'app_{folder_name}.log')"
                                        subprocess.run([sys.executable, "-c", zip_script], check=True)
                                    shutil.rmtree(folder_path)
                                    logger.info(f"Compressed legacy AngelOne log folder: {folder_name}")
                                except subprocess.CalledProcessError as sub_err:
                                    logger.warning(f"Failed to compress log folder {folder_name}: {sub_err}")
                                except (PermissionError, OSError) as lock_err:
                                    locked_skip.add(folder_path)
                                    logger.warning(
                                        f"Log folder {folder_name} is still held open "
                                        f"(likely the SDK logger); skipping until next restart: {lock_err}"
                                    )

                    # Database Expiry Pruning
                    import calendar
                    from data.sqlite_db import DatabaseManager
                    
                    def get_last_thursday(year, month):
                        month_cal = calendar.monthcalendar(year, month)
                        if month_cal[-1][calendar.THURSDAY] != 0:
                            return datetime.date(year, month, month_cal[-1][calendar.THURSDAY])
                        else:
                            return datetime.date(year, month, month_cal[-2][calendar.THURSDAY])
                            
                    last_thursday = get_last_thursday(now.year, now.month)
                    
                    if now.date() >= last_thursday:
                        cleanup_marker = os.path.join(logs_dir, ".last_db_cleanup")
                        current_month_str = now.strftime("%Y-%m")
                        
                        last_cleaned_month = ""
                        if os.path.exists(cleanup_marker):
                            with open(cleanup_marker, "r") as f:
                                last_cleaned_month = f.read().strip()
                                
                        if last_cleaned_month != current_month_str:
                            logger.info(f"Triggering automated Expiry-Based DB Pruning for {current_month_str}...")
                            try:
                                db = DatabaseManager()
                                # Gate the marker on the prune actually succeeding.
                                # cleanup_old_records swallows its own exceptions, so
                                # writing the marker on a normal return marked the month
                                # "done" after a VACUUM that failed on a locked database,
                                # and every remaining hourly tick that month then skipped.
                                if db.cleanup_old_records(days=31):
                                    with open(cleanup_marker, "w") as f:
                                        f.write(current_month_str)
                                else:
                                    logger.warning(
                                        "Monthly DB prune did not complete; leaving the "
                                        "marker unset so the next hourly tick retries."
                                    )
                            except Exception as db_err:
                                logger.error(f"Failed DB Pruning: {db_err}")

            except Exception as e:
                logger.error(f"Error in AngelOne log cleaner: {e}")
            time.sleep(3600) # Check every 1 hour

    threading.Thread(target=_cleaner_loop, daemon=True, name="LogCleaner").start()


def _parse_hhmm(value: str, default: dt_time) -> dt_time:
    try:
        parts = str(value or "").strip().split(":")
        if len(parts) < 2:
            return default
        return dt_time(int(parts[0]), int(parts[1]))
    except Exception:
        return default


def _parse_hhmmss(value: str, default: dt_time) -> dt_time:
    try:
        parts = str(value or "").strip().split(":")
        if len(parts) < 2:
            return default
        hour = int(parts[0])
        minute = int(parts[1])
        second = int(parts[2]) if len(parts) >= 3 else 0
        return dt_time(hour, minute, second)
    except Exception:
        return default


def _setting_enabled(value, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _FALSEY_ENV:
        return False
    if text in _TRUTHY_ENV:
        return True
    return default


def _ist_at(session_date, clock: dt_time) -> datetime:
    return IST.localize(datetime.combine(session_date, clock))


def _pid_is_running(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            process_query_limited_information = 0x1000
            process = ctypes.windll.kernel32.OpenProcess(
                process_query_limited_information, False, int(pid)
            )
            if not process:
                return False
            ctypes.windll.kernel32.CloseHandle(process)
            return True
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _start_tracker_process(
    *,
    since: str,
    until: str,
    interval: int,
    out_dir: Path,
    pid_file: Path,
) -> subprocess.Popen | None:
    try:
        if pid_file.exists():
            try:
                existing_pid = int(pid_file.read_text(encoding="utf-8").strip() or "0")
            except Exception:
                existing_pid = 0
            if _pid_is_running(existing_pid):
                logger.info(f"Live session tracker already running with PID {existing_pid}.")
                return None
            pid_file.unlink(missing_ok=True)

        out_dir.mkdir(parents=True, exist_ok=True)
        log_path = out_dir / "tracker_launcher.log"
        log_handle = log_path.open("a", encoding="utf-8")
        cmd = [
            sys.executable,
            str(Path(__file__).parent / "scripts" / "live_session_tracker.py"),
            "--watch",
            "--since",
            since,
            "--until",
            until,
            "--interval",
            str(max(15, int(interval or 60))),
        ]
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen(
            cmd,
            cwd=str(Path(__file__).parent),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        pid_file.write_text(str(proc.pid), encoding="utf-8")
        logger.info(
            f"Live session tracker started PID {proc.pid}; "
            f"report since {since} until {until} IST; log={log_path}"
        )
        return proc
    except Exception as exc:
        logger.error(f"Failed to start live session tracker: {exc}")
        return None


def start_live_session_tracker_scheduler():
    """Start the read-only live-session bug/regression tracker for market hours.

    Defaults to 09:00-15:40 IST. The child tracker writes rolling JSON/markdown
    reports under runtime_logs/session_tracker and never touches order state.
    """
    enabled = str(os.getenv("LIVE_SESSION_TRACKER_ENABLED", "1")).strip().lower()
    if enabled in {"0", "false", "no", "off"}:
        logger.info("Live session tracker scheduler disabled by LIVE_SESSION_TRACKER_ENABLED.")
        return

    start_at = _parse_hhmm(os.getenv("LIVE_SESSION_TRACKER_START", "09:00"), dt_time(9, 0))
    stop_at = _parse_hhmm(os.getenv("LIVE_SESSION_TRACKER_UNTIL", "15:40"), dt_time(15, 40))
    interval = int(os.getenv("LIVE_SESSION_TRACKER_INTERVAL", "60") or "60")
    root = Path(__file__).parent
    out_dir = root / "runtime_logs" / "session_tracker"
    pid_file = out_dir / "tracker.pid"

    def _scheduler_loop():
        while True:
            now = datetime.now(IST)
            today_start = IST.localize(datetime.combine(now.date(), start_at))
            today_stop = IST.localize(datetime.combine(now.date(), stop_at))
            if start_at <= now.time() <= stop_at:
                _start_tracker_process(
                    since=start_at.strftime("%H:%M"),
                    until=stop_at.strftime("%H:%M"),
                    interval=interval,
                    out_dir=out_dir,
                    pid_file=pid_file,
                )
                sleep_seconds = 60.0
            elif now.time() < start_at:
                sleep_seconds = max(5.0, (today_start - now).total_seconds())
            else:
                tomorrow_start = today_start + timedelta(days=1)
                sleep_seconds = max(60.0, (tomorrow_start - now).total_seconds())

            logger.info(
                f"Live session tracker scheduler sleeping {sleep_seconds:.0f}s "
                f"(window {start_at.strftime('%H:%M')}-{stop_at.strftime('%H:%M')} IST)."
            )
            time.sleep(sleep_seconds)

    threading.Thread(
        target=_scheduler_loop,
        daemon=True,
        name="LiveSessionTrackerScheduler",
    ).start()


def _scanner_liveness_alert_due(
    running: bool, seen_running: bool, elapsed: float, grace: float
) -> bool:
    """Whether a not-running scanner should be escalated.

    Split out of the watchdog loop so it can be tested directly: the loop sleeps a
    floor of 15s between iterations, so a test can never observe a SECOND poll inside
    a reasonable window, and the interesting cases here are all about the second poll
    onwards.

    Silent only while the scanner has never once been seen up and the startup grace is
    still open. A scanner that dies after coming up is reported immediately, which is
    the incident the watchdog exists for.
    """
    if running:
        return False
    return bool(seen_running or elapsed >= grace)


def _apply_runtime_resource_guard(scanner) -> tuple[bool, str]:
    """Latch an entry-only inhibition without changing Auto exit semantics."""
    if (
        str(getattr(scanner, "mode", "") or "").upper() != "REAL"
        or not bool(getattr(scanner, "auto_mode", False))
    ):
        scanner._auto_resource_entry_inhibited = False
        scanner._auto_resource_entry_inhibit_reason = ""
        return False, ""
    try:
        blockers = list(scanner._auto_execution_resource_blockers() or [])
    except Exception as exc:
        blockers = [
            f"Auto resource guard failed to obtain a fresh sample ({exc}); "
            "new exposure is fail-closed"
        ]
    if not blockers:
        scanner._auto_resource_entry_inhibited = False
        scanner._auto_resource_entry_inhibit_reason = ""
        return False, ""

    scanner._auto_resource_entry_inhibited = True
    scanner._auto_resource_entry_inhibit_reason = str(blockers[0])
    return True, "New Auto entries inhibited; " + str(blockers[0])


def start_runtime_watchdog(scanner: Scanner, settings) -> None:
    """Alert on the silent failure modes that unattended operation cannot tolerate.

    Three real incidents motivated this:
      * the scanner thread died while uvicorn kept serving, so the process looked alive
        for hours with nothing scanning during market hours;
      * Auto Mode is not persisted, so a restart silently reverts to manual and NOTHING
        executes even though signals keep being produced;
      * the Fyers access token expires ~06:00 daily and cannot auto-refresh (broker
        returns code -16, "disabled to comply with SEBI regulations"), so the option
        chain quietly disappears and OI/PCR/Greeks go blank.

    Most checks are detection-only. Critical host memory/commit pressure is the narrow
    exception: it inhibits new Auto exposure while deliberately preserving the Auto
    flag used by software stops, targets, profit locks, and reconciliation.
    """
    if str(os.getenv("RUNTIME_WATCHDOG_ENABLED", "1")).strip().lower() in _FALSEY_ENV:
        logger.info("Runtime watchdog disabled by RUNTIME_WATCHDOG_ENABLED.")
        return

    stale_after = float(os.getenv("RUNTIME_WATCHDOG_STALE_SECONDS", "180") or 180.0)
    check_every = float(os.getenv("RUNTIME_WATCHDOG_INTERVAL_SECONDS", "60") or 60.0)
    start_at = _parse_hhmm(os.getenv("RUNTIME_WATCHDOG_START", "09:15"), dt_time(9, 15))
    stop_at = _parse_hhmm(os.getenv("RUNTIME_WATCHDOG_UNTIL", "15:30"), dt_time(15, 30))

    def _alert(key: str, message: str, state: dict) -> None:
        """CRITICAL + desktop notification, de-duplicated per incident."""
        if state.get(key):
            return
        state[key] = True
        logger.critical(f"WATCHDOG: {message}")
        try:
            scanner.log_event(f"WATCHDOG: {message}", "error")
        except Exception:
            pass
        try:
            send_desktop_notification(f"UT1 WATCHDOG: {message}", "error")
        except Exception as exc:
            logger.debug(f"Watchdog desktop alert unavailable: {exc}")

    # The scanner thread does not set is_running until it has finished coming up, and
    # this loop runs its first check immediately. Restarting inside market hours
    # therefore fired "scanner is NOT running" every single time — observed on
    # 2026-08-07 at 10:02:32.033, the same MILLISECOND the scanner logged that it had
    # started. A CRITICAL plus a desktop alert on every routine restart is how an
    # operator learns to ignore CRITICALs, which is the opposite of what a watchdog
    # covering three real silent-failure incidents is for.
    #
    # A scanner that is genuinely dead on arrival is still caught: the grace only
    # defers the check, it never cancels it.
    startup_grace = float(
        os.getenv("RUNTIME_WATCHDOG_STARTUP_GRACE_SECONDS", "120") or 120.0
    )

    def _loop():
        state: dict = {}
        auto_mode_seen_on = False
        watchdog_started = time.monotonic()
        scanner_seen_running = False
        while True:
            try:
                now = datetime.now(IST)
                in_session = (
                    now.weekday() < 5 and start_at <= now.time() <= stop_at
                )

                # 1. Scanner liveness — the dashboard staying up is NOT proof of life.
                if in_session:
                    last_scan = getattr(scanner, "last_scan_time", None)
                    running = bool(getattr(scanner, "is_running", False))
                    if running:
                        scanner_seen_running = True
                    if _scanner_liveness_alert_due(
                        running,
                        scanner_seen_running,
                        time.monotonic() - watchdog_started,
                        startup_grace,
                    ):
                        _alert(
                            "dead", "scanner is NOT running during market hours", state
                        )
                    elif last_scan is not None:
                        try:
                            last = last_scan if last_scan.tzinfo else IST.localize(last_scan)
                            age = (now - last.astimezone(IST)).total_seconds()
                        except Exception:
                            age = 0.0
                        if age > stale_after:
                            _alert(
                                "stale",
                                f"no scan cycle for {age:.0f}s (threshold {stale_after:.0f}s)",
                                state,
                            )
                        else:
                            state.pop("stale", None)
                            state.pop("dead", None)

                # 2. Auto Mode silently reverting to OFF (it is never persisted).
                auto_now = bool(getattr(scanner, "auto_mode", False))
                if auto_now:
                    auto_mode_seen_on = True
                    state.pop("auto_off", None)
                    state.pop("auto_never_armed", None)
                elif auto_mode_seen_on and in_session:
                    _alert(
                        "auto_off",
                        "Auto Mode reverted to OFF during market hours - signals will "
                        "NOT execute until it is re-enabled",
                        state,
                    )
                elif in_session and str(
                    getattr(scanner, "mode", "") or ""
                ).upper() == "REAL":
                    # The restart case, which the latch above CANNOT see (2026-08-16).
                    # auto_mode_seen_on is per-process, so a process that restarted
                    # mid-session has never seen AUTO on and never trips the branch
                    # above — while the 09:00 premarket worker DOES re-arm REAL after a
                    # restart (completed_dates is in-memory). The result is a dashboard
                    # showing REAL with feeds green and signals printing, zero entries
                    # for the rest of the day, and not one CRITICAL raised. That is
                    # indistinguishable from a quiet market until the close.
                    #
                    # REAL + in-session + AUTO off is worth saying out loud even on a
                    # deliberately manual day: it costs one alert, and the alternative
                    # is a silent full-day outage. _alert de-duplicates on the state key.
                    _alert(
                        "auto_never_armed",
                        "broker mode is REAL during market hours but Auto Mode is OFF - "
                        "signals will NOT execute. Auto Mode is not persisted, so a "
                        "restart always reverts it to manual; re-enable it on the "
                        "dashboard if this session is meant to trade unattended",
                        state,
                    )

                # 2b. Kite session — the token dies every morning, and without it
                # every order is refused while the scanner still looks perfectly
                # healthy. This is the Kite equivalent of the AngelOne AG7002 trap.
                try:
                    from data.kite_auth import get_kite_auth

                    kite_status = get_kite_auth().status()
                except Exception as exc:
                    kite_status = {"auth_required": True, "reason": f"status unavailable: {exc}"}
                if kite_status.get("auth_required"):
                    _alert(
                        "kite_token",
                        f"Kite login required ({kite_status.get('reason') or 'no session'}) - "
                        f"NO order can reach the exchange until you log in at "
                        f"http://{settings.dashboard_host}:{settings.dashboard_port}/api/kite_login",
                        state,
                    )
                else:
                    state.pop("kite_token", None)

                # 2c. Kite TICKS — a separate question from the Kite SESSION above. A
                # socket can stay open and simply stop delivering: on_close/on_error
                # never fire, so kiteconnect's reconnect never runs and nothing replays
                # the subscription. 2026-08-21: connected 10:23:05, silent from ~14:35,
                # no error until 15:30:19 (after the close), kite_ws age reaching 1,672s.
                #
                # kite_stream_stalled already existed and was already read twice — a
                # readiness warning (engine/safe_mode.py:276) and a dashboard issue
                # (engine/scanner_telemetry.py:757) — but neither writes a CRITICAL nor
                # attempts recovery, so 28 minutes of a dead feed left no line in the log
                # of its own. It surfaced only because a signal happened to arrive and hit
                # the entry gate, which correctly refuses entries without an
                # order-authority source. That gate is the safety response; this is the
                # alarm and the repair.
                try:
                    health = scanner.data.get_source_health()
                except Exception:
                    health = {}
                if health.get("kite_stream_stalled"):
                    if not state.get("kite_stream_resubscribed"):
                        state["kite_stream_resubscribed"] = True
                        try:
                            scanner.data.resubscribe_kite_tokens()
                        except Exception as exc:
                            logger.debug(f"Kite resubscribe attempt failed: {exc}")
                    age = health.get("kite_tick_age_seconds")
                    _alert(
                        "kite_stream",
                        "Kite tick stream is silent while the socket reports connected"
                        + (f" ({age:.0f}s since the last tick)" if isinstance(age, (int, float)) else " (no ticks yet)")
                        + (" - REST prices still serving" if health.get("kite_data_fresh")
                           else " - REST prices are stale too")
                        + ". Entries are refused while no order-authority source is fresh; "
                          "a resubscribe has been attempted",
                        state,
                    )
                else:
                    state.pop("kite_stream", None)
                    state.pop("kite_stream_resubscribed", None)

                # 3. Fyers auth — expires daily and cannot auto-refresh.
                try:
                    status = scanner.data.get_fyers_auth_status()
                except Exception:
                    status = {}
                if status.get("auth_required"):
                    _alert(
                        "fyers",
                        f"Fyers login required ({status.get('reason') or 'token invalid'}) - "
                        f"option chain/OI/PCR unavailable until you visit "
                        f"http://{settings.dashboard_host}:{settings.dashboard_port}/api/fyers_login",
                        state,
                    )
                else:
                    state.pop("fyers", None)

                # 4. Host memory / Windows commit pressure.  Fail closed only for NEW
                # exposure. Do not change scanner.auto_mode: that same flag controls
                # software exits, so switching it off would strand them behind manual
                # confirmation while the machine is already under stress.
                pressured, pressure_message = _apply_runtime_resource_guard(scanner)
                if pressured:
                    _alert(
                        "resource_pressure",
                        pressure_message,
                        state,
                    )
                else:
                    state.pop("resource_pressure", None)
            except Exception as exc:
                logger.debug(f"Runtime watchdog iteration failed: {exc}")
            time.sleep(max(15.0, check_every))

    threading.Thread(target=_loop, daemon=True, name="RuntimeWatchdog").start()
    logger.info(
        f"Runtime watchdog active (stale>{stale_after:.0f}s, "
        f"{start_at.strftime('%H:%M')}-{stop_at.strftime('%H:%M')} IST)."
    )


def _contract_token_for(data_provider, symbol: str) -> str:
    """Resolve an option contract's broker token, or "" when it cannot be found."""
    for attr in ("get_token_for_symbol", "resolve_token", "symbol_token"):
        fn = getattr(data_provider, attr, None)
        if callable(fn):
            try:
                token = str(fn(symbol) or "").strip()
                if token:
                    return token
            except Exception:
                continue
    lookup = getattr(data_provider, "contract_master", None)
    if isinstance(lookup, dict):
        row = lookup.get(symbol) or {}
        return str(row.get("token") or row.get("symboltoken") or "").strip()
    return ""


def _archive_missing_contract_count(value) -> int:
    """Normalize manifest missing-contract coverage across schema generations."""
    if isinstance(value, dict):
        return len(value)
    if isinstance(value, (list, tuple, set)):
        return len(value)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 1


def _session_archive_manifest_complete(root: Path, day: str) -> bool:
    path = root / "data_store" / "session_archive" / day / "manifest.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        symbols = {
            str(symbol or "").strip()
            for symbol in (manifest.get("traded_symbols_recorded") or [])
            if str(symbol or "").strip()
        }
        contract_count = int(manifest.get("contract_files_archived") or 0)
        spot_count = int(manifest.get("spot_files_archived") or 0)
        missing = _archive_missing_contract_count(
            manifest.get("contract_files_without_day_bars")
        )
        archived_symbols_value = manifest.get("contract_symbols_archived")
        archived_symbols = (
            {
                str(symbol or "").strip()
                for symbol in archived_symbols_value
                if str(symbol or "").strip()
            }
            if isinstance(archived_symbols_value, (list, tuple, set))
            else set()
        )
        missing_symbols = _archive_missing_contract_count(
            manifest.get("missing_contract_symbols")
        )
        exact_contract_coverage = bool(
            not symbols
            or (
                isinstance(archived_symbols_value, (list, tuple, set))
                and symbols.issubset(archived_symbols)
                and missing_symbols == 0
                and contract_count >= len(symbols)
            )
        )
        return bool(
            int(manifest.get("events") or 0) > 0
            and spot_count >= 12
            and missing == 0
            and exact_contract_coverage
        )
    except Exception:
        return False


def _next_due_session_archive_day(
    root: Path,
    now: datetime,
    run_at: dt_time,
    attempted: set[str],
) -> str | None:
    """Select each due incomplete ledger once before retrying a failed date.

    Newest-first keeps the latest session available quickly. ``attempted`` makes a
    persistent failure yield to the rest of the backlog; after one complete pass it is
    cleared so unresolved dates remain retryable rather than being abandoned.
    """
    now_ist = IST.localize(now) if now.tzinfo is None else now.astimezone(IST)
    event_dir = root / "data_store" / "rtracker" / "events"
    due_today = now_ist >= _ist_at(now_ist.date(), run_at)
    due: list[str] = []
    try:
        for path in event_dir.glob("????-??-??.jsonl"):
            day = path.stem
            try:
                parsed = datetime.strptime(day, "%Y-%m-%d").date()
            except ValueError:
                continue
            if parsed < now_ist.date() or (parsed == now_ist.date() and due_today):
                if not _session_archive_manifest_complete(root, day):
                    due.append(day)
    except OSError as exc:
        logger.warning(f"Unable to inspect session ledgers for archive catch-up: {exc}")
        return None

    due = sorted(set(due), reverse=True)
    if not due:
        attempted.clear()
        return None
    pending = [day for day in due if day not in attempted]
    if not pending:
        attempted.clear()
        pending = due
    return pending[0]


def _fetch_contract_history_for_session(
    data_provider,
    session_day: str,
    symbol: str,
    token: str,
) -> bool:
    """Fetch one option contract with the requested session as the API end date."""
    try:
        target_date = datetime.strptime(str(session_day), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return False
    target_end = _ist_at(target_date, dt_time(15, 45))
    df = data_provider.get_historical_candles(
        token,
        "BFO" if str(symbol).upper().startswith("SENSEX") else "NFO",
        "1min",
        days_back=1,
        instrument_name=symbol,
        force_refresh=True,
        to_date=target_end,
    )
    return df is not None and len(df) > 0


def _resolve_session_archive_provider(data_provider):
    """Return (provider, deferred_not_ready, reason) without touching broker state."""
    if data_provider is None:
        # An explicitly unwired scheduler is allowed to archive cache-only data.
        return None, False, ""
    if hasattr(data_provider, "get_historical_candles"):
        return data_provider, False, ""
    if not callable(data_provider):
        return None, False, ""
    try:
        resolved = data_provider()
    except Exception as exc:
        return None, True, f"deferred provider unavailable: {exc}"
    if resolved is None:
        return None, True, "deferred provider not initialized"
    if not hasattr(resolved, "get_historical_candles"):
        return None, True, "deferred provider has no historical-candle API"
    return resolved, False, ""


def start_session_archiver_scheduler(data_provider=None):
    """Archive each session's trades + real option-premium candles after the close.

    Builds the multi-day validation set the offline exit backtester replays. The live
    candle cache only keeps the last-fetched contracts, so without this snapshot a
    traded strike's intraday bars are lost/overwritten (2026-07-24: only ~40% of trades
    were replayable). Runs once per day AFTER market close, never during market hours,
    in a child process so it cannot stall the scanner loop.
    """
    enabled = str(os.getenv("SESSION_ARCHIVE_ENABLED", "1")).strip().lower()
    if enabled in _FALSEY_ENV:
        logger.info("Session archiver disabled by SESSION_ARCHIVE_ENABLED.")
        return

    run_at = _parse_hhmm(os.getenv("SESSION_ARCHIVE_TIME", "15:50"), dt_time(15, 50))
    try:
        provider_retry_seconds = max(
            5.0,
            min(15.0, float(os.getenv("SESSION_ARCHIVE_PROVIDER_RETRY_SECONDS", "10") or 10.0)),
        )
    except (TypeError, ValueError):
        provider_retry_seconds = 10.0
    root = Path(__file__).parent
    script = root / "scripts" / "archive_session_data.py"

    def _manifest_complete(day: str) -> bool:
        return _session_archive_manifest_complete(root, day)

    def _latest_due_event_day(now: datetime, attempted: set[str]) -> str | None:
        """Next incomplete ledger, rotating across the full due backlog."""
        return _next_due_session_archive_day(root, now, run_at, attempted)

    def _archive_loop():
        completed: set[str] = set()
        attempted: set[str] = set()
        last_provider_wait_reason = ""
        while True:
            now = datetime.now(IST)
            today_run = _ist_at(now.date(), run_at)
            session_day = _latest_due_event_day(now, attempted)
            if session_day is None:
                target = today_run if now < today_run else _ist_at(now.date() + timedelta(days=1), run_at)
                time.sleep(max(30.0, (target - now).total_seconds()))
                continue
            # BACK-FILL FIRST, in this process, because this is where the broker
            # session lives. The archiver runs as a child with no auth, so it can only
            # copy what the live cache already holds -- and for OPTION contracts the
            # cache held nothing: as of 2026-08-14 it had no CE/PE file newer than
            # 2026-07-31, so every session after that archived index candles and no
            # premium, and became permanently unreplayable. The snapshot existed; the
            # thing it was meant to snapshot did not.
            # Resolved HERE, not at wiring time. The provider is built inside
            # create_system(), which runs on the dashboard bootstrap task well after
            # this scheduler is started, so at wiring time there is nothing to pass.
            # A callable defers the lookup to the moment it is actually needed --
            # after the close, by which point the scanner exists.
            provider, provider_pending, provider_reason = _resolve_session_archive_provider(
                data_provider
            )
            if provider_pending:
                # Startup race: the scheduler thread can reach an overdue ledger before
                # create_system publishes runtime["scanner"]. This is not an archive
                # attempt. Do not rotate away from the newest day and do not run the
                # cache-only child against data we already know is not ready yet.
                if provider_reason != last_provider_wait_reason:
                    logger.info(
                        f"Session archive waiting for runtime data provider: {provider_reason}"
                    )
                    last_provider_wait_reason = provider_reason
                time.sleep(provider_retry_seconds)
                continue
            last_provider_wait_reason = ""
            # Mark only after deferred startup dependencies are ready. A real
            # provider/back-fill/child failure should yield to the rest of the backlog.
            attempted.add(session_day)
            if provider is not None:
                try:
                    from scripts.archive_session_data import (
                        backfill_contract_history, contract_tokens, load_events,
                    )

                    # The token comes from the session's OWN event stream. Asking the
                    # provider never worked: _contract_token_for probes four attributes
                    # (get_token_for_symbol / resolve_token / symbol_token /
                    # contract_master) that MarketDataProvider does not have -- it
                    # exposes get_option_token(name, strike, option_type) instead. So the
                    # token was always "", _fetch always returned False, and this
                    # back-fill filled nothing on any session. It stays as a fallback.
                    session_events = load_events(session_day)
                    session_tokens = contract_tokens(session_events)

                    def _fetch(symbol: str) -> bool:
                        token = (
                            session_tokens.get(symbol)
                            or _contract_token_for(provider, symbol)
                        )
                        if not token:
                            return False
                        # force_refresh, because the whole point of this call is to GO
                        # AND GET the day. Otherwise get_historical_candles returns the
                        # local option CSV whenever the file merely EXISTS, without
                        # checking that it covers the day asked for -- so a contract
                        # whose cache stops at an earlier session answers instantly with
                        # a non-empty frame, this counts a success, and nothing new is
                        # ever written for the archiver to copy.
                        return _fetch_contract_history_for_session(
                            provider,
                            session_day,
                            symbol,
                            token,
                        )

                    filled = backfill_contract_history(
                        session_day, _fetch, events=session_events, quiet=True
                    )
                    if filled:
                        logger.info(
                            f"Back-filled {len(filled)} contract histories before "
                            f"archiving {session_day}: {', '.join(filled[:6])}"
                            + (" ..." if len(filled) > 6 else "")
                        )
                except Exception as exc:
                    # Never block the archive: a session that cannot be back-filled must
                    # still archive whatever it does have.
                    logger.warning(f"Contract back-fill skipped for {session_day}: {exc}")
            try:
                result = subprocess.run(
                    [sys.executable, str(script), session_day, "--quiet"],
                    cwd=str(root),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    stdin=subprocess.DEVNULL,
                    timeout=600,
                    check=True,
                    text=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                if not _manifest_complete(session_day):
                    raise RuntimeError("archive process returned success but manifest coverage is incomplete")
                logger.info(f"Session archive verified for {session_day}.")
                completed.add(session_day)
            except Exception as exc:
                detail = ""
                if isinstance(exc, subprocess.CalledProcessError):
                    detail = str((exc.stderr or exc.stdout or "").strip())[-500:]
                logger.warning(
                    f"Session archive failed for {session_day}: {exc}"
                    + (f"; child output: {detail}" if detail else "")
                )
                # Retry the missed ledger instead of marking it complete, but do not
                # spin a failing child process continuously.
                time.sleep(300.0)

    threading.Thread(target=_archive_loop, daemon=True, name="SessionArchiver").start()


def _scanner_trading_day(scanner: Scanner, now_ist: datetime) -> bool:
    checker = getattr(scanner, "_is_trading_day", None)
    if callable(checker):
        try:
            return bool(checker(now_ist))
        except Exception as exc:
            logger.warning(f"Premarket REAL worker trading-day check failed: {exc}")
            return False
    if now_ist.weekday() >= 5:
        return False
    data = getattr(scanner, "data", None)
    try:
        if data is not None and hasattr(data, "is_market_holiday") and data.is_market_holiday(now_ist):
            return False
    except Exception:
        pass
    return True


def _auto_real_mode_attempt(scanner: Scanner, settings, now_ist: datetime | None = None) -> tuple[bool, str]:
    """Runtime-only HISTORICAL -> REAL/manual premarket arm.

    This intentionally never enables Auto Mode. It only changes the feed/broker
    operating mode so live data, broker health, warmup, and manual supervision
    are ready before the 09:18 trading gate.
    """
    if not _setting_enabled(getattr(settings, "auto_real_mode_premarket_enabled", True), True):
        return False, "disabled"

    now_ist = now_ist or datetime.now(IST)
    if now_ist.tzinfo is None:
        now_ist = IST.localize(now_ist)
    else:
        now_ist = now_ist.astimezone(IST)

    if hasattr(scanner, "is_running") and not bool(getattr(scanner, "is_running", False)):
        return False, "scanner_not_running"

    if not _scanner_trading_day(scanner, now_ist):
        return False, "not_trading_day"

    switch_at = _parse_hhmmss(
        getattr(settings, "auto_real_mode_premarket_switch_time", "09:00:00"),
        dt_time(9, 0, 0),
    )
    live_end = _parse_hhmmss(getattr(settings, "real_mode_live_end", "15:30:00"), dt_time(15, 30))
    if now_ist.time() < switch_at:
        return False, "before_switch_time"
    if now_ist.time() > live_end:
        return False, "after_live_end"

    current_mode = str(getattr(scanner, "mode", "") or "").upper()
    if current_mode == "REAL":
        if bool(getattr(scanner, "auto_mode", False)):
            scanner.configure(auto_mode=False, persist_settings=False)
            logger.warning("Premarket REAL worker forced Auto Mode OFF while already in REAL mode.")
        return True, "already_real_manual"
    if current_mode != "HISTORICAL":
        return False, f"unsupported_mode:{current_mode or 'unknown'}"

    if bool(getattr(scanner, "auto_mode", False)):
        scanner.configure(auto_mode=False, persist_settings=False)
        logger.info("Premarket REAL worker disabled Auto Mode before arming REAL/manual mode.")

    configured = scanner.configure(
        mode="REAL",
        auto_mode=False,
        confirm_real_mode=True,
        real_mode_verification="YES",
        persist_settings=False,
    )
    if configured and str(getattr(scanner, "mode", "") or "").upper() == "REAL":
        if bool(getattr(scanner, "auto_mode", False)):
            scanner.configure(auto_mode=False, persist_settings=False)
        message = "Auto premarket arm: switched to REAL manual mode; Auto Mode remains OFF."
        logger.warning(message)
        try:
            scanner.log_event(message, "system")
        except Exception:
            pass
        return True, "switched_real_manual"

    reason = str(getattr(scanner, "_last_configure_block_reason", "") or "configure_blocked")
    logger.warning(f"Auto premarket REAL arm blocked: {reason}")
    return False, reason


def start_premarket_real_mode_worker(scanner: Scanner, settings) -> None:
    """Arm REAL/manual mode at 09:00 on trading days when the app is already running."""
    if not _setting_enabled(getattr(settings, "auto_real_mode_premarket_enabled", True), True):
        logger.info("Premarket REAL/manual worker disabled by settings.")
        return

    switch_at = _parse_hhmmss(
        getattr(settings, "auto_real_mode_premarket_switch_time", "09:00:00"),
        dt_time(9, 0, 0),
    )
    retry_seconds = max(
        5.0,
        float(getattr(settings, "auto_real_mode_premarket_retry_seconds", 30.0) or 30.0),
    )
    completed_dates: set[str] = set()
    last_reason_by_date: dict[str, str] = {}

    def _scheduler_loop():
        while True:
            now = datetime.now(IST)
            session_day = now.date().isoformat()
            today_switch = _ist_at(now.date(), switch_at)

            if session_day in completed_dates:
                tomorrow_switch = _ist_at(now.date() + timedelta(days=1), switch_at)
                sleep_seconds = max(60.0, (tomorrow_switch - now).total_seconds())
            elif now < today_switch:
                sleep_seconds = max(5.0, (today_switch - now).total_seconds())
            else:
                ok, reason = _auto_real_mode_attempt(scanner, settings, now)
                if ok:
                    completed_dates.add(session_day)
                    sleep_seconds = 60.0
                elif reason in {"disabled", "not_trading_day", "after_live_end"}:
                    completed_dates.add(session_day)
                    tomorrow_switch = _ist_at(now.date() + timedelta(days=1), switch_at)
                    sleep_seconds = max(60.0, (tomorrow_switch - now).total_seconds())
                else:
                    if last_reason_by_date.get(session_day) != reason:
                        logger.warning(f"Premarket REAL/manual worker waiting: {reason}")
                        last_reason_by_date[session_day] = reason
                    sleep_seconds = retry_seconds

            logger.info(
                "Premarket REAL/manual worker sleeping "
                f"{sleep_seconds:.0f}s (switch {switch_at.strftime('%H:%M:%S')} IST)."
            )
            time.sleep(sleep_seconds)

    threading.Thread(
        target=_scheduler_loop,
        daemon=True,
        name="PremarketRealModeWorker",
    ).start()



def create_system():
    """Initialize all system components"""
    settings = get_settings()
    instruments = settings.get_instruments()

    risk_summary = settings.risk_configuration_summary()
    logger.info(
        f"Risk configuration: capital={risk_summary['capital_total']:,.0f} "
        f"FUT={risk_summary['capital_fut']:,.0f} OPT={risk_summary['capital_opt']:,.0f} | "
        f"SL FUT={risk_summary['futures_sl_pct']:.3f}% "
        f"OPT={risk_summary['options_sl_pct']:.2f}% | "
        f"absolute cap={risk_summary['max_trade_loss_abs']:,.0f}"
    )
    for warning in risk_summary["warnings"]:
        logger.warning(f"Risk configuration warning: {warning}")
    if settings.trading_mode == "REAL" and not risk_summary["ok"]:
        logger.critical(
            "REAL startup refused by risk configuration: "
            + "; ".join(risk_summary["issues"])
        )
        settings.trading_mode = "HISTORICAL"

    env_path = Path(__file__).parent / ".env"
    plaintext_secret_keys = set()
    if env_path.exists():
        for raw_line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            key, separator, value = raw_line.partition("=")
            if separator and value.strip() and key.strip().upper() in {
                "ANGELONE_API_KEY",
                "ANGELONE_PASSWORD",
                "ANGELONE_TOTP_SECRET",
                "ANGELONE_SECRET_KEY",
                "FYERS_SECRET_KEY",
                "FYERS_PASSWORD",
            }:
                plaintext_secret_keys.add(key.strip().upper())
    if plaintext_secret_keys:
        logger.warning(
            "Broker credentials are stored in local plaintext .env; keep this host single-user "
            "and restrict file ACLs."
        )
    if (
        settings.trading_mode == "REAL"
        and settings.shared_host_mode
        and plaintext_secret_keys
        and not settings.allow_plaintext_broker_secrets
    ):
        logger.critical("REAL startup refused: plaintext broker secrets are forbidden in shared-host mode.")
        settings.trading_mode = "HISTORICAL"

    logger.info("═" * 60)
    logger.info("🚀 UT1 Index Trading System v2.0")
    logger.info("═" * 60)

    # 1. Data Provider (Yahoo Finance + AngelOne)
    # No credentials passed: Kite authenticates through the browser login, and the
    # provider binds to the shared KiteAuth session in connect(). Feeding it the old
    # AngelOne credentials implied they still mattered — they do not.
    data_provider = MarketDataProvider()
    data_provider.connect()
    if settings.trading_mode == "REAL":
        readiness = validate_real_mode_readiness(settings, data_provider)
        if readiness.get("blockers") == ["all broker sources unavailable"]:
            for _ in range(12):
                time.sleep(0.25)
                readiness = validate_real_mode_readiness(settings, data_provider)
                if readiness.get("blockers") != ["all broker sources unavailable"]:
                    break
        # Warnings are not blockers, but they were being discarded entirely — including
        # "broker free cash is below the floor for one index lot". See
        # Scanner._surface_readiness_warnings for the full account.
        for warning in readiness.get("warnings") or []:
            logger.warning(f"REAL readiness warning: {warning}")
        if getattr(settings, "production_safe_mode_required", True) and not readiness.get("ok"):
            logger.error(
                "REAL startup refused by safe mode: "
                + "; ".join(readiness.get("blockers") or ["readiness failed"])
            )
            settings.trading_mode = "HISTORICAL"
            try:
                settings.save_to_env()
            except Exception as exc:
                logger.error(f"Failed to persist HISTORICAL safe-mode fallback: {exc}")
    
    # 1b. Broker Setup (Dual Mode)
    sim_broker = SignalSimBroker()
    real_broker = None
    # Build the Kite broker even with no session yet. Gating this on a live connection
    # was an AngelOne assumption: that login happened automatically at startup, so
    # "not connected" meant "broker impossible". Kite's login is a manual browser step
    # that normally happens AFTER startup, and refusing to construct the broker left
    # the system stuck in SIGNAL SIMULATION until a full restart — the operator would
    # log in, see a healthy dashboard, and still place no live orders.
    # KiteBroker holds the shared auth, so the session it gets rebound at login time.
    if True:
        # Zerodha Kite is the execution broker. The session comes from the shared
        # KiteAuth so the dashboard login route, the data layer and the broker all
        # observe one token — a browser re-login must not leave the broker holding a
        # dead one. The lease is keyed on the Kite user id, since it is Kite positions
        # that are account-netted now.
        from data.kite_auth import get_kite_auth
        from trading.kite_broker import KiteBroker

        kite_auth = get_kite_auth(settings)
        real_broker = KiteBroker(
            kite=kite_auth.kite,
            auth=kite_auth,
            account_id=str(getattr(settings, "kite_user_id", "") or "").strip(),
            instance_id=f"UT1:{settings.dashboard_port}",
        )
        data_provider.on_kite_reauth = real_broker.replace_api
        if kite_auth.kite is None:
            # Not fatal at startup: the operator logs in through the dashboard and the
            # session is rebound. It IS fatal for placing orders, and KiteBroker
            # refuses them with that exact instruction until then.
            logger.critical(
                "Kite session required: no valid token. Complete the daily login at "
                f"http://{settings.dashboard_host}:{settings.dashboard_port}/api/kite_login "
                f"({kite_auth.status().get('reason')})"
            )
        # A blank account id builds no lease, and the lease accessors answer True
        # when there is none — so this gate could never fire in exactly the case
        # where single-instance ownership is least establishable. Refuse REAL
        # unless the lease is both establishable and actually held.
        if settings.trading_mode == "REAL" and not (
            real_broker.execution_lease_established()
            and real_broker.acquire_execution_lease()
        ):
            logger.critical(
                "REAL startup refused: " + real_broker.execution_lease_block_reason()
            )
            settings.trading_mode = "HISTORICAL"
            try:
                settings.save_to_env()
            except Exception as exc:
                logger.error(f"Failed to persist lease safe-mode fallback: {exc}")
        data_provider.fetch_instruments()
        if kite_auth.kite is not None:
            logger.info("✅ Broker (Zerodha Kite LIVE Ready)")
        else:
            logger.warning(
                "⚠️ Broker built but IDLE — Kite login pending; orders are refused until then"
            )

        # The Kite app's registered redirect must match the port this dashboard
        # actually serves, or Zerodha bounces the browser to a dead address and the
        # login silently never completes.
        redirect = str(getattr(settings, "kite_redirect_uri", "") or "").strip()
        expected = f"/api/kite_login"
        if redirect and (
            str(settings.dashboard_port) not in redirect or expected not in redirect
        ):
            logger.critical(
                f"KITE REDIRECT MISMATCH: the app is registered to {redirect} but this "
                f"dashboard serves http://{settings.dashboard_host}:{settings.dashboard_port}"
                f"{expected}. Update the redirect URL at developers.kite.trade or the "
                f"login will never complete."
            )

    # Initial broker selection based on settings
    active_broker = real_broker if (settings.trading_mode == "REAL" and real_broker) else sim_broker
    logger.info(f"✅ Active Broker: {'REAL' if active_broker == real_broker else 'SIGNAL SIMULATION (Real Data)'}")

    # 2. Candle Builder
    candle_builder = CandleBuilder(max_candles=2000)
    logger.info("✅ Candle Builder")

    # Connect tick streams
    data_provider.on_tick = candle_builder.update_from_tick

    # 3. UT Bot Multi-TF Engine
    mtf_engine = MultiTimeframeEngine(settings.get_ut_engine_params())
    active_index_count = len(getattr(settings, "active_indices", []) or [])
    scanner_count = active_index_count * 3
    logger.info(
        f"✅ UT1 Multi-TF Engine ({active_index_count} active indices × 3 TFs = {scanner_count} live scanners)"
    )

    # 4. Signal Manager
    signal_manager = SignalManager()
    logger.info("✅ Signal Manager (dedup + grading)")

    # 5. Intelligence Aggregator
    intelligence = IntelligenceAggregator()
    logger.info("✅ Intelligence (Vol/OI/PCR/Greeks/Regime/OrderFlow)")

    # 6. Trade Manager
    trade_manager = TradeManager(
        broker=active_broker,
        max_positions=settings.max_concurrent_positions,
        session_end=settings.ut_session_end,
        product_type=settings.trade_product_type,
        on_notification=send_desktop_notification,
    )
    # Give trade_manager references to both for hot-swapping
    trade_manager.mock_broker = sim_broker
    trade_manager.real_broker = real_broker

    # The live adapter asks this at the moment of every mutation, not when the caller
    # was queued. That is what closes the hot-swap window: a job enqueued while REAL and
    # dispatched after a switch to HISTORICAL asks again here and is refused, and so is
    # anything on a timer (reconcile runs every ~5s in every mode) that reaches the
    # broker after the mode changed underneath it. Bound to the manager rather than to a
    # captured value so it reads the CURRENT mode every time.
    if hasattr(real_broker, "set_execution_authority"):
        real_broker.set_execution_authority(trade_manager._live_orders_permitted)

    # 7. Performance Tracker
    performance = PerformanceTracker()
    logger.info("✅ Performance Tracker")

    # 8. Scanner
    scanner = Scanner(
        data_provider=data_provider,
        candle_builder=candle_builder,
        mtf_engine=mtf_engine,
        signal_manager=signal_manager,
        intelligence=intelligence,
        trade_manager=trade_manager,
        performance=performance,
        instruments_config=instruments,
        trading_mode=settings.trading_mode,
        on_update=broadcast,
        on_notification=send_desktop_notification,
    )
    scanner.configure(
        capital_fut=settings.capital_fut,
        risk_fut_pct=settings.risk_fut_pct,
        capital_opt=settings.capital_opt,
        risk_opt_pct=settings.risk_opt_pct,
    )
    scanner.max_daily_loss_pct = settings.max_daily_loss_pct
    logger.info("✅ Scanner (exit-before-entry, 30s data cache)")

    # Set scanner reference for dashboard API
    dashboard_module.scanner_ref = scanner

    logger.info("═" * 60)
    logger.info(f"📊 Dashboard: http://localhost:{settings.dashboard_port}")
    logger.info(f"📊 Mode: {settings.trading_mode.upper()}")
    logger.info(f"📊 Max Daily Loss: {settings.max_daily_loss_pct}%")
    logger.info("═" * 60)

    return scanner, settings


async def run_scanner(scanner: Scanner):
    """Run scanner in background"""
    await scanner.run()


def main():
    settings = get_settings()
    runtime: dict[str, object] = {}

    async def _bootstrap_system_after_dashboard_start():
        await asyncio.sleep(0.5)
        # Browser is launched directly by Start_UT1.bat using a TCP listener to avoid Python boot delays.
        try:
            dashboard_loop = asyncio.get_running_loop()
            scanner, _ = await asyncio.to_thread(create_system)
            runtime["scanner"] = scanner
            # Lifespan shutdown may begin while create_system is running in its
            # worker thread.  Publish the resource so the shutdown hook can close
            # it, but never launch a scanner thread after ingress has been gated.
            if bool(getattr(dashboard_module, "_dashboard_shutting_down", False)):
                logger.warning("Dashboard shutdown began during bootstrap; scanner launch skipped.")
                return

            async def _threadsafe_broadcast(payload):
                future = asyncio.run_coroutine_threadsafe(broadcast(payload), dashboard_loop)
                await asyncio.wrap_future(future)

            scanner.on_update = _threadsafe_broadcast
            # Immediate trade-panel refresh on entry/exit (thread-safe poke).
            scanner.on_trade_event = dashboard_module.notify_trade_change

            def _run_scanner_loop():
                asyncio.run(scanner.run())

            scanner_thread = threading.Thread(
                target=_run_scanner_loop,
                name="ScannerEventLoop",
                daemon=True,
            )
            # Retain the runner so graceful shutdown can prove the scan/event
            # loop has actually stopped before draining order work and releasing
            # the account execution lease.
            scanner._runner_thread = scanner_thread
            runtime["scanner_thread"] = scanner_thread
            scanner_thread.start()
            start_premarket_real_mode_worker(scanner, settings)
            start_runtime_watchdog(scanner, settings)
        except Exception as exc:
            logger.exception(f"System bootstrap failed after dashboard start: {exc}")

    dashboard_module.startup_hook = lambda: asyncio.create_task(_bootstrap_system_after_dashboard_start())

    async def _graceful_system_shutdown() -> None:
        """Drain broker work, persist scanner state, then release live resources."""
        scanner = runtime.get("scanner")
        await dashboard_module.shutdown_scanner()
        if scanner is None:
            return
        real_broker = getattr(getattr(scanner, "trades", None), "real_broker", None)
        release_lease = getattr(real_broker, "release_execution_lease", None)
        if callable(release_lease):
            try:
                await asyncio.to_thread(release_lease)
                logger.info("✅ Broker account execution lease released.")
            except Exception as exc:
                logger.error(f"Broker lease release failed during shutdown: {exc}")
        telemetry_shutdown = getattr(scanner, "shutdown_telemetry_executor", None)
        if callable(telemetry_shutdown):
            telemetry_shutdown(wait=False)
        lab_executor = getattr(scanner, "_engine_lab_executor", None)
        if lab_executor is not None:
            try:
                lab_executor.shutdown(wait=False, cancel_futures=True)
            except Exception as exc:
                logger.debug(f"Engine-lab executor shutdown skipped: {exc}")
        # The dashboard's dedicated chart pool. Every other executor in this system is
        # shut down here; a new one that is not would keep the process alive on exit.
        chart_executor = getattr(dashboard_module, "_chart_executor", None)
        if chart_executor is not None:
            try:
                chart_executor.shutdown(wait=False, cancel_futures=True)
            except Exception as exc:
                logger.debug(f"Chart executor shutdown skipped: {exc}")
        runtime.clear()

    dashboard_module.shutdown_hook = _graceful_system_shutdown

    # Start the log cleaner thread
    start_angelone_log_cleaner()
    start_live_session_tracker_scheduler()
    # The provider does not exist yet: it is built by create_system() on the bootstrap
    # task that runs after the dashboard starts, and is reachable as scanner.data.
    # Passing the object here referenced a name bound only inside create_system(), which
    # crashed startup with NameError before uvicorn ever came up. The archiver sleeps
    # until after the close, so a getter is resolved in time.
    start_session_archiver_scheduler(
        data_provider=lambda: getattr(runtime.get("scanner"), "data", None)
    )

    logger.info(f"Starting dashboard server on {settings.dashboard_host}:{settings.dashboard_port}")
    uvicorn.run(
        app,
        host=settings.dashboard_host,
        port=settings.dashboard_port,
        log_level="warning",
        ws_max_size=16777216,
    )
    return


if __name__ == "__main__":
    main()
