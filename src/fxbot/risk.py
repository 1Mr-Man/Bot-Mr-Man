from dataclasses import dataclass
import math


@dataclass(frozen=True)
class RiskLimits:
    risk_per_trade: float = 0.01
    max_daily_loss: float = 0.03

    def __post_init__(self) -> None:
        if not 0 < self.risk_per_trade <= 1:
            raise ValueError("risk_per_trade must be greater than 0 and at most 1")
        if not 0 < self.max_daily_loss <= 1:
            raise ValueError("max_daily_loss must be greater than 0 and at most 1")

    def position_units(
        self,
        *,
        equity: float,
        stop_distance: float,
        value_per_price_unit: float,
    ) -> float:
        """Size a position so the stop-distance loss fits the risk budget.

        `value_per_price_unit` converts a one-unit price move for one position
        unit into the account currency. The caller must supply the correct
        instrument- and account-specific conversion.
        """
        values = (equity, stop_distance, value_per_price_unit)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("sizing inputs must be finite")
        if equity <= 0 or stop_distance <= 0 or value_per_price_unit <= 0:
            raise ValueError("sizing inputs must be positive")
        risk_budget = equity * self.risk_per_trade
        units = risk_budget / (stop_distance * value_per_price_unit)
        if not math.isfinite(units):
            raise ValueError("calculated position size must be finite")
        return units

    def daily_loss_limit_reached(self, *, starting_equity: float, realized_pnl: float) -> bool:
        if not math.isfinite(starting_equity) or not math.isfinite(realized_pnl):
            raise ValueError("daily-loss inputs must be finite")
        if starting_equity <= 0:
            raise ValueError("starting_equity must be positive")
        return realized_pnl <= -(starting_equity * self.max_daily_loss)
