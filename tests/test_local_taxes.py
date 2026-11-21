"""地方税种核对测试（合成表；真实数据由 tools/real_local_tax_check.py 跑）。"""

from __future__ import annotations

import unittest
from datetime import datetime

from loop_mvp.local_taxes import (
    check_land_use_tax,
    check_property_tax,
    check_stamp_duty,
    check_surtax,
)


def stamp_rows(amount: str = "500", paid: str = "500", owed: str = "0"):
    type_row = [None] * 12
    type_row[8] = "产权转移书据"
    rate_row = [None] * 12
    rate_row[8] = 0.0005
    data_row = [None] * 17
    data_row[0] = datetime(2020, 1, 1)
    data_row[8] = 1000000
    data_row[13] = amount
    data_row[14] = paid
    data_row[15] = owed
    return [["标题"], ["", "201382.99"], ["时间", "类型"], type_row, rate_row, data_row]


def property_rows(remain: str = "4324939.49"):
    row = [None] * 23
    row[3] = 6178484.98
    row[4] = 0.3
    row[5] = float(remain)
    row[6] = 0.012
    row[7] = 12974.82
    row[12] = 0
    return [["房产税计算表"], [], [], [], row]


def land_rows(tax: str = "78303.01", owed: str = "0"):
    row = [None] * 34
    row[2] = 4.5
    row[7] = 69602.6792419178
    row[8] = float(tax)
    row[29] = float(tax)
    row[30] = float(tax)
    row[31] = float(owed)
    return [["城镇土地使用税计算表"], [], [], row]


def surtax_rows(tax: str = "70", total: str = "70"):
    row = [None] * 52
    row[1] = "城建税"
    row[2] = float(total)
    row[4] = 1000
    row[5] = 0.07
    row[6] = float(tax)
    return [["标题"], [], row]


class TestStampDuty(unittest.TestCase):
    def test_formula_and_owed_pass(self) -> None:
        checks = check_stamp_duty(stamp_rows())
        self.assertEqual([c.status for c in checks], ["PASS", "PASS"])

    def test_formula_flagged(self) -> None:
        checks = check_stamp_duty(stamp_rows(amount="501"))
        self.assertEqual(checks[0].status, "FLAG")

    def test_owed_flagged(self) -> None:
        checks = check_stamp_duty(stamp_rows(amount="500", paid="500", owed="1"))
        self.assertEqual(checks[1].status, "FLAG")


class TestPropertyTax(unittest.TestCase):
    def test_pass(self) -> None:
        checks = check_property_tax(property_rows())
        self.assertTrue(checks)
        self.assertTrue(all(check.status == "PASS" for check in checks), checks)

    def test_mismatch_flagged(self) -> None:
        checks = check_property_tax(property_rows(remain="4324940.49"))
        self.assertTrue(any(check.code == "PROPERTY_REMAIN" and check.status == "FLAG"
                            for check in checks))


class TestLandUseTax(unittest.TestCase):
    def test_pass(self) -> None:
        checks = check_land_use_tax(land_rows())
        self.assertTrue(checks)
        self.assertTrue(all(check.status == "PASS" for check in checks), checks)

    def test_owed_flagged(self) -> None:
        checks = check_land_use_tax(land_rows(owed="-100"))
        self.assertTrue(any(check.code == "LAND_OWED" and check.status == "FLAG"
                            for check in checks))


class TestSurtax(unittest.TestCase):
    def test_pass(self) -> None:
        checks = check_surtax(surtax_rows())
        self.assertTrue(checks)
        self.assertTrue(all(check.status == "PASS" for check in checks), checks)

    def test_mismatch_flagged(self) -> None:
        checks = check_surtax(surtax_rows(tax="71"))
        self.assertTrue(any(check.status == "FLAG" for check in checks))


if __name__ == "__main__":
    unittest.main()
