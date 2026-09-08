import os
import re
import sqlite3
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta
import json
from loguru import logger
import pytz

IST = pytz.timezone('Asia/Kolkata')


def _resolve_db_path(db_path: str = "data_store/utbot_data.db") -> str:
    """Prefer explicit test/override path so pytest never mutates production SQLite."""
    override = (
        os.environ.get("UTBOT_TEST_DB_PATH")
        or os.environ.get("UTBOT_DB_PATH")
        or ""
    ).strip()
    if override:
        return override
    if os.environ.get("PYTEST_CURRENT_TEST"):
        base = Path(tempfile.gettempdir()) / "utbot_pytest_db"
        base.mkdir(parents=True, exist_ok=True)
        return str(base / "utbot_data.db")
    return db_path or "data_store/utbot_data.db"


# Single source of truth for what a trade row persists. save_trade() and
# save_trades() each used to carry their own copy of this list, and the two drifted:
# a column missing from the copy is silently dropped, and because both writers use
# INSERT OR REPLACE, a dropped column is NULLed on the stored row rather than left
# untouched. Keep this in step with Trade.to_dict()/from_dict() — tests/
# test_trade_persistence_roundtrip.py fails if any of the three fall out of sync.
TRADE_PERSISTED_COLUMNS = frozenset({
    "id", "instrument", "timeframe", "direction", "entry_price", "entry_time",
    "trailing_stop", "current_stop", "lots", "lot_size", "grade",
    "entry_timestamp", "exit_timestamp", "broker_order_id", "exit_order_id",
    "execution_status", "execution_error", "pending_execution", "entry_submitted_at",
    "pending_exit", "broker_quantity", "trading_symbol", "symbol_token",
    "atm_strike", "option_type", "target", "rr_ratio", "confidence",
    "inst_type", "instrument_multiplier", "exec_type", "status",
    "current_price", "entry_spot", "spot_stop", "spot_target", "exit_price",
    "exit_time", "exit_reason", "pnl", "charges", "broker_realised_cumulative",
    "peak_pnl", "max_drawdown",
    "is_ghost", "market_regime", "data_provenance", "price_action_verdict",
    "entry_intelligence_score", "intelligence_score", "strategy_revision", "strategy_direction_mode",
    "initial_stop", "scalp_lock_mode", "market_wide_reversal_entry",
    "volume_spike_entry", "power_move", "source_lane",
    # Immutable entry-decision evidence.  These fields are needed after a restart
    # to distinguish the two price domains and to audit the exact forecast/policy
    # verdict that authorised fresh exposure.
    "underlying_entry", "underlying_stop", "underlying_target",
    "premium_entry", "premium_stop", "premium_target",
    "geometry_observed_at", "geometry_source", "geometry_verified", "geometry_schema",
    "expected_move_pct", "expected_move_floor_pct", "expected_move_uncertainty_pct",
    "expected_move_probability", "expected_move_sample_count",
    "expected_move_horizon_minutes", "expected_move_generated_at",
    "expected_move_data_as_of", "expected_move_model_version", "expected_move_source",
    "entry_policy_allowed", "entry_policy_phase", "entry_policy_checked_at",
    "entry_policy_reason", "entry_policy_actual_rr",
    "entry_policy_underlying_stop_pct", "entry_policy_version", "quality_verdict",
    "is_recovery",
    # Exchange-side protection + recovery state: read by from_dict(), so it has to
    # be written or every restart silently resets it.
    "protective_order_id", "protective_trigger", "protective_quantity",
    "gtt_trigger_id",
    "protective_updated_at", "recovery_required_since", "signal_time",
    "entry_replace_count",
    # partial_exit_flatten_count BOUNDS how many times the remainder of a partially
    # filled exit may be re-submitted; unstored, a restart reset the bound to zero and
    # the attempt budget started over. reversal_watch_required marks a contract whose
    # protective stop may still be resting after a flatten — the operator-facing half of
    # a hazard the logs shout about.
    "partial_exit_flatten_count", "reversal_watch_required", "is_reversion_scalp",
    # repaint_guard_checked: without it a restart rehydrates every intracandle trade as
    # "structurally unconfirmed", which disables let-winners-run and arms the blunt
    # sub-1R give-back guard on a position whose source candle closed hours earlier.
    "repaint_guard_checked",
    # gtt_arm_uncertain: a possible orphaned server-side trigger. Losing this on a
    # restart is how a duplicate GTT goes unreconciled until both it and the SL-M
    # fire, double-exiting the position into a naked reversed leg.
    "gtt_arm_uncertain",
    # Measured execution quality. Trade computes both on every fill and to_dict()/
    # from_dict() have always carried them, but they were not in this list, so the
    # only durable record of how an option actually filled was discarded at save.
    # This system trades options first, and the options leg has never had a single
    # calibrated slippage sample -- estimate_trade_charges falls back to the static
    # slippage_opt_pct for every option it prices. Nothing can tune that number
    # without a stored history of real fills to tune it against.
    "entry_slippage", "exit_slippage",
    # The six columns below already existed in the trades table and NOTHING wrote them:
    # schema with no producer, so every stored row read 0/NULL. They are the raw evidence
    # of how an order filled — what was asked for, what came back, and how long it took —
    # and without the requested price stored beside the filled one, realised slippage
    # cannot be recomputed from history at all.
    "requested_entry_price", "requested_exit_price",
    "entry_filled_at", "exit_filled_at",
    "fill_latency_ms", "exit_fill_latency_ms",
    # Scale-out state. An in-flight reduce that did not survive a restart would be
    # re-submitted against lots that are already gone.
    "partial_derisk_done", "partial_reduce_order_id",
    "partial_reduce_requested_quantity",
    "scale_in_count",
    "scale_in_lots",
    "frozen_initial_risk_rs",
    "scale_in_order_id",
    "scale_in_requested_quantity",
    # Runner mode suppresses the fixed-target exit. Losing it on restart changes how
    # a live position is managed, so its state and provenance are operational data.
    "lifecycle_id",
    "runner_mode",
    "runner_activation_timestamp",
    "runner_activation_reason",
    "runner_stop",
    "runner_stop_basis",
    "stop_provenance",
})


_TRADE_JSON_COLUMNS = frozenset({"quality_verdict"})


def _encode_trade_value(column: str, value: Any) -> Any:
    """Convert non-native trade values into stable SQLite representations."""
    if isinstance(value, datetime):
        return value.isoformat()
    if value == "--" and column in ("entry_time", "exit_time"):
        return None
    if column in _TRADE_JSON_COLUMNS:
        # Loaded rows may be handed back to a writer, so accept both the decoded
        # mapping and its already-encoded representation without double encoding.
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (TypeError, ValueError, json.JSONDecodeError):
                value = {}
        if not isinstance(value, dict):
            value = {}
        return json.dumps(value, separators=(",", ":"), default=str)
    return value


def _decode_trade_row(row: sqlite3.Row) -> Dict[str, Any]:
    """Decode structured trade columns while failing closed on legacy corruption."""
    item = dict(row)
    for column in _TRADE_JSON_COLUMNS:
        raw = item.get(column)
        if isinstance(raw, dict):
            item[column] = dict(raw)
            continue
        try:
            decoded = json.loads(raw) if raw else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            decoded = {}
        item[column] = decoded if isinstance(decoded, dict) else {}
    return item


def _running_under_pytest() -> bool:
    return bool(
        os.environ.get("PYTEST_CURRENT_TEST")
        or os.environ.get("UTBOT_TEST_DB_PATH")
    )


class DatabaseManager:
    """Thread-safe SQLite database manager for trade state and history."""
    
    _instance = None
    _lock = threading.Lock()

    def __new__(cls, db_path: str = "data_store/utbot_data.db"):
        resolved = _resolve_db_path(db_path)
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(DatabaseManager, cls).__new__(cls)
                cls._instance._init_db(resolved)
            return cls._instance

    def _init_db(self, db_path: str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        
        # We use check_same_thread=False because multiple threads (websocket, scanner, trade sync) might access the DB.
        # But we must serialize writes.
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.write_lock = threading.Lock()
        # The dashboard, scanner and reconciliation threads read while checkpoint
        # writes occur. WAL prevents readers from blocking that safety-critical write;
        # FULL keeps acknowledged trade state durable across an OS/power interruption.
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = FULL")
        self.conn.execute("PRAGMA foreign_keys = ON")
        
        self._create_tables()
        # CONSTRUCTION DOES NOT PRUNE. This used to call cleanup_old_records() on every
        # instantiation, suppressed only under pytest — so merely importing a module that
        # builds a db handle DELETED 30-day-old session_signals, signal_decisions and
        # closed trades from the production ledger, then VACUUMed it. Any script, any
        # ad-hoc `python -c` import, any tooling: a read-shaped action with a destructive
        # side effect, and nothing in the call looked like a delete.
        #
        # Retention is not lost. main.py:149 already runs the prune deliberately, once a
        # month, gated on a marker it only writes when the prune actually succeeds — and
        # that marker was meaningless while startup had already pruned on construction.
        # Explicit callers still work, which is what the cleanup tests exercise.
        if _running_under_pytest():
            logger.debug(f"SQLite test isolation active: {self.db_path}")


    def _snapshot_before_prune(self, doomed: int, cutoff_date: str) -> str:
        """A VERIFIED copy of the ledger, written before a prune removes anything.

        Raises on any failure, and the caller aborts the prune. The first version of
        this was "best effort", which protects nothing: if the snapshot failed while
        rows were eligible, the delete went ahead anyway and destroyed exactly the data
        the snapshot existed to preserve. A backup that is allowed to fail silently is
        not a backup. Retention is a scheduling preference; the rows are not
        recoverable, so the rows win.

        Uses sqlite3's online backup API, which is consistent against a live connection
        and correct under WAL. The earlier shutil.copy2 fallback was not: copying the
        main database file while a connection is open can capture a torn page set and
        leaves the -wal/-shm sidecars behind, so the "backup" may not open.

        Verified before it is trusted: PRAGMA integrity_check, plus a confirmation that
        the rows about to be deleted are actually present in the copy.
        """
        raw = str(getattr(self, "db_path", "") or "")
        if not raw or raw == ":memory:":
            # No durable file, so nothing a snapshot could preserve. The hazard this
            # guards is irreversible loss from the production ledger; an in-memory
            # database is ephemeral by construction and its rows do not outlive the
            # process either way. Returning rather than raising keeps the cleanup
            # contract testable against an isolated in-memory db, which is how
            # tests/test_sqlite_cleanup.py exercises it.
            return ""
        source = Path(raw)
        if not source.exists():
            raise FileNotFoundError(f"ledger missing at {source}")
        # Microseconds, not seconds: two prunes inside one second are rare but real, and
        # a name collision made the second silently reuse the first's snapshot, which
        # would describe the wrong pre-state.
        stamp = datetime.now(IST).strftime("%Y%m%d_%H%M%S_%f")
        target = source.with_name(f"{source.stem}.pre_prune_{stamp}{source.suffix}")

        dest = sqlite3.connect(str(target))
        try:
            self.conn.backup(dest)
        finally:
            dest.close()

        check = sqlite3.connect(str(target))
        try:
            status = check.execute("PRAGMA integrity_check").fetchone()[0]
            if str(status).lower() != "ok":
                raise sqlite3.DatabaseError(f"snapshot integrity_check said {status!r}")
            preserved = 0
            for table, column in (
                ("session_signals", "date"),
                ("signal_decisions", "date"),
            ):
                try:
                    preserved += int(
                        check.execute(
                            f"SELECT COUNT(*) FROM {table} WHERE {column} < ?",
                            (cutoff_date,),
                        ).fetchone()[0]
                    )
                except sqlite3.Error:
                    pass
            try:
                preserved += int(
                    check.execute(
                        "SELECT COUNT(*) FROM trades "
                        "WHERE COALESCE(exit_timestamp, exit_time) < ? "
                        "AND status = 'CLOSED'",
                        (cutoff_date,),
                    ).fetchone()[0]
                )
            except sqlite3.Error:
                pass
            if preserved <= 0 < doomed:
                raise sqlite3.DatabaseError(
                    "snapshot does not contain the rows about to be deleted"
                )
        finally:
            check.close()

        logger.info(
            f"Pre-prune snapshot verified: {target.name} "
            f"({doomed} rows older than {cutoff_date} about to be removed)"
        )
        return str(target)

    def cleanup_old_records(self, days=30):
        # Automatic cleanup is suppressed by _init_db during pytest imports.
        # Explicit calls remain executable so the cleanup contract can be tested
        # safely against an isolated/in-memory database.
        retention_days = max(1, int(days))
        # SQLite's date('now')/datetime('now') are UTC, but every row in these tables is
        # written on IST wall-clock, so the cutoff sat 5h30m behind the data: a "30 day"
        # retention was really 30 days and 5.5 hours. The skew was strictly conservative
        # — it over-retained and could never delete early — so this is the window meaning
        # what it says, not a data-loss fix. Computed in Python and bound as a literal
        # rather than stacking SQLite modifiers, which is easier to read and to test.
        cutoff_dt = datetime.now(IST) - timedelta(days=retention_days)
        cutoff_date = cutoff_dt.strftime("%Y-%m-%d")
        cutoff_ts = cutoff_dt.strftime("%Y-%m-%d %H:%M:%S")
        with self.write_lock:
            try:
                cursor = self.conn.cursor()
                # Snapshot BEFORE deleting, and only when there is something to delete.
                # The prune had no backup and no pre-counts, so when it fired
                # unintentionally there was no way to answer "what did that remove?" --
                # only to observe that the oldest surviving row sat one day inside the
                # cutoff. Skipping the snapshot when nothing matches keeps the common
                # case free: no copy, no VACUUM, no churn.
                # A COUNT that cannot be taken is not a count of zero. Swallowing the
                # failure left doomed at 0, which skipped the snapshot and let the
                # DELETE run anyway -- the backup bypassed by the one condition that
                # should have made it mandatory. Unknown now means "assume rows are at
                # risk", so the snapshot is taken and, if it fails, the prune aborts.
                doomed = 0
                countable = True
                for table, column in (
                    ("session_signals", "date"),
                    ("signal_decisions", "date"),
                ):
                    try:
                        doomed += int(
                            cursor.execute(
                                f"SELECT COUNT(*) FROM {table} WHERE {column} < ?",
                                (cutoff_date,),
                            ).fetchone()[0]
                        )
                    except Exception as exc:
                        logger.warning(f"Pre-prune count failed for {table}: {exc}")
                        countable = False
                try:
                    doomed += int(
                        cursor.execute(
                            "SELECT COUNT(*) FROM trades "
                            "WHERE COALESCE(exit_timestamp, exit_time) < ? "
                            "AND status = 'CLOSED'",
                            (cutoff_ts,),
                        ).fetchone()[0]
                    )
                except Exception as exc:
                    logger.warning(f"Pre-prune count failed for trades: {exc}")
                    countable = False

                if not countable and doomed <= 0:
                    doomed = -1  # unknown, and therefore treated as "something to lose"

                if doomed == 0:
                    # Genuinely nothing to delete: no snapshot, no DELETE, and no VACUUM.
                    # The previous version still VACUUMed on a no-op prune.
                    logger.debug(
                        f"Retention: nothing older than {cutoff_date}; nothing to do."
                    )
                    return True

                try:
                    self._snapshot_before_prune(doomed, cutoff_date)
                except Exception as exc:
                    logger.critical(
                        f"RETENTION ABORTED: rows older than {cutoff_date} were eligible "
                        f"for deletion but the pre-prune snapshot failed ({exc}). "
                        "Nothing was deleted."
                    )
                    return False

                cursor.execute(
                    "DELETE FROM session_signals WHERE date < ?",
                    (cutoff_date,),
                )
                cursor.execute(
                    "DELETE FROM signal_decisions WHERE date < ?",
                    (cutoff_date,),
                )
                cursor.execute(
                    "DELETE FROM trades "
                    "WHERE COALESCE(exit_timestamp, exit_time) < ? "
                    "AND status = 'CLOSED'",
                    (cutoff_ts,),
                )
                self.conn.commit()
                cursor.execute("VACUUM")
                logger.info("Database cleanup completed and vacuumed.")
                return True
            except Exception as e:
                # Roll back before reporting. The DELETEs run as one transaction and a
                # failure part-way through would otherwise leave some tables pruned and
                # others not, with no commit to mark where it stopped -- and the next
                # run's snapshot would then describe that half-pruned state as the
                # "before".
                try:
                    self.conn.rollback()
                except Exception as rollback_exc:
                    logger.error(f"Rollback after failed cleanup also failed: {rollback_exc}")
                logger.error(f"Error during DB cleanup: {e}")
                # Reported, not raised — but the caller needs to know, because the
                # monthly prune in main.py writes its "already done this month" marker
                # on the strength of this returning normally.
                return False
                
    def _create_tables(self):
        with self.write_lock:
            cursor = self.conn.cursor()
            
            # 1. Trades Table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS trades (
                    id TEXT PRIMARY KEY,
                    instrument TEXT,
                    timeframe TEXT,
                    direction TEXT,
                    entry_price REAL,
                    entry_time TEXT,
                    trailing_stop REAL,
                    current_stop REAL,
                    lots INTEGER,
                    lot_size INTEGER,
                    grade TEXT,
                    entry_timestamp REAL,
                    broker_order_id TEXT,
                    exit_order_id TEXT,
                    execution_status TEXT,
                    execution_error TEXT,
                    pending_execution INTEGER,
                    entry_submitted_at TEXT,
                    pending_exit INTEGER,
                    broker_quantity INTEGER,
                    trading_symbol TEXT,
                    symbol_token TEXT,
                    atm_strike REAL,
                    option_type TEXT,
                    target REAL,
                    rr_ratio REAL,
                    confidence REAL,
                    inst_type TEXT,
                    instrument_multiplier REAL,
                    exec_type TEXT,
                    status TEXT,
                    current_price REAL,
                    entry_spot REAL,
                    spot_stop REAL,
                    spot_target REAL,
                    exit_price REAL,
                    exit_time TEXT,
                    exit_timestamp TEXT,
                    exit_reason TEXT,
                    pnl REAL,
                    charges REAL,
                    peak_pnl REAL,
                    max_drawdown REAL,
                    initial_stop REAL,
                    scalp_lock_mode INTEGER,
                    market_wide_reversal_entry INTEGER,
                    volume_spike_entry INTEGER,
                    power_move INTEGER,
                    source_lane TEXT,
                    quality_verdict TEXT,
                    lifecycle_id TEXT,
                    runner_mode INTEGER,
                    runner_activation_timestamp TEXT,
                    runner_activation_reason TEXT,
                    runner_stop REAL,
                    runner_stop_basis TEXT,
                    stop_provenance TEXT
                )
            ''')
            
            self._ensure_columns(
                cursor,
                "trades",
                {
                    "entry_timestamp": "REAL",
                    "exit_timestamp": "TEXT",
                    "exit_order_id": "TEXT",
                    "execution_status": "TEXT",
                    "execution_error": "TEXT",
                    "pending_execution": "INTEGER",
                    "entry_submitted_at": "TEXT",
                    "pending_exit": "INTEGER",
                    "broker_quantity": "INTEGER",
                    "is_ghost": "INTEGER",
                    "market_regime": "TEXT",
                    "data_provenance": "TEXT",
                    "price_action_verdict": "TEXT",
                    "entry_intelligence_score": "REAL",
                    "intelligence_score": "REAL",
                    "broker_realised_cumulative": "REAL",
                    "strategy_revision": "TEXT",
                    "strategy_direction_mode": "TEXT",
                    "initial_stop": "REAL",
                    "scalp_lock_mode": "INTEGER",
                    "market_wide_reversal_entry": "INTEGER",
                    "volume_spike_entry": "INTEGER",
                    "power_move": "INTEGER",
                    "source_lane": "TEXT",
                    "underlying_entry": "REAL",
                    "underlying_stop": "REAL",
                    "underlying_target": "REAL",
                    "premium_entry": "REAL",
                    "premium_stop": "REAL",
                    "premium_target": "REAL",
                    "geometry_observed_at": "TEXT",
                    "geometry_source": "TEXT",
                    "geometry_verified": "INTEGER",
                    "geometry_schema": "TEXT",
                    "expected_move_pct": "REAL",
                    "expected_move_floor_pct": "REAL",
                    "expected_move_uncertainty_pct": "REAL",
                    "expected_move_probability": "REAL",
                    "expected_move_sample_count": "INTEGER",
                    "expected_move_horizon_minutes": "INTEGER",
                    "expected_move_generated_at": "TEXT",
                    "expected_move_data_as_of": "TEXT",
                    "expected_move_model_version": "TEXT",
                    "expected_move_source": "TEXT",
                    "entry_policy_allowed": "INTEGER",
                    "entry_policy_phase": "TEXT",
                    "entry_policy_checked_at": "TEXT",
                    "entry_policy_reason": "TEXT",
                    "entry_policy_actual_rr": "REAL",
                    "entry_policy_underlying_stop_pct": "REAL",
                    "entry_policy_version": "TEXT",
                    "quality_verdict": "TEXT",
                    "is_recovery": "INTEGER",
                    # Exchange-side protection state. Trade.from_dict() has always read
                    # these back, but nothing ever wrote them, so a restart forgot that a
                    # live SL-M order existed — emergency_flatten_trade() would then market
                    # out without cancelling it first, risking a double exit.
                    "protective_order_id": "TEXT",
                    "gtt_trigger_id": "TEXT",
                    "protective_trigger": "REAL",
                    "protective_quantity": "INTEGER",
                    "protective_updated_at": "TEXT",
                    "recovery_required_since": "TEXT",
                    "signal_time": "TEXT",
                    "entry_replace_count": "INTEGER",
                    "partial_exit_flatten_count": "INTEGER",
                    "reversal_watch_required": "INTEGER",
                    "is_reversion_scalp": "INTEGER",
                    "repaint_guard_checked": "INTEGER",
                    "entry_slippage": "REAL",
                    "exit_slippage": "REAL",
                    # Present in long-lived databases from an earlier schema, absent from
                    # a freshly created one — so they need migrating like anything else.
                    "requested_entry_price": "REAL",
                    "requested_exit_price": "REAL",
                    "entry_filled_at": "TEXT",
                    "exit_filled_at": "TEXT",
                    "fill_latency_ms": "REAL",
                    "exit_fill_latency_ms": "REAL",
                    "gtt_arm_uncertain": "INTEGER",
                    "partial_derisk_done": "INTEGER",
                    "partial_reduce_order_id": "TEXT",
                    "partial_reduce_requested_quantity": "INTEGER",
                    "scale_in_count": "INTEGER",
                    "scale_in_lots": "INTEGER",
                    "frozen_initial_risk_rs": "REAL",
                    "scale_in_order_id": "TEXT",
                    "scale_in_requested_quantity": "INTEGER",
                    "lifecycle_id": "TEXT",
                    "runner_mode": "INTEGER",
                    "runner_activation_timestamp": "TEXT",
                    "runner_activation_reason": "TEXT",
                    "runner_stop": "REAL",
                    "runner_stop_basis": "TEXT",
                    "stop_provenance": "TEXT",
                },
            )
            
            # 2. Session Signals Table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS session_signals (
                    id TEXT PRIMARY KEY,
                    date TEXT,
                    timestamp TEXT,
                    instrument TEXT,
                    timeframe TEXT,
                    direction TEXT,
                    price REAL,
                    confidence REAL,
                    grade TEXT,
                    executed INTEGER,
                    payload TEXT
                )
            ''')

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS signal_decisions (
                    id TEXT PRIMARY KEY,
                    date TEXT,
                    timestamp TEXT,
                    instrument TEXT,
                    timeframe TEXT,
                    direction TEXT,
                    status TEXT,
                    reason TEXT,
                    message TEXT,
                    source TEXT,
                    payload TEXT
                )
            ''')
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_signal_decisions_date "
                "ON signal_decisions(date, timestamp)"
            )

            # 4. Order fill ledger — who owns what, per ORDER rather than per contract.
            #
            # Kite reports POSITIONS per tradingsymbol, so when several trades sit on one
            # contract the broker cannot say which trade owns which quantity. Splitting
            # that aggregate by a rule works until a sibling exits, after which the
            # remainder has to be re-attributed and the rule is a guess. broker_quantity
            # SIZES protective stops, so a wrong guess arms the wrong size.
            #
            # An order, by contrast, belongs to exactly one trade and its fill is a fact
            # the broker states. Recording those facts makes ownership derivable instead
            # of inferred, and persisting them makes it survive the restart that would
            # otherwise lose the attribution entirely.
            #
            # order_id is the PRIMARY KEY, which is what makes recording idempotent:
            # retries, reconcile re-reads and restart replay all write the same row.
            # quantity_delta is SIGNED — an entry or add is positive, an exit negative —
            # so a trade's holding is just the sum, and a fully exited trade sums to zero
            # without needing a separate status.
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS order_fills (
                    order_id TEXT PRIMARY KEY,
                    trade_id TEXT NOT NULL,
                    tradingsymbol TEXT,
                    leg TEXT,
                    quantity_delta INTEGER NOT NULL,
                    tag TEXT,
                    recorded_at TEXT
                )
            ''')
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_order_fills_trade ON order_fills(trade_id)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_order_fills_symbol ON order_fills(tradingsymbol)"
            )

            # ── SIGNAL FORENSICS / FLIGHT RECORDER TABLE ─────────────────────
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS signal_lifecycle_events (
                    event_id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_date        TEXT NOT NULL,
                    event_ts            TEXT NOT NULL,
                    lifecycle_id        TEXT NOT NULL,
                    signal_id           TEXT,
                    instrument          TEXT NOT NULL,
                    contract            TEXT,
                    symbol_token        TEXT,
                    timeframe           TEXT,
                    direction           TEXT,
                    phase               TEXT NOT NULL,
                    decision            TEXT NOT NULL,
                    reason_code         TEXT,
                    reason_text         TEXT,
                    source              TEXT,
                    grade               TEXT,
                    confidence          REAL,
                    engine              TEXT,
                    lane                TEXT,
                    ltp                 REAL,
                    entry_price         REAL,
                    initial_stop        REAL,
                    current_stop        REAL,
                    initial_target      REAL,
                    current_target      REAL,
                    pnl                 REAL,
                    peak_pnl            REAL,
                    drawdown            REAL,
                    quote_source        TEXT,
                    quote_verified      INTEGER,
                    execution_mode      TEXT,
                    payload_json        TEXT,
                    state_fingerprint   TEXT
                )
            ''')
            # AUTO vs MANUAL. The forensics surface showed nothing for weeks because it
            # only ever received rows for trades, and in Manual mode a signal is
            # announced and never becomes a trade -- so an operator running manually had
            # an empty flight recorder with no way to tell that from a broken one.
            self._ensure_columns(
                cursor,
                "signal_lifecycle_events",
                {"execution_mode": "TEXT"},
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_forensics_session "
                "ON signal_lifecycle_events(session_date, event_id DESC)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_forensics_lifecycle "
                "ON signal_lifecycle_events(lifecycle_id, event_ts ASC)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_forensics_instrument_tf "
                "ON signal_lifecycle_events(session_date, instrument, timeframe)"
            )

            self.conn.commit()

    # ── order fill ledger ────────────────────────────────────────────────────

    def record_order_fill(
        self,
        order_id: str,
        trade_id: str,
        quantity_delta: int,
        *,
        tradingsymbol: str = "",
        leg: str = "",
        tag: str = "",
    ) -> bool:
        """Record what ONE order filled, for ONE trade. Idempotent on order_id.

        `quantity_delta` is signed: positive for an entry or add, negative for an exit.
        Recording the same order twice replaces the row rather than double-counting,
        which is what makes this safe to call from a retry, a reconcile re-read, or a
        restart replay.
        """
        oid = str(order_id or "").strip()
        tid = str(trade_id or "").strip()
        if not oid or not tid:
            # Without both, the row cannot be attributed and would poison the sum.
            return False
        try:
            delta = int(quantity_delta)
        except (TypeError, ValueError):
            return False
        with self.write_lock:
            try:
                self.conn.execute(
                    "INSERT OR REPLACE INTO order_fills "
                    "(order_id, trade_id, tradingsymbol, leg, quantity_delta, tag, recorded_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        oid, tid, str(tradingsymbol or ""), str(leg or ""), delta,
                        str(tag or ""), datetime.now(IST).isoformat(),
                    ),
                )
                self.conn.commit()
                return True
            except Exception as exc:
                logger.error(f"Failed to record order fill {oid} for {tid}: {exc}")
                return False

    def owned_quantity(self, trade_id: str) -> Optional[int]:
        """This trade's holding from its OWN fills, or None when the ledger is silent.

        None and 0 are different answers and callers must treat them differently:
        0 means "its own orders net to nothing", which is a fact; None means "no rows",
        which is no information at all. Collapsing the two is how an unrecorded trade
        would be read as flat and have its protection sized to nothing — or, worse in
        the other direction, fall back to intended size and be armed for quantity the
        account does not hold.
        """
        tid = str(trade_id or "").strip()
        if not tid:
            return None
        try:
            row = self.conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(quantity_delta), 0) FROM order_fills "
                "WHERE trade_id = ?",
                (tid,),
            ).fetchone()
        except Exception as exc:
            logger.error(f"Failed to read owned quantity for {tid}: {exc}")
            return None
        if not row or not int(row[0] or 0):
            return None
        return max(0, int(row[1] or 0))

    def owned_quantities_for_symbol(self, tradingsymbol: str) -> Dict[str, int]:
        """{trade_id: holding} for every trade the ledger describes on one contract."""
        symbol = str(tradingsymbol or "").strip()
        if not symbol:
            return {}
        try:
            rows = self.conn.execute(
                "SELECT trade_id, COALESCE(SUM(quantity_delta), 0) FROM order_fills "
                "WHERE tradingsymbol = ? GROUP BY trade_id",
                (symbol,),
            ).fetchall()
        except Exception as exc:
            logger.error(f"Failed to read owned quantities for {symbol}: {exc}")
            return {}
        return {str(r[0]): max(0, int(r[1] or 0)) for r in rows}

    def _ensure_columns(self, cursor: sqlite3.Cursor, table: str, columns: Dict[str, str]) -> None:
        """Add missing columns for DBs created by older restore points."""
        existing = {row[1] for row in cursor.execute(f"PRAGMA table_info({table})").fetchall()}
        for name, column_type in columns.items():
            if name not in existing:
                cursor.execute(f"ALTER TABLE {table} ADD COLUMN {name} {column_type}")
                logger.info(f"Added missing DB column {table}.{name}")

    def save_trade(self, trade_dict: dict) -> bool:
        """Insert or replace a trade record."""
        # SQLite doesn't natively support datetime objects without adapters, 
        # so we ensure entry_time and exit_time are strings.
        filtered_dict = {k: v for k, v in trade_dict.items() if k in TRADE_PERSISTED_COLUMNS}

        with self.write_lock:
            cursor = self.conn.cursor()
            
            columns = ', '.join(filtered_dict.keys())
            placeholders = ', '.join(['?'] * len(filtered_dict))
            
            # Handle datetime and structured-column serialization even when callers
            # provide a raw dataclass mapping instead of Trade.to_dict().
            values = [
                _encode_trade_value(column, value)
                for column, value in filtered_dict.items()
            ]

            sql = f'''
                INSERT OR REPLACE INTO trades ({columns}) 
                VALUES ({placeholders})
            '''
            try:
                cursor.execute(sql, values)
                self.conn.commit()
            except Exception as e:
                self.conn.rollback()
                logger.error(f"Error saving trade to DB: {e}")
                raise
        return True

    def save_trades(self, trade_dicts: List[Dict]) -> bool:
        """Persist a trade snapshot in one transaction instead of one commit per row."""
        grouped: Dict[tuple, List[tuple]] = {}
        for trade_dict in trade_dicts:
            filtered = {
                key: value
                for key, value in trade_dict.items()
                if key in TRADE_PERSISTED_COLUMNS
            }
            if not filtered:
                continue
            columns = tuple(filtered)
            values = [
                _encode_trade_value(key, filtered[key])
                for key in columns
            ]
            grouped.setdefault(columns, []).append(tuple(values))

        if not grouped:
            return True
        with self.write_lock:
            cursor = self.conn.cursor()
            try:
                for columns, rows in grouped.items():
                    column_sql = ", ".join(columns)
                    placeholders = ", ".join("?" for _ in columns)
                    cursor.executemany(
                        f"INSERT OR REPLACE INTO trades ({column_sql}) VALUES ({placeholders})",
                        rows,
                    )
                self.conn.commit()
            except Exception as exc:
                self.conn.rollback()
                logger.error(f"Error saving trade snapshot to DB: {exc}")
                raise
        return True

    def quarantine_open_trades(self, trade_ids: List[str], reason: str) -> int:
        """Atomically remove proven-invalid rows from the OPEN recovery set.

        The rows remain in SQLite for audit, but can no longer resurrect on a later
        restart when the broker instrument list is temporarily unavailable.
        """
        ids = sorted({str(item or "").strip() for item in trade_ids if str(item or "").strip()})
        if not ids:
            return 0
        changed = 0
        with self.write_lock:
            cursor = self.conn.cursor()
            try:
                # Stay below SQLite's common 999-parameter limit.
                for offset in range(0, len(ids), 900):
                    batch = ids[offset:offset + 900]
                    placeholders = ", ".join("?" for _ in batch)
                    cursor.execute(
                        f"""
                        UPDATE trades
                           SET status = 'QUARANTINED',
                               execution_status = 'QUARANTINED',
                               execution_error = ?,
                               exit_reason = 'DELISTED_CONTRACT_QUARANTINED',
                               exit_time = COALESCE(exit_time, ?)
                         WHERE status = 'OPEN' AND id IN ({placeholders})
                        """,
                        [str(reason or "Invalid open-trade recovery record")[:500],
                         datetime.now(IST).isoformat(), *batch],
                    )
                    changed += max(0, int(cursor.rowcount or 0))
                self.conn.commit()
            except Exception as exc:
                self.conn.rollback()
                logger.error(f"Error quarantining invalid open trades: {exc}")
                raise
        return changed

    def load_all_trades(self) -> List[Dict]:
        """Load all trades from the database."""
        with self.write_lock:
            cursor = self.conn.cursor()
            cursor.execute("SELECT * FROM trades")
            rows = cursor.fetchall()
            return [_decode_trade_row(row) for row in rows]

    _TRADABLE_SYMBOLS: Optional[frozenset] = None

    INSTRUMENT_LISTING_PATH = "data_store/kite_instruments.json"

    @classmethod
    def _tradable_symbols(cls, path: Optional[str] = None) -> Optional[frozenset]:
        """Every symbol the broker currently lists, or None if that is unknowable.

        None is the important return: it means "no opinion", and every caller must then
        keep whatever it has. A missing or unreadable instrument file must never be
        allowed to look like "this contract does not exist".
        """
        if path is None and cls._TRADABLE_SYMBOLS is not None:
            return cls._TRADABLE_SYMBOLS or None
        try:
            listing_path = Path(path or cls.INSTRUMENT_LISTING_PATH)
            if not listing_path.exists():
                return None
            with open(listing_path, encoding="utf-8") as handle:
                listing = json.load(handle)
            symbols = {
                str(row.get("symbol") or row.get("tradingsymbol") or "")
                for row in listing
                if isinstance(row, dict)
            }
            symbols.discard("")
            if len(symbols) < 1000:
                # A plausible instrument dump is tens of thousands of rows. Anything
                # smaller is a truncated or half-written file, and trusting it would
                # delete live positions.
                logger.warning(
                    f"Instrument list looks truncated ({len(symbols)} symbols); "
                    f"skipping the expired-contract check entirely."
                )
                return None
            result = frozenset(symbols)
            if path is None:
                cls._TRADABLE_SYMBOLS = result
            return result
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            logger.warning(f"Instrument list unreadable, keeping every open row: {exc}")
            return None

    def load_open_trades(self) -> List[Dict]:
        """Load only open trades from the database.

        A PURE accessor: it returns what is stored, unfiltered. The delisted-contract
        guard deliberately lives in TradeManager.load_state instead — filtering here
        would also change what the persistence round-trip reads back, and "save a row
        then read it again" must mean exactly that. See _tradable_symbols above.
        """
        with self.write_lock:
            cursor = self.conn.cursor()
            cursor.execute("SELECT * FROM trades WHERE status = 'OPEN'")
            rows = cursor.fetchall()
            return [_decode_trade_row(row) for row in rows]

    def load_closed_trades(self) -> List[Dict]:
        """Load only closed trades from the database."""
        with self.write_lock:
            cursor = self.conn.cursor()
            cursor.execute("SELECT * FROM trades WHERE status = 'CLOSED'")
            rows = cursor.fetchall()
            return [_decode_trade_row(row) for row in rows]

    def save_session_signals(self, date_str: str, signals: List[Dict]):
        """Replace the saved session candidates for one trading date."""
        with self.write_lock:
            cursor = self.conn.cursor()
            rows_to_save = []
            for signal_dict in signals:
                payload_day = self._payload_session_date(signal_dict)
                if payload_day and payload_day != date_str:
                    logger.warning(
                        f"Skipping stale session signal payload for {payload_day}; active ledger date is {date_str}"
                    )
                    continue
                sig_id = str(signal_dict.get("id") or signal_dict.get("trade_id") or "")
                if not sig_id:
                    sig_id = "|".join(
                        [
                            "SESSION",
                            str(signal_dict.get("instrument") or ""),
                            str(signal_dict.get("timeframe") or ""),
                            str(signal_dict.get("action") or "ENTRY"),
                            str(signal_dict.get("direction") or signal_dict.get("type") or ""),
                            str(signal_dict.get("timestamp") or signal_dict.get("signal_timestamp") or ""),
                        ]
                    )
                    signal_dict = dict(signal_dict)
                    signal_dict["id"] = sig_id
                rows_to_save.append((sig_id, signal_dict))

            cursor.execute("SELECT COUNT(*) AS row_count FROM session_signals WHERE date = ?", (date_str,))
            existing = int(cursor.fetchone()["row_count"] or 0)
            if existing and not rows_to_save:
                logger.warning(
                    f"Refusing to replace {existing} persisted session signal row(s) "
                    f"for {date_str} with an empty/stale transient ledger"
                )
                return
            cursor.execute("DELETE FROM session_signals WHERE date = ?", (date_str,))
            for sig_id, signal_dict in rows_to_save:
                payload_str = json.dumps(signal_dict)
                cursor.execute('''
                    INSERT OR REPLACE INTO session_signals 
                    (id, date, timestamp, instrument, timeframe, direction, price, confidence, grade, executed, payload)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    sig_id,
                    date_str,
                    signal_dict.get('timestamp'),
                    signal_dict.get('instrument'),
                    signal_dict.get('timeframe'),
                    signal_dict.get('direction', signal_dict.get('type')),
                    signal_dict.get('price', 0.0),
                    signal_dict.get('confidence', 0.0),
                    signal_dict.get('grade', ''),
                    1 if signal_dict.get('executed') else 0,
                    payload_str
                ))
            self.conn.commit()

    def load_session_signals(self, date_str: str) -> List[Dict]:
        """Retrieve all session candidates for a given date."""
        with self.write_lock:
            cursor = self.conn.cursor()
            cursor.execute("SELECT id, payload FROM session_signals WHERE date = ?", (date_str,))
            rows = cursor.fetchall()
            signals = []
            stale_ids = []
            for row in rows:
                try:
                    payload = json.loads(row['payload'])
                except json.JSONDecodeError:
                    stale_ids.append(row['id'])
                    continue
                payload_day = self._payload_session_date(payload)
                if payload_day and payload_day != date_str:
                    stale_ids.append(row['id'])
                    continue
                signals.append(payload)
            if stale_ids:
                cursor.executemany("DELETE FROM session_signals WHERE id = ?", [(sid,) for sid in stale_ids])
                self.conn.commit()
                logger.warning(f"Purged {len(stale_ids)} stale/corrupt session signal rows for {date_str}")
            return signals

    def list_session_signal_dates(self) -> List[str]:
        """Return persisted session ledger dates in ascending order."""
        with self.write_lock:
            cursor = self.conn.cursor()
            cursor.execute("SELECT DISTINCT date FROM session_signals ORDER BY date")
            return [str(row["date"]) for row in cursor.fetchall() if row["date"]]

    def save_signal_decision(self, decision: Dict[str, Any]) -> None:
        """Persist one live rejection/block decision for daily reporting."""
        timestamp = str(decision.get("timestamp") or datetime.now(IST).isoformat())
        date_str = str(decision.get("date") or timestamp[:10])
        decision_id = str(decision.get("id") or "")
        if not decision_id:
            decision_id = "|".join(
                [
                    date_str,
                    timestamp,
                    str(decision.get("instrument") or ""),
                    str(decision.get("timeframe") or ""),
                    str(decision.get("direction") or ""),
                    str(decision.get("message") or ""),
                ]
            )
        payload = dict(decision)
        payload["id"] = decision_id
        payload["date"] = date_str
        payload["timestamp"] = timestamp
        with self.write_lock:
            cursor = self.conn.cursor()
            cursor.execute(
                '''
                INSERT OR IGNORE INTO signal_decisions
                (id, date, timestamp, instrument, timeframe, direction, status, reason, message, source, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    decision_id,
                    date_str,
                    timestamp,
                    decision.get("instrument", ""),
                    decision.get("timeframe", ""),
                    decision.get("direction", ""),
                    decision.get("status", "REJECTED"),
                    decision.get("reason", ""),
                    decision.get("message", ""),
                    decision.get("source", "runtime"),
                    json.dumps(payload),
                ),
            )
            self.conn.commit()

    def load_signal_decisions(self, date_str: str) -> List[Dict[str, Any]]:
        """Load persisted rejection/block decisions for one session date."""
        with self.write_lock:
            cursor = self.conn.cursor()
            cursor.execute(
                "SELECT * FROM signal_decisions WHERE date = ? ORDER BY timestamp",
                (date_str,),
            )
            rows = []
            for row in cursor.fetchall():
                item = dict(row)
                try:
                    payload = json.loads(item.get("payload") or "{}")
                    if isinstance(payload, dict):
                        item.update(payload)
                except Exception:
                    pass
                rows.append(item)
            return rows

    @staticmethod
    def _payload_session_date(payload: Dict) -> Optional[str]:
        """Return the IST session date encoded in a persisted signal payload."""
        raw_ts = payload.get("signal_timestamp") or payload.get("timestamp")
        if not raw_ts:
            return None
        if isinstance(raw_ts, datetime):
            ts = raw_ts
        else:
            raw = str(raw_ts).strip()
            if len(raw) >= 10 and raw[4] == "-" and raw[7] == "-":
                try:
                    ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                except ValueError:
                    return raw[:10]
            else:
                return None
        if ts.tzinfo is not None:
            return ts.astimezone(IST).date().isoformat()
        return ts.date().isoformat()

    # ── signal forensics / flight recorder CRUD ──────────────────────────────

    _last_forensic_fingerprints: Dict[str, str] = {}

    @staticmethod
    def _forensic_fingerprint(event: Dict[str, Any]) -> str:
        """SHA-256 based fingerprint for deduplication of identical consecutive states."""
        import hashlib
        key_props = (
            str(event.get("lifecycle_id") or ""),
            str(event.get("phase") or ""),
            str(event.get("decision") or ""),
            str(event.get("reason_code") or ""),
            str(event.get("reason_text") or ""),
            # AUTO and MANUAL are separate operator/execution facts.  Without this a
            # mode transition with identical price geometry collapsed as a duplicate and
            # left the forensic flight recorder saying the wrong mode.
            str(event.get("execution_mode") or "").upper(),
            f"{float(event.get('current_stop') or 0.0):.2f}",
            f"{float(event.get('current_target') or 0.0):.2f}",
            f"{float(event.get('pnl') or 0.0):.2f}",
        )
        raw = "|".join(key_props)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    def append_signal_lifecycle_event(self, event: Dict[str, Any]) -> Optional[int]:
        """Append one signal lifecycle event with fingerprint deduplication."""
        fp = self._forensic_fingerprint(event)
        lifecycle_id = str(event.get("lifecycle_id") or "")
        payload = event.get("payload")
        payload_json = json.dumps(payload, separators=(",", ":")) if isinstance(payload, (dict, list)) else (payload or "")
        with self.write_lock:
            # Suppress identical consecutive state for the same lifecycle
            last_fp = self._last_forensic_fingerprints.get(lifecycle_id)
            if last_fp == fp:
                return None
            self._last_forensic_fingerprints[lifecycle_id] = fp
            try:
                cur = self.conn.execute(
                    """
                    INSERT INTO signal_lifecycle_events (
                        session_date, event_ts, lifecycle_id, signal_id, instrument,
                        contract, symbol_token, timeframe, direction, phase, decision,
                        reason_code, reason_text, source, grade, confidence, engine,
                        lane, ltp, entry_price, initial_stop, current_stop, initial_target,
                        current_target, pnl, peak_pnl, drawdown, quote_source,
                        quote_verified, execution_mode, payload_json, state_fingerprint
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        str(event.get("session_date") or ""),
                        str(event.get("event_ts") or ""),
                        lifecycle_id,
                        event.get("signal_id"),
                        str(event.get("instrument") or ""),
                        event.get("contract"),
                        event.get("symbol_token"),
                        event.get("timeframe"),
                        event.get("direction"),
                        str(event.get("phase") or "DETECTED"),
                        str(event.get("decision") or "PENDING"),
                        event.get("reason_code"),
                        event.get("reason_text"),
                        event.get("source"),
                        event.get("grade"),
                        event.get("confidence"),
                        event.get("engine"),
                        event.get("lane"),
                        event.get("ltp"),
                        event.get("entry_price"),
                        event.get("initial_stop"),
                        event.get("current_stop"),
                        event.get("initial_target"),
                        event.get("current_target"),
                        event.get("pnl"),
                        event.get("peak_pnl"),
                        event.get("drawdown"),
                        event.get("quote_source"),
                        1 if event.get("quote_verified") else 0,
                        str(event.get("execution_mode") or "") or None,
                        payload_json,
                        fp,
                    ),
                )
                self.conn.commit()
                return cur.lastrowid
            except Exception as exc:
                logger.debug(f"Signal forensics insert failed: {exc}")
                return None

    def append_signal_lifecycle_events(self, events: List[Dict[str, Any]]) -> int:
        """Batch append signal lifecycle events with in-memory deduplication."""
        if not events:
            return 0
        to_insert = []
        with self.write_lock:
            for event in events:
                fp = self._forensic_fingerprint(event)
                lifecycle_id = str(event.get("lifecycle_id") or "")
                last_fp = self._last_forensic_fingerprints.get(lifecycle_id)
                if last_fp == fp:
                    continue
                self._last_forensic_fingerprints[lifecycle_id] = fp
                payload = event.get("payload")
                payload_json = json.dumps(payload, separators=(",", ":")) if isinstance(payload, (dict, list)) else (payload or "")
                to_insert.append((
                    str(event.get("session_date") or ""),
                    str(event.get("event_ts") or ""),
                    lifecycle_id,
                    event.get("signal_id"),
                    str(event.get("instrument") or ""),
                    event.get("contract"), event.get("symbol_token"),
                    event.get("timeframe"), event.get("direction"),
                    str(event.get("phase") or "DETECTED"),
                    str(event.get("decision") or "PENDING"),
                    event.get("reason_code"), event.get("reason_text"),
                    event.get("source"), event.get("grade"),
                    event.get("confidence"), event.get("engine"),
                    event.get("lane"), event.get("ltp"),
                    event.get("entry_price"), event.get("initial_stop"),
                    event.get("current_stop"), event.get("initial_target"),
                    event.get("current_target"), event.get("pnl"),
                    event.get("peak_pnl"), event.get("drawdown"),
                    event.get("quote_source"),
                    1 if event.get("quote_verified") else 0,
                    str(event.get("execution_mode") or "") or None,
                    payload_json, fp,
                ))
            if not to_insert:
                return 0
            try:
                self.conn.executemany(
                    """
                    INSERT INTO signal_lifecycle_events (
                        session_date, event_ts, lifecycle_id, signal_id, instrument,
                        contract, symbol_token, timeframe, direction, phase, decision,
                        reason_code, reason_text, source, grade, confidence, engine,
                        lane, ltp, entry_price, initial_stop, current_stop, initial_target,
                        current_target, pnl, peak_pnl, drawdown, quote_source,
                        quote_verified, execution_mode, payload_json, state_fingerprint
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    to_insert,
                )
                self.conn.commit()
                return len(to_insert)
            except Exception as exc:
                logger.debug(f"Signal forensics batch insert failed: {exc}")
                return 0

    @staticmethod
    def _decode_forensic_row(row: sqlite3.Row) -> Dict[str, Any]:
        """Convert a sqlite3.Row to a dict, parsing payload_json."""
        item = dict(row)
        raw_payload = item.pop("payload_json", None)
        if raw_payload:
            try:
                item["payload"] = json.loads(raw_payload)
            except Exception:
                item["payload"] = raw_payload
        else:
            item["payload"] = {}
        item["quote_verified"] = bool(item.get("quote_verified"))
        return item

    def query_signal_lifecycle_events(
        self,
        *,
        session_date: Optional[str] = None,
        cursor: Optional[str] = None,
        limit: int = 100,
        lifecycle_id: Optional[str] = None,
        instrument: Optional[str] = None,
        timeframe: Optional[str] = None,
        phase: Optional[str] = None,
        decision: Optional[str] = None,
        search: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Keyset-cursor paginated query for signal forensics events."""
        where = []
        params: List[Any] = []
        if session_date:
            where.append("session_date = ?")
            params.append(session_date)
        if lifecycle_id:
            where.append("lifecycle_id = ?")
            params.append(lifecycle_id)
        if instrument:
            where.append("instrument = ?")
            params.append(instrument)
        if timeframe:
            where.append("timeframe = ?")
            params.append(timeframe)
        if phase:
            where.append("phase = ?")
            params.append(phase)
        if decision:
            where.append("decision = ?")
            params.append(decision)
        if search:
            where.append("(reason_code LIKE ? OR reason_text LIKE ? OR source LIKE ? OR contract LIKE ?)")
            pattern = f"%{search}%"
            params.extend([pattern, pattern, pattern, pattern])

        where_clause = f"WHERE {' AND '.join(where)}" if where else ""

        # Totals
        count_sql = f"SELECT COUNT(*), COUNT(DISTINCT lifecycle_id) FROM signal_lifecycle_events {where_clause}"
        total, signal_total = self.conn.execute(count_sql, params).fetchone()

        latest_row = self.conn.execute(
            f"SELECT MAX(event_id) FROM signal_lifecycle_events {where_clause}",
            params,
        ).fetchone()
        latest_event_id = latest_row[0] if latest_row and latest_row[0] else 0

        # Cursor-based pagination
        page_params = list(params)
        page_where = list(where) if where else []
        if cursor:
            page_where.append("event_id < ?")
            page_params.append(int(cursor))

        page_clause = f"WHERE {' AND '.join(page_where)}" if page_where else ""
        clamped_limit = max(1, min(int(limit), 500))
        sql = f"""
            SELECT * FROM signal_lifecycle_events
            {page_clause}
            ORDER BY event_id DESC
            LIMIT ?
        """
        page_params.append(clamped_limit)
        rows = self.conn.execute(sql, page_params).fetchall()
        items = [self._decode_forensic_row(r) for r in rows]

        next_cursor = str(items[-1]["event_id"]) if len(items) == clamped_limit else None
        return {
            "items": items,
            "next_cursor": next_cursor,
            "total": total,
            "signal_total": signal_total,
            "latest_event_id": latest_event_id,
        }

    def load_signal_lifecycle(self, lifecycle_id: str) -> List[Dict[str, Any]]:
        """Load full chronological timeline for a single lifecycle."""
        rows = self.conn.execute(
            "SELECT * FROM signal_lifecycle_events WHERE lifecycle_id = ? ORDER BY event_id ASC",
            (lifecycle_id,),
        ).fetchall()
        return [self._decode_forensic_row(r) for r in rows]

    def list_signal_forensics_dates(self) -> List[str]:
        """Return distinct session dates, newest first."""
        rows = self.conn.execute(
            "SELECT DISTINCT session_date FROM signal_lifecycle_events ORDER BY session_date DESC"
        ).fetchall()
        return [row[0] for row in rows if row[0]]

    def purge_old_signal_lifecycle_events(self) -> int:
        """Retain only the 2 most recent session dates."""
        dates = self.list_signal_forensics_dates()
        if len(dates) <= 2:
            return 0
        cutoff_dates = dates[2:]
        with self.write_lock:
            cur = self.conn.execute(
                f"DELETE FROM signal_lifecycle_events WHERE session_date IN ({','.join(['?'] * len(cutoff_dates))})",
                cutoff_dates,
            )
            self.conn.commit()
            return int(cur.rowcount or 0)

# Global instance
db = DatabaseManager()
