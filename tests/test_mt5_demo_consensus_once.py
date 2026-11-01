from datetime import datetime, timedelta, timezone
import unittest

from examples.mt5_demo_consensus_once import evaluate_entry
from fxbot.models import Bar, Side


def _bars(now: datetime, *, timeframe_minutes: int, count: int = 100) -> list[Bar]:
    result = []
    for index in range(count):
        close = 2.0 - index * 0.001
        result.append(
            Bar(
                symbol="GBPUSD",
                timestamp=now
                - timedelta(minutes=timeframe_minutes * (count - index)),
                open=close,
                high=close + 0.0001,
                low=close - 0.0001,
                close=close,
            )
        )
    return result


class ConsensusDemoOnceTests(unittest.TestCase):
    def test_entry_requires_matching_lower_and_higher_timeframe_votes(self) -> None:
        now = datetime.now(timezone.utc)
        bars = _bars(now, timeframe_minutes=5)
        confirmation = _bars(now, timeframe_minutes=15)

        side, signal_id = evaluate_entry(
            bars,
            confirmation,
            timeframe="M5",
            confirmation_timeframe="M15",
            now=now,
        )

        self.assertEqual(side, Side.SHORT)
        self.assertEqual(signal_id, bars[-1].timestamp.isoformat())

    def test_stale_signal_is_rejected(self) -> None:
        now = datetime.now(timezone.utc)
        bars = _bars(now - timedelta(minutes=15), timeframe_minutes=5)
        confirmation = _bars(now, timeframe_minutes=15)

        with self.assertRaisesRegex(RuntimeError, "M5 bar is stale"):
            evaluate_entry(
                bars,
                confirmation,
                timeframe="M5",
                confirmation_timeframe="M15",
                now=now,
            )

    def test_higher_timeframe_opposition_is_rejected(self) -> None:
        now = datetime.now(timezone.utc)
        bars = _bars(now, timeframe_minutes=5)
        confirmation = []
        for index in range(100):
            close = 1.0 + index * 0.001
            confirmation.append(
                Bar(
                    symbol="GBPUSD",
                    timestamp=now
                    - timedelta(minutes=15 * (100 - index)),
                    open=close,
                    high=close + 0.0001,
                    low=close - 0.0001,
                    close=close,
                )
            )

        with self.assertRaisesRegex(RuntimeError, "confirmation disagrees"):
            evaluate_entry(
                bars,
                confirmation,
                timeframe="M5",
                confirmation_timeframe="M15",
                now=now,
            )

    def test_lower_timeframe_requires_three_of_five(self) -> None:
        now = datetime.now(timezone.utc)
        flat = [
            Bar(
                symbol="GBPUSD",
                timestamp=now - timedelta(minutes=5 * (100 - index)),
                open=1.0,
                high=1.0,
                low=1.0,
                close=1.0,
            )
            for index in range(100)
        ]

        with self.assertRaisesRegex(RuntimeError, "No 3-of-5"):
            evaluate_entry(
                flat,
                _bars(now, timeframe_minutes=15),
                timeframe="M5",
                confirmation_timeframe="M15",
                now=now,
            )
