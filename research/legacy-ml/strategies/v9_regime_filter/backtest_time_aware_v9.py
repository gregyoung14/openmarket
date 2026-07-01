"""
POLYMARKET BTC 15-MIN BACKTESTER — V9 TIME-AWARE
=================================================
Extends v9 regime-aware with TIME-OF-DAY & LIQUIDITY GATING.

LIVE OBSERVATION (Feb 16, 2026):
    4:15 PM – 8:15 PM EST  →  68.8% WR  →  +$5.47  (trending, liquid)
    8:15 PM – 11:15 PM EST →  40.0% WR  →  -$6.75  (choppy, thin)

THE FIX: Three-tier liquidity regime system:
    TIER 1 – PRIME  (9 AM – 7 PM EST):  Full signals, 5% bet
    TIER 2 – EXTENDED (7 PM – 9 PM EST): Raised conf (+5%), 3% bet
    TIER 3 – THIN   (9 PM – 9 AM EST):  Very high conf (+10%), 2% bet, or skip

Additionally measures real-time liquidity from Binance trade intensity to
adapt thresholds dynamically — handles holidays, weekends, and news events
where the clock-based tiers may not match actual conditions.

KEY ANALYSIS OUTPUT:
    • Hour-by-hour accuracy & P&L breakdown (EST)
    • Tier 1/2/3 performance comparison
    • "Prime only" vs "All hours" equity curves
    • Binance liquidity correlation with signal accuracy
"""

import sqlite3
import pandas as pd
import numpy as np
from scipy.stats import norm
import warnings
warnings.filterwarnings('ignore')
import time
import json
from datetime import datetime, timezone, timedelta

DB_PATH = 'polymarket_btc_data.db'
EST_OFFSET_HOURS = -5  # EST = UTC-5

# ========================================================================
# CONFIG (inherits v9 core, adds time/liquidity layer)
# ========================================================================
INITIAL_BANKROLL    = 100.0
SLIPPAGE            = 0.005
FEE_RATE            = 0.01

# --- Signal Architecture (v9 core — unchanged) ---
W_DRIFT             = 0.55
W_OFI_ACCEL         = 0.30
W_SCOREBOARD        = 0.15
SCOREBOARD_SCALE    = 1000
OFI_SCALE           = 3

# --- Regime Detection (v9 core — unchanged) ---
REGIME_TREND_THRESHOLD = 0.15
REGIME_CHOP_THRESHOLD  = 0.06
REGIME_AUTOCORR_CHOP   = -0.25
REGIME_LOOKBACK        = 60
NEUTRAL_CONF_PENALTY   = 0.02

# --- Timing (v9 core — unchanged) ---
MIN_SECS_INTO_MARKET = 60
MAX_SECS_INTO_MARKET = 600
MARKET_DURATION_SECS = 900

# --- Adaptive Confirmation (v9 core — unchanged) ---
BASE_CONFIRM_WINDOW  = 30
MIN_CONFIRM_WINDOW   = 15
MAX_CONFIRM_WINDOW   = 50

# --- Entry Filters (v9 core) ---
MIN_CONFIDENCE       = 0.55
MAX_ENTRY_PRICE      = 0.80
MIN_EDGE             = 0.05

# --- Risk ---
MAX_DAILY_LOSS_PCT   = 0.20

# ========================================================================
# TIME-OF-DAY TIERS (NEW)
# ========================================================================
# Tier 1: PRIME — US market hours, peak liquidity
TIER1_START_EST = 9    # 9 AM EST
TIER1_END_EST   = 19   # 7 PM EST (19:00)

# Tier 2: EXTENDED — tail end of US session, decent but thinning
TIER2_END_EST   = 21   # 9 PM EST (21:00)

# Tier 3: THIN — everything else (9 PM – 9 AM EST)
# Automatically anything outside Tier 1/2

# --- Per-Tier Config ---
TIER_CONFIG = {
    1: {  # PRIME
        'name': 'PRIME (9AM-7PM)',
        'bet_frac': 0.05,          # Full bet
        'conf_boost': 0.00,        # No boost needed
        'min_conf': 0.60,          # Standard threshold
        'enabled': True,
    },
    2: {  # EXTENDED
        'name': 'EXTENDED (7PM-9PM)',
        'bet_frac': 0.03,          # Reduced bet
        'conf_boost': 0.05,        # Need 5% more confidence
        'min_conf': 0.65,          # Raised floor
        'enabled': True,
    },
    3: {  # THIN
        'name': 'THIN (9PM-9AM)',
        'bet_frac': 0.02,          # Minimum bet
        'conf_boost': 0.10,        # Need 10% more confidence
        'min_conf': 0.70,          # High floor
        'enabled': True,           # Set False to skip entirely
    },
}

# --- Liquidity Measurement ---
# If Binance trade intensity (trades/sec) is below this percentile of the
# session average, override to next-worse tier even during prime hours.
LIQUIDITY_PERCENTILE_DOWNGRADE = 25  # Bottom 25% of session intensity → downgrade tier

# --- Sweeps ---
CONFIDENCE_LEVELS    = [0.55, 0.60, 0.65, 0.70, 0.75, 0.80]

# ========================================================================
# DATA LOADING
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
# TIME TIER CLASSIFICATION
# ========================================================================

def get_est_hour(epoch_ms):
    """Convert epoch milliseconds to EST hour (0-23)."""
    dt_utc = datetime.fromtimestamp(epoch_ms / 1000.0, tz=timezone.utc)
    dt_est = dt_utc + timedelta(hours=EST_OFFSET_HOURS)
    return dt_est.hour

def classify_tier(hour_est):
    """Classify an EST hour into time tier 1/2/3."""
    if TIER1_START_EST <= hour_est < TIER1_END_EST:
        return 1
    elif TIER1_END_EST <= hour_est < TIER2_END_EST:
        return 2
    else:
        return 3

def get_tier_label(tier):
    return TIER_CONFIG[tier]['name']

# ========================================================================
# LIQUIDITY MEASUREMENT
# ========================================================================

def compute_session_liquidity(df_trades, df_meta):
    """
    Compute per-market Binance trade intensity (trades/second) and the
    session-wide distribution for dynamic tier adjustment.

    Returns:
        dict: market_slug → {'trades_per_sec': float, 'total_volume': float}
        float: session median trades_per_sec (for percentile comparison)
    """
    liq = {}
    intensities = []

    for _, market in df_meta.iterrows():
        slug = market['market_slug']
        epoch_s = int(slug.split('-')[-1])
        start_ms = epoch_s * 1000
        end_ms = start_ms + MARKET_DURATION_SECS * 1000

        mask = (df_trades['trade_time'] >= start_ms) & (df_trades['trade_time'] < end_ms)
        count = mask.sum()
        tps = count / MARKET_DURATION_SECS
        vol = df_trades.loc[mask, 'quantity'].sum() if count > 0 else 0.0

        liq[slug] = {'trades_per_sec': tps, 'total_volume': float(vol)}
        intensities.append(tps)

    intensities = np.array(intensities)
    threshold = np.percentile(intensities, LIQUIDITY_PERCENTILE_DOWNGRADE) if len(intensities) > 0 else 0
    median_tps = float(np.median(intensities)) if len(intensities) > 0 else 0

    return liq, threshold, median_tps

# ========================================================================
# REGIME DETECTION (from v9 — unchanged)
# ========================================================================

def detect_regime(close_1s, lookback=REGIME_LOOKBACK):
    n = len(close_1s)
    recent = close_1s[-min(n, lookback):]
    valid = recent[~np.isnan(recent) & (recent > 0)]
    if len(valid) < 15:
        return 'neutral', 0.0, 0.0

    direct = abs(float(valid[-1]) - float(valid[0]))
    total_path = np.sum(np.abs(np.diff(valid)))
    path_eff = direct / (total_path + 1e-12)

    returns = np.diff(np.log(valid.astype(float) + 1e-9))
    if len(returns) > 5:
        autocorr = float(np.corrcoef(returns[:-1], returns[1:])[0, 1])
        if np.isnan(autocorr):
            autocorr = 0.0
    else:
        autocorr = 0.0

    if autocorr < REGIME_AUTOCORR_CHOP:
        return 'chop', path_eff, autocorr
    if path_eff >= REGIME_TREND_THRESHOLD and autocorr > -0.10:
        return 'trend', path_eff, autocorr
    elif path_eff < REGIME_CHOP_THRESHOLD:
        return 'chop', path_eff, autocorr
    else:
        return 'neutral', path_eff, autocorr

# ========================================================================
# V9 SIGNAL GENERATOR (from v9 — unchanged)
# ========================================================================

def compute_signal_v9(close_1s, buy_vol_1s, sell_vol_1s, open_price,
                      entry_secs, remaining_secs):
    n = len(close_1s)
    valid_mask = ~np.isnan(close_1s) & (close_1s > 0)
    valid_prices = close_1s[valid_mask]
    if len(valid_prices) < 15:
        return None, None, None

    current_price = float(valid_prices[-1])
    regime, path_eff, autocorr = detect_regime(close_1s)

    log_returns = np.diff(np.log(valid_prices.astype(float) + 1e-9))
    if len(log_returns) < 5:
        return None, None, None

    mu = float(np.mean(log_returns))
    sigma = float(np.std(log_returns))

    if sigma > 0 and remaining_secs > 0:
        z = mu * np.sqrt(remaining_secs) / sigma
        drift_prob_up = float(norm.cdf(z))
    else:
        drift_prob_up = 0.5

    half = max(n // 2, 5)
    buy_recent = float(buy_vol_1s[-half:].sum())
    sell_recent = float(sell_vol_1s[-half:].sum())
    buy_earlier = float(buy_vol_1s[:half].sum())
    sell_earlier = float(sell_vol_1s[:half].sum())

    ofi_recent = (buy_recent - sell_recent) / (buy_recent + sell_recent + 1e-9)
    ofi_earlier = (buy_earlier - sell_earlier) / (buy_earlier + sell_earlier + 1e-9)
    ofi_accel = ofi_recent - ofi_earlier
    ofi_accel_signal = 1.0 / (1.0 + np.exp(-ofi_accel * OFI_SCALE))

    price_vs_open = (current_price - open_price) / (open_price + 1e-9)
    scoreboard_signal = 1.0 / (1.0 + np.exp(-price_vs_open * SCOREBOARD_SCALE))

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

    if regime == 'neutral':
        confidence -= NEUTRAL_CONF_PENALTY

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
        'regime': regime, 'path_eff': path_eff, 'autocorr': autocorr,
        'drift_prob_up': drift_prob_up, 'drift_mu': mu, 'drift_sigma': sigma,
        'ofi_accel': ofi_accel, 'ofi_accel_signal': ofi_accel_signal,
        'scoreboard': price_vs_open, 'scoreboard_signal': scoreboard_signal,
        'combined_prob_up': combined_prob_up, 'consistency': consistency,
        'adaptive_confirm': adaptive_confirm, 'vol_1s': vol,
    }

    return direction, confidence, components

# ========================================================================
# PRE-COMPUTE 1-SECOND BARS (from v9 — unchanged)
# ========================================================================

def build_1s_bars(market_trades, epoch_s):
    start_ms = epoch_s * 1000
    sec_key = ((market_trades['trade_time'].values - start_ms) // 1000).astype(np.int64)
    sec_key = np.clip(sec_key, 0, 899)
    mt = market_trades.copy()
    mt['sec'] = sec_key

    sec_close = mt.groupby('sec')['price'].last()
    sec_close = sec_close.reindex(range(900)).ffill().bfill()

    buy_mask = mt['is_buyer_maker'] == 0
    buy_vol = mt[buy_mask].groupby('sec')['quantity'].sum().reindex(range(900), fill_value=0.0)
    sell_vol = mt[~buy_mask].groupby('sec')['quantity'].sum().reindex(range(900), fill_value=0.0)

    return sec_close.values.astype(float), buy_vol.values.astype(float), sell_vol.values.astype(float)

# ========================================================================
# MARKET-LEVEL SIGNAL GENERATION (V9 + Time/Liquidity Tier)
# ========================================================================

def build_market_signals(df_meta, df_trades, df_ticks, liquidity_map, liq_threshold):
    """
    For each 15-min market, compute v9 drift signals with regime gating,
    adaptive confirmation, and time/liquidity tier classification.
    """
    print("\n  Computing v9 time-aware signals...")

    p_ticks = df_ticks[df_ticks['event_type'] == 'price_change'].copy()
    signals = []

    # Counters
    total_markets = 0
    regime_chop_blocks = 0
    tier_blocks = 0
    edge_rejects = 0
    price_cap_rejects = 0
    no_signal_markets = 0
    tier_counts = {1: 0, 2: 0, 3: 0}
    regime_at_entry = {'trend': 0, 'neutral': 0}

    for _, market in df_meta.iterrows():
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

        # --- TIME TIER ---
        hour_est = get_est_hour(start_ms)
        base_tier = classify_tier(hour_est)

        # --- LIQUIDITY OVERRIDE ---
        # If Binance intensity is below 25th percentile, downgrade tier
        mkt_liq = liquidity_map.get(slug, {})
        tps = mkt_liq.get('trades_per_sec', 0)
        if tps < liq_threshold and base_tier < 3:
            effective_tier = min(base_tier + 1, 3)
        else:
            effective_tier = base_tier

        tier_cfg = TIER_CONFIG[effective_tier]

        # --- CHECK IF TIER IS ENABLED ---
        if not tier_cfg['enabled']:
            tier_blocks += 1
            no_signal_markets += 1
            continue

        # --- GET TIER-ADJUSTED THRESHOLDS ---
        tier_min_conf = tier_cfg['min_conf']
        tier_conf_boost = tier_cfg['conf_boost']
        tier_bet_frac = tier_cfg['bet_frac']

        btc_start = float(market_trades.iloc[0]['price'])
        btc_end = float(market_trades.iloc[-1]['price'])
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

            # Apply tier confidence boost (raising the effective bar)
            effective_confidence = confidence - tier_conf_boost

            if effective_confidence >= tier_min_conf:
                if direction == confirm_direction:
                    confirm_count += 1
                else:
                    confirm_direction = direction
                    confirm_count = 1

                if confirm_count >= adaptive_window:
                    current_ms = start_ms + s * 1000

                    # --- POLY LOOKUP ---
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

                    if entry_ask > MAX_ENTRY_PRICE:
                        price_cap_rejects += 1
                        confirm_count = 0
                        confirm_direction = None
                        continue

                    if edge < MIN_EDGE:
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
                    tier_counts[effective_tier] += 1

                    hit_signal = {
                        'slug': slug,
                        'start_ms': current_ms,
                        'end_ms': end_ms,
                        'btc_start': btc_start, 'btc_end': btc_end,
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
                        # V9 diagnostics
                        'regime': components['regime'],
                        'path_eff': components['path_eff'],
                        'autocorr': components['autocorr'],
                        'drift_prob_up': components['drift_prob_up'],
                        'ofi_accel': components['ofi_accel'],
                        'scoreboard': components['scoreboard'],
                        'combined_prob_up': components['combined_prob_up'],
                        'adaptive_confirm': components['adaptive_confirm'],
                        'vol_1s': components['vol_1s'],
                        # TIME-AWARE fields
                        'hour_est': hour_est,
                        'base_tier': base_tier,
                        'effective_tier': effective_tier,
                        'tier_name': tier_cfg['name'],
                        'tier_bet_frac': tier_bet_frac,
                        'trades_per_sec': tps,
                        'liq_downgraded': effective_tier != base_tier,
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

    # --- Summary ---
    cols = [k for k in signals[0].keys()
            if k not in ['up_trajectory', 'down_trajectory']] if signals else []
    df_signals = pd.DataFrame([{k: v for k, v in s.items() if k in cols}
                                for s in signals])

    print(f"\n  === V9 Time-Aware Signal Summary ===")
    print(f"  Markets scanned:         {total_markets}")
    print(f"  Signals emitted:         {len(signals)}")
    print(f"  No-signal markets:       {no_signal_markets}")
    print(f"    (chop-gated):          {regime_chop_blocks}")
    print(f"    (tier-disabled):       {tier_blocks}")
    print(f"  Edge rejections:         {edge_rejects}")
    print(f"  Price cap rejections:    {price_cap_rejects}")
    print(f"  Tier distribution:       T1={tier_counts[1]}  T2={tier_counts[2]}  T3={tier_counts[3]}")
    print(f"  Regime at entry:         trend={regime_at_entry.get('trend',0)}  "
          f"neutral={regime_at_entry.get('neutral',0)}")

    if len(df_signals) > 0:
        correct = (df_signals['signal'] == df_signals['actual']).sum()
        print(f"\n  Raw signal accuracy:     {correct}/{len(df_signals)} = {correct/len(df_signals):.1%}")
        print(f"  Avg confidence:          {df_signals['confidence'].mean():.3f}")
        print(f"  Avg edge:                {df_signals['edge'].mean():.3f}")
        print(f"  Avg entry time:          {df_signals['entry_secs_in'].mean():.0f}s")

        # Per-tier accuracy
        for t in [1, 2, 3]:
            tier_mask = df_signals['effective_tier'] == t
            if tier_mask.sum() > 0:
                tc = (df_signals[tier_mask]['signal'] == df_signals[tier_mask]['actual']).sum()
                tn = tier_mask.sum()
                print(f"  Tier {t} accuracy:         {tc}/{tn} = {tc/tn:.1%}  "
                      f"({TIER_CONFIG[t]['name']})")

    return df_signals, signals

# ========================================================================
# BACKTEST ENGINE — TIME-AWARE (tiered bet sizing)
# ========================================================================

def backtest_time_aware(signals, bankroll, slippage, fee_rate, min_conf,
                         use_tiers=True, flat_bet_frac=0.05,
                         max_daily_loss_pct=MAX_DAILY_LOSS_PCT):
    """
    Hold-to-resolve with time-of-day tiered bet sizing.

    If use_tiers=True:  bet fraction comes from the signal's tier_bet_frac
    If use_tiers=False: flat bet fraction for comparison (baseline)
    """
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
        if entry_ask > MAX_ENTRY_PRICE:
            continue

        entry_price = entry_ask + slippage
        edge = s['confidence'] - entry_price
        if edge < MIN_EDGE:
            continue

        # Tiered or flat bet sizing
        bet_frac = s.get('tier_bet_frac', flat_bet_frac) if use_tiers else flat_bet_frac

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
            'regime': s.get('regime', '?'),
            'path_eff': round(s.get('path_eff', 0), 4),
            'hour_est': s.get('hour_est', -1),
            'tier': s.get('effective_tier', 0),
            'tier_name': s.get('tier_name', '?'),
            'bet_frac': bet_frac,
            'trades_per_sec': round(s.get('trades_per_sec', 0), 1),
            'actual': s['actual'], 'correct': correct,
            'entry_secs_in': s.get('entry_secs_in', 0),
            'strategy': 'TIME_AWARE' if use_tiers else 'FLAT',
        })
        equity_curve.append((len(log), bankroll))

    return log, equity_curve, bankroll


def backtest_prime_only(signals, bankroll, bet_frac, slippage, fee_rate, min_conf,
                         max_daily_loss_pct=MAX_DAILY_LOSS_PCT):
    """Only trade during Tier 1 (prime hours). Skip everything else."""
    prime_signals = [s for s in signals if s.get('effective_tier') == 1]
    return backtest_time_aware(prime_signals, bankroll, slippage, fee_rate,
                                min_conf, use_tiers=False, flat_bet_frac=bet_frac,
                                max_daily_loss_pct=max_daily_loss_pct)


def backtest_skip_thin(signals, bankroll, bet_frac, slippage, fee_rate, min_conf,
                        max_daily_loss_pct=MAX_DAILY_LOSS_PCT):
    """Trade Tier 1 and 2, skip Tier 3 (thin hours)."""
    filtered = [s for s in signals if s.get('effective_tier') <= 2]
    return backtest_time_aware(filtered, bankroll, slippage, fee_rate,
                                min_conf, use_tiers=True, flat_bet_frac=bet_frac,
                                max_daily_loss_pct=max_daily_loss_pct)

# ========================================================================
# ANALYSIS: HOUR-BY-HOUR BREAKDOWN
# ========================================================================

def analyze_by_hour(signals):
    """Print hour-by-hour signal accuracy breakdown (EST)."""
    if not signals:
        return

    df = pd.DataFrame([{
        'hour_est': s['hour_est'],
        'correct': s['signal'] == s['actual'],
        'tier': s['effective_tier'],
        'confidence': s['confidence'],
        'tps': s.get('trades_per_sec', 0),
    } for s in signals])

    print(f"\n  {'Hour':>6s}  {'Tier':>4s}  {'Signals':>7s}  {'Correct':>7s}  "
          f"{'WR%':>6s}  {'AvgConf':>7s}  {'AvgTPS':>7s}")
    print(f"  {'-'*6}  {'-'*4}  {'-'*7}  {'-'*7}  {'-'*6}  {'-'*7}  {'-'*7}")

    for hour in sorted(df['hour_est'].unique()):
        hdf = df[df['hour_est'] == hour]
        n = len(hdf)
        c = hdf['correct'].sum()
        wr = c / n * 100
        tier = hdf['tier'].mode().iloc[0] if len(hdf) > 0 else 0
        avg_conf = hdf['confidence'].mean()
        avg_tps = hdf['tps'].mean()
        tier_label = f"T{tier}"
        bar = '#' * int(wr / 5)
        print(f"  {hour:>4d}h   {tier_label:>3s}  {n:>6d}   {c:>6d}   "
              f"{wr:>5.1f}%  {avg_conf:>6.3f}  {avg_tps:>6.1f}  {bar}")

    # Per-tier summary
    print(f"\n  === Per-Tier Summary ===")
    for t in sorted(df['tier'].unique()):
        tdf = df[df['tier'] == t]
        n = len(tdf)
        c = tdf['correct'].sum()
        wr = c / n * 100
        print(f"  Tier {t} ({TIER_CONFIG[t]['name']:20s}): {c}/{n} = {wr:.1f}% accuracy  "
              f"| AvgConf={tdf['confidence'].mean():.3f}")


def analyze_by_hour_pnl(log):
    """Print hour-by-hour P&L breakdown from trade log."""
    if not log:
        return

    df = pd.DataFrame(log)
    if 'hour_est' not in df.columns:
        return

    print(f"\n  {'Hour':>6s}  {'Tier':>4s}  {'Trades':>6s}  {'W':>3s}  {'L':>3s}  "
          f"{'WR%':>6s}  {'P&L':>8s}  {'AvgWin':>7s}  {'AvgLoss':>8s}")
    print(f"  {'-'*6}  {'-'*4}  {'-'*6}  {'-'*3}  {'-'*3}  {'-'*6}  {'-'*8}  {'-'*7}  {'-'*8}")

    for hour in sorted(df['hour_est'].unique()):
        hdf = df[df['hour_est'] == hour]
        n = len(hdf)
        w = hdf['correct'].sum()
        l = n - w
        wr = w / n * 100
        pnl = hdf['pnl'].sum()
        avg_w = hdf[hdf['pnl'] > 0]['pnl'].mean() if w > 0 else 0
        avg_l = hdf[hdf['pnl'] <= 0]['pnl'].mean() if l > 0 else 0
        tier = hdf['tier'].mode().iloc[0] if len(hdf) > 0 else 0
        cls = '+' if pnl >= 0 else ''
        print(f"  {hour:>4d}h   T{tier:>1d}  {n:>5d}   {w:>2d}  {l:>2d}   "
              f"{wr:>5.1f}%  ${pnl:>+7.2f}  ${avg_w:>+6.2f}  ${avg_l:>+7.2f}")

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
    total_wins = sum(t['pnl'] for t in log if t['pnl'] > 0)
    total_losses = abs(sum(t['pnl'] for t in log if t['pnl'] <= 0))

    return {
        'trades': trades, 'wins': wins, 'losses': trades - wins,
        'wr': wr * 100, 'roi': roi, 'final': final_bankroll,
        'pnl': total_pnl, 'mdd': mdd,
        'avg_win': avg_win, 'avg_loss': avg_loss,
        'profit_factor': total_wins / (total_losses + 1e-9),
    }

# ========================================================================
# CHART GENERATION
# ========================================================================

def generate_chart(all_results, signals_df=None):
    chart_data = {}
    for name, (log, ec, final) in all_results.items():
        curve = []
        b = INITIAL_BANKROLL
        for trade in log:
            b += trade['pnl']
            curve.append(b)
        chart_data[name] = curve

    colors = ['#00d4aa', '#ff6b6b', '#4ecdc4', '#ffd93d', '#6c5ce7', '#fd79a8',
              '#a29bfe', '#55efc4', '#fdcb6e', '#e17055', '#74b9ff', '#dfe6e9']

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>V9 Time-Aware Backtest</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ background: #0a0a1a; color: #e0e0e0; font-family: 'Inter', sans-serif; padding: 24px; }}
  h1 {{ font-size: 28px; font-weight: 700; margin-bottom: 8px;
    background: linear-gradient(135deg, #ffd93d, #4ecdc4);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent; }}
  h2 {{ font-size: 20px; font-weight: 600; margin: 24px 0 12px;
    color: #4ecdc4; }}
  .subtitle {{ color: #888; font-size: 14px; margin-bottom: 12px; }}
  .config-badge {{ display: inline-block; background: #1a1a35; border: 1px solid #2a2a55;
    border-radius: 6px; padding: 4px 10px; font-size: 11px; color: #4ecdc4;
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
  table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
  th {{ text-align: left; padding: 10px 8px; color: #888;
    border-bottom: 1px solid #1e1e3a; font-weight: 500; }}
  td {{ padding: 8px; border-bottom: 1px solid #0d0d22; }}
  tr:hover {{ background: #1a1a35; }}
  .win {{ color: #00d4aa; }}
  .loss {{ color: #ff6b6b; }}
  .tier1 {{ color: #00d4aa; }}
  .tier2 {{ color: #ffd93d; }}
  .tier3 {{ color: #ff6b6b; }}
  .legend {{ display: flex; gap: 20px; flex-wrap: wrap; margin-bottom: 16px; }}
  .legend-item {{ display: flex; align-items: center; gap: 8px; font-size: 13px; }}
  .legend-dot {{ width: 12px; height: 12px; border-radius: 3px; }}
</style>
</head>
<body>
<h1>V9 Time-Aware Backtest</h1>
<p class="subtitle">v9 Regime-Aware + Time-of-Day Tiered Bet Sizing + Liquidity Gate</p>
<div>
  <span class="config-badge">Bankroll: ${INITIAL_BANKROLL}</span>
  <span class="config-badge">T1 Prime: {TIER1_START_EST}-{TIER1_END_EST}h EST @ {TIER_CONFIG[1]['bet_frac']*100:.0f}%</span>
  <span class="config-badge">T2 Extended: {TIER1_END_EST}-{TIER2_END_EST}h EST @ {TIER_CONFIG[2]['bet_frac']*100:.0f}%</span>
  <span class="config-badge">T3 Thin: rest @ {TIER_CONFIG[3]['bet_frac']*100:.0f}%</span>
  <span class="config-badge">Liq Down: &lt;p{LIQUIDITY_PERCENTILE_DOWNGRADE}</span>
</div>
"""

    # Stats cards
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

    # Chart
    html += '<div class="chart-container"><div class="legend">'
    for i, name in enumerate(chart_data.keys()):
        c = colors[i % len(colors)]
        html += f'<div class="legend-item"><div class="legend-dot" style="background:{c}"></div>{name}</div>'
    html += '</div><canvas id="chart"></canvas></div>'

    # Trade log for best strategy
    best_name = max(all_results.keys(), key=lambda k: all_results[k][2]) if all_results else None
    if best_name:
        best_log = all_results[best_name][0]
        html += f'<div class="trade-log"><h2>Trade Log — {best_name}</h2><table><thead><tr>'
        headers = ['#', 'Market', 'Time', 'Side', 'Entry', 'Exit', 'Bet%', 'Bet$', 'P&L',
                   'Bank', 'Conf', 'Edge', 'Hour', 'Tier', 'TPS', 'Regime', 'Actual', 'Result']
        for h in headers:
            html += f'<th>{h}</th>'
        html += '</tr></thead><tbody>'

        for idx, t in enumerate(best_log):
            cls = 'win' if t['pnl'] > 0 else 'loss'
            result = 'WIN' if t.get('correct') else 'LOSS'
            tier = t.get('tier', 0)
            tier_cls = f'tier{tier}'
            html += f'<tr><td>{idx+1}</td>'
            html += f'<td>{t["market"].split("-")[-1]}</td>'
            html += f'<td>{t["entry_time"][11:19]}</td>'
            html += f'<td>{t["side"]}</td>'
            html += f'<td>${t["entry_price"]:.3f}</td>'
            html += f'<td>${t["exit_price"]:.3f}</td>'
            html += f'<td>{t.get("bet_frac",0)*100:.0f}%</td>'
            html += f'<td>${t["bet_amount"]:.2f}</td>'
            html += f'<td class="{cls}">${t["pnl"]:+.2f}</td>'
            html += f'<td>${t["bankroll"]:.2f}</td>'
            html += f'<td>{t["confidence"]:.2f}</td>'
            html += f'<td>{t.get("edge",0):.2f}</td>'
            html += f'<td>{t.get("hour_est",-1)}h</td>'
            html += f'<td class="{tier_cls}">T{tier}</td>'
            html += f'<td>{t.get("trades_per_sec",0):.0f}</td>'
            html += f'<td>{t.get("regime","?")}</td>'
            html += f'<td>{t["actual"]}</td>'
            html += f'<td class="{cls}">{result}</td></tr>'

        html += '</tbody></table></div>'

    # Chart JS
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

    out_path = 'time_aware_backtest_results.html'
    with open(out_path, 'w') as f:
        f.write(html)
    print(f"\n  Chart saved to {out_path}")

# ========================================================================
# MAIN
# ========================================================================

def main():
    t0 = time.time()

    print("=" * 70)
    print(" POLYMARKET BTC 15-MIN BACKTEST — V9 TIME-AWARE")
    print("=" * 70)
    print(f"  Signal:              v9 Drift({W_DRIFT:.0%}) + OFI_Accel({W_OFI_ACCEL:.0%}) + Scoreboard({W_SCOREBOARD:.0%})")
    print(f"  Regime gate:         trend>{REGIME_TREND_THRESHOLD}  chop<{REGIME_CHOP_THRESHOLD}")
    print(f"  Confirmation:        adaptive {MIN_CONFIRM_WINDOW}-{MAX_CONFIRM_WINDOW}s")
    print(f"  Edge filter:         >{MIN_EDGE}")
    print(f"  Price cap:           ${MAX_ENTRY_PRICE}")
    print(f"")
    print(f"  TIME TIERS (EST):")
    for t in [1, 2, 3]:
        tc = TIER_CONFIG[t]
        print(f"    T{t} {tc['name']:22s}: bet={tc['bet_frac']*100:.0f}%  "
              f"conf_boost=+{tc['conf_boost']*100:.0f}%  min_conf={tc['min_conf']*100:.0f}%  "
              f"enabled={tc['enabled']}")
    print(f"  Liq downgrade:       <p{LIQUIDITY_PERCENTILE_DOWNGRADE} Binance TPS")
    print(f"  Bankroll:            ${INITIAL_BANKROLL}")
    print(f"  Max drawdown halt:   {MAX_DAILY_LOSS_PCT:.0%}")

    # Load data
    print("\n  Loading data...")
    df_meta, df_ticks, df_trades = load_all_data()
    print(f"  Markets: {len(df_meta)}, Ticks: {len(df_ticks):,}, Trades: {len(df_trades):,}")

    # Compute session liquidity
    print("\n  Computing session liquidity...")
    liq_map, liq_threshold, median_tps = compute_session_liquidity(df_trades, df_meta)
    print(f"  Median Binance TPS:  {median_tps:.1f}")
    print(f"  p{LIQUIDITY_PERCENTILE_DOWNGRADE} threshold:      {liq_threshold:.1f} TPS")

    # Build signals
    signals_df, signals_full = build_market_signals(df_meta, df_trades, df_ticks, liq_map, liq_threshold)

    if len(signals_df) == 0:
        print("\n  No signals generated. Exiting.")
        return

    # ==================================================================
    # HOUR-BY-HOUR ANALYSIS
    # ==================================================================
    print(f"\n{'='*70}")
    print(f" HOUR-BY-HOUR SIGNAL ACCURACY (EST)")
    print(f"{'='*70}")
    analyze_by_hour(signals_full)

    # ==================================================================
    # STRATEGY 1: FLAT (v9 baseline — no time awareness)
    # ==================================================================
    all_results = {}

    print(f"\n{'='*70}")
    print(f" STRATEGY COMPARISON: CONFIDENCE SWEEP")
    print(f"{'='*70}")

    sweep_rows = []

    for conf in CONFIDENCE_LEVELS:
        # A) Flat 5% — baseline (all hours, no tier adjustments)
        name_flat = f'v9 Flat 5% C>{conf:.0%}'
        log_f, ec_f, final_f = backtest_time_aware(
            signals_full, INITIAL_BANKROLL, SLIPPAGE, FEE_RATE, conf,
            use_tiers=False, flat_bet_frac=0.05)
        all_results[name_flat] = (log_f, ec_f, final_f)

        # B) Time-Aware Tiers
        name_tier = f'v9 Tiered C>{conf:.0%}'
        log_t, ec_t, final_t = backtest_time_aware(
            signals_full, INITIAL_BANKROLL, SLIPPAGE, FEE_RATE, conf,
            use_tiers=True)
        all_results[name_tier] = (log_t, ec_t, final_t)

        # C) Prime Only (T1)
        name_prime = f'v9 Prime-Only C>{conf:.0%}'
        log_p, ec_p, final_p = backtest_prime_only(
            signals_full, INITIAL_BANKROLL, 0.05, SLIPPAGE, FEE_RATE, conf)
        all_results[name_prime] = (log_p, ec_p, final_p)

        # D) Skip Thin (T1+T2 only)
        name_skip = f'v9 Skip-Thin C>{conf:.0%}'
        log_s, ec_s, final_s = backtest_skip_thin(
            signals_full, INITIAL_BANKROLL, 0.05, SLIPPAGE, FEE_RATE, conf)
        all_results[name_skip] = (log_s, ec_s, final_s)

        for label, lg, fn in [
            ('Flat 5%', log_f, final_f),
            ('Tiered', log_t, final_t),
            ('Prime-Only', log_p, final_p),
            ('Skip-Thin', log_s, final_s),
        ]:
            if len(lg) > 0:
                st = compute_stats(lg, fn)
                sweep_rows.append({
                    'conf': conf, 'mode': label, **st
                })

    # Print comparison table
    print(f"\n  {'Conf':>5s}  {'Mode':>12s}  {'Trades':>6s}  {'WR%':>6s}  "
          f"{'ROI':>8s}  {'Final':>8s}  {'MDD':>6s}  {'PF':>5s}  {'AvgW':>6s}  {'AvgL':>7s}")
    print(f"  {'-'*5}  {'-'*12}  {'-'*6}  {'-'*6}  {'-'*8}  {'-'*8}  {'-'*6}  {'-'*5}  {'-'*6}  {'-'*7}")
    for r in sweep_rows:
        print(f"  {r['conf']:>4.0%}  {r['mode']:>12s}  {r['trades']:>5d}   "
              f"{r['wr']:>5.1f}%  {r['roi']:>+7.1f}%  ${r['final']:>7.2f}  "
              f"{r['mdd']:>5.1%}  {r['profit_factor']:>4.2f}  ${r['avg_win']:>5.2f}  ${r['avg_loss']:>6.2f}")

    # ==================================================================
    # TOP STRATEGIES RANKING
    # ==================================================================
    print(f"\n{'='*70}")
    print(f" TOP STRATEGIES (by Final Bankroll)")
    print(f"{'='*70}")

    ranked = sorted(
        [(n, l, e, f) for n, (l, e, f) in all_results.items() if len(l) > 0],
        key=lambda x: -x[3]
    )[:15]

    for rank, (name, log, ec, final) in enumerate(ranked, 1):
        s = compute_stats(log, final)
        marker = ' << BEST' if rank == 1 else ''
        print(f"  #{rank:<2d} {name:35s}: ${final:>7.2f} ({s['roi']:>+6.1f}%)  "
              f"WR={s['wr']:>5.1f}%  PF={s['profit_factor']:.2f}{marker}")

    # ==================================================================
    # BEST STRATEGY HOUR-BY-HOUR P&L
    # ==================================================================
    best_name = ranked[0][0] if ranked else None
    if best_name:
        best_log = all_results[best_name][0]
        print(f"\n{'='*70}")
        print(f" HOUR-BY-HOUR P&L: {best_name}")
        print(f"{'='*70}")
        analyze_by_hour_pnl(best_log)

    # ==================================================================
    # TIER PERFORMANCE COMPARISON (best confidence level)
    # ==================================================================
    best_conf = ranked[0][0].split('C>')[1].split('%')[0] if ranked else '55'
    try:
        best_conf_val = int(best_conf) / 100
    except:
        best_conf_val = 0.55

    print(f"\n{'='*70}")
    print(f" TIER vs FLAT BREAKDOWN @ C>{best_conf_val:.0%}")
    print(f"{'='*70}")

    for mode_name in [f'v9 Flat 5% C>{best_conf_val:.0%}',
                       f'v9 Tiered C>{best_conf_val:.0%}',
                       f'v9 Prime-Only C>{best_conf_val:.0%}',
                       f'v9 Skip-Thin C>{best_conf_val:.0%}']:
        if mode_name in all_results:
            lg, ec, fn = all_results[mode_name]
            if len(lg) > 0:
                s = compute_stats(lg, fn)
                print(f"  {mode_name:35s}: ${fn:>7.2f} ({s['roi']:>+6.1f}%)  "
                      f"{s['trades']} trades  WR={s['wr']:.1f}%  "
                      f"MDD={s['mdd']:.1%}  PF={s['profit_factor']:.2f}")

    # Generate chart
    generate_chart(all_results, signals_df)

    # Save outputs
    if best_name and all_results[best_name][0]:
        df_log = pd.DataFrame(all_results[best_name][0])
        df_log.to_csv('time_aware_trade_log.csv', index=False)
        print(f"\n  Trade log saved: time_aware_trade_log.csv ({best_name})")

    if sweep_rows:
        pd.DataFrame(sweep_rows).to_csv('time_aware_sweep.csv', index=False)
        print(f"  Sweep saved:     time_aware_sweep.csv")

    elapsed = time.time() - t0
    print(f"\n  Done in {elapsed:.0f}s")


if __name__ == "__main__":
    main()
