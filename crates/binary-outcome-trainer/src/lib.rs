//! Walk-forward logistic regression trainer for step3 binary calibration exports.

use std::collections::HashSet;
use std::fs;
use std::path::{Path, PathBuf};
use std::time::Instant;

use anyhow::{bail, Context, Result};
use rayon::prelude::*;
use serde::Serialize;

const META_COLUMNS: &[&str] = &[
    "market_slug",
    "market_start_ms",
    "market_end_ms",
    "ts_ms",
    "market_open_price",
    "market_close_price",
    "label_up_final",
];

#[derive(Debug, Clone)]
pub struct TrainConfig {
    pub min_train_markets: usize,
    pub test_markets: usize,
    pub step_markets: usize,
    pub epochs: usize,
    pub lr: f64,
    pub fee_rate: f64,
    pub slippage: f64,
    pub min_ev: f64,
}

impl Default for TrainConfig {
    fn default() -> Self {
        Self {
            min_train_markets: 12,
            test_markets: 4,
            step_markets: 4,
            epochs: 300,
            lr: 0.05,
            fee_rate: 0.01,
            slippage: 0.005,
            min_ev: 0.0,
        }
    }
}

#[derive(Debug, Clone)]
pub struct Dataset {
    pub feature_names: Vec<String>,
    pub features: Vec<f64>,
    pub labels: Vec<u8>,
    pub market_ranges: Vec<(usize, usize)>,
    pub up_best_ask_idx: Option<usize>,
    pub down_best_ask_idx: Option<usize>,
}

impl Dataset {
    pub fn n_rows(&self) -> usize {
        self.labels.len()
    }

    pub fn n_features(&self) -> usize {
        self.feature_names.len()
    }

    pub fn row_features(&self, row: usize) -> &[f64] {
        let w = self.n_features();
        let start = row * w;
        &self.features[start..start + w]
    }
}

#[derive(Debug, Clone)]
pub struct LrModel {
    pub w: Vec<f64>,
    pub b: f64,
    pub mean: Vec<f64>,
    pub std: Vec<f64>,
}

#[derive(Debug, Clone, Serialize)]
pub struct WindowSummary {
    pub train_markets: usize,
    pub test_markets: usize,
    pub rows_test: usize,
    pub auc_roc_raw: f64,
    pub brier_raw: f64,
    pub log_loss_raw: f64,
}

#[derive(Debug, Clone, Serialize)]
pub struct Metrics {
    pub auc_roc: f64,
    pub brier: f64,
    pub ece: f64,
    pub log_loss: f64,
    pub positive_ev_hit_rate: f64,
    pub positive_ev_trades: usize,
    pub up_chosen: usize,
    pub down_chosen: usize,
    pub realized_pnl_per_trade: f64,
    pub realized_total_pnl: f64,
}

#[derive(Debug, Clone, Serialize)]
pub struct ModelArtifact {
    pub artifact_version: String,
    pub generated_at: String,
    pub feature_names: Vec<String>,
    pub means: Vec<f64>,
    pub stds: Vec<f64>,
    pub weights: Vec<f64>,
    pub intercept: f64,
    pub calibration_method: String,
    pub platt_a: f64,
    pub platt_b: f64,
    pub fee_rate: f64,
    pub slippage: f64,
    pub source_export_path: String,
    pub metrics: Metrics,
}

#[derive(Debug, Clone, Serialize)]
pub struct MetricsDocument {
    pub generated_at: String,
    pub export_path: String,
    pub manifest_path: Option<String>,
    pub feature_names: Vec<String>,
    pub markets_total: usize,
    pub rows_total: usize,
    pub walk_forward_windows: Vec<WindowSummary>,
    pub metrics: Metrics,
}

#[derive(Debug, Clone)]
pub struct TrainSummary {
    pub artifact_path: PathBuf,
    pub latest_path: PathBuf,
    pub metrics_path: PathBuf,
    pub markets_total: usize,
    pub rows_total: usize,
    pub metrics: Metrics,
    pub elapsed_seconds: f64,
}

pub fn load_dataset(csv_path: &Path) -> Result<Dataset> {
    let mut reader = csv::Reader::from_path(csv_path)
        .with_context(|| format!("open csv {}", csv_path.display()))?;
    let headers = reader
        .headers()
        .context("csv headers")?
        .iter()
        .map(|s| s.to_string())
        .collect::<Vec<_>>();
    if headers.is_empty() {
        bail!("csv has no header: {}", csv_path.display());
    }

    let meta: HashSet<&str> = META_COLUMNS.iter().copied().collect();
    let feature_names = headers
        .iter()
        .filter(|name| !meta.contains(name.as_str()))
        .cloned()
        .collect::<Vec<_>>();
    let n_features = feature_names.len();
    if n_features == 0 {
        bail!("no feature columns found in {}", csv_path.display());
    }

    let col_index = |name: &str| -> Option<usize> { headers.iter().position(|h| h == name) };
    let feat_cols = feature_names
        .iter()
        .map(|name| col_index(name).expect("feature column"))
        .collect::<Vec<_>>();
    let label_col = col_index("label_up_final").context("label_up_final column")?;
    let market_start_col = col_index("market_start_ms").context("market_start_ms column")?;

    let up_best_ask_feat = feature_names.iter().position(|n| n == "up_best_ask");
    let down_best_ask_feat = feature_names.iter().position(|n| n == "down_best_ask");

    let mut features = Vec::new();
    let mut labels = Vec::new();
    let mut market_starts = Vec::new();

    for row in reader.records() {
        let row = row.context("csv row")?;
        labels.push(parse_label(row.get(label_col)));
        market_starts.push(parse_i64(row.get(market_start_col)));
        for &col in &feat_cols {
            features.push(parse_f64(row.get(col)));
        }
    }

    if labels.is_empty() {
        bail!("no rows in {}", csv_path.display());
    }

    let market_ranges = market_ranges_from_starts(&market_starts);
    Ok(Dataset {
        feature_names,
        features,
        labels,
        market_ranges,
        up_best_ask_idx: up_best_ask_feat,
        down_best_ask_idx: down_best_ask_feat,
    })
}

pub fn train(config: &TrainConfig, dataset: &Dataset, export_path: &Path, artifact_dir: &Path) -> Result<TrainSummary> {
    fs::create_dir_all(artifact_dir).with_context(|| format!("create {}", artifact_dir.display()))?;

    let n_markets = dataset.market_ranges.len();
    if n_markets < config.min_train_markets + config.test_markets {
        bail!(
            "not enough markets for walk-forward: have {n_markets}, need at least {}",
            config.min_train_markets + config.test_markets
        );
    }

    let started = Instant::now();
    let n_feat = dataset.n_features();

    #[derive(Clone, Copy)]
    struct WindowSpec {
        train_start: usize,
        train_end: usize,
        test_start: usize,
        test_end: usize,
        train_markets: usize,
    }

    let mut window_specs = Vec::new();
    let mut market_cursor = config.min_train_markets;
    while market_cursor + config.test_markets <= n_markets {
        let (train_start, train_end) = row_range(&dataset.market_ranges, 0, market_cursor);
        let (test_start, test_end) =
            row_range(&dataset.market_ranges, market_cursor, market_cursor + config.test_markets);
        window_specs.push(WindowSpec {
            train_start,
            train_end,
            test_start,
            test_end,
            train_markets: market_cursor,
        });
        market_cursor += config.step_markets;
    }

    let window_results: Vec<(WindowSummary, Vec<f64>, Vec<f64>, Vec<u8>, Vec<usize>)> = window_specs
        .par_iter()
        .map(|spec| {
            let model = train_lr(
                &dataset.features,
                &dataset.labels,
                n_feat,
                spec.train_start,
                spec.train_end,
                config.epochs,
                config.lr,
                1e-4,
            );
            let n_test = spec.test_end - spec.test_start;
            let mut logits = Vec::with_capacity(n_test);
            let mut raw_probs = Vec::with_capacity(n_test);
            let mut y_test = Vec::with_capacity(n_test);
            let mut test_row_indices = Vec::with_capacity(n_test);

            for row in spec.test_start..spec.test_end {
                let logit = predict_logit(&model, dataset.row_features(row), n_feat);
                logits.push(logit);
                raw_probs.push(sigmoid(logit));
                y_test.push(dataset.labels[row]);
                test_row_indices.push(row);
            }

            let summary = WindowSummary {
                train_markets: spec.train_markets,
                test_markets: config.test_markets,
                rows_test: n_test,
                auc_roc_raw: roc_auc_score(&y_test, &raw_probs),
                brier_raw: brier_score(&y_test, &raw_probs),
                log_loss_raw: log_loss(&y_test, &raw_probs),
            };
            (summary, logits, raw_probs, y_test, test_row_indices)
        })
        .collect();

    let mut window_summaries = Vec::with_capacity(window_results.len());
    let mut all_logits = Vec::new();
    let mut all_labels = Vec::new();
    let mut all_eval_rows = Vec::new();

    for (summary, logits, _raw_probs, y_test, row_indices) in window_results {
        window_summaries.push(summary);
        all_logits.extend(logits);
        all_labels.extend(y_test);
        all_eval_rows.extend(row_indices);
    }

    let (platt_a, platt_b) = fit_platt(&all_logits, &all_labels);
    let calibrated_probs: Vec<f64> = all_logits
        .iter()
        .map(|&logit| sigmoid(platt_a * logit + platt_b))
        .collect();
    let ece = calibration_ece(&all_labels, &calibrated_probs);
    let trading = evaluate_trades(
        dataset,
        &all_eval_rows,
        &calibrated_probs,
        config.fee_rate,
        config.slippage,
        config.min_ev,
    );

    let metrics = Metrics {
        auc_roc: roc_auc_score(&all_labels, &calibrated_probs),
        brier: brier_score(&all_labels, &calibrated_probs),
        ece,
        log_loss: log_loss(&all_labels, &calibrated_probs),
        positive_ev_hit_rate: trading.hit_rate,
        positive_ev_trades: trading.chosen,
        up_chosen: trading.up_chosen,
        down_chosen: trading.down_chosen,
        realized_pnl_per_trade: trading.pnl_per_trade,
        realized_total_pnl: trading.total_pnl,
    };

    let final_model = train_lr(
        &dataset.features,
        &dataset.labels,
        n_feat,
        0,
        dataset.n_rows(),
        config.epochs,
        config.lr,
        1e-4,
    );

    let generated_at = chrono::Utc::now().format("%Y-%m-%dT%H:%M:%SZ").to_string();
    let ts = chrono::Utc::now().timestamp_millis();
    let artifact_path = artifact_dir.join(format!("binary_outcome_model_{ts}.json"));
    let latest_path = artifact_dir.join("latest_binary_model.json");
    let metrics_path = artifact_dir.join(format!("binary_outcome_metrics_{ts}.json"));

    let artifact = ModelArtifact {
        artifact_version: "binary_outcome_v1".to_string(),
        generated_at: generated_at.clone(),
        feature_names: dataset.feature_names.clone(),
        means: final_model.mean,
        stds: final_model.std,
        weights: final_model.w,
        intercept: final_model.b,
        calibration_method: "platt".to_string(),
        platt_a,
        platt_b,
        fee_rate: config.fee_rate,
        slippage: config.slippage,
        source_export_path: export_path.display().to_string(),
        metrics: metrics.clone(),
    };

    let metrics_doc = MetricsDocument {
        generated_at,
        export_path: export_path.display().to_string(),
        manifest_path: None,
        feature_names: dataset.feature_names.clone(),
        markets_total: n_markets,
        rows_total: dataset.n_rows(),
        walk_forward_windows: window_summaries,
        metrics: metrics.clone(),
    };

    fs::write(&artifact_path, serde_json::to_string_pretty(&artifact)?)?;
    fs::copy(&artifact_path, &latest_path)?;
    fs::write(&metrics_path, serde_json::to_string_pretty(&metrics_doc)?)?;

    Ok(TrainSummary {
        artifact_path,
        latest_path,
        metrics_path,
        markets_total: n_markets,
        rows_total: dataset.n_rows(),
        metrics,
        elapsed_seconds: started.elapsed().as_secs_f64(),
    })
}

fn parse_f64(value: Option<&str>) -> f64 {
    match value {
        None | Some("") => 0.0,
        Some(s) => s.parse().unwrap_or(0.0),
    }
}

fn parse_label(value: Option<&str>) -> u8 {
    match value {
        None | Some("") => 0,
        Some(s) => s.parse::<f64>().map(|v| v as u8).unwrap_or(0),
    }
}

fn parse_i64(value: Option<&str>) -> i64 {
    match value {
        None | Some("") => 0,
        Some(s) => s.parse::<f64>().map(|v| v as i64).unwrap_or(0),
    }
}

fn market_ranges_from_starts(market_starts: &[i64]) -> Vec<(usize, usize)> {
    if market_starts.is_empty() {
        return Vec::new();
    }
    let mut ranges = Vec::new();
    let mut start = 0usize;
    let mut current = market_starts[0];
    for (idx, &market_start) in market_starts.iter().enumerate().skip(1) {
        if market_start != current {
            ranges.push((start, idx));
            start = idx;
            current = market_start;
        }
    }
    ranges.push((start, market_starts.len()));
    ranges
}

fn row_range(ranges: &[(usize, usize)], start_market: usize, end_market: usize) -> (usize, usize) {
    if start_market >= end_market {
        return (0, 0);
    }
    let start_row = ranges[start_market].0;
    let end_row = ranges[end_market - 1].1;
    (start_row, end_row)
}

fn sigmoid(x: f64) -> f64 {
    if x >= 0.0 {
        1.0 / (1.0 + (-x).exp())
    } else {
        let z = x.exp();
        z / (1.0 + z)
    }
}

fn train_lr(
    features: &[f64],
    labels: &[u8],
    n_feat: usize,
    row_start: usize,
    row_end: usize,
    epochs: usize,
    lr: f64,
    l2: f64,
) -> LrModel {
    let n_rows = row_end - row_start;
    let mut mean = vec![0.0; n_feat];
    let mut std = vec![1.0; n_feat];

    for j in 0..n_feat {
        let mut sum = 0.0;
        for row in row_start..row_end {
            sum += features[row * n_feat + j];
        }
        let avg = sum / n_rows as f64;
        mean[j] = avg;
        let mut var_sum = 0.0;
        for row in row_start..row_end {
            let diff = features[row * n_feat + j] - avg;
            var_sum += diff * diff;
        }
        let sigma = if n_rows > 1 {
            (var_sum / n_rows as f64).sqrt()
        } else {
            1.0
        };
        std[j] = if sigma <= 1e-12 { 1.0 } else { sigma };
    }

    let mut z = vec![0.0; n_rows * n_feat];
    for (local_row, row) in (row_start..row_end).enumerate() {
        for j in 0..n_feat {
            z[local_row * n_feat + j] = (features[row * n_feat + j] - mean[j]) / std[j];
        }
    }

    let mut w = vec![0.0; n_feat];
    let mut b = 0.0;
    let y: Vec<f64> = labels[row_start..row_end]
        .iter()
        .map(|&v| v as f64)
        .collect();

    for _ in 0..epochs {
        let mut grad_w = vec![0.0; n_feat];
        let mut grad_b = 0.0;
        for local_row in 0..n_rows {
            let mut dot = b;
            for j in 0..n_feat {
                dot += w[j] * z[local_row * n_feat + j];
            }
            let prob = sigmoid(dot);
            let err = prob - y[local_row];
            for j in 0..n_feat {
                grad_w[j] += err * z[local_row * n_feat + j];
            }
            grad_b += err;
        }
        let n = n_rows as f64;
        for j in 0..n_feat {
            grad_w[j] = grad_w[j] / n + l2 * w[j];
            w[j] -= lr * grad_w[j];
        }
        b -= lr * (grad_b / n);
    }

    LrModel { w, b, mean, std }
}

fn predict_logit(model: &LrModel, row: &[f64], n_feat: usize) -> f64 {
    let mut logit = model.b;
    for j in 0..n_feat {
        let z = (row[j] - model.mean[j]) / model.std[j];
        logit += model.w[j] * z;
    }
    logit
}

fn fit_platt(logits: &[f64], y: &[u8]) -> (f64, f64) {
    let mut a = 1.0;
    let mut b = 0.0;
    let n = logits.len().max(1) as f64;
    let lr = 0.01;
    for _ in 0..400 {
        let mut grad_a = 0.0;
        let mut grad_b = 0.0;
        for (&logit, &target) in logits.iter().zip(y.iter()) {
            let prob = sigmoid(a * logit + b);
            let err = prob - target as f64;
            grad_a += err * logit;
            grad_b += err;
        }
        a -= lr * grad_a / n;
        b -= lr * grad_b / n;
    }
    (a, b)
}

fn brier_score(y: &[u8], p: &[f64]) -> f64 {
    let n = y.len().max(1) as f64;
    y.iter()
        .zip(p.iter())
        .map(|(&yi, &pi)| {
            let yf = yi as f64;
            (yf - pi).powi(2)
        })
        .sum::<f64>()
        / n
}

fn log_loss(y: &[u8], p: &[f64]) -> f64 {
    let eps = 1e-12;
    let n = y.len().max(1) as f64;
    let total: f64 = y
        .iter()
        .zip(p.iter())
        .map(|(&yi, &pi)| {
            let pi = pi.clamp(eps, 1.0 - eps);
            let yf = yi as f64;
            yf * pi.ln() + (1.0 - yf) * (1.0 - pi).ln()
        })
        .sum();
    -total / n
}

fn roc_auc_score(y: &[u8], p: &[f64]) -> f64 {
    let n_pos = y.iter().filter(|&&v| v == 1).count();
    let n_neg = y.len() - n_pos;
    if n_pos == 0 || n_neg == 0 {
        return 0.5;
    }

    let mut pairs: Vec<(f64, u8)> = p.iter().copied().zip(y.iter().copied()).collect();
    pairs.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap_or(std::cmp::Ordering::Equal));

    let mut rank_sum_pos = 0.0;
    let mut i = 0usize;
    let mut rank = 1usize;
    while i < pairs.len() {
        let mut j = i;
        while j < pairs.len() && pairs[j].0 == pairs[i].0 {
            j += 1;
        }
        let avg_rank = (rank as f64 + (rank + (j - i) - 1) as f64) / 2.0;
        rank_sum_pos += avg_rank * pairs[i..j].iter().filter(|(_, y)| *y == 1).count() as f64;
        rank += j - i;
        i = j;
    }

    let n_pos_f = n_pos as f64;
    let n_neg_f = n_neg as f64;
    (rank_sum_pos - n_pos_f * (n_pos_f + 1.0) / 2.0) / (n_pos_f * n_neg_f)
}

fn calibration_ece(y: &[u8], p: &[f64]) -> f64 {
    let bins = 10usize;
    let n = y.len();
    if n == 0 {
        return 0.0;
    }
    let mut ece = 0.0;
    for bucket in 0..bins {
        let lo = bucket as f64 / bins as f64;
        let hi = (bucket + 1) as f64 / bins as f64;
        let idx: Vec<usize> = p
            .iter()
            .enumerate()
            .filter(|(_, &value)| {
                (lo <= value && value < hi) || (bucket == bins - 1 && value == 1.0)
            })
            .map(|(i, _)| i)
            .collect();
        if idx.is_empty() {
            continue;
        }
        let pm: f64 = idx.iter().map(|&i| p[i]).sum::<f64>() / idx.len() as f64;
        let ym: f64 = idx.iter().map(|&i| y[i] as f64).sum::<f64>() / idx.len() as f64;
        ece += (idx.len() as f64 / n as f64) * (pm - ym).abs();
    }
    ece
}

struct TradingResult {
    chosen: usize,
    hit_rate: f64,
    up_chosen: usize,
    down_chosen: usize,
    pnl_per_trade: f64,
    total_pnl: f64,
}

fn evaluate_trades(
    dataset: &Dataset,
    row_indices: &[usize],
    probs: &[f64],
    fee_rate: f64,
    slippage: f64,
    min_ev: f64,
) -> TradingResult {
    let up_idx = dataset.up_best_ask_idx;
    let down_idx = dataset.down_best_ask_idx;
    let mut chosen = 0usize;
    let mut wins = 0usize;
    let mut up_chosen = 0usize;
    let mut down_chosen = 0usize;
    let mut total_pnl = 0.0;

    for (&row, &prob_up) in row_indices.iter().zip(probs.iter()) {
        let up_ask = up_idx
            .map(|i| dataset.row_features(row)[i])
            .unwrap_or(f64::NAN);
        let down_ask = down_idx
            .map(|i| dataset.row_features(row)[i])
            .unwrap_or(f64::NAN);
        let ev_up = expected_value(prob_up, up_ask, fee_rate, slippage);
        let ev_down = expected_value(1.0 - prob_up, down_ask, fee_rate, slippage);
        let (Some(ev_up), Some(ev_down)) = (ev_up, ev_down) else {
            continue;
        };
        if ev_up <= min_ev && ev_down <= min_ev {
            continue;
        }

        let label = dataset.labels[row];
        if label > 1 {
            continue;
        }

        chosen += 1;
        let (ask, win, is_up) = if ev_up >= ev_down {
            up_chosen += 1;
            (up_ask, label == 1, true)
        } else {
            down_chosen += 1;
            (down_ask, label == 0, false)
        };
        let _ = is_up;
        if win {
            wins += 1;
            total_pnl += ((1.0 - fee_rate) / (ask + slippage)) - (1.0 + fee_rate);
        } else {
            total_pnl -= 1.0 + fee_rate;
        }
    }

    TradingResult {
        chosen,
        hit_rate: if chosen == 0 { 0.0 } else { wins as f64 / chosen as f64 },
        up_chosen,
        down_chosen,
        pnl_per_trade: if chosen == 0 { 0.0 } else { total_pnl / chosen as f64 },
        total_pnl,
    }
}

fn expected_value(prob_win: f64, ask: f64, fee_rate: f64, slippage: f64) -> Option<f64> {
    if !prob_win.is_finite() || !ask.is_finite() || ask <= 0.0 {
        return None;
    }
    let entry = ask + slippage;
    if entry <= 0.0 {
        return None;
    }
    Some((prob_win * (1.0 - fee_rate) / entry) - (1.0 + fee_rate))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sigmoid_matches_python_style() {
        assert!((sigmoid(0.0) - 0.5).abs() < 1e-12);
    }
}