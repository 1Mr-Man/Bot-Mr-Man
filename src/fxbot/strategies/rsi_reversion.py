from collections import deque

from fxbot.models import Bar, Side


class RsiMeanReversion:
    """Enter at RSI extremes and hold until RSI returns through its midpoint.

    RSI is calculated from simple rolling average gains and losses.
    """

    def __init__(
        self,
        period: int = 14,
        oversold: float = 30,
        overbought: float = 70,
        exit_level: float = 50,
    ) -> None:
        if period < 1:
            raise ValueError("period must be positive")
        if not 0 <= oversold < exit_level < overbought <= 100:
            raise ValueError(
                "RSI levels must satisfy 0 <= oversold < exit < overbought <= 100"
            )
        self.period = period
        self.oversold = oversold
        self.overbought = overbought
        self.exit_level = exit_level
        self._changes: deque[float] = deque(maxlen=period)
        self._previous_close: float | None = None
        self._position = Side.FLAT

    def on_bar(self, bar: Bar) -> Side:
        if self._previous_close is None:
            self._previous_close = bar.close
            return self._position

        self._changes.append(bar.close - self._previous_close)
        self._previous_close = bar.close
        if len(self._changes) < self.period:
            return self._position

        gains = sum(max(change, 0.0) for change in self._changes) / self.period
        losses = sum(max(-change, 0.0) for change in self._changes) / self.period
        if losses == 0:
            rsi = 50.0 if gains == 0 else 100.0
        else:
            relative_strength = gains / losses
            rsi = 100 - 100 / (1 + relative_strength)

        if self._position == Side.FLAT:
            if rsi <= self.oversold:
                self._position = Side.LONG
            elif rsi >= self.overbought:
                self._position = Side.SHORT
        elif self._position == Side.LONG and rsi >= self.exit_level:
            self._position = Side.FLAT
        elif self._position == Side.SHORT and rsi <= self.exit_level:
            self._position = Side.FLAT
        return self._position
