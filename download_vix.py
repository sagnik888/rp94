import yfinance as yf
import json
from datetime import datetime, timedelta

def download_vix():
    vix = yf.Ticker("^INDIAVIX")

    frames = []
    try:
        daily = vix.history(period="1y", interval="1d")
        if not daily.empty:
            frames.append(daily)
            print(f"Downloaded {len(daily)} daily India VIX observations")
    except Exception as e:
        print(f"Failed to download daily India VIX history: {e}")

    try:
        intraday = vix.history(period="7d", interval="1m")
        if intraday.empty:
            intraday = vix.history(period="30d", interval="5m")
        if not intraday.empty:
            frames.append(intraday)
            print(f"Downloaded {len(intraday)} intraday India VIX observations")
    except Exception as e:
        print(f"Failed to download intraday India VIX history: {e}")

    if not frames:
        print("No data downloaded!")
        return

    # Convert to a dict mapping timestamp string to VIX value
    data = {}
    for hist in frames:
        for index, row in hist.iterrows():
            ts_str = index.strftime("%Y-%m-%d %H:%M:%S")
            data[ts_str] = float(row['Close'])
        
    import os
    os.makedirs("data_store", exist_ok=True)
    
    with open("data_store/vix_history.json", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)
        
    print(f"Saved {len(data)} data points to data_store/vix_history.json")

if __name__ == "__main__":
    download_vix()
