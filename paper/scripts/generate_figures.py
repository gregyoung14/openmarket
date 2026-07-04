#!/usr/bin/env python3
"""Generate paper/assets/figures/*.pdf — clean flowcharts for LaTeX."""
from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, ConnectionPatch

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "assets" / "figures"
REPO = ROOT.parent
LAG_SAMPLE = REPO / "data/hf_release/sample_flat/lag_pairs_ms.parquet"

# Palette
FILL = "#eef2ff"
EDGE = "#334155"
TEXT = "#0f172a"
ACCENT = "#4f46e5"
ORANGE = "#ea580c"


class Chart:
    """Coordinate-normalized (0–1) flowchart builder."""

    def __init__(self, w: float, h: float, title: str | None = None):
        self.fig, self.ax = plt.subplots(figsize=(w, h))
        self.ax.set_xlim(0, 1)
        self.ax.set_ylim(0, 1)
        self.ax.axis("off")
        self.ax.set_aspect("auto")
        self.nodes: dict[str, tuple[float, float, float, float]] = {}
        if title:
            self.ax.set_title(title, fontsize=11, fontweight="bold", color=TEXT, pad=10)

    def node(self, nid: str, cx: float, cy: float, label: str,
             w: float = 0.30, h: float = 0.07) -> None:
        x, y = cx - w / 2, cy - h / 2
        patch = FancyBboxPatch(
            (x, y), w, h,
            boxstyle="round,pad=0.008,rounding_size=0.015",
            linewidth=1.2, edgecolor=EDGE, facecolor=FILL, zorder=2,
        )
        self.ax.add_patch(patch)
        self.ax.text(cx, cy, label, ha="center", va="center",
                     fontsize=8.5, color=TEXT, zorder=3, wrap=True)
        self.nodes[nid] = (cx, cy, w, h)

    def arrow(self, src: str, dst: str, *,
              src_side: str = "bottom", dst_side: str = "top",
              color: str = EDGE, style: str = "-|>") -> None:
        sx, sy, sw, sh = self.nodes[src]
        dx, dy, dw, dh = self.nodes[dst]
        anchors = {
            "top": (sx, sy + sh / 2),
            "bottom": (sx, sy - sh / 2),
            "left": (sx - sw / 2, sy),
            "right": (sx + sw / 2, sy),
        }
        danchors = {
            "top": (dx, dy + dh / 2),
            "bottom": (dx, dy - dh / 2),
            "left": (dx - dw / 2, dy),
            "right": (dx + dw / 2, dy),
        }
        p0, p1 = anchors[src_side], danchors[dst_side]
        self.ax.add_patch(ConnectionPatch(
            p0, p1, "data", "data",
            arrowstyle=style, shrinkA=4, shrinkB=4,
            linewidth=1.2, color=color, zorder=1,
        ))

    def save(self, name: str) -> None:
        self.fig.savefig(OUT / name, bbox_inches="tight", pad_inches=0.08)
        plt.close(self.fig)


def architecture():
    c = Chart(5.5, 7.0, "OpenMarket Pipeline")
    # Main spine (center)
    c.node("bws", 0.50, 0.94, "Binance WS")
    c.node("tick", 0.50, 0.82, "Tick Stream\nCollector", h=0.08)
    c.node("sync", 0.50, 0.66, "Timestamp\nSynchronizer", h=0.08)
    c.node("feat", 0.50, 0.50, "Feature Generator")
    c.node("ml", 0.50, 0.38, "ML / Signal Engine")
    c.node("bt", 0.50, 0.26, "Backtester")
    c.node("eval", 0.50, 0.14, "Evaluation")
    for a, b in [("bws", "tick"), ("tick", "sync"), ("sync", "feat"),
                 ("feat", "ml"), ("ml", "bt"), ("bt", "eval")]:
        c.arrow(a, b)
    # Polymarket branch (right)
    c.node("pws", 0.82, 0.74, "Polymarket WS")
    c.node("book", 0.82, 0.58, "Order Book\nCollector", h=0.08)
    c.arrow("pws", "book")
    c.arrow("book", "sync", src_side="left", dst_side="right")
    c.save("architecture.pdf")


def features():
    c = Chart(7.0, 2.2, "Feature Engineering Pipeline")
    labels = ["Raw ticks", "Alignment", "Feature families", "Label generation"]
    xs = [0.14, 0.38, 0.62, 0.86]
    ids = [f"f{i}" for i in range(4)]
    for i, (nid, x, lab) in enumerate(zip(ids, xs, labels)):
        c.node(nid, x, 0.50, lab, w=0.22, h=0.22)
    for i in range(3):
        c.arrow(ids[i], ids[i + 1], src_side="right", dst_side="left")
    c.save("features.pdf")


def training():
    c = Chart(4.5, 5.5, "Training Workflow")
    steps = ["Feature matrix", "Time-based split", "Model fit",
             "Calibration check", "HF artifact export"]
    ys = [0.88, 0.70, 0.52, 0.34, 0.16]
    ids = [f"t{i}" for i in range(5)]
    for nid, y, lab in zip(ids, ys, steps):
        c.node(nid, 0.50, y, lab, w=0.42, h=0.10)
    for i in range(4):
        c.arrow(ids[i], ids[i + 1])
    c.save("training.pdf")


def backtest():
    c = Chart(4.5, 5.5, "Backtesting (Simulated)")
    steps = ["Market window", "Signal gate", "Simulated fill",
             "Settlement", "Metric aggregation"]
    ys = [0.88, 0.70, 0.52, 0.34, 0.16]
    ids = [f"b{i}" for i in range(5)]
    for nid, y, lab in zip(ids, ys, steps):
        c.node(nid, 0.50, y, lab, w=0.42, h=0.10)
    for i in range(4):
        c.arrow(ids[i], ids[i + 1])
    c.save("backtest.pdf")


def repro():
    c = Chart(6.5, 4.0, "Reproducibility Flow")
    c.node("clone", 0.50, 0.90, "git clone", w=0.28)
    c.node("check", 0.50, 0.72, "cargo check", w=0.28)
    c.node("valid", 0.50, 0.54, "HF validate", w=0.28)
    c.arrow("clone", "check")
    c.arrow("check", "valid")
    branches = [("nb", 0.18, "notebook"), ("uni", 0.50, "unified split"),
                ("dock", 0.82, "Docker")]
    for nid, x, lab in branches:
        c.node(nid, x, 0.22, lab, w=0.24, h=0.10)
        c.arrow("valid", nid)
    c.save("repro.pdf")


def schema():
    c = Chart(6.5, 3.5, "Core Dataset Tables")
    c.node("bin", 0.18, 0.78, "binance_ticks_ms", w=0.30)
    c.node("poly", 0.50, 0.78, "polymarket_ticks_ms", w=0.32)
    c.node("meta", 0.82, 0.78, "market_meta", w=0.26)
    c.node("lag", 0.50, 0.28, "lag_pairs_ms", w=0.30, h=0.10)
    c.arrow("bin", "lag")
    c.arrow("poly", "lag")
    c.arrow("meta", "lag")
    c.save("schema.pdf")


def lead_lag_hist():
    try:
        import numpy as np
        import pyarrow.parquet as pq
    except ImportError:
        print("warning: pyarrow/numpy missing; skipping lead-lag histogram", file=sys.stderr)
        return

    if not LAG_SAMPLE.exists():
        print(f"warning: {LAG_SAMPLE} not found", file=sys.stderr)
        return

    table = pq.read_table(LAG_SAMPLE)
    col = next((n for n in table.column_names if "lead_lag" in n.lower()), None)
    if col is None:
        return

    data = table[col].to_numpy()
    data = data[np.isfinite(data)]
    if len(data) == 0:
        return

    fig, ax = plt.subplots(figsize=(5.5, 3.2))
    ax.hist(data, bins=min(40, max(10, len(data) // 5)),
            color=ORANGE, edgecolor="white", linewidth=0.4)
    p5, med, p95 = np.percentile(data, [5, 50, 95])
    ax.axvline(med, color=ACCENT, linestyle="--", linewidth=1.5,
               label=f"median = {med:.0f} ms")
    ax.axvline(p5, color="#94a3b8", linestyle=":", linewidth=1.2,
               label=f"p5 = {p5:.0f} ms")
    ax.axvline(p95, color="#94a3b8", linestyle=":", linewidth=1.2,
               label=f"p95 = {p95:.0f} ms")
    ax.set_xlabel("lead_lag_ms", fontsize=9)
    ax.set_ylabel("count", fontsize=9)
    ax.set_title("Lead-Lag Distribution (HF sample)", fontsize=10, color=TEXT)
    ax.legend(fontsize=8, frameon=False)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig.savefig(OUT / "lead-lag-hist.pdf", bbox_inches="tight")
    plt.close(fig)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update({
        "font.family": "sans-serif",
        "font.sans-serif": ["Helvetica", "Arial", "DejaVu Sans"],
        "font.size": 9,
        "figure.dpi": 200,
    })
    # Pipeline diagrams are native TikZ in sections/*.tex. Legacy benchmark
    # figures are intentionally not emitted; current benchmark figures are
    # generated by scripts/paper/generate_ml_figures.py.
    # lead-lag-hist.pdf: generated by scripts/paper/analyze_unified.py (unified split)
    print(f"generated data figures in {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
