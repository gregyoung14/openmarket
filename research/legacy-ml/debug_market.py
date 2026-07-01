
import sqlite3
import pandas as pd
import numpy as np
from scipy.stats import norm
import warnings
warnings.filterwarnings('ignore')

DB_PATH = '<LOCAL_OPENMARKET_PREDECESSOR>/polymarket_btc_data.db'

# Constants from backtest_v9_2.py
REGIME_TREND_THRESHOLD = 0.15
REGIME_CHOP_THRESHOLD  = 0.06
REGIME_AUTOCORR_CHOP   = -0.25
REGIME_LOOKBACK        = 60
NEUTRAL_CONF_PENALTY   = 0.02

W_DRIFT             = 0.55
W_OFI_ACCEL         = 0.30
W_SCOREBOARD        = 0.15
SCOREBOARD_SCALE    = 1000
OFI_SCALE           = 3

BASE_CONFIRM_WINDOW  = 30
MIN_CONFIRM_WINDOW   = 15
MAX_CONFIRM_WINDOW   = 50

MIN_CONFIDENCE       = 0.60
MAX_ENTRY_PRICE      = 0.55
MIN_SECS_INTO_MARKET = 60
MAX_SECS_INTO_MARKET = 600
MARKET_DURATION_SECS = 900

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
        if np.isnan(autocorr): autocorr = 0.0
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

def compute_signal_v9(close_1s, buy_vol_1s, sell_vol_1s, open_price, entry_secs, remaining_secs):
    n = len(close_1s)
    valid_mask = ~np.isnan(close_1s) & (close_1s > 0)
    valid_prices = close_1s[valid_mask]
    if len(valid_prices) < 15: return None, None, None
    current_price = float(valid_prices[-1])
    regime, path_eff, autocorr = detect_regime(close_1s)
    log_returns = np.diff(np.log(valid_prices.astype(float) + 1e-9))
    if len(log_returns) < 5: return None, None, None
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
    combined_prob_up = (W_DRIFT * drift_prob_up + W_OFI_ACCEL * ofi_accel_signal + W_SCOREBOARD * scoreboard_signal)
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
    return direction, confidence, {'regime': regime, 'adaptive_confirm': adaptive_confirm, 'combined_prob_up': combined_prob_up}

def build_1s_bars(market_trades, epoch_s):
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

def debug_market(epoch_s):
    slug = f"btc-updown-15m-{epoch_s}"
    conn = sqlite3.connect(DB_PATH)
    start_ms = epoch_s * 1000
    end_ms = start_ms + MARKET_DURATION_SECS * 1000
    df_trades = pd.read_sql_query(
        "SELECT trade_time, price, quantity, is_buyer_maker FROM binance_trades WHERE trade_time >= ? AND trade_time < ?",
        conn, params=(start_ms, end_ms))
    df_ticks = pd.read_sql_query(
        "SELECT * FROM polymarket_ticks_ms WHERE market_slug = ? AND event_type = 'price_change'",
        conn, params=(slug,))
    conn.close()

    if len(df_trades) < 50:
        print(f"Skipped {slug} due to low trades ({len(df_trades)})")
        return

    btc_start = float(df_trades.iloc[0]['price'])
    close_arr, buy_arr, sell_arr = build_1s_bars(df_trades, epoch_s)

    confirm_count = 0
    confirm_direction = None
    
    print(f"Analyzing {slug}...")
    for s in range(MIN_SECS_INTO_MARKET, MAX_SECS_INTO_MARKET):
        direction, confidence, comp = compute_signal_v9(
            close_arr[:s+1], buy_arr[:s+1], sell_arr[:s+1], btc_start, s, MARKET_DURATION_SECS - s
        )
        
        if direction is None:
            confirm_count = 0
            confirm_direction = None
            continue

        if comp['regime'] == 'chop':
            if confirm_count > 0:
                print(f"[{s}s] RESET: Regime CHOP")
            confirm_count = 0
            confirm_direction = None
            continue

        adapt = comp['adaptive_confirm']
        
        if confidence >= MIN_CONFIDENCE:
            if direction == confirm_direction:
                confirm_count += 1
            else:
                confirm_direction = direction
                confirm_count = 1
            
            if confirm_count >= adapt:
                # Check price
                current_ms = start_ms + s * 1000
                mkt_ticks = df_ticks[df_ticks['side_label'] == direction].sort_values('source_ts_ms')
                backward = mkt_ticks[mkt_ticks['source_ts_ms'] <= current_ms]
                if len(backward) > 0:
                    entry_ask = float(backward.iloc[-1]['best_ask'])
                else:
                    forward = mkt_ticks[
                        (mkt_ticks['source_ts_ms'] >= current_ms) &
                        (mkt_ticks['source_ts_ms'] < current_ms + 15000)
                    ]
                    if len(forward) > 0:
                        entry_ask = float(forward.iloc[0]['best_ask'])
                    else:
                        entry_ask = 0.50
                
                entry_price = entry_ask + 0.005
                edge = confidence - entry_price

                print(f"[{s}s] SIGNAL {direction} conf={confidence:.4f} ask={entry_ask:.4f} edge={edge:.4f} confirm={confirm_count}/{adapt}")
                
                if entry_ask > MAX_ENTRY_PRICE:
                    print(f"      REJECTED: Price {entry_ask:.4f} > {MAX_ENTRY_PRICE}")
                    confirm_count = 0
                    confirm_direction = None
                    continue
                elif edge < 0:
                    print(f"      REJECTED: Edge {edge:.4f} < 0")
                    confirm_count = 0
                    confirm_direction = None
                    continue
                else:
                    print(f"      EMITTED! at {s}s")
                    return
        else:
            if confirm_count > 0:
                # print(f"[{s}s] RESET: Confidence low ({confidence:.4f})")
                pass
            confirm_count = 0
            confirm_direction = None

    print(f"No signal emitted for {slug}")

debug_market(1770895800)
