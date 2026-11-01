from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fxbot.data.mt5 import Mt5BarSource


class FakeMt5:
    TIMEFRAME_H4 = 240
    TIMEFRAME_H1 = 60
    TIMEFRAME_M5 = 5
    TIMEFRAME_M15 = 15
    TIMEFRAME_M30 = 30
    ACCOUNT_TRADE_MODE_DEMO = 0
    ACCOUNT_TRADE_MODE_CONTEST = 1

    def __init__(self, rates: list[dict[str, int | float]], trade_mode: int = 0) -> None:
        self.rates = rates
        self.trade_mode = trade_mode
        self.initialized = False
        self.shutdown_called = False
        self.request: tuple[str, int, datetime, datetime] | None = None
        self.last_request: tuple[str, int, datetime, datetime] | None = None

    def initialize(self, *, timeout: int) -> bool:
        self.initialized = timeout == 60_000
        return self.initialized

    def shutdown(self) -> None:
        self.shutdown_called = True

    def account_info(self) -> SimpleNamespace:
        return SimpleNamespace(trade_mode=self.trade_mode)

    def copy_rates_range(
        self,
        symbol: str,
        timeframe: int,
        date_from: datetime,
        date_to: datetime,
    ) -> list[dict[str, int | float]]:
        self.request = (symbol, timeframe, date_from, date_to)
        self.last_request = self.request
        return self.rates

    def last_error(self) -> tuple[int, str]:
        return (0, "no error")


class Mt5BarSourceTests(unittest.TestCase):
    def test_loads_completed_bars_from_demo_and_shuts_down(self) -> None:
        start = datetime(2026, 1, 5, tzinfo=timezone.utc)
        end = start + timedelta(hours=2)
        rates = [
            {
                "time": int(start.timestamp()),
                "open": 1.1,
                "high": 1.11,
                "low": 1.09,
                "close": 1.105,
                "tick_volume": 20,
            }
        ]
        api = FakeMt5(rates)
        source = Mt5BarSource()

        with patch("fxbot.data.mt5.import_module", return_value=api):
            bars = source.load(start, end)

        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].symbol, "EURUSD")
        self.assertEqual(bars[0].timestamp, start)
        self.assertEqual(api.last_request, ("EURUSD", api.TIMEFRAME_H1, start, end))
        self.assertTrue(api.shutdown_called)

    def test_loads_gold_m15_bars_with_m15_completion_window(self) -> None:
        start = datetime(2026, 1, 5, tzinfo=timezone.utc)
        end = start + timedelta(hours=1)
        rates = [
            {
                "time": int(start.timestamp()),
                "open": 2_000.0,
                "high": 2_005.0,
                "low": 1_999.0,
                "close": 2_003.0,
                "tick_volume": 20,
            }
        ]
        api = FakeMt5(rates)
        source = Mt5BarSource(symbol="XAUUSD", timeframe="M15")

        with patch("fxbot.data.mt5.import_module", return_value=api):
            bars = source.load(start, end)

        self.assertEqual(bars[0].symbol, "XAUUSD")
        self.assertEqual(api.last_request, ("XAUUSD", api.TIMEFRAME_M15, start, end))
        self.assertTrue(api.shutdown_called)

    def test_loads_h4_bars(self) -> None:
        start = datetime(2026, 1, 5, tzinfo=timezone.utc)
        rates = [
            {
                "time": int(start.timestamp()),
                "open": 2_000.0,
                "high": 2_005.0,
                "low": 1_999.0,
                "close": 2_003.0,
                "tick_volume": 20,
            }
        ]
        api = FakeMt5(rates)

        with patch("fxbot.data.mt5.import_module", return_value=api):
            bars = Mt5BarSource(symbol="XAUUSD", timeframe="H4").load(
                start,
                start + timedelta(days=1),
            )

        self.assertEqual(len(bars), 1)
        self.assertEqual(api.last_request[1], api.TIMEFRAME_H4)

    def test_rejects_unsupported_timeframe(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported timeframe"):
            Mt5BarSource(timeframe="D1")

    def test_rejects_non_demo_account_and_shuts_down(self) -> None:
        start = datetime(2026, 1, 5, tzinfo=timezone.utc)
        api = FakeMt5([], trade_mode=2)

        with patch("fxbot.data.mt5.import_module", return_value=api):
            with self.assertRaisesRegex(RuntimeError, "demo or contest"):
                Mt5BarSource().load(start, start + timedelta(hours=1))

        self.assertTrue(api.shutdown_called)
        self.assertIsNone(api.request)

    def test_rejects_naive_datetimes_before_connecting(self) -> None:
        api = FakeMt5([])

        with patch("fxbot.data.mt5.import_module", return_value=api):
            with self.assertRaisesRegex(ValueError, "start must be timezone-aware"):
                Mt5BarSource().load(
                    datetime(2026, 1, 5),
                    datetime(2026, 1, 6, tzinfo=timezone.utc),
                )

        self.assertFalse(api.initialized)
