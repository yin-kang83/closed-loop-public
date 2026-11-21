"""税务决策 MPC 测试：**第一原则护栏** + 合规硬约束 + 现金成本 + 优先级。

哲学边界测试是硬要求：法定税负不可优化、候选必须有官方依据、违法/违规候选必须被排除。
"""

from __future__ import annotations

import unittest

from loop_mvp.mpc import MpcConfig, MpcPlanner
from loop_mvp.tax_planning import (
    OPTIMIZABLE_OBJECTIVES,
    OverpaymentDecision,
    PrepaymentMethodDecision,
    validate_objectives,
)
from loop_mvp.types import dec


class TestPhilosophyGuards(unittest.TestCase):
    """第一原则：不做法外帮凶（legalwiki-kb/docs/哲学基础.md）。"""

    def test_tax_cannot_be_optimized(self) -> None:
        validate_objectives(("compliance", "cash"))  # 允许
        with self.assertRaises(ValueError):
            validate_objectives(("compliance", "tax"))
        with self.assertRaises(ValueError):
            validate_objectives(("tax",))

    def test_only_compliance_and_cash_optimizable(self) -> None:
        self.assertEqual(OPTIMIZABLE_OBJECTIVES, ("compliance", "cash"))

    def test_all_candidates_have_legal_basis(self) -> None:
        over = OverpaymentDecision(overpayment=dec("1000"), next_year_tax_due=dec("5000"))
        prepay = PrepaymentMethodDecision(annual_tax_due=dec("1000"),
                                          actual_profit_prepaid=dec("1000"),
                                          average_prior_year_prepaid=dec("500"))
        for option in over.options() + prepay.options():
            self.assertTrue(option.legal_basis, option.label)

    def test_tax_below_statutory_is_compliance_violation(self) -> None:
        decision = OverpaymentDecision(overpayment=dec("1000"), next_year_tax_due=dec("5000"))
        codes = decision.compliance_violations({"refund": dec("1000"), "offset": dec("0"),
                                                "tax": dec("4999")})
        self.assertIn("TAX_BELOW_STATUTORY", codes)

    def test_tax_not_statutory_flagged(self) -> None:
        decision = OverpaymentDecision(overpayment=dec("1000"), next_year_tax_due=dec("5000"))
        codes = decision.compliance_violations({"refund": dec("1000"), "offset": dec("0"),
                                                "tax": dec("5001")})
        self.assertIn("TAX_NOT_STATUTORY", codes)


class TestOverpaymentDecision(unittest.TestCase):
    def test_over_claim_violation(self) -> None:
        decision = OverpaymentDecision(overpayment=dec("1000"), next_year_tax_due=dec("5000"))
        codes = decision.compliance_violations(
            {"refund": dec("0"), "offset": dec("1000.01"), "tax": dec("5000")})
        self.assertIn("OVER_CLAIM", codes)

    def test_refund_cheaper_under_default_assumption(self) -> None:
        decision = OverpaymentDecision(overpayment=dec("1000"), next_year_tax_due=dec("5000"),
                                       refund_delay_days=90, offset_effect_days=365)
        scored = dict(decision.scored())
        self.assertLess(scored["全额申请退税"]["cash"], scored["全额抵缴下一年度"]["cash"])

    def test_refund_delay_flips_decision(self) -> None:
        planner = MpcPlanner(MpcConfig(objectives=("compliance", "cash")))
        short = OverpaymentDecision(overpayment=dec("1000"), next_year_tax_due=dec("5000"),
                                    refund_delay_days=30)
        long = OverpaymentDecision(overpayment=dec("1000"), next_year_tax_due=dec("5000"),
                                   refund_delay_days=400)
        self.assertEqual(planner.select_scored(short.scored()).chosen.label, "全额申请退税")
        self.assertEqual(planner.select_scored(long.scored()).chosen.label, "全额抵缴下一年度")

    def test_violating_candidate_never_chosen(self) -> None:
        decision = OverpaymentDecision(overpayment=dec("1000"), next_year_tax_due=dec("5000"))
        planner = MpcPlanner(MpcConfig(objectives=("compliance", "cash")))
        selection = planner.select_scored(decision.scored())
        self.assertNotEqual(selection.chosen.label, "超额抵缴（违规示例）")
        self.assertEqual(selection.chosen.scores["compliance"], dec("0"))


class TestPrepaymentDecision(unittest.TestCase):
    def test_unapproved_method_is_compliance_violation(self) -> None:
        decision = PrepaymentMethodDecision(
            annual_tax_due=dec("6000"), actual_profit_prepaid=dec("5923"),
            average_prior_year_prepaid=dec("20000"))
        scored = dict(decision.scored())
        self.assertEqual(scored["按实际利润额预缴"]["compliance"], dec("0"))
        self.assertEqual(scored["按上年度平均额预缴（需核定）"]["compliance"], dec("1"))

    def test_compliance_first_rejects_unapproved_even_if_cash_better(self) -> None:
        # 平均额预缴恰好足额（现金成本为 0），比实际利润额更"省现金"，但未核定 → 必须排除
        decision = PrepaymentMethodDecision(
            annual_tax_due=dec("1000"), actual_profit_prepaid=dec("5000"),
            average_prior_year_prepaid=dec("1000"))
        planner = MpcPlanner(MpcConfig(objectives=("compliance", "cash")))
        selection = planner.select_scored(decision.scored())
        self.assertEqual(selection.chosen.label, "按实际利润额预缴")

    def test_approved_other_method_can_be_chosen_by_cash(self) -> None:
        decision = PrepaymentMethodDecision(
            annual_tax_due=dec("1000"), actual_profit_prepaid=dec("5000"),
            average_prior_year_prepaid=dec("1000"), other_method_approved=True)
        planner = MpcPlanner(MpcConfig(objectives=("compliance", "cash")))
        selection = planner.select_scored(decision.scored())
        self.assertEqual(selection.chosen.label, "按上年度平均额预缴（需核定）")


if __name__ == "__main__":
    unittest.main()
