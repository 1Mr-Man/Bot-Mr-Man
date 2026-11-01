from datetime import datetime, timezone
import unittest

from fxbot.models import Bar, Side
from fxbot.strategies import (
    DonchianBreakout,
    EmaTrendFollowing,
    MacdTrendFollowing,
    RsiMeanReversion,
    SmaTrendFollowing,
)


def make_bar(close: float) -> Bar:
    return Bar(
        symbol="EURUSD",
        timestamp=datetime.now(timezone.utc),
        open=close,
        high=close,
        low=close,
        close=close,
    )


class BarTests(unittest.TestCase):
    def test_rejects_naive_timestamp(self) -> None:
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            Bar("EURUSD", datetime(2026, 1, 1), 1.1, 1.1, 1.1, 1.1)

    def test_rejects_invalid_ohlc(self) -> None:
        with self.assertRaisesRegex(ValueError, "high"):
            Bar("EURUSD", datetime.now(timezone.utc), 1.1, 1.0, 0.9, 1.05)


class SmaTrendFollowingTests(unittest.TestCase):
    def test_returns_flat_until_slow_window_is_ready(self) -> None:
        strategy = SmaTrendFollowing(fast_period=2, slow_period=3)

        self.assertEqual(strategy.on_bar(make_bar(1.0)), Side.FLAT)
        self.assertEqual(strategy.on_bar(make_bar(1.1)), Side.FLAT)

    def test_tracks_up_and_down_trends(self) -> None:
        strategy = SmaTrendFollowing(fast_period=2, slow_period=3)
        values = [1.0, 1.0, 1.2, 1.3, 0.8, 0.7]

        signals = [strategy.on_bar(make_bar(value)) for value in values]

        self.assertEqual(signals[2], Side.LONG)
        self.assertEqual(signals[-1], Side.SHORT)

    def test_rejects_invalid_periods(self) -> None:
        with self.assertRaisesRegex(ValueError, "fast_period"):
            SmaTrendFollowing(fast_period=5, slow_period=5)


class DonchianBreakoutTests(unittest.TestCase):
    def test_waits_for_channel_then_enters_on_breakout(self) -> None:
        strategy = DonchianBreakout(lookback=2)
        values = [1.0, 1.0, 1.2]

        signals = [strategy.on_bar(make_bar(value)) for value in values]

        self.assertEqual(signals, [Side.FLAT, Side.FLAT, Side.LONG])

    def test_holds_position_until_opposite_breakout(self) -> None:
        strategy = DonchianBreakout(lookback=2)
        bars = [
            make_bar(1.0),
            make_bar(1.0),
            make_bar(1.2),
            make_bar(1.1),
            make_bar(0.9),
        ]

        signals = [strategy.on_bar(bar) for bar in bars]

        self.assertEqual(signals, [Side.FLAT, Side.FLAT, Side.LONG, Side.LONG, Side.SHORT])

    def test_rejects_invalid_lookback(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least 2"):
            DonchianBreakout(lookback=1)


class AdditionalStrategyTests(unittest.TestCase):
    def test_ema_trend_waits_for_warmup_then_tracks_direction(self) -> None:
        strategy = EmaTrendFollowing(fast_period=1, slow_period=3)
        signals = [strategy.on_bar(make_bar(value)) for value in (1.0, 1.1, 1.2, 0.8)]

        self.assertEqual(signals[:2], [Side.FLAT, Side.FLAT])
        self.assertEqual(signals[2], Side.LONG)
        self.assertEqual(signals[3], Side.SHORT)

    def test_macd_waits_for_signal_ema_then_reports_direction(self) -> None:
        strategy = MacdTrendFollowing(fast_period=1, slow_period=2, signal_period=2)
        signals = [
            strategy.on_bar(make_bar(value)) for value in (1.0, 1.1, 1.2, 1.4)
        ]

        self.assertEqual(signals[:2], [Side.FLAT, Side.FLAT])
        self.assertEqual(signals[2], Side.FLAT)
        self.assertEqual(signals[3], Side.LONG)

    def test_rsi_enters_on_oversold_and_exits_at_midpoint(self) -> None:
        strategy = RsiMeanReversion(period=3)
        values = (1.3, 1.2, 1.1, 1.0, 1.1, 1.2)
        signals = [strategy.on_bar(make_bar(value)) for value in values]

        self.assertEqual(signals[3], Side.LONG)
        self.assertEqual(signals[-1], Side.FLAT)

    def test_rsi_rejects_inconsistent_thresholds(self) -> None:
        with self.assertRaisesRegex(ValueError, "RSI levels"):
            RsiMeanReversion(oversold=60, exit_level=50, overbought=70)
