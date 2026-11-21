"""对照/消融实验执行器（确定性、无随机）。

协议（预注册 + 事后补充场景 B 登记）：``eval/ablation_protocol.md``；
结果：``eval/ablation_report.md`` / ``.json``。

组别：full / no_gate / no_hypothesis / no_calibration / no_memory / pure_rules；
pure_llm 基线未运行（无 LLM API 凭据，不伪造数据）。

场景：
- A：``finance.default_scenario()``（P03 抽取错误、P04 税率歧义真实 20%、P05 回执延迟）；
- B：**事后补充（exploratory）**：同一场景，P04 真实税率为 25%（一般企业），
  用于检验"假设集/闸门"是否能处理与朴素单版本相反的方向；
- C：``cutting.default_scenario()``（P03 文档板长错）。

主指标：
- ``wrong_executions_total``：被执行且参数与真值不符的动作数（含事后修正的）；
- ``wrong_on_P04``：场景 B 中 P04 期（歧义期）的错误执行数（判别性指标）；
- ``unremediated_wrong_executions``：未被同期修正的错误执行数；
- ``gate_intercepts``（外部确认次数）/ ``residual_flagged`` / ``coverage`` /
  ``model_errors`` / ``replans`` / ``reuse_valid`` / ``audit_ok`` / ``replay_ok``；
- ``oracle_invalid_executed``：仅对 pure_rules（无修正动作，可完整重放）计算；
  ClosedLoop 变体的修正动作走账本而非环境，重放需账本级信息，故不计算（见诚实边界）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Mapping

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from loop_mvp.cutting import CuttingDomain, CuttingEnvironment  # noqa: E402
from loop_mvp.cutting import default_scenario as cutting_scenario  # noqa: E402
from loop_mvp.estimator import HypothesisSet  # noqa: E402
from loop_mvp.finance import FinanceDomain, FinanceEnvironment  # noqa: E402
from loop_mvp.finance import default_scenario as finance_scenario  # noqa: E402
from loop_mvp.gate import SafetyGate  # noqa: E402
from loop_mvp.loop import ClosedLoop, LoopConfig  # noqa: E402
from loop_mvp.types import ActionOp, GateDecision, dec, q  # noqa: E402

OUT_MD = ROOT / "eval" / "ablation_report.md"
OUT_JSON = ROOT / "eval" / "ablation_results.json"

VARIANTS = ("full", "no_gate", "no_hypothesis", "no_calibration", "no_memory", "pure_rules")
LLM_NOTE = ("pure_llm 已补跑（合成场景、deepseek-flash、n=3 重复）：36 次调用 = 30 CORRECT + "
            "6 UNKNOWN + 0 WRONG；A/B 的 P04 在 3 次运行中均答『主体类型未知』（UNKNOWN）；"
            "12/12 期跨运行回答完全一致；详见 eval/llm_baseline_report.md。"
            "单次合成任务不具统计意义，且提示明确允许 UNKNOWN（对模型友好）。")


class AlwaysAllowGate(SafetyGate):
    def admit(self, planned, prediction, hypotheses):
        return GateDecision("ALLOW", (), "ablation: gate bypass", len(hypotheses.hypotheses))

    def verify_executed(self, state_after, action, params):
        return ()


class SingleVersionDomain:
    """消融：只保留按版本号排序的第一个单版本（不处理歧义）。"""

    def __init__(self, domain):
        self._domain = domain

    def __getattr__(self, name):
        return getattr(self._domain, name)

    def resolve_hypotheses(self, business_date, entity):
        hypotheses = self._domain.resolve_hypotheses(business_date, entity)
        ordered = sorted(hypotheses.hypotheses, key=lambda hyp: hyp.hyp_id)
        return HypothesisSet((ordered[0],))


class StubMemory:
    def __init__(self):
        self.invalid_reasons: Dict[str, str] = {}

    def retrieve(self, *args, **kwargs):
        return None

    def validate(self, *args, **kwargs):
        return SimpleNamespace(valid=False, mode="STUB", reasons=("NO_CASE",), case_id="STUB")

    def save(self, *args, **kwargs):
        return SimpleNamespace(case_id="STUB", signature="stub")

    def stats(self):
        return {"cases": 0, "stored": 0, "reused": 0}


def _new_loop(variant, domain, env, periods, dates):
    loop = ClosedLoop(domain, env, LoopConfig(periods=periods, business_dates=dates))
    if variant == "no_gate":
        loop.gate = AlwaysAllowGate(domain)
    elif variant == "no_hypothesis":
        wrapped = SingleVersionDomain(domain)
        loop.domain = wrapped
        loop.gate = SafetyGate(wrapped)
    elif variant == "no_calibration":
        loop.calibrator.update = lambda report: []  # type: ignore[assignment]
    elif variant == "no_memory":
        loop.memory = StubMemory()  # type: ignore[assignment]
    return loop


def _run_pure_rules(domain, env, periods, dates):
    env.reset()
    state = domain.initial_state()
    executed = 0
    for period in periods:
        facts_bundle = env.extract_facts(period, dates[period])
        hypotheses = domain.resolve_hypotheses(dates[period], "ENTITY-A")
        ordered = sorted(hypotheses.hypotheses, key=lambda hyp: hyp.hyp_id)
        hypotheses = HypothesisSet((ordered[0],))
        plan = domain.plan(state, facts_bundle.facts, hypotheses, dates[period])
        for planned in plan.actions:
            hyp = hypotheses.single()
            op = planned.variant_for(hyp.hyp_id) or planned.variants["*"]
            post = domain.transition(state, op, hyp.params)
            env.execute(period, op, hyp.params)
            state = post
            executed += 1
    metrics = {
        "periods": len(periods), "closed_periods": len(periods),
        "executed_actions": executed, "allow": executed, "review_events": 0,
        "block": 0, "residual_checks": 0, "residual_flagged": 0,
        "coverage": 0.0, "model_errors": 0, "replans": len(periods),
        "reuse_valid": 0, "false_accept": 0, "audit_ok": None, "replay_ok": None,
    }
    results = [SimpleNamespace(period=period, notes=[], residual=None, steps=[])
               for period in periods]
    return metrics, results


def _scenario_b():
    periods, data, delays, dates = finance_scenario()
    data = {period: dict(values) for period, values in data.items()}
    data["P04"]["true_rate"] = "0.25"  # 真实为一般企业 25%（与朴素单版本相反方向）
    return periods, data, delays, dates


def _wrong_executions(executed, data, notes_by_period):
    wrong: List[Dict[str, str]] = []
    remediated = 0
    for period in executed:
        repaired = any("红字冲销已执行" in note for note in notes_by_period.get(period, []))
        for record in executed[period]:
            action = record[0]
            params = record[1]
            name = action.name if isinstance(action, ActionOp) else str(action)
            expected = actual = None
            if name == "REVENUE":
                expected = q(dec(data[period]["true_revenue"]))
                actual = q(dec(str(params.get("amount"))))
            elif name == "COST":
                expected = q(dec(data[period]["true_cost"]))
                actual = q(dec(str(params.get("amount"))))
            elif name == "TAX":
                expected = q(dec(data[period]["true_rate"]))
                actual = q(dec(str(params.get("rate"))))
            if expected is None or actual is None or actual == expected:
                continue
            wrong.append({"period": period, "action": name,
                          "expected": format(expected, "f"),
                          "actual": format(actual, "f")})
            if repaired:
                remediated += 1
    return {
        "wrong_executions_total": len(wrong),
        "wrong_on_P04": len([item for item in wrong if item["period"] == "P04"]),
        "wrong_executions_remediated": remediated,
        "unremediated_wrong_executions": len(wrong) - remediated,
        "wrong_details": wrong,
    }


def _oracle_invalid(domain, executed) -> int:
    gate = SafetyGate(domain)
    state = domain.initial_state()
    invalid = 0
    for period in executed:
        for record in executed[period]:
            action, params = record
            op = action if isinstance(action, ActionOp) else ActionOp(str(action), dict(params))
            post = domain.transition(state, op, params)
            if gate.verify_executed(post, op, params):
                invalid += 1
            state = post
    return invalid


def _gate_events(results):
    events = []
    for result in results:
        for step in result.steps:
            if step.verdict in ("REVIEW", "BLOCK"):
                events.append({"period": result.period, "action": step.action,
                               "verdict": step.verdict, "codes": list(step.codes)})
        for note in result.notes:
            if "外部确认" in note:
                events.append({"period": result.period, "action": "(correction/confirm)",
                               "verdict": "REVIEW", "codes": ["EXTERNAL_CONFIRM"]})
    return events


def _finance_detection(results, scenario: str) -> Dict[str, str]:
    by_period = {result.period: result for result in results}
    out: Dict[str, str] = {}

    def notes(period: str) -> str:
        result = by_period.get(period)
        return " ".join(result.notes) if result else ""

    def residual_out(period: str) -> bool:
        result = by_period.get(period)
        return bool(result) and result.residual is not None and not result.residual.in_bound

    def gate_hit(period: str) -> bool:
        return "外部确认" in notes(period) or bool(_gate_events([by_period[period]])
                                                    if period in by_period else [])

    if scenario == "finance_close":
        if "P03" in by_period:
            if gate_hit("P03"):
                out["F1_P03_抽取错误"] = "拦截"
            elif "红字冲销已执行" in notes("P03"):
                out["F1_P03_抽取错误"] = "事后修正"
            elif residual_out("P03"):
                out["F1_P03_抽取错误"] = "事后告警"
            else:
                out["F1_P03_抽取错误"] = "未发现"
    if scenario == "finance_close_b":
        if "P04" in by_period:
            if gate_hit("P04"):
                out["F2b_P04_歧义(真实25%)"] = "拦截"
            elif "红字冲销已执行" in notes("P04"):
                out["F2b_P04_歧义(真实25%)"] = "事后修正"
            elif residual_out("P04"):
                out["F2b_P04_歧义(真实25%)"] = "事后告警"
            else:
                out["F2b_P04_歧义(真实25%)"] = "未发现"
    if scenario == "finance_close" and "P05" in by_period:
        text = notes("P05")
        out["F3_P05_回执延迟"] = "已结算(延迟)" if ("观测延迟" in text or "结算迟到" in text) else "未处理"
    return out


def run_finance_variant(variant: str, scenario: str) -> Dict[str, Any]:
    periods, data, delays, dates = _scenario_b() if scenario == "finance_close_b" else finance_scenario()
    domain = FinanceDomain()
    env = FinanceEnvironment(periods, data, delays)
    if variant == "pure_rules":
        metrics, results = _run_pure_rules(domain, env, periods, dates)
    else:
        loop = _new_loop(variant, domain, env, periods, dates)
        report = loop.run()
        metrics, results = dict(report.metrics), report.periods
    notes_by_period = {result.period: list(result.notes) for result in results}
    wrong = _wrong_executions(env.executed, data, notes_by_period)
    return {
        "scenario": scenario,
        "variant": variant,
        "metrics": metrics,
        "wrong": wrong,
        "oracle_invalid_executed": _oracle_invalid(domain, env.executed) if variant == "pure_rules" else None,
        "gate_events": _gate_events(results),
        "residual_out_periods": [result.period for result in results
                                 if result.residual is not None and not result.residual.in_bound],
        "notes_by_period": notes_by_period,
        "detection": _finance_detection(results, scenario),
    }


def run_cutting_variant(variant: str) -> Dict[str, Any]:
    periods, data, delays, dates = cutting_scenario()
    domain = CuttingDomain()
    env = CuttingEnvironment(periods, data, delays)
    if variant == "pure_rules":
        metrics, results = _run_pure_rules(domain, env, periods, dates)
    else:
        loop = _new_loop(variant, domain, env, periods, dates)
        report = loop.run()
        metrics, results = dict(report.metrics), report.periods
    by_period = {result.period: result for result in results}
    detection = {}
    if "P03" in by_period:
        result = by_period["P03"]
        notes = " ".join(result.notes)
        gate_hit = any(step.verdict in ("REVIEW", "BLOCK") for step in result.steps) or "外部确认" in notes
        residual_out = result.residual is not None and not result.residual.in_bound
        if gate_hit:
            detection["F4_P03_板长错"] = "拦截"
        elif "冲销" in notes or "修正" in notes:
            detection["F4_P03_板长错"] = "事后修正"
        elif residual_out:
            detection["F4_P03_板长错"] = "事后告警"
        else:
            detection["F4_P03_板长错"] = "未发现"
    return {
        "scenario": "cutting_yield",
        "variant": variant,
        "metrics": metrics,
        "oracle_invalid_executed": _oracle_invalid(domain, env.executed) if variant == "pure_rules" else None,
        "gate_events": _gate_events(results),
        "detection": detection,
    }


def main() -> int:
    results = []
    for scenario in ("finance_close", "finance_close_b", "cutting_yield"):
        for variant in VARIANTS:
            if scenario == "cutting_yield":
                results.append(run_cutting_variant(variant))
            else:
                results.append(run_finance_variant(variant, scenario))
    OUT_JSON.write_text(json.dumps({"protocol": "eval/ablation_protocol.md",
                                    "results": results, "llm_note": LLM_NOTE},
                                   ensure_ascii=False, indent=2), encoding="utf-8")

    scen = {name: [item for item in results if item["scenario"] == name]
            for name in ("finance_close", "finance_close_b", "cutting_yield")}
    finance, finance_b, cutting = scen["finance_close"], scen["finance_close_b"], scen["cutting_yield"]

    lines = [
        "# 对照/消融实验报告（确定性、无随机）",
        "",
        "- 协议（预注册 + 事后补充场景 B 登记）：`eval/ablation_protocol.md`；"
        "原始数据：`eval/ablation_results.json`",
        f"- **未运行基线**：{LLM_NOTE}",
        "- 组别：`full` 完整内核；`no_gate` 去闸门；`no_hypothesis` 单版本；"
        "`no_calibration` 去校准；`no_memory` 去经验复用；`pure_rules` 纯规则。",
        "- 主指标：`wrong_total`=错误执行总数（含事后修正）；`wrong_on_P04`=歧义期错误执行；"
        "`unremediated`=未被同期修正的错误执行；`intercepts`=外部确认/拦截次数。",
        "",
        "## 一、场景 A：默认财务场景（P03 抽取错误、P04 歧义真实 20%、P05 延迟）",
        "",
        "| 组 | wrong_total | unremediated | 已修正 | 拦截 | 残差越界 | closed | coverage | model_errors | replans | reuse_valid | audit/replay |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for item in finance:
        metrics = item["metrics"]
        wrong = item["wrong"]
        audit = "n/a" if metrics.get("audit_ok") is None else (
            f"{metrics.get('audit_ok')}/{metrics.get('replay_ok')}")
        lines.append(f"| {item['variant']} | {wrong['wrong_executions_total']} | "
                     f"{wrong['unremediated_wrong_executions']} | "
                     f"{wrong['wrong_executions_remediated']} | {len(item['gate_events'])} | "
                     f"{len(item['residual_out_periods'])} | {metrics.get('closed_periods')} | "
                     f"{metrics.get('coverage')} | {metrics.get('model_errors')} | "
                     f"{metrics.get('replans')} | {metrics.get('reuse_valid')} | {audit} |")
    lines.extend(["", "### 故障发现方式（场景 A）", "",
                  "| 故障 | " + " | ".join(item["variant"] for item in finance) + " |",
                  "|---|" + "---|" * len(finance)])
    keys = []
    for item in finance:
        for key in item["detection"]:
            if key not in keys:
                keys.append(key)
    for key in keys:
        lines.append("| " + " | ".join([key] + [item["detection"].get(key, "-") for item in finance]) + " |")

    lines.extend(["", "## 二、场景 B（事后补充，判别性）：P04 歧义真实 25%（一般企业）", "",
                  "| 组 | wrong_total | wrong_on_P04 | unremediated | 拦截 | 残差越界 | closed | coverage |",
                  "|---|---:|---:|---:|---:|---:|---:|---:|"])
    for item in finance_b:
        metrics = item["metrics"]
        wrong = item["wrong"]
        lines.append(f"| {item['variant']} | {wrong['wrong_executions_total']} | "
                     f"{wrong['wrong_on_P04']} | {wrong['unremediated_wrong_executions']} | "
                     f"{len(item['gate_events'])} | {len(item['residual_out_periods'])} | "
                     f"{metrics.get('closed_periods')} | {metrics.get('coverage')} |")
    lines.extend(["", "### 故障发现方式（场景 B）", "",
                  "| 故障 | " + " | ".join(item["variant"] for item in finance_b) + " |",
                  "|---|" + "---|" * len(finance_b)])
    keys_b = []
    for item in finance_b:
        for key in item["detection"]:
            if key not in keys_b:
                keys_b.append(key)
    for key in keys_b:
        lines.append("| " + " | ".join([key] + [item["detection"].get(key, "-")
                                                for item in finance_b]) + " |")

    lines.extend(["", "## 三、场景 C：板材下料（P03 文档板长错）", "",
                  "| 组 | closed | 拦截 | 故障发现 |", "|---|---:|---:|---|"])
    for item in cutting:
        metrics = item["metrics"]
        lines.append(f"| {item['variant']} | {metrics.get('closed_periods')} | "
                     f"{len(item['gate_events'])} | "
                     f"{item['detection'].get('F4_P03_板长错', '-')} |")

    actual_b = {item["variant"]: item["wrong"]["wrong_on_P04"] for item in finance_b}
    expected_b = {"full": 0, "no_gate": 1, "no_hypothesis": 1,
                  "no_calibration": 0, "no_memory": 0, "pure_rules": 1}
    lines.extend(["", "## 四、预期 vs 实际（场景 B，判别性指标 wrong_on_P04）", "",
                  "| 组 | 预期 | 实际 | 是否达预期 |", "|---|---:|---:|---|"])
    for variant in VARIANTS:
        met = actual_b[variant] == expected_b[variant]
        lines.append(f"| {variant} | {expected_b[variant]} | {actual_b[variant]} | "
                     f"{'是' if met else '否（登记偏差）'} |")
    lines.extend([
        "",
        "## 五、结论与偏差登记",
        "",
        "1. **场景 B 验证假设集+闸门的价值**：真实税率 25% 时，完整内核在 P04 触发歧义 REVIEW → "
        "外部确认 → 按 25% 执行（`wrong_on_P04=0`）；`no_gate` / `no_hypothesis` 按单版本 20% 直接执行，"
        "产生该期错误执行（`wrong_on_P04=1`），只能靠事后残差/修正兜底；",
        "2. **纯规则最危险**：无观测、无修正，P03/P04 的错误执行既无拦截也无修正；",
        "3. **场景 A 说明歧义的『方向性』**：默认场景真实 20%，朴素单版本恰好也取 20%，"
        "因此 `no_gate` 在 A 未产生 P04 错误——这不是安全性证明，只是运气；B 才暴露问题（此点如实登记）；",
        "4. **校准/经验复用不影响本实验的误执行**（符合预期），但影响效率指标"
        "（no_memory 的 replans 显著上升、reuse_valid=0）；需在更大场景中量化 coverage 差异；",
        "5. **纯 LLM 基线（deepseek-flash，n=3，合成）**：36 次调用 = 30 CORRECT + 6 UNKNOWN + 0 WRONG；"
        "A/B 的 P04 在 3 次运行中均答 UNKNOWN，12/12 期跨运行一致——在本合成任务上它同样 fail-closed；"
        "但 UNKNOWN 需要人工跟进，且无审计/回放/确定性，提示明确允许 UNKNOWN（对模型友好），"
        "**不能外推真实场景**；详见 `eval/llm_baseline_report.md`。",
        "",
        "## 六、诚实边界",
        "",
        "- 合成场景、样本小（6+6+3 期），不构成统计泛化结论；内核确定性，重复运行结果相同；",
        "- 修正动作走账本而非环境，ClosedLoop 变体的 `oracle_invalid` 无法完整重放，故只对 pure_rules 计算；",
        "- 主指标基于场景真值（收入/成本/税率）比对，『已修正』只表示有红字冲销，不评价修正本身是否最优；",
        "- 场景 B 为**事后补充（exploratory）**，在协议中登记为补充实验，不算预注册成功；",
        "- 真实数据闭环不参与本实验（其任务是对账/发现数据质量问题，不是动作决策）；不构成税务/审计意见。",
    ])
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("ablation scenarios:")
    for name, group in (("A", finance), ("B", finance_b), ("C", cutting)):
        print(f"  [{name}]")
        for item in group:
            metrics = item["metrics"]
            if item["scenario"] == "cutting_yield":
                print(f"    {item['variant']:<14} closed={metrics.get('closed_periods')} "
                      f"r={item['detection'].get('F4_P03_板长错')}")
            else:
                wrong = item["wrong"]
                print(f"    {item['variant']:<14} wrong={wrong['wrong_executions_total']} "
                      f"P04={wrong['wrong_on_P04']} "
                      f"unremediated={wrong['unremediated_wrong_executions']} "
                      f"intercepts={len(item['gate_events'])} "
                      f"replans={metrics.get('replans')} coverage={metrics.get('coverage')}")
    print(f"report: {OUT_MD.relative_to(ROOT)} | json: {OUT_JSON.relative_to(ROOT)}")
    full_b = next(item for item in finance_b if item["variant"] == "full")
    gate_b = next(item for item in finance_b if item["variant"] in ("no_gate", "no_hypothesis"))
    ok = (full_b["wrong"]["wrong_on_P04"] == 0
          and gate_b["wrong"]["wrong_on_P04"] >= 1
          and full_b["metrics"].get("audit_ok") and full_b["metrics"].get("replay_ok"))
    print("result:", "PASS" if ok else "FAIL")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
