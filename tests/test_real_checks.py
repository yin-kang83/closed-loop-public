"""真实报表勾稽校验的纯函数测试（合成表；不依赖 E 盘真实数据）。"""

from __future__ import annotations

import unittest

from loop_mvp.real_checks import (
    BalanceTable,
    ProfitTable,
    canonical_label,
    check_balance_sheet,
    check_cross_statement,
    check_profit_statement,
    diagnose_patterns,
    norm_label,
    parse_balance_rows,
    parse_profit_rows,
    summarize,
    to_decimal,
)

BS_HEADER = ["资    产", "2023年期末余额", "2024年期末余额",
             "负债和所有者权益（或股东权益）", "2023年期末余额", "2024年期末余额"]


def balance_rows():
    return [
        BS_HEADER,
        ["资产总计", "100", "110", "负债和所有者权益（或股东权益）总计", "100", "110"],
        ["流动资产合计", "60", "70", "负债合计", "40", "45"],
        ["非流动资产合计", "40", "40", "所有者权益（或股东权益）合计", "60", "65"],
        [None, None, None, "流动负债合计", "40", "45"],
        [None, None, None, "非流动负债合计", "0", "0"],
        [None, None, None, "实收资本（或股本）", "50", "50"],
        [None, None, None, "未分配利润", "10", "15"],
    ]


PL_HEADER = ["项       目", "2023年金额", "2024年金额"]


def profit_rows():
    return [
        PL_HEADER,
        ["一、营业收入", "100", "120"],
        ["    减：营业成本", "60", "70"],
        ["           税金及附加", "2", "3"],
        ["           销售费用", "5", "6"],
        ["           管理费用", "4", "5"],
        ["           财务费用", "3", "4"],
        ["           资产减值损失", "0", "0"],
        ['    加：公允价值变动收益（损失以"-"填列）', "0", "0"],
        ['           投资收益（损失以"-"填列）', "0", "0"],
        ['二、营业利润（亏损以"－"号填列）', "26", "32"],
        ["   加：营业外收入", "1", "0"],
        ["   减：营业外支出", "0", "0"],
        ['三、利润总额（亏损总额以"－"号填列）', "27", "32"],
        ["   减：所得税费用", "4", "5"],
        ['四、净利润（净亏损以"-"号填列）', "23", "27"],
    ]


class TestLabelHelpers(unittest.TestCase):
    def test_norm_and_canonical(self) -> None:
        self.assertEqual(norm_label("  一、营业  收入 "), "一、营业收入")
        self.assertEqual(canonical_label("    减：营业成本"), "营业成本")
        self.assertEqual(canonical_label("其中：对联营企业的投资收益"), "对联营企业的投资收益")
        self.assertEqual(canonical_label("四、净利润（净亏损以\"-\"号填列）")[:3], "净利润")

    def test_to_decimal(self) -> None:
        self.assertEqual(to_decimal("1,234.50"), to_decimal("1234.5"))
        self.assertIsNone(to_decimal(""))
        self.assertIsNone(to_decimal("—"))
        self.assertEqual(to_decimal("-4,705.43"), to_decimal("-4705.43"))


class TestBalanceChecks(unittest.TestCase):
    def test_parse_and_pass(self) -> None:
        table = parse_balance_rows(balance_rows())
        self.assertEqual(table.years, ("2023", "2024"))
        self.assertEqual(table.get("left", "资产总计", "2023"), to_decimal("100"))
        self.assertEqual(table.get("right", "负债合计", "2024"), to_decimal("45"))
        results = check_balance_sheet(table)
        self.assertTrue(all(result.status == "PASS" for result in results), results)

    def test_imbalance_detected(self) -> None:
        rows = balance_rows()
        rows[2] = ["流动资产合计", "60", "70", "负债合计", "50", "45"]  # 2023 负债多 10
        results = check_balance_sheet(parse_balance_rows(rows))
        fails = [result for result in results if result.status == "FAIL"]
        self.assertTrue(any(result.code == "BS_IDENTITY" and result.year == "2023"
                            and result.delta == "-10.00" for result in fails), fails)


class TestProfitChecks(unittest.TestCase):
    def test_parse_and_pass(self) -> None:
        table = parse_profit_rows(profit_rows())
        self.assertEqual(table.get("营业收入", "2023"), to_decimal("100"))
        self.assertEqual(table.get("营业利润", "2024"), to_decimal("32"))
        self.assertEqual(table.get("净利润", "2024"), to_decimal("27"))
        results = check_profit_statement(table)
        self.assertTrue(all(result.status == "PASS" for result in results), results)

    def test_missing_revenue_unverifiable_not_pass(self) -> None:
        rows = [row for row in profit_rows() if "营业收入" not in str(row[0])]
        results = check_profit_statement(parse_profit_rows(rows))
        op = [result for result in results if result.code == "PL_OPERATING_PROFIT"]
        self.assertTrue(op and all(result.status == "UNVERIFIABLE" for result in op), op)

    def test_net_profit_mismatch_detected(self) -> None:
        rows = profit_rows()
        rows[-1] = ['四、净利润（净亏损以"-"号填列）', "23", "99"]
        results = check_profit_statement(parse_profit_rows(rows))
        self.assertTrue(any(result.code == "PL_NET_PROFIT" and result.status == "FAIL"
                            for result in results))


class TestCrossStatement(unittest.TestCase):
    def test_retained_matches_net_profit(self) -> None:
        balance = parse_balance_rows(balance_rows())
        profit = parse_profit_rows(profit_rows())
        results = check_cross_statement(balance, profit)
        # 2024: Δ未分配利润 = 15-10 = 5，净利润 = 27 → 差额 INFO
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, "INFO")
        self.assertEqual(results[0].delta, "-22.00")

    def test_retained_exact_pass(self) -> None:
        rows = balance_rows()
        rows[-1] = [None, None, None, "未分配利润", "10", "37"]
        balance = parse_balance_rows(rows)
        profit = parse_profit_rows(profit_rows())
        results = check_cross_statement(balance, profit)
        self.assertEqual(results[0].status, "PASS")

    def test_summary_counts(self) -> None:
        counts = summarize(check_balance_sheet(parse_balance_rows(balance_rows())))
        self.assertGreater(counts["PASS"], 0)
        self.assertEqual(counts["FAIL"], 0)


class TestDraftColumns(unittest.TestCase):
    def _imbalanced_rows(self):
        rows = balance_rows()
        rows[2] = ["流动资产合计", "60", "70", "负债合计", "50", "45"]  # 2023 负债多 10
        return rows

    def test_imbalance_in_draft_year_is_not_fail(self) -> None:
        results = check_balance_sheet(parse_balance_rows(self._imbalanced_rows()),
                                      draft_years=("2023",))
        identity = [r for r in results if r.code == "BS_IDENTITY" and r.year == "2023"][0]
        self.assertEqual(identity.status, "UNVERIFIED_DRAFT")
        self.assertEqual(identity.delta, "-10.00")
        self.assertEqual(summarize(results)["FAIL"], 0)

    def test_imbalance_in_final_year_is_fail(self) -> None:
        results = check_balance_sheet(parse_balance_rows(self._imbalanced_rows()))
        identity = [r for r in results if r.code == "BS_IDENTITY" and r.year == "2023"][0]
        self.assertEqual(identity.status, "FAIL")


class TestDiagnosePatterns(unittest.TestCase):
    def test_frozen_right_column_and_unposted_profit(self) -> None:
        years = ("2024", "2025")
        balance = BalanceTable(
            years=years,
            left={"资产总计": {"2024": to_decimal("100"), "2025": to_decimal("120")}},
            right={
                "负债合计": {"2024": to_decimal("40"), "2025": to_decimal("40")},
                "所有者权益（或股东权益）合计": {"2024": to_decimal("60"),
                                                "2025": to_decimal("60")},
                "未分配利润": {"2024": to_decimal("10"), "2025": to_decimal("10")},
            },
        )
        profit = ProfitTable(
            years=years,
            rows={"净利润（净亏损以\"-\"号填列）": {"2024": to_decimal("1"),
                                                "2025": to_decimal("-5")}},
        )
        hints = diagnose_patterns(balance, profit)
        text = "\n".join(hints)
        self.assertIn("负债和所有者权益列与 2024 年逐行完全相同", text)
        self.assertIn("净利润 -5.00 未反映到未分配利润", text)

    def test_no_hint_when_columns_change(self) -> None:
        years = ("2024", "2025")
        balance = BalanceTable(
            years=years,
            left={"资产总计": {"2024": to_decimal("100"), "2025": to_decimal("120")}},
            right={
                "负债合计": {"2024": to_decimal("40"), "2025": to_decimal("50")},
                "所有者权益（或股东权益）合计": {"2024": to_decimal("60"),
                                                "2025": to_decimal("70")},
                "未分配利润": {"2024": to_decimal("10"), "2025": to_decimal("20")},
            },
        )
        profit = ProfitTable(
            years=years,
            rows={"净利润（净亏损以\"-\"号填列）": {"2024": to_decimal("1"),
                                                "2025": to_decimal("10")}},
        )
        self.assertEqual(diagnose_patterns(balance, profit), [])


if __name__ == "__main__":
    unittest.main()
