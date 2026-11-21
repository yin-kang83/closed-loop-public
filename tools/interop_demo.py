"""对接层演示：闭环审计包（math-gate 四档语义 / fin-agent-platform 判断-签字结构）。

运行：``python tools/interop_demo.py``
- 跑两个域（财务结账 / 板材下料）的闭环，导出确定性审计包 + shadow 事件流；
- 校验内容哈希与判定映射；退出码 0 = 两域审计包可校验、0 误放行、映射自洽。
- 只读推演（shadow）：不触发任何真实记账/落料/申报动作。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from loop_mvp.cutting import CuttingDomain, CuttingEnvironment  # noqa: E402
from loop_mvp.cutting import default_scenario as cutting_scenario  # noqa: E402
from loop_mvp.finance import FinanceDomain, FinanceEnvironment  # noqa: E402
from loop_mvp.finance import default_scenario as finance_scenario  # noqa: E402
from loop_mvp.interop import (  # noqa: E402
    MATH_GATE_TIERS,
    build_audit_pack,
    shadow_events,
    to_fin_agent_judgment,
    to_math_gate,
    verify_audit_pack,
)
from loop_mvp.loop import ClosedLoop, LoopConfig  # noqa: E402
from loop_mvp.types import GateDecision  # noqa: E402

OUT_DIR = ROOT / "eval"
REPORT_MD = OUT_DIR / "interop_report.md"


def run_finance():
    periods, data, delays, dates = finance_scenario()
    return ClosedLoop(FinanceDomain(), FinanceEnvironment(periods, data, delays),
                      LoopConfig(periods=periods, business_dates=dates)).run()


def run_cutting():
    periods, data, delays, dates = cutting_scenario()
    return ClosedLoop(CuttingDomain(), CuttingEnvironment(periods, data, delays),
                      LoopConfig(periods=periods, business_dates=dates)).run()


def main() -> int:
    finance = run_finance()
    cutting = run_cutting()

    report = finance
    pack_finance = build_audit_pack(report)
    pack_cutting = build_audit_pack(cutting)
    (OUT_DIR / "audit_pack_finance.json").write_text(
        json.dumps(pack_finance, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT_DIR / "audit_pack_cutting.json").write_text(
        json.dumps(pack_cutting, ensure_ascii=False, indent=2), encoding="utf-8")

    shadow = shadow_events(finance) + shadow_events(cutting)
    (OUT_DIR / "shadow_events.jsonl").write_text(
        "\n".join(json.dumps(event, ensure_ascii=False) for event in shadow) + "\n",
        encoding="utf-8")

    # 映射自洽性：三种判定的退出码/证据级/人复核要求
    mapping_ok = True
    for verdict, tier in MATH_GATE_TIERS.items():
        item = to_math_gate(verdict)
        if item["exit_code"] != tier["exit_code"] or item["decision"] != tier["decision"]:
            mapping_ok = False

    judgment_example = to_fin_agent_judgment(
        GateDecision("REVIEW", ("AMBIGUOUS_ACTION",), "示例", 2),
        period="P01", proposal={"action": "SETTLE_TAX"},
        evidence={"source": "loop_gate"})
    judgment_ok = (
        judgment_example["signoff"]["status"] == "PENDING"
        and judgment_example["review"]["human_review_required"] is True
        and judgment_example["math_gate"]["decision"] == "REVIEW")

    verify_ok = verify_audit_pack(pack_finance) and verify_audit_pack(pack_cutting)
    tampered = json.loads(json.dumps(pack_finance))
    tampered["metrics"]["false_accept"] = 999
    tamper_detected = verify_audit_pack(tampered) is False

    ok = (verify_ok and tamper_detected and mapping_ok and judgment_ok
          and finance.metrics["false_accept"] == 0
          and cutting.metrics["false_accept"] == 0
          and finance.metrics["audit_ok"] and cutting.metrics["audit_ok"])

    lines = [
        "# 对接层报告：math-gate 四档语义 + fin-agent-platform 判断/签字结构 + 确定性审计包",
        "",
        "- 性质：**只读推演（shadow）**，不触发任何真实记账/落料/申报动作；不构成税务/审计/法律意见。",
        "- 判定映射（与 math-gate 语义对齐，判不了不放行）：",
        "",
        "| 闭环判定 | math-gate verdict | 证据级 | gate decision | exit code | 必须人复核 |",
        "|---|---|---|---|---:|---|",
    ]
    for verdict in ("ALLOW", "REVIEW", "BLOCK"):
        tier = MATH_GATE_TIERS[verdict]
        lines.append(f"| {verdict} | {tier['verdict']} | {tier['evidence']} | "
                     f"{tier['decision']} | {tier['exit_code']} | {tier['human_review_required']} |")
    lines.extend([
        "",
        "## 审计包（可校验、可定位篡改；金额一律字符串）",
        "",
        "| 域 | 内容哈希 | 校验 | 事件数 |",
        "|---|---|---|---:|",
    ])
    for name, pack, report_obj in (("finance_close", pack_finance, finance),
                                   ("cutting_yield", pack_cutting, cutting)):
        events = sum(len(episode["steps"]) for episode in pack["episodes"])
        lines.append(f"| {name} | `{pack['content_hash'][:16]}…` | "
                     f"{'PASS' if verify_audit_pack(pack) else 'FAIL'} | {events} |")
    lines.extend([
        "",
        f"- 篡改检测：{'PASS（改一个数字即校验失败）' if tamper_detected else 'FAIL'}",
        f"- 判断/签字结构：四眼复核 PENDING、人签字占位（工具不代签）："
        f"{'PASS' if judgment_ok else 'FAIL'}",
        f"- shadow 事件流：{len(shadow)} 条（`eval/shadow_events.jsonl`，executed=false）",
        "",
        "## 输出",
        "",
        "- `eval/audit_pack_finance.json` / `eval/audit_pack_cutting.json`：确定性审计包；",
        "- `eval/shadow_events.jsonl`：shadow 事件流（一行一个 JSON）；",
        "",
        "## 诚实边界",
        "",
        "- 映射是**语义对齐**，不是把 math-gate 的符号引擎接进来重算；数值正确性仍由本闭环的确定性内核保证；",
        "- 审计包只含指标/事件/残差与免责声明，不含客户隐私数据；",
        "- 任何 REVIEW/BLOCK 均要求人工复核；对外执行以人签字为准。",
    ])
    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"interop: finance_hash={pack_finance['content_hash'][:12]} "
          f"cutting_hash={pack_cutting['content_hash'][:12]} "
          f"verify={verify_ok} tamper_detected={tamper_detected} "
          f"mapping_ok={mapping_ok} judgment_ok={judgment_ok} events={len(shadow)}")
    print(f"report: {REPORT_MD.relative_to(ROOT)}")
    print("result:", "PASS" if ok else "FAIL")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
