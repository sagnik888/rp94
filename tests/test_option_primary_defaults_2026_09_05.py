"""Option-primary safety rails for unattended/fresh deployments."""

from __future__ import annotations

import ast
from pathlib import Path

from config.settings import Settings
from engine.quality_verdict import QualityInputs, evaluate_quality


ROOT = Path(__file__).resolve().parents[1]


def test_fresh_settings_default_to_options_not_hybrid_or_futures():
    """Missing INST_PREF must keep the primary long-premium product."""
    assert Settings.model_fields["inst_pref"].default == "OPT"


def test_example_environment_declares_the_option_primary_product():
    lines = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    values = dict(
        line.split("=", 1)
        for line in lines
        if line and not line.startswith("#") and "=" in line
    )
    assert values["INST_PREF"] == "OPT"


def test_every_production_open_trade_call_declares_its_instrument_type():
    """A missing product must not fall through to Trade's legacy FUT default."""
    sources = [
        ROOT / "scanner.py",
        ROOT / "main.py",
        *(ROOT / "engine").rglob("*.py"),
        *(ROOT / "intelligence").rglob("*.py"),
        *(ROOT / "trading").rglob("*.py"),
    ]
    calls = []
    for source in sources:
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == "open_trade":
                    calls.append((source.name, node.lineno, {kw.arg for kw in node.keywords}))

    assert calls
    assert all("inst_type" in keywords for _, _, keywords in calls)


def test_option_quality_uses_confidence_when_legacy_grade_is_absent():
    """A direct/recovery option candidate may omit its display-grade field."""
    verdict = evaluate_quality(
        QualityInputs(
            timeframe="5min",
            inst_type="OPT",
            confidence=0.85,
            regime="TRENDING_UP",
            adx_value=30.0,
            trend_15m_agrees=True,
            volume_ratio=1.3,
        ),
        Settings(),
    )
    assert verdict.allowed is True
