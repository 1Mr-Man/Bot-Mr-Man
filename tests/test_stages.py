import unittest

from fxbot.stages import build_stage_targets, stage_reached_by_simulation


class StageTargetTests(unittest.TestCase):
    def test_twenty_dollar_targets_match_literal_formula(self) -> None:
        targets = build_stage_targets(20, stages=30, growth_rate=0.3)

        self.assertAlmostEqual(targets[9].target_balance, 275.7169836980, places=8)
        self.assertAlmostEqual(targets[29].target_balance, 52399.9128729990, places=8)

    def test_stage_reached_uses_simulated_balance(self) -> None:
        self.assertEqual(
            stage_reached_by_simulation(20, 100, stages=30, growth_rate=0.3),
            6,
        )
        self.assertEqual(
            stage_reached_by_simulation(20, 20, stages=30, growth_rate=0.3),
            0,
        )

    def test_rejects_invalid_inputs(self) -> None:
        with self.assertRaises(ValueError):
            build_stage_targets(0)
        with self.assertRaises(ValueError):
            build_stage_targets(10, stages=0)
        with self.assertRaises(ValueError):
            build_stage_targets(10, growth_rate=0)


if __name__ == "__main__":
    unittest.main()
