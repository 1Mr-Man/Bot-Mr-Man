from collections import deque

from fxbot.models import Bar, Side


class DonchianBreakout:
    """Enter on an N-bar channel breakout; exit on the opposite breakout.

    The current bar is excluded from both channels. A position is held between
    breakouts so ordinary bars do not spuriously close it.
    """

    def __init__(self, lookback: int = 20) -> None:
        if lookback < 2:
            raise ValueError("lookback must be at least 2")
        self.lookback = lookback
        self._bars: deque[Bar] = deque(maxlen=lookback)
        self._position = Side.FLAT

    def on_bar(self, bar: Bar) -> Side:
        if len(self._bars) < self.lookback:
            self._bars.append(bar)
            return Side.FLAT

        upper_channel = max(item.high for item in self._bars)
        lower_channel = min(item.low for item in self._bars)
        if bar.close > upper_channel:
            self._position = Side.LONG
        elif bar.close < lower_channel:
            self._position = Side.SHORT

        self._bars.append(bar)
        return self._position
