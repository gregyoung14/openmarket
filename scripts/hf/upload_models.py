#!/usr/bin/env python3
"""Upload OpenMarket model artifacts to Hugging Face Models.

Usage:
    .venv/bin/python scripts/hf/upload_models.py
    .venv/bin/python scripts/hf/upload_models.py --source-dir /path/to/ml_artifacts
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from huggingface_hub import HfApi


DEFAULT_REPO = "gregyoung14/openmarket-models"
DEFAULT_SOURCE = Path("/Users/greg/Software/polymarket-btc-scraper/data/ml_artifacts")
DEFAULT_STAGING = Path("models/hf_staging/v0.1")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-id", default=DEFAULT_REPO)
    parser.add_argument("--source-dir", default=DEFAULT_SOURCE, type=Path)
    parser.add_argument("--staging-dir", default=DEFAULT_STAGING, type=Path)
    parser.add_argument("--version", default="v0.1")
    parser.add_argument("--dataset-version", default="v0.3-unified")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def stage_artifacts(args: argparse.Namespace) -> Path:
    src = args.source_dir
    if not src.exists():
        raise SystemExit(f"source dir does not exist: {src}")

    stage = args.staging_dir
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True, exist_ok=True)

    latest = src / "latest_binary_model.json"
    if latest.exists():
        shutil.copy2(latest, stage / "binary_outcome_model.json")

    for path in sorted(src.glob("binary_outcome_metrics_*.json")):
        shutil.copy2(path, stage / path.name)

    card_src = Path("models/hf/README.md")
    card = stage / "README.md"
    if card_src.exists():
        text = card_src.read_text(encoding="utf-8")
        text = text.replace("No public pretrained model has been released yet.", (
            f"Published calibrated binary-outcome scorer ({args.version}) trained on "
            f"OpenMarket step3 features. Paired dataset version: `{args.dataset_version}`."
        ))
        card.write_text(text, encoding="utf-8")

    manifest = {
        "model_version": args.version,
        "dataset_version": args.dataset_version,
        "artifact": "binary_outcome_model.json",
        "staged_at": datetime.now(timezone.utc).isoformat(),
        "source_dir": str(src),
    }
    (stage / "model_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return stage


def main() -> int:
    args = parse_args()
    stage = stage_artifacts(args)
    files = [p for p in stage.rglob("*") if p.is_file()]
    print(f"staging {len(files)} files from {args.source_dir} -> {stage}")

    if args.dry_run:
        for path in files:
            print(f"  {path.relative_to(stage)}")
        return 0

    api = HfApi(token=os.environ.get("HF_TOKEN"))
    try:
        api.upload_folder(
            repo_id=args.repo_id,
            repo_type="model",
            folder_path=str(stage),
            path_in_repo=args.version,
            commit_message=f"upload OpenMarket models {args.version}",
            multi_commits=True,
            multi_commits_verbose=True,
        )
    except TypeError:
        api.upload_folder(
            repo_id=args.repo_id,
            repo_type="model",
            folder_path=str(stage),
            path_in_repo=args.version,
            commit_message=f"upload OpenMarket models {args.version}",
        )

    readme = stage / "README.md"
    if readme.exists():
        api.upload_file(
            path_or_fileobj=str(readme),
            path_in_repo="README.md",
            repo_id=args.repo_id,
            repo_type="model",
            commit_message=f"update model card for {args.version}",
        )

    print(f"uploaded -> {args.repo_id}/{args.version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())