import argparse
from datetime import datetime, timedelta, timezone
from dataclasses import replace
import hashlib
from importlib import import_module
import math
from pathlib import Path
import sys
import time
from typing import Protocol, cast

from fxbot.consensus import (
    STRATEGY_IDS,
    consensus_side,
    count_strategy_votes,
    make_all_strategies,
    make_strategies,
    scan_latest_signals,
    trend_vote_counts,
)
from fxbot.data.finnhub import (
    EconomicEvent,
    FinnhubClient,
    NewsHeadline,
    high_impact_event_block,
)
from fxbot.data.mt5 import Mt5BarSource
from fxbot.execution.mt5_demo import (
    DemoOrderSettings,
    Mt5TradingApi,
    close_demo_position,
    prepare_demo_order,
    submit_demo_order,
)
from fxbot.indicators import average_true_range
from fxbot.models import Bar, Side


class _Position(Protocol):
    symbol: str
    magic: int
    type: int
    ticket: int
    sl: float
    volume: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Continuously trade configured strategy signals on an MT5 demo account."
    )
    parser.add_argument("--symbol", default="XAUUSD")
    parser.add_argument(
        "--symbols",
        help="Comma-separated MT5 symbols to scan, for example XAUUSD,EURUSD,GBPUSD",
    )
    parser.add_argument(
        "--timeframe",
        choices=("M5", "M15", "M30", "H1"),
        default="M5",
    )
    parser.add_argument(
        "--confirmation-timeframe",
        choices=("M15", "M30", "H1", "H4"),
        help="Higher timeframe for additional trend and signal confirmation",
    )
    parser.add_argument("--lookback", type=int, default=20)
    parser.add_argument(
        "--strategy-mode",
        choices=("consensus", "single"),
        default="consensus",
        help="Use selected-strategy consensus (default) or one strategy per account",
    )
    parser.add_argument(
        "--strategy",
        choices=STRATEGY_IDS,
        default="sma",
        help="Selected rule when --strategy-mode single is used",
    )
    parser.add_argument(
        "--strategies",
        default=",".join(STRATEGY_IDS),
        help="Enabled rule IDs for consensus, e.g. sma,donchian,ema,macd,rsi",
    )
    parser.add_argument("--consensus-votes", type=int, default=3)
    parser.add_argument("--risk-per-trade", type=float, default=0.001)
    parser.add_argument("--max-daily-loss", type=float, default=0.02)
    parser.add_argument("--max-volume", type=float, default=0.01)
    parser.add_argument("--max-open-positions", type=int)
    parser.add_argument("--max-total-open-risk", type=float, default=0.01)
    parser.add_argument(
        "--news-guard",
        action="store_true",
        help="Block new entries around high-impact Finnhub economic events",
    )
    parser.add_argument(
        "--max-spread",
        type=float,
        help="Maximum spread for a single symbol, in its price units",
    )
    parser.add_argument(
        "--max-spreads",
        help="Per-symbol limits, e.g. XAUUSD=0.50,EURUSD=0.0003",
    )
    parser.add_argument("--atr-period", type=int, default=14)
    parser.add_argument("--atr-multiplier", type=float, default=2.0)
    parser.add_argument(
        "--reward-risk-ratio",
        type=float,
        default=2.0,
        help="Take-profit reward as a multiple of stop risk (default: 2.0)",
    )
    parser.add_argument("--expected-account-id", type=int, required=True)
    parser.add_argument(
        "--run-demo-bot",
        action="store_true",
        help="Explicitly enable continuous demo order execution",
    )
    parser.add_argument(
        "--confirm",
        default="",
        help='Must equal "RUN DEMO BOT" to enable continuous execution',
    )
    parser.add_argument(
        "--kill-switch",
        type=Path,
        default=Path("runs", "DEMO_STOP"),
        help="Creating this file stops the bot at its next polling check",
    )
    args = parser.parse_args()
    if not args.run_demo_bot:
        parser.error("continuous execution is disabled; pass --run-demo-bot explicitly")
    if args.confirm != "RUN DEMO BOT":
        parser.error('--confirm must exactly equal "RUN DEMO BOT"')
    if args.expected_account_id <= 0:
        parser.error("--expected-account-id must be positive")
    if args.lookback < 2:
        parser.error("--lookback must be at least 2")
    strategy_ids = [item.strip() for item in args.strategies.split(",")]
    if (
        not strategy_ids
        or any(item not in STRATEGY_IDS for item in strategy_ids)
        or len(set(strategy_ids)) != len(strategy_ids)
    ):
        parser.error(f"--strategies must be unique IDs from {','.join(STRATEGY_IDS)}")
    if not 1 <= args.consensus_votes <= len(strategy_ids):
        parser.error("--consensus-votes must be 1 through the number of enabled strategies")
    if args.strategy_mode == "single" and args.strategy not in strategy_ids:
        parser.error("--strategy must be included in --strategies")
    args.strategy_ids = tuple(strategy_ids)
    default_confirmation = {
        "M5": "M15",
        "M15": "H1",
        "M30": "H1",
        "H1": "H4",
    }[args.timeframe]
    if args.confirmation_timeframe is None:
        args.confirmation_timeframe = default_confirmation
    if (
        Mt5BarSource.TIMEFRAME_MINUTES[args.confirmation_timeframe]
        <= Mt5BarSource.TIMEFRAME_MINUTES[args.timeframe]
    ):
        parser.error("--confirmation-timeframe must be higher than --timeframe")
    if (
        args.atr_period < 1
        or args.atr_multiplier <= 0
        or args.reward_risk_ratio <= 0
        or args.reward_risk_ratio > 10
    ):
        parser.error(
            "--atr-period and --atr-multiplier must be positive; "
            "--reward-risk-ratio must be greater than 0 and at most 10"
        )
    if args.max_spread is not None and args.max_spread <= 0:
        parser.error("--max-spread must be positive")
    if not 0 < args.max_total_open_risk <= 0.01:
        parser.error("--max-total-open-risk must be greater than 0 and at most 0.01")
    symbols = (
        [item.strip() for item in args.symbols.split(",")]
        if args.symbols
        else [args.symbol]
    )
    if any(not item for item in symbols):
        parser.error("--symbols must contain non-empty symbol names")
    if len(set(symbol.upper() for symbol in symbols)) != len(symbols):
        parser.error("--symbols must not contain duplicate instruments")
    if len(symbols) > 50:
        parser.error("no more than 50 symbols can be scanned in one process")
    spread_limits: dict[str, float] = {}
    if args.max_spreads:
        try:
            for item in args.max_spreads.split(","):
                name, value = item.split("=", maxsplit=1)
                limit = float(value)
                if not name.strip() or not math.isfinite(limit) or limit <= 0:
                    raise ValueError
                spread_limits[name.strip().upper()] = limit
        except ValueError:
            parser.error("--max-spreads must contain SYMBOL=positive-number entries")
    if args.max_spread is not None and len(symbols) > 1:
        parser.error("use --max-spreads with one limit per symbol when scanning multiple symbols")
    for symbol in symbols:
        key = symbol.upper()
        if key not in spread_limits and args.max_spread is None:
            if key.startswith(("XAU", "GOLD")):
                spread_limits[key] = 0.50
            elif key.startswith("EURUSD"):
                spread_limits[key] = 0.0003
            elif len(symbols) > 1:
                parser.error(f"provide --max-spreads for {symbol}")
            else:
                parser.error(f"provide --max-spread for {symbol}")
    args.spread_limits = spread_limits
    if args.max_open_positions is None:
        args.max_open_positions = min(10, len(symbols))
    if args.max_open_positions < 1 or args.max_open_positions > 10:
        parser.error("--max-open-positions must be between 1 and 10")
    if args.max_open_positions > len(symbols):
        parser.error("--max-open-positions cannot exceed the number of configured symbols")
    args.symbols = symbols
    return args


def _max_spread(args: argparse.Namespace, symbol: str) -> float:
    if hasattr(args, "spread_limits") and symbol.upper() in args.spread_limits:
        return args.spread_limits[symbol.upper()]
    if args.max_spread is not None:
        return args.max_spread
    if symbol.upper().startswith(("XAU", "GOLD")):
        return 0.50
    if symbol.upper().startswith("EURUSD"):
        return 0.0003
    raise ValueError(
        f"Set a broker-specific --max-spread for {symbol}."
    )


def _symbol_state_path(folder: str, symbol: str) -> Path:
    key = hashlib.sha256(symbol.encode("utf-8")).hexdigest()[:16]
    return Path("runs", folder, f"{key}.marker")


def _check_account(api: Mt5TradingApi, expected_account_id: int) -> None:
    account = api.account_info()
    if account is None:
        raise RuntimeError(f"Could not read MT5 account: {api.last_error()}")
    if account.trade_mode != api.ACCOUNT_TRADE_MODE_DEMO:
        raise RuntimeError("Continuous order execution is hard-blocked unless MT5 reports DEMO")
    if account.login != expected_account_id:
        raise RuntimeError(
            f"MT5 login {account.login} does not match expected demo login "
            f"{expected_account_id}"
        )


def _is_retryable_data_error(error: RuntimeError) -> bool:
    message = str(error)
    return message.startswith(
        (
            "Could not initialize MetaTrader 5:",
            "Could not load MT5 rates:",
        )
    )


def _managed_position(
    api: Mt5TradingApi,
    *,
    symbol: str,
    magic_number: int,
    allowed_symbols: list[str],
) -> _Position | None:
    positions = api.positions_get()
    if positions is None:
        raise RuntimeError(f"Could not check MT5 positions: {api.last_error()}")
    if not positions:
        return None
    matches: list[_Position] = []
    for raw_position in positions:
        position = cast(_Position, raw_position)
        if (
            not all(
                hasattr(position, field)
                for field in ("symbol", "magic", "type", "ticket", "sl", "volume")
            )
            or position.magic != magic_number
            or position.symbol not in allowed_symbols
            or position.type not in (api.ORDER_TYPE_BUY, api.ORDER_TYPE_SELL)
            or not isinstance(position.ticket, int)
            or position.ticket <= 0
            or not math.isfinite(position.volume)
            or position.volume <= 0
            or not math.isfinite(position.sl)
            or position.sl <= 0
        ):
            raise RuntimeError(
                "An account position is foreign, incomplete, or lacks a protective "
                "stop; bot stopped without changing it"
            )
        if position.symbol == symbol:
            matches.append(position)
    if len(matches) > 1:
        raise RuntimeError(
            f"Multiple positions exist for {symbol}; bot stopped for manual review"
        )
    return matches[0] if matches else None


def run_bar(
    api: Mt5TradingApi,
    *,
    bars: list[Bar],
    confirmation_bars: list[Bar],
    args: argparse.Namespace,
    settings: DemoOrderSettings,
    enter_allowed: bool,
    entry_block_reason: str | None = None,
    neutral_wait_file: Path = Path("runs", "DEMO_WAIT_FOR_NEUTRAL"),
    active_position_file: Path = Path("runs", "DEMO_POSITION_ACTIVE"),
) -> None:
    strategy_ids = getattr(args, "strategy_ids", STRATEGY_IDS)
    strategies = make_strategies(
        lookback=args.lookback,
        strategy_ids=strategy_ids,
    )
    results = scan_latest_signals(bars, strategies)
    threshold = getattr(args, "consensus_votes", 3)
    strategy_mode = getattr(args, "strategy_mode", "consensus")
    if strategy_mode == "single":
        selected = args.strategy
        selected_result = next(result for result in results if result[0] == {
            "sma": "SMA 10/30 trend",
            "donchian": f"Donchian {args.lookback} breakout",
            "ema": "EMA 12/26 trend",
            "macd": "MACD 12/26/9 trend",
            "rsi": "RSI 14 mean reversion",
        }[selected])
        long_votes = int(selected_result[2] == Side.LONG)
        short_votes = int(selected_result[2] == Side.SHORT)
        desired = selected_result[2]
        threshold = 1
    else:
        long_votes, short_votes = count_strategy_votes(results)
        desired = consensus_side(long_votes, short_votes, threshold)
    signal_bar = bars[-1]
    age = datetime.now(timezone.utc) - signal_bar.timestamp
    max_age = timedelta(minutes=2 * Mt5BarSource.TIMEFRAME_MINUTES[args.timeframe])
    confirmation_results = scan_latest_signals(
        confirmation_bars,
        make_strategies(
            lookback=args.lookback,
            strategy_ids=strategy_ids,
        ),
    )
    confirmation_long, confirmation_short = count_strategy_votes(
        confirmation_results
    )
    confirmation_threshold = (
        1 if strategy_mode == "single" else min(threshold, len(confirmation_results))
    )
    confirmation_side = consensus_side(
        confirmation_long,
        confirmation_short,
        confirmation_threshold,
    )
    trend_long, trend_short = trend_vote_counts(confirmation_results)
    confirmation_trend = consensus_side(
        trend_long,
        trend_short,
        min(threshold, max(1, len(confirmation_results) - 1)),
    )
    confirmation_bar = confirmation_bars[-1]
    confirmation_age = datetime.now(timezone.utc) - confirmation_bar.timestamp
    confirmation_duration = timedelta(
        minutes=Mt5BarSource.TIMEFRAME_MINUTES[args.confirmation_timeframe]
    )
    confirmation_fresh = (
        timedelta(0) <= confirmation_age <= confirmation_duration * 2
        and confirmation_bar.timestamp <= signal_bar.timestamp
    )
    print(
        f"\nCompleted bar: {signal_bar.timestamp.isoformat()} | "
        f"{strategy_mode} LONG {long_votes}/{len(results)}, "
        f"SHORT {short_votes}/{len(results)} (threshold {threshold})"
    )
    for name, previous, current in results:
        print(f"  {name:<26} {current.name}")
    print(
        f"Confirmation {args.confirmation_timeframe}: "
        f"{confirmation_bar.timestamp.isoformat()} UTC | "
        f"signal LONG {confirmation_long}/{len(confirmation_results)}, "
        f"SHORT {confirmation_short}/{len(confirmation_results)}; "
        f"trend LONG {trend_long}/{max(0, len(confirmation_results) - 1)}, "
        f"SHORT {trend_short}/{max(0, len(confirmation_results) - 1)}"
    )

    if age < timedelta(0) or age > max_age:
        print(f"Action: skipped; latest bar is stale ({age})")
        return

    account = api.account_info()
    if account is None:
        raise RuntimeError(f"Could not read MT5 account: {api.last_error()}")
    if account.trade_mode != api.ACCOUNT_TRADE_MODE_DEMO:
        raise RuntimeError("Continuous order execution is hard-blocked unless MT5 reports DEMO")
    if account.login != settings.expected_account_id:
        raise RuntimeError("MT5 account changed; expected demo login does not match")

    position = _managed_position(
        api,
        symbol=args.symbol,
        magic_number=settings.magic_number,
        allowed_symbols=args.symbols,
    )
    if position is not None:
        _write_marker(active_position_file)
    elif active_position_file.exists():
        active_position_file.unlink()
        _write_marker(neutral_wait_file)

    if position is not None:
        held_side = Side.LONG if position.type == api.ORDER_TYPE_BUY else Side.SHORT
        opposing_votes = short_votes if held_side == Side.LONG else long_votes
        print(
            f"Position: {held_side.name}, {position.volume:.2f} lots; "
            f"opposing strategy votes {opposing_votes}/{len(results)}"
        )
        if opposing_votes < threshold:
            print("Action: hold; broker protective stop remains active")
            return
        key = hashlib.sha256(
            (
                f"close|{settings.expected_account_id}|{args.symbol}|"
                f"{args.timeframe}|{position.ticket}|{signal_bar.timestamp.isoformat()}"
            ).encode("utf-8")
        ).hexdigest()
        outcome = close_demo_position(
            api,
            symbol=args.symbol,
            settings=settings,
            idempotency_file=Path("runs", "demo-close-attempts", f"{key}.json"),
            signal_id=signal_bar.timestamp.isoformat(),
        )
        print(f"Exit status: {outcome.status}; {outcome.comment}")
        if outcome.status != "FILLED":
            raise RuntimeError(
                "Position close did not fully fill; bot stopped for manual review"
            )
        active_position_file.unlink(missing_ok=True)
        _write_marker(neutral_wait_file)
        return

    if neutral_wait_file.exists():
        if desired == Side.FLAT:
            neutral_wait_file.unlink()
            print(
                "Action: consensus neutralized; new entries may be considered "
                "from the next completed bar"
            )
        else:
            print(
                "Action: waiting for strategy consensus to neutralize before "
                "allowing another entry"
            )
        return

    if not enter_allowed:
        print("Action: waiting for the next completed bar before a new entry")
        return
    if desired == Side.FLAT:
        print(
            f"Action: no entry; need {threshold} matching vote(s) "
            f"from {len(results)} enabled strategy rules"
        )
        return
    if entry_block_reason:
        print(f"Action: entry blocked; {entry_block_reason}")
        return
    if not confirmation_fresh:
        print(
            f"Action: entry blocked; {args.confirmation_timeframe} "
            f"confirmation is stale or newer than the M5/M15 signal bar"
        )
        return
    if confirmation_side != desired or confirmation_trend != desired:
        print(
            "Action: entry blocked; higher-timeframe signal and trend must "
            f"both match {desired.name} (signal {confirmation_side.name}, "
            f"trend {confirmation_trend.name})"
        )
        return

    stop_distance = average_true_range(bars, args.atr_period) * args.atr_multiplier
    preview = prepare_demo_order(
        api,
        symbol=args.symbol,
        side=desired,
        stop_distance=stop_distance,
        settings=settings,
    )
    print(
        f"Entry: {desired.name}, {preview.volume:.2f} lots at "
        f"{preview.entry_price:.5f}; stop {preview.stop_price:.5f}; "
        f"target {float(preview.request['tp']):.5f} "
        f"({settings.reward_risk_ratio:g}R); "
        f"estimated risk {preview.estimated_risk:.2f}"
    )
    key = hashlib.sha256(
        (
            f"entry|{settings.expected_account_id}|{args.symbol}|"
            f"{args.timeframe}|{signal_bar.timestamp.isoformat()}|{desired.name}"
        ).encode("utf-8")
    ).hexdigest()
    _write_marker(active_position_file)
    outcome = submit_demo_order(
        api,
        preview,
        settings=settings,
        enabled=True,
        confirmation="SUBMIT DEMO ORDER",
        idempotency_file=Path("runs", "demo-order-attempts", f"{key}.json"),
        signal_id=signal_bar.timestamp.isoformat(),
    )
    print(f"Entry status: {outcome.status}; {outcome.comment}")
    if outcome.status != "FILLED":
        raise RuntimeError(
            "Demo entry did not fully fill; bot stopped for manual review"
        )


def main() -> None:
    args = parse_args()
    if args.kill_switch.exists():
        raise RuntimeError(f"Kill switch is engaged: {args.kill_switch}")
    news_client = FinnhubClient.from_environment() if args.news_guard else None
    settings = DemoOrderSettings(
        expected_account_id=args.expected_account_id,
        risk_per_trade=args.risk_per_trade,
        max_daily_loss=args.max_daily_loss,
        max_volume=args.max_volume,
        max_spread=_max_spread(args, args.symbols[0]),
        reward_risk_ratio=args.reward_risk_ratio,
        max_open_positions=args.max_open_positions,
        max_total_open_risk=args.max_total_open_risk,
    )
    api = _load_api()
    last_bar_time: dict[str, datetime] = {}
    print(
        f"Starting continuous DEMO bot: {len(args.symbols)} symbols "
        f"{args.timeframe}; "
        f"{args.strategy_mode} strategy ({len(args.strategy_ids)} rules, "
        f"threshold {args.consensus_votes}) + {args.confirmation_timeframe} "
        "trend/signal confirmation, Ctrl+C or kill-switch file to stop."
    )
    if args.news_guard:
        print(
            "News guard: high-impact economic events block new entries "
            "30 minutes before through 15 minutes after; headlines are context only."
        )
    print(
        f"Limits: risk {settings.risk_per_trade:.2%}, daily loss "
        f"{settings.max_daily_loss:.2%}, max volume {settings.max_volume:g}, "
        f"max open positions {settings.max_open_positions}, total open risk "
        f"{settings.max_total_open_risk:.2%}, "
        f"ATR stop {args.atr_multiplier:g}x{args.atr_period}, "
        f"target {args.reward_risk_ratio:g}R, "
        f"spread cap {settings.max_spread:g}."
    )
    last_printed_headlines: tuple[NewsHeadline, ...] | None = None
    try:
        while True:
            if args.kill_switch.exists():
                print(f"Kill switch engaged at {args.kill_switch}; stopping bot.")
                return

            calendar_events: tuple[EconomicEvent, ...] = ()
            headlines: tuple[NewsHeadline, ...] = ()
            news_error: str | None = None
            if news_client is not None:
                try:
                    calendar_events = news_client.economic_events()
                    headlines = news_client.market_news()
                except RuntimeError as exc:
                    news_error = str(exc)
                    print(
                        f"Finnhub data unavailable: {news_error}. "
                        "New entries will be blocked; existing positions can still exit.",
                        flush=True,
                    )
                else:
                    if headlines != last_printed_headlines:
                        for headline in headlines:
                            print(
                                f"News context [{headline.source}]: {headline.headline}"
                            )
                        last_printed_headlines = headlines

            for symbol in args.symbols:
                symbol_args = argparse.Namespace(**vars(args))
                symbol_args.symbol = symbol
                now = datetime.now(timezone.utc)
                try:
                    bars = Mt5BarSource(
                        symbol=symbol,
                        timeframe=args.timeframe,
                    ).load(now - timedelta(days=60), now)
                    confirmation_bars = Mt5BarSource(
                        symbol=symbol,
                        timeframe=args.confirmation_timeframe,
                    ).load(now - timedelta(days=60), now)
                except RuntimeError as exc:
                    if not _is_retryable_data_error(exc):
                        print(f"{symbol}: data unavailable; no order: {exc}", flush=True)
                        continue
                    print(
                        f"\n{symbol}: MT5 data connection failed: {exc}\n"
                        "No order was placed. Check MT5 login/server and live "
                        "quotes; retrying this symbol next cycle.",
                        flush=True,
                    )
                    continue

                current_bar_time = bars[-1].timestamp
                previous_bar_time = last_bar_time.get(symbol)
                is_new_bar = (
                    previous_bar_time is not None
                    and current_bar_time > previous_bar_time
                )
                duration = timedelta(
                    minutes=Mt5BarSource.TIMEFRAME_MINUTES[args.timeframe]
                )
                if previous_bar_time is None or is_new_bar:
                    if not api.initialize(timeout=60_000):
                        print(
                            f"\n{symbol}: MT5 trading connection failed: "
                            f"{api.last_error()}; no order placed, retrying next cycle.",
                            flush=True,
                        )
                        continue
                    try:
                        _check_account(api, args.expected_account_id)
                        symbol_settings = replace(
                            settings,
                            max_spread=_max_spread(args, symbol),
                        )
                        run_bar(
                            api,
                            bars=bars,
                            confirmation_bars=confirmation_bars,
                            args=symbol_args,
                            settings=symbol_settings,
                            enter_allowed=is_new_bar,
                            entry_block_reason=_news_entry_block(
                                symbol,
                                calendar_events,
                                news_error=news_error,
                                enabled=news_client is not None,
                            ),
                            neutral_wait_file=_symbol_state_path(
                                "DEMO_WAIT_FOR_NEUTRAL", symbol
                            ),
                            active_position_file=_symbol_state_path(
                                "DEMO_POSITION_ACTIVE", symbol
                            ),
                        )
                    finally:
                        api.shutdown()
                    last_bar_time[symbol] = current_bar_time
                else:
                    age = datetime.now(timezone.utc) - (current_bar_time + duration)
                    if age > duration:
                        print(
                            f"\n{symbol}: latest completed bar is stale ({age}); "
                            f"MT5 reports {current_bar_time.isoformat()} UTC. "
                            "No orders will be placed until fresh bars arrive.",
                            flush=True,
                        )
                    else:
                        print(
                            f"{symbol}: waiting for next {args.timeframe} bar; "
                            f"latest completed {current_bar_time.isoformat()} UTC"
                        )
            if _wait_with_kill_switch(args.kill_switch, 30.0):
                return
    except KeyboardInterrupt:
        print("\nStopped by user. Any open position retains its broker stop-loss.")


def _load_api() -> Mt5TradingApi:
    try:
        return cast(Mt5TradingApi, import_module("MetaTrader5"))
    except ImportError as exc:
        raise RuntimeError(
            'MetaTrader5 is required; install it with: python -m pip install -e ".[mt5]"'
        ) from exc


def _news_entry_block(
    symbol: str,
    events: tuple[EconomicEvent, ...],
    *,
    news_error: str | None,
    enabled: bool,
) -> str | None:
    if not enabled:
        return None
    if news_error is not None:
        return "Finnhub calendar unavailable; " + news_error
    try:
        event = high_impact_event_block(symbol, events)
    except ValueError as exc:
        return f"calendar mapping unavailable ({exc})"
    if event is not None:
        return event
    return None


def _write_marker(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(datetime.now(timezone.utc).isoformat() + "\n", encoding="utf-8")


def _wait_with_kill_switch(kill_switch: Path, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while True:
        if kill_switch.exists():
            print(f"\nKill switch engaged at {kill_switch}; stopping bot.")
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(30.0, remaining))


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
