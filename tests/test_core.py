"""内核单元测试：契约 / 审计链 / 双时态账本 / 规则 / 估计层 / 经验库。"""

from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal

from loop_mvp.audit import HashChain
from loop_mvp.estimator import (
    ConformalCalibrator,
    HypothesisSet,
    HypothesisSieve,
    ResidualAnalyzer,
)
from loop_mvp.ledger import TemporalLedger
from loop_mvp.memory import CaseMemory
from loop_mvp.rules import RuleBook, RuleVersion
from loop_mvp.types import (
    ActionOp,
    FactBundle,
    Interval,
    Observation,
    Plan,
    Prediction,
    ResidualReport,
    StateSnapshot,
    ZERO,
    canonical_json,
    dec,
    fingerprint,
    q,
)


class TestContracts(unittest.TestCase):
    def test_decimal_quantize(self) -> None:
        self.assertEqual(q("1.005"), Decimal("1.01"))
        self.assertEqual(q(Decimal("2.344")), Decimal("2.34"))

    def test_interval_basic(self) -> None:
        interval = Interval(dec("10"), dec("20"))
        self.assertTrue(interval.contains(dec("15")))
        self.assertFalse(interval.contains(dec("21")))
        self.assertEqual(interval.expand(dec("5")).lo, dec("5"))
        self.assertEqual(Interval.from_values([dec("3"), dec("1")]).hi, dec("3"))
        with self.assertRaises(ValueError):
            Interval(dec("2"), dec("1"))

    def test_canonical_json_is_deterministic(self) -> None:
        first = canonical_json({"b": Decimal("1.50"), "a": [1, 2]})
        second = canonical_json({"a": [1, 2], "b": Decimal("1.50")})
        self.assertEqual(first, second)
        self.assertEqual(first, canonical_json({"a": [1, 2], "b": Decimal("1.5")}))

    def test_action_fingerprint_stable(self) -> None:
        a = ActionOp("TAX", {"amount": dec("125"), "rate": dec("0.25")})
        b = ActionOp("TAX", {"rate": dec("0.25"), "amount": dec("125")})
        self.assertEqual(a.fingerprint(), b.fingerprint())

    def test_state_snapshot_quantizes(self) -> None:
        state = StateSnapshot("P01", {"x": Decimal("1.239")})
        self.assertEqual(state.get("x"), Decimal("1.24"))
        self.assertEqual(state.get("missing"), ZERO)


class TestAuditChain(unittest.TestCase):
    def test_append_and_verify(self) -> None:
        chain = HashChain("t")
        for i in range(3):
            chain.append({"i": i})
        ok, bad = chain.verify()
        self.assertTrue(ok)
        self.assertIsNone(bad)
        self.assertEqual(len(chain.records), 3)

    def test_tamper_detected_and_located(self) -> None:
        chain = HashChain("t")
        for i in range(3):
            chain.append({"value": i})
        chain.records[1]["payload"]["value"] = 999
        ok, bad = chain.verify()
        self.assertFalse(ok)
        self.assertEqual(bad, 1)


class TestTemporalLedger(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger = TemporalLedger()
        self.bd = date(2026, 1, 31)

    def test_record_and_resolve(self) -> None:
        bundle = FactBundle("P01", self.bd, {"revenue": dec("1000")})
        self.ledger.record_facts(bundle, "P01")
        record = self.ledger.resolve_fact("revenue", self.bd, "P01")
        self.assertIsNotNone(record)
        self.assertEqual(record.value, q("1000"))

    def test_supersede_same_period(self) -> None:
        self.ledger.record_facts(FactBundle("P01", self.bd, {"revenue": dec("1000")}), "P01")
        self.ledger.record_facts(FactBundle("P01", self.bd, {"revenue": dec("1100")}), "P01")
        record = self.ledger.resolve_fact("revenue", self.bd, "P01")
        self.assertEqual(record.value, q("1100"))
        # 旧记录仍在，但被 supersedes 排除
        self.assertEqual(len(self.ledger.facts), 2)

    def test_idempotent_record(self) -> None:
        bundle = FactBundle("P01", self.bd, {"revenue": dec("1000")})
        self.ledger.record_facts(bundle, "P01")
        self.ledger.record_facts(bundle, "P01")
        self.assertEqual(len(self.ledger.facts), 1)

    def test_knowledge_at_filters(self) -> None:
        self.ledger.record_facts(FactBundle("P01", self.bd, {"revenue": dec("1000")}), "P01")
        self.assertIsNone(self.ledger.resolve_fact("revenue", self.bd, "P00"))
        self.assertIsNotNone(self.ledger.resolve_fact("revenue", self.bd, "P02"))

    def test_fact_chain_verify(self) -> None:
        self.ledger.record_facts(FactBundle("P01", self.bd, {"revenue": dec("1000")}), "P01")
        self.ledger.record_facts(FactBundle("P02", date(2026, 2, 28), {"revenue": dec("1100")}), "P02")
        ok, bad = self.ledger.verify_facts()
        self.assertTrue(ok)
        self.assertIsNone(bad)
        self.ledger.facts[0] = self.ledger.facts[0].__class__(**{
            **self.ledger.facts[0].__dict__, "value": dec("9999")})
        ok, bad = self.ledger.verify_facts()
        self.assertFalse(ok)
        self.assertEqual(bad, 0)


class TestRules(unittest.TestCase):
    def test_applicable_and_hashes(self) -> None:
        book = RuleBook((
            RuleVersion("R", "V1", {"rate": dec("0.25")}, date(2026, 1, 1), date(2026, 3, 31)),
            RuleVersion("R", "V2", {"rate": dec("0.20")}, date(2026, 4, 1), None),
        ))
        self.assertEqual(len(book.applicable("R", date(2026, 2, 1))), 1)
        self.assertEqual(len(book.applicable("R", date(2026, 5, 1))), 1)
        self.assertNotEqual(book.hashes()["R"], RuleBook(book.versions[1:]).hashes().get("R", ""))
        with self.assertRaises(LookupError):
            book.resolve_one("R", date(2025, 12, 1))

    def test_scope_ambiguity(self) -> None:
        book = RuleBook((
            RuleVersion("R", "A", {"rate": dec("0.25")}, date(2026, 4, 1), None, "GENERAL"),
            RuleVersion("R", "B", {"rate": dec("0.20")}, date(2026, 4, 1), None, "SMALL"),
        ))
        self.assertEqual(len(book.applicable("R", date(2026, 4, 2), entity_scope=None)), 2)
        self.assertEqual(len(book.applicable("R", date(2026, 4, 2), entity_scope="SMALL")), 1)
        with self.assertRaises(LookupError):
            book.resolve_one("R", date(2026, 4, 2), entity_scope=None)


class TestEstimator(unittest.TestCase):
    def _prediction(self) -> Prediction:
        return Prediction(
            period="P01",
            step=0,
            step_name="X",
            scenarios={
                "h1": StateSnapshot("P01", {"k": dec("10")}),
                "h2": StateSnapshot("P01", {"k": dec("12")}),
            },
            intervals={"k": Interval(dec("10"), dec("12"))},
            variant_fingerprints={"h1": "a", "h2": "b"},
        )

    def test_residual_in_and_out(self) -> None:
        analyzer = ResidualAnalyzer()
        report = analyzer.analyze(self._prediction(),
                                  Observation("P01", {"k": dec("11")}, "src", "P01"))
        self.assertTrue(report.in_bound)
        self.assertEqual(report.max_abs_delta(), ZERO)

        report = analyzer.analyze(self._prediction(),
                                  Observation("P01", {"k": dec("15")}, "src", "P01"))
        self.assertFalse(report.in_bound)
        self.assertEqual(report.out_of_bound[0].delta, dec("3"))
        self.assertEqual(report.nonconformity(), dec("3"))

    def test_calibrator_monotone(self) -> None:
        calibrator = ConformalCalibrator()
        analyzer = ResidualAnalyzer()
        calibrator.update(analyzer.analyze(self._prediction(),
                                           Observation("P01", {"k": dec("15")}, "src", "P01")))
        self.assertEqual(calibrator.bound("k"), dec("3"))
        calibrator.update(analyzer.analyze(self._prediction(),
                                           Observation("P01", {"k": dec("13")}, "src", "P01")))
        self.assertEqual(calibrator.bound("k"), dec("3"))  # 只增不减（保守）

    def test_sieve(self) -> None:
        sieve = HypothesisSieve()
        kept, matched = sieve.sieve(
            self._prediction(),
            Observation("P01", {"k": dec("12")}, "src", "P01"),
            {"k": dec("0.01")},
        )
        self.assertTrue(matched)
        self.assertEqual(kept, ("h2",))

    def test_hypothesis_set_subset_empty_raises(self) -> None:
        from loop_mvp.estimator import Hypothesis
        hypotheses = HypothesisSet((
            Hypothesis("h1", {"rate": dec("0.25")}, "R@V1"),
            Hypothesis("h2", {"rate": dec("0.20")}, "R@V2"),
        ))
        self.assertEqual(len(hypotheses.subset(["h1"]).hypotheses), 1)
        with self.assertRaises(ValueError):
            hypotheses.subset(["nope"])


class TestCaseMemory(unittest.TestCase):
    def _plan(self) -> Plan:
        return Plan(
            period="P01",
            goal="G",
            actions=(),
            skeleton=("A", "B"),
            rule_hashes={"rule": "h1"},
            fact_keys=("x",),
        )

    def test_save_dedupe_validate(self) -> None:
        memory = CaseMemory("d")
        first = memory.save(self._plan(), "P01", "closed", {"input_hash": "abc"})
        second = memory.save(self._plan(), "P02", "closed", {})
        self.assertEqual(first.case_id, second.case_id)
        self.assertEqual(memory.deduped, 1)

        same = memory.validate(first, "G", {"rule": "h1"}, {"x": dec("1")}, "abc")
        self.assertTrue(same.valid)
        self.assertEqual(same.mode, "EXACT")

        template = memory.validate(first, "G", {"rule": "h1"}, {"x": dec("1")}, "zzz")
        self.assertTrue(template.valid)
        self.assertEqual(template.mode, "TEMPLATE")

        stale = memory.validate(first, "G", {"rule": "h2"}, {"x": dec("1")})
        self.assertFalse(stale.valid)
        self.assertIn("RULE_VERSION_CHANGED:rule", stale.reasons)

        missing = memory.validate(first, "G", {"rule": "h1"}, {})
        self.assertFalse(missing.valid)
        self.assertIn("MISSING_FACT:x", missing.reasons)

        no_case = memory.validate(None, "G", {"rule": "h1"}, {"x": dec("1")})
        self.assertFalse(no_case.valid)
        self.assertEqual(no_case.reasons, ("NO_CASE",))


if __name__ == "__main__":
    unittest.main()
