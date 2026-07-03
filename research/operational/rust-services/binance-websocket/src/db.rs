use crate::config::DATABASE_FILE;
use rusqlite::{Connection, Result};
use tracing::info;

pub fn init_database() -> Result<()> {
    let conn = Connection::open(DATABASE_FILE)?;

    // Enable WAL mode - 10x faster writes
    conn.execute_batch(
        "
        PRAGMA journal_mode=WAL;
        PRAGMA cache_size=10000;
        PRAGMA synchronous=OFF;
        PRAGMA temp_store=MEMORY;
    ",
    )?;

    // binance_trades
    conn.execute(
        "
        CREATE TABLE IF NOT EXISTS binance_trades (
            trade_id INTEGER PRIMARY KEY,
            trade_time INTEGER NOT NULL,
            price REAL NOT NULL,
            quantity REAL NOT NULL,
            quote_volume REAL NOT NULL,
            is_buyer_maker INTEGER,
            received_at INTEGER NOT NULL
        )
    ",
        [],
    )?;

    conn.execute(
        "
        CREATE INDEX IF NOT EXISTS idx_binance_trades_time 
        ON binance_trades(trade_time DESC)
    ",
        [],
    )?;

    // candles
    for interval in ["1s", "5s", "1m", "5m", "15m", "1h"] {
        conn.execute(
            &format!(
                "
            CREATE TABLE IF NOT EXISTS binance_candles_{} (
                candle_start INTEGER PRIMARY KEY,
                candle_end INTEGER NOT NULL,
                open_price REAL NOT NULL,
                high_price REAL NOT NULL,
                low_price REAL NOT NULL,
                close_price REAL NOT NULL,
                volume REAL NOT NULL,
                quote_volume REAL NOT NULL,
                trade_count INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            )
        ",
                interval
            ),
            [],
        )?;

        conn.execute(
            &format!(
                "
            CREATE INDEX IF NOT EXISTS idx_candles_{}_time 
            ON binance_candles_{}(candle_start DESC)
        ",
                interval, interval
            ),
            [],
        )?;
    }

    // crossover_alerts
    conn.execute(
        "
        CREATE TABLE IF NOT EXISTS crossover_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alert_type TEXT,
            timestamp INTEGER,
            trade_time INTEGER,
            price REAL,
            ma_25 REAL,
            signal TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ",
        [],
    )?;

    info!("Database initialized with WAL mode");
    Ok(())
}

pub fn get_db_conn() -> Result<Connection> {
    let conn = Connection::open(DATABASE_FILE)?;
    conn.pragma_update(None, "journal_mode", "WAL")?;
    conn.pragma_update(None, "synchronous", "NORMAL")?;
    conn.pragma_update(None, "cache_size", "10000")?;
    Ok(conn)
}
