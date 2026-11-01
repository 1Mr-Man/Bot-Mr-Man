from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from fxbot.models import Bar, Side
from fxbot.strategies.base import Strategy


@dataclass(frozen=True)
class PositionChange:
    symbol: str
    timestamp: datetime
    price: float
    from_side: Side
    to_side: Side


def run_signal_backtest(bars: Iterable[Bar], strategy: Strategy) -> list[PositionChange]:
    """Record target-position changes, filled at the following bar's open.

    This signal-only runner does not calculate PnL or model trading costs.
    """
    history = list(bars)
    if any(
        current.symbol != previous.symbol
        for previous, current in zip(history, history[1:])
    ):
        raise ValueError("a backtest run must contain bars for only one symbol")
    if any(
        current.timestamp <= previous.timestamp
        for previous, current in zip(history, history[1:])
    ):
        raise ValueError("bar timestamps must be strictly increasing")

    changes: list[PositionChange] = []
    current_side = Side.FLAT
    for index, bar in enumerate(history[:-1]):
        target_side = strategy.on_bar(bar)
        if target_side == current_side:
            continue
        fill_bar = history[index + 1]
        changes.append(
            PositionChange(
                symbol=bar.symbol,
                timestamp=fill_bar.timestamp,
                price=fill_bar.open,
                from_side=current_side,
                to_side=target_side,
            )
        )
        current_side = target_side
    return changes
