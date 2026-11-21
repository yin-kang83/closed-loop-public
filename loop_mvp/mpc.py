"""MPC 规划层（域无关）：候选计划 × 可配置字典序代价 × 合规硬约束。

设计原则（避免把风险偏好写死）：

1. **合规是硬约束，不是权重**：``compliance`` 永远排第一（即使配置里不写）；
2. **软目标用字典序**：``objectives`` 是显式、可配置的优先级元组（如
   "fulfillment > waste" 或 "waste > fulfillment"），换顺序会换决策——有测试证明；
3. **不猜未来**：候选由域提供；每个候选由域的 ``score_plan`` 用当前假设集
   做确定性推演打分；horizon 接口保留（当前演示域逐期独立，horizon=1）；
4. **闸门仍是否决者**：选出的计划还要逐步过 SafetyGate，MPC 不拥有放行权。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .types import Decimal, Plan, ZERO, q

DEFAULT_OBJECTIVES = ("compliance", "fulfillment", "waste")


@dataclass(frozen=True)
class MpcConfig:
    objectives: Tuple[str, ...] = DEFAULT_OBJECTIVES
    horizon: int = 1  # 演示域逐期独立；多期滚动留接口

    def __post_init__(self) -> None:
        if not self.objectives:
            raise ValueError("MPC 至少需要一个目标")
        if self.horizon < 1:
            raise ValueError("MPC horizon 必须 >= 1")

    def effective_objectives(self) -> Tuple[str, ...]:
        """合规强制第一，其余按配置顺序（去重）。"""
        order = ["compliance"]
        for objective in self.objectives:
            if objective not in order:
                order.append(objective)
        return tuple(order)


@dataclass(frozen=True)
class MpcCandidate:
    label: str
    plan: Plan
    scores: Mapping[str, Decimal]

    def key(self, objectives: Sequence[str]) -> Tuple[Decimal, ...]:
        return tuple(q(self.scores.get(name, ZERO)) for name in objectives)


@dataclass(frozen=True)
class MpcSelection:
    chosen: MpcCandidate
    objectives: Tuple[str, ...]
    candidates: Tuple[MpcCandidate, ...]

    def summary(self) -> str:
        return (
            f"chosen={self.chosen.label} objectives={'>'.join(self.objectives)} "
            f"scores={{{', '.join(f'{k}:{format(v, 'f')}' for k, v in sorted(self.chosen.scores.items()))}}}"
        )


@dataclass(frozen=True)
class ScoredOption:
    label: str
    scores: Mapping[str, Decimal]

    def key(self, objectives: Sequence[str]) -> Tuple[Decimal, ...]:
        return tuple(q(self.scores.get(name, ZERO)) for name in objectives)


@dataclass(frozen=True)
class ScoredSelection:
    chosen: ScoredOption
    objectives: Tuple[str, ...]
    candidates: Tuple[ScoredOption, ...]

    def summary(self) -> str:
        return (
            f"chosen={self.chosen.label} objectives={'>'.join(self.objectives)} "
            f"scores={{{', '.join(f'{k}:{format(v, 'f')}' for k, v in sorted(self.chosen.scores.items()))}}}"
        )


class MpcPlanner:
    """按字典序选最优候选计划：每个目标都是"越小越好"的成本。"""

    def __init__(self, config: Optional[MpcConfig] = None) -> None:
        self.config = config or MpcConfig()

    def select_scored(
        self,
        options: Sequence[Tuple[str, Mapping[str, Decimal]]],
    ) -> ScoredSelection:
        """通用入口：候选 = (标签, 成本字典)；不依赖 Plan/域，供税务决策等复用。"""
        if not options:
            raise ValueError("MPC 没有候选方案（至少给一个）")
        objectives = self.config.effective_objectives()
        scored = [ScoredOption(label=label, scores=dict(scores)) for label, scores in options]
        best = min(scored, key=lambda option: option.key(objectives))
        return ScoredSelection(chosen=best, objectives=objectives, candidates=tuple(scored))

    def select(
        self,
        candidates: Sequence[Tuple[str, Plan]],
        scorer: Callable[[Plan], Mapping[str, Decimal]],
    ) -> MpcSelection:
        if not candidates:
            raise ValueError("MPC 没有候选计划（域必须至少给一个）")
        objectives = self.config.effective_objectives()
        scored: List[MpcCandidate] = [
            MpcCandidate(label=label, plan=plan, scores=dict(scorer(plan)))
            for label, plan in candidates
        ]
        best = min(scored, key=lambda candidate: candidate.key(objectives))
        return MpcSelection(chosen=best, objectives=objectives, candidates=tuple(scored))
