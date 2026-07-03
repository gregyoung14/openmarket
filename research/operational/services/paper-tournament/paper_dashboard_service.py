#!/usr/bin/env python3
from __future__ import annotations
"""
Paper-Trade Tournament Dashboard Backend

Serves tournament.html and provides JSON APIs for the paper-trade dashboard.
Reads paper executor CSV log files and computes per-strategy metrics.

Usage:
    python3 paper_dashboard_service.py --csv-dir /var/lib/polymarket/paper_logs/ --port 8007
"""

import argparse
import csv
import glob
import json
import os
import re
import time
from datetime import datetime, timezone
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# ── Strategy color map ──────────────────────────────────────────────────────

STRATEGY_COLORS = {
    "v14_baseline":      "#ffa726",
    "v14.1_no_volgate":  "#42a5f5",
    "v15_brier_cb":      "#66bb6a",
    "v14_relaxed_conf":  "#ab47bc",
    "v14_wide_confirm":  "#ef5350",
    "v14_tight_regime":  "#26c6da",
    "v14_canary_early_highcap": "#7cb342",
    "v16_calibrated_shadow": "#3949ab",
    "v16_calibrated_active_paper": "#00897b",
    "v15_aggressive":    "#ffd93d",
    "v14_low_whipsaw":   "#ec407a",
    "custom_1":          "#8d6e63",
    "custom_2":          "#78909c",
}
DEFAULT_COLORS = list(STRATEGY_COLORS.values())


def parse_float(value, default=0.0):
    try:
        if value is None:
            return default
        if isinstance(value, str) and not value.strip():
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


# ── CSV parsing with mtime-based cache ──────────────────────────────────────

class PaperDataCache:
    def __init__(
        self,
        csv_dir: str,
        default_version: str = "all",
        split_gap_hours: float = 24.0,
    ):
        self.csv_dir = csv_dir
        self._file_mtimes: dict[str, float] = {}
        self._trades: list[dict] = []
        self._compare_by_version: dict[str, list[dict]] = {
            "all": [],
            "v1": [],
            "v2": [],
        }
        self._cutover_ts: int | None = None
        self._split_mode: str = "none"
        self._split_gap_hours = split_gap_hours
        self._default_version = self._normalize_version(default_version, fallback="all")
        self._start_time = time.time()

    @staticmethod
    def _normalize_version(version: str | None, fallback: str = "all") -> str:
        normalized = (version or fallback or "all").lower()
        if normalized in ("v1", "v2", "all"):
            return normalized
        return fallback

    @staticmethod
    def _to_epoch_seconds(ts: int | None) -> int | None:
        if ts is None:
            return None
        return ts // 1000 if ts > 10**12 else ts

    @staticmethod
    def _infer_file_version(filepath: str) -> str | None:
        filename = os.path.basename(filepath).lower()
        m = re.search(r"(^|[_\-.])v([12])([_\-.]|$)", filename)
        if not m:
            return None
        return f"v{m.group(2)}"

    def _gap_threshold_units(self, timestamps: list[int]) -> int:
        use_ms = bool(timestamps) and max(timestamps) > 10**12
        unit_mult = 1000 if use_ms else 1
        return max(1, int(self._split_gap_hours * 3600 * unit_mult))

    def _needs_reload(self) -> bool:
        pattern = os.path.join(self.csv_dir, "paper_log_*.csv")
        files = glob.glob(pattern)
        for f in files:
            try:
                mtime = os.path.getmtime(f)
            except OSError:
                continue
            if f not in self._file_mtimes or self._file_mtimes[f] != mtime:
                return True
        # Check if a file was removed
        if set(self._file_mtimes.keys()) != set(files):
            return True
        return False

    def _reload(self):
        pattern = os.path.join(self.csv_dir, "paper_log_*.csv")
        files = glob.glob(pattern)
        self._file_mtimes = {}
        all_trades = []

        for filepath in files:
            try:
                mtime = os.path.getmtime(filepath)
                self._file_mtimes[filepath] = mtime
            except OSError:
                continue

            file_version = self._infer_file_version(filepath)

            try:
                with open(filepath, "r") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        trade = self._parse_row(row, file_version=file_version)
                        if trade:
                            all_trades.append(trade)
            except (OSError, csv.Error):
                continue

        # Deduplicate by newest timestamp so rotated legacy files cannot
        # overwrite the current row for the same strategy/market.
        seen = {}
        for t in all_trades:
            key = (t["strategy"], t["slug"])
            existing = seen.get(key)
            if existing is None:
                seen[key] = t
                continue

            existing_ts = existing.get("timestamp", 0)
            current_ts = t.get("timestamp", 0)
            if current_ts > existing_ts:
                seen[key] = t
                continue

            if current_ts == existing_ts and existing.get("result") == "PENDING" and t.get("result") != "PENDING":
                seen[key] = t
        self._trades = sorted(seen.values(), key=lambda t: t.get("timestamp", 0))

        self._assign_versions(self._trades)

        # Compute comparison stats per version
        self._compare_by_version["all"] = self._compute_compare(self._trades)
        self._compare_by_version["v1"] = self._compute_compare(
            [t for t in self._trades if t.get("version") == "v1"]
        )
        self._compare_by_version["v2"] = self._compute_compare(
            [t for t in self._trades if t.get("version") == "v2"]
        )

    def _parse_row(self, row: dict, file_version: str | None = None) -> dict | None:
        try:
            ts = int(row.get("timestamp", 0))
            original_pnl = parse_float(row.get("pnl", 0))
            original_bankroll = parse_float(row.get("bankroll", 0))
            pnl = parse_float(row.get("audit_corrected_pnl"), original_pnl)
            bankroll = parse_float(row.get("audit_corrected_bankroll"), original_bankroll)
            confidence = parse_float(row.get("confidence", 0))
            edge = parse_float(row.get("edge", 0))
            entry_ask = parse_float(row.get("entry_ask", 0))
            brier_str = row.get("brier_score", "")
            brier = float(brier_str) if brier_str and brier_str.strip() else None
            cb_paused = row.get("cb_paused", "false").lower() == "true"
            audit_entry_stake = parse_float(row.get("audit_entry_stake"), None)
            audit_pnl_delta = parse_float(row.get("audit_pnl_delta"), 0.0)
            ranking_score = parse_float(row.get("ranking_score"), None)
            raw_model_prob_up = parse_float(row.get("raw_model_prob_up"), None)
            calibrated_prob_up = parse_float(row.get("calibrated_prob_up"), None)
            selected_side_prob = parse_float(row.get("selected_side_prob"), None)
            ev_up = parse_float(row.get("ev_up"), None)
            ev_down = parse_float(row.get("ev_down"), None)

            return {
                "timestamp": ts,
                "strategy": row.get("strategy", "unknown"),
                "slug": row.get("slug", ""),
                "direction": row.get("direction", ""),
                "confidence": confidence,
                "edge": edge,
                "regime": row.get("regime", "unknown"),
                "entry_ask": entry_ask,
                "result": row.get("result", "PENDING"),
                "pnl": pnl,
                "bankroll": bankroll,
                "original_pnl": original_pnl,
                "original_bankroll": original_bankroll,
                "audit_entry_stake": audit_entry_stake,
                "audit_pnl_delta": audit_pnl_delta,
                "is_audit_corrected": row.get("audit_corrected_pnl", "").strip() != "",
                "brier_score": brier,
                "cb_paused": cb_paused,
                "scoring_mode": row.get("scoring_mode", "") or None,
                "ranking_basis": row.get("ranking_basis", "") or None,
                "ranking_score": ranking_score,
                "raw_model_prob_up": raw_model_prob_up,
                "calibrated_prob_up": calibrated_prob_up,
                "selected_side_prob": selected_side_prob,
                "ev_up": ev_up,
                "ev_down": ev_down,
                "artifact_version": row.get("artifact_version", "") or None,
                "version": file_version,
            }
        except (ValueError, KeyError):
            return None

    def _assign_versions(self, trades: list[dict]) -> None:
        self._cutover_ts = None
        self._split_mode = "none"
        if not trades:
            return

        labeled_v1 = [t for t in trades if t.get("version") == "v1"]
        labeled_v2 = [t for t in trades if t.get("version") == "v2"]

        cutover_ts = None
        if labeled_v1 and labeled_v2:
            self._split_mode = "filename"
            cutover_ts = min(t["timestamp"] for t in labeled_v2)
        elif labeled_v2:
            self._split_mode = "filename"
            cutover_ts = min(t["timestamp"] for t in labeled_v2)
        else:
            timestamps = sorted(t["timestamp"] for t in trades if t.get("timestamp"))
            if len(timestamps) >= 2:
                threshold = self._gap_threshold_units(timestamps)
                largest_gap = 0
                largest_gap_end = None
                prev = timestamps[0]
                for curr in timestamps[1:]:
                    gap = curr - prev
                    if gap > largest_gap:
                        largest_gap = gap
                        largest_gap_end = curr
                    prev = curr
                if largest_gap_end is not None and largest_gap >= threshold:
                    cutover_ts = largest_gap_end
                    self._split_mode = "gap"

        self._cutover_ts = cutover_ts

        for t in trades:
            if t.get("version") in ("v1", "v2"):
                continue
            if cutover_ts is None:
                t["version"] = "v2"
            else:
                t["version"] = "v1" if t.get("timestamp", 0) < cutover_ts else "v2"

    def _compute_compare(self, trades_subset: list[dict]) -> list[dict]:
        """Compute per-strategy leaderboard stats."""
        strategies: dict[str, list[dict]] = {}
        for t in trades_subset:
            s = t["strategy"]
            if s not in strategies:
                strategies[s] = []
            strategies[s].append(t)

        hours_elapsed = (time.time() - self._start_time) / 3600.0
        if hours_elapsed < 0.01:
            hours_elapsed = 0.01

        result = []
        color_idx = 0
        for name, trades in sorted(strategies.items()):
            trades = sorted(trades, key=lambda t: t.get("timestamp", 0))
            resolved = [t for t in trades if t["result"] in ("WIN", "LOSS")]
            wins = [t for t in resolved if t["result"] == "WIN"]
            losses = [t for t in resolved if t["result"] == "LOSS"]
            total = len(resolved)
            win_count = len(wins)

            resolved_ts = [self._to_epoch_seconds(t.get("timestamp")) for t in resolved]
            resolved_ts = [ts for ts in resolved_ts if ts is not None]
            if len(resolved_ts) >= 2:
                active_hours = max((max(resolved_ts) - min(resolved_ts)) / 3600.0, 0.01)
            else:
                active_hours = hours_elapsed

            total_pnl = sum(t["pnl"] for t in resolved)
            win_rate = (win_count / total * 100) if total > 0 else 0.0
            trades_per_day = total / (active_hours / 24.0)

            avg_win = sum(t["pnl"] for t in wins) / len(wins) if wins else 0.0
            avg_loss = sum(t["pnl"] for t in losses) / len(losses) if losses else 0.0
            total_wins_dollar = sum(t["pnl"] for t in wins)
            total_losses_dollar = abs(sum(t["pnl"] for t in losses))
            profit_factor = (total_wins_dollar / total_losses_dollar) if total_losses_dollar > 0 else None

            # Max drawdown
            peak = 0.0
            max_dd = 0.0
            running_pnl = 0.0
            for t in resolved:
                running_pnl += t["pnl"]
                if running_pnl > peak:
                    peak = running_pnl
                dd = peak - running_pnl
                if dd > max_dd:
                    max_dd = dd
            if trades:
                first_trade = trades[0]
                initial_bankroll = first_trade.get("bankroll", 0.0) - first_trade.get("pnl", 0.0)
            else:
                initial_bankroll = 0.0
            max_dd_pct = (max_dd / initial_bankroll * 100) if initial_bankroll > 0 else 0.0

            # Brier score (average of non-null values)
            brier_vals = [t["brier_score"] for t in resolved if t["brier_score"] is not None]
            brier_avg = sum(brier_vals) / len(brier_vals) if brier_vals else None
            cb_pauses = sum(1 for t in trades if t.get("cb_paused"))

            # ROI
            roi_pct = (total_pnl / initial_bankroll * 100) if initial_bankroll > 0 else 0.0

            color = STRATEGY_COLORS.get(name, DEFAULT_COLORS[color_idx % len(DEFAULT_COLORS)])
            color_idx += 1

            result.append({
                "strategy": name,
                "color": color,
                "trades": total,
                "wins": win_count,
                "win_rate": round(win_rate, 1),
                "total_pnl": round(total_pnl, 2),
                "roi_pct": round(roi_pct, 1),
                "trades_per_day": round(trades_per_day, 1),
                "max_drawdown_pct": round(max_dd_pct, 1),
                "profit_factor": round(profit_factor, 2) if profit_factor is not None else None,
                "avg_win": round(avg_win, 2),
                "avg_loss": round(avg_loss, 2),
                "brier_avg": round(brier_avg, 3) if brier_avg is not None else None,
                "cb_pauses": cb_pauses,
            })

        return result

    def get_trades(self, version: str | None = None) -> list[dict]:
        if self._needs_reload():
            self._reload()
        selected_version = self._normalize_version(version, fallback=self._default_version)
        if selected_version == "all":
            return self._trades
        return [t for t in self._trades if t.get("version") == selected_version]

    def get_compare(self, version: str | None = None) -> list[dict]:
        if self._needs_reload():
            self._reload()
        selected_version = self._normalize_version(version, fallback=self._default_version)
        return self._compare_by_version.get(selected_version, [])

    def get_health(self, version: str | None = None) -> dict:
        selected_version = self._normalize_version(version, fallback=self._default_version)
        trades_all = self.get_trades("all")
        trades = self.get_trades(selected_version)
        compare = self.get_compare(selected_version)
        hours_elapsed = (time.time() - self._start_time) / 3600.0

        v1_count = len([t for t in trades_all if t.get("version") == "v1" and t["result"] in ("WIN", "LOSS")])
        v2_count = len([t for t in trades_all if t.get("version") == "v2" and t["result"] in ("WIN", "LOSS")])

        cutover_iso = None
        cutover_seconds = self._to_epoch_seconds(self._cutover_ts)
        if cutover_seconds is not None:
            cutover_iso = datetime.fromtimestamp(cutover_seconds, tz=timezone.utc).isoformat()

        return {
            "status": "ok",
            "version": selected_version,
            "strategies": len(compare),
            "total_trades": len([t for t in trades if t["result"] in ("WIN", "LOSS")]),
            "version_counts": {
                "v1": v1_count,
                "v2": v2_count,
            },
            "split_mode": self._split_mode,
            "cutover_timestamp": self._cutover_ts,
            "cutover_iso": cutover_iso,
            "running_since": datetime.fromtimestamp(self._start_time, tz=timezone.utc).isoformat(),
            "hours_elapsed": round(hours_elapsed, 1),
            "csv_dir": self.csv_dir,
        }


# ── HTTP Handler ────────────────────────────────────────────────────────────

class PaperDashboardHandler(SimpleHTTPRequestHandler):
    cache: PaperDataCache
    html_path: str

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        query = parse_qs(parsed.query)
        version = query.get("version", [None])[0]

        # Strip /paper prefix if present (nginx proxy)
        if path.startswith("/paper"):
            path = path[6:] or ""

        if path == "" or path == "/index.html" or path == "/tournament.html":
            self._serve_html()
        elif path == "/health":
            self._serve_json(self.cache.get_health(version))
        elif path == "/ledger":
            self._serve_json(self.cache.get_trades(version))
        elif path == "/compare":
            self._serve_json(self.cache.get_compare(version))
        else:
            self.send_error(404, "Not Found")

    def _serve_html(self):
        try:
            with open(self.html_path, "rb") as f:
                content = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        except FileNotFoundError:
            self.send_error(404, "tournament.html not found")

    def _serve_json(self, data):
        body = json.dumps(data, separators=(",", ":")).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        # Suppress per-request logging noise
        pass


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Paper-Trade Tournament Dashboard")
    parser.add_argument("--csv-dir", default=str(Path(__file__).resolve().parent / "logs" / ""),
                        help="Directory containing paper_log_*.csv files")
    parser.add_argument("--port", type=int, default=8008,
                        help="HTTP server port")
    parser.add_argument("--default-version", choices=["all", "v1", "v2"], default="all",
                        help="Default dataset version when query param is omitted")
    parser.add_argument("--split-gap-hours", type=float, default=24.0,
                        help="Minimum timestamp gap (hours) used to infer v1/v2 split when logs are mixed")
    args = parser.parse_args()

    csv_dir = os.path.abspath(args.csv_dir)
    if not os.path.isdir(csv_dir):
        os.makedirs(csv_dir, exist_ok=True)
        print(f"Created CSV directory: {csv_dir}")

    script_dir = Path(__file__).parent
    html_path = str(script_dir / "tournament.html")

    cache = PaperDataCache(
        csv_dir,
        default_version=args.default_version,
        split_gap_hours=args.split_gap_hours,
    )
    PaperDashboardHandler.cache = cache
    PaperDashboardHandler.html_path = html_path

    server = HTTPServer(("0.0.0.0", args.port), PaperDashboardHandler)
    print(f"Paper-Trade Tournament Dashboard")
    print(f"  Port: {args.port}")
    print(f"  CSV dir: {csv_dir}")
    print(f"  Default version: {args.default_version}")
    print(f"  Dashboard: http://localhost:{args.port}/")
    print(f"  API: /paper/health, /paper/ledger, /paper/compare")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()


if __name__ == "__main__":
    main()
