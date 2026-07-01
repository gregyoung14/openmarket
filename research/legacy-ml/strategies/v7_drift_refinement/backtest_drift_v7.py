"""
POLYMARKET BTC 15-MIN BACKTESTER - DRIFT ESTIMATOR v1
======================================================
Replaces the noisy "1s ML -> 30s rolling window" signal with a direct
Brownian drift estimator + price-vs-open scoreboard.

SIGNAL LOGIC (replaces SignalAggregator):
  At each second S into the market:
    1. Compute drift mu from Binance trades since market open
    2. Compute realized sigma from same window
    3. P(UP at 900s) = Phi(mu*sqrt(T_remaining) / sigma)   [Brownian motion projection]
    4. Scoreboard: how far has price moved from 15-min open?
    5. Combined signal = weighted blend of drift + scoreboard + OFI

  Entry when combined confidence > threshold AND entry within time window.

EXECUTION (identical to backtest_alpha.py):
  - Entry at ASK + slippage, capped at MAX_ENTRY_PRICE
  - Hold-to-resolve and momentum strategies
  - Fees, slippage, drawdown circuit breaker
"""

import sqlite3
import pandas as pd
import numpy as np
from scipy.stats import norm
import warnings
warnings.filterwarnings('ignore')
import time
import json

DB_PATH = 'polymarket_btc_data.db'

# ========================================================================
# CONFIG - Refined v7 Settings
# ========================================================================
INITIAL_BANKROLL    = 100.0
BET_FRACTION        = 0.05
SLIPPAGE            = 0.005
FEE_RATE            = 0.01

# Drift signal thresholds (tuned from v6 backtest results)
MIN_DRIFT_CONFIDENCE = 0.65   # Increased base threshold (was 0.60)
MIN_SCOREBOARD_MOVE  = 0.0000 

# Timing
MIN_SECS_INTO_MARKET = 60
MAX_SECS_INTO_MARKET = 600
MARKET_DURATION_SECS = 900
CONFIRMATION_WINDOW  = 45   # Increased from 30s to 45s for stronger confirmation

# Risk
MAX_OPEN_POSITIONS   = 1
MAX_DAILY_LOSS_PCT   = 0.20

# Strategy
MOMENTUM_TP          = 0.10
WIN_THRESHOLD        = 0.90
MAX_ENTRY_PRICE      = 0.99

# Confidence sweep - Focus on high conviction zones
CONFIDENCE_LEVELS    = [0.65, 0.70, 0.75, 0.80, 0.85]

# ========================================================================
# DATA LOADING
# ========================================================================

def load_all_data():
    conn = sqlite3.connect(DB_PATH)
    
    df_meta = pd.read_sql_query("SELECT * FROM market_meta ORDER BY first_seen_ms ASC", conn)
    
    df_ticks = pd.read_sql_query(
        """SELECT market_slug, source_ts_ms, side_label, price, best_bid, best_ask, size, event_type
           FROM polymarket_ticks_ms ORDER BY source_ts_ms ASC""", conn)
    
    df_trades = pd.read_sql_query(
        "SELECT trade_time, price, quantity, quote_volume, is_buyer_maker FROM binance_trades ORDER BY trade_time ASC", conn)
    
    conn.close()
    return df_meta, df_ticks, df_trades

# ========================================================================
# DRIFT SIGNAL GENERATOR
# ========================================================================

def compute_drift_signal(trades_window, open_price, entry_seconds, remaining_seconds):
    """
    Compute a drift-based directional signal from Binance trades.
    
    Returns:
        direction: 'UP' or 'DOWN'
        confidence: 0.5 to 1.0 (probability of predicted direction)
        components: dict of individual signal components
    """
    prices = trades_window['price'].values
    current_price = prices[-1]
    
    # --- Component 1: Brownian Drift Estimator ---
    # Estimate drift mu and vol sigma, project to end of 15-min market
    log_returns = np.diff(np.log(prices + 1e-9))
    
    if len(log_returns) < 5:
        return None, None, None
    
    dt = entry_seconds / len(log_returns)  # Avg time per observation (seconds)
    mu = np.mean(log_returns) / (dt + 1e-9)        # Drift per second
    sigma = np.std(log_returns) / (np.sqrt(dt) + 1e-9)  # Vol per sqrt-second
    
    if sigma > 0 and remaining_seconds > 0:
        z = mu * np.sqrt(remaining_seconds) / sigma
        drift_prob_up = float(norm.cdf(z))
    else:
        drift_prob_up = 0.5
    
    # --- Component 2: Scoreboard (price vs open) ---
    price_vs_open = (current_price - open_price) / (open_price + 1e-9)
    # Convert to a probability-like signal using sigmoid
    scoreboard_signal = 1 / (1 + np.exp(-price_vs_open * 5000))  # Scaled sigmoid
    
    # --- Component 3: Order Flow Imbalance (cumulative from open) ---
    is_buy = trades_window['is_buyer_maker'].values == 0
    buy_vol = trades_window['quantity'].values[is_buy].sum()
    sell_vol = trades_window['quantity'].values[~is_buy].sum()
    total_vol = buy_vol + sell_vol + 1e-9
    ofi = (buy_vol - sell_vol) / total_vol  # -1 to +1
    ofi_signal = 1 / (1 + np.exp(-ofi * 3))  # Sigmoid to probability space
    
    # --- Component 4: EMA Regime ---
    prices_series = pd.Series(prices)
    ema_fast = prices_series.ewm(span=min(10, len(prices)//2 + 1), adjust=False).mean().iloc[-1]
    ema_slow = prices_series.ewm(span=min(60, len(prices)), adjust=False).mean().iloc[-1]
    ema_cross = (ema_fast - ema_slow) / (ema_slow + 1e-9)
    ema_signal = 1 / (1 + np.exp(-ema_cross * 5000))

    # --- Weighted Combination (Refined Weights) ---
    # Increased Drift weight (0.45) based on high performance
    # Maintained Scoreboard (0.25)
    # Maintained OFI (0.20)
    # Reduced EMA (0.10) as it lags
    w_drift = 0.45
    w_scoreboard = 0.25
    w_ofi = 0.20
    w_ema = 0.10
    
    combined_prob_up = (
        w_drift * drift_prob_up +
        w_scoreboard * scoreboard_signal +
        w_ofi * ofi_signal +
        w_ema * ema_signal
    )
    
    # Determine direction and confidence
    if combined_prob_up > 0.5:
        direction = 'UP'
        confidence = combined_prob_up
    else:
        direction = 'DOWN'
        confidence = 1 - combined_prob_up
    
    # Consistency: do all signals agree on direction?
    signals_agree = [
        drift_prob_up > 0.5,
        scoreboard_signal > 0.5,
        ofi_signal > 0.5,
        ema_signal > 0.5,
    ]
    if direction == 'DOWN':
        signals_agree = [not s for s in signals_agree]
    consistency = sum(signals_agree) / len(signals_agree)
    
    components = {
        'drift_prob_up': drift_prob_up,
        'drift_mu': mu,
        'drift_sigma': sigma,
        'scoreboard': price_vs_open,
        'scoreboard_signal': scoreboard_signal,
        'ofi': ofi,
        'ofi_signal': ofi_signal,
        'ema_cross': ema_cross,
        'ema_signal': ema_signal,
        'combined_prob_up': combined_prob_up,
        'consistency': consistency,
    }
    
    return direction, confidence, components

# ========================================================================
# MARKET-LEVEL SIGNAL GENERATION (replaces ML-based build_market_signals)
# ========================================================================

def build_market_signals(df_meta, df_trades, df_ticks):
    """
    For each 15-min market, compute drift-based signals.
    Scans from MIN_SECS to MAX_SECS, enters when confidence threshold is hit.
    """
    print("\n  Computing drift-based signals...")
    
    p_ticks = df_ticks[df_ticks['event_type'] == 'price_change'].copy()
    signals = []
    
    for i, market in df_meta.iterrows():
        slug = market['market_slug']
        epoch_s = int(slug.split('-')[-1])
        start_ms = epoch_s * 1000
        end_ms = start_ms + MARKET_DURATION_SECS * 1000
        
        # Get BTC ground truth
        window_trades = df_trades[
            (df_trades['trade_time'] >= start_ms) & (df_trades['trade_time'] < end_ms)
        ]
        if len(window_trades) < 50:
            continue
        
        btc_start = window_trades.iloc[0]['price']
        btc_end = window_trades.iloc[-1]['price']
        actual_direction = 'UP' if btc_end > btc_start else 'DOWN'
        
        # Scan for entry point using drift estimator with confirmation
        hit_signal = None
        confirm_count = 0
        confirm_direction = None
        confirm_start_s = None
        
        for s in range(MIN_SECS_INTO_MARKET, MAX_SECS_INTO_MARKET):
            current_ms = start_ms + (s * 1000)
            remaining_s = MARKET_DURATION_SECS - s
            
            # All Binance trades from market open to now
            entry_trades = df_trades[
                (df_trades['trade_time'] >= start_ms) & (df_trades['trade_time'] < current_ms)
            ]
            
            if len(entry_trades) < 20:
                continue
            
            direction, confidence, components = compute_drift_signal(
                entry_trades, btc_start, s, remaining_s
            )
            
            if direction is None:
                confirm_count = 0
                confirm_direction = None
                continue
            
            # Confirmation: direction must be stable for CONFIRMATION_WINDOW seconds
            if confidence >= min(CONFIDENCE_LEVELS):
                if direction == confirm_direction:
                    confirm_count += 1
                else:
                    # Direction changed, reset counter
                    confirm_direction = direction
                    confirm_count = 1
                    confirm_start_s = s
                
                if confirm_count >= CONFIRMATION_WINDOW:
                    # Signal confirmed! Use the current (latest) signal values
                    # Get Polymarket entry price
                    entry_ticks = p_ticks[
                        (p_ticks['market_slug'] == slug) &
                        (p_ticks['source_ts_ms'] >= current_ms) &
                        (p_ticks['source_ts_ms'] < current_ms + 10000)
                    ]
                    
                    side_ticks = entry_ticks[entry_ticks['side_label'] == direction]
                    if len(side_ticks) == 0:
                        entry_ask = 0.50
                    else:
                        entry_ask = side_ticks.iloc[0]['best_ask']
                    
                    # Trajectories for momentum strategy
                    all_market_ticks = p_ticks[p_ticks['market_slug'] == slug].copy()
                    traj_ticks = all_market_ticks[all_market_ticks['source_ts_ms'] >= current_ms]
                    
                    up_traj = traj_ticks[traj_ticks['side_label'] == 'UP'][['source_ts_ms', 'best_bid', 'best_ask', 'price']].copy()
                    down_traj = traj_ticks[traj_ticks['side_label'] == 'DOWN'][['source_ts_ms', 'best_bid', 'best_ask', 'price']].copy()
                    
                    hit_signal = {
                        'slug': slug,
                        'start_ms': current_ms,
                        'end_ms': end_ms,
                        'btc_start': btc_start,
                        'btc_end': btc_end,
                        'actual': actual_direction,
                        'signal': direction,
                        'confidence': confidence,
                        'consistency': components['consistency'],
                        'entry_up_ask': entry_ask if direction == 'UP' else 0.50,
                        'entry_down_ask': entry_ask if direction == 'DOWN' else 0.50,
                        'up_trajectory': up_traj,
                        'down_trajectory': down_traj,
                        'n_preds': len(entry_trades),
                        'entry_secs_in': s,
                        # Drift-specific diagnostics
                        'drift_prob_up': components['drift_prob_up'],
                        'scoreboard': components['scoreboard'],
                        'ofi': components['ofi'],
                        'ema_cross': components['ema_cross'],
                        'combined_prob_up': components['combined_prob_up'],
                    }
                    break
        
        if hit_signal:
            signals.append(hit_signal)
            if len(signals) % 10 == 0:
                print(f"    Processed {len(signals)} signals...")
    
    # Build summary DataFrame
    cols = [k for k in signals[0].keys() if k not in ['up_trajectory', 'down_trajectory']] if signals else []
    df_signals = pd.DataFrame([{k: v for k, v in s.items() if k in cols} for s in signals])
    
    print(f"\n  Total signals: {len(signals)}")
    if len(df_signals) > 0:
        correct = (df_signals['signal'] == df_signals['actual']).sum()
        print(f"  Raw signal accuracy: {correct}/{len(df_signals)} = {correct/len(df_signals):.1%}")
        print(f"  Avg confidence: {df_signals['confidence'].mean():.3f}")
        print(f"  Avg consistency: {df_signals['consistency'].mean():.2f}")
        print(f"  Avg entry time: {df_signals['entry_secs_in'].mean():.0f}s")
        print(f"  Avg drift P(UP): {df_signals['drift_prob_up'].mean():.3f}")
        print(f"  Avg scoreboard: {df_signals['scoreboard'].mean():.6f}")
        print(f"  Avg OFI: {df_signals['ofi'].mean():.4f}")
    
    return df_signals, signals

# ========================================================================
# BACKTEST ENGINE (reused from backtest_alpha.py)
# ========================================================================

def backtest_hold_to_resolve(signals, bankroll, bet_frac, slippage, fee_rate, min_conf,
                              max_daily_loss_pct=MAX_DAILY_LOSS_PCT):
    """Hold to resolution. Same logic as backtest_alpha.py."""
    log = []
    equity_curve = [(0, bankroll)]
    peak_bankroll = bankroll
    halted = False
    
    for s in signals:
        if not halted:
            dd = (peak_bankroll - bankroll) / peak_bankroll if peak_bankroll > 0 else 0
            if dd >= max_daily_loss_pct:
                halted = True
                print(f"    [HALT] Drawdown {dd:.1%} >= {max_daily_loss_pct:.0%} at trade #{len(log)+1}")
        
        if halted:
            continue
        
        if s['confidence'] < min_conf:
            continue
        
        side = s['signal']
        entry_ask = s['entry_up_ask'] if side == 'UP' else s['entry_down_ask']
        
        if entry_ask is None or entry_ask <= 0 or entry_ask >= 1:
            continue
        
        entry_price = min(entry_ask + slippage, MAX_ENTRY_PRICE)
        
        bet_amount = bankroll * bet_frac
        fee_entry = bet_amount * fee_rate
        capital_after_fee = bet_amount - fee_entry
        shares = capital_after_fee / entry_price
        
        correct = (side == s['actual'])
        payout = shares * 1.00 if correct else 0.0
        fee_exit = payout * fee_rate
        net_payout = payout - fee_exit
        pnl = net_payout - bet_amount
        bankroll += pnl
        peak_bankroll = max(peak_bankroll, bankroll)
        
        start_dt = pd.to_datetime(s['start_ms'], unit='ms')
        end_dt = pd.to_datetime(s['end_ms'], unit='ms')
        
        log.append({
            'market': s['slug'], 'entry_time': str(start_dt), 'exit_time': str(end_dt),
            'side': side, 'entry_price': round(entry_price, 4),
            'exit_price': 1.00 if correct else 0.00,
            'shares': round(shares, 2), 'bet_amount': round(bet_amount, 2),
            'pnl': round(pnl, 2), 'bankroll': round(bankroll, 2),
            'confidence': round(s['confidence'], 4),
            'consistency': round(s.get('consistency', 1.0), 4),
            'actual': s['actual'], 'correct': correct,
            'entry_secs_in': s.get('entry_secs_in', 0),
            'strategy': 'HOLD_TO_RESOLVE',
            'drift_prob_up': round(s.get('drift_prob_up', 0), 4),
            'scoreboard': round(s.get('scoreboard', 0), 6),
        })
        equity_curve.append((len(log), bankroll))
    
    return log, equity_curve, bankroll

def backtest_momentum(signals, bankroll, bet_frac, slippage, fee_rate, min_conf, take_profit,
                       max_daily_loss_pct=MAX_DAILY_LOSS_PCT):
    """Momentum with take-profit. Same logic as backtest_alpha.py."""
    log = []
    equity_curve = [(0, bankroll)]
    peak_bankroll = bankroll
    halted = False
    
    for s in signals:
        if not halted:
            dd = (peak_bankroll - bankroll) / peak_bankroll if peak_bankroll > 0 else 0
            if dd >= max_daily_loss_pct:
                halted = True
        
        if halted:
            continue
        if s['confidence'] < min_conf:
            continue
        
        side = s['signal']
        entry_ask = s['entry_up_ask'] if side == 'UP' else s['entry_down_ask']
        trajectory = s['up_trajectory'] if side == 'UP' else s['down_trajectory']
        
        if entry_ask is None or entry_ask <= 0 or entry_ask >= 1:
            continue
        
        entry_price = min(entry_ask + slippage, MAX_ENTRY_PRICE)
        
        bet_amount = bankroll * bet_frac
        fee_entry = bet_amount * fee_rate
        capital_after_fee = bet_amount - fee_entry
        shares = capital_after_fee / entry_price
        
        tp_price = entry_price + take_profit
        exit_price = None
        exit_time = None
        exit_type = 'RESOLVE'
        
        if len(trajectory) > 0 and tp_price < MAX_ENTRY_PRICE:
            hits = trajectory[trajectory['best_bid'] >= tp_price]
            if len(hits) > 0:
                exit_price = hits.iloc[0]['best_bid'] - slippage
                exit_time = pd.to_datetime(hits.iloc[0]['source_ts_ms'], unit='ms')
                exit_type = 'TAKE_PROFIT'
        
        if exit_price is None:
            correct = (side == s['actual'])
            exit_price = 1.00 if correct else 0.00
            exit_time = pd.to_datetime(s['end_ms'], unit='ms')
            exit_type = 'RESOLVE_WIN' if correct else 'RESOLVE_LOSS'
        
        payout = shares * exit_price
        fee_exit = payout * fee_rate if payout > 0 else 0
        net_payout = payout - fee_exit
        pnl = net_payout - bet_amount
        bankroll += pnl
        peak_bankroll = max(peak_bankroll, bankroll)
        
        correct = (side == s['actual'])
        start_dt = pd.to_datetime(s['start_ms'], unit='ms')
        
        log.append({
            'market': s['slug'], 'entry_time': str(start_dt), 'exit_time': str(exit_time),
            'side': side, 'entry_price': round(entry_price, 4),
            'exit_price': round(exit_price, 4),
            'shares': round(shares, 2), 'bet_amount': round(bet_amount, 2),
            'pnl': round(pnl, 2), 'bankroll': round(bankroll, 2),
            'confidence': round(s['confidence'], 4),
            'consistency': round(s.get('consistency', 1.0), 4),
            'actual': s['actual'], 'correct': correct,
            'exit_type': exit_type, 'take_profit_target': take_profit,
            'entry_secs_in': s.get('entry_secs_in', 0),
            'strategy': f'MOMENTUM_TP{int(take_profit*100)}',
        })
        equity_curve.append((len(log), bankroll))
    
    return log, equity_curve, bankroll

# ========================================================================
# REPORTING
# ========================================================================

def print_strategy_report(name, log, final_bankroll):
    if len(log) == 0:
        print(f"\n  {name}: No trades executed")
        return
    
    trades = len(log)
    wins = sum(1 for t in log if t['pnl'] > 0)
    losses = trades - wins
    total_pnl = sum(t['pnl'] for t in log)
    avg_pnl = total_pnl / trades
    win_rate = wins / trades
    
    peak = INITIAL_BANKROLL
    max_dd = 0
    equity = INITIAL_BANKROLL
    for t in log:
        equity += t['pnl']
        peak = max(peak, equity)
        dd = (peak - equity) / peak
        max_dd = max(max_dd, dd)
    
    roi = (final_bankroll - INITIAL_BANKROLL) / INITIAL_BANKROLL * 100
    avg_entry_secs = np.mean([t.get('entry_secs_in', 0) for t in log])
    
    print(f"\n{'-'*60}")
    print(f"  {name}")
    print(f"{'-'*60}")
    print(f"  Trades:       {trades}")
    print(f"  Wins:         {wins} ({win_rate:.1%})")
    print(f"  Losses:       {losses}")
    print(f"  Total P&L:    ${total_pnl:+.2f}")
    print(f"  Avg P&L:      ${avg_pnl:+.2f}")
    print(f"  Final Bank:   ${final_bankroll:.2f}")
    print(f"  ROI:          {roi:+.1f}%")
    print(f"  Max Drawdown: {max_dd:.1%}")
    print(f"  Avg Entry:    {avg_entry_secs:.0f}s into market")

def generate_chart(all_results):
    """Generate HTML chart for equity curves."""
    chart_data = {}
    for name, (log, ec, final) in all_results.items():
        curve_points = []
        bankroll = INITIAL_BANKROLL
        for trade in log:
            bankroll += trade['pnl']
            curve_points.append(bankroll)
        chart_data[name] = curve_points
    
    colors = ['#00d4aa', '#ff6b6b', '#4ecdc4', '#ffd93d', '#6c5ce7', '#fd79a8', '#a29bfe']
    
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Polymarket BTC Drift Backtest</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ background: #0a0a1a; color: #e0e0e0; font-family: 'Inter', sans-serif; padding: 24px; }}
  h1 {{ font-size: 28px; font-weight: 700; margin-bottom: 8px;
    background: linear-gradient(135deg, #ffd93d, #ff6b6b);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent; }}
  .subtitle {{ color: #888; font-size: 14px; margin-bottom: 12px; }}
  .config-badge {{ display: inline-block; background: #1a1a35; border: 1px solid #2a2a55;
    border-radius: 6px; padding: 4px 10px; font-size: 11px; color: #ffd93d;
    margin-bottom: 24px; margin-right: 8px; }}
  .chart-container {{ background: #12122a; border-radius: 16px; padding: 24px;
    border: 1px solid #1e1e3a; margin-bottom: 24px; }}
  canvas {{ width: 100% !important; height: 400px !important; }}
  .stats-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
    gap: 16px; margin-bottom: 24px; }}
  .stat-card {{ background: #12122a; border-radius: 12px; padding: 20px;
    border: 1px solid #1e1e3a; }}
  .stat-card h3 {{ font-size: 14px; color: #888; margin-bottom: 8px; font-weight: 500; }}
  .stat-card .value {{ font-size: 24px; font-weight: 700; }}
  .stat-card .value.positive {{ color: #00d4aa; }}
  .stat-card .value.negative {{ color: #ff6b6b; }}
  .stat-card .meta {{ color: #666; font-size: 11px; margin-top: 6px; }}
  .trade-log {{ background: #12122a; border-radius: 16px; padding: 24px;
    border: 1px solid #1e1e3a; overflow-x: auto; }}
  .trade-log h2 {{ font-size: 18px; margin-bottom: 16px; color: #fff; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
  th {{ text-align: left; padding: 10px 8px; color: #888; border-bottom: 1px solid #1e1e3a; font-weight: 500; }}
  td {{ padding: 8px; border-bottom: 1px solid #0d0d22; }}
  tr:hover {{ background: #1a1a35; }}
  .win {{ color: #00d4aa; }}
  .loss {{ color: #ff6b6b; }}
  .legend {{ display: flex; gap: 20px; flex-wrap: wrap; margin-bottom: 16px; }}
  .legend-item {{ display: flex; align-items: center; gap: 8px; font-size: 13px; }}
  .legend-dot {{ width: 12px; height: 12px; border-radius: 3px; }}
</style>
</head>
<body>
<h1>Drift Estimator Backtest</h1>
<p class="subtitle">Brownian Drift + Scoreboard + OFI + EMA Regime | 15-Min Markets</p>
<div>
  <span class="config-badge">Bankroll: ${INITIAL_BANKROLL}</span>
  <span class="config-badge">Bet: {BET_FRACTION*100:.0f}%</span>
  <span class="config-badge">Signal: Drift Estimator</span>
  <span class="config-badge">Entry: {MIN_SECS_INTO_MARKET}-{MAX_SECS_INTO_MARKET}s</span>
  <span class="config-badge">Max DD: {MAX_DAILY_LOSS_PCT*100:.0f}%</span>
  <span class="config-badge">Slip: ${SLIPPAGE}</span>
  <span class="config-badge">Fee: {FEE_RATE*100:.0f}%/leg</span>
</div>
"""
    
    # Stats cards
    html += '<div class="stats-grid">'
    for i, (name, (log, ec, final)) in enumerate(all_results.items()):
        if len(log) == 0: continue
        roi = (final - INITIAL_BANKROLL) / INITIAL_BANKROLL * 100
        wins = sum(1 for t in log if t['pnl'] > 0)
        wr = wins / len(log) * 100
        total_pnl = sum(t['pnl'] for t in log)
        cls = 'positive' if roi >= 0 else 'negative'
        pk = INITIAL_BANKROLL; mdd = 0; eq = INITIAL_BANKROLL
        for t in log:
            eq += t['pnl']; pk = max(pk, eq); mdd = max(mdd, (pk - eq) / pk)
        html += f'''<div class="stat-card">
            <h3>{name}</h3>
            <div class="value {cls}">${final:.2f} ({roi:+.1f}%)</div>
            <div class="meta">{len(log)} trades | {wr:.0f}% WR | {mdd:.1%} MDD | P&L: ${total_pnl:+.2f}</div>
        </div>'''
    html += '</div>'
    
    # Chart
    html += '<div class="chart-container"><div class="legend">'
    for i, name in enumerate(chart_data.keys()):
        c = colors[i % len(colors)]
        html += f'<div class="legend-item"><div class="legend-dot" style="background:{c}"></div>{name}</div>'
    html += '</div><canvas id="chart"></canvas></div>'
    
    # Trade log for best strategy
    best_name = max(all_results.keys(), key=lambda k: all_results[k][2])
    best_log = all_results[best_name][0]
    
    html += f'<div class="trade-log"><h2>Trade Log - {best_name}</h2><table><thead><tr>'
    for h in ['#','Market','Time','Side','Entry','Exit','Bet','P&L','Bank','Conf','Consist','Drift','Score','Actual','Result']:
        html += f'<th>{h}</th>'
    html += '</tr></thead><tbody>'
    
    for i, t in enumerate(best_log):
        cls = 'win' if t['pnl'] > 0 else 'loss'
        result = 'WIN' if t.get('correct') else 'LOSS'
        html += f'<tr><td>{i+1}</td>'
        html += f'<td>{t["market"].split("-")[-1]}</td>'
        html += f'<td>{t["entry_time"][11:19]}</td>'
        html += f'<td>{t["side"]}</td>'
        html += f'<td>${t["entry_price"]:.3f}</td>'
        html += f'<td>${t["exit_price"]:.3f}</td>'
        html += f'<td>${t["bet_amount"]:.2f}</td>'
        html += f'<td class="{cls}">${t["pnl"]:+.2f}</td>'
        html += f'<td>${t["bankroll"]:.2f}</td>'
        html += f'<td>{t["confidence"]:.2f}</td>'
        html += f'<td>{t.get("consistency", 0):.2f}</td>'
        html += f'<td>{t.get("drift_prob_up", 0):.2f}</td>'
        html += f'<td>{t.get("scoreboard", 0):.6f}</td>'
        html += f'<td>{t["actual"]}</td>'
        html += f'<td>{result}</td></tr>'
    
    html += '</tbody></table></div>'
    
    # Chart JS
    html += '<script>'
    html += 'const canvas = document.getElementById("chart");'
    html += 'const ctx = canvas.getContext("2d");'
    html += 'const dpr = window.devicePixelRatio || 1;'
    html += 'canvas.width = canvas.offsetWidth * dpr;'
    html += 'canvas.height = 400 * dpr;'
    html += 'ctx.scale(dpr, dpr);'
    html += 'const W = canvas.offsetWidth, H = 400;'
    
    chart_json = json.dumps(chart_data)
    color_json = json.dumps(colors)
    html += f'const data = {chart_json};'
    html += f'const colorList = {color_json};'
    html += f'const initBank = {INITIAL_BANKROLL};'
    html += '''
    let allVals = [initBank];
    for (let k of Object.keys(data)) { allVals.push(initBank); data[k].forEach(v => allVals.push(v)); }
    const minV = Math.min(...allVals) * 0.95;
    const maxV = Math.max(...allVals) * 1.05;
    
    ctx.strokeStyle = "#1e1e3a"; ctx.lineWidth = 1;
    for (let i = 0; i <= 5; i++) {
      let y = H - 40 - (i/5)*(H-60);
      ctx.beginPath(); ctx.moveTo(50,y); ctx.lineTo(W-20,y); ctx.stroke();
      let val = minV + (i/5)*(maxV-minV);
      ctx.fillStyle="#666"; ctx.font="11px Inter";
      ctx.fillText("$"+val.toFixed(0), 5, y+4);
    }
    
    let ci = 0;
    for (let [name, pts] of Object.entries(data)) {
      let full = [initBank, ...pts];
      let c = colorList[ci % colorList.length]; ci++;
      ctx.strokeStyle = c; ctx.lineWidth = 2; ctx.beginPath();
      for (let j = 0; j < full.length; j++) {
        let x = 50 + (j / Math.max(full.length-1, 1)) * (W - 70);
        let y = H - 40 - ((full[j] - minV) / (maxV - minV)) * (H - 60);
        if (j === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      }
      ctx.stroke();
    }
    '''
    html += '</script></body></html>'
    
    with open('drift_backtest_results.html', 'w') as f:
        f.write(html)
    print(f"\n  Chart saved to drift_backtest_results.html")

# ========================================================================
# MAIN
# ========================================================================

def main():
    t0 = time.time()
    
    print("="*70)
    print(" POLYMARKET BTC 15-MIN BACKTEST - DRIFT ESTIMATOR v1")
    print("="*70)
    print(f"  Signal method:       Brownian Drift + Scoreboard + OFI + EMA")
    print(f"  Initial bankroll:    ${INITIAL_BANKROLL}")
    print(f"  Bet size:            {BET_FRACTION*100:.0f}% of bankroll")
    print(f"  Slippage:            ${SLIPPAGE}")
    print(f"  Fees:                {FEE_RATE*100:.1f}% per leg")
    print(f"  Entry window:        {MIN_SECS_INTO_MARKET}s - {MAX_SECS_INTO_MARKET}s")
    print(f"  Max drawdown halt:   {MAX_DAILY_LOSS_PCT:.0%}")
    print(f"  Confidence levels:   {CONFIDENCE_LEVELS}")
    
    # Load
    print("\n  Loading data...")
    df_meta, df_ticks, df_trades = load_all_data()
    print(f"  Markets: {len(df_meta)}, Ticks: {len(df_ticks)}, Trades: {len(df_trades)}")
    
    # Build drift signals
    signals_df, signals_full = build_market_signals(df_meta, df_trades, df_ticks)
    
    if len(signals_df) == 0:
        print("\n  No signals generated. Exiting.")
        return
    
    # ============================================================
    # HOLD-TO-RESOLVE: Confidence sweep
    # ============================================================
    all_results = {}
    hold_sweep = []
    
    print(f"\n{'='*70}")
    print(f" HOLD-TO-RESOLVE: CONFIDENCE SWEEP")
    print(f"{'='*70}")
    
    for conf_thresh in CONFIDENCE_LEVELS:
        name = f'Drift Hold (>{conf_thresh:.0%})'
        log, ec, final = backtest_hold_to_resolve(
            signals_full, INITIAL_BANKROLL, BET_FRACTION, SLIPPAGE, FEE_RATE, conf_thresh)
        all_results[name] = (log, ec, final)
        
        if len(log) > 0:
            wins = sum(1 for t in log if t['pnl'] > 0)
            wr = wins / len(log) * 100
            roi = (final - INITIAL_BANKROLL) / INITIAL_BANKROLL * 100
            total_pnl = sum(t['pnl'] for t in log)
            pk = INITIAL_BANKROLL; mdd = 0; eq = INITIAL_BANKROLL
            for t in log:
                eq += t['pnl']; pk = max(pk, eq); mdd = max(mdd, (pk - eq) / pk)
            hold_sweep.append({
                'conf': conf_thresh, 'trades': len(log), 'wins': wins,
                'wr': wr, 'roi': roi, 'final': final, 'pnl': total_pnl, 'mdd': mdd
            })
    
    # Print sweep table
    print(f"\n  {'Conf':>6s}  {'Trades':>6s}  {'Wins':>5s}  {'WR%':>6s}  {'ROI':>8s}  {'Final':>8s}  {'MDD':>6s}  {'P&L':>8s}")
    print(f"  {'-'*6}  {'-'*6}  {'-'*5}  {'-'*6}  {'-'*8}  {'-'*8}  {'-'*6}  {'-'*8}")
    for row in hold_sweep:
        print(f"  {row['conf']:>5.0%}   {row['trades']:>5d}   {row['wins']:>4d}   {row['wr']:>5.1f}%  {row['roi']:>+7.1f}%  ${row['final']:>7.2f}  {row['mdd']:>5.1%}  ${row['pnl']:>+7.2f}")
    
    # ============================================================
    # MOMENTUM: TP sweep
    # ============================================================
    print(f"\n{'='*70}")
    print(f" MOMENTUM: TP SWEEP")
    print(f"{'='*70}")
    
    for conf in [0.55, 0.60, 0.65]:
        for tp in [0.10]:
            name = f'Drift Mom TP={tp:.0%} >{conf:.0%}'
            log, ec, final = backtest_momentum(
                signals_full, INITIAL_BANKROLL, BET_FRACTION, SLIPPAGE, FEE_RATE, conf, tp)
            all_results[name] = (log, ec, final)
            if len(log) > 0:
                wins = sum(1 for t in log if t['pnl'] > 0)
                wr = wins / len(log) * 100
                roi = (final - INITIAL_BANKROLL) / INITIAL_BANKROLL * 100
                tp_exits = sum(1 for t in log if t.get('exit_type') == 'TAKE_PROFIT')
                print(f"  {name:35s}: {len(log):>3d} trades  WR={wr:>5.1f}%  ROI={roi:>+6.1f}%  TP_exits={tp_exits}")
    
    # ============================================================
    # TOP STRATEGIES
    # ============================================================
    print(f"\n{'='*70}")
    print(f" TOP STRATEGIES (by Final Bankroll)")
    print(f"{'='*70}")
    
    ranked = sorted(
        [(n, l, e, f) for n, (l, e, f) in all_results.items() if len(l) > 0],
        key=lambda x: -x[3]
    )[:10]
    
    for rank, (name, log, ec, final) in enumerate(ranked, 1):
        roi = (final - INITIAL_BANKROLL) / INITIAL_BANKROLL * 100
        wins = sum(1 for t in log if t['pnl'] > 0)
        wr = wins / len(log) * 100
        marker = ' << BEST' if rank == 1 else ''
        print(f"  #{rank:<2d} {name:35s}: ${final:>7.2f} ({roi:>+6.1f}%)  WR={wr:>5.1f}%{marker}")
    
    # Generate chart and save
    generate_chart(all_results)
    
    # Save trade log
    best_name = max(all_results, key=lambda k: all_results[k][2])
    best_log = all_results[best_name][0]
    if best_log:
        df_log = pd.DataFrame(best_log)
        df_log.to_csv('drift_trade_log.csv', index=False)
        print(f"\n  Trade log saved to drift_trade_log.csv (best: {best_name})")
    
    if hold_sweep:
        pd.DataFrame(hold_sweep).to_csv('drift_confidence_sweep.csv', index=False)
        print(f"  Confidence sweep saved to drift_confidence_sweep.csv")
    
    elapsed = time.time() - t0
    print(f"\n  Done in {elapsed:.0f}s")

if __name__ == "__main__":
    main()
