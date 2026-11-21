"""真实财务报表勾稽校验（纯函数，域无关；供 tools/real_data_check.py 调用）。

只做"能精确算的检查"，不猜业务原因、不改源数据：

- 资产负债表：资产总计 = 负债合计 + 所有者权益合计；小计 = 分项之和；权益 = 各权益项之和；
- 利润表：营业利润、利润总额、净利润逐级重算（缺项目标 UNVERIFIABLE，不硬判 PASS）；
- 跨表：未分配利润变动 vs 净利润（差额标 INFO，待人工解释，不判违规）。

状态语义：``PASS`` 精确相等；``FAIL`` 精确不等；``UNVERIFIABLE`` 缺项无法判定；
``INFO`` 需要人工解释的勾稽差异（不计入失败）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .types import ZERO, dec, q

YEAR_RE = re.compile(r"(20\d{2})\s*年")
_WS_RE = re.compile(r"\s+")
_ORDINAL_RE = re.compile(r"^[一二三四五六七八九十百]+、")
_LABEL_PREFIXES = ("（一）", "（二）", "（三）", "减：", "加：", "其中：")

# 资产负债表标签（去空白后的标准名）
A_TOTAL = "资产总计"
A_CURRENT = "流动资产合计"
A_NONCURRENT = "非流动资产合计"
L_TOTAL = "负债合计"
L_CURRENT = "流动负债合计"
L_NONCURRENT = "非流动负债合计"
LE_TOTAL = "负债和所有者权益（或股东权益）总计"
E_TOTAL = "所有者权益（或股东权益）合计"
E_REQUIRED_ITEMS = ("实收资本（或股本）", "未分配利润")
E_OPTIONAL_ITEMS = ("资本公积", "盈余公积")
E_DEDUCT_ITEMS = ("库存股",)  # 减项

# 利润表：用"关键字前缀"定位行，容忍"一、营业收入 / 减：营业成本"等排版前缀
P_KEYWORDS = {
    "revenue": "营业收入",
    "cost": "营业成本",
    "surtax": "税金及附加",
    "selling": "销售费用",
    "admin": "管理费用",
    "finance": "财务费用",
    "impairment": "资产减值损失",
    "fair_value": "公允价值变动收益",
    "investment": "投资收益",
    "operating_profit": "营业利润",
    "nonop_income": "营业外收入",
    "nonop_expense": "营业外支出",
    "total_profit": "利润总额",
    "income_tax": "所得税费用",
    "net_profit": "净利润",
}


def norm_label(value: Any) -> str:
    """去掉所有空白字符，便于对齐排版后的中文标签。"""
    if value is None:
        return ""
    return _WS_RE.sub("", str(value))


def canonical_label(value: Any) -> str:
    """去掉"一、/六、/减：/其中："等排版前缀，得到稳定标签。"""
    text = norm_label(value)
    changed = True
    while changed:
        changed = False
        match = _ORDINAL_RE.match(text)
        if match:
            text = text[match.end():]
            changed = True
        for prefix in _LABEL_PREFIXES:
            if text.startswith(prefix):
                text = text[len(prefix):]
                changed = True
    return text


def to_decimal(value: Any) -> Optional[Decimal]:
    """单元格值转 Decimal；空值返回 None（缺项≠0）。"""
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
    except Exception:  # noqa: BLE001 - 非数值文本按缺项处理
        return None


@dataclass(frozen=True)
class BalanceTable:
    years: Tuple[str, ...]
    left: Mapping[str, Mapping[str, Decimal]]
    right: Mapping[str, Mapping[str, Decimal]]

    def get(self, side: str, label: str, year: str) -> Optional[Decimal]:
        table = self.left if side == "left" else self.right
        return table.get(label, {}).get(year)


@dataclass(frozen=True)
class ProfitTable:
    years: Tuple[str, ...]
    rows: Mapping[str, Mapping[str, Decimal]]  # canonical label -> year -> value

    def get(self, keyword: str, year: str) -> Optional[Decimal]:
        """按关键字前缀找行（容忍"一、营业收入"这类前缀与括号注释）。"""
        for label, series in self.rows.items():
            if label.startswith(keyword):
                return series.get(year)
        return None


@dataclass(frozen=True)
class CheckResult:
    code: str
    year: str
    status: str  # PASS / FAIL / UNVERIFIABLE / INFO
    expected: str
    actual: str
    delta: str
    note: str = ""

    def to_dict(self) -> Dict[str, str]:
        return {
            "code": self.code, "year": self.year, "status": self.status,
            "expected": self.expected, "actual": self.actual,
            "delta": self.delta, "note": self.note,
        }


def _fmt(value: Optional[Decimal]) -> str:
    return "" if value is None else format(q(value), "f")


def _cmp(code: str, year: str, expected: Optional[Decimal], actual: Optional[Decimal],
         note: str = "", missing: str = "",
         draft_years: Iterable[str] = ()) -> CheckResult:
    if expected is None or actual is None:
        return CheckResult(code, year, "UNVERIFIABLE", _fmt(expected), _fmt(actual), "",
                           missing or note)
    delta = q(actual - expected)
    if delta == ZERO:
        return CheckResult(code, year, "PASS", _fmt(expected), _fmt(actual), "0.00", note)
    if year in set(draft_years):
        return CheckResult(code, year, "UNVERIFIED_DRAFT", _fmt(expected), _fmt(actual),
                           _fmt(delta), note + "；该列为在编/未结账，不做 FAIL 级结论")
    return CheckResult(code, year, "FAIL", _fmt(expected), _fmt(actual), _fmt(delta), note)


# ---------------------------------------------------------------------------
# 解析（输入为 sheet 的原始行序列，元素类型不限）
# ---------------------------------------------------------------------------

def _year_columns(header: Sequence[Any]) -> Dict[int, str]:
    out: Dict[int, str] = {}
    for idx, cell in enumerate(header):
        match = YEAR_RE.search(str(cell or ""))
        if match:
            out[idx] = match.group(1)
    return out


def parse_balance_rows(rows: Sequence[Sequence[Any]]) -> BalanceTable:
    """资产负债表：左半区=资产，右半区=负债与权益（两半区各有一列标签）。"""
    if not rows:
        raise ValueError("资产负债表为空")
    header = list(rows[0])
    year_cols = _year_columns(header)
    if not year_cols:
        raise ValueError("资产负债表表头未识别到年份")
    columns = sorted(year_cols)
    split: Optional[Tuple[int, int]] = None
    for left_col, right_col in zip(columns, columns[1:]):
        if right_col - left_col > 1:  # 中间隔着右侧标签列
            split = (left_col, right_col)
            break
    if split is None:
        raise ValueError("资产负债表未识别出左右两半区（年份列之间缺少标签列）")
    left_cols = [c for c in columns if c <= split[0]]
    right_cols = [c for c in columns if c >= split[1]]
    left_label_col = left_cols[0] - 1
    right_label_col = right_cols[0] - 1

    left: Dict[str, Dict[str, Decimal]] = {}
    right: Dict[str, Dict[str, Decimal]] = {}
    width = len(header)
    for row in rows[1:]:
        cells = list(row) + [None] * (width - len(row))
        left_label = norm_label(cells[left_label_col]) if left_label_col < width else ""
        right_label = norm_label(cells[right_label_col]) if right_label_col < width else ""
        for col in left_cols:
            value = to_decimal(cells[col]) if col < width else None
            if value is not None and left_label:
                left.setdefault(left_label, {})[year_cols[col]] = value
        for col in right_cols:
            value = to_decimal(cells[col]) if col < width else None
            if value is not None and right_label:
                right.setdefault(right_label, {})[year_cols[col]] = value
    years = tuple(sorted(set(year_cols.values())))
    return BalanceTable(years, left, right)


def parse_profit_rows(rows: Sequence[Sequence[Any]]) -> ProfitTable:
    if not rows:
        raise ValueError("利润表为空")
    header = list(rows[0])
    year_cols = _year_columns(header)
    if not year_cols:
        raise ValueError("利润表表头未识别到年份")
    label_col = min(year_cols) - 1
    table: Dict[str, Dict[str, Decimal]] = {}
    width = len(header)
    for row in rows[1:]:
        cells = list(row) + [None] * (width - len(row))
        label = canonical_label(cells[label_col]) if label_col < width else ""
        if not label:
            continue
        for col, year in year_cols.items():
            value = to_decimal(cells[col]) if col < width else None
            if value is not None:
                table.setdefault(label, {})[year] = value
    return ProfitTable(tuple(sorted(set(year_cols.values()))), table)


# ---------------------------------------------------------------------------
# 检查
# ---------------------------------------------------------------------------

def check_balance_sheet(balance: BalanceTable,
                        draft_years: Iterable[str] = ()) -> List[CheckResult]:
    results: List[CheckResult] = []
    for year in balance.years:
        a_total = balance.get("left", A_TOTAL, year)
        l_total = balance.get("right", L_TOTAL, year)
        e_total = balance.get("right", E_TOTAL, year)
        right_total = balance.get("right", LE_TOTAL, year)
        a_current = balance.get("left", A_CURRENT, year)
        a_noncurrent = balance.get("left", A_NONCURRENT, year)
        l_current = balance.get("right", L_CURRENT, year)
        l_noncurrent = balance.get("right", L_NONCURRENT, year)

        expected_assets = (q(l_total + e_total)
                           if l_total is not None and e_total is not None else None)
        results.append(_cmp("BS_IDENTITY", year, expected_assets, a_total,
                            note="资产总计 = 负债合计 + 所有者权益合计",
                            draft_years=draft_years))
        results.append(_cmp("BS_RIGHT_TOTAL", year, a_total, right_total,
                            note="左列资产总计 = 右列负债和所有者权益总计",
                            draft_years=draft_years))
        if a_current is not None and a_noncurrent is not None:
            results.append(_cmp("BS_ASSET_SUBTOTAL", year, q(a_current + a_noncurrent), a_total,
                                note="流动资产合计 + 非流动资产合计 = 资产总计",
                                draft_years=draft_years))
        if l_current is not None and l_noncurrent is not None:
            results.append(_cmp("BS_LIABILITY_SUBTOTAL", year, q(l_current + l_noncurrent), l_total,
                                note="流动负债合计 + 非流动负债合计 = 负债合计",
                                draft_years=draft_years))

        equity_expected = ZERO
        equity_missing = False
        for item in E_REQUIRED_ITEMS:
            value = balance.get("right", item, year)
            if value is None:
                equity_missing = True
            else:
                equity_expected += value
        for item in E_OPTIONAL_ITEMS:
            equity_expected += balance.get("right", item, year) or ZERO
        for item in E_DEDUCT_ITEMS:
            equity_expected -= balance.get("right", item, year) or ZERO
        results.append(_cmp("BS_EQUITY_COMPONENTS", year,
                            None if equity_missing else equity_expected, e_total,
                            note="权益合计 = 实收资本+资本公积+盈余公积+未分配利润−库存股",
                            missing="缺实收资本或未分配利润，无法判定",
                            draft_years=draft_years))
    return results


def check_profit_statement(profit: ProfitTable,
                           draft_years: Iterable[str] = ()) -> List[CheckResult]:
    results: List[CheckResult] = []
    for year in profit.years:
        revenue = profit.get(P_KEYWORDS["revenue"], year)
        cost = profit.get(P_KEYWORDS["cost"], year)
        op_profit = profit.get(P_KEYWORDS["operating_profit"], year)
        if revenue is None or cost is None:
            results.append(CheckResult(
                "PL_OPERATING_PROFIT", year, "UNVERIFIABLE", "", _fmt(op_profit), "",
                "缺营业收入或营业成本，无法重算营业利润（不硬判 PASS）"))
        else:
            subtotal = revenue - cost
            missing: List[str] = []
            for key in ("surtax", "selling", "admin", "finance", "impairment"):
                value = profit.get(P_KEYWORDS[key], year)
                if value is None:
                    missing.append(P_KEYWORDS[key])
                else:
                    subtotal -= value
            for key in ("fair_value", "investment"):
                value = profit.get(P_KEYWORDS[key], year)
                if value is not None:
                    subtotal += value
            results.append(_cmp("PL_OPERATING_PROFIT", year, q(subtotal), op_profit,
                                note="营业利润 = 收入−成本−税金及附加−三费−减值+公允价值变动+投资收益"
                                     + (f"；缺项按 0：{missing}" if missing else ""),
                                draft_years=draft_years))

        total_profit = profit.get(P_KEYWORDS["total_profit"], year)
        nonop_income = profit.get(P_KEYWORDS["nonop_income"], year) or ZERO
        nonop_expense = profit.get(P_KEYWORDS["nonop_expense"], year) or ZERO
        results.append(_cmp("PL_TOTAL_PROFIT", year,
                            None if op_profit is None else q(op_profit + nonop_income - nonop_expense),
                            total_profit,
                            note="利润总额 = 营业利润 + 营业外收入 − 营业外支出",
                            draft_years=draft_years))

        net = profit.get(P_KEYWORDS["net_profit"], year)
        income_tax = profit.get(P_KEYWORDS["income_tax"], year) or ZERO
        results.append(_cmp("PL_NET_PROFIT", year,
                            None if total_profit is None else q(total_profit - income_tax), net,
                            note="净利润 = 利润总额 − 所得税费用",
                            draft_years=draft_years))
    return results


def check_cross_statement(balance: BalanceTable, profit: ProfitTable) -> List[CheckResult]:
    """未分配利润变动 vs 净利润：相等 PASS；不等 INFO（分红/以前年度调整等，须人工解释）。"""
    results: List[CheckResult] = []
    years = [year for year in balance.years if year in profit.years]
    for idx in range(1, len(years)):
        year, prev = years[idx], years[idx - 1]
        re_now = balance.get("right", "未分配利润", year)
        re_prev = balance.get("right", "未分配利润", prev)
        net = profit.get(P_KEYWORDS["net_profit"], year)
        if re_now is None or re_prev is None or net is None:
            results.append(CheckResult("CROSS_RETAINED", year, "UNVERIFIABLE",
                                       "", "", "", "缺未分配利润或净利润"))
            continue
        delta_re = q(re_now - re_prev)
        delta = q(delta_re - net)
        if delta == ZERO:
            results.append(CheckResult("CROSS_RETAINED", year, "PASS", _fmt(net),
                                       _fmt(delta_re), "0.00", "Δ未分配利润 = 净利润"))
        else:
            results.append(CheckResult(
                "CROSS_RETAINED", year, "INFO", _fmt(net), _fmt(delta_re), _fmt(delta),
                "Δ未分配利润与净利润有差额（可能为分红/以前年度调整/未结转；需人工解释，不判违规）"))
    return results


def run_all_checks(balance: BalanceTable, profit: ProfitTable) -> List[CheckResult]:
    return (check_balance_sheet(balance) + check_profit_statement(profit)
            + check_cross_statement(balance, profit))


def diagnose_patterns(balance: BalanceTable, profit: ProfitTable) -> List[str]:
    """模式识别（只描述可观察事实，不猜业务原因）：

    - 最后一年某一半区与上一年逐行完全相同 → 疑为整列未更新；
    - 最后一年净利润非零但未分配利润没有变动 → 疑为未结转/未过账。
    """
    hints: List[str] = []
    years = list(balance.years)
    if len(years) < 2:
        return hints
    prev, last = years[-2], years[-1]
    for name, table in (("left", balance.left), ("right", balance.right)):
        shared = [
            label for label, series in table.items()
            if series.get(prev) is not None and series.get(last) is not None
        ]
        if shared and all(table[label][prev] == table[label][last] for label in shared):
            hints.append(
                f"{last} 年{'资产' if name == 'left' else '负债和所有者权益'}列与 {prev} 年"
                f"逐行完全相同（{len(shared)} 行）→ 疑为整列未更新（在编列时属正常现象，需财务确认）"
            )
    net = profit.get(P_KEYWORDS["net_profit"], last)
    re_prev = balance.get("right", "未分配利润", prev)
    re_last = balance.get("right", "未分配利润", last)
    if (net not in (None, ZERO) and re_prev is not None and re_last is not None
            and re_last == re_prev):
        hints.append(
            f"{last} 年净利润 {format(q(net), 'f')} 未反映到未分配利润（Δ=0）→ 疑为未结转/未过账"
        )
    return hints


def summarize(results: Sequence[CheckResult]) -> Dict[str, int]:
    counts = {"PASS": 0, "FAIL": 0, "UNVERIFIABLE": 0, "INFO": 0, "UNVERIFIED_DRAFT": 0}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    return counts
