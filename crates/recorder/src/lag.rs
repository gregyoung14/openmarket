use anyhow::Result;
use csv::Writer;
use serde::Serialize;
use serde_json::json;
use signal_engine::calibrated::{
    build_1s_bars_from_arrays, build_calibrated_feature_snapshot, build_raw_1s_arrays,
    DEFAULT_FEATURE_NAMES,
};
use signal_engine::drift::compute_drift_signal_v14;
use signal_engine::models::{BinanceTrade as SignalBinanceTrade, MarketInfo as SignalMarketInfo};
use std::collections::{BTreeMap, HashMap};
use std::fs;
use std::time::{SystemTime, UNIX_EPOCH};
use tokio::time::{sleep, Duration};
use tracing::error;

use crate::config;
use crate::db;
use crate::services::AppState;

fn now_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_millis() as i64
}

pub fn spawn_lag_pairing_task(state: AppState) {
    tokio::spawn(async move {
        loop {
            if let Err(e) = pair_once(&state).await {
                error!("lag pair task error: {}", e);
            }
            sleep(Duration::from_millis(config::LAG_LOOP_MS)).await;
        }
    });
}

async fn pair_once(state: &AppState) -> Result<()> {
    let (paired_count,): (u64,) = tokio::task::spawn_blocking(move || {
        let conn = db::get_db_conn()?;
        let candidates = db::fetch_unpaired_polymarket(&conn, config::LAG_FETCH_BATCH)?;
        let mut written = 0u64;

        for p in candidates {
            if let Some(b) = db::find_nearest_binance(&conn, p.source_ts_ms, config::LAG_WINDOW_MS)?
            {
                let lag_ms = p.source_ts_ms - b.source_ts_ms;
                let delta_bps = if b.price > 0.0 {
                    ((p.best_bid - b.price) / b.price) * 10_000.0
                } else {
                    0.0
                };

                let quality = if lag_ms.abs() <= 100 {
                    "tight"
                } else if lag_ms.abs() <= 300 {
                    "medium"
                } else {
                    "wide"
                };

                db::insert_lag_pair(
                    &conn,
                    now_ms(),
                    p.market_slug.as_deref(),
                    &p.side_label,
                    b.id,
                    p.id,
                    b.source_ts_ms,
                    p.source_ts_ms,
                    lag_ms,
                    b.price,
                    p.best_bid,
                    delta_bps,
                    quality,
                )?;
                db::mark_polymarket_paired(&conn, p.id)?;
                written += 1;
            }
        }

        Ok::<(u64,), anyhow::Error>((written,))
    })
    .await??;

    if paired_count > 0 {
        state.inc_lag_pairs(paired_count);
    }

    Ok(())
}

pub fn export_step1_csv() -> Result<String> {
    let export_dir = config::export_dir();
    fs::create_dir_all(&export_dir)?;
    let path = format!("{}/step1_lag_pairs_{}.csv", export_dir, now_ms());
    let conn = db::get_db_conn()?;

    let mut wtr = Writer::from_path(&path)?;
    wtr.write_record([
        "paired_at_ms",
        "market_slug",
        "side_label",
        "lead_lag_ms",
        "binance_price",
        "polymarket_bid",
        "price_delta_bps",
        "quality_flag",
    ])?;

    let mut stmt = conn.prepare(
        "SELECT paired_at_ms, COALESCE(market_slug,''), side_label, lead_lag_ms,
                binance_price, polymarket_bid, price_delta_bps, quality_flag
         FROM lag_pairs_ms
         ORDER BY paired_at_ms DESC
         LIMIT 200000",
    )?;

    let rows = stmt.query_map([], |row| {
        Ok((
            row.get::<_, i64>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, String>(2)?,
            row.get::<_, i64>(3)?,
            row.get::<_, f64>(4)?,
            row.get::<_, f64>(5)?,
            row.get::<_, f64>(6)?,
            row.get::<_, String>(7)?,
        ))
    })?;

    for r in rows {
        let (a, b, c, d, e, f, g, h) = r?;
        wtr.write_record([
            a.to_string(),
            b,
            c,
            d.to_string(),
            format!("{:.8}", e),
            format!("{:.8}", f),
            format!("{:.6}", g),
            h,
        ])?;
    }

    wtr.flush()?;
    Ok(path)
}

pub fn export_step2_features_csv() -> Result<String> {
    let export_dir = config::export_dir();
    fs::create_dir_all(&export_dir)?;
    let path = format!("{}/step2_features_15m_{}.csv", export_dir, now_ms());

    let conn = db::get_db_conn()?;
    let mut stmt = conn.prepare(
        "SELECT source_ts_ms, price, volume
         FROM binance_ticks_ms
         WHERE source_ts_ms > ?
         ORDER BY source_ts_ms ASC",
    )?;

    let lookback_ms = 7 * 24 * 60 * 60 * 1000_i64;
    let start = now_ms() - lookback_ms;

    #[derive(Clone, Debug)]
    struct Trade {
        ts: i64,
        price: f64,
        volume: f64,
    }

    let ticks: Vec<Trade> = stmt
        .query_map([start], |row| {
            Ok(Trade {
                ts: row.get::<_, i64>(0)?,
                price: row.get::<_, f64>(1)?,
                volume: row.get::<_, f64>(2)?,
            })
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?;

    let mut p_stmt = conn.prepare(
        "SELECT source_ts_ms, side_label, best_bid, best_ask
         FROM polymarket_ticks_ms
         WHERE source_ts_ms > ? AND side_label IN ('UP','DOWN')
         ORDER BY source_ts_ms ASC",
    )?;

    #[derive(Clone, Debug)]
    struct PTick {
        ts: i64,
        side: String,
        best_bid: Option<f64>,
        best_ask: Option<f64>,
    }

    let p_ticks: Vec<PTick> = p_stmt
        .query_map([start], |row| {
            Ok(PTick {
                ts: row.get::<_, i64>(0)?,
                side: row.get::<_, String>(1)?,
                best_bid: row.get::<_, Option<f64>>(2)?,
                best_ask: row.get::<_, Option<f64>>(3)?,
            })
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?;

    #[derive(Clone)]
    #[allow(dead_code)]
    struct Candle {
        ts: i64,
        open: f64,
        high: f64,
        low: f64,
        close: f64,
        volume: f64,
        quote_volume: f64,
        trade_count: u64,
        buy_quote_volume: f64,
        sell_quote_volume: f64,
        buy_qty: f64,
        sell_qty: f64,
        vwap: f64,
        ofi: f64,
        burstiness: f64,
        rv_1s: f64,
        rv_5s: f64,
        range_pct: f64,
        close_in_range: f64,
        vwap_dist_close: f64,
        up_bid_last: Option<f64>,
        up_ask_last: Option<f64>,
        down_bid_last: Option<f64>,
        down_ask_last: Option<f64>,
        up_spread: Option<f64>,
        down_spread: Option<f64>,
        sum_bid: Option<f64>,
        sum_ask: Option<f64>,
        mid_up: Option<f64>,
        mid_down: Option<f64>,
        lag_mean_ms: f64,
        lag_abs_mean_ms: f64,
        up_bid_drift_5s: Option<f64>,
        up_bid_drift_30s: Option<f64>,
        up_bid_drift_120s: Option<f64>,
        down_bid_drift_5s: Option<f64>,
        down_bid_drift_30s: Option<f64>,
        down_bid_drift_120s: Option<f64>,
    }

    #[derive(Default)]
    struct CandleAcc {
        open: Option<f64>,
        high: f64,
        low: f64,
        close: f64,
        volume: f64,
        quote_volume: f64,
        trade_count: u64,
        buy_quote_volume: f64,
        sell_quote_volume: f64,
        buy_qty: f64,
        sell_qty: f64,
        sec_trade_count: HashMap<i64, u64>,
        sec_close: HashMap<i64, f64>,
        sec5_close: HashMap<i64, f64>,
    }

    let mut acc_map: BTreeMap<i64, CandleAcc> = BTreeMap::new();

    let mut prev_price: Option<f64> = None;
    for t in &ticks {
        let bucket = (t.ts / 900000) * 900000;
        let sec_bucket = (t.ts / 1000) * 1000;
        let sec5_bucket = (t.ts / 5000) * 5000;
        let a = acc_map.entry(bucket).or_default();
        let qv = t.price * t.volume;

        if a.open.is_none() {
            a.open = Some(t.price);
            a.high = t.price;
            a.low = t.price;
        }
        a.high = a.high.max(t.price);
        a.low = a.low.min(t.price);
        a.close = t.price;
        a.volume += t.volume;
        a.quote_volume += qv;
        a.trade_count += 1;

        // Tick-rule proxy for aggressor side when side flags are unavailable.
        if let Some(pp) = prev_price {
            if t.price >= pp {
                a.buy_quote_volume += qv;
                a.buy_qty += t.volume;
            } else {
                a.sell_quote_volume += qv;
                a.sell_qty += t.volume;
            }
        } else {
            a.buy_quote_volume += qv;
            a.buy_qty += t.volume;
        }
        prev_price = Some(t.price);

        *a.sec_trade_count.entry(sec_bucket).or_insert(0) += 1;
        a.sec_close.insert(sec_bucket, t.price);
        a.sec5_close.insert(sec5_bucket, t.price);
    }

    // Polymarket per-side time series for response drifts
    let mut up_bid_series: Vec<(i64, f64)> = Vec::new();
    let mut down_bid_series: Vec<(i64, f64)> = Vec::new();

    #[derive(Default, Clone)]
    struct PolyAgg {
        up_bid_last: Option<f64>,
        up_ask_last: Option<f64>,
        down_bid_last: Option<f64>,
        down_ask_last: Option<f64>,
    }
    let mut poly_map: BTreeMap<i64, PolyAgg> = BTreeMap::new();

    for p in &p_ticks {
        let bucket = (p.ts / 900000) * 900000;
        let pa = poly_map.entry(bucket).or_default();
        match p.side.as_str() {
            "UP" => {
                if let Some(b) = p.best_bid {
                    pa.up_bid_last = Some(b);
                    up_bid_series.push((p.ts, b));
                }
                if let Some(a) = p.best_ask {
                    pa.up_ask_last = Some(a);
                }
            }
            "DOWN" => {
                if let Some(b) = p.best_bid {
                    pa.down_bid_last = Some(b);
                    down_bid_series.push((p.ts, b));
                }
                if let Some(a) = p.best_ask {
                    pa.down_ask_last = Some(a);
                }
            }
            _ => {}
        }
    }

    // Lag stats per 15m bucket from lag_pairs_ms
    let mut lag_stmt = conn.prepare(
        "SELECT polymarket_source_ts_ms, lead_lag_ms FROM lag_pairs_ms WHERE polymarket_source_ts_ms > ?",
    )?;
    let lag_rows = lag_stmt
        .query_map([start], |row| {
            Ok((row.get::<_, i64>(0)?, row.get::<_, i64>(1)?))
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    let mut lag_map: HashMap<i64, Vec<i64>> = HashMap::new();
    for (ts, lag) in lag_rows {
        lag_map.entry((ts / 900000) * 900000).or_default().push(lag);
    }

    let mut candles: Vec<Candle> = Vec::new();
    for (bucket, a) in acc_map {
        let open = a.open.unwrap_or(a.close);
        let vwap = if a.volume > 0.0 {
            a.quote_volume / a.volume
        } else {
            a.close
        };
        let ofi = if (a.buy_quote_volume + a.sell_quote_volume) > 0.0 {
            (a.buy_quote_volume - a.sell_quote_volume) / (a.buy_quote_volume + a.sell_quote_volume)
        } else {
            0.0
        };

        let mut sec_counts: Vec<u64> = a.sec_trade_count.values().copied().collect();
        sec_counts.sort_unstable();
        let max_tps = sec_counts.last().copied().unwrap_or(0) as f64;
        let avg_tps = if sec_counts.is_empty() {
            0.0
        } else {
            sec_counts.iter().map(|x| *x as f64).sum::<f64>() / sec_counts.len() as f64
        };
        let burstiness = if avg_tps > 0.0 {
            max_tps / avg_tps
        } else {
            0.0
        };

        let rv_1s = realized_vol_from_map(&a.sec_close);
        let rv_5s = realized_vol_from_map(&a.sec5_close);

        let range_pct = if open > 0.0 {
            (a.high - a.low) / open
        } else {
            0.0
        };
        let close_in_range = if (a.high - a.low).abs() > f64::EPSILON {
            (a.close - a.low) / (a.high - a.low)
        } else {
            0.5
        };
        let vwap_dist_close = if vwap > 0.0 {
            (a.close - vwap) / vwap
        } else {
            0.0
        };

        let p = poly_map.get(&bucket).cloned().unwrap_or_default();
        let up_spread = spread_opt(p.up_bid_last, p.up_ask_last);
        let down_spread = spread_opt(p.down_bid_last, p.down_ask_last);
        let sum_bid = sum_opt(p.up_bid_last, p.down_bid_last);
        let sum_ask = sum_opt(p.up_ask_last, p.down_ask_last);
        let mid_up = mid_opt(p.up_bid_last, p.up_ask_last);
        let mid_down = mid_opt(p.down_bid_last, p.down_ask_last);

        let bucket_end = bucket + 899_999;
        let up_bid_drift_5s = drift_at(&up_bid_series, bucket_end, 5_000);
        let up_bid_drift_30s = drift_at(&up_bid_series, bucket_end, 30_000);
        let up_bid_drift_120s = drift_at(&up_bid_series, bucket_end, 120_000);
        let down_bid_drift_5s = drift_at(&down_bid_series, bucket_end, 5_000);
        let down_bid_drift_30s = drift_at(&down_bid_series, bucket_end, 30_000);
        let down_bid_drift_120s = drift_at(&down_bid_series, bucket_end, 120_000);

        let (lag_mean_ms, lag_abs_mean_ms) = if let Some(v) = lag_map.get(&bucket) {
            let mean = v.iter().map(|x| *x as f64).sum::<f64>() / v.len() as f64;
            let abs_mean = v.iter().map(|x| x.abs() as f64).sum::<f64>() / v.len() as f64;
            (mean, abs_mean)
        } else {
            (0.0, 0.0)
        };

        candles.push(Candle {
            ts: bucket,
            open,
            high: a.high,
            low: a.low,
            close: a.close,
            volume: a.volume,
            quote_volume: a.quote_volume,
            trade_count: a.trade_count,
            buy_quote_volume: a.buy_quote_volume,
            sell_quote_volume: a.sell_quote_volume,
            buy_qty: a.buy_qty,
            sell_qty: a.sell_qty,
            vwap,
            ofi,
            burstiness,
            rv_1s,
            rv_5s,
            range_pct,
            close_in_range,
            vwap_dist_close,
            up_bid_last: p.up_bid_last,
            up_ask_last: p.up_ask_last,
            down_bid_last: p.down_bid_last,
            down_ask_last: p.down_ask_last,
            up_spread,
            down_spread,
            sum_bid,
            sum_ask,
            mid_up,
            mid_down,
            lag_mean_ms,
            lag_abs_mean_ms,
            up_bid_drift_5s,
            up_bid_drift_30s,
            up_bid_drift_120s,
            down_bid_drift_5s,
            down_bid_drift_30s,
            down_bid_drift_120s,
        });
    }

    candles.sort_by_key(|c| c.ts);

    let mut wtr = Writer::from_path(&path)?;
    wtr.write_record([
        "ts_ms",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "quote_volume",
        "trade_count",
        "buy_quote_volume",
        "sell_quote_volume",
        "buy_sell_imbalance",
        "vwap",
        "vwap_dist_close",
        "burstiness",
        "rv_1s",
        "rv_5s",
        "range_pct",
        "close_in_range",
        "ret_1",
        "ret_2",
        "ret_4",
        "roc_3",
        "roc_6",
        "mom_3",
        "mom_6",
        "ema_9",
        "ema_21",
        "ema_50",
        "ema_12",
        "ema_26",
        "ema_slope_9",
        "ema_slope_21",
        "macd_line",
        "macd_signal",
        "macd_hist",
        "ema_cross",
        "rsi_14",
        "rsi_30",
        "stoch_k_14",
        "stoch_d_3",
        "cci_20",
        "atr_14",
        "bb_width_20",
        "bb_squeeze_20",
        "volume_z_20",
        "quote_volume_z_20",
        "obv",
        "obv_delta",
        "up_bid_last",
        "up_ask_last",
        "down_bid_last",
        "down_ask_last",
        "up_spread",
        "down_spread",
        "sum_bid",
        "sum_ask",
        "mid_up",
        "mid_down",
        "lag_mean_ms",
        "lag_abs_mean_ms",
        "up_bid_drift_5s",
        "up_bid_drift_30s",
        "up_bid_drift_120s",
        "down_bid_drift_5s",
        "down_bid_drift_30s",
        "down_bid_drift_120s",
        "target_next_up",
        "target_next2_up",
        "target_continuation_strong",
        "target_up_bid_next_up",
        "target_down_bid_next_up",
        "target_edge_up",
        "target_edge_down",
        "split_is_train",
        "train_end_ts",
        "test_start_ts",
    ])?;

    if candles.is_empty() {
        wtr.flush()?;
        return Ok(path);
    }

    let mut ema9 = candles[0].close;
    let mut ema21 = candles[0].close;
    let mut ema50 = candles[0].close;
    let mut ema12 = candles[0].close;
    let mut ema26 = candles[0].close;
    let mut macd_signal = 0.0;
    let mut obv = 0.0;

    let mut avg_gain_14 = 0.0;
    let mut avg_loss_14 = 0.0;
    let mut avg_gain_30 = 0.0;
    let mut avg_loss_30 = 0.0;

    let mut prev_ema9 = ema9;
    let mut prev_ema21 = ema21;

    let split_idx = ((candles.len() as f64) * 0.8).floor() as usize;
    let train_end_ts = candles
        .get(split_idx.saturating_sub(1))
        .map(|c| c.ts)
        .unwrap_or(candles[0].ts);
    let test_start_ts = candles
        .get(split_idx)
        .map(|c| c.ts)
        .unwrap_or(candles.last().map(|c| c.ts).unwrap_or(candles[0].ts));

    let k9 = 2.0 / 10.0;
    let k21 = 2.0 / 22.0;
    let k50 = 2.0 / 51.0;
    let k12 = 2.0 / 13.0;
    let k26 = 2.0 / 27.0;
    let ksig = 2.0 / 10.0;

    let closes: Vec<f64> = candles.iter().map(|c| c.close).collect();
    let highs: Vec<f64> = candles.iter().map(|c| c.high).collect();
    let lows: Vec<f64> = candles.iter().map(|c| c.low).collect();
    let volumes: Vec<f64> = candles.iter().map(|c| c.volume).collect();
    let qvols: Vec<f64> = candles.iter().map(|c| c.quote_volume).collect();

    let mut tr_vec: Vec<f64> = Vec::with_capacity(candles.len());

    for i in 0..candles.len() {
        let c = &candles[i];
        if i > 0 {
            prev_ema9 = ema9;
            prev_ema21 = ema21;
        }

        ema9 = c.close * k9 + ema9 * (1.0 - k9);
        ema21 = c.close * k21 + ema21 * (1.0 - k21);
        ema50 = c.close * k50 + ema50 * (1.0 - k50);
        ema12 = c.close * k12 + ema12 * (1.0 - k12);
        ema26 = c.close * k26 + ema26 * (1.0 - k26);
        let macd_line = ema12 - ema26;
        macd_signal = macd_line * ksig + macd_signal * (1.0 - ksig);
        let macd_hist = macd_line - macd_signal;

        let ret_1 = pct_ret(&closes, i, 1);
        let ret_2 = pct_ret(&closes, i, 2);
        let ret_4 = pct_ret(&closes, i, 4);
        let roc_3 = pct_ret(&closes, i, 3);
        let roc_6 = pct_ret(&closes, i, 6);
        let mom_3 = diff_n(&closes, i, 3);
        let mom_6 = diff_n(&closes, i, 6);

        let ema_slope_9 = ema9 - prev_ema9;
        let ema_slope_21 = ema21 - prev_ema21;

        // RSI (Wilder)
        let chg = if i > 0 { c.close - closes[i - 1] } else { 0.0 };
        let gain = chg.max(0.0);
        let loss = (-chg).max(0.0);
        if i == 1 {
            avg_gain_14 = gain;
            avg_loss_14 = loss;
            avg_gain_30 = gain;
            avg_loss_30 = loss;
        } else if i > 1 {
            avg_gain_14 = (avg_gain_14 * 13.0 + gain) / 14.0;
            avg_loss_14 = (avg_loss_14 * 13.0 + loss) / 14.0;
            avg_gain_30 = (avg_gain_30 * 29.0 + gain) / 30.0;
            avg_loss_30 = (avg_loss_30 * 29.0 + loss) / 30.0;
        }
        let rsi_14 = rsi_from_avg(avg_gain_14, avg_loss_14);
        let rsi_30 = rsi_from_avg(avg_gain_30, avg_loss_30);

        let stoch_k = stoch_k_lookback(&highs, &lows, &closes, i, 14);
        let stoch_d = sma_last(
            &(0..=i)
                .map(|j| stoch_k_lookback(&highs, &lows, &closes, j, 14))
                .collect::<Vec<_>>(),
            i,
            3,
        );

        let cci_20 = cci_lookback(&highs, &lows, &closes, i, 20);

        // ATR
        let tr = if i == 0 {
            c.high - c.low
        } else {
            let prev_close = closes[i - 1];
            (c.high - c.low)
                .max((c.high - prev_close).abs())
                .max((c.low - prev_close).abs())
        };
        tr_vec.push(tr);
        let atr_14 = sma_last(&tr_vec, i, 14);

        let (bb_width_20, bb_squeeze_20) = bb_stats(&closes, i, 20);
        let volume_z_20 = zscore_last(&volumes, i, 20);
        let quote_volume_z_20 = zscore_last(&qvols, i, 20);

        if i > 0 {
            obv += if c.close > closes[i - 1] {
                c.volume
            } else if c.close < closes[i - 1] {
                -c.volume
            } else {
                0.0
            };
        }
        let prev_obv = if i == 0 {
            0.0
        } else {
            obv_prev(&closes, &volumes, i)
        };
        let obv_delta = obv - prev_obv;

        let target_next_up = if i + 1 < candles.len() {
            (candles[i + 1].close > c.close) as i32
        } else {
            0
        };
        let target_next2_up = if i + 2 < candles.len() {
            (candles[i + 2].close > c.close) as i32
        } else {
            0
        };
        let target_continuation_strong = if i + 1 < candles.len() {
            let fwd = ((candles[i + 1].close / c.close) - 1.0).abs();
            (fwd > 0.001) as i32
        } else {
            0
        };

        let target_up_bid_next_up = if i + 1 < candles.len() {
            match (c.up_bid_last, candles[i + 1].up_bid_last) {
                (Some(a), Some(b)) => (b > a) as i32,
                _ => 0,
            }
        } else {
            0
        };

        let target_down_bid_next_up = if i + 1 < candles.len() {
            match (c.down_bid_last, candles[i + 1].down_bid_last) {
                (Some(a), Some(b)) => (b > a) as i32,
                _ => 0,
            }
        } else {
            0
        };

        let target_edge_up = if i + 1 < candles.len() {
            match (c.up_ask_last, candles[i + 1].up_bid_last) {
                (Some(ask_now), Some(bid_next)) => (bid_next - ask_now > 0.0) as i32,
                _ => 0,
            }
        } else {
            0
        };

        let target_edge_down = if i + 1 < candles.len() {
            match (c.down_ask_last, candles[i + 1].down_bid_last) {
                (Some(ask_now), Some(bid_next)) => (bid_next - ask_now > 0.0) as i32,
                _ => 0,
            }
        } else {
            0
        };

        let split_is_train = (i < split_idx) as i32;

        wtr.write_record([
            c.ts.to_string(),
            format!("{:.8}", c.open),
            format!("{:.8}", c.high),
            format!("{:.8}", c.low),
            format!("{:.8}", c.close),
            format!("{:.8}", c.volume),
            format!("{:.8}", c.quote_volume),
            c.trade_count.to_string(),
            format!("{:.8}", c.buy_quote_volume),
            format!("{:.8}", c.sell_quote_volume),
            format!("{:.8}", c.ofi),
            format!("{:.8}", c.vwap),
            format!("{:.8}", c.vwap_dist_close),
            format!("{:.8}", c.burstiness),
            format!("{:.8}", c.rv_1s),
            format!("{:.8}", c.rv_5s),
            format!("{:.8}", c.range_pct),
            format!("{:.8}", c.close_in_range),
            format!("{:.8}", ret_1),
            format!("{:.8}", ret_2),
            format!("{:.8}", ret_4),
            format!("{:.8}", roc_3),
            format!("{:.8}", roc_6),
            format!("{:.8}", mom_3),
            format!("{:.8}", mom_6),
            format!("{:.8}", ema9),
            format!("{:.8}", ema21),
            format!("{:.8}", ema50),
            format!("{:.8}", ema12),
            format!("{:.8}", ema26),
            format!("{:.8}", ema_slope_9),
            format!("{:.8}", ema_slope_21),
            format!("{:.8}", macd_line),
            format!("{:.8}", macd_signal),
            format!("{:.8}", macd_hist),
            ((ema12 > ema26) as i32).to_string(),
            format!("{:.8}", rsi_14),
            format!("{:.8}", rsi_30),
            format!("{:.8}", stoch_k),
            format!("{:.8}", stoch_d),
            format!("{:.8}", cci_20),
            format!("{:.8}", atr_14),
            format!("{:.8}", bb_width_20),
            format!("{:.8}", bb_squeeze_20),
            format!("{:.8}", volume_z_20),
            format!("{:.8}", quote_volume_z_20),
            format!("{:.8}", obv),
            format!("{:.8}", obv_delta),
            opt_fmt(c.up_bid_last),
            opt_fmt(c.up_ask_last),
            opt_fmt(c.down_bid_last),
            opt_fmt(c.down_ask_last),
            opt_fmt(c.up_spread),
            opt_fmt(c.down_spread),
            opt_fmt(c.sum_bid),
            opt_fmt(c.sum_ask),
            opt_fmt(c.mid_up),
            opt_fmt(c.mid_down),
            format!("{:.8}", c.lag_mean_ms),
            format!("{:.8}", c.lag_abs_mean_ms),
            opt_fmt(c.up_bid_drift_5s),
            opt_fmt(c.up_bid_drift_30s),
            opt_fmt(c.up_bid_drift_120s),
            opt_fmt(c.down_bid_drift_5s),
            opt_fmt(c.down_bid_drift_30s),
            opt_fmt(c.down_bid_drift_120s),
            target_next_up.to_string(),
            target_next2_up.to_string(),
            target_continuation_strong.to_string(),
            target_up_bid_next_up.to_string(),
            target_down_bid_next_up.to_string(),
            target_edge_up.to_string(),
            target_edge_down.to_string(),
            split_is_train.to_string(),
            train_end_ts.to_string(),
            test_start_ts.to_string(),
        ])?;
    }

    wtr.flush()?;
    Ok(path)
}

fn pct_ret(closes: &[f64], idx: usize, n: usize) -> f64 {
    if idx >= n && closes[idx - n] > 0.0 {
        (closes[idx] / closes[idx - n]) - 1.0
    } else {
        0.0
    }
}

fn diff_n(closes: &[f64], idx: usize, n: usize) -> f64 {
    if idx >= n {
        closes[idx] - closes[idx - n]
    } else {
        0.0
    }
}

fn rsi_from_avg(avg_gain: f64, avg_loss: f64) -> f64 {
    if avg_loss <= f64::EPSILON {
        100.0
    } else {
        let rs = avg_gain / avg_loss;
        100.0 - (100.0 / (1.0 + rs))
    }
}

fn stoch_k_lookback(highs: &[f64], lows: &[f64], closes: &[f64], idx: usize, n: usize) -> f64 {
    if idx + 1 < n {
        return 50.0;
    }
    let start = idx + 1 - n;
    let hh = highs[start..=idx].iter().fold(f64::MIN, |a, b| a.max(*b));
    let ll = lows[start..=idx].iter().fold(f64::MAX, |a, b| a.min(*b));
    if (hh - ll).abs() <= f64::EPSILON {
        50.0
    } else {
        ((closes[idx] - ll) / (hh - ll)) * 100.0
    }
}

fn cci_lookback(highs: &[f64], lows: &[f64], closes: &[f64], idx: usize, n: usize) -> f64 {
    if idx + 1 < n {
        return 0.0;
    }
    let start = idx + 1 - n;
    let tps: Vec<f64> = (start..=idx)
        .map(|i| (highs[i] + lows[i] + closes[i]) / 3.0)
        .collect();
    let sma = tps.iter().sum::<f64>() / tps.len() as f64;
    let md = tps.iter().map(|x| (x - sma).abs()).sum::<f64>() / tps.len() as f64;
    if md <= f64::EPSILON {
        0.0
    } else {
        (tps[tps.len() - 1] - sma) / (0.015 * md)
    }
}

fn sma_last(values: &[f64], idx: usize, n: usize) -> f64 {
    if values.is_empty() {
        return 0.0;
    }
    let start = idx.saturating_sub(n.saturating_sub(1));
    let slice = &values[start..=idx];
    slice.iter().sum::<f64>() / slice.len() as f64
}

fn std_last(values: &[f64], idx: usize, n: usize) -> f64 {
    let start = idx.saturating_sub(n.saturating_sub(1));
    let slice = &values[start..=idx];
    let m = slice.iter().sum::<f64>() / slice.len() as f64;
    let v = slice.iter().map(|x| (x - m).powi(2)).sum::<f64>() / slice.len() as f64;
    v.sqrt()
}

fn bb_stats(closes: &[f64], idx: usize, n: usize) -> (f64, f64) {
    if closes.is_empty() {
        return (0.0, 0.0);
    }
    let ma = sma_last(closes, idx, n);
    let sd = std_last(closes, idx, n);
    let upper = ma + 2.0 * sd;
    let lower = ma - 2.0 * sd;
    let width = if ma.abs() > f64::EPSILON {
        (upper - lower) / ma.abs()
    } else {
        0.0
    };
    let squeeze = (width < 0.02) as i32 as f64;
    (width, squeeze)
}

fn zscore_last(values: &[f64], idx: usize, n: usize) -> f64 {
    if values.is_empty() {
        return 0.0;
    }
    let start = idx.saturating_sub(n.saturating_sub(1));
    let slice = &values[start..=idx];
    let m = slice.iter().sum::<f64>() / slice.len() as f64;
    let sd = (slice.iter().map(|x| (x - m).powi(2)).sum::<f64>() / slice.len() as f64).sqrt();
    if sd <= f64::EPSILON {
        0.0
    } else {
        (values[idx] - m) / sd
    }
}

fn realized_vol_from_map(m: &HashMap<i64, f64>) -> f64 {
    if m.len() < 3 {
        return 0.0;
    }
    let mut pts: Vec<(i64, f64)> = m.iter().map(|(k, v)| (*k, *v)).collect();
    pts.sort_by_key(|x| x.0);
    let mut rets = Vec::new();
    for i in 1..pts.len() {
        if pts[i - 1].1 > 0.0 {
            rets.push((pts[i].1 / pts[i - 1].1).ln());
        }
    }
    if rets.is_empty() {
        0.0
    } else {
        let mean = rets.iter().sum::<f64>() / rets.len() as f64;
        let var = rets.iter().map(|x| (x - mean).powi(2)).sum::<f64>() / rets.len() as f64;
        var.sqrt()
    }
}

fn spread_opt(bid: Option<f64>, ask: Option<f64>) -> Option<f64> {
    match (bid, ask) {
        (Some(b), Some(a)) => Some(a - b),
        _ => None,
    }
}

fn sum_opt(a: Option<f64>, b: Option<f64>) -> Option<f64> {
    match (a, b) {
        (Some(x), Some(y)) => Some(x + y),
        _ => None,
    }
}

fn mid_opt(bid: Option<f64>, ask: Option<f64>) -> Option<f64> {
    match (bid, ask) {
        (Some(b), Some(a)) => Some((a + b) / 2.0),
        _ => None,
    }
}

fn last_le(series: &[(i64, f64)], ts: i64) -> Option<f64> {
    series
        .iter()
        .rev()
        .find_map(|(t, v)| if *t <= ts { Some(*v) } else { None })
}

fn drift_at(series: &[(i64, f64)], base_ts: i64, delta_ms: i64) -> Option<f64> {
    let base = last_le(series, base_ts)?;
    let fut = last_le(series, base_ts + delta_ms)?;
    Some(fut - base)
}

fn opt_fmt(v: Option<f64>) -> String {
    match v {
        Some(x) => format!("{:.8}", x),
        None => "".to_string(),
    }
}

fn obv_prev(closes: &[f64], volumes: &[f64], idx: usize) -> f64 {
    if idx == 0 {
        return 0.0;
    }
    let mut obv = 0.0;
    for i in 1..idx {
        if closes[i] > closes[i - 1] {
            obv += volumes[i];
        } else if closes[i] < closes[i - 1] {
            obv -= volumes[i];
        }
    }
    obv
}

fn resolve_export_start_ts(conn: &rusqlite::Connection) -> Result<i64> {
    if let Ok(value) = std::env::var("EXPORT_START_TS_MS") {
        return Ok(value.parse()?);
    }
    if std::env::var("ARCHIVE_EXPORT").ok().as_deref() == Some("1") {
        let min_ts: Option<i64> = conn
            .query_row("SELECT MIN(source_ts_ms) FROM binance_ticks_ms", [], |row| {
                row.get(0)
            })
            .unwrap_or(None);
        return Ok(min_ts.unwrap_or(0));
    }
    let lookback_ms = 72 * 60 * 60 * 1000_i64;
    Ok(now_ms() - lookback_ms)
}

pub fn export_step2_hf_features_csv() -> Result<(String, String)> {
    let export_dir = config::export_dir();
    fs::create_dir_all(&export_dir)?;
    let conn = db::get_db_conn()?;

    let start = resolve_export_start_ts(&conn)?;

    let mut t_stmt = conn.prepare(
        "SELECT source_ts_ms, price, volume
         FROM binance_ticks_ms
         WHERE source_ts_ms > ?
         ORDER BY source_ts_ms ASC",
    )?;
    let ticks: Vec<(i64, f64, f64)> = t_stmt
        .query_map([start], |row| {
            Ok((
                row.get::<_, i64>(0)?,
                row.get::<_, f64>(1)?,
                row.get::<_, f64>(2)?,
            ))
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?;

    let mut p_stmt = conn.prepare(
        "SELECT source_ts_ms, side_label, best_bid, best_ask
         FROM polymarket_ticks_ms
         WHERE source_ts_ms > ? AND side_label IN ('UP','DOWN')
         ORDER BY source_ts_ms ASC",
    )?;
    let p_ticks: Vec<(i64, String, Option<f64>, Option<f64>)> = p_stmt
        .query_map([start], |row| {
            Ok((
                row.get::<_, i64>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, Option<f64>>(2)?,
                row.get::<_, Option<f64>>(3)?,
            ))
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?;

    let mut lag_stmt = conn.prepare(
        "SELECT polymarket_source_ts_ms, lead_lag_ms
         FROM lag_pairs_ms
         WHERE polymarket_source_ts_ms > ?",
    )?;
    let lag_rows: Vec<(i64, i64)> = lag_stmt
        .query_map([start], |row| {
            Ok((row.get::<_, i64>(0)?, row.get::<_, i64>(1)?))
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?;

    let ctx15 = build_15m_context_map(&ticks, &p_ticks, &lag_rows);

    let ts = now_ms();
    let p100 = format!("{}/step2_features_100ms_{}.csv", export_dir, ts);
    let p1s = format!("{}/step2_features_1s_{}.csv", export_dir, ts);

    export_hf_one(&p100, 100, &ticks, &p_ticks, &lag_rows, &ctx15)?;
    export_hf_one(&p1s, 1000, &ticks, &p_ticks, &lag_rows, &ctx15)?;

    Ok((p100, p1s))
}

#[derive(Clone, Default)]
struct Ctx15m {
    close: f64,
    ret_1: f64,
    ema_21: f64,
    ema_50: f64,
    regime_up: i32,
    lag_mean_ms: f64,
    sum_bid: Option<f64>,
}

fn build_15m_context_map(
    ticks: &[(i64, f64, f64)],
    p_ticks: &[(i64, String, Option<f64>, Option<f64>)],
    lag_rows: &[(i64, i64)],
) -> BTreeMap<i64, Ctx15m> {
    #[derive(Default)]
    struct Acc {
        open: Option<f64>,
        close: f64,
        quote_volume: f64,
        volume: f64,
    }

    let mut acc: BTreeMap<i64, Acc> = BTreeMap::new();
    for (ts, price, volume) in ticks {
        let b = (ts / 900000) * 900000;
        let a = acc.entry(b).or_default();
        if a.open.is_none() {
            a.open = Some(*price);
        }
        a.close = *price;
        a.volume += *volume;
        a.quote_volume += price * volume;
    }

    #[derive(Default, Clone)]
    struct PolyA {
        up_bid: Option<f64>,
        down_bid: Option<f64>,
    }
    let mut pm: BTreeMap<i64, PolyA> = BTreeMap::new();
    for (ts, side, best_bid, _) in p_ticks {
        let b = (ts / 900000) * 900000;
        let e = pm.entry(b).or_default();
        match side.as_str() {
            "UP" => {
                if best_bid.is_some() {
                    e.up_bid = *best_bid;
                }
            }
            "DOWN" => {
                if best_bid.is_some() {
                    e.down_bid = *best_bid;
                }
            }
            _ => {}
        }
    }

    let mut lag_map: HashMap<i64, Vec<i64>> = HashMap::new();
    for (ts, lag) in lag_rows {
        lag_map
            .entry((ts / 900000) * 900000)
            .or_default()
            .push(*lag);
    }

    let mut out: BTreeMap<i64, Ctx15m> = BTreeMap::new();
    let mut closes: Vec<f64> = Vec::new();
    let mut ema21 = 0.0;
    let mut ema50 = 0.0;
    let k21 = 2.0 / 22.0;
    let k50 = 2.0 / 51.0;

    for (i, (b, a)) in acc.into_iter().enumerate() {
        if i == 0 {
            ema21 = a.close;
            ema50 = a.close;
        } else {
            ema21 = a.close * k21 + ema21 * (1.0 - k21);
            ema50 = a.close * k50 + ema50 * (1.0 - k50);
        }
        closes.push(a.close);

        let ret_1 = if i > 0 && closes[i - 1] > 0.0 {
            (a.close / closes[i - 1]) - 1.0
        } else {
            0.0
        };

        let lag_mean = lag_map
            .get(&b)
            .map(|v| v.iter().map(|x| *x as f64).sum::<f64>() / v.len() as f64)
            .unwrap_or(0.0);

        let pb = pm.get(&b).cloned().unwrap_or_default();
        let sum_bid = sum_opt(pb.up_bid, pb.down_bid);

        out.insert(
            b,
            Ctx15m {
                close: a.close,
                ret_1,
                ema_21: ema21,
                ema_50: ema50,
                regime_up: (ema21 > ema50) as i32,
                lag_mean_ms: lag_mean,
                sum_bid,
            },
        );
    }
    out
}

fn export_hf_one(
    path: &str,
    bucket_ms: i64,
    ticks: &[(i64, f64, f64)],
    p_ticks: &[(i64, String, Option<f64>, Option<f64>)],
    lag_rows: &[(i64, i64)],
    ctx15: &BTreeMap<i64, Ctx15m>,
) -> Result<()> {
    #[derive(Default)]
    struct Acc {
        open: Option<f64>,
        high: f64,
        low: f64,
        close: f64,
        volume: f64,
        quote_volume: f64,
        trade_count: u64,
        buy_qv: f64,
        sell_qv: f64,
    }

    let mut bmap: BTreeMap<i64, Acc> = BTreeMap::new();
    let mut prev_price: Option<f64> = None;
    for (ts, price, volume) in ticks {
        let b = (ts / bucket_ms) * bucket_ms;
        let a = bmap.entry(b).or_default();
        if a.open.is_none() {
            a.open = Some(*price);
            a.high = *price;
            a.low = *price;
        }
        a.high = a.high.max(*price);
        a.low = a.low.min(*price);
        a.close = *price;
        a.volume += *volume;
        let qv = price * volume;
        a.quote_volume += qv;
        a.trade_count += 1;
        if let Some(pp) = prev_price {
            if *price >= pp {
                a.buy_qv += qv;
            } else {
                a.sell_qv += qv;
            }
        } else {
            a.buy_qv += qv;
        }
        prev_price = Some(*price);
    }

    #[derive(Default, Clone)]
    struct PolyA {
        up_bid: Option<f64>,
        up_ask: Option<f64>,
        down_bid: Option<f64>,
        down_ask: Option<f64>,
    }
    let mut pmap: BTreeMap<i64, PolyA> = BTreeMap::new();
    let mut up_series: Vec<(i64, f64)> = Vec::new();
    let mut down_series: Vec<(i64, f64)> = Vec::new();
    for (ts, side, bid, ask) in p_ticks {
        let b = (ts / bucket_ms) * bucket_ms;
        let e = pmap.entry(b).or_default();
        match side.as_str() {
            "UP" => {
                if let Some(x) = bid {
                    e.up_bid = Some(*x);
                    up_series.push((*ts, *x));
                }
                if let Some(x) = ask {
                    e.up_ask = Some(*x);
                }
            }
            "DOWN" => {
                if let Some(x) = bid {
                    e.down_bid = Some(*x);
                    down_series.push((*ts, *x));
                }
                if let Some(x) = ask {
                    e.down_ask = Some(*x);
                }
            }
            _ => {}
        }
    }

    let mut lag_map: HashMap<i64, Vec<i64>> = HashMap::new();
    for (ts, lag) in lag_rows {
        lag_map
            .entry((ts / bucket_ms) * bucket_ms)
            .or_default()
            .push(*lag);
    }

    #[derive(Clone)]
    struct Row {
        ts: i64,
        open: f64,
        high: f64,
        low: f64,
        close: f64,
        volume: f64,
        quote_volume: f64,
        trade_count: u64,
        imbalance: f64,
        vwap: f64,
        up_bid: Option<f64>,
        up_ask: Option<f64>,
        down_bid: Option<f64>,
        down_ask: Option<f64>,
        lag_mean: f64,
        lag_abs_mean: f64,
        up_drift_500: Option<f64>,
        up_drift_1000: Option<f64>,
        up_drift_5000: Option<f64>,
        down_drift_500: Option<f64>,
        down_drift_1000: Option<f64>,
        down_drift_5000: Option<f64>,
        ctx15: Ctx15m,
    }

    let mut rows: Vec<Row> = Vec::new();
    for (b, a) in bmap {
        let open = a.open.unwrap_or(a.close);
        let vwap = if a.volume > 0.0 {
            a.quote_volume / a.volume
        } else {
            a.close
        };
        let denom = a.buy_qv + a.sell_qv;
        let imbalance = if denom > 0.0 {
            (a.buy_qv - a.sell_qv) / denom
        } else {
            0.0
        };

        let p = pmap.get(&b).cloned().unwrap_or_default();
        let le = lag_map.get(&b);
        let lag_mean = le
            .map(|v| v.iter().map(|x| *x as f64).sum::<f64>() / v.len() as f64)
            .unwrap_or(0.0);
        let lag_abs_mean = le
            .map(|v| v.iter().map(|x| x.abs() as f64).sum::<f64>() / v.len() as f64)
            .unwrap_or(0.0);

        let bucket_end = b + bucket_ms - 1;
        let ctx_b = (b / 900000) * 900000;
        let ctx = ctx15.get(&ctx_b).cloned().unwrap_or_default();

        rows.push(Row {
            ts: b,
            open,
            high: a.high,
            low: a.low,
            close: a.close,
            volume: a.volume,
            quote_volume: a.quote_volume,
            trade_count: a.trade_count,
            imbalance,
            vwap,
            up_bid: p.up_bid,
            up_ask: p.up_ask,
            down_bid: p.down_bid,
            down_ask: p.down_ask,
            lag_mean,
            lag_abs_mean,
            up_drift_500: drift_at(&up_series, bucket_end, 500),
            up_drift_1000: drift_at(&up_series, bucket_end, 1_000),
            up_drift_5000: drift_at(&up_series, bucket_end, 5_000),
            down_drift_500: drift_at(&down_series, bucket_end, 500),
            down_drift_1000: drift_at(&down_series, bucket_end, 1_000),
            down_drift_5000: drift_at(&down_series, bucket_end, 5_000),
            ctx15: ctx,
        });
    }
    rows.sort_by_key(|r| r.ts);

    let mut wtr = Writer::from_path(path)?;
    wtr.write_record([
        "ts_ms",
        "bucket_ms",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "quote_volume",
        "trade_count",
        "buy_sell_imbalance",
        "vwap",
        "vwap_dist_close",
        "range_pct",
        "ret_1",
        "ret_5",
        "ret_10",
        "tps_est",
        "up_bid_last",
        "up_ask_last",
        "down_bid_last",
        "down_ask_last",
        "up_spread",
        "down_spread",
        "sum_bid",
        "sum_ask",
        "lag_mean_ms",
        "lag_abs_mean_ms",
        "up_bid_drift_500ms",
        "up_bid_drift_1000ms",
        "up_bid_drift_5000ms",
        "down_bid_drift_500ms",
        "down_bid_drift_1000ms",
        "down_bid_drift_5000ms",
        "target_next_up",
        "target_h1s_up",
        "target_h5s_up",
        "ctx_15m_close",
        "ctx_15m_ret_1",
        "ctx_15m_ema_21",
        "ctx_15m_ema_50",
        "ctx_15m_regime_up",
        "ctx_15m_lag_mean_ms",
        "ctx_15m_sum_bid",
    ])?;

    if rows.is_empty() {
        wtr.flush()?;
        return Ok(());
    }

    let closes: Vec<f64> = rows.iter().map(|r| r.close).collect();
    let h1_bars = (1_000 / bucket_ms).max(1) as usize;
    let h5_bars = (5_000 / bucket_ms).max(1) as usize;

    for i in 0..rows.len() {
        let r = &rows[i];
        let ret_1 = pct_ret(&closes, i, 1);
        let ret_5 = pct_ret(&closes, i, 5);
        let ret_10 = pct_ret(&closes, i, 10);
        let range_pct = if r.open > 0.0 {
            (r.high - r.low) / r.open
        } else {
            0.0
        };
        let vwap_dist_close = if r.vwap > 0.0 {
            (r.close - r.vwap) / r.vwap
        } else {
            0.0
        };
        let tps_est = r.trade_count as f64 / (bucket_ms as f64 / 1000.0);

        let target_next_up = if i + 1 < rows.len() {
            (rows[i + 1].close > r.close) as i32
        } else {
            0
        };
        let target_h1s_up = if i + h1_bars < rows.len() {
            (rows[i + h1_bars].close > r.close) as i32
        } else {
            0
        };
        let target_h5s_up = if i + h5_bars < rows.len() {
            (rows[i + h5_bars].close > r.close) as i32
        } else {
            0
        };

        wtr.write_record([
            r.ts.to_string(),
            bucket_ms.to_string(),
            format!("{:.8}", r.open),
            format!("{:.8}", r.high),
            format!("{:.8}", r.low),
            format!("{:.8}", r.close),
            format!("{:.8}", r.volume),
            format!("{:.8}", r.quote_volume),
            r.trade_count.to_string(),
            format!("{:.8}", r.imbalance),
            format!("{:.8}", r.vwap),
            format!("{:.8}", vwap_dist_close),
            format!("{:.8}", range_pct),
            format!("{:.8}", ret_1),
            format!("{:.8}", ret_5),
            format!("{:.8}", ret_10),
            format!("{:.8}", tps_est),
            opt_fmt(r.up_bid),
            opt_fmt(r.up_ask),
            opt_fmt(r.down_bid),
            opt_fmt(r.down_ask),
            opt_fmt(spread_opt(r.up_bid, r.up_ask)),
            opt_fmt(spread_opt(r.down_bid, r.down_ask)),
            opt_fmt(sum_opt(r.up_bid, r.down_bid)),
            opt_fmt(sum_opt(r.up_ask, r.down_ask)),
            format!("{:.8}", r.lag_mean),
            format!("{:.8}", r.lag_abs_mean),
            opt_fmt(r.up_drift_500),
            opt_fmt(r.up_drift_1000),
            opt_fmt(r.up_drift_5000),
            opt_fmt(r.down_drift_500),
            opt_fmt(r.down_drift_1000),
            opt_fmt(r.down_drift_5000),
            target_next_up.to_string(),
            target_h1s_up.to_string(),
            target_h5s_up.to_string(),
            format!("{:.8}", r.ctx15.close),
            format!("{:.8}", r.ctx15.ret_1),
            format!("{:.8}", r.ctx15.ema_21),
            format!("{:.8}", r.ctx15.ema_50),
            r.ctx15.regime_up.to_string(),
            format!("{:.8}", r.ctx15.lag_mean_ms),
            opt_fmt(r.ctx15.sum_bid),
        ])?;
    }

    wtr.flush()?;
    Ok(())
}

#[derive(Debug, Clone, Copy)]
pub struct Step3ExportOptions {
    pub start_ts_ms: Option<i64>,
    pub end_ts_ms: Option<i64>,
    pub lookback_hours: u64,
    pub market_limit: Option<usize>,
}

#[derive(Debug, Clone, Serialize)]
pub struct Step3ExportSummary {
    pub csv_path: String,
    pub manifest_path: String,
    pub markets: usize,
    pub rows: usize,
    pub ties_dropped: usize,
}

#[derive(Debug, Clone)]
struct Step3Market {
    slug: String,
    start_ms: i64,
    end_ms: i64,
}

#[derive(Debug, Clone)]
struct PolyBookTick {
    ts_ms: i64,
    side: String,
    best_bid: Option<f64>,
    best_ask: Option<f64>,
}

pub fn export_step3_binary_calibration_csv(
    options: Step3ExportOptions,
) -> Result<Step3ExportSummary> {
    let export_dir = config::export_dir();
    fs::create_dir_all(&export_dir)?;
    let conn = db::get_db_conn()?;

    let archive = std::env::var("ARCHIVE_EXPORT").ok().as_deref() == Some("1");
    let end_ts_ms = options.end_ts_ms.unwrap_or_else(|| {
        if archive {
            conn.query_row(
                "SELECT MAX(source_ts_ms) FROM binance_ticks_ms",
                [],
                |row| row.get::<_, i64>(0),
            )
            .unwrap_or_else(|_| now_ms())
        } else {
            now_ms()
        }
    });
    let start_ts_ms = options.start_ts_ms.unwrap_or_else(|| {
        if archive {
            conn.query_row(
                "SELECT MIN(source_ts_ms) FROM binance_ticks_ms",
                [],
                |row| row.get::<_, i64>(0),
            )
            .unwrap_or(0)
        } else {
            end_ts_ms.saturating_sub((options.lookback_hours as i64) * 60 * 60 * 1000)
        }
    });

    let mut markets = list_step3_markets(&conn, start_ts_ms, end_ts_ms)?;
    if let Some(limit) = options.market_limit {
        if markets.len() > limit {
            markets = markets.split_off(markets.len() - limit);
        }
    }

    let ts = now_ms();
    let csv_path = format!("{}/step3_binary_calibration_{}.csv", export_dir, ts);
    let manifest_path = format!(
        "{}/step3_binary_calibration_{}.manifest.json",
        export_dir, ts
    );
    let mut writer = Writer::from_path(&csv_path)?;
    let feature_names: Vec<String> = DEFAULT_FEATURE_NAMES
        .iter()
        .map(|name| (*name).to_string())
        .collect();

    let mut header: Vec<String> = vec![
        "market_slug".to_string(),
        "market_start_ms".to_string(),
        "market_end_ms".to_string(),
        "ts_ms".to_string(),
        "market_open_price".to_string(),
        "market_close_price".to_string(),
        "label_up_final".to_string(),
    ];
    header.extend(feature_names.iter().cloned());
    writer.write_record(&header)?;

    let mut rows_written = 0usize;
    let mut markets_written = 0usize;
    let mut ties_dropped = 0usize;

    for market in &markets {
        let trades = load_market_trades(&conn, market.start_ms, market.end_ms)?;
        if trades.len() < signal_engine::config::MIN_TRADES_FOR_SIGNAL {
            continue;
        }

        let market_open_price = trades.first().map(|trade| trade.price).unwrap_or(0.0);
        let market_close_price = trades.last().map(|trade| trade.price).unwrap_or(0.0);
        if (market_close_price - market_open_price).abs() <= f64::EPSILON {
            ties_dropped += 1;
            continue;
        }
        let label_up_final = if market_close_price > market_open_price {
            1
        } else {
            0
        };

        let poly_ticks =
            load_market_poly_ticks(&conn, &market.slug, market.start_ms, market.end_ms)?;
        if poly_ticks.is_empty() {
            continue;
        }

        let (raw_close, raw_buy_vol, raw_sell_vol) = build_raw_1s_arrays(
            &trades,
            market.start_ms,
            signal_engine::config::MARKET_DURATION_SECS as u64,
        );

        let mut trade_idx = 0usize;
        let mut poly_idx = 0usize;
        let mut up_best_bid = 0.0;
        let mut up_best_ask = 0.0;
        let mut down_best_bid = 0.0;
        let mut down_best_ask = 0.0;
        let mut market_rows = 0usize;

        for secs_in in (15_u64..signal_engine::config::MARKET_DURATION_SECS as u64).step_by(5) {
            let row_ts_ms = market.start_ms + (secs_in as i64 * 1000);
            while trade_idx < trades.len() && trades[trade_idx].trade_time_ms <= row_ts_ms {
                trade_idx += 1;
            }
            while poly_idx < poly_ticks.len() && poly_ticks[poly_idx].ts_ms <= row_ts_ms {
                let tick = &poly_ticks[poly_idx];
                match tick.side.as_str() {
                    "UP" => {
                        if let Some(best_bid) = tick.best_bid {
                            up_best_bid = best_bid;
                        }
                        if let Some(best_ask) = tick.best_ask {
                            up_best_ask = best_ask;
                        }
                    }
                    "DOWN" => {
                        if let Some(best_bid) = tick.best_bid {
                            down_best_bid = best_bid;
                        }
                        if let Some(best_ask) = tick.best_ask {
                            down_best_ask = best_ask;
                        }
                    }
                    _ => {}
                }
                poly_idx += 1;
            }

            if trade_idx < signal_engine::config::MIN_TRADES_FOR_SIGNAL {
                continue;
            }
            if up_best_bid <= 0.0
                || up_best_ask <= 0.0
                || down_best_bid <= 0.0
                || down_best_ask <= 0.0
            {
                continue;
            }

            let Some(bars) =
                build_1s_bars_from_arrays(&raw_close, &raw_buy_vol, &raw_sell_vol, secs_in)
            else {
                continue;
            };
            let secs_left = signal_engine::config::MARKET_DURATION_SECS as u64 - secs_in;
            let Some(signal) = compute_drift_signal_v14(&bars, market_open_price, secs_left as f64)
            else {
                continue;
            };

            let market_state = SignalMarketInfo {
                slug: market.slug.clone(),
                start_ms: market.start_ms,
                end_ms: market.end_ms,
                up_price: (up_best_bid + up_best_ask) / 2.0,
                down_price: (down_best_bid + down_best_ask) / 2.0,
                up_best_ask,
                down_best_ask,
                up_best_bid,
                down_best_bid,
            };
            let snapshot = build_calibrated_feature_snapshot(
                &bars,
                market_open_price,
                &market_state,
                secs_in,
                &signal,
                trade_idx,
            );
            let feature_values = snapshot.ordered_values(&feature_names)?;

            let mut record = vec![
                market.slug.clone(),
                market.start_ms.to_string(),
                market.end_ms.to_string(),
                row_ts_ms.to_string(),
                format!("{:.8}", market_open_price),
                format!("{:.8}", market_close_price),
                label_up_final.to_string(),
            ];
            record.extend(
                feature_values
                    .into_iter()
                    .map(|value| format!("{:.8}", value)),
            );
            writer.write_record(&record)?;
            rows_written += 1;
            market_rows += 1;
        }

        if market_rows > 0 {
            markets_written += 1;
        }
    }

    writer.flush()?;

    fs::write(
        &manifest_path,
        serde_json::to_string_pretty(&json!({
            "generated_at_ms": ts,
            "csv_path": csv_path.clone(),
            "database_file": config::database_file(),
            "start_ts_ms": start_ts_ms,
            "end_ts_ms": end_ts_ms,
            "lookback_hours": options.lookback_hours,
            "market_limit": options.market_limit,
            "feature_names": feature_names,
            "markets_selected": markets.len(),
            "markets_written": markets_written,
            "rows_written": rows_written,
            "ties_dropped": ties_dropped,
        }))?,
    )?;

    Ok(Step3ExportSummary {
        csv_path,
        manifest_path,
        markets: markets_written,
        rows: rows_written,
        ties_dropped,
    })
}

fn list_step3_markets(
    conn: &rusqlite::Connection,
    start_ts_ms: i64,
    end_ts_ms: i64,
) -> Result<Vec<Step3Market>> {
    let mut stmt = conn.prepare(
        "SELECT market_slug
         FROM market_meta
         WHERE market_slug LIKE 'btc-updown-15m-%'
         ORDER BY market_slug ASC",
    )?;

    let rows = stmt
        .query_map([], |row| row.get::<_, String>(0))?
        .collect::<rusqlite::Result<Vec<_>>>()?;

    let mut markets: Vec<Step3Market> = rows
        .into_iter()
        .filter_map(|slug| {
            let start_ms = parse_market_start_ms(&slug)?;
            let end_ms = start_ms + ((signal_engine::config::MARKET_DURATION_SECS as i64) * 1000);
            if start_ms >= start_ts_ms && start_ms <= end_ts_ms {
                Some(Step3Market {
                    slug,
                    start_ms,
                    end_ms,
                })
            } else {
                None
            }
        })
        .collect();
    markets.sort_by_key(|market| market.start_ms);
    Ok(markets)
}

fn parse_market_start_ms(slug: &str) -> Option<i64> {
    slug.rsplit('-')
        .next()
        .and_then(|value| value.parse::<i64>().ok())
        .map(|epoch_s| epoch_s * 1000)
}

fn sqlite_table_exists(conn: &rusqlite::Connection, table_name: &str) -> Result<bool> {
    let mut stmt =
        conn.prepare("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1")?;
    let exists = stmt.exists([table_name])?;
    Ok(exists)
}

fn load_market_trades(
    conn: &rusqlite::Connection,
    start_ms: i64,
    end_ms: i64,
) -> Result<Vec<SignalBinanceTrade>> {
    if sqlite_table_exists(conn, "binance_trades")? {
        let mut stmt = conn.prepare(
            "SELECT trade_time, price, quantity, is_buyer_maker
             FROM binance_trades
             WHERE trade_time >= ? AND trade_time <= ?
             ORDER BY trade_time ASC",
        )?;

        let trades = stmt
            .query_map([start_ms, end_ms], |row| {
                Ok(SignalBinanceTrade {
                    trade_time_ms: row.get::<_, i64>(0)?,
                    price: row.get::<_, f64>(1)?,
                    quantity: row.get::<_, f64>(2)?,
                    is_buyer_maker: row.get::<_, i64>(3)? != 0,
                })
            })?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        return Ok(trades);
    }

    let mut stmt = conn.prepare(
        "SELECT source_ts_ms, price, volume
         FROM binance_ticks_ms
         WHERE source_ts_ms >= ? AND source_ts_ms <= ?
         ORDER BY source_ts_ms ASC",
    )?;
    let ticks = stmt
        .query_map([start_ms, end_ms], |row| {
            Ok((
                row.get::<_, i64>(0)?,
                row.get::<_, f64>(1)?,
                row.get::<_, f64>(2)?,
            ))
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?;

    let mut previous_price: Option<f64> = None;
    let mut out = Vec::with_capacity(ticks.len());
    for (trade_time_ms, price, quantity) in ticks {
        let is_buyer_maker = previous_price.map(|prev| price < prev).unwrap_or(false);
        out.push(SignalBinanceTrade {
            trade_time_ms,
            price,
            quantity,
            is_buyer_maker,
        });
        previous_price = Some(price);
    }
    Ok(out)
}

fn load_market_poly_ticks(
    conn: &rusqlite::Connection,
    market_slug: &str,
    start_ms: i64,
    end_ms: i64,
) -> Result<Vec<PolyBookTick>> {
    let mut stmt = conn.prepare(
        "SELECT source_ts_ms, side_label, best_bid, best_ask
         FROM polymarket_ticks_ms
         WHERE market_slug = ?
           AND source_ts_ms >= ?
           AND source_ts_ms <= ?
           AND side_label IN ('UP', 'DOWN')
         ORDER BY source_ts_ms ASC",
    )?;
    let rows = stmt
        .query_map(rusqlite::params![market_slug, start_ms, end_ms], |row| {
            Ok(PolyBookTick {
                ts_ms: row.get::<_, i64>(0)?,
                side: row.get::<_, String>(1)?,
                best_bid: row.get::<_, Option<f64>>(2)?,
                best_ask: row.get::<_, Option<f64>>(3)?,
            })
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    Ok(rows)
}

#[cfg(test)]
mod step3_tests {
    use super::*;
    use std::path::Path;

    #[test]
    fn parse_market_start_ms_uses_slug_suffix() {
        assert_eq!(
            parse_market_start_ms("btc-updown-15m-1775410000"),
            Some(1_775_410_000_000)
        );
    }

    #[test]
    #[ignore]
    fn export_step3_smoke_test_live_db() {
        if !Path::new(&config::database_file()).exists() {
            return;
        }

        let summary = export_step3_binary_calibration_csv(Step3ExportOptions {
            start_ts_ms: None,
            end_ts_ms: None,
            lookback_hours: 6,
            market_limit: Some(8),
        })
        .expect("step3 export should succeed against the live DB");

        assert!(summary.rows > 0, "expected some step3 rows to be exported");
    }
}
