# V10.0 Polars Performance Backtester (Rust)

This backtester is a high-performance Rust port of the V9.2 Regime-Aware strategy. It implement's the exact same logic but uses **Polars** for data processing and **Rayon** for multi-threaded market analysis, fulfilling the speed requirements identified in the "Backtester + Mojo Potential" analysis.

## Key Improvements
- **10-50x Faster**: Uses pure Rust and Polars for data ingestion and signal computation.
- **Parallelized**: Processes all 155+ markets in parallel using all available CPU cores.
- **Memory Efficient**: Optimized data structures and zero-copy filters.
- **Vectorized Data Prep**: 1-second bar building is significantly faster than pandas-based grouping.

## Implementation Details
- **Signal**: Brownian Drift (55%) + OFI Accel (30%) + Scoreboard (15%).
- **Regime Detection**: Path efficiency and autocorrelation gating.
- **Hour Blacklist**: ET hours {0, 9, 10, 15, 16} are blocked to match V9.2 calibration.
- **Entry Filters**: Max entry ask $0.55, Min Edge 0.08.

## How to Run

1.  Navigate to this directory:
    ```bash
    cd strategies/v10_0_polars_performance
    ```
2.  Run the backtester:
    ```bash
    cargo run --release -- --db-path ../../polymarket_btc_data.db
    ```

### Command Line Arguments
- `--db-path`: Path to the SQLite database (default: `polymarket_btc_data.db`).
- `--bankroll`: Initial $ (default: 100.0).
- `--bet-fraction`: Bet size (default: 0.05).
- `--min-confidence`: Confidence floor (default: 0.60).
- `--min-edge`: Required edge above entry price (default: 0.08).
- `--max-entry-price`: Max price for entry (default: 0.55).

## Expected Output
A terminal table summary of:
- Total Trades
- Win Rate
- Final Bankroll
- Total ROI
