"""企业所得税汇算闭环域测试（合成数据；真实数据由 tools/real_tax_loop.py 跑）。"""

from __future__ import annotations

import unittest
from datetime import date

from loop_mvp.estimator import HypothesisSet
from loop_mvp.gate import SafetyGate
from loop_mvp.loop import ClosedLoop, LoopConfig
from loop_mvp.rules import RuleVersion
from loop_mvp.tax_cit import (
    CARRY,
    SKELETON,
    TAX,
    TAXABLE,
    TaxCitDomain,
    TaxCitEnvironment,
)
from loop_mvp.types import (
    ActionOp,
    PlannedAction,
    Prediction,
    StateSnapshot,
    dec,
    q,
)

DATES = {"2019": date(2019, 12, 31), "2020": date(2020, 12, 31), "2021": date(2021, 12, 31)}


def build_env(carry_bias: str = "0"):
    periods = ("2019", "2020", "2021")
    facts = {
        "2019": {"adjusted_income": dec("300")},
        "2020": {"adjusted_income": dec("-50")},
        "2021": {"adjusted_income": dec("80")},
    }
    declared = {
        "2019": {TAXABLE: dec("200"), TAX: dec("50")},
        "2020": {TAXABLE: dec("0"), TAX: dec("0")},
        "2021": {TAXABLE: dec("30"), TAX: dec("7.5")},
    }
    domain = TaxCitDomain(initial_carry=q("100") + q(carry_bias))
    env = TaxCitEnvironment(periods, facts, declared, DATES)
    loop = ClosedLoop(domain, env, LoopConfig(periods=periods, business_dates=DATES))
    return domain, loop


class TestTaxCitDomain(unittest.TestCase):
    def test_carry_offset_and_tax(self) -> None:
        domain = TaxCitDomain(initial_carry=q("100"))
        state = domain.initial_state()
        carry = ActionOp("CARRY_LOSS", {"base": dec("300"), "amount": dec("100"),
                                         "prior_carry": dec("100")})
        state = domain.transition(state, carry, {})
        self.assertEqual(state.get(CARRY), q("0"))
        self.assertEqual(state.get(TAXABLE), q("200"))
        self.assertEqual(domain.violations(state, carry, {}), ())

        assess = ActionOp("ASSESS_TAX", {"taxable": dec("200")})
        state = domain.transition(state, assess, {"rate": dec("0.25")})
        self.assertEqual(state.get(TAX), q("50"))
        self.assertEqual(domain.violations(state, assess, {"rate": dec("0.25")}), ())

    def test_over_offset_detected(self) -> None:
        domain = TaxCitDomain(initial_carry=q("100"))
        state = domain.initial_state()
        action = ActionOp("CARRY_LOSS", {"base": dec("50"), "amount": dec("100"),
                                         "prior_carry": dec("100")})
        state = domain.transition(state, action, {})
        self.assertIn("OVER_OFFSET", domain.violations(state, action, {}))

    def test_tax_formula_mismatch_detected(self) -> None:
        domain = TaxCitDomain()
        state = domain.initial_state().with_values(
            {CARRY: dec("0"), TAXABLE: dec("100"), TAX: dec("99")})
        assess = ActionOp("ASSESS_TAX", {"taxable": dec("100")})
        self.assertIn("TAX_FORMULA_MISMATCH", domain.violations(state, assess, {"rate": dec("0.25")}))

    def test_plan_skeleton_and_observation_step(self) -> None:
        domain = TaxCitDomain(initial_carry=q("100"))
        hypotheses = domain.resolve_hypotheses(date(2019, 12, 31), None)
        plan = domain.plan(domain.initial_state(), {"adjusted_income": dec("300")},
                           hypotheses, date(2019, 12, 31))
        self.assertEqual(plan.skeleton, SKELETON)
        self.assertEqual(domain.observation_step(plan), 1)
        with self.assertRaises(ValueError):
            domain.instantiate(("CARRY_LOSS",), domain.initial_state(),
                               {"adjusted_income": dec("300")}, hypotheses, date(2019, 12, 31))

    def test_gate_blocks_over_offset(self) -> None:
        domain = TaxCitDomain(initial_carry=q("100"))
        hypotheses = domain.resolve_hypotheses(date(2019, 12, 31), None)
        state = domain.initial_state()
        bad = ActionOp("CARRY_LOSS", {"base": dec("50"), "amount": dec("100"),
                                      "prior_carry": dec("100")})
        planned = PlannedAction("CARRY_LOSS", "多弥补", {"*": bad})
        scenarios = {hyp.hyp_id: domain.transition(state, bad, hyp.params)
                     for hyp in hypotheses.hypotheses}
        prediction = Prediction("2019", 0, "CARRY_LOSS", scenarios, {}, {})
        decision = SafetyGate(domain).admit(planned, prediction, hypotheses)
        self.assertEqual(decision.verdict, "BLOCK")
        self.assertIn("OVER_OFFSET", decision.codes)


class TestTaxCitLoop(unittest.TestCase):
    def test_clean_loop_reproduces_declared(self) -> None:
        _domain, loop = build_env()
        report = loop.run()
        self.assertEqual(report.metrics["closed_periods"], 3)
        self.assertEqual(report.metrics["executed_actions"], 9)
        self.assertEqual(report.metrics["residual_flagged"], 0)
        self.assertEqual(report.metrics["coverage"], 1.0)
        self.assertEqual(report.metrics["false_accept"], 0)
        self.assertTrue(report.metrics["audit_ok"])
        self.assertTrue(report.metrics["replay_ok"])
        self.assertEqual(report.metrics["replans"], 1)
        self.assertEqual(report.metrics["reuse_valid"], 2)

    def test_carry_bias_is_flagged(self) -> None:
        _domain, loop = build_env(carry_bias="0.20")
        report = loop.run()
        self.assertGreaterEqual(report.metrics["residual_flagged"], 1)
        self.assertLess(report.metrics["coverage"], 1.0)


if __name__ == "__main__":
    unittest.main()
