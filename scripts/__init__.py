"""Helper scripts, importable as a package.

Without this file `scripts` is only an implicit namespace package (PEP 420). That
resolves on some interpreters and layouts and not others, so
tests/test_gate_edge_report_2026_08_13.py collected cleanly here while an independent
run of the same commit failed at `import scripts.gate_edge_report`. A collection error
is not a portable-enough way to find out.
"""
