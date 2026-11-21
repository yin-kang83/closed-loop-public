"""估计层（域无关）：假设集、残差分解、经验误差界、假设筛除。

关键设计（对"闭眼推演"的科学化）：

- 假设集 = 规则版本歧义/外部参数的离散可能世界；不允许 LLM 在隐空间"猜"；
- 预测区间 = 全假设场景枚举的 min/max，再按经验误差界外扩；
- 残差分解 = 观测落在区间内（不修正）/ 落在区间外（诊断 + 重校准），
  而不是把目标误差当预测误差、也不是"失败重试"；
- 误差界只由历史越界距离保守更新（单调增），明确定位为经验界，不是形式保证。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from .rules import RuleVersion
from .types import (
    Decimal,
    Interval,
    Observation,
    Prediction,
    ResidualReport,
    ResidualRow,
    ZERO,
    dec,
    fingerprint,
    q,
)

__all__ = [
    "Hypothesis",
    "HypothesisSet",
    "ResidualAnalyzer",
    "HypothesisSieve",
    "ConformalCalibrator",
    "CoverageTracker",
]


@dataclass(frozen=True)
class Hypothesis:
    """一个可能世界：参数明确（税率 20% / 切缝 12mm …）。"""

    hyp_id: str
    params: Mapping[str, object]
    label: str
    rule_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "params", dict(self.params))

    def decimal(self, key: str) -> Decimal:
        if key not in self.params:
            raise KeyError(f"假设 {self.hyp_id} 缺参数 {key}")
        return dec(self.params[key])

    @staticmethod
    def from_rule(version: RuleVersion) -> "Hypothesis":
        return Hypothesis(
            hyp_id="H-" + version.hash()[:10],
            params=dict(version.params),
            label=version.label(),
            rule_hash=version.hash(),
        )


@dataclass(frozen=True)
class HypothesisSet:
    hypotheses: Tuple[Hypothesis, ...]

    def __post_init__(self) -> None:
        hs = tuple(self.hypotheses)
        if not hs:
            raise ValueError("假设集不能为空（缺参应直接拒绝，而不是空集放行）")
        ids = [h.hyp_id for h in hs]
        if len(ids) != len(set(ids)):
            raise ValueError("假设集存在重复 id")
        object.__setattr__(self, "hypotheses", hs)

    @staticmethod
    def from_rules(versions: Sequence[RuleVersion]) -> "HypothesisSet":
        return HypothesisSet(tuple(Hypothesis.from_rule(v) for v in versions))

    @property
    def ids(self) -> Tuple[str, ...]:
        return tuple(h.hyp_id for h in self.hypotheses)

    @property
    def is_singleton(self) -> bool:
        return len(self.hypotheses) == 1

    def single(self) -> Hypothesis:
        if not self.is_singleton:
            raise ValueError(f"假设集不唯一（{len(self.hypotheses)} 个），不允许直接取一个")
        return self.hypotheses[0]

    def by_id(self) -> Dict[str, Hypothesis]:
        return {h.hyp_id: h for h in self.hypotheses}

    def params_for(self, hyp_id: str) -> Mapping[str, object]:
        return self.by_id()[hyp_id].params

    def subset(self, keep: Sequence[str]) -> "HypothesisSet":
        keep_set = set(keep)
        kept = tuple(h for h in self.hypotheses if h.hyp_id in keep_set)
        if not kept:
            raise ValueError("筛除后假设集为空：视为模型错误，不允许静默继续")
        return HypothesisSet(kept)

    def fingerprint(self) -> str:
        return fingerprint([h.hyp_id for h in self.hypotheses])


class ResidualAnalyzer:
    """观测 vs 预测区间：算残差、判越界。"""

    def analyze(self, prediction: Prediction, observation: Observation) -> ResidualReport:
        rows: List[ResidualRow] = []
        for key, observed in observation.values.items():
            interval = prediction.intervals.get(key)
            if interval is None:
                continue
            if observed < interval.lo:
                delta = q(observed - interval.lo)
            elif observed > interval.hi:
                delta = q(observed - interval.hi)
            else:
                delta = ZERO
            rows.append(ResidualRow(key=key, interval=interval, observed=observed, delta=delta))
        return ResidualReport(period=observation.period, rows=tuple(rows))


class HypothesisSieve:
    """用观测筛除假设（"睁眼一次"消除歧义）。全部假设都不匹配 → 模型错误。"""

    def sieve(
        self,
        prediction: Prediction,
        observation: Observation,
        tolerance: Mapping[str, Decimal],
    ) -> Tuple[Tuple[str, ...], bool]:
        kept: List[str] = []
        for hyp_id, scenario in prediction.scenarios.items():
            ok = True
            for key, observed in observation.values.items():
                if key not in tolerance:
                    continue
                if abs(q(scenario.get(key) - observed)) > tolerance[key]:
                    ok = False
                    break
            if ok:
                kept.append(hyp_id)
        return tuple(kept), bool(kept)


class ConformalCalibrator:
    """经验误差界：按越界距离保守更新（单调增）。

    诚实声明：这是"历史最坏越界距离"的保守界，不是 conformal 的形式保证；
    它在样本外不保证覆盖率，只保证"不会比历史见过的误差更窄"。
    """

    def __init__(self, default_bound: Decimal = ZERO) -> None:
        self.default_bound = q(default_bound)
        self.bounds: Dict[str, Decimal] = {}
        self.misses: Dict[str, int] = {}
        self.updates: List[str] = []

    def bound(self, key: str) -> Decimal:
        return self.bounds.get(key, self.default_bound)

    def widen(self, key: str, violation: Decimal) -> None:
        violation = abs(q(violation))
        old = self.bounds.get(key, ZERO)
        if violation > old:
            self.bounds[key] = violation
            self.updates.append(key)

    def update(self, report: ResidualReport) -> List[str]:
        changed: List[str] = []
        for row in report.rows:
            if row.in_bound:
                continue
            self.misses[row.key] = self.misses.get(row.key, 0) + 1
            before = self.bounds.get(row.key)
            self.widen(row.key, row.delta)
            if self.bounds.get(row.key) != before:
                changed.append(row.key)
        return changed


class CoverageTracker:
    """覆盖率：观测落在预测区间内的比例（校准质量的可核查指标）。"""

    def __init__(self) -> None:
        self.total = 0
        self.covered = 0

    def record(self, report: ResidualReport) -> None:
        for row in report.rows:
            self.total += 1
            if row.in_bound:
                self.covered += 1

    @property
    def coverage(self) -> float:
        return self.covered / self.total if self.total else 0.0
