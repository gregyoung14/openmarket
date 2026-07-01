#!/usr/bin/env python3
"""Deep diagnosis of v11 live trades — v4.2.0-drift-v11 tagged."""

trades = [
    # (id, window, entry_time, side, shares, price, invested, payout, pnl, result)
    (762, "1:30AM-1:45AM", "1:49AM", "Down", 10.98, 0.3851, 4.23, 0.00, -4.23, "LOSS"),
    (763, "2:00AM-2:15AM", "2:17AM", "Down", 8.03, 0.5000, 4.02, 6.06, 2.05, "LOSS"),  # partial payout
    (764, "2:15AM-2:30AM", "2:33AM", "Up", 11.13, 0.3799, 4.23, 0.00, -4.23, "LOSS"),
    (765, "3:45AM-4:00AM", "4:04AM", "Down", 8.18, 0.5199, 4.25, 0.00, -4.25, "LOSS"),
    (766, "4:15AM-4:30AM", "4:34AM", "Up", 9.04, 0.4800, 4.34, 9.04, 4.70, "WIN"),
    (767, "5:30AM-5:45AM", "5:48AM", "Down", 11.11, 0.3999, 4.45, 0.00, -4.45, "LOSS"),
    (768, "7:45AM-8:00AM", "8:05AM", "Up", 12.99, 0.3261, 4.24, 12.99, 8.75, "WIN"),
    (769, "11:00AM-11:15AM", "11:19AM", "Up", 8.17, 0.5399, 4.41, 0.00, -4.41, "LOSS"),
    (770, "12:45PM-1:00PM", "1:04PM", "Down", 8.17, 0.5399, 4.41, 0.00, -4.41, "LOSS"),
    (771, "1:30PM-1:45PM", "1:48PM", "Down", 8.03, 0.5099, 4.09, 3.97, -0.12, "LOSS"),
    (772, "5:00PM-5:15PM", "5:18PM", "Down", 9.04, 0.4800, 4.34, 0.00, -4.34, "LOSS"),
    (773, "5:15PM-5:30PM", "5:35PM", "Down", 10.64, 0.3799, 4.04, 10.64, 6.60, "WIN"),
    (774, "5:30PM-5:45PM", "5:49PM", "Up", 9.06, 0.4399, 3.99, 9.06, 5.08, "WIN"),
    (775, "6:00PM-6:15PM", "6:19PM", "Up", 8.02, 0.5499, 4.41, 8.02, 3.61, "WIN"),
    (776, "6:30PM-6:45PM", "6:49PM", "Up", 10.07, 0.4499, 4.53, 0.00, -4.53, "LOSS"),
    (777, "6:45PM-7:00PM", "7:05PM", "Down", 12.17, 0.3599, 4.38, 0.00, -4.38, "LOSS"),
    (778, "7:45PM-8:00PM", "8:05PM", "Up", 9.05, 0.4599, 4.16, 0.00, -4.16, "LOSS"),
    (779, "8:30PM-8:45PM", "8:47PM", "Up", 8.03, 0.5099, 4.09, 0.68, -3.41, "LOSS"),
    (780, "9:00PM-9:15PM", "9:20PM", "Down", 9.06, 0.4500, 4.08, 0.00, -4.08, "LOSS"),
    (781, "9:30PM-9:45PM", "9:50PM", "Down", 9.07, 0.4299, 3.90, 0.00, -3.90, "LOSS"),
    (782, "11:15PM-11:30PM", "11:35PM", "Up", 10.11, 0.3899, 3.94, 0.00, -3.94, "LOSS"),
    (783, "11:30PM-11:45PM", "11:49PM", "Up", 13.27, 0.3099, 4.11, 0.00, -4.11, "LOSS"),
    # --- day 2 ---
    (784, "1:45AM-2:00AM", "2:04AM", "Up", 384.99, 0.0100, 3.85, 0.00, -3.85, "LOSS"),
    (785, "2:45AM-3:00AM", "3:04AM", "Up", 24.57, 0.1499, 3.68, 0.00, -3.68, "LOSS"),
    (786, "7:00AM-7:15AM", "7:18AM", "Down", 15.14, 0.2293, 3.47, 0.00, -3.47, "LOSS"),
    (787, "7:30AM-7:45AM", "7:49AM", "Down", 11.86, 0.3091, 3.67, 0.06, -3.61, "LOSS"),
    (788, "11:30AM-11:45AM", "11:49AM", "Up", 8.23, 0.4499, 3.70, 8.23, 4.53, "WIN"),
    (789, "12:15PM-12:30PM", "12:33PM", "Down", 14.04, 0.2764, 3.88, 0.00, -3.88, "LOSS"),
    (790, "1:00PM-1:15PM", "1:20PM", "Up", 11.36, 0.3200, 3.64, 0.00, -3.64, "LOSS"),
    (791, "2:00PM-2:15PM", "2:18PM", "Up", 117.31, 0.0299, 3.52, 0.00, -3.52, "LOSS"),
    (792, "2:45PM-3:00PM", "3:04PM", "Down", 10.16, 0.3399, 3.46, 10.16, 6.71, "WIN"),
    (793, "6:15PM-6:30PM", "6:32PM", "Down", 8.15, 0.4399, 3.58, 8.15, 4.56, "WIN"),
    (794, "6:45PM-7:00PM", "7:05PM", "Down", 21.95, 0.1699, 3.73, 0.00, -3.73, "LOSS"),
    (795, "8:00PM-8:15PM", "8:19PM", "Up", 375.99, 0.0100, 3.76, 0.00, -3.76, "LOSS"),
    (796, "10:15PM-10:30PM", "10:33PM", "Up", 345.85, 0.0103, 3.57, 0.00, -3.57, "LOSS"),
    (797, "10:30PM-10:45PM", "10:47PM", "Up", 9.12, 0.3699, 3.37, 9.07, 5.70, "WIN"),
]

print("=" * 80)
print(" V11 LIVE DEEP DIAGNOSIS — 4.2.0-drift-v11 (ALL trades)")
print("=" * 80)

total = len(trades)
wins = sum(1 for t in trades if t[9] == "WIN")
print(f"\n  Trades: {total}  |  Wins: {wins}  |  WR: {wins/total*100:.1f}%")
print(f"  Total P&L: ${sum(t[8] for t in trades):+.2f}")

# ═══ BUG 1: No MIN_ENTRY_PRICE floor ═══
print(f"\n{'─'*80}")
print(f"  BUG 1: Penny bets (no MIN_ENTRY_PRICE floor)")
print(f"{'─'*80}")
penny = [t for t in trades if t[5] < 0.15]
print(f"  Trades with entry < $0.15: {len(penny)}")
for t in penny:
    cost_per_share = t[6] / t[4]
    print(f"    #{t[0]} {t[1]:>17s}  {t[3]:>4s}  price=${t[5]:.4f}  shares={t[4]:>6.0f}  ${t[6]:.2f} → {t[9]}")
print(f"  All lost. P&L: ${sum(t[8] for t in penny):+.2f}")
print(f"\n  WHY THIS PASSES v11 FILTERS:")
print(f"    MAX_ENTRY_PRICE = 0.55 → $0.01 < 0.55 ✅ (ceiling, not floor!)")
print(f"    MIN_EDGE = 0.08 → conf(0.65) - (0.01 + 0.005) = 0.635 >> 0.08 ✅")
print(f"    RESULT: No filter catches cheap contracts.")

# ═══ BUG 2: Entry timing — redemption times show late execution ═══
print(f"\n{'─'*80}")
print(f"  BUG 2: Entry timing analysis (best-candidate delay)")
print(f"{'─'*80}")

def parse_window_end_min(window):
    """Parse the end time from window string like '1:30AM-1:45AM' → minutes from midnight"""
    end = window.split("-")[1]
    is_pm = "PM" in end.upper()
    end_clean = end.replace("AM","").replace("PM","").strip()
    parts = end_clean.split(":")
    h, m = int(parts[0]), int(parts[1])
    if is_pm and h != 12: h += 12
    if not is_pm and h == 12: h = 0
    return h * 60 + m

def parse_entry_min(entry_time):
    """Parse entry time like '1:49AM' → minutes from midnight"""
    is_pm = "PM" in entry_time.upper()
    clean = entry_time.replace("AM","").replace("PM","").strip()
    parts = clean.split(":")
    h, m = int(parts[0]), int(parts[1])
    if is_pm and h != 12: h += 12
    if not is_pm and h == 12: h = 0
    return h * 60 + m

# Note: the second column is REDEMPTION time (when position was settled after market close)
# NOT the entry time. So we compute how long after market close redemption happened.
print(f"  The 'entry time' column is actually REDEMPTION time (post-market-close).")
print(f"  Market closes, then poistions are redeemed ~4-5min later. This is normal.")
print(f"\n  However, the best-candidate mode fires entry at MAX_SECS_INTO_MARKET=600s.")
print(f"  That's 10 min into a 15 min window = only 5 min left.")
print(f"  The signal captures market.up_best_ask at QUALIFICATION time (earlier),")
print(f"  but the market may have shifted dramatically by then.")

# ═══ BUG 3: Blacklist gaps ═══
print(f"\n{'─'*80}")
print(f"  BUG 3: Blacklisted hours that still traded")
print(f"{'─'*80}")

# Feb 27 = Thursday (dow=3), Feb 28 = Friday (dow=4)
# Global blacklist: hours 0, 9, 10, 15, 16
# Thursday-specific: (3,6), (3,19), (3,23)
# Friday-specific: (4,7), (4,12), (4,13), (4,14), (4,17), (4,18), (4,19), (4,23)

blacklist_set = set()
for h in [0, 9, 10, 15, 16]:
    for d in range(7):
        blacklist_set.add((d, h))
blacklist_dow_hour = [
    (0,13),(0,18),(0,20),
    (1,3),(1,5),(1,6),(1,7),(1,8),(1,18),(1,21),(1,23),
    (2,7),(2,13),(2,18),(2,22),
    (3,6),(3,19),(3,23),
    (4,7),(4,12),(4,13),(4,14),(4,17),(4,18),(4,19),(4,23),
    (5,3),(5,5),(5,6),(5,21),(5,23),
    (6,1),(6,3),(6,20),(6,22),(6,23),
]
for dh in blacklist_dow_hour:
    blacklist_set.add(dh)

# Parse each trade's market-opening hour
hour_map = {
    "1:30AM": 1, "2:00AM": 2, "2:15AM": 2, "3:45AM": 3, "4:15AM": 4,
    "5:30AM": 5, "7:45AM": 7, "11:00AM": 11, "12:45PM": 12, "1:30PM": 13,
    "5:00PM": 17, "5:15PM": 17, "5:30PM": 17, "6:00PM": 18, "6:30PM": 18,
    "6:45PM": 18, "7:45PM": 19, "8:30PM": 20, "9:00PM": 21, "9:30PM": 21,
    "11:15PM": 23, "11:30PM": 23,
    "1:45AM": 1, "2:45AM": 2, "7:00AM": 7, "7:30AM": 7,
    "11:30AM": 11, "12:15PM": 12, "1:00PM": 13, "2:00PM": 14, "2:45PM": 14,
    "6:15PM": 18, "8:00PM": 20, "10:15PM": 22, "10:30PM": 22,
}

should_be_blocked = []
for t in trades:
    start_time = t[1].split("-")[0]
    hour_et = hour_map.get(start_time, -1)
    if t[0] <= 783:
        dow = 3  # Thursday
    else:
        dow = 4  # Friday
    
    is_bl = (dow, hour_et) in blacklist_set
    if is_bl:
        should_be_blocked.append(t)

print(f"  Trades that SHOULD have been blocked by blacklist: {len(should_be_blocked)}")
for t in should_be_blocked:
    start_time = t[1].split("-")[0]
    hour_et = hour_map.get(start_time, -1)
    dow = 3 if t[0] <= 783 else 4
    print(f"    #{t[0]} dow={dow} hour={hour_et}  {t[1]:>17s}  {t[3]:>4s}  ${t[5]:.4f}  → {t[9]}")
bl_wins = sum(1 for t in should_be_blocked if t[9] == "WIN")
if should_be_blocked:
    print(f"  Blocked WR: {bl_wins}/{len(should_be_blocked)} = {bl_wins/len(should_be_blocked)*100:.0f}%")
    print(f"  Blocked P&L: ${sum(t[8] for t in should_be_blocked):+.2f}")

# ═══ WR by price bucket, excluding penny bets ═══
print(f"\n{'─'*80}")
print(f"  CORE SIGNAL QUALITY (excluding penny bets and blacklisted)")
print(f"{'─'*80}")
clean = [t for t in trades if t[5] >= 0.15 and t not in should_be_blocked]
clean_wins = sum(1 for t in clean if t[9] == "WIN")
if clean:
    print(f"  Clean trades: {len(clean)}  Wins: {clean_wins}  WR: {clean_wins/len(clean)*100:.1f}%")
    print(f"  Clean P&L: ${sum(t[8] for t in clean):+.2f}")

    # By price bucket
    for lo, hi, label in [(0.15, 0.35, "$0.15-$0.35"), (0.35, 0.50, "$0.35-$0.50"), (0.50, 0.56, "$0.50-$0.55")]:
        bucket = [t for t in clean if lo <= t[5] < hi]
        if bucket:
            bw = sum(1 for t in bucket if t[9] == "WIN")
            print(f"    {label}: {len(bucket)} trades, {bw} wins, WR={bw/len(bucket)*100:.1f}%, P&L=${sum(t[8] for t in bucket):+.2f}")

# ═══ Direction bias ═══
print(f"\n{'─'*80}")
print(f"  DIRECTION BIAS")
print(f"{'─'*80}")
up_trades = [t for t in trades if t[3] == "Up"]
down_trades = [t for t in trades if t[3] == "Down"]
up_wins = sum(1 for t in up_trades if t[9] == "WIN")
down_wins = sum(1 for t in down_trades if t[9] == "WIN")
print(f"  UP trades:   {len(up_trades)}, wins: {up_wins}, WR: {up_wins/max(len(up_trades),1)*100:.1f}%")
print(f"  DOWN trades: {len(down_trades)}, wins: {down_wins}, WR: {down_wins/max(len(down_trades),1)*100:.1f}%")

up_clean = [t for t in up_trades if t[5] >= 0.15]
down_clean = [t for t in down_trades if t[5] >= 0.15]
up_c_wins = sum(1 for t in up_clean if t[9] == "WIN")
down_c_wins = sum(1 for t in down_clean if t[9] == "WIN")
print(f"\n  Excluding penny bets:")
print(f"  UP trades:   {len(up_clean)}, wins: {up_c_wins}, WR: {up_c_wins/max(len(up_clean),1)*100:.1f}%")
print(f"  DOWN trades: {len(down_clean)}, wins: {down_c_wins}, WR: {down_c_wins/max(len(down_clean),1)*100:.1f}%")

# ═══ Key findings ═══
print(f"\n{'═'*80}")
print(f"  KEY FINDINGS")
print(f"{'═'*80}")
print(f"""
  1. PENNY BETS ($0.01-$0.03): {len(penny)} trades, ALL LOST, -${abs(sum(t[8] for t in penny)):.2f}
     The v11 filters have NO MIN_ENTRY_PRICE floor.
     MAX_ENTRY_PRICE=0.55 is a CEILING. $0.01 passes easily.
     MIN_EDGE=0.08 also passes: conf(0.65) - 0.015 = 0.635 >> 0.08.
     FIX: Add MIN_ENTRY_PRICE = 0.15 or 0.20

  2. BLACKLIST GAPS: {len(should_be_blocked)} trades should've been blocked.
     Either the blacklist isn't applying or markets are mislabeled.

  3. CORE SIGNAL (clean trades): {len(clean)} trades, {clean_wins} wins, {clean_wins/max(len(clean),1)*100:.1f}% WR
     Even "clean" trades only achieve ~{clean_wins/max(len(clean),1)*100:.0f}% — below backtest's 68%+.
     This suggests the signal itself is underperforming in live conditions.

  4. SCOREBOARD_SCALE=300 (should be 1000) changes confidence calculations,
     which shifts which trades pass filters and alters edge values.
""")
