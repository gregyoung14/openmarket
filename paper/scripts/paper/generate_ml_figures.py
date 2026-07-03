#!/usr/bin/env python3
"""Tier-3 ML / throughput figures for the OpenMarket paper.

Reads local HF release artifacts and writes:
  - assets/figures/feature-correlation.pdf
  - assets/figures/calibration-curve.pdf
  - assets/figures/walk-forward-metrics.pdf
  - assets/figures/throughput-bench.pdf
  - assets/stats/benchmarks.json
  - extends assets/stats/characterization.tex with bench + hardware macros

Usage:
  paper/.venv/bin/python scripts/paper/generate_ml_figures.py
"""
from __future__ import annotations

import json
import math
import os
import platform
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import matplotlib.pyplot as plt
import numpy as np
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[2]
REPO = ROOT.parent
FIG_DIR = ROOT / "assets/figures"
STATS_DIR = ROOT / "assets/stats"
FEATURES_PARQUET = REPO / "data/hf_release/features_parquet/step2_100ms"
FEATURES_CSV = REPO / "data/hf_release/features_exports"
STEP3_GLOB = "step3_binary_calibration_*.csv"
STAGING_DB = REPO / "data/hf_release/staging/polymarket_btc_data_2026-05-14_003913.recovered.db"
MODEL_JSON = REPO / "models/hf_staging/v0.2.1/binary_outcome_model.json"
METRICS_JSON = REPO / "models/hf_staging/v0.2.1/binary_outcome_metrics_1782951964345.json"
LEDGER_JSON = REPO / "research/legacy-ml/strategies/v9_regime_filter/ledger.json"
UNIFIED_LAG = REPO / "data/hf_release/unified_parquet/lag_pairs_ms"
BASELINE_JSON = REPO / "benchmarks/baselines/v0.1-sample.json"
CHAR_JSON = STATS_DIR / "characterization.json"
CHAR_TEX = STATS_DIR / "characterization.tex"
NY_TZ = ZoneInfo("America/New_York")

META_COLS = {
    "market_slug", "market_start_ms", "market_end_ms", "ts_ms",
    "market_open_price", "market_close_price", "label_up_final",
}


def sigmoid(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=np.float64)
    return np.where(x >= 0, 1 / (1 + np.exp(-x)), np.exp(x) / (1 + np.exp(x)))


def platt_calibrate(raw_prob: np.ndarray, a: float, b: float) -> np.ndarray:
    clipped = np.clip(raw_prob, 1e-6, 1 - 1e-6)
    logit = np.log(clipped / (1.0 - clipped))
    return sigmoid(a * logit + b)


def load_model(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def score_rows(model: dict, X: np.ndarray) -> np.ndarray:
    means = np.array(model["means"], dtype=np.float64)
    stds = np.array(model["stds"], dtype=np.float64)
    weights = np.array(model["weights"], dtype=np.float64)
    stds = np.where(np.abs(stds) <= 1e-12, 1.0, stds)
    z = (X - means) / stds
    logits = z @ weights + float(model["intercept"])
    raw = sigmoid(logits)
    return platt_calibrate(raw, float(model["platt_a"]), float(model["platt_b"]))


def find_step3_csv() -> Path | None:
    exports = sorted((REPO / "data/hf_release/features_exports").glob(STEP3_GLOB))
    for path in reversed(exports):
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        if len(lines) > 1:
            return path
    return None


def export_step3_if_needed() -> Path | None:
    existing = find_step3_csv()
    if existing is not None:
        return existing
    if not STAGING_DB.exists():
        print(f"warning: no step3 CSV and staging DB missing: {STAGING_DB}", flush=True)
        return None

    env = os.environ.copy()
    env.update({
        "DATABASE_FILE": str(STAGING_DB),
        "ML_EXPORT_DIR": str(REPO / "data/hf_release/features_exports"),
        "ARCHIVE_EXPORT": "1",
    })
    print("exporting step3 calibration CSV via ml_export...", flush=True)
    subprocess.run(
        ["cargo", "run", "-p", "market-data-recorder", "--bin", "ml_export", "--release", "--", "step3"],
        cwd=REPO,
        env=env,
        check=False,
    )
    return find_step3_csv()


def load_feature_matrix() -> tuple[np.ndarray, list[str]]:
    pq_files = sorted(FEATURES_PARQUET.rglob("*.parquet"))
    if pq_files:
        table = pq.read_table(pq_files[0])
        df_cols = [c for c in table.column_names if c not in {"ts_ms", "bucket_ms"}]
        numeric = []
        names = []
        for col in df_cols:
            arr = table[col].to_numpy(zero_copy_only=False)
            if np.issubdtype(arr.dtype, np.number):
                numeric.append(arr.astype(np.float64))
                names.append(col)
        if numeric:
            return np.column_stack(numeric), names

    csv_files = sorted(FEATURES_CSV.glob("step2_features_100ms_*.csv"))
    if not csv_files:
        return np.empty((0, 0)), []
    import csv

    rows: list[list[float]] = []
    names: list[str] = []
    with csv_files[-1].open(encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        names = [c for c in reader.fieldnames or [] if c not in {"ts_ms", "bucket_ms"}]
        for row in reader:
            rows.append([float(row.get(c, 0) or 0) for c in names])
    return np.array(rows, dtype=np.float64), names


def plot_feature_correlation(out: Path) -> dict:
    X, names = load_feature_matrix()
    if X.size == 0 or len(names) < 2:
        print("warning: insufficient feature data for correlation heatmap", flush=True)
        return {"rows": 0, "features": 0}

    # Drop near-constant columns for readability
    keep = [i for i in range(X.shape[1]) if np.nanstd(X[:, i]) > 1e-12]
    X = X[:, keep]
    names = [names[i] for i in keep]
    if len(names) > 24:
        # Prefer microstructure + target columns
        priority = [n for n in names if any(k in n for k in (
            "ret_", "rv_", "volume", "imbalance", "lag_", "spread", "target", "vwap", "tps"
        ))]
        if len(priority) >= 8:
            idx = [names.index(n) for n in priority[:24]]
            X = X[:, idx]
            names = [names[i] for i in idx]

    corr = np.corrcoef(X, rowvar=False)
    corr = np.nan_to_num(corr, nan=0.0)

    fig, ax = plt.subplots(figsize=(7.2, 6.0))
    im = ax.imshow(corr, cmap="RdBu_r", vmin=-1, vmax=1, aspect="auto")
    ax.set_xticks(range(len(names)))
    ax.set_yticks(range(len(names)))
    short = [n.replace("_", "\n") if len(n) > 12 else n for n in names]
    ax.set_xticklabels(short, rotation=90, fontsize=5)
    ax.set_yticklabels(short, fontsize=5)
    ax.set_title(f"Step-2 Feature Correlation (100ms, n={X.shape[0]:,})")
    fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
    fig.tight_layout()
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    return {"rows": int(X.shape[0]), "features": int(X.shape[1])}


def calibration_bins(y: np.ndarray, p: np.ndarray, n_bins: int = 10) -> tuple[list[dict], float]:
    rows: list[dict] = []
    ece = 0.0
    n = len(y)
    if n == 0:
        return rows, 0.0
    for bucket in range(n_bins):
        lo, hi = bucket / n_bins, (bucket + 1) / n_bins
        if bucket == n_bins - 1:
            mask = (p >= lo) & (p <= hi)
        else:
            mask = (p >= lo) & (p < hi)
        if not np.any(mask):
            rows.append({"bin": bucket, "count": 0, "pred_mean": 0.0, "realized": 0.0})
            continue
        pm = float(np.mean(p[mask]))
        ym = float(np.mean(y[mask]))
        cnt = int(mask.sum())
        ece += (cnt / n) * abs(pm - ym)
        rows.append({"bin": bucket, "count": cnt, "pred_mean": pm, "realized": ym})
    return rows, float(ece)


def plot_calibration_curve(out: Path) -> dict:
    import csv

    step3 = export_step3_if_needed()
    model = load_model(MODEL_JSON) if MODEL_JSON.exists() else {}
    feature_names = model.get("feature_names", [])

    y_list: list[int] = []
    p_list: list[float] = []

    if step3 is not None and feature_names:
        with step3.open(encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                label = int(float(row.get("label_up_final", 0) or 0))
                feats = [float(row.get(name, 0) or 0) for name in feature_names]
                X = np.array([feats], dtype=np.float64)
                prob = float(score_rows(model, X)[0])
                y_list.append(label)
                p_list.append(prob)

    metrics_doc = {}
    if METRICS_JSON.exists():
        metrics_doc = json.loads(METRICS_JSON.read_text(encoding="utf-8"))

    if not y_list and metrics_doc:
        # Fallback: show published pilot metrics (no per-row CSV in tree)
        m = metrics_doc.get("metrics", {})
        fig, ax = plt.subplots(figsize=(5.5, 3.4))
        labels = ["AUC", "Brier", "ECE", "log loss"]
        vals = [m.get("auc_roc", 0), m.get("brier", 0), m.get("ece", 0), m.get("log_loss", 0)]
        bars = ax.bar(labels, vals, color=["#4f46e5", "#ea580c", "#0d9488", "#64748b"])
        ax.set_ylim(0, 1.05)
        ax.set_title("v0.2.1 Binary Scorer (aggregated metrics)")
        ax.bar_label(bars, fmt="%.3f", fontsize=8)
        ax.spines[["top", "right"]].set_visible(False)
        fig.tight_layout()
        fig.savefig(out, bbox_inches="tight")
        plt.close(fig)
        return {"source": "metrics_json", "rows": metrics_doc.get("rows_total", 0), **m}

    y = np.array(y_list, dtype=np.int32)
    p = np.array(p_list, dtype=np.float64)
    bins, ece = calibration_bins(y, p)

    fig, ax = plt.subplots(figsize=(5.5, 3.8))
    nonempty = [b for b in bins if b["count"] > 0]
    pred = [b["pred_mean"] for b in nonempty]
    real = [b["realized"] for b in nonempty]
    counts = [b["count"] for b in nonempty]
    ax.plot([0, 1], [0, 1], "k--", linewidth=1.1, label="perfect calibration", zorder=1)
    ax.plot(
        pred,
        real,
        color="#ea580c",
        linewidth=1.4,
        marker="o",
        markersize=5,
        markerfacecolor="#ea580c",
        markeredgecolor="white",
        markeredgewidth=0.7,
        label="bins",
        zorder=3,
    )
    for x, y_obs, count in zip(pred, real, counts):
        ax.annotate(
            f"{count/1000:.0f}k",
            (x, y_obs),
            textcoords="offset points",
            xytext=(0, 6),
            ha="center",
            fontsize=6,
            color="#475569",
        )
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.set_xlabel("mean predicted P(up)")
    ax.set_ylabel("observed frequency")
    ax.set_title(f"Calibration Curve (step3, n={len(y):,}, ECE={ece:.3f})")
    ax.legend(fontsize=8, frameon=False)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    return {
        "source": str(step3) if step3 else "none",
        "rows": int(len(y)),
        "ece": ece,
        "auc_roc": metrics_doc.get("metrics", {}).get("auc_roc"),
    }


def _rolling_median(values: np.ndarray, k: int = 25) -> np.ndarray:
    """Centered rolling median; edges use available samples."""
    n = len(values)
    if n == 0:
        return values
    k = max(3, min(k, n))
    half = k // 2
    out = np.empty(n, dtype=np.float64)
    for i in range(n):
        lo = max(0, i - half)
        hi = min(n, i + half + 1)
        out[i] = float(np.median(values[lo:hi]))
    return out


def plot_walk_forward(out: Path) -> dict:
    if not METRICS_JSON.exists():
        return {}
    doc = json.loads(METRICS_JSON.read_text(encoding="utf-8"))
    windows = doc.get("walk_forward_windows", [])
    if not windows:
        return {}

    auc = np.array([w["auc_roc_raw"] for w in windows], dtype=np.float64)
    brier = np.array([w["brier_raw"] for w in windows], dtype=np.float64)
    agg = doc.get("metrics", {})
    x = np.arange(1, len(windows) + 1)
    auc_roll = _rolling_median(auc, k=25)
    brier_roll = _rolling_median(brier, k=25)

    fig, axes = plt.subplots(2, 1, figsize=(6.6, 4.2), sharex=True)
    axes[0].plot(x, auc, color="#c7d2fe", linewidth=0.6, alpha=0.85, label="per-window")
    axes[0].plot(x, auc_roll, color="#4f46e5", linewidth=1.4, label="rolling median (25)")
    if agg.get("auc_roc"):
        axes[0].axhline(float(agg["auc_roc"]), color="#0f172a", linewidth=1.0, linestyle=":",
                        label=f"pooled={agg['auc_roc']:.3f}")
    axes[0].set_ylabel("AUC")
    axes[0].set_ylim(0.0, 1.05)
    axes[0].set_title(f"v0.2.1 walk-forward stability ({len(windows)} windows)")
    axes[0].legend(fontsize=7, frameon=False, loc="lower right", ncol=2)
    axes[0].spines[["top", "right"]].set_visible(False)

    axes[1].plot(x, brier, color="#fed7aa", linewidth=0.6, alpha=0.85, label="per-window")
    axes[1].plot(x, brier_roll, color="#ea580c", linewidth=1.4, label="rolling median (25)")
    if agg.get("brier"):
        axes[1].axhline(float(agg["brier"]), color="#0f172a", linewidth=1.0, linestyle=":",
                        label=f"pooled={agg['brier']:.3f}")
    axes[1].set_ylabel("Brier")
    axes[1].set_xlabel("walk-forward window index")
    axes[1].legend(fontsize=7, frameon=False, loc="upper right", ncol=2)
    axes[1].spines[["top", "right"]].set_visible(False)

    # Sparse, readable ticks only (no train-market labels).
    tick_step = max(50, len(windows) // 6)
    tick_idx = np.arange(1, len(windows) + 1, tick_step)
    axes[1].set_xticks(tick_idx)
    axes[1].set_xlim(1, len(windows))

    summary = (
        f"AUC: min={auc.min():.3f}, med={np.median(auc):.3f}, max={auc.max():.3f}  |  "
        f"Brier: min={brier.min():.3f}, med={np.median(brier):.3f}, max={brier.max():.3f}"
    )
    fig.text(0.5, 0.01, summary, ha="center", fontsize=7, color="#475569")
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    fig.savefig(out, bbox_inches="tight", dpi=300)
    plt.close(fig)
    return {
        "windows": len(windows),
        "auc_min": float(auc.min()),
        "auc_median": float(np.median(auc)),
        "auc_max": float(auc.max()),
        "brier_min": float(brier.min()),
        "brier_median": float(np.median(brier)),
        "brier_max": float(brier.max()),
    }


def ledger_candidates() -> list[Path]:
    """Operational ledgers are optional and are consumed only as aggregates."""
    explicit = [
        REPO / "data/trade_ledger.json",
        REPO / "data/paper_ledger.json",
        REPO / "data/paper_trade_ledger.json",
        REPO / "data/paper_trades.json",
        REPO / "data/paper_trades.csv",
        REPO / "data/paper_trade_log.csv",
        REPO / "v15_trade_log.csv",
        LEDGER_JSON,
    ]
    found: list[Path] = []
    for path in explicit:
        if path.exists() and path.is_file() and path.stat().st_size > 0:
            found.append(path)
    for root in [REPO / "data", REPO / "research"]:
        if not root.exists():
            continue
        for pattern in ["*ledger*.json", "*ledger*.csv", "*trade_log*.csv", "*paper*trade*.json", "*paper*trade*.csv"]:
            for path in root.rglob(pattern):
                if path.is_file() and path.stat().st_size > 0 and path not in found:
                    found.append(path)
    return found


def infer_ledger_kind(path: Path, row: dict) -> str:
    text = str(path).lower()
    strategy = str(row.get("strategy", row.get("signal_version", ""))).lower()
    mode = str(row.get("mode", row.get("account", ""))).lower()
    if "paper" in text or "paper" in strategy or "paper" in mode:
        return "paper"
    if "backtest" in text or "v15_trade_log" in text:
        return "paper"
    return "actual"


def parse_bool(value) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"1", "true", "win", "won", "success", "yes", "y"}:
        return True
    if text in {"0", "false", "loss", "lost", "fail", "failed", "no", "n"}:
        return False
    return None


def ledger_timestamp(row: dict) -> datetime | None:
    slug = str(row.get("slug", row.get("market_slug", "")))
    match = re.search(r"-(\d{10})$", slug)
    if match:
        return datetime.fromtimestamp(int(match.group(1)), tz=timezone.utc).astimezone(NY_TZ)

    for field in ["market_start_ms", "ts_ms", "entry_ts_ms", "entry_time_ms", "created_at_ms", "redeemed_at_ms"]:
        raw = row.get(field)
        if raw not in (None, ""):
            try:
                return datetime.fromtimestamp(float(raw) / 1000.0, tz=timezone.utc).astimezone(NY_TZ)
            except (TypeError, ValueError, OSError):
                pass

    for field in ["market_start", "entry_time", "entered_at", "created_at", "redeemed_at", "timestamp"]:
        raw = row.get(field)
        if not raw:
            continue
        text = str(raw).replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=NY_TZ)
            return dt.astimezone(NY_TZ)
        except ValueError:
            pass
    return None


def ledger_win_flag(row: dict) -> bool | None:
    for field in ["won", "is_win", "win", "correct"]:
        parsed = parse_bool(row.get(field))
        if parsed is not None:
            return parsed
    for field in ["result", "outcome_result", "status"]:
        parsed = parse_bool(row.get(field))
        if parsed is not None:
            return parsed
    for field in ["pnl", "cash_pnl", "profit", "return"]:
        raw = row.get(field)
        if raw not in (None, ""):
            try:
                return float(raw) > 0
            except (TypeError, ValueError):
                pass
    return None


def iter_ledger_rows(path: Path):
    if path.suffix.lower() == ".json":
        doc = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(doc, dict):
            for key in ["trades", "ledger", "rows", "data"]:
                if isinstance(doc.get(key), list):
                    doc = doc[key]
                    break
        if isinstance(doc, list):
            for row in doc:
                if isinstance(row, dict):
                    yield row
    elif path.suffix.lower() == ".csv":
        import csv
        with path.open(encoding="utf-8") as fh:
            yield from csv.DictReader(fh)


def load_trade_timing_records() -> tuple[list[dict], list[str]]:
    records: list[dict] = []
    sources: list[str] = []
    for path in ledger_candidates():
        source_count = 0
        for row in iter_ledger_rows(path):
            dt = ledger_timestamp(row)
            won = ledger_win_flag(row)
            if dt is None or won is None:
                continue
            records.append({
                "kind": infer_ledger_kind(path, row),
                "dow": dt.weekday(),
                "hour": dt.hour,
                "date": dt.date().isoformat(),
                "won": bool(won),
                "source": path.relative_to(REPO).as_posix() if path.is_relative_to(REPO) else path.as_posix(),
            })
            source_count += 1
        if source_count:
            sources.append(path.relative_to(REPO).as_posix() if path.is_relative_to(REPO) else path.as_posix())
    return records, sources


def plot_ledger_hour_dow(out: Path) -> dict:
    records, sources = load_trade_timing_records()
    groups = [(name, [r for r in records if r["kind"] == name]) for name in ["actual", "paper"]]
    groups = [(name, rows) for name, rows in groups if rows]

    if not groups:
        fig, ax = plt.subplots(figsize=(8, 2.5))
        ax.axis("off")
        ax.text(0.5, 0.5, "No sanitized actual or paper trade ledger found", ha="center", va="center")
        fig.savefig(out, bbox_inches="tight")
        plt.close(fig)
        return {"trades": 0, "sources": []}

    dow_labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    fig, axes = plt.subplots(len(groups), 2, figsize=(10.8, 3.2 * len(groups)), squeeze=False)
    stats: dict[str, object] = {"trades": len(records), "sources": sources}

    for row_idx, (kind, rows) in enumerate(groups):
        cnt = np.zeros((7, 24), dtype=np.float64)
        wins = np.zeros((7, 24), dtype=np.float64)
        for rec in rows:
            cnt[int(rec["dow"]), int(rec["hour"])] += 1
            wins[int(rec["dow"]), int(rec["hour"])] += 1 if rec["won"] else 0
        wr = np.divide(wins, cnt, out=np.full_like(wins, np.nan), where=cnt > 0) * 100.0
        top_count = int(cnt.max()) if cnt.size else 0
        date_count = len({r["date"] for r in rows})
        stats[f"{kind}_trades"] = len(rows)
        stats[f"{kind}_dates"] = date_count
        stats[f"{kind}_top_cell"] = top_count

        im0 = axes[row_idx, 0].imshow(wr, aspect="auto", cmap="RdYlGn", vmin=0, vmax=100)
        axes[row_idx, 0].set_title(f"{kind.title()} ledger win rate (%)")
        axes[row_idx, 0].set_yticks(range(7))
        axes[row_idx, 0].set_yticklabels(dow_labels)
        axes[row_idx, 0].set_xticks(range(0, 24, 3))
        axes[row_idx, 0].set_xlabel("ET hour")
        fig.colorbar(im0, ax=axes[row_idx, 0], fraction=0.046, pad=0.04)

        im1 = axes[row_idx, 1].imshow(cnt, aspect="auto", cmap="Blues", vmin=0)
        axes[row_idx, 1].set_title(f"{kind.title()} ledger trade count")
        axes[row_idx, 1].set_yticks(range(7))
        axes[row_idx, 1].set_yticklabels(dow_labels)
        axes[row_idx, 1].set_xticks(range(0, 24, 3))
        axes[row_idx, 1].set_xlabel("ET hour")
        fig.colorbar(im1, ax=axes[row_idx, 1], fraction=0.046, pad=0.04)

        for d in range(7):
            for h in range(24):
                if cnt[d, h] >= 5:
                    axes[row_idx, 1].text(
                        h, d, f"{int(cnt[d, h])}", ha="center", va="center",
                        fontsize=6, color="white" if cnt[d, h] > max(1, top_count) * 0.45 else "#0f172a",
                    )
        axes[row_idx, 0].text(
            0.0, -0.28, f"n={len(rows):,} trades across {date_count} dates; aggregate-safe fields only",
            transform=axes[row_idx, 0].transAxes, fontsize=7, color="#475569",
        )

    fig.suptitle("Sanitized Trade-Ledger Timing (ET hour x weekday)", fontsize=11, y=1.01)
    fig.tight_layout()
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    return stats


def plot_lag_hour_heatmap(out: Path, max_rows: int = 500_000) -> dict:
    if not UNIFIED_LAG.exists():
        return {"pairs": 0}
    chunks_p: list[np.ndarray] = []
    chunks_h: list[np.ndarray] = []
    for pq_path in sorted(UNIFIED_LAG.rglob("*.parquet")):
        pf = pq.ParquetFile(pq_path)
        table = pf.read(columns=["lead_lag_ms", "polymarket_source_ts_ms"])
        lag = table["lead_lag_ms"].to_numpy(zero_copy_only=False)
        ts = table["polymarket_source_ts_ms"].to_numpy(zero_copy_only=False)
        mask = np.isfinite(lag) & np.isfinite(ts)
        lag = lag[mask]
        ts = ts[mask]
        hours = ((ts // 1000) % 86400) // 3600
        chunks_p.append(lag.astype(np.float64))
        chunks_h.append(hours.astype(np.int32))
    if not chunks_p:
        return {"pairs": 0}
    lag_all = np.concatenate(chunks_p)
    hour_all = np.concatenate(chunks_h)
    if len(lag_all) > max_rows:
        rng = np.random.default_rng(42)
        idx = rng.choice(len(lag_all), size=max_rows, replace=False)
        lag_all = lag_all[idx]
        hour_all = hour_all[idx]

    medians = np.full(24, np.nan)
    counts = np.zeros(24)
    for h in range(24):
        sel = lag_all[hour_all == h]
        if len(sel):
            medians[h] = np.median(sel)
            counts[h] = len(sel)

    fig, ax = plt.subplots(figsize=(6.5, 3.0))
    bars = ax.bar(range(24), medians, color="#4f46e5", edgecolor="white")
    ax.axhline(0, color="#94a3b8", linewidth=0.8)
    ax.set_xlabel("hour of day (UTC, from polymarket_source_ts_ms)")
    ax.set_ylabel("median lead_lag_ms")
    ax.set_title(f"Lead--Lag by Hour (sample n={len(lag_all):,})")
    ax.set_xticks(range(0, 24, 2))
    ax.set_xlim(-0.5, 23.5)
    ax.bar_label(bars, labels=[f"{v:.0f}" if not math.isnan(v) else "" for v in medians],
                fontsize=6, padding=2)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    return {"pairs": int(len(lag_all))}


def hardware_spec() -> dict:
    cpu = ""
    try:
        cpu = subprocess.check_output(["sysctl", "-n", "machdep.cpu.brand_string"], text=True).strip()
    except Exception:
        cpu = platform.processor() or platform.machine()
    mem_gb = 0.0
    try:
        mem_bytes = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True).strip())
        mem_gb = mem_bytes / (1024 ** 3)
    except Exception:
        pass
    rust = ""
    try:
        rust = subprocess.check_output(["rustc", "--version"], text=True).strip()
    except Exception:
        rust = "unknown"
    return {
        "cpu": cpu,
        "ram_gb": round(mem_gb),
        "os": f"{platform.system()} {platform.mac_ver()[0]}".strip(),
        "rust": rust,
    }


def bench_parquet_load(root: Path, max_parts: int = 3) -> dict:
    files = sorted(root.rglob("*.parquet"))[:max_parts]
    if not files:
        return {"rows": 0, "seconds": 0.0}
    t0 = time.perf_counter()
    rows = 0
    for f in files:
        tbl = pq.read_table(f)
        rows += tbl.num_rows
    return {"rows": rows, "seconds": round(time.perf_counter() - t0, 4), "parts": len(files)}


def sqlite_market_count(db: Path) -> int:
    try:
        out = subprocess.check_output(
            ["sqlite3", str(db),
             "SELECT COUNT(*) FROM market_meta WHERE market_slug LIKE 'btc-updown-15m-%';"],
            text=True,
        ).strip()
        return int(out or 0)
    except Exception:
        return 0


def bench_backtester(db: Path) -> dict:
    if not db.exists():
        return {"status": "skipped"}
    markets_in_db = sqlite_market_count(db)
    bin_path = REPO / "target/release/v15_brier_calibration"
    if not bin_path.exists():
        subprocess.run(
            ["cargo", "build", "-p", "v15_brier_calibration", "--release"],
            cwd=REPO,
            check=False,
            capture_output=True,
        )
    if not bin_path.exists():
        return {"status": "build_failed"}
    t0 = time.perf_counter()
    proc = subprocess.run(
        [str(bin_path), "--db-path", str(db)],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    elapsed = time.perf_counter() - t0
    trades = 0
    for line in proc.stdout.splitlines():
        if "Total Trades" in line:
            m = re.search(r"\|\s*(\d+)\s*\|", line)
            if m:
                trades = int(m.group(1))
    return {
        "status": "ok" if proc.returncode == 0 else f"exit_{proc.returncode}",
        "seconds": round(elapsed, 2),
        "markets": markets_in_db,
        "trades": trades,
        "db_mib": round(db.stat().st_size / (1024 ** 2), 1),
    }


def run_throughput_benchmarks() -> dict:
    hw = hardware_spec()
    unified = REPO / "data/hf_release/unified_parquet"
    features = bench_parquet_load(FEATURES_PARQUET)
    if features.get("rows") == 0:
        pq_files = sorted(FEATURES_PARQUET.rglob("*.parquet"))
        if pq_files:
            t0 = time.perf_counter()
            tbl = pq.read_table(pq_files[0])
            features = {"rows": tbl.num_rows, "seconds": round(time.perf_counter() - t0, 4), "parts": 1}

    char = {}
    if CHAR_JSON.exists():
        char = json.loads(CHAR_JSON.read_text(encoding="utf-8"))

    baseline = {}
    if BASELINE_JSON.exists():
        baseline = json.loads(BASELINE_JSON.read_text(encoding="utf-8"))

    backtest = bench_backtester(STAGING_DB)

    bench = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "hardware": hw,
        "hf_sample_download_s": baseline.get("download_seconds"),
        "hf_sample_load_s": baseline.get("load_seconds"),
        "hf_sample_rows": baseline.get("total_rows"),
        "unified_metadata_scan_s": char.get("scan_seconds"),
        "unified_lag_load_s": char.get("lag_load_seconds"),
        "unified_total_rows": char.get("totals", {}).get("total_rows"),
        "unified_gib": round(char.get("totals", {}).get("total_gib", 0), 2),
        "feature_parquet_load": features,
        "backtest_staging_db": backtest,
    }
    return bench


def plot_throughput(bench: dict, out: Path) -> None:
    labels: list[str] = []
    values: list[float] = []
    if bench.get("hf_sample_load_s"):
        labels.append("HF sample\nload (s)")
        values.append(float(bench["hf_sample_load_s"]))
    if bench.get("unified_metadata_scan_s"):
        labels.append("Unified\nmeta scan (s)")
        values.append(float(bench["unified_metadata_scan_s"]))
    if bench.get("unified_lag_load_s"):
        labels.append("Lag table\nload (s)")
        values.append(float(bench["unified_lag_load_s"]))
    feat = bench.get("feature_parquet_load", {})
    if feat.get("seconds"):
        labels.append("Features\nparquet (s)")
        values.append(float(feat["seconds"]))
    bt = bench.get("backtest_staging_db", {})
    if bt.get("seconds") and bt.get("status") == "ok":
        labels.append("Backtester\n(staging, s)")
        values.append(float(bt["seconds"]))

    if not labels:
        return

    hw = bench.get("hardware", {})
    subtitle = f"{hw.get('cpu', 'unknown')}, {hw.get('ram_gb', '?')} GB RAM — {hw.get('rust', '')}"

    fig, ax = plt.subplots(figsize=(6.2, 3.4))
    bars = ax.bar(range(len(labels)), values, color="#4f46e5", edgecolor="white")
    ax.set_xticks(range(len(labels)))
    ax.set_xticklabels(labels, fontsize=8)
    ax.set_ylabel("seconds")
    ax.set_title("Throughput Benchmarks (local workstation)")
    ax.bar_label(bars, fmt="%.3g", fontsize=8, padding=2)
    ax.spines[["top", "right"]].set_visible(False)
    fig.text(0.5, -0.02, subtitle, ha="center", fontsize=7, color="#475569")
    fig.tight_layout()
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)


def append_tex_macros(bench: dict, results: dict) -> None:
    hw = bench.get("hardware", {})
    lines: list[str] = []
    if CHAR_TEX.exists():
        text = CHAR_TEX.read_text(encoding="utf-8")
        generated_prefixes = (
            "\\newcommand{\\OpenMarketHardware}",
            "\\newcommand{\\OpenMarketRustVersion}",
            "\\newcommand{\\OpenMarketBench",
            "\\newcommand{\\OpenMarketFeatureCorr",
        )
        lines = [
            ln for ln in text.splitlines()
            if not ln.startswith(generated_prefixes)
        ]
    else:
        lines = ["% Auto-generated macros"]

    cpu = hw.get("cpu", "unknown").replace("_", "\\_")
    feat = bench.get("feature_parquet_load", {})
    bt = bench.get("backtest_staging_db", {})
    corr = results.get("feature_correlation", {})

    extra = [
        f"\\newcommand{{\\OpenMarketHardware}}{{{cpu}, {hw.get('ram_gb', '?')}~GB RAM}}",
        f"\\newcommand{{\\OpenMarketRustVersion}}{{{hw.get('rust', 'Rust unknown')}}}",
        f"\\newcommand{{\\OpenMarketBenchHfLoad}}{{{bench.get('hf_sample_load_s', '—')}}}",
        f"\\newcommand{{\\OpenMarketBenchFeatLoad}}{{{feat.get('seconds', '—')}}}",
        f"\\newcommand{{\\OpenMarketBenchFeatRows}}{{{feat.get('rows', 0):,}}}",
        f"\\newcommand{{\\OpenMarketBenchFeatParts}}{{{feat.get('parts', 0)}}}",
        f"\\newcommand{{\\OpenMarketBenchBacktest}}{{{bt.get('seconds', '—')}}}",
        f"\\newcommand{{\\OpenMarketBenchBacktestMarkets}}{{{bt.get('markets', 0)}}}",
        f"\\newcommand{{\\OpenMarketFeatureCorrRows}}{{{corr.get('rows', 0):,}}}",
        f"\\newcommand{{\\OpenMarketFeatureCorrFeatures}}{{{corr.get('features', 0)}}}",
        "",
    ]
    CHAR_TEX.write_text("\n".join(lines + extra), encoding="utf-8")


def main() -> int:
    FIG_DIR.mkdir(parents=True, exist_ok=True)
    STATS_DIR.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update({"font.size": 9, "figure.dpi": 300})

    results = {
        "feature_correlation": plot_feature_correlation(FIG_DIR / "feature-correlation.pdf"),
        "calibration": plot_calibration_curve(FIG_DIR / "calibration-curve.pdf"),
        "walk_forward": plot_walk_forward(FIG_DIR / "walk-forward-metrics.pdf"),
    }
    bench = run_throughput_benchmarks()
    plot_throughput(bench, FIG_DIR / "throughput-bench.pdf")
    append_tex_macros(bench, results)

    out = {"figures": results, "benchmarks": bench}
    bench_path = STATS_DIR / "benchmarks.json"
    bench_path.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {bench_path}")
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
