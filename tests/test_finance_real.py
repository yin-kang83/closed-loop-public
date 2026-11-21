"""财务真实账套闭环域测试（合成报表数据；真实数据由 tools/real_finance_loop.py 跑）。"""

from __future__ import annotations

import unittest
from datetime import date

from loop_mvp.finance_real import (
    FinanceRealDomain,
    FinanceRealEnvironment,
)
from loop_mvp.loop import ClosedLoop, LoopConfig
from loop_mvp.types import dec, q

DATES = {"2024": date(2024, 12, 31), "2025": date(2025, 12, 31)}


def base_facts(net_profit: str = "10", reported_re: str = "0",
               reported_le: str = "150", cash: str = "100", inventory: str = "50",
               payable: str = "50", paid_in: str = "100") -> dict:
    return {
        "L:货币资金": dec(cash),
        "L:存货": dec(inventory),
        "L:流动资产合计": dec(cash) + dec(inventory),
        "L:非流动资产合计": dec("0"),
        "L:资产总计": dec(cash) + dec(inventory),
        "R:应付账款": dec(payable),
        "R:流动负债合计": dec(payable),
        "R:非流动负债合计": dec("0"),
        "R:负债合计": dec(payable),
        "R:实收资本（或股本）": dec(paid_in),
        "R:未分配利润": dec(reported_re),
        "R:所有者权益（或股东权益）合计": dec(paid_in) + dec(reported_re),
        "R:负债和所有者权益（或股东权益）总计": dec(reported_le),
        "P:净利润": dec(net_profit),
    }


def build_loop(facts_by_year, declared_by_year, opening_re: str = "-10"):
    periods = tuple(sorted(facts_by_year))
    domain = FinanceRealDomain(opening_re=q(opening_re))
    domain.required_fact_keys = tuple(sorted(facts_by_year[periods[0]].keys()))
    env = FinanceRealEnvironment(periods, facts_by_year, declared_by_year, DATES)
    return ClosedLoop(domain, env, LoopConfig(periods=periods, business_dates=DATES))


class TestFinanceRealDomain(unittest.TestCase):
    def test_identity_breach_detected(self) -> None:
        facts = base_facts()
        facts["R:负债和所有者权益（或股东权益）总计"] = dec("149")  # 左右差 1
        domain = FinanceRealDomain(opening_re=dec("-10"))
        hypotheses = domain.resolve_hypotheses(date(2024, 12, 31), None)
        plan = domain.plan(domain.initial_state(), facts, hypotheses, date(2024, 12, 31))
        build = plan.actions[0].variants["*"]
        post = domain.transition(domain.initial_state(), build, hypotheses.single().params)
        codes = domain.violations(post, build, hypotheses.single().params)
        self.assertIn("LE_TOTAL_MISMATCH", codes)

    def test_unknown_account_detected(self) -> None:
        facts = base_facts()
        facts["L:神秘科目"] = dec("1")
        domain = FinanceRealDomain()
        hypotheses = domain.resolve_hypotheses(date(2024, 12, 31), None)
        plan = domain.plan(domain.initial_state(), facts, hypotheses, date(2024, 12, 31))
        build = plan.actions[0].variants["*"]
        post = domain.transition(domain.initial_state(), build, hypotheses.single().params)
        codes = domain.violations(post, build, hypotheses.single().params)
        self.assertIn("UNKNOWN_ACCOUNT", codes)

    def test_roll_forward_expected(self) -> None:
        facts = base_facts(net_profit="10", reported_re="0")
        domain = FinanceRealDomain(opening_re=dec("-10"))
        hypotheses = domain.resolve_hypotheses(date(2024, 12, 31), None)
        plan = domain.plan(domain.initial_state(), facts, hypotheses, date(2024, 12, 31))
        roll = plan.actions[1].variants["*"]
        post = domain.transition(domain.initial_state(), roll, hypotheses.single().params)
        self.assertEqual(post.get("FR_预期未分配利润"), q("0"))


class TestFinanceRealLoop(unittest.TestCase):
    def test_clean_year_closes_and_matches(self) -> None:
        facts = {"2024": base_facts(net_profit="10", reported_re="0", reported_le="150")}
        declared = {"2024": {"FR_负债和所有者权益总计": dec("150"),
                             "FR_预期未分配利润": dec("0")}}
        loop = build_loop(facts, declared)
        report = loop.run()
        self.assertEqual(report.metrics["closed_periods"], 1)
        self.assertEqual(report.metrics["residual_flagged"], 0)
        self.assertEqual(report.metrics["coverage"], 1.0)
        self.assertEqual(report.metrics["false_accept"], 0)
        self.assertTrue(report.metrics["audit_ok"])
        self.assertTrue(report.metrics["replay_ok"])

    def test_roll_forward_mismatch_flagged(self) -> None:
        # 账面平衡：资产 180 = 负债 50 + 权益(实收 100 + 未分配 30)；
        # 但期初 -10 + 净利润 10 = 0，报告未分配利润 30 → 残差越界
        facts = {"2024": base_facts(net_profit="10", reported_re="30", reported_le="180",
                                    cash="130", inventory="50", paid_in="100")}
        declared = {"2024": {"FR_负债和所有者权益总计": dec("180"),
                             "FR_预期未分配利润": dec("30")}}
        loop = build_loop(facts, declared)
        report = loop.run()
        self.assertEqual(report.metrics["residual_flagged"], 1)
        self.assertLess(report.metrics["coverage"], 1.0)
        self.assertGreaterEqual(report.metrics["model_errors"], 1)

    def test_inconsistent_balance_blocks(self) -> None:
        facts = {"2024": base_facts(reported_le="149")}  # 负债+权益 150 ≠ 左表总计 150? 改右总计
        declared = {"2024": {"FR_负债和所有者权益总计": dec("149"),
                             "FR_预期未分配利润": dec("0")}}
        loop = build_loop(facts, declared)
        report = loop.run()
        self.assertEqual(report.metrics["closed_periods"], 0)
        self.assertGreaterEqual(report.metrics["block"], 1)


if __name__ == "__main__":
    unittest.main()
