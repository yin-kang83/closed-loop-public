"""财务凭证级闭环域（期间费用明细 → 利润表口径对账）。

只做确定性聚合与对账，不判断会计处理是否正确、不构成审计意见：
- 事实：期间费用明细按年度汇总的三条费用线（销售/管理/财务），**已排除结转分录**；
- 动作：聚合凭证级发生额 → 三条费用线（预测报表口径）；
- 观测：真实利润表对应列（按 +5 年列标签映射，映射依据见报告）；
- 残差：凭证聚合与利润表不一致 → 告警并交人工解释（不猜、不改数）。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Dict, Mapping, Optional, Tuple

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

SALES = "FV_销售费用"
ADMIN = "FV_管理费用"
FINANCE = "FV_财务费用"
STATE_KEYS = (SALES, ADMIN, FINANCE)

MAPPING_RULE = "EXPENSE_LINE_MAPPING"
VERSIONS = (
    RuleVersion(MAPPING_RULE, "V1",
                {"label_offset": dec("5"), "closing_keyword": "结转"},
                date(2000, 1, 1), None, "ALL",
                "费用线映射：列报项目前缀→利润表行；利润表列标签 = 凭证年度 + 5（依据：2018-2021 三线逐年精确一致）"),
)
SKELETON = ("AGGREGATE_EXPENSE",)


class FinanceVoucherDomain(Domain):
    name = "finance_voucher"
    goal = "EXPENSE_REPORT_CLOSE"
    observation_keys = (SALES, ADMIN, FINANCE)
    required_fact_keys = ("V:销售费用", "V:管理费用", "V:财务费用",
                          "V:未映射项目数", "V:结转行数")

    def rule_hashes(self, business_date: date) -> Mapping[str, str]:
        return RuleBook(VERSIONS).hashes()

    def resolve_hypotheses(self, business_date: date, entity: Optional[str]) -> HypothesisSet:
        versions = RuleBook(VERSIONS).applicable(MAPPING_RULE, business_date, entity_scope=None)
        if not versions:
            raise LookupError("费用线映射规则无适用版本（缺参，拒绝）")
        return HypothesisSet.from_rules(versions)

    def initial_state(self) -> StateSnapshot:
        return StateSnapshot("P00", {key: ZERO for key in STATE_KEYS})

    def transition(self, state: StateSnapshot, action: ActionOp,
                   params: Mapping[str, Any]) -> StateSnapshot:
        if action.name != "AGGREGATE_EXPENSE":
            raise ValueError(f"凭证域未知动作: {action.name}")
        values = {
            SALES: q(action.decimal("sales")),
            ADMIN: q(action.decimal("admin")),
            FINANCE: q(action.decimal("finance")),
        }
        return state.with_values(values)

    def violations(self, state: StateSnapshot, action: ActionOp,
                   params: Mapping[str, Any]) -> Tuple[str, ...]:
        if action.name != "AGGREGATE_EXPENSE":
            return ()
        codes = []
        if q(action.decimal("unknown_count")) > ZERO:
            codes.append("UNKNOWN_EXPENSE_ITEM")
        if q(action.decimal("closing_rows")) < ZERO:
            codes.append("NEGATIVE_CLOSING_ROWS")
        return tuple(codes)

    def plan(self, state: StateSnapshot, facts: Mapping[str, Decimal],
             hypotheses: HypothesisSet, business_date: date) -> Plan:
        sales = q(facts["V:销售费用"])
        admin = q(facts["V:管理费用"])
        finance = q(facts["V:财务费用"])
        unknown = q(facts["V:未映射项目数"])
        closing_rows = q(facts["V:结转行数"])
        action = ActionOp("AGGREGATE_EXPENSE", {
            "sales": sales, "admin": admin, "finance": finance,
            "unknown_count": unknown, "closing_rows": closing_rows,
        })
        return Plan(
            period=state.period, goal=self.goal,
            actions=(PlannedAction("AGGREGATE_EXPENSE", "聚合期间费用", {"*": action}),),
            skeleton=SKELETON, rule_hashes=self.rule_hashes(business_date),
            fact_keys=self.required_fact_keys,
        )

    def instantiate(self, skeleton: Tuple[str, ...], state: StateSnapshot,
                    facts: Mapping[str, Decimal], hypotheses: HypothesisSet,
                    business_date: date) -> Plan:
        if tuple(skeleton) != SKELETON:
            raise ValueError(f"经验骨架不匹配：{skeleton} != {SKELETON}")
        return self.plan(state, facts, hypotheses, business_date)

    def observation_step(self, plan: Plan) -> int:
        return 0


class FinanceVoucherEnvironment(Environment):
    """真实数据环境：facts=凭证聚合，declared=利润表对应行（观测）。"""

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
        self.executed: Dict[str, list] = {period: [] for period in self.periods}

    def extract_facts(self, period: str, business_date: date) -> FactBundle:
        return FactBundle(period=period, business_date=business_date,
                          facts=dict(self.facts[period]), source="expense_ledger")

    def execute(self, period: str, action: ActionOp, params: Mapping[str, Any]) -> None:
        self.executed[period].append((action.name, dict(action.params)))

    def finalize(self, period: str) -> Tuple[Observation, int]:
        return Observation(period=period, values=dict(self.declared[period]),
                           source="reported_profit_statement", received_at=period), 0

    def confirm(self, period: str, hypotheses: HypothesisSet) -> HypothesisSet:
        return hypotheses

    def truth(self, period: str) -> Mapping[str, Decimal]:
        return self.declared[period]
