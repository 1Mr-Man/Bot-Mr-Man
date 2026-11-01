import sys
from datetime import datetime, timezone

from fxbot.data.finnhub import FinnhubClient


def main() -> int:
    try:
        client = FinnhubClient.from_environment()
    except (RuntimeError, ValueError) as exc:
        print(f"Finnhub check failed: {exc}", file=sys.stderr)
        return 1

    failures: list[str] = []
    try:
        events = client.economic_events()
    except RuntimeError as exc:
        events = ()
        failures.append(str(exc))
        calendar_available = False
        print(f"Economic calendar: unavailable ({exc})")
    else:
        calendar_available = True
        print(f"Economic calendar: OK ({len(events)} events received)")

    try:
        headlines = client.market_news()
    except RuntimeError as exc:
        headlines = ()
        failures.append(str(exc))
        print(f"General headlines: unavailable ({exc})")
    else:
        print(f"General headlines: OK ({len(headlines)} received)")

    now = datetime.now(timezone.utc)
    if calendar_available:
        upcoming = [
            event
            for event in events
            if event.timestamp >= now
            and event.impact.strip().casefold() in {"high", "3", "3.0"}
        ]
        print("Upcoming high-impact events (UTC):")
        if not upcoming:
            print("  None in the returned calendar window")
        for event in upcoming:
            print(
                f"  {event.timestamp.isoformat()} | {event.country} | "
                f"{event.event} | impact={event.impact}"
            )
    else:
        print("Upcoming high-impact events: unknown (calendar unavailable)")
    for headline in headlines:
        print(f"  [{headline.source}] {headline.headline}")
        if headline.url:
            print(f"    {headline.url}")
    print("Read-only check complete; no broker connection or orders were used.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
