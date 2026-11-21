"""MPC 层测试：字典序、合规硬约束、代价顺序换决策、闭环集成。"""

from __future__ import annotations

import unittest
from datetime import date
from typing import Dict, Mapping

from loop_mvp.cutting import CuttingDomain, CuttingEnvironment, mpc_scenario
from loop_mvp.loop import ClosedLoop, LoopConfig
from loop_mvp.mpc import MpcConfig, MpcPlanner
from loop_mvp.types import Decimal, Plan, ZERO, dec, q


def _plan(label: str) -> Plan:
    return Plan(period="P01", goal=label, actions=(), skeleton=(label,),
                rule_hashes={}, fact_keys=())


class TestMpcPlanner(unittest.TestCase):
    def test_lexicographic_order_changes_choice(self) -> None:
        plans = {"A": _plan("A"), "B": _plan("B")}
        scores = {
            "A": {"compliance": ZERO, "fulfillment": dec("1"), "waste": dec("10")},
            "B": {"compliance": ZERO, "fulfillment": ZERO, "waste": dec("20")},
        }
        candidates = [(label, plan) for label, plan in plans.items()]
        scorer = lambda plan: scores[plan.skeleton[0]]  # noqa: E731

        fulfill_first = MpcPlanner(MpcConfig(objectives=("compliance", "fulfillment", "waste"))).select(candidates, scorer)
        waste_first = MpcPlanner(MpcConfig(objectives=("compliance", "waste", "fulfillment"))).select(candidates, scorer)
        self.assertEqual(fulfill_first.chosen.label, "B")
        self.assertEqual(waste_first.chosen.label, "A")

    def test_compliance_is_hard_constraint(self) -> None:
        plans = {"good": _plan("good"), "cheat": _plan("cheat")}
        scores = {
            "good": {"compliance": ZERO, "fulfillment": dec("3")},
            "cheat": {"compliance": dec("1"), "fulfillment": ZERO},  # 其他指标最好但违规
        }
        selection = MpcPlanner(MpcConfig(objectives=("fulfillment",))).select(
            list(plans.items()), lambda plan: scores[plan.skeleton[0]])
        self.assertEqual(selection.chosen.label, "good")
        self.assertEqual(selection.objectives[0], "compliance")  # 即使配置没写也强制第一

    def test_empty_candidates_raises(self) -> None:
        with self.assertRaises(ValueError):
            MpcPlanner().select([], lambda plan: {})

    def test_selection_summary(self) -> None:
        plan = _plan("A")
        selection = MpcPlanner().select([("A", plan)], lambda _p: {"compliance": ZERO})
        self.assertIn("chosen=A", selection.summary())


class TestCuttingMpcOrderSensitivity(unittest.TestCase):
    def _candidates_and_scorer(self):
        domain = CuttingDomain()
        hypotheses = domain.resolve_hypotheses(date(2026, 2, 11), None)
        facts = {
            "board_length_mm": dec("2440"),
            "board_length_alt_mm": dec("3000"),
            "demand_A": dec("2"), "demand_B": dec("1"), "demand_C": dec("1"),
            "length_A": dec("800"), "length_B": dec("700"), "length_C": dec("500"),
        }
        state = domain.initial_state()
        candidates = list(enumerate(domain.candidate_plans(state, facts, hypotheses, date(2026, 2, 11))))
        labeled = [(f"greedy:{index}", plan) for index, plan in candidates]
        scorer = lambda plan: domain.score_plan(plan, state, facts, hypotheses)  # noqa: E731
        return labeled, scorer, domain

    def test_fulfillment_first_picks_3000(self) -> None:
        candidates, scorer, domain = self._candidates_and_scorer()
        selection = MpcPlanner(MpcConfig(objectives=("compliance", "fulfillment", "waste"))).select(candidates, scorer)
        board = selection.chosen.plan.actions[0].variants["*"].decimal("board_length_mm")
        self.assertEqual(board, q("3000"))
        self.assertEqual(selection.chosen.scores["fulfillment"], ZERO)
        self.assertEqual(selection.chosen.scores["compliance"], ZERO)

    def test_waste_first_picks_2440(self) -> None:
        candidates, scorer, domain = self._candidates_and_scorer()
        selection = MpcPlanner(MpcConfig(objectives=("compliance", "waste", "fulfillment"))).select(candidates, scorer)
        board = selection.chosen.plan.actions[0].variants["*"].decimal("board_length_mm")
        self.assertEqual(board, q("2440"))
        self.assertGreater(selection.chosen.scores["fulfillment"], ZERO)


class TestCuttingMpcLoop(unittest.TestCase):
    def _run(self, objectives):
        periods, data, delays, dates = mpc_scenario()
        env = CuttingEnvironment(periods, data, delays)
        loop = ClosedLoop(CuttingDomain(), env, LoopConfig(
            periods=periods, business_dates=dates, mpc_objectives=objectives))
        return loop, loop.run()

    def test_order_changes_decisions_in_loop(self) -> None:
        _loop_a, fulfill = self._run(("compliance", "fulfillment", "waste"))
        _loop_b, material = self._run(("compliance", "waste", "fulfillment"))
        self.assertNotEqual(fulfill.metrics["mpc_choices"]["P02"],
                            material.metrics["mpc_choices"]["P02"])
        self.assertEqual(fulfill.metrics["mpc_choices"]["P02"], "greedy:1")   # 3000 板
        self.assertEqual(material.metrics["mpc_choices"]["P02"], "greedy:0")  # 2440 板
        for report in (fulfill, material):
            self.assertEqual(report.metrics["closed_periods"], 3)
            self.assertEqual(report.metrics["coverage"], 1.0)
            self.assertEqual(report.metrics["false_accept"], 0)
            self.assertTrue(report.metrics["audit_ok"])
            self.assertTrue(report.metrics["replay_ok"])
            self.assertEqual(report.metrics["mpc_objectives"][0], "compliance")
            self.assertGreaterEqual(report.metrics["mpc_considered"], 2)

    def test_greedy_mode_unchanged(self) -> None:
        periods, data, delays, dates = mpc_scenario()
        env = CuttingEnvironment(periods, data, delays)
        loop = ClosedLoop(CuttingDomain(), env, LoopConfig(periods=periods, business_dates=dates))
        report = loop.run()
        self.assertEqual(report.metrics["mpc_objectives"], [])
        self.assertEqual(report.metrics["mpc_choices"], {})
        self.assertEqual(report.metrics["closed_periods"], 3)


if __name__ == "__main__":
    unittest.main()
