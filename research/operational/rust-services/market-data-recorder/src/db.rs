use rusqlite::{params, Connection, OptionalExtension, Result};
use std::fs;

use crate::config;
use crate::models::{BinanceTick, MarketMeta, NearestBinance, PolyUnpaired, PolymarketTick};

pub fn init_database() -> Result<()> {
    let conn = Connection::open(config::database_file())?;

    conn.execute_batch(
        "
        PRAGMA journal_mode=WAL;
        PRAGMA cache_size=10000;
        PRAGMA synchronous=OFF;
        PRAGMA temp_store=MEMORY;
    ",
    )?;

    conn.execute_batch(
        "
        CREATE TABLE IF NOT EXISTS binance_ticks_ms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_ts_ms INTEGER NOT NULL,
            ingest_ts_ms INTEGER NOT NULL,
            trade_time_ms INTEGER NOT NULL,
            price REAL NOT NULL,
            volume REAL NOT NULL,
            raw_json TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_binance_ticks_source_ts ON binance_ticks_ms(source_ts_ms);
        CREATE INDEX IF NOT EXISTS idx_binance_ticks_ingest_ts ON binance_ticks_ms(ingest_ts_ms);

        CREATE TABLE IF NOT EXISTS polymarket_ticks_ms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_ts_ms INTEGER NOT NULL,
            ingest_ts_ms INTEGER NOT NULL,
            market_slug TEXT,
            asset_id TEXT NOT NULL,
            side_label TEXT NOT NULL,
            event_type TEXT NOT NULL,
            price REAL,
            best_bid REAL,
            best_ask REAL,
            size REAL,
            paired INTEGER NOT NULL DEFAULT 0,
            raw_json TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_polymarket_ticks_source_ts ON polymarket_ticks_ms(source_ts_ms);
        CREATE INDEX IF NOT EXISTS idx_polymarket_ticks_ingest_ts ON polymarket_ticks_ms(ingest_ts_ms);
        CREATE INDEX IF NOT EXISTS idx_polymarket_ticks_asset ON polymarket_ticks_ms(asset_id);
        CREATE INDEX IF NOT EXISTS idx_polymarket_ticks_unpaired ON polymarket_ticks_ms(paired, source_ts_ms);

        CREATE TABLE IF NOT EXISTS market_meta (
            market_slug TEXT PRIMARY KEY,
            question TEXT NOT NULL,
            up_token_id TEXT NOT NULL,
            down_token_id TEXT NOT NULL,
            up_price REAL NOT NULL,
            down_price REAL NOT NULL,
            first_seen_ms INTEGER NOT NULL,
            last_seen_ms INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS lag_pairs_ms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            paired_at_ms INTEGER NOT NULL,
            market_slug TEXT,
            side_label TEXT NOT NULL,
            binance_tick_id INTEGER NOT NULL,
            polymarket_tick_id INTEGER NOT NULL,
            binance_source_ts_ms INTEGER NOT NULL,
            polymarket_source_ts_ms INTEGER NOT NULL,
            lead_lag_ms INTEGER NOT NULL,
            binance_price REAL NOT NULL,
            polymarket_bid REAL NOT NULL,
            price_delta_bps REAL NOT NULL,
            quality_flag TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_lag_pairs_time ON lag_pairs_ms(paired_at_ms);
        CREATE INDEX IF NOT EXISTS idx_lag_pairs_lag ON lag_pairs_ms(lead_lag_ms);
        ",
    )?;

    let _ = fs::create_dir_all(config::export_dir());
    Ok(())
}

pub fn get_db_conn() -> Result<Connection> {
    let conn = Connection::open(config::database_file())?;
    conn.pragma_update(None, "journal_mode", "WAL")?;
    conn.pragma_update(None, "synchronous", "NORMAL")?;
    conn.pragma_update(None, "cache_size", "10000")?;
    Ok(conn)
}

pub fn insert_binance_ticks(conn: &mut Connection, rows: &[BinanceTick]) -> Result<usize> {
    if rows.is_empty() {
        return Ok(0);
    }
    let tx = conn.transaction()?;
    let mut stmt = tx.prepare(
        "INSERT INTO binance_ticks_ms (source_ts_ms, ingest_ts_ms, trade_time_ms, price, volume, raw_json)
         VALUES (?, ?, ?, ?, ?, ?)",
    )?;
    for r in rows {
        stmt.execute(params![
            r.source_ts_ms,
            r.ingest_ts_ms,
            r.trade_time_ms,
            r.price,
            r.volume,
            r.raw_json
        ])?;
    }
    drop(stmt);
    tx.commit()?;
    Ok(rows.len())
}

pub fn insert_polymarket_ticks(conn: &mut Connection, rows: &[PolymarketTick]) -> Result<usize> {
    if rows.is_empty() {
        return Ok(0);
    }
    let tx = conn.transaction()?;
    let mut stmt = tx.prepare(
        "INSERT INTO polymarket_ticks_ms
         (source_ts_ms, ingest_ts_ms, market_slug, asset_id, side_label, event_type, price, best_bid, best_ask, size, raw_json)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
    )?;
    for r in rows {
        stmt.execute(params![
            r.source_ts_ms,
            r.ingest_ts_ms,
            r.market_slug,
            r.asset_id,
            r.side_label,
            r.event_type,
            r.price,
            r.best_bid,
            r.best_ask,
            r.size,
            r.raw_json
        ])?;
    }
    drop(stmt);
    tx.commit()?;
    Ok(rows.len())
}

pub fn upsert_market_meta(conn: &mut Connection, rows: &[MarketMeta]) -> Result<usize> {
    if rows.is_empty() {
        return Ok(0);
    }
    let tx = conn.transaction()?;
    let mut stmt = tx.prepare(
        "INSERT INTO market_meta
         (market_slug, question, up_token_id, down_token_id, up_price, down_price, first_seen_ms, last_seen_ms)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?)
         ON CONFLICT(market_slug) DO UPDATE SET
            question=excluded.question,
            up_token_id=excluded.up_token_id,
            down_token_id=excluded.down_token_id,
            up_price=excluded.up_price,
            down_price=excluded.down_price,
            last_seen_ms=excluded.last_seen_ms",
    )?;

    for r in rows {
        stmt.execute(params![
            r.market_slug,
            r.question,
            r.up_token_id,
            r.down_token_id,
            r.up_price,
            r.down_price,
            r.first_seen_ms,
            r.last_seen_ms
        ])?;
    }
    drop(stmt);
    tx.commit()?;
    Ok(rows.len())
}

pub fn fetch_unpaired_polymarket(conn: &Connection, limit: usize) -> Result<Vec<PolyUnpaired>> {
    let mut stmt = conn.prepare(
        "SELECT id, source_ts_ms, market_slug, side_label, best_bid
         FROM polymarket_ticks_ms
         WHERE paired = 0 AND best_bid IS NOT NULL
         ORDER BY source_ts_ms ASC
         LIMIT ?",
    )?;

    let rows = stmt
        .query_map([limit as i64], |row| {
            Ok(PolyUnpaired {
                id: row.get(0)?,
                source_ts_ms: row.get(1)?,
                market_slug: row.get(2)?,
                side_label: row.get(3)?,
                best_bid: row.get(4)?,
            })
        })?
        .collect::<Result<Vec<_>, _>>()?;

    Ok(rows)
}

pub fn find_nearest_binance(
    conn: &Connection,
    ts_ms: i64,
    window_ms: i64,
) -> Result<Option<NearestBinance>> {
    let mut stmt = conn.prepare(
        "SELECT id, source_ts_ms, price
         FROM binance_ticks_ms
         WHERE source_ts_ms BETWEEN ? AND ?
         ORDER BY ABS(source_ts_ms - ?) ASC
         LIMIT 1",
    )?;

    stmt.query_row([ts_ms - window_ms, ts_ms + window_ms, ts_ms], |row| {
        Ok(NearestBinance {
            id: row.get(0)?,
            source_ts_ms: row.get(1)?,
            price: row.get(2)?,
        })
    })
    .optional()
}

#[allow(clippy::too_many_arguments)]
pub fn insert_lag_pair(
    conn: &Connection,
    paired_at_ms: i64,
    market_slug: Option<&str>,
    side_label: &str,
    binance_tick_id: i64,
    polymarket_tick_id: i64,
    binance_source_ts_ms: i64,
    polymarket_source_ts_ms: i64,
    lead_lag_ms: i64,
    binance_price: f64,
    polymarket_bid: f64,
    price_delta_bps: f64,
    quality_flag: &str,
) -> Result<()> {
    conn.execute(
        "INSERT INTO lag_pairs_ms
        (paired_at_ms, market_slug, side_label, binance_tick_id, polymarket_tick_id,
         binance_source_ts_ms, polymarket_source_ts_ms, lead_lag_ms, binance_price,
         polymarket_bid, price_delta_bps, quality_flag)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        params![
            paired_at_ms,
            market_slug,
            side_label,
            binance_tick_id,
            polymarket_tick_id,
            binance_source_ts_ms,
            polymarket_source_ts_ms,
            lead_lag_ms,
            binance_price,
            polymarket_bid,
            price_delta_bps,
            quality_flag,
        ],
    )?;
    Ok(())
}

pub fn mark_polymarket_paired(conn: &Connection, id: i64) -> Result<()> {
    conn.execute(
        "UPDATE polymarket_ticks_ms SET paired = 1 WHERE id = ?",
        params![id],
    )?;
    Ok(())
}

pub fn get_latest_market_meta(conn: &Connection) -> Result<Option<MarketMeta>> {
    let mut stmt = conn.prepare(
        "SELECT market_slug, question, up_token_id, down_token_id, up_price, down_price, first_seen_ms, last_seen_ms
         FROM market_meta ORDER BY last_seen_ms DESC LIMIT 1",
    )?;

    stmt.query_row([], |row| {
        Ok(MarketMeta {
            market_slug: row.get(0)?,
            question: row.get(1)?,
            up_token_id: row.get(2)?,
            down_token_id: row.get(3)?,
            up_price: row.get(4)?,
            down_price: row.get(5)?,
            first_seen_ms: row.get(6)?,
            last_seen_ms: row.get(7)?,
        })
    })
    .optional()
}

/// Load ALL market_meta rows — used to pre-populate the asset_map on startup
pub fn get_all_market_meta(conn: &Connection) -> Result<Vec<MarketMeta>> {
    let mut stmt = conn.prepare(
        "SELECT market_slug, question, up_token_id, down_token_id, up_price, down_price, first_seen_ms, last_seen_ms
         FROM market_meta ORDER BY first_seen_ms ASC",
    )?;

    let rows = stmt
        .query_map([], |row| {
            Ok(MarketMeta {
                market_slug: row.get(0)?,
                question: row.get(1)?,
                up_token_id: row.get(2)?,
                down_token_id: row.get(3)?,
                up_price: row.get(4)?,
                down_price: row.get(5)?,
                first_seen_ms: row.get(6)?,
                last_seen_ms: row.get(7)?,
            })
        })?
        .collect::<Result<Vec<_>, _>>()?;

    Ok(rows)
}
