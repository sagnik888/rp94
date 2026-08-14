"""main() must not reference a name that does not exist at runtime.

Startup crashed on the first line of a live session:

    File "main.py", line 1091, in main
      start_session_archiver_scheduler(data_provider=data_provider)
    NameError: name 'data_provider' is not defined

`data_provider` is built inside create_system() (main.py:813). The archiver call
sits in main(), which never binds it, so Python compiled the reference as a
GLOBAL lookup -- syntactically fine, import-clean, and fatal the moment main()
ran. Nothing in the suite executed main(), so nothing caught it.

The ordering is the real constraint, and it is not fixable by moving the line:
create_system() runs on the dashboard bootstrap task AFTER the scheduler is
wired, so at wiring time no provider exists at all. The archiver sleeps until
after the close, so the provider is passed as a getter and resolved when the
back-fill actually needs it.

This test walks main()'s bytecode, and every nested function inside it, and
asserts every global it loads actually resolves -- which would have failed on the
crashing line without running a single thread.
"""
import builtins
import dis
import inspect
import types

import pytest

import main as main_module


def _global_names(code, seen=None):
    """Every global/name load in this code object and its nested code objects."""
    seen = seen if seen is not None else set()
    for instruction in dis.get_instructions(code):
        if instruction.opname in ("LOAD_GLOBAL", "LOAD_NAME", "STORE_GLOBAL", "DELETE_GLOBAL"):
            if isinstance(instruction.argval, str):
                seen.add(instruction.argval)
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            _global_names(const, seen)
    return seen


def _unresolved(func):
    module = inspect.getmodule(func)
    return sorted(
        name
        for name in _global_names(func.__code__)
        if not hasattr(module, name) and not hasattr(builtins, name)
    )


def test_main_loads_no_name_that_does_not_exist():
    """The regression: `data_provider` resolved to nothing and killed startup."""
    assert _unresolved(main_module.main) == []


def test_create_system_loads_no_name_that_does_not_exist():
    """The other half of the startup path, by the same rule."""
    assert _unresolved(main_module.create_system) == []


def test_the_archiver_scheduler_loads_no_name_that_does_not_exist():
    assert _unresolved(main_module.start_session_archiver_scheduler) == []


# ── the getter contract ──────────────────────────────────────────────────────

def test_the_scheduler_still_accepts_being_given_no_provider():
    """Default construction must stay valid; the back-fill is optional."""
    sig = inspect.signature(main_module.start_session_archiver_scheduler)

    assert sig.parameters["data_provider"].default is None


def test_main_passes_a_getter_not_a_provider_object():
    """Pinned: the object cannot be resolved at wiring time, only later.

    If this reverts to passing a provider instance, startup either crashes again
    (the name is unbound in main) or wires in a provider that is still None.
    """
    src = inspect.getsource(main_module.main)
    i = src.index("start_session_archiver_scheduler(")
    window = src[i: i + 200]

    assert "lambda" in window, "the provider must be resolved lazily, at fire time"
    assert 'runtime.get("scanner")' in window, "the getter must read the bootstrapped system"


def test_the_backfill_resolves_a_callable_before_use():
    """A getter must be called, and a plain provider must still work unchanged."""
    src = inspect.getsource(main_module.start_session_archiver_scheduler)

    assert "callable(provider)" in src
    assert 'hasattr(provider, "get_historical_candles")' in src, (
        "a real provider must be used directly, never invoked as a getter"
    )


def test_a_getter_that_raises_does_not_stop_the_archive():
    """The archive must run even when the back-fill cannot be set up.

    Pinned at source rather than by driving the daemon loop, which sleeps to the
    next session boundary.
    """
    src = inspect.getsource(main_module.start_session_archiver_scheduler)
    i = src.index("provider = provider()")
    window = src[i: i + 260]

    assert "except Exception" in window, "a failing getter must not kill the archiver"
    assert "provider = None" in window, "it must fall through to archiving without back-fill"
