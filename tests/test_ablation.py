"""对照/消融实验测试（合成场景；全部数字来自 tools/run_ablation.py 的同源实现）。"""

from __future__ import annotations

import unittest

from tools.run_ablation import (
    run_cutting_variant,
    run_finance_variant,
)


class TestAblationFinanceB(unittest.TestCase):
    """场景 B（事后补充，exploratory）：P04 真实税率 25%，用于区分闸门/假设集。"""

    def test_full_intercepts_ambiguity(self) -> None:
        item = run_finance_variant("full", "finance_close_b")
        self.assertEqual(item["wrong"]["wrong_on_P04"], 0)
        self.assertTrue(any(event.get("codes") == ["EXTERNAL_CONFIRM"]
                            for event in item["gate_events"]))
        self.assertEqual(item["metrics"]["false_accept"], 0)
        self.assertTrue(item["metrics"]["audit_ok"])
        self.assertTrue(item["metrics"]["replay_ok"])

    def test_no_gate_executes_wrong_rate(self) -> None:
        item = run_finance_variant("no_gate", "finance_close_b")
        self.assertGreaterEqual(item["wrong"]["wrong_on_P04"], 1)
        self.assertFalse(any(event.get("codes") == ["EXTERNAL_CONFIRM"]
                             for event in item["gate_events"]))

    def test_no_hypothesis_executes_wrong_rate(self) -> None:
        item = run_finance_variant("no_hypothesis", "finance_close_b")
        self.assertGreaterEqual(item["wrong"]["wrong_on_P04"], 1)

    def test_pure_rules_no_detection(self) -> None:
        item = run_finance_variant("pure_rules", "finance_close")
        self.assertGreater(item["wrong"]["unremediated_wrong_executions"], 0)
        self.assertEqual(item["detection"].get("F1_P03_抽取错误"), "未发现")


class TestAblationFinanceA(unittest.TestCase):
    def test_full_corrects_extraction_error(self) -> None:
        item = run_finance_variant("full", "finance_close")
        self.assertEqual(item["wrong"]["unremediated_wrong_executions"], 0)
        self.assertEqual(item["detection"].get("F1_P03_抽取错误"), "事后修正")

    def test_no_memory_replans_more(self) -> None:
        full = run_finance_variant("full", "finance_close")
        no_memory = run_finance_variant("no_memory", "finance_close")
        self.assertGreater(no_memory["metrics"]["replans"], full["metrics"]["replans"])
        self.assertEqual(no_memory["metrics"]["reuse_valid"], 0)


class TestAblationCutting(unittest.TestCase):
    def test_full_detects_board_error(self) -> None:
        item = run_cutting_variant("full")
        self.assertIn(item["detection"].get("F4_P03_板长错"), ("拦截", "事后修正", "事后告警"))

    def test_pure_rules_misses_board_error(self) -> None:
        item = run_cutting_variant("pure_rules")
        self.assertEqual(item["detection"].get("F4_P03_板长错"), "未发现")


if __name__ == "__main__":
    unittest.main()
