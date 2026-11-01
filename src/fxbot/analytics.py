from dataclasses import asdict, dataclass
from datetime import datetime
import json
from pathlib import Path
from typing import Iterable

from fxbot.models import Bar
from fxbot.paper import OpenPosition, PaperConfig, PaperResult


@dataclass(frozen=True)
class BacktestMetrics:
    total_return_pct: float
    max_drawdown_pct: float
    closed_trade_count: int
    win_rate_pct: float | None
    average_net_trade_pnl: float | None
    profit_factor: float | None
    gross_profit: float
    gross_loss: float
    has_open_position: bool


@dataclass(frozen=True)
class OpenPositionDetails:
    symbol: str
    side: str
    units: float
    entry_time: str
    entry_price: float
    stop_price: float
    unrealized_pnl: float


@dataclass(frozen=True)
class BacktestRunCard:
    schema_version: int
    strategy: str
    symbol: str | None
    bar_count: int
    start: str | None
    end: str | None
    initial_equity: float
    final_balance: float
    final_equity: float
    risk_per_trade: float
    max_daily_loss: float
    stop_distance: float
    spread: float
    slippage: float
    commission_per_unit_per_side: float
    value_per_price_unit: float
    lockout_dates_utc: tuple[str, ...]
    metrics: BacktestMetrics
    open_position: OpenPositionDetails | None
    warnings: tuple[str, ...]

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True, allow_nan=False)

    def write_json(self, path: str | Path) -> None:
        Path(path).write_text(self.to_json() + "\n", encoding="utf-8")


def build_run_card(
    result: PaperResult,
    bars: Iterable[Bar],
    config: PaperConfig,
    *,
    strategy_name: str,
) -> BacktestRunCard:
    """Summarize one paper run with explicit assumptions and caveats."""
    if not strategy_name.strip():
        raise ValueError("strategy_name must not be empty")
    history = list(bars)
    if any(
        current.symbol != previous.symbol
        for previous, current in zip(history, history[1:])
    ):
        raise ValueError("a run card must contain bars for only one symbol")
    if any(
        current.timestamp <= previous.timestamp
        for previous, current in zip(history, history[1:])
    ):
        raise ValueError("bar timestamps must be strictly increasing")

    closed_pnls = [trade.net_pnl for trade in result.trades]
    winners = [pnl for pnl in closed_pnls if pnl > 0]
    losers = [pnl for pnl in closed_pnls if pnl < 0]
    gross_profit = sum(winners)
    gross_loss = sum(losers)
    profit_factor = gross_profit / abs(gross_loss) if gross_loss < 0 else None
    win_rate = len(winners) / len(closed_pnls) * 100 if closed_pnls else None
    average_trade = sum(closed_pnls) / len(closed_pnls) if closed_pnls else None

    equity_values = [result.initial_equity]
    equity_values.extend(point.equity for point in result.equity_curve)
    peak = equity_values[0]
    max_drawdown = 0.0
    for equity in equity_values:
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - equity) / peak)

    warnings = [
        "Simulation models one net position; it omits financing/swaps, margin, "
        "broker-specific rules, and market impact.",
        "Configured spread, slippage, commission, and currency conversion are "
        "assumptions, not observed execution costs.",
        "Historical simulation is not forward paper trading and does not "
        "establish future performance.",
    ]
    if result.open_position is not None:
        warnings.append(
            "An open position remains at the end; final equity includes its "
            "mark-to-market PnL, but it is not a closed trade."
        )
    if not result.trades:
        warnings.append(
            "No trades were closed; win rate, average trade PnL, and profit "
            "factor are undefined."
        )
    elif gross_loss == 0:
        warnings.append("Profit factor is undefined because no closed trade lost money.")

    metrics = BacktestMetrics(
        total_return_pct=(result.final_equity / result.initial_equity - 1) * 100,
        max_drawdown_pct=max_drawdown * 100,
        closed_trade_count=len(closed_pnls),
        win_rate_pct=win_rate,
        average_net_trade_pnl=average_trade,
        profit_factor=profit_factor,
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        has_open_position=result.open_position is not None,
    )
    return BacktestRunCard(
        schema_version=1,
        strategy=strategy_name,
        symbol=history[0].symbol if history else None,
        bar_count=len(history),
        start=_iso(history[0].timestamp) if history else None,
        end=_iso(history[-1].timestamp) if history else None,
        initial_equity=result.initial_equity,
        final_balance=result.final_balance,
        final_equity=result.final_equity,
        risk_per_trade=config.risk_limits.risk_per_trade,
        max_daily_loss=config.risk_limits.max_daily_loss,
        stop_distance=config.stop_distance,
        spread=config.spread,
        slippage=config.slippage,
        commission_per_unit_per_side=config.commission_per_unit_per_side,
        value_per_price_unit=config.value_per_price_unit,
        lockout_dates_utc=tuple(day.isoformat() for day in result.lockout_dates),
        metrics=metrics,
        open_position=_position_details(result.open_position),
        warnings=tuple(warnings),
    )


def _position_details(position: OpenPosition | None) -> OpenPositionDetails | None:
    if position is None:
        return None
    return OpenPositionDetails(
        symbol=position.symbol,
        side=position.side.name,
        units=position.units,
        entry_time=_iso(position.entry_time),
        entry_price=position.entry_price,
        stop_price=position.stop_price,
        unrealized_pnl=position.unrealized_pnl,
    )


def _iso(value: datetime) -> str:
    return value.isoformat()
