import sqlite3
import pandas as pd
from datetime import datetime

DB_PATH = 'polymarket_btc_data.db'

def get_stats():
    try:
        conn = sqlite3.connect(DB_PATH)
        
        # Table counts
        ticks_count = conn.execute("SELECT count(*) FROM polymarket_ticks_ms").fetchone()[0]
        trades_count = conn.execute("SELECT count(*) FROM binance_trades").fetchone()[0]
        meta_count = conn.execute("SELECT count(*) FROM market_meta").fetchone()[0]
        lag_count = conn.execute("SELECT count(*) FROM lag_pairs_ms").fetchone()[0]
        
        # Time ranges
        ticks_min, ticks_max = conn.execute("SELECT min(source_ts_ms), max(source_ts_ms) FROM polymarket_ticks_ms").fetchone()
        trades_min, trades_max = conn.execute("SELECT min(trade_time), max(trade_time) FROM binance_trades").fetchone()
        
        conn.close()
        
        print(f"Polymarket Ticks: {ticks_count:,}")
        print(f"Binance Trades: {trades_count:,}")
        print(f"Market Meta: {meta_count:,}")
        print(f"Lag Pairs: {lag_count:,}")
        
        if ticks_min:
            print(f"Ticks Time Range: {datetime.fromtimestamp(ticks_min/1000)} to {datetime.fromtimestamp(ticks_max/1000)}")
        if trades_min:
            print(f"Trades Time Range: {datetime.fromtimestamp(trades_min/1000)} to {datetime.fromtimestamp(trades_max/1000)}")
            
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    get_stats()
