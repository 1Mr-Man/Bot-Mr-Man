from collections import deque


class _ExponentialMovingAverage:
    def __init__(self, period: int) -> None:
        self.period = period
        self._seed: deque[float] = deque(maxlen=period)
        self.value: float | None = None
        self._alpha = 2 / (period + 1)

    def update(self, value: float) -> float | None:
        if self.value is None:
            self._seed.append(value)
            if len(self._seed) == self.period:
                self.value = sum(self._seed) / self.period
            return self.value

        self.value += self._alpha * (value - self.value)
        return self.value
