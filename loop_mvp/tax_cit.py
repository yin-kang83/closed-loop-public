"""企业所得税年度汇算闭环域（真实申报数据）：亏损结转 → 弥补 → 计税 → 观测对账。

这是"税务真实域"，状态是**跨年度可弥补亏损余额**，动作是弥补/计税/结转，
观测是真实 A100000 申报表的"应纳税所得额 / 应纳所得税额"。

不变量（闸门检查）：
- 亏损余额不得为负；应纳税所得额、应纳所得税额不得为负；
- 弥补额不得超过可弥补亏损余额，也不得超过当年纳税调整后所得（不许多弥补）；
- 应纳所得税额必须精确等于应纳税所得额 × 税率；
- 年度结转后亏损余额 = 结转前余额 + 当年新增亏损。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .domain import Domain, Environment
from .estimator import HypothesisSet
from .rules import RuleBook, RuleVersion
from .types import (
    ActionOp,
    FactBundle,
    Observation,
    Plan,
    PlannedAction,
    StateSnapshot,
    ZERO,
    dec,
    q,
)

CARRY = "CIT_可弥补亏损余额"
TAXABLE = "CIT_本期应纳税所得额"
TAX = "CIT_本期应纳所得税额"
STATE_KEYS = (CARRY, TAXABLE, TAX)

RATE_RULE = "CIT_RATE"
VERSIONS = (
    RuleVersion(RATE_RULE, "V1", {"rate": dec("0.25")}, date(2008, 1, 1), None, "ALL",
                "企业所得税法：税率 25%"),
)
SKELETON = ("CARRY_LOSS", "ASSESS_TAX", "CLOSE_YEAR")


class TaxCitDomain(Domain):
    name = "tax_cit_real"
    goal = "ANNUAL_CIT_SETTLE"
    observation_keys = (TAXABLE, TAX)
    required_fact_keys = ("adjusted_income",)

    def __init__(self, initial_carry: Decimal = ZERO) -> None:
        self.initial_carry = q(initial_carry)

    # -- 规则 --------------------------------------------------------------
    def rule_hashes(self, business_date: date) -> Mapping[str, str]:
        return RuleBook(VERSIONS).hashes()

    def resolve_hypotheses(self, business_date: date, entity: Optional[str]) -> HypothesisSet:
        versions = RuleBook(VERSIONS).applicable(RATE_RULE, business_date, entity_scope=None)
        if not versions:
            raise LookupError("企业所得税率规则无适用版本（缺参，拒绝）")
        return HypothesisSet.from_rules(versions)

    # -- 状态与转移 --------------------------------------------------------
    def initial_state(self) -> StateSnapshot:
        return StateSnapshot("P00", {CARRY: self.initial_carry, TAXABLE: ZERO, TAX: ZERO})

    def transition(self, state: StateSnapshot, action: ActionOp,
                   params: Mapping[str, Any]) -> StateSnapshot:
        values = {key: state.get(key) for key in STATE_KEYS}
        if action.name == "CARRY_LOSS":
            base = q(action.decimal("base"))
            amount = q(action.decimal("amount"))
            values[CARRY] = q(values[CARRY] - amount)
            values[TAXABLE] = q(max(ZERO, base - amount))
        elif action.name == "ASSESS_TAX":
            rate = dec(params["rate"])
            values[TAX] = q(values[TAXABLE] * rate)
        elif action.name == "CLOSE_YEAR":
            adjusted = q(action.decimal("adjusted"))
            values[CARRY] = q(values[CARRY] + max(ZERO, -adjusted))
        else:
            raise ValueError(f"税务域未知动作: {action.name}")
        return state.with_values(values)

    def violations(self, state: StateSnapshot, action: ActionOp,
                   params: Mapping[str, Any]) -> Tuple[str, ...]:
        values = {key: state.get(key) for key in STATE_KEYS}
        codes = []
        if values[CARRY] < ZERO:
            codes.append("NEGATIVE_LOSS_CARRY")
        if values[TAXABLE] < ZERO:
            codes.append("NEGATIVE_TAXABLE_INCOME")
        if values[TAX] < ZERO:
            codes.append("NEGATIVE_TAX_DUE")
        if action.name == "CARRY_LOSS":
            amount = q(action.decimal("amount"))
            base = q(action.decimal("base"))
            prior = q(action.decimal("prior_carry"))
            if amount > prior or amount > max(ZERO, base):
                codes.append("OVER_OFFSET")
        if action.name == "ASSESS_TAX":
            rate = dec(params["rate"])
            if values[TAX] != q(values[TAXABLE] * rate):
                codes.append("TAX_FORMULA_MISMATCH")
        if action.name == "CLOSE_YEAR":
            adjusted = q(action.decimal("adjusted"))
            prior = q(action.decimal("prior_carry"))
            if values[CARRY] != q(prior + max(ZERO, -adjusted)):
                codes.append("CARRY_ROLLOVER_MISMATCH")
        return tuple(codes)

    # -- 规划 / 经验复用 ---------------------------------------------------
    def plan(self, state: StateSnapshot, facts: Mapping[str, Decimal],
             hypotheses: HypothesisSet, business_date: date) -> Plan:
        adjusted = q(facts["adjusted_income"])
        carry = state.get(CARRY)
        base = max(ZERO, adjusted)
        amount = q(min(carry, base))
        remaining = q(carry - amount)
        actions = (
            PlannedAction("CARRY_LOSS", "弥补以前年度亏损", {"*": ActionOp("CARRY_LOSS", {
                "base": adjusted, "amount": amount, "prior_carry": carry})}),
            PlannedAction("ASSESS_TAX", "计提应纳所得税", {"*": ActionOp("ASSESS_TAX", {
                "taxable": q(max(ZERO, adjusted - amount))})}),
            PlannedAction("CLOSE_YEAR", "年度亏损结转", {"*": ActionOp("CLOSE_YEAR", {
                "adjusted": adjusted, "prior_carry": remaining})}),
        )
        return Plan(
            period=state.period, goal=self.goal, actions=actions, skeleton=SKELETON,
            rule_hashes=self.rule_hashes(business_date), fact_keys=self.required_fact_keys,
        )

    def instantiate(self, skeleton: Tuple[str, ...], state: StateSnapshot,
                    facts: Mapping[str, Decimal], hypotheses: HypothesisSet,
                    business_date: date) -> Plan:
        if tuple(skeleton) != SKELETON:
            raise ValueError(f"经验骨架不匹配：{skeleton} != {SKELETON}")
        return self.plan(state, facts, hypotheses, business_date)

    def observation_step(self, plan: Plan) -> int:
        return 1  # ASSESS_TAX 之后即是申报口径


class TaxCitEnvironment(Environment):
    """真实申报数据环境：facts=申报表可复算输入，declared=申报表申报数（观测）。"""

    def __init__(self, periods: Tuple[str, ...],
                 facts: Mapping[str, Mapping[str, Decimal]],
                 declared: Mapping[str, Mapping[str, Decimal]],
                 dates: Mapping[str, date]) -> None:
        self.periods = tuple(periods)
        self.facts = facts
        self.declared = declared
        self.dates = dates
        self.reset()

    def reset(self) -> None:
        self.executed: Dict[str, list] = {p: [] for p in self.periods}

    def extract_facts(self, period: str, business_date: date) -> FactBundle:
        return FactBundle(period=period, business_date=business_date,
                          facts=dict(self.facts[period]), source="A100000_extract")

    def execute(self, period: str, action: ActionOp, params: Mapping[str, Any]) -> None:
        self.executed[period].append((action.name, dict(action.params)))

    def finalize(self, period: str) -> Tuple[Observation, int]:
        return Observation(period=period, values=dict(self.declared[period]),
                           source="A100000_declared", received_at=period), 0

    def confirm(self, period: str, hypotheses: HypothesisSet) -> HypothesisSet:
        return hypotheses

    def truth(self, period: str) -> Mapping[str, Decimal]:
        return self.declared[period]
