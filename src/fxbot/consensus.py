from fxbot.models import Bar, Side
from fxbot.strategies import (
    DonchianBreakout,
    EmaTrendFollowing,
    MacdTrendFollowing,
    RsiMeanReversion,
    SmaTrendFollowing,
    Strategy,
)


STRATEGY_IDS = ("sma", "donchian", "ema", "macd", "rsi")


def make_all_strategies(lookback: int) -> list[tuple[str, Strategy]]:
    return make_strategies(lookback=lookback)


def make_strategies(
    *,
    lookback: int,
    strategy_ids: tuple[str, ...] | list[str] | None = None,
) -> list[tuple[str, Strategy]]:
    selected = STRATEGY_IDS if strategy_ids is None else tuple(strategy_ids)
    if not selected:
        raise ValueError("at least one strategy must be selected")
    if len(set(selected)) != len(selected) or any(
        item not in STRATEGY_IDS for item in selected
    ):
        raise ValueError("strategy selection contains unknown or duplicate ids")
    factories = {
        "sma": lambda: (
            "SMA 10/30 trend",
            SmaTrendFollowing(fast_period=10, slow_period=30),
        ),
        "donchian": lambda: (
            f"Donchian {lookback} breakout",
            DonchianBreakout(lookback=lookback),
        ),
        "ema": lambda: (
            "EMA 12/26 trend",
            EmaTrendFollowing(fast_period=12, slow_period=26),
        ),
        "macd": lambda: ("MACD 12/26/9 trend", MacdTrendFollowing()),
        "rsi": lambda: ("RSI 14 mean reversion", RsiMeanReversion()),
    }
    return [factories[item]() for item in selected]


def scan_latest_signals(
    bars: list[Bar],
    strategies: list[tuple[str, Strategy]],
) -> list[tuple[str, Side, Side]]:
    """Return each strategy's previous and current target side."""
    results: list[tuple[str, Side, Side]] = []
    for name, strategy in strategies:
        previous = Side.FLAT
        current = Side.FLAT
        for bar in bars:
            previous = current
            current = strategy.on_bar(bar)
        results.append((name, previous, current))
    return results


def describe_scan_result(previous: Side, current: Side) -> str:
    if current != previous:
        return f"NEW {current.name}" if current != Side.FLAT else "EXIT to FLAT"
    return f"HOLD {current.name}" if current != Side.FLAT else "NO SIGNAL"


def count_strategy_votes(
    results: list[tuple[str, Side, Side]],
) -> tuple[int, int]:
    """Count latest long and short target states across strategies."""
    long_votes = sum(current == Side.LONG for _, _, current in results)
    short_votes = sum(current == Side.SHORT for _, _, current in results)
    return long_votes, short_votes


def consensus_side(
    long_votes: int,
    short_votes: int,
    threshold: int = 3,
) -> Side:
    """Return a side only when the configured number of votes agrees."""
    if threshold < 1:
        raise ValueError("consensus threshold must be positive")
    if long_votes >= threshold and long_votes > short_votes:
        return Side.LONG
    if short_votes >= threshold and short_votes > long_votes:
        return Side.SHORT
    return Side.FLAT


def trend_vote_counts(
    results: list[tuple[str, Side, Side]],
) -> tuple[int, int]:
    """Count trend-following votes, excluding the RSI mean-reversion strategy."""
    trend_results = [
        result for result in results if not result[0].startswith("RSI ")
    ]
    long_votes = sum(current == Side.LONG for _, _, current in trend_results)
    short_votes = sum(current == Side.SHORT for _, _, current in trend_results)
    return long_votes, short_votes
