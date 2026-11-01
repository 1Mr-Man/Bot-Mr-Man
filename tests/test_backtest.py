from datetime import datetime, timedelta, timezone
import unittest

from fxbot.backtest import run_signal_backtest
from fxbot.models import Bar, Side
from fxbot.paper import PaperConfig, run_paper_backtest
from fxbot.risk import RiskLimits


class BuyOnce:
    def __init__(self) -> None:
        self.called = False

    def on_bar(self, bar: Bar) -> Side:
        if not self.called:
            self.called = True
            return Side.LONG
        return Side.FLAT


class AlwaysLong:
    def on_bar(self, bar: Bar) -> Side:
        return Side.LONG


class BacktestTests(unittest.TestCase):
    def test_signal_fills_at_next_bar_open(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        bars = [
            Bar("EURUSD", start, 1.10, 1.11, 1.09, 1.105),
            Bar("EURUSD", start + timedelta(hours=1), 1.12, 1.13, 1.11, 1.125),
        ]

        changes = run_signal_backtest(bars, BuyOnce())

        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].price, 1.12)
        self.assertEqual(changes[0].timestamp, bars[1].timestamp)
        self.assertEqual(changes[0].to_side, Side.LONG)

    def test_rejects_mixed_symbols(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        bars = [
            Bar("EURUSD", start, 1.10, 1.11, 1.09, 1.105),
            Bar("GBPUSD", start + timedelta(hours=1), 1.30, 1.31, 1.29, 1.305),
        ]

        with self.assertRaisesRegex(ValueError, "only one symbol"):
            run_signal_backtest(bars, BuyOnce())

    def test_rejects_out_of_order_bars(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        bars = [
            Bar("EURUSD", start, 1.10, 1.11, 1.09, 1.105),
            Bar("EURUSD", start - timedelta(hours=1), 1.10, 1.11, 1.09, 1.105),
        ]

        with self.assertRaisesRegex(ValueError, "strictly increasing"):
            run_signal_backtest(bars, BuyOnce())

    def test_paper_entry_uses_next_bar_open_and_marks_to_market(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        bars = [
            Bar("EURUSD", start, 1.1000, 1.1010, 1.0990, 1.1000),
            Bar("EURUSD", start + timedelta(hours=1), 1.1010, 1.1030, 1.1005, 1.1020),
        ]
        config = PaperConfig(
            initial_equity=10_000,
            stop_distance=0.01,
            spread=0,
            risk_limits=RiskLimits(risk_per_trade=0.01),
        )

        result = run_paper_backtest(bars, BuyOnce(), config)

        self.assertEqual(result.open_position.entry_time, bars[1].timestamp)
        self.assertEqual(result.open_position.entry_price, bars[1].open)
        self.assertAlmostEqual(result.final_equity, 10_010)

    def test_stop_closes_position_and_records_net_pnl(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        bars = [
            Bar("EURUSD", start, 1.1000, 1.1010, 1.0990, 1.1000),
            Bar("EURUSD", start + timedelta(hours=1), 1.1000, 1.1010, 1.0980, 1.0990),
        ]
        config = PaperConfig(
            initial_equity=10_000,
            stop_distance=0.001,
            spread=0,
            risk_limits=RiskLimits(risk_per_trade=0.01),
        )

        result = run_paper_backtest(bars, BuyOnce(), config)

        self.assertIsNone(result.open_position)
        self.assertEqual(len(result.trades), 1)
        self.assertEqual(result.trades[0].exit_reason, "stop")
        self.assertAlmostEqual(result.trades[0].net_pnl, -100)
        self.assertAlmostEqual(result.final_balance, 9_900)

    def test_daily_loss_limit_blocks_new_trade_after_stop(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        bars = [
            Bar("EURUSD", start, 1.1000, 1.1010, 1.0990, 1.1000),
            Bar("EURUSD", start + timedelta(hours=1), 1.1000, 1.1010, 1.0980, 1.0990),
            Bar("EURUSD", start + timedelta(hours=2), 1.1000, 1.1010, 1.0990, 1.1000),
        ]
        config = PaperConfig(
            initial_equity=10_000,
            stop_distance=0.001,
            spread=0,
            risk_limits=RiskLimits(risk_per_trade=0.01, max_daily_loss=0.005),
        )

        result = run_paper_backtest(bars, AlwaysLong(), config)

        self.assertEqual(len(result.trades), 1)
        self.assertEqual(result.lockout_dates, (start.date(),))
        self.assertIsNone(result.open_position)

    def test_spread_is_charged_on_entry_and_exit(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        bars = [
            Bar("EURUSD", start, 1.1000, 1.1010, 1.0990, 1.1000),
            Bar("EURUSD", start + timedelta(hours=1), 1.1000, 1.1010, 1.0990, 1.1000),
            Bar("EURUSD", start + timedelta(hours=2), 1.1000, 1.1010, 1.0990, 1.1000),
        ]
        config = PaperConfig(
            initial_equity=10_000,
            stop_distance=0.01,
            spread=0.0002,
            risk_limits=RiskLimits(risk_per_trade=0.01),
        )

        result = run_paper_backtest(bars, BuyOnce(), config)

        self.assertEqual(len(result.trades), 1)
        self.assertAlmostEqual(result.trades[0].entry_price, 1.1001)
        self.assertAlmostEqual(result.trades[0].exit_price, 1.0999)
        self.assertAlmostEqual(result.trades[0].net_pnl, -2)
