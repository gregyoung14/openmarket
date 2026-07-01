import argparse
import math
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from scipy.optimize import differential_evolution


METRIC_PATTERNS = {
    "trades": re.compile(r"\|\s*Total Trades\s*\|\s*(\d+)"),
    "win_rate": re.compile(r"\|\s*Win Rate\s*\|\s*([\d\.]+)%"),
    "roi": re.compile(r"\|\s*Total ROI\s*\|\s*([\-\d\.]+)%"),
    "alpha": re.compile(r"\|\s*Strategy Alpha[^\d\-]*([\-\d\.]+)%"),
}


@dataclass
class TrialResult:
    trades: int
    win_rate: float
    roi: float
    alpha: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Optimize signal weights plus policy thresholds for a Rust backtester."
    )
    parser.add_argument(
        "--binary",
        default="./target/release/v15_brier_calibration",
        help="Path to the compiled Rust backtester binary.",
    )
    parser.add_argument(
        "--db-path",
        default="../../data/polymarket_btc_data.db",
        help="SQLite database path passed to the backtester.",
    )
    parser.add_argument(
        "--build",
        action="store_true",
        help="Run cargo build --release before optimization.",
    )
    parser.add_argument(
        "--maxiter",
        type=int,
        default=12,
        help="Differential evolution iteration count.",
    )
    parser.add_argument(
        "--popsize",
        type=int,
        default=6,
        help="Population size multiplier for differential evolution.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Parallel worker count.",
    )
    parser.add_argument(
        "--min-trades",
        type=int,
        default=75,
        help="Trials with fewer trades than this are heavily penalized.",
    )
    parser.add_argument(
        "--alpha-weight",
        type=float,
        default=0.15,
        help="Optional alpha bonus added to ROI in the objective.",
    )
    parser.add_argument(
        "--quiet-build",
        action="store_true",
        help="Use cargo build --release -q if --build is set.",
    )
    return parser.parse_args()


def extract_metric(text: str, key: str, default: float = 0.0) -> float:
    match = METRIC_PATTERNS[key].search(text)
    if not match:
        return default
    return float(match.group(1))


def parse_trial_result(stdout: str) -> TrialResult:
    return TrialResult(
        trades=int(extract_metric(stdout, "trades", 0.0)),
        win_rate=extract_metric(stdout, "win_rate", 0.0),
        roi=extract_metric(stdout, "roi", 0.0),
        alpha=extract_metric(stdout, "alpha", 0.0),
    )


def build_binary(args: argparse.Namespace) -> None:
    if not args.build:
        return

    build_cmd = ["cargo", "build", "--release"]
    if args.quiet_build:
        build_cmd.append("-q")

    print("Building Rust binary...")
    subprocess.run(build_cmd, check=True)


def make_objective(args: argparse.Namespace):
    binary_path = Path(args.binary)
    db_path = args.db_path
    min_trades = args.min_trades
    alpha_weight = args.alpha_weight

    def run_backtest(params):
        (
            w_drift,
            w_ofi,
            w_score,
            w_whipsaw,
            min_confidence,
            min_edge,
            max_entry_price,
        ) = params

        env = os.environ.copy()
        env["W_DRIFT"] = f"{w_drift:.8f}"
        env["W_OFI_ACCEL"] = f"{w_ofi:.8f}"
        env["W_SCOREBOARD"] = f"{w_score:.8f}"
        env["WHIPSAW_WEIGHT"] = f"{w_whipsaw:.8f}"

        with tempfile.NamedTemporaryFile(
            prefix="policy_opt_",
            suffix=".csv",
            delete=False,
        ) as handle:
            output_csv = handle.name

        cmd = [
            str(binary_path),
            "--db-path",
            db_path,
            "--min-confidence",
            f"{min_confidence:.8f}",
            "--min-edge",
            f"{min_edge:.8f}",
            "--max-entry-price",
            f"{max_entry_price:.8f}",
            "--output-csv",
            output_csv,
        ]

        try:
            result = subprocess.run(
                cmd,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                return float("inf")

            metrics = parse_trial_result(result.stdout)
        finally:
            try:
                os.remove(output_csv)
            except OSError:
                pass

        if metrics.trades < min_trades:
            penalty = 1_000_000.0 + (min_trades - metrics.trades) * 1_000.0
            print(
                "Params "
                f"[{w_drift:6.3f}, {w_ofi:6.3f}, {w_score:6.3f}, {w_whipsaw:6.3f}, "
                f"{min_confidence:5.3f}, {min_edge:5.3f}, {max_entry_price:5.3f}] -> "
                f"Trades: {metrics.trades:3d}, penalized for low sample"
            )
            return penalty

        objective = -(metrics.roi + alpha_weight * metrics.alpha)
        print(
            "Params "
            f"[{w_drift:6.3f}, {w_ofi:6.3f}, {w_score:6.3f}, {w_whipsaw:6.3f}, "
            f"{min_confidence:5.3f}, {min_edge:5.3f}, {max_entry_price:5.3f}] -> "
            f"Trades: {metrics.trades:3d}, WR: {metrics.win_rate:5.1f}%, "
            f"Alpha: {metrics.alpha:6.2f}%, ROI: {metrics.roi:8.1f}%, Loss: {objective:9.3f}"
        )
        return objective

    return run_backtest


def main() -> None:
    args = parse_args()
    build_binary(args)

    print("Starting differential evolution over weights + policy thresholds...")
    print(f"Binary: {args.binary}")
    print(f"DB: {args.db_path}")
    print(f"Minimum trades required: {args.min_trades}")
    print(f"Alpha bonus weight: {args.alpha_weight}")

    bounds = [
        (0.0, 5.0),
        (0.0, 5.0),
        (0.0, 5.0),
        (-3.0, 0.0),
        (0.52, 0.75),
        (0.03, 0.15),
        (0.40, 0.70),
    ]

    result = differential_evolution(
        make_objective(args),
        bounds,
        maxiter=args.maxiter,
        popsize=args.popsize,
        workers=args.workers,
        disp=True,
        polish=True,
        updating="deferred" if args.workers != 1 else "immediate",
    )

    if not result.success and math.isfinite(result.fun):
        print("Optimizer finished without a formal success flag, but returned a finite result.")

    print("\n" + "=" * 60)
    print("OPTIMIZATION FINISHED")
    print("=" * 60)
    print(result)
    print("\nBest Parameters Found:")
    print(f"W_DRIFT         = {result.x[0]:.4f}")
    print(f"W_OFI_ACCEL     = {result.x[1]:.4f}")
    print(f"W_SCOREBOARD    = {result.x[2]:.4f}")
    print(f"WHIPSAW_WEIGHT  = {result.x[3]:.4f}")
    print(f"MIN_CONFIDENCE  = {result.x[4]:.4f}")
    print(f"MIN_EDGE        = {result.x[5]:.4f}")
    print(f"MAX_ENTRY_PRICE = {result.x[6]:.4f}")


if __name__ == "__main__":
    main()