"""申报一致性核对测试（合成表；真实数据由 tools/real_tax_compliance.py 跑）。"""

from __future__ import annotations

import unittest

from loop_mvp.tax_compliance import (
    DISCLAIMER,
    check_difference_sheet,
    check_prepayment,
    compare_tables,
    count_status,
    key_row_diffs,
    parse_figure_rows,
)
from loop_mvp.types import dec

HEADER = ["纳税申报项目", "财务报表", "2020年度", "2021年度"]
TAX_ROWS = [
    HEADER,
    ["一、营业收入", "一、营业收入", "100", "200"],
    ["减：营业成本", "减：营业成本", "60", "120"],
    ["二、营业利润", "二、营业利润", "40", "80"],
]
FIN_ROWS = [
    HEADER,
    ["一、营业收入", "一、营业收入", "100", "210"],
    ["减：营业成本", "减：营业成本", "60", "120"],
    ["二、营业利润", "二、营业利润", "40", "90"],
]
DIFF_ROWS = [
    HEADER,
    ["一、营业收入", "一、营业收入", "0", "-10"],
    ["减：营业成本", "减：营业成本", "0", "0"],
    ["二、营业利润", "二、营业利润", "0", "-10"],
]


class TestCompareTables(unittest.TestCase):
    def test_match_diff_unverifiable(self) -> None:
        tax_table = parse_figure_rows(TAX_ROWS)
        fin_table = parse_figure_rows(FIN_ROWS)
        checks = compare_tables(tax_table, fin_table)
        by_key = {(check.year, check.note.split("：")[0]): check for check in checks}
        self.assertEqual(by_key[("2020", "一、营业收入")].status, "MATCH")
        self.assertEqual(by_key[("2021", "一、营业收入")].status, "DIFF")
        self.assertEqual(by_key[("2021", "一、营业收入")].delta, "-10.00")
        counts = count_status(checks)
        self.assertGreaterEqual(counts.get("DIFF", 0), 1)
        self.assertGreaterEqual(counts.get("MATCH", 0), 1)

    def test_key_row_diffs(self) -> None:
        diffs = key_row_diffs(parse_figure_rows(TAX_ROWS), parse_figure_rows(FIN_ROWS),
                              keywords=("营业收入",))
        self.assertEqual(len(diffs), 2)
        self.assertEqual(diffs[1]["delta"], "-10.00")


class TestDifferenceSelfCheck(unittest.TestCase):
    def test_consistent_sheet_passes(self) -> None:
        checks = check_difference_sheet(parse_figure_rows(DIFF_ROWS),
                                        parse_figure_rows(TAX_ROWS),
                                        parse_figure_rows(FIN_ROWS))
        self.assertTrue(checks)
        self.assertTrue(all(check.status == "PASS" for check in checks), checks)

    def test_inconsistent_sheet_flagged(self) -> None:
        rows = [list(row) for row in DIFF_ROWS]
        rows[1][3] = "-99"  # 篡改差异表
        checks = check_difference_sheet(parse_figure_rows(rows),
                                        parse_figure_rows(TAX_ROWS),
                                        parse_figure_rows(FIN_ROWS))
        self.assertTrue(any(check.status == "FLAG" for check in checks))


PREPAY_ROWS = [
    ["企业所得税季度预缴计算表", "", "", "", "", ""],
    ["行次", "项       目", "第1季度", "第2季度", "第3季度", "第4季度"],
    ["1", "营业收入", "", "", "", ""],
    ["3", "利润总额", "100", "", "", ""],
    ["4", "加：特定业务计算的应纳税所得额", "20", "", "", ""],
    ["9", "实际利润额（3+4-5-6-7-8）", "120", "", "", ""],
    ["11", "应纳所得税额（9×10）", "30", "", "", ""],
    ["13", "减：实际已缴纳所得税额", "10", "", "", ""],
    ["15", "本期应补（退）所得税额（11-12-13-14）", "20", "", "", ""],
]


class TestPrepayment(unittest.TestCase):
    def test_formula_checks_pass(self) -> None:
        checks = check_prepayment(PREPAY_ROWS, dec("100"), dec("100"))
        statuses = {check.code: check.status for check in checks}
        self.assertEqual(statuses["PREPAY_ACTUAL_PROFIT"], "PASS")
        self.assertEqual(statuses["PREPAY_TAX_FORMULA"], "PASS")
        self.assertEqual(statuses["PREPAY_REFUND_FORMULA"], "PASS")
        self.assertEqual(statuses["ANNUAL_SETTLE"], "INFO")

    def test_tampered_formula_flagged(self) -> None:
        rows = [list(row) for row in PREPAY_ROWS]
        rows[6][2] = "31"  # 应纳所得税额被改
        checks = check_prepayment(rows, dec("100"), dec("100"))
        self.assertTrue(any(check.code == "PREPAY_TAX_FORMULA" and check.status == "FLAG"
                            for check in checks))

    def test_annual_settle_ratio(self) -> None:
        checks = check_prepayment(PREPAY_ROWS, dec("200"), dec("150"))
        annual = [check for check in checks if check.code == "ANNUAL_SETTLE"][0]
        self.assertEqual(annual.status, "INFO")
        self.assertEqual(annual.expected, "200.00")
        self.assertEqual(annual.actual, "50.00")
        self.assertEqual(annual.delta, "75.00%")  # 150/200


class TestPhilosophyBoundary(unittest.TestCase):
    def test_disclaimer_present(self) -> None:
        self.assertIn("不构成税务意见", DISCLAIMER)
        self.assertIn("以主管税务机关为准", DISCLAIMER)
        self.assertIn("不做税务筹划", DISCLAIMER)

    def test_no_optimization_api(self) -> None:
        import loop_mvp.tax_compliance as module
        for forbidden in ("optimize", "minimize_tax", "plan_tax"):
            self.assertFalse(hasattr(module, forbidden))


if __name__ == "__main__":
    unittest.main()
