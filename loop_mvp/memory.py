"""经验 case 库（域无关内核）：存的是"可复用骨架 + 前置条件签名"，不是结果。

复用协议（这是与"一次计算直接复用"最大的区别）：

1. 检索：按 goal 找 case；
2. 校验：规则版本哈希是否变（换版 → 失效）、所需事实键是否齐（缺参 → 失效）；
3. 实例化：用**当前**事实重新生成动作（模板复用），不是套用旧金额；
4. 闸门重验：复用出来的动作照样过闸门，不因为"以前成功过"就免检。

去重键 = 域 + goal + 骨架 + 规则哈希 + 所需事实键；同键只存一条。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Tuple

from .estimator import HypothesisSet
from .types import (
    CaseRecord,
    Decimal,
    Plan,
    fingerprint,
    q,
)


@dataclass(frozen=True)
class ReuseCheck:
    """复用校验结果。"""

    valid: bool
    mode: str  # EXACT / TEMPLATE / INVALID
    reasons: Tuple[str, ...]
    case_id: str = ""

    def fingerprint(self) -> str:
        return fingerprint(self)


class CaseMemory:
    def __init__(self, domain: str) -> None:
        self.domain = domain
        self.cases: List[CaseRecord] = []
        self.by_signature: Dict[str, CaseRecord] = {}
        self.deduped = 0
        self.retrievals = 0
        self.invalid_reasons: Dict[str, int] = {}

    # -- 存取 --------------------------------------------------------------
    def signature(self, goal: str, skeleton: Tuple[str, ...], rule_hashes: Mapping[str, str],
                  fact_keys: Tuple[str, ...]) -> str:
        return fingerprint(
            {"domain": self.domain, "goal": goal, "skeleton": tuple(skeleton),
             "rule_hashes": dict(rule_hashes), "fact_keys": tuple(sorted(fact_keys))}
        )

    def save(self, plan: Plan, period: str, outcome: str,
             metrics: Mapping[str, str]) -> CaseRecord:
        sig = self.signature(plan.goal, plan.skeleton, plan.rule_hashes, plan.fact_keys)
        if sig in self.by_signature:
            self.deduped += 1
            return self.by_signature[sig]
        case = CaseRecord(
            case_id="C-" + sig[:10],
            domain=self.domain,
            goal=plan.goal,
            skeleton=tuple(plan.skeleton),
            rule_hashes=dict(plan.rule_hashes),
            required_fact_keys=tuple(sorted(plan.fact_keys)),
            created_period=period,
            outcome=outcome,
            metrics=dict(metrics),
            signature=sig,
        )
        self.cases.append(case)
        self.by_signature[sig] = case
        return case

    def retrieve(self, goal: str) -> Optional[CaseRecord]:
        self.retrievals += 1
        hits = [c for c in self.cases if c.goal == goal]
        if not hits:
            return None
        return max(hits, key=lambda c: c.created_period)

    # -- 校验与复用 --------------------------------------------------------
    def validate(
        self,
        case: Optional[CaseRecord],
        goal: str,
        current_rule_hashes: Mapping[str, str],
        facts: Mapping[str, Decimal],
        fact_values_hash: str = "",
    ) -> ReuseCheck:
        if case is None or case.goal != goal:
            return self._invalid("NO_CASE")
        reasons: List[str] = []
        for rule_id, old_hash in case.rule_hashes.items():
            if current_rule_hashes.get(rule_id) != old_hash:
                reasons.append(f"RULE_VERSION_CHANGED:{rule_id}")
        for key in case.required_fact_keys:
            if key not in facts:
                reasons.append(f"MISSING_FACT:{key}")
        if reasons:
            return self._invalid(*reasons, case_id=case.case_id)
        stored = case.metrics.get("input_hash", "")
        mode = "EXACT" if (stored and stored == fact_values_hash) else "TEMPLATE"
        return ReuseCheck(valid=True, mode=mode, reasons=(), case_id=case.case_id)

    def _invalid(self, *reasons: str, case_id: str = "") -> ReuseCheck:
        for reason in reasons:
            head = reason.split(":", 1)[0]
            self.invalid_reasons[head] = self.invalid_reasons.get(head, 0) + 1
        return ReuseCheck(valid=False, mode="INVALID", reasons=tuple(reasons), case_id=case_id)

    def stats(self) -> Dict[str, object]:
        return {
            "cases": len(self.cases),
            "deduped": self.deduped,
            "retrievals": self.retrievals,
            "invalid_reasons": dict(self.invalid_reasons),
        }
