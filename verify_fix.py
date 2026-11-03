
from scanner import Scanner
from datetime import datetime, timedelta
import pandas as pd
import pytz

class MockScanner(Scanner):
    def __init__(self):
        class CandlesMock:
            def get_candles(self, inst, tf):
                idx = pd.DatetimeIndex([datetime.now() - timedelta(minutes=5), datetime.now()])
                return pd.DataFrame({'open':[1,2], 'high':[2,3], 'low':[1,2], 'close':[2,3], 'volume':[100, 200]}, index=idx)
        self.candles = CandlesMock()
        self.mode = 'REAL'

s = MockScanner()
metrics = s._candle_thrust_metrics('NIFTY', '15min', 'BUY', require_closed=True)
print('With require_closed=True on a forming candle, metrics is:', metrics)

