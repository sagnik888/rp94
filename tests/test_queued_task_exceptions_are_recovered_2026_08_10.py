"""An exception escaping a queued order task stranded the trade silently.

`AsyncExecutionQueue` catches whatever escapes a task and logs it:

    except Exception as e:
        logger.error(f"Error in AsyncExecutionQueue task '{task_name}': {e}")

The WORKER survives — task_done is in a finally, so no slot leaks — but the TRADE does
not get cleaned up. Both order tasks did all their per-trade bookkeeping inside explicit
success/rejection branches with no outer handler, and every statement after
execute_order_with_retry returns is unprotected: save_state, _log_ui,
_alert_blocking_broker_condition, and the on_execution_result / on_exit_result callbacks.

So a throw in any of those left the row in open_trades with pending_execution (or
pending_exit) still set — holding a position slot, counting against exposure and the
concurrency caps — while nothing rested at the exchange and no alert fired.

The fix is an `except`, NOT a `finally`, and it marks RECOVERY_REQUIRED rather than
closing the row:

  * an arbitrary exception says nothing about whether the order reached the broker, and
    concluding "not placed" from a failure is the mistake this codebase keeps making;
  * pending_exit is deliberately left alone — clearing it after an exception raised
    AFTER the exit was accepted would permit a second exit on top of a live one, which
    is worse than a stranded row.

RECOVERY_REQUIRED is the state a human reconciles from, and it already drives the
recovery alert and the session-end sweep.
"""
import inspect
import re

import pytest

from trading.trade_manager import TradeManager


def _body_of(name):
    """Source of a nested def, sliced by indentation.

    The indent is taken from the def LINE. An earlier version derived it from a regex
    group that also swallowed the preceding newline, making the indent one too deep so
    every body came back EMPTY — and an empty body makes every assertion below pass
    vacuously, which is worse than failing. Hence the explicit non-empty check.
    """
    lines = inspect.getsource(TradeManager).splitlines()
    for i, ln in enumerate(lines):
        if re.match(rf"\s*def {name}\(\):\s*$", ln):
            indent = len(ln) - len(ln.lstrip())
            out = []
            for nxt in lines[i + 1:]:
                if nxt.strip() and (len(nxt) - len(nxt.lstrip())) <= indent:
                    break
                out.append(nxt)
            body = "\n".join(out)
            assert body.strip(), f"{name} body came back empty"
            return body
    raise AssertionError(f"{name} not found")


# ── both tasks are wrapped ───────────────────────────────────────────────────

@pytest.mark.parametrize("wrapper,body", [
    ("place_bg", "_place_bg_body"),
    ("close_bg", "_close_bg_body"),
])
def test_the_task_submitted_to_the_queue_is_the_wrapper(wrapper, body):
    src = inspect.getsource(TradeManager)
    assert f"def {wrapper}():" in src and f"def {body}():" in src
    # the wrapper must be defined AFTER the body it calls, or the closure is unbound
    assert src.index(f"def {body}():") < src.index(f"def {wrapper}():")
    assert f"submit(" in src
    w = _body_of(wrapper)
    assert f"{body}()" in w, f"{wrapper} does not call {body}"


@pytest.mark.parametrize("wrapper", ["place_bg", "close_bg"])
def test_the_wrapper_catches_rather_than_finallys(wrapper):
    """A finally would also run on SUCCESS and could undo good state."""
    w = _body_of(wrapper)
    assert "except Exception" in w
    assert "finally:" not in w


@pytest.mark.parametrize("wrapper", ["place_bg", "close_bg"])
def test_an_escaping_exception_marks_recovery(wrapper):
    w = _body_of(wrapper)
    assert "_mark_recovery" in w, (
        "the trade is still stranded with no operator-visible state"
    )


@pytest.mark.parametrize("wrapper", ["place_bg", "close_bg"])
def test_the_failure_is_logged_critically(wrapper):
    w = _body_of(wrapper)
    assert "logger.critical" in w


@pytest.mark.parametrize("wrapper", ["place_bg", "close_bg"])
def test_the_wrapper_does_not_conclude_the_order_never_reached_the_broker(wrapper):
    """Closing the row would assert a state the exception cannot prove."""
    w = _body_of(wrapper)
    assert 'status = "CLOSED"' not in w
    assert "closed_trades.append" not in w


def test_the_exit_wrapper_does_not_clear_pending_exit():
    """Clearing it after an exception raised post-acceptance would allow a SECOND exit
    on top of a live one."""
    w = _body_of("close_bg")
    assert "pending_exit" not in w or "pending_exit = False" not in w


def test_a_failure_inside_mark_recovery_does_not_re_escape():
    """The handler must not become the next thing that throws into the queue."""
    for wrapper in ("place_bg", "close_bg"):
        w = _body_of(wrapper)
        idx = w.index("_mark_recovery")
        assert "try:" in w[:idx], f"{wrapper} calls _mark_recovery unguarded"


# ── the premise ──────────────────────────────────────────────────────────────

def test_the_queue_still_swallows_what_escapes():
    """If the queue ever starts re-raising, these wrappers stop being load-bearing."""
    import engine.async_queue as q

    src = inspect.getsource(q.AsyncExecutionQueue)
    assert "except Exception" in src
    assert "raise" not in src.split("except Exception")[1][:200]


def test_the_worker_slot_itself_was_never_the_problem():
    """task_done in a finally means the POOL was always fine — only the trade leaked."""
    import engine.async_queue as q

    src = inspect.getsource(q.AsyncExecutionQueue)
    assert "finally" in src and "task_done" in src
