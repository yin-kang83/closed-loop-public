"""对接层测试：判定映射 / 判断-签字结构 / 审计包确定性 / 篡改检测 / shadow 事件。"""

from __future__ import annotations

import unittest

from demo import run_finance
from loop_mvp.interop import (
    MATH_GATE_TIERS,
    build_audit_pack,
    shadow_events,
    to_fin_agent_judgment,
    to_math_gate,
    verify_audit_pack,
)
from loop_mvp.types import GateDecision


class TestVerdictMapping(unittest.TestCase):
    def test_mapping_table(self) -> None:
        expected = {
            "ALLOW": ("PASS", "L1_EXACT", "ALLOW", 0, False),
            "REVIEW": ("UNKNOWN", "L3_UNKNOWN", "REVIEW", 3, True),
            "BLOCK": ("FAIL_EQUIVALENCE", "R_REFUTED", "BLOCK", 2, True),
        }
        for verdict, values in expected.items():
            item = to_math_gate(verdict)
            self.assertEqual((item["verdict"], item["evidence"], item["decision"],
                              item["exit_code"], item["human_review_required"]), values)

    def test_gate_decision_codes_preserved(self) -> None:
        item = to_math_gate(GateDecision("REVIEW", ("AMBIGUOUS_ACTION",), "d", 2))
        self.assertEqual(item["codes"], ["AMBIGUOUS_ACTION"])
        self.assertEqual(item["source_verdict"], "REVIEW")

    def test_unknown_verdict_rejected(self) -> None:
        with self.assertRaises(ValueError):
            to_math_gate("MAYBE")


class TestJudgmentRecord(unittest.TestCase):
    def test_review_requires_signoff(self) -> None:
        record = to_fin_agent_judgment(
            GateDecision("REVIEW", ("AMBIGUOUS_ACTION",), "d", 2),
            period="P01", proposal={"action": "SETTLE_TAX"}, evidence={"source": "gate"})
        self.assertTrue(record["signoff"]["required"])
        self.assertEqual(record["signoff"]["status"], "PENDING")
        self.assertIsNone(record["signoff"]["signed_by"])
        self.assertTrue(record["review"]["human_review_required"])

    def test_allow_with_policy_still_requires_signoff(self) -> None:
        record = to_fin_agent_judgment(
            GateDecision("ALLOW", (), "ok", 1), period="P01",
            proposal={"action": "REVENUE"}, evidence={})
        self.assertTrue(record["signoff"]["required"])

    def test_allow_without_policy_no_signoff(self) -> None:
        record = to_fin_agent_judgment(
            GateDecision("ALLOW", (), "ok", 1), period="P01",
            proposal={"action": "REVENUE"}, evidence={},
            policy_requires_signoff=False)
        self.assertFalse(record["signoff"]["required"])


class TestAuditPack(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.finance = run_finance()

    def test_pack_verifies_and_is_deterministic(self) -> None:
        pack_a = build_audit_pack(self.finance, generated_at="2026-11-21T00:00:00")
        pack_b = build_audit_pack(self.finance, generated_at="2026-11-21T00:00:00")
        self.assertTrue(verify_audit_pack(pack_a))
        self.assertEqual(pack_a["content_hash"], pack_b["content_hash"])
        self.assertEqual(pack_a["philosophy"]["no_tax_planning"], True)

    def test_tamper_detected(self) -> None:
        pack = build_audit_pack(self.finance)
        pack["metrics"]["false_accept"] = 999
        self.assertFalse(verify_audit_pack(pack))

    def test_audit_pack_structure(self) -> None:
        pack = build_audit_pack(self.finance)
        self.assertEqual(pack["schema"], "closed-loop-audit-pack/v1")
        self.assertTrue(pack["episodes"])
        step = pack["episodes"][0]["steps"][0]
        self.assertIn("math_gate", step)
        self.assertIn("judgment", step)


class TestShadow(unittest.TestCase):
    def test_shadow_events_never_execute(self) -> None:
        events = shadow_events(run_finance())
        self.assertTrue(events)
        self.assertTrue(all(event["executed"] is False for event in events))
        self.assertTrue(all(event["mode"] == "shadow" for event in events))


if __name__ == "__main__":
    unittest.main()
