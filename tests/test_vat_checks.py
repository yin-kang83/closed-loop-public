"""增值税口径核对测试（合成表；真实数据由 tools/real_vat_lvat_check.py 跑）。"""

from __future__ import annotations

import unittest

from loop_mvp.tax_compliance import (
    check_vat_annual_scope,
    check_vat_monthly,
    cross_check_year_totals,
)
from loop_mvp.types import dec

MONTHLY_ROWS = [
    ["年度-月份", "", "增值税申报", "", "发票", "增值税系统与发票差异", "增值税系统与发票差异累计"],
    ["44409", "9%税率的服务、不动产和无形资产", "100", "2021年8月", "90", "10", "10"],
    ["44440", "9%税率的服务、不动产和无形资产", "50", "2021年9月", "55", "-5", "5"],
]

SCOPE_HEADER = [""] * 24
SCOPE_HEADER[0] = "项目（金额均为不含税金额）"
for index, year in enumerate(("2020", "2021", "2022", "2023", "2024")):
    SCOPE_HEADER[1 + index] = f"{year}年度发生数"
    SCOPE_HEADER[6 + index] = f"截止{year}年度累计"
SCOPE_HEADER[12] = "项目"
SCOPE_HEADER[13] = "含税金额"
SCOPE_HEADER[14] = "不含税金额"


def scope_rows():
    return [
        SCOPE_HEADER,
        # label, 2020..2024, cum2020..cum2024
        ["发票系统", None, "100", "50", "0", "0", "0", "100", "150", "150", "150"],
        ["增值税纳税申报系统", None, "150", "50", "2", "0", "0", "150", "200", "202", "202"],
        ["增值税系统与发票系统差异", "0", "50", "0", "2", "0", "0", "50", "50", "52", "52"],
        ["所得税纳税申报系统", "1000", "150", "50", "2", "0", "1000", "1150", "1200", "1202", "1202"],
        ["所得税纳税申报系统与增值税纳税申报系统差异",
         "1000", "0", "0", "0", "0", "1000", "1000", "1000", "1000", "1000"],
        ["", "", "", "", "", "", "", "", "", "", "", "",
         "网签系统（2021年-2024年）", "1090", "1000"],
    ]


class TestVatMonthly(unittest.TestCase):
    def test_monthly_diff_and_cumulative(self) -> None:
        checks, totals = check_vat_monthly(MONTHLY_ROWS)
        self.assertTrue(checks)
        self.assertTrue(all(check.status == "PASS" for check in checks), checks)
        self.assertEqual(totals["2021"], dec("5"))

    def test_tampered_diff_flagged(self) -> None:
        rows = [list(row) for row in MONTHLY_ROWS]
        rows[2][5] = "7"  # 应 -5
        checks, _totals = check_vat_monthly(rows)
        self.assertTrue(any(check.code == "VAT_MONTH_DIFF" and check.status == "FLAG"
                            for check in checks))


class TestVatAnnualScope(unittest.TestCase):
    def test_formulas_and_cumulative_chain_pass(self) -> None:
        checks, annual_diff = check_vat_annual_scope(scope_rows())
        self.assertTrue(checks)
        flags = [check for check in checks if check.status == "FLAG"]
        self.assertEqual(flags, [], flags)
        self.assertEqual(annual_diff["2021"], dec("50"))
        conversions = [check for check in checks if check.code == "VAT_CONVERSION"]
        self.assertTrue(conversions)
        self.assertEqual(conversions[0].status, "PASS")

    def test_tampered_annual_diff_flagged(self) -> None:
        rows = [list(row) for row in scope_rows()]
        rows[3][2] = "51"  # 增值税与发票差异 2021 应为 50
        checks, _annual = check_vat_annual_scope(rows)
        self.assertTrue(any(check.code == "VAT_ANNUAL_DIFF" and check.status == "FLAG"
                            for check in checks))

    def test_conversion_mismatch_flagged(self) -> None:
        rows = [list(row) for row in scope_rows()]
        rows[-1][13] = "1100"  # 含税被改
        checks, _annual = check_vat_annual_scope(rows)
        self.assertTrue(any(check.code == "VAT_CONVERSION" and check.status == "FLAG"
                            for check in checks))


class TestCrossYear(unittest.TestCase):
    def test_equal_passes(self) -> None:
        checks = cross_check_year_totals({"2021": dec("50")}, {"2021": dec("50")})
        self.assertEqual(checks[0].status, "PASS")

    def test_tail_difference_is_info(self) -> None:
        checks = cross_check_year_totals({"2021": dec("50.24")}, {"2021": dec("50")})
        self.assertEqual(checks[0].status, "INFO")
        self.assertEqual(checks[0].delta, "0.24")


if __name__ == "__main__":
    unittest.main()
