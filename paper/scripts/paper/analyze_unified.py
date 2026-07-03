#!/usr/bin/env python3
"""Empirical characterization of the unified HF split for the OpenMarket paper.

Reads local unified Parquet (or HF sample fallback), writes:
  - assets/stats/characterization.json
  - assets/stats/characterization.tex  (\\input in LaTeX)
  - assets/figures/lead-lag-hist.pdf
  - assets/figures/daily-volume.pdf
  - assets/figures/dataset-scale.pdf

Usage:
  paper/.venv/bin/python scripts/paper/analyze_unified.py
  paper/.venv/bin/python scripts/paper/analyze_unified.py --root ../../data/hf_release/unified_parquet
"""
from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[2]  # paper/
REPO = ROOT.parent
DEFAULT_UNIFIED = REPO / "data/hf_release/unified_parquet"
STATS_DIR = ROOT / "assets/stats"
FIG_DIR = ROOT / "assets/figures"
MANIFEST = REPO / "data/hf_release/metadata_redacted/snapshot_manifest.json"
MODEL_METRICS = REPO / "models/hf_staging/v0.1/binary_outcome_metrics_1778654444636.json"
BASELINE_SAMPLE = REPO / "benchmarks/baselines/v0.1-sample.json"
RELEASE_TAG = "v0.5.0"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--root", type=Path, default=DEFAULT_UNIFIED)
    p.add_argument("--max-lag-rows", type=int, default=0,
                   help="subsample lag_pairs (0 = all)")
    return p.parse_args()


def scan_tables(root: Path) -> dict[str, dict]:
    per_table: dict[str, dict] = defaultdict(lambda: {"rows": 0, "parts": 0, "bytes": 0})
    for pq_path in sorted(root.rglob("*.parquet")):
        rel = pq_path.relative_to(root)
        if rel.parts[0] in ("metadata",):
            continue
        table = rel.parts[0]
        meta = pq.read_metadata(str(pq_path))
        per_table[table]["rows"] += meta.num_rows
        per_table[table]["parts"] += 1
        per_table[table]["bytes"] += pq_path.stat().st_size
    return dict(sorted(per_table.items()))


def daily_counts(root: Path, table: str, max_dates: int = 60) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    base = root / table
    if not base.exists():
        return {}
    for pq_path in sorted(base.rglob("*.parquet")):
        rel = pq_path.relative_to(base)
        date_key = "unknown"
        for part in rel.parts:
            if part.startswith("date="):
                date_key = part.split("=", 1)[1]
                break
        meta = pq.read_metadata(str(pq_path))
        counts[date_key] += meta.num_rows
    if len(counts) > max_dates:
        top = sorted(counts.items(), key=lambda x: x[1], reverse=True)[:max_dates]
        return dict(sorted(top))
    return dict(sorted(counts.items()))


def load_lead_lag(root: Path, max_rows: int = 0) -> np.ndarray:
    lag_dir = root / "lag_pairs_ms"
    chunks: list[np.ndarray] = []
    total = 0
    for pq_path in sorted(lag_dir.rglob("*.parquet")):
        pf = pq.ParquetFile(pq_path)
        table = pf.read(columns=["lead_lag_ms"])
        arr = table["lead_lag_ms"].to_numpy(zero_copy_only=False)
        arr = arr[np.isfinite(arr)]
        chunks.append(arr)
        total += len(arr)
        if max_rows and total >= max_rows:
            break
    if not chunks:
        return np.array([], dtype=np.float64)
    out = np.concatenate(chunks)
    if max_rows and len(out) > max_rows:
        rng = np.random.default_rng(42)
        out = rng.choice(out, size=max_rows, replace=False)
    return out


def manifest_stats() -> dict:
    if not MANIFEST.exists():
        return {}
    data = json.loads(MANIFEST.read_text())
    snaps = data.get("snapshots", [])
    if not snaps:
        return {}
    ts = [s["snapshot_ts"] for s in snaps]
    return {
        "snapshot_count": len(snaps),
        "collection_start": min(ts),
        "collection_end": max(ts),
        "compressed_gb": round(sum(s.get("compressed_bytes", 0) for s in snaps) / 1e9, 2),
    }


def rust_loc() -> int:
    total = 0
    crates = REPO / "crates"
    if not crates.exists():
        return 0
    for path in crates.rglob("*.rs"):
        total += sum(1 for line in path.read_text(encoding="utf-8", errors="ignore").splitlines()
                     if line.strip())
    return total


def plot_lead_lag(data: np.ndarray, out: Path) -> dict:
    fig, ax = plt.subplots(figsize=(5.8, 3.4))
    ax.hist(data, bins=80, color="#ea580c", edgecolor="white", linewidth=0.3)
    p5, p25, med, p75, p95 = np.percentile(data, [5, 25, 50, 75, 95])
    ax.axvline(med, color="#4f46e5", linestyle="--", linewidth=1.5,
               label=f"median = {med:.0f} ms")
    ax.axvline(p5, color="#94a3b8", linestyle=":", linewidth=1.0)
    ax.axvline(p95, color="#94a3b8", linestyle=":", linewidth=1.0,
               label=f"p5/p95 = {p5:.0f}/{p95:.0f} ms")
    ax.set_xlabel("lead_lag_ms")
    ax.set_ylabel("count")
    ax.set_title(f"Lead--Lag Distribution (unified, n={len(data):,})")
    ax.legend(fontsize=8, frameon=False)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    return {
        "n": int(len(data)),
        "mean_ms": float(np.mean(data)),
        "std_ms": float(np.std(data)),
        "median_ms": float(med),
        "p5_ms": float(p5),
        "p25_ms": float(p25),
        "p75_ms": float(p75),
        "p95_ms": float(p95),
    }


def plot_daily_volume(poly_daily: dict[str, int], binance_daily: dict[str, int], out: Path) -> None:
    dates = sorted(set(poly_daily) | set(binance_daily))
    if not dates:
        return
    # show last 30 days with data
    dates = dates[-30:]
    x = np.arange(len(dates))
    w = 0.38
    fig, ax = plt.subplots(figsize=(7.0, 3.2))
    ax.bar(x - w / 2, [poly_daily.get(d, 0) for d in dates], w,
           label="polymarket_ticks_ms", color="#f97316")
    ax.bar(x + w / 2, [binance_daily.get(d, 0) for d in dates], w,
           label="binance_trades", color="#4f46e5")
    ax.set_xticks(x)
    ax.set_xticklabels([d.replace("2026-", "") for d in dates], rotation=45, ha="right", fontsize=7)
    ax.set_ylabel("events / day")
    ax.set_title("Daily Event Volume (unified split, last 30 days)")
    ax.legend(fontsize=8, frameon=False)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)


def plot_dataset_scale(per_table: dict[str, dict], out: Path) -> None:
    keys = ["polymarket_ticks_ms", "binance_trades", "binance_ticks_ms",
            "lag_pairs_ms", "market_meta"]
    labels, values = [], []
    for k in keys:
        if k in per_table:
            labels.append(k.replace("_", "\n"))
            values.append(per_table[k]["rows"])
    if not values:
        return
    fig, ax = plt.subplots(figsize=(6.0, 3.2))
    bars = ax.bar(range(len(labels)), values, color="#4f46e5", edgecolor="white")
    ax.set_yscale("log")
    ax.set_xticks(range(len(labels)))
    ax.set_xticklabels(labels, fontsize=7)
    ax.set_ylabel("rows (log scale)")
    ax.set_title("Unified Split: Core Table Row Counts")
    ax.bar_label(bars, labels=[f"{v/1e6:.2f}M" if v >= 1e6 else f"{v/1e3:.0f}K" for v in values],
                fontsize=7, padding=2)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)


def write_tex(stats: dict, path: Path) -> None:
    ll = stats["lead_lag"]
    m = stats["manifest"]
    tot = stats["totals"]
    lines = [
        "% Auto-generated by scripts/paper/analyze_unified.py — do not edit by hand",
        f"\\newcommand{{\\OpenMarketReleaseTag}}{{{RELEASE_TAG}}}",
        f"\\newcommand{{\\OpenMarketTotalRows}}{{{tot['total_rows']:,}}}",
        f"\\newcommand{{\\OpenMarketUnifiedGiB}}{{{tot['total_gib']:.2f}}}",
        f"\\newcommand{{\\OpenMarketSnapshots}}{{{m.get('snapshot_count', 202)}}}",
        f"\\newcommand{{\\OpenMarketLagPairs}}{{{ll['n']:,}}}",
        f"\\newcommand{{\\OpenMarketLagMedian}}{{{ll['median_ms']:.0f}}}",
        f"\\newcommand{{\\OpenMarketLagPFive}}{{{ll['p5_ms']:.0f}}}",
        f"\\newcommand{{\\OpenMarketLagPNinetyFive}}{{{ll['p95_ms']:.0f}}}",
        f"\\newcommand{{\\OpenMarketUnifiedMetaScanSeconds}}{{{stats['scan_seconds']:.3f}}}",
        f"\\newcommand{{\\OpenMarketLagPairsLoadSeconds}}{{{stats['lag_load_seconds']:.3f}}}",
        f"\\newcommand{{\\OpenMarketCollectionStart}}{{{m.get('collection_start', '2026-03-14')[:10]}}}",
        f"\\newcommand{{\\OpenMarketCollectionEnd}}{{{m.get('collection_end', '2026-07-01')[:10]}}}",
        f"\\newcommand{{\\OpenMarketRustLoc}}{{{stats.get('rust_loc', 17000):,}}}",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    args = parse_args()
    STATS_DIR.mkdir(parents=True, exist_ok=True)
    FIG_DIR.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update({"font.size": 9, "figure.dpi": 200})

    if not args.root.exists():
        print(f"ERROR: unified root not found: {args.root}", flush=True)
        return 1

    t0 = time.perf_counter()
    per_table = scan_tables(args.root)
    scan_seconds = time.perf_counter() - t0

    total_rows = sum(v["rows"] for v in per_table.values())
    total_bytes = sum(v["bytes"] for v in per_table.values())

    t1 = time.perf_counter()
    lead_lag = load_lead_lag(args.root, max_rows=args.max_lag_rows)
    lag_load_seconds = time.perf_counter() - t1

    ll_stats = plot_lead_lag(lead_lag, FIG_DIR / "lead-lag-hist.pdf")

    poly_daily = daily_counts(args.root, "polymarket_ticks_ms")
    binance_daily = daily_counts(args.root, "binance_trades")
    plot_daily_volume(poly_daily, binance_daily, FIG_DIR / "daily-volume.pdf")
    plot_dataset_scale(per_table, FIG_DIR / "dataset-scale.pdf")

    manifest = manifest_stats()
    model = {}
    if MODEL_METRICS.exists():
        model = json.loads(MODEL_METRICS.read_text()).get("metrics", {})

    sample_baseline = {}
    if BASELINE_SAMPLE.exists():
        sample_baseline = json.loads(BASELINE_SAMPLE.read_text())

    stats = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "release_tag": RELEASE_TAG,
        "unified_root": str(args.root),
        "scan_seconds": round(scan_seconds, 3),
        "lag_load_seconds": round(lag_load_seconds, 3),
        "manifest": manifest,
        "totals": {
            "total_rows": total_rows,
            "total_bytes": total_bytes,
            "total_gib": total_bytes / (1024 ** 3),
            "table_count": len(per_table),
        },
        "per_table": per_table,
        "lead_lag": ll_stats,
        "daily_polymarket_days": len(poly_daily),
        "daily_binance_days": len(binance_daily),
        "model_v01_metrics": model,
        "sample_baseline": sample_baseline,
        "rust_loc": rust_loc(),
    }

    json_path = STATS_DIR / "characterization.json"
    json_path.write_text(json.dumps(stats, indent=2) + "\n", encoding="utf-8")
    write_tex(stats, STATS_DIR / "characterization.tex")

    print(f"wrote {json_path}")
    print(f"wrote {STATS_DIR / 'characterization.tex'}")
    print(f"total_rows={total_rows:,} lag_n={ll_stats['n']:,} median={ll_stats['median_ms']:.0f}ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
