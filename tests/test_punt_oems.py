#!/usr/bin/env python3
"""Unit tests for Punt OEMS."""
import unittest
from unittest.mock import patch, MagicMock
import tempfile
import json
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import punt_oems


class TestPuntOEMS(unittest.TestCase):
    def setUp(self):
        punt_oems.CONFIG = {
            "live": False,
            "keypairPath": None,
            "jupiterApiKey": None,
            "maxLivePlanUsd": 50000.0,
            "instruments": punt_oems.DEFAULT_INSTRUMENTS,
        }
        punt_oems.MARKET.clear()
        punt_oems.PLANS.clear()
        punt_oems.FILLS.clear()

    def test_helpers(self):
        self.assertEqual(punt_oems.clamp(5, 0, 10), 5)
        self.assertEqual(punt_oems.clamp(-5, 0, 10), 0)
        self.assertEqual(punt_oems.clamp(15, 0, 10), 10)
        self.assertEqual(punt_oems.num("123.45"), 123.45)
        self.assertIsNone(punt_oems.num("invalid"))
        self.assertEqual(punt_oems.num(None, default=0.0), 0.0)

    def test_max_slice_from_curve(self):
        curve = [
            {"usd": 250, "impactBps": 10},
            {"usd": 500, "impactBps": 25},
            {"usd": 1000, "impactBps": 45},
            {"usd": 2500, "impactBps": 85},
        ]
        # At 50 bps limit, max slice is 1000
        self.assertEqual(punt_oems.max_slice_from_curve(curve, 50), 1000)
        # At 20 bps limit, max slice is 250
        self.assertEqual(punt_oems.max_slice_from_curve(curve, 20), 250)
        # At 5 bps limit, returns 0.0
        self.assertEqual(punt_oems.max_slice_from_curve(curve, 5), 0.0)

    def test_child_notional(self):
        plan = {
            "remainingUsd": 10000.0,
            "sliceUsd": 2000.0,
            "symbol": "ZCAT",
            "maxParticipationPct": 2.0,
        }
        # When 5m volume is 50,000, 2% is 1,000, which caps the 2,000 target slice
        punt_oems.MARKET["ZCAT"] = {"volume5m": 50000.0}
        self.assertEqual(punt_oems.child_notional(plan), 1000.0)

        # When 5m volume is large, sliceUsd is used
        punt_oems.MARKET["ZCAT"] = {"volume5m": 500000.0}
        self.assertEqual(punt_oems.child_notional(plan), 2000.0)

    def test_guard_conditions(self):
        punt_oems.MARKET["ZCAT"] = {
            "liquidity": 300000.0,
            "sourceDivergencePct": 1.2,
            "price": 1.00,
            "contract": {"transferFee": {"bps": 300}},
        }
        plan = {
            "symbol": "ZCAT",
            "side": "BUY",
            "minLiquidityUsd": 250000.0,
            "maxSourceDivergencePct": 5.0,
            "arrivalPrice": 1.00,
            "maxAdverseMovePct": 2.5,
        }

        # Healthy market
        ok, reason = punt_oems.guard(plan)
        self.assertTrue(ok)
        self.assertEqual(reason, "ok")

        # Insufficient liquidity
        punt_oems.MARKET["ZCAT"]["liquidity"] = 200000.0
        ok, reason = punt_oems.guard(plan)
        self.assertFalse(ok)
        self.assertIn("liquidity", reason)
        punt_oems.MARKET["ZCAT"]["liquidity"] = 300000.0

        # High cross-source divergence
        punt_oems.MARKET["ZCAT"]["sourceDivergencePct"] = 6.5
        ok, reason = punt_oems.guard(plan)
        self.assertFalse(ok)
        self.assertIn("source divergence", reason)
        punt_oems.MARKET["ZCAT"]["sourceDivergencePct"] = 1.2

        # Adverse price runaway on BUY (price jumped up 4%)
        punt_oems.MARKET["ZCAT"]["price"] = 1.04
        ok, reason = punt_oems.guard(plan)
        self.assertFalse(ok)
        self.assertIn("adverse move", reason)
        punt_oems.MARKET["ZCAT"]["price"] = 1.00

        # Token-2022 transfer fee altered from 300 bps to 400 bps
        punt_oems.MARKET["ZCAT"]["contract"]["transferFee"]["bps"] = 400
        ok, reason = punt_oems.guard(plan)
        self.assertFalse(ok)
        self.assertIn("transfer fee changed", reason)

    def test_fit_child(self):
        plan = {
            "symbol": "ZCAT",
            "side": "BUY",
            "minSliceUsd": 250.0,
            "maxImpactBps": 50.0,
        }

        def mock_quote(symbol, side, usd):
            # If USD >= 1000, impact is 70 bps (> 50 bps limit)
            # If USD < 1000, impact is 30 bps
            impact = 0.70 if usd >= 1000 else 0.30
            return {"priceImpactPct": impact, "route": "Orca"}

        with patch("punt_oems.quote_usd", side_effect=mock_quote):
            fitted_usd, q = punt_oems.fit_child(plan, 2000.0)
            self.assertEqual(fitted_usd, 500.0)
            self.assertEqual(q["priceImpactPct"], 0.30)

    def test_plan_restart_recovery(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            temp_path = Path(f.name)

        try:
            original_state_path = punt_oems.STATE_PATH
            punt_oems.STATE_PATH = temp_path

            # Create a working plan
            plan = {
                "id": "test-plan-1",
                "status": "WORKING",
                "notionalUsd": 5000.0,
                "remainingUsd": 5000.0,
                "filledUsd": 0.0,
            }
            punt_oems.PLANS["test-plan-1"] = plan
            punt_oems.save_state()

            # Clear memory
            punt_oems.PLANS.clear()

            # Reload state
            punt_oems.load_state()
            self.assertIn("test-plan-1", punt_oems.PLANS)
            # Working plans must reload as PAUSED for safety
            self.assertEqual(punt_oems.PLANS["test-plan-1"]["status"], "PAUSED")
            self.assertEqual(punt_oems.PLANS["test-plan-1"]["error"], "paused after restart")
        finally:
            punt_oems.STATE_PATH = original_state_path
            if temp_path.exists():
                temp_path.unlink()


if __name__ == "__main__":
    unittest.main()
