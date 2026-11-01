from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from fxbot.analytics import build_run_card
from fxbot.models import Bar, Side
from fxbot.paper import (
    EquityPoint,
    OpenPosition,
    PaperConfig,
    PaperResult,
    PaperTrade,
)
from fxbot.risk import RiskLimits


class RunCardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.bars = [
            Bar("EURUSD", self.start + timedelta(hours=index), 1.1, 1.2, 1.0, 1.1)
            for index in range(4)
        ]
        self.config = PaperConfig(
            initial_equity=1_000,
            stop_distance=0.01,
            spread=0.0002,
            slippage=0.0001,
            commission_per_unit_per_side=0.00001,
            risk_limits=RiskLimits(risk_per_trade=0.01, max_daily_loss=0.03),
        )
        self.trades = (
            PaperTrade(
                "EURUSD", Side.LONG, 1, self.start, 1.1,
                self.start + timedelta(hours=1), 1.2, "signal", 100, 0, 100,
            ),
            PaperTrade(
                "EURUSD", Side.SHORT, 1, self.start + timedelta(hours=1), 1.2,
                self.start + timedelta(hours=2), 1.15, "signal", 50, 0, -50,
            ),
            PaperTrade(
                "EURUSD", Side.LONG, 1, self.start + timedelta(hours=2), 1.15,
                self.start + timedelta(hours=3), 1.15, "signal", 0, 0, 0,
            ),
        )
        self.result = PaperResult(
            initial_equity=1_000,
            final_balance=1_100,
            final_equity=1_100,
            trades=self.trades,
            equity_curve=(
                EquityPoint(self.start, 1_000, 1_000),
                EquityPoint(self.start + timedelta(hours=1), 1_000, 1_200),
                EquityPoint(self.start + timedelta(hours=2), 1_000, 900),
                EquityPoint(self.start + timedelta(hours=3), 1_100, 1_100),
            ),
            open_position=None,
            lockout_dates=(),
        )

    def test_reports_metrics_and_cost_assumptions(self) -> None:
        card = build_run_card(
            self.result,
            self.bars,
            self.config,
            strategy_name="test strategy",
        )

        self.assertAlmostEqual(card.metrics.total_return_pct, 10)
        self.assertAlmostEqual(card.metrics.max_drawdown_pct, 25)
        self.assertEqual(card.metrics.closed_trade_count, 3)
        self.assertAlmostEqual(card.metrics.win_rate_pct, 100 / 3)
        self.assertAlmostEqual(card.metrics.average_net_trade_pnl, 50 / 3)
        self.assertEqual(card.metrics.profit_factor, 2)
        self.assertEqual(card.spread, self.config.spread)
        self.assertEqual(card.symbol, "EURUSD")
        self.assertEqual(card.bar_count, 4)

    def test_undefined_metrics_are_null_and_open_position_is_disclosed(self) -> None:
        result = PaperResult(
            initial_equity=1_000,
            final_balance=1_000,
            final_equity=1_000,
            trades=(),
            equity_curve=(),
            open_position=OpenPosition(
                "EURUSD", Side.LONG, 100, self.start, 1.1, 1.09, 0,
            ),
            lockout_dates=(),
        )

        card = build_run_card(
            result,
            self.bars[:1],
            self.config,
            strategy_name="test strategy",
        )

        self.assertIsNone(card.metrics.win_rate_pct)
        self.assertIsNone(card.metrics.average_net_trade_pnl)
        self.assertIsNone(card.metrics.profit_factor)
        self.assertTrue(card.metrics.has_open_position)
        self.assertEqual(card.open_position.side, "LONG")
        self.assertEqual(card.open_position.symbol, "EURUSD")
        self.assertTrue(any("open position remains" in warning for warning in card.warnings))
        self.assertTrue(any("No trades were closed" in warning for warning in card.warnings))

    def test_serializes_nulls_and_writes_json_run_card(self) -> None:
        card = build_run_card(
            self.result,
            self.bars,
            self.config,
            strategy_name="test strategy",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "run-card.json"
            card.write_json(output)
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["metrics"]["profit_factor"], 2)
        self.assertEqual(payload["strategy"], "test strategy")
        self.assertIsNone(payload["open_position"])

    def test_rejects_empty_strategy_name_and_unsorted_bars(self) -> None:
        with self.assertRaisesRegex(ValueError, "strategy_name"):
            build_run_card(self.result, self.bars, self.config, strategy_name=" ")
        with self.assertRaisesRegex(ValueError, "strictly increasing"):
            build_run_card(
                self.result,
                [self.bars[1], self.bars[0]],
                self.config,
                strategy_name="test strategy",
            )
