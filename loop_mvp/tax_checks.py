"""企业所得税年度申报表（A100000）逐级复算校验（纯函数，域无关）。

真实税务域第一步：先证明"我们的转移模型能复现真实申报表"，再接闭环。
只做表内可精确重算的行间关系，不判断税务处理是否正确、不构成税务意见。

覆盖链路（每条都是申报表自带的计算式）：
    营业利润 -> 利润总额 -> 纳税调整后所得 -> 应纳税所得额 -> 应纳所得税额
    -> 应纳税额 -> 实际应纳所得税额 -> 本年应补（退）所得税额 -> 本年实际应补（退）
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .real_checks import CheckResult, canonical_label, to_decimal
from .types import ZERO, q

YEAR_RE = re.compile(r"(20\d{2})\s*年")

# 行定位关键字（前缀匹配；按申报表行序取第一个）
K = {
    "revenue": "营业收入",
    "cost": "营业成本",
    "surtax": "税金及附加",
    "selling": "销售费用",
    "admin": "管理费用",
    "rnd": "研发费用",
    "finance": "财务费用",
    "other_income": "其他收益",
    "investment": "投资收益",
    "hedge": "净敞口套期收益",
    "fair_value": "公允价值变动收益",
    "credit_impair": "信用减值损失",
    "asset_impair": "资产减值损失",
    "disposal": "资产处置收益",
    "operating_profit": "营业利润",
    "nonop_income": "营业外收入",
    "nonop_expense": "营业外支出",
    "total_profit": "利润总额",
    "overseas_income": "境外所得",
    "adj_add": "纳税调整增加额",
    "adj_reduce": "纳税调整减少额",
    "tax_exempt": "免税、减计收入及加计扣除",
    "overseas_offset": "境外应税所得抵减境内亏损",
    "adjusted_income": "纳税调整后所得",
    "income_reduction": "所得减免",
    "loss_offset": "弥补以前年度亏损",
    "taxable_deduction": "抵扣应纳税所得额",
    "taxable_income": "应纳税所得额",
    "tax_rate": "税率",
    "tax_due": "应纳所得税额",
    "tax_exemption": "减免所得税额",
    "tax_credit": "抵免所得税额",
    "tax_payable": "应纳税额",
    "overseas_tax": "境外所得应纳所得税额",
    "overseas_credit": "境外所得抵免所得税额",
    "actual_due": "实际应纳所得税额",
    "paid": "本年累计实际已缴纳",
    "refund": "本年应补（退）所得税额",
    "final_refund": "本年实际应补（退）所得税额",
    "ethnic": "民族自治地区",
    "audit": "稽查查补",
    "special": "特别纳税调整",
}

DEFAULT_TOLERANCE = Decimal("0.01")  # 分位四舍五入容差


@dataclass(frozen=True)
class CitReturnTable:
    years: Tuple[str, ...]
    rows: Mapping[str, Mapping[str, Decimal]]

    def get(self, keyword: str, year: str) -> Optional[Decimal]:
        for label, series in self.rows.items():
            if label.startswith(keyword):
                return series.get(year)
        return None

    def get0(self, keyword: str, year: str) -> Decimal:
        """缺项按 0（用于公式分量）；目标行仍需 get 判缺项。"""
        value = self.get(keyword, year)
        return ZERO if value is None else value


def parse_cit_return_rows(rows: Sequence[Sequence[Any]]) -> CitReturnTable:
    if not rows:
        raise ValueError("A100000 为空")
    header = list(rows[0])
    year_cols: Dict[int, str] = {}
    for idx, cell in enumerate(header):
        match = YEAR_RE.search(str(cell or ""))
        if match:
            year_cols[idx] = match.group(1)
    if not year_cols:
        raise ValueError("A100000 表头未识别到年份")
    label_col = min(year_cols) - 1
    width = len(header)
    table: Dict[str, Dict[str, Decimal]] = {}
    for row in rows[1:]:
        cells = list(row) + [None] * (width - len(row))
        label = canonical_label(cells[label_col]) if label_col < width else ""
        if not label:
            continue
        for col, year in year_cols.items():
            if col >= width:
                continue
            value = to_decimal(cells[col])
            if value is not None:
                table.setdefault(label, {})[year] = value
    return CitReturnTable(tuple(sorted(set(year_cols.values()))), table)


def _fmt(value: Optional[Decimal]) -> str:
    return "" if value is None else format(q(value), "f")


def _cmp_tol(code: str, year: str, expected: Optional[Decimal], actual: Optional[Decimal],
             note: str = "", tol: Decimal = DEFAULT_TOLERANCE,
             missing: str = "") -> CheckResult:
    if expected is None or actual is None:
        return CheckResult(code, year, "UNVERIFIABLE", _fmt(expected), _fmt(actual), "",
                           missing or note)
    delta = q(actual - expected)
    status = "PASS" if abs(delta) <= abs(tol) else "FAIL"
    return CheckResult(code, year, status, _fmt(expected), _fmt(actual), _fmt(delta), note)


def check_cit_return(table: CitReturnTable,
                     tolerance: Decimal = DEFAULT_TOLERANCE) -> List[CheckResult]:
    results: List[CheckResult] = []
    for year in table.years:
        revenue = table.get0(K["revenue"], year)
        missing_components = [
            K[key] for key in ("revenue", "cost", "surtax", "selling", "admin", "rnd", "finance")
            if table.get(K[key], year) is None
        ]
        operating_expected = q(
            revenue
            - table.get0(K["cost"], year)
            - table.get0(K["surtax"], year)
            - table.get0(K["selling"], year)
            - table.get0(K["admin"], year)
            - table.get0(K["rnd"], year)
            - table.get0(K["finance"], year)
            + table.get0(K["other_income"], year)
            + table.get0(K["investment"], year)
            + table.get0(K["hedge"], year)
            + table.get0(K["fair_value"], year)
            + table.get0(K["credit_impair"], year)
            + table.get0(K["asset_impair"], year)
            + table.get0(K["disposal"], year)
        )
        results.append(_cmp_tol(
            "CIT_OPERATING_PROFIT", year, operating_expected,
            table.get(K["operating_profit"], year),
            note="营业利润 = 收入−成本−税金及附加−期间费用+其他收益+投资收益等"
                 + (f"；缺项按 0：{missing_components}" if missing_components else ""),
            tol=tolerance))

        total_profit = table.get(K["total_profit"], year)
        results.append(_cmp_tol(
            "CIT_TOTAL_PROFIT", year,
            None if total_profit is None else q(operating_expected
                                                + table.get0(K["nonop_income"], year)
                                                - table.get0(K["nonop_expense"], year)),
            total_profit,
            note="利润总额 = 营业利润 + 营业外收入 − 营业外支出",
            tol=tolerance))

        adjusted = table.get(K["adjusted_income"], year)
        results.append(_cmp_tol(
            "CIT_ADJUSTED_INCOME", year,
            None if total_profit is None else q(
                total_profit
                - table.get0(K["overseas_income"], year)
                + table.get0(K["adj_add"], year)
                - table.get0(K["adj_reduce"], year)
                - table.get0(K["tax_exempt"], year)
                + table.get0(K["overseas_offset"], year)),
            adjusted,
            note="纳税调整后所得 = 利润总额 − 境外所得 + 调增 − 调减 − 免税减计 + 境外抵减",
            tol=tolerance))

        taxable = table.get(K["taxable_income"], year)
        expected_taxable: Optional[Decimal] = None
        if adjusted is not None:
            expected_taxable = max(ZERO, q(
                adjusted
                - table.get0(K["income_reduction"], year)
                - table.get0(K["loss_offset"], year)
                - table.get0(K["taxable_deduction"], year)))
        results.append(_cmp_tol(
            "CIT_TAXABLE_INCOME", year, expected_taxable, taxable,
            note="应纳税所得额 = max(0, 纳税调整后所得 − 所得减免 − 弥补亏损 − 抵扣)；亏损年度按 0",
            tol=tolerance))

        rate = table.get(K["tax_rate"], year)
        results.append(_cmp_tol(
            "CIT_TAX_DUE", year,
            None if taxable is None or rate is None else q(taxable * rate),
            table.get(K["tax_due"], year),
            note="应纳所得税额 = 应纳税所得额 × 税率（分位容差）",
            tol=tolerance))

        tax_due = table.get(K["tax_due"], year)
        results.append(_cmp_tol(
            "CIT_TAX_PAYABLE", year,
            None if tax_due is None else q(tax_due
                                           - table.get0(K["tax_exemption"], year)
                                           - table.get0(K["tax_credit"], year)),
            table.get(K["tax_payable"], year),
            note="应纳税额 = 应纳所得税额 − 减免 − 抵免",
            tol=tolerance))

        tax_payable = table.get(K["tax_payable"], year)
        results.append(_cmp_tol(
            "CIT_ACTUAL_DUE", year,
            None if tax_payable is None else q(tax_payable
                                               + table.get0(K["overseas_tax"], year)
                                               - table.get0(K["overseas_credit"], year)),
            table.get(K["actual_due"], year),
            note="实际应纳所得税额 = 应纳税额 + 境外应纳 − 境外抵免",
            tol=tolerance))

        actual_due = table.get(K["actual_due"], year)
        results.append(_cmp_tol(
            "CIT_REFUND", year,
            None if actual_due is None else q(actual_due - table.get0(K["paid"], year)),
            table.get(K["refund"], year),
            note="本年应补（退）= 实际应纳所得税额 − 本年累计已缴",
            tol=tolerance))

        refund = table.get(K["refund"], year)
        results.append(_cmp_tol(
            "CIT_FINAL_REFUND", year,
            None if refund is None else q(refund
                                          - table.get0(K["ethnic"], year)
                                          - table.get0(K["audit"], year)
                                          - table.get0(K["special"], year)),
            table.get(K["final_refund"], year),
            note="本年实际应补（退）= 应补（退）− 民族自治 − 稽查 − 特别纳税调整",
            tol=tolerance))
    return results
