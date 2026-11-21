"""MPC 演示：同一份数据、同一套内核，只换"代价顺序"，决策随之改变。

用来证明：MPC 的风险偏好是**显式配置**，不是写死在代码里。

用法：``python tools/mpc_demo.py``
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from loop_mvp.cutting import CuttingDomain, CuttingEnvironment, mpc_scenario  # noqa: E402
from loop_mvp.loop import ClosedLoop, LoopConfig  # noqa: E402

ORDERS = {
    "交付优先": ("compliance", "fulfillment", "waste"),
    "材料优先": ("compliance", "waste", "fulfillment"),
}


def run(order_name: str, objectives) -> "object":
    periods, data, delays, dates = mpc_scenario()
    env = CuttingEnvironment(periods, data, delays)
    loop = ClosedLoop(CuttingDomain(), env, LoopConfig(
        periods=periods, business_dates=dates, mpc_objectives=tuple(objectives)))
    report = loop.run()
    print(f"[{order_name}] objectives={'>'.join(report.metrics['mpc_objectives'])}")
    for period in report.periods:
        print(f"  {period.period}: chosen={report.metrics['mpc_choices'].get(period.period)} "
              f"closed={period.closed} residual="
              f"{'n/a' if period.residual is None else ('in' if period.residual.in_bound else 'OUT')}")
        for note in period.notes:
            if note.startswith("MPC"):
                print("    " + note)
    print(f"  metrics: closed={report.metrics['closed_periods']} "
          f"residual_flagged={report.metrics['residual_flagged']} "
          f"coverage={report.metrics['coverage']} "
          f"false_accept={report.metrics['false_accept']} "
          f"audit_ok={report.metrics['audit_ok']} replay_ok={report.metrics['replay_ok']}")
    return report


def main() -> int:
    reports = {name: run(name, order) for name, order in ORDERS.items()}
    fulfill = reports["交付优先"]
    material = reports["材料优先"]
    p02_fulfill = fulfill.metrics["mpc_choices"].get("P02")
    p02_material = material.metrics["mpc_choices"].get("P02")
    print()
    print(f"P02 决策差异：交付优先={p02_fulfill} vs 材料优先={p02_material}")
    ok = (
        p02_fulfill != p02_material
        and all(report.metrics["closed_periods"] == 3 for report in reports.values())
        and all(report.metrics["false_accept"] == 0 for report in reports.values())
        and all(report.metrics["audit_ok"] and report.metrics["replay_ok"]
                for report in reports.values())
    )
    print("result:", "PASS（换代价顺序 → 换决策；合规/审计/回放均不破）" if ok else "FAIL")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
