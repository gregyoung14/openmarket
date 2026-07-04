#!/usr/bin/env python3
"""Targeted microstructure analyses for the OpenMarket paper (Section 15+).

Writes assets/stats/research_findings.json and PDF figures under assets/figures/.

Usage:
  .venv/bin/python paper/scripts/paper/analyze_research.py [--refresh-clock]

Clock-offset validation scans all tick partitions (~minutes); its results are
cached in assets/stats/clock_validation.json and reused unless --refresh-clock.
"""
from __future__ import annotations

import argparse
import json
import math
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pyarrow.compute as pc
import pyarrow.parquet as pq

REPO = Path(__file__).resolve().parents[3]
ROOT = REPO / "paper"
UNIFIED = REPO / "data/hf_release/unified_parquet"
STEP3 = REPO / "data/hf_release/features_exports/step3_binary_calibration_1782951891604.csv"
MODEL = REPO / "models/hf_staging/v0.2.1/binary_outcome_model.json"
MODEL_METRICS_DIR = REPO / "models/hf_staging/v0.2.1"
# Walk-forward protocol constants; must match binary-outcome-trainer TrainConfig defaults.
WF_MIN_TRAIN_MARKETS = 12
WF_TEST_MARKETS = 4
WF_STEP_MARKETS = 4
FIG = ROOT / "assets/figures"
STATS = ROOT / "assets/stats"


def latest_model_metrics_path() -> Path | None:
    files = sorted(MODEL_METRICS_DIR.glob("binary_outcome_metrics_*.json"))
    return files[-1] if files else None


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
    y: np.ndarray,
    p_new: np.ndarray,
    p_base: np.ndarray,
    block_ids: np.ndarray,
    n_boot: int = 1000,
    seed: int = 42,
) -> dict:
    """Paired block bootstrap for AUC(new) - AUC(base)."""
    rng = np.random.default_rng(seed)
    obs = roc_auc(y, p_new) - roc_auc(y, p_base)
    unique_blocks = np.unique(block_ids)
    block_index = {b: np.flatnonzero(block_ids == b) for b in unique_blocks}
    diffs = np.empty(n_boot, dtype=np.float64)
    for i in range(n_boot):
        sampled = rng.choice(unique_blocks, size=len(unique_blocks), replace=True)
        idx = np.concatenate([block_index[b] for b in sampled])
        diffs[i] = roc_auc(y[idx], p_new[idx]) - roc_auc(y[idx], p_base[idx])
    p_one = float(np.mean(diffs <= 0))
    return {
        "observed_diff": float(obs),
        "ci_low": float(np.percentile(diffs, 2.5)),
        "ci_high": float(np.percentile(diffs, 97.5)),
        "p_value_one_sided": p_one,
        "n_bootstrap": n_boot,
        "n_blocks": int(len(unique_blocks)),
        "block_unit": "market_start_ms",
    }


def _average_ranks(x: np.ndarray) -> np.ndarray:
    order = np.argsort(x, kind="mergesort")
    ranks = np.empty(len(x), dtype=np.float64)
    i = 0
    while i < len(x):
        j = i + 1
        while j < len(x) and x[order[j]] == x[order[i]]:
            j += 1
        ranks[order[i:j]] = (i + 1 + j) / 2.0
        i = j
    return ranks


def spearman_corr(x: np.ndarray, y: np.ndarray) -> float:
    rx = _average_ranks(x)
    ry = _average_ranks(y)
    return float(np.corrcoef(rx, ry)[0, 1])


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


# --- Clock-offset validation -------------------------------------------------
# Both venues' ticks carry source_ts_ms (venue clock) and ingest_ts_ms
# (collector clock). Per-day minimum-delay envelopes bound relative clock
# *drift*; event-anchored response lags around large Binance moves, measured
# on the collector's single ingest clock, establish causal cross-venue
# ordering without any venue-clock synchronization assumption.

CLOCK_JUMP_REL = 0.0005     # >= 5 bps move within <= 1 s
CLOCK_JUMP_SEP_MS = 5000
CLOCK_RESP_WINDOW_MS = 2000
CLOCK_BASELINE_MS = 5000
CLOCK_BID_TICK = 0.01


def _day_parts(table: str) -> dict[str, list[Path]]:
    out: dict[str, list[Path]] = defaultdict(list)
    for p in sorted((UNIFIED / table).rglob("*.parquet")):
        for part in p.relative_to(UNIFIED / table).parts:
            if part.startswith("date="):
                out[part[5:]].append(p)
                break
    return dict(out)


def _load_day(paths: list[Path], cols: list[str], flt=None) -> dict[str, np.ndarray]:
    arrs: dict[str, list] = {c: [] for c in cols}
    for p in paths:
        t = pq.ParquetFile(p).read(columns=cols + ([flt[0]] if flt else []))
        if flt:
            t = t.filter(pc.equal(t[flt[0]], flt[1]))
        for c in cols:
            arrs[c].append(t[c].to_numpy(zero_copy_only=False))
    return {c: (np.concatenate(v) if v else np.array([])) for c, v in arrs.items()}


def clock_envelope() -> dict:
    env = {}
    for table, key in (("binance_ticks_ms", "binance"),
                       ("polymarket_ticks_ms", "polymarket")):
        days, mins = [], []
        for day, paths in _day_parts(table).items():
            d = _load_day(paths, ["source_ts_ms", "ingest_ts_ms"])
            delay = d["ingest_ts_ms"] - d["source_ts_ms"]
            if len(delay) < 100:
                continue
            days.append(day)
            mins.append(int(delay.min()))
        arr = np.array(mins, dtype=float)
        env[key] = {
            "days": days,
            "per_day_min_ms": mins,
            "median_of_min_ms": float(np.median(arr)),
            "range_of_min_ms": float(arr.max() - arr.min()),
        }
    return env


def _detect_jumps(src: np.ndarray, ing: np.ndarray, price: np.ndarray):
    order = np.argsort(src, kind="stable")
    src, ing, price = src[order], ing[order], price[order]
    idx = np.searchsorted(src, src - 1000, side="left")
    prev = price[np.clip(idx, 0, len(src) - 1)]
    rel = (price - prev) / prev
    hit = np.abs(rel) >= CLOCK_JUMP_REL
    jumps, last_t = [], -(10 ** 15)
    for i in np.flatnonzero(hit):
        if src[i] - last_t >= CLOCK_JUMP_SEP_MS:
            jumps.append((int(src[i]), int(ing[i]), 1 if rel[i] > 0 else -1))
            last_t = src[i]
    return jumps


def clock_event_anchored() -> dict:
    b_days = _day_parts("binance_ticks_ms")
    p_days = _day_parts("polymarket_ticks_ms")
    d_ing, d_src, asym = [], [], []
    n_jumps = 0
    for day in sorted(set(b_days) & set(p_days)):
        b = _load_day(b_days[day], ["source_ts_ms", "ingest_ts_ms", "price"])
        if len(b["price"]) < 1000:
            continue
        jumps = _detect_jumps(b["source_ts_ms"], b["ingest_ts_ms"], b["price"])
        if not jumps:
            continue
        p = _load_day(p_days[day],
                      ["source_ts_ms", "ingest_ts_ms", "best_bid", "market_slug"],
                      flt=("side_label", "UP"))
        if len(p["best_bid"]) == 0:
            continue
        o = np.argsort(p["ingest_ts_ms"], kind="stable")
        pi, ps = p["ingest_ts_ms"][o], p["source_ts_ms"][o]
        pb, pm = p["best_bid"][o], p["market_slug"][o]
        n_jumps += len(jumps)
        for (tb_src, tb_ing, sgn) in jumps:
            lo = np.searchsorted(pi, tb_ing - CLOCK_BASELINE_MS)
            hi = np.searchsorted(pi, tb_ing + CLOCK_RESP_WINDOW_MS)
            if hi - lo < 2:
                continue
            wi, ws, wb, wm = pi[lo:hi], ps[lo:hi], pb[lo:hi], pm[lo:hi]
            best = None
            for mkt in np.unique(wm):
                m = wm == mkt
                mi, ms_, mb = wi[m], ws[m], wb[m]
                pre = mi < tb_ing
                if not pre.any():
                    continue
                base = mb[pre][-1]
                post = np.flatnonzero((mi >= tb_ing) & ((mb - base) * sgn >= CLOCK_BID_TICK))
                if len(post):
                    j = post[0]
                    cand = (int(mi[j] - tb_ing), int(ms_[j] - tb_src),
                            int((mi[j] - ms_[j]) - (tb_ing - tb_src)))
                    if best is None or cand[0] < best[0]:
                        best = cand
            if best is not None:
                d_ing.append(best[0])
                d_src.append(best[1])
                asym.append(best[2])
    di, ds, da = (np.array(x, dtype=float) for x in (d_ing, d_src, asym))
    def stats(a):
        return {"median_ms": float(np.median(a)),
                "p25_ms": float(np.percentile(a, 25)),
                "p75_ms": float(np.percentile(a, 75))} if len(a) else {}
    return {
        "n_jumps": n_jumps,
        "n_matched": int(len(di)),
        "jump_threshold_rel": CLOCK_JUMP_REL,
        "response_window_ms": CLOCK_RESP_WINDOW_MS,
        "ingest_clock": stats(di),
        "source_clock": stats(ds),
        "pointwise_delay_asymmetry_ms": stats(da),
        "response_lags_ingest_ms": di.tolist(),
    }


def clock_validation(refresh: bool) -> dict:
    cache = STATS / "clock_validation.json"
    if cache.exists() and not refresh:
        return json.loads(cache.read_text())
    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "envelope": clock_envelope(),
        "event_anchored": clock_event_anchored(),
    }
    cache.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    return result


def plot_clock_validation(cv: dict, out: Path) -> None:
    env = cv["envelope"]
    ea = cv["event_anchored"]
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(7.0, 3.0))
    for key, color, label in (("binance", "#4f46e5", "Binance"),
                              ("polymarket", "#f97316", "Polymarket")):
        y = env[key]["per_day_min_ms"]
        ax1.plot(range(len(y)), y, marker="o", ms=2.5, lw=0.8, color=color,
                 label=f"{label} (med {env[key]['median_of_min_ms']:.0f} ms)")
    ax1.set_ylim(0, 115)
    ax1.set_xlabel("observed day index")
    ax1.set_ylabel("per-day min(ingest $-$ source) [ms]")
    ax1.set_title("(a) Min-delay envelopes", fontsize=9)
    ax1.legend(fontsize=7, frameon=False)
    ax1.spines[["top", "right"]].set_visible(False)

    lags = np.array(ea.get("response_lags_ingest_ms", []), dtype=float)
    lags = lags[lags <= 2000]
    ax2.hist(lags, bins=50, color="#ea580c", edgecolor="white", linewidth=0.3)
    ax2.axvline(np.median(lags), color="#4f46e5", ls="--", lw=1.2,
                label=f"median = {np.median(lags):.0f} ms")
    ax2.set_xlabel("response lag on collector clock [ms]")
    ax2.set_ylabel("count")
    ax2.set_title("(b) Quote response to $\\geq$5 bps moves", fontsize=9)
    ax2.legend(fontsize=7, frameon=False)
    ax2.spines[["top", "right"]].set_visible(False)
    fig.suptitle("Clock-offset validation", fontsize=10)
    fig.tight_layout()
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)


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


def load_step3() -> tuple[np.ndarray, np.ndarray, list[str], np.ndarray, np.ndarray]:
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
    market_start = np.array([int(r["market_start_ms"]) for r in rows], dtype=np.int64)
    return X, y, feature_names, vol, market_start


def pooled_oos_mask(market_start: np.ndarray) -> np.ndarray:
    """Rows scored out-of-sample by the trainer's walk-forward protocol.

    Markets are consecutive runs of market_start_ms in export order; the
    trainer seeds training with the first WF_MIN_TRAIN_MARKETS markets and
    tests expanding windows of WF_TEST_MARKETS markets stepped by
    WF_STEP_MARKETS, so pooled OOS rows are exactly the rows of markets
    [WF_MIN_TRAIN_MARKETS, WF_MIN_TRAIN_MARKETS + n_windows * WF_TEST_MARKETS).
    """
    boundaries = np.flatnonzero(np.diff(market_start) != 0) + 1
    starts = np.concatenate(([0], boundaries))
    n_markets = len(starts)
    n_windows = (n_markets - WF_MIN_TRAIN_MARKETS) // WF_STEP_MARKETS
    first_test_market = WF_MIN_TRAIN_MARKETS
    last_test_market = WF_MIN_TRAIN_MARKETS + n_windows * WF_TEST_MARKETS  # exclusive
    first_row = starts[first_test_market]
    last_row = starts[last_test_market] if last_test_market < n_markets else len(market_start)
    mask = np.zeros(len(market_start), dtype=bool)
    mask[first_row:last_row] = True
    return mask


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


def plot_lead_lag_vs_disagreement(lag: dict[str, np.ndarray], out: Path) -> tuple[dict, dict]:
    """Two-panel figure: median lead-lag by disagreement quintile (left)
    and by disagreement tercile regime (right)."""
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

    ts = np.quantile(delta, [0, 1 / 3, 2 / 3, 1.0])
    regime_stats = []
    for i, label in enumerate(["low", "mid", "high"]):
        lo, hi = ts[i], ts[i + 1]
        mask = (delta >= lo) & (delta <= hi if i == 2 else delta < hi)
        regime_stats.append({
            "regime": label,
            "median_lead_lag_ms": float(np.median(ll[mask])),
            "iqr_ms": float(np.subtract(*np.percentile(ll[mask], [75, 25]))),
            "n": int(mask.sum()),
        })

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(7.0, 3.0), sharey=True)
    x = np.arange(len(labels))
    ax1.bar(x, medians, color="#4f46e5", edgecolor="white")
    ax1.bar_label(ax1.containers[0], labels=[f"{v:.0f}" for v in medians], fontsize=7, padding=2)
    ax1.axhline(0, color="#94a3b8", linewidth=0.8)
    ax1.set_xticks(x)
    ax1.set_xticklabels(labels)
    ax1.set_ylabel("median lead_lag_ms")
    ax1.set_xlabel("|price_delta_bps| quintile")
    ax1.set_title("(a) By disagreement quintile", fontsize=9)
    ax1.spines[["top", "right"]].set_visible(False)

    ax2.bar([s["regime"] for s in regime_stats],
            [s["median_lead_lag_ms"] for s in regime_stats],
            color="#ea580c", edgecolor="white")
    ax2.bar_label(ax2.containers[0],
                  labels=[f"{s['median_lead_lag_ms']:.0f}" for s in regime_stats],
                  fontsize=7, padding=2)
    ax2.axhline(0, color="#94a3b8", linewidth=0.8)
    ax2.set_xlabel("disagreement regime (terciles)")
    ax2.set_title("(b) By disagreement regime", fontsize=9)
    ax2.spines[["top", "right"]].set_visible(False)

    fig.suptitle("Lead--Lag vs Cross-Venue Disagreement", fontsize=10)
    fig.tight_layout()
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    return ({"quintile_medians_ms": medians, "quintile_counts": counts},
            {"by_regime": regime_stats})


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
        "p90": float(np.quantile(spreads, 0.90)),
        "p95": float(np.quantile(spreads, 0.95)),
        "p99": float(np.quantile(spreads, 0.99)),
        "share_one_tick": float(np.mean(spreads <= 0.011)),
        "share_two_tick": float(np.mean((spreads > 0.011) & (spreads <= 0.021))),
    }


def plot_model_benchmarks(results: dict, out: Path, oos: dict | None = None) -> None:
    order = [k for k in BENCHMARK_LABELS if k in results]
    labels = [BENCHMARK_LABELS[k] for k in order]
    aucs = [results[k]["auc"] for k in order]
    colors = ["#94a3b8", "#4f46e5", "#f97316", "#cbd5e1"][: len(order)]
    fig, axes = plt.subplots(1, 2, figsize=(7.0, 2.8), width_ratios=[1.0, 1.35])
    ax = axes[1]
    bars = ax.barh(labels, aucs, color=colors)
    ax.set_xlim(0.5, 1.0)
    ax.set_xlabel("ROC AUC")
    ax.set_title("(b) Diagnostic full timeline", fontsize=9)
    ax.bar_label(bars, labels=[f"{v:.3f}" for v in aucs], padding=4, fontsize=8)
    ax.spines[["top", "right"]].set_visible(False)

    ax2 = axes[0]
    if oos:
        naive_oos = oos.get("naive_mid_prior_oos", {})
        model_oos = oos.get("model_pooled_oos", {})
        oos_labels = ["Naive mid prior", "Logistic v0.2.1"]
        oos_aucs = [naive_oos.get("auc", 0), model_oos.get("auc", 0)]
        bars2 = ax2.bar(oos_labels, oos_aucs, color=["#94a3b8", "#4f46e5"])
        ax2.bar_label(bars2, labels=[f"{v:.4f}" for v in oos_aucs], padding=3, fontsize=8)
    ax2.set_ylim(0.82, 0.85)
    ax2.set_ylabel("ROC AUC")
    ax2.set_title("(a) Pooled out-of-sample", fontsize=9)
    ax2.tick_params(axis="x", labelrotation=20)
    ax2.spines[["top", "right"]].set_visible(False)
    fig.suptitle("Forecast Benchmarks", fontsize=10)
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
    drift = forecasts.get("drift_only", {})
    imbalance = forecasts.get("ofi_60s_sigmoid", {})
    lines = [
        "% Auto-generated by analyze_research.py — do not edit by hand",
        f"\\newcommand{{\\OpenMarketAucDiff}}{{{auc.get('observed_diff', 0):.4f}}}",
        f"\\newcommand{{\\OpenMarketAucDiffLow}}{{{auc.get('ci_low', 0):.4f}}}",
        f"\\newcommand{{\\OpenMarketAucDiffHigh}}{{{auc.get('ci_high', 0):.4f}}}",
        f"\\newcommand{{\\OpenMarketAucPvalue}}{{{_format_pvalue(auc.get('p_value_one_sided', 1))}}}",
        f"\\newcommand{{\\OpenMarketLogisticAuc}}{{{logistic.get('auc', 0):.4f}}}",
        f"\\newcommand{{\\OpenMarketNaiveAuc}}{{{naive.get('auc', 0):.4f}}}",
        f"\\newcommand{{\\OpenMarketDriftAuc}}{{{drift.get('auc', 0):.4f}}}",
        f"\\newcommand{{\\OpenMarketImbalanceAuc}}{{{imbalance.get('auc', 0):.4f}}}",
        f"\\newcommand{{\\OpenMarketLogisticEce}}{{{logistic.get('ece', 0):.3f}}}",
        f"\\newcommand{{\\OpenMarketNaiveEce}}{{{naive.get('ece', 0):.3f}}}",
        f"\\newcommand{{\\OpenMarketLogisticScoreUs}}{{{logistic.get('score_us_per_row', 0):.3f}}}",
        f"\\newcommand{{\\OpenMarketBootstrapN}}{{{auc.get('n_bootstrap', 0):,}}}",
        f"\\newcommand{{\\OpenMarketBootstrapBlocks}}{{{auc.get('n_blocks', 0):,}}}",
    ]
    oos = findings.get("pooled_oos_comparison", {})
    if oos:
        naive_oos = oos.get("naive_mid_prior_oos", {})
        model_oos = oos.get("model_pooled_oos", {})
        lines.extend([
            f"\\newcommand{{\\OpenMarketOosRows}}{{{oos.get('oos_rows', 0):,}}}",
            f"\\newcommand{{\\OpenMarketNaiveOosAuc}}{{{naive_oos.get('auc', 0):.4f}}}",
            f"\\newcommand{{\\OpenMarketNaiveOosBrier}}{{{naive_oos.get('brier', 0):.3f}}}",
            f"\\newcommand{{\\OpenMarketNaiveOosEce}}{{{naive_oos.get('ece', 0):.3f}}}",
            f"\\newcommand{{\\OpenMarketNaiveOosLogLoss}}{{{naive_oos.get('log_loss', 0):.3f}}}",
            f"\\newcommand{{\\OpenMarketModelOosAuc}}{{{(model_oos.get('auc') or 0):.4f}}}",
            f"\\newcommand{{\\OpenMarketModelOosBrier}}{{{(model_oos.get('brier') or 0):.3f}}}",
            f"\\newcommand{{\\OpenMarketModelOosEce}}{{{(model_oos.get('ece') or 0):.3f}}}",
            f"\\newcommand{{\\OpenMarketModelOosLogLoss}}{{{(model_oos.get('log_loss') or 0):.3f}}}",
        ])
    flags = findings.get("quality_flag_distribution", {})
    if flags:
        for band, macro in (("tight", "Tight"), ("medium", "Medium"), ("wide", "Wide")):
            frac = flags.get(band, {}).get("fraction", 0.0)
            lines.append(
                f"\\newcommand{{\\OpenMarketFlag{macro}Pct}}{{{100 * frac:.1f}}}"
            )
    cv = findings.get("clock_validation", {})
    if cv:
        env = cv.get("envelope", {})
        ea = cv.get("event_anchored", {})
        b, p = env.get("binance", {}), env.get("polymarket", {})
        drift = b.get("range_of_min_ms", 0) + p.get("range_of_min_ms", 0)
        lines.extend([
            f"\\newcommand{{\\OpenMarketEnvBinanceMed}}{{{b.get('median_of_min_ms', 0):.0f}}}",
            f"\\newcommand{{\\OpenMarketEnvBinanceRange}}{{{b.get('range_of_min_ms', 0):.0f}}}",
            f"\\newcommand{{\\OpenMarketEnvPolyMed}}{{{p.get('median_of_min_ms', 0):.0f}}}",
            f"\\newcommand{{\\OpenMarketEnvPolyRange}}{{{p.get('range_of_min_ms', 0):.0f}}}",
            f"\\newcommand{{\\OpenMarketDriftBound}}{{{drift:.0f}}}",
            f"\\newcommand{{\\OpenMarketJumpCount}}{{{ea.get('n_jumps', 0):,}}}",
            f"\\newcommand{{\\OpenMarketJumpMatched}}{{{ea.get('n_matched', 0):,}}}",
            f"\\newcommand{{\\OpenMarketRespMedianIngest}}{{{ea.get('ingest_clock', {}).get('median_ms', 0):.0f}}}",
            f"\\newcommand{{\\OpenMarketRespMedianSource}}{{{ea.get('source_clock', {}).get('median_ms', 0):.0f}}}",
            f"\\newcommand{{\\OpenMarketDelayAsym}}{{{-ea.get('pointwise_delay_asymmetry_ms', {}).get('median_ms', 0):.0f}}}",
        ])
    if spread:
        tight = next((r for r in spread if r["regime"] == "tight"), spread[0])
        wide = next((r for r in spread if r["regime"] == "wide"), spread[-1])
        lines.extend([
            f"\\newcommand{{\\OpenMarketBrierTight}}{{{tight.get('brier_full_model', 0):.3f}}}",
            f"\\newcommand{{\\OpenMarketBrierWide}}{{{wide.get('brier_full_model', 0):.3f}}}",
        ])
    spread_stats = findings.get("spread", {})
    if spread_stats:
        lines.extend([
            f"\\newcommand{{\\OpenMarketSpreadMedian}}{{{spread_stats.get('median', 0):.2f}}}",
            f"\\newcommand{{\\OpenMarketSpreadPNinety}}{{{spread_stats.get('p90', 0):.2f}}}",
            f"\\newcommand{{\\OpenMarketSpreadPNinetyFive}}{{{spread_stats.get('p95', 0):.2f}}}",
            f"\\newcommand{{\\OpenMarketSpreadPNinetyNine}}{{{spread_stats.get('p99', 0):.2f}}}",
            f"\\newcommand{{\\OpenMarketSpreadOneTickPct}}{{{100 * spread_stats.get('share_one_tick', 0):.1f}}}",
            f"\\newcommand{{\\OpenMarketSpreadTwoTickPct}}{{{100 * spread_stats.get('share_two_tick', 0):.1f}}}",
        ])
    lag_corr = findings.get("lead_lag_price_corr", {})
    if lag_corr.get("spearman_abs_delta") is not None:
        corr = lag_corr["spearman_abs_delta"]
        if abs(corr) < 0.005:
            corr = 0.0
        lines.append(
            f"\\newcommand{{\\OpenMarketLagPriceCorr}}{{{corr:.2f}}}"
        )
    (STATS / "research_stats.tex").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh-clock", action="store_true",
                    help="recompute clock validation instead of using the cache")
    args = ap.parse_args()

    plt.rcParams.update({"font.size": 9, "figure.dpi": 300})
    FIG.mkdir(parents=True, exist_ok=True)
    STATS.mkdir(parents=True, exist_ok=True)

    cv = clock_validation(args.refresh_clock)
    plot_clock_validation(cv, FIG / "clock-validation.pdf")

    lag = load_lag_sample()
    spread = load_spread_sample()

    findings: dict = {"generated_at": datetime.now(timezone.utc).isoformat()}
    findings["clock_validation"] = {
        "envelope": {k: {kk: vv for kk, vv in v.items() if kk != "per_day_min_ms"}
                     for k, v in cv["envelope"].items()},
        "event_anchored": {k: v for k, v in cv["event_anchored"].items()
                           if k != "response_lags_ingest_ms"},
    }

    findings["lead_lag_vs_disagreement"], findings["lead_lag_by_regime"] = (
        plot_lead_lag_vs_disagreement(lag, FIG / "lead-lag-vs-disagreement.pdf")
    )
    findings["spread"] = plot_spread_hist(spread, FIG / "spread-distribution.pdf")

    # quality_flag is a categorical pairing-window band assigned by the
    # recorder: tight (|lag| <= 100 ms), medium (<= 300 ms), wide (> 300 ms).
    quality = lag["quality_flag"].astype(str)
    ll_all = lag["lead_lag_ms"]
    flag_dist = {}
    for band in ("tight", "medium", "wide"):
        mask = quality == band
        flag_dist[band] = {
            "fraction": float(np.mean(mask)) if len(quality) else 0.0,
            "n": int(mask.sum()),
            "median_lag_ms": float(np.median(ll_all[mask])) if mask.any() else None,
        }
    findings["quality_flag_distribution"] = flag_dist

    X, y, names, vol, market_start = load_step3()
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
        findings["auc_bootstrap"] = bootstrap_auc_diff(y, full_p, prior, market_start)

        # Naive prior restricted to the trainer's pooled OOS rows, directly
        # comparable to the published walk-forward pooled OOS model metrics.
        oos = pooled_oos_mask(market_start)
        naive_oos = benchmark_row(y[oos], prior[oos])
        pooled_model = {}
        metrics_path = latest_model_metrics_path()
        if metrics_path and metrics_path.exists():
            pooled_model = json.loads(metrics_path.read_text()).get("metrics", {})
        findings["pooled_oos_comparison"] = {
            "oos_rows": int(oos.sum()),
            "naive_mid_prior_oos": naive_oos,
            "model_pooled_oos": {
                "auc": pooled_model.get("auc_roc"),
                "brier": pooled_model.get("brier"),
                "ece": pooled_model.get("ece"),
                "log_loss": pooled_model.get("log_loss"),
            },
        }
    if "drift_prob_up" in names:
        drift = X[:, names.index("drift_prob_up")]
        benchmarks["drift_only"] = benchmark_row(y, drift)
    if "imbalance_60s" in names:
        ofi = X[:, names.index("imbalance_60s")]
        ofi_p = 1 / (1 + np.exp(-ofi))
        benchmarks["ofi_60s_sigmoid"] = benchmark_row(y, ofi_p)

    findings["forecast_benchmarks"] = benchmarks
    plot_model_benchmarks(
        benchmarks,
        FIG / "forecast-benchmarks.pdf",
        findings.get("pooled_oos_comparison"),
    )

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
            "spearman_abs_delta": spearman_corr(np.abs(delta[valid]), ll[valid]),
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
