import logging


def test_smartapi_transport_logger_is_suppressed():
    import data.market_data  # noqa: F401
    import trading.broker  # noqa: F401
    import logzero

    assert logzero.logger.getEffectiveLevel() >= logging.CRITICAL
