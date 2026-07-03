use anyhow::{Context, Result, bail};
use async_compression::tokio::bufread::GzipEncoder;
use axum::{Router, extract::State, http::StatusCode, response::Json, routing::get};
use chrono::{DateTime, Utc};
use reqwest::Body;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;
use tokio::io::BufReader;
use tokio::sync::Mutex;
use tokio_util::io::ReaderStream;
use tracing::{error, info, warn};

const SERVER_HOST: &str = "0.0.0.0";
const SERVER_PORT: u16 = 8007;

const DEFAULT_DATABASE_PATH: &str = "/var/lib/polymarket/polymarket_btc_data.db";
const DEFAULT_STATE_PATH: &str = "/var/lib/polymarket/polymarket_btc_data.backup_state.json";
const DEFAULT_VACUUM_TEMP_DIRS: &str = "/var/tmp/polymarket-db-maintenance:/tmp";
const DEFAULT_BACKUP_INTERVAL_SECS: u64 = 7 * 24 * 60 * 60;
const DEFAULT_PRUNE_INTERVAL_SECS: u64 = 24 * 60 * 60;
const DEFAULT_PRUNE_RETENTION_DAYS: u64 = 14;
const MIN_SPACE_HEADROOM_BYTES: u64 = 512 * 1024 * 1024;

const CDN_REGION: &str = "ny";
const CDN_STORAGE_ZONE: &str = "YOUR_STORAGE_ZONE";
const CDN_FOLDER: &str = "polymarket-bot";

#[derive(Clone, Debug, Serialize)]
struct AppConfig {
    database_path: PathBuf,
    backup_interval_secs: u64,
    prune_interval_secs: u64,
    prune_retention_days: u64,
    prune_recent_backup_max_age_secs: u64,
    backup_state_path: PathBuf,
    vacuum_temp_dirs: Vec<PathBuf>,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
struct PersistentState {
    last_backup_file: Option<String>,
    last_backup_completed_at: Option<String>,
    last_prune_completed_at: Option<String>,
}

struct AppState {
    config: AppConfig,
    persistent_state: Mutex<PersistentState>,
    operation_lock: Mutex<()>,
}

fn parse_env_u64(name: &str, default: u64) -> Result<u64> {
    match std::env::var(name) {
        Ok(value) => value
            .parse::<u64>()
            .with_context(|| format!("{name} must be a positive integer")),
        Err(std::env::VarError::NotPresent) => Ok(default),
        Err(error) => Err(error).with_context(|| format!("failed to read {name}")),
    }
}

fn split_path_list(value: &str) -> Vec<PathBuf> {
    value
        .split(':')
        .map(str::trim)
        .filter(|entry| !entry.is_empty())
        .map(PathBuf::from)
        .collect()
}

fn load_config() -> Result<AppConfig> {
    let backup_interval_secs = parse_env_u64("DB_BACKUP_INTERVAL_SECS", DEFAULT_BACKUP_INTERVAL_SECS)?;
    let prune_interval_secs = parse_env_u64("DB_PRUNE_INTERVAL_SECS", DEFAULT_PRUNE_INTERVAL_SECS)?;
    let prune_retention_days = parse_env_u64("DB_PRUNE_RETENTION_DAYS", DEFAULT_PRUNE_RETENTION_DAYS)?;

    let prune_recent_backup_max_age_secs = match std::env::var(
        "DB_PRUNE_REQUIRED_RECENT_BACKUP_MAX_AGE_SECS",
    ) {
        Ok(value) => value.parse::<u64>().with_context(|| {
            "DB_PRUNE_REQUIRED_RECENT_BACKUP_MAX_AGE_SECS must be a positive integer"
        })?,
        Err(std::env::VarError::NotPresent) => backup_interval_secs
            .saturating_add(prune_interval_secs.max(3600)),
        Err(error) => {
            return Err(error)
                .with_context(|| "failed to read DB_PRUNE_REQUIRED_RECENT_BACKUP_MAX_AGE_SECS");
        }
    };

    let database_path = PathBuf::from(
        std::env::var("DB_BACKUP_DATABASE_PATH")
            .unwrap_or_else(|_| DEFAULT_DATABASE_PATH.to_string()),
    );
    let backup_state_path = PathBuf::from(
        std::env::var("DB_BACKUP_STATE_PATH")
            .unwrap_or_else(|_| DEFAULT_STATE_PATH.to_string()),
    );
    let vacuum_temp_dirs = split_path_list(
        &std::env::var("DB_VACUUM_TEMP_DIRS")
            .unwrap_or_else(|_| DEFAULT_VACUUM_TEMP_DIRS.to_string()),
    );

    if vacuum_temp_dirs.is_empty() {
        bail!("DB_VACUUM_TEMP_DIRS produced no usable directories");
    }

    Ok(AppConfig {
        database_path,
        backup_interval_secs,
        prune_interval_secs,
        prune_retention_days,
        prune_recent_backup_max_age_secs,
        backup_state_path,
        vacuum_temp_dirs,
    })
}

async fn load_persistent_state(path: &Path) -> Result<PersistentState> {
    match tokio::fs::read_to_string(path).await {
        Ok(raw) => serde_json::from_str(&raw)
            .with_context(|| format!("failed to parse state file {}", path.display())),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(PersistentState::default()),
        Err(error) => Err(error)
            .with_context(|| format!("failed to read state file {}", path.display())),
    }
}

async fn save_persistent_state(path: &Path, state: &PersistentState) -> Result<()> {
    if let Some(parent) = path.parent() {
        tokio::fs::create_dir_all(parent)
            .await
            .with_context(|| format!("failed to create {}", parent.display()))?;
    }

    let tmp_path = PathBuf::from(format!("{}.tmp", path.display()));
    let payload = serde_json::to_vec_pretty(state).context("failed to serialize persistent state")?;

    tokio::fs::write(&tmp_path, payload)
        .await
        .with_context(|| format!("failed to write {}", tmp_path.display()))?;
    tokio::fs::rename(&tmp_path, path)
        .await
        .with_context(|| format!("failed to move {} into place", tmp_path.display()))?;

    Ok(())
}

async fn mutate_persistent_state<F>(state: &AppState, mutate: F) -> Result<()>
where
    F: FnOnce(&mut PersistentState),
{
    let snapshot = {
        let mut guard = state.persistent_state.lock().await;
        mutate(&mut guard);
        guard.clone()
    };

    save_persistent_state(&state.config.backup_state_path, &snapshot).await
}

fn format_gib(bytes: u64) -> String {
    format!("{:.2} GiB", bytes as f64 / 1_073_741_824.0)
}

fn space_margin_bytes(bytes: u64) -> u64 {
    MIN_SPACE_HEADROOM_BYTES.max(bytes / 10)
}

fn escape_sqlite_path(path: &Path) -> String {
    path.to_string_lossy().replace('\'', "''")
}

fn file_name_with_suffix(path: &Path, suffix: &str) -> PathBuf {
    let file_name = path
        .file_name()
        .map(|value| value.to_string_lossy().to_string())
        .unwrap_or_else(|| "database.db".to_string());
    path.with_file_name(format!("{file_name}{suffix}"))
}

fn parse_rfc3339_utc(value: &str) -> Option<DateTime<Utc>> {
    DateTime::parse_from_rfc3339(value)
        .ok()
        .map(|parsed| parsed.with_timezone(&Utc))
}

fn has_recent_backup(snapshot: &PersistentState, max_age_secs: u64) -> bool {
    has_recent_timestamp(snapshot.last_backup_completed_at.as_deref(), max_age_secs)
}

fn timestamp_age_secs(value: Option<&str>) -> Option<u64> {
    value.and_then(parse_rfc3339_utc).map(|timestamp| {
        Utc::now()
            .signed_duration_since(timestamp)
            .num_seconds()
            .max(0) as u64
    })
}

fn has_recent_timestamp(value: Option<&str>, max_age_secs: u64) -> bool {
    timestamp_age_secs(value)
        .map(|age_secs| age_secs <= max_age_secs)
        .unwrap_or(false)
}

async fn free_bytes_for_path(path: &Path) -> Result<u64> {
    let output = tokio::process::Command::new("df")
        .arg("-Pk")
        .arg(path)
        .output()
        .await
        .with_context(|| format!("failed to inspect free space for {}", path.display()))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        bail!("df failed for {}: {stderr}", path.display());
    }

    let stdout = String::from_utf8(output.stdout).context("df output was not valid UTF-8")?;
    let line = stdout
        .lines()
        .find(|candidate| candidate.starts_with('/'))
        .or_else(|| stdout.lines().nth(1))
        .with_context(|| format!("unexpected df output for {}", path.display()))?;

    let fields: Vec<&str> = line.split_whitespace().collect();
    if fields.len() < 4 {
        bail!("unexpected df fields for {}: {line}", path.display());
    }

    let available_kb = fields[3]
        .parse::<u64>()
        .with_context(|| format!("failed to parse df free space for {}", path.display()))?;
    Ok(available_kb * 1024)
}

async fn move_file(src: &Path, dst: &Path) -> Result<()> {
    let output = tokio::process::Command::new("mv")
        .arg(src)
        .arg(dst)
        .output()
        .await
        .with_context(|| format!("failed to move {} to {}", src.display(), dst.display()))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        bail!("mv failed from {} to {}: {stderr}", src.display(), dst.display());
    }

    Ok(())
}

async fn checkpoint_wal(config: &AppConfig) -> Result<()> {
    info!("Running WAL checkpoint on {}", config.database_path.display());

    let output = tokio::process::Command::new("sqlite3")
        .arg(&config.database_path)
        .arg("PRAGMA wal_checkpoint(TRUNCATE);")
        .output()
        .await
        .context("failed to run sqlite3 WAL checkpoint")?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        bail!("WAL checkpoint failed: {stderr}");
    }

    let stdout = String::from_utf8_lossy(&output.stdout);
    info!("WAL checkpoint complete: {}", stdout.trim());
    Ok(())
}

async fn upload_backup(config: &AppConfig, cdn_access_key: &str) -> Result<String> {
    let timestamp = Utc::now().format("%Y-%m-%d_%H%M%S").to_string();
    let filename = format!("polymarket_btc_data_{timestamp}.db.gz");
    let upload_url = format!(
        "https://{CDN_REGION}.storage.bunnycdn.com/{CDN_STORAGE_ZONE}/{CDN_FOLDER}/{filename}"
    );

    info!("Opening database file: {}", config.database_path.display());
    let file = tokio::fs::File::open(&config.database_path)
        .await
        .with_context(|| format!("failed to open {}", config.database_path.display()))?;

    let file_size = file.metadata().await.map(|metadata| metadata.len()).unwrap_or(0);
    info!("Database file size: {}", format_gib(file_size));

    let buf_reader = BufReader::with_capacity(8 * 1024 * 1024, file);
    let gzip_encoder = GzipEncoder::new(buf_reader);
    let stream = ReaderStream::with_capacity(gzip_encoder, 2 * 1024 * 1024);
    let body = Body::wrap_stream(stream);

    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(24 * 60 * 60))
        .build()
        .context("failed to build backup HTTP client")?;

    info!("Uploading compressed backup to {upload_url}");
    let response = client
        .put(&upload_url)
        .header("AccessKey", cdn_access_key)
        .header("Content-Type", "application/octet-stream")
        .body(body)
        .send()
        .await
        .context("CDN upload request failed")?;

    let status = response.status();
    let body_text = response.text().await.unwrap_or_default();
    if status == StatusCode::CREATED || status == StatusCode::OK {
        info!("Backup uploaded successfully: {filename} (HTTP {status})");
        Ok(filename)
    } else {
        bail!("CDN upload failed: HTTP {status} — {body_text}");
    }
}

async fn pick_vacuum_output_path(config: &AppConfig, required_bytes: u64) -> Result<PathBuf> {
    let file_name = config
        .database_path
        .file_name()
        .map(|value| value.to_string_lossy().to_string())
        .unwrap_or_else(|| "polymarket_btc_data.db".to_string());
    let timestamp = Utc::now().format("%Y%m%d_%H%M%S");

    let mut candidates = Vec::new();
    for dir in &config.vacuum_temp_dirs {
        if let Err(error) = tokio::fs::create_dir_all(dir).await {
            warn!("Skipping {}: {error}", dir.display());
            continue;
        }

        let free_bytes = match free_bytes_for_path(dir).await {
            Ok(value) => value,
            Err(error) => {
                warn!("Skipping {}: {error:#}", dir.display());
                continue;
            }
        };

        candidates.push(format!("{}={}", dir.display(), format_gib(free_bytes)));
        if free_bytes >= required_bytes {
            let output_path = dir.join(format!("{file_name}.vacuum.{timestamp}.db"));
            info!(
                "Selected vacuum staging path {} (free: {}, required: {})",
                output_path.display(),
                format_gib(free_bytes),
                format_gib(required_bytes)
            );
            return Ok(output_path);
        }
    }

    bail!(
        "no vacuum staging directory has enough free space (required {}, candidates: {})",
        format_gib(required_bytes),
        candidates.join(", ")
    );
}

async fn safe_swap_vacuumed_database(config: &AppConfig, vacuumed_path: &Path) -> Result<()> {
    let vacuumed_size = tokio::fs::metadata(vacuumed_path)
        .await
        .with_context(|| format!("failed to stat {}", vacuumed_path.display()))?
        .len();
    let required_destination_bytes = vacuumed_size + space_margin_bytes(vacuumed_size);

    let database_dir = config.database_path.parent().unwrap_or_else(|| Path::new("/"));
    let destination_free_bytes = free_bytes_for_path(database_dir).await?;
    if destination_free_bytes < required_destination_bytes {
        let _ = tokio::fs::remove_file(vacuumed_path).await;
        bail!(
            "insufficient free space on {} for safe database swap: need {}, have {}",
            database_dir.display(),
            format_gib(required_destination_bytes),
            format_gib(destination_free_bytes)
        );
    }

    let swap_path = file_name_with_suffix(&config.database_path, ".swap");
    let backup_suffix = format!(".preprune-{}.bak", Utc::now().format("%Y%m%d_%H%M%S"));
    let old_backup_path = file_name_with_suffix(&config.database_path, &backup_suffix);

    if tokio::fs::try_exists(&swap_path).await.unwrap_or(false) {
        let _ = tokio::fs::remove_file(&swap_path).await;
    }

    move_file(vacuumed_path, &swap_path).await?;

    let wal_path = PathBuf::from(format!("{}-wal", config.database_path.display()));
    let shm_path = PathBuf::from(format!("{}-shm", config.database_path.display()));
    let _ = tokio::fs::remove_file(&wal_path).await;
    let _ = tokio::fs::remove_file(&shm_path).await;

    tokio::fs::rename(&config.database_path, &old_backup_path)
        .await
        .with_context(|| {
            format!(
                "failed to move {} to {} before swap",
                config.database_path.display(),
                old_backup_path.display()
            )
        })?;

    if let Err(error) = tokio::fs::rename(&swap_path, &config.database_path).await {
        let _ = tokio::fs::rename(&old_backup_path, &config.database_path).await;
        let _ = tokio::fs::remove_file(&swap_path).await;
        bail!("failed to swap vacuumed database into place: {error}");
    }

    if let Err(error) = tokio::fs::remove_file(&old_backup_path).await {
        warn!(
            "Vacuum swap succeeded but failed to remove {}: {error}",
            old_backup_path.display()
        );
    }

    info!(
        "Vacuumed database swapped into place safely (destination free before swap: {})",
        format_gib(destination_free_bytes)
    );

    Ok(())
}

async fn prune_old_data(config: &AppConfig) -> Result<()> {
    let cutoff_ms = Utc::now().timestamp_millis()
        - (config.prune_retention_days as i64 * 86_400_000);
    info!(
        "Pruning data older than {} days (cutoff: {})",
        config.prune_retention_days,
        chrono::DateTime::from_timestamp_millis(cutoff_ms)
            .map(|value| value.format("%Y-%m-%d %H:%M").to_string())
            .unwrap_or_else(|| "unknown".to_string())
    );

    checkpoint_wal(config).await?;

    let prune_targets = [
        ("polymarket_ticks_ms", "source_ts_ms"),
        ("binance_ticks_ms", "source_ts_ms"),
        ("binance_trades", "trade_time"),
        ("lag_pairs_ms", "paired_at_ms"),
        ("binance_candles_1s", "candle_start"),
        ("binance_candles_5s", "candle_start"),
    ];

    for (table, timestamp_column) in &prune_targets {
        let sql = format!(
            "DELETE FROM {table} WHERE {timestamp_column} < {cutoff_ms}; SELECT changes();"
        );
        info!("Pruning {table} where {timestamp_column} < {cutoff_ms}");

        let output = tokio::process::Command::new("sqlite3")
            .arg(&config.database_path)
            .arg(&sql)
            .output()
            .await
            .with_context(|| format!("failed to prune {table}"))?;

        if !output.status.success() {
            let stderr = String::from_utf8_lossy(&output.stderr);
            warn!("Prune {table} failed: {stderr}");
            continue;
        }

        let stdout = String::from_utf8_lossy(&output.stdout);
        let deleted = stdout.lines().last().unwrap_or("0").trim();
        info!("Pruned {table}: {deleted} rows deleted");
    }

    checkpoint_wal(config).await?;

    let current_size = tokio::fs::metadata(&config.database_path)
        .await
        .with_context(|| format!("failed to stat {}", config.database_path.display()))?
        .len();
    let required_staging_bytes = current_size + space_margin_bytes(current_size);
    let vacuumed_path = pick_vacuum_output_path(config, required_staging_bytes).await?;

    info!("Running VACUUM INTO {}", vacuumed_path.display());
    let output = tokio::process::Command::new("sqlite3")
        .arg(&config.database_path)
        .arg(format!("VACUUM INTO '{}';", escape_sqlite_path(&vacuumed_path)))
        .output()
        .await
        .context("failed to run VACUUM INTO")?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        let _ = tokio::fs::remove_file(&vacuumed_path).await;
        bail!("VACUUM INTO failed: {stderr}");
    }

    let old_size = tokio::fs::metadata(&config.database_path).await?.len();
    let new_size = tokio::fs::metadata(&vacuumed_path).await?.len();
    info!(
        "VACUUM complete: {} -> {} (freed {})",
        format_gib(old_size),
        format_gib(new_size),
        format_gib(old_size.saturating_sub(new_size))
    );

    safe_swap_vacuumed_database(config, &vacuumed_path).await?;
    info!("Prune and safe VACUUM cycle complete");

    Ok(())
}

async fn run_backup(state: &AppState, cdn_access_key: &str) -> Result<String> {
    let _operation_guard = state.operation_lock.lock().await;
    let started_at = std::time::Instant::now();

    if let Err(error) = checkpoint_wal(&state.config).await {
        warn!("WAL checkpoint failed before backup (continuing anyway): {error:#}");
    }

    let filename = upload_backup(&state.config, cdn_access_key).await?;
    let completed_at = Utc::now().to_rfc3339();

    if let Err(error) = mutate_persistent_state(state, |snapshot| {
        snapshot.last_backup_file = Some(filename.clone());
        snapshot.last_backup_completed_at = Some(completed_at.clone());
    })
    .await
    {
        error!("failed to persist backup state after upload: {error:#}");
    }

    info!(
        "Upload complete in {:.1} minutes: {filename}",
        started_at.elapsed().as_secs_f64() / 60.0
    );

    match prune_old_data(&state.config).await {
        Ok(()) => {
            let prune_completed_at = Utc::now().to_rfc3339();
            if let Err(error) = mutate_persistent_state(state, |snapshot| {
                snapshot.last_prune_completed_at = Some(prune_completed_at.clone());
            })
            .await
            {
                error!("failed to persist prune state after backup: {error:#}");
            }
            info!("Post-backup prune succeeded");
        }
        Err(error) => error!("Post-backup prune failed (backup upload is safe): {error:#}"),
    }

    info!(
        "Full backup/prune cycle complete in {:.1} minutes",
        started_at.elapsed().as_secs_f64() / 60.0
    );

    Ok(filename)
}

async fn run_prune_if_safe(state: &AppState, reason: &str) -> Result<bool> {
    let _operation_guard = state.operation_lock.lock().await;
    let snapshot = state.persistent_state.lock().await.clone();

    if !has_recent_backup(&snapshot, state.config.prune_recent_backup_max_age_secs) {
        warn!(
            "Skipping prune ({reason}): no successful backup within the last {} seconds (last_backup_completed_at={:?})",
            state.config.prune_recent_backup_max_age_secs,
            snapshot.last_backup_completed_at
        );
        return Ok(false);
    }

    prune_old_data(&state.config).await?;

    let prune_completed_at = Utc::now().to_rfc3339();
    if let Err(error) = mutate_persistent_state(state, |persistent| {
        persistent.last_prune_completed_at = Some(prune_completed_at.clone());
    })
    .await
    {
        error!("failed to persist prune state: {error:#}");
    }

    info!("Prune completed successfully ({reason})");
    Ok(true)
}

async fn health(State(state): State<Arc<AppState>>) -> Json<Value> {
    let snapshot = state.persistent_state.lock().await.clone();
    let operation_in_progress = state.operation_lock.try_lock().is_err();
    let recent_backup_available = has_recent_backup(
        &snapshot,
        state.config.prune_recent_backup_max_age_secs,
    );
    let backup_age_secs = timestamp_age_secs(snapshot.last_backup_completed_at.as_deref());
    let prune_recent_threshold_secs = state
        .config
        .prune_interval_secs
        .saturating_add(3600)
        .max(3600);
    let recent_prune_available = if state.config.prune_interval_secs == 0 {
        true
    } else {
        has_recent_timestamp(
            snapshot.last_prune_completed_at.as_deref(),
            prune_recent_threshold_secs,
        )
    };
    let prune_age_secs = timestamp_age_secs(snapshot.last_prune_completed_at.as_deref());
    let status = if recent_backup_available && recent_prune_available {
        "ok"
    } else if (!recent_backup_available && snapshot.last_backup_completed_at.is_some())
        || (!recent_prune_available && snapshot.last_prune_completed_at.is_some())
    {
        "error"
    } else {
        "warning"
    };

    Json(json!({
        "service": "db-backup",
        "status": status,
        "last_backup": snapshot.last_backup_file,
        "last_backup_completed_at": snapshot.last_backup_completed_at,
        "last_prune_completed_at": snapshot.last_prune_completed_at,
        "backup_in_progress": operation_in_progress,
        "recent_backup_available": recent_backup_available,
        "recent_prune_available": recent_prune_available,
        "backup_age_secs": backup_age_secs,
        "prune_age_secs": prune_age_secs,
        "prune_recent_threshold_secs": if state.config.prune_interval_secs == 0 {
            serde_json::Value::Null
        } else {
            json!(prune_recent_threshold_secs)
        },
        "config": {
            "database_path": state.config.database_path.display().to_string(),
            "backup_interval_secs": state.config.backup_interval_secs,
            "prune_interval_secs": state.config.prune_interval_secs,
            "prune_retention_days": state.config.prune_retention_days,
            "prune_recent_backup_max_age_secs": state.config.prune_recent_backup_max_age_secs,
            "vacuum_temp_dirs": state
                .config
                .vacuum_temp_dirs
                .iter()
                .map(|path| path.display().to_string())
                .collect::<Vec<_>>(),
            "state_path": state.config.backup_state_path.display().to_string(),
        }
    }))
}

async fn trigger_backup(State(state): State<Arc<AppState>>) -> (StatusCode, Json<Value>) {
    let cdn_key = match std::env::var("BUNNY_CDN_ACCESS_KEY") {
        Ok(value) => value,
        Err(_) => {
            return (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(json!({"error": "BUNNY_CDN_ACCESS_KEY not set"})),
            );
        }
    };

    if state.operation_lock.try_lock().is_err() {
        return (
            StatusCode::CONFLICT,
            Json(json!({"error": "backup/prune already in progress"})),
        );
    }

    let state_clone = Arc::clone(&state);
    tokio::spawn(async move {
        match run_backup(&state_clone, &cdn_key).await {
            Ok(filename) => info!("Manual backup succeeded: {filename}"),
            Err(error) => error!("Manual backup failed: {error:#}"),
        }
    });

    (
        StatusCode::ACCEPTED,
        Json(json!({"status": "backup_started"})),
    )
}

async fn trigger_prune(State(state): State<Arc<AppState>>) -> (StatusCode, Json<Value>) {
    if state.operation_lock.try_lock().is_err() {
        return (
            StatusCode::CONFLICT,
            Json(json!({"error": "backup/prune already in progress"})),
        );
    }

    let state_clone = Arc::clone(&state);
    tokio::spawn(async move {
        match run_prune_if_safe(&state_clone, "manual trigger").await {
            Ok(true) => info!("Manual prune succeeded"),
            Ok(false) => warn!("Manual prune skipped because no recent backup was available"),
            Err(error) => error!("Manual prune failed: {error:#}"),
        }
    });

    (
        StatusCode::ACCEPTED,
        Json(json!({"status": "prune_started"})),
    )
}

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "db_backup=info".parse().unwrap()),
        )
        .init();

    let config = load_config()?;
    let cdn_access_key = std::env::var("BUNNY_CDN_ACCESS_KEY")
        .context("BUNNY_CDN_ACCESS_KEY environment variable must be set")?;
    let persistent_state = load_persistent_state(&config.backup_state_path).await?;

    info!("db-backup service starting");
    info!("Database: {}", config.database_path.display());
    info!("CDN target: {CDN_STORAGE_ZONE}/{CDN_FOLDER}/");
    info!("Backup interval: {} seconds", config.backup_interval_secs);
    info!("Prune interval: {} seconds", config.prune_interval_secs);
    info!("Prune retention: {} days", config.prune_retention_days);
    info!(
        "Prune requires a backup no older than {} seconds",
        config.prune_recent_backup_max_age_secs
    );
    info!(
        "Vacuum staging dirs: {}",
        config
            .vacuum_temp_dirs
            .iter()
            .map(|path| path.display().to_string())
            .collect::<Vec<_>>()
            .join(", ")
    );

    let state = Arc::new(AppState {
        config: config.clone(),
        persistent_state: Mutex::new(persistent_state),
        operation_lock: Mutex::new(()),
    });

    if config.backup_interval_secs > 0 {
        let backup_state = Arc::clone(&state);
        let backup_key = cdn_access_key.clone();
        tokio::spawn(async move {
            let mut interval = tokio::time::interval(Duration::from_secs(config.backup_interval_secs));
            interval.tick().await;
            loop {
                interval.tick().await;
                info!("Scheduled backup triggered");
                match run_backup(&backup_state, &backup_key).await {
                    Ok(filename) => info!("Scheduled backup succeeded: {filename}"),
                    Err(error) => error!("Scheduled backup failed: {error:#}"),
                }
            }
        });
    } else {
        warn!("Scheduled backups are disabled because DB_BACKUP_INTERVAL_SECS=0");
    }

    if state.config.prune_interval_secs > 0 {
        let prune_state = Arc::clone(&state);
        let prune_interval_secs = state.config.prune_interval_secs;
        tokio::spawn(async move {
            let mut interval = tokio::time::interval(Duration::from_secs(prune_interval_secs));
            interval.tick().await;
            loop {
                interval.tick().await;
                info!("Scheduled prune triggered");
                match run_prune_if_safe(&prune_state, "scheduled interval").await {
                    Ok(true) => info!("Scheduled prune succeeded"),
                    Ok(false) => warn!("Scheduled prune skipped because no recent backup was available"),
                    Err(error) => error!("Scheduled prune failed: {error:#}"),
                }
            }
        });
    } else {
        warn!("Scheduled prunes are disabled because DB_PRUNE_INTERVAL_SECS=0");
    }

    let app = Router::new()
        .route("/health", get(health))
        .route("/backup", axum::routing::post(trigger_backup))
        .route("/prune", axum::routing::post(trigger_prune))
        .with_state(state);

    let address = format!("{SERVER_HOST}:{SERVER_PORT}");
    info!("Listening on {address}");
    let listener = tokio::net::TcpListener::bind(&address).await?;
    axum::serve(listener, app).await?;

    Ok(())
}
