from dataclasses import dataclass
from datetime import datetime, time, timezone
from decimal import Decimal
import json
import math
from pathlib import Path
from typing import Protocol, cast

from fxbot.models import Side


class _AccountInfo(Protocol):
    trade_mode: int
    login: int
    balance: float
    equity: float
    trade_allowed: bool


class _TerminalInfo(Protocol):
    trade_allowed: bool


class _SymbolInfo(Protocol):
    visible: bool
    trade_mode: int
    point: float
    digits: int
    trade_stops_level: int
    volume_min: float
    volume_max: float
    volume_step: float
    filling_mode: int


class _Tick(Protocol):
    bid: float
    ask: float


class _CheckResult(Protocol):
    retcode: int
    comment: str


class _OrderResult(Protocol):
    retcode: int
    comment: str
    order: int
    deal: int
    volume: float
    price: float


class _Deal(Protocol):
    profit: float
    commission: float
    swap: float
    fee: float
    entry: int


class _Position(Protocol):
    ticket: int
    symbol: str
    type: int
    volume: float
    magic: int
    sl: float
    price_open: float


class Mt5TradingApi(Protocol):
    ACCOUNT_TRADE_MODE_DEMO: int
    DEAL_ENTRY_OUT: int
    DEAL_ENTRY_OUT_BY: int
    DEAL_ENTRY_INOUT: int
    SYMBOL_TRADE_MODE_DISABLED: int
    ORDER_FILLING_FOK: int
    ORDER_FILLING_IOC: int
    ORDER_FILLING_RETURN: int
    ORDER_TYPE_BUY: int
    ORDER_TYPE_SELL: int
    TRADE_ACTION_DEAL: int
    ORDER_TIME_GTC: int
    TRADE_RETCODE_DONE: int
    TRADE_RETCODE_DONE_PARTIAL: int

    def initialize(self, *, timeout: int) -> bool: ...
    def shutdown(self) -> None: ...
    def account_info(self) -> _AccountInfo | None: ...
    def terminal_info(self) -> _TerminalInfo | None: ...
    def symbol_info(self, symbol: str) -> _SymbolInfo | None: ...
    def symbol_select(self, symbol: str, enable: bool) -> bool: ...
    def symbol_info_tick(self, symbol: str) -> _Tick | None: ...
    def positions_get(self) -> tuple[object, ...] | None: ...
    def history_deals_get(self, date_from: datetime, date_to: datetime) -> tuple[_Deal, ...] | None: ...
    def order_calc_profit(self, order_type: int, symbol: str, volume: float, price_open: float, price_close: float) -> float | None: ...
    def order_check(self, request: dict[str, object]) -> _CheckResult | None: ...
    def order_send(self, request: dict[str, object]) -> _OrderResult | None: ...
    def last_error(self) -> object: ...


@dataclass(frozen=True)
class DemoOrderSettings:
    expected_account_id: int
    risk_per_trade: float = 0.001
    max_daily_loss: float = 0.02
    max_volume: float = 0.01
    max_spread: float = 0.0003
    reward_risk_ratio: float = 2.0
    max_open_positions: int = 10
    max_total_open_risk: float = 0.01
    deviation_points: int = 20
    magic_number: int = 2601021
    max_risk_per_trade: float = 0.0025

    def __post_init__(self) -> None:
        if self.expected_account_id <= 0:
            raise ValueError("expected_account_id must be a positive MT5 login")
        if not 0 < self.risk_per_trade <= self.max_risk_per_trade:
            raise ValueError(
                f"risk_per_trade must be greater than 0 and at most "
                f"{self.max_risk_per_trade:.2%} for this demo executor"
            )
        if not 0 < self.max_daily_loss <= 0.05:
            raise ValueError("max_daily_loss must be greater than 0 and at most 5%")
        if not math.isfinite(self.max_volume) or self.max_volume <= 0:
            raise ValueError("max_volume must be finite and positive")
        if not math.isfinite(self.max_spread) or self.max_spread <= 0:
            raise ValueError("max_spread must be finite and positive")
        if (
            not math.isfinite(self.reward_risk_ratio)
            or not 0 < self.reward_risk_ratio <= 10
        ):
            raise ValueError("reward_risk_ratio must be finite, positive, and at most 10")
        if self.deviation_points < 0:
            raise ValueError("deviation_points must not be negative")
        if not 1 <= self.max_open_positions <= 10:
            raise ValueError("max_open_positions must be between 1 and 10")
        if (
            not math.isfinite(self.max_total_open_risk)
            or not 0 < self.max_total_open_risk <= 0.01
        ):
            raise ValueError("max_total_open_risk must be positive and at most 1%")


@dataclass(frozen=True)
class DemoOrderPreview:
    request: dict[str, object]
    side: Side
    symbol: str
    volume: float
    entry_price: float
    stop_price: float
    estimated_risk: float
    daily_realized_pnl: float
    stop_distance: float


@dataclass(frozen=True)
class DemoOrderOutcome:
    status: str
    retcode: int
    comment: str
    order_ticket: int
    deal_ticket: int
    filled_volume: float
    fill_price: float
    idempotency_file: str


def prepare_demo_order(
    api: Mt5TradingApi,
    *,
    symbol: str,
    side: Side,
    stop_distance: float,
    settings: DemoOrderSettings,
    require_trade_permission: bool = True,
) -> DemoOrderPreview:
    """Validate demo account and build a bounded, stop-protected entry request."""
    if side not in (Side.LONG, Side.SHORT):
        raise ValueError("only LONG or SHORT entry signals can be prepared")
    if not symbol.strip():
        raise ValueError("symbol must not be empty")
    if not math.isfinite(stop_distance) or stop_distance <= 0:
        raise ValueError("stop_distance must be finite and positive")

    account = _require_demo_account(api, settings.expected_account_id)
    terminal = api.terminal_info()
    if terminal is None:
        raise RuntimeError(f"Could not read MT5 terminal status: {api.last_error()}")
    if require_trade_permission and (not terminal.trade_allowed or not account.trade_allowed):
        raise RuntimeError(
            "MT5 terminal or account has trading disabled. For demo execution, "
            "enable Algo Trading in MT5 and check the account's trading permissions."
        )
    positions = _get_all_managed_positions(api, settings.magic_number)
    if len(positions) >= settings.max_open_positions:
        raise RuntimeError(
            f"Demo entry blocked: maximum of {settings.max_open_positions} "
            "open bot positions reached"
        )
    if any(position.symbol == symbol for position in positions):
        raise RuntimeError(
            f"Demo entry blocked: the bot already has an open {symbol} position"
        )
    open_risk = _total_open_risk(api, positions)

    day_start = datetime.combine(
        datetime.now(timezone.utc).date(),
        time.min,
        tzinfo=timezone.utc,
    )
    deals = api.history_deals_get(day_start, datetime.now(timezone.utc))
    if deals is None:
        raise RuntimeError(f"Could not check today's account PnL: {api.last_error()}")
    daily_pnl = sum(
        _finite_closed_deal_net(deal)
        for deal in deals
        if _is_closed_deal(api, deal)
    )
    start_of_day_balance = account.balance - daily_pnl
    if start_of_day_balance <= 0:
        raise RuntimeError(
            "Could not establish a positive UTC day-start balance from "
            "closed deals; verify MT5 account history"
        )
    if daily_pnl <= -(start_of_day_balance * settings.max_daily_loss):
        raise RuntimeError(
            f"Daily loss limit reached: today's realized PnL is {daily_pnl:.2f}"
        )

    info = api.symbol_info(symbol)
    if info is None:
        raise RuntimeError(f"MT5 does not know symbol {symbol!r}: {api.last_error()}")
    if not info.visible and not api.symbol_select(symbol, True):
        raise RuntimeError(f"Could not select MT5 symbol {symbol!r}: {api.last_error()}")
    if info.trade_mode == api.SYMBOL_TRADE_MODE_DISABLED:
        raise RuntimeError(f"MT5 symbol {symbol!r} is not enabled for trading")

    tick = api.symbol_info_tick(symbol)
    if tick is None or not _is_positive_finite(tick.bid) or not _is_positive_finite(tick.ask):
        raise RuntimeError(f"MT5 returned an invalid quote for {symbol!r}: {api.last_error()}")
    spread = tick.ask - tick.bid
    if spread <= 0 or spread > settings.max_spread:
        raise RuntimeError(
            f"Current spread {spread:.8f} exceeds the configured maximum "
            f"{settings.max_spread:.8f}"
        )
    if not _is_positive_finite(info.point) or info.digits < 0:
        raise RuntimeError("MT5 returned invalid price precision for the symbol")

    order_type = api.ORDER_TYPE_BUY if side == Side.LONG else api.ORDER_TYPE_SELL
    entry_price = tick.ask if side == Side.LONG else tick.bid
    raw_stop = entry_price - int(side) * stop_distance
    stop_price = round(raw_stop, info.digits)
    minimum_stop_distance = max(0, info.trade_stops_level) * info.point
    stop_distance_from_market = (
        tick.bid - stop_price if side == Side.LONG else stop_price - tick.ask
    )
    if stop_distance_from_market < minimum_stop_distance:
        raise RuntimeError(
            f"Stop is closer than the broker minimum ({minimum_stop_distance:.8f})"
        )
    if stop_price <= 0:
        raise RuntimeError("Calculated stop price is invalid")
    take_profit = round(
        entry_price + int(side) * stop_distance * settings.reward_risk_ratio,
        info.digits,
    )
    take_profit_distance_from_market = (
        take_profit - tick.bid if side == Side.LONG else tick.ask - take_profit
    )
    if take_profit <= 0 or take_profit_distance_from_market < minimum_stop_distance:
        raise RuntimeError(
            f"Take-profit is closer than the broker minimum ({minimum_stop_distance:.8f})"
        )

    loss_per_lot = api.order_calc_profit(
        order_type,
        symbol,
        1.0,
        entry_price,
        stop_price,
    )
    if loss_per_lot is None or not math.isfinite(loss_per_lot) or loss_per_lot >= 0:
        raise RuntimeError(
            f"MT5 could not calculate a valid loss at stop for {symbol!r}: {api.last_error()}"
        )
    if not _is_positive_finite(account.equity):
        raise RuntimeError("MT5 account equity is invalid")
    total_risk_budget = account.equity * settings.max_total_open_risk
    remaining_risk_budget = total_risk_budget - open_risk
    risk_budget = min(account.equity * settings.risk_per_trade, remaining_risk_budget)
    if not _is_positive_finite(risk_budget):
        raise RuntimeError(
            "Demo entry blocked: the portfolio open-risk cap has been reached"
        )

    volume_step = info.volume_step
    if not _is_positive_finite(volume_step) or not _is_positive_finite(info.volume_min):
        raise RuntimeError("MT5 returned invalid symbol volume limits")
    broker_volume_max = info.volume_max
    if not _is_positive_finite(broker_volume_max):
        raise RuntimeError("MT5 returned invalid maximum volume")
    volume_cap = min(settings.max_volume, broker_volume_max)
    raw_volume = min(risk_budget / abs(loss_per_lot), volume_cap)
    volume = _round_volume_down(raw_volume, volume_step)
    if volume < info.volume_min:
        raise RuntimeError(
            f"Risk-based volume {volume:.8f} is below broker minimum "
            f"{info.volume_min:.8f}; refusing to round risk upward"
        )
    estimated_risk = abs(loss_per_lot) * volume
    if estimated_risk > risk_budget + max(1e-8, risk_budget * 1e-9):
        raise RuntimeError("Rounded order volume exceeds the configured risk budget")
    if open_risk + estimated_risk > total_risk_budget + max(1e-8, total_risk_budget * 1e-9):
        raise RuntimeError("Order would exceed the configured total open-risk cap")

    request: dict[str, object] = {
        "action": api.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": volume,
        "type": order_type,
        "price": round(entry_price, info.digits),
        "sl": stop_price,
        "tp": take_profit,
        "deviation": settings.deviation_points,
        "magic": settings.magic_number,
        "comment": "fxbot demo portfolio",
        "type_time": api.ORDER_TIME_GTC,
        "type_filling": _filling_mode(api, info.filling_mode),
    }
    return DemoOrderPreview(
        request=request,
        side=side,
        symbol=symbol,
        volume=volume,
        entry_price=round(entry_price, info.digits),
        stop_price=stop_price,
        estimated_risk=estimated_risk,
        daily_realized_pnl=daily_pnl,
        stop_distance=stop_distance,
    )


def submit_demo_order(
    api: Mt5TradingApi,
    preview: DemoOrderPreview,
    *,
    settings: DemoOrderSettings,
    enabled: bool,
    confirmation: str,
    idempotency_file: str | Path,
    signal_id: str,
) -> DemoOrderOutcome:
    """Submit exactly one demo entry after explicit opt-in and preflight checks."""
    if not enabled:
        raise RuntimeError("Demo order execution is disabled; pass the explicit opt-in flag")
    if confirmation != "SUBMIT DEMO ORDER":
        raise RuntimeError('Order not submitted; confirmation must be exactly "SUBMIT DEMO ORDER"')

    preview = prepare_demo_order(
        api,
        symbol=preview.symbol,
        side=preview.side,
        stop_distance=preview.stop_distance,
        settings=settings,
    )
    check = api.order_check(preview.request)
    if check is None:
        raise RuntimeError(f"MT5 order_check failed: {api.last_error()}")
    if check.retcode != 0:
        raise RuntimeError(f"MT5 rejected order preflight ({check.retcode}): {check.comment}")

    marker = Path(idempotency_file)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker_payload = {
        "status": "SUBMISSION_ATTEMPTED",
        "account_id": settings.expected_account_id,
        "symbol": preview.symbol,
        "side": preview.side.name,
        "signal_id": signal_id,
        "volume": preview.volume,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    try:
        with marker.open("x", encoding="utf-8") as output:
            json.dump(marker_payload, output, indent=2)
            output.write("\n")
    except FileExistsError as exc:
        raise RuntimeError(
            "This signal already has an order-attempt marker; inspect the marker "
            "and MT5 account before any retry"
        ) from exc

    # Recheck the account immediately before the external side effect.
    _require_demo_account(api, settings.expected_account_id)
    terminal = api.terminal_info()
    account = api.account_info()
    tick = api.symbol_info_tick(preview.symbol)
    if (
        terminal is None
        or not terminal.trade_allowed
        or account is None
        or not account.trade_allowed
    ):
        raise RuntimeError("MT5 terminal or account no longer permits trading")
    current_positions = _get_all_managed_positions(api, settings.magic_number)
    if len(current_positions) >= settings.max_open_positions:
        raise RuntimeError("Order aborted because the portfolio position limit was reached")
    if any(position.symbol == preview.symbol for position in current_positions):
        raise RuntimeError("Order aborted because a position in this symbol appeared")
    if (
        _total_open_risk(api, current_positions) + preview.estimated_risk
        > account.equity * settings.max_total_open_risk
        + max(1e-8, account.equity * settings.max_total_open_risk * 1e-9)
    ):
        raise RuntimeError("Order aborted because the portfolio open-risk cap was reached")
    if (
        tick is None
        or not _is_positive_finite(tick.bid)
        or not _is_positive_finite(tick.ask)
        or tick.ask <= tick.bid
        or tick.ask - tick.bid > settings.max_spread
    ):
        raise RuntimeError("Order aborted because the latest quote is invalid or spread exceeded the cap")
    order_result = api.order_send(preview.request)
    if order_result is None:
        raise RuntimeError(
            "MT5 returned no order result. The attempt marker was retained; "
            "inspect the terminal before retrying."
        )

    status = (
        "FILLED"
        if order_result.retcode == api.TRADE_RETCODE_DONE
        else "PARTIALLY_FILLED"
        if order_result.retcode == api.TRADE_RETCODE_DONE_PARTIAL
        else "REJECTED"
    )
    outcome = DemoOrderOutcome(
        status=status,
        retcode=order_result.retcode,
        comment=order_result.comment,
        order_ticket=order_result.order,
        deal_ticket=order_result.deal,
        filled_volume=order_result.volume,
        fill_price=order_result.price,
        idempotency_file=str(marker),
    )
    marker_payload.update(
        {
            "status": status,
            "retcode": outcome.retcode,
            "comment": outcome.comment,
            "order_ticket": outcome.order_ticket,
            "deal_ticket": outcome.deal_ticket,
            "filled_volume": outcome.filled_volume,
            "fill_price": outcome.fill_price,
        }
    )
    _replace_json(marker, marker_payload)
    return outcome


def close_demo_position(
    api: Mt5TradingApi,
    *,
    symbol: str,
    settings: DemoOrderSettings,
    idempotency_file: str | Path,
    signal_id: str,
) -> DemoOrderOutcome:
    """Close the bot's one stop-protected demo position without opening a hedge."""
    account = _require_demo_account(api, settings.expected_account_id)
    terminal = api.terminal_info()
    if terminal is None:
        raise RuntimeError(f"Could not read MT5 terminal status: {api.last_error()}")
    if not terminal.trade_allowed or not account.trade_allowed:
        raise RuntimeError("MT5 terminal or account does not permit demo position closing")

    position = _get_managed_position(api, symbol, settings.magic_number)
    info = api.symbol_info(symbol)
    if info is None:
        raise RuntimeError(f"MT5 does not know symbol {symbol!r}: {api.last_error()}")
    if info.trade_mode == api.SYMBOL_TRADE_MODE_DISABLED:
        raise RuntimeError(f"MT5 symbol {symbol!r} is not enabled for trading")
    tick = api.symbol_info_tick(symbol)
    if (
        tick is None
        or not _is_positive_finite(tick.bid)
        or not _is_positive_finite(tick.ask)
        or tick.ask <= tick.bid
    ):
        raise RuntimeError(f"MT5 returned an invalid quote for {symbol!r}: {api.last_error()}")

    is_long = position.type == api.ORDER_TYPE_BUY
    close_type = api.ORDER_TYPE_SELL if is_long else api.ORDER_TYPE_BUY
    close_price = tick.bid if is_long else tick.ask
    request: dict[str, object] = {
        "action": api.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "position": position.ticket,
        "volume": position.volume,
        "type": close_type,
        "price": round(close_price, info.digits),
        "deviation": settings.deviation_points,
        "magic": settings.magic_number,
        "comment": "fxbot demo consensus exit",
        "type_time": api.ORDER_TIME_GTC,
        "type_filling": _filling_mode(api, info.filling_mode),
    }
    check = api.order_check(request)
    if check is None:
        raise RuntimeError(f"MT5 close order_check failed: {api.last_error()}")
    if check.retcode != 0:
        raise RuntimeError(
            f"MT5 rejected close preflight ({check.retcode}): {check.comment}"
        )

    marker = Path(idempotency_file)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker_payload = {
        "status": "CLOSE_ATTEMPTED",
        "account_id": settings.expected_account_id,
        "symbol": symbol,
        "position_ticket": position.ticket,
        "side": "LONG" if is_long else "SHORT",
        "signal_id": signal_id,
        "volume": position.volume,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    try:
        with marker.open("x", encoding="utf-8") as output:
            json.dump(marker_payload, output, indent=2)
            output.write("\n")
    except FileExistsError as exc:
        raise RuntimeError(
            "This close signal already has an attempt marker; inspect MT5 and "
            "the marker before any retry"
        ) from exc

    _require_demo_account(api, settings.expected_account_id)
    terminal = api.terminal_info()
    account = api.account_info()
    current = _get_managed_position(api, symbol, settings.magic_number)
    latest_tick = api.symbol_info_tick(symbol)
    if (
        terminal is None
        or not terminal.trade_allowed
        or account is None
        or not account.trade_allowed
    ):
        raise RuntimeError("MT5 terminal or account no longer permits position closing")
    if current.ticket != position.ticket or current.type != position.type:
        raise RuntimeError("Close aborted because the managed position changed during preflight")
    if (
        current.volume != position.volume
        or current.sl != position.sl
        or latest_tick is None
        or not _is_positive_finite(latest_tick.bid)
        or not _is_positive_finite(latest_tick.ask)
        or latest_tick.ask <= latest_tick.bid
    ):
        raise RuntimeError(
            "Close aborted because the position or quote changed during preflight"
        )
    request["price"] = round(
        latest_tick.bid if is_long else latest_tick.ask,
        info.digits,
    )

    result = api.order_send(request)
    if result is None:
        raise RuntimeError(
            "MT5 returned no close result. The attempt marker was retained; "
            "inspect the terminal before retrying."
        )
    status = (
        "FILLED"
        if result.retcode == api.TRADE_RETCODE_DONE
        else "PARTIALLY_FILLED"
        if result.retcode == api.TRADE_RETCODE_DONE_PARTIAL
        else "REJECTED"
    )
    outcome = DemoOrderOutcome(
        status=status,
        retcode=result.retcode,
        comment=result.comment,
        order_ticket=result.order,
        deal_ticket=result.deal,
        filled_volume=result.volume,
        fill_price=result.price,
        idempotency_file=str(marker),
    )
    marker_payload.update(
        {
            "status": status,
            "retcode": outcome.retcode,
            "comment": outcome.comment,
            "order_ticket": outcome.order_ticket,
            "deal_ticket": outcome.deal_ticket,
            "filled_volume": outcome.filled_volume,
            "fill_price": outcome.fill_price,
        }
    )
    _replace_json(marker, marker_payload)
    return outcome


def _require_demo_account(api: Mt5TradingApi, expected_account_id: int) -> _AccountInfo:
    account = api.account_info()
    if account is None:
        raise RuntimeError(f"Could not read MT5 account: {api.last_error()}")
    if account.trade_mode != api.ACCOUNT_TRADE_MODE_DEMO:
        raise RuntimeError("Order execution is hard-blocked unless MT5 reports a DEMO account")
    if account.login != expected_account_id:
        raise RuntimeError(
            f"MT5 login {account.login} does not match the expected demo login "
            f"{expected_account_id}"
        )
    return account


def _get_managed_position(
    api: Mt5TradingApi,
    symbol: str,
    magic_number: int,
) -> _Position:
    positions = _get_all_managed_positions(api, magic_number)
    matching = [position for position in positions if position.symbol == symbol]
    if len(matching) != 1:
        raise RuntimeError(
            f"Expected exactly one bot-managed {symbol} position; "
            "manual intervention is required"
        )
    return matching[0]


def _get_all_managed_positions(
    api: Mt5TradingApi,
    magic_number: int,
) -> list[_Position]:
    positions = api.positions_get()
    if positions is None:
        raise RuntimeError(f"Could not check MT5 positions: {api.last_error()}")
    managed: list[_Position] = []
    for raw_position in positions:
        required = ("ticket", "symbol", "type", "volume", "magic", "sl", "price_open")
        if any(not hasattr(raw_position, field) for field in required):
            raise RuntimeError(
                "MT5 returned an incomplete or unmanaged account position; "
                "refusing to add or close positions"
            )
        position = cast(_Position, raw_position)
        if position.magic != magic_number:
            raise RuntimeError(
                "An account position is not managed by this bot; refusing to "
                "add or close positions while foreign positions exist"
            )
        if position.type not in (api.ORDER_TYPE_BUY, api.ORDER_TYPE_SELL):
            raise RuntimeError("MT5 returned an unsupported position direction")
        if (
            not isinstance(position.ticket, int)
            or position.ticket <= 0
            or not position.symbol
            or not _is_positive_finite(position.volume)
            or not _is_positive_finite(position.sl)
            or not _is_positive_finite(position.price_open)
        ):
            raise RuntimeError(
                "Bot-managed position has invalid volume, price, or no protective stop"
            )
        managed.append(position)
    return managed


def _total_open_risk(
    api: Mt5TradingApi,
    positions: tuple[_Position, ...] | list[_Position],
) -> float:
    total = 0.0
    for position in positions:
        risk = api.order_calc_profit(
            position.type,
            position.symbol,
            position.volume,
            position.price_open,
            position.sl,
        )
        if risk is None or not math.isfinite(risk) or risk >= 0:
            raise RuntimeError(
                f"Could not calculate valid stop risk for existing "
                f"{position.symbol} position: {api.last_error()}"
            )
        total += abs(risk)
    if not math.isfinite(total):
        raise RuntimeError("MT5 returned invalid aggregate portfolio risk")
    return total


def _is_closed_deal(api: Mt5TradingApi, deal: _Deal) -> bool:
    entry = getattr(deal, "entry", None)
    if entry is None:
        raise RuntimeError(
            "MT5 history returned a deal without an entry type; "
            "cannot safely calculate today's realized PnL"
        )
    closing_entries = {
        getattr(api, "DEAL_ENTRY_OUT", 1),
        getattr(api, "DEAL_ENTRY_OUT_BY", 3),
        getattr(api, "DEAL_ENTRY_INOUT", 2),
    }
    return entry in closing_entries


def _finite_closed_deal_net(deal: _Deal) -> float:
    values = (deal.profit, deal.commission, deal.swap, deal.fee)
    if not all(math.isfinite(value) for value in values):
        raise RuntimeError("MT5 history contains a non-finite deal value")
    return sum(values)


def _round_volume_down(volume: float, step: float) -> float:
    rounded = math.floor((volume / step) + 1e-10) * step
    decimals = max(0, -Decimal(str(step)).as_tuple().exponent)
    return round(rounded, decimals)


def _filling_mode(api: Mt5TradingApi, available_modes: int) -> int:
    if available_modes & 1:
        return api.ORDER_FILLING_FOK
    if available_modes & 2:
        return api.ORDER_FILLING_IOC
    return api.ORDER_FILLING_RETURN


def _replace_json(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _is_positive_finite(value: float) -> bool:
    return math.isfinite(value) and value > 0
