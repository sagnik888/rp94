"""Independent UTS post-entry sampling; ordinary scans and stops keep running."""
from datetime import timedelta
import asyncio

from loguru import logger

from config.settings import get_settings
from engine import uts_lane
from engine.alt_scanners import AltSignalEngine
from trading.trade_manager import IST


class UTSRepaintWorker:
    def __init__(self, scanner):
        self.scanner = scanner
        self._next_checks = {}
        self._close_checked = set()

    @staticmethod
    def _calculate(frame, instrument, timeframe, now):
        # Private results cannot emit entries or replace the live/chart caches.
        return AltSignalEngine(signal_source='ut_smart').process(
            frame, instrument=instrument, timeframe=timeframe, as_of=now,
            uts_intrabar_entries=True)

    async def run_once(self, now):
        scanner = self.scanner
        if str(getattr(scanner, 'mode', '')).upper() == 'HISTORICAL':
            return
        clock = uts_lane._market_time(now).to_pydatetime()
        settings = get_settings()
        # Restore eligibility from persisted terminal trades after a restart.
        release = getattr(scanner, '_release_uts_repaint_signal', None)
        if callable(release):
            for closed in list(getattr(scanner.trades, 'closed_trades', []) or []):
                if str(getattr(closed, 'exit_reason', '')).upper() == 'UTS_REPAINT':
                    release(closed)
        every = max(1., uts_lane._g(settings, 'uts_lane_repaint_check_seconds', 10.))
        trades = list(scanner.trades.open_trades.values())
        live_ids = {trade.id for trade in trades}
        self._next_checks = {key: value for key, value in self._next_checks.items() if key in live_ids}
        self._close_checked.intersection_update(live_ids)
        results = {}
        for trade in trades:
            if (not uts_lane.is_uts(trade) or getattr(trade, 'status', '') != 'OPEN'
                    or not getattr(trade, 'entered_intracandle', False)
                    or (getattr(trade, 'uts_confirmed', False) and not getattr(trade, 'uts_repaint_exit_required', False))
                    or getattr(trade, 'pending_exit', False)
                    or str(getattr(trade, 'execution_status', 'OPEN')).upper() not in {'OPEN', 'PARTIAL'}):
                continue
            stamp = getattr(trade, 'signal_time', None) or getattr(trade, 'signal_timestamp', None)
            if stamp is None:
                continue
            try:
                close = uts_lane._market_time(stamp).to_pydatetime() + timedelta(
                    minutes=uts_lane._tf_minutes(trade.timeframe))
                entry = uts_lane._market_time(getattr(trade, 'entry_filled_at', None) or trade.entry_time).to_pydatetime()
                due = self._next_checks.get(trade.id, entry + timedelta(seconds=every))
                final_check = clock >= close and trade.id not in self._close_checked
                if clock < due and not final_check:
                    continue
                self._next_checks[trade.id] = clock + timedelta(seconds=every)
                if final_check:
                    self._close_checked.add(trade.id)
                key = (trade.instrument, trade.timeframe)
                if not getattr(trade, 'uts_repaint_exit_required', False) and key not in results:
                    frame = scanner.candles.get_candles(*key)
                    if frame is None or frame.empty:
                        continue
                    results[key] = await asyncio.to_thread(
                        self._calculate, frame.copy(deep=True), *key, clock)
                if (getattr(trade, 'status', '') != 'OPEN'
                        or not getattr(scanner, 'is_running', True)
                        or getattr(trade, 'pending_exit', False)
                        or str(getattr(trade, 'execution_status', 'OPEN')).upper() not in {'OPEN', 'PARTIAL'}
                        or str(getattr(scanner, 'mode', '')).upper() == 'HISTORICAL'):
                    continue
                confirmed_before = bool(getattr(trade, 'uts_confirmed', False))
                reason = ('REPAINT' if getattr(trade, 'uts_repaint_exit_required', False)
                          else uts_lane.repaint_exit(trade, results[key], clock, settings, force_check=True))
                if reason == 'REPAINT' or (not confirmed_before and getattr(trade, 'uts_confirmed', False)):
                    if reason == 'REPAINT':
                        trade.uts_repaint_exit_required = True
                    else:
                        trade.repaint_guard_checked = True
                    dirty = getattr(scanner.trades, '_mark_dirty', None)
                    if callable(dirty):
                        dirty()
                if reason != 'REPAINT':
                    continue
                if str(getattr(trade, 'inst_type', '')).upper() == 'OPT':
                    premium = scanner._get_live_premium_cached(trade, allow_blocking=False)
                    spot = scanner.candles.get_latest_price(trade.instrument)
                    mark, source, verified = scanner._active_option_decision_mark(trade, float(spot or 0.), premium)
                    if not verified or float(mark or 0.) <= 0.:
                        continue
                    trade.quote_verified, trade.quote_source = True, source
                else:
                    mark = scanner._get_live_fut_ltp_cached(trade)
                    if not mark or float(mark) <= 0.:
                        continue
                # Existing gateway owns Auto/Manual authority and broker protection.
                if getattr(scanner, 'auto_mode', False):
                    await asyncio.to_thread(scanner._close_and_record, trade.id, float(mark),
                                            'UTS_REPAINT', exit_time=clock)
                else:
                    # Manual notifications need the scanner's running event loop.
                    scanner._close_and_record(trade.id, float(mark), 'UTS_REPAINT', exit_time=clock)
            except Exception as exc:
                logger.debug(f'UTS repaint sample skipped for {trade.id}: {type(exc).__name__}')

    async def run(self):
        from datetime import datetime
        while self.scanner.is_running:
            try:
                await self.run_once(datetime.now(IST))
            except Exception as exc:
                logger.debug(f'UTS repaint worker retry: {type(exc).__name__}')
            await asyncio.sleep(1.)
