"""
UT1 Index Trading System — Main Entry Point
═══════════════════════════════════════════════════════════════
Run: python main.py
Dashboard: http://localhost:8001
"""

import sys
import asyncio
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
logger.remove()
logger.add(
    sys.stderr,
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
                                db.cleanup_old_records(days=31)
                                with open(cleanup_marker, "w") as f:
                                    f.write(current_month_str)
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

    Detection only — it never changes trading state, it just makes the failure loud.
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
                elif auto_mode_seen_on and in_session:
                    _alert(
                        "auto_off",
                        "Auto Mode reverted to OFF during market hours - signals will "
                        "NOT execute until it is re-enabled",
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
            except Exception as exc:
                logger.debug(f"Runtime watchdog iteration failed: {exc}")
            time.sleep(max(15.0, check_every))

    threading.Thread(target=_loop, daemon=True, name="RuntimeWatchdog").start()
    logger.info(
        f"Runtime watchdog active (stale>{stale_after:.0f}s, "
        f"{start_at.strftime('%H:%M')}-{stop_at.strftime('%H:%M')} IST)."
    )


def start_session_archiver_scheduler():
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
    root = Path(__file__).parent
    script = root / "scripts" / "archive_session_data.py"

    def _archive_loop():
        completed: set[str] = set()
        while True:
            now = datetime.now(IST)
            session_day = now.date().isoformat()
            today_run = _ist_at(now.date(), run_at)
            if session_day in completed or now < today_run:
                target = (
                    _ist_at(now.date() + timedelta(days=1), run_at)
                    if session_day in completed
                    else today_run
                )
                time.sleep(max(30.0, (target - now).total_seconds()))
                continue
            try:
                subprocess.run(
                    [sys.executable, str(script), session_day, "--quiet"],
                    cwd=str(root),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                    timeout=600,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                logger.info(f"Session archive written for {session_day}.")
            except Exception as exc:
                logger.warning(f"Session archive failed for {session_day}: {exc}")
            completed.add(session_day)

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
        if settings.trading_mode == "REAL" and not real_broker.acquire_execution_lease():
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
    start_session_archiver_scheduler()

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
