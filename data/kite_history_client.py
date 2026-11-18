"""Kite SDK client with an equivalent fast path for ISO candle timestamps."""
from datetime import datetime

from dateutil.parser import parse
from kiteconnect import KiteConnect


class HistoryKiteConnect(KiteConnect):
    def _format_historical(self, data):
        records = []
        for candle in data["candles"]:
            try:
                stamp = datetime.fromisoformat(candle[0])
            except (TypeError, ValueError):
                # Preserve SDK compatibility if the service changes its clock format.
                stamp = parse(candle[0])
            row = {"date": stamp, "open": candle[1], "high": candle[2],
                   "low": candle[3], "close": candle[4], "volume": candle[5]}
            if len(candle) == 7:
                row["oi"] = candle[6]
            records.append(row)
        return records


def make_kite_client(*args, **kwargs):
    """Retain an injected SDK client factory used by integrations and offline probes."""
    from kiteconnect import KiteConnect as client_type
    if client_type is KiteConnect:
        client_type = HistoryKiteConnect
    return client_type(*args, **kwargs)
