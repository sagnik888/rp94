import asyncio
import requests
import pandas as pd
import json
import os
import tempfile
import threading
from datetime import datetime, date, time as dt_time, timedelta
from typing import Dict, List, Optional, Set
from loguru import logger
from pathlib import Path

class ExpiryManager:
    """
    Intelligent Expiry Resolver for AngelOne
    Resolves Weekly/Monthly expiries, handling holiday shifts and instrument specific rules.
    """
    
    TOKEN_URL = "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"
    CACHE_DIR = Path("data_store")
    CACHE_FILE = CACHE_DIR / "instruments.json"
    OPTION_ROLLOVER_DAYS = 2  # legacy calendar constant; option rollover now uses
    # option_rollover_working_days (trading-day, holiday-aware) — see _trading_days_until.

    def __init__(self):
        self.df: Optional[pd.DataFrame] = None
        self.expiries: Dict[str, Dict] = {}
        self.CACHE_DIR.mkdir(exist_ok=True)
        self._loaded_date = date.today()
        self._token_cache: Dict[tuple, Dict] = {}
        self._state_lock = threading.RLock()
        self._refresh_lock = threading.RLock()
        self._bg_download_pending = False
        self._refresh_thread: Optional[threading.Thread] = None
        self._staged_df: Optional[pd.DataFrame] = None
        self._snapshot_generation = 0

    def download_master(self, force=False):
        """Load a candidate master without exposing a half-analyzed snapshot."""
        with self._refresh_lock:
            try:
                if not force and self.CACHE_FILE.exists():
                    file_time = datetime.fromtimestamp(self.CACHE_FILE.stat().st_mtime).date()
                    if file_time == date.today():
                        logger.info("Using cached instrument master.")
                        data = json.loads(self.CACHE_FILE.read_text(encoding="utf-8"))
                        self._staged_df = pd.DataFrame(data)
                        return not self._staged_df.empty

                logger.info("Downloading instrument master from AngelOne...")
                response = requests.get(self.TOKEN_URL, timeout=30)
                response.raise_for_status()
                data = response.json()
                candidate = pd.DataFrame(data)
                if candidate.empty:
                    raise ValueError("instrument master response was empty")
                self.CACHE_DIR.mkdir(parents=True, exist_ok=True)
                fd, tmp_name = tempfile.mkstemp(
                    prefix="instruments_", suffix=".json", dir=str(self.CACHE_DIR)
                )
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as handle:
                        json.dump(data, handle)
                    os.replace(tmp_name, self.CACHE_FILE)
                except Exception:
                    try:
                        os.unlink(tmp_name)
                    except Exception:
                        pass
                    raise
                self._staged_df = candidate
                logger.success("Instrument master downloaded and staged.")
                return True
            except Exception as exc:
                logger.error(f"Failed to download instruments: {exc}")
                csv_path = Path("data_store/instruments.csv")
                if csv_path.exists():
                    age_days = max(
                        0,
                        (datetime.now() - datetime.fromtimestamp(csv_path.stat().st_mtime)).days,
                    )
                    if age_days <= 7:
                        logger.warning(
                            f"Falling back to {csv_path} ({age_days} day(s) old)."
                        )
                        self._staged_df = pd.read_csv(csv_path)
                        return not self._staged_df.empty
                    logger.error(
                        f"Refusing stale fallback instrument CSV ({age_days} days old)."
                    )
                return False

    def analyze_expiries(self):
        """Analyze into locals, then atomically publish the complete snapshot."""
        with self._refresh_lock:
            source = self._staged_df if self._staged_df is not None else self.df
            if source is None or source.empty:
                logger.warning("No data to analyze expiries.")
                return {}
            frame = source.copy()
            resolved = self._build_expiry_map(frame)
            if not resolved:
                logger.error("Instrument master produced no usable expiry snapshot.")
                return {}
            with self._state_lock:
                self.df = frame
                self.expiries = resolved
                self._token_cache = {}
                self._loaded_date = date.today()
                self._snapshot_generation += 1
            self._staged_df = None
            return dict(resolved)

    def _build_expiry_map(self, frame: pd.DataFrame) -> Dict[str, Dict]:
        """Pure expiry calculation for one immutable master candidate."""
        required = {"name", "instrumenttype", "expiry", "symbol", "token"}
        missing = required.difference(frame.columns)
        if missing:
            logger.error(f"Instrument master missing required columns: {sorted(missing)}")
            return {}

        indices = ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"]
        frame["expiry_dt"] = pd.to_datetime(
            frame["expiry"], format="%d%b%Y", errors="coerce"
        )
        resolved: Dict[str, Dict] = {}
        for name in indices:
            # 1. Filter Futures
            fut_df = frame[(frame['name'] == name) & (frame['instrumenttype'] == 'FUTIDX')].copy()
            fut_df = fut_df.sort_values('expiry_dt')
            
            # 2. Filter Options
            opt_df = frame[(frame['name'] == name) & (frame['instrumenttype'].isin(['OPTIDX']))].copy()
            opt_df = opt_df.sort_values('expiry_dt')
            
            all_expiries = sorted(opt_df['expiry_dt'].dropna().unique())
            now = pd.Timestamp(date.today())
            
            # Current/Nearest Expiry (Weekly)
            future_expiries = [d for d in all_expiries if d >= now]
            nearest_expiry = future_expiries[0] if len(future_expiries) > 0 else None
            next_weekly = future_expiries[1] if len(future_expiries) > 1 else nearest_expiry
            
            # Determine Monthly Expiries (Last expiry of each month)
            monthly_list = []
            for exp in future_expiries:
                m_list = [d for d in all_expiries if d.month == exp.month and d.year == exp.year]
                if m_list:
                    monthly_list.append(m_list[-1])
            
            unique_monthlies = sorted(list(set(monthly_list)))
            monthly_expiry = unique_monthlies[0] if len(unique_monthlies) > 0 else None
            next_monthly = unique_monthlies[1] if len(unique_monthlies) > 1 else monthly_expiry

            # Rollover in TRADING days (holiday-aware), not calendar days. Roll only on the
            # last trading session before expiry (+ expiry day) when threshold == 1.
            roll_days = self._rollover_working_days()
            today = now.date() if hasattr(now, "date") else now
            wdte_weekly = self._trading_days_until(today, nearest_expiry.date()) if nearest_expiry is not None else 10
            wdte_monthly = self._trading_days_until(today, monthly_expiry.date()) if monthly_expiry is not None else 10

            is_weekly_rollover = wdte_weekly <= roll_days
            is_monthly_rollover = wdte_monthly <= roll_days

            # 3. Determine Final Active Expiries based on rollover rules
            # NIFTY/SENSEX use weekly contracts and roll to next_weekly at T-2.
            final_weekly = next_weekly if is_weekly_rollover else nearest_expiry
            
            # BANKNIFTY/MIDCPNIFTY use monthly contracts and roll to next_monthly at T-2.
            final_monthly = next_monthly if is_monthly_rollover else monthly_expiry

            # 4. Futures Rollover (1 day before expiry rule)
            current_fut_expiry = fut_df.iloc[0]['expiry_dt'] if not fut_df.empty else None
            fut_dte = (current_fut_expiry - now).days if current_fut_expiry else 10
            is_fut_rollover = fut_dte <= 1
            
            fut_idx = 1 if (is_fut_rollover and len(fut_df) > 1) else 0
            if is_fut_rollover and len(fut_df) > 1:
                logger.info(f"🔄 Futures Rollover triggered for {name} (DTE: {fut_dte}). Using next month's contract.")

            # Results Mapping
            resolved[name] = {
                "weekly": nearest_expiry.strftime('%d%b%Y').upper() if nearest_expiry else None,
                "next_weekly": next_weekly.strftime('%d%b%Y').upper() if next_weekly else None,
                "monthly": monthly_expiry.strftime('%d%b%Y').upper() if monthly_expiry else None,
                "next_monthly": next_monthly.strftime('%d%b%Y').upper() if next_monthly else None,
                "active_weekly": final_weekly.strftime('%d%b%Y').upper() if final_weekly else None,
                "active_monthly": final_monthly.strftime('%d%b%Y').upper() if final_monthly else None,
                "current_fut": fut_df.iloc[fut_idx]['symbol'] if not fut_df.empty else None,
                "current_fut_token": fut_df.iloc[fut_idx]['token'] if not fut_df.empty else None,
                "is_monthly_week": nearest_expiry == monthly_expiry if nearest_expiry and monthly_expiry else False,
                "is_weekly_rollover": is_weekly_rollover,
                "is_monthly_rollover": is_monthly_rollover
            }
            
            logger.info(f"Resolved {name}: Weekly={resolved[name]['weekly']}, Monthly={resolved[name]['monthly']}")
            if is_weekly_rollover: logger.info(f"⚠️ {name} Weekly Rollover Active: Using {resolved[name]['active_weekly']}")
            if is_monthly_rollover: logger.info(f"⚠️ {name} Monthly Rollover Active: Using {resolved[name]['active_monthly']}")
        return resolved

    def _ensure_expiry_dates(self):
        if self.df is not None and "expiry_dt" not in self.df.columns:
            self.df["expiry_dt"] = pd.to_datetime(self.df["expiry"], format="%d%b%Y", errors="coerce")

    def _ensure_ready(self) -> bool:
        """Load and analyze the instrument master before token lookups."""
        needs_reload = (
            self.df is None
            or self.df.empty
            or not self.expiries
            or (hasattr(self, "_loaded_date") and date.today() != self._loaded_date)
        )
        if needs_reload:
            usable = self.df is not None and not self.df.empty and bool(self.expiries)
            if usable:
                self._start_background_refresh()
                return True
            # Token/expiry lookups are called from the scanner's asyncio loop.
            # A cold cache must fail closed and warm in the background instead
            # of running the 30-second HTTP download on that latency-sensitive
            # loop.  Synchronous startup callers may still initialize eagerly.
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                pass
            else:
                self._start_background_refresh()
                return False
            return bool(self.pre_market_check())
        return self.df is not None and not self.df.empty and bool(self.expiries)

    def _start_background_refresh(self) -> bool:
        with self._refresh_lock:
            if self._bg_download_pending:
                return False
            self._bg_download_pending = True

        def _refresh() -> None:
            try:
                if self.download_master(force=True):
                    self.analyze_expiries()
            except Exception as exc:
                logger.warning(f"Background instrument master refresh failed: {exc}")
            finally:
                with self._refresh_lock:
                    self._bg_download_pending = False

        thread = threading.Thread(
            target=_refresh, daemon=True, name="InstrumentMasterRefresh"
        )
        self._refresh_thread = thread
        thread.start()
        return True

    _holiday_cache: Optional[Set[str]] = None
    _special_session_cache: Optional[Set[str]] = None

    @classmethod
    def _load_calendar(cls):
        if cls._holiday_cache is None:
            try:
                from data.market_calendar import load_nse_holidays, load_nse_special_trading_days
                cls._holiday_cache = load_nse_holidays()
                cls._special_session_cache = load_nse_special_trading_days()
            except Exception:
                cls._holiday_cache = set()
                cls._special_session_cache = set()
        return cls._holiday_cache, cls._special_session_cache

    @classmethod
    def _is_trading_day(cls, d: date) -> bool:
        hols, specials = cls._load_calendar()
        ds = d.strftime("%Y-%m-%d")
        if ds in specials:
            return True
        return d.weekday() < 5 and ds not in hols

    @classmethod
    def _trading_days_until(cls, start_date: date, end_date: date) -> int:
        """Number of trading sessions in (start_date, end_date] — holiday/weekend aware.
        0 when start_date is the expiry itself; 1 on the last trading day before it."""
        if end_date <= start_date:
            return 0
        count = 0
        d = start_date + timedelta(days=1)
        while d <= end_date:
            if cls._is_trading_day(d):
                count += 1
            d += timedelta(days=1)
        return count

    @staticmethod
    def _rollover_working_days() -> int:
        try:
            from config.settings import get_settings
            return int(getattr(get_settings(), "option_rollover_working_days", 1) or 1)
        except Exception:
            return 1

    @staticmethod
    def _parse_hhmm(val, default=dt_time(13, 0)) -> dt_time:
        try:
            h, m = str(val or "").split(":")
            return dt_time(int(h), int(m))
        except Exception:
            return default

    def expiry_day_override(self, current_expiry, next_expiry, target_date: date,
                            now_dt=None) -> Optional[str]:
        """Decide whether expiry-day gamma mode overrides the T-2 rollover.

        Pure function of settings + the bar's date/time (no side effects), so it is
        identical in live and backtest. Returns:
          None       -> no override; apply the normal T-2 rollover (unchanged behaviour)
          "current"  -> use the CURRENT (expiring) expiry for max gamma
          "futures"  -> after-cutoff 'futures' mode; caller should route to the index FUT
        """
        try:
            from config.settings import get_settings
            s = get_settings()
        except Exception:
            return None
        if not bool(getattr(s, "expiry_day_trade_current_week", False)):
            return None
        if current_expiry is None or next_expiry is None or current_expiry == next_expiry:
            return None
        try:
            if target_date > current_expiry.date():
                return None
            # Trading days to expiry (holiday-aware): 0 on expiry day, 1 on the last
            # trading session before it.
            dte = self._trading_days_until(target_date, current_expiry.date())
        except Exception:
            return None
        include_t1 = bool(getattr(s, "expiry_day_include_t1", False))
        if dte == 1:
            # Day before expiry: theta still mild, ride the gamma all session (no cutoff).
            return "current" if include_t1 else None
        if dte != 0:
            return None
        # Expiry day (T-0): morning = gamma; midday lull = after-cutoff mode; then the LATE
        # settlement window (default 14:00-15:15) re-engages gamma for the last-hour move.
        cutoff = self._parse_hhmm(getattr(s, "expiry_day_gamma_cutoff", "13:00"))
        now_t = getattr(now_dt, "time", None)
        now_t = now_t() if callable(now_t) else None
        if now_t is None or now_t < cutoff:
            return "current"
        if bool(getattr(s, "expiry_day_late_window", True)):
            lstart = self._parse_hhmm(getattr(s, "expiry_day_late_window_start", "14:00"), dt_time(14, 0))
            lend = self._parse_hhmm(getattr(s, "expiry_day_late_window_end", "15:15"), dt_time(15, 15))
            if lstart <= now_t < lend:
                return "current"
        mode = str(getattr(s, "expiry_day_after_cutoff_mode", "rollover") or "rollover").lower()
        if mode == "futures":
            return "futures"
        if mode == "momentum_current":
            # Keep current-week ATM; the system's existing momentum/quality gates are what
            # filter this down to big, short-tenure moves.
            return "current"
        return None  # "rollover" (safe default) -> normal next-week behaviour

    def in_gamma_window(self, name: str, now_dt=None) -> bool:
        """True when expiry-day gamma mode would have the system trading the CURRENT
        (expiring) expiry right now — used by the theta-crush risk controls (tighter SL,
        early time-exit, momentum gate). Short-circuits to False when the toggle is off so
        it costs nothing in the default configuration."""
        try:
            from config.settings import get_settings
            if not bool(getattr(get_settings(), "expiry_day_trade_current_week", False)):
                return False
        except Exception:
            return False
        try:
            now_dt = now_dt or datetime.now()
            td = now_dt.date()
            self._ensure_expiry_dates()
            opt = self.df[(self.df["name"] == name)
                          & (self.df["instrumenttype"].isin(["OPTIDX"]))
                          & (self.df["expiry_dt"].dt.date >= td)]
            exps = sorted(opt["expiry_dt"].dropna().unique())
            if name in {"BANKNIFTY", "MIDCPNIFTY"}:
                monthlies = []
                for e in exps:
                    same = [d for d in exps if d.month == e.month and d.year == e.year]
                    if same:
                        monthlies.append(same[-1])
                exps = sorted(set(monthlies))
            if not exps:
                return False
            cur = pd.Timestamp(exps[0])
            nxt = pd.Timestamp(exps[1]) if len(exps) > 1 else cur
            return self.expiry_day_override(cur, nxt, td, now_dt) == "current"
        except Exception:
            return False

    def _select_option_expiry_for_date(self, name: str, target_date: date, expiry_type: str = "weekly",
                                       now_dt=None) -> Optional[pd.Timestamp]:
        self._ensure_expiry_dates()
        opt_df = self.df[
            (self.df["name"] == name)
            & (self.df["instrumenttype"].isin(["OPTIDX"]))
            & (self.df["expiry_dt"].dt.date >= target_date)
        ].copy()
        if opt_df.empty:
            return None

        all_expiries = sorted(opt_df["expiry_dt"].dropna().unique())
        if expiry_type in ("monthly", "active_monthly"):
            monthlies = []
            for exp in all_expiries:
                same_month = [d for d in all_expiries if d.month == exp.month and d.year == exp.year]
                if same_month:
                    monthlies.append(same_month[-1])
            candidates = sorted(set(monthlies))
        else:
            candidates = all_expiries

        if not candidates:
            return None

        current = candidates[0]
        next_expiry = candidates[1] if len(candidates) > 1 else current
        # Expiry-day gamma mode (gated, default off): may keep the CURRENT expiring expiry
        # during the T-2..T-0 window instead of rolling. "futures" is a routing hint handled
        # upstream, so here it falls through to the normal rollover (rolled next-week option).
        override = self.expiry_day_override(current, next_expiry, target_date, now_dt)
        if override == "current":
            return pd.Timestamp(current)
        wdte = self._trading_days_until(target_date, current.date())
        if wdte <= self._rollover_working_days() and next_expiry != current:
            return pd.Timestamp(next_expiry)
        return pd.Timestamp(current)

    def get_token_for_date(self, name: str, strike: float, option_type: str, target_date: date, expiry_type: str = "weekly", now_dt=None) -> Optional[Dict]:
        """Find an option token for the signal date, respecting weekly/monthly T-2 rollover.

        `now_dt` (the signal's full timestamp) is only used by expiry-day gamma mode to
        apply the intraday cutoff; it is part of the cache key (bucketed before/after the
        cutoff) so a pre-cutoff token is never reused after the cutoff and vice-versa."""
        if not self._ensure_ready():
            return None

        self._ensure_expiry_dates()
        # Resolve the expiry first (gamma mode may pick the current vs rolled expiry based on
        # now_dt), then key the cache on the resolved expiry so before/after-cutoff selections
        # never collide.
        expiry_dt = self._select_option_expiry_for_date(name, target_date, expiry_type, now_dt=now_dt)
        if expiry_dt is None:
            return None
        cache_key = (name, float(strike), option_type, target_date.isoformat(), expiry_type, expiry_dt.isoformat())
        if cache_key in self._token_cache:
            return self._token_cache[cache_key]

        opt_df = self.df[
            (self.df["name"] == name)
            & (self.df["instrumenttype"].isin(["OPTIDX"]))
            & (self.df["symbol"].str.endswith(option_type))
            & (self.df["expiry_dt"] == expiry_dt)
        ].copy()
        if opt_df.empty:
            return None

        expiry_df = opt_df.copy()
        expiry_str = expiry_dt.strftime("%d%b%Y").upper()

        exact = expiry_df[expiry_df["strike"].astype(float) == float(strike) * 100]
        if not exact.empty:
            res = exact.iloc[0].to_dict()
            res["date_aware_expiry"] = expiry_str
            self._token_cache[cache_key] = res
            return res

        expiry_df["strike_val"] = expiry_df["strike"].astype(float) / 100.0
        expiry_df["diff"] = (expiry_df["strike_val"] - float(strike)).abs()
        best_match = expiry_df.loc[expiry_df["diff"].idxmin()]
        res = best_match.to_dict()
        res["date_aware_expiry"] = expiry_str
        self._token_cache[cache_key] = res
        logger.info(f"Using closest dated strike: {name} {expiry_str} {best_match['strike_val']} {option_type}")
        return res

    def get_token(self, name: str, strike: float, option_type: str, expiry_type: str = "weekly") -> Optional[Dict]:
        """Find the specific token for an option strike.

        This is the LIVE (non-dated) path. It normally uses the rolled `active_weekly`/
        `active_monthly` expiry, but expiry-day gamma mode (gated) swaps to the CURRENT
        (expiring) expiry while in the gamma window — judged against wall-clock time, which
        is correct for live. (Backtest uses get_token_for_date with the bar timestamp.)"""
        if not self._ensure_ready(): return None

        expiry_val = self.expiries.get(name, {}).get(expiry_type)
        # Gamma override: trade the current (non-rolled) expiry during the gamma window.
        try:
            if self.in_gamma_window(name):
                base_type = "weekly" if "weekly" in str(expiry_type) else "monthly"
                cur = self.expiries.get(name, {}).get(base_type)
                if cur:
                    expiry_val = cur
        except Exception:
            pass
        if not expiry_val: return None

        # Cache keyed on the RESOLVED expiry so gamma (current) and normal (rolled) tokens
        # never collide as the window opens/closes intraday.
        cache_key = (name, float(strike), option_type, expiry_type, expiry_val)
        if cache_key in self._token_cache:
            return self._token_cache[cache_key]

        logger.debug(f"Searching for {name} {expiry_type} ({expiry_val}) strike {strike} ({float(strike)*100})")
        
        nifty_opt = self.df[(self.df['name'] == name) & (self.df['expiry'] == expiry_val)]
        
        # Exact match logic
        match = self.df[
            (self.df['name'] == name) & 
            (self.df['expiry'] == expiry_val) & 
            (self.df['strike'].astype(float) == float(strike) * 100) & # AngelOne uses strike * 100
            (self.df['symbol'].str.endswith(option_type))
        ]
        
        if not match.empty:
            res = match.iloc[0].to_dict()
            self._token_cache[cache_key] = res
            return res
            
        # Fallback: Find closest available strike
        if not nifty_opt.empty:
            logger.warning(f"Exact strike {strike} not found for {name} on {expiry_val}. Finding closest available...")
            # Filter by option type too to avoid mismatch
            opt_type_filtered = nifty_opt[nifty_opt['symbol'].str.endswith(option_type)].copy()
            if not opt_type_filtered.empty:
                opt_type_filtered['strike_val'] = opt_type_filtered['strike'].astype(float) / 100.0
                opt_type_filtered['diff'] = (opt_type_filtered['strike_val'] - float(strike)).abs()
                best_match = opt_type_filtered.loc[opt_type_filtered['diff'].idxmin()]
                res = best_match.to_dict()
                self._token_cache[cache_key] = res
                logger.info(f"Using closest available strike: {best_match['strike_val']}")
                return res
                
        return None

    def is_expiry_day(self, name: str, target_date: Optional[date] = None) -> bool:
        """Returns True if target_date is the weekly/monthly expiry day for the instrument"""
        dt = target_date or date.today()

        if self.df is not None and not self.df.empty:
            self._ensure_expiry_dates()
            matches = self.df[
                (self.df["name"] == name)
                & (self.df["instrumenttype"].isin(["OPTIDX"]))
                & (self.df["expiry_dt"].dt.date == dt)
            ]
            if not matches.empty:
                return True

        # Check the nearest unrolled contract, not the active T-2 contract.
        if self.expiries and name in self.expiries:
            exp_data = self.expiries[name]
            dt_str = dt.strftime('%d%b%Y').upper()
            expiry_key = "monthly" if name in {"BANKNIFTY", "MIDCPNIFTY"} else "weekly"
            return dt_str == exp_data.get(expiry_key)
                
        # Fallback to hardcoded defaults only if fetch failed
        if name == "NIFTY":
            return dt.weekday() == 3 # Thursday
        elif name == "SENSEX":
            return dt.weekday() == 4 # Friday
        elif name == "BANKNIFTY":
            return dt.weekday() == 2 # Wednesday
        elif name == "MIDCPNIFTY":
            return dt.weekday() == 1 # Tuesday
        return False

    def get_dte(self, name: str, target_date: Optional[date] = None) -> int:
        """Get Days To Expiry (DTE) for the given instrument on a specific date"""
        dt = target_date or date.today()
        expiry_type = "active_monthly" if name in {"BANKNIFTY", "MIDCPNIFTY"} else "active_weekly"
        exp_dt = None
        if target_date is not None and self.df is not None and not self.df.empty:
            selected = self._select_option_expiry_for_date(name, target_date, expiry_type)
            if selected is not None:
                exp_dt = selected.date()
        if exp_dt is None:
            exp_str = (
                self.expiries.get(name, {}).get(expiry_type)
                or self.expiries.get(name, {}).get("weekly")
            )
            if not exp_str:
                return 3
            try:
                exp_dt = datetime.strptime(exp_str, "%d%b%Y").date()
            except Exception:
                return 3
        diff = (exp_dt - dt).days
        return max(0, diff)

    def pre_market_check(self, force: bool = False):
        """Main entry point for daily pre-market analysis"""
        logger.info("═══ PRE-MARKET EXPIRY ANALYSIS ═══")
        usable = self.df is not None and not self.df.empty and bool(self.expiries)
        stale = date.today() != getattr(self, "_loaded_date", date.today())
        if usable and stale and not force:
            self._start_background_refresh()
            return dict(self.expiries)
        if usable and not stale and not force:
            return dict(self.expiries)
        if self.download_master(force=force):
            return self.analyze_expiries()
        return {}

# Singleton for system-wide use
expiry_manager = ExpiryManager()
