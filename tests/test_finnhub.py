from datetime import datetime, timedelta, timezone
from io import BytesIO
from contextlib import redirect_stdout
from io import StringIO
from urllib.error import HTTPError
from unittest.mock import patch
import unittest

from examples.finnhub_news_check import main as run_news_check
from fxbot.data.finnhub import (
    EconomicEvent,
    FinnhubClient,
    high_impact_event_block,
)


class FinnhubDataTests(unittest.TestCase):
    def test_high_impact_event_blocks_matching_currency_pair(self) -> None:
        now = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
        events = (
            EconomicEvent(
                country="United States",
                event="Interest rate decision",
                impact="high",
                timestamp=now + timedelta(minutes=10),
            ),
        )

        reason = high_impact_event_block("EURUSD.a", events, now=now)

        self.assertIn("Interest rate decision", reason or "")

    def test_event_outside_blackout_and_low_impact_do_not_block(self) -> None:
        now = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
        events = (
            EconomicEvent(
                country="United States",
                event="CPI",
                impact="high",
                timestamp=now + timedelta(minutes=31),
            ),
            EconomicEvent(
                country="United States",
                event="Retail sales",
                impact="medium",
                timestamp=now,
            ),
        )

        self.assertIsNone(high_impact_event_block("XAUUSD", events, now=now))

    def test_event_country_must_match_instrument_currencies(self) -> None:
        now = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
        events = (
            EconomicEvent(
                country="Japan",
                event="Policy statement",
                impact="high",
                timestamp=now,
            ),
        )

        self.assertIsNone(high_impact_event_block("EURUSD", events, now=now))

    def test_economic_calendar_iso_country_codes_are_supported(self) -> None:
        now = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
        events = (
            EconomicEvent(
                country="US",
                event="CPI",
                impact="high",
                timestamp=now,
            ),
        )

        self.assertIsNotNone(high_impact_event_block("EURUSD", events, now=now))

    def test_unsupported_symbol_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "mapping"):
            high_impact_event_block("BTCUSD", (), now=datetime.now(timezone.utc))

    def test_calendar_response_is_parsed_and_cached(self) -> None:
        client = FinnhubClient("test-key")
        body = (
            b'{"economicCalendar":[{"country":"United States",'
            b'"event":"CPI","impact":"high","time":"2026-10-22 12:30:00"}]}'
        )
        with patch("fxbot.data.finnhub.urlopen", return_value=BytesIO(body)) as request:
            now = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
            first = client.economic_events(now=now)
            second = client.economic_events(now=now)

        self.assertEqual(first, second)
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].timestamp.tzinfo, timezone.utc)
        self.assertEqual(request.call_count, 1)

    def test_calendar_provider_errors_are_explicit_and_do_not_expose_key(self) -> None:
        client = FinnhubClient("secret-test-key")
        with patch(
            "fxbot.data.finnhub.urlopen",
            side_effect=TimeoutError("timed out"),
        ):
            with self.assertRaisesRegex(RuntimeError, "Finnhub request failed"):
                client.economic_events()

    def test_http_error_identifies_endpoint_without_exposing_key(self) -> None:
        client = FinnhubClient("secret-test-key")
        error = HTTPError(
            "https://finnhub.io/api/v1/calendar/economic",
            403,
            "Forbidden",
            {},
            BytesIO(b"forbidden"),
        )
        with patch("fxbot.data.finnhub.urlopen", side_effect=error):
            with self.assertRaisesRegex(
                RuntimeError, r"calendar/economic failed with HTTP 403"
            ) as raised:
                client.economic_events()

        self.assertNotIn("secret-test-key", str(raised.exception))

    def test_news_items_are_returned_as_context(self) -> None:
        client = FinnhubClient("test-key")
        body = (
            b'[{"headline":"Market update","source":"Example",'
            b'"url":"https://example.invalid/story"}]'
        )
        with patch("fxbot.data.finnhub.urlopen", return_value=BytesIO(body)):
            news = client.market_news()

        self.assertEqual(len(news), 1)
        self.assertEqual(news[0].headline, "Market update")
        self.assertEqual(news[0].source, "Example")

    def test_checker_does_not_report_empty_calendar_when_calendar_is_unavailable(self) -> None:
        client = FinnhubClient("test-key")
        with (
            patch(
                "examples.finnhub_news_check.FinnhubClient.from_environment",
                return_value=client,
            ),
            patch.object(
                client,
                "economic_events",
                side_effect=RuntimeError("calendar denied"),
            ),
            patch.object(
                client,
                "market_news",
                return_value=(),
            ),
        ):
            output = StringIO()
            with redirect_stdout(output):
                result = run_news_check()

        self.assertEqual(result, 1)
        self.assertIn("Upcoming high-impact events: unknown", output.getvalue())
        self.assertNotIn("None in the returned calendar window", output.getvalue())
