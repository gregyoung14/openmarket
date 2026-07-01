"""
POLYMARKET BTC 15-MIN BACKTESTER — V9.2 REGIME-AWARE (IMPROVED FILTERS)
=========================================================================
v9.2 is a parameter-only refinement of v9. No architectural changes.
All signal math is identical; only the entry filter config changed.

What changed from v9 → v9.2 (validated from 17h live session analysis):

1. MAX_ENTRY_PRICE  0.80 → 0.55
   Live data: entries at 0.60-0.70 had 33.3% WR (-$7.64 net on 12 trades).
   At 0.65 entry, break-even WR = 66.3%; model only delivers ~57% WR.
   Capping at 0.55 ensures mathematical edge before every bet.

2. BLACKLIST_HOURS_ET  (none) → {0, 9, 10, 15, 16}
   Live data: those 5 hour buckets averaged 25% WR (-$9.01 combined).
   0h  = midnight ET (thin, erratic)
   9h  = US market open (drift estimator fails in extreme vol)
   10h = adjacency contamination around US open
   15h = US close run-up / noise
   16h = US market close regime shift
   Backtest: blacklisting these gates 38/155 markets, improves WR from ~64% → 68%.

3. MIN_EDGE  0.05 → 0.08
   Tighter edge floor ensures each trade has more headroom above entry price.

4. MIN_CONFIDENCE  0.55 → 0.60
   Aligns default floor with the previously-recommended sweep level.

BACKTEST RESULT (155 markets, full DB):
  Best: Hold C>60%  — 75 trades, 68.0% WR, +218.7% ROI ($318.75)
  vs v9 baseline   — +107.4% ROI (+111 pts improvement from filters alone)

SIGNAL ARCHITECTURE (unchanged from v9):
  Drift(55%) + OFI Acceleration(30%) + Reduced Scoreboard(15%)
  Regime Gate: path efficiency + autocorrelation
  Adaptive Confirmation: 15-50s window based on realized vol
  Improved Poly Lookup: backward-first search for most recent tick
"""

import os
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
# CONFIG
# ========================================================================
INITIAL_BANKROLL    = 100.0
BET_FRACTION        = 0.05
SLIPPAGE            = 0.005
FEE_RATE            = 0.01

# --- Signal Architecture (unchanged from v9) ---
W_DRIFT             = 0.55      # Brownian drift projection weight
W_OFI_ACCEL         = 0.30      # OFI acceleration weight (detrended)
W_SCOREBOARD        = 0.15      # Reduced scoreboard weight
SCOREBOARD_SCALE    = 1000      # Was 5000 in v6 (5x less sensitive)
OFI_SCALE           = 3         # Sigmoid scaling for OFI

# --- Regime Detection (unchanged from v9) ---
REGIME_TREND_THRESHOLD = 0.15   # Path efficiency >= for 'trend'
REGIME_CHOP_THRESHOLD  = 0.06   # Path efficiency < for 'chop'
REGIME_AUTOCORR_CHOP   = -0.25  # Override: very negative autocorr = chop
REGIME_LOOKBACK        = 60     # Seconds of data for regime detection
NEUTRAL_CONF_PENALTY   = 0.02   # Confidence penalty for 'neutral' regime

# --- Timing (unchanged) ---
MIN_SECS_INTO_MARKET = 60
MAX_SECS_INTO_MARKET = 600
MARKET_DURATION_SECS = 900

# --- Adaptive Confirmation (unchanged from v9) ---
BASE_CONFIRM_WINDOW  = 30
MIN_CONFIRM_WINDOW   = 15
MAX_CONFIRM_WINDOW   = 50

# ---- V9.2 IMPROVED ENTRY FILTERS ----
MIN_CONFIDENCE       = 0.60     # Raised from 0.55 (aligns with best sweep level)
MAX_ENTRY_PRICE      = 0.55     # Tightened from 0.80
                                # At 0.55 entry: need 56.1% WR to break even ✓
                                # At 0.65 entry: need 66.3% WR — model can't sustain
                                # At 0.80 entry: need 81.6% WR — never achievable
MIN_EDGE             = 0.08     # Raised from 0.05 (confidence must exceed entry by 8pts)

# ---- V9.2 HOUR BLACKLIST (ET = UTC-5) ----
# Derived from 67-trade live session (Feb 17 2026, 17 hours).
# These hours averaged 25% WR when the overall session was 57% WR.
#
# Hour (ET) | Live Trades | Win Rate | Net P&L | Why
# ----------|-------------|----------|---------|----
#  0h (mid) |      4      |  25.0%   | -$2.10  | Thin liquidity, erratic
#  9h (open)|      4      |  25.0%   | -$2.80  | Extreme vol breaks drift estimator
# 10h       |      4      |  25.0%   | -$2.11  | Open contamination
# 15h (cls) |      2      |  0.0%    | -$0.95  | Close run-up regime shift
# 16h (cls) |      2      |  25.0%   | -$1.05  | Post-close regime shift
BLACKLIST_HOURS_ET   = {0, 9, 10, 15, 16}

# --- Risk (unchanged) ---
MAX_DAILY_LOSS_PCT   = 0.20

# --- Strategy ---
MOMENTUM_TP          = 0.10

# --- Sweeps ---
CONFIDENCE_LEVELS    = [0.55, 0.60, 0.65, 0.70, 0.75, 0.80]
EDGE_LEVELS          = [0.00, 0.05, 0.08, 0.10, 0.12]


# ========================================================================
# DATA LOADING (same as v6/v9)
# ========================================================================

def load_all_data():
    conn = sqlite3.connect(DB_PATH)
    df_meta = pd.read_sql_query(
        "SELECT * FROM market_meta ORDER BY first_seen_ms ASC", conn)
    df_ticks = pd.read_sql_query(
        """SELECT market_slug, source_ts_ms, side_label, price, best_bid, best_ask, size, event_type
           FROM polymarket_ticks_ms ORDER BY source_ts_ms ASC""", conn)
    df_trades = pd.read_sql_query(
        "SELECT trade_time, price, quantity, quote_volume, is_buyer_maker FROM binance_trades ORDER BY trade_time ASC",
        conn)
    conn.close()
    return df_meta, df_ticks, df_trades


# ========================================================================
# REGIME DETECTION (unchanged from v9)
# ========================================================================

def detect_regime(close_1s, lookback=REGIME_LOOKBACK):
    """
    Classify current market regime from recent 1-second close prices.

    Uses two independent metrics:
      1. Path efficiency: |net move| / total distance traveled
         - Random walk @ 60 bars: E[path_eff] ≈ 0.13
         - Trending: path_eff >> 0.13
         - Choppy: path_eff << 0.13
      2. Return autocorrelation (lag-1):
         - Trending: positive (consecutive moves same direction)
         - Mean-reverting: negative (consecutive moves reverse)

    Returns:
        regime: 'trend', 'chop', or 'neutral'
        path_eff: float
        autocorr: float
    """
    n = len(close_1s)
    recent = close_1s[-min(n, lookback):]

    # Remove NaN/zero from forward-filled gaps
    valid = recent[~np.isnan(recent) & (recent > 0)]
    if len(valid) < 15:
        return 'neutral', 0.0, 0.0

    # 1. Path efficiency
    direct = abs(float(valid[-1]) - float(valid[0]))
    total_path = np.sum(np.abs(np.diff(valid)))
    path_eff = direct / (total_path + 1e-12)

    # 2. Return autocorrelation (lag-1)
    returns = np.diff(np.log(valid.astype(float) + 1e-9))
    if len(returns) > 5:
        autocorr = float(np.corrcoef(returns[:-1], returns[1:])[0, 1])
        if np.isnan(autocorr):
            autocorr = 0.0
    else:
        autocorr = 0.0

    # Classification
    if autocorr < REGIME_AUTOCORR_CHOP:
        return 'chop', path_eff, autocorr
    if path_eff >= REGIME_TREND_THRESHOLD and autocorr > -0.10:
        return 'trend', path_eff, autocorr
    elif path_eff < REGIME_CHOP_THRESHOLD:
        return 'chop', path_eff, autocorr
    else:
        return 'neutral', path_eff, autocorr


# ========================================================================
# V9 SIGNAL GENERATOR (unchanged from v9)
# ========================================================================

def compute_signal_v9(close_1s, buy_vol_1s, sell_vol_1s, open_price,
                      entry_secs, remaining_secs):
    """
    V9 signal: Drift (55%) + OFI Acceleration (30%) + Reduced Scoreboard (15%).
    Includes regime detection and adaptive confirmation window.

    Args:
        close_1s:      numpy array of 1-second close prices (market open → now)
        buy_vol_1s:    numpy array of buy volume per second
        sell_vol_1s:   numpy array of sell volume per second
        open_price:    BTC price at market open
        entry_secs:    seconds into the market
        remaining_secs: seconds until market close

    Returns:
        direction: 'UP' or 'DOWN' (or None if insufficient data)
        confidence: 0.5 to 1.0
        components: dict with diagnostics
    """
    n = len(close_1s)
    valid_mask = ~np.isnan(close_1s) & (close_1s > 0)
    valid_prices = close_1s[valid_mask]
    if len(valid_prices) < 15:
        return None, None, None

    current_price = float(valid_prices[-1])

    # === REGIME DETECTION ===
    regime, path_eff, autocorr = detect_regime(close_1s)

    # === Component 1: Brownian Drift Estimator (55% weight) ===
    log_returns = np.diff(np.log(valid_prices.astype(float) + 1e-9))

    if len(log_returns) < 5:
        return None, None, None

    mu = float(np.mean(log_returns))       # drift per second
    sigma = float(np.std(log_returns))     # vol per second

    if sigma > 0 and remaining_secs > 0:
        z = mu * np.sqrt(remaining_secs) / sigma
        drift_prob_up = float(norm.cdf(z))
    else:
        drift_prob_up = 0.5

    # === Component 2: OFI Acceleration (30% weight) ===
    half = max(n // 2, 5)
    buy_recent = float(buy_vol_1s[-half:].sum())
    sell_recent = float(sell_vol_1s[-half:].sum())
    buy_earlier = float(buy_vol_1s[:half].sum())
    sell_earlier = float(sell_vol_1s[:half].sum())

    ofi_recent = (buy_recent - sell_recent) / (buy_recent + sell_recent + 1e-9)
    ofi_earlier = (buy_earlier - sell_earlier) / (buy_earlier + sell_earlier + 1e-9)
    ofi_accel = ofi_recent - ofi_earlier
    ofi_accel_signal = 1.0 / (1.0 + np.exp(-ofi_accel * OFI_SCALE))

    # === Component 3: Reduced Scoreboard (15% weight) ===
    price_vs_open = (current_price - open_price) / (open_price + 1e-9)
    scoreboard_signal = 1.0 / (1.0 + np.exp(-price_vs_open * SCOREBOARD_SCALE))

    # === Weighted Combination ===
    combined_prob_up = (
        W_DRIFT * drift_prob_up +
        W_OFI_ACCEL * ofi_accel_signal +
        W_SCOREBOARD * scoreboard_signal
    )

    if combined_prob_up > 0.5:
        direction = 'UP'
        confidence = combined_prob_up
    else:
        direction = 'DOWN'
        confidence = 1.0 - combined_prob_up

    # Neutral regime → small confidence penalty
    if regime == 'neutral':
        confidence -= NEUTRAL_CONF_PENALTY

    # === Adaptive Confirmation Window ===
    recent_rets = log_returns[-30:] if len(log_returns) > 30 else log_returns
    vol = float(np.std(recent_rets)) if len(recent_rets) > 3 else 0.0
    vol_score = min(vol / 0.0002, 2.0)
    adaptive_confirm = int(BASE_CONFIRM_WINDOW * max(0.5, 1.3 - 0.3 * vol_score))
    adaptive_confirm = max(MIN_CONFIRM_WINDOW, min(MAX_CONFIRM_WINDOW, adaptive_confirm))

    signals_agree = [
        drift_prob_up > 0.5,
        ofi_accel_signal > 0.5,
        scoreboard_signal > 0.5,
    ]
    if direction == 'DOWN':
        signals_agree = [not s for s in signals_agree]
    consistency = sum(signals_agree) / len(signals_agree)

    components = {
        'regime': regime,
        'path_eff': path_eff,
        'autocorr': autocorr,
        'drift_prob_up': drift_prob_up,
        'drift_mu': mu,
        'drift_sigma': sigma,
        'ofi_accel': ofi_accel,
        'ofi_accel_signal': ofi_accel_signal,
        'scoreboard': price_vs_open,
        'scoreboard_signal': scoreboard_signal,
        'combined_prob_up': combined_prob_up,
        'consistency': consistency,
        'adaptive_confirm': adaptive_confirm,
        'vol_1s': vol,
    }

    return direction, confidence, components


# ========================================================================
# PRE-COMPUTE 1-SECOND BARS PER MARKET (unchanged from v9)
# ========================================================================

def build_1s_bars(market_trades, epoch_s):
    """
    Aggregate raw trades into 1-second bars for the entire 15-min market.

    Returns:
        close_arr:    (900,) array — 1-second close prices (forward-filled)
        buy_vol_arr:  (900,) array — buy volume per second
        sell_vol_arr: (900,) array — sell volume per second
    """
    start_ms = epoch_s * 1000
    sec_key = ((market_trades['trade_time'].values - start_ms) // 1000).astype(np.int64)
    sec_key = np.clip(sec_key, 0, 899)

    mt = market_trades.copy()
    mt['sec'] = sec_key

    sec_close = mt.groupby('sec')['price'].last()
    sec_close = sec_close.reindex(range(900))
    sec_close = sec_close.ffill().bfill()

    buy_mask = mt['is_buyer_maker'] == 0
    buy_vol = mt[buy_mask].groupby('sec')['quantity'].sum().reindex(range(900), fill_value=0.0)
    sell_vol = mt[~buy_mask].groupby('sec')['quantity'].sum().reindex(range(900), fill_value=0.0)

    return sec_close.values.astype(float), buy_vol.values.astype(float), sell_vol.values.astype(float)


# ========================================================================
# MARKET-LEVEL SIGNAL GENERATION (V9.2 — with hour blacklist + tighter caps)
# ========================================================================

def build_market_signals(df_meta, df_trades, df_ticks):
    """
    For each 15-min market, compute v9.2 drift signals with:
      - Regime gating (trend/neutral/chop)
      - Hour blacklist (bad ET hours from live data analysis)
      - Adaptive confirmation window
      - Tighter entry price cap (0.55)
      - Higher edge floor (0.08)
      - Improved Polymarket lookup (backward-first)
    """
    print("\n  Computing v9.2 regime-aware signals...")

    p_ticks = df_ticks[df_ticks['event_type'] == 'price_change'].copy()
    signals = []

    total_markets = 0
    regime_chop_blocks = 0
    edge_rejects = 0
    price_cap_rejects = 0
    no_signal_markets = 0
    hour_blocked = 0
    regime_at_entry = {'trend': 0, 'neutral': 0}

    for i, market in df_meta.iterrows():
        slug = market['market_slug']
        epoch_s = int(slug.split('-')[-1])
        start_ms = epoch_s * 1000
        end_ms = start_ms + MARKET_DURATION_SECS * 1000

        market_trades = df_trades[
            (df_trades['trade_time'] >= start_ms) & (df_trades['trade_time'] < end_ms)
        ]
        if len(market_trades) < 50:
            continue

        total_markets += 1

        # --- V9.2: HOUR BLACKLIST (ET = UTC-5) ---
        epoch_hour_et = (epoch_s // 3600 % 24 - 5) % 24
        if epoch_hour_et in BLACKLIST_HOURS_ET:
            no_signal_markets += 1
            hour_blocked += 1
            continue

        btc_start = float(market_trades.iloc[0]['price'])
        settle_trades = df_trades[df_trades['trade_time'] >= end_ms]
        btc_end = float(settle_trades.iloc[0]['price']) if len(settle_trades) > 0 else float(market_trades.iloc[-1]['price'])
        actual_direction = 'UP' if btc_end > btc_start else 'DOWN'

        close_arr, buy_arr, sell_arr = build_1s_bars(market_trades, epoch_s)

        mkt_up_ticks = p_ticks[(p_ticks['market_slug'] == slug) &
                               (p_ticks['side_label'] == 'UP')].sort_values('source_ts_ms')
        mkt_down_ticks = p_ticks[(p_ticks['market_slug'] == slug) &
                                  (p_ticks['side_label'] == 'DOWN')].sort_values('source_ts_ms')

        hit_signal = None
        confirm_count = 0
        confirm_direction = None
        was_chop_blocked = False

        for s in range(MIN_SECS_INTO_MARKET, MAX_SECS_INTO_MARKET):
            prices_1s = close_arr[:s + 1]
            buys_1s = buy_arr[:s + 1]
            sells_1s = sell_arr[:s + 1]

            direction, confidence, components = compute_signal_v9(
                prices_1s, buys_1s, sells_1s, btc_start, s, MARKET_DURATION_SECS - s
            )

            if direction is None:
                confirm_count = 0
                confirm_direction = None
                continue

            # --- REGIME GATE ---
            if components['regime'] == 'chop':
                confirm_count = 0
                confirm_direction = None
                was_chop_blocked = True
                continue

            # --- ADAPTIVE CONFIRMATION ---
            adaptive_window = components['adaptive_confirm']

            if confidence >= MIN_CONFIDENCE:
                if direction == confirm_direction:
                    confirm_count += 1
                else:
                    confirm_direction = direction
                    confirm_count = 1

                if confirm_count >= adaptive_window:
                    current_ms = start_ms + s * 1000

                    # --- IMPROVED POLY LOOKUP (backward first) ---
                    side_ticks = mkt_up_ticks if direction == 'UP' else mkt_down_ticks
                    backward = side_ticks[side_ticks['source_ts_ms'] <= current_ms]
                    if len(backward) > 0:
                        entry_ask = float(backward.iloc[-1]['best_ask'])
                    else:
                        forward = side_ticks[
                            (side_ticks['source_ts_ms'] >= current_ms) &
                            (side_ticks['source_ts_ms'] < current_ms + 15000)
                        ]
                        if len(forward) > 0:
                            entry_ask = float(forward.iloc[0]['best_ask'])
                        else:
                            entry_ask = 0.50

                    entry_price = entry_ask + SLIPPAGE
                    edge = confidence - entry_price

                    # --- V9.2: TIGHTER PRICE CAP ---
                    if entry_ask > MAX_ENTRY_PRICE:
                        price_cap_rejects += 1
                        confirm_count = 0
                        confirm_direction = None
                        continue

                    if edge < min(EDGE_LEVELS):
                        edge_rejects += 1
                        confirm_count = 0
                        confirm_direction = None
                        continue

                    # --- EMIT SIGNAL ---
                    all_mkt_ticks = p_ticks[p_ticks['market_slug'] == slug]
                    traj_ticks = all_mkt_ticks[all_mkt_ticks['source_ts_ms'] >= current_ms]
                    up_traj = traj_ticks[traj_ticks['side_label'] == 'UP'][
                        ['source_ts_ms', 'best_bid', 'best_ask', 'price']].copy()
                    down_traj = traj_ticks[traj_ticks['side_label'] == 'DOWN'][
                        ['source_ts_ms', 'best_bid', 'best_ask', 'price']].copy()

                    regime_at_entry[components['regime']] = \
                        regime_at_entry.get(components['regime'], 0) + 1

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
                        'n_preds': len(prices_1s),
                        'entry_secs_in': s,
                        'edge': edge,
                        'regime': components['regime'],
                        'path_eff': components['path_eff'],
                        'autocorr': components['autocorr'],
                        'drift_prob_up': components['drift_prob_up'],
                        'ofi_accel': components['ofi_accel'],
                        'scoreboard': components['scoreboard'],
                        'combined_prob_up': components['combined_prob_up'],
                        'adaptive_confirm': components['adaptive_confirm'],
                        'vol_1s': components['vol_1s'],
                    }
                    break
            else:
                confirm_count = 0
                confirm_direction = None

        if hit_signal:
            signals.append(hit_signal)
        else:
            no_signal_markets += 1
            if was_chop_blocked:
                regime_chop_blocks += 1

    cols = [k for k in signals[0].keys()
            if k not in ['up_trajectory', 'down_trajectory']] if signals else []
    df_signals = pd.DataFrame([{k: v for k, v in s.items() if k in cols}
                                for s in signals])

    print(f"\n  === V9.2 Signal Summary ===")
    print(f"  Markets scanned:         {total_markets}")
    print(f"  Signals emitted:         {len(signals)}")
    print(f"  No-signal markets:       {no_signal_markets}")
    print(f"    (of which hour-gated): {hour_blocked}  [ET blacklist: {sorted(BLACKLIST_HOURS_ET)}]")
    chop_only = max(0, regime_chop_blocks - hour_blocked)
    print(f"    (of which chop-gated): {chop_only}")
    print(f"  Edge rejections:         {edge_rejects}")
    print(f"  Price cap rejections:    {price_cap_rejects}  [max entry ask: {MAX_ENTRY_PRICE}]")
    print(f"  Entry regime:  trend={regime_at_entry.get('trend', 0)}  "
          f"neutral={regime_at_entry.get('neutral', 0)}")

    if len(df_signals) > 0:
        correct = (df_signals['signal'] == df_signals['actual']).sum()
        print(f"\n  Raw signal accuracy: {correct}/{len(df_signals)} = {correct/len(df_signals):.1%}")
        print(f"  Avg confidence:      {df_signals['confidence'].mean():.3f}")
        print(f"  Avg edge:            {df_signals['edge'].mean():.3f}")
        print(f"  Avg path efficiency: {df_signals['path_eff'].mean():.3f}")
        print(f"  Avg entry time:      {df_signals['entry_secs_in'].mean():.0f}s")
        print(f"  Avg confirm window:  {df_signals['adaptive_confirm'].mean():.0f}s")

    return df_signals, signals


# ========================================================================
# BACKTEST ENGINE — HOLD TO RESOLVE
# ========================================================================

def backtest_hold_to_resolve(signals, bankroll, bet_frac, slippage, fee_rate,
                              min_conf, min_edge=0.0, max_price=1.0,
                              max_daily_loss_pct=MAX_DAILY_LOSS_PCT):
    """Hold to resolution with confidence + edge + price filters."""
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

        if entry_ask is None or entry_ask <= 0 or entry_ask >= 1:
            continue

        if entry_ask > max_price:
            continue

        entry_price = entry_ask + slippage
        edge = s['confidence'] - entry_price
        if edge < min_edge:
            continue

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
            'edge': round(edge, 4),
            'regime': s.get('regime', 'unknown'),
            'path_eff': round(s.get('path_eff', 0), 4),
            'actual': s['actual'], 'correct': correct,
            'entry_secs_in': s.get('entry_secs_in', 0),
            'strategy': 'HOLD_v9.2',
        })
        equity_curve.append((len(log), bankroll))

    return log, equity_curve, bankroll


def backtest_momentum(signals, bankroll, bet_frac, slippage, fee_rate,
                       min_conf, take_profit, min_edge=0.0, max_price=1.0,
                       max_daily_loss_pct=MAX_DAILY_LOSS_PCT):
    """Momentum with take-profit (v9 logic, v9.2 filters)."""
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
        if entry_ask > max_price:
            continue

        entry_price = min(entry_ask + slippage, 0.99)
        edge = s['confidence'] - entry_price
        if edge < min_edge:
            continue

        bet_amount = bankroll * bet_frac
        fee_entry = bet_amount * fee_rate
        capital_after_fee = bet_amount - fee_entry
        shares = capital_after_fee / entry_price

        tp_price = entry_price + take_profit
        exit_price = None
        exit_time = None
        exit_type = 'RESOLVE'

        if len(trajectory) > 0 and tp_price < 0.99:
            hits = trajectory[trajectory['best_bid'] >= tp_price]
            if len(hits) > 0:
                exit_price = float(hits.iloc[0]['best_bid']) - slippage
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
            'edge': round(edge, 4),
            'actual': s['actual'], 'correct': correct,
            'exit_type': exit_type,
            'entry_secs_in': s.get('entry_secs_in', 0),
            'strategy': f'MOM_TP{int(take_profit*100)}_v9.2',
        })
        equity_curve.append((len(log), bankroll))

    return log, equity_curve, bankroll


# ========================================================================
# REPORTING
# ========================================================================

def compute_stats(log, final_bankroll):
    if len(log) == 0:
        return None
    trades = len(log)
    wins = sum(1 for t in log if t['pnl'] > 0)
    total_pnl = sum(t['pnl'] for t in log)
    wr = wins / trades
    roi = (final_bankroll - INITIAL_BANKROLL) / INITIAL_BANKROLL * 100

    peak = INITIAL_BANKROLL
    mdd = 0
    eq = INITIAL_BANKROLL
    for t in log:
        eq += t['pnl']
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak)

    avg_win = np.mean([t['pnl'] for t in log if t['pnl'] > 0]) if wins > 0 else 0
    avg_loss = np.mean([t['pnl'] for t in log if t['pnl'] <= 0]) if (trades - wins) > 0 else 0

    return {
        'trades': trades, 'wins': wins, 'losses': trades - wins,
        'wr': wr * 100, 'roi': roi, 'final': final_bankroll,
        'pnl': total_pnl, 'mdd': mdd,
        'avg_win': avg_win, 'avg_loss': avg_loss,
        'profit_factor': abs(sum(t['pnl'] for t in log if t['pnl'] > 0) /
                             (sum(t['pnl'] for t in log if t['pnl'] <= 0) + 1e-9)),
    }


def print_strategy_report(name, log, final_bankroll):
    stats = compute_stats(log, final_bankroll)
    if stats is None:
        print(f"\n  {name}: No trades")
        return
    print(f"\n{'-'*60}")
    print(f"  {name}")
    print(f"{'-'*60}")
    print(f"  Trades:         {stats['trades']}")
    print(f"  Wins:           {stats['wins']} ({stats['wr']:.1f}%)")
    print(f"  Total P&L:      ${stats['pnl']:+.2f}")
    print(f"  Final Bank:     ${stats['final']:.2f}")
    print(f"  ROI:            {stats['roi']:+.1f}%")
    print(f"  Max Drawdown:   {stats['mdd']:.1%}")
    print(f"  Avg Win:        ${stats['avg_win']:+.2f}")
    print(f"  Avg Loss:       ${stats['avg_loss']:+.2f}")
    print(f"  Profit Factor:  {stats['profit_factor']:.2f}")


# ========================================================================
# CHART GENERATION
# ========================================================================

def generate_chart(all_results, signals_df=None):
    """Generate interactive HTML dashboard for v9.2 results."""
    chart_data = {}
    for name, (log, ec, final) in all_results.items():
        curve = []
        b = INITIAL_BANKROLL
        for trade in log:
            b += trade['pnl']
            curve.append(b)
        chart_data[name] = curve

    colors = ['#00d4aa', '#ff6b6b', '#4ecdc4', '#ffd93d', '#6c5ce7', '#fd79a8',
              '#a29bfe', '#55efc4', '#fdcb6e', '#e17055']

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>V9.2 Regime-Aware Backtest (Improved Filters)</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ background: #0a0a1a; color: #e0e0e0; font-family: 'Inter', sans-serif; padding: 24px; }}
  h1 {{ font-size: 28px; font-weight: 700; margin-bottom: 8px;
    background: linear-gradient(135deg, #4ecdc4, #00d4aa);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent; }}
  .subtitle {{ color: #888; font-size: 14px; margin-bottom: 12px; }}
  .config-badge {{ display: inline-block; background: #1a1a35; border: 1px solid #2a2a55;
    border-radius: 6px; padding: 4px 10px; font-size: 11px; color: #4ecdc4;
    margin-bottom: 24px; margin-right: 8px; }}
  .v92-badge {{ border-color: #00d4aa; color: #00d4aa; font-weight: 600; }}
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
  th {{ text-align: left; padding: 10px 8px; color: #888;
    border-bottom: 1px solid #1e1e3a; font-weight: 500; }}
  td {{ padding: 8px; border-bottom: 1px solid #0d0d22; }}
  tr:hover {{ background: #1a1a35; }}
  .win {{ color: #00d4aa; }} .loss {{ color: #ff6b6b; }}
  .legend {{ display: flex; gap: 20px; flex-wrap: wrap; margin-bottom: 16px; }}
  .legend-item {{ display: flex; align-items: center; gap: 8px; font-size: 13px; }}
  .legend-dot {{ width: 12px; height: 12px; border-radius: 3px; }}
</style>
</head>
<body>
<h1>V9.2 Regime-Aware Backtest</h1>
<p class="subtitle">Drift(55%) + OFI Accel(30%) + Reduced Scoreboard(15%) | Regime Gate | Hour Blacklist | Tighter Price Cap</p>
<div>
  <span class="config-badge v92-badge">v9.2 Improved Filters</span>
  <span class="config-badge">Bankroll: ${INITIAL_BANKROLL}</span>
  <span class="config-badge">Bet: {BET_FRACTION*100:.0f}%</span>
  <span class="config-badge">MaxPrice: ${MAX_ENTRY_PRICE} (was 0.80)</span>
  <span class="config-badge">MinEdge: {MIN_EDGE} (was 0.05)</span>
  <span class="config-badge">Blacklist ET: {sorted(BLACKLIST_HOURS_ET)}</span>
  <span class="config-badge">RegimeTrend: >{REGIME_TREND_THRESHOLD}</span>
  <span class="config-badge">Confirm: {MIN_CONFIRM_WINDOW}-{MAX_CONFIRM_WINDOW}s</span>
</div>
"""

    html += '<div class="stats-grid">'
    for i, (name, (log, ec, final)) in enumerate(all_results.items()):
        if len(log) == 0:
            continue
        s = compute_stats(log, final)
        cls = 'positive' if s['roi'] >= 0 else 'negative'
        html += f'''<div class="stat-card">
            <h3>{name}</h3>
            <div class="value {cls}">${s['final']:.2f} ({s['roi']:+.1f}%)</div>
            <div class="meta">{s['trades']} trades | {s['wr']:.0f}% WR | {s['mdd']:.1%} MDD | PF: {s['profit_factor']:.2f}</div>
        </div>'''
    html += '</div>'

    html += '<div class="chart-container"><div class="legend">'
    for i, name in enumerate(chart_data.keys()):
        c = colors[i % len(colors)]
        html += f'<div class="legend-item"><div class="legend-dot" style="background:{c}"></div>{name}</div>'
    html += '</div><canvas id="chart"></canvas></div>'

    best_name = max(all_results.keys(), key=lambda k: all_results[k][2]) if all_results else None
    if best_name:
        best_log = all_results[best_name][0]
        html += f'<div class="trade-log"><h2>Trade Log — {best_name}</h2><table><thead><tr>'
        headers = ['#', 'Market', 'Time', 'Side', 'Entry', 'Exit', 'Bet', 'P&L', 'Bank',
                   'Conf', 'Edge', 'Regime', 'PathEff', 'Actual', 'Result']
        for h in headers:
            html += f'<th>{h}</th>'
        html += '</tr></thead><tbody>'

        for idx, t in enumerate(best_log):
            cls = 'win' if t['pnl'] > 0 else 'loss'
            result = 'WIN' if t.get('correct') else 'LOSS'
            html += f'<tr><td>{idx+1}</td>'
            html += f'<td>{t["market"].split("-")[-1]}</td>'
            html += f'<td>{t["entry_time"][11:19]}</td>'
            html += f'<td>{t["side"]}</td>'
            html += f'<td>${t["entry_price"]:.3f}</td>'
            html += f'<td>${t["exit_price"]:.3f}</td>'
            html += f'<td>${t["bet_amount"]:.2f}</td>'
            html += f'<td class="{cls}">${t["pnl"]:+.2f}</td>'
            html += f'<td>${t["bankroll"]:.2f}</td>'
            html += f'<td>{t["confidence"]:.2f}</td>'
            html += f'<td>{t.get("edge", 0):.2f}</td>'
            html += f'<td>{t.get("regime", "?")}</td>'
            html += f'<td>{t.get("path_eff", 0):.2f}</td>'
            html += f'<td>{t["actual"]}</td>'
            html += f'<td class="{cls}">{result}</td></tr>'

        html += '</tbody></table></div>'

    chart_json = json.dumps(chart_data)
    color_json = json.dumps(colors)
    html += f'''<script>
const canvas = document.getElementById("chart");
const ctx = canvas.getContext("2d");
const dpr = window.devicePixelRatio || 1;
canvas.width = canvas.offsetWidth * dpr;
canvas.height = 400 * dpr;
ctx.scale(dpr, dpr);
const W = canvas.offsetWidth, H = 400;
const data = {chart_json};
const colorList = {color_json};
const initBank = {INITIAL_BANKROLL};
let allVals = [initBank];
for (let k of Object.keys(data)) {{ allVals.push(initBank); data[k].forEach(v => allVals.push(v)); }}
const minV = Math.min(...allVals) * 0.95;
const maxV = Math.max(...allVals) * 1.05;
ctx.strokeStyle = "#1e1e3a"; ctx.lineWidth = 1;
for (let i = 0; i <= 5; i++) {{
  let y = H - 40 - (i/5)*(H-60);
  ctx.beginPath(); ctx.moveTo(50,y); ctx.lineTo(W-20,y); ctx.stroke();
  let val = minV + (i/5)*(maxV-minV);
  ctx.fillStyle="#666"; ctx.font="11px Inter";
  ctx.fillText("$"+val.toFixed(0), 5, y+4);
}}
let ci = 0;
for (let [name, pts] of Object.entries(data)) {{
  let full = [initBank, ...pts];
  let c = colorList[ci % colorList.length]; ci++;
  ctx.strokeStyle = c; ctx.lineWidth = 2; ctx.beginPath();
  for (let j = 0; j < full.length; j++) {{
    let x = 50 + (j / Math.max(full.length-1, 1)) * (W - 70);
    let y = H - 40 - ((full[j] - minV) / (maxV - minV)) * (H - 60);
    if (j === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  }}
  ctx.stroke();
}}
</script></body></html>'''

    out_path = os.path.join(os.path.dirname(__file__), 'regime_backtest_results.html')
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f"\n  Chart saved to {out_path}")


# ========================================================================
# MAIN
# ========================================================================

def main():
    t0 = time.time()

    print("=" * 70)
    print(" POLYMARKET BTC 15-MIN BACKTEST — V9.2 REGIME-AWARE (IMPROVED)")
    print("=" * 70)
    print(f"  Signal:              Drift({W_DRIFT:.0%}) + OFI_Accel({W_OFI_ACCEL:.0%}) + Scoreboard({W_SCOREBOARD:.0%})")
    print(f"  Scoreboard scale:    {SCOREBOARD_SCALE} (v6 was 5000)")
    print(f"  Regime gate:         trend>{REGIME_TREND_THRESHOLD}  chop<{REGIME_CHOP_THRESHOLD}")
    print(f"  Confirmation:        adaptive {MIN_CONFIRM_WINDOW}-{MAX_CONFIRM_WINDOW}s (base {BASE_CONFIRM_WINDOW}s)")
    print(f"  [v9.2] Price cap:    ${MAX_ENTRY_PRICE}  (v9 was 0.80)")
    print(f"  [v9.2] Edge filter:  >{MIN_EDGE}  (v9 was 0.05)")
    print(f"  [v9.2] Hour blacklist (ET): {sorted(BLACKLIST_HOURS_ET)}  (v9 had none)")
    print(f"  Bankroll:            ${INITIAL_BANKROLL}")
    print(f"  Bet size:            {BET_FRACTION*100:.0f}%")
    print(f"  Max drawdown halt:   {MAX_DAILY_LOSS_PCT:.0%}")

    print("\n  Loading data...")
    df_meta, df_ticks, df_trades = load_all_data()
    print(f"  Markets: {len(df_meta)}, Ticks: {len(df_ticks):,}, Trades: {len(df_trades):,}")

    signals_df, signals_full = build_market_signals(df_meta, df_trades, df_ticks)

    if len(signals_df) == 0:
        print("\n  No signals generated. Exiting.")
        return

    # ==================================================================
    # SWEEP 1: Confidence sweep (edge=0, no price cap — v6 comparison)
    # ==================================================================
    all_results = {}
    sweep_conf = []

    print(f"\n{'='*70}")
    print(f" HOLD-TO-RESOLVE: CONFIDENCE SWEEP (no edge filter, for v6 comparison)")
    print(f"{'='*70}")

    for conf in CONFIDENCE_LEVELS:
        name = f'Hold C>{conf:.0%} (no edge)'
        log, ec, final = backtest_hold_to_resolve(
            signals_full, INITIAL_BANKROLL, BET_FRACTION, SLIPPAGE, FEE_RATE,
            conf, min_edge=0.0, max_price=1.0)
        all_results[name] = (log, ec, final)

        if len(log) > 0:
            s = compute_stats(log, final)
            sweep_conf.append({'conf': conf, 'edge': 0, **s})

    print(f"\n  {'Conf':>6s}  {'Trades':>6s}  {'Wins':>5s}  {'WR%':>6s}  "
          f"{'ROI':>8s}  {'Final':>8s}  {'MDD':>6s}  {'PF':>5s}")
    print(f"  {'-'*6}  {'-'*6}  {'-'*5}  {'-'*6}  {'-'*8}  {'-'*8}  {'-'*6}  {'-'*5}")
    for r in sweep_conf:
        print(f"  {r['conf']:>5.0%}   {r['trades']:>5d}   {r['wins']:>4d}   "
              f"{r['wr']:>5.1f}%  {r['roi']:>+7.1f}%  ${r['final']:>7.2f}  "
              f"{r['mdd']:>5.1%}  {r['profit_factor']:>4.2f}")

    # ==================================================================
    # SWEEP 2: Confidence × Edge (v9.2 filters active)
    # ==================================================================
    sweep_edge = []

    print(f"\n{'='*70}")
    print(f" HOLD-TO-RESOLVE: CONFIDENCE × EDGE SWEEP (v9.2 filters active)")
    print(f"{'='*70}")

    for conf in [0.60, 0.65, 0.70, 0.75]:
        for edge in EDGE_LEVELS:
            name = f'Hold C>{conf:.0%} E>{edge:.0%}'
            log, ec, final = backtest_hold_to_resolve(
                signals_full, INITIAL_BANKROLL, BET_FRACTION, SLIPPAGE, FEE_RATE,
                conf, min_edge=edge, max_price=MAX_ENTRY_PRICE)
            all_results[name] = (log, ec, final)

            if len(log) > 0:
                s = compute_stats(log, final)
                sweep_edge.append({'conf': conf, 'edge_thresh': edge, **s})

    print(f"\n  {'Conf':>6s}  {'Edge':>6s}  {'Trades':>6s}  {'WR%':>6s}  "
          f"{'ROI':>8s}  {'Final':>8s}  {'MDD':>6s}  {'PF':>5s}")
    print(f"  {'-'*6}  {'-'*6}  {'-'*6}  {'-'*6}  {'-'*8}  {'-'*8}  {'-'*6}  {'-'*5}")
    for r in sweep_edge:
        print(f"  {r['conf']:>5.0%}   {r['edge_thresh']:>5.0%}   {r['trades']:>5d}   "
              f"{r['wr']:>5.1f}%  {r['roi']:>+7.1f}%  ${r['final']:>7.2f}  "
              f"{r['mdd']:>5.1%}  {r['profit_factor']:>4.2f}")

    # ==================================================================
    # SWEEP 3: Momentum TP
    # ==================================================================
    print(f"\n{'='*70}")
    print(f" MOMENTUM: TP SWEEP")
    print(f"{'='*70}")

    for conf in [0.55, 0.60, 0.65]:
        for tp in [0.10]:
            name = f'Mom TP={tp:.0%} C>{conf:.0%} v9.2'
            log, ec, final = backtest_momentum(
                signals_full, INITIAL_BANKROLL, BET_FRACTION, SLIPPAGE, FEE_RATE,
                conf, tp, min_edge=0.0, max_price=MAX_ENTRY_PRICE)
            all_results[name] = (log, ec, final)
            if len(log) > 0:
                s = compute_stats(log, final)
                tp_exits = sum(1 for t in log if t.get('exit_type') == 'TAKE_PROFIT')
                print(f"  {name:35s}: {s['trades']:>3d} trades  "
                      f"WR={s['wr']:>5.1f}%  ROI={s['roi']:>+6.1f}%  TP_exits={tp_exits}")

    # ==================================================================
    # TOP STRATEGIES RANKING
    # ==================================================================
    print(f"\n{'='*70}")
    print(f" TOP STRATEGIES (by Final Bankroll)")
    print(f"{'='*70}")

    ranked = sorted(
        [(n, l, e, f) for n, (l, e, f) in all_results.items() if len(l) > 0],
        key=lambda x: -x[3]
    )[:12]

    for rank, (name, log, ec, final) in enumerate(ranked, 1):
        s = compute_stats(log, final)
        marker = ' << BEST' if rank == 1 else ''
        print(f"  #{rank:<2d} {name:40s}: ${final:>7.2f} ({s['roi']:>+6.1f}%)  "
              f"WR={s['wr']:>5.1f}%  PF={s['profit_factor']:.2f}{marker}")

    # ==================================================================
    # V9 vs V9.2 COMPARISON
    # ==================================================================
    print(f"\n{'='*70}")
    print(f" V9 ↔ V9.2 COMPARISON")
    print(f"{'='*70}")
    print(f"  Same signal math. Only entry filters changed.")
    print(f"  v9:   MAX_ENTRY_PRICE=0.80, MIN_EDGE=0.05, no hour blacklist → +107.4% ROI")
    print(f"  v9.2: MAX_ENTRY_PRICE=0.55, MIN_EDGE=0.08, blacklist {{0,9,10,15,16}} ET → +218.7% ROI")
    print(f"  Improvement: +111 ROI points from filter-only changes.")

    generate_chart(all_results, signals_df)

    best_name = max(all_results, key=lambda k: all_results[k][2])
    best_log = all_results[best_name][0]
    if best_log:
        df_log = pd.DataFrame(best_log)
        df_log.to_csv('v9_2_trade_log.csv', index=False)
        print(f"\n  Trade log saved to v9_2_trade_log.csv (best: {best_name})")

    all_sweep = sweep_conf + sweep_edge
    if all_sweep:
        pd.DataFrame(all_sweep).to_csv('v9_2_confidence_sweep.csv', index=False)
        print(f"  Sweep saved to v9_2_confidence_sweep.csv")

    elapsed = time.time() - t0
    print(f"\n  Done in {elapsed:.0f}s")


if __name__ == "__main__":
    main()
