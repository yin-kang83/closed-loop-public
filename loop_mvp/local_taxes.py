"""地方税种口径核对（印花税/房产税/城镇土地使用税/增值税附加税费）。

只做可复算的算术核对与差异展示：不判断税务处理是否正确、不构成税务意见、不做税务筹划；
差异（FLAG）只表示表内公式/勾稽不一致，需人工核对底稿。
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .tax_compliance import (
    ComplianceCheck,
    TOLERANCE,
    _fmt,
    q,
    to_decimal,
)
from .types import ZERO, dec

MINUS = Decimal("-0.01")
DATE_RE = re.compile(r"(\d{4})-\d{1,2}-\d{1,2}")


def _raw(value: Any) -> Optional[Decimal]:
    """非量化解析（税率/比例不能被 q 量化到 0.01，否则 0.0005 变 0）。"""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return dec(str(value))
    text = str(value).strip()
    if not text or text in {"-", "—", "/"}:
        return None
    text = text.replace(",", "").replace("，", "")
    try:
        return dec(text)
    except Exception:  # noqa: BLE001
        return None


def _is_date(value: Any) -> bool:
    if isinstance(value, (date, datetime)):
        return True
    return bool(DATE_RE.match(str(value or "").strip()[:10]))

# ---------------------------------------------------------------------------
# 印花税：应缴 = Σ(计税依据 × 税率)；欠缴 = 应缴 − 已缴
# ---------------------------------------------------------------------------

STAMP_AMOUNT_COL = 13   # 应缴税款
STAMP_PAID_COL = 14     # 已缴税款
STAMP_OWED_COL = 15     # 欠缴税款


def _round_yuan(value: Decimal) -> Decimal:
    """四舍五入到分（0.01）。"""
    return q(value)


def check_stamp_duty(rows: Sequence[Sequence[Any]]) -> List[ComplianceCheck]:
    """印花税月度表：逐行应缴 = Σ(计税依据×税率)，欠缴 = 应缴−已缴。"""
    checks: List[ComplianceCheck] = []
    if len(rows) < 6:
        return checks
    type_row = list(rows[3]) + [None] * 20
    rate_row = list(rows[4]) + [None] * 20
    columns: List[Tuple[int, str, Decimal]] = []
    for index in range(1, 12):
        label = str(type_row[index] or "").strip()
        rate = _raw(rate_row[index])
        if label and rate is not None and rate > ZERO:
            columns.append((index, label, rate))
    for row_index, row in enumerate(rows[5:], start=6):
        cells = list(row) + [None] * 20
        if not _is_date(cells[0]):
            continue
        expected = ZERO
        detail = []
        for index, label, rate in columns:
            basis = _raw(cells[index])
            if basis is None or basis == ZERO:
                continue
            amount = q(basis * rate)
            expected += amount
            detail.append(f"{label}={_fmt(amount)}")
        stored = _raw(cells[STAMP_AMOUNT_COL])
        if stored is not None:
            delta = q(stored - expected)
            checks.append(ComplianceCheck(
                "STAMP_FORMULA", f"r{row_index}",
                "PASS" if abs(delta) <= Decimal("0.05") else "FLAG",
                _fmt(expected), _fmt(stored), _fmt(delta),
                "应缴 = Σ(计税依据×税率)（表内四舍五入到角）" +
                ("；" + "；".join(detail) if detail else "（无计税依据）")))
        paid = _raw(cells[STAMP_PAID_COL])
        owed = _raw(cells[STAMP_OWED_COL])
        if paid is not None and owed is not None and stored is not None:
            delta = q(owed - (stored - paid))
            checks.append(ComplianceCheck(
                "STAMP_OWED", f"r{row_index}",
                "PASS" if abs(delta) <= Decimal("0.02") else "FLAG",
                _fmt(q(stored - paid)), _fmt(owed), _fmt(delta),
                "欠缴 = 应缴 − 已缴"))
    return checks


# ---------------------------------------------------------------------------
# 房产税：从价：余值=原值×(1−扣除比例)，税额=余值×1.2%；从租：税额=不含税租金×12%
# ---------------------------------------------------------------------------

PROPERTY_ORIGINAL_COL = 3
PROPERTY_DEDUCT_COL = 4
PROPERTY_REMAIN_COL = 5
PROPERTY_RATE_COL = 6
PROPERTY_TAX_COL = 7
PROPERTY_RENT_NET_COL = 12
PROPERTY_RENT_RATE_COL = 13
PROPERTY_RENT_TAX_COL = 14


def check_property_tax(rows: Sequence[Sequence[Any]]) -> List[ComplianceCheck]:
    checks: List[ComplianceCheck] = []
    for row_index, row in enumerate(rows, start=1):
        if row_index <= 4:
            continue
        cells = list(row) + [None] * 23
        original = _raw(cells[PROPERTY_ORIGINAL_COL])
        deduct = _raw(cells[PROPERTY_DEDUCT_COL])
        remain = _raw(cells[PROPERTY_REMAIN_COL])
        rate = _raw(cells[PROPERTY_RATE_COL])
        tax = _raw(cells[PROPERTY_TAX_COL])
        if original is not None and original != ZERO and deduct is not None and remain is not None:
            expected = q(original * (dec("1") - deduct))
            delta = q(remain - expected)
            checks.append(ComplianceCheck(
                "PROPERTY_REMAIN", f"r{row_index}",
                "PASS" if abs(delta) <= Decimal("0.02") else "FLAG",
                _fmt(expected), _fmt(remain), _fmt(delta),
                "计税余值 = 房产原值 × (1 − 扣除比例)"))
            if rate is not None and tax is not None:
                expected_tax = q(remain * rate / dec("4"))  # 按季缴纳：年税率 ÷ 4
                delta = q(tax - expected_tax)
                checks.append(ComplianceCheck(
                    "PROPERTY_TAX", f"r{row_index}",
                    "PASS" if abs(delta) <= Decimal("0.05") else "FLAG",
                    _fmt(expected_tax), _fmt(tax), _fmt(delta),
                    "从价税额（按季）= 计税余值 × 税率 ÷ 4"))
        rent_net = _raw(cells[PROPERTY_RENT_NET_COL])
        rent_rate = _raw(cells[PROPERTY_RENT_RATE_COL])
        rent_tax = _raw(cells[PROPERTY_RENT_TAX_COL])
        if (rent_net is not None and rent_net != ZERO and rent_rate is not None
                and rent_tax is not None):
            expected = q(rent_net * rent_rate)
            delta = q(rent_tax - expected)
            checks.append(ComplianceCheck(
                "PROPERTY_RENT", f"r{row_index}",
                "PASS" if abs(delta) <= Decimal("0.05") else "FLAG",
                _fmt(expected), _fmt(rent_tax), _fmt(delta),
                "从租税额 = 不含税租金 × 12%"))
    return checks


# ---------------------------------------------------------------------------
# 城镇土地使用税：季度税额 = 应税面积 × 年税额标准 ÷ 4；合计 = 各期之和
# ---------------------------------------------------------------------------

LAND_RATE_COL = 2
LAND_GROUPS = (
    (7, 8),    # 1-2期：应税面积、本期应纳税额
    (16, 17),  # 3-4期
    (24, 25),  # 5期
)
LAND_TOTAL_COL = 29
LAND_PAID_COL = 30
LAND_OWED_COL = 31


def check_land_use_tax(rows: Sequence[Sequence[Any]]) -> List[ComplianceCheck]:
    checks: List[ComplianceCheck] = []
    for row_index, row in enumerate(rows, start=1):
        if row_index <= 3:
            continue
        cells = list(row) + [None] * 34
        rate = _raw(cells[LAND_RATE_COL])
        if rate is None or rate == ZERO:
            continue
        group_taxes = []
        for area_col, tax_col in LAND_GROUPS:
            area = _raw(cells[area_col])
            tax = _raw(cells[tax_col])
            if area is None or tax is None:
                continue
            expected = q(area * rate / dec("4"))
            delta = q(tax - expected)
            checks.append(ComplianceCheck(
                "LAND_TAX", f"r{row_index}:面积{area_col}",
                "PASS" if abs(delta) <= Decimal("0.05") else "FLAG",
                _fmt(expected), _fmt(tax), _fmt(delta),
                "季度税额 = 应税面积 × 年税额标准 ÷ 4"))
            group_taxes.append(tax)
        total = _raw(cells[LAND_TOTAL_COL])
        if total is not None and group_taxes:
            delta = q(total - sum(group_taxes, ZERO))
            checks.append(ComplianceCheck(
                "LAND_TOTAL", f"r{row_index}",
                "PASS" if abs(delta) <= Decimal("0.05") else "FLAG",
                _fmt(sum(group_taxes, ZERO)), _fmt(total), _fmt(delta),
                "本期应缴金额 = 各期应纳税额之和"))
        paid = _raw(cells[LAND_PAID_COL])
        owed = _raw(cells[LAND_OWED_COL])
        if total is not None and paid is not None and owed is not None:
            delta = q(owed - (total - paid))
            checks.append(ComplianceCheck(
                "LAND_OWED", f"r{row_index}",
                "PASS" if abs(delta) <= Decimal("0.05") else "FLAG",
                _fmt(q(total - paid)), _fmt(owed), _fmt(delta),
                "应交未交 = 应缴 − 实缴"))
    return checks


# ---------------------------------------------------------------------------
# 增值税附加税费：税额 = 增值税额 × 税率（7%/3%/2%），合计 = 各月之和
# ---------------------------------------------------------------------------

SURTAX_ROWS = ("城建税", "教育费附加", "地方教育费附加")
SURTAX_TOTAL_COL = 2


def check_surtax(rows: Sequence[Sequence[Any]]) -> List[ComplianceCheck]:
    checks: List[ComplianceCheck] = []
    for row_index, row in enumerate(rows, start=1):
        cells = list(row) + [None] * 52
        label = str(cells[1] or "").strip()
        if label not in SURTAX_ROWS:
            continue
        monthly_taxes = []
        for month in range(1, 13):
            base_col = 4 + 4 * (month - 1)
            rate_col = base_col + 1
            tax_col = base_col + 2
            base = _raw(cells[base_col])
            rate = _raw(cells[rate_col])
            tax = _raw(cells[tax_col])
            if base is None or rate is None or tax is None:
                continue
            expected = q(base * rate)
            delta = q(tax - expected)
            checks.append(ComplianceCheck(
                "SURTAX_FORMULA", f"r{row_index}:{month}月",
                "PASS" if abs(delta) <= Decimal("0.02") else "FLAG",
                _fmt(expected), _fmt(tax), _fmt(delta),
                f"{label} = 增值税额 × 税率"))
            monthly_taxes.append(tax)
        total = _raw(cells[SURTAX_TOTAL_COL])
        if total is not None and monthly_taxes:
            delta = q(total - sum(monthly_taxes, ZERO))
            checks.append(ComplianceCheck(
                "SURTAX_TOTAL", f"r{row_index}",
                "PASS" if abs(delta) <= Decimal("0.02") else "FLAG",
                _fmt(sum(monthly_taxes, ZERO)), _fmt(total), _fmt(delta),
                f"{label} 1-12月合计 = 各月税额之和"))
    return checks
