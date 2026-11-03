
import pandas as pd
idx = pd.DatetimeIndex([pd.Timestamp('2026-09-17 15:29:00', tz='Asia/Kolkata'), pd.Timestamp('2026-09-18 09:15:00', tz='Asia/Kolkata')])
df = pd.DataFrame({'close': [1,2]}, index=idx)
try:
    session_day = pd.Timestamp(df.index[-1]).normalize()
    print(session_day)
    df = df.loc[df.index.normalize() == session_day]
    print('Filtered len:', len(df))
except Exception as e:
    print('Error:', e)

