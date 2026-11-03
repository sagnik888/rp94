"""A refused decision row must say which build refused it.

On 2026-09-15 a refusal could not be attributed to any build. The only marker on a
decision row was `strategy.revision`, the constant `"ut1-current"` read from settings, so
an edited working tree and a released commit produced identical rows. Worse, all 15
`_build_rejected_candidate` returns sat ABOVE the accepted-candidate stamp, so every
refusal reached rtracker with an empty revision AND an empty data_provenance -- 1,057 of
2,295 archived verdict rows, exactly the set whose ADX also arrived as 0.0.

These tests pin that the fingerprint distinguishes builds by CONTENT (the tree is dirty
for days at a time, so a commit SHA alone cannot), and that the stamp reaches the
archived row.
"""
import hashlib

import pytest

from engine.build_identity import (
    FINGERPRINT_SOURCES,
    build_fingerprint,
    build_identity,
)


@pytest.fixture
def fake_repo(tmp_path):
    """A miniature checkout carrying every file the fingerprint reads."""
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (tmp_path / ".git" / "refs" / "heads").mkdir(parents=True)
    (tmp_path / ".git" / "refs" / "heads" / "main").write_text(
        "abc123def4567890abcdef\n", encoding="utf-8"
    )
    for rel in FINGERPRINT_SOURCES:
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"# {rel}\n", encoding="utf-8")
    return tmp_path


# --- the fingerprint ------------------------------------------------------------
def test_fingerprint_is_commit_plus_content_digest(fake_repo):
    value = build_fingerprint(fake_repo, refresh=True)
    sha, _, digest = value.partition("+")

    assert sha == "abc123def456"
    assert len(digest) == 12
    assert all(c in "0123456789abcdef" for c in digest)


def test_same_tree_gives_the_same_fingerprint(fake_repo):
    first = build_fingerprint(fake_repo, refresh=True)
    second = build_fingerprint(fake_repo, refresh=True)
    assert first == second


def test_an_uncommitted_edit_changes_the_fingerprint(fake_repo):
    """The whole point: a dirty tree must not masquerade as its commit."""
    before = build_fingerprint(fake_repo, refresh=True)
    (fake_repo / "engine" / "quality_verdict.py").write_text(
        "# edited, never committed\n", encoding="utf-8"
    )
    after = build_fingerprint(fake_repo, refresh=True)

    assert before != after
    assert before.split("+")[0] == after.split("+")[0], "commit did not change"


def test_editing_an_unlisted_file_does_not_change_the_fingerprint(fake_repo):
    before = build_fingerprint(fake_repo, refresh=True)
    (fake_repo / "README.md").write_text("docs only\n", encoding="utf-8")
    assert build_fingerprint(fake_repo, refresh=True) == before


def test_a_missing_decision_module_is_not_silently_equal(fake_repo):
    """A truncated deployment must not produce a matching digest."""
    before = build_fingerprint(fake_repo, refresh=True)
    (fake_repo / "engine" / "quality_verdict.py").unlink()
    assert build_fingerprint(fake_repo, refresh=True) != before


def test_packed_refs_are_read_when_the_loose_ref_is_absent(fake_repo):
    (fake_repo / ".git" / "refs" / "heads" / "main").unlink()
    (fake_repo / ".git" / "packed-refs").write_text(
        "# pack-refs with: peeled fully-peeled sorted\n"
        "9999888877776666555544443333222211110000 refs/heads/main\n",
        encoding="utf-8",
    )
    assert build_fingerprint(fake_repo, refresh=True).startswith("999988887777+")


def test_a_tree_without_git_still_fingerprints(tmp_path):
    """A zipped restore point has no .git and must still be identifiable."""
    for rel in FINGERPRINT_SOURCES:
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"# {rel}\n", encoding="utf-8")

    value = build_fingerprint(tmp_path, refresh=True)
    assert "+" not in value and len(value) == 12


def test_the_live_repo_fingerprints_without_raising():
    value = build_fingerprint()
    assert value and "+" in value or len(value) == 12


def test_repeat_calls_are_cached_not_recomputed(fake_repo):
    """Rejections call this per row; re-reading scanner.py each time would not do."""
    first = build_fingerprint(fake_repo, refresh=True)
    (fake_repo / "scanner.py").write_text("# changed after caching\n", encoding="utf-8")
    assert build_fingerprint(fake_repo) == first
    assert build_fingerprint(fake_repo, refresh=True) != first


# --- the stamped triple ---------------------------------------------------------
class _Settings:
    strategy_revision = "ut1-current"
    strategy_direction_mode = "LONG_SHORT"


class _Scanner:
    def __init__(self, mode):
        self.mode = mode


def test_identity_reports_live_only_for_real_mode():
    assert build_identity(_Settings(), _Scanner("REAL"))["data_provenance"] == "LIVE"
    for mode in ("HISTORICAL", "PAPER", "", "real "):
        assert build_identity(_Settings(), _Scanner(mode))["data_provenance"] == "BACKTEST"


def test_identity_carries_revision_and_direction_mode():
    identity = build_identity(_Settings(), _Scanner("REAL"))
    assert identity["strategy_revision"] == "ut1-current"
    assert identity["strategy_direction_mode"] == "LONG_SHORT"
    assert identity["build_fingerprint"]


def test_identity_degrades_rather_than_raising_on_missing_inputs():
    identity = build_identity(None, None)
    assert identity["strategy_revision"] == ""
    assert identity["data_provenance"] == "BACKTEST"
    assert identity["build_fingerprint"]


def test_every_stamped_field_is_populated_on_a_live_rejection():
    """The empty-revision, empty-provenance row is what made refusals unattributable."""
    identity = build_identity(_Settings(), _Scanner("REAL"))
    assert set(identity) == {
        "strategy_revision",
        "strategy_direction_mode",
        "data_provenance",
        "build_fingerprint",
    }
    assert all(str(v).strip() for v in identity.values())


# --- it has to survive into the archived row ------------------------------------
def test_the_archived_row_carries_the_fingerprint():
    from intelligence.rtracker import RegimeTracker

    class _Candidate:
        instrument = "NIFTY"
        direction = "SHORT"
        timeframe = "5min"
        status = "REJECTED - Price Action"
        strategy_revision = "ut1-current"
        strategy_direction_mode = "LONG_SHORT"
        data_provenance = "LIVE"
        build_fingerprint = "deadbeef1234+0011223344ff"

    event = RegimeTracker._event_from_candidate(
        RegimeTracker.__new__(RegimeTracker),
        _Candidate(),
        decision="REJECTED",
        reason="Price-action ADVERSE",
        source="price_action_gate",
        extra=None,
    )

    assert event["strategy"]["build_fingerprint"] == "deadbeef1234+0011223344ff"
    assert event["strategy"]["revision"] == "ut1-current"
    assert event["strategy"]["data_provenance"] == "LIVE"


def test_accepted_and_rejected_rows_are_stamped_by_the_same_call():
    """Verified live on 2026-09-16: after the first fix, an ACCEPTED NIFTY row carried
    revision and provenance but no build_fingerprint, because only the rejection funnel
    was stamped. That is the original gap inverted, so both paths call build_identity.
    """
    import inspect

    from engine import signal_processor

    source = inspect.getsource(signal_processor)
    assert source.count("build_identity(settings, scanner)") == 2, (
        "expected one stamp on the rejection funnel and one on the accepted path"
    )
    # The accepted path must no longer hand-roll a partial stamp.
    assert 'setattr(candidate, "strategy_revision"' not in source


def test_an_unstamped_candidate_archives_an_empty_fingerprint_not_a_crash():
    """Older rows and replay rows have no stamp; the archiver must still write them."""
    from intelligence.rtracker import RegimeTracker

    class _Bare:
        instrument = "NIFTY"
        direction = "LONG"
        timeframe = "15min"
        status = "REJECTED"

    event = RegimeTracker._event_from_candidate(
        RegimeTracker.__new__(RegimeTracker),
        _Bare(),
        decision="REJECTED",
        reason="",
        source="test",
        extra=None,
    )
    assert event["strategy"]["build_fingerprint"] == ""
