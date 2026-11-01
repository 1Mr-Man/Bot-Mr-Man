from datetime import datetime, timedelta, timezone

from fxbot.models import Bar
from fxbot.paper import PaperConfig, run_paper_backtest
from fxbot.risk import RiskLimits
from fxbot.strategies import SmaTrendFollowing


def synthetic_bars() -> list[Bar]:
    closes = [
        1.1000,
        1.0990,
        1.0980,
        1.0990,
        1.1010,
        1.1030,
        1.1040,
        1.1020,
        1.1000,
        1.0980,
        1.0970,
        1.0990,
    ]
    start = datetime(2026, 1, 5, tzinfo=timezone.utc)
    bars = []
    previous_close = closes[0]
    for index, close in enumerate(closes):
        open_price = closes[0] if index == 0 else previous_close
        bars.append(
            Bar(
                symbol="EURUSD",
                timestamp=start + timedelta(hours=index),
                open=open_price,
                high=max(open_price, close) + 0.0003,
                low=min(open_price, close) - 0.0003,
                close=close,
            )
        )
        previous_close = close
    return bars


def main() -> None:
    result = run_paper_backtest(
        synthetic_bars(),
        SmaTrendFollowing(fast_period=2, slow_period=3),
        PaperConfig(
            initial_equity=10_000,
            stop_distance=0.002,
            spread=0.0001,
            risk_limits=RiskLimits(risk_per_trade=0.01, max_daily_loss=0.03),
        ),
    )
    print(f"Final balance: ${result.final_balance:,.2f}")
    print(f"Final equity:  ${result.final_equity:,.2f}")
    print(f"Closed trades: {len(result.trades)}")
    for trade in result.trades:
        print(
            f"{trade.side.name:5} {trade.entry_time.isoformat()} -> "
            f"{trade.exit_time.isoformat()} | "
            f"{trade.exit_reason:6} | net ${trade.net_pnl:,.2f}"
        )
    if result.open_position is not None:
        position = result.open_position
        print(
            f"Open position: {position.side.name} {position.symbol}, "
            f"{position.units:,.0f} units, stop {position.stop_price:.5f}"
        )
    if result.lockout_dates:
        print(f"Daily loss lockout dates (UTC): {', '.join(map(str, result.lockout_dates))}")


if __name__ == "__main__":
    main()
