from typing import Protocol

from fxbot.models import Bar, Side


class Strategy(Protocol):
    """A strategy consumes completed bars and returns a target position."""

    def on_bar(self, bar: Bar) -> Side:
        ...
