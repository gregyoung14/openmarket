"""
LIVE TRADING ENGINE — Polymarket BTC 15-Min Binary Options
=============================================================
Real-time inference + execution via:
  - Binance WebSocket (trade stream)
  - Polymarket WebSocket (CLOB price feed)
  - Polymarket REST API (order placement)

Architecture:
  ┌─────────────────┐     ┌────────────────┐
  │  Binance WS     │────►│                │
  │  btcusdt@trade  │     │  Feature Engine │──► ML Ensemble ──► Execution
  │                 │     │  (1s buckets)   │                    Engine
  │  Polymarket WS  │────►│                │
  │  CLOB feed      │     └────────────────┘
  └─────────────────┘

Usage:
  # Dry-run mode (no real orders):
  python live_trader.py

  # Live mode:
  python live_trader.py --live

  # Config via .env file:
  POLY_API_KEY=xxx
  POLY_API_SECRET=xxx
  POLY_PASSPHRASE=xxx

"""

import os
import sys
import json
import time
import math
import logging
import threading
import argparse
from datetime import datetime, timezone
from collections import deque
from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, List

import numpy as np
import pandas as pd
import joblib
import websocket

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ========================================================================
# CONFIG
# ========================================================================

@dataclass
class TradingConfig:
    # Bankroll
    initial_bankroll: float = 100.0
    bet_fraction: float = 0.05           # 5% per trade
    
    # Execution
    slippage: float = 0.005              # $0.005 per share
    fee_rate: float = 0.01               # 1% per leg
    min_confidence: float = 0.60         # Only trade at >60% ensemble confidence
    
    # Strategy
    strategy: str = 'HOLD_TO_RESOLVE'    # or 'MOMENTUM'
    momentum_tp: float = 0.10            # Take-profit target for momentum
    
    # Feature engine
    buffer_size: int = 120               # Keep 120s of 1s bars in memory
    signal_window: int = 30              # Average last 30 predictions for market signal
    min_seconds_into_market: int = 30    # Wait 30s into market before trading
    max_seconds_into_market: int = 600   # Don't enter after 10 min (prices too unfavorable)
    
    # Risk
    max_open_positions: int = 1          # Only 1 position at a time
    max_daily_loss: float = 0.20         # Stop if drawdown exceeds 20%
    
    # WebSocket URLs
    binance_ws_url: str = 'wss://stream.binance.com:9443/ws/btcusdt@trade'
    polymarket_ws_url: str = 'wss://ws-subscriptions-clob.polymarket.com/ws/market'

# ========================================================================
# LOGGING SETUP
# ========================================================================

def setup_logging():
    fmt = '%(asctime)s | %(levelname)-7s | %(message)s'
    logging.basicConfig(level=logging.INFO, format=fmt,
                       handlers=[
                           logging.StreamHandler(sys.stdout),
                           logging.FileHandler('live_trader.log', encoding='utf-8'),
                       ])
    return logging.getLogger('LiveTrader')

log = setup_logging()

# ========================================================================
# TRADE RECORD
# ========================================================================

@dataclass
class Position:
    market_slug: str
    side: str                          # 'UP' or 'DOWN'
    entry_price: float
    shares: float
    bet_amount: float
    confidence: float
    entry_time: str
    market_end_ms: int
    exit_price: Optional[float] = None
    exit_time: Optional[str] = None
    pnl: Optional[float] = None
    exit_type: Optional[str] = None

# ========================================================================
# FEATURE ENGINE — Streaming 1s bar construction
# ========================================================================

class FeatureEngine:
    """
    Maintains rolling buffers of 1s and 5s aggregated data.
    Computes the exact same features used in training.
    """
    
    def __init__(self, config: TradingConfig, feature_list: list):
        self.config = config
        self.feature_list = feature_list
        
        # Raw trade buffer for current second
        self.current_second_ms = None
        self.current_trades = []
        
        # 1s bar buffer (rolling)
        self.bars_1s = deque(maxlen=config.buffer_size)
        
        # Polymarket latest state
        self.poly_state = {
            'p_up_last': 0, 'p_up_bid': 0, 'p_up_ask': 0,
            'p_down_last': 0, 'p_down_bid': 0, 'p_down_ask': 0,
            'p_up_vol': 0, 'p_down_vol': 0,
            'p_up_cnt': 0, 'p_down_cnt': 0,
        }
        
        # Lag data (simplified — we track Binance-Poly timing diff)
        self.last_binance_ts = 0
        self.last_poly_ts = 0
        self.lag_ms_buffer = deque(maxlen=30)
        
        # 5s context
        self.bars_5s_buffer = deque(maxlen=30)
        self.current_5s_trades = []
        self.current_5s_ms = None
        
        # Running state for features
        self._lock = threading.Lock()
    
    def on_binance_trade(self, trade_time_ms: int, price: float, qty: float, is_buyer_maker: bool):
        """Process a raw Binance trade tick."""
        with self._lock:
            second_ms = (trade_time_ms // 1000) * 1000
            five_s_ms = (trade_time_ms // 5000) * 5000
            
            self.last_binance_ts = trade_time_ms
            
            trade = {
                'time': trade_time_ms,
                'price': price,
                'qty': qty,
                'is_buyer_maker': is_buyer_maker,
            }
            
            # 1s bucket
            if self.current_second_ms is None:
                self.current_second_ms = second_ms
            
            if second_ms != self.current_second_ms:
                # Flush current second into a bar
                self._flush_1s_bar()
                self.current_second_ms = second_ms
                self.current_trades = []
            
            self.current_trades.append(trade)
            
            # 5s bucket
            if self.current_5s_ms is None:
                self.current_5s_ms = five_s_ms
            
            if five_s_ms != self.current_5s_ms:
                self._flush_5s_bar()
                self.current_5s_ms = five_s_ms
                self.current_5s_trades = []
            
            self.current_5s_trades.append(trade)
    
    def on_polymarket_tick(self, side: str, price: float, bid: float, ask: float, 
                           size: float, ts_ms: int):
        """Process a Polymarket price update."""
        with self._lock:
            sl = side.lower()
            self.poly_state[f'p_{sl}_last'] = price
            self.poly_state[f'p_{sl}_bid'] = bid
            self.poly_state[f'p_{sl}_ask'] = ask
            self.poly_state[f'p_{sl}_vol'] += size
            self.poly_state[f'p_{sl}_cnt'] += 1
            self.last_poly_ts = ts_ms
            
            # Update lag estimate
            if self.last_binance_ts > 0:
                lag = ts_ms - self.last_binance_ts
                self.lag_ms_buffer.append(lag)
    
    def _flush_1s_bar(self):
        """Convert accumulated trades into a 1s bar."""
        if not self.current_trades:
            return
        
        trades = self.current_trades
        prices = [t['price'] for t in trades]
        qtys = [t['qty'] for t in trades]
        
        buy_vol = sum(t['qty'] for t in trades if not t['is_buyer_maker'])
        sell_vol = sum(t['qty'] for t in trades if t['is_buyer_maker'])
        
        bar = {
            'ts_ms': self.current_second_ms,
            'o': prices[0],
            'h': max(prices),
            'l': min(prices),
            'c': prices[-1],
            'v': sum(qtys),
            'tc': len(trades),
            'bv': buy_vol,
            'sv': sell_vol,
            'ats': np.mean(qtys),
            'mts': max(qtys),
            'vwap': sum(p*q for p,q in zip(prices, qtys)) / (sum(qtys) + 1e-9),
            'pstd': np.std(prices) if len(prices) > 1 else 0,
            # Snapshot poly state
            **{k: v for k, v in self.poly_state.items()},
            # Lag features
            'lgm': np.mean(self.lag_ms_buffer) if self.lag_ms_buffer else 0,
            'lgstd': np.std(self.lag_ms_buffer) if len(self.lag_ms_buffer) > 1 else 0,
            'lgpr': (sum(1 for x in self.lag_ms_buffer if x > 0) / len(self.lag_ms_buffer)) if self.lag_ms_buffer else 0.5,
            'lgn': len(self.lag_ms_buffer),
        }
        
        self.bars_1s.append(bar)
    
    def _flush_5s_bar(self):
        """Convert accumulated trades into a 5s bar."""
        if not self.current_5s_trades:
            return
        
        trades = self.current_5s_trades
        prices = [t['price'] for t in trades]
        qtys = [t['qty'] for t in trades]
        
        bar = {
            'ts_ms': self.current_5s_ms,
            'c': prices[-1],
            'v': sum(qtys),
            'tc': len(trades),
            'p_up_last': self.poly_state['p_up_last'],
            'p_up_vol': self.poly_state['p_up_vol'],
        }
        
        self.bars_5s_buffer.append(bar)
    
    def compute_features(self) -> Optional[pd.DataFrame]:
        """
        Compute the full feature vector from the current buffer.
        Returns a single-row DataFrame with all features, or None if not enough data.
        """
        with self._lock:
            if len(self.bars_1s) < 15:  # Need at least 15 bars for rolling features
                return None
            
            bars = list(self.bars_1s)
        
        # Build DataFrame from bars
        df = pd.DataFrame(bars)
        df['ts'] = pd.to_datetime(df['ts_ms'], unit='ms')
        df.set_index('ts', inplace=True)
        
        # 5s context
        if self.bars_5s_buffer:
            last_5s = self.bars_5s_buffer[-1]
            df['c5s'] = last_5s['c']
            df['v5s'] = last_5s['v']
            df['tc5s'] = last_5s['tc']
            df['p_up_5s'] = last_5s['p_up_last']
            df['p_up_vol_5s'] = last_5s['p_up_vol']
        
        # Forward fill poly/lag cols
        pcols = [c for c in df.columns if c.startswith('p_') or c.startswith('lg')]
        df[pcols] = df[pcols].ffill().fillna(0)
        
        # ============ FEATURES (exact same as training) ============
        df['ret'] = df['c'].pct_change()
        df['hl'] = (df['h'] - df['l']) / (df['c'] + 1e-9)
        df['co'] = (df['c'] - df['o']) / (df['o'] + 1e-9)
        df['vwap_d'] = (df['c'] - df['vwap']) / (df['vwap'] + 1e-9)
        df['ivol'] = df['pstd'] / (df['c'] + 1e-9)
        
        tot = df['bv'] + df['sv'] + 1e-9
        df['ofi'] = (df['bv'] - df['sv']) / tot
        df['br'] = df['bv'] / tot
        for w in [3, 5, 10]:
            df[f'ofi_m{w}'] = df['ofi'].rolling(w, min_periods=1).mean()
            df[f'ofi_a{w}'] = df['ofi'] - df[f'ofi_m{w}']
        df['cum_ofi'] = df['ofi'].rolling(30, min_periods=1).sum()
        
        df['tc_r'] = df['tc'].pct_change()
        df['tc_m5'] = df['tc'].rolling(5, min_periods=1).mean()
        df['rtc'] = df['tc'] / (df['tc_m5'] + 1e-9)
        df['ats_m'] = df['ats'].rolling(10, min_periods=1).mean()
        df['rats'] = df['ats'] / (df['ats_m'] + 1e-9)
        df['whale'] = df['mts'] / (df['mts'].rolling(10, min_periods=1).mean() + 1e-9)
        
        df['v3'] = df['ret'].rolling(3, min_periods=1).std()
        df['v10'] = df['ret'].rolling(10, min_periods=1).std()
        df['vratio'] = df['v3'] / (df['v10'] + 1e-9)
        
        for p in [3, 5, 10]:
            df[f'roc{p}'] = df['c'].pct_change(periods=p)
        
        d = df['c'].diff()
        g = d.where(d > 0, 0).rolling(10, min_periods=1).mean()
        l = (-d.where(d < 0, 0)).rolling(10, min_periods=1).mean()
        df['rsi'] = 100 - (100 / (1 + g / (l + 1e-9)))
        
        df['ema_x'] = (df['c'].ewm(span=5).mean() - df['c'].ewm(span=15).mean()) / \
                       (df['c'].ewm(span=15).mean() + 1e-9)
        
        if 'p_up_last' in df.columns:
            df['pup'] = df['p_up_last']
            df['psp_u'] = df['p_up_ask'] - df['p_up_bid']
            df['psp_d'] = df['p_down_ask'] - df['p_down_bid']
            df['pm3'] = df['p_up_last'].pct_change(3)
            df['pm5'] = df['p_up_last'].pct_change(5)
            df['pd1'] = df['p_up_last'].diff()
            ptv = df['p_up_vol'] + df['p_down_vol'] + 1e-9
            df['pvr'] = df['p_up_vol'] / ptv
            df['pdiv'] = df['p_up_last'].pct_change() - df['ret']
        
        if 'lgm' in df.columns:
            df['lgdir'] = np.sign(df['lgm'])
            df['lgchg'] = df['lgm'].diff()
        
        df['hour'] = df.index.hour
        df['hour_sin'] = np.sin(2 * np.pi * df['hour'] / 24)
        df['hour_cos'] = np.cos(2 * np.pi * df['hour'] / 24)
        
        if 'c5s' in df.columns:
            df['ret_5s'] = df['c5s'].pct_change()
            df['cross_tf'] = df['ret'] - df['ret_5s']
        
        for i in range(1, 6):
            df[f'rl{i}'] = df['ret'].shift(i)
            df[f'ol{i}'] = df['ofi'].shift(i)
        if 'pup' in df.columns:
            for i in range(1, 4):
                df[f'pl{i}'] = df['pup'].shift(i)
        
        # Clean
        df = df.fillna(0).replace([np.inf, -np.inf], 0)
        
        # Return last row only — that's our current feature vector
        last_row = df.iloc[[-1]]
        
        # Ensure all expected features exist
        for f in self.feature_list:
            if f not in last_row.columns:
                last_row[f] = 0
        
        return last_row[self.feature_list]

# ========================================================================
# ML INFERENCE
# ========================================================================

class EnsemblePredictor:
    """Loads saved models and runs ensemble prediction."""
    
    def __init__(self, models_dir='models'):
        log.info(f"Loading models from {models_dir}/")
        self.xgb_model = joblib.load(os.path.join(models_dir, 'xgb_model.pkl'))
        self.lgb_model = joblib.load(os.path.join(models_dir, 'lgb_model.pkl'))
        self.meta_clf = joblib.load(os.path.join(models_dir, 'meta_clf.pkl'))
        
        with open(os.path.join(models_dir, 'features.json')) as f:
            self.feature_list = json.load(f)
        
        log.info(f"Models loaded. {len(self.feature_list)} features.")
    
    def predict(self, X: pd.DataFrame) -> tuple:
        """
        Returns: (direction, confidence, raw_up_prob)
        """
        xp = self.xgb_model.predict_proba(X)[:, 1]
        lp = self.lgb_model.predict_proba(X)[:, 1]
        meta_X = np.column_stack([lp, xp])
        prob = self.meta_clf.predict_proba(meta_X)[:, 1][0]
        
        if prob > 0.5:
            return 'UP', prob, prob
        else:
            return 'DOWN', 1 - prob, prob

# ========================================================================
# MARKET TRACKER — Knows which 15-min market is active
# ========================================================================

class MarketTracker:
    """Tracks which Polymarket 15-min market is currently active."""
    
    def __init__(self):
        self.current_market = None
        self.market_start_ms = None
        self.market_end_ms = None
        self.known_markets = {}  # slug -> {token_id, condition_id}
    
    def update_from_poly_message(self, msg: dict):
        """Extract market info from Polymarket WebSocket messages."""
        if 'market' in msg:
            slug = msg.get('market', '')
            if slug.startswith('btc-updown-15m-'):
                epoch_s = int(slug.split('-')[-1])
                start_ms = epoch_s * 1000
                end_ms = start_ms + 900_000
                now_ms = int(time.time() * 1000)
                
                if start_ms <= now_ms < end_ms:
                    if self.current_market != slug:
                        self.current_market = slug
                        self.market_start_ms = start_ms
                        self.market_end_ms = end_ms
                        log.info(f"MARKET ACTIVE: {slug}")
                        log.info(f"  Window: {datetime.fromtimestamp(epoch_s, tz=timezone.utc)} -> "
                                f"{datetime.fromtimestamp(epoch_s + 900, tz=timezone.utc)}")
    
    def get_current_market(self) -> Optional[str]:
        """Return current active market slug, or None."""
        now_ms = int(time.time() * 1000)
        
        # Check if we're still in a known window
        if self.market_end_ms and now_ms >= self.market_end_ms:
            log.info(f"Market {self.current_market} has RESOLVED.")
            self.current_market = None
            self.market_start_ms = None
            self.market_end_ms = None
        
        # Try to find current market from timestamp
        now_s = int(time.time())
        # Markets start on 15-min boundaries
        market_start_s = (now_s // 900) * 900
        slug = f'btc-updown-15m-{market_start_s}'
        
        if self.current_market != slug:
            self.current_market = slug
            self.market_start_ms = market_start_s * 1000
            self.market_end_ms = self.market_start_ms + 900_000
            log.info(f"MARKET INFERRED: {slug}")
        
        return self.current_market
    
    def seconds_into_market(self) -> int:
        if self.market_start_ms:
            return int((time.time() * 1000 - self.market_start_ms) / 1000)
        return 0
    
    def seconds_until_resolve(self) -> int:
        if self.market_end_ms:
            return int((self.market_end_ms - time.time() * 1000) / 1000)
        return 999

# ========================================================================
# EXECUTION ENGINE
# ========================================================================

class ExecutionEngine:
    """
    Manages order placement and position tracking.
    
    Supports two modes:
      - DRY_RUN: Logs theoretical trades, no real orders
      - LIVE: Places real orders via Polymarket CLOB API
    """
    
    def __init__(self, config: TradingConfig, live_mode: bool = False):
        self.config = config
        self.live_mode = live_mode
        self.bankroll = config.initial_bankroll
        self.peak_bankroll = config.initial_bankroll
        self.positions: List[Position] = []
        self.closed_positions: List[Position] = []
        self.trade_log = []
        self._lock = threading.Lock()
        
        # Polymarket CLOB client (only if live)
        self.clob_client = None
        if live_mode:
            self._init_clob_client()
        
        log.info(f"Execution engine: {'LIVE' if live_mode else 'DRY RUN'}")
        log.info(f"Bankroll: ${self.bankroll:.2f}")
    
    def _init_clob_client(self):
        """Initialize Polymarket CLOB client for live trading."""
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import OrderArgs, OrderType
            
            api_key = os.getenv('POLY_API_KEY')
            api_secret = os.getenv('POLY_API_SECRET')
            passphrase = os.getenv('POLY_PASSPHRASE')
            
            if not all([api_key, api_secret, passphrase]):
                log.error("Missing POLY_API_KEY, POLY_API_SECRET, or POLY_PASSPHRASE in .env")
                log.error("Falling back to DRY RUN mode")
                self.live_mode = False
                return
            
            self.clob_client = ClobClient(
                host="https://clob.polymarket.com",
                key=api_key,
                chain_id=137,  # Polygon mainnet
            )
            
            # Derive API creds
            self.clob_client.set_api_creds(
                self.clob_client.create_or_derive_api_creds()
            )
            
            log.info("Polymarket CLOB client initialized successfully")
            
        except Exception as e:
            log.error(f"Failed to initialize CLOB client: {e}")
            log.error("Falling back to DRY RUN mode")
            self.live_mode = False
    
    def can_trade(self) -> bool:
        """Check if we're allowed to enter a new position."""
        with self._lock:
            # Position limit
            if len(self.positions) >= self.config.max_open_positions:
                return False
            
            # Drawdown limit
            dd = (self.peak_bankroll - self.bankroll) / self.peak_bankroll
            if dd > self.config.max_daily_loss:
                log.warning(f"MAX DAILY LOSS hit ({dd:.1%}). Trading paused.")
                return False
            
            return self.bankroll > 1.0  # Need at least $1
    
    def enter_position(self, market_slug: str, side: str, confidence: float,
                       entry_ask: float, market_end_ms: int):
        """Enter a new position."""
        with self._lock:
            entry_price = entry_ask + self.config.slippage
            entry_price = min(entry_price, 0.99)
            
            bet_amount = self.bankroll * self.config.bet_fraction
            fee_entry = bet_amount * self.config.fee_rate
            capital = bet_amount - fee_entry
            shares = capital / entry_price
            
            pos = Position(
                market_slug=market_slug,
                side=side,
                entry_price=entry_price,
                shares=shares,
                bet_amount=bet_amount,
                confidence=confidence,
                entry_time=datetime.now(timezone.utc).isoformat(),
                market_end_ms=market_end_ms,
            )
            
            self.positions.append(pos)
            
            log.info(f"{'LIVE' if self.live_mode else 'SIM'} ENTRY:")
            log.info(f"  Market:     {market_slug}")
            log.info(f"  Side:       {side}")
            log.info(f"  Entry:      ${entry_price:.4f}")
            log.info(f"  Shares:     {shares:.2f}")
            log.info(f"  Bet:        ${bet_amount:.2f}")
            log.info(f"  Confidence: {confidence:.4f}")
            
            if self.live_mode and self.clob_client:
                self._place_live_order(market_slug, side, entry_price, shares)
    
    def _place_live_order(self, market_slug: str, side: str, price: float, size: float):
        """Place a real order on Polymarket CLOB."""
        try:
            from py_clob_client.clob_types import OrderArgs, OrderType
            
            # Get the token ID for the correct side
            # This requires looking up the market's condition tokens
            token_id = self._get_token_id(market_slug, side)
            if not token_id:
                log.error(f"Could not find token ID for {market_slug} {side}")
                return
            
            order_args = OrderArgs(
                price=price,
                size=size,
                side="BUY",
                token_id=token_id,
            )
            
            signed_order = self.clob_client.create_order(order_args)
            resp = self.clob_client.post_order(signed_order, OrderType.GTC)
            log.info(f"LIVE ORDER PLACED: {resp}")
            
        except Exception as e:
            log.error(f"Failed to place live order: {e}")
    
    def _get_token_id(self, market_slug: str, side: str) -> Optional[str]:
        """Look up the token ID for a market side."""
        try:
            # Polymarket API to get market info
            resp = self.clob_client.get_market(market_slug)
            tokens = resp.get('tokens', [])
            for token in tokens:
                if token.get('outcome', '').upper() == side:
                    return token['token_id']
        except Exception as e:
            log.error(f"Token ID lookup failed: {e}")
        return None
    
    def check_exits(self, current_bid_up: float, current_bid_down: float):
        """Check if any open positions should be exited."""
        with self._lock:
            now_ms = int(time.time() * 1000)
            
            for pos in self.positions[:]:
                # Check resolution
                if now_ms >= pos.market_end_ms:
                    # Market has resolved — we'll determine outcome from price
                    resolve_price = current_bid_up if pos.side == 'UP' else current_bid_down
                    if resolve_price > 0.90:
                        exit_price = 1.00
                        exit_type = 'RESOLVE_WIN'
                    else:
                        exit_price = 0.00
                        exit_type = 'RESOLVE_LOSS'
                    self._close_position(pos, exit_price, exit_type)
                    continue
                
                # Check momentum take-profit
                if self.config.strategy == 'MOMENTUM':
                    current_bid = current_bid_up if pos.side == 'UP' else current_bid_down
                    tp_price = pos.entry_price + self.config.momentum_tp
                    
                    if current_bid >= tp_price:
                        exit_price = current_bid - self.config.slippage
                        self._close_position(pos, exit_price, 'TAKE_PROFIT')
    
    def _close_position(self, pos: Position, exit_price: float, exit_type: str):
        """Close a position and update bankroll."""
        payout = pos.shares * exit_price
        fee_exit = payout * self.config.fee_rate if payout > 0 else 0
        net_payout = payout - fee_exit
        pnl = net_payout - pos.bet_amount
        
        pos.exit_price = exit_price
        pos.exit_time = datetime.now(timezone.utc).isoformat()
        pos.pnl = pnl
        pos.exit_type = exit_type
        
        self.bankroll += pnl
        self.peak_bankroll = max(self.peak_bankroll, self.bankroll)
        
        self.positions.remove(pos)
        self.closed_positions.append(pos)
        self.trade_log.append(asdict(pos))
        
        # Save trade log
        with open('live_trades.json', 'w') as f:
            json.dump(self.trade_log, f, indent=2, default=str)
        
        emoji = '+' if pnl > 0 else '-'
        log.info(f"{'LIVE' if self.live_mode else 'SIM'} EXIT [{exit_type}]:")
        log.info(f"  Market:   {pos.market_slug}")
        log.info(f"  Side:     {pos.side}")
        log.info(f"  Entry:    ${pos.entry_price:.4f} -> Exit: ${exit_price:.4f}")
        log.info(f"  P&L:      ${pnl:+.2f}")
        log.info(f"  Bankroll: ${self.bankroll:.2f}")

# ========================================================================
# WEBSOCKET HANDLERS
# ========================================================================

class BinanceWS:
    """Binance trade stream WebSocket handler."""
    
    def __init__(self, feature_engine: FeatureEngine, config: TradingConfig):
        self.feature_engine = feature_engine
        self.config = config
        self.ws = None
        self.trade_count = 0
    
    def start(self):
        def on_message(ws, message):
            try:
                data = json.loads(message)
                self.feature_engine.on_binance_trade(
                    trade_time_ms=data['T'],
                    price=float(data['p']),
                    qty=float(data['q']),
                    is_buyer_maker=data['m'],
                )
                self.trade_count += 1
            except Exception as e:
                log.error(f"Binance WS error: {e}")
        
        def on_error(ws, error):
            log.error(f"Binance WS error: {error}")
        
        def on_close(ws, code, reason):
            log.warning(f"Binance WS closed: {code} {reason}. Reconnecting...")
            time.sleep(2)
            self.start()
        
        def on_open(ws):
            log.info("Binance WebSocket CONNECTED")
        
        self.ws = websocket.WebSocketApp(
            self.config.binance_ws_url,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
            on_open=on_open,
        )
        
        thread = threading.Thread(target=self.ws.run_forever, daemon=True)
        thread.start()

class PolymarketWS:
    """Polymarket CLOB WebSocket handler."""
    
    def __init__(self, feature_engine: FeatureEngine, market_tracker: MarketTracker,
                 config: TradingConfig):
        self.feature_engine = feature_engine
        self.market_tracker = market_tracker
        self.config = config
        self.ws = None
        self.tick_count = 0
    
    def start(self):
        def on_message(ws, message):
            try:
                data = json.loads(message)
                
                # Handle different Polymarket WS message types
                if isinstance(data, list):
                    for item in data:
                        self._process_tick(item)
                else:
                    self._process_tick(data)
                    
            except Exception as e:
                log.error(f"Polymarket WS error: {e}")
        
        def _process_tick(self, tick):
            if 'price' in tick and 'market' in tick:
                side = tick.get('outcome', tick.get('side', 'UP')).upper()
                self.feature_engine.on_polymarket_tick(
                    side=side,
                    price=float(tick.get('price', 0)),
                    bid=float(tick.get('best_bid', tick.get('price', 0))),
                    ask=float(tick.get('best_ask', tick.get('price', 0))),
                    size=float(tick.get('size', 0)),
                    ts_ms=int(tick.get('timestamp', time.time() * 1000)),
                )
                self.market_tracker.update_from_poly_message(tick)
                self.tick_count += 1
        
        self._process_tick = lambda tick: _process_tick(self, tick)
        
        def on_error(ws, error):
            log.error(f"Polymarket WS error: {error}")
        
        def on_close(ws, code, reason):
            log.warning(f"Polymarket WS closed: {code} {reason}. Reconnecting...")
            time.sleep(2)
            self.start()
        
        def on_open(ws):
            log.info("Polymarket WebSocket CONNECTED")
            # Subscribe to BTC UP/DOWN markets
            subscribe_msg = json.dumps({
                "type": "subscribe",
                "channel": "market",
                "assets_id": [],  # Will be populated with active market token IDs
            })
            ws.send(subscribe_msg)
        
        self.ws = websocket.WebSocketApp(
            self.config.polymarket_ws_url,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
            on_open=on_open,
        )
        
        thread = threading.Thread(target=self.ws.run_forever, daemon=True)
        thread.start()

# ========================================================================
# SIGNAL AGGREGATOR — Smooths predictions into market-level signals
# ========================================================================

class SignalAggregator:
    """
    Collects per-second ML predictions and produces a smoothed market-level signal.
    """
    
    def __init__(self, window: int = 30):
        self.window = window
        self.predictions = deque(maxlen=window)
        self.current_market = None
    
    def add_prediction(self, direction: str, confidence: float, raw_prob: float, 
                       market_slug: str):
        """Add a new prediction. Resets if market changes."""
        if market_slug != self.current_market:
            self.predictions.clear()
            self.current_market = market_slug
        
        self.predictions.append({
            'direction': direction,
            'confidence': confidence,
            'raw_prob': raw_prob,
            'time': time.time(),
        })
    
    def get_signal(self) -> Optional[dict]:
        """
        Aggregate recent predictions into a market signal.
        Returns None if not enough data.
        """
        if len(self.predictions) < 5:
            return None
        
        preds = list(self.predictions)
        
        # Average raw UP probability
        avg_prob = np.mean([p['raw_prob'] for p in preds])
        
        if avg_prob > 0.5:
            direction = 'UP'
            confidence = avg_prob
        else:
            direction = 'DOWN'
            confidence = 1 - avg_prob
        
        # Consistency score — how much do predictions agree?
        directions = [p['direction'] for p in preds]
        dominant = max(set(directions), key=directions.count)
        consistency = directions.count(dominant) / len(directions)
        
        return {
            'direction': direction,
            'confidence': confidence,
            'consistency': consistency,
            'n_preds': len(preds),
            'avg_raw_prob': avg_prob,
        }

# ========================================================================
# MAIN TRADING LOOP
# ========================================================================

class LiveTrader:
    """Main trading orchestrator."""
    
    def __init__(self, config: TradingConfig, live_mode: bool = False):
        self.config = config
        self.live_mode = live_mode
        
        # Load ML models
        self.predictor = EnsemblePredictor()
        
        # Initialize components
        self.feature_engine = FeatureEngine(config, self.predictor.feature_list)
        self.market_tracker = MarketTracker()
        self.execution = ExecutionEngine(config, live_mode)
        self.signal_agg = SignalAggregator(config.signal_window)
        
        # WebSocket handlers
        self.binance_ws = BinanceWS(self.feature_engine, config)
        self.poly_ws = PolymarketWS(self.feature_engine, self.market_tracker, config)
        
        # Stats
        self.inference_count = 0
        self.start_time = time.time()
    
    def run(self):
        """Main entry point."""
        log.info("=" * 70)
        log.info(" POLYMARKET BTC LIVE TRADER")
        log.info("=" * 70)
        log.info(f"  Mode:        {'LIVE' if self.live_mode else 'DRY RUN (paper)'}")
        log.info(f"  Bankroll:    ${self.config.initial_bankroll:.2f}")
        log.info(f"  Bet Size:    {self.config.bet_fraction*100:.0f}%")
        log.info(f"  Min Conf:    {self.config.min_confidence:.0%}")
        log.info(f"  Strategy:    {self.config.strategy}")
        log.info(f"  Features:    {len(self.predictor.feature_list)}")
        log.info("=" * 70)
        
        # Start WebSocket feeds
        log.info("Connecting to data feeds...")
        self.binance_ws.start()
        self.poly_ws.start()
        
        # Wait for initial data
        log.info("Warming up feature engine (need 15s of data)...")
        time.sleep(16)
        
        # Main inference loop — runs every 1 second
        log.info("Starting inference loop...")
        
        try:
            while True:
                self._inference_tick()
                time.sleep(1)
        except KeyboardInterrupt:
            log.info("Shutting down...")
            self._shutdown()
    
    def _inference_tick(self):
        """Run one inference cycle (every 1s)."""
        try:
            # 1. Compute features
            features = self.feature_engine.compute_features()
            if features is None:
                return
            
            # 2. Run ML prediction
            direction, confidence, raw_prob = self.predictor.predict(features)
            self.inference_count += 1
            
            # 3. Get current market
            market = self.market_tracker.get_current_market()
            if not market:
                return
            
            secs_in = self.market_tracker.seconds_into_market()
            secs_left = self.market_tracker.seconds_until_resolve()
            
            # 4. Aggregate signal
            self.signal_agg.add_prediction(direction, confidence, raw_prob, market)
            signal = self.signal_agg.get_signal()
            
            # 5. Log status periodically
            if self.inference_count % 10 == 0:
                bid_up = self.feature_engine.poly_state['p_up_bid']
                bid_dn = self.feature_engine.poly_state['p_down_bid']
                ask_up = self.feature_engine.poly_state['p_up_ask']
                ask_dn = self.feature_engine.poly_state['p_down_ask']
                btc = features.iloc[0].get('c', 0) if hasattr(features.iloc[0], 'get') else 0
                
                status = (f"[{self.inference_count:>5}] "
                         f"BTC=${list(self.feature_engine.bars_1s)[-1]['c']:.2f} | "
                         f"UP={bid_up:.3f}/{ask_up:.3f} DN={bid_dn:.3f}/{ask_dn:.3f} | "
                         f"Signal={direction} ({confidence:.3f}) | "
                         f"Market {secs_in}s in, {secs_left}s left | "
                         f"Bank=${self.execution.bankroll:.2f}")
                log.info(status)
            
            # 6. Check exits on existing positions
            self.execution.check_exits(
                self.feature_engine.poly_state['p_up_bid'],
                self.feature_engine.poly_state['p_down_bid'],
            )
            
            # 7. Check entry conditions
            if signal and self.execution.can_trade():
                if (signal['confidence'] >= self.config.min_confidence and
                    signal['consistency'] >= 0.60 and
                    secs_in >= self.config.min_seconds_into_market and
                    secs_in <= self.config.max_seconds_into_market):
                    
                    # Get entry price
                    if signal['direction'] == 'UP':
                        entry_ask = self.feature_engine.poly_state['p_up_ask']
                    else:
                        entry_ask = self.feature_engine.poly_state['p_down_ask']
                    
                    if 0 < entry_ask < 1:
                        log.info(f">>> SIGNAL FIRED: {signal['direction']} "
                                f"conf={signal['confidence']:.3f} "
                                f"consistency={signal['consistency']:.0%}")
                        
                        self.execution.enter_position(
                            market_slug=market,
                            side=signal['direction'],
                            confidence=signal['confidence'],
                            entry_ask=entry_ask,
                            market_end_ms=self.market_tracker.market_end_ms,
                        )
                        
                        # Reset signal aggregator after entry
                        self.signal_agg.predictions.clear()
        
        except Exception as e:
            log.error(f"Inference tick error: {e}", exc_info=True)
    
    def _shutdown(self):
        """Clean shutdown."""
        log.info("=" * 70)
        log.info(" SESSION SUMMARY")
        log.info("=" * 70)
        
        elapsed = time.time() - self.start_time
        log.info(f"  Runtime:      {elapsed/60:.1f} minutes")
        log.info(f"  Inferences:   {self.inference_count}")
        log.info(f"  Binance ticks: {self.binance_ws.trade_count}")
        log.info(f"  Poly ticks:    {self.poly_ws.tick_count}")
        
        # Close any open positions
        for pos in self.execution.positions:
            log.warning(f"  OPEN POSITION AT SHUTDOWN: {pos.market_slug} {pos.side}")
        
        total_trades = len(self.execution.closed_positions)
        if total_trades > 0:
            wins = sum(1 for p in self.execution.closed_positions if p.pnl and p.pnl > 0)
            total_pnl = sum(p.pnl for p in self.execution.closed_positions if p.pnl)
            log.info(f"  Total trades: {total_trades}")
            log.info(f"  Win rate:     {wins/total_trades:.1%}")
            log.info(f"  Total P&L:    ${total_pnl:+.2f}")
        
        log.info(f"  Final bank:   ${self.execution.bankroll:.2f}")
        
        # Save final state
        if self.execution.trade_log:
            with open('live_trades.json', 'w') as f:
                json.dump(self.execution.trade_log, f, indent=2, default=str)
            log.info("  Trade log saved to live_trades.json")

# ========================================================================
# ENTRY POINT
# ========================================================================

def main():
    parser = argparse.ArgumentParser(description='Polymarket BTC Live Trader')
    parser.add_argument('--live', action='store_true', help='Enable live trading (real orders)')
    parser.add_argument('--bankroll', type=float, default=100.0, help='Starting bankroll')
    parser.add_argument('--bet-size', type=float, default=0.05, help='Bet fraction (0.05 = 5%%)')
    parser.add_argument('--min-conf', type=float, default=0.60, help='Minimum confidence threshold')
    parser.add_argument('--strategy', choices=['HOLD_TO_RESOLVE', 'MOMENTUM'], 
                       default='HOLD_TO_RESOLVE', help='Trading strategy')
    parser.add_argument('--tp', type=float, default=0.10, help='Take-profit for momentum strategy')
    
    args = parser.parse_args()
    
    config = TradingConfig(
        initial_bankroll=args.bankroll,
        bet_fraction=args.bet_size,
        min_confidence=args.min_conf,
        strategy=args.strategy,
        momentum_tp=args.tp,
    )
    
    trader = LiveTrader(config, live_mode=args.live)
    trader.run()

if __name__ == '__main__':
    main()
