"""Chart selection must stay paired with its off-loop snapshot build."""

import asyncio
import time


def test_overlapping_chart_requests_cannot_swap_the_active_selection(monkeypatch):
    from dashboard import server

    class FakeScanner:
        active_chart_instrument = "NIFTY"
        active_chart_tf = "5min"

        def get_chart_snapshot(self, instrument, timeframe):
            # Let the second request reach the scanner loop while this build runs.
            time.sleep(0.03)
            return {
                "instrument": instrument,
                "timeframe": timeframe,
                "active_instrument": self.active_chart_instrument,
                "active_timeframe": self.active_chart_tf,
            }

    scanner = FakeScanner()

    async def select_on_scanner_loop(callback, **_kwargs):
        return callback()

    monkeypatch.setattr(server, "scanner_ref", scanner)
    monkeypatch.setattr(server, "_call_on_scanner_loop", select_on_scanner_loop)

    async def request_pair():
        return await asyncio.gather(
            server._build_chart_off_loop("BANKNIFTY", "1min"),
            server._build_chart_off_loop("SENSEX", "15min"),
        )

    bank, sensex = asyncio.run(request_pair())
    assert (bank["active_instrument"], bank["active_timeframe"]) == ("BANKNIFTY", "1min")
    assert (sensex["active_instrument"], sensex["active_timeframe"]) == ("SENSEX", "15min")
