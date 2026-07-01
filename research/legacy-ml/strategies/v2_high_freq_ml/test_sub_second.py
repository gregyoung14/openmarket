"""Quick test: 250ms and 500ms timeframes on the expanded Binance dataset."""
import sqlite3
import time
from high_freq_ml import (
    load_binance_trades, load_polymarket_ticks, load_lag_pairs,
    aggregate_binance, aggregate_polymarket, aggregate_lag_pairs,
    build_features, run_xgboost
)

DB_PATH = 'polymarket_btc_data.db'

def main():
    t0 = time.time()
    conn = sqlite3.connect(DB_PATH)
    
    df_trades = load_binance_trades(conn)
    df_poly = load_polymarket_ticks(conn)
    df_lag = load_lag_pairs(conn)
    conn.close()
    
    print(f"\nData loaded: {len(df_trades)} trades, {len(df_poly)} poly ticks, {len(df_lag)} lag pairs\n")
    
    timeframes = {'250ms': 250, '500ms': 500, '1s': 1000}
    results = {}
    
    for label, ms in timeframes.items():
        print(f"\n{'#'*70}")
        print(f"# {label}")
        print(f"{'#'*70}")
        
        try:
            df_b = aggregate_binance(df_trades, ms)
            df_p = aggregate_polymarket(df_poly, ms)
            df_l = aggregate_lag_pairs(df_lag, ms)
            
            df = df_b.copy()
            if not df_p.empty:
                df = df.join(df_p, how='left')
            if not df_l.empty:
                df = df.join(df_l, how='left')
            
            print(f"After merge: {len(df)} rows, {len(df.columns)} cols")
            
            df = build_features(df)
            print(f"After features: {len(df)} rows")
            
            r = run_xgboost(df, label)
            if r:
                results[label] = r['acc']
        except Exception as e:
            print(f"ERROR: {e}")
            import traceback
            traceback.print_exc()
    
    elapsed = time.time() - t0
    print(f"\n\n{'='*70}")
    print(f" RESULTS  ({elapsed:.0f}s)")
    print(f"{'='*70}")
    for l in sorted(results, key=results.get, reverse=True):
        a = results[l]
        bar = '█' * int(a * 60)
        s = '🚀' if a > 0.60 else '🔥' if a > 0.55 else '⚡' if a > 0.53 else ''
        print(f"  {l:>5s}: {a:.4f} {bar} {s}")
    
    if results:
        print(f"\n  Best: {max(results, key=results.get)} at {max(results.values()):.4f}")

if __name__ == "__main__":
    main()
