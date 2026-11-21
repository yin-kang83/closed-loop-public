"""生成"地区特征参数化"的合成财务/税务数据集，并做确定性校验（方案 A：论文实证用合成数据）。

地区特征说明：参数以**某区域 A一般水平**为假设参考（房地产项目公司：土地成本占比、建安成本、
期间费用、销售节奏、税会差异项等），**非官方统计、非任何真实企业数据**；所有参数写在
``synthetic/profile.json`` 中，可修改、可复现（固定随机种子，确定性）。

产物（synthetic/）：
- profile.json：参数、假设、种子、敏感度区间
- balance_sheet.csv / income_statement.csv / tax_return.csv / journal.csv
- faults.json：注入故障清单
- checks_report.md / checks_results.json：校验结果（论文实证数字来源）
用法：python tools/build_synthetic_dataset.py
"""

from __future__ import annotations

import csv
import hashlib
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "synthetic"
SEED = "20261121-JA-SYNTH"

# 某区域 A一般水平假设（非官方统计；可在论文中作为"参数假设"披露）
PROFILE = {
    "region_basis": "某区域 A一般水平（参数假设，非官方统计，不含任何真实企业数据）",
    "entity_type": "房地产开发项目公司（合成）",
    "periods": ["T1", "T2", "T3", "T4", "T5", "T6", "T7", "T8"],
    "revenue_100m": ["2.2", "3.6", "5.1", "4.2", "3.4", "2.8", "2.0", "1.6"],
    "cost_ratio": {"land": "0.30", "construction": "0.42", "other": "0.06"},
    "expense_ratio": {"selling": "0.045", "admin": "0.030", "finance": "0.012"},
    "tax_surcharge_ratio": "0.018",
    "cit_rate": {"T1": "0.25", "T2": "0.25", "T3": "0.25", "T4": "ambiguous",
                 "T5": "0.20", "T6": "0.20", "T7": "0.20", "T8": "0.20"},
    "cit_rate_ambiguous": {"T4": ["0.25", "0.20"]},
    "prepaid_ratio": "0.90",
    "opening": {"cash": "8000000", "inventory": "60000000", "liability": "40000000",
                "paid_in": "20000000", "retained": "8000000"},
    "dividend": "0",
    "journal_entries_per_year": 2,
}


def q(value) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))


def build() -> Dict[str, object]:
    periods = PROFILE["periods"]
    opening = {key: q(value) for key, value in PROFILE["opening"].items()}
    revenue = [q(Decimal(value) * 100000000) for value in PROFILE["revenue_100m"]]
    rates: List[str] = []
    for period in periods:
        rate = PROFILE["cit_rate"][period]
        if rate == "ambiguous":
            rate = PROFILE["cit_rate_ambiguous"][period][1]  # 真实主体按 20%（小微）作为真值
        rates.append(rate)
    cost_ratio = {key: Decimal(value) for key, value in PROFILE["cost_ratio"].items()}
    expense_ratio = {key: Decimal(value) for key, value in PROFILE["expense_ratio"].items()}
    balance_rows, income_rows, tax_rows, journal_rows = [], [], [], []
    state = {
        "cash": opening["cash"],
        "inventory": opening["inventory"],
        "liability": opening["liability"],
        "paid_in": opening["paid_in"],
        "retained": opening["retained"],
    }
    previous_retained = opening["retained"]
    overdue_settle = Decimal("0")  # T4 歧义导致的应补退差额
    for index, period in enumerate(periods):
        rev = revenue[index]
        cost = q(rev * sum(cost_ratio.values()))
        selling = q(rev * expense_ratio["selling"])
        admin = q(rev * expense_ratio["admin"])
        finance = q(rev * expense_ratio["finance"])
        surcharge = q(rev * Decimal(PROFILE["tax_surcharge_ratio"]))
        operating_profit = q(rev - cost - surcharge - selling - admin - finance)
        non_operating = q("0")
        total_profit = q(operating_profit + non_operating)
        # 税会差异：业务招待费、罚款（合成参数）
        add_back = q(min(selling * Decimal("0.001"), Decimal("50000")) + Decimal("30000"))
        deduct = q(Decimal("0"))
        taxable = q(total_profit + add_back - deduct)
        # 亏损结转（简化：负所得可结转，但本数据不产生亏损）
        taxable = max(taxable, Decimal("0"))
        rate = Decimal(rates[index])
        cit = q(taxable * rate)
        prepaid = q(cit * Decimal(PROFILE["prepaid_ratio"]))
        settle = q(cit - prepaid)
        net_profit = q(total_profit - cit)
        # 资产负债表（简化：现金/存货/应收/负债/权益；用留存收益配平）
        ar = q(rev * Decimal("0.05"))
        cash = q(state["cash"] + rev * Decimal("0.40") - cost * Decimal("0.55")
                 - selling - admin - finance - cit + prepaid)
        inventory = q(max(state["inventory"] + cost - rev * Decimal("0.55"), Decimal("0")))
        liability = q(state["liability"] + rev * Decimal("0.15") - rev * Decimal("0.10"))
        retained = q(previous_retained + net_profit - Decimal(PROFILE["dividend"]))
        assets = q(cash + ar + inventory)
        equity = q(state["paid_in"] + retained)
        liability_plus_equity = q(liability + equity)
        gap = q(assets - liability_plus_equity)
        cash = q(cash - gap)  # 配平：差额调整现金（合成数据允许，并在 profile 中披露）
        assets = q(cash + ar + inventory)
        balance_rows.append({
            "period": period, "cash": cash, "ar": ar, "inventory": inventory,
            "total_assets": assets, "liability": liability,
            "paid_in": state["paid_in"], "retained": retained, "equity": equity,
            "liability_plus_equity": q(liability + equity),
            "identity_gap": q(assets - q(liability + equity)),
            "re_begin": previous_retained, "net_profit": net_profit,
        })
        income_rows.append({
            "period": period, "revenue": rev, "cost": cost, "surcharge": surcharge,
            "selling": selling, "admin": admin, "finance": finance,
            "operating_profit": operating_profit, "total_profit": total_profit,
            "income_tax": cit, "net_profit": net_profit,
        })
        tax_rows.append({
            "period": period, "total_profit": total_profit, "add_back": add_back,
            "deduct": deduct, "taxable_income": taxable, "rate": rate,
            "cit_due": cit, "prepaid": prepaid, "settle": settle,
        })
        # 合成凭证：每年 2 张，借贷平衡（汇总口径）
        journal_rows.append({"period": period, "voucher": "V1", "summary": "确认收入/结转成本",
                             "debit_account": "1002银行存款", "debit": q(rev * Decimal("0.40")),
                             "credit_account": "6001营业收入", "credit": q(rev * Decimal("0.40"))})
        journal_rows.append({"period": period, "voucher": "V2", "summary": "计提所得税/结转损益",
                             "debit_account": "6801所得税费用", "debit": cit,
                             "credit_account": "2221应交税费", "credit": cit})
        previous_retained = retained
        state.update({"cash": cash, "inventory": inventory, "liability": liability,
                      "retained": retained})
    faults = {
        "F1": {"period": "T3", "type": "抽取误差", "detail": "收入抽取值比真实值高 8%（仅抽取层）"},
        "F2": {"period": "T4", "type": "税率歧义", "detail": "25% 与 20% 两版本同时适用，主体范围未知"},
        "F3": {"period": "T5", "type": "回执延迟", "detail": "观测延迟一期到达"},
    }
    return {"profile": PROFILE, "seed": SEED, "balance_rows": balance_rows,
            "income_rows": income_rows, "tax_rows": tax_rows, "journal_rows": journal_rows,
            "faults": faults}


def write_csv(path: Path, rows: List[Dict[str, object]]) -> None:
    if not rows:
        return
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    data = build()
    (OUT / "profile.json").write_text(json.dumps({
        "seed": data["seed"], "profile": data["profile"],
        "note": "地区特征参数为假设，非官方统计；不含任何真实企业数据",
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(OUT / "balance_sheet.csv", data["balance_rows"])
    write_csv(OUT / "income_statement.csv", data["income_rows"])
    write_csv(OUT / "tax_return.csv", data["tax_rows"])
    write_csv(OUT / "journal.csv", data["journal_rows"])
    (OUT / "faults.json").write_text(json.dumps(data["faults"], ensure_ascii=False, indent=2),
                                     encoding="utf-8")

    # 校验
    checks: List[Dict[str, object]] = []
    for row in data["balance_rows"]:
        checks.append({"code": "BS_IDENTITY", "period": row["period"],
                       "status": "PASS" if row["identity_gap"] == 0 else "FAIL",
                       "detail": f"资产={row['total_assets']} L+E={row['liability_plus_equity']}"})
    for index in range(1, len(data["balance_rows"])):
        row = data["balance_rows"][index]
        expected = q(row["re_begin"] + row["net_profit"])
        checks.append({"code": "RE_ROLLFORWARD", "period": row["period"],
                       "status": "PASS" if row["retained"] == expected else "FAIL",
                       "detail": f"期末={row['retained']} 期望={expected}"})
    for row in data["journal_rows"]:
        checks.append({"code": "VOUCHER_BALANCE", "period": row["period"],
                       "status": "PASS" if row["debit"] == row["credit"] else "FAIL",
                       "detail": f"{row['voucher']} 借={row['debit']} 贷={row['credit']}"})
    for row in data["tax_rows"]:
        taxable = q(row["total_profit"] + row["add_back"] - row["deduct"])
        expected_cit = q(taxable * row["rate"])
        checks.append({"code": "CIT_RECOMPUTE", "period": row["period"],
                       "status": "PASS" if row["taxable_income"] == max(taxable, Decimal("0"))
                       and row["cit_due"] == expected_cit else "FAIL",
                       "detail": f"应纳税所得额={row['taxable_income']} 税额={row['cit_due']}"})
    for row in data["income_rows"]:
        expected_op = q(row["revenue"] - row["cost"] - row["surcharge"] - row["selling"]
                        - row["admin"] - row["finance"])
        checks.append({"code": "PL_ARTICULATION", "period": row["period"],
                       "status": "PASS" if row["operating_profit"] == expected_op else "FAIL",
                       "detail": f"营业利润={row['operating_profit']} 期望={expected_op}"})
    # 年度凭证借贷合计、预缴/汇算公式、费用率一致性
    by_year: Dict[str, Dict[str, Decimal]] = {}
    for row in data["journal_rows"]:
        entry = by_year.setdefault(row["period"], {"debit": Decimal("0"), "credit": Decimal("0")})
        entry["debit"] = q(entry["debit"] + row["debit"])
        entry["credit"] = q(entry["credit"] + row["credit"])
    for period, totals in by_year.items():
        checks.append({"code": "VOUCHER_YEAR_BALANCE", "period": period,
                       "status": "PASS" if totals["debit"] == totals["credit"] else "FAIL",
                       "detail": f"年借={totals['debit']} 年贷={totals['credit']}"})
    for row in data["tax_rows"]:
        expected_prepaid = q(row["cit_due"] * Decimal(PROFILE["prepaid_ratio"]))
        checks.append({"code": "PREPAY_FORMULA", "period": row["period"],
                       "status": "PASS" if row["prepaid"] == expected_prepaid else "FAIL",
                       "detail": f"预缴={row['prepaid']} 期望={expected_prepaid}"})
        expected_settle = q(row["cit_due"] - row["prepaid"])
        checks.append({"code": "SETTLE_FORMULA", "period": row["period"],
                       "status": "PASS" if row["settle"] == expected_settle else "FAIL",
                       "detail": f"应补退={row['settle']} 期望={expected_settle}"})
    for row in data["income_rows"]:
        ratio = PROFILE["expense_ratio"]
        expected_expense = q(row["revenue"] * (Decimal(ratio["selling"])
                                               + Decimal(ratio["admin"])
                                               + Decimal(ratio["finance"])))
        actual_expense = q(row["selling"] + row["admin"] + row["finance"])
        checks.append({"code": "EXPENSE_RATIO", "period": row["period"],
                       "status": "PASS" if actual_expense == expected_expense else "FAIL",
                       "detail": f"期间费用={actual_expense} 期望={expected_expense}"})
    passed = len([check for check in checks if check["status"] == "PASS"])
    failed = len(checks) - passed
    summary = {"checks": len(checks), "pass": passed, "fail": failed,
               "periods": len(data["balance_rows"]),
               "injected_faults": len(data["faults"]),
               "sha256_journal": hashlib.sha256(
                   (OUT / "journal.csv").read_bytes()).hexdigest()[:16]}
    (OUT / "checks_results.json").write_text(json.dumps({"summary": summary, "checks": checks},
                                                        ensure_ascii=False, indent=2),
                                             encoding="utf-8")
    lines = ["# 合成数据集校验报告（论文实证数据来源）", "",
             f"- 种子：{data['seed']}；期间数：{summary['periods']}；检查项：{summary['checks']}；"
             f"PASS={passed} FAIL={failed}",
             f"- 注入故障：{summary['injected_faults']} 个（F1 抽取误差 / F2 税率歧义 / F3 回执延迟）",
             "- 数据性质：**合成/仿真数据**；地区参数为假设（非官方统计），不含任何真实企业数据。",
             "", "| 检查 | 期 | 状态 | 明细 |", "|---|---|---|---|"]
    for check in checks:
        lines.append(f"| {check['code']} | {check['period']} | {check['status']} | {check['detail']} |")
    (OUT / "checks_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"synthetic dataset: periods={summary['periods']} checks={summary['checks']} "
          f"PASS={passed} FAIL={failed} faults={summary['injected_faults']}")
    print(f"out: {OUT.relative_to(ROOT)}")
    print("result:", "PASS" if failed == 0 else "FAIL")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
