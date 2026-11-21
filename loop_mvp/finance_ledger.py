"""分录级闭环域（全量序时账 → 期末余额 → 资产负债表口径对账）。

只做确定性复算与对账，不判断会计处理是否正确、不构成审计意见：
- 事实：全量序时账按年度累计的资产/负债/权益余额 + 凭证完整性指标；
- 动作：凭证完整性检查（借贷平衡/金额一致/损益结平/未知科目）+ 期末余额构建；
- 观测：真实资产负债表报告的资产总计/负债合计/权益合计（按 +5 年列标签映射）；
- 残差：分录级余额与报表列不一致 → 告警并交人工解释（不猜、不改数）。
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

ASSETS = "JL_资产余额"
LIABILITIES = "JL_负债余额"
EQUITY = "JL_权益余额"
STATE_KEYS = (ASSETS, LIABILITIES, EQUITY)

CLASSIFY_RULE = "LEDGER_CLASSIFICATION"
VERSIONS = (
    RuleVersion(CLASSIFY_RULE, "V1",
                {"asset_prefixes": "1,5", "liability_prefixes": "2",
                 "equity_prefixes": "4", "pnl_prefixes": "6"},
                date(2000, 1, 1), None, "ALL",
                "科目编码首位分类：1/5 资产(含开发成本)、2 负债、4 权益、6 损益；损益应结平"),
)
SKELETON = ("CHECK_JOURNAL", "BUILD_BALANCE")


class FinanceLedgerDomain(Domain):
    name = "finance_ledger"
    goal = "LEDGER_TO_REPORT_CLOSE"
    observation_keys = STATE_KEYS
    required_fact_keys = (
        "J:资产余额", "J:负债余额", "J:权益余额",
        "J:凭证数", "J:不平衡凭证数", "J:金额不符凭证数",
        "J:年度借方", "J:年度贷方", "J:损益余额", "J:未知科目数",
    )

    def rule_hashes(self, business_date: date) -> Mapping[str, str]:
        return RuleBook(VERSIONS).hashes()

    def resolve_hypotheses(self, business_date: date, entity: Optional[str]) -> HypothesisSet:
        versions = RuleBook(VERSIONS).applicable(CLASSIFY_RULE, business_date, entity_scope=None)
        if not versions:
            raise LookupError("科目分类规则无适用版本（缺参，拒绝）")
        return HypothesisSet.from_rules(versions)

    def initial_state(self) -> StateSnapshot:
        return StateSnapshot("P00", {key: ZERO for key in STATE_KEYS})

    def transition(self, state: StateSnapshot, action: ActionOp,
                   params: Mapping[str, Any]) -> StateSnapshot:
        values = {key: state.get(key) for key in STATE_KEYS}
        if action.name == "CHECK_JOURNAL":
            return state.with_values(values)
        if action.name == "BUILD_BALANCE":
            values[ASSETS] = q(action.decimal("assets"))
            values[LIABILITIES] = q(action.decimal("liabilities"))
            values[EQUITY] = q(action.decimal("equity"))
            return state.with_values(values)
        raise ValueError(f"分录域未知动作: {action.name}")

    def violations(self, state: StateSnapshot, action: ActionOp,
                   params: Mapping[str, Any]) -> Tuple[str, ...]:
        if action.name != "CHECK_JOURNAL":
            return ()
        codes = []
        debit_total = q(action.decimal("debit_total"))
        credit_total = q(action.decimal("credit_total"))
        if debit_total != credit_total:
            codes.append("JOURNAL_NOT_BALANCED")
        if q(action.decimal("unbalanced_vouchers")) > ZERO:
            codes.append("UNBALANCED_VOUCHERS")
        if q(action.decimal("voucher_total_mismatch")) > ZERO:
            codes.append("VOUCHER_TOTAL_MISMATCH")
        if abs(q(action.decimal("pnl_balance"))) > q("0.01"):
            codes.append("PNL_NOT_CLOSED")
        if q(action.decimal("unknown_code_count")) > ZERO:
            codes.append("UNKNOWN_ACCOUNT_CODE")
        return tuple(codes)

    def plan(self, state: StateSnapshot, facts: Mapping[str, Decimal],
             hypotheses: HypothesisSet, business_date: date) -> Plan:
        check = ActionOp("CHECK_JOURNAL", {
            "debit_total": q(facts["J:年度借方"]),
            "credit_total": q(facts["J:年度贷方"]),
            "voucher_count": q(facts["J:凭证数"]),
            "unbalanced_vouchers": q(facts["J:不平衡凭证数"]),
            "voucher_total_mismatch": q(facts["J:金额不符凭证数"]),
            "pnl_balance": q(facts["J:损益余额"]),
            "unknown_code_count": q(facts["J:未知科目数"]),
        })
        build = ActionOp("BUILD_BALANCE", {
            "assets": q(facts["J:资产余额"]),
            "liabilities": q(facts["J:负债余额"]),
            "equity": q(facts["J:权益余额"]),
        })
        return Plan(
            period=state.period, goal=self.goal,
            actions=(PlannedAction("CHECK_JOURNAL", "凭证完整性检查", {"*": check}),
                     PlannedAction("BUILD_BALANCE", "构建期末余额", {"*": build})),
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
        return 1  # 余额构建之后即观测口径


class FinanceLedgerEnvironment(Environment):
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
                          facts=dict(self.facts[period]), source="full_journal")

    def execute(self, period: str, action: ActionOp, params: Mapping[str, Any]) -> None:
        self.executed[period].append((action.name, dict(action.params)))

    def finalize(self, period: str) -> Tuple[Observation, int]:
        return Observation(period=period, values=dict(self.declared[period]),
                           source="reported_balance_sheet", received_at=period), 0

    def confirm(self, period: str, hypotheses: HypothesisSet) -> HypothesisSet:
        return hypotheses

    def truth(self, period: str) -> Mapping[str, Decimal]:
        return self.declared[period]
