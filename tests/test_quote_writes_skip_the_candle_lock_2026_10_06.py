"""Quote writes must not queue behind candle mutations on CandleBuilder._lock.

py-spy on the live process, 2026-10-06 ~14:55 IST (PID 62324, 90s, --threads --idle,
after the trades-heartbeat fix), samples inside vs. on the `with self._lock:` line:

    holding the lock    Fyers tick thread 51% (5m/15m resample)  ScannerEventLoop 25%
                        Kite tick thread  14%                    -> ~90% occupied
    waiting for it      ScannerEventLoop 33% at update_latest_price (via _fetch_ltp_raw)
                        Kite tick thread 21% at update_from_tick's quote write, 36% at
                        its candle mutation

Quote state is two dict entries -- the latest price and its receipt clock -- and it is
deliberately separate from candle state: get_latest_price, _price_plausible and the
100ms trade monitor already read it without the candle lock. Making the WRITE wait for
a resample bought nothing, and cost the scanner loop a third of its time and the exit
path a fresh tick price whenever the lock was busy.
"""
import threading
import time
from datetime import datetime

from data.candle_builder import CandleBuilder


def _while_candle_lock_held(builder, fn, timeout=1.0):
    """Run fn on another thread while this thread holds the candle lock.

    Returns (finished_while_held, result). The lock is held exactly as the tick thread
    holds it mid-resample, which is the state the live waits were queued behind.
    """
    out = {}
    done = threading.Event()

    def run():
        out["result"] = fn()
        done.set()

    with builder._lock:
        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        finished = done.wait(timeout)
    worker.join(5.0)
    return finished, out.get("result")


def test_a_quote_lands_while_a_candle_mutation_holds_the_lock():
    builder = CandleBuilder()

    finished, _ = _while_candle_lock_held(
        builder, lambda: builder.update_latest_price("NIFTY", 25000.0)
    )

    assert finished, "update_latest_price queued behind the candle lock"
    assert builder.get_latest_price("NIFTY") == 25000.0


def test_quote_receipt_reads_do_not_wait_for_the_candle_lock():
    builder = CandleBuilder()
    builder.update_latest_price("NIFTY", 25000.0)

    finished, receipt = _while_candle_lock_held(
        builder, lambda: builder.latest_quote_receipt("NIFTY")
    )

    assert finished, "latest_quote_receipt queued behind the candle lock"
    assert isinstance(receipt, datetime)


def test_a_tick_price_is_visible_while_its_candle_update_waits():
    builder = CandleBuilder()
    tick_at = datetime(2026, 10, 6, 10, 15, 5)
    seen = []

    with builder._lock:  # the other feed's thread mid-resample
        worker = threading.Thread(
            target=lambda: builder.update_from_tick("NIFTY", 25010.0, 0.0, tick_at),
            daemon=True,
        )
        worker.start()
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if builder.get_latest_price("NIFTY") == 25010.0:
                seen.append(time.monotonic())
                break
            time.sleep(0.005)
        # The candle half of the tick still needs the lock, so it must still be waiting.
        candle_still_waiting = "NIFTY" not in builder._current_candle
    worker.join(5.0)

    assert seen, "the tick's price stayed invisible until the candle lock was released"
    assert candle_still_waiting
    # Releasing the lock lets the same tick finish its candle work, unchanged.
    assert not worker.is_alive()
    assert builder._current_candle["NIFTY"]["close"] == 25010.0
    assert builder.latest_quote_receipt("NIFTY") is not None


def test_quote_semantics_are_unchanged():
    builder = CandleBuilder()
    builder.update_latest_price("NIFTY", 25000.0)
    first_receipt = builder.latest_quote_receipt("NIFTY")

    # A >3% jump on an index is a cross-index leak: ignored, receipt untouched.
    builder.update_latest_price("NIFTY", 26000.0)
    assert builder.get_latest_price("NIFTY") == 25000.0
    assert builder.latest_quote_receipt("NIFTY") == first_receipt
    builder.update_latest_price("NIFTY", 0.0)
    assert builder.get_latest_price("NIFTY") == 25000.0

    # A quote is not a candle observation.
    assert builder.get_candles("NIFTY", "1min") is None
    assert builder.latest_candle_receipt("NIFTY", "1min") is None

    builder.update_latest_price("NIFTY", 25010.0)
    assert builder.get_latest_price("NIFTY") == 25010.0
    assert builder.latest_quote_receipt("NIFTY") >= first_receipt


def test_quotes_and_candles_from_many_threads_neither_deadlock_nor_drop_writes():
    builder = CandleBuilder(max_candles=500)
    instruments = ("NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY")
    base = {"NIFTY": 25000.0, "BANKNIFTY": 56000.0, "SENSEX": 82000.0, "MIDCPNIFTY": 13000.0}
    stop = time.monotonic() + 0.8
    errors = []
    last_written = {}

    def quotes(name):
        step = 0
        try:
            while time.monotonic() < stop:
                price = base[name] + (step % 5)
                builder.update_latest_price(name, price)
                last_written[name] = price
                step += 1
        except Exception as exc:  # pragma: no cover - the assertion reports it
            errors.append(exc)

    def ticks(name):
        minute = 0
        try:
            while time.monotonic() < stop:
                at = datetime(2026, 10, 6, 10, 0 + minute % 50, 1)
                builder.update_from_tick(name, base[name] + 1, 0.0, at)
                minute += 1
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    workers = [threading.Thread(target=quotes, args=(n,), daemon=True) for n in instruments]
    workers += [threading.Thread(target=ticks, args=(n,), daemon=True) for n in instruments[:2]]
    for w in workers:
        w.start()
    for w in workers:
        w.join(10.0)

    assert not errors
    assert not any(w.is_alive() for w in workers), "a quote/candle writer deadlocked"
    for name in instruments[2:]:
        # Only the quote writer touched these, so the last write is what is published.
        assert builder.get_latest_price(name) == last_written[name]
