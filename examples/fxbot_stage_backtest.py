import argparse
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import sys

from fxbot.analytics import build_run_card
from fxbot.consensus import STRATEGY_IDS, make_strategies
from fxbot.data.mt5 import Mt5BarSource
from fxbot.models import Bar, Side
from fxbot.paper import PaperConfig, run_paper_backtest
from fxbot.risk import RiskLimits
from fxbot.stages import build_stage_targets, stage_reached_by_simulation


_DEFAULT_STOP = {
    "XAUUSD": 5.0,
    "EURUSD": 0.001,
}
_DEFAULT_SPREAD = {
    "XAUUSD": 0.50,
    "EURUSD": 0.0003,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Historical paper simulation with stage targets; never submits orders."
    )
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--timeframe", choices=("M5", "M15", "M30", "H1", "H4"), default="H1")
    parser.add_argument("--strategy", choices=STRATEGY_IDS, default="sma")
    parser.add_argument("--start", required=True, help="UTC start date YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="UTC end date YYYY-MM-DD (inclusive)")
    parser.add_argument("--initial-balance", type=float, default=10000.0)
    parser.add_argument("--stages", type=int, default=30)
    parser.add_argument("--growth-rate", type=float, default=0.30)
    parser.add_argument("--stop-distance", type=float)
    parser.add_argument("--spread", type=float)
    parser.add_argument("--slippage", type=float, default=0.0)
    parser.add_argument("--commission-per-unit", type=float, default=0.0)
    parser.add_argument("--value-per-price-unit", type=float, default=1.0)
    parser.add_argument("--risk-per-trade", type=float, default=0.001)
    parser.add_argument("--max-daily-loss", type=float, default=0.02)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        args.start_date = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end_date = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        parser.error("--start and --end must be dates in YYYY-MM-DD format")
    if end_date < args.start_date:
        parser.error("--end must be on or after --start")
    args.end_exclusive = end_date + timedelta(days=1)
    if not math.isfinite(args.initial_balance) or args.initial_balance <= 0:
        parser.error("--initial-balance must be finite and positive")
    if not math.isfinite(args.growth_rate) or not 0 < args.growth_rate <= 1:
        parser.error("--growth-rate must be greater than 0 and at most 1")
    if args.stages < 1 or args.stages > 100:
        parser.error("--stages must be between 1 and 100")
    if args.risk_per_trade <= 0 or args.max_daily_loss <= 0:
        parser.error("risk and daily loss settings must be positive")
    symbol_key = args.symbol.upper()
    if args.stop_distance is None:
        args.stop_distance = next(
            (value for key, value in _DEFAULT_STOP.items() if symbol_key.startswith(key)),
            None,
        )
    if args.spread is None:
        args.spread = next(
            (value for key, value in _DEFAULT_SPREAD.items() if symbol_key.startswith(key)),
            None,
        )
    if args.stop_distance is None or args.spread is None:
        parser.error("provide --stop-distance and --spread for this broker symbol")
    if args.stop_distance <= 0 or args.spread < 0:
        parser.error("stop distance must be positive and spread nonnegative")
    return args


def evaluate_strategy(bars: list[Bar], strategy_id: str, lookback: int = 20) -> tuple[Side, ...]:
    strategy = make_strategies(lookback=lookback, strategy_ids=(strategy_id,))[0][1]
    return tuple(strategy.on_bar(bar) for bar in bars)


def main() -> None:
    args = parse_args()
    bars = Mt5BarSource(symbol=args.symbol, timeframe=args.timeframe).load(
        args.start_date,
        args.end_exclusive,
    )
    strategy_name, strategy = make_strategies(
        lookback=20,
        strategy_ids=(args.strategy,),
    )[0]
    config = PaperConfig(
        initial_equity=args.initial_balance,
        stop_distance=args.stop_distance,
        spread=args.spread,
        slippage=args.slippage,
        commission_per_unit_per_side=args.commission_per_unit,
        value_per_price_unit=args.value_per_price_unit,
        risk_limits=RiskLimits(
            risk_per_trade=args.risk_per_trade,
            max_daily_loss=args.max_daily_loss,
        ),
    )
    result = run_paper_backtest(bars, strategy, config)
    card = build_run_card(result, bars, config, strategy_name=strategy_name)
    targets = build_stage_targets(
        args.initial_balance,
        stages=args.stages,
        growth_rate=args.growth_rate,
    )
    reached = stage_reached_by_simulation(
        args.initial_balance,
        result.final_equity,
        stages=args.stages,
        growth_rate=args.growth_rate,
    )
    summary = {
        "strategy": strategy_name,
        "symbol": args.symbol,
        "timeframe": args.timeframe,
        "bar_count": len(bars),
        "period_start_utc": bars[0].timestamp.isoformat(),
        "period_end_utc": bars[-1].timestamp.isoformat(),
        "initial_balance": args.initial_balance,
        "final_balance": result.final_balance,
        "final_equity": result.final_equity,
        "closed_trades": len(result.trades),
        "wins": sum(trade.net_pnl > 0 for trade in result.trades),
        "losses": sum(trade.net_pnl < 0 for trade in result.trades),
        "win_rate_pct": card.metrics.win_rate_pct,
        "profit_factor": card.metrics.profit_factor,
        "max_drawdown_pct": card.metrics.max_drawdown_pct,
        "simulation_reached_stage": reached,
        "stage_rule": f"initial_balance * (1 + {args.growth_rate}) ** stage",
        "stage_targets": [asdict(target) for target in targets],
        "reliability_warning": (
            "Historical backtest statistics do not establish future reliability. "
            "Use out-of-sample and forward paper tests; targets are not forecasts."
        ),
        "assumptions": json.loads(card.to_json()),
    }
    rendered = json.dumps(summary, indent=2, allow_nan=False)
    print(rendered)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered + "\n", encoding="utf-8")
        print(f"Report saved: {args.report}")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
