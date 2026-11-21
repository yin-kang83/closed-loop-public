"""税务决策 MPC（**第一原则优先**：不做法外帮凶）。

## 价值地基（先于技术目标）

依据 `legalwiki-kb/docs/哲学基础.md`（第一原则）：
- 拒绝清单第 1 条"设计逃税/避税操作方案"、第 7 条"歪曲解读税收政策"；
- 公理"有边界"：系统必须知道什么不做，并拒绝得清楚；
- 公理"人最终决定"：机器只给合规选项与证据，不做不可逆决定。

## 因此本模块的硬边界（不是配置项）

1. **法定应纳税额不可优化**：`tax` 不能作为优化目标；候选的税额必须**等于**法定口径，
   低于法定值直接判合规违例（TAX_BELOW_STATUTORY），高于也只是不必要，不允许当卖点。
2. **候选白名单，每项必须有官方依据**：只允许程序性时点选择——
   - 多缴税款：申请退税 / 抵缴下一年度（《税收征收管理法》第五十一条退还；多地税务机关答疑）；
   - 预缴方式：其他预缴方式需税务机关核定（行政许可/办事指南），未核定即用属违例；
3. 不生成"税务筹划/避税"候选；不教操作，只讲合规；输出附"不构成税务意见、以主管税务机关为准"；
4. 最终动作由人确认执行（本模块只出选项与依据）。

目标口径：``compliance``（硬约束，违例直接排除）与 ``cash``（资金占用成本）可优化；
``tax`` 是**校验项**，不是优化项——``validate_objectives`` 会直接拒绝把 tax 当目标。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Mapping, Tuple

from .types import Decimal, ZERO, dec, q

DAYS_IN_YEAR = Decimal("365")
DEFAULT_CAPITAL_RATE = Decimal("0.04")  # 资金成本率：配置假设，非事实
DEFAULT_REFUND_DELAY_DAYS = 90          # 退税到账延迟：配置假设，非事实
DEFAULT_OFFSET_EFFECT_DAYS = 365        # 抵缴生效日近似：配置假设，非事实

# 允许被优化的目标：合规（硬）与现金（合法程序性时点选择）
OPTIMIZABLE_OBJECTIVES: Tuple[str, ...] = ("compliance", "cash")

# 候选的官方依据（每项候选必须能落到其中一条；来源见 eval/tax_mpc_report.md）
LEGAL_BASIS: Dict[str, str] = {
    "多缴申请退税": "《税收征收管理法》第五十一条：多缴税款应予退还/可申请退还",
    "多缴抵缴下一年度": "税务机关答疑：汇算清缴多缴税款可抵缴以后年度应缴企业所得税",
    "按实际利润额预缴": "企业所得税预缴常规方式（国家税务总局关于加强企业所得税预缴工作的通知）",
    "其他方式需核定": "对采取实际利润额预缴以外的其他企业所得税预缴方式的核定（税务行政许可事项）",
}


def validate_objectives(objectives: Tuple[str, ...]) -> None:
    """拒绝把税务合规/法定税负当"优化变量"——这是哲学边界，不是参数。"""
    invalid = [name for name in objectives if name not in OPTIMIZABLE_OBJECTIVES]
    if invalid:
        raise ValueError(
            "税务决策只允许优化 compliance/cash；法定税负(tax)是硬校验、不可优化，"
            f"以下目标被第一原则拒绝：{invalid}"
        )


@dataclass(frozen=True)
class TaxOption:
    label: str
    scores: Mapping[str, Decimal]
    legal_basis: str


@dataclass(frozen=True)
class OverpaymentDecision:
    """多缴税款处理：申请退税 或 抵缴下一年度（不得重复主张、不得超过多缴额）。"""

    overpayment: Decimal
    next_year_tax_due: Decimal
    capital_rate: Decimal = DEFAULT_CAPITAL_RATE
    refund_delay_days: int = DEFAULT_REFUND_DELAY_DAYS
    offset_effect_days: int = DEFAULT_OFFSET_EFFECT_DAYS

    def statutory_tax(self) -> Decimal:
        return q(self.next_year_tax_due)

    def candidates(self) -> List[Tuple[str, Mapping[str, Decimal]]]:
        over = q(self.overpayment)
        tax = self.statutory_tax()
        return [
            ("全额申请退税", {"refund": over, "offset": ZERO, "tax": tax},
             LEGAL_BASIS["多缴申请退税"]),
            ("全额抵缴下一年度", {"refund": ZERO, "offset": over, "tax": tax},
             LEGAL_BASIS["多缴抵缴下一年度"]),
            ("超额抵缴（违规示例）", {"refund": ZERO, "offset": q(over + q("1000")), "tax": tax},
             "无依据：超过多缴税款抵缴，属违规示例，仅用于验证闸门"),
        ]

    def options(self) -> List[TaxOption]:
        return [TaxOption(label, scores, basis) for label, scores, basis in self.candidates()]

    def compliance_violations(self, scores: Mapping[str, Decimal]) -> Tuple[str, ...]:
        refund = q(scores.get("refund", ZERO))
        offset = q(scores.get("offset", ZERO))
        tax = q(scores.get("tax", ZERO))
        codes: List[str] = []
        if refund < ZERO or offset < ZERO:
            codes.append("NEGATIVE_AMOUNT")
        if refund + offset > q(self.overpayment):
            codes.append("OVER_CLAIM")
        if offset > q(self.next_year_tax_due):
            codes.append("OFFSET_OVER_DUE")
        if refund > ZERO and offset > ZERO:
            codes.append("DOUBLE_CLAIM")
        # 第一原则：法定应纳税额不可被"优化"降低
        if tax < self.statutory_tax():
            codes.append("TAX_BELOW_STATUTORY")
        elif tax > self.statutory_tax():
            codes.append("TAX_NOT_STATUTORY")
        return tuple(codes)

    def cash_cost(self, scores: Mapping[str, Decimal]) -> Decimal:
        refund_hold = q(q(scores.get("refund", ZERO)) * self.capital_rate
                        * dec(self.refund_delay_days) / DAYS_IN_YEAR)
        offset_hold = q(q(scores.get("offset", ZERO)) * self.capital_rate
                        * dec(self.offset_effect_days) / DAYS_IN_YEAR)
        return q(refund_hold + offset_hold)

    def scored(self) -> List[Tuple[str, Dict[str, Decimal]]]:
        out: List[Tuple[str, Dict[str, Decimal]]] = []
        for label, scores, _basis in self.candidates():
            values = dict(scores)
            values["compliance"] = dec(len(self.compliance_violations(scores)))
            values["cash"] = self.cash_cost(scores)
            out.append((label, values))
        return out


@dataclass(frozen=True)
class PrepaymentMethodDecision:
    """预缴方式：按实际利润额（常规）vs 其他方式（须税务机关核定）。"""

    annual_tax_due: Decimal
    actual_profit_prepaid: Decimal
    average_prior_year_prepaid: Decimal
    capital_rate: Decimal = DEFAULT_CAPITAL_RATE
    settlement_lag_days: int = 365
    other_method_approved: bool = False

    def statutory_tax(self) -> Decimal:
        return q(self.annual_tax_due)

    def candidates(self) -> List[Tuple[str, Mapping[str, Decimal]]]:
        tax = self.statutory_tax()
        return [
            ("按实际利润额预缴", {"prepaid": q(self.actual_profit_prepaid),
                                  "tax": tax, "approved": ZERO},
             LEGAL_BASIS["按实际利润额预缴"]),
            ("按上年度平均额预缴（需核定）",
             {"prepaid": q(self.average_prior_year_prepaid), "tax": tax,
              "approved": dec("1") if not self.other_method_approved else ZERO},
             LEGAL_BASIS["其他方式需核定"]),
        ]

    def options(self) -> List[TaxOption]:
        return [TaxOption(label, scores, basis) for label, scores, basis in self.candidates()]

    def compliance_violations(self, scores: Mapping[str, Decimal]) -> Tuple[str, ...]:
        codes: List[str] = []
        if q(scores.get("approved", ZERO)) > ZERO:
            codes.append("METHOD_NOT_APPROVED")
        if q(scores.get("prepaid", ZERO)) < ZERO:
            codes.append("NEGATIVE_PREPAID")
        tax = q(scores.get("tax", ZERO))
        if tax < self.statutory_tax():
            codes.append("TAX_BELOW_STATUTORY")
        elif tax > self.statutory_tax():
            codes.append("TAX_NOT_STATUTORY")
        return tuple(codes)

    def cash_cost(self, scores: Mapping[str, Decimal]) -> Decimal:
        over = q(q(scores.get("prepaid", ZERO)) - self.statutory_tax())
        return q(over * self.capital_rate * dec(self.settlement_lag_days) / DAYS_IN_YEAR)

    def scored(self) -> List[Tuple[str, Dict[str, Decimal]]]:
        out: List[Tuple[str, Dict[str, Decimal]]] = []
        for label, scores, _basis in self.candidates():
            values = dict(scores)
            values["compliance"] = dec(len(self.compliance_violations(scores)))
            values["cash"] = self.cash_cost(scores)
            out.append((label, values))
        return out
