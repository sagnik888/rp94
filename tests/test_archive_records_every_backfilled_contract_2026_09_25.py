"""The pre-archive back-fill fetched contracts whose outcome was never recorded (2026-09-25).

backfill_contract_history() requests every contract in the session lacking a file (31
for 2026-08-12), but repair evidence was kept only for the manifest's missing list (12).
The other 19 - expired August options - got "InputException: invalid token" (the
INSTRUMENT token, not the session) on every archiver rotation, all night, because no
terminal verdict was ever stored for them.
"""
import json

import main
from tests.test_session_archive_catchup_2026_08_22 import _event, _incomplete_manifest

INVALID = {
    "state": "terminal_unfillable",
    "reason": "broker permanently rejected the exact instrument token as invalid",
    "permanent": True,
    "attempted": True,
}


def test_an_unlisted_contract_with_an_invalid_token_is_not_requested_again(tmp_path):
    day = "2026-08-12"
    listed, extra = "NIFTY26AUG24000CE", "BANKNIFTY26AUG57300CE"
    _event(tmp_path, day)
    _incomplete_manifest(tmp_path, day, listed)
    status = main._record_session_archive_repair(
        tmp_path, day,
        backfill_outcomes={listed: {"state": "retryable", "reason": "zero rows", "attempted": True},
                           extra: dict(INVALID)},
    )
    assert status["other_contracts"][extra]["state"] == "terminal_unfillable"
    terminal = main._session_archive_terminal_contracts(tmp_path, day)
    assert extra in terminal and listed not in terminal       # skipped next cycle


def test_unlisted_contracts_never_decide_the_days_state(tmp_path):
    day = "2026-08-13"
    listed, extra = "NIFTY26AUG24000CE", "MIDCPNIFTY26AUG15000CE"
    _event(tmp_path, day)
    _incomplete_manifest(tmp_path, day, listed)
    status = main._record_session_archive_repair(
        tmp_path, day,
        backfill_outcomes={listed: dict(INVALID),
                           extra: {"state": "retryable", "reason": "zero rows", "attempted": True}},
    )
    # The listed contract is terminal; the unlisted retryable one does not hold the day open.
    assert status["missing_contracts"][listed]["state"] == "terminal_unfillable"
    assert status["other_contracts"][extra]["state"] == "retryable"


def test_unlisted_contract_attempts_accumulate_across_cycles(tmp_path):
    day = "2026-08-14"
    listed, extra = "NIFTY26AUG24000CE", "BANKNIFTY26AUG57400PE"
    _event(tmp_path, day)
    _incomplete_manifest(tmp_path, day, listed)
    retry = {"state": "retryable", "reason": "zero rows", "attempted": True}
    main._record_session_archive_repair(tmp_path, day, backfill_outcomes={extra: dict(retry)})
    status = main._record_session_archive_repair(tmp_path, day, backfill_outcomes={extra: dict(retry)})
    assert status["other_contracts"][extra]["attempts"] == 2
