"""An absent option chain must not starve independent candle intelligence."""
import asyncio
from types import SimpleNamespace
import pandas as pd
from intelligence.intelligence_aggregator import IntelligenceAggregator
from tests.test_intel_refresh_starvation_2026_10_23 import _source


def test_missing_chain_still_publishes_all_index_timeframes(monkeypatch):
    async def scenario():
        source=_source()
        source._get_option_chain_cached=lambda *a,**k:(None,{})
        seen=[]
        def analyze(**kw):
            seen.append(kw)
            return {"regime":{"regime":"RANGING","candle_count":40},
                    "volume":{"available":True,"current_volume":123.},
                    "data_quality":kw["chain_quality"]}
        source.intel=SimpleNamespace(analyze=analyze)
        real_sleep=asyncio.sleep
        async def poll(seconds):await real_sleep(.002 if seconds>=1 else 0)
        monkeypatch.setattr(asyncio,"sleep",poll)
        worker=asyncio.create_task(source._background_intel_worker())
        async def ready():
            while not all(f"{name}_{tf}" in source._intel_cache
                for name in source.active_indices for tf in ("1min","5min","15min")):
                await real_sleep(.002)
        try:
            await asyncio.wait_for(ready(),2.)
            assert len(seen)>=12
            assert all(row["options_chain"].empty and not row["chain_quality"]["intelligence_usable"] for row in seen)
            assert source._intel_cache["NIFTY_5min"]["volume"]["current_volume"]==123.
        finally:
            source.is_running=False
            worker.cancel()
            await asyncio.gather(worker,return_exceptions=True)
    asyncio.run(scenario())


def test_empty_chain_keeps_measured_volume_and_marks_oi_pcr_unavailable():
    index = pd.date_range("2026-10-30 09:15", periods=40, freq="5min")
    frame = pd.DataFrame({
        "open": range(100, 140), "high": range(101, 141),
        "low": range(99, 139), "close": range(100, 140),
        "volume": [1000] * 40,
    }, index=index)
    result = IntelligenceAggregator().analyze(
        "NIFTY", "5min", frame, options_chain=pd.DataFrame(), spot_price=139,
        strike_interval=50, chain_quality={"intelligence_usable": False},
    )
    assert result["volume"]["available"] is True
    assert result["oi"]["available"] is False
    assert result["pcr"]["available"] is False
    assert result["data_quality"]["reason"] == "Option chain unavailable"
