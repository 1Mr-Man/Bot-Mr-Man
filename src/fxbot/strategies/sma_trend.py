from collections import deque

from fxbot.models import Bar, Side


class SmaTrendFollowing:
    """Long above the slow SMA, short below it, and flat during warm-up."""

    def __init__(self, fast_period: int = 10, slow_period: int = 30) -> None:
        if fast_period < 1 or slow_period < 2:
            raise ValueError("periods must be positive and slow_period at least 2")
        if fast_period >= slow_period:
            raise ValueError("fast_period must be less than slow_period")
        self.fast_period = fast_period
        self.slow_period = slow_period
        self._closes: deque[float] = deque(maxlen=slow_period)

    def on_bar(self, bar: Bar) -> Side:
        self._closes.append(bar.close)
        if len(self._closes) < self.slow_period:
            return Side.FLAT

        values = tuple(self._closes)
        fast_average = sum(values[-self.fast_period :]) / self.fast_period
        slow_average = sum(values) / self.slow_period
        if fast_average > slow_average:
            return Side.LONG
        if fast_average < slow_average:
            return Side.SHORT
        return Side.FLAT
