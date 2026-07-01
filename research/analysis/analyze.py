"""
Comprehensive analysis of backtest trade logs.
Finds patterns by hour, day of week, volume, volatility, whipsaw,
and uses ML to identify optimal blacklist hours + feature importance.

Usage:
    python analysis/analyze.py [--trade-log v10_0_trade_log.csv] [--data-dir data/raw]
"""

import argparse
import glob
import os
import sys
from datetime import datetime, timezone, timedelta

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np
import pandas as pd
import seaborn as sns
from sklearn.ensemble import (
    GradientBoostingClassifier,
    RandomForestClassifier,
)
from sklearn.inspection import permutation_importance
from sklearn.metrics import classification_report, confusion_matrix
from sklearn.model_selection import cross_val_score, TimeSeriesSplit
from sklearn.preprocessing import StandardScaler

# ──────────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────────
ET_OFFSET = timedelta(hours=-5)  # UTC → ET
OUTPUT_DIR = "analysis/output"

CURRENT_BLACKLIST_ET = {0, 9, 10, 15, 16}


def parse_args():
    p = argparse.ArgumentParser(description="Backtest trade analysis")
    p.add_argument("--trade-log", default="v10_0_trade_log.csv")
    p.add_argument("--data-dir", default="data/raw")
    p.add_argument("--output", default=OUTPUT_DIR)
    return p.parse_args()


# ──────────────────────────────────────────────────────────────────
# Data Loading
# ──────────────────────────────────────────────────────────────────
def load_trades(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)

    # Extract epoch from slug: "btcusdc-1766361600"
    df["epoch_s"] = df["slug"].str.split("-").str[-1].astype(int)
    df["datetime_utc"] = pd.to_datetime(df["epoch_s"], unit="s", utc=True)
    df["datetime_et"] = df["datetime_utc"] + ET_OFFSET

    df["hour_utc"] = df["datetime_utc"].dt.hour
    df["hour_et"] = df["datetime_et"].dt.hour
    df["dow"] = df["datetime_et"].dt.dayofweek  # 0=Mon, 6=Sun
    df["dow_name"] = df["datetime_et"].dt.day_name()
    df["date"] = df["datetime_et"].dt.date
    df["week"] = df["datetime_et"].dt.isocalendar().week.astype(int)

    df["won"] = df["correct"].astype(int)
    df["lost"] = (~df["correct"]).astype(int)

    return df


def load_kline_stats(data_dir: str) -> pd.DataFrame:
    """Load raw klines and compute daily stats: volume, volatility, whipsaw."""
    csv_files = sorted(glob.glob(os.path.join(data_dir, "*.csv")))
    if not csv_files:
        print(f"  ⚠️  No CSVs in {data_dir}, skipping kline analysis")
        return pd.DataFrame()

    cols = [
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "num_trades",
        "taker_buy_base_vol", "taker_buy_quote_vol", "ignore",
    ]

    records = []
    for f in csv_files:
        df = pd.read_csv(f, header=None, names=cols)
        df["open_time"] = pd.to_datetime(df["open_time"], unit="us", utc=True)

        # Compute hourly stats
        df["hour_utc"] = df["open_time"].dt.hour
        df["date"] = df["open_time"].dt.date

        for (date, hour), group in df.groupby(["date", "hour_utc"]):
            closes = group["close"].values
            highs = group["high"].values
            lows = group["low"].values

            # Volatility: std of log returns
            log_rets = np.diff(np.log(closes + 1e-12))
            volatility = np.std(log_rets) if len(log_rets) > 1 else 0

            # Whipsaw: sum of direction changes / total bars
            direction_changes = np.sum(np.diff(np.sign(np.diff(closes))) != 0) if len(closes) > 2 else 0
            whipsaw = direction_changes / max(len(closes) - 2, 1)

            # Volume
            total_vol = group["volume"].sum()
            buy_vol = group["taker_buy_base_vol"].sum()
            sell_vol = total_vol - buy_vol

            # Range
            hour_range = (highs.max() - lows.min()) / (closes[0] + 1e-12)

            # Path efficiency
            direct = abs(closes[-1] - closes[0])
            total_path = np.sum(np.abs(np.diff(closes)))
            path_eff = direct / (total_path + 1e-12)

            records.append({
                "date": date,
                "hour_utc": hour,
                "volatility": volatility,
                "whipsaw": whipsaw,
                "volume": total_vol,
                "buy_vol": buy_vol,
                "sell_vol": sell_vol,
                "ofi": (buy_vol - sell_vol) / (total_vol + 1e-12),
                "range_pct": hour_range,
                "path_eff": path_eff,
                "num_bars": len(group),
            })

    return pd.DataFrame(records)


# ──────────────────────────────────────────────────────────────────
# Analysis Functions
# ──────────────────────────────────────────────────────────────────
def print_header(title: str):
    print(f"\n{'='*70}")
    print(f"  {title}")
    print(f"{'='*70}")


def analyze_by_hour(df: pd.DataFrame, output_dir: str):
    print_header("HOURLY ANALYSIS (ET)")

    hourly = df.groupby("hour_et").agg(
        trades=("won", "count"),
        wins=("won", "sum"),
        losses=("lost", "sum"),
        avg_conf=("conf", "mean"),
        avg_edge=("edge", "mean"),
        avg_pnl=("pnl", "mean"),
    )
    hourly["win_rate"] = (hourly["wins"] / hourly["trades"] * 100).round(1)
    hourly["is_blacklisted"] = hourly.index.isin(CURRENT_BLACKLIST_ET)

    print(hourly.to_string())

    # Identify bad hours (below 55% win rate with enough trades)
    bad_hours = hourly[(hourly["win_rate"] < 55) & (hourly["trades"] >= 10)]
    marginal_hours = hourly[(hourly["win_rate"] >= 55) & (hourly["win_rate"] < 60) & (hourly["trades"] >= 10)]

    print(f"\n  🔴 BAD HOURS (< 55% win rate, ≥10 trades):")
    if len(bad_hours) > 0:
        for h in bad_hours.index:
            bl = " [ALREADY BLACKLISTED]" if h in CURRENT_BLACKLIST_ET else " ⚡ RECOMMEND BLACKLIST"
            print(f"     Hour {h:02d} ET: {bad_hours.loc[h, 'win_rate']:.1f}% ({bad_hours.loc[h, 'trades']} trades){bl}")
    else:
        print("     None!")

    print(f"\n  🟡 MARGINAL HOURS (55-60% win rate):")
    if len(marginal_hours) > 0:
        for h in marginal_hours.index:
            print(f"     Hour {h:02d} ET: {marginal_hours.loc[h, 'win_rate']:.1f}% ({marginal_hours.loc[h, 'trades']} trades)")
    else:
        print("     None!")

    # Plot
    fig, axes = plt.subplots(2, 1, figsize=(14, 10))

    colors = []
    for h in hourly.index:
        if h in CURRENT_BLACKLIST_ET:
            colors.append("#444444")
        elif hourly.loc[h, "win_rate"] < 55:
            colors.append("#ff4444")
        elif hourly.loc[h, "win_rate"] < 60:
            colors.append("#ffaa00")
        elif hourly.loc[h, "win_rate"] >= 70:
            colors.append("#00cc44")
        else:
            colors.append("#4488ff")

    axes[0].bar(hourly.index, hourly["win_rate"], color=colors, edgecolor="white", linewidth=0.5)
    axes[0].axhline(y=65, color="#00cc44", linestyle="--", alpha=0.5, label="65% target")
    axes[0].axhline(y=55, color="#ff4444", linestyle="--", alpha=0.5, label="55% danger")
    axes[0].axhline(y=50, color="#888888", linestyle=":", alpha=0.5, label="50% (coin flip)")
    axes[0].set_xlabel("Hour (ET)")
    axes[0].set_ylabel("Win Rate (%)")
    axes[0].set_title("Win Rate by Hour (ET)")
    axes[0].set_xticks(range(24))
    axes[0].legend()

    axes[1].bar(hourly.index, hourly["trades"], color=colors, edgecolor="white", linewidth=0.5)
    axes[1].set_xlabel("Hour (ET)")
    axes[1].set_ylabel("Trade Count")
    axes[1].set_title("Trade Volume by Hour (ET)")
    axes[1].set_xticks(range(24))

    plt.tight_layout()
    plt.savefig(os.path.join(output_dir, "hourly_analysis.png"), dpi=150, bbox_inches="tight")
    plt.close()
    print(f"\n  📊 Saved: {output_dir}/hourly_analysis.png")

    return hourly


def analyze_by_dow(df: pd.DataFrame, output_dir: str):
    print_header("DAY OF WEEK ANALYSIS (ET)")

    dow_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    daily = df.groupby("dow_name").agg(
        trades=("won", "count"),
        wins=("won", "sum"),
        losses=("lost", "sum"),
        avg_conf=("conf", "mean"),
        avg_edge=("edge", "mean"),
        avg_pnl=("pnl", "mean"),
    )
    daily["win_rate"] = (daily["wins"] / daily["trades"] * 100).round(1)
    daily = daily.reindex(dow_order)

    print(daily.to_string())

    best = daily["win_rate"].idxmax()
    worst = daily["win_rate"].idxmin()
    print(f"\n  🏆 Best day:  {best} ({daily.loc[best, 'win_rate']:.1f}%)")
    print(f"  💀 Worst day: {worst} ({daily.loc[worst, 'win_rate']:.1f}%)")

    # Plot
    fig, ax = plt.subplots(figsize=(10, 6))
    colors = ["#00cc44" if wr >= 65 else "#4488ff" if wr >= 60 else "#ffaa00" if wr >= 55 else "#ff4444"
              for wr in daily["win_rate"]]
    bars = ax.bar(daily.index, daily["win_rate"], color=colors, edgecolor="white", linewidth=0.5)
    ax.axhline(y=65, color="#00cc44", linestyle="--", alpha=0.5)
    ax.axhline(y=50, color="#888888", linestyle=":", alpha=0.5)

    for bar, wr, tc in zip(bars, daily["win_rate"], daily["trades"]):
        ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.5,
                f"{wr:.1f}%\n({tc})", ha="center", va="bottom", fontsize=9)

    ax.set_ylabel("Win Rate (%)")
    ax.set_title("Win Rate by Day of Week (ET)")
    plt.tight_layout()
    plt.savefig(os.path.join(output_dir, "dow_analysis.png"), dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  📊 Saved: {output_dir}/dow_analysis.png")

    return daily


def analyze_by_date(df: pd.DataFrame, output_dir: str):
    print_header("DAILY WIN RATE OVER TIME")

    daily = df.groupby("date").agg(
        trades=("won", "count"),
        wins=("won", "sum"),
    )
    daily["win_rate"] = (daily["wins"] / daily["trades"] * 100).round(1)

    # Rolling 3-day average
    daily["win_rate_3d"] = daily["win_rate"].rolling(3, min_periods=1).mean()

    print(f"  Best day:  {daily['win_rate'].idxmax()} ({daily['win_rate'].max():.1f}%)")
    print(f"  Worst day: {daily['win_rate'].idxmin()} ({daily['win_rate'].min():.1f}%)")
    print(f"  Avg daily: {daily['win_rate'].mean():.1f}%")
    print(f"  Std daily: {daily['win_rate'].std():.1f}%")

    # Days below 55%
    bad_days = daily[daily["win_rate"] < 55]
    print(f"\n  🔴 Days below 55%: {len(bad_days)}/{len(daily)}")
    for d in bad_days.index:
        wr = bad_days.loc[d, "win_rate"]
        tc = bad_days.loc[d, "trades"]
        day_name = pd.Timestamp(d).day_name()
        print(f"     {d} ({day_name}): {wr:.1f}% ({tc} trades)")

    fig, axes = plt.subplots(2, 1, figsize=(16, 10))

    dates = [pd.Timestamp(d) for d in daily.index]

    axes[0].plot(dates, daily["win_rate"], "o-", color="#4488ff", markersize=4, label="Daily WR")
    axes[0].plot(dates, daily["win_rate_3d"], "-", color="#ff8800", linewidth=2, label="3-day MA")
    axes[0].axhline(y=65, color="#00cc44", linestyle="--", alpha=0.5)
    axes[0].axhline(y=55, color="#ff4444", linestyle="--", alpha=0.5)
    axes[0].axhline(y=50, color="#888888", linestyle=":", alpha=0.5)
    axes[0].set_ylabel("Win Rate (%)")
    axes[0].set_title("Daily Win Rate Over Time")
    axes[0].legend()
    axes[0].xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
    axes[0].tick_params(axis="x", rotation=45)

    axes[1].bar(dates, daily["trades"], color="#4488ff", alpha=0.7)
    axes[1].set_ylabel("Trades")
    axes[1].set_title("Daily Trade Count")
    axes[1].xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
    axes[1].tick_params(axis="x", rotation=45)

    plt.tight_layout()
    plt.savefig(os.path.join(output_dir, "daily_timeseries.png"), dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  📊 Saved: {output_dir}/daily_timeseries.png")


def analyze_by_hour_dow_heatmap(df: pd.DataFrame, output_dir: str):
    print_header("HOUR × DAY OF WEEK HEATMAP")

    dow_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

    pivot_wr = df.pivot_table(
        values="won", index="hour_et", columns="dow_name",
        aggfunc="mean"
    ).reindex(columns=dow_order) * 100

    pivot_count = df.pivot_table(
        values="won", index="hour_et", columns="dow_name",
        aggfunc="count"
    ).reindex(columns=dow_order)

    fig, axes = plt.subplots(1, 2, figsize=(20, 10))

    sns.heatmap(pivot_wr, annot=True, fmt=".0f", cmap="RdYlGn", center=60,
                vmin=40, vmax=80, ax=axes[0], linewidths=0.5)
    axes[0].set_title("Win Rate (%) by Hour × Day of Week")
    axes[0].set_ylabel("Hour (ET)")

    sns.heatmap(pivot_count, annot=True, fmt=".0f", cmap="Blues",
                ax=axes[1], linewidths=0.5)
    axes[1].set_title("Trade Count by Hour × Day of Week")
    axes[1].set_ylabel("Hour (ET)")

    plt.tight_layout()
    plt.savefig(os.path.join(output_dir, "hour_dow_heatmap.png"), dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  📊 Saved: {output_dir}/hour_dow_heatmap.png")

    # Find the absolute best/worst combos
    print("\n  🏆 TOP 10 BEST (Hour×Day combos, ≥5 trades):")
    for h in pivot_wr.index:
        for d in pivot_wr.columns:
            wr = pivot_wr.loc[h, d]
            ct = pivot_count.loc[h, d] if pd.notna(pivot_count.loc[h, d]) else 0
            if pd.notna(wr) and ct >= 5:
                pass  # collected below

    combos = []
    for h in pivot_wr.index:
        for d in pivot_wr.columns:
            wr = pivot_wr.loc[h, d]
            ct = pivot_count.loc[h, d] if pd.notna(pivot_count.loc[h, d]) else 0
            if pd.notna(wr) and ct >= 5:
                combos.append((h, d, wr, int(ct)))

    combos.sort(key=lambda x: x[2], reverse=True)
    for h, d, wr, ct in combos[:10]:
        print(f"     {d:9s} {h:02d}:00 ET → {wr:.1f}% ({ct} trades)")

    print("\n  💀 TOP 10 WORST (Hour×Day combos, ≥5 trades):")
    for h, d, wr, ct in combos[-10:]:
        print(f"     {d:9s} {h:02d}:00 ET → {wr:.1f}% ({ct} trades)")


def analyze_regime(df: pd.DataFrame, output_dir: str):
    print_header("REGIME ANALYSIS")

    regime_stats = df.groupby("regime").agg(
        trades=("won", "count"),
        wins=("won", "sum"),
        avg_conf=("conf", "mean"),
        avg_edge=("edge", "mean"),
        avg_path_eff=("path_eff", "mean"),
        avg_autocorr=("autocorr", "mean"),
    )
    regime_stats["win_rate"] = (regime_stats["wins"] / regime_stats["trades"] * 100).round(1)

    print(regime_stats.to_string())


def analyze_volume_volatility(df: pd.DataFrame, kline_stats: pd.DataFrame, output_dir: str):
    if kline_stats.empty:
        return

    print_header("VOLUME / VOLATILITY / WHIPSAW ANALYSIS")

    # Merge kline stats with trades
    df_merged = df.merge(
        kline_stats,
        left_on=["date", "hour_utc"],
        right_on=["date", "hour_utc"],
        how="left",
        suffixes=("", "_kline"),
    )

    # Bin by volatility quartile
    df_merged["vol_quartile"] = pd.qcut(
        df_merged["volatility"].dropna(), 4, labels=["Low", "Med-Low", "Med-High", "High"]
    ).reindex(df_merged.index)

    vol_q = df_merged.dropna(subset=["vol_quartile"]).groupby("vol_quartile").agg(
        trades=("won", "count"),
        wins=("won", "sum"),
    )
    vol_q["win_rate"] = (vol_q["wins"] / vol_q["trades"] * 100).round(1)
    print("\n  Win Rate by Volatility Quartile:")
    print(f"  {vol_q.to_string()}")

    # Bin by whipsaw
    df_merged["whipsaw_quartile"] = pd.qcut(
        df_merged["whipsaw"].dropna(), 4, labels=["Low", "Med-Low", "Med-High", "High"]
    ).reindex(df_merged.index)

    ws_q = df_merged.dropna(subset=["whipsaw_quartile"]).groupby("whipsaw_quartile").agg(
        trades=("won", "count"),
        wins=("won", "sum"),
    )
    ws_q["win_rate"] = (ws_q["wins"] / ws_q["trades"] * 100).round(1)
    print("\n  Win Rate by Whipsaw Quartile:")
    print(f"  {ws_q.to_string()}")

    # Bin by volume
    df_merged["volume_quartile"] = pd.qcut(
        df_merged["volume"].dropna(), 4, labels=["Low", "Med-Low", "Med-High", "High"]
    ).reindex(df_merged.index)

    v_q = df_merged.dropna(subset=["volume_quartile"]).groupby("volume_quartile").agg(
        trades=("won", "count"),
        wins=("won", "sum"),
    )
    v_q["win_rate"] = (v_q["wins"] / v_q["trades"] * 100).round(1)
    print("\n  Win Rate by Volume Quartile:")
    print(f"  {v_q.to_string()}")

    # Bin by path efficiency
    df_merged["patheff_quartile"] = pd.qcut(
        df_merged["path_eff_kline"].dropna(), 4, labels=["Low", "Med-Low", "Med-High", "High"]
    ).reindex(df_merged.index) if "path_eff_kline" in df_merged.columns else None

    if df_merged["patheff_quartile"] is not None:
        pe_q = df_merged.dropna(subset=["patheff_quartile"]).groupby("patheff_quartile").agg(
            trades=("won", "count"),
            wins=("won", "sum"),
        )
        pe_q["win_rate"] = (pe_q["wins"] / pe_q["trades"] * 100).round(1)
        print("\n  Win Rate by Path Efficiency Quartile:")
        print(f"  {pe_q.to_string()}")

    # Plot
    fig, axes = plt.subplots(2, 2, figsize=(14, 10))

    for ax, (col, label) in zip(axes.flat, [
        ("vol_quartile", "Volatility"),
        ("whipsaw_quartile", "Whipsaw"),
        ("volume_quartile", "Volume"),
        ("patheff_quartile", "Path Efficiency"),
    ]):
        if col not in df_merged.columns or df_merged[col].isna().all():
            continue
        grp = df_merged.dropna(subset=[col]).groupby(col)["won"].mean() * 100
        grp.plot(kind="bar", ax=ax, color=["#ff4444", "#ffaa00", "#4488ff", "#00cc44"], edgecolor="white")
        ax.set_title(f"Win Rate by {label}")
        ax.set_ylabel("Win Rate (%)")
        ax.axhline(y=65, color="#00cc44", linestyle="--", alpha=0.5)
        ax.axhline(y=50, color="#888888", linestyle=":", alpha=0.5)
        ax.tick_params(axis="x", rotation=0)

    plt.tight_layout()
    plt.savefig(os.path.join(output_dir, "volume_volatility.png"), dpi=150, bbox_inches="tight")
    plt.close()
    print(f"\n  📊 Saved: {output_dir}/volume_volatility.png")

    return df_merged


def ml_analysis(df: pd.DataFrame, kline_stats: pd.DataFrame, output_dir: str):
    print_header("ML FEATURE IMPORTANCE + BLACKLIST RECOMMENDATION")

    # Build feature matrix
    features = df[["hour_et", "dow", "conf", "edge", "path_eff", "autocorr", "entry_secs_in"]].copy()

    # Encode regime
    regime_dummies = pd.get_dummies(df["regime"], prefix="regime")
    features = pd.concat([features, regime_dummies], axis=1)

    # Add cyclical hour encoding
    features["hour_sin"] = np.sin(2 * np.pi * features["hour_et"] / 24)
    features["hour_cos"] = np.cos(2 * np.pi * features["hour_et"] / 24)

    # Add cyclical dow encoding
    features["dow_sin"] = np.sin(2 * np.pi * features["dow"] / 7)
    features["dow_cos"] = np.cos(2 * np.pi * features["dow"] / 7)

    # Merge kline stats if available
    if not kline_stats.empty:
        merged = df[["date", "hour_utc"]].join(features)
        kline_feats = df[["date", "hour_utc"]].merge(
            kline_stats, on=["date", "hour_utc"], how="left"
        )[["volatility", "whipsaw", "volume", "ofi", "range_pct", "path_eff"]].rename(
            columns={"path_eff": "kline_path_eff"}
        )
        features = pd.concat([features, kline_feats], axis=1)

    target = df["won"].values

    # Drop NaN rows
    mask = features.notna().all(axis=1)
    X = features[mask].values
    y = target[mask]
    feature_names = features.columns.tolist()

    print(f"  Features: {len(feature_names)}")
    print(f"  Samples:  {len(X)} (dropped {len(target) - len(X)} with NaN)")

    # Scale
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)

    # ── Random Forest ──
    rf = RandomForestClassifier(n_estimators=200, max_depth=8, random_state=42, n_jobs=-1)
    cv = TimeSeriesSplit(n_splits=5)
    scores = cross_val_score(rf, X_scaled, y, cv=cv, scoring="accuracy")
    print(f"\n  🌲 Random Forest CV accuracy: {scores.mean():.3f} ± {scores.std():.3f}")

    rf.fit(X_scaled, y)

    # Feature importance (impurity-based)
    importances = rf.feature_importances_
    sorted_idx = np.argsort(importances)[::-1]

    print(f"\n  Feature Importance (top 15):")
    for i in sorted_idx[:15]:
        print(f"     {feature_names[i]:25s} {importances[i]:.4f}")

    # ── Gradient Boosting ──
    gb = GradientBoostingClassifier(n_estimators=200, max_depth=4, learning_rate=0.1, random_state=42)
    scores_gb = cross_val_score(gb, X_scaled, y, cv=cv, scoring="accuracy")
    print(f"\n  🚀 Gradient Boosting CV accuracy: {scores_gb.mean():.3f} ± {scores_gb.std():.3f}")

    gb.fit(X_scaled, y)
    gb_importances = gb.feature_importances_
    sorted_idx_gb = np.argsort(gb_importances)[::-1]

    print(f"\n  Feature Importance - GB (top 15):")
    for i in sorted_idx_gb[:15]:
        print(f"     {feature_names[i]:25s} {gb_importances[i]:.4f}")

    # ── Permutation Importance (more reliable) ──
    perm_imp = permutation_importance(gb, X_scaled, y, n_repeats=10, random_state=42, n_jobs=-1)
    sorted_perm = np.argsort(perm_imp.importances_mean)[::-1]

    print(f"\n  Permutation Importance - GB (top 15):")
    for i in sorted_perm[:15]:
        print(f"     {feature_names[i]:25s} {perm_imp.importances_mean[i]:.4f} ± {perm_imp.importances_std[i]:.4f}")

    # Plot feature importance
    fig, axes = plt.subplots(1, 2, figsize=(16, 8))

    top_n = min(15, len(feature_names))
    axes[0].barh(
        [feature_names[i] for i in sorted_idx[:top_n]][::-1],
        [importances[i] for i in sorted_idx[:top_n]][::-1],
        color="#4488ff",
    )
    axes[0].set_title("Random Forest Feature Importance")

    axes[1].barh(
        [feature_names[i] for i in sorted_idx_gb[:top_n]][::-1],
        [gb_importances[i] for i in sorted_idx_gb[:top_n]][::-1],
        color="#ff8800",
    )
    axes[1].set_title("Gradient Boosting Feature Importance")

    plt.tight_layout()
    plt.savefig(os.path.join(output_dir, "ml_feature_importance.png"), dpi=150, bbox_inches="tight")
    plt.close()
    print(f"\n  📊 Saved: {output_dir}/ml_feature_importance.png")

    # ── ML-based Blacklist Recommendation ──
    print_header("ML BLACKLIST RECOMMENDATION")

    # For each hour, predict win probability
    print("  Per-hour ML predicted win rate vs actual:")
    hour_analysis = []
    for h in range(24):
        mask_h = df["hour_et"] == h
        if mask_h.sum() < 5:
            continue

        X_h = features[mask_h & mask].values
        if len(X_h) == 0:
            continue

        X_h_scaled = scaler.transform(X_h)
        pred_proba = gb.predict_proba(X_h_scaled)[:, 1].mean()
        actual_wr = df.loc[mask_h, "won"].mean()
        trades = mask_h.sum()

        bl = "🔒" if h in CURRENT_BLACKLIST_ET else "  "
        status = "🔴" if actual_wr < 0.55 else "🟡" if actual_wr < 0.60 else "🟢"

        hour_analysis.append({
            "hour": h,
            "actual_wr": actual_wr,
            "predicted_wr": pred_proba,
            "trades": trades,
            "blacklisted": h in CURRENT_BLACKLIST_ET,
        })

        print(f"     {bl} {h:02d}:00 ET  actual={actual_wr*100:.1f}%  predicted={pred_proba*100:.1f}%  trades={trades:4d}  {status}")

    # Suggest new blacklist
    ha_df = pd.DataFrame(hour_analysis)
    new_blacklist = set(ha_df[
        (ha_df["actual_wr"] < 0.57) & (ha_df["trades"] >= 10)
    ]["hour"].tolist())

    added = new_blacklist - CURRENT_BLACKLIST_ET
    print(f"\n  📌 Current blacklist (ET): {sorted(CURRENT_BLACKLIST_ET)}")
    print(f"  📌 Recommended blacklist:  {sorted(new_blacklist)}")
    if added:
        print(f"  ⚡ ADD these hours:        {sorted(added)}")
    else:
        print(f"  ✅ No additional hours to blacklist")

    return gb, feature_names


def analyze_streaks(df: pd.DataFrame, output_dir: str):
    print_header("WIN/LOSS STREAK ANALYSIS")

    # Compute streaks
    streaks = []
    current_streak = 0
    current_type = None

    for _, row in df.iterrows():
        if row["correct"] == current_type:
            current_streak += 1
        else:
            if current_type is not None:
                streaks.append((current_type, current_streak))
            current_type = row["correct"]
            current_streak = 1
    if current_type is not None:
        streaks.append((current_type, current_streak))

    win_streaks = [s for t, s in streaks if t]
    loss_streaks = [s for t, s in streaks if not t]

    print(f"  Max win streak:  {max(win_streaks) if win_streaks else 0}")
    print(f"  Max loss streak: {max(loss_streaks) if loss_streaks else 0}")
    print(f"  Avg win streak:  {np.mean(win_streaks):.1f}")
    print(f"  Avg loss streak: {np.mean(loss_streaks):.1f}")

    # Distribution
    fig, axes = plt.subplots(1, 2, figsize=(12, 5))
    axes[0].hist(win_streaks, bins=range(1, max(win_streaks)+2), color="#00cc44", edgecolor="white", alpha=0.8)
    axes[0].set_title("Win Streak Distribution")
    axes[0].set_xlabel("Streak Length")
    axes[0].set_ylabel("Frequency")

    axes[1].hist(loss_streaks, bins=range(1, max(loss_streaks)+2), color="#ff4444", edgecolor="white", alpha=0.8)
    axes[1].set_title("Loss Streak Distribution")
    axes[1].set_xlabel("Streak Length")
    axes[1].set_ylabel("Frequency")

    plt.tight_layout()
    plt.savefig(os.path.join(output_dir, "streak_analysis.png"), dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  📊 Saved: {output_dir}/streak_analysis.png")


def print_summary(df: pd.DataFrame):
    print_header("OVERALL SUMMARY")

    total = len(df)
    wins = df["won"].sum()
    losses = df["lost"].sum()
    wr = wins / total * 100

    print(f"  Total trades:  {total}")
    print(f"  Wins:          {wins} ({wr:.1f}%)")
    print(f"  Losses:        {losses} ({100-wr:.1f}%)")
    print(f"  Avg confidence:{df['conf'].mean():.3f}")
    print(f"  Avg edge:      {df['edge'].mean():.3f}")
    print(f"  Avg entry secs:{df['entry_secs_in'].mean():.0f}")


# ──────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────
def main():
    args = parse_args()
    os.makedirs(args.output, exist_ok=True)

    print("=" * 70)
    print("  BACKTEST ANALYSIS SUITE")
    print("=" * 70)

    # Load data
    print(f"\n📂 Loading trade log: {args.trade_log}")
    df = load_trades(args.trade_log)
    print(f"   {len(df)} trades loaded")
    print(f"   Date range: {df['date'].min()} → {df['date'].max()}")

    print(f"\n📂 Loading kline stats from: {args.data_dir}")
    kline_stats = load_kline_stats(args.data_dir)
    if not kline_stats.empty:
        print(f"   {len(kline_stats)} hourly stat records")

    # Run all analyses
    print_summary(df)
    analyze_by_hour(df, args.output)
    analyze_by_dow(df, args.output)
    analyze_by_date(df, args.output)
    analyze_by_hour_dow_heatmap(df, args.output)
    analyze_regime(df, args.output)
    analyze_streaks(df, args.output)

    df_merged = analyze_volume_volatility(df, kline_stats, args.output)
    ml_analysis(df, kline_stats, args.output)

    print_header("DONE")
    print(f"  All charts saved to: {args.output}/")
    print(f"  Files:")
    for f in sorted(os.listdir(args.output)):
        if f.endswith(".png"):
            print(f"    📊 {f}")


if __name__ == "__main__":
    main()
