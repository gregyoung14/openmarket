"""Quick ledger analysis — v9-regime trades."""
import json

with open('strategies/v9_regime_filter/ledger.json') as f:
    trades = json.load(f)

print(f"Total trades: {len(trades)}")
versions = {}
for t in trades:
    v = t.get('signal_version', 'unknown')
    versions[v] = versions.get(v, 0) + 1
for v, c in sorted(versions.items()):
    print(f"  {v}: {c} trades")

# V9-regime analysis
v9 = [t for t in trades if t.get('signal_version') == 'v9-regime']
print(f"\n{'='*80}")
print(f"V9-REGIME TRADES ({len(v9)})")
print(f"{'='*80}")

if v9:
    wins = sum(1 for t in v9 if t['won'])
    losses = len(v9) - wins
    total_pnl = sum(t['cash_pnl'] for t in v9)
    total_invested = sum(t['initial_value'] for t in v9)
    print(f"W/L: {wins}/{losses} = {wins/len(v9)*100:.1f}% WR")
    print(f"Total P&L: ${total_pnl:.4f}")
    print(f"Total invested: ${total_invested:.4f}")
    print(f"ROI on capital deployed: {total_pnl/total_invested*100:.1f}%")

    avg_win_price = 0
    avg_loss_price = 0
    win_prices = [t['avg_price'] for t in v9 if t['won']]
    loss_prices = [t['avg_price'] for t in v9 if not t['won']]
    if win_prices:
        avg_win_price = sum(win_prices) / len(win_prices)
    if loss_prices:
        avg_loss_price = sum(loss_prices) / len(loss_prices)

    print(f"\nAvg entry price (wins):  ${avg_win_price:.4f}")
    print(f"Avg entry price (losses): ${avg_loss_price:.4f}")
    print(f"Avg win P&L:  ${sum(t['cash_pnl'] for t in v9 if t['won'])/max(wins,1):.4f}")
    print(f"Avg loss P&L: ${sum(t['cash_pnl'] for t in v9 if not t['won'])/max(losses,1):.4f}")

    print(f"\n{'#':>3}  {'Title':55s}  {'W/L':4s}  {'Side':5s}  {'AvgPx':>7s}  {'Size':>6s}  "
          f"{'P&L':>8s}  {'Pct':>8s}  {'USDC_After':>10s}")
    print(f"{'':>3}  {'':55s}  {'':4s}  {'':5s}  {'':>7s}  {'':>6s}  {'':>8s}  {'':>8s}  {'':>10s}")

    for i, t in enumerate(v9):
        result = 'WIN' if t['won'] else 'LOSS'
        title = t['title'][:55]
        print(f"{i+1:>3}  {title:55s}  {result:4s}  {t['outcome']:5s}  "
              f"${t['avg_price']:>6.4f}  {t['size']:>5.2f}  "
              f"${t['cash_pnl']:>+7.4f}  {t['percent_pnl']:>+7.1f}%  "
              f"${t.get('usdc_after', 0):>9.2f}")

    # Group by time to see chronological pattern
    print(f"\n--- Chronological Order (by slug epoch) ---")
    v9_sorted = sorted(v9, key=lambda t: int(t['slug'].split('-')[-1]))
    running_pnl = 0
    for i, t in enumerate(v9_sorted):
        epoch = int(t['slug'].split('-')[-1])
        from datetime import datetime, timezone, timedelta
        dt = datetime.fromtimestamp(epoch, tz=timezone.utc) + timedelta(hours=-5)
        result = 'WIN' if t['won'] else 'LOSS'
        running_pnl += t['cash_pnl']
        print(f"{i+1:>3}  {dt.strftime('%b %d %I:%M%p'):15s}  {result:4s}  "
              f"bet {t['outcome']:4s} @ ${t['avg_price']:.4f}  "
              f"pnl=${t['cash_pnl']:+.4f}  running=${running_pnl:+.4f}  "
              f"usdc=${t.get('usdc_after', 0):.2f}")

# Summary of all versions
print(f"\n{'='*80}")
print(f"ALL VERSIONS COMPARISON")
print(f"{'='*80}")
for ver in sorted(versions.keys()):
    vt = [t for t in trades if t.get('signal_version') == ver]
    w = sum(1 for t in vt if t['won'])
    pnl = sum(t['cash_pnl'] for t in vt)
    invested = sum(t['initial_value'] for t in vt)
    roi = pnl / invested * 100 if invested > 0 else 0
    avg_px = sum(t['avg_price'] for t in vt) / len(vt)
    print(f"  {ver:15s}: {w:>2}/{len(vt):>2} = {w/len(vt)*100:>5.1f}% WR  "
          f"P&L=${pnl:>+8.4f}  invested=${invested:>7.2f}  ROI={roi:>+6.1f}%  "
          f"avg_px=${avg_px:.3f}")
