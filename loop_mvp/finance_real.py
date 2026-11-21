"""财务真实账套闭环域（真实报表数据）：报表合计复算 → 未分配利润结转 → 观测对账。

只做**确定性复算与对账**，不判断会计处理是否正确、不构成审计意见：
- 状态：期末未分配利润（跨年结转）；
- 动作：构建报表合计（资产/负债/权益/负债和权益总计）、未分配利润结转、期末关账；
- 观测：真实报表报告的"负债和所有者权益总计"与"未分配利润"；
- 残差：合计/结转与报告数不一致 → 告警并交人工解释（不猜、不改数）。

2026 在编列不作为观测（列状态 DRAFT），本域只跑已结账年份。
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

# 报表合计（已结账列）
A_TOTAL = "FR_资产总计"
L_TOTAL = "FR_负债合计"
E_TOTAL = "FR_权益合计"
LE_TOTAL = "FR_负债和所有者权益总计"
EXPECTED_RE = "FR_预期未分配利润"
CLOSING_RE = "FR_期末未分配利润"
STATE_KEYS = (A_TOTAL, L_TOTAL, E_TOTAL, LE_TOTAL, EXPECTED_RE, CLOSING_RE)

DIVIDEND_RULE = "DIVIDEND_POLICY"
VERSIONS = (
    RuleVersion(DIVIDEND_RULE, "V1", {"dividend": dec("0")}, date(2000, 1, 1), None, "ALL",
                "假设：观察期内无分红（2023-2024 与数据一致；其他权益变动需人工解释）"),
)
SKELETON = ("BUILD_TOTALS", "ROLL_FORWARD_RE", "CLOSE_YEAR")

# 资产负债表项目分类（真实报表标签；adapter 用 L:/R: 前缀写入事实）
CURRENT_ASSETS = (
    "货币资金", "交易性金融资产", "应收票据", "应收账款", "预付款项", "应收利息",
    "应收股利", "其他应收款", "存货", "一年内到期的非流动资产", "其他流动资产",
)
NONCURRENT_ASSETS = (
    "可供出售金融资产", "持有至到期投资", "长期应收款", "长期股权投资", "投资性房地产",
    "固定资产", "在建工程", "工程物资", "固定资产清理", "生产性生物资产", "油气资产",
    "无形资产", "开发支出", "商誉", "长期待摊费用", "递延所得税资产", "其他非流动资产",
)
CURRENT_LIABILITIES = (
    "短期借款", "交易性金融负债", "应付票据", "应付账款", "预收款项", "应付职工薪酬",
    "应交税费", "应付利息", "应付股利", "其他应付款", "一年内到期的非流动负债", "其他流动负债",
)
NONCURRENT_LIABILITIES = (
    "长期借款", "应付债券", "长期应付款", "专项应付款", "预计负债",
    "递延所得税负债", "其他非流动负债",
)
EQUITY_ITEMS = (
    ("实收资本（或股本）", 1), ("资本公积", 1), ("盈余公积", 1), ("未分配利润", 1),
    ("减：库存股", -1),
)
REPORTED_SUBTOTALS = {
    "流动资产合计": "L", "非流动资产合计": "L", "资产总计": "L",
    "流动负债合计": "R", "非流动负债合计": "R", "负债合计": "R",
    "所有者权益（或股东权益）合计": "R", "负债和所有者权益（或股东权益）总计": "R",
}
KNOWN_LABELS = (set(CURRENT_ASSETS) | set(NONCURRENT_ASSETS) | set(CURRENT_LIABILITIES)
                | set(NONCURRENT_LIABILITIES) | {label for label, _sign in EQUITY_ITEMS}
                | set(REPORTED_SUBTOTALS))


def _sum_items(facts: Mapping[str, Decimal], prefix: str, labels) -> Tuple[Decimal, int]:
    total = ZERO
    for label in labels:
        value = facts.get(prefix + label)
        if value is not None:
            total += q(value)
    return q(total), 0


def _unknown_accounts(facts: Mapping[str, Decimal]) -> List[str]:
    unknown: List[str] = []
    for key in facts:
        if not (key.startswith("L:") or key.startswith("R:")):
            continue
        label = key[2:]
        if label not in KNOWN_LABELS:
            unknown.append(key)
    return sorted(unknown)


class FinanceRealDomain(Domain):
    name = "finance_real"
    goal = "ANNUAL_REPORT_CLOSE"
    observation_keys = (LE_TOTAL, EXPECTED_RE)
    required_fact_keys = ("P:净利润",)  # 其余报表事实由 adapter 全量写入

    def __init__(self, opening_re: Decimal = ZERO) -> None:
        self.opening_re = q(opening_re)

    # -- 规则 --------------------------------------------------------------
    def rule_hashes(self, business_date: date) -> Mapping[str, str]:
        return RuleBook(VERSIONS).hashes()

    def resolve_hypotheses(self, business_date: date, entity: Optional[str]) -> HypothesisSet:
        versions = RuleBook(VERSIONS).applicable(DIVIDEND_RULE, business_date, entity_scope=None)
        if not versions:
            raise LookupError("分红假设规则无适用版本（缺参，拒绝）")
        return HypothesisSet.from_rules(versions)

    # -- 状态与转移 --------------------------------------------------------
    def initial_state(self) -> StateSnapshot:
        return StateSnapshot("P00", {
            A_TOTAL: ZERO, L_TOTAL: ZERO, E_TOTAL: ZERO, LE_TOTAL: ZERO,
            EXPECTED_RE: ZERO, CLOSING_RE: self.opening_re,
        })

    def transition(self, state: StateSnapshot, action: ActionOp,
                   params: Mapping[str, Any]) -> StateSnapshot:
        values = {key: state.get(key) for key in STATE_KEYS}
        if action.name == "BUILD_TOTALS":
            values[A_TOTAL] = q(action.decimal("computed_total_assets"))
            values[L_TOTAL] = q(action.decimal("computed_total_liabilities"))
            values[E_TOTAL] = q(action.decimal("computed_equity"))
            values[LE_TOTAL] = q(values[L_TOTAL] + values[E_TOTAL])
        elif action.name == "ROLL_FORWARD_RE":
            net_profit = q(action.decimal("net_profit"))
            dividend = q(action.decimal("dividend"))
            values[EXPECTED_RE] = q(values[CLOSING_RE] + net_profit - dividend)
        elif action.name == "CLOSE_YEAR":
            values[CLOSING_RE] = q(action.decimal("reported_re"))
        else:
            raise ValueError(f"财务真实域未知动作: {action.name}")
        return state.with_values(values)

    def violations(self, state: StateSnapshot, action: ActionOp,
                   params: Mapping[str, Any]) -> Tuple[str, ...]:
        values = {key: state.get(key) for key in STATE_KEYS}
        codes = []
        if action.name != "BUILD_TOTALS":
            if action.name == "ROLL_FORWARD_RE" and action.decimal("dividend") < ZERO:
                codes.append("NEGATIVE_DIVIDEND")
            return tuple(codes)
        if int(action.decimal("unknown_accounts")) > 0:
            codes.append("UNKNOWN_ACCOUNT")
        if values[A_TOTAL] != q(values[L_TOTAL] + values[E_TOTAL]):
            codes.append("IDENTITY_BREACH")
        reported = {
            "current_assets": action.params.get("reported_current_assets"),
            "noncurrent_assets": action.params.get("reported_noncurrent_assets"),
            "total_assets": action.params.get("reported_total_assets"),
            "current_liabilities": action.params.get("reported_current_liabilities"),
            "noncurrent_liabilities": action.params.get("reported_noncurrent_liabilities"),
            "total_liabilities": action.params.get("reported_total_liabilities"),
            "equity": action.params.get("reported_equity"),
            "le_total": action.params.get("reported_le_total"),
            "computed_current_assets": action.params.get("computed_current_assets"),
            "computed_noncurrent_assets": action.params.get("computed_noncurrent_assets"),
            "computed_current_liabilities": action.params.get("computed_current_liabilities"),
            "computed_noncurrent_liabilities": action.params.get("computed_noncurrent_liabilities"),
        }
        for computed_key, reported_key, code in (
            ("computed_current_assets", "current_assets", "ASSET_CURRENT_SUBTOTAL_MISMATCH"),
            ("computed_noncurrent_assets", "noncurrent_assets", "ASSET_NONCURRENT_SUBTOTAL_MISMATCH"),
            ("computed_current_liabilities", "current_liabilities", "LIABILITY_CURRENT_SUBTOTAL_MISMATCH"),
            ("computed_noncurrent_liabilities", "noncurrent_liabilities",
             "LIABILITY_NONCURRENT_SUBTOTAL_MISMATCH"),
        ):
            computed = reported[computed_key]
            expected = reported[reported_key]
            if computed is not None and expected is not None and q(computed) != q(expected):
                codes.append(code)
        if reported["total_assets"] is not None and values[A_TOTAL] != q(reported["total_assets"]):
            codes.append("ASSET_TOTAL_MISMATCH")
        if reported["total_liabilities"] is not None and values[L_TOTAL] != q(reported["total_liabilities"]):
            codes.append("LIABILITY_TOTAL_MISMATCH")
        if reported["equity"] is not None and values[E_TOTAL] != q(reported["equity"]):
            codes.append("EQUITY_TOTAL_MISMATCH")
        if (reported["le_total"] is not None
                and q(reported["total_liabilities"] or ZERO) + q(reported["equity"] or ZERO)
                != q(reported["le_total"])):
            codes.append("LE_TOTAL_MISMATCH")
        return tuple(codes)

    # -- 规划 / 经验复用 ---------------------------------------------------
    def plan(self, state: StateSnapshot, facts: Mapping[str, Decimal],
             hypotheses: HypothesisSet, business_date: date) -> Plan:
        dividend = hypotheses.single().decimal("dividend")
        current_assets, _ = _sum_items(facts, "L:", CURRENT_ASSETS)
        noncurrent_assets, _ = _sum_items(facts, "L:", NONCURRENT_ASSETS)
        current_liabilities, _ = _sum_items(facts, "R:", CURRENT_LIABILITIES)
        noncurrent_liabilities, _ = _sum_items(facts, "R:", NONCURRENT_LIABILITIES)
        equity = ZERO
        for label, sign in EQUITY_ITEMS:
            value = facts.get("R:" + label)
            if value is not None:
                equity += q(value) * sign
        total_assets = q(current_assets + noncurrent_assets)
        total_liabilities = q(current_liabilities + noncurrent_liabilities)
        unknown = _unknown_accounts(facts)
        reported = {name: facts.get(side + ":" + name)
                    for name, side in REPORTED_SUBTOTALS.items()}
        net_profit = facts.get("P:净利润")
        reported_re = facts.get("R:未分配利润")
        build_params: Dict[str, Any] = {
            "computed_current_assets": current_assets,
            "computed_noncurrent_assets": noncurrent_assets,
            "computed_total_assets": total_assets,
            "computed_current_liabilities": current_liabilities,
            "computed_noncurrent_liabilities": noncurrent_liabilities,
            "computed_total_liabilities": total_liabilities,
            "computed_equity": q(equity),
            "unknown_accounts": q(len(unknown)),
            "reported_current_assets": reported["流动资产合计"],
            "reported_noncurrent_assets": reported["非流动资产合计"],
            "reported_total_assets": reported["资产总计"],
            "reported_current_liabilities": reported["流动负债合计"],
            "reported_noncurrent_liabilities": reported["非流动负债合计"],
            "reported_total_liabilities": reported["负债合计"],
            "reported_equity": reported["所有者权益（或股东权益）合计"],
            "reported_le_total": reported["负债和所有者权益（或股东权益）总计"],
        }
        if net_profit is None or reported_re is None:
            raise ValueError("缺少净利润或未分配利润事实（拒绝默认）")
        actions = (
            PlannedAction("BUILD_TOTALS", "构建报表合计", {"*": ActionOp("BUILD_TOTALS", build_params)}),
            PlannedAction("ROLL_FORWARD_RE", "未分配利润结转", {"*": ActionOp("ROLL_FORWARD_RE", {
                "net_profit": q(net_profit), "dividend": q(dividend)})}),
            PlannedAction("CLOSE_YEAR", "期末关账", {"*": ActionOp("CLOSE_YEAR", {
                "reported_re": q(reported_re)})}),
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
        return 1  # 结转之后即观测口径


class FinanceRealEnvironment(Environment):
    """真实报表环境：facts=报表项目，declared=报告合计/未分配利润（观测）。"""

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
                          facts=dict(self.facts[period]), source="report_extract")

    def execute(self, period: str, action: ActionOp, params: Mapping[str, Any]) -> None:
        self.executed[period].append((action.name, dict(action.params)))

    def finalize(self, period: str) -> Tuple[Observation, int]:
        return Observation(period=period, values=dict(self.declared[period]),
                           source="reported_financial_statements", received_at=period), 0

    def confirm(self, period: str, hypotheses: HypothesisSet) -> HypothesisSet:
        return hypotheses

    def truth(self, period: str) -> Mapping[str, Decimal]:
        return self.declared[period]
