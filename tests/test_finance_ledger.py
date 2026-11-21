"""分录级闭环域测试（合成数据；真实数据由 tools/real_ledger_loop.py 跑）。"""

from __future__ import annotations

import unittest
from datetime import date

from loop_mvp.finance_ledger import (
    ASSETS,
    EQUITY,
    LIABILITIES,
    FinanceLedgerDomain,
    FinanceLedgerEnvironment,
)
from loop_mvp.loop import ClosedLoop, LoopConfig
from loop_mvp.types import dec

DATES = {"2018": date(2018, 12, 31), "2019": date(2019, 12, 31)}


def make_facts(assets="100", liabilities="60", equity="40", unbalanced="0",
               mismatch="0", pnl="0", debit="500", credit="500", unknown="0"):
    return {
        "J:资产余额": dec(assets),
        "J:负债余额": dec(liabilities),
        "J:权益余额": dec(equity),
        "J:凭证数": dec("10"),
        "J:不平衡凭证数": dec(unbalanced),
        "J:金额不符凭证数": dec(mismatch),
        "J:年度借方": dec(debit),
        "J:年度贷方": dec(credit),
        "J:损益余额": dec(pnl),
        "J:未知科目数": dec(unknown),
    }


def make_declared(assets="100", liabilities="60", equity="40"):
    return {ASSETS: dec(assets), LIABILITIES: dec(liabilities), EQUITY: dec(equity)}


def build_loop(facts_by_year, declared_by_year):
    periods = tuple(sorted(facts_by_year))
    dates = {year: DATES[year] for year in periods}
    domain = FinanceLedgerDomain()
    env = FinanceLedgerEnvironment(periods, facts_by_year, declared_by_year, dates)
    return ClosedLoop(domain, env, LoopConfig(periods=periods, business_dates=dates))


class TestFinanceLedgerDomain(unittest.TestCase):
    def test_clean_year_passes(self) -> None:
        domain = FinanceLedgerDomain()
        hypotheses = domain.resolve_hypotheses(date(2018, 12, 31), None)
        plan = domain.plan(domain.initial_state(), make_facts(), hypotheses, date(2018, 12, 31))
        check = plan.actions[0].variants["*"]
        post = domain.transition(domain.initial_state(), check, hypotheses.single().params)
        self.assertEqual(domain.violations(post, check, hypotheses.single().params), ())

    def test_unbalanced_voucher_violation(self) -> None:
        domain = FinanceLedgerDomain()
        hypotheses = domain.resolve_hypotheses(date(2018, 12, 31), None)
        plan = domain.plan(domain.initial_state(), make_facts(unbalanced="1"),
                           hypotheses, date(2018, 12, 31))
        check = plan.actions[0].variants["*"]
        post = domain.transition(domain.initial_state(), check, hypotheses.single().params)
        self.assertIn("UNBALANCED_VOUCHERS",
                      domain.violations(post, check, hypotheses.single().params))

    def test_pnl_not_closed_violation(self) -> None:
        domain = FinanceLedgerDomain()
        hypotheses = domain.resolve_hypotheses(date(2018, 12, 31), None)
        plan = domain.plan(domain.initial_state(), make_facts(pnl="1"),
                           hypotheses, date(2018, 12, 31))
        check = plan.actions[0].variants["*"]
        post = domain.transition(domain.initial_state(), check, hypotheses.single().params)
        self.assertIn("PNL_NOT_CLOSED",
                      domain.violations(post, check, hypotheses.single().params))


class TestFinanceLedgerLoop(unittest.TestCase):
    def test_clean_loop_reproduces_report(self) -> None:
        loop = build_loop({"2018": make_facts(), "2019": make_facts()},
                          {"2018": make_declared(), "2019": make_declared()})
        report = loop.run()
        self.assertEqual(report.metrics["closed_periods"], 2)
        self.assertEqual(report.metrics["residual_flagged"], 0)
        self.assertEqual(report.metrics["coverage"], 1.0)
        self.assertEqual(report.metrics["false_accept"], 0)
        self.assertTrue(report.metrics["audit_ok"])
        self.assertTrue(report.metrics["replay_ok"])

    def test_balance_mismatch_flagged(self) -> None:
        loop = build_loop({"2018": make_facts(assets="100", liabilities="60", equity="40")},
                          {"2018": make_declared(assets="200", liabilities="60", equity="40")})
        report = loop.run()
        self.assertEqual(report.metrics["residual_flagged"], 1)
        self.assertLess(report.metrics["coverage"], 1.0)

    def test_unbalanced_voucher_blocks(self) -> None:
        loop = build_loop({"2018": make_facts(unbalanced="1")},
                          {"2018": make_declared()})
        report = loop.run()
        self.assertGreaterEqual(report.metrics["block"], 1)
        self.assertEqual(report.metrics["closed_periods"], 0)


if __name__ == "__main__":
    unittest.main()
