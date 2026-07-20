"""Pytest collection hygiene for local audit/probe files.

These files are kept in the workspace as manual diagnostics or restore copies,
but they are not maintained regression tests and should not run in the default
suite.
"""

collect_ignore = [
    "test_backtest_head.py",
    "test_db.py",
    "test_db2.py",
    "test_db3.py",
    "test_db4.py",
    "test_db5.py",
    "test_db6.py",
    "test_warm_startup.py",
    "test_ws.py",  # manual smoke; not under tests/ and needs live server
    "ut1-index-final3",
]

# Never collect under orphaned pytest temp dirs (permission-denied noise).
collect_ignore_glob = [
    "pytest-tmp-base/*",
    "pytest-tmp-base*/**",
    "restore_point_*/**",
]
