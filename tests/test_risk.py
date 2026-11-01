import unittest

from fxbot.risk import RiskLimits


class RiskLimitsTests(unittest.TestCase):
    def test_position_size_respects_risk_budget(self) -> None:
        limits = RiskLimits(risk_per_trade=0.01)

        units = limits.position_units(
            equity=10_000,
            stop_distance=0.01,
            value_per_price_unit=1,
        )

        self.assertEqual(units, 10_000)
        self.assertAlmostEqual(units * 0.01, 100)

    def test_rejects_nonpositive_sizing_inputs(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be positive"):
            RiskLimits().position_units(
                equity=10_000,
                stop_distance=0,
                value_per_price_unit=1,
            )

    def test_daily_loss_lockout_threshold(self) -> None:
        limits = RiskLimits(max_daily_loss=0.03)

        self.assertFalse(
            limits.daily_loss_limit_reached(starting_equity=10_000, realized_pnl=-299)
        )
        self.assertTrue(
            limits.daily_loss_limit_reached(starting_equity=10_000, realized_pnl=-300)
        )
