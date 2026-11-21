"""通用契约层（域无关）：区间、状态、动作、预测、观测、残差、事实、经验 case。

两个演示域（财务结账 / 板材下料）共用本层，证明闭环内核可跨域复用：
- 状态统一为 ``科目/量名 -> Decimal`` 的向量；
- 动作 = ``ActionOp(name, params)``，由域自行解释；
- 假设 = ``Hypothesis(id, params)``，由域的规则版本解析产生；
- 预测 = 全假设场景枚举 + 经验误差界 → 区间；
- 观测 = 真实环境回执（可延迟）；
- 残差 = 观测 vs 区间，越界才触发诊断/重校准，不触发"重试"。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

CENT = Decimal("0.01")
ZERO = Decimal("0.00")


def dec(value: "Decimal | int | float | str") -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def q(value: "Decimal | int | str") -> Decimal:
    return dec(value).quantize(CENT, rounding=ROUND_HALF_UP)


def _encode(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        # 规范化：去掉末尾零，保证 Decimal("1.5") 与 Decimal("1.50") 哈希一致
        if obj == 0:
            return "0"
        return format(obj.normalize(), "f")
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, Mapping):
        return {str(k): _encode(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_encode(v) for v in obj]
    if hasattr(obj, "__dataclass_fields__"):
        return {k: _encode(getattr(obj, k)) for k in obj.__dataclass_fields__}
    return obj


def canonical_json(obj: Any) -> str:
    return json.dumps(_encode(obj), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fingerprint(obj: Any) -> str:
    return sha256_hex(canonical_json(obj))


# ---------------------------------------------------------------------------
# 区间 / 状态 / 动作
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Interval:
    """闭区间：预测值的 sound 范围（场景枚举 + 经验误差界）。"""

    lo: Decimal
    hi: Decimal

    def __post_init__(self) -> None:
        lo, hi = q(self.lo), q(self.hi)
        if lo > hi:
            raise ValueError(f"区间下界大于上界: {lo} > {hi}")
        object.__setattr__(self, "lo", lo)
        object.__setattr__(self, "hi", hi)

    @staticmethod
    def from_values(values: Sequence[Decimal]) -> "Interval":
        if not values:
            raise ValueError("空集合无法构成区间")
        return Interval(min(values), max(values))

    def contains(self, value: "Decimal | int | str") -> bool:
        return self.lo <= dec(value) <= self.hi

    @property
    def width(self) -> Decimal:
        return q(self.hi - self.lo)

    def expand(self, delta: Decimal) -> "Interval":
        delta = abs(q(delta))
        return Interval(self.lo - delta, self.hi + delta)

    def union(self, other: "Interval") -> "Interval":
        return Interval(min(self.lo, other.lo), max(self.hi, other.hi))

    def to_dict(self) -> Dict[str, str]:
        return {"lo": format(self.lo, "f"), "hi": format(self.hi, "f")}


@dataclass
class StateSnapshot:
    """域状态：名称 -> Decimal（科目余额 / 物料长度 / 出材率 …）。"""

    period: str
    values: Mapping[str, Decimal]

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", {str(k): q(v) for k, v in self.values.items()})

    def get(self, key: str) -> Decimal:
        return self.values.get(key, ZERO)

    def with_values(self, values: Mapping[str, Decimal]) -> "StateSnapshot":
        return StateSnapshot(period=self.period, values=values)

    def fingerprint(self) -> str:
        return fingerprint({"period": self.period, "values": self.values})


@dataclass(frozen=True)
class ActionOp:
    """域动作：名称 + 参数（金额/长度/件号…），由域的转移函数解释。"""

    name: str
    params: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "params", dict(self.params))

    def get(self, key: str, default: Any = None) -> Any:
        return self.params.get(key, default)

    def decimal(self, key: str) -> Decimal:
        if key not in self.params:
            raise KeyError(f"动作 {self.name} 缺参数 {key}")
        return dec(self.params[key])

    def fingerprint(self) -> str:
        return fingerprint(self)


@dataclass(frozen=True)
class PlannedAction:
    """带假设分支的动作：``variants`` 键为假设 id 或 ``"*"``（无条件）。

    - 财务域：税额依赖税率假设 → 多个不同金额的变体 → 闸门看到歧义直接 REVIEW；
    - 下料域：切割指令与切缝假设无关 → 单一变体，闸门改为"全假设可行"的鲁棒检查。
    """

    name: str
    label: str
    variants: Mapping[str, ActionOp]

    def __post_init__(self) -> None:
        object.__setattr__(self, "variants", dict(self.variants))

    def variant_for(self, hypothesis_id: Optional[str]) -> Optional[ActionOp]:
        if hypothesis_id is not None and hypothesis_id in self.variants:
            return self.variants[hypothesis_id]
        return self.variants.get("*")

    def distinct_fingerprints(self) -> Tuple[str, ...]:
        return tuple(sorted({v.fingerprint() for v in self.variants.values()}))

    def is_ambiguous(self) -> bool:
        return len(self.distinct_fingerprints()) > 1

    def amounts(self) -> Tuple[Decimal, ...]:
        return tuple(sorted({v.decimal("amount") for v in self.variants.values()
                             if "amount" in v.params}))

    def fingerprint(self) -> str:
        return fingerprint(
            {"name": self.name, "label": self.label,
             "variants": {k: v.fingerprint() for k, v in self.variants.items()}}
        )


@dataclass(frozen=True)
class Plan:
    """域规划结果：动作序列 + 计划骨架 + 规则版本哈希 + 所需事实键。"""

    period: str
    goal: str
    actions: Tuple[PlannedAction, ...]
    skeleton: Tuple[str, ...]
    rule_hashes: Mapping[str, str]
    fact_keys: Tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "actions", tuple(self.actions))
        object.__setattr__(self, "skeleton", tuple(self.skeleton))
        object.__setattr__(self, "rule_hashes", dict(self.rule_hashes))
        object.__setattr__(self, "fact_keys", tuple(self.fact_keys))

    def fingerprint(self) -> str:
        return fingerprint(
            {"period": self.period, "goal": self.goal,
             "actions": [a.fingerprint() for a in self.actions],
             "skeleton": self.skeleton, "rule_hashes": self.rule_hashes}
        )


# ---------------------------------------------------------------------------
# 预测 / 观测 / 残差
# ---------------------------------------------------------------------------

@dataclass
class Prediction:
    """"闭眼推演"结果：每个假设下的确定状态 + 观测键的区间。"""

    period: str
    step: int
    step_name: str
    scenarios: Mapping[str, StateSnapshot]
    intervals: Mapping[str, Interval]
    variant_fingerprints: Mapping[str, str]

    def fingerprint(self) -> str:
        return fingerprint(
            {"period": self.period, "step": self.step, "step_name": self.step_name,
             "scenarios": {k: v.fingerprint() for k, v in self.scenarios.items()},
             "intervals": {k: v.to_dict() for k, v in self.intervals.items()},
             "variants": self.variant_fingerprints}
        )


@dataclass(frozen=True)
class Observation:
    """真实环境反馈（对账/申报回执/实测剩余料），可能延迟到达。"""

    period: str
    values: Mapping[str, Decimal]
    source: str
    received_at: str
    delayed: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", {str(k): q(v) for k, v in self.values.items()})

    def fingerprint(self) -> str:
        return fingerprint(self)


@dataclass(frozen=True)
class ResidualRow:
    key: str
    interval: Interval
    observed: Decimal
    delta: Decimal

    @property
    def in_bound(self) -> bool:
        return self.interval.contains(self.observed)


@dataclass(frozen=True)
class ResidualReport:
    period: str
    rows: Tuple[ResidualRow, ...]

    @property
    def in_bound(self) -> bool:
        return all(r.in_bound for r in self.rows)

    @property
    def out_of_bound(self) -> Tuple[ResidualRow, ...]:
        return tuple(r for r in self.rows if not r.in_bound)

    def max_abs_delta(self) -> Decimal:
        if not self.rows:
            return ZERO
        return max(abs(r.delta) for r in self.rows)

    def nonconformity(self) -> Decimal:
        """区间外距离（区间内记 0），用于经验误差界的保守更新。"""
        worst = ZERO
        for row in self.rows:
            if row.observed < row.interval.lo:
                worst = max(worst, row.interval.lo - row.observed)
            elif row.observed > row.interval.hi:
                worst = max(worst, row.observed - row.interval.hi)
        return worst

    def fingerprint(self) -> str:
        return fingerprint(self)


# ---------------------------------------------------------------------------
# 双时态事实 / 经验 case
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FactBundle:
    """文档抽取结果（候选事实），尚未入账。"""

    period: str
    business_date: date
    facts: Mapping[str, Decimal]
    source: str = "document_extract"

    def __post_init__(self) -> None:
        object.__setattr__(self, "facts", {str(k): q(v) for k, v in self.facts.items()})

    def fingerprint(self) -> str:
        return fingerprint({"period": self.period, "business_date": self.business_date,
                            "facts": self.facts, "source": self.source})


@dataclass(frozen=True)
class FactRecord:
    """双时态事实：业务有效期 + 录入时点；只追加，修正走 supersedes。"""

    key: str
    value: Decimal
    unit: str
    valid_from: date
    valid_to: Optional[date]
    recorded_at: str
    source_hash: str
    supersedes: Optional[str] = None
    prev_hash: str = ""
    record_hash: str = ""


@dataclass(frozen=True)
class CaseRecord:
    """经验 case：复用"计划骨架 + 前置条件签名"，不直接复用结果数字。"""

    case_id: str
    domain: str
    goal: str
    skeleton: Tuple[str, ...]
    rule_hashes: Mapping[str, str]
    required_fact_keys: Tuple[str, ...]
    created_period: str
    outcome: str
    metrics: Mapping[str, str]
    signature: str = ""


@dataclass(frozen=True)
class GateDecision:
    """闸门判定：ALLOW 才执行；REVIEW 挂起等外部确认；BLOCK 拒绝。"""

    verdict: str
    codes: Tuple[str, ...]
    detail: str
    checked_hypotheses: int

    @property
    def allowed(self) -> bool:
        return self.verdict == "ALLOW"

    def fingerprint(self) -> str:
        return fingerprint(self)
