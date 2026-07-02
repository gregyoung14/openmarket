#!/usr/bin/env python3
"""Targeted microstructure analyses for the OpenMarket paper (Section 15+).

Writes assets/stats/research_findings.json and PDF figures under assets/figures/.

Usage:
  .venv/bin/python paper/scripts/paper/analyze_research.py
"""
from __future__ import annotations

import json
import math
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pyarrow.parquet as pq

REPO = Path(__file__).resolve().parents[3]
ROOT = REPO / "paper"
UNIFIED = REPO / "data/hf_release/unified_parquet"
STEP3 = REPO / "data/hf_release/features_exports/step3_binary_calibration_1782951891604.csv"
MODEL = REPO / "models/hf_staging/v0.2.1/binary_outcome_model.json"
FIG = ROOT / "assets/figures"
STATS = ROOT / "assets/stats"


def roc_auc(y: np.ndarray, p: np.ndarray) -> float:
    order = np.argsort(p)
    y = y[order]
    n_pos = y.sum()
    n_neg = len(y) - n_pos
    if n_pos == 0 or n_neg == 0:
        return float("nan")
    ranks = np.arange(1, len(y) + 1)
    sum_ranks_pos = ranks[y == 1].sum()
    return (sum_ranks_pos - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)


def brier(y: np.ndarray, p: np.ndarray) -> float:
    return float(np.mean((p - y) ** 2))


def log_loss(y: np.ndarray, p: np.ndarray) -> float:
    p = np.clip(p, 1e-12, 1 - 1e-12)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def calibration_ece(y: np.ndarray, p: np.ndarray, n_bins: int = 10) -> float:
    ece = 0.0
    n = len(y)
    if n == 0:
        return 0.0
    for bucket in range(n_bins):
        lo = bucket / n_bins
        hi = (bucket + 1) / n_bins
        if bucket == n_bins - 1:
            mask = (p >= lo) & (p <= hi)
        else:
            mask = (p >= lo) & (p < hi)
        if not np.any(mask):
            continue
        ece += float(mask.sum() / n * abs(np.mean(p[mask]) - np.mean(y[mask])))
    return ece


def benchmark_row(y: np.ndarray, p: np.ndarray, elapsed_s: float | None = None) -> dict:
    row = {
        "auc": roc_auc(y, p),
        "brier": brier(y, p),
        "ece": calibration_ece(y, p),
        "log_loss": log_loss(y, p),
        "n": int(len(y)),
    }
    if elapsed_s is not None:
        row["score_seconds"] = float(elapsed_s)
        row["score_us_per_row"] = float(elapsed_s * 1_000_000 / max(len(y), 1))
    return row


def bootstrap_auc_diff(
    y: np.ndarray, p_new: np.ndarray, p_base: np.ndarray, n_boot: int = 2000, seed: int = 42
) -> dict:
    """Paired bootstrap for AUC(new) - AUC(base)."""
    rng = np.random.default_rng(seed)
    n = len(y)
    obs = roc_auc(y, p_new) - roc_auc(y, p_base)
    diffs = np.empty(n_boot, dtype=np.float64)
    for i in range(n_boot):
        idx = rng.integers(0, n, n)
        diffs[i] = roc_auc(y[idx], p_new[idx]) - roc_auc(y[idx], p_base[idx])
    p_one = float(np.mean(diffs <= 0))
    return {
        "observed_diff": float(obs),
        "ci_low": float(np.percentile(diffs, 2.5)),
        "ci_high": float(np.percentile(diffs, 97.5)),
        "p_value_one_sided": p_one,
        "n_bootstrap": n_boot,
    }


BENCHMARK_LABELS = {
    "naive_mid_prior": "Naive mid prior",
    "logistic_v02_full": "Logistic v0.2.1",
    "drift_only": "Drift only",
    "ofi_60s_sigmoid": "OFI (sigmoid)",
}


def load_lag_sample(max_rows: int = 500_000) -> dict[str, np.ndarray]:
    cols = [
        "lead_lag_ms", "price_delta_bps", "quality_flag",
        "paired_at_ms", "binance_price", "polymarket_bid",
    ]
    chunks: dict[str, list] = {c: [] for c in cols}
    total = 0
    for pq_path in sorted((UNIFIED / "lag_pairs_ms").rglob("*.parquet")):
        pf = pq.ParquetFile(pq_path)
        t = pf.read(columns=cols)
        for c in cols:
            chunks[c].append(t[c].to_numpy(zero_copy_only=False))
        total += t.num_rows
        if total >= max_rows:
            break
    out = {c: np.concatenate(v) for c, v in chunks.items()}
    if len(out["lead_lag_ms"]) > max_rows:
        rng = np.random.default_rng(42)
        idx = rng.choice(len(out["lead_lag_ms"]), size=max_rows, replace=False)
        out = {c: arr[idx] for c, arr in out.items()}
    return out


def load_spread_sample(max_rows: int = 200_000) -> np.ndarray:
    spreads = []
    total = 0
    for pq_path in sorted((UNIFIED / "polymarket_ticks_ms").rglob("*.parquet")):
        t = pq.ParquetFile(pq_path).read(columns=["best_bid", "best_ask", "event_type"])
        bid = t["best_bid"].to_numpy(zero_copy_only=False)
        ask = t["best_ask"].to_numpy(zero_copy_only=False)
        mask = np.isfinite(bid) & np.isfinite(ask) & (bid > 0) & (ask > 0)
        s = (ask - bid)[mask]
        spreads.append(s)
        total += len(s)
        if total >= max_rows:
            break
    if not spreads:
        return np.array([])
    out = np.concatenate(spreads)
    return out[:max_rows]


def load_step3() -> tuple[np.ndarray, np.ndarray, list[str], np.ndarray]:
    import csv

    rows = []
    with STEP3.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        for row in reader:
            rows.append(row)
    if not rows:
        raise SystemExit(f"empty step3: {STEP3}")

    y = np.array([int(r["label_up_final"]) for r in rows], dtype=np.int8)
    feature_names = [c for c in fieldnames if c not in {
        "market_slug", "market_start_ms", "market_end_ms", "ts_ms",
        "market_open_price", "market_close_price", "label_up_final",
    }]
    X = np.array([[float(r[c]) for c in feature_names] for r in rows], dtype=np.float64)
    vol = X[:, feature_names.index("rv_60s")] if "rv_60s" in feature_names else np.zeros(len(rows))
    return X, y, feature_names, vol


def score_model(model: dict, X: np.ndarray, names: list[str]) -> np.ndarray:
    idx = {n: i for i, n in enumerate(names)}
    means = np.array(model["means"], dtype=np.float64)
    stds = np.array(model["stds"], dtype=np.float64)
    weights = np.array(model["weights"], dtype=np.float64)
    stds = np.where(np.abs(stds) <= 1e-12, 1.0, stds)
    z = (X - means) / stds
    logits = z @ weights + float(model["intercept"])
    raw = 1 / (1 + np.exp(-np.clip(logits, -30, 30)))
    return 1 / (1 + np.exp(-(float(model["platt_a"]) * np.log(raw / (1 - raw + 1e-9) + 1e-9) + float(model["platt_b"]))))


def plot_lead_lag_vs_disagreement(lag: dict[str, np.ndarray], out: Path) -> dict:
    delta = np.abs(lag["price_delta_bps"])
    ll = lag["lead_lag_ms"]
    valid = np.isfinite(delta) & np.isfinite(ll)
    delta, ll = delta[valid], ll[valid]
    qs = np.quantile(delta, [0, 0.2, 0.4, 0.6, 0.8, 1.0])
    labels, medians, counts = [], [], []
    for i in range(5):
        lo, hi = qs[i], qs[i + 1]
        mask = (delta >= lo) & (delta <= hi if i == 4 else delta < hi)
        if mask.sum() < 100:
            continue
        labels.append(f"Q{i+1}")
        medians.append(float(np.median(ll[mask])))
        counts.append(int(mask.sum()))

    fig, ax1 = plt.subplots(figsize=(5.5, 3.2))
    x = np.arange(len(labels))
    ax1.bar(x, medians, color="#4f46e5", edgecolor="white")
    ax1.axhline(0, color="#94a3b8", linewidth=0.8)
    ax1.set_xticks(x)
    ax1.set_xticklabels(labels)
    ax1.set_ylabel("median lead_lag_ms")
    ax1.set_xlabel("|price_delta_bps| quintile")
    ax1.set_title("Lead--Lag vs Cross-Venue Disagreement")
    ax1.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    return {"quintile_medians_ms": medians, "quintile_counts": counts}


def plot_spread_hist(spreads: np.ndarray, out: Path) -> dict:
    fig, ax = plt.subplots(figsize=(5.5, 3.2))
    s = spreads[spreads < np.quantile(spreads, 0.99)]
    ax.hist(s, bins=60, color="#f97316", edgecolor="white", linewidth=0.3)
    ax.set_xlabel("UP/DOWN spread (best_ask - best_bid)")
    ax.set_ylabel("count")
    ax.set_title(f"Polymarket Top-of-Book Spread (n={len(spreads):,})")
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    return {
        "n": int(len(spreads)),
        "median": float(np.median(spreads)),
        "mean": float(np.mean(spreads)),
        "p95": float(np.quantile(spreads, 0.95)),
    }


def plot_model_benchmarks(results: dict, out: Path) -> None:
    order = [k for k in BENCHMARK_LABELS if k in results]
    labels = [BENCHMARK_LABELS[k] for k in order]
    aucs = [results[k]["auc"] for k in order]
    colors = ["#94a3b8", "#4f46e5", "#f97316", "#cbd5e1"][: len(order)]
    fig, ax = plt.subplots(figsize=(5.8, 2.8))
    bars = ax.barh(labels, aucs, color=colors)
    ax.set_xlim(0.5, 1.0)
    ax.set_xlabel("ROC AUC")
    ax.set_title("Forecast Benchmarks (357k step3 rows)")
    ax.bar_label(bars, labels=[f"{v:.3f}" for v in aucs], padding=4, fontsize=8)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig.savefig(out, bbox_inches="tight", dpi=300)
    plt.close(fig)


def _format_pvalue(p: float) -> str:
    return "<0.001" if p < 0.001 else f"{p:.3f}"


def write_research_stats_tex(findings: dict) -> None:
    auc = findings.get("auc_bootstrap", {})
    spread = findings.get("calibration_by_spread", [])
    forecasts = findings.get("forecast_benchmarks", {})
    logistic = forecasts.get("logistic_v02_full", {})
    naive = forecasts.get("naive_mid_prior", {})
    lines = [
        "% Auto-generated by analyze_research.py — do not edit by hand",
        f"\\newcommand{{\\OpenMarketAucDiff}}{{{auc.get('observed_diff', 0):.4f}}}",
        f"\\newcommand{{\\OpenMarketAucDiffLow}}{{{auc.get('ci_low', 0):.4f}}}",
        f"\\newcommand{{\\OpenMarketAucDiffHigh}}{{{auc.get('ci_high', 0):.4f}}}",
        f"\\newcommand{{\\OpenMarketAucPvalue}}{{{_format_pvalue(auc.get('p_value_one_sided', 1))}}}",
        f"\\newcommand{{\\OpenMarketLogisticEce}}{{{logistic.get('ece', 0):.3f}}}",
        f"\\newcommand{{\\OpenMarketNaiveEce}}{{{naive.get('ece', 0):.3f}}}",
        f"\\newcommand{{\\OpenMarketLogisticScoreUs}}{{{logistic.get('score_us_per_row', 0):.3f}}}",
    ]
    if spread:
        tight = next((r for r in spread if r["regime"] == "tight"), spread[0])
        wide = next((r for r in spread if r["regime"] == "wide"), spread[-1])
        lines.extend([
            f"\\newcommand{{\\OpenMarketBrierTight}}{{{tight.get('brier_full_model', 0):.3f}}}",
            f"\\newcommand{{\\OpenMarketBrierWide}}{{{wide.get('brier_full_model', 0):.3f}}}",
        ])
    lag_corr = findings.get("lead_lag_price_corr", {})
    if lag_corr.get("pearson_abs_delta") is not None:
        lines.append(
            f"\\newcommand{{\\OpenMarketLagPriceCorr}}{{{lag_corr['pearson_abs_delta']:.2f}}}"
        )
    (STATS / "research_stats.tex").write_text("\n".join(lines) + "\n", encoding="utf-8")


def plot_lead_lag_by_vol(lag_ll: np.ndarray, vol_proxy: np.ndarray, out: Path) -> dict:
    """vol_proxy: abs(price_delta_bps) as disagreement/regime proxy."""
    valid = np.isfinite(lag_ll) & np.isfinite(vol_proxy)
    lag_ll, vol_proxy = lag_ll[valid], vol_proxy[valid]
    qs = np.quantile(vol_proxy, [0, 1 / 3, 2 / 3, 1.0])
    stats = []
    for i, label in enumerate(["low", "mid", "high"]):
        lo, hi = qs[i], qs[i + 1]
        mask = (vol_proxy >= lo) & (vol_proxy <= hi if i == 2 else vol_proxy < hi)
        stats.append({
            "regime": label,
            "median_lead_lag_ms": float(np.median(lag_ll[mask])),
            "iqr_ms": float(np.subtract(*np.percentile(lag_ll[mask], [75, 25]))),
            "n": int(mask.sum()),
        })
    fig, ax = plt.subplots(figsize=(5.2, 3.0))
    ax.bar([s["regime"] for s in stats], [s["median_lead_lag_ms"] for s in stats],
           color="#ea580c", edgecolor="white")
    ax.axhline(0, color="#64748b", linewidth=0.8)
    ax.set_ylabel("median lead_lag_ms")
    ax.set_title("Lead--Lag by Disagreement Regime")
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    return {"by_regime": stats}


def main() -> int:
    plt.rcParams.update({"font.size": 9, "figure.dpi": 300})
    FIG.mkdir(parents=True, exist_ok=True)
    STATS.mkdir(parents=True, exist_ok=True)

    lag = load_lag_sample()
    spread = load_spread_sample()

    findings: dict = {"generated_at": datetime.now(timezone.utc).isoformat()}

    findings["lead_lag_vs_disagreement"] = plot_lead_lag_vs_disagreement(
        lag, FIG / "lead-lag-vs-disagreement.pdf"
    )
    findings["lead_lag_by_regime"] = plot_lead_lag_by_vol(
        lag["lead_lag_ms"], np.abs(lag["price_delta_bps"]), FIG / "lead-lag-by-regime.pdf"
    )
    findings["spread"] = plot_spread_hist(spread, FIG / "spread-distribution.pdf")

    quality = lag["quality_flag"]
    findings["quality_flag_rate"] = {
        "fraction_flagged": float(np.mean(quality != 0)) if len(quality) else 0.0,
        "median_lag_flagged": float(np.median(lag["lead_lag_ms"][quality != 0])) if (quality != 0).any() else None,
        "median_lag_clean": float(np.median(lag["lead_lag_ms"][quality == 0])) if (quality == 0).any() else None,
    }

    X, y, names, vol = load_step3()
    model = json.loads(MODEL.read_text(encoding="utf-8"))
    t0 = time.perf_counter()
    full_p = score_model(model, X, names)
    full_score_s = time.perf_counter() - t0

    benchmarks = {}
    benchmarks["logistic_v02_full"] = benchmark_row(y, full_p, full_score_s)
    prior = None
    if "market_mid_prior_up" in names:
        prior = X[:, names.index("market_mid_prior_up")]
        benchmarks["naive_mid_prior"] = benchmark_row(y, prior)
        findings["auc_bootstrap"] = bootstrap_auc_diff(y, full_p, prior)
    if "drift_prob_up" in names:
        drift = X[:, names.index("drift_prob_up")]
        benchmarks["drift_only"] = benchmark_row(y, drift)
    if "imbalance_60s" in names:
        ofi = X[:, names.index("imbalance_60s")]
        ofi_p = 1 / (1 + np.exp(-ofi))
        benchmarks["ofi_60s_sigmoid"] = benchmark_row(y, ofi_p)

    findings["forecast_benchmarks"] = benchmarks
    plot_model_benchmarks(benchmarks, FIG / "forecast-benchmarks.pdf")

    # Volatility regime effect on model Brier (terciles of rv_60s)
    if vol is not None and len(vol) == len(y):
        qs = np.quantile(vol, [0, 1 / 3, 2 / 3, 1.0])
        regime_brier = []
        for i, label in enumerate(["low_vol", "mid_vol", "high_vol"]):
            lo, hi = qs[i], qs[i + 1]
            mask = (vol >= lo) & (vol <= hi if i == 2 else vol < hi)
            regime_brier.append({
                "regime": label,
                "brier_full_model": brier(y[mask], full_p[mask]),
                "n": int(mask.sum()),
            })
        findings["calibration_by_vol_regime"] = regime_brier

    if "up_spread" in names:
        spread_feat = X[:, names.index("up_spread")]
        # Spreads are heavily point-massed at one tick; use fixed probability thresholds.
        regimes = [
            ("tight", spread_feat <= 0.011),
            ("mid", (spread_feat > 0.011) & (spread_feat < 0.015)),
            ("wide", spread_feat >= 0.015),
        ]
        spread_brier = []
        for label, mask in regimes:
            if not np.any(mask):
                continue
            row = {
                "regime": label,
                "brier_full_model": brier(y[mask], full_p[mask]),
                "n": int(mask.sum()),
            }
            if prior is not None:
                row["brier_naive_mid"] = brier(y[mask], prior[mask])
            spread_brier.append(row)
        findings["calibration_by_spread"] = spread_brier

    delta = lag["price_delta_bps"]
    ll = lag["lead_lag_ms"]
    valid = np.isfinite(delta) & np.isfinite(ll)
    if valid.sum() > 100:
        findings["lead_lag_price_corr"] = {
            "pearson_abs_delta": float(np.corrcoef(np.abs(delta[valid]), ll[valid])[0, 1]),
            "n": int(valid.sum()),
        }

    write_research_stats_tex(findings)

    out = STATS / "research_findings.json"
    out.write_text(json.dumps(findings, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out}")
    print(json.dumps({k: v for k, v in findings.items() if k != "generated_at"}, indent=2)[:2000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
