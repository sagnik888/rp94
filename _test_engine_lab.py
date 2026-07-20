import urllib.request, json, sys

try:
    r = urllib.request.urlopen('http://localhost:8001/api/trades', timeout=5)
    d = json.loads(r.read())
except Exception as e:
    print(f"API error: {e}")
    sys.exit(1)

# Engine Lab data
lab = d.get('engine_lab') or d.get('engineLab') or {}
rows = lab.get('rows', [])
summary = lab.get('summary', {})
active = lab.get('active_engine', '?')

print(f"=== ENGINE LAB STATUS ===")
print(f"  Active Engine: {active}")
print(f"  Total Rows: {len(rows)}")
print(f"  Lab Summary: engines={summary.get('engines',0)}, signals={summary.get('signals',0)}, latency={summary.get('latency_ms','?')}ms")
print()

if rows:
    print(f"{'#':<3} {'Engine':<20} {'Active':<7} {'Signals':<8} {'W/L':<8} {'WR%':<7} {'Points':<10} {'PF':<6} {'Sharpe':<8} {'Basis':<15}")
    print("-" * 100)
    for i, row in enumerate(rows):
        eng = row.get('engine', '?')
        act = '>>> ' if row.get('active') else ''
        sigs = row.get('signals', 0)
        wins = row.get('wins', 0)
        losses = row.get('losses', 0)
        wr = row.get('win_rate', 0)
        pts = row.get('total_points', row.get('total_money', 0))
        pf = row.get('profit_factor', 0)
        sr = row.get('sharpe', 0)
        basis = row.get('display_basis', '?')
        print(f"{i+1:<3} {act}{eng:<16} {'YES' if row.get('active') else '':<7} {sigs:<8} {wins}/{losses:<6} {wr:<7.1f} {pts:<10.1f} {pf:<6.2f} {sr:<8.2f} {basis}")
else:
    print("  NO ENGINE LAB ROWS FOUND")
    # Check if lab is in the payload at all
    print(f"  Keys in payload: {list(d.keys())}")

# Check meta
meta = d.get('meta', {})
print(f"\n=== SYSTEM META ===")
print(f"  Mode: {meta.get('mode', '?')}")
print(f"  Engine Lab Enabled: {meta.get('engine_lab_enabled', '?')}")
print(f"  Scanner Engine: {meta.get('scanner_engine', '?')}")

# Check active engine's real trade stats
s = d.get('summary', {})
print(f"\n=== ACTIVE ENGINE REAL TRADES ===")
print(f"  Total Trades: {s.get('total_trades', 0)}")
print(f"  W/L: {s.get('wins', 0)}/{s.get('losses', 0)}")
print(f"  Daily PnL: {s.get('daily_pnl', 0)}")
print(f"  FUT Trades: {s.get('fut_trades', 0)}, OPT Trades: {s.get('opt_trades', 0)}")
