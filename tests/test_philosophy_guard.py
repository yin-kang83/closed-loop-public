"""第一原则护栏测试：把"不做税务筹划/避税"变成每次测试都验证的工程约束。"""

from __future__ import annotations

import unittest

from tools.philosophy_check import (
    FORBIDDEN_NAME,
    run_checks,
)


class TestPhilosophyGuard(unittest.TestCase):
    def test_all_checks_pass(self) -> None:
        results = run_checks()
        failed = [item for item in results if item["ok"] != "PASS"]
        self.assertEqual(failed, [], failed)

    def test_forbidden_name_pattern(self) -> None:
        for name in ("minimize_tax", "optimize_tax_burden", "plan_tax_reduction",
                     "avoid_tax", "tax_evasion_plan"):
            self.assertTrue(FORBIDDEN_NAME.search(name), name)
        for name in ("statutory_tax", "cash_cost", "tax_amount", "prepaid_tax",
                     "compliance_violations"):
            self.assertFalse(FORBIDDEN_NAME.search(name), name)


if __name__ == "__main__":
    unittest.main()
