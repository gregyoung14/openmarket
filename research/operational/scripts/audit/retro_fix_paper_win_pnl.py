#!/usr/bin/env python3
"""
Retroactively correct paper-trade win PnL using entry stake from executor logs.

The current paper executor logs a PENDING row at entry time and later appends a
resolved WIN/LOSS row. WIN rows are mispriced because settlement recomputes the
bet amount and ignores the contract entry price. This script reconstructs the
executed entry stake from the executor logs, then recalculates WIN PnL using a
share-based payout model while leaving LOSS rows unchanged.

Outputs:
- One corrected CSV per strategy with audit columns appended
- One summary CSV with original vs corrected bankroll/PnL totals
"""

from __future__ import annotations

import argparse
import csv
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
ENTRY_RE = re.compile(
    r'Entry signal processed.*?slug=.*?"(?P<slug>btc-updown-15m-\d+)".*?position_size=.*?"\$(?P<stake>[0-9]+(?:\.[0-9]+)?)".*?bankroll_before=.*?"\$(?P<bankroll>[0-9]+(?:\.[0-9]+)?)"'
)

TWOPLACES = Decimal("0.01")
ZERO = Decimal("0")
ONE = Decimal("1")


def to_decimal(value: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid decimal value: {value!r}") from exc


def round_money(value: Decimal) -> Decimal:
    return value.quantize(TWOPLACES, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class EntryStake:
    slug: str
    stake: Decimal
    bankroll_before: Decimal


@dataclass
class StrategySummary:
    strategy: str
    settled: int = 0
    wins: int = 0
    losses: int = 0
    pending: int = 0
    initial_bankroll: Decimal = ZERO
    original_total_pnl: Decimal = ZERO
    corrected_total_pnl: Decimal = ZERO
    original_final_bankroll: Decimal = ZERO
    corrected_final_bankroll: Decimal = ZERO
    total_win_delta: Decimal = ZERO
    missing_entry_stakes: int = 0


def parse_executor_log(log_path: Path) -> dict[str, EntryStake]:
    stakes: dict[str, EntryStake] = {}
    for raw_line in log_path.read_text(errors="ignore").splitlines():
        line = ANSI_RE.sub("", raw_line)
        match = ENTRY_RE.search(line)
        if not match:
            continue
        slug = match.group("slug")
        stakes[slug] = EntryStake(
            slug=slug,
            stake=to_decimal(match.group("stake")),
            bankroll_before=to_decimal(match.group("bankroll")),
        )
    return stakes


def compute_corrected_win_pnl(
    stake: Decimal,
    entry_ask: Decimal,
    fee_rate: Decimal,
    settlement_price: Decimal,
) -> Decimal:
    if entry_ask <= ZERO:
        raise ValueError(f"entry_ask must be positive, got {entry_ask}")

    shares = stake / entry_ask
    gross_exit_value = shares * settlement_price
    entry_fee = stake * fee_rate
    exit_fee = gross_exit_value * fee_rate
    corrected_pnl = gross_exit_value - stake - entry_fee - exit_fee
    return round_money(corrected_pnl)


def infer_strategy_name(csv_path: Path) -> str:
    name = csv_path.stem
    if name.startswith("paper_log_"):
        return name[len("paper_log_") :]
    return name


def build_output_row(
    row: dict[str, str],
    stake: Decimal | None,
    corrected_pnl: Decimal,
    corrected_bankroll: Decimal,
    pnl_delta: Decimal,
) -> dict[str, str]:
    out = dict(row)
    out["audit_entry_stake"] = f"{stake:.2f}" if stake is not None else ""
    out["audit_corrected_pnl"] = f"{corrected_pnl:.2f}"
    out["audit_pnl_delta"] = f"{pnl_delta:.2f}"
    out["audit_corrected_bankroll"] = f"{corrected_bankroll:.2f}"
    return out


def process_csv(
    csv_path: Path,
    stake_map: dict[str, EntryStake],
    out_dir: Path,
    fee_rate: Decimal,
    settlement_price: Decimal,
) -> StrategySummary:
    strategy = infer_strategy_name(csv_path)
    summary = StrategySummary(strategy=strategy)

    with csv_path.open("r", newline="") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])

    if not rows:
        raise ValueError(f"CSV is empty: {csv_path}")

    initial_bankroll = to_decimal(rows[0].get("bankroll", "0"))
    summary.initial_bankroll = initial_bankroll
    corrected_bankroll = initial_bankroll

    output_rows: list[dict[str, str]] = []

    for row in rows:
        result = row.get("result", "")
        slug = row.get("slug", "")
        original_pnl = to_decimal(row.get("pnl", "0"))
        corrected_pnl = original_pnl
        pnl_delta = ZERO
        stake: Decimal | None = None

        if result == "PENDING":
            summary.pending += 1
        elif result == "LOSS":
            summary.settled += 1
            summary.losses += 1
            summary.original_total_pnl += original_pnl
            summary.corrected_total_pnl += corrected_pnl
            corrected_bankroll += corrected_pnl
        elif result == "WIN":
            summary.settled += 1
            summary.wins += 1
            summary.original_total_pnl += original_pnl

            entry = stake_map.get(slug)
            if entry is None:
                summary.missing_entry_stakes += 1
            else:
                stake = entry.stake
                entry_ask = to_decimal(row.get("entry_ask", "0"))
                corrected_pnl = compute_corrected_win_pnl(
                    stake=stake,
                    entry_ask=entry_ask,
                    fee_rate=fee_rate,
                    settlement_price=settlement_price,
                )
                pnl_delta = corrected_pnl - original_pnl
                summary.total_win_delta += pnl_delta

            summary.corrected_total_pnl += corrected_pnl
            corrected_bankroll += corrected_pnl
        else:
            summary.corrected_total_pnl += corrected_pnl

        if result not in ("WIN", "LOSS"):
            snapshot_bankroll = corrected_bankroll
        else:
            snapshot_bankroll = corrected_bankroll

        output_rows.append(
            build_output_row(
                row=row,
                stake=stake,
                corrected_pnl=corrected_pnl,
                corrected_bankroll=snapshot_bankroll,
                pnl_delta=pnl_delta,
            )
        )

    summary.original_final_bankroll = initial_bankroll + summary.original_total_pnl
    summary.corrected_final_bankroll = initial_bankroll + summary.corrected_total_pnl

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{csv_path.stem}.audit_fixed.csv"
    output_fieldnames = fieldnames + [
        "audit_entry_stake",
        "audit_corrected_pnl",
        "audit_pnl_delta",
        "audit_corrected_bankroll",
    ]
    with out_path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=output_fieldnames)
        writer.writeheader()
        writer.writerows(output_rows)

    return summary


def write_summary(out_dir: Path, summaries: list[StrategySummary]) -> Path:
    out_path = out_dir / "paper_win_pnl_audit_summary.csv"
    with out_path.open("w", newline="") as fh:
        fieldnames = [
            "strategy",
            "settled",
            "wins",
            "losses",
            "pending",
            "initial_bankroll",
            "original_total_pnl",
            "corrected_total_pnl",
            "total_win_delta",
            "original_final_bankroll",
            "corrected_final_bankroll",
            "missing_entry_stakes",
        ]
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for item in summaries:
            writer.writerow(
                {
                    "strategy": item.strategy,
                    "settled": item.settled,
                    "wins": item.wins,
                    "losses": item.losses,
                    "pending": item.pending,
                    "initial_bankroll": f"{item.initial_bankroll:.2f}",
                    "original_total_pnl": f"{item.original_total_pnl:.2f}",
                    "corrected_total_pnl": f"{item.corrected_total_pnl:.2f}",
                    "total_win_delta": f"{item.total_win_delta:.2f}",
                    "original_final_bankroll": f"{item.original_final_bankroll:.2f}",
                    "corrected_final_bankroll": f"{item.corrected_final_bankroll:.2f}",
                    "missing_entry_stakes": item.missing_entry_stakes,
                }
            )
    return out_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--csv-dir",
        default="/var/lib/polymarket/paper_logs",
        help="Directory containing paper_log_*.csv files",
    )
    parser.add_argument(
        "--log-dir",
        default="/var/lib/polymarket/paper_logs",
        help="Directory containing executor_*.log files",
    )
    parser.add_argument(
        "--out-dir",
        default="/var/lib/polymarket/paper_logs/audit_fixed",
        help="Directory for corrected audit outputs",
    )
    parser.add_argument(
        "--fee-rate",
        default="0.01",
        help="Per-side fee rate used for the retroactive correction",
    )
    parser.add_argument(
        "--settlement-price",
        default="1.0",
        help="Settlement price for winning shares",
    )
    args = parser.parse_args()

    csv_dir = Path(args.csv_dir)
    log_dir = Path(args.log_dir)
    out_dir = Path(args.out_dir)
    fee_rate = to_decimal(args.fee_rate)
    settlement_price = to_decimal(args.settlement_price)

    summaries: list[StrategySummary] = []

    for csv_path in sorted(csv_dir.glob("paper_log_*.csv")):
        strategy = infer_strategy_name(csv_path)
        log_path = log_dir / f"executor_{strategy}.log"
        if not log_path.exists():
            raise FileNotFoundError(f"missing executor log for {strategy}: {log_path}")
        stake_map = parse_executor_log(log_path)
        summaries.append(
            process_csv(
                csv_path=csv_path,
                stake_map=stake_map,
                out_dir=out_dir,
                fee_rate=fee_rate,
                settlement_price=settlement_price,
            )
        )

    summary_path = write_summary(out_dir, summaries)

    print(f"wrote_summary={summary_path}")
    for item in summaries:
        print(
            "strategy={strategy} original_final={orig:.2f} corrected_final={corr:.2f} "
            "win_delta={delta:.2f} missing_entry_stakes={missing}".format(
                strategy=item.strategy,
                orig=item.original_final_bankroll,
                corr=item.corrected_final_bankroll,
                delta=item.total_win_delta,
                missing=item.missing_entry_stakes,
            )
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())