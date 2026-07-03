#!/usr/bin/env python3
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "redeem_positions_service.py"
spec = importlib.util.spec_from_file_location("redeem_positions_service", MODULE_PATH)
svc = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = svc
spec.loader.exec_module(svc)


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class CostBasisFallbackTests(unittest.TestCase):
    def test_activity_fallback_prefers_api_price(self):
        service = object.__new__(svc.RedeemPositionsService)
        service.account = SimpleNamespace(address="0xabc")

        # price (0.2794) should win over usdc/size (9.25/32.76449 ~= 0.2823)
        batches = [
            [
                {
                    "type": "TRADE",
                    "side": "BUY",
                    "conditionId": "0xcond",
                    "size": 32.76449,
                    "usdcSize": 9.25,
                    "price": 0.2794561933534743,
                    "timestamp": 1773176105,
                }
            ],
            [],
        ]

        with patch.object(svc.requests, "get", side_effect=[_FakeResponse(b) for b in batches]):
            out = service._load_activity_trade_fallbacks()

        self.assertIn("0xcond", out)
        self.assertAlmostEqual(out["0xcond"]["avg_price"], 0.2794561933534743, places=12)
        self.assertAlmostEqual(out["0xcond"]["initial_value"], 9.25, places=12)

    def test_entry_log_parser_handles_ansi(self):
        ansi_line_1 = (
            "\x1b[2m2026-03-10T20:55:01.140689Z\x1b[0m INFO execution_engine: "
            "ENTRY signal received market=Some(\"btc-updown-15m-1773175500\")\n"
        )
        ansi_line_2 = (
            "\x1b[2m2026-03-10T20:55:01.140764Z\x1b[0m INFO execution_engine::order_executor: "
            "Placing BUY order token_id=\"abc\" price=0.37 size=25\n"
        )

        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "execution-engine.log"
            p.write_text(ansi_line_1 + ansi_line_2, encoding="utf-8")

            service = object.__new__(svc.RedeemPositionsService)
            with patch.object(svc, "EXECUTION_ENGINE_LOG_PATH", str(p)):
                out = service._load_entry_price_fallbacks()

        self.assertIn("btc-updown-15m-1773175500", out)
        self.assertAlmostEqual(out["btc-updown-15m-1773175500"]["price"], 0.37, places=12)
        self.assertAlmostEqual(out["btc-updown-15m-1773175500"]["size"], 25.0, places=12)


if __name__ == "__main__":
    unittest.main(verbosity=2)
