import argparse
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from fxbot.analytics import build_run_card
from fxbot.data.mt5 import Mt5BarSource
from fxbot.paper import PaperConfig, run_paper_backtest
from fxbot.risk import RiskLimits
from fxbot.strategies import (
    DonchianBreakout,
    EmaTrendFollowing,
    MacdTrendFollowing,
    RsiMeanReversion,
    SmaTrendFollowing,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Paper-simulate MT5 historical bars.")
    parser.add_argument("--start", required=True, type=date.fromisoformat, help="First UTC date")
    parser.add_argument("--end", required=True, type=date.fromisoformat, help="Last UTC date, inclusive")
    parser.add_argument("--symbol", default="EURUSD", help="Broker's EUR/USD symbol name")
    parser.add_argument(
        "--timeframe",
        choices=("M5", "M15", "M30", "H1", "H4"),
        default="H1",
        help="MT5 candle interval (default: H1)",
    )
    parser.add_argument(
        "--strategy",
        choices=("sma", "donchian", "ema", "macd", "rsi"),
        default="sma",
        help="Candidate strategy to backtest",
    )
    parser.add_argument(
        "--lookback",
        type=int,
        default=20,
        help="Donchian channel lookback in bars (default: 20)",
    )
    parser.add_argument("--stop-distance", required=True, type=float, help="Stop distance in price units")
    parser.add_argument("--spread", required=True, type=float, help="Full spread in price units")
    parser.add_argument("--slippage", type=float, default=0.0, help="Slippage per side in price units")
    parser.add_argument("--commission-per-unit", type=float, default=0.0)
    parser.add_argument("--risk-per-trade", type=float, default=0.01)
    parser.add_argument("--max-daily-loss", type=float, default=0.03)
    parser.add_argument(
        "--report",
        type=Path,
        help="JSON run report path (default: runs/mt5-paper-<UTC timestamp>.json)",
    )
    args = parser.parse_args()
    if args.start > args.end:
        parser.error("--start must not be later than --end")
    if args.lookback < 2:
        parser.error("--lookback must be at least 2")
    return args


def main() -> None:
    args = parse_args()
    start = datetime.combine(args.start, time.min, tzinfo=timezone.utc)
    end = datetime.combine(args.end + timedelta(days=1), time.min, tzinfo=timezone.utc)
    bar_duration = timedelta(minutes=Mt5BarSource.TIMEFRAME_MINUTES[args.timeframe])
    bars = Mt5BarSource(symbol=args.symbol, timeframe=args.timeframe).load(
        start - 30 * bar_duration,
        end - timedelta(microseconds=1),
    )
    if args.strategy == "donchian":
        strategy = DonchianBreakout(lookback=args.lookback)
        strategy_name = f"Donchian breakout ({args.lookback}-bar channel)"
    elif args.strategy == "sma":
        strategy = SmaTrendFollowing(fast_period=10, slow_period=30)
        strategy_name = "SMA trend (fast 10, slow 30)"
    elif args.strategy == "ema":
        strategy = EmaTrendFollowing(fast_period=12, slow_period=26)
        strategy_name = "EMA trend (fast 12, slow 26)"
    elif args.strategy == "macd":
        strategy = MacdTrendFollowing()
        strategy_name = "MACD trend (12, 26, 9)"
    else:
        strategy = RsiMeanReversion()
        strategy_name = "RSI mean reversion (14, 30/70)"
    config = PaperConfig(
        initial_equity=10_000,
        stop_distance=args.stop_distance,
        spread=args.spread,
        slippage=args.slippage,
        commission_per_unit_per_side=args.commission_per_unit,
        risk_limits=RiskLimits(
            risk_per_trade=args.risk_per_trade,
            max_daily_loss=args.max_daily_loss,
        ),
    )
    result = run_paper_backtest(bars, strategy, config)
    report = build_run_card(
        result,
        bars,
        config,
        strategy_name=strategy_name,
    )
    report_path = args.report or Path(
        "runs",
        f"mt5-paper-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json",
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report.write_json(report_path)

    print(f"Symbol/bars:   {args.symbol} {args.timeframe} ({len(bars)} bars)")
    print(f"Final balance: ${result.final_balance:,.2f}")
    print(f"Final equity:  ${result.final_equity:,.2f}")
    print(f"Closed trades: {len(result.trades)}")
    metrics = report.metrics
    print(f"Total return:  {metrics.total_return_pct:.2f}%")
    print(f"Max drawdown:  {metrics.max_drawdown_pct:.2f}%")
    win_rate = "n/a" if metrics.win_rate_pct is None else f"{metrics.win_rate_pct:.2f}%"
    profit_factor = (
        "n/a" if metrics.profit_factor is None else f"{metrics.profit_factor:.2f}"
    )
    print(f"Win rate:      {win_rate}")
    print(f"Profit factor: {profit_factor}")
    print(f"Lockout dates: {', '.join(map(str, result.lockout_dates)) or 'none'}")
    if result.open_position is None:
        print("Open position: none")
    else:
        position = result.open_position
        print(
            f"Open position: {position.side.name} {position.symbol}, "
            f"{position.units:,.0f} units; unrealized PnL "
            f"${position.unrealized_pnl:,.2f}"
        )
    print(f"Run report:    {report_path}")


if __name__ == "__main__":
    main()
