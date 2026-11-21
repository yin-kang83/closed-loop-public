"""财务结账域包：会计恒等式 + 版本化税率 + 双时态事实。

域内动作（一个账期）：缴纳上期税款 → 确认收入 → 结转成本 → 计提所得税 → 期末结转。

不变量（闸门检查的"真理标尺"）：
- IDENTITY：资产 = 负债 + 权益 +（收入 − 成本 − 所得税）——借贷记账的定义式；
- NEGATIVE_BALANCE：现金/应收/存货/应交税费/所得税费用不得为负；
- TAX_MISMATCH：计提所得税后，应交税费必须等于本期所得税费用；
- SETTLE_MISMATCH：缴税后应交税费必须清零；
- PERIOD_NOT_CLOSED：期末结转后损益类科目必须全为零。

税率规则版本（跨期换版 + 主体范围歧义 = 假设集来源）：
V1 25% 2026-01-01~03-31；V2 25% / V3 20% 2026-04（一般/小微，主体范围未知 → 歧义）；
V4 20% 2026-05 起（政策落地，统一适用）。
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
    ResidualReport,
    StateSnapshot,
    ZERO,
    dec,
    q,
)

# 科目（与真实账套同一套借贷语义）
A_CASH = "1001_现金"
A_AR = "1122_应收账款"
A_INV = "1405_存货"
A_TAX = "2221_应交税费"
A_CAP = "4001_实收资本"
A_RE = "4104_未分配利润"
A_REV = "6001_营业收入"
A_COST = "6401_营业成本"
A_INCTAX = "6801_所得税费用"

ALL_ACCOUNTS = (A_CASH, A_AR, A_INV, A_TAX, A_CAP, A_RE, A_REV, A_COST, A_INCTAX)

OPENING = {
    A_CASH: q("10000"),
    A_INV: q("5000"),
    A_CAP: q("10000"),
    A_RE: q("5000"),
}

RATE_RULE = "CIT_RATE"


def _rate_version(version: str, rate: str, start: date, end: Optional[date],
                  scope: str, citation: str) -> RuleVersion:
    return RuleVersion(
        rule_id=RATE_RULE,
        version=version,
        params={"rate": dec(rate)},
        effective_from=start,
        effective_to=end,
        entity_scope=scope,
        citation=citation,
    )


VERSIONS = (
    _rate_version("V1", "0.25", date(2026, 1, 1), date(2026, 3, 31), "ALL", "2026Q1 法定税率 25%"),
    _rate_version("V2", "0.25", date(2026, 4, 1), date(2026, 4, 30), "GENERAL", "一般企业 25%"),
    _rate_version("V3", "0.20", date(2026, 4, 1), date(2026, 4, 30), "SMALL", "小微企业优惠 20%"),
    _rate_version("V4", "0.20", date(2026, 5, 1), None, "ALL", "政策落地：统一适用 20%"),
)


class FinanceDomain(Domain):
    name = "finance_close"
    goal = "MONTHLY_CLOSE"
    observation_keys = (A_RE, A_TAX)
    required_fact_keys = ("revenue", "cost")
    SKELETON = ("SETTLE_TAX", "REVENUE", "COST", "TAX", "CLOSE")

    # -- 规则 --------------------------------------------------------------
    def rule_book(self, business_date: date) -> RuleBook:
        """只把"业务日之前已生效"的版本视为已知（新版本生效 = 规则哈希变化）。"""
        known = tuple(v for v in VERSIONS if v.effective_from <= business_date)
        return RuleBook(known)

    def rule_hashes(self, business_date: date) -> Mapping[str, str]:
        return self.rule_book(business_date).hashes()

    def resolve_hypotheses(self, business_date: date, entity: Optional[str]) -> HypothesisSet:
        book = self.rule_book(business_date)
        versions = book.applicable(RATE_RULE, business_date, entity_scope=None)
        if not versions:
            raise LookupError(f"税率规则在 {business_date} 无适用版本（缺参，拒绝）")
        return HypothesisSet.from_rules(versions)

    # -- 状态与转移 --------------------------------------------------------
    def initial_state(self) -> StateSnapshot:
        return StateSnapshot(period="P00", values=OPENING)

    def transition(self, state: StateSnapshot, action: ActionOp,
                   params: Mapping[str, Any]) -> StateSnapshot:
        values = {key: state.get(key) for key in ALL_ACCOUNTS}
        name = action.name
        if name == "SETTLE_TAX":
            amount = action.decimal("amount")
            values[A_TAX] -= amount
            values[A_CASH] -= amount
        elif name == "REVENUE":
            amount = action.decimal("amount")
            values[A_AR] += amount
            values[A_REV] += amount
        elif name == "COST":
            amount = action.decimal("amount")
            values[A_COST] += amount
            values[A_INV] -= amount
        elif name == "TAX":
            amount = action.decimal("amount")
            values[A_INCTAX] += amount
            values[A_TAX] += amount
        elif name == "CLOSE":
            net = q(values[A_REV] - values[A_COST] - values[A_INCTAX])
            values[A_RE] += net
            values[A_REV] = ZERO
            values[A_COST] = ZERO
            values[A_INCTAX] = ZERO
        elif name == "REVERSAL":
            # 红字冲销：冲减多记的应收/应交税费，并把差额调回未分配利润
            revenue_error = action.decimal("revenue_error")
            tax_error = action.decimal("tax_error")
            values[A_AR] -= revenue_error
            values[A_TAX] -= tax_error
            values[A_RE] -= q(revenue_error - tax_error)
        else:
            raise ValueError(f"财务域未知动作: {name}")
        return state.with_values({key: q(value) for key, value in values.items()})

    def violations(self, state: StateSnapshot, action: ActionOp,
                   params: Mapping[str, Any]) -> Tuple[str, ...]:
        v = {key: state.get(key) for key in ALL_ACCOUNTS}
        codes = []
        assets = q(v[A_CASH] + v[A_AR] + v[A_INV])
        liabilities = q(v[A_TAX])
        equity = q(v[A_CAP] + v[A_RE])
        profit = q(v[A_REV] - v[A_COST] - v[A_INCTAX])
        if assets != q(liabilities + equity + profit):
            codes.append("IDENTITY_BREACH")
        for key in (A_CASH, A_AR, A_INV, A_TAX, A_INCTAX, A_REV, A_COST):
            if v[key] < ZERO:
                codes.append("NEGATIVE_BALANCE")
                break
        if action.name == "TAX" and v[A_TAX] != v[A_INCTAX]:
            codes.append("TAX_MISMATCH")
        if action.name == "SETTLE_TAX" and v[A_TAX] != ZERO:
            codes.append("SETTLE_MISMATCH")
        if action.name == "CLOSE" and (v[A_REV] != ZERO or v[A_COST] != ZERO or v[A_INCTAX] != ZERO):
            codes.append("PERIOD_NOT_CLOSED")
        if action.name == "REVERSAL":
            settled = True
            if "target_re" in action.params and v[A_RE] != q(action.decimal("target_re")):
                settled = False
            if "target_tax" in action.params and v[A_TAX] != q(action.decimal("target_tax")):
                settled = False
            if not settled:
                codes.append("CORRECTION_NOT_SETTLED")
        return tuple(codes)

    def correction_action(
        self,
        report: ResidualReport,
        observation: Observation,
        state: StateSnapshot,
        hypotheses: HypothesisSet,
    ) -> Optional[ActionOp]:
        """从残差反解抽取误差：ΔRE = Δ收入 − Δ所得税；Δ应交税费 = Δ所得税。

        只有同期观测、且账面比实际"多记/少记"能被有符号残差解释时才生成冲销动作；
        冲销后必须让 RE/应交税费精确等于观测值，否则闸门会以 CORRECTION_NOT_SETTLED 拒绝。
        """
        if report.in_bound:
            return None
        observed_re = observation.values.get(A_RE)
        observed_tax = observation.values.get(A_TAX)
        if observed_re is None or observed_tax is None:
            return None
        delta_re = q(state.get(A_RE) - observed_re)
        delta_tax = q(state.get(A_TAX) - observed_tax)
        revenue_error = q(delta_re + delta_tax)
        if revenue_error == ZERO:
            return None
        if revenue_error > ZERO and state.get(A_AR) < revenue_error:
            return None
        if delta_tax > ZERO and state.get(A_TAX) < delta_tax:
            return None
        return ActionOp("REVERSAL", {
            "revenue_error": revenue_error,
            "tax_error": delta_tax,
            "re_error": delta_re,
            "target_re": observed_re,
            "target_tax": observed_tax,
        })

    # -- 规划与经验复用 ----------------------------------------------------
    def plan(self, state: StateSnapshot, facts: Mapping[str, Decimal],
             hypotheses: HypothesisSet, business_date: date) -> Plan:
        revenue = q(facts["revenue"])
        cost = q(facts["cost"])
        settle = q(state.get(A_TAX))
        taxable = q(revenue - cost)
        actions = [
            PlannedAction("SETTLE_TAX", "缴纳上期税款",
                          {"*": ActionOp("SETTLE_TAX", {"amount": settle})}),
            PlannedAction("REVENUE", "确认收入",
                          {"*": ActionOp("REVENUE", {"amount": revenue})}),
            PlannedAction("COST", "结转成本",
                          {"*": ActionOp("COST", {"amount": cost})}),
        ]
        tax_variants = {
            hyp.hyp_id: ActionOp("TAX", {"amount": q(taxable * hyp.decimal("rate")),
                                         "rate": hyp.decimal("rate")})
            for hyp in hypotheses.hypotheses
        }
        actions.append(PlannedAction("TAX", "计提所得税", tax_variants))
        actions.append(PlannedAction("CLOSE", "期末结转", {"*": ActionOp("CLOSE", {})}))
        return Plan(
            period=state.period,
            goal=self.goal,
            actions=tuple(actions),
            skeleton=self.SKELETON,
            rule_hashes=self.rule_hashes(business_date),
            fact_keys=self.required_fact_keys,
        )

    def instantiate(self, skeleton: Tuple[str, ...], state: StateSnapshot,
                    facts: Mapping[str, Decimal], hypotheses: HypothesisSet,
                    business_date: date) -> Plan:
        if tuple(skeleton) != self.SKELETON:
            raise ValueError(f"经验骨架不匹配：{skeleton} != {self.SKELETON}")
        return self.plan(state, facts, hypotheses, business_date)

    def observation_step(self, plan: Plan) -> int:
        return len(plan.actions) - 1 if plan.actions else 0


# ---------------------------------------------------------------------------
# 环境：真实税率/真实收入可能和抽取结果不同；回执可能延迟
# ---------------------------------------------------------------------------

class FinanceEnvironment(Environment):
    def __init__(self, periods: Tuple[str, ...],
                 data: Mapping[str, Mapping[str, Any]],
                 delays: Optional[Mapping[str, int]] = None) -> None:
        self.periods = tuple(periods)
        self.data = data
        self.delays = dict(delays or {})
        self.reset()

    def reset(self) -> None:
        self.executed: Dict[str, list] = {p: [] for p in self.periods}

    # -- 抽取 / 执行 --------------------------------------------------------
    def extract_facts(self, period: str, business_date: date) -> FactBundle:
        d = self.data[period]
        facts = {
            "revenue": d.get("extracted_revenue", d["true_revenue"]),
            "cost": d.get("extracted_cost", d["true_cost"]),
        }
        return FactBundle(period=period, business_date=business_date,
                          facts={k: dec(v) for k, v in facts.items()},
                          source="annual_report_extract")

    def execute(self, period: str, action: ActionOp, params: Mapping[str, Any]) -> None:
        self.executed[period].append((action.name, dict(action.params)))

    # -- 回执 / 确认 --------------------------------------------------------
    def finalize(self, period: str) -> Tuple[Observation, int]:
        obs = Observation(period=period, values=self._truth_values(period),
                          source="tax_receipt", received_at=period)
        return obs, self.delays.get(period, 0)

    def confirm(self, period: str, hypotheses: HypothesisSet) -> HypothesisSet:
        true_rate = dec(self.data[period]["true_rate"])
        kept = tuple(h for h in hypotheses.hypotheses if q(h.decimal("rate")) == q(true_rate))
        return HypothesisSet(kept) if kept else hypotheses

    def truth(self, period: str) -> Mapping[str, Decimal]:
        return self._truth_values(period)

    def _truth_values(self, period: str) -> Mapping[str, Decimal]:
        idx = self.periods.index(period)
        cumulative = ZERO
        for i in range(idx + 1):
            p = self.periods[i]
            d = self.data[p]
            revenue, cost = dec(d["true_revenue"]), dec(d["true_cost"])
            rate = dec(d["true_rate"])
            cumulative += q((revenue - cost) * (1 - rate))
        d = self.data[period]
        tax = q((dec(d["true_revenue"]) - dec(d["true_cost"])) * dec(d["true_rate"]))
        return {
            A_RE: q(dec("5000") + cumulative),
            A_TAX: tax,
        }


def default_scenario() -> Tuple[Tuple[str, ...], Dict[str, Dict[str, Any]], Dict[str, int], Dict[str, date]]:
    """演示/评测用的 6 个账期（含 1 次抽取错误 + 1 次外部确认 + 1 次回执延迟）。"""
    periods = ("P01", "P02", "P03", "P04", "P05", "P06")
    data: Dict[str, Dict[str, Any]] = {
        "P01": {"true_revenue": "1200", "true_cost": "700", "true_rate": "0.25"},
        "P02": {"true_revenue": "1500", "true_cost": "900", "true_rate": "0.25"},
        "P03": {"true_revenue": "900", "true_cost": "500", "true_rate": "0.25",
                "extracted_revenue": "1000"},
        "P04": {"true_revenue": "1800", "true_cost": "1000", "true_rate": "0.20"},
        "P05": {"true_revenue": "1400", "true_cost": "800", "true_rate": "0.20"},
        "P06": {"true_revenue": "1600", "true_cost": "950", "true_rate": "0.20"},
    }
    delays = {"P05": 1}
    dates = {
        "P01": date(2026, 1, 31),
        "P02": date(2026, 2, 28),
        "P03": date(2026, 3, 31),
        "P04": date(2026, 4, 30),
        "P05": date(2026, 5, 31),
        "P06": date(2026, 6, 30),
    }
    return periods, data, delays, dates
