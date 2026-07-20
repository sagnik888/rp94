from trading.order_retry import ORDER_RETRY_DELAYS, execute_order_with_retry


def test_order_retry_uses_five_standard_attempts_then_two_long_attempts(monkeypatch):
    sleeps = []
    calls = []
    monkeypatch.setattr(
        "trading.order_retry.time.sleep",
        lambda seconds: sleeps.append(seconds),
    )

    def operation():
        calls.append(len(calls) + 1)
        return "FINAL-ORDER-ID" if len(calls) == 7 else None

    order_id, error, attempts = execute_order_with_retry(
        operation,
        lambda result: str(result or ""),
        "test order",
    )

    assert ORDER_RETRY_DELAYS == (4.0, 4.0, 4.0, 4.0, 10.0, 10.0)
    assert calls == [1, 2, 3, 4, 5, 6, 7]
    assert sleeps == list(ORDER_RETRY_DELAYS)
    assert order_id == "FINAL-ORDER-ID"
    assert error == ""
    assert attempts == 7


def test_order_retry_flags_error_only_after_all_seven_attempts(monkeypatch):
    sleeps = []
    monkeypatch.setattr(
        "trading.order_retry.time.sleep",
        lambda seconds: sleeps.append(seconds),
    )

    order_id, error, attempts = execute_order_with_retry(
        lambda: None,
        lambda result: "",
        "test order",
    )

    assert order_id == ""
    assert error == "Broker rejected order"
    assert attempts == 7
    assert sleeps == list(ORDER_RETRY_DELAYS)
