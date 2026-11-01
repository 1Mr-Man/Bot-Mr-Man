from collections.abc import Sequence
import math

from fxbot.models import Bar


def average_true_range(bars: Sequence[Bar], period: int = 14) -> float:
    """Simple average of the most recent true ranges, in price units."""
    if period < 1:
        raise ValueError("period must be positive")
    if len(bars) < period + 1:
        raise ValueError(f"at least {period + 1} bars are required to calculate ATR")

    true_ranges: list[float] = []
    for previous, current in zip(bars[-period - 1 : -1], bars[-period:]):
        true_range = max(
            current.high - current.low,
            abs(current.high - previous.close),
            abs(current.low - previous.close),
        )
        if not math.isfinite(true_range) or true_range <= 0:
            raise ValueError("bars produced an invalid true range")
        true_ranges.append(true_range)
    return sum(true_ranges) / period
