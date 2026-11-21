"""A100000 企业所得税申报表复算的纯函数测试（合成表，不依赖 E 盘）。"""

from __future__ import annotations

import unittest

from loop_mvp.real_checks import summarize
from loop_mvp.tax_checks import check_cit_return, parse_cit_return_rows

HEADER = ["行次", "类别", "项        目", "2021年税务系统", "2022年税务系统"]


def cit_rows():
    return [
        HEADER,
        ["1", "利润总额计算", "一、营业收入(填写A101010)", "100", "200"],
        ["2", "", "减：营业成本(填写A102010)", "60", "120"],
        ["3", "", "减：税金及附加", "2", "3"],
        ["4", "", "减：销售费用(填写A104000)", "5", "6"],
        ["5", "", "减：管理费用(填写A104000)", "4", "5"],
        ["6", "", "减：研发费用(填写A104000)", "1", "1"],
        ["7", "", "减：财务费用(填写A104000)", "3", "4"],
        ["8", "", "加：其他收益", "0", "0"],
        ["9", "", "加：投资收益（损失以'－'号填列）", "0", "0"],
        ["10", "", "加：净敞口套期收益（损失以'－'号填列）", "0", "0"],
        ["11", "", "加：公允价值变动收益", "0", "0"],
        ["12", "", "加：信用减值损失（损失以'－'号填列）", "0", "0"],
        ["13", "", "加：资产减值损失（损失以'－'号填列）", "0", "0"],
        ["14", "", "加：资产处置收益（损失以'－'号填列）", "0", "0"],
        ["15", "", "二、营业利润(1-2-3-4-5-6-7+8+9)", "25", "61"],
        ["16", "", "加：营业外收入", "1", "0"],
        ["17", "", "减：营业外支出", "0", "0"],
        ["18", "", "三、利润总额（10+11-12）", "26", "61"],
        ["19", "应纳税所得额计算", "减：境外所得（填写A108010）", "0", "0"],
        ["20", "", "加：纳税调整增加额（填写A105000）", "10", "20"],
        ["21", "", "减：纳税调整减少额（填写A105000）", "2", "5"],
        ["22", "", "减：免税、减计收入及加计扣除（22.1＋22.2＋…）", "0", "0"],
        ["23", "", "加：境外应税所得抵减境内亏损（填写A108000）", "0", "0"],
        ["24", "", "四、纳税调整后所得（18－19＋20－21－22＋23）", "34", "76"],
        ["25", "", "减：所得减免（填写A107020）", "0", "0"],
        ["26", "", "减：弥补以前年度亏损（填写A106000）", "4", "16"],
        ["27", "", "减：抵扣应纳税所得额（填写A107030）", "0", "0"],
        ["28", "", "五、应纳税所得额（24－25－26－27）", "30", "60"],
        ["29", "应纳税额计算", "税率（25%）", "0.25", "0.25"],
        ["30", "", "六、应纳所得税额（28×29）", "7.5", "15"],
        ["31", "", "减：减免所得税额（31.1＋31.2＋…）", "0", "0"],
        ["32", "", "减：抵免所得税额（填写A107050）", "0", "0"],
        ["33", "", "七、应纳税额（30－31－32）", "7.5", "15"],
        ["34", "", "加：境外所得应纳所得税额（填写A108000）", "0", "0"],
        ["35", "", "减：境外所得抵免所得税额（填写A108000）", "0", "0"],
        ["36", "", "八、实际应纳所得税额（33＋34－35）", "7.5", "15"],
        ["37", "实际应纳税额计算", "减：本年累计实际已缴纳的所得税额", "3", "10"],
        ["38", "", "九、本年应补（退）所得税额（36－37）", "4.5", "5"],
        ["42", "", "减：民族自治地区企业所得税地方分享部分", "0", "0"],
        ["43", "", "减：稽查查补（退）所得税额", "0", "0"],
        ["44", "", "减：特别纳税调整补（退）所得税额", "0", "0"],
        ["45", "", "十、本年实际应补（退）所得税额（38－42－43－44）", "4.5", "5"],
    ]


class TestCitReturnChecks(unittest.TestCase):
    def test_parse_and_pass(self) -> None:
        table = parse_cit_return_rows(cit_rows())
        self.assertEqual(table.years, ("2021", "2022"))
        self.assertEqual(table.get("营业收入", "2021"), 100)
        results = check_cit_return(table)
        fails = [result for result in results if result.status != "PASS"]
        self.assertEqual(fails, [], fails)
        self.assertGreaterEqual(len(results), 18)

    def test_tolerance_for_rounded_tax(self) -> None:
        rows = cit_rows()
        for row in rows:
            if row[2].startswith("五、应纳税所得额"):
                row[3], row[4] = "0.00435", "60"
            if row[2].startswith("六、应纳所得税额"):
                row[3] = "0"  # 0.0011 元四舍五入为 0
        results = check_cit_return(parse_cit_return_rows(rows))
        tax_due = [result for result in results
                   if result.code == "CIT_TAX_DUE" and result.year == "2021"][0]
        self.assertEqual(tax_due.status, "PASS")

    def test_mismatch_detected(self) -> None:
        rows = cit_rows()
        for row in rows:
            if row[2].startswith("三、利润总额"):
                row[3] = "27"  # 应为 26
        results = check_cit_return(parse_cit_return_rows(rows))
        fails = [result for result in results if result.status == "FAIL"]
        self.assertTrue(any(result.code == "CIT_TOTAL_PROFIT" and result.year == "2021"
                            for result in fails), fails)
        self.assertGreaterEqual(summarize(results)["FAIL"], 1)

    def test_missing_target_is_unverifiable(self) -> None:
        rows = [row for row in cit_rows() if not row[2].startswith("五、应纳税所得额")]
        results = check_cit_return(parse_cit_return_rows(rows))
        taxable = [result for result in results if result.code == "CIT_TAXABLE_INCOME"]
        self.assertTrue(taxable)
        self.assertTrue(all(result.status == "UNVERIFIABLE" for result in taxable))


if __name__ == "__main__":
    unittest.main()
