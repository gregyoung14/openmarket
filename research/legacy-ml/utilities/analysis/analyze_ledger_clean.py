"""Clean ledger analysis — deduplicate by slug, focus on v9-regime."""
import json
from datetime import datetime, timezone, timedelta
from collections import defaultdict

with open('strategies/v9_regime_filter/ledger.json') as f:
    trades = json.load(f)

# ============================================================
# DEDUP: Group by (slug, signal_version) → aggregate
# ============================================================
grouped = defaultdict(list)
for t in trades:
    key = (t['slug'], t.get('signal_version', 'unknown'))
    grouped[key].append(t)

print(f"Total ledger entries: {len(trades)}")
print(f"Unique (slug, version) pairs: {len(grouped)}")

# Count duplicates
dup_counts = [len(v) for v in grouped.values()]
multi = [c for c in dup_counts if c > 1]
print(f"Markets with >1 entry: {len(multi)}")
if multi:
    print(f"  Max entries per market: {max(multi)}")
    print(f"  Avg entries per market: {sum(multi)/len(multi):.1f}")

# ============================================================
# V9-REGIME UNIQUE MARKETS
# ============================================================
v9_markets = {}
for (slug, ver), entries in grouped.items():
    if ver == 'v9-regime':
        # Aggregate: total size, total invested, total pnl
        total_invested = sum(e['initial_value'] for e in entries)
        total_pnl = sum(e['cash_pnl'] for e in entries)
        total_size = sum(e['size'] for e in entries)
        n_fills = len(entries)
        won = entries[0]['won']  # All fills on same market have same outcome
        outcome = entries[0]['outcome']
        title = entries[0]['title']
        avg_price = sum(e['avg_price'] * e['size'] for e in entries) / total_size if total_size > 0 else 0

        v9_markets[slug] = {
            'slug': slug, 'title': title, 'outcome': outcome, 'won': won,
            'avg_price': avg_price, 'total_size': total_size,
            'total_invested': total_invested, 'total_pnl': total_pnl,
            'n_fills': n_fills,
            'usdc_after': entries[-1].get('usdc_after', 0),
        }

v9_list = sorted(v9_markets.values(), key=lambda x: int(x['slug'].split('-')[-1]))

print(f"\n{'='*90}")
print(f"V9-REGIME: {len(v9_list)} UNIQUE MARKETS  ({sum(v['n_fills'] for v in v9_list)} total fills)")
print(f"{'='*90}")

wins = sum(1 for v in v9_list if v['won'])
losses = len(v9_list) - wins
total_pnl = sum(v['total_pnl'] for v in v9_list)
total_invested = sum(v['total_invested'] for v in v9_list)
print(f"W/L: {wins}/{losses} = {wins/len(v9_list)*100:.1f}% WR")
print(f"Total P&L: ${total_pnl:.2f}")
print(f"Total invested: ${total_invested:.2f}")
print(f"ROI: {total_pnl/total_invested*100:.1f}%")

win_pnl = sum(v['total_pnl'] for v in v9_list if v['won'])
loss_pnl = sum(v['total_pnl'] for v in v9_list if not v['won'])
print(f"Total win P&L: ${win_pnl:.2f}")
print(f"Total loss P&L: ${loss_pnl:.2f}")
print(f"Profit Factor: {abs(win_pnl / loss_pnl):.2f}" if loss_pnl != 0 else "Profit Factor: inf")

# Detail each market
print(f"\n{'#':>3}  {'Time (ET)':15s}  {'W/L':4s}  {'Side':5s}  {'AvgPx':>7s}  "
      f"{'Fills':>5s}  {'Invested':>9s}  {'P&L':>9s}  {'USDC':>8s}")
print(f"{'':>3}  {'':15s}  {'':4s}  {'':5s}  {'':>7s}  {'':>5s}  {'':>9s}  {'':>9s}  {'':>8s}")

running_pnl = 0
for i, v in enumerate(v9_list):
    epoch = int(v['slug'].split('-')[-1])
    dt = datetime.fromtimestamp(epoch, tz=timezone.utc) + timedelta(hours=-5)
    result = 'WIN' if v['won'] else 'LOSS'
    running_pnl += v['total_pnl']
    hour = dt.hour
    marker = ''
    if v['n_fills'] > 5:
        marker = f'  ** {v["n_fills"]} FILLS!'

    print(f"{i+1:>3}  {dt.strftime('%b%d %I:%M%p'):15s}  {result:4s}  {v['outcome']:5s}  "
          f"${v['avg_price']:>6.4f}  {v['n_fills']:>4d}   ${v['total_invested']:>8.2f}  "
          f"${v['total_pnl']:>+8.2f}  ${v['usdc_after']:>7.2f}{marker}")

print(f"\n  Running P&L: ${running_pnl:.2f}")

# ============================================================
# PATTERN ANALYSIS
# ============================================================
print(f"\n{'='*90}")
print(f"PATTERN ANALYSIS")
print(f"{'='*90}")

# Entry price distribution
win_prices = [v['avg_price'] for v in v9_list if v['won']]
loss_prices = [v['avg_price'] for v in v9_list if not v['won']]
print(f"\nEntry prices:")
print(f"  Wins:   avg=${sum(win_prices)/len(win_prices):.4f}  "
      f"min=${min(win_prices):.4f}  max=${max(win_prices):.4f}" if win_prices else "  Wins: none")
print(f"  Losses: avg=${sum(loss_prices)/len(loss_prices):.4f}  "
      f"min=${min(loss_prices):.4f}  max=${max(loss_prices):.4f}" if loss_prices else "  Losses: none")

# Fill count vs outcome
print(f"\nFill counts:")
fill_counts = defaultdict(lambda: {'wins': 0, 'losses': 0})
for v in v9_list:
    bucket = '1' if v['n_fills'] == 1 else '2-5' if v['n_fills'] <= 5 else '6-20' if v['n_fills'] <= 20 else '20+'
    if v['won']:
        fill_counts[bucket]['wins'] += 1
    else:
        fill_counts[bucket]['losses'] += 1

for bucket in ['1', '2-5', '6-20', '20+']:
    fc = fill_counts[bucket]
    total = fc['wins'] + fc['losses']
    if total > 0:
        print(f"  {bucket:>4s} fills: {fc['wins']}/{total} = {fc['wins']/total*100:.0f}% WR")

# Consecutive patterns
print(f"\nStreak analysis:")
streaks = []
current_streak = 0
current_type = None
for v in v9_list:
    if v['won']:
        if current_type == 'win':
            current_streak += 1
        else:
            if current_type is not None:
                streaks.append((current_type, current_streak))
            current_streak = 1
            current_type = 'win'
    else:
        if current_type == 'loss':
            current_streak += 1
        else:
            if current_type is not None:
                streaks.append((current_type, current_streak))
            current_streak = 1
            current_type = 'loss'
if current_type:
    streaks.append((current_type, current_streak))

for st, ct in streaks:
    marker = ' <<' if ct >= 3 else ''
    print(f"  {st.upper():4s} x{ct}{marker}")

# Side distribution
print(f"\nSide distribution:")
up_trades = [v for v in v9_list if v['outcome'] == 'Up']
down_trades = [v for v in v9_list if v['outcome'] == 'Down']
up_wins = sum(1 for v in up_trades if v['won'])
down_wins = sum(1 for v in down_trades if v['won'])
print(f"  UP:   {up_wins}/{len(up_trades)} = {up_wins/len(up_trades)*100:.0f}% WR  "
      f"P&L=${sum(v['total_pnl'] for v in up_trades):+.2f}" if up_trades else "  UP: none")
print(f"  DOWN: {down_wins}/{len(down_trades)} = {down_wins/len(down_trades)*100:.0f}% WR  "
      f"P&L=${sum(v['total_pnl'] for v in down_trades):+.2f}" if down_trades else "  DOWN: none")

# ============================================================
# ALL VERSIONS (deduped)
# ============================================================
print(f"\n{'='*90}")
print(f"ALL VERSIONS COMPARISON (deduplicated by market)")
print(f"{'='*90}")

for ver in sorted(set(t.get('signal_version', 'unknown') for t in trades)):
    ver_markets = {}
    for (slug, v), entries in grouped.items():
        if v == ver:
            won = entries[0]['won']
            pnl = sum(e['cash_pnl'] for e in entries)
            invested = sum(e['initial_value'] for e in entries)
            ver_markets[slug] = {'won': won, 'pnl': pnl, 'invested': invested, 'fills': len(entries)}

    w = sum(1 for m in ver_markets.values() if m['won'])
    n = len(ver_markets)
    pnl = sum(m['pnl'] for m in ver_markets.values())
    invested = sum(m['invested'] for m in ver_markets.values())
    fills = sum(m['fills'] for m in ver_markets.values())
    if n > 0:
        print(f"  {ver:15s}: {w:>2}/{n:>2} = {w/n*100:>5.1f}% WR  "
              f"P&L=${pnl:>+8.2f}  invested=${invested:>7.2f}  "
              f"ROI={pnl/invested*100:>+6.1f}%  fills={fills}")
