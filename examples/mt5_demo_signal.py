import argparse
from datetime import datetime, timedelta, timezone
import hashlib
from importlib import import_module
from pathlib import Path
import sys
from typing import cast

from fxbot.data.mt5 import Mt5BarSource
from fxbot.consensus import (
    describe_scan_result,
    make_all_strategies,
    scan_latest_signals,
)
from fxbot.execution.mt5_demo import (
    DemoOrderSettings,
    Mt5TradingApi,
    prepare_demo_order,
    submit_demo_order,
)
from fxbot.indicators import average_true_range
from fxbot.models import Bar, Side
from fxbot.risk import RiskLimits
from fxbot.strategies import (
    DonchianBreakout,
    EmaTrendFollowing,
    MacdTrendFollowing,
    RsiMeanReversion,
    SmaTrendFollowing,
    Strategy,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate the latest completed MT5 bar; demo orders are opt-in."
    )
    symbol_group = parser.add_mutually_exclusive_group()
    symbol_group.add_argument("--symbol", default="EURUSD")
    symbol_group.add_argument(
        "--symbols",
        help="Comma-separated MT5 symbols for a read-only --strategy all scan",
    )
    parser.add_argument(
        "--timeframe",
        choices=("M5", "M15", "M30", "H1", "H4"),
        default="H1",
        help="MT5 candle interval (default: H1)",
    )
    parser.add_argument(
        "--strategy",
        choices=("donchian", "sma", "ema", "macd", "rsi", "all"),
        default="donchian",
        help="Strategy to evaluate; 'all' scans five candidates without order preview",
    )
    parser.add_argument("--lookback", type=int, default=20)
    stop_group = parser.add_mutually_exclusive_group()
    stop_group.add_argument(
        "--stop-distance",
        type=float,
        help="Fixed protective stop distance in the symbol's price units",
    )
    stop_group.add_argument(
        "--atr-multiplier",
        type=float,
        default=2.0,
        help="Stop distance = recent ATR times this value (default: 2)",
    )
    parser.add_argument("--atr-period", type=int, default=14)
    parser.add_argument("--risk-per-trade", type=float, default=0.001)
    parser.add_argument("--max-daily-loss", type=float, default=0.02)
    parser.add_argument("--max-volume", type=float, default=0.01)
    parser.add_argument(
        "--max-spread",
        type=float,
        help="Maximum allowed live spread in symbol price units; choose for your broker",
    )
    parser.add_argument("--max-signal-age-minutes", type=int)
    parser.add_argument("--expected-account-id", type=int)
    parser.add_argument(
        "--execute-demo",
        action="store_true",
        help="Explicitly allow one protected entry on the verified MT5 demo account",
    )
    parser.add_argument(
        "--confirm",
        default="",
        help='Must equal "SUBMIT DEMO ORDER" when --execute-demo is used',
    )
    parser.add_argument(
        "--kill-switch",
        type=Path,
        default=Path("runs", "DEMO_STOP"),
        help="If this file exists, demo order submission is blocked",
    )
    args = parser.parse_args()
    if args.lookback < 2:
        parser.error("--lookback must be at least 2")
    if args.stop_distance is not None and args.stop_distance <= 0:
        parser.error("--stop-distance must be positive")
    if args.atr_period < 1:
        parser.error("--atr-period must be positive")
    if args.atr_multiplier is not None and args.atr_multiplier <= 0:
        parser.error("--atr-multiplier must be positive")
    if args.max_spread is not None and args.max_spread <= 0:
        parser.error("--max-spread must be positive")
    if args.max_signal_age_minutes is not None and args.max_signal_age_minutes < 1:
        parser.error("--max-signal-age-minutes must be positive")
    if args.execute_demo and args.expected_account_id is None:
        parser.error("--expected-account-id is required with --execute-demo")
    if args.execute_demo and args.strategy == "all":
        parser.error("--execute-demo cannot be used with --strategy all")
    if not args.execute_demo and args.confirm:
        parser.error("--confirm is only used with --execute-demo")
    args.symbols = (
        [symbol.strip() for symbol in args.symbols.split(",")]
        if args.symbols
        else [args.symbol]
    )
    if any(not symbol for symbol in args.symbols):
        parser.error("--symbols must contain non-empty symbol names")
    if len({symbol.upper() for symbol in args.symbols}) != len(args.symbols):
        parser.error("--symbols must not contain duplicate symbols")
    if len(args.symbols) > 50:
        parser.error("no more than 50 symbols can be scanned in one command")
    if len(args.symbols) > 1 and args.strategy != "all":
        parser.error("--symbols requires --strategy all")
    if len(args.symbols) > 1 and args.execute_demo:
        parser.error("multi-symbol scanning is read-only; demo execution is unavailable")
    args.symbol = args.symbols[0]
    return args


def make_strategy(args: argparse.Namespace) -> tuple[Strategy, str]:
    if args.strategy == "donchian":
        return DonchianBreakout(lookback=args.lookback), f"Donchian {args.lookback}"
    if args.strategy == "sma":
        return SmaTrendFollowing(fast_period=10, slow_period=30), "SMA 10/30"
    if args.strategy == "ema":
        return EmaTrendFollowing(fast_period=12, slow_period=26), "EMA 12/26"
    if args.strategy == "macd":
        return MacdTrendFollowing(), "MACD 12/26/9"
    return RsiMeanReversion(), "RSI 14 (30/70)"


def latest_entry_signal(
    bars: list[Bar],
    strategy: Strategy,
) -> tuple[Side, Bar] | None:
    """Only return a transition generated by the most recent completed bar."""
    previous = Side.FLAT
    current = Side.FLAT
    for bar in bars:
        previous = current
        current = strategy.on_bar(bar)
    if not bars or current == Side.FLAT or current == previous:
        return None
    return current, bars[-1]


def main() -> None:
    args = parse_args()
    if args.execute_demo and args.kill_switch.exists():
        raise RuntimeError(f"Kill switch is engaged: {args.kill_switch}")

    end = datetime.now(timezone.utc)
    bar_minutes = Mt5BarSource.TIMEFRAME_MINUTES[args.timeframe]
    start = end - timedelta(days=60)
    if args.strategy == "all":
        scan_end = datetime.now(timezone.utc)
        scan_start = scan_end - timedelta(days=60)
        failed_symbols = 0
        for symbol in args.symbols:
            try:
                bars = Mt5BarSource(
                    symbol=symbol,
                    timeframe=args.timeframe,
                ).load(scan_start, scan_end)
            except RuntimeError as exc:
                failed_symbols += 1
                print(
                    f"\nSymbol/timeframe: {symbol} {args.timeframe}\n"
                    f"Data status:      unavailable ({exc})"
                )
                continue
            if not bars:
                failed_symbols += 1
                print(
                    f"\nSymbol/timeframe: {symbol} {args.timeframe}\n"
                    "Data status:      no completed bars available"
                )
                continue
            _print_all_strategy_scan(symbol, args.timeframe, bars, args.lookback)
        print(
            f"\nScan complete: {len(args.symbols) - failed_symbols}/"
            f"{len(args.symbols)} symbols available; scan only, no orders submitted"
        )
        if failed_symbols == len(args.symbols):
            raise RuntimeError("No configured symbols returned completed bars")
        return

    bars = Mt5BarSource(
        symbol=args.symbol,
        timeframe=args.timeframe,
    ).load(start, end)
    if not bars:
        raise RuntimeError(f"No completed bars available for {args.symbol} {args.timeframe}")

    strategy, strategy_name = make_strategy(args)
    candidate = latest_entry_signal(bars, strategy)
    print(f"Symbol/timeframe: {args.symbol} {args.timeframe}")
    print(f"Strategy:         {strategy_name}")
    print(f"Completed bars:   {len(bars)}")
    print(f"Latest bar UTC:   {bars[-1].timestamp.isoformat()}")

    if candidate is None:
        print("Signal:           no new entry signal on the latest completed bar")
        return

    side, signal_bar = candidate
    age = datetime.now(timezone.utc) - signal_bar.timestamp
    max_signal_age = args.max_signal_age_minutes or bar_minutes * 2
    print(f"Signal:           {side.name} (rule-based setup, not a price forecast)")
    print(f"Signal bar UTC:   {signal_bar.timestamp.isoformat()}")
    if age < timedelta(0) or age > timedelta(minutes=max_signal_age):
        print(
            f"Order status:     blocked; signal age {age} exceeds "
            f"{max_signal_age} minutes"
        )
        return

    if args.stop_distance is not None:
        stop_distance = args.stop_distance
        stop_description = f"fixed {stop_distance:g}"
    else:
        atr = average_true_range(bars, args.atr_period)
        stop_distance = atr * args.atr_multiplier
        stop_description = (
            f"{args.atr_multiplier:g} x {args.atr_period}-bar ATR "
            f"({atr:.5f}) = {stop_distance:.5f}"
        )
    if args.max_spread is not None:
        max_spread = args.max_spread
    elif args.symbol.upper().startswith(("XAU", "GOLD")):
        max_spread = 0.50
    elif args.symbol.upper().startswith("EURUSD"):
        max_spread = 0.0003
    else:
        raise ValueError(
            "Set --max-spread explicitly for this symbol; the broker's spread "
            "is instrument-specific."
        )
    print(f"Stop distance:    {stop_description}")
    print(f"Spread cap:       {max_spread:g} {args.symbol} price units")

    api = _load_api()
    if not api.initialize(timeout=60_000):
        raise RuntimeError(f"Could not initialize MT5: {api.last_error()}")
    try:
        account = api.account_info()
        if account is None:
            raise RuntimeError(f"Could not read MT5 account: {api.last_error()}")
        expected_id = args.expected_account_id or account.login
        settings = DemoOrderSettings(
            expected_account_id=expected_id,
            risk_per_trade=args.risk_per_trade,
            max_daily_loss=args.max_daily_loss,
            max_volume=args.max_volume,
            max_spread=max_spread,
        )
        preview = prepare_demo_order(
            api,
            symbol=args.symbol,
            side=side,
            stop_distance=stop_distance,
            settings=settings,
            require_trade_permission=args.execute_demo,
        )
        print(f"Entry quote:      {preview.entry_price:.5f}")
        print(f"Protective stop:  {preview.stop_price:.5f}")
        print(f"Volume:           {preview.volume:.2f} lots")
        print(f"Estimated risk:   {preview.estimated_risk:.2f} account currency")
        print(f"Today's realized: {preview.daily_realized_pnl:.2f} account currency")
        if not args.execute_demo:
            print("Order status:     preview only; no order submitted")
            print(
                "To explicitly submit one demo order, add --execute-demo "
                f"--expected-account-id {account.login} --confirm "
                '"SUBMIT DEMO ORDER"'
            )
            return

        key_text = "|".join(
            (
                str(expected_id),
                args.symbol,
                args.timeframe,
                strategy_name,
                signal_bar.timestamp.isoformat(),
                side.name,
            )
        )
        key = hashlib.sha256(key_text.encode("utf-8")).hexdigest()
        attempt_file = Path("runs", "demo-order-attempts", f"{key}.json")
        outcome = submit_demo_order(
            api,
            preview,
            settings=settings,
            enabled=args.execute_demo,
            confirmation=args.confirm,
            idempotency_file=attempt_file,
            signal_id=signal_bar.timestamp.isoformat(),
        )
        print(f"Order status:     {outcome.status}")
        print(f"MT5 retcode:      {outcome.retcode} ({outcome.comment})")
        print(f"Order ticket:     {outcome.order_ticket}")
        print(f"Deal ticket:      {outcome.deal_ticket}")
        print(f"Filled volume:    {outcome.filled_volume:.2f}")
        print(f"Fill price:       {outcome.fill_price:.5f}")
        print(f"Attempt record:   {outcome.idempotency_file}")
    finally:
        api.shutdown()


def _load_api() -> Mt5TradingApi:
    try:
        return cast(Mt5TradingApi, import_module("MetaTrader5"))
    except ImportError as exc:
        raise RuntimeError(
            'MetaTrader5 is required; install it with: python -m pip install -e ".[mt5]"'
        ) from exc


def _print_all_strategy_scan(
    symbol: str,
    timeframe: str,
    bars: list[Bar],
    lookback: int,
) -> None:
    print(f"\nSymbol/timeframe: {symbol} {timeframe}")
    print(f"Completed bars:   {len(bars)}")
    print(f"Latest bar UTC:   {bars[-1].timestamp.isoformat()}")
    print("Strategy scan (rule-based setups, not price forecasts):")
    for name, previous, current in scan_latest_signals(
        bars,
        make_all_strategies(lookback),
    ):
        print(f"  {name:<26} {describe_scan_result(previous, current)}")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
