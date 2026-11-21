"""闭环内核（域无关）：环境交互 → 闭眼推演 → 睁眼反馈 → 残差收敛 → 经验固化。

一个账期/一张料单 = 一个 episode：

1. 先结算迟到观测（上一期"睁眼"的结果，可能延迟到达）；
2. 抽取事实 → 双时态入账 → 规则版本解析出假设集（歧义不猜）；
3. 检索经验 case → 校验（规则换版/缺键 → 失效）→ 模板实例化，否则重新规划；
4. 逐步执行：闭眼推演（全假设场景）→ 安全过滤 → ALLOW 才落账/落料；
   REVIEW 时等外部确认收缩假设集后重判，BLOCK 直接拒绝；
5. 期末"睁眼"：回执 → 残差分解 → 越界才重校准；用观测筛假设；
   物理量域用实测修正状态（apply_observation），账务域只做对照不改账；
6. 完整跑通 → 经验固化（骨架 + 规则哈希 + 所需事实键 + 输入指纹，去重）。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .audit import HashChain
from .domain import Domain, Environment
from .estimator import (
    ConformalCalibrator,
    CoverageTracker,
    HypothesisSet,
    HypothesisSieve,
    ResidualAnalyzer,
)
from .gate import SafetyGate
from .ledger import TemporalLedger
from .memory import CaseMemory
from .mpc import MpcConfig, MpcPlanner
from .types import (
    GateDecision,
    Interval,
    Observation,
    Plan,
    PlannedAction,
    Prediction,
    ResidualReport,
    StateSnapshot,
    fingerprint,
)


@dataclass
class LoopConfig:
    periods: Tuple[str, ...]
    business_dates: Mapping[str, date]
    entity: Optional[str] = "ENTITY-A"
    max_review_resolutions: int = 1
    # MPC：给出目标顺序即启用（合规永远强制第一）；None = 贪心规划
    mpc_objectives: Optional[Tuple[str, ...]] = None
    mpc_horizon: int = 1


@dataclass
class StepRecord:
    step: int
    action: str
    verdict: str
    codes: Tuple[str, ...]
    executed: bool
    hyp_id: str = ""


@dataclass
class PeriodResult:
    period: str
    hypotheses_before: int = 0
    hypotheses_after: int = 0
    reuse_mode: str = ""
    reuse_reasons: Tuple[str, ...] = ()
    steps: List[StepRecord] = field(default_factory=list)
    escalated: bool = False
    blocked: bool = False
    closed: bool = False
    observation: Optional[Observation] = None
    residual: Optional[ResidualReport] = None
    calibration_changes: Tuple[str, ...] = ()
    sieve_kept: Tuple[str, ...] = ()
    notes: List[str] = field(default_factory=list)
    delayed_settled: List[str] = field(default_factory=list)

    def summary(self) -> str:
        residual = "n/a" if self.residual is None else ("in" if self.residual.in_bound else "OUT")
        return (
            f"{self.period}: mode={self.reuse_mode} hyps={self.hypotheses_before}->{self.hypotheses_after} "
            f"steps={len(self.steps)} closed={self.closed} escalated={self.escalated} "
            f"blocked={self.blocked} residual={residual}"
        )


@dataclass
class LoopReport:
    domain: str
    periods: List[PeriodResult]
    metrics: Dict[str, Any]
    observations: List[Observation] = field(default_factory=list)

    def summary(self) -> str:
        lines = [f"[{self.domain}] episodes={len(self.periods)}"]
        lines.extend("  " + p.summary() for p in self.periods)
        for key in sorted(self.metrics):
            lines.append(f"  {key}: {self.metrics[key]}")
        return "\n".join(lines)


class ClosedLoop:
    def __init__(
        self,
        domain: Domain,
        env: Environment,
        config: LoopConfig,
        audit: Optional[HashChain] = None,
    ) -> None:
        self.domain = domain
        self.env = env
        self.config = config
        self.ledger = TemporalLedger()
        self.gate = SafetyGate(domain)
        self.analyzer = ResidualAnalyzer()
        self.sieve = HypothesisSieve()
        self.calibrator = ConformalCalibrator()
        self.coverage = CoverageTracker()
        self.memory = CaseMemory(domain.name)
        self.audit = audit or HashChain(name=f"loop-{domain.name}")
        self.mpc: Optional[MpcPlanner] = (
            MpcPlanner(MpcConfig(objectives=tuple(config.mpc_objectives),
                                 horizon=config.mpc_horizon))
            if config.mpc_objectives else None
        )

        self.state: StateSnapshot = domain.initial_state()
        self.results: Dict[str, PeriodResult] = {}
        self.predictions: Dict[str, Tuple[Prediction, HypothesisSet, Plan]] = {}
        self.residuals: List[ResidualReport] = []
        self.observations: List[Observation] = []
        self._pending_obs: List[Tuple[str, Observation]] = []

        # 指标
        self.allow_count = 0
        self.review_count = 0
        self.review_events = 0
        self.confirmations = 0
        self.block_count = 0
        self.escalations = 0
        self.replans = 0
        self.executed_count = 0
        self.hypothesis_shrinks = 0
        self.invariant_breaches = 0
        self.model_errors = 0
        self.unexplained_in_bound = 0
        self.corrections_applied = 0
        self.corrections_rejected = 0
        self.corrections_unavailable = 0
        self.delayed_settlements = 0
        self.delayed_scheduled = 0
        self.delayed_unreleased = 0
        self.reuse_attempts = 0
        self.reuse_valid = 0
        self.reuse_exact = 0
        self.mpc_considered = 0
        self.mpc_choices: Dict[str, str] = {}

    # ------------------------------------------------------------------
    # 编排
    # ------------------------------------------------------------------
    def run(self) -> LoopReport:
        self.env.reset()
        for period in self.config.periods:
            business_date = self.config.business_dates[period]
            result = PeriodResult(period=period)
            self.results[period] = result
            self.state = StateSnapshot(period=period, values=self.state.values)

            # 1) 结算迟到观测
            self._settle_due(period)
            if result.notes:
                self.audit.append({"event": "delayed_settle", "period": period,
                                  "notes": list(result.notes)})

            # 2) 事实入账（双时态）
            bundle = self.env.extract_facts(period, business_date)
            self.ledger.record_facts(bundle, recorded_at=period)
            facts = self.ledger.facts_at(business_date, period, self.domain.required_fact_keys)
            self.audit.append({"event": "facts", "period": period, "bundle": bundle.fingerprint()})

            # 3) 规则版本 → 假设集
            try:
                hypotheses = self.domain.resolve_hypotheses(business_date, self.config.entity)
            except LookupError as exc:
                result.blocked = True
                result.notes.append(f"规则解析拒绝: {exc}")
                self.audit.append({"event": "rule_rejected", "period": period, "reason": str(exc)})
                continue
            result.hypotheses_before = len(hypotheses.hypotheses)
            self.audit.append({"event": "hypotheses", "period": period,
                               "ids": list(hypotheses.ids),
                               "labels": [h.label for h in hypotheses.hypotheses]})

            # 4) 经验复用 / 重新规划
            plan = self._plan(period, business_date, facts, hypotheses, result)

            # 5) 逐步执行：推演 → 闸门 → 落账/落料
            predictions: Dict[int, Prediction] = {}
            working = hypotheses
            executed_all = True
            review_used = 0
            for idx, planned in enumerate(plan.actions):
                self.state = StateSnapshot(period=period, values=self.state.values)
                prediction = self._forecast(planned, idx, working)
                predictions[idx] = prediction
                decision = self.gate.admit(planned, prediction, working)
                self._record_decision(period, idx, planned, decision, working)

                if decision.verdict == "REVIEW" and review_used < self.config.max_review_resolutions:
                    review_used += 1
                    self.review_events += 1
                    confirmed = self.env.confirm(period, working)
                    if len(confirmed.hypotheses) < len(working.hypotheses):
                        result.notes.append(
                            f"step{idx} 外部确认: {len(working.hypotheses)} -> "
                            f"{len(confirmed.hypotheses)} 个假设"
                        )
                        self.hypothesis_shrinks += 1
                        self.confirmations += 1
                        working = confirmed
                        prediction = self._forecast(planned, idx, working)
                        predictions[idx] = prediction
                        decision = self.gate.admit(planned, prediction, working)
                        self._record_decision(period, idx, planned, decision, working)

                if decision.verdict == "REVIEW":
                    self.review_count += 1
                    self.escalations += 1
                    result.escalated = True
                    result.steps.append(StepRecord(idx, planned.label, "REVIEW", decision.codes, False))
                    result.notes.append(f"step{idx} 挂起等人工: {decision.codes}")
                    executed_all = False
                    break
                if decision.verdict == "BLOCK":
                    self.review_count += 0
                    self.block_count += 1
                    result.blocked = True
                    result.steps.append(StepRecord(idx, planned.label, "BLOCK", decision.codes, False))
                    result.notes.append(f"step{idx} 熔断: {decision.codes}")
                    executed_all = False
                    break

                # ALLOW → 执行 + 纵深复核
                hyp = self.domain.execution_hypothesis(working)
                op = planned.variant_for(hyp.hyp_id)
                if op is None:
                    op = planned.variants["*"]
                post = self.domain.transition(self.state, op, hyp.params)
                breaches = self.gate.verify_executed(post, op, hyp.params)
                if breaches:
                    self.invariant_breaches += 1
                    result.blocked = True
                    result.notes.append(f"step{idx} 执行后复核发现违例: {breaches}")
                    executed_all = False
                    break
                self.ledger.post_action(period, idx, op, hyp.hyp_id, hyp.params, post)
                self.env.execute(period, op, hyp.params)
                self.state = StateSnapshot(period=period, values=post.values)
                self.allow_count += 1
                self.executed_count += 1
                result.steps.append(StepRecord(idx, planned.label, "ALLOW", decision.codes, True, hyp.hyp_id))
                self.audit.append({"event": "execute", "period": period, "step": idx,
                                   "action": op.fingerprint(), "hyp": hyp.hyp_id,
                                   "state_after": post.fingerprint()})

            result.hypotheses_after = len(working.hypotheses)

            # 6) 期末睁眼
            obs_step = self.domain.observation_step(plan)
            final_prediction = predictions.get(obs_step)
            if final_prediction is None and predictions:
                final_prediction = predictions[max(predictions)]
            if final_prediction is not None:
                self.predictions[period] = (final_prediction, working, plan)
                self.audit.append({
                    "event": "prediction", "period": period, "step": final_prediction.step,
                    "fingerprint": final_prediction.fingerprint(),
                    "hypotheses": list(working.ids),
                })

            if not executed_all or final_prediction is None:
                continue

            observation, delay = self.env.finalize(period)
            result.observation = observation
            if delay <= 0:
                self._settle_observation(observation, current_period=period)
            else:
                release = self._release_label(period, delay)
                if release is None:
                    self.delayed_unreleased += 1
                    result.notes.append(f"观测延迟 {delay} 期，超出评估窗口（未结算）")
                else:
                    self._pending_obs.append((release, observation))
                    self.delayed_scheduled += 1
                    result.notes.append(f"观测延迟到 {release} 结算")

            # 7) 经验固化（仅完整跑通的账期）
            self._remember(plan, period, business_date, result)

        return self._report()

    # ------------------------------------------------------------------
    # 规划与复用
    # ------------------------------------------------------------------
    def _plan(
        self,
        period: str,
        business_date: date,
        facts: Mapping[str, Any],
        hypotheses: HypothesisSet,
        result: PeriodResult,
    ) -> Plan:
        goal = self.domain.goal
        if self.mpc is not None:
            return self._mpc_plan(period, business_date, facts, hypotheses, result)
        rule_hashes = self.domain.rule_hashes(business_date)
        fact_hash = self.ledger.fact_values_hash(
            business_date, period, self.domain.required_fact_keys)
        case = self.memory.retrieve(goal)
        self.reuse_attempts += 1
        check = self.memory.validate(case, goal, rule_hashes, facts, fact_hash)
        if check.valid:
            try:
                plan = self.domain.instantiate(
                    case.skeleton, self.state, facts, hypotheses, business_date)
                result.reuse_mode = check.mode
                result.reuse_reasons = check.reasons
                result.notes.append(
                    f"经验复用 {check.mode} case={check.case_id} skeleton={case.skeleton}")
                self.reuse_valid += 1
                if check.mode == "EXACT":
                    self.reuse_exact += 1
                self.audit.append({"event": "reuse", "period": period,
                                   "mode": check.mode, "case": check.case_id})
                return plan
            except ValueError as exc:
                result.reuse_reasons = check.reasons + (f"INSTANTIATE_FAILED:{exc}",)
                result.notes.append(f"经验骨架实例化失败: {exc} → 重新规划")
        else:
            result.reuse_reasons = check.reasons
            result.notes.append(f"经验失效: {list(check.reasons)} → 重新规划")
            self.audit.append({"event": "reuse_invalid", "period": period,
                               "reasons": list(check.reasons)})
        result.reuse_mode = "REPLAN"
        self.replans += 1
        plan = self.domain.plan(self.state, facts, hypotheses, business_date)
        self.audit.append({"event": "plan", "period": period,
                           "fingerprint": plan.fingerprint(), "skeleton": list(plan.skeleton)})
        return plan

    def _mpc_plan(
        self,
        period: str,
        business_date: date,
        facts: Mapping[str, Any],
        hypotheses: HypothesisSet,
        result: PeriodResult,
    ) -> Plan:
        """MPC 路径：候选（域内备选 + 可复用经验）→ 字典序选择 → 交闸门。"""
        assert self.mpc is not None
        goal = self.domain.goal
        rule_hashes = self.domain.rule_hashes(business_date)
        fact_hash = self.ledger.fact_values_hash(
            business_date, period, self.domain.required_fact_keys)
        candidates = [
            ("greedy:" + str(index), plan)
            for index, plan in enumerate(
                self.domain.candidate_plans(self.state, facts, hypotheses, business_date))
        ]
        self.reuse_attempts += 1
        case = self.memory.retrieve(goal)
        check = self.memory.validate(case, goal, rule_hashes, facts, fact_hash)
        if check.valid:
            try:
                reused = self.domain.instantiate(
                    case.skeleton, self.state, facts, hypotheses, business_date)
                candidates.append(("reuse:" + check.mode, reused))
                result.reuse_reasons = check.reasons
            except ValueError:
                result.reuse_reasons = check.reasons + ("INSTANTIATE_FAILED",)
        selection = self.mpc.select(
            candidates,
            lambda plan: self.domain.score_plan(plan, self.state, facts, hypotheses),
        )
        chosen = selection.chosen
        result.reuse_mode = "MPC"
        result.notes.append("MPC: " + selection.summary())
        self.mpc_considered += len(candidates)
        self.mpc_choices[period] = chosen.label
        if chosen.label.startswith("reuse"):
            self.reuse_valid += 1
            if chosen.label.endswith("EXACT"):
                self.reuse_exact += 1
        else:
            self.replans += 1
        self.audit.append({
            "event": "mpc", "period": period, "chosen": chosen.label,
            "objectives": list(selection.objectives), "considered": len(candidates),
            "scores": {key: format(value, "f") for key, value in chosen.scores.items()},
        })
        return chosen.plan

    def _remember(self, plan: Plan, period: str, business_date: date,
                  result: PeriodResult) -> None:
        fact_hash = self.ledger.fact_values_hash(
            business_date, period, self.domain.required_fact_keys)
        case = self.memory.save(plan, period, outcome="closed",
                                metrics={"input_hash": fact_hash, "episodes": "1"})
        result.closed = True
        result.notes.append(f"经验固化 case={case.case_id}")
        self.audit.append({"event": "case_saved", "period": period,
                           "case": case.case_id, "signature": case.signature})

    # ------------------------------------------------------------------
    # 推演 / 执行 / 观测
    # ------------------------------------------------------------------
    def _forecast(self, planned: PlannedAction, step: int,
                  hypotheses: HypothesisSet) -> Prediction:
        scenarios: Dict[str, StateSnapshot] = {}
        variant_fps: Dict[str, str] = {}
        for hyp in hypotheses.hypotheses:
            op = planned.variant_for(hyp.hyp_id)
            if op is None:
                continue
            scenarios[hyp.hyp_id] = self.domain.transition(self.state, op, hyp.params)
            variant_fps[hyp.hyp_id] = op.fingerprint()

        intervals: Dict[str, Interval] = {}
        for key in self.domain.observation_keys:
            values = [scenario.get(key) for scenario in scenarios.values()]
            if not values:
                continue
            intervals[key] = Interval.from_values(values).expand(self.calibrator.bound(key))
        return Prediction(
            period=self.state.period,
            step=step,
            step_name=planned.name,
            scenarios=scenarios,
            intervals=intervals,
            variant_fingerprints=variant_fps,
        )

    def _execution_hypothesis(self, hypotheses: HypothesisSet):
        """执行记账用的确定假设：唯一就用它；多假设时按 id 序取第一个（仅供可回放推演）。"""
        if hypotheses.is_singleton:
            return hypotheses.single()
        return sorted(hypotheses.hypotheses, key=lambda h: h.hyp_id)[0]

    def _release_label(self, period: str, delay: int) -> Optional[str]:
        periods = list(self.config.periods)
        idx = periods.index(period) + delay
        if 0 <= idx < len(periods):
            return periods[idx]
        return None

    def _settle_due(self, period: str) -> None:
        due = [obs for release, obs in self._pending_obs if release == period]
        self._pending_obs = [(rel, obs) for rel, obs in self._pending_obs if rel != period]
        for obs in due:
            self._settle_observation(
                replace(obs, received_at=period, delayed=True), current_period=period)

    def _settle_observation(self, obs: Observation, current_period: str) -> None:
        """睁眼：残差分解 → 筛假设 → 重校准 → （同期）状态修正。"""
        self.observations.append(obs)
        stored = self.predictions.get(obs.period)
        target = self.results.get(obs.period)
        if stored is None or target is None:
            self.model_errors += 1
            self.audit.append({"event": "observation_without_prediction", "period": obs.period})
            return

        prediction, hypotheses, _plan = stored
        report = self.analyzer.analyze(prediction, obs)
        self.residuals.append(report)
        self.coverage.record(report)
        target.residual = report

        tolerance = {key: self.domain.observation_tolerance(key)
                     for key in self.domain.observation_keys if key in obs.values}
        kept, any_match = self.sieve.sieve(prediction, obs, tolerance)
        changes = self.calibrator.update(report)
        target.calibration_changes = tuple(changes)
        target.sieve_kept = kept

        if any_match and 0 < len(kept) < len(hypotheses.hypotheses):
            self.hypothesis_shrinks += 1
            target.notes.append(f"观测筛假设: {len(hypotheses.hypotheses)} -> {len(kept)}")
            target.hypotheses_after = len(kept)
        elif not any_match and report.in_bound:
            self.unexplained_in_bound += 1
            target.notes.append("观测落在误差界内但无假设精确解释（账实/模型持续偏差，交人工）")
        elif not any_match:
            self.model_errors += 1
            target.notes.append("观测越界且与全部假设都不匹配 → 模型/抽取错误（重校准，不猜）")

        self.audit.append({
            "event": "observation", "period": obs.period, "received_at": current_period,
            "delayed": obs.period != current_period, "fingerprint": obs.fingerprint(),
            "in_bound": report.in_bound,
            "out_of_bound": [row.key for row in report.out_of_bound],
            "calibration_changes": list(changes), "sieve_kept": list(kept),
        })

        # 同期越界 → 域可提出"红字冲销/追溯调整"，但同样要过闸门
        if not report.in_bound and obs.period == current_period:
            self._apply_correction(obs, report, hypotheses, target)

        if obs.period != current_period:
            self.delayed_settlements += 1
            target.delayed_settled.append(current_period)
            current = self.results.get(current_period)
            if current is not None:
                current.notes.append(
                    f"结算迟到观测 {obs.period}（in_bound={report.in_bound}）")
            return  # 迟到观测只校准，不回改已推进的状态

        corrected = self.domain.apply_observation(self.state, obs)
        if corrected.fingerprint() != self.state.fingerprint():
            self.ledger.post_observation(obs, corrected)
            self.state = StateSnapshot(period=current_period, values=corrected.values)

    def _apply_correction(
        self,
        obs: Observation,
        report: ResidualReport,
        hypotheses: HypothesisSet,
        target: PeriodResult,
    ) -> None:
        """睁眼修正（改账）：域提出修正动作 → 闭眼复核 → 安全过滤 → 落账。

        修正动作不是特权：它和普通动作一样过闸门；过不了就只告警，绝不硬改。
        """
        correction = self.domain.correction_action(report, obs, self.state, hypotheses)
        if correction is None:
            self.corrections_unavailable += 1
            target.notes.append("观测越界但域不提供修正动作（保持告警，不改账）")
            return
        planned = PlannedAction(correction.name, f"红字冲销（{obs.period}）", {"*": correction})
        scenarios = {
            hyp.hyp_id: self.domain.transition(self.state, correction, hyp.params)
            for hyp in hypotheses.hypotheses
        }
        prediction = Prediction(
            period=obs.period, step=-2, step_name=correction.name, scenarios=scenarios,
            intervals={},
            variant_fingerprints={hyp.hyp_id: correction.fingerprint()
                                  for hyp in hypotheses.hypotheses},
        )
        decision = self.gate.admit(planned, prediction, hypotheses)
        self.audit.append({
            "event": "correction_gate", "period": obs.period, "verdict": decision.verdict,
            "codes": list(decision.codes), "action": correction.fingerprint(),
        })
        if not decision.allowed:
            self.corrections_rejected += 1
            target.notes.append(f"红字冲销未通过闸门: {decision.codes}")
            return
        hyp = self.domain.execution_hypothesis(hypotheses)
        post = self.domain.transition(self.state, correction, hyp.params)
        breaches = self.gate.verify_executed(post, correction, hyp.params)
        if breaches:
            self.corrections_rejected += 1
            target.notes.append(f"红字冲销执行后复核发现违例: {breaches}")
            self.invariant_breaches += 1
            return
        self.ledger.post_action(obs.period, -2, correction, hyp.hyp_id, hyp.params, post)
        self.state = StateSnapshot(period=obs.period, values=post.values)
        self.corrections_applied += 1
        target.notes.append(
            "红字冲销已执行: 收入误差 " + format(correction.decimal("revenue_error"), "f")
            + "，所得税误差 " + format(correction.decimal("tax_error"), "f")
        )
        self.audit.append({
            "event": "correction", "period": obs.period, "action": correction.fingerprint(),
            "hyp": hyp.hyp_id, "state_after": post.fingerprint(),
        })

    # ------------------------------------------------------------------
    # 审计与报告
    # ------------------------------------------------------------------
    def _record_decision(self, period: str, step: int, planned: PlannedAction,
                         decision: GateDecision, hypotheses: HypothesisSet) -> None:
        self.audit.append({
            "event": "gate", "period": period, "step": step, "action": planned.label,
            "verdict": decision.verdict, "codes": list(decision.codes),
            "checked": decision.checked_hypotheses, "hypotheses": list(hypotheses.ids),
        })

    def _report(self) -> LoopReport:
        facts_ok, facts_bad = self.ledger.verify_facts()
        journal_ok, journal_bad = self.ledger.verify_journal()
        audit_ok, audit_bad = self.audit.verify()
        replay_ok = True
        replay_error = ""
        try:
            self.ledger.replay(self.domain)
        except Exception as exc:  # noqa: BLE001 - 回放失败必须暴露
            replay_ok = False
            replay_error = str(exc)

        flagged = sum(1 for report in self.residuals if not report.in_bound)
        periods = [self.results[p] for p in self.config.periods if p in self.results]
        metrics: Dict[str, Any] = {
            "periods": len(periods),
            "closed_periods": sum(1 for p in periods if p.closed),
            "blocked_periods": sum(1 for p in periods if p.blocked),
            "escalated_periods": sum(1 for p in periods if p.escalated),
            "executed_actions": self.executed_count,
            "allow": self.allow_count,
            "review": self.review_count,
            "review_events": self.review_events,
            "confirmations": self.confirmations,
            "block": self.block_count,
            "escalations": self.escalations,
            "replans": self.replans,
            "reuse_attempts": self.reuse_attempts,
            "reuse_valid": self.reuse_valid,
            "reuse_exact": self.reuse_exact,
            "mpc_objectives": list(self.mpc.config.effective_objectives()) if self.mpc else [],
            "mpc_considered": self.mpc_considered,
            "mpc_choices": dict(self.mpc_choices),
            "reuse_invalid_reasons": dict(self.memory.invalid_reasons),
            "residual_checks": len(self.residuals),
            "residual_flagged": flagged,
            "coverage": round(self.coverage.coverage, 4),
            "hypothesis_shrinks": self.hypothesis_shrinks,
            "delayed_scheduled": self.delayed_scheduled,
            "delayed_settlements": self.delayed_settlements,
            "delayed_unreleased": self.delayed_unreleased,
            "model_errors": self.model_errors,
            "unexplained_in_bound": self.unexplained_in_bound,
            "corrections_applied": self.corrections_applied,
            "corrections_rejected": self.corrections_rejected,
            "corrections_unavailable": self.corrections_unavailable,
            "invariant_breaches": self.invariant_breaches,
            "false_accept": self.invariant_breaches,
            "ledger_facts_ok": bool(facts_ok and facts_bad is None),
            "ledger_journal_ok": bool(journal_ok and journal_bad is None),
            "audit_ok": bool(audit_ok and audit_bad is None),
            "audit_records": len(self.audit.records),
            "replay_ok": replay_ok,
            "calibrator_bounds": {k: format(v, "f")
                                  for k, v in sorted(self.calibrator.bounds.items())},
            "cases": self.memory.stats(),
        }
        if not replay_ok:
            metrics["replay_error"] = replay_error
        return LoopReport(domain=self.domain.name, periods=periods,
                          metrics=metrics, observations=self.observations)
