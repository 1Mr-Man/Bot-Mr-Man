from datetime import datetime, timedelta, timezone
import unittest

from fxbot.indicators import average_true_range
from fxbot.models import Bar


class AverageTrueRangeTests(unittest.TestCase):
    def test_includes_gaps_from_previous_close(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        bars = [
            Bar("XAUUSD", start, 100, 101, 99, 100),
            Bar("XAUUSD", start + timedelta(minutes=15), 105, 106, 104, 105),
            Bar("XAUUSD", start + timedelta(minutes=30), 105, 106, 104, 105),
        ]

        self.assertEqual(average_true_range(bars, period=2), 4.0)

    def test_rejects_insufficient_bars_and_bad_period(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        bars = [Bar("XAUUSD", start, 100, 101, 99, 100)]

        with self.assertRaisesRegex(ValueError, "at least 2 bars"):
            average_true_range(bars, period=1)
        with self.assertRaisesRegex(ValueError, "period must be positive"):
            average_true_range(bars, period=0)
