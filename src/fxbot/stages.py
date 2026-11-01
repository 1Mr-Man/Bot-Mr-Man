from __future__ import annotations

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class StageTarget:
    stage: int
    target_balance: float


def build_stage_targets(
    initial_balance: float,
    *,
    stages: int = 30,
    growth_rate: float = 0.3,
) -> tuple[StageTarget, ...]:
    if not math.isfinite(initial_balance) or initial_balance <= 0:
        raise ValueError("initial_balance must be finite and positive")
    if isinstance(stages, bool) or not isinstance(stages, int) or not 1 <= stages <= 100:
        raise ValueError("stages must be an integer from 1 to 100")
    if not math.isfinite(growth_rate) or not 0 < growth_rate <= 1:
        raise ValueError("growth_rate must be finite and greater than 0 and at most 1")
    return tuple(
        StageTarget(
            stage=stage,
            target_balance=initial_balance * ((1 + growth_rate) ** stage),
        )
        for stage in range(1, stages + 1)
    )


def stage_reached_by_simulation(
    initial_balance: float,
    simulated_balance: float,
    *,
    stages: int = 30,
    growth_rate: float = 0.3,
) -> int:
    if not math.isfinite(simulated_balance):
        raise ValueError("simulated_balance must be finite")
    reached = 0
    for target in build_stage_targets(initial_balance, stages=stages, growth_rate=growth_rate):
        if simulated_balance < target.target_balance:
            break
        reached = target.stage
    return reached
