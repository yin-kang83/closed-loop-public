"""演示：同一闭环内核跑两个域（财务结账 / 板材下料）。

运行：``python demo.py``
- 控制台只输出 ASCII 摘要（Windows 控制台 GBK 下不乱码）；
- 完整报告（含中文备注）写入 ``demo_report.txt``（UTF-8）；
- 退出码 0 = 两域闭环、审计/回放通过、0 误放行。
"""

from __future__ import annotations

import sys
from pathlib import Path

from loop_mvp.cutting import CuttingDomain, CuttingEnvironment
from loop_mvp.cutting import default_scenario as cutting_scenario
from loop_mvp.finance import FinanceDomain, FinanceEnvironment
from loop_mvp.finance import default_scenario as finance_scenario
from loop_mvp.loop import ClosedLoop, LoopConfig

REPORT_PATH = Path(__file__).resolve().parent / "demo_report.txt"

COMPARE_KEYS = [
    "periods", "closed_periods", "executed_actions", "allow", "review_events",
    "confirmations", "block", "replans", "reuse_valid", "residual_checks",
    "residual_flagged", "coverage", "hypothesis_shrinks", "delayed_scheduled",
    "delayed_settlements", "model_errors", "unexplained_in_bound",
    "corrections_applied", "false_accept", "audit_ok", "replay_ok",
    "ledger_journal_ok",
]


def run_finance():
    periods, data, delays, dates = finance_scenario()
    env = FinanceEnvironment(periods, data, delays)
    return ClosedLoop(FinanceDomain(), env,
                      LoopConfig(periods=periods, business_dates=dates)).run()


def run_cutting():
    periods, data, delays, dates = cutting_scenario()
    env = CuttingEnvironment(periods, data, delays)
    return ClosedLoop(CuttingDomain(), env,
                      LoopConfig(periods=periods, business_dates=dates)).run()


def main() -> int:
    finance = run_finance()
    cutting = run_cutting()

    lines = []
    lines.append("=" * 82)
    lines.append("域 1：财务结账（会计恒等式 + 税率版本歧义 + 抽取错误 + 回执延迟）")
    lines.append("=" * 82)
    lines.append(finance.summary())
    lines.append("")
    for period in finance.periods:
        lines.append(f"{period.summary()}")
        lines.extend(f"    - {note}" for note in period.notes)
    lines.append("")
    lines.append("=" * 82)
    lines.append("域 2：板材下料（物料守恒 + 切缝假设 + 文档板长写错）")
    lines.append("=" * 82)
    lines.append(cutting.summary())
    lines.append("")
    for period in cutting.periods:
        lines.append(f"{period.summary()}")
        lines.extend(f"    - {note}" for note in period.notes)
    lines.append("")
    lines.append("=" * 82)
    lines.append("跨域内核指标（同一套 gate / estimator / memory / ledger / loop）")
    lines.append("=" * 82)
    lines.append(f"{'metric':<24}{'finance_close':>18}{'cutting_yield':>18}")
    for key in COMPARE_KEYS:
        lines.append(f"{key:<24}{str(finance.metrics.get(key)):>18}{str(cutting.metrics.get(key)):>18}")

    ok = (
        finance.metrics["audit_ok"] and finance.metrics["replay_ok"]
        and cutting.metrics["audit_ok"] and cutting.metrics["replay_ok"]
        and finance.metrics["false_accept"] == 0
        and cutting.metrics["false_accept"] == 0
        and finance.metrics["closed_periods"] >= 5
        and cutting.metrics["closed_periods"] == 3
    )
    lines.append("")
    lines.append("结论：" + ("PASS（两域闭环、审计与回放校验通过、0 误放行）" if ok else "FAIL"))
    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("closed-loop MVP: finance_close + cutting_yield")
    for name, report in (("finance_close", finance), ("cutting_yield", cutting)):
        print(f"  [{name}]")
        for period in report.periods:
            print("    " + period.summary())
    print()
    print(f"{'metric':<24}{'finance_close':>18}{'cutting_yield':>18}")
    for key in COMPARE_KEYS:
        print(f"{key:<24}{str(finance.metrics.get(key)):>18}{str(cutting.metrics.get(key)):>18}")
    print()
    print("result:", "PASS" if ok else "FAIL", "| full report:", REPORT_PATH.name)
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
