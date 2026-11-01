import argparse
from datetime import datetime, timedelta, timezone
import hashlib
from importlib import import_module
from pathlib import Path
import sys
from typing import cast

from fxbot.consensus import (
    consensus_side,
    count_strategy_votes,
    make_all_strategies,
    scan_latest_signals,
    trend_vote_counts,
)
from fxbot.data.mt5 import Mt5BarSource
from fxbot.execution.mt5_demo import (
    DemoOrderSettings,
    Mt5TradingApi,
    prepare_demo_order,
    submit_demo_order,
)
from fxbot.indicators import average_true_range
from fxbot.models import Bar, Side


_DEFAULT_CONFIRMATION = {
    "M5": "M15",
    "M15": "H1",
    "M30": "H1",
    "H1": "H4",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Attempt at most one protected consensus order on an MT5 demo account."
    )
    parser.add_argument("--symbol", required=True)
    parser.add_argument(
        "--timeframe",
        choices=("M5", "M15", "M30", "H1"),
        default="M5",
    )
    parser.add_argument(
        "--confirmation-timeframe",
        choices=("M15", "M30", "H1", "H4"),
    )
    parser.add_argument("--expected-account-id", type=int, required=True)
    parser.add_argument("--max-spread", type=float, required=True)
    parser.add_argument("--risk-per-trade", type=float, default=0.001)
    parser.add_argument("--max-daily-loss", type=float, default=0.02)
    parser.add_argument("--max-volume", type=float, default=0.01)
    parser.add_argument("--max-total-open-risk", type=float, default=0.01)
    parser.add_argument("--atr-period", type=int, default=14)
    parser.add_argument("--atr-multiplier", type=float, default=2.0)
    parser.add_argument("--reward-risk-ratio", type=float, default=2.0)
    parser.add_argument(
        "--confirm",
        required=True,
        help='Must exactly equal "SUBMIT ONE DEMO ORDER"',
    )
    parser.add_argument(
        "--kill-switch",
        type=Path,
        default=Path("runs", "DEMO_STOP"),
    )
    args = parser.parse_args()
    if args.confirm != "SUBMIT ONE DEMO ORDER":
        parser.error('--confirm must exactly equal "SUBMIT ONE DEMO ORDER"')
    if args.expected_account_id <= 0:
        parser.error("--expected-account-id must be positive")
    if args.max_spread <= 0:
        parser.error("--max-spread must be positive")
    if args.atr_period < 1 or args.atr_multiplier <= 0:
        parser.error("--atr-period and --atr-multiplier must be positive")
    if args.confirmation_timeframe is None:
        args.confirmation_timeframe = _DEFAULT_CONFIRMATION[args.timeframe]
    if (
        Mt5BarSource.TIMEFRAME_MINUTES[args.confirmation_timeframe]
        <= Mt5BarSource.TIMEFRAME_MINUTES[args.timeframe]
    ):
        parser.error("--confirmation-timeframe must be higher than --timeframe")
    return args


def evaluate_entry(
    bars: list[Bar],
    confirmation_bars: list[Bar],
    *,
    timeframe: str,
    confirmation_timeframe: str,
    now: datetime,
) -> tuple[Side, str]:
    if not bars or not confirmation_bars:
        raise RuntimeError("Both trading and confirmation timeframes need completed bars")
    now_utc = now.astimezone(timezone.utc)
    lower_bar = bars[-1]
    higher_bar = confirmation_bars[-1]
    lower_age = now_utc - lower_bar.timestamp
    lower_limit = timedelta(
        minutes=2 * Mt5BarSource.TIMEFRAME_MINUTES[timeframe]
    )
    if lower_age < timedelta(0) or lower_age > lower_limit:
        raise RuntimeError(f"Latest {timeframe} bar is stale ({lower_age})")
    higher_age = now_utc - higher_bar.timestamp
    higher_duration = timedelta(
        minutes=Mt5BarSource.TIMEFRAME_MINUTES[confirmation_timeframe]
    )
    if (
        higher_age < timedelta(0)
        or higher_age > higher_duration * 2
        or higher_bar.timestamp > lower_bar.timestamp
    ):
        raise RuntimeError(f"Latest {confirmation_timeframe} confirmation is stale")

    strategies = make_all_strategies(20)
    lower_results = scan_latest_signals(bars, strategies)
    long_votes, short_votes = count_strategy_votes(lower_results)
    desired = consensus_side(long_votes, short_votes)
    if desired == Side.FLAT:
        raise RuntimeError(
            f"No 3-of-5 {timeframe} consensus "
            f"(LONG {long_votes}/5, SHORT {short_votes}/5)"
        )

    higher_results = scan_latest_signals(
        confirmation_bars,
        make_all_strategies(20),
    )
    higher_long, higher_short = count_strategy_votes(higher_results)
    trend_long, trend_short = trend_vote_counts(higher_results)
    higher_signal = consensus_side(higher_long, higher_short)
    higher_trend = consensus_side(trend_long, trend_short)
    if higher_signal != desired or higher_trend != desired:
        raise RuntimeError(
            f"{confirmation_timeframe} confirmation disagrees with {desired.name}: "
            f"signal LONG {higher_long}/5, SHORT {higher_short}/5; "
            f"trend LONG {trend_long}/4, SHORT {trend_short}/4"
        )
    signal_id = lower_bar.timestamp.isoformat()
    return desired, signal_id


def main() -> None:
    args = parse_args()
    if args.kill_switch.exists():
        raise RuntimeError(f"Kill switch is engaged: {args.kill_switch}")
    now = datetime.now(timezone.utc)
    bars = Mt5BarSource(
        symbol=args.symbol,
        timeframe=args.timeframe,
    ).load(now - timedelta(days=60), now)
    confirmation_bars = Mt5BarSource(
        symbol=args.symbol,
        timeframe=args.confirmation_timeframe,
    ).load(now - timedelta(days=60), now)
    side, signal_id = evaluate_entry(
        bars,
        confirmation_bars,
        timeframe=args.timeframe,
        confirmation_timeframe=args.confirmation_timeframe,
        now=datetime.now(timezone.utc),
    )
    print(
        f"{args.symbol} {args.timeframe}: {side.name} consensus confirmed by "
        f"{args.confirmation_timeframe}."
    )
    print(f"Completed signal bar UTC: {signal_id}")
    stop_distance = average_true_range(bars, args.atr_period) * args.atr_multiplier
    api = _load_api()
    if not api.initialize(timeout=60_000):
        raise RuntimeError(f"Could not initialize MT5: {api.last_error()}")
    try:
        settings = DemoOrderSettings(
            expected_account_id=args.expected_account_id,
            risk_per_trade=args.risk_per_trade,
            max_daily_loss=args.max_daily_loss,
            max_volume=args.max_volume,
            max_spread=args.max_spread,
            reward_risk_ratio=args.reward_risk_ratio,
            max_total_open_risk=args.max_total_open_risk,
        )
        preview = prepare_demo_order(
            api,
            symbol=args.symbol,
            side=side,
            stop_distance=stop_distance,
            settings=settings,
            require_trade_permission=True,
        )
        print(
            f"Preflight: {preview.side.name} {preview.symbol}, "
            f"{preview.volume:g} lots; entry {preview.entry_price}; "
            f"SL {preview.stop_price}; estimated risk "
            f"{preview.estimated_risk:.2f}; daily realized "
            f"{preview.daily_realized_pnl:.2f}."
        )
        key = hashlib.sha256(
            (
                f"consensus-once|{args.expected_account_id}|{args.symbol}|"
                f"{args.timeframe}|{args.confirmation_timeframe}|"
                f"{signal_id}|{side.name}"
            ).encode("utf-8")
        ).hexdigest()
        outcome = submit_demo_order(
            api,
            preview,
            settings=settings,
            enabled=True,
            confirmation="SUBMIT DEMO ORDER",
            idempotency_file=Path("runs", "demo-order-attempts", f"{key}.json"),
            signal_id=signal_id,
        )
        print(
            f"Order status: {outcome.status}; retcode={outcome.retcode}; "
            f"ticket={outcome.order_ticket}; volume={outcome.filled_volume:g}; "
            f"fill={outcome.fill_price}."
        )
        if outcome.status != "FILLED":
            raise RuntimeError(
                "Demo order was not fully filled; inspect MT5 and the attempt record."
            )
    finally:
        api.shutdown()


def _load_api() -> Mt5TradingApi:
    try:
        return cast(Mt5TradingApi, import_module("MetaTrader5"))
    except ImportError as exc:
        raise RuntimeError(
            'MetaTrader5 is required; install it with: python -m pip install -e ".[mt5]"'
        ) from exc


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
