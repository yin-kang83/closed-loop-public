"""域与闸门测试：财务结账 / 板材下料 / 安全过滤 / 环境故障。"""

from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal

from loop_mvp.cutting import (
    BOARD,
    KERF,
    REMAIN,
    USED,
    YIELD,
    CuttingDomain,
    CuttingEnvironment,
    default_scenario as cutting_scenario,
)
from loop_mvp.domain import Domain
from loop_mvp.estimator import HypothesisSet
from loop_mvp.estimator import HypothesisSet, ResidualAnalyzer
from loop_mvp.finance import (
    A_AR,
    A_CAP,
    A_COST,
    A_INCTAX,
    A_INV,
    A_RE,
    A_REV,
    A_TAX,
    ALL_ACCOUNTS,
    FinanceDomain,
    FinanceEnvironment,
    default_scenario as finance_scenario,
)
from loop_mvp.gate import SafetyGate
from loop_mvp.loop import ClosedLoop, LoopConfig
from loop_mvp.types import (
    ActionOp,
    Interval,
    Observation,
    PlannedAction,
    Prediction,
    StateSnapshot,
    ZERO,
    dec,
    q,
)


class TestFinanceDomain(unittest.TestCase):
    def test_month_close_flow(self) -> None:
        domain = FinanceDomain()
        state = domain.initial_state()
        state = domain.transition(state, ActionOp("REVENUE", {"amount": dec("1200")}), {})
        self.assertEqual(state.get(A_AR), q("1200"))
        self.assertEqual(state.get(A_REV), q("1200"))
        state = domain.transition(state, ActionOp("COST", {"amount": dec("700")}), {})
        self.assertEqual(state.get(A_INV), q("4300"))
        tax = ActionOp("TAX", {"amount": dec("125")})
        state = domain.transition(state, tax, {})
        self.assertEqual(domain.violations(state, tax, {}), ())
        close = ActionOp("CLOSE", {})
        state = domain.transition(state, close, {})
        self.assertEqual(state.get(A_RE), q("5375"))
        self.assertEqual(state.get(A_REV), ZERO)
        self.assertEqual(state.get(A_COST), ZERO)
        self.assertEqual(state.get(A_INCTAX), ZERO)
        self.assertEqual(state.get(A_TAX), q("125"))
        self.assertEqual(domain.violations(state, close, {}), ())

    def test_settle_tax(self) -> None:
        domain = FinanceDomain()
        state = domain.initial_state()
        state = domain.transition(state, ActionOp("REVENUE", {"amount": dec("1200")}), {})
        state = domain.transition(state, ActionOp("COST", {"amount": dec("700")}), {})
        state = domain.transition(state, ActionOp("TAX", {"amount": dec("125")}), {})
        state = domain.transition(state, ActionOp("CLOSE", {}), {})
        settle = ActionOp("SETTLE_TAX", {"amount": dec("125")})
        state = domain.transition(state, settle, {})
        self.assertEqual(state.get(A_TAX), ZERO)
        self.assertEqual(domain.violations(state, settle, {}), ())

    def test_identity_breach_detected_for_imbalanced_operator(self) -> None:
        class BuggyRevenue(FinanceDomain):
            def transition(self, state, action, params):
                values = {key: state.get(key) for key in ALL_ACCOUNTS}
                if action.name == "REVENUE":
                    amount = action.decimal("amount")
                    values[A_AR] += amount
                    values[A_REV] += amount - dec("0.01")  # 记账 bug：差一分
                    return state.with_values({k: q(v) for k, v in values.items()})
                return super().transition(state, action, params)

        domain = BuggyRevenue()
        state = domain.initial_state()
        action = ActionOp("REVENUE", {"amount": dec("1200")})
        state = domain.transition(state, action, {})
        self.assertIn("IDENTITY_BREACH", domain.violations(state, action, {}))

    def test_rule_versions_and_ambiguity(self) -> None:
        domain = FinanceDomain()
        early = domain.resolve_hypotheses(date(2026, 1, 31), None)
        self.assertEqual(len(early.hypotheses), 1)
        self.assertEqual(early.single().decimal("rate"), dec("0.25"))

        april = domain.resolve_hypotheses(date(2026, 4, 30), None)
        self.assertEqual(len(april.hypotheses), 2)
        self.assertEqual({h.decimal("rate") for h in april.hypotheses},
                         {dec("0.25"), dec("0.20")})

        may = domain.resolve_hypotheses(date(2026, 5, 31), None)
        self.assertEqual(len(may.hypotheses), 1)

        with self.assertRaises(LookupError):
            domain.resolve_hypotheses(date(2025, 12, 31), None)

        self.assertNotEqual(domain.rule_hashes(date(2026, 3, 31)),
                            domain.rule_hashes(date(2026, 4, 1)))

    def test_plan_tax_variants_are_ambiguous(self) -> None:
        domain = FinanceDomain()
        hypotheses = domain.resolve_hypotheses(date(2026, 4, 30), None)
        plan = domain.plan(domain.initial_state(),
                           {"revenue": dec("1800"), "cost": dec("1000")},
                           hypotheses, date(2026, 4, 30))
        tax_step = plan.actions[3]
        self.assertTrue(tax_step.is_ambiguous())
        self.assertEqual(sorted(tax_step.amounts()), [q("160"), q("200")])
        self.assertEqual(domain.observation_step(plan), 4)

    def test_instantiate_rejects_wrong_skeleton(self) -> None:
        domain = FinanceDomain()
        hypotheses = domain.resolve_hypotheses(date(2026, 1, 31), None)
        with self.assertRaises(ValueError):
            domain.instantiate(("REVENUE",), domain.initial_state(),
                               {"revenue": dec("1"), "cost": dec("0")},
                               hypotheses, date(2026, 1, 31))


class TestCuttingDomain(unittest.TestCase):
    def test_transitions_and_conservation(self) -> None:
        domain = CuttingDomain()
        state = domain.initial_state()
        load = ActionOp("LOAD_BOARD", {"board_length_mm": dec("2440")})
        state = domain.transition(state, load, {})
        self.assertEqual(state.get(BOARD), q("2440"))
        cut = ActionOp("CUT_A", {"length_mm": dec("800")})
        state = domain.transition(state, cut, {"kerf_mm": dec("12")})
        self.assertEqual(state.get(REMAIN), q("1628"))
        self.assertEqual(state.get(USED), q("800"))
        self.assertEqual(state.get(KERF), q("12"))
        self.assertEqual(state.get(YIELD), q("32.79"))
        self.assertEqual(domain.violations(state, cut, {"kerf_mm": dec("12")}), ())

    def test_negative_remaining_detected(self) -> None:
        domain = CuttingDomain()
        state = domain.transition(domain.initial_state(),
                                  ActionOp("LOAD_BOARD", {"board_length_mm": dec("2440")}), {})
        cut = ActionOp("CUT_A", {"length_mm": dec("2440")})
        state = domain.transition(state, cut, {"kerf_mm": dec("12")})
        codes = domain.violations(state, cut, {"kerf_mm": dec("12")})
        self.assertIn("NEGATIVE_REMAINING", codes)

    def test_plan_uses_worst_case_kerf(self) -> None:
        domain = CuttingDomain()
        hypotheses = domain.resolve_hypotheses(date(2026, 1, 15), None)
        self.assertEqual(len(hypotheses.hypotheses), 2)
        facts = {
            "board_length_mm": dec("2440"),
            "demand_A": dec("2"), "demand_B": dec("1"), "demand_C": dec("1"),
            "length_A": dec("800"), "length_B": dec("700"), "length_C": dec("500"),
        }
        plan = domain.plan(domain.initial_state(), facts, hypotheses, date(2026, 1, 15))
        self.assertEqual(plan.skeleton, ("LOAD_BOARD", "CUT_A", "CUT_A", "CUT_B"))

        # 需求变大 → 骨架不匹配，复用必须被拒（触发重规划）
        bigger = {**facts, "demand_A": dec("5")}
        with self.assertRaises(ValueError):
            domain.instantiate(plan.skeleton, domain.initial_state(), bigger,
                               hypotheses, date(2026, 1, 15))

    def test_apply_observation_corrects_board_and_yield(self) -> None:
        domain = CuttingDomain()
        hypotheses = domain.resolve_hypotheses(date(2026, 1, 15), None)
        worst = max(hypotheses.hypotheses, key=lambda h: h.decimal("kerf_mm"))
        state = domain.initial_state()
        for name, length in (("LOAD_BOARD", None), ("CUT_A", "800"), ("CUT_A", "800"), ("CUT_B", "700")):
            if name == "LOAD_BOARD":
                op, params = ActionOp(name, {"board_length_mm": dec("2440")}), {}
            else:
                op, params = ActionOp(name, {"length_mm": dec(length)}), {"kerf_mm": worst.decimal("kerf_mm")}
            state = domain.transition(state, op, params)
        self.assertEqual(state.get(REMAIN), q("104"))
        self.assertEqual(state.get(USED), q("2300"))
        self.assertEqual(state.get(KERF), q("36"))

        observation = Observation("P01", {REMAIN: dec("64"), YIELD: dec("95.83")},
                                  "machine_measure", "P01")
        corrected = domain.apply_observation(state, observation)
        self.assertEqual(corrected.get(REMAIN), q("64"))
        self.assertEqual(corrected.get(BOARD), q("2400"))  # 实测板长 = 64 + 2300 + 36
        self.assertEqual(corrected.get(YIELD), q("95.83"))
        cut = ActionOp("CUT_B", {"length_mm": dec("700")})
        self.assertEqual(domain.violations(corrected, cut, {"kerf_mm": dec("12")}), ())


class TestFinanceCorrection(unittest.TestCase):
    """观测越界后的红字冲销：反解误差 → 修正 → 必须过闸门。"""

    def _closed_state(self):
        domain = FinanceDomain()
        state = domain.initial_state()
        for action in (
            ActionOp("REVENUE", {"amount": dec("1000")}),
            ActionOp("COST", {"amount": dec("500")}),
            ActionOp("TAX", {"amount": dec("125")}),
            ActionOp("CLOSE", {}),
        ):
            state = domain.transition(state, action, {})
        return domain, state

    def _report(self, state, observed_re, observed_tax):
        prediction = Prediction(
            period="P03", step=4, step_name="CLOSE", scenarios={"h": state},
            intervals={
                A_RE: Interval(state.get(A_RE), state.get(A_RE)),
                A_TAX: Interval(state.get(A_TAX), state.get(A_TAX)),
            },
            variant_fingerprints={"h": "x"},
        )
        observation = Observation(
            "P03", {A_RE: dec(observed_re), A_TAX: dec(observed_tax)}, "tax_receipt", "P03")
        return ResidualAnalyzer().analyze(prediction, observation), observation

    def test_correction_reverses_overstated_revenue(self) -> None:
        domain, state = self._closed_state()
        self.assertEqual(state.get(A_RE), q("5375"))
        report, observation = self._report(state, "5300", "100")
        self.assertFalse(report.in_bound)
        hypotheses = domain.resolve_hypotheses(date(2026, 1, 31), None)
        correction = domain.correction_action(report, observation, state, hypotheses)
        self.assertIsNotNone(correction)
        self.assertEqual(correction.decimal("revenue_error"), q("100"))
        self.assertEqual(correction.decimal("tax_error"), q("25"))
        post = domain.transition(state, correction, {})
        self.assertEqual(post.get(A_RE), q("5300"))
        self.assertEqual(post.get(A_TAX), q("100"))
        self.assertEqual(post.get(A_AR), q("900"))
        self.assertEqual(domain.violations(post, correction, {}), ())

    def test_no_correction_when_in_bound(self) -> None:
        domain, state = self._closed_state()
        prediction = Prediction(
            period="P03", step=4, step_name="CLOSE", scenarios={"h": state},
            intervals={A_RE: Interval(q("5300"), q("5375")),
                       A_TAX: Interval(q("100"), q("125"))},
            variant_fingerprints={"h": "x"},
        )
        observation = Observation("P03", {A_RE: dec("5350"), A_TAX: dec("120")},
                                  "tax_receipt", "P03")
        report = ResidualAnalyzer().analyze(prediction, observation)
        self.assertTrue(report.in_bound)
        hypotheses = domain.resolve_hypotheses(date(2026, 1, 31), None)
        self.assertIsNone(domain.correction_action(report, observation, state, hypotheses))

    def test_bad_correction_blocked_by_gate(self) -> None:
        domain, state = self._closed_state()
        hypotheses = domain.resolve_hypotheses(date(2026, 1, 31), None)
        bad = ActionOp("REVERSAL", {
            "revenue_error": dec("100"), "tax_error": dec("25"), "re_error": dec("75"),
            "target_re": dec("9999"), "target_tax": dec("100"),  # 目标被篡改
        })
        planned = PlannedAction("REVERSAL", "冲销", {"*": bad})
        scenarios = {hyp.hyp_id: domain.transition(state, bad, hyp.params)
                     for hyp in hypotheses.hypotheses}
        prediction = Prediction("P03", -2, "REVERSAL", scenarios, {}, {})
        decision = SafetyGate(domain).admit(planned, prediction, hypotheses)
        self.assertEqual(decision.verdict, "BLOCK")
        self.assertIn("CORRECTION_NOT_SETTLED", decision.codes)


class TestGate(unittest.TestCase):
    def test_allow_when_all_hypotheses_pass(self) -> None:
        domain = CuttingDomain()
        hypotheses = domain.resolve_hypotheses(date(2026, 1, 15), None)
        state = domain.transition(domain.initial_state(),
                                  ActionOp("LOAD_BOARD", {"board_length_mm": dec("2440")}), {})
        op = ActionOp("CUT_A", {"length_mm": dec("800")})
        planned = PlannedAction("CUT_A", "切A", {"*": op})
        scenarios = {h.hyp_id: domain.transition(state, op, h.params) for h in hypotheses.hypotheses}
        prediction = Prediction("P01", 0, "CUT_A", scenarios, {}, {})
        decision = SafetyGate(domain).admit(planned, prediction, hypotheses)
        self.assertEqual(decision.verdict, "ALLOW")

    def test_review_when_partial_hypotheses_violate(self) -> None:
        domain = CuttingDomain()
        hypotheses = domain.resolve_hypotheses(date(2026, 1, 15), None)
        state = domain.transition(domain.initial_state(),
                                  ActionOp("LOAD_BOARD", {"board_length_mm": dec("2440")}), {})
        op = ActionOp("CUT_A", {"length_mm": dec("2432")})  # 8mm 切缝可行，12mm 越界
        planned = PlannedAction("CUT_A", "切A", {"*": op})
        scenarios = {h.hyp_id: domain.transition(state, op, h.params) for h in hypotheses.hypotheses}
        prediction = Prediction("P01", 0, "CUT_A", scenarios, {}, {})
        decision = SafetyGate(domain).admit(planned, prediction, hypotheses)
        self.assertEqual(decision.verdict, "REVIEW")
        self.assertIn("NEGATIVE_REMAINING", decision.codes)

    def test_review_on_ambiguous_action(self) -> None:
        domain = FinanceDomain()
        hypotheses = domain.resolve_hypotheses(date(2026, 4, 30), None)
        state = domain.initial_state()
        state = domain.transition(state, ActionOp("REVENUE", {"amount": dec("1800")}), {})
        state = domain.transition(state, ActionOp("COST", {"amount": dec("1000")}), {})
        plan = domain.plan(state, {"revenue": dec("1800"), "cost": dec("1000")},
                           hypotheses, date(2026, 4, 30))
        tax_step = plan.actions[3]
        scenarios = {}
        for hyp in hypotheses.hypotheses:
            op = tax_step.variant_for(hyp.hyp_id)
            scenarios[hyp.hyp_id] = domain.transition(state, op, hyp.params)
        prediction = Prediction("P01", 3, "TAX", scenarios, {}, {})
        decision = SafetyGate(domain).admit(tax_step, prediction, hypotheses)
        self.assertEqual(decision.verdict, "REVIEW")
        self.assertIn("AMBIGUOUS_ACTION", decision.codes)

    def test_block_when_operator_bug_breaks_tax_link(self) -> None:
        class BuggyTax(FinanceDomain):
            def transition(self, state, action, params):
                if action.name == "TAX":
                    values = {key: state.get(key) for key in ALL_ACCOUNTS}
                    amount = action.decimal("amount")
                    values[A_INCTAX] += amount
                    values[A_CAP] += amount  # 记错科目：贷记实收资本
                    return state.with_values({k: q(v) for k, v in values.items()})
                return super().transition(state, action, params)

        domain = BuggyTax()
        hypotheses = domain.resolve_hypotheses(date(2026, 1, 31), None)
        state = domain.initial_state()
        state = domain.transition(state, ActionOp("REVENUE", {"amount": dec("1200")}), {})
        state = domain.transition(state, ActionOp("COST", {"amount": dec("700")}), {})
        planned = PlannedAction("TAX", "计税", {
            "*": ActionOp("TAX", {"amount": dec("125"), "rate": dec("0.25")})
        })
        op = planned.variants["*"]
        scenarios = {hyp.hyp_id: domain.transition(state, op, hyp.params) for hyp in hypotheses.hypotheses}
        prediction = Prediction("P01", 3, "TAX", scenarios, {}, {})
        decision = SafetyGate(domain).admit(planned, prediction, hypotheses)
        self.assertEqual(decision.verdict, "BLOCK")
        self.assertIn("TAX_MISMATCH", decision.codes)

    def test_missing_variant_reviews(self) -> None:
        domain = FinanceDomain()
        hypotheses = domain.resolve_hypotheses(date(2026, 4, 30), None)
        planned = PlannedAction("TAX", "计税", {hypotheses.hypotheses[0].hyp_id: ActionOp("TAX", {"amount": dec("1")})})
        prediction = Prediction("P01", 0, "TAX", {}, {}, {})
        decision = SafetyGate(domain).admit(planned, prediction, hypotheses)
        self.assertEqual(decision.verdict, "REVIEW")
        self.assertIn("MISSING_VARIANT", decision.codes)


class TestEnvironments(unittest.TestCase):
    def test_finance_extraction_fault_and_delay(self) -> None:
        periods, data, delays, _dates = finance_scenario()
        env = FinanceEnvironment(periods, data, delays)
        bundle = env.extract_facts("P03", date(2026, 3, 31))
        self.assertEqual(bundle.facts["revenue"], dec("1000"))  # 提取错值
        self.assertEqual(env.truth("P03")[A_TAX], dec("100"))
        observation, delay = env.finalize("P05")
        self.assertEqual(delay, 1)
        self.assertIn(A_RE, observation.values)

    def test_cutting_env_true_values(self) -> None:
        periods, data, _delays, _dates = cutting_scenario()
        env = CuttingEnvironment(periods, data)
        domain = CuttingDomain()
        state = domain.initial_state()
        for length in ("800", "800", "700"):
            op = ActionOp("CUT_A" if length != "700" else "CUT_B", {"length_mm": dec(length)})
            env.execute("P01", op, {"kerf_mm": dec("12")})
        observation, delay = env.finalize("P01")
        self.assertEqual(delay, 0)
        self.assertEqual(observation.values[REMAIN], q("104"))
        self.assertEqual(observation.values[YIELD], q("94.26"))

    def test_cutting_document_board_length_fault(self) -> None:
        periods, data, _delays, _dates = cutting_scenario()
        env = CuttingEnvironment(periods, data)
        bundle = env.extract_facts("P03", date(2026, 1, 17))
        self.assertEqual(bundle.facts["board_length_mm"], dec("2440"))
        env.execute("P03", ActionOp("CUT_A", {"length_mm": dec("800")}), {"kerf_mm": dec("12")})
        self.assertEqual(env.truth("P03")[REMAIN], q("1588"))  # 真实板长 2400：2400-800-12
        env.execute("P03", ActionOp("CUT_A", {"length_mm": dec("800")}), {"kerf_mm": dec("12")})
        env.execute("P03", ActionOp("CUT_B", {"length_mm": dec("700")}), {"kerf_mm": dec("12")})
        self.assertEqual(env.truth("P03")[REMAIN], q("64"))  # 真实板长 2400
        self.assertEqual(env.truth("P03")[YIELD], q("95.83"))


if __name__ == "__main__":
    unittest.main()
