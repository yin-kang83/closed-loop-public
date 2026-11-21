"""端到端闭环测试：两域闭环 + 故障注入 + 审计/回放 + 内核域无关性。"""

from __future__ import annotations

import unittest
from datetime import date
from pathlib import Path

import loop_mvp
from loop_mvp.cutting import CuttingDomain, CuttingEnvironment
from loop_mvp.cutting import default_scenario as cutting_scenario
from loop_mvp.finance import (
    A_CAP,
    A_INCTAX,
    ALL_ACCOUNTS,
    FinanceDomain,
    FinanceEnvironment,
)
from loop_mvp.finance import default_scenario as finance_scenario
from loop_mvp.loop import ClosedLoop, LoopConfig
from loop_mvp.types import q


def build_finance(domain=None, delays=None):
    periods, data, default_delays, dates = finance_scenario()
    env = FinanceEnvironment(periods, data, default_delays if delays is None else delays)
    loop = ClosedLoop(domain or FinanceDomain(), env,
                      LoopConfig(periods=periods, business_dates=dates))
    return loop, loop.run()


def build_cutting(domain=None, delays=None):
    periods, data, default_delays, dates = cutting_scenario()
    env = CuttingEnvironment(periods, data, default_delays if delays is None else delays)
    loop = ClosedLoop(domain or CuttingDomain(), env,
                      LoopConfig(periods=periods, business_dates=dates))
    return loop, loop.run()


class TestFinanceLoop(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.loop, cls.report = build_finance()

    def test_all_periods_closed(self) -> None:
        self.assertEqual(self.report.metrics["closed_periods"], 6)
        self.assertEqual(self.report.metrics["periods"], 6)
        self.assertEqual(self.report.metrics["executed_actions"], 30)

    def test_ambiguity_review_and_external_confirmation(self) -> None:
        self.assertEqual(self.report.metrics["review_events"], 1)
        self.assertEqual(self.report.metrics["confirmations"], 1)
        self.assertEqual(self.report.metrics["escalations"], 0)
        p04 = [p for p in self.report.periods if p.period == "P04"][0]
        self.assertEqual(p04.hypotheses_before, 2)
        self.assertEqual(p04.hypotheses_after, 1)
        self.assertTrue(any("外部确认" in note for note in p04.notes))

    def test_extraction_fault_flagged_then_corrected(self) -> None:
        self.assertEqual(self.report.metrics["residual_flagged"], 1)
        p03 = [p for p in self.report.periods if p.period == "P03"][0]
        self.assertIsNotNone(p03.residual)
        self.assertFalse(p03.residual.in_bound)
        self.assertEqual(self.report.metrics["model_errors"], 1)
        self.assertEqual(self.report.metrics["corrections_applied"], 1)
        self.assertEqual(self.report.metrics["corrections_rejected"], 0)
        self.assertEqual(self.report.metrics["unexplained_in_bound"], 0)
        self.assertTrue(any("红字冲销" in note for note in p03.notes))
        actions = [entry.action_name for entry in self.loop.ledger.journal if entry.kind == "ACTION"]
        self.assertIn("REVERSAL", actions)

    def test_delayed_feedback_settled(self) -> None:
        self.assertEqual(self.report.metrics["delayed_scheduled"], 1)
        self.assertEqual(self.report.metrics["delayed_settlements"], 1)
        self.assertEqual(self.report.metrics["delayed_unreleased"], 0)
        p05 = [p for p in self.report.periods if p.period == "P05"][0]
        self.assertIsNotNone(p05.residual)  # 在 P06 被结算回填

    def test_experience_reuse_and_staleness(self) -> None:
        self.assertEqual(self.report.metrics["replans"], 3)
        self.assertEqual(self.report.metrics["reuse_valid"], 3)
        reasons = self.report.metrics["reuse_invalid_reasons"]
        self.assertGreaterEqual(reasons.get("RULE_VERSION_CHANGED", 0), 1)

    def test_no_false_accept_and_auditable(self) -> None:
        self.assertEqual(self.report.metrics["false_accept"], 0)
        self.assertEqual(self.report.metrics["invariant_breaches"], 0)
        self.assertTrue(self.report.metrics["audit_ok"])
        self.assertTrue(self.report.metrics["replay_ok"])
        self.assertTrue(self.report.metrics["ledger_facts_ok"])
        self.assertTrue(self.report.metrics["ledger_journal_ok"])


class TestCuttingLoop(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.loop, cls.report = build_cutting()

    def test_all_boards_closed(self) -> None:
        self.assertEqual(self.report.metrics["closed_periods"], 3)
        self.assertEqual(self.report.metrics["executed_actions"], 12)

    def test_observation_shrinks_kerf_hypotheses(self) -> None:
        self.assertEqual(self.report.metrics["hypothesis_shrinks"], 2)
        p01 = [p for p in self.report.periods if p.period == "P01"][0]
        p02 = [p for p in self.report.periods if p.period == "P02"][0]
        self.assertEqual(p01.hypotheses_before, 2)
        self.assertEqual(p01.hypotheses_after, 1)
        self.assertEqual(p02.hypotheses_before, 2)
        self.assertEqual(p02.hypotheses_after, 1)

    def test_board_length_fault_detected(self) -> None:
        self.assertEqual(self.report.metrics["residual_flagged"], 1)
        p03 = [p for p in self.report.periods if p.period == "P03"][0]
        self.assertIsNotNone(p03.residual)
        self.assertFalse(p03.residual.in_bound)
        self.assertTrue(any("越界" in note for note in p03.notes))

    def test_reuse_and_auditable(self) -> None:
        self.assertEqual(self.report.metrics["reuse_valid"], 2)
        self.assertEqual(self.report.metrics["replans"], 1)
        self.assertTrue(self.report.metrics["audit_ok"])
        self.assertTrue(self.report.metrics["replay_ok"])
        self.assertEqual(self.report.metrics["false_accept"], 0)

    def test_state_corrected_by_observation(self) -> None:
        # 观测把"账面推演状态"校正为实测状态（执行账里有 OBS 记录）
        kinds = [entry.kind for entry in self.loop.ledger.journal]
        self.assertIn("OBS", kinds)


class TestFaultInjection(unittest.TestCase):
    def test_buggy_operator_is_blocked_never_executed(self) -> None:
        class BuggyTax(FinanceDomain):
            def transition(self, state, action, params):
                if action.name == "TAX":
                    values = {key: state.get(key) for key in ALL_ACCOUNTS}
                    amount = action.decimal("amount")
                    values[A_INCTAX] += amount
                    values[A_CAP] += amount  # 贷记错科目
                    return state.with_values({k: q(v) for k, v in values.items()})
                return super().transition(state, action, params)

        _loop, report = build_finance(domain=BuggyTax())
        self.assertGreaterEqual(report.metrics["block"], 1)
        self.assertEqual(report.metrics["closed_periods"], 0)
        self.assertEqual(report.metrics["false_accept"], 0)
        self.assertEqual(report.metrics["invariant_breaches"], 0)
        blocked_steps = [
            step for period in report.periods for step in period.steps if step.verdict == "BLOCK"
        ]
        self.assertTrue(blocked_steps)
        self.assertIn("TAX_MISMATCH", blocked_steps[0].codes)

    def test_rule_rejection_blocks_period(self) -> None:
        periods, data, delays, dates = finance_scenario()
        old_dates = {period: date(2025, 12, 31) for period in periods}
        env = FinanceEnvironment(periods, data, delays)
        loop = ClosedLoop(FinanceDomain(), env,
                          LoopConfig(periods=periods, business_dates=old_dates))
        report = loop.run()
        self.assertEqual(report.metrics["closed_periods"], 0)
        self.assertTrue(all(period.blocked for period in report.periods))
        self.assertTrue(any("规则解析拒绝" in note for period in report.periods for note in period.notes))

    def test_delayed_beyond_window(self) -> None:
        periods, data, delays, dates = finance_scenario()
        late = dict(delays)
        late["P06"] = 2
        env = FinanceEnvironment(periods, data, late)
        loop = ClosedLoop(FinanceDomain(), env,
                          LoopConfig(periods=periods, business_dates=dates))
        report = loop.run()
        self.assertEqual(report.metrics["delayed_unreleased"], 1)

    def test_audit_tamper_detected(self) -> None:
        loop, _report = build_cutting()
        ok, _bad = loop.audit.verify()
        self.assertTrue(ok)
        loop.audit.records[2]["payload"]["event"] = "forged"
        ok, bad = loop.audit.verify()
        self.assertFalse(ok)
        self.assertEqual(bad, 2)


class TestCoreIsDomainAgnostic(unittest.TestCase):
    CORE_MODULES = (
        "types.py", "audit.py", "ledger.py", "rules.py", "estimator.py",
        "gate.py", "memory.py", "loop.py", "domain.py",
    )

    def test_core_has_no_domain_knowledge(self) -> None:
        root = Path(loop_mvp.__file__).resolve().parent
        for name in self.CORE_MODULES:
            source = (root / name).read_text(encoding="utf-8").lower()
            self.assertNotIn("finance", source, f"{name} 不应含财务域知识")
            self.assertNotIn("cutting", source, f"{name} 不应含下料域知识")
            self.assertNotIn("from .finance", source)
            self.assertNotIn("from .cutting", source)


if __name__ == "__main__":
    unittest.main()
