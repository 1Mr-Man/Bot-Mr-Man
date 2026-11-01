from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import math
import os
from time import monotonic
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen


_BASE_URL = "https://finnhub.io/api/v1"
_CURRENCY_COUNTRIES = {
    "AUD": ("australia", "au"),
    "CAD": ("canada", "ca"),
    "CHF": ("switzerland", "ch"),
    "CNY": ("china", "cn"),
    "EUR": ("euro area", "euro zone", "eurozone", "european union", "eu", "ea"),
    "GBP": ("united kingdom", "uk", "gb"),
    "JPY": ("japan", "jp"),
    "NZD": ("new zealand", "nz"),
    "USD": ("united states", "united states of america", "usa", "us"),
}


@dataclass(frozen=True)
class EconomicEvent:
    country: str
    event: str
    impact: str
    timestamp: datetime


@dataclass(frozen=True)
class NewsHeadline:
    headline: str
    source: str
    url: str


class FinnhubClient:
    def __init__(self, api_key: str, *, timeout: float = 5.0) -> None:
        if not api_key.strip():
            raise ValueError("Finnhub API key must not be empty")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Finnhub timeout must be positive")
        self._api_key = api_key.strip()
        self._timeout = timeout
        self._calendar_cache: tuple[float, tuple[EconomicEvent, ...]] | None = None
        self._news_cache: tuple[float, tuple[NewsHeadline, ...]] | None = None

    @classmethod
    def from_environment(cls) -> "FinnhubClient":
        api_key = os.environ.get("FINNHUB_API_KEY", "")
        if not api_key.strip():
            raise RuntimeError(
                "Set FINNHUB_API_KEY locally to enable the optional news guard"
            )
        return cls(api_key)

    def economic_events(self, *, now: datetime | None = None) -> tuple[EconomicEvent, ...]:
        current = _utc(now or datetime.now(timezone.utc))
        if self._calendar_cache and monotonic() - self._calendar_cache[0] < 900:
            return self._calendar_cache[1]
        payload = self._request(
            "calendar/economic",
            {
                "from": (current.date() - timedelta(days=1)).isoformat(),
                "to": (current.date() + timedelta(days=7)).isoformat(),
            },
        )
        rows = payload.get("economicCalendar")
        if not isinstance(rows, list):
            raise RuntimeError("Finnhub economic calendar response has an invalid shape")
        events: list[EconomicEvent] = []
        for row in rows:
            if not isinstance(row, dict):
                raise RuntimeError("Finnhub economic calendar contains an invalid event")
            country = row.get("country")
            name = row.get("event")
            impact = row.get("impact")
            event_time = row.get("time")
            if not all(isinstance(value, str) and value.strip() for value in (country, name, event_time)):
                raise RuntimeError("Finnhub economic calendar event is missing required fields")
            events.append(
                EconomicEvent(
                    country=country.strip(),
                    event=name.strip(),
                    impact=str(impact or "").strip().lower(),
                    timestamp=_parse_event_time(event_time),
                )
            )
        result = tuple(events)
        self._calendar_cache = (monotonic(), result)
        return result

    def market_news(self) -> tuple[NewsHeadline, ...]:
        if self._news_cache and monotonic() - self._news_cache[0] < 900:
            return self._news_cache[1]
        payload = self._request("news", {"category": "general"})
        if not isinstance(payload, list):
            raise RuntimeError("Finnhub market news response has an invalid shape")
        headlines: list[NewsHeadline] = []
        for row in payload[:5]:
            if not isinstance(row, dict):
                raise RuntimeError("Finnhub market news contains an invalid item")
            headline = row.get("headline")
            source = row.get("source")
            url = row.get("url")
            if not all(isinstance(value, str) for value in (headline, source, url)):
                raise RuntimeError("Finnhub market news item is missing required fields")
            if headline.strip():
                headlines.append(
                    NewsHeadline(headline=headline.strip(), source=source.strip(), url=url.strip())
                )
        result = tuple(headlines)
        self._news_cache = (monotonic(), result)
        return result

    def _request(self, endpoint: str, params: dict[str, str]) -> Any:
        query = urlencode({**params, "token": self._api_key})
        request_url = f"{_BASE_URL}/{endpoint}?{query}"
        try:
            with urlopen(request_url, timeout=self._timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise RuntimeError(
                f"Finnhub request to {endpoint} failed with HTTP {exc.code}"
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise RuntimeError("Finnhub request failed; check network and API availability") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Finnhub returned an invalid JSON response") from exc
        if isinstance(payload, dict) and payload.get("error"):
            raise RuntimeError("Finnhub rejected the request; check API access and limits")
        return payload


def high_impact_event_block(
    symbol: str,
    events: tuple[EconomicEvent, ...],
    *,
    now: datetime | None = None,
    before: timedelta = timedelta(minutes=30),
    after: timedelta = timedelta(minutes=15),
) -> str | None:
    current = _utc(now or datetime.now(timezone.utc))
    if before < timedelta(0) or after < timedelta(0):
        raise ValueError("event blackout windows must not be negative")
    currencies = _symbol_currencies(symbol)
    countries = {
        country
        for currency in currencies
        for country in _CURRENCY_COUNTRIES.get(currency, ())
    }
    if not countries:
        raise ValueError(f"no economic-calendar currency mapping for {symbol}")
    for event in events:
        if (
            _is_high_impact(event.impact)
            and event.country.strip().casefold() in countries
            and current - after <= _utc(event.timestamp) <= current + before
        ):
            return (
                f"high-impact {event.country} event '{event.event}' at "
                f"{_utc(event.timestamp).isoformat()}"
            )
    return None


def _symbol_currencies(symbol: str) -> tuple[str, ...]:
    letters = "".join(character for character in symbol.upper() if character.isalpha())
    if letters.startswith(("XAU", "XAG", "GOLD")):
        return ("USD",)
    pair = letters[:6]
    if len(pair) < 6:
        raise ValueError(f"cannot determine currencies for {symbol}")
    currencies = (pair[:3], pair[3:6])
    if not all(currency in _CURRENCY_COUNTRIES for currency in currencies):
        raise ValueError(f"no economic-calendar currency mapping for {symbol}")
    return currencies


def _is_high_impact(impact: str) -> bool:
    return impact.strip().casefold() in {"high", "3", "3.0"}


def _parse_event_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError("Finnhub economic calendar has an invalid event time") from exc
    return _utc(parsed)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
