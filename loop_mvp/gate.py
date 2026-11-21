"""安全过滤闸门（域无关内核）。

学术对应：控制论里的 safety filter —— 模型给提案，闸门把动作投影到安全集。

判定顺序（fail-closed）：

1. **歧义动作**：*当前假设集下*同一动作有不同实现（金额/参数）→ REVIEW（不许猜），
   等外部确认收缩假设集后重判。
2. **鲁棒可行性**：每个假设场景都跑一遍转移与不变量检查：
   - 全部场景通过 → ALLOW；
   - 全部场景同一违例 → BLOCK（确定违反）；
   - 只有部分场景违例 → REVIEW（不确定是否违反，不许执行）。
3. 任何场景缺变体/缺场景 → REVIEW（结构不完整也不放行）。

闸门只做否决，不做优化；规划器无权放行。
"""

from __future__ import annotations

from typing import Dict, List, Tuple

from .domain import Domain
from .estimator import HypothesisSet
from .types import GateDecision, PlannedAction, Prediction


class SafetyGate:
    def __init__(self, domain: Domain) -> None:
        self.domain = domain

    def _applicable_variants(self, planned: PlannedAction, hypotheses: HypothesisSet):
        out = []
        for hyp in hypotheses.hypotheses:
            variant = planned.variant_for(hyp.hyp_id)
            if variant is not None:
                out.append((hyp, variant))
        return out

    def admit(
        self,
        planned: PlannedAction,
        prediction: Prediction,
        hypotheses: HypothesisSet,
    ) -> GateDecision:
        applicable = self._applicable_variants(planned, hypotheses)
        checked = len(applicable)

        # 1) 歧义动作：当前假设集下存在不同实现 → REVIEW
        distinct = {variant.fingerprint() for _hyp, variant in applicable}
        if len(distinct) > 1:
            detail = "动作在当前假设下有不同实现: " + ", ".join(
                f"{hyp.hyp_id}:{variant.fingerprint()[:8]}" for hyp, variant in applicable
            )
            return GateDecision("REVIEW", ("AMBIGUOUS_ACTION",), detail, checked)

        # 2) 鲁棒可行性
        code_counts: Dict[str, int] = {}
        missing: List[str] = []
        for hyp, variant in applicable:
            scenario = prediction.scenarios.get(hyp.hyp_id)
            if scenario is None:
                missing.append(hyp.hyp_id)
                continue
            for code in self.domain.violations(scenario, variant, hyp.params):
                code_counts[code] = code_counts.get(code, 0) + 1

        if missing:
            return GateDecision(
                "REVIEW", ("MISSING_VARIANT",),
                f"缺场景/变体的假设: {missing}", checked,
            )
        if not code_counts:
            return GateDecision("ALLOW", (), f"全 {checked} 个假设场景通过不变量", checked)

        codes = tuple(sorted(code_counts))
        if all(count == checked for count in code_counts.values()):
            return GateDecision("BLOCK", codes, "全部假设场景均违反不变量", checked)
        return GateDecision("REVIEW", codes, "部分假设场景违反不变量（不许执行）", checked)

    def verify_executed(self, state_after, action, params) -> Tuple[str, ...]:
        """执行后复核（纵深防御）：真实落账/落料后再查一次不变量。"""
        return self.domain.violations(state_after, action, params)
