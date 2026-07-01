"""
ML Bridge — Pipe integration for Rust WebSocket → Python ML → Rust Executor
============================================================================

Usage:
  ./rust_ws_ingest | python ml_bridge.py | ./rust_executor

  OR with TCP sockets:
  python ml_bridge.py --mode tcp --port 9999

Input format (JSON lines from Rust, one per line):
  {"type":"binance","T":1770895800123,"p":68150.25,"q":0.003,"m":false}
  {"type":"poly","side":"UP","price":0.45,"bid":0.44,"ask":0.46,"size":100.5,"ts":1770895800456,"slug":"btc-updown-15m-1770895800"}

Output format (JSON lines to Rust, one per line):
  {"direction":"DOWN","confidence":0.673,"raw_prob":0.327,"timestamp":1770895830000}
  {"action":"ENTER","side":"DOWN","confidence":0.71,"entry_ask":0.545,"market":"btc-updown-15m-1770895800","market_end_ms":1770896700000}
"""

import sys
import os
import json
import time
import argparse
import logging
import socket
import threading
from collections import deque

import numpy as np

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from live_trader import (
    FeatureEngine, EnsemblePredictor, SignalAggregator,
    MarketTracker, TradingConfig
)

# ========================================================================
# CONFIG
# ========================================================================

MIN_CONFIDENCE = float(os.getenv('MIN_CONFIDENCE', '0.60'))
MIN_CONSISTENCY = float(os.getenv('MIN_CONSISTENCY', '0.60'))
SIGNAL_WINDOW = int(os.getenv('SIGNAL_WINDOW', '30'))
BET_FRACTION = float(os.getenv('BET_FRACTION', '0.05'))
SLIPPAGE = float(os.getenv('SLIPPAGE', '0.005'))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-7s | %(message)s',
    handlers=[logging.FileHandler('ml_bridge.log', encoding='utf-8')],
)
log = logging.getLogger('MLBridge')

# ========================================================================
# CORE BRIDGE
# ========================================================================

class MLBridge:
    """
    Core bridge logic. Receives market data, produces trading signals.
    """
    
    def __init__(self):
        self.config = TradingConfig(
            min_confidence=MIN_CONFIDENCE,
            signal_window=SIGNAL_WINDOW,
        )
        self.predictor = EnsemblePredictor()
        self.feature_engine = FeatureEngine(self.config, self.predictor.feature_list)
        self.signal_agg = SignalAggregator(SIGNAL_WINDOW)
        self.market_tracker = MarketTracker()
        
        self.last_inference = 0
        self.inference_count = 0
        self.current_position = None  # Track if we've entered this market
        
        log.info(f"MLBridge initialized. {len(self.predictor.feature_list)} features.")
        log.info(f"Min confidence: {MIN_CONFIDENCE}, Window: {SIGNAL_WINDOW}")
    
    def process_message(self, msg: dict) -> list:
        """
        Process a single message. Returns list of output signals (may be empty).
        """
        outputs = []
        
        try:
            if msg.get('type') == 'binance':
                self.feature_engine.on_binance_trade(
                    trade_time_ms=int(msg['T']),
                    price=float(msg['p']),
                    qty=float(msg['q']),
                    is_buyer_maker=bool(msg['m']),
                )
            
            elif msg.get('type') == 'poly':
                self.feature_engine.on_polymarket_tick(
                    side=msg['side'].upper(),
                    price=float(msg['price']),
                    bid=float(msg['bid']),
                    ask=float(msg['ask']),
                    size=float(msg.get('size', 0)),
                    ts_ms=int(msg['ts']),
                )
                # Also track market
                if 'slug' in msg:
                    self.market_tracker.update_from_poly_message({'market': msg['slug']})
            
            # Rate-limit inference to once per second
            now = time.time()
            if now - self.last_inference < 0.9:
                return outputs
            self.last_inference = now
            
            # Compute features
            features = self.feature_engine.compute_features()
            if features is None:
                return outputs
            
            # Run prediction
            direction, confidence, raw_prob = self.predictor.predict(features)
            self.inference_count += 1
            
            # Get market info
            market = self.market_tracker.get_current_market()
            secs_in = self.market_tracker.seconds_into_market()
            secs_left = self.market_tracker.seconds_until_resolve()
            
            # Aggregate signal
            if market:
                self.signal_agg.add_prediction(direction, confidence, raw_prob, market)
            
            # Always emit a prediction signal
            pred_signal = {
                'type': 'prediction',
                'direction': direction,
                'confidence': round(float(confidence), 4),
                'raw_prob': round(float(raw_prob), 4),
                'timestamp': int(now * 1000),
                'market': market,
                'secs_in': secs_in,
                'secs_left': secs_left,
                'n': self.inference_count,
            }
            outputs.append(pred_signal)
            
            # Check for trade entry signal
            if market and self.current_position != market:
                agg_signal = self.signal_agg.get_signal()
                
                if (agg_signal and
                    agg_signal['confidence'] >= MIN_CONFIDENCE and
                    agg_signal['consistency'] >= MIN_CONSISTENCY and
                    30 <= secs_in <= 600):
                    
                    side = agg_signal['direction']
                    if side == 'UP':
                        entry_ask = self.feature_engine.poly_state['p_up_ask']
                    else:
                        entry_ask = self.feature_engine.poly_state['p_down_ask']
                    
                    if 0 < entry_ask < 1:
                        entry_signal = {
                            'type': 'entry',
                            'action': 'ENTER',
                            'side': side,
                            'confidence': round(float(agg_signal['confidence']), 4),
                            'consistency': round(float(agg_signal['consistency']), 4),
                            'entry_ask': round(float(entry_ask), 4),
                            'entry_price': round(float(min(entry_ask + SLIPPAGE, 0.99)), 4),
                            'market': market,
                            'market_end_ms': self.market_tracker.market_end_ms,
                            'bet_fraction': BET_FRACTION,
                            'timestamp': int(now * 1000),
                        }
                        outputs.append(entry_signal)
                        self.current_position = market  # Don't double-enter
                        self.signal_agg.predictions.clear()
                        
                        log.info(f"ENTRY SIGNAL: {side} on {market} "
                                f"conf={agg_signal['confidence']:.3f} "
                                f"ask={entry_ask:.3f}")
            
            # Check for market resolution (emit exit signal)
            if self.current_position and secs_left <= 0:
                up_bid = self.feature_engine.poly_state['p_up_bid']
                down_bid = self.feature_engine.poly_state['p_down_bid']
                
                exit_signal = {
                    'type': 'exit',
                    'action': 'RESOLVE',
                    'market': self.current_position,
                    'up_bid': round(float(up_bid), 4),
                    'down_bid': round(float(down_bid), 4),
                    'timestamp': int(now * 1000),
                }
                outputs.append(exit_signal)
                self.current_position = None
                log.info(f"EXIT SIGNAL: Market resolved")
            
            # Log periodically
            if self.inference_count % 30 == 0:
                btc = list(self.feature_engine.bars_1s)[-1]['c'] if self.feature_engine.bars_1s else 0
                log.info(f"[{self.inference_count}] BTC=${btc:.2f} "
                        f"Signal={direction}({confidence:.3f}) "
                        f"Market={market} {secs_in}s in")
        
        except Exception as e:
            log.error(f"Process error: {e}", exc_info=True)
        
        return outputs


# ========================================================================
# PIPE MODE (stdin/stdout)
# ========================================================================

def run_pipe():
    """Read JSON lines from stdin, write signals to stdout."""
    bridge = MLBridge()
    
    log.info("MLBridge running in PIPE mode (stdin -> stdout)")
    
    # Write a ready signal
    print(json.dumps({'type': 'ready', 'features': len(bridge.predictor.feature_list)}), flush=True)
    
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        
        outputs = bridge.process_message(msg)
        
        for out in outputs:
            print(json.dumps(out, default=str), flush=True)


# ========================================================================
# TCP SOCKET MODE
# ========================================================================

def run_tcp(host='127.0.0.1', port=9999):
    """Listen on TCP socket, read JSON lines, write signals back."""
    bridge = MLBridge()
    
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((host, port))
    server.listen(1)
    
    log.info(f"MLBridge running in TCP mode on {host}:{port}")
    print(f"ML Bridge listening on {host}:{port}", file=sys.stderr)
    
    while True:
        conn, addr = server.accept()
        log.info(f"Connection from {addr}")
        print(f"Connected: {addr}", file=sys.stderr)
        
        # Send ready
        conn.sendall(json.dumps({'type': 'ready', 'features': len(bridge.predictor.feature_list)}).encode() + b'\n')
        
        buffer = b""
        try:
            while True:
                data = conn.recv(8192)
                if not data:
                    break
                
                buffer += data
                while b'\n' in buffer:
                    line, buffer = buffer.split(b'\n', 1)
                    if not line.strip():
                        continue
                    
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    
                    outputs = bridge.process_message(msg)
                    
                    for out in outputs:
                        conn.sendall(json.dumps(out, default=str).encode() + b'\n')
        
        except (ConnectionResetError, BrokenPipeError):
            log.warning(f"Connection lost: {addr}")
        finally:
            conn.close()
            log.info(f"Disconnected: {addr}")


# ========================================================================
# ENTRY POINT
# ========================================================================

def main():
    parser = argparse.ArgumentParser(description='ML Bridge — Rust/Python integration')
    parser.add_argument('--mode', choices=['pipe', 'tcp'], default='pipe',
                       help='Communication mode')
    parser.add_argument('--host', default='127.0.0.1', help='TCP bind host')
    parser.add_argument('--port', type=int, default=9999, help='TCP bind port')
    
    args = parser.parse_args()
    
    if args.mode == 'tcp':
        run_tcp(args.host, args.port)
    else:
        run_pipe()

if __name__ == '__main__':
    main()
