"""凭证级闭环域测试（合成数据；真实数据由 tools/real_voucher_loop.py 跑）。"""

from __future__ import annotations

import unittest
from datetime import date

from loop_mvp.finance_voucher import (
    ADMIN,
    FINANCE,
    SALES,
    FinanceVoucherDomain,
    FinanceVoucherEnvironment,
)
from loop_mvp.loop import ClosedLoop, LoopConfig
from loop_mvp.types import dec


def make_facts(sales="10", admin="5", finance="1", unknown="0", closing="3"):
    return {
        "V:销售费用": dec(sales),
        "V:管理费用": dec(admin),
        "V:财务费用": dec(finance),
        "V:未映射项目数": dec(unknown),
        "V:结转行数": dec(closing),
    }


def make_declared(sales="10", admin="5", finance="1"):
    return {SALES: dec(sales), ADMIN: dec(admin), FINANCE: dec(finance)}


def build_loop(facts_by_year, declared_by_year):
    periods = tuple(sorted(facts_by_year))
    dates = {year: date(int(year), 12, 31) for year in periods}
    domain = FinanceVoucherDomain()
    env = FinanceVoucherEnvironment(periods, facts_by_year, declared_by_year, dates)
    return ClosedLoop(domain, env, LoopConfig(periods=periods, business_dates=dates))


class TestFinanceVoucherDomain(unittest.TestCase):
    def test_aggregate_transition(self) -> None:
        domain = FinanceVoucherDomain()
        hypotheses = domain.resolve_hypotheses(date(2018, 12, 31), None)
        plan = domain.plan(domain.initial_state(), make_facts(), hypotheses, date(2018, 12, 31))
        action = plan.actions[0].variants["*"]
        post = domain.transition(domain.initial_state(), action, hypotheses.single().params)
        self.assertEqual(post.get(SALES), dec("10"))
        self.assertEqual(post.get(FINANCE), dec("1"))
        self.assertEqual(domain.violations(post, action, hypotheses.single().params), ())

    def test_unknown_item_is_violation(self) -> None:
        domain = FinanceVoucherDomain()
        hypotheses = domain.resolve_hypotheses(date(2018, 12, 31), None)
        plan = domain.plan(domain.initial_state(), make_facts(unknown="1"),
                          hypotheses, date(2018, 12, 31))
        action = plan.actions[0].variants["*"]
        post = domain.transition(domain.initial_state(), action, hypotheses.single().params)
        self.assertIn("UNKNOWN_EXPENSE_ITEM",
                      domain.violations(post, action, hypotheses.single().params))


class TestFinanceVoucherLoop(unittest.TestCase):
    def test_clean_loop_reproduces_report(self) -> None:
        loop = build_loop(
            {"2018": make_facts(), "2019": make_facts()},
            {"2018": make_declared(), "2019": make_declared()},
        )
        report = loop.run()
        self.assertEqual(report.metrics["closed_periods"], 2)
        self.assertEqual(report.metrics["residual_flagged"], 0)
        self.assertEqual(report.metrics["coverage"], 1.0)
        self.assertEqual(report.metrics["false_accept"], 0)
        self.assertTrue(report.metrics["audit_ok"])
        self.assertTrue(report.metrics["replay_ok"])
        self.assertEqual(report.metrics["reuse_valid"], 1)

    def test_mismatch_flagged(self) -> None:
        loop = build_loop(
            {"2018": make_facts(), "2019": make_facts(finance="1")},
            {"2018": make_declared(), "2019": make_declared(finance="2")},
        )
        report = loop.run()
        self.assertEqual(report.metrics["residual_flagged"], 1)
        self.assertLess(report.metrics["coverage"], 1.0)
        self.assertGreaterEqual(report.metrics["model_errors"], 1)

    def test_unknown_item_blocks(self) -> None:
        loop = build_loop(
            {"2018": make_facts(unknown="1")},
            {"2018": make_declared()},
        )
        report = loop.run()
        self.assertGreaterEqual(report.metrics["block"], 1)
        self.assertEqual(report.metrics["closed_periods"], 0)


if __name__ == "__main__":
    unittest.main()
