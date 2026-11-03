"""Logical source views for structural tests after scanner service extraction.

Tests should inspect the component that owns behavior.  The combined views are
reserved for cross-cutting assertions that intentionally span the scanner host
and its extracted services.
"""

from __future__ import annotations

import inspect

from engine.entry_analysis_service import EntryAnalysisService
from engine.execution_service import ExecutionService
from engine.exit_cycle import ExitCycleService
from engine.signal_processor import SignalProcessor
from scanner import Scanner


def scanner_runtime_source() -> str:
    return "\n".join(
        (
            inspect.getsource(Scanner),
            inspect.getsource(EntryAnalysisService),
            inspect.getsource(ExecutionService),
            inspect.getsource(ExitCycleService),
        )
    )


def signal_pipeline_source() -> str:
    return "\n".join(
        (
            inspect.getsource(SignalProcessor.process_best_signal),
            inspect.getsource(SignalProcessor._prepare_signal_pipeline_inputs),
            inspect.getsource(SignalProcessor._collect_signal_candidates),
            inspect.getsource(SignalProcessor._finalize_signal_candidates),
            inspect.getsource(SignalProcessor._record_signal_pipeline_evidence),
        )
    )
