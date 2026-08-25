"""
Live Market Session Tracker  (READ-ONLY)
========================================
Tracks everything that matters during a live trading session WITHOUT touching
the order path or competing with the running scanner for broker quota:

  * Polls the dashboard API (/api/status, /api/diagnostics, /api/trades) over loopback.
  * Tails logs/ut1.log incrementally and classifies events (data-feed,
    option-chain, broker, protection/recovery, trades, errors).
  * Writes a rolling JSON state + a human-readable markdown report with
    auto-generated, severity-ranked fix suggestions.

It NEVER places/cancels orders and NEVER switches modes. Pure observer.

Usage:
  python scripts/live_session_tracker.py --once                 # one snapshot + report
  python scripts/live_session_tracker.py --watch --since 09:00 --until 15:35  # track the session
  python scripts/live_session_tracker.py --watch --interval 120 # custom cadence
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.request
import uuid
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import pytz

IST = pytz.timezone("Asia/Kolkata")
ROOT = Path(__file__).resolve().parents[1]
LOG_FILE = ROOT / "logs" / "ut1.log"
OUT_DIR = ROOT / "runtime_logs" / "session_tracker"
def _resolve_api_base() -> str:
    """The dashboard THIS build serves, not a hardcoded port.

    Was `http://127.0.0.1:8001`. rp46-3 serves DASHBOARD_PORT=8500, and 8001 belongs to
    a different build on this machine. Both outcomes were wrong: with 8001 down every
    poll failed (2026-08-20: "Dashboard API unreachable polls: 316/316", every health
    field `n/a`), and with 8001 UP the tracker reported the OTHER build's state under
    this build's filename — the 2026-08-21 report claims "FUT budget 18000.0 · max trade
    loss 5000.0 · warnings 0" against this build's Rs.0.015, 0, and one warning.

    SESSION_TRACKER_API still wins, so a build can be pointed somewhere else on purpose.
    """
    override = (os.getenv("SESSION_TRACKER_API") or "").strip()
    if override:
        return override.rstrip("/")
    host: str = "127.0.0.1"
    port: Optional[int] = None

    # ROOT/.env FIRST, and deliberately not via config.settings. Two traps stack here:
    #
    #   1. This module is RUN AS A SCRIPT, so sys.path[0] is scripts/ and `config` is
    #      not importable at all. Importing the module in a test DOES put the repo root
    #      on the path, so a settings-based resolver passes its tests and still fails in
    #      the only context that runs.
    #   2. Even with the import fixed, get_settings() reads `.env` relative to the CWD.
    #      The tracker is launched from scripts/, where there is no .env, so settings
    #      hand back the CODE DEFAULT — `dashboard_port: int = 8001`
    #      (config/settings.py:1664). That default is precisely the wrong port, and it
    #      is why the original hardcoded 8001 looked reasonable.
    #
    # .env is the real settings store in this build, and ROOT is absolute, so reading it
    # directly is both cwd-independent and the actual source of truth.
    def _from_env_file(key: str) -> str:
        try:
            for raw in (ROOT / ".env").read_text(
                encoding="utf-8", errors="replace"
            ).splitlines():
                name, sep, value = raw.partition("=")
                if sep and name.strip() == key:
                    return value.strip().strip("'\"").strip()
        except Exception:
            return ""
        return ""

    try:
        port = int(_from_env_file("DASHBOARD_PORT") or 0) or None
    except (TypeError, ValueError):
        port = None
    host = _from_env_file("DASHBOARD_HOST") or host

    if port is None:
        # No .env (a fresh checkout, or a build that configures settings another way).
        try:
            import sys

            if str(ROOT) not in sys.path:
                sys.path.insert(0, str(ROOT))
            from config.settings import get_settings

            settings = get_settings()
            port = int(getattr(settings, "dashboard_port", 0) or 0) or None
            host = str(getattr(settings, "dashboard_host", host) or host).strip()
        except Exception:
            port = None
    if host in ("", "0.0.0.0", "::"):
        # A bind-all address is not something you can dial.
        host = "127.0.0.1"
    return f"http://{host}:{port or 8001}".rstrip("/")


API = _resolve_api_base()
PID_FILE_NAME = "tracker.pid"
LOCK_FILE_NAME = "tracker.lock"
MARKET_START_MINUTES = 9 * 60 + 15
MARKET_END_MINUTES = 15 * 60 + 30
EXPECTED_INDICES = ("NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY")
EXPECTED_ENGINE_LAB_ROWS = 10
INTEL_COMPONENTS = ("volume", "oi", "pcr", "regime", "order_flow", "aggregate")
SESSION_FEATURE_TOGGLES = (
    ("engine_lab_enabled", "Engine Lab 12-mode shadow comparison"),
    ("execution_touch_quote_shadow_enabled", "Touch-quote execution shadow"),
    ("stabilization_momentum_override_enabled", "Stabilization momentum override"),
    ("reversal_entry_relief_enabled", "Reversal-aware entry relief"),
    ("directional_census_reversal_relief_enabled", "Directional-census reversal relief"),
    ("price_action_reversal_relief_enabled", "Conditional price-action veto relief"),
    ("late_session_choppy_entry_guard_enabled", "Late-session choppy entry guard"),
    ("market_wide_reversal_enabled", "Cross-index reversal detection"),
    ("volume_spike_entry_enabled", "Volume-spike/power-move entry path"),
    ("power_move_override_enabled", "Power-move override"),
    ("one_min_scalping_enabled", "1m scalping mode"),
    ("live_15min_momentum_override_enabled", "15m momentum override"),
    ("late_session_reclaim_override_enabled", "Late-session reclaim override"),
    ("fifteen_min_choppy_scalp_guard_enabled", "15m choppy scalp guard"),
    ("ut_confidence_calibration_enabled", "Outcome calibration evidence collection"),
    ("live_intrabar_signal_entries", "Intrabar signal tracking"),
)
QUAD_PRIME_FILTER_GROUPS = (
    (
        "runtime",
        "Runtime",
        (
            ("scanner_engine", "Engine"),
            ("grade_preference", "Grade"),
            ("ut_regime_adaptation", "Regime adaptation"),
        ),
    ),
    (
        "late_session_reclaim",
        "Late-session reclaim",
        (
            ("late_session_reclaim_override_enabled", "Enabled"),
            ("late_session_reclaim_start", "Start"),
            ("late_session_reclaim_end", "End"),
            ("late_session_reclaim_core_votes", "Basket votes"),
            ("late_session_reclaim_total_votes", "Total votes"),
            ("late_session_reclaim_reclaim_votes", "Reclaim votes"),
            ("late_session_reclaim_volume_votes", "Volume votes"),
            ("late_session_reclaim_min_volume_ratio", "Min volume"),
            ("late_session_reclaim_min_close_location", "Close location"),
            ("late_session_reclaim_max_skew_seconds", "Max skew sec"),
            ("late_session_reclaim_options_sl_pct", "OPT SL %"),
            ("late_session_reclaim_futures_sl_pct", "FUT SL %"),
            ("late_session_reclaim_rr", "RR"),
        ),
    ),
    (
        "fifteen_min_choppy_scalp",
        "15m choppy scalp guard",
        (
            ("fifteen_min_choppy_scalp_guard_enabled", "Enabled"),
            ("fifteen_min_choppy_scalp_min_intel_score", "Min intel"),
            ("fifteen_min_choppy_scalp_min_volume_ratio", "Min volume"),
            ("fifteen_min_choppy_scalp_micro_max_skew_seconds", "Micro skew sec"),
            ("fifteen_min_choppy_scalp_min_close_location", "Close location"),
            ("fifteen_min_choppy_scalp_profit_lock_r_multiple", "Profit lock R"),
            ("fifteen_min_choppy_scalp_min_peak_rs", "Min peak Rs"),
        ),
    ),
    (
        "midcap_15min_buffer",
        "MIDCP 15m risky buffer",
        (
            ("midcap_15min_risky_stabilization_seconds", "Seconds"),
            ("midcap_15min_risky_stabilization_min_scans", "Min scans"),
            ("midcap_15min_risky_stabilization_min_volume_ratio", "Min volume"),
            ("midcap_15min_risky_stabilization_min_intel_score", "Min intel"),
        ),
    ),
    (
        "market_wide_reversal",
        "Market-wide reversal",
        (
            ("market_wide_reversal_enabled", "Enabled"),
            ("market_wide_reversal_core_votes", "Core votes"),
            ("market_wide_reversal_total_votes", "Total votes"),
            ("market_wide_reversal_min_range_atr_ratio", "Range ATR"),
            ("market_wide_reversal_min_range_atr_ratio_5min", "5m Range ATR"),
            ("market_wide_reversal_min_range_atr_ratio_15min", "15m Range ATR"),
            ("market_wide_reversal_midcap_min_range_atr_ratio_5min", "MIDCP 5m Range ATR"),
            ("market_wide_reversal_midcap_min_range_atr_ratio_15min", "MIDCP 15m Range ATR"),
            ("market_wide_reversal_min_close_location", "Close location"),
            ("market_wide_reversal_max_skew_seconds_5min", "5m max skew sec"),
            ("market_wide_reversal_max_skew_seconds_15min", "15m max skew sec"),
        ),
    ),
    (
        "volume_spike",
        "Volume spike and timing",
        (
            ("volume_spike_entry_enabled", "Enabled"),
            ("one_min_scalping_enabled", "1m scalp"),
            ("power_move_override_enabled", "Power move"),
            ("power_move_spike_bypass_15m", "Spike bypass 15m"),
            ("power_move_spike_range_atr_mult", "Spike range ATR mult"),
            ("volume_spike_entry_min_volume_ratio", "Volume spike ratio"),
            ("volume_spike_entry_strong_1min_min_volume_ratio", "Strong 1m volume"),
            ("volume_spike_entry_strong_1min_adx_threshold", "Strong 1m ADX"),
            ("volume_spike_entry_strong_1min_intel_threshold", "Strong 1m intel"),
            ("volume_spike_entry_min_range_atr_ratio", "Range ATR"),
            ("volume_spike_entry_min_close_location", "Close location"),
            ("volume_spike_entry_futures_sl_pct", "FUT SL %"),
            ("volume_spike_entry_options_sl_pct", "OPT SL %"),
            ("volume_spike_entry_1min_scalp_rr", "1m scalp RR"),
            ("volume_spike_entry_stabilization_seconds", "Stabilization sec"),
            ("live_signal_stabilization_seconds_15min", "15m stabilization sec"),
            ("ut_no_entry_after", "15m no-entry after"),
        ),
    ),
    (
        "fifteen_min_momentum",
        "15m momentum relief",
        (
            ("live_15min_momentum_override_enabled", "Enabled"),
            ("live_15min_momentum_min_confidence", "Min confidence"),
            ("live_15min_momentum_volume_ratio", "Min volume"),
            ("live_15min_momentum_range_atr_ratio", "Range ATR"),
            ("live_15min_momentum_close_location", "Close location"),
            ("live_15min_momentum_adx_threshold", "ADX"),
            ("live_15min_momentum_intel_threshold", "Intel"),
        ),
    ),
)

# ── Event taxonomy: (key, severity, compiled regex) ────────────────────────
# severity: 3=critical (investigate/fix), 2=warn (watch), 1=info (baseline)
EVENT_PATTERNS = [
    ("traceback",            3, re.compile(r"Traceback \(most recent call last\)")),
    ("critical_log",         3, re.compile(r"\|\s*CRITICAL\s*\|")),
    ("winerror32_oi",        2, re.compile(r"WinError 32.*option_oi_snapshots")),
    ("winerror32_other",     2, re.compile(r"WinError 32")),
    ("option_chain_error",   2, re.compile(r"optionchain response status:\s*error", re.I)),
    ("stale_chain_fallback", 2, re.compile(r"Using stale option chain")),
    ("fyers_token_issue",    2, re.compile(r"Fyers Token Issue|token failed pre-reconnect|Attempting refresh")),
    ("fyers_ws_reconnect",   2, re.compile(r"Fyers WS Reconnecting|Silent Disconnect")),
    ("fyers_auth_required",  3, re.compile(r"reconnect paused until token refresh|Fyers auth required")),
    ("broker_disconnect",    3, re.compile(r"Broker.*disconnect|AngelOne.*disconnect|broker connection lost", re.I)),
    ("recovery_required",    3, re.compile(r"RECOVERY_REQUIRED|requires broker reconciliation|mark_recovery", re.I)),
    ("auto_flatten",         3, re.compile(r"auto.?flatten|RECOVERY_AUTO_FLATTEN|emergency_flatten", re.I)),
    ("circuit_breaker",      3, re.compile(r"HALTING ALL TRADING|daily loss limit (?:breached|hit|reached)|circuit breaker (?:tripped|trip|halt)", re.I)),
    ("protective_armed",     1, re.compile(r"Broker SL-M armed|protective stop")),
    ("trade_logged",         1, re.compile(r"TRADE LOGGED")),
    ("broker_order_ok",      1, re.compile(r"Broker order placed successfully")),
    ("broker_order_fail",    3, re.compile(r"Broker entry failed|entry acknowledgement ambiguous|BROKER_ERROR", re.I)),
    ("trade_closed",         1, re.compile(r"Trade closed|EXIT_FILLED|exit_reason=|position closed", re.I)),
    ("signal_repaint_abort", 2, re.compile(r"INTRA-CANDLE REPAINT ABORT")),
    ("safe_mode_block",      1, re.compile(r"blocked by safe mode|REAL mode blocked|outside REAL arming window")),
    # Session-readiness / feed-health recognizers (added after 2026-07-01: these
    # dominated a session yet were previously buried as generic CRITICAL/noise).
    ("broker_creds_missing",  3, re.compile(r"Missing Fyers credentials|Cannot refresh .* token|Missing .*credentials in \.env", re.I)),
    ("capital_misconfig",     3, re.compile(r"segment capital exceeds total capital")),
    ("broker_rate_limited",   2, re.compile(r"exceeding access rate|access rate|too many requests", re.I)),
    ("delayed_data_fallback", 2, re.compile(r"Using .*delayed .*data for", re.I)),
    ("entry_dropped_no_quote",2, re.compile(r"no verified LTP|no entry-eligible broker source is fresh", re.I)),
    ("signal_gen_timeout",    2, re.compile(r"Signal generation might be hanging|did not finish within|per-index budget", re.I)),
    ("generic_error",        2, re.compile(r"\|\s*ERROR\s*\|")),
]
# generic_error is a catch-all: if a line also matches a specific pattern, the
# specific one wins and generic_error is suppressed (avoids double-count/noise).

LINE_TS = re.compile(r"^(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})")
# loguru source locator e.g. "scanner:_process_instrument:4330"
ERR_SRC = re.compile(r"\|\s*(?:ERROR|CRITICAL)\s*\|\s*([\w.]+:[\w<>]+:\d+)")
LOG_LEVEL = re.compile(r"\|\s*(WARNING|ERROR|CRITICAL)\s*\|")
VOLATILE_NUMBERS = re.compile(r"\b\d+(?:\.\d+)?\b")
TEST_ARTIFACT = re.compile(
    r"TESTFUT|KILL_ALL_TEST|MANUAL_RETRY|test order failed|pytest-of-|"
    r"test_live_session_tracker|Signal vanished for T2\b|ut1-test",
    re.I,
)
STARTUP_MARKER = re.compile(r"\|\s*(?:__main__|main):main:\d+\s+-\s+Starting dashboard server")


def now_ist() -> datetime:
    return datetime.now(IST)


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(float(value))
    except Exception:
        return default


def _process_is_running(pid: int) -> bool:
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


def _compact_payload(payload: Any, max_string: int = 240) -> Any:
    """Keep tracker state useful without dumping huge dashboard payloads."""
    if isinstance(payload, dict):
        return {str(k): _compact_payload(v, max_string) for k, v in payload.items()}
    if isinstance(payload, list):
        return [_compact_payload(v, max_string) for v in payload[:25]]
    if isinstance(payload, str) and len(payload) > max_string:
        return payload[: max_string - 3] + "..."
    return payload


class TrackerRunLock:
    """File-backed single-writer guard for the read-only tracker process."""

    def __init__(self, args):
        self.args = args
        self.pid = os.getpid()
        self.run_id = uuid.uuid4().hex[:12]
        self.lock_file = OUT_DIR / LOCK_FILE_NAME
        self.pid_file = OUT_DIR / PID_FILE_NAME
        self.owner: dict[str, Any] | None = None
        self.acquired = False

    def _read_owner(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.lock_file.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}

    def _write_owner(self, fd: int) -> None:
        payload = {
            "pid": self.pid,
            "run_id": self.run_id,
            "started_at": now_ist().isoformat(),
            "mode": "watch" if getattr(self.args, "watch", False) else "once",
            "since": getattr(self.args, "since", None),
            "until": getattr(self.args, "until", None),
            "interval": getattr(self.args, "interval", None),
        }
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)

    def acquire(self) -> bool:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(str(self.lock_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                owner = self._read_owner()
                owner_pid = _coerce_int(owner.get("pid"))
                if not _process_is_running(owner_pid):
                    self.lock_file.unlink(missing_ok=True)
                    self.pid_file.unlink(missing_ok=True)
                    continue
                self.owner = owner
                return False
            self._write_owner(fd)
            self.pid_file.write_text(str(self.pid), encoding="utf-8")
            self.acquired = True
            return True
        return False

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            owner = self._read_owner()
            if (
                _coerce_int(owner.get("pid")) == self.pid
                and str(owner.get("run_id")) == self.run_id
            ):
                self.lock_file.unlink(missing_ok=True)
        finally:
            try:
                if self.pid_file.read_text(encoding="utf-8").strip() == str(self.pid):
                    self.pid_file.unlink(missing_ok=True)
            except FileNotFoundError:
                pass
            except Exception:
                pass


_DASHBOARD_TOKEN_CACHE: str = ""
_DASHBOARD_TOKEN_FETCHED_AT: float = 0.0


def _resolve_dashboard_token(*, force: bool = False) -> str:
    """Load dashboard token for sensitive read APIs.

    Prefer env/settings when set; otherwise fetch the runtime token from the
    loopback-only /api/client_config endpoint (same path the UI uses).
    """
    global _DASHBOARD_TOKEN_CACHE, _DASHBOARD_TOKEN_FETCHED_AT
    now = time.time()
    if (
        not force
        and _DASHBOARD_TOKEN_CACHE
        and (now - float(_DASHBOARD_TOKEN_FETCHED_AT or 0.0)) < 300.0
    ):
        return _DASHBOARD_TOKEN_CACHE

    token = (
        str(os.getenv("DASHBOARD_AUTH_TOKEN", "") or "").strip()
        or str(os.getenv("UT_DASHBOARD_TOKEN", "") or "").strip()
    )
    if not token:
        try:
            from config.settings import get_settings

            token = str(getattr(get_settings(), "dashboard_auth_token", "") or "").strip()
        except Exception:
            token = ""
    if not token:
        try:
            req = urllib.request.Request(
                f"{API}/api/client_config",
                headers={"Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=4.0) as r:
                payload = json.loads(r.read().decode("utf-8", "replace"))
            token = str((payload or {}).get("dashboard_token") or "").strip()
        except Exception:
            token = ""

    _DASHBOARD_TOKEN_CACHE = token
    _DASHBOARD_TOKEN_FETCHED_AT = now
    return token


def api_get(path: str, timeout: float = 6.0):
    """GET a dashboard API path. Sensitive routes require x-dashboard-token."""
    try:
        headers = {"Accept": "application/json"}
        # /api/status is intentionally token-optional; still attach token when known.
        token = _resolve_dashboard_token()
        if token:
            headers["x-dashboard-token"] = token
        req = urllib.request.Request(f"{API}{path}", headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        err = str(e)
        # One refresh if auth failed (runtime token rotated on process restart).
        if "401" in err or "Unauthorized" in err:
            try:
                token = _resolve_dashboard_token(force=True)
                if token:
                    req = urllib.request.Request(
                        f"{API}{path}",
                        headers={
                            "Accept": "application/json",
                            "x-dashboard-token": token,
                        },
                    )
                    with urllib.request.urlopen(req, timeout=timeout) as r:
                        return json.loads(r.read().decode("utf-8", "replace"))
            except Exception as retry_exc:
                return {"_error": str(retry_exc)}
        return {"_error": err}


class Tracker:
    def __init__(self, args, run_id: str | None = None):
        self.args = args
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        self.session_date = now_ist().strftime("%Y-%m-%d")
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.counts: dict[str, int] = defaultdict(int)
        self.in_session_counts: dict[str, int] = defaultdict(int)
        self.error_sources: dict[str, int] = defaultdict(int)          # source:func:line -> count (all day)
        self.error_sources_session: dict[str, int] = defaultdict(int)  # same, in-session only
        self.first_seen: dict[str, str] = {}
        self.last_seen: dict[str, str] = {}
        self.samples: dict[str, list] = defaultdict(list)
        self.severity = {k: s for k, s, _ in EVENT_PATTERNS}
        self.log_offset = 0
        self.poll_snapshots: list[dict] = []
        self.since_minutes = self._parse_hhmm_to_minutes(getattr(args, "since", None))
        self.api_status_seen = {"mode": set(), "scanner_running": set()}
        self.max_scanner_age = 0.0
        self.stale_scan_polls = 0
        self.poll_count = 0
        self.system_issues: dict[str, int] = defaultdict(int)
        self.system_issue_samples: dict[str, list[str]] = defaultdict(list)
        self.repeated_log_fingerprints: dict[str, int] = defaultdict(int)
        self.repeated_log_fingerprints_session: dict[str, int] = defaultdict(int)
        self.clean_repeated_log_fingerprints: dict[str, int] = defaultdict(int)
        self.clean_repeated_log_fingerprints_session: dict[str, int] = defaultdict(int)
        self.ignored_artifacts: dict[str, int] = defaultdict(int)
        self.clean_start_ts: str | None = None
        self.clean_start_reason: str = ""
        self.clean_counts: dict[str, int] = defaultdict(int)
        self.clean_in_session_counts: dict[str, int] = defaultdict(int)
        self.clean_error_sources: dict[str, int] = defaultdict(int)
        self.clean_error_sources_session: dict[str, int] = defaultdict(int)
        self.clean_samples: dict[str, list] = defaultdict(list)
        self.market_clean_start_ts: str | None = None
        self.market_clean_start_reason: str = ""
        self.market_clean_counts: dict[str, int] = defaultdict(int)
        self.market_clean_error_sources: dict[str, int] = defaultdict(int)
        self.market_clean_samples: dict[str, list] = defaultdict(list)
        self.market_clean_repeated_log_fingerprints: dict[str, int] = defaultdict(int)
        self.max_api_latency_ms = 0.0
        self.max_diag_latency_ms = 0.0
        self.max_queue_fill_pct = 0.0
        self.max_open_trades = 0
        self.max_closed_trades = 0
        self.max_session_rows = 0
        self.max_session_closed = 0
        self.latest_gateway_status: dict[str, Any] = {}
        self.latest_status_summary: dict[str, Any] = {}
        self.latest_diagnostics_summary: dict[str, Any] = {}
        self.latest_trade_contract: dict[str, Any] = {}
        self.latest_state_summary: dict[str, Any] = {}
        self.latest_feature_matrix: dict[str, Any] = {}
        self.latest_quad_prime_filters: dict[str, Any] = {}
        self.latest_market_direction: dict[str, Any] = {}
        self.latest_readiness_assessment: dict[str, Any] = {}
        self.max_engine_lab_rows = 0
        self.state_poll_count = 0
        self._prime_offset_to_today()

    def _prime_offset_to_today(self):
        """Baseline: scan today's existing lines, then continue incrementally."""
        if not LOG_FILE.exists():
            return
        with LOG_FILE.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                m = LINE_TS.match(line)
                if m and m.group(1) == self.session_date and self._after_since(m.group(2)):
                    self._classify(line)
            self.log_offset = f.tell()

    @staticmethod
    def _is_session_time(hhmmss: str) -> bool:
        try:
            mins = Tracker._hhmmss_to_minutes(hhmmss)
            return MARKET_START_MINUTES <= mins <= MARKET_END_MINUTES
        except Exception:
            return False

    @staticmethod
    def _hhmmss_to_minutes(hhmmss: str) -> int:
        h, m, *_ = (int(x) for x in hhmmss.split(":"))
        return h * 60 + m

    @staticmethod
    def _parse_hhmm_to_minutes(value: str | None) -> int | None:
        if not value:
            return None
        try:
            parts = str(value).strip().split(":")
            return int(parts[0]) * 60 + int(parts[1])
        except Exception:
            return None

    def _after_since(self, hhmmss: str) -> bool:
        if self.since_minutes is None:
            return True
        try:
            h, m, _ = (int(x) for x in hhmmss.split(":"))
            return h * 60 + m >= self.since_minutes
        except Exception:
            return True

    def _reset_clean_window(self, ts: str, reason: str):
        self.clean_start_ts = ts
        self.clean_start_reason = reason
        self.clean_counts.clear()
        self.clean_in_session_counts.clear()
        self.clean_error_sources.clear()
        self.clean_error_sources_session.clear()
        self.clean_samples.clear()
        self.clean_repeated_log_fingerprints.clear()
        self.clean_repeated_log_fingerprints_session.clear()

    def _reset_market_clean_window(self, ts: str, reason: str):
        self.market_clean_start_ts = ts
        self.market_clean_start_reason = reason
        self.market_clean_counts.clear()
        self.market_clean_error_sources.clear()
        self.market_clean_samples.clear()
        self.market_clean_repeated_log_fingerprints.clear()

    def _should_reset_market_clean_window(self, hhmmss: str | None) -> bool:
        if not hhmmss:
            return True
        try:
            return self._hhmmss_to_minutes(hhmmss) <= MARKET_END_MINUTES
        except Exception:
            return True

    def _classify(self, line: str):
        m = LINE_TS.match(line)
        ts = f"{m.group(1)} {m.group(2)}" if m else now_ist().strftime("%Y-%m-%d %H:%M:%S")
        if m and not self._after_since(m.group(2)):
            return
        if TEST_ARTIFACT.search(line):
            self.ignored_artifacts["test_harness_log"] += 1
            reason = "after latest test/mock artifact"
            self._reset_clean_window(ts, reason)
            if self._should_reset_market_clean_window(m.group(2) if m else None):
                self._reset_market_clean_window(ts, reason)
            return
        if STARTUP_MARKER.search(line):
            reason = "after latest runtime start"
            self._reset_clean_window(ts, reason)
            if self._should_reset_market_clean_window(m.group(2) if m else None):
                self._reset_market_clean_window(ts, reason)
            return
        in_session = self._is_session_time(m.group(2)) if m else False
        matched = [key for key, _sev, rx in EVENT_PATTERNS if rx.search(line)]
        if "generic_error" in matched and len(matched) > 1:
            matched.remove("generic_error")  # specific pattern wins over catch-all
        for key in matched:
            self.counts[key] += 1
            self.clean_counts[key] += 1
            if in_session:
                self.in_session_counts[key] += 1
                self.clean_in_session_counts[key] += 1
                self.market_clean_counts[key] += 1
            self.first_seen.setdefault(key, ts)
            self.last_seen[key] = ts
            if len(self.samples[key]) < 3:
                self.samples[key].append(line.strip()[:300])
            if len(self.clean_samples[key]) < 3:
                self.clean_samples[key].append(line.strip()[:300])
            if in_session and len(self.market_clean_samples[key]) < 3:
                self.market_clean_samples[key].append(line.strip()[:300])
        # Categorize every ERROR/CRITICAL by its source:func:line so uncategorized
        # failures self-surface (e.g. UnboundLocalError, API JSON-parse failures).
        em = ERR_SRC.search(line)
        if em:
            self.error_sources[em.group(1)] += 1
            self.clean_error_sources[em.group(1)] += 1
            if in_session:
                self.error_sources_session[em.group(1)] += 1
                self.clean_error_sources_session[em.group(1)] += 1
                self.market_clean_error_sources[em.group(1)] += 1
        if LOG_LEVEL.search(line):
            fp = self._fingerprint_log(line)
            self.repeated_log_fingerprints[fp] += 1
            if in_session:
                self.repeated_log_fingerprints_session[fp] += 1
            if self.repeated_log_fingerprints[fp] in (5, 15, 30):
                self._note_issue("repeated_log_noise", line.strip()[:220])
            self.clean_repeated_log_fingerprints[fp] += 1
            if in_session:
                self.clean_repeated_log_fingerprints_session[fp] += 1
                self.market_clean_repeated_log_fingerprints[fp] += 1

    @staticmethod
    def _fingerprint_log(line: str) -> str:
        line = LINE_TS.sub("", line).strip()
        line = re.sub(r"\s+", " ", line)
        line = VOLATILE_NUMBERS.sub("#", line)
        return line[:240]

    def _note_issue(self, key: str, sample: str | None = None):
        self.system_issues[key] += 1
        if sample and len(self.system_issue_samples[key]) < 4:
            self.system_issue_samples[key].append(str(sample)[:260])

    def _walk_payload(self, payload: Any, prefix: str = ""):
        if isinstance(payload, dict):
            for key, value in payload.items():
                path = f"{prefix}.{key}" if prefix else str(key)
                yield path, value
                yield from self._walk_payload(value, path)
        elif isinstance(payload, list):
            for idx, value in enumerate(payload[:25]):
                yield f"{prefix}[{idx}]", value

    def _audit_gateway_status(self, status: dict[str, Any]):
        gateway = status.get("gateway_status") or status.get("source_health") or {}
        if not isinstance(gateway, dict):
            return
        self.latest_gateway_status = gateway
        if gateway.get("broker_degraded"):
            self._note_issue("broker_degraded", gateway)
        if gateway.get("all_brokers_unavailable"):
            self._note_issue("all_brokers_unavailable", gateway)
        if gateway.get("fyers_auth_required"):
            self._note_issue("fyers_auth_required_api", gateway)
        freshest = gateway.get("freshest")
        if isinstance(freshest, dict) and str(freshest.get("latency_class", "")).upper() == "DELAYED":
            self._note_issue("delayed_market_source", freshest)

    def _audit_diagnostics_payload(self, diag: dict[str, Any]):
        for path, value in self._walk_payload(diag):
            leaf = path.rsplit(".", 1)[-1].lower()
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                numeric = float(value)
                if "latency" in leaf or leaf.endswith("_ms") or leaf == "last_ms":
                    self.max_diag_latency_ms = max(self.max_diag_latency_ms, numeric)
                    if numeric >= 5000:
                        self._note_issue("high_latency_metric", f"{path}={numeric:.0f}")
                if "queue_fill_pct" in leaf:
                    self.max_queue_fill_pct = max(self.max_queue_fill_pct, numeric)
                    if numeric >= 60:
                        self._note_issue("queue_backlog", f"{path}={numeric:.1f}%")
                if ("queue_size" in leaf or "queued_tasks" in leaf) and numeric >= 5:
                    self._note_issue("queue_backlog", f"{path}={numeric:.0f}")
                if "high_latency_events" in leaf and numeric > 0:
                    self._note_issue("high_latency_events", f"{path}={numeric:.0f}")
            elif isinstance(value, str):
                lowered = value.lower()
                if leaf == "status" and lowered in {"queued", "worker_running", "applying", "scan_running"}:
                    self._note_issue("recalculation_active", f"{path}={value}")
                if any(word in leaf for word in ("error", "warning")) and value:
                    self._note_issue("diagnostic_message", f"{path}={value}")
            elif isinstance(value, list) and value and any(word in leaf for word in ("warning", "error", "issue")):
                self._note_issue("diagnostic_message", f"{path}={value[:3]}")

    @staticmethod
    def _maybe_int(value: Any) -> int | None:
        if value in (None, ""):
            return None
        try:
            return int(float(value))
        except Exception:
            return None

    @staticmethod
    def _fmt_bool(value: Any) -> str:
        if value is True:
            return "yes"
        if value is False:
            return "no"
        return "n/a"

    @staticmethod
    def _fmt_value(value: Any, default: str = "n/a") -> str:
        if value in (None, ""):
            return default
        return str(value)

    def _summarize_status(self, status: dict[str, Any]) -> dict[str, Any]:
        gateway = status.get("gateway_status") or status.get("source_health") or {}
        if not isinstance(gateway, dict):
            gateway = {}
        config = status.get("config") if isinstance(status.get("config"), dict) else {}
        return {
            "status": status.get("status"),
            "mode": status.get("mode"),
            "scanner_running": status.get("scanner_running"),
            "scanner_engine": status.get("scanner_engine") or config.get("scanner_engine"),
            "grade_preference": status.get("grade_preference") or config.get("grade_preference"),
            "scan_count": status.get("scan_count"),
            "latency_ms": status.get("latency_ms"),
            "last_scan": status.get("last_scan"),
            "gateway_status": _compact_payload(
                {
                    "angel": gateway.get("angel"),
                    "angel_tick_age_seconds": gateway.get("angel_tick_age_seconds"),
                    "fyers": gateway.get("fyers"),
                    "fyers_tick_age_seconds": gateway.get("fyers_tick_age_seconds"),
                    "broker_degraded": gateway.get("broker_degraded"),
                    "all_brokers_unavailable": gateway.get("all_brokers_unavailable"),
                    "fyers_auth_required": gateway.get("fyers_auth_required"),
                    "fyers_refresh_disabled": gateway.get("fyers_refresh_disabled"),
                    "yahoo": gateway.get("yahoo"),
                    "yahoo_mode": gateway.get("yahoo_mode"),
                }
            ),
        }

    def _summarize_diagnostics(self, diag: dict[str, Any]) -> dict[str, Any]:
        keys = (
            "scanner_freshness",
            "latency",
            "worker_resources",
            "session_stats",
            "risk_configuration",
            "sources",
            "recalculation",
            "option_history",
            "option_history_quality",
            "option_history_bundle",
            "repaint_guard",
            "instrument_selection",
            "system_performance",
        )
        return {key: _compact_payload(diag.get(key)) for key in keys if key in diag}

    @staticmethod
    def _safe_float(value: Any, default: float = 0.0) -> float:
        try:
            if value in (None, ""):
                return default
            return float(value)
        except Exception:
            return default

    @staticmethod
    def _round_value(value: Any, digits: int = 2) -> Any:
        try:
            if value in (None, ""):
                return value
            return round(float(value), digits)
        except Exception:
            return value

    @staticmethod
    def _direction_vote(value: Any) -> str:
        text = str(value or "").strip().upper()
        if not text:
            return "FLAT"
        if any(word in text for word in ("BEAR", "SELL", "SHORT", "DOWN")):
            return "SHORT"
        if any(word in text for word in ("BULL", "BUY", "LONG", "UP")):
            return "LONG"
        return "FLAT"

    @staticmethod
    def _vote_from_sources(*values: Any) -> str:
        counts = {"LONG": 0, "SHORT": 0}
        for value in values:
            vote = Tracker._direction_vote(value)
            if vote in counts:
                counts[vote] += 1
        if counts["LONG"] > counts["SHORT"]:
            return "LONG"
        if counts["SHORT"] > counts["LONG"]:
            return "SHORT"
        return "FLAT"

    def _summarize_feature_matrix(self, config: dict[str, Any]) -> dict[str, Any]:
        matrix: dict[str, Any] = {}
        disabled: list[str] = []
        for key, label in SESSION_FEATURE_TOGGLES:
            enabled = config.get(key)
            if enabled is None:
                status = "unknown"
            else:
                status = "on" if bool(enabled) else "off"
            matrix[key] = {"label": label, "status": status}
            if status == "off":
                disabled.append(label)
                self._note_issue("feature_disabled", f"{label} ({key})")
        matrix["disabled"] = disabled
        matrix["enabled_count"] = sum(1 for v in matrix.values() if isinstance(v, dict) and v.get("status") == "on")
        matrix["known_count"] = sum(1 for v in matrix.values() if isinstance(v, dict) and v.get("status") in {"on", "off"})
        return matrix

    def _summarize_quad_prime_filters(self, config: dict[str, Any]) -> dict[str, Any]:
        groups: dict[str, Any] = {}
        missing: list[str] = []
        for group_key, group_label, fields in QUAD_PRIME_FILTER_GROUPS:
            rows = []
            for key, label in fields:
                present = key in config
                value = config.get(key) if present else None
                if not present:
                    missing.append(key)
                rows.append(
                    {
                        "key": key,
                        "label": label,
                        "value": value,
                        "present": present,
                    }
                )
            groups[group_key] = {"label": group_label, "settings": rows}
        if missing and len(missing) >= 6:
            self._note_issue(
                "quad_prime_filter_config_missing",
                f"missing={len(missing)} e.g. {', '.join(missing[:6])}",
            )
        return {
            "groups": groups,
            "missing": missing,
            "missing_count": len(missing),
            "known_count": sum(
                1
                for _, _, fields in QUAD_PRIME_FILTER_GROUPS
                for key, _ in fields
                if key in config
            ),
        }

    def _summarize_engine_lab(self, state: dict[str, Any]) -> dict[str, Any]:
        lab = state.get("engine_lab") if isinstance(state.get("engine_lab"), dict) else {}
        rows = lab.get("rows") if isinstance(lab.get("rows"), list) else []
        summary = lab.get("summary") if isinstance(lab.get("summary"), dict) else {}
        rows_summary = []
        for row in rows[:EXPECTED_ENGINE_LAB_ROWS]:
            if not isinstance(row, dict):
                continue
            total_points = row.get("total_points")
            if total_points in (None, ""):
                total_points = row.get("net_points")
            if total_points in (None, ""):
                total_points = row.get("raw_total_points")
            if total_points in (None, ""):
                total_points = row.get("raw_net_points")
            if total_points in (None, ""):
                total_points = row.get("open_points")
            if total_points in (None, ""):
                total_points = row.get("raw_open_points")
            total_money = row.get("total_money")
            rank_value = total_money if bool(row.get("actual_result")) and total_money not in (None, "") else total_points
            rows_summary.append(
                {
                    "engine": row.get("engine"),
                    "active": bool(row.get("active")),
                    "basis": row.get("display_basis"),
                    "signals": self._maybe_int(row.get("signals")) or 0,
                    "fresh": self._maybe_int(row.get("fresh_signals")) or 0,
                    "buy": self._maybe_int(row.get("buy")) or 0,
                    "sell": self._maybe_int(row.get("sell")) or 0,
                    "open": self._maybe_int(row.get("open")) or 0,
                    "closed": self._maybe_int(row.get("closed")) or 0,
                    "wins": self._maybe_int(row.get("wins")) or 0,
                    "losses": self._maybe_int(row.get("losses")) or 0,
                    "win_rate": self._round_value(row.get("win_rate")),
                    "total_points": self._round_value(total_points),
                    "total_money": self._round_value(total_money),
                    "rank_value": self._round_value(rank_value),
                    "profit_factor": self._round_value(row.get("profit_factor")),
                    "sharpe": self._round_value(row.get("sharpe")),
                    "errors": self._maybe_int(row.get("errors")) or 0,
                }
            )
        row_count = len(rows)
        self.max_engine_lab_rows = max(self.max_engine_lab_rows, row_count)
        enabled = bool(lab.get("enabled"))
        if enabled and row_count < EXPECTED_ENGINE_LAB_ROWS:
            self._note_issue("engine_lab_incomplete", f"rows={row_count}/{EXPECTED_ENGINE_LAB_ROWS}")
        if enabled and not rows:
            self._note_issue("engine_lab_missing", "enabled but no rows in /api/state")
        row_errors = sum(int(row.get("errors") or 0) for row in rows if isinstance(row, dict))
        if row_errors:
            self._note_issue("engine_lab_row_errors", f"row_errors={row_errors}")
        ranked = sorted(rows_summary, key=lambda r: self._safe_float(r.get("rank_value")), reverse=True)
        return {
            "enabled": enabled,
            "active_engine": lab.get("active_engine"),
            "rows": row_count,
            "expected_rows": EXPECTED_ENGINE_LAB_ROWS,
            "basis": summary.get("basis"),
            "signals": summary.get("signals"),
            "fresh_signals": summary.get("fresh_signals"),
            "open": summary.get("open"),
            "closed": summary.get("closed"),
            "latency_ms": summary.get("latency_ms"),
            "actual_deferred": summary.get("actual_deferred"),
            "row_errors": row_errors,
            "best": ranked[:3],
            "worst": list(reversed(ranked[-3:])) if ranked else [],
            "rows_summary": rows_summary,
        }

    def _summarize_market_direction(self, state: dict[str, Any]) -> dict[str, Any]:
        instruments = state.get("instruments") if isinstance(state.get("instruments"), dict) else {}
        rows: dict[str, Any] = {}
        missing_indices: list[str] = []
        missing_components: dict[str, list[str]] = {}
        votes = {"LONG": 0, "SHORT": 0, "FLAT": 0}

        for name in EXPECTED_INDICES:
            item = instruments.get(name)
            if not isinstance(item, dict):
                missing_indices.append(name)
                continue
            intel = item.get("intelligence") if isinstance(item.get("intelligence"), dict) else {}
            missing = [key for key in INTEL_COMPONENTS if not isinstance(intel.get(key), dict)]
            if missing:
                missing_components[name] = missing
            mtf = item.get("mtf") if isinstance(item.get("mtf"), dict) else {}
            aggregate = intel.get("aggregate") if isinstance(intel.get("aggregate"), dict) else {}
            regime = intel.get("regime") if isinstance(intel.get("regime"), dict) else {}
            volume = intel.get("volume") if isinstance(intel.get("volume"), dict) else {}
            oi = intel.get("oi") if isinstance(intel.get("oi"), dict) else {}
            pcr = intel.get("pcr") if isinstance(intel.get("pcr"), dict) else {}
            flow = intel.get("order_flow") if isinstance(intel.get("order_flow"), dict) else {}
            pre = item.get("pre_candle_prediction") if isinstance(item.get("pre_candle_prediction"), dict) else {}
            pre5 = pre.get("5min") if isinstance(pre.get("5min"), dict) else {}
            pre15 = pre.get("15min") if isinstance(pre.get("15min"), dict) else {}
            state5 = mtf.get("state_5min") if isinstance(mtf.get("state_5min"), dict) else {}
            state15 = mtf.get("state_15min") if isinstance(mtf.get("state_15min"), dict) else {}

            vote = self._vote_from_sources(
                mtf.get("confluence_signal"),
                aggregate.get("signal"),
                regime.get("direction") or regime.get("regime"),
                pre5.get("direction") if self._safe_float(pre5.get("confidence")) >= 0.55 else "",
                pre15.get("direction") if self._safe_float(pre15.get("confidence")) >= 0.55 else "",
            )
            votes[vote] = votes.get(vote, 0) + 1
            rows[name] = {
                "spot": self._round_value(item.get("spot_price") or item.get("ltp")),
                "change_pct": self._round_value(item.get("change_pct")),
                "mtf_signal": mtf.get("confluence_signal"),
                "mtf_score": self._round_value(mtf.get("confluence_score"), 3),
                "state_5min": state5.get("position_label"),
                "adx_5min": self._round_value(state5.get("adx_value"), 1),
                "state_15min": state15.get("position_label"),
                "adx_15min": self._round_value(state15.get("adx_value"), 1),
                "aggregate_signal": aggregate.get("signal"),
                "aggregate_score": self._round_value(aggregate.get("score"), 2),
                "regime": regime.get("regime") or regime.get("description"),
                "regime_direction": regime.get("direction") or regime.get("breakout_direction"),
                "regime_confidence": self._round_value(regime.get("confidence"), 1),
                "volume_ratio": self._round_value(volume.get("volume_ratio") or volume.get("relative_volume_ratio"), 2),
                "oi_signal": oi.get("signal") or oi.get("activity"),
                "pcr_signal": pcr.get("signal") or pcr.get("sentiment"),
                "pcr": self._round_value(pcr.get("primary_pcr") or pcr.get("pcr_oi"), 2),
                "order_flow": flow.get("signal") or flow.get("confirmation"),
                "order_ratio": self._round_value(flow.get("buy_sell_ratio") or flow.get("ratio"), 2),
                "pre_5min": {
                    "state": pre5.get("state"),
                    "direction": pre5.get("direction"),
                    "confidence": self._round_value(pre5.get("confidence"), 2),
                    "reason": pre5.get("reason"),
                },
                "pre_15min": {
                    "state": pre15.get("state"),
                    "direction": pre15.get("direction"),
                    "confidence": self._round_value(pre15.get("confidence"), 2),
                    "reason": pre15.get("reason"),
                },
                "vote": vote,
                "missing_intel": missing,
            }

        if missing_indices:
            self._note_issue("state_payload_gap", f"missing indices: {', '.join(missing_indices)}")
        if missing_components:
            sample = "; ".join(f"{k}:{','.join(v)}" for k, v in list(missing_components.items())[:3])
            self._note_issue("market_intel_missing", sample)
        pre_count = sum(
            1
            for row in rows.values()
            if row.get("pre_5min", {}).get("state") or row.get("pre_15min", {}).get("state")
        )
        if rows and pre_count < len(rows):
            self._note_issue("pre_candle_unavailable", f"{pre_count}/{len(rows)} indices have pre-candle state")

        if votes["LONG"] >= 3:
            broad = "BULLISH"
        elif votes["SHORT"] >= 3:
            broad = "BEARISH"
        elif votes["LONG"] == votes["SHORT"]:
            broad = "MIXED"
        elif votes["LONG"] > votes["SHORT"]:
            broad = "LEAN_BULLISH"
        elif votes["SHORT"] > votes["LONG"]:
            broad = "LEAN_BEARISH"
        else:
            broad = "FLAT"
        return {
            "broad_direction": broad,
            "votes": votes,
            "indices": rows,
            "missing_indices": missing_indices,
            "missing_components": missing_components,
        }

    def _summarize_activity_log(self, state: dict[str, Any]) -> dict[str, Any]:
        activity = state.get("activity_log") if isinstance(state.get("activity_log"), list) else []
        type_counts: dict[str, int] = defaultdict(int)
        recent = []
        for event in activity[:25]:
            if not isinstance(event, dict):
                continue
            event_type = str(event.get("type") or "unknown").lower()
            type_counts[event_type] += 1
            if len(recent) < 6:
                recent.append(
                    {
                        "time": event.get("time"),
                        "type": event_type,
                        "msg": str(event.get("msg") or "")[:180],
                    }
                )
        if type_counts.get("error"):
            self._note_issue("activity_error_alerts", f"recent activity errors={type_counts['error']}")
        return {"counts": dict(type_counts), "recent": recent, "total_visible": len(activity)}

    def _summarize_state_payload(self, state: dict[str, Any], status: dict[str, Any] | None = None) -> dict[str, Any]:
        if not isinstance(state, dict) or not state:
            self._note_issue("state_payload_gap", "empty /api/state payload")
            return {}
        status_config = (status or {}).get("config") if isinstance((status or {}).get("config"), dict) else {}
        state_config = state.get("config") if isinstance(state.get("config"), dict) else {}
        config = {**status_config, **state_config}
        self.latest_feature_matrix = self._summarize_feature_matrix(config)
        self.latest_quad_prime_filters = self._summarize_quad_prime_filters(config)
        self.latest_market_direction = self._summarize_market_direction(state)
        engine_lab = self._summarize_engine_lab(state)
        activity = self._summarize_activity_log(state)
        summary = {
            "timestamp": state.get("timestamp"),
            "mode": state.get("mode"),
            "market_status": _compact_payload(state.get("market_status")),
            "system_power": _compact_payload(state.get("system_power")),
            "scan_count": state.get("scan_count"),
            "latency": state.get("latency"),
            "is_calculating": state.get("is_calculating"),
            "engine_lab": engine_lab,
            "market_direction": self.latest_market_direction,
            "activity": activity,
            "feature_matrix": self.latest_feature_matrix,
            "quad_prime_filters": self.latest_quad_prime_filters,
        }
        return summary

    def _build_readiness_assessment(self) -> dict[str, Any]:
        status = self.latest_status_summary or {}
        diag = self.latest_diagnostics_summary or {}
        gateway = status.get("gateway_status") or {}
        sources = diag.get("sources") or gateway or {}
        freshness = diag.get("scanner_freshness") or {}
        risk = diag.get("risk_configuration") or {}
        latency = diag.get("latency") or {}
        option_quality = diag.get("option_history_quality") or {}
        trade_contract = self.latest_trade_contract or {}
        engine_lab = (self.latest_state_summary or {}).get("engine_lab") or {}
        feature_matrix = self.latest_feature_matrix or {}
        blockers: list[str] = []
        watch: list[str] = []

        if status.get("scanner_running") is False:
            blockers.append("scanner_running=false")
        if sources.get("all_brokers_unavailable"):
            blockers.append("all broker sources unavailable")
        if sources.get("fyers_auth_required"):
            blockers.append("Fyers auth required")
        if risk and risk.get("ok") is False:
            blockers.append("risk configuration not OK")
        risk_issues = risk.get("issues") if isinstance(risk.get("issues"), list) else []
        if risk_issues:
            blockers.append(f"risk issues={len(risk_issues)}")
        if trade_contract.get("status") == "MISMATCH":
            blockers.append("trade contract mismatch")
        if sources.get("broker_degraded"):
            watch.append("broker/source degraded")
        if freshness.get("is_stale"):
            watch.append("scanner freshness stale")
        if self._safe_float(latency.get("last_ms")) >= 2500:
            watch.append(f"scan latency {latency.get('last_ms')}ms")
        if option_quality and str(option_quality.get("status") or "").upper() not in {"", "OK", "GOOD"}:
            watch.append(f"option history quality {option_quality.get('status')}")
        if engine_lab.get("enabled") and int(engine_lab.get("rows") or 0) < EXPECTED_ENGINE_LAB_ROWS:
            watch.append(f"Engine Lab rows {engine_lab.get('rows')}/{EXPECTED_ENGINE_LAB_ROWS}")
        missing_components = (self.latest_market_direction or {}).get("missing_components") or {}
        if missing_components:
            watch.append(f"market-intel missing components on {len(missing_components)} index(es)")
        disabled_features = feature_matrix.get("disabled") if isinstance(feature_matrix, dict) else []
        if disabled_features:
            watch.append(f"{len(disabled_features)} session feature toggle(s) off")
        if self.system_issues.get("api_unreachable"):
            watch.append("dashboard API unreachable during tracker run")
        if self.system_issues.get("activity_error_alerts"):
            watch.append("recent Live Tape activity contains errors")

        score = 100 - (25 * len(blockers)) - (7 * len(watch))
        score = max(0, min(100, score))
        if blockers:
            status_label = "BLOCKED"
        elif watch:
            status_label = "WATCH"
        else:
            status_label = "READY"
        return {
            "status": status_label,
            "score": score,
            "blockers": blockers,
            "watch": watch,
        }

    def _summarize_trade_contract(
        self,
        trades: dict[str, Any],
        diag: dict[str, Any] | None,
        status: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        open_rows = trades.get("open", []) or trades.get("open_trades", []) or []
        closed_rows = trades.get("closed", []) or trades.get("closed_trades", []) or []
        if not isinstance(open_rows, list):
            open_rows = []
        if not isinstance(closed_rows, list):
            closed_rows = []
        signal_rows = trades.get("signals", [])
        if not isinstance(signal_rows, list):
            signal_rows = []

        api_open_total = self._maybe_int(trades.get("open_total"))
        api_closed_total = self._maybe_int(trades.get("closed_total"))
        api_signals_total = self._maybe_int(trades.get("signals_total"))
        if api_open_total is None:
            api_open_total = len(open_rows)
        if api_closed_total is None:
            api_closed_total = len(closed_rows)
        if api_signals_total is None:
            api_signals_total = len(signal_rows) if signal_rows else api_open_total + api_closed_total

        api_rows_total = int(api_open_total or 0) + int(api_closed_total or 0)
        visible_rows = open_rows + closed_rows
        visible_signal_rows = len(signal_rows)
        signal_exit_rows = sum(
            1
            for row in signal_rows
            if str(row.get("action", "") or "").upper() == "EXIT" or bool(row.get("is_exit"))
        )
        live_rows = sum(1 for row in visible_rows if str(row.get("id", "")).startswith("SESSION|"))
        historical_rows = sum(1 for row in visible_rows if str(row.get("id", "")).startswith("H_"))

        stats = (diag or {}).get("session_stats") if isinstance(diag, dict) else {}
        if not isinstance(stats, dict):
            stats = {}
        diag_entries = self._maybe_int(stats.get("entries"))
        diag_exits = self._maybe_int(stats.get("exits"))
        meta = trades.get("meta") if isinstance(trades, dict) else {}
        if not isinstance(meta, dict):
            meta = {}
        source = str(meta.get("source") or "").lower()
        mode = str(meta.get("mode") or (status or {}).get("mode") or "").upper()
        building_source = source == "historical_building_guard"
        mixed_historical_source = source == "historical_session_plus_simulation"
        historical_trade_manager_source = source == "historical_trade_manager"
        restored_signal_source = source == "restored_session_signals"
        restored_signal_rows = self._maybe_int(meta.get("restored_signal_rows"))
        simulated_signal_rows = self._maybe_int(meta.get("simulated_signal_rows"))
        comparison_rows_total = api_rows_total
        comparison_closed_total = api_closed_total
        report_rows_total = api_rows_total
        report_closed_total = api_closed_total
        session_rows_total = self._maybe_int(
            trades.get("session_rows_total", meta.get("session_rows_total"))
        )
        session_closed_total = self._maybe_int(
            trades.get("session_closed_total", meta.get("session_closed_total"))
        )
        if session_rows_total is not None and not api_rows_total:
            # Authoritative session-row contract (added 2026-07-02): trust it over
            # heuristic reconstruction from signals/meta.
            comparison_rows_total = session_rows_total
            comparison_closed_total = (
                session_closed_total if session_closed_total is not None else session_rows_total
            )
            report_rows_total = comparison_rows_total
            report_closed_total = comparison_closed_total
        elif restored_signal_source and not api_rows_total:
            restored_count = restored_signal_rows
            if restored_count is None:
                restored_count = api_signals_total
            comparison_rows_total = int(restored_count or 0)
            comparison_closed_total = int(
                signal_exit_rows
                if signal_exit_rows
                else (restored_count or 0)
            )
            report_rows_total = comparison_rows_total
            report_closed_total = comparison_closed_total
        if mixed_historical_source:
            restored_count = restored_signal_rows
            if restored_count is None:
                restored_count = live_rows
            comparison_rows_total = int(restored_count or 0)
            comparison_closed_total = int(restored_count or 0)
        elif historical_trade_manager_source or (
            mode == "HISTORICAL" and historical_rows > 0 and live_rows == 0 and not restored_signal_rows
        ):
            # Pure backtest rows are intentionally separate from today's live
            # session ledger. diagnostics.session_stats stays live-session
            # scoped, so comparing it here creates a false post-replay mismatch.
            comparison_rows_total = api_rows_total
            comparison_closed_total = api_closed_total

        mismatches: list[str] = []
        skip_session_stats_compare = (
            historical_trade_manager_source
            or (
                mode == "HISTORICAL"
                and historical_rows > 0
                and live_rows == 0
                and not restored_signal_rows
            )
        )
        if not building_source:
            if not skip_session_stats_compare and diag_entries is not None and diag_entries != comparison_rows_total:
                mismatches.append(f"diagnostics.entries={diag_entries} vs session_rows={comparison_rows_total}")
            if not skip_session_stats_compare and diag_exits is not None and comparison_closed_total is not None and diag_exits != comparison_closed_total:
                mismatches.append(f"diagnostics.exits={diag_exits} vs session_closed={comparison_closed_total}")
            if api_open_total is not None and api_open_total < len(open_rows):
                mismatches.append(f"trades.open_total={api_open_total} below visible open rows={len(open_rows)}")
            if api_closed_total is not None and api_closed_total < len(closed_rows):
                mismatches.append(f"trades.closed_total={api_closed_total} below visible closed rows={len(closed_rows)}")

        return {
            "status": "BUILDING" if building_source else ("MISMATCH" if mismatches else "OK"),
            "mismatches": mismatches,
            "source": meta.get("source"),
            "mode": mode,
            "comparison_basis": "historical_rows" if skip_session_stats_compare else "session_stats",
            "diagnostics_entries": diag_entries,
            "diagnostics_exits": diag_exits,
            "trades_signals_total": api_signals_total,
            "trades_rows_total": report_rows_total,
            "trades_open_total": api_open_total,
            "trades_closed_total": report_closed_total,
            "raw_open_closed_rows_total": api_rows_total,
            "visible_signal_rows": visible_signal_rows,
            "comparison_rows_total": comparison_rows_total,
            "comparison_closed_total": comparison_closed_total,
            "restored_signal_rows": restored_signal_rows,
            "simulated_signal_rows": simulated_signal_rows,
            "visible_open_rows": len(open_rows),
            "visible_closed_rows": len(closed_rows),
            "visible_live_session_rows": live_rows,
            "visible_historical_rows": historical_rows,
        }

    def _read_new_log(self):
        if not LOG_FILE.exists():
            return
        size = LOG_FILE.stat().st_size
        if size < self.log_offset:  # rotated/truncated
            self.log_offset = 0
        with LOG_FILE.open("r", encoding="utf-8", errors="replace") as f:
            f.seek(self.log_offset)
            for line in f:
                self._classify(line)
            self.log_offset = f.tell()

    def _poll_api(self):
        def timed_api(path: str, timeout: float = 6.0):
            start = time.perf_counter()
            try:
                payload = api_get(path, timeout=timeout)
            except Exception as e:
                payload = {"_error": str(e)}
            elapsed_ms = (time.perf_counter() - start) * 1000
            self.max_api_latency_ms = max(self.max_api_latency_ms, elapsed_ms)
            if elapsed_ms >= 2500:
                self._note_issue("api_poll_slow", f"{path} took {elapsed_ms:.0f}ms")
            return payload

        status = timed_api("/api/status")
        diag = timed_api("/api/diagnostics")
        trades = timed_api("/api/trades")
        state: dict[str, Any] = {}
        snap = {"t": now_ist().isoformat(), "run_id": self.run_id}
        if "_error" not in status:
            self.latest_status_summary = self._summarize_status(status)
            snap["mode"] = status.get("mode")
            snap["scanner_running"] = status.get("scanner_running")
            snap["scan_count"] = status.get("scan_count")
            config = status.get("config") if isinstance(status.get("config"), dict) else {}
            snap["scanner_engine"] = status.get("scanner_engine") or config.get("scanner_engine")
            self.api_status_seen["mode"].add(str(status.get("mode")))
            self.api_status_seen["scanner_running"].add(str(status.get("scanner_running")))
            if status.get("scanner_running") is False:
                self._note_issue("scanner_not_running", status)
            self._audit_gateway_status(status)
        else:
            snap["api_status_error"] = status["_error"]
            self.counts["api_unreachable"] += 1
            self._note_issue("api_unreachable", status["_error"])
        if "_error" not in diag:
            self.latest_diagnostics_summary = self._summarize_diagnostics(diag)
            fr = diag.get("scanner_freshness", {}) or {}
            recalc = diag.get("recalculation", {}) or {}
            age = float(fr.get("age_seconds") or 0.0)
            self.max_scanner_age = max(self.max_scanner_age, age)
            historical_idle = (
                str(snap.get("mode") or "").upper() == "HISTORICAL"
                and str(recalc.get("status") or "").lower() in ("", "idle")
                and not bool(fr.get("is_calculating"))
                and not bool(fr.get("calculation_lock"))
            )
            if fr.get("is_stale") and not historical_idle:
                self.stale_scan_polls += 1
                self._note_issue("scanner_stale", fr)
            if age >= 10 and not historical_idle:
                self._note_issue("scanner_lag", f"scanner_age={age:.1f}s")
            oh = diag.get("option_history", {}) or {}
            rc = diag.get("risk_configuration", {}) or {}
            snap["scanner_age_s"] = age
            snap["scanner_stale"] = bool(fr.get("is_stale"))
            snap["opt_fallback_to_fut"] = oh.get("fallback_to_fut")
            snap["risk_warnings"] = rc.get("warnings")
            if rc.get("warnings"):
                self.counts["risk_config_warning"] += 1
                self._note_issue("risk_config_warning", rc.get("warnings"))
            self._audit_diagnostics_payload(diag)
        else:
            self._note_issue("api_unreachable", f"/api/diagnostics {diag['_error']}")
        if isinstance(trades, dict) and "_error" not in trades:
            open_trades = len(trades.get("open", []) or trades.get("open_trades", []) or [])
            closed_trades = len(trades.get("closed", []) or trades.get("closed_trades", []) or [])
            self.latest_trade_contract = self._summarize_trade_contract(
                trades,
                diag if isinstance(diag, dict) and "_error" not in diag else None,
                status if isinstance(status, dict) and "_error" not in status else None,
            )
            snap["open_trades"] = open_trades
            snap["closed_trades"] = closed_trades
            snap["trade_contract"] = self.latest_trade_contract
            self.max_open_trades = max(self.max_open_trades, open_trades)
            self.max_closed_trades = max(self.max_closed_trades, closed_trades)
            contract = self.latest_trade_contract or {}
            self.max_session_rows = max(
                self.max_session_rows, int(contract.get("trades_rows_total") or 0)
            )
            self.max_session_closed = max(
                self.max_session_closed, int(contract.get("trades_closed_total") or 0)
            )
            if self.latest_trade_contract.get("mismatches"):
                self._note_issue(
                    "trade_contract_mismatch",
                    "; ".join(self.latest_trade_contract.get("mismatches", [])[:3]),
                )
            if open_trades and self.latest_gateway_status.get("all_brokers_unavailable"):
                self._note_issue("open_trades_without_broker_sources", self.latest_gateway_status)
        elif isinstance(trades, dict):
            self._note_issue("api_unreachable", f"/api/trades {trades.get('_error')}")
        else:
            self._note_issue("trades_payload_gap", type(trades).__name__)
        if bool(getattr(self.args, "track_state", False)):
            state = timed_api("/api/state", timeout=8.0)
            self.state_poll_count += 1
            if isinstance(state, dict) and "_error" not in state:
                self.latest_state_summary = self._summarize_state_payload(
                    state,
                    status if isinstance(status, dict) and "_error" not in status else None,
                )
                market_direction = self.latest_market_direction or {}
                engine_lab = (self.latest_state_summary or {}).get("engine_lab") or {}
                snap["broad_direction"] = market_direction.get("broad_direction")
                snap["direction_votes"] = market_direction.get("votes")
                snap["engine_lab_rows"] = engine_lab.get("rows")
                snap["engine_lab_active"] = engine_lab.get("active_engine")
            else:
                error = state.get("_error") if isinstance(state, dict) else type(state).__name__
                self._note_issue("state_payload_unreachable", f"/api/state {error}")
        self.latest_readiness_assessment = self._build_readiness_assessment()
        snap["readiness"] = self.latest_readiness_assessment
        self.poll_snapshots.append(snap)
        self.poll_count += 1

    # ── reporting ──────────────────────────────────────────────────────
    def _suggestions(self) -> list[tuple[int, str]]:
        s: list[tuple[int, str]] = []
        c = self.counts
        cc = self.clean_counts
        sc = self.in_session_counts  # market-hours subset (09:15-15:30), full-day (may contain test noise)
        mc = self.market_clean_counts  # market-hours subset since the clean boundary (test noise excluded)
        if cc.get("traceback") or cc.get("critical_log"):
            s.append((3, f"**Code fix:** clean-window has {cc.get('traceback',0)} traceback(s) and "
                         f"{cc.get('critical_log',0)} CRITICAL log(s) since the latest clean boundary — inspect samples below; "
                         f"these are current in-process exceptions, not graceful degradations."))
        elif c.get("traceback") or c.get("critical_log"):
            s.append((1, f"**Legacy code noise:** full-day has {c.get('traceback',0)} traceback(s) and "
                         f"{c.get('critical_log',0)} CRITICAL log(s), but none in the clean window since the latest clean boundary. "
                         f"Treat as fixed/stale unless it reappears."))
        if c.get("winerror32_oi"):
            sess_note = f" ({sc.get('winerror32_oi',0)} in-session)" if sc.get("winerror32_oi") else " (none in-session so far)"
            s.append((2, f"**Data-persistence (WinError 32 ×{c['winerror32_oi']}{sess_note}):** `option_oi_snapshots.json` atomic-rename is "
                         f"losing to a concurrent reader. Add a short retry/backoff around the os.replace in "
                         f"`_save_option_oi_snapshots`, or write per-PID temp + retry. Non-order-path; data loss is one snapshot tick."))
        if c.get("option_chain_error") or c.get("stale_chain_fallback"):
            insess = sc.get("option_chain_error", 0) + sc.get("stale_chain_fallback", 0)
            if insess == 0:
                s.append((1, f"**Option-chain (pre/post-market only):** {c.get('option_chain_error',0)} chain error(s) / "
                             f"{c.get('stale_chain_fallback',0)} stale-fallback(s), all OUTSIDE 09:15-15:30 — expected (no live chain when "
                             f"market closed). Re-check after the session for any in-session occurrences."))
            else:
                s.append((2, f"**Option-chain resilience (IN-SESSION):** {sc.get('option_chain_error',0)} chain error(s) → "
                             f"{sc.get('stale_chain_fallback',0)} stale-fallback(s) during market hours. If these cluster near entries, "
                             f"tune the error-cooldown / add a faster retry before falling back, and watch Fyers rate limits."))
        if cc.get("fyers_auth_required"):
            s.append((3, f"**Feed continuity:** Fyers auth/refresh halted the WS ×{cc['fyers_auth_required']} in the clean window. If this "
                         f"fires in-session the data feed goes dark — confirm token auto-refresh succeeded; if not, re-auth via /api/fyers_login."))
        elif c.get("fyers_auth_required"):
            s.append((1, f"**Feed continuity (stale/test noise):** Fyers auth events ×{c['fyers_auth_required']} occurred only before the "
                         f"clean boundary (test runs or a restarted runtime). Treat as historical unless it reappears in the clean window."))
        # ── Session-readiness diagnoses (root causes of a session that could not trade) ──
        # Clean-window counts only: full-day counts include test-suite log artifacts
        # (mock broker/capital fixtures) that previously produced false CRITICALs here.
        if cc.get("broker_creds_missing"):
            s.append((3, f"**🔴 SESSION READINESS — broker auth broken (×{cc['broker_creds_missing']}):** a broker credential is "
                         f"missing/invalid so token auto-refresh failed. This cascades into WS drops, delayed-data fallback, and dropped "
                         f"entries. Populate the credential in .env (e.g. FYERS_PASSWORD) and re-verify auth BEFORE the next session."))
        elif c.get("broker_creds_missing"):
            s.append((1, f"**Broker-auth noise (pre-boundary only ×{c['broker_creds_missing']}):** credential errors appeared only before "
                         f"the clean boundary — typically test-suite fixtures. Verify once, then ignore unless seen in the clean window."))
        if cc.get("capital_misconfig"):
            s.append((3, f"**🔴 SESSION READINESS — REAL execution blocked (×{cc['capital_misconfig']}):** risk/capital config is invalid "
                         f"(segment capital exceeds total). Auto-execution stays blocked the whole session. Set "
                         f"capital_fut + capital_opt ≤ capital_total, then re-arm REAL."))
        elif c.get("capital_misconfig"):
            s.append((1, f"**Capital-config noise (pre-boundary only ×{c['capital_misconfig']}):** capital misconfig events appeared only "
                         f"before the clean boundary — typically test-suite fixtures. Live risk_configuration was OK on polls."))
        if mc.get("entry_dropped_no_quote"):
            s.append((2, f"**Signals lost to quote failures (IN-SESSION ×{mc['entry_dropped_no_quote']}):** live entries were skipped because "
                         f"the option/underlying quote could not be verified — each is a missed trade. Stabilize the broker feed/token and "
                         f"consider a short quote-retry before dropping the candidate."))
        elif sc.get("entry_dropped_no_quote"):
            s.append((1, f"**Quote-failure noise (pre-boundary ×{sc['entry_dropped_no_quote']}):** in-session quote failures occurred only "
                         f"before the clean boundary; re-check the next clean session before tuning."))
        if mc.get("delayed_data_fallback"):
            s.append((2, f"**Delayed-data windows (IN-SESSION ×{mc['delayed_data_fallback']}):** an index fell back to delayed data (entries "
                         f"correctly held to context-only). Frequent occurrences mean the live feed is unstable — check broker WS/token health."))
        if mc.get("broker_rate_limited"):
            s.append((2, f"**Broker rate-limiting (IN-SESSION ×{mc['broker_rate_limited']}):** REST calls hit the broker access-rate wall. "
                         f"Reduce poll cadence (positions/LTP), skip position sync when flat, or push order/position updates over a socket; "
                         f"confirm exponential backoff is engaging."))
        if mc.get("signal_gen_timeout"):
            s.append((2, f"**Signal-gen timeouts (IN-SESSION ×{mc['signal_gen_timeout']}):** signal generation hung for an instrument — late/stale "
                         f"signals during those windows. Profile scan-cycle latency, recalculation contention, and CPU/disk pressure."))
        if cc.get("broker_order_fail"):
            s.append((3, f"**Order path:** {cc['broker_order_fail']} clean-window broker entry failure/ambiguous-ack event(s) — review each; "
                         f"ambiguous acks should have forced RECOVERY_REQUIRED reconciliation."))
        elif c.get("broker_order_fail"):
            s.append((1, f"**Order path:** full-day broker failures were present, but no clean-window broker failures remain after "
                         f"test-harness filtering. Do not treat TESTFUT failures as live-market failures."))
        if cc.get("auto_flatten") or cc.get("recovery_required"):
            s.append((3, f"**Protection events:** clean-window recovery_required ×{cc.get('recovery_required',0)}, "
                         f"auto_flatten ×{cc.get('auto_flatten',0)} — "
                         f"expected safety behaviour, but each means a position lacked exchange-side protection briefly. Confirm cause."))
        if cc.get("circuit_breaker"):
            s.append((3, f"**Risk:** clean-window circuit-breaker/daily-loss event ×{cc['circuit_breaker']} — verify the trip threshold matched intended budget."))
        if self.stale_scan_polls:
            s.append((2, f"**Scanner freshness:** scanner reported stale on {self.stale_scan_polls}/{self.poll_count} polls "
                         f"(max age {self.max_scanner_age:.1f}s). If frequent in-session, check recalculation contention / CPU."))
        if c.get("api_unreachable"):
            s.append((2, f"**Observability:** dashboard API unreachable on {c['api_unreachable']} poll(s) — service may have restarted; "
                         f"verify uptime on port 8001."))
        if c.get("risk_config_warning"):
            s.append((2, "**Config:** risk_configuration reported warnings during the session — re-check capital/budget vs max_trade_loss_abs."))
        clean_repeated_count = sum(
            1 for count in self.market_clean_repeated_log_fingerprints.values() if count >= 5
        )
        if clean_repeated_count:
            s.append((2, f"**Redundancy/noise:** repeated warning/error lines are present in the market-hours clean window. "
                         f"Collapse or rate-limit noisy logs so real bugs are visible. Observed ×{clean_repeated_count}."))
        elif self.system_issues.get("repeated_log_noise"):
            s.append((1, f"**Legacy redundancy/noise:** repeated warning/error lines occurred earlier in the day, "
                         f"but none repeated in the clean window. Treat as historical unless it reappears."))
        issue_suggestions = {
            "api_poll_slow": (2, "**Bottleneck:** tracker observed slow dashboard API polls. If this clusters with scan latency, profile scanner cycle time and dashboard serialization."),
            "high_latency_metric": (2, "**Latency:** diagnostics reported high latency metrics. Check scan cycle duration, broker API waits, and recalculation contention."),
            "queue_backlog": (2, "**Queue pressure:** async/recalculation queue backlog appeared. If persistent, exits/orders can lag behind signals; inspect worker resources and broker retry loops."),
            "scanner_not_running": (3, "**Core function:** scanner_running was false while tracker was active. If during market hours, the system is not analysing live market."),
            "scanner_stale": (2, "**Scanner freshness:** scanner freshness flagged stale. Confirm feed arrival, candle build, and recalculation locks."),
            "scanner_lag": (2, "**Scanner lag:** scanner age exceeded expected tick cadence. Check data-fetch fanout and CPU pressure."),
            "broker_degraded": (2, "**Feed/broker:** broker/source health degraded. The system may still run, but quote/execution confidence is reduced."),
            "all_brokers_unavailable": (3, "**Feed/broker:** all broker sources were unavailable. REAL trading should stay blocked or recover before entries."),
            "fyers_auth_required_api": (3, "**Fyers auth:** API reported auth required. Refresh token before live session, then confirm WS reconnect."),
            "delayed_market_source": (2, "**Market data:** freshest source was delayed. Avoid entries until real-time source resumes."),
            "risk_config_warning": (2, "**Risk config:** dashboard diagnostics reported risk warnings. Fix before live trading."),
            "diagnostic_message": (2, "**Diagnostics:** warning/error fields appeared in /api/diagnostics. Review samples in the state JSON."),
            "recalculation_active": (1, "**Background work:** recalculation was active. Normal if short; a long run during session can create stale candles/signals."),
            "open_trades_without_broker_sources": (3, "**Consistency bug:** open trades existed while all broker sources were unavailable. Reconciliation/auto-flatten coverage must be checked."),
            "trades_payload_gap": (2, "**Dashboard contract:** /api/trades payload was not in the expected shape. Fix the API contract or tracker parser."),
            "trade_contract_mismatch": (3, "**Dashboard contract:** /api/trades totals disagreed with /api/diagnostics.session_stats. Fix the API contract, tracker parser, or session stats source before relying on tracker trade counts."),
            "state_payload_unreachable": (2, "**Observability:** /api/state was unavailable or incomplete, so market-intel/engine-lab coverage could not be audited."),
            "state_payload_gap": (2, "**Dashboard state:** /api/state missed expected live dashboard fields or index rows."),
            "market_intel_missing": (2, "**Market intelligence:** one or more indices missed required intel components (volume/OI/PCR/regime/order-flow/aggregate)."),
            "pre_candle_unavailable": (1, "**Pre-candle tracker:** one or more index/timeframe rows had no pre-candle state. Watch during 09:15-15:30 before tuning."),
            "engine_lab_missing": (2, "**Engine Lab:** Engine Lab is enabled but returned no rows."),
            "engine_lab_incomplete": (2, "**Engine Lab:** fewer than all 10 scanner modes were visible in the shadow comparison."),
            "engine_lab_row_errors": (2, "**Engine Lab:** one or more engine-lab rows reported errors."),
            "feature_disabled": (1, "**Feature coverage:** a session-observation or safety toggle is OFF; confirm this is intentional before live market."),
            "quad_prime_filter_config_missing": (2, "**Tracker coverage:** latest Quad Prime filter settings were missing from API config, so the tracker cannot fully explain live decision gates."),
            "activity_error_alerts": (2, "**Live Tape:** recent dashboard activity contains error alerts. Check whether these are stale historical alerts or current runtime failures."),
        }
        for key, count in sorted(self.system_issues.items(), key=lambda kv: -kv[1]):
            if key in issue_suggestions:
                sev, msg = issue_suggestions[key]
                s.append((sev, f"{msg} Observed ×{count}."))
        top_sess = sorted(self.error_sources_session.items(), key=lambda kv: -kv[1])[:4]
        top_clean_session = sorted(self.market_clean_error_sources.items(), key=lambda kv: -kv[1])[:4]
        top_clean = sorted(self.clean_error_sources.items(), key=lambda kv: -kv[1])[:4]
        if top_clean_session:
            srcs = "; ".join(f"`{src}`×{n}" for src, n in top_clean_session)
            s.append((3, f"**Top clean-window in-session error sources** (investigate each): {srcs}. "
                         f"`scanner:_process_instrument*` = intelligence-analysis failures; "
                         f"`*get_historical_candles*`/`broker:get_positions*` = broker/API JSON-parse (rate-limit?) — "
                         f"add retry/backoff or reduce fetch cadence."))
        elif top_clean:
            srcs = "; ".join(f"`{src}`×{n}" for src, n in top_clean[:3])
            s.append((2, f"**Clean-window off-session errors:** {srcs}; no clean-window in-session error sources remain. "
                         f"Watch at next 09:00-15:30 session before changing order-path logic."))
        elif top_sess:
            srcs = "; ".join(f"`{src}`×{n}" for src, n in top_sess[:3])
            s.append((1, f"**Legacy in-session errors:** {srcs}; none remain in the clean window since the latest clean boundary."))
        if not s:
            s.append((1, "No actionable issues detected so far. Data-feed degradations (if any) were graceful and off the order path."))
        return sorted(s, key=lambda x: -x[0])

    def _system_diagnosis_lines(self) -> list[str]:
        status = self.latest_status_summary or {}
        diag = self.latest_diagnostics_summary or {}
        gateway = status.get("gateway_status") or {}
        sources = diag.get("sources") or gateway or {}
        freshness = diag.get("scanner_freshness") or {}
        latency = diag.get("latency") or {}
        workers = diag.get("worker_resources") or {}
        queue = workers.get("async_execution_queue") or {}
        broker_exec = workers.get("broker_executor") or {}
        risk = diag.get("risk_configuration") or {}
        option_history = diag.get("option_history") or {}
        option_quality = diag.get("option_history_quality") or {}
        option_bundle = diag.get("option_history_bundle") or {}
        repaint = diag.get("repaint_guard") or {}
        instrument_selection = diag.get("instrument_selection") or {}
        recalc = diag.get("recalculation") or {}
        perf = diag.get("system_performance") or {}
        trade_contract = self.latest_trade_contract or {}

        risk_issues = risk.get("issues") if isinstance(risk.get("issues"), list) else []
        risk_warnings = risk.get("warnings") if isinstance(risk.get("warnings"), list) else []
        perf_issues = perf.get("issues") if isinstance(perf.get("issues"), list) else []
        contract_mismatches = trade_contract.get("mismatches") or []

        lines = [
            (
                f"- Runtime: mode `{self._fmt_value(status.get('mode'))}` · "
                f"scanner `{self._fmt_bool(status.get('scanner_running'))}` · "
                f"engine `{self._fmt_value(status.get('scanner_engine'))}` · "
                f"grade `{self._fmt_value(status.get('grade_preference'))}` · "
                f"scan_count `{self._fmt_value(status.get('scan_count'))}`"
            ),
            (
                f"- Feeds/brokers: Angel `{self._fmt_bool(sources.get('angel'))}`"
                f" age `{self._fmt_value(sources.get('angel_tick_age_seconds'))}s` · "
                f"Fyers `{self._fmt_bool(sources.get('fyers'))}`"
                f" age `{self._fmt_value(sources.get('fyers_tick_age_seconds'))}s` · "
                f"degraded `{self._fmt_bool(sources.get('broker_degraded'))}` · "
                f"all unavailable `{self._fmt_bool(sources.get('all_brokers_unavailable'))}` · "
                f"Fyers auth required `{self._fmt_bool(sources.get('fyers_auth_required'))}`"
            ),
            (
                f"- Scanner freshness: age `{self._fmt_value(freshness.get('age_seconds'))}s` · "
                f"stale `{self._fmt_bool(freshness.get('is_stale'))}` · "
                f"calculating `{self._fmt_bool(freshness.get('is_calculating'))}` · "
                f"lock `{self._fmt_bool(freshness.get('calculation_lock'))}`"
            ),
            (
                f"- Latency/workers: last `{self._fmt_value(latency.get('last_ms'))}ms` · "
                f"high events `{self._fmt_value(latency.get('high_latency_events'), '0')}` · "
                f"threads `{self._fmt_value(workers.get('active_threads'))}/{self._fmt_value(workers.get('logical_cpu_count'))}` · "
                f"default workers `{self._fmt_value(workers.get('default_executor_max_workers'))}` · "
                f"live capacity `{self._fmt_value(workers.get('live_processing_capacity_ratio'))}x` · "
                f"broker queue `{self._fmt_value(broker_exec.get('queued_tasks'), '0')}`"
            ),
            (
                f"- Async/recalc queue: available `{self._fmt_bool(queue.get('available'))}` · "
                f"workers `{self._fmt_value(queue.get('configured_workers'))}` · "
                f"capacity `{self._fmt_value(queue.get('queue_capacity'))}`"
            ),
            (
                f"- Risk config: ok `{self._fmt_bool(risk.get('ok'))}` · "
                f"issues `{len(risk_issues)}` · warnings `{len(risk_warnings)}` · "
                f"FUT budget `{self._fmt_value(risk.get('fut_daily_budget'))}` · "
                f"OPT budget `{self._fmt_value(risk.get('opt_daily_budget'))}` · "
                f"max trade loss `{self._fmt_value(risk.get('max_trade_loss_abs'))}`"
            ),
            (
                f"- Trade contract: `{self._fmt_value(trade_contract.get('status'))}` · "
                f"diag entries/exits `{self._fmt_value(trade_contract.get('diagnostics_entries'))}/"
                f"{self._fmt_value(trade_contract.get('diagnostics_exits'))}` · "
                f"api rows/closed `{self._fmt_value(trade_contract.get('trades_rows_total'))}/"
                f"{self._fmt_value(trade_contract.get('trades_closed_total'))}` · "
                f"basis `{self._fmt_value(trade_contract.get('comparison_basis'))}` · "
                f"signals `{self._fmt_value(trade_contract.get('trades_signals_total'))}` · "
                f"visible live/historical rows `{self._fmt_value(trade_contract.get('visible_live_session_rows'))}/"
                f"{self._fmt_value(trade_contract.get('visible_historical_rows'))}`"
            ),
            (
                f"- Option data: attempts `{self._fmt_value(option_history.get('attempts'), '0')}` · "
                f"hits `{self._fmt_value(option_history.get('hits'), '0')}` · "
                f"real hits `{self._fmt_value(option_quality.get('real_option_hits'), '0')}` · "
                f"misses `{self._fmt_value(option_history.get('misses'), '0')}` · "
                f"synthetic `{self._fmt_value(option_history.get('synthetic'), '0')}` · "
                f"FUT fallback `{self._fmt_value(option_history.get('fallback_to_fut'), '0')}` · "
                f"quality `{self._fmt_value(option_quality.get('status'), 'n/a')}` · "
                f"fallback rate `{self._fmt_value(option_quality.get('fallback_rate_pct'), '0')}%` · "
                f"bundle files `{self._fmt_value(option_bundle.get('files'))}`"
            ),
            (
                f"- Repaint guard: checked `{self._fmt_value(repaint.get('checked'), '0')}` · "
                f"aborted `{self._fmt_value(repaint.get('aborted'), '0')}` · "
                f"passed `{self._fmt_value(repaint.get('passed'), '0')}` · "
                f"post-exit due `{self._fmt_value(repaint.get('post_repaint_recheck_due'), '0')}`"
            ),
            (
                f"- Instrument selection: OPT `{self._fmt_value(instrument_selection.get('OPT'), '0')}` · "
                f"FUT `{self._fmt_value(instrument_selection.get('FUT'), '0')}`"
            ),
            (
                f"- Recalculation/performance: recalc `{self._fmt_value(recalc.get('status'))}` · "
                f"duration `{self._fmt_value(recalc.get('duration_seconds'))}s` · "
                f"system `{self._fmt_value(perf.get('status'))}` · "
                f"perf issues `{len(perf_issues)}`"
            ),
        ]
        if contract_mismatches:
            lines.append(f"- Trade contract mismatch detail: `{' ; '.join(str(x) for x in contract_mismatches[:4])}`")
        return lines

    def _readiness_lines(self) -> list[str]:
        readiness = self.latest_readiness_assessment or {}
        if not readiness:
            return ["- Readiness not calculated yet."]
        lines = [
            (
                f"- Status `{self._fmt_value(readiness.get('status'))}` · "
                f"score `{self._fmt_value(readiness.get('score'))}/100`"
            )
        ]
        blockers = readiness.get("blockers") if isinstance(readiness.get("blockers"), list) else []
        watch = readiness.get("watch") if isinstance(readiness.get("watch"), list) else []
        lines.append(f"- Blockers: `{', '.join(str(x) for x in blockers) if blockers else 'none'}`")
        lines.append(f"- Watch: `{', '.join(str(x) for x in watch[:8]) if watch else 'none'}`")
        if len(watch) > 8:
            lines.append(f"- Watch overflow: `{len(watch) - 8}` more item(s) in state JSON.")
        return lines

    def _feature_matrix_lines(self) -> list[str]:
        matrix = self.latest_feature_matrix or {}
        if not matrix:
            return ["- Feature matrix not collected; run tracker with state polling enabled."]
        lines = [
            (
                f"- Known toggles `{self._fmt_value(matrix.get('known_count'), '0')}` · "
                f"enabled `{self._fmt_value(matrix.get('enabled_count'), '0')}` · "
                f"disabled `{len(matrix.get('disabled') or [])}`"
            )
        ]
        for key, payload in matrix.items():
            if not isinstance(payload, dict):
                continue
            lines.append(f"- `{key}`: `{payload.get('status')}` · {payload.get('label')}")
        return lines

    def _quad_prime_filter_lines(self) -> list[str]:
        snapshot = self.latest_quad_prime_filters or {}
        groups = snapshot.get("groups") if isinstance(snapshot.get("groups"), dict) else {}
        if not groups:
            return ["- Quad Prime filter snapshot not collected; run tracker with state polling enabled."]
        lines = [
            (
                f"- Known settings `{self._fmt_value(snapshot.get('known_count'), '0')}` · "
                f"missing `{self._fmt_value(snapshot.get('missing_count'), '0')}`"
            )
        ]
        for group_key, group in groups.items():
            if not isinstance(group, dict):
                continue
            settings = group.get("settings") if isinstance(group.get("settings"), list) else []
            rendered = []
            for item in settings:
                if not isinstance(item, dict) or not item.get("present"):
                    continue
                rendered.append(f"{item.get('label')}={self._fmt_value(item.get('value'))}")
            if rendered:
                lines.append(f"- `{group_key}` {group.get('label')}: " + " · ".join(rendered))
        missing = snapshot.get("missing") if isinstance(snapshot.get("missing"), list) else []
        if missing:
            lines.append(f"- Missing setting keys sample: `{', '.join(str(x) for x in missing[:10])}`")
        return lines

    def _market_direction_lines(self) -> list[str]:
        direction = self.latest_market_direction or {}
        if not direction:
            return ["- Market direction matrix not collected; run tracker with state polling enabled."]
        votes = direction.get("votes") if isinstance(direction.get("votes"), dict) else {}
        lines = [
            (
                f"- Broad direction `{self._fmt_value(direction.get('broad_direction'))}` · "
                f"votes L/S/F `{self._fmt_value(votes.get('LONG'), '0')}/"
                f"{self._fmt_value(votes.get('SHORT'), '0')}/"
                f"{self._fmt_value(votes.get('FLAT'), '0')}`"
            )
        ]
        missing_indices = direction.get("missing_indices") or []
        if missing_indices:
            lines.append(f"- Missing indices: `{', '.join(str(x) for x in missing_indices)}`")
        missing_components = direction.get("missing_components") or {}
        if missing_components:
            sample = "; ".join(f"{k}:{','.join(v)}" for k, v in list(missing_components.items())[:4])
            lines.append(f"- Missing intel components: `{sample}`")
        rows = direction.get("indices") if isinstance(direction.get("indices"), dict) else {}
        for name in EXPECTED_INDICES:
            row = rows.get(name)
            if not isinstance(row, dict):
                continue
            pre5 = row.get("pre_5min") if isinstance(row.get("pre_5min"), dict) else {}
            pre15 = row.get("pre_15min") if isinstance(row.get("pre_15min"), dict) else {}
            lines.append(
                f"- `{name}` vote `{row.get('vote')}` · MTF `{row.get('mtf_signal')}` "
                f"score `{row.get('mtf_score')}` · 5m/15m `{row.get('state_5min')}/{row.get('state_15min')}` "
                f"ADX `{row.get('adx_5min')}/{row.get('adx_15min')}` · agg `{row.get('aggregate_signal')}` "
                f"`{row.get('aggregate_score')}` · regime `{row.get('regime')}` `{row.get('regime_direction')}` · "
                f"vol `{row.get('volume_ratio')}x` · OI `{row.get('oi_signal')}` · PCR `{row.get('pcr_signal')} "
                f"{row.get('pcr')}` · flow `{row.get('order_flow')} {row.get('order_ratio')}` · "
                f"pre5 `{pre5.get('state')}/{pre5.get('direction')}/{pre5.get('confidence')}` · "
                f"pre15 `{pre15.get('state')}/{pre15.get('direction')}/{pre15.get('confidence')}`"
            )
        return lines

    @staticmethod
    def _engine_lab_perf_label(row: dict[str, Any]) -> str:
        basis = f" basis `{row.get('basis')}`" if row.get("basis") else ""
        money = f" money `{row.get('total_money')}`" if row.get("total_money") not in (None, "") else ""
        return (
            f"`{row.get('engine')}` pts `{row.get('total_points')}`{money} "
            f"WR `{row.get('win_rate')}` PF `{row.get('profit_factor')}`{basis}"
        )

    def _engine_lab_lines(self) -> list[str]:
        engine_lab = (self.latest_state_summary or {}).get("engine_lab") or {}
        if not engine_lab:
            return ["- Engine Lab matrix not collected; run tracker with state polling enabled."]
        lines = [
            (
                f"- Enabled `{self._fmt_bool(engine_lab.get('enabled'))}` · active "
                f"`{self._fmt_value(engine_lab.get('active_engine'))}` · rows "
                f"`{self._fmt_value(engine_lab.get('rows'), '0')}/{self._fmt_value(engine_lab.get('expected_rows'), str(EXPECTED_ENGINE_LAB_ROWS))}` · "
                f"basis `{self._fmt_value(engine_lab.get('basis'))}` · signals `{self._fmt_value(engine_lab.get('signals'), '0')}` · "
                f"open/closed `{self._fmt_value(engine_lab.get('open'), '0')}/{self._fmt_value(engine_lab.get('closed'), '0')}` · "
                f"latency `{self._fmt_value(engine_lab.get('latency_ms'))}ms` · row errors `{self._fmt_value(engine_lab.get('row_errors'), '0')}`"
            )
        ]
        best = engine_lab.get("best") if isinstance(engine_lab.get("best"), list) else []
        worst = engine_lab.get("worst") if isinstance(engine_lab.get("worst"), list) else []
        if best:
            lines.append("- Best rows: " + "; ".join(
                self._engine_lab_perf_label(r)
                for r in best[:3]
                if isinstance(r, dict)
            ))
        if worst:
            lines.append("- Weak rows: " + "; ".join(
                self._engine_lab_perf_label(r)
                for r in worst[:3]
                if isinstance(r, dict)
            ))
        return lines

    def _live_activity_lines(self) -> list[str]:
        activity = (self.latest_state_summary or {}).get("activity") or {}
        if not activity:
            return ["- Live Tape/activity summary not collected; run tracker with state polling enabled."]
        counts = activity.get("counts") if isinstance(activity.get("counts"), dict) else {}
        lines = [
            f"- Visible activity rows `{self._fmt_value(activity.get('total_visible'), '0')}` · type counts `{json.dumps(counts, sort_keys=True)}`"
        ]
        recent = activity.get("recent") if isinstance(activity.get("recent"), list) else []
        for item in recent[:5]:
            if not isinstance(item, dict):
                continue
            lines.append(f"- `{item.get('time')}` `{item.get('type')}` {item.get('msg')}")
        return lines

    def write_report(self):
        state = {
            "session_date": self.session_date,
            "run_id": self.run_id,
            "generated_at": now_ist().isoformat(),
            "poll_count": self.poll_count,
            "modes_seen": sorted(self.api_status_seen["mode"]),
            "scanner_running_seen": sorted(self.api_status_seen["scanner_running"]),
            "max_scanner_age_s": round(self.max_scanner_age, 1),
            "max_api_latency_ms": round(self.max_api_latency_ms, 1),
            "max_diag_latency_ms": round(self.max_diag_latency_ms, 1),
            "max_queue_fill_pct": round(self.max_queue_fill_pct, 1),
            "max_open_trades": self.max_open_trades,
            "max_closed_trades": self.max_closed_trades,
            "max_session_rows": self.max_session_rows,
            "max_session_closed": self.max_session_closed,
            "max_engine_lab_rows": self.max_engine_lab_rows,
            "state_poll_count": self.state_poll_count,
            "stale_scan_polls": self.stale_scan_polls,
            "system_issues": dict(sorted(self.system_issues.items(), key=lambda kv: -kv[1])),
            "system_issue_samples": dict(self.system_issue_samples),
            "latest_status_summary": self.latest_status_summary,
            "latest_diagnostics_summary": self.latest_diagnostics_summary,
            "latest_trade_contract": self.latest_trade_contract,
            "latest_state_summary": self.latest_state_summary,
            "latest_readiness_assessment": self.latest_readiness_assessment,
            "latest_feature_matrix": self.latest_feature_matrix,
            "latest_quad_prime_filters": self.latest_quad_prime_filters,
            "latest_market_direction": self.latest_market_direction,
            "ignored_artifacts": dict(self.ignored_artifacts),
            "clean_start_ts": self.clean_start_ts,
            "clean_start_reason": self.clean_start_reason,
            "market_clean_start_ts": self.market_clean_start_ts,
            "market_clean_start_reason": self.market_clean_start_reason,
            "market_clean_event_counts": dict(sorted(self.market_clean_counts.items(), key=lambda kv: (-self.severity.get(kv[0], 1), -kv[1]))),
            "market_clean_error_sources": dict(sorted(self.market_clean_error_sources.items(), key=lambda kv: -kv[1])),
            "market_clean_repeated_log_fingerprints": dict(sorted(self.market_clean_repeated_log_fingerprints.items(), key=lambda kv: -kv[1])[:20]),
            "market_clean_samples": self.market_clean_samples,
            "clean_event_counts": dict(sorted(self.clean_counts.items(), key=lambda kv: (-self.severity.get(kv[0], 1), -kv[1]))),
            "clean_event_counts_in_session": dict(sorted(self.clean_in_session_counts.items(), key=lambda kv: (-self.severity.get(kv[0], 1), -kv[1]))),
            "clean_error_sources": dict(sorted(self.clean_error_sources.items(), key=lambda kv: -kv[1])),
            "clean_error_sources_in_session": dict(sorted(self.clean_error_sources_session.items(), key=lambda kv: -kv[1])),
            "clean_samples": self.clean_samples,
            "clean_repeated_log_fingerprints_session": dict(sorted(self.clean_repeated_log_fingerprints_session.items(), key=lambda kv: -kv[1])[:20]),
            "clean_repeated_log_fingerprints": dict(sorted(self.clean_repeated_log_fingerprints.items(), key=lambda kv: -kv[1])[:20]),
            "repeated_log_fingerprints_session": dict(sorted(self.repeated_log_fingerprints_session.items(), key=lambda kv: -kv[1])[:20]),
            "repeated_log_fingerprints": dict(sorted(self.repeated_log_fingerprints.items(), key=lambda kv: -kv[1])[:20]),
            "event_counts": dict(sorted(self.counts.items(), key=lambda kv: (-self.severity.get(kv[0], 1), -kv[1]))),
            "error_sources": dict(sorted(self.error_sources.items(), key=lambda kv: -kv[1])),
            "error_sources_in_session": dict(sorted(self.error_sources_session.items(), key=lambda kv: -kv[1])),
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "samples": self.samples,
        }
        (OUT_DIR / f"session_state_{self.session_date}.json").write_text(
            json.dumps(state, indent=2, default=str), encoding="utf-8")
        with (OUT_DIR / f"timeline_{self.session_date}.jsonl").open("a", encoding="utf-8") as f:
            if self.poll_snapshots:
                f.write(json.dumps(self.poll_snapshots[-1], default=str) + "\n")

        sev_name = {3: "🔴 CRITICAL", 2: "🟠 WATCH", 1: "🟢 INFO"}
        lines = [
            f"# Live Session Tracker — {self.session_date}",
            f"_Generated {now_ist().strftime('%Y-%m-%d %H:%M:%S IST')} · run_id={self.run_id} · polls={self.poll_count} · "
            f"since={getattr(self.args, 'since', None) or 'start-of-day'} · READ-ONLY observer_",
            "",
            "## Health snapshot",
            f"- Modes seen: `{', '.join(sorted(self.api_status_seen['mode'])) or 'n/a'}`",
            f"- Scanner running: `{', '.join(sorted(self.api_status_seen['scanner_running'])) or 'n/a'}`",
            f"- Max scanner age: `{self.max_scanner_age:.1f}s` · stale polls: `{self.stale_scan_polls}/{self.poll_count}`",
            f"- Max API poll latency: `{self.max_api_latency_ms:.0f}ms` · max diagnostic latency metric: `{self.max_diag_latency_ms:.0f}ms`",
            f"- Max async/recalc queue fill: `{self.max_queue_fill_pct:.1f}%`",
            f"- Max trades seen: session rows `{self.max_session_rows}` (closed `{self.max_session_closed}`) · "
            f"raw api arrays: open `{self.max_open_trades}` · closed `{self.max_closed_trades}`",
            # Session rows without a single raw broker position = paper/virtual
            # ledger. The 2026-07-03 report was misread as real option P&L.
            *(
                [
                    "- ⚠️ **VIRTUAL SESSION**: session rows exist but the broker API "
                    "reported zero open/closed positions all day — every trade row is a "
                    "paper/virtual lifecycle, not a real fill."
                ]
                if (
                    self.max_session_rows > 0
                    and self.max_open_trades == 0
                    and self.max_closed_trades == 0
                )
                else []
            ),
            f"- Trade contract: `{self.latest_trade_contract.get('status', 'n/a')}`",
            f"- Dashboard API unreachable polls: `{self.counts.get('api_unreachable', 0)}`",
            f"- Ignored test/mock log lines: `{sum(self.ignored_artifacts.values())}`",
            f"- Clean window boundary: `{self.clean_start_ts or 'not detected'}`"
            f"{f' ({self.clean_start_reason})' if self.clean_start_reason else ''}",
            "",
            "## Upcoming Session Readiness",
        ]
        lines.extend(self._readiness_lines())
        lines += [
            "",
            "## Feature Coverage Matrix",
        ]
        lines.extend(self._feature_matrix_lines())
        lines += [
            "",
            "## Quad Prime / Late-Session Filter Settings",
        ]
        lines.extend(self._quad_prime_filter_lines())
        lines += [
            "",
            "## Broad Market Direction & Intelligence",
        ]
        lines.extend(self._market_direction_lines())
        lines += [
            "",
            "## Engine Lab 12-Mode Coverage",
        ]
        lines.extend(self._engine_lab_lines())
        lines += [
            "",
            "## Live Tape / Activity Snapshot",
        ]
        lines.extend(self._live_activity_lines())
        lines += [
            "",
            "## System Function Diagnosis",
        ]
        lines.extend(self._system_diagnosis_lines())
        lines += [
            "",
            "## Market-Hours Clean Window (Default View)",
        ]
        if self.market_clean_start_ts:
            reason = f" · {self.market_clean_start_reason}" if self.market_clean_start_reason else ""
            lines.append(f"_Boundary `{self.market_clean_start_ts}`{reason}. Showing clean events inside 09:15-15:30 first._")
        else:
            lines.append("_No dashboard-start marker detected; this view uses the selected log window inside 09:15-15:30._")
        if self.market_clean_counts:
            for k, v in sorted(self.market_clean_counts.items(), key=lambda kv: (-self.severity.get(kv[0], 1), -kv[1])):
                sev = sev_name.get(self.severity.get(k, 1), "")
                lines.append(f"- {sev} `{k}` ×**{v}**")
        else:
            lines.append("- _(none)_")
        if self.market_clean_error_sources:
            lines += ["", "### Market-Hours Clean Error/Critical Sources"]
            for src, n in sorted(self.market_clean_error_sources.items(), key=lambda kv: -kv[1])[:8]:
                lines.append(f"- `{src}` ×**{n}**")
        clean_session_noisy = sorted(self.market_clean_repeated_log_fingerprints.items(), key=lambda kv: -kv[1])[:5]
        if clean_session_noisy:
            lines += ["", "### Market-Hours Clean Repeated Warning/Error Fingerprints"]
            for fp, n in clean_session_noisy:
                if n >= 3:
                    lines.append(f"- ×**{n}** `{fp}`")
        lines += [
            "",
            "## Function / lag / redundancy audit",
        ]
        if self.system_issues:
            for k, v in sorted(self.system_issues.items(), key=lambda kv: -kv[1]):
                sample = self.system_issue_samples.get(k, [""])[0]
                suffix = f" — `{sample}`" if sample else ""
                lines.append(f"- `{k}` ×**{v}**{suffix}")
        else:
            lines.append("- No system-function, latency, redundancy, or consistency flags detected yet.")
        noisy = sorted(self.repeated_log_fingerprints.items(), key=lambda kv: -kv[1])[:5]
        if noisy:
            lines += ["", "### Top repeated warning/error fingerprints"]
            for fp, n in noisy:
                if n >= 3:
                    lines.append(f"- ×**{n}** `{fp}`")
        clean_noisy = sorted(self.market_clean_repeated_log_fingerprints.items(), key=lambda kv: -kv[1])[:5]
        if clean_noisy:
            lines += ["", "### Market-hours clean-window repeated warning/error fingerprints"]
            for fp, n in clean_noisy:
                if n >= 3:
                    lines.append(f"- ×**{n}** `{fp}`")
        lines += [
            "",
            "## Full-Day Event Counts (Secondary / Noise-Aware)",
        ]
        if self.counts:
            for k, v in sorted(self.counts.items(), key=lambda kv: (-self.severity.get(kv[0], 1), -kv[1])):
                sev = sev_name.get(self.severity.get(k, 1), "")
                insess = self.in_session_counts.get(k, 0)
                sess = f" · **{insess} in-session**" if insess else " · 0 in-session"
                window = f" (last {self.last_seen.get(k,'?')})" if k in self.first_seen else ""
                lines.append(f"- {sev} `{k}` ×**{v}**{sess}{window}")
        else:
            lines.append("- _(none yet)_")
        lines += [
            "",
            "## Clean Window Since Latest Boundary (All Hours)",
        ]
        if self.clean_start_ts:
            reason = f" · {self.clean_start_reason}" if self.clean_start_reason else ""
            lines.append(f"_Boundary `{self.clean_start_ts}`{reason}. Test/mock artifacts are excluded._")
        else:
            lines.append("_No dashboard-start marker detected; clean window equals the selected log window._")
        if self.clean_counts:
            for k, v in sorted(self.clean_counts.items(), key=lambda kv: (-self.severity.get(kv[0], 1), -kv[1])):
                sev = sev_name.get(self.severity.get(k, 1), "")
                insess = self.clean_in_session_counts.get(k, 0)
                sess = f" · **{insess} in-session**" if insess else " · 0 in-session"
                lines.append(f"- {sev} `{k}` ×**{v}**{sess}")
        else:
            lines.append("- _(none)_")
        if self.clean_error_sources:
            lines += ["", "### Clean Error/Critical Sources"]
            for src, n in sorted(self.clean_error_sources.items(), key=lambda kv: -kv[1])[:8]:
                lines.append(f"- `{src}` ×**{n}** · {self.clean_error_sources_session.get(src, 0)} in-session")
        if self.error_sources:
            lines += ["", "## Error/Critical sources (source:func:line — count · in-session)"]
            for src, n in sorted(self.error_sources.items(), key=lambda kv: -kv[1])[:12]:
                lines.append(f"- `{src}` ×**{n}** · {self.error_sources_session.get(src, 0)} in-session")
        lines += ["", "## Suggested fixes (severity-ranked)"]
        for sev, text in self._suggestions():
            lines.append(f"- {sev_name.get(sev,'')} {text}")
        # samples for the scary ones
        scary = [k for k in ("traceback", "critical_log", "broker_order_fail", "winerror32_oi",
                             "option_chain_error", "fyers_auth_required") if self.samples.get(k)]
        if scary:
            lines += ["", "## Samples"]
            for k in scary:
                lines.append(f"**{k}**")
                for ln in self.samples[k]:
                    lines.append(f"  - `{ln}`")
        report_path = OUT_DIR / f"session_report_{self.session_date}.md"
        report_path.write_text("\n".join(lines), encoding="utf-8")
        return report_path

    def cycle(self):
        self._read_new_log()
        self._poll_api()
        return self.write_report()

    def run(self):
        if self.args.once:
            p = self.cycle()
            print(f"[tracker] snapshot written: {p}")
            return
        until = None
        if self.args.until:
            hh, mm = (int(x) for x in self.args.until.split(":"))
            until = now_ist().replace(hour=hh, minute=mm, second=0, microsecond=0)
        print(f"[tracker] watching every {self.args.interval}s until {self.args.until or 'Ctrl-C'} (READ-ONLY)")
        while True:
            p = self.cycle()
            n = now_ist()
            print(f"[tracker] {n.strftime('%H:%M:%S')} poll #{self.poll_count} "
                  f"events={sum(self.counts.values())} -> {p.name}")
            if until and n >= until:
                print("[tracker] reached --until; final report written.")
                return
            time.sleep(max(15, self.args.interval))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="single snapshot then exit")
    ap.add_argument("--watch", action="store_true", help="continuous tracking")
    ap.add_argument("--interval", type=int, default=120, help="poll seconds (watch mode)")
    ap.add_argument("--since", type=str, default=None, help="HH:MM IST log start time")
    ap.add_argument("--until", type=str, default=None, help="HH:MM IST stop time (watch mode)")
    ap.add_argument("--no-lock", action="store_true", help="disable single-instance guard for isolated tests")
    ap.add_argument(
        "--no-state",
        action="store_false",
        dest="track_state",
        default=True,
        help="skip /api/state intelligence/Engine Lab coverage polling",
    )
    args = ap.parse_args()
    if not args.once and not args.watch:
        args.once = True
    lock = None
    run_id = None
    if not args.no_lock:
        lock = TrackerRunLock(args)
        if not lock.acquire():
            owner = lock.owner or {}
            print(
                "[tracker] already running "
                f"pid={owner.get('pid', 'unknown')} run_id={owner.get('run_id', 'unknown')}; exiting."
            )
            return
        run_id = lock.run_id
    try:
        Tracker(args, run_id=run_id).run()
    finally:
        if lock is not None:
            lock.release()


if __name__ == "__main__":
    main()
