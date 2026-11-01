from dataclasses import dataclass, field
from datetime import date, datetime, timezone
import math
from typing import Iterable

from fxbot.models import Bar, Side
from fxbot.risk import RiskLimits
from fxbot.strategies.base import Strategy


@dataclass(frozen=True)
class PaperConfig:
    initial_equity: float
    stop_distance: float
    spread: float
    slippage: float = 0.0
    commission_per_unit_per_side: float = 0.0
    value_per_price_unit: float = 1.0
    risk_limits: RiskLimits = field(default_factory=RiskLimits)

    def __post_init__(self) -> None:
        positive_values = (
            self.initial_equity,
            self.stop_distance,
            self.value_per_price_unit,
        )
        nonnegative_values = (
            self.spread,
            self.slippage,
            self.commission_per_unit_per_side,
        )
        if not all(math.isfinite(value) and value > 0 for value in positive_values):
            raise ValueError("equity, stop distance, and price-unit value must be positive and finite")
        if not all(math.isfinite(value) and value >= 0 for value in nonnegative_values):
            raise ValueError("spread, slippage, and commission must be finite and nonnegative")


@dataclass(frozen=True)
class PaperTrade:
    symbol: str
    side: Side
    units: float
    entry_time: datetime
    entry_price: float
    exit_time: datetime
    exit_price: float
    exit_reason: str
    gross_pnl: float
    commission: float
    net_pnl: float


@dataclass(frozen=True)
class EquityPoint:
    timestamp: datetime
    balance: float
    equity: float


@dataclass(frozen=True)
class OpenPosition:
    symbol: str
    side: Side
    units: float
    entry_time: datetime
    entry_price: float
    stop_price: float
    unrealized_pnl: float


@dataclass(frozen=True)
class PaperResult:
    initial_equity: float
    final_balance: float
    final_equity: float
    trades: tuple[PaperTrade, ...]
    equity_curve: tuple[EquityPoint, ...]
    open_position: OpenPosition | None
    lockout_dates: tuple[date, ...]


@dataclass
class _Position:
    symbol: str
    side: Side
    units: float
    entry_time: datetime
    entry_price: float
    stop_price: float
    entry_commission: float


class _PaperAccount:
    def __init__(self, config: PaperConfig) -> None:
        self.config = config
        self.balance = config.initial_equity
        self.position: _Position | None = None
        self.trades: list[PaperTrade] = []
        self.equity_curve: list[EquityPoint] = []
        self.lockout_dates: set[date] = set()
        self._session_date: date | None = None
        self._session_starting_balance = config.initial_equity
        self._session_realized_pnl = 0.0

    def begin_bar(self, bar: Bar) -> None:
        session_date = bar.timestamp.astimezone(timezone.utc).date()
        if session_date != self._session_date:
            self._session_date = session_date
            self._session_starting_balance = self.balance
            self._session_realized_pnl = 0.0

    def rebalance(self, target: Side, bar: Bar) -> None:
        if self.position is not None and self.position.side != target:
            self._close(bar, bar.open, "signal")

        if target == Side.FLAT or self.position is not None:
            return
        if self._daily_loss_limit_reached():
            self.lockout_dates.add(bar.timestamp.astimezone(timezone.utc).date())
            return

        side = target
        fill_price = self._execution_price(bar.open, side)
        units = self.config.risk_limits.position_units(
            equity=self.balance,
            stop_distance=self.config.stop_distance,
            value_per_price_unit=self.config.value_per_price_unit,
        )
        commission = units * self.config.commission_per_unit_per_side
        stop_price = fill_price - int(side) * self.config.stop_distance
        self._record_realized(-commission)
        self.position = _Position(
            symbol=bar.symbol,
            side=side,
            units=units,
            entry_time=bar.timestamp,
            entry_price=fill_price,
            stop_price=stop_price,
            entry_commission=commission,
        )

    def check_stop(self, bar: Bar) -> None:
        position = self.position
        if position is None:
            return
        if position.side == Side.LONG:
            if bar.open <= position.stop_price:
                self._close(bar, bar.open, "stop")
            elif bar.low <= position.stop_price:
                self._close(bar, position.stop_price, "stop")
        elif position.side == Side.SHORT:
            if bar.open >= position.stop_price:
                self._close(bar, bar.open, "stop")
            elif bar.high >= position.stop_price:
                self._close(bar, position.stop_price, "stop")

    def mark_to_market(self, bar: Bar) -> None:
        unrealized_pnl = 0.0
        if self.position is not None:
            unrealized_pnl = (
                (bar.close - self.position.entry_price)
                * int(self.position.side)
                * self.position.units
                * self.config.value_per_price_unit
            )
        self.equity_curve.append(
            EquityPoint(
                timestamp=bar.timestamp,
                balance=self.balance,
                equity=self.balance + unrealized_pnl,
            )
        )

    def result(self) -> PaperResult:
        last_equity = (
            self.equity_curve[-1].equity
            if self.equity_curve
            else self.config.initial_equity
        )
        open_position = None
        if self.position is not None:
            last_point = self.equity_curve[-1]
            open_position = OpenPosition(
                symbol=self.position.symbol,
                side=self.position.side,
                units=self.position.units,
                entry_time=self.position.entry_time,
                entry_price=self.position.entry_price,
                stop_price=self.position.stop_price,
                unrealized_pnl=last_point.equity - last_point.balance,
            )
        return PaperResult(
            initial_equity=self.config.initial_equity,
            final_balance=self.balance,
            final_equity=last_equity,
            trades=tuple(self.trades),
            equity_curve=tuple(self.equity_curve),
            open_position=open_position,
            lockout_dates=tuple(sorted(self.lockout_dates)),
        )

    def _close(self, bar: Bar, raw_price: float, reason: str) -> None:
        position = self.position
        if position is None:
            return
        closing_side = Side(-int(position.side))
        exit_price = self._execution_price(raw_price, closing_side)
        exit_commission = position.units * self.config.commission_per_unit_per_side
        gross_pnl = (
            (exit_price - position.entry_price)
            * int(position.side)
            * position.units
            * self.config.value_per_price_unit
        )
        exit_pnl = gross_pnl - exit_commission
        total_commission = position.entry_commission + exit_commission
        self._record_realized(exit_pnl)
        self.trades.append(
            PaperTrade(
                symbol=bar.symbol,
                side=position.side,
                units=position.units,
                entry_time=position.entry_time,
                entry_price=position.entry_price,
                exit_time=bar.timestamp,
                exit_price=exit_price,
                exit_reason=reason,
                gross_pnl=gross_pnl,
                commission=total_commission,
                net_pnl=gross_pnl - total_commission,
            )
        )
        self.position = None

    def _execution_price(self, mid_price: float, side: Side) -> float:
        adverse_adjustment = self.config.spread / 2 + self.config.slippage
        return mid_price + int(side) * adverse_adjustment

    def _record_realized(self, pnl: float) -> None:
        self.balance += pnl
        self._session_realized_pnl += pnl
        self._daily_loss_limit_reached()

    def _daily_loss_limit_reached(self) -> bool:
        return self.config.risk_limits.daily_loss_limit_reached(
            starting_equity=self._session_starting_balance,
            realized_pnl=self._session_realized_pnl,
        )


def run_paper_backtest(
    bars: Iterable[Bar],
    strategy: Strategy,
    config: PaperConfig,
) -> PaperResult:
    """Simulate one instrument, executing completed-bar signals next bar.

    Stops are checked against each bar's range. Losses larger than the stop
    distance are possible when a bar gaps through a stop. Daily lockout is
    based on realized PnL per UTC calendar day and does not force liquidation.
    """
    history = list(bars)
    if any(
        current.symbol != previous.symbol
        for previous, current in zip(history, history[1:])
    ):
        raise ValueError("a paper run must contain bars for only one symbol")
    if any(
        current.timestamp <= previous.timestamp
        for previous, current in zip(history, history[1:])
    ):
        raise ValueError("bar timestamps must be strictly increasing")

    account = _PaperAccount(config)
    target_for_open = Side.FLAT
    for bar in history:
        account.begin_bar(bar)
        account.rebalance(target_for_open, bar)
        account.check_stop(bar)
        account.mark_to_market(bar)
        target_for_open = strategy.on_bar(bar)
        if not isinstance(target_for_open, Side):
            raise TypeError("strategy must return a Side value")
    return account.result()
