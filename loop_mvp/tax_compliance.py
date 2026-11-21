"""申报一致性核对（真实数据，纯函数；**只报差异，不判违规、不做优化**）。

第一原则：本模块只做"可验证"的算术核对与差异展示，不输出税务筹划/避税建议，
不评价税务处理是否正确；涉税输出须附"不构成税务意见、以主管税务机关为准"。

三类检查：
1. 申报口径 vs 财报口径逐项差异（MATCH / DIFF / UNVERIFIABLE，DIFF 是正常税会差异，只提示需人工解释）；
2. 差异对比表自检：表内"申报−报表"是否等于两张源表的差（发现工作簿自相矛盾）；
3. 预缴准确性：季度预缴表内部公式一致性 + 预缴 vs 年度应纳差异（汇算补退属正常申报行为，不判违规）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .types import ZERO, dec, q

YEAR_RE = re.compile(r"(20\d{2})\s*年")
TOLERANCE = Decimal("0.01")

# 关键行（用于报告摘要；完整逐行在报告明细里）
KEY_ROWS = ("营业收入", "营业成本", "营业利润", "利润总额", "财务费用", "管理费用", "销售费用")

DISCLAIMER = (
    "本报告只做可复算的申报一致性核对，不判断税务处理是否正确、不构成税务意见、"
    "不做税务筹划/避税建议；涉税事项以主管税务机关为准。"
)


@dataclass(frozen=True)
class RowFigure:
    index: int
    label: str
    values: Dict[str, Decimal]


@dataclass(frozen=True)
class FigureTable:
    years: Tuple[str, ...]
    rows: Tuple[RowFigure, ...]

    def row(self, index: int) -> Optional[RowFigure]:
        for row in self.rows:
            if row.index == index:
                return row
        return None


@dataclass(frozen=True)
class ComplianceCheck:
    code: str
    year: str
    status: str  # MATCH / DIFF / UNVERIFIABLE / PASS / FLAG
    expected: str
    actual: str
    delta: str
    note: str = ""

    def to_dict(self) -> Dict[str, str]:
        return {"code": self.code, "year": self.year, "status": self.status,
                "expected": self.expected, "actual": self.actual,
                "delta": self.delta, "note": self.note}


def _fmt(value: Optional[Decimal]) -> str:
    return "" if value is None else format(q(value), "f")


def to_decimal(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return q(value)
    text = str(value).strip()
    if not text or text in {"-", "—", "/"}:
        return None
    text = text.replace(",", "").replace("，", "")
    try:
        return q(dec(text))
    except Exception:  # noqa: BLE001
        return None


def parse_figure_rows(rows: Sequence[Sequence[Any]], label_col: int = 1) -> FigureTable:
    """通用两列标签表：第 1 行表头含年份，数据行按年份取值。

    差异对比工作簿的 A/B/C 三张表结构一致（col0=申报项目、col1=财报项目），
    以行号对齐，label 取 col1（缺失时回退 col0）。
    """
    if not rows:
        raise ValueError("表为空")
    header = list(rows[0])
    year_cols: Dict[int, str] = {}
    for index, cell in enumerate(header):
        match = YEAR_RE.search(str(cell or ""))
        if match:
            year_cols[index] = match.group(1)
    if not year_cols:
        raise ValueError("表头未识别到年份")
    figures: List[RowFigure] = []
    for row_index, row in enumerate(rows[1:], start=1):
        cells = list(row) + [None] * (len(header) - len(row))
        label = str(cells[label_col] or cells[0] or "").strip()
        if not label:
            continue
        values: Dict[str, Decimal] = {}
        for column, year in year_cols.items():
            value = to_decimal(cells[column]) if column < len(cells) else None
            if value is not None:
                values[year] = value
        if values:
            figures.append(RowFigure(index=row_index, label=label, values=values))
    return FigureTable(tuple(sorted(set(year_cols.values()))), tuple(figures))


def compare_tables(
    tax_table: FigureTable,
    financial_table: FigureTable,
    code: str = "CMP",
    tolerance: Decimal = TOLERANCE,
) -> List[ComplianceCheck]:
    """申报口径 vs 财报口径逐项差异：DIFF 只表示差异存在，不判违规。"""
    checks: List[ComplianceCheck] = []
    for row in tax_table.rows:
        other = financial_table.row(row.index)
        label = row.label or (other.label if other else "")
        for year in tax_table.years:
            expected = row.values.get(year)
            actual = other.values.get(year) if other else None
            if expected is None or actual is None:
                checks.append(ComplianceCheck(
                    code, year, "UNVERIFIABLE", _fmt(expected), _fmt(actual), "",
                    f"{label}：一方缺项"))
                continue
            delta = q(expected - actual)
            status = "MATCH" if abs(delta) <= tolerance else "DIFF"
            note = f"{label}" + ("" if status == "MATCH" else "：存在税会差异，需人工解释（不判违规）")
            checks.append(ComplianceCheck(code, year, status, _fmt(expected), _fmt(actual),
                                          _fmt(delta), note))
    return checks


def check_difference_sheet(
    diff_table: FigureTable,
    tax_table: FigureTable,
    financial_table: FigureTable,
    tolerance: Decimal = TOLERANCE,
) -> List[ComplianceCheck]:
    """表内自检：差异对比表的数必须等于 申报口径 − 财报口径（否则工作簿自相矛盾）。"""
    checks: List[ComplianceCheck] = []
    for row in diff_table.rows:
        tax_row = tax_table.row(row.index)
        fin_row = financial_table.row(row.index)
        label = row.label or (tax_row.label if tax_row else "")
        for year in diff_table.years:
            stored = row.values.get(year)
            tax_value = tax_row.values.get(year) if tax_row else None
            fin_value = fin_row.values.get(year) if fin_row else None
            if stored is None or tax_value is None or fin_value is None:
                continue  # 缺项行不参与自检
            expected = q(tax_value - fin_value)
            delta = q(stored - expected)
            status = "PASS" if abs(delta) <= tolerance else "FLAG"
            note = f"{label}：差异对比表 = 申报 − 报表" + (
                "" if status == "PASS" else "；表内不一致（数据质量问题，需核对底稿）")
            checks.append(ComplianceCheck("DIFF_SHEET", year, status, _fmt(expected),
                                          _fmt(stored), _fmt(delta), note))
    return checks


def check_prepayment(
    prepay_rows: Sequence[Sequence[Any]],
    annual_tax_due: Decimal,
    annual_paid: Decimal,
    tolerance: Decimal = TOLERANCE,
) -> List[ComplianceCheck]:
    """季度预缴表公式一致性 + 预缴 vs 年度应纳差异（只报差异，不判风险）。"""
    checks: List[ComplianceCheck] = []
    # 定位季度表头行与行标签列
    header_index = None
    quarters: Dict[int, str] = {}
    for row_index, row in enumerate(prepay_rows):
        cells = [str(cell or "") for cell in row]
        if any("第1季度" in cell for cell in cells):
            header_index = row_index
            for column, cell in enumerate(cells):
                if "第1季度" in cell:
                    quarters[column] = "Q1"
                elif "第2季度" in cell:
                    quarters[column] = "Q2"
                elif "第3季度" in cell:
                    quarters[column] = "Q3"
                elif "第4季度" in cell:
                    quarters[column] = "Q4"
            break
    if header_index is None or not quarters:
        raise ValueError("预缴表未识别到季度表头")

    def find_row(keyword: str) -> Optional[List[Any]]:
        for row in prepay_rows[header_index + 1:]:
            cells = list(row)
            if len(cells) > 1 and keyword in str(cells[1] or ""):
                return cells
        return None

    profit_total = find_row("利润总额")
    adjust = find_row("特定业务计算的应纳税所得额")
    actual_profit = find_row("实际利润额")
    tax_due = find_row("应纳所得税额")
    paid = find_row("实际已缴纳所得税额")
    refund = find_row("本期应补（退）所得税额")
    if None in (profit_total, adjust, actual_profit, tax_due, paid, refund):
        raise ValueError("预缴表缺少必要行（利润总额/调整/实际利润额/应纳所得税额/已缴/应补退）")

    for column, quarter in sorted(quarters.items()):
        profit = to_decimal(profit_total[column] if column < len(profit_total) else None)
        adj = to_decimal(adjust[column] if column < len(adjust) else None) or ZERO
        actual = to_decimal(actual_profit[column] if column < len(actual_profit) else None)
        due = to_decimal(tax_due[column] if column < len(tax_due) else None)
        paid_q = to_decimal(paid[column] if column < len(paid) else None) or ZERO
        refund_q = to_decimal(refund[column] if column < len(refund) else None)
        if profit is not None:
            checks.append(_cmp("PREPAY_ACTUAL_PROFIT", quarter, q(profit + adj), actual))
        if actual is not None and due is not None:
            checks.append(_cmp("PREPAY_TAX_FORMULA", quarter, q(actual * dec("0.25")), due))
        if due is not None and refund_q is not None:
            checks.append(_cmp("PREPAY_REFUND_FORMULA", quarter, q(due - paid_q), refund_q))
    # 年度口径：预缴 vs 年度应纳（汇算补退属正常申报，不判违规）
    settle = q(annual_tax_due - annual_paid)
    ratio = q(annual_paid / annual_tax_due * 100) if annual_tax_due > ZERO else ZERO
    checks.append(ComplianceCheck(
        "ANNUAL_SETTLE", "annual", "INFO", format(q(annual_tax_due), "f"),
        format(q(settle), "f"), format(ratio, "f") + "%",
        "年度应纳 − 已预缴 = 汇算应补（退）；属正常申报行为，不判违规；仅提示差异幅度"))
    return checks


def _cmp(code: str, year: str, expected: Optional[Decimal], actual: Optional[Decimal],
         tolerance: Decimal = TOLERANCE) -> ComplianceCheck:
    if expected is None or actual is None:
        return ComplianceCheck(code, year, "UNVERIFIABLE", _fmt(expected), _fmt(actual), "", "缺项")
    delta = q(actual - expected)
    status = "PASS" if abs(delta) <= tolerance else "FLAG"
    return ComplianceCheck(code, year, status, _fmt(expected), _fmt(actual), _fmt(delta),
                           "公式复算")


def count_status(checks: Sequence[ComplianceCheck]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for check in checks:
        counts[check.status] = counts.get(check.status, 0) + 1
    return counts


def key_row_diffs(
    tax_table: FigureTable,
    financial_table: FigureTable,
    keywords: Sequence[str] = KEY_ROWS,
) -> List[Dict[str, str]]:
    """报告摘要：关键行逐年差异（只展示，不判违规）。"""
    out: List[Dict[str, str]] = []
    for keyword in keywords:
        tax_row = next((row for row in tax_table.rows if keyword in row.label), None)
        if tax_row is None:
            continue
        fin_row = financial_table.row(tax_row.index)
        for year in tax_table.years:
            tax_value = tax_row.values.get(year)
            fin_value = fin_row.values.get(year) if fin_row else None
            if tax_value is None or fin_value is None:
                continue
            out.append({
                "item": keyword, "year": year,
                "tax": format(q(tax_value), "f"),
                "financial": format(q(fin_value), "f"),
                "delta": format(q(tax_value - fin_value), "f"),
            })
    return out


# ---------------------------------------------------------------------------
# 增值税口径核对（只做算术：月度差异勾稽、年度口径表自检、价税换算）
# ---------------------------------------------------------------------------

VAT_YEARS = ("2020", "2021", "2022", "2023", "2024")
MONTH_RE = re.compile(r"(20\d{2})\s*年\s*(\d{1,2})\s*月")


def _blank_zero(value: Optional[Decimal]) -> Decimal:
    return ZERO if value is None else value


def check_vat_monthly(rows: Sequence[Sequence[Any]]) -> Tuple[List[ComplianceCheck], Dict[str, Decimal]]:
    """月度「增值税申报 vs 发票」差异：逐月差异勾稽 + 累计勾稽，返回按年差异合计。"""
    checks: List[ComplianceCheck] = []
    year_totals: Dict[str, Decimal] = {}
    previous_cumulative = ZERO
    for row_index, row in enumerate(rows[1:], start=2):
        cells = list(row) + [None] * 7
        declared = to_decimal(cells[2])
        invoice = to_decimal(cells[4])
        stored_diff = to_decimal(cells[5])
        stored_cumulative = to_decimal(cells[6])
        month_label = str(cells[3] or "")
        if declared is None or invoice is None:
            continue
        expected_diff = q(declared - invoice)
        if stored_diff is not None:
            delta = q(stored_diff - expected_diff)
            checks.append(ComplianceCheck(
                "VAT_MONTH_DIFF", month_label or f"r{row_index}",
                "PASS" if abs(delta) <= TOLERANCE else "FLAG",
                _fmt(expected_diff), _fmt(stored_diff), _fmt(delta),
                f"第{row_index}行：申报 − 发票 = 月度差异"))
        expected_cumulative = q(previous_cumulative + expected_diff)
        if stored_cumulative is not None:
            delta = q(stored_cumulative - expected_cumulative)
            checks.append(ComplianceCheck(
                "VAT_MONTH_CUM", month_label or f"r{row_index}",
                "PASS" if abs(delta) <= TOLERANCE else "FLAG",
                _fmt(expected_cumulative), _fmt(stored_cumulative), _fmt(delta),
                f"第{row_index}行：累计差异 = 上期累计 + 本期差异"))
        previous_cumulative = expected_cumulative
        match = MONTH_RE.search(month_label)
        if match:
            year = match.group(1)
            year_totals[year] = q(year_totals.get(year, ZERO) + expected_diff)
    return checks, year_totals


def check_vat_annual_scope(rows: Sequence[Sequence[Any]]) -> Tuple[List[ComplianceCheck], Dict[str, Decimal]]:
    """年度口径表自检：差异=申报−发票、所得税−增值税、累计链；价税换算（9% 假设）。

    返回年度「增值税−发票」差异（用于与月度数据跨表核对）。
    """
    checks: List[ComplianceCheck] = []
    annual_diff: Dict[str, Decimal] = {}
    if len(rows) < 2:
        return checks, annual_diff
    # 左侧：项目行（列1..5=年度，列6..10=截止累计）
    project: Dict[str, List[Optional[Decimal]]] = {}
    for row in rows[1:]:
        cells = list(row) + [None] * 24
        label = str(cells[0] or "").strip()
        if not label:
            continue
        values = [to_decimal(cells[column]) for column in range(1, 11)]
        project[label] = values

    def series(keyword: str) -> Optional[List[Optional[Decimal]]]:
        for label, values in project.items():
            if keyword in label:
                return values
        return None

    invoice = series("发票系统")
    vat = series("增值税纳税申报系统")
    vat_diff_row = series("增值税系统与发票系统差异")
    income = series("所得税纳税申报系统")
    income_diff_row = series("所得税纳税申报系统与增值税纳税申报系统差异")

    for index, year in enumerate(VAT_YEARS):
        if invoice is None or vat is None or vat_diff_row is None:
            break
        expected = q(_blank_zero(vat[index]) - _blank_zero(invoice[index]))
        actual = _blank_zero(vat_diff_row[index])
        delta = q(actual - expected)
        checks.append(ComplianceCheck(
            "VAT_ANNUAL_DIFF", year,
            "PASS" if abs(delta) <= TOLERANCE else "FLAG",
            _fmt(expected), _fmt(actual), _fmt(delta), "增值税系统与发票系统差异 = 申报 − 发票"))
        annual_diff[year] = expected
        if income is not None and income_diff_row is not None:
            expected_income = q(_blank_zero(income[index]) - _blank_zero(vat[index]))
            actual_income = _blank_zero(income_diff_row[index])
            delta_income = q(actual_income - expected_income)
            checks.append(ComplianceCheck(
                "VAT_INCOME_DIFF", year,
                "PASS" if abs(delta_income) <= TOLERANCE else "FLAG",
                _fmt(expected_income), _fmt(actual_income), _fmt(delta_income),
                "所得税申报系统与增值税申报系统差异 = 所得税 − 增值税"))

    # 累计链：cum[i] = cum[i-1] + year[i]，i=0 时上期为 0
    # 列映射：values[0..4]=年度发生数(2020..2024)，values[5..9]=截止累计(2020..2024)
    for label, values in project.items():
        previous = ZERO
        for index, year in enumerate(VAT_YEARS):
            current_year = _blank_zero(values[index])
            expected = q(previous + current_year)
            actual = _blank_zero(values[5 + index])
            delta = q(actual - expected)
            checks.append(ComplianceCheck(
                "VAT_CUM_CHAIN", f"{year}:{label[:12]}",
                "PASS" if abs(delta) <= TOLERANCE else "FLAG",
                _fmt(expected), _fmt(actual), _fmt(delta), "截止累计 = 上年累计 + 本年发生数"))
            previous = expected

    # 右侧：系统含税/不含税（9% 税率换算假设；不匹配需人工核对口径）
    for row in rows[1:]:
        cells = list(row) + [None] * 24
        label = str(cells[12] or "").strip()
        with_tax = to_decimal(cells[13])
        without_tax = to_decimal(cells[14])
        if not label or with_tax is None or without_tax is None:
            continue
        expected = q(without_tax * dec("1.09"))
        delta = q(with_tax - expected)
        checks.append(ComplianceCheck(
            "VAT_CONVERSION", label[:18],
            "PASS" if abs(delta) <= Decimal("0.02") else "FLAG",
            _fmt(expected), _fmt(with_tax), _fmt(delta),
            "含税 = 不含税 × 1.09（按 9% 税率换算核对；若该口径含其他税率需人工调整）"))
    return checks, annual_diff


def cross_check_year_totals(
    monthly_totals: Dict[str, Decimal],
    annual_diff: Dict[str, Decimal],
    tolerance: Decimal = TOLERANCE,
) -> List[ComplianceCheck]:
    """跨表核对：月度差异按年合计 vs 年度口径表差异（尾差标 INFO，不判违规）。"""
    checks: List[ComplianceCheck] = []
    for year in sorted(annual_diff):
        monthly = monthly_totals.get(year)
        annual = annual_diff[year]
        if monthly is None:
            checks.append(ComplianceCheck("VAT_CROSS_YEAR", year, "UNVERIFIABLE",
                                          _fmt(annual), "", "", "月度数据缺该年"))
            continue
        delta = q(monthly - annual)
        if abs(delta) <= tolerance:
            status = "PASS"
            note = "月度合计 = 年度口径差异"
        else:
            status = "INFO"
            note = "跨表尾差/口径差异，需人工核对（不判违规）"
        checks.append(ComplianceCheck("VAT_CROSS_YEAR", year, status,
                                      _fmt(annual), _fmt(monthly), _fmt(delta), note))
    return checks
