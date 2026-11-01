from datetime import datetime, timedelta, timezone
import unittest

from fxbot.models import Bar, Side
from fxbot.consensus import (
    consensus_side,
    count_strategy_votes,
    describe_scan_result,
    make_all_strategies,
    scan_latest_signals,
)
from fxbot.strategies import DonchianBreakout
from examples.mt5_demo_signal import latest_entry_signal


def bar(hour: int, close: float) -> Bar:
    return Bar(
        symbol="EURUSD",
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(hours=hour),
        open=close,
        high=close,
        low=close,
        close=close,
    )


class LatestEntrySignalTests(unittest.TestCase):
    def test_only_emits_transition_on_latest_completed_bar(self) -> None:
        strategy = DonchianBreakout(lookback=2)
        bars = [bar(0, 1.0), bar(1, 1.0), bar(2, 1.2)]

        candidate = latest_entry_signal(bars, strategy)

        self.assertIsNotNone(candidate)
        self.assertEqual(candidate[0], Side.LONG)
        self.assertEqual(candidate[1], bars[-1])

    def test_does_not_repeat_a_prior_entry_signal(self) -> None:
        strategy = DonchianBreakout(lookback=2)
        bars = [bar(0, 1.0), bar(1, 1.0), bar(2, 1.2), bar(3, 1.1)]

        self.assertIsNone(latest_entry_signal(bars, strategy))

    def test_returns_none_for_empty_history(self) -> None:
        self.assertIsNone(latest_entry_signal([], DonchianBreakout(lookback=2)))

    def test_all_strategy_scan_returns_five_results(self) -> None:
        results = scan_latest_signals(
            [bar(index, 1.0 + index * 0.001) for index in range(80)],
            make_all_strategies(20),
        )

        self.assertEqual(len(results), 5)
        self.assertEqual(
            [result[0] for result in results],
            [
                "SMA 10/30 trend",
                "Donchian 20 breakout",
                "EMA 12/26 trend",
                "MACD 12/26/9 trend",
                "RSI 14 mean reversion",
            ],
        )

    def test_scan_status_describes_new_hold_and_exit(self) -> None:
        self.assertEqual(describe_scan_result(Side.FLAT, Side.LONG), "NEW LONG")
        self.assertEqual(describe_scan_result(Side.LONG, Side.LONG), "HOLD LONG")
        self.assertEqual(describe_scan_result(Side.SHORT, Side.FLAT), "EXIT to FLAT")

    def test_consensus_requires_three_matching_strategy_states(self) -> None:
        results = [
            ("one", Side.FLAT, Side.LONG),
            ("two", Side.FLAT, Side.LONG),
            ("three", Side.FLAT, Side.LONG),
            ("four", Side.FLAT, Side.SHORT),
            ("five", Side.FLAT, Side.FLAT),
        ]

        self.assertEqual(count_strategy_votes(results), (3, 1))
        self.assertEqual(consensus_side(3, 1), Side.LONG)
        self.assertEqual(consensus_side(2, 2), Side.FLAT)
        self.assertEqual(consensus_side(1, 4), Side.SHORT)
