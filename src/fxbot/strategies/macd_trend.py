from fxbot.models import Bar, Side
from fxbot.strategies._ema import _ExponentialMovingAverage


class MacdTrendFollowing:
    """Follow the MACD line relative to its signal-line EMA."""

    def __init__(
        self,
        fast_period: int = 12,
        slow_period: int = 26,
        signal_period: int = 9,
    ) -> None:
        if fast_period < 1 or slow_period < 2 or signal_period < 1:
            raise ValueError("periods must be positive and slow_period at least 2")
        if fast_period >= slow_period:
            raise ValueError("fast_period must be less than slow_period")
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.signal_period = signal_period
        self._fast = _ExponentialMovingAverage(fast_period)
        self._slow = _ExponentialMovingAverage(slow_period)
        self._signal = _ExponentialMovingAverage(signal_period)

    def on_bar(self, bar: Bar) -> Side:
        fast = self._fast.update(bar.close)
        slow = self._slow.update(bar.close)
        if fast is None or slow is None:
            return Side.FLAT

        macd = fast - slow
        signal = self._signal.update(macd)
        if signal is None:
            return Side.FLAT
        if macd > signal:
            return Side.LONG
        if macd < signal:
            return Side.SHORT
        return Side.FLAT
