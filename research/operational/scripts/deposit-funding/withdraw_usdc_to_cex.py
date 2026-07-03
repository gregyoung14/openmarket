#!/usr/bin/env python3
"""Generic entry point for withdrawing USDC from a Polymarket Polygon wallet."""

import runpy
from pathlib import Path


runpy.run_path(str(Path(__file__).with_name("withdraw_to_coinbase.py")), run_name="__main__")
