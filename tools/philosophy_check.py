"""第一原则护栏的可执行检查（机器可验证，CI 可接）。

检查项（全部来自 README §0 / `legalwiki-kb/docs/哲学基础.md` / 本仓库实现）：
1. 法定税负不可优化：`tax` 等目标被 `validate_objectives` 拒绝；
2. 候选白名单：每条候选必须有官方依据；违规示例必须被闸门识别；
3. 法定税负下界：低于法定应纳税额 → TAX_BELOW_STATUTORY；预缴方式未核定 → METHOD_NOT_APPROVED；
4. 涉税输出免责声明：相关模块/工具必须含"不构成税务意见"；
5. README §0 拒绝清单/第一原则存在；
6. 静态扫描：禁止出现 `minimize_tax` / `optimize_tax` / `avoid_tax` / `plan_tax` 之类的函数/类名；
7. 静态扫描：MPC 目标列表不得包含 `tax`（优化目标白名单）。

用法：``python tools/philosophy_check.py``；退出码 0=全部通过，2=有失败。
产物：``eval/philosophy_check.md``（可作论文/审计证据）。
"""

from __future__ import annotations

import ast
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from loop_mvp.tax_planning import (  # noqa: E402
    LEGAL_BASIS,
    OPTIMIZABLE_OBJECTIVES,
    OverpaymentDecision,
    PrepaymentMethodDecision,
    validate_objectives,
)
from loop_mvp.types import dec  # noqa: E402

OUT = ROOT / "synthetic" / "philosophy_check.md"

FORBIDDEN_NAME = re.compile(
    r"(?i)(?:minimi[sz]e|optimi[sz]e|avoid|evade|plan|reduc)\w*tax"
    r"|tax\w*(?:minimi[sz]e|optimi[sz]e|avoid|evade|plan|reduc)"
)

DISCLAIMER = "不构成税务意见"
DISCLAIMER_FILES = (
    "loop_mvp/tax_planning.py",
    "loop_mvp/tax_compliance.py",
    "loop_mvp/tax_checks.py",
    "loop_mvp/local_taxes.py",
)
SCAN_DIRS = ("loop_mvp", "tools")
EXCLUDE_FILES = {"tools/philosophy_check.py"}


def _list_py_files() -> List[Path]:
    files: List[Path] = []
    for directory in SCAN_DIRS:
        files.extend(sorted((ROOT / directory).rglob("*.py")))
    for name in ("demo.py",):
        path = ROOT / name
        if path.exists():
            files.append(path)
    return [path for path in files
            if path.relative_to(ROOT).as_posix() not in EXCLUDE_FILES]


def _check_tax_not_optimizable() -> Tuple[bool, str]:
    rejected = []
    for bad in (("tax",), ("compliance", "tax"), ("minimize_tax",), ("tax_burden",)):
        try:
            validate_objectives(bad)
        except ValueError:
            rejected.append(True)
        else:
            rejected.append(False)
    try:
        validate_objectives(("compliance", "cash"))
        allowed = True
    except ValueError:
        allowed = False
    ok = all(rejected) and allowed
    return ok, f"allow={list(OPTIMIZABLE_OBJECTIVES)}; rejected={sum(rejected)}/{len(rejected)}; allowed_pass={allowed}"


def _check_candidates_legal_basis() -> Tuple[bool, str]:
    problems: List[str] = []
    over = OverpaymentDecision(overpayment=dec("1000"), next_year_tax_due=dec("500"))
    for label, scores, basis in over.candidates():
        if not basis:
            problems.append(f"无依据: {label}")
        if "违规" in label and not over.compliance_violations(scores):
            problems.append(f"违规示例未被识别: {label}")
    prepay = PrepaymentMethodDecision(
        annual_tax_due=dec("1000"), actual_profit_prepaid=dec("900"),
        average_prior_year_prepaid=dec("1100"), other_method_approved=False)
    for label, scores, basis in prepay.candidates():
        if not basis:
            problems.append(f"无依据: {label}")
    approved = prepay.compliance_violations(
        {"prepaid": dec("1100"), "tax": dec("1000"), "approved": dec("1")})
    if "METHOD_NOT_APPROVED" not in approved:
        problems.append("未核定预缴方式未被识别")
    ok = not problems and all(bool(value) for value in LEGAL_BASIS.values())
    return ok, f"candidates_ok={not problems}; legal_basis_entries={len(LEGAL_BASIS)}" + (
        f"; problems={problems}" if problems else "")


def _check_tax_floor() -> Tuple[bool, str]:
    over = OverpaymentDecision(overpayment=dec("1000"), next_year_tax_due=dec("500"))
    below = over.compliance_violations({"refund": dec("0"), "offset": dec("0"), "tax": dec("499")})
    statutory = over.compliance_violations({"refund": dec("0"), "offset": dec("0"), "tax": dec("500")})
    ok = "TAX_BELOW_STATUTORY" in below and "TAX_BELOW_STATUTORY" not in statutory
    return ok, f"below={list(below)}; statutory={list(statutory)}"


def _check_disclaimers() -> Tuple[bool, str]:
    missing = []
    for relative in DISCLAIMER_FILES:
        path = ROOT / relative
        if not path.exists():
            missing.append(f"{relative}(missing)")
            continue
        if DISCLAIMER not in path.read_text(encoding="utf-8"):
            missing.append(relative)
    return not missing, f"checked={len(DISCLAIMER_FILES)}; missing={missing}"


def _check_readme() -> Tuple[bool, str]:
    path = ROOT / "README.md"
    if not path.exists():
        return False, "README.md missing"
    text = path.read_text(encoding="utf-8")
    keywords = ("第一原则", "拒绝清单", "不能成为违法乱纪者的帮凶", "不做税务筹划")
    missing = [keyword for keyword in keywords if keyword not in text]
    return not missing, f"keywords_missing={missing}"


def _check_forbidden_names() -> Tuple[bool, str]:
    hits: List[str] = []
    for path in _list_py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if FORBIDDEN_NAME.search(node.name):
                    hits.append(f"{path.relative_to(ROOT).as_posix()}::{node.name}")
    return not hits, f"hits={hits}"


def _tuple_has_tax(node: ast.AST) -> bool:
    if isinstance(node, (ast.Tuple, ast.List)):
        return any(isinstance(elt, ast.Constant) and elt.value == "tax" for elt in node.elts)
    return False


def _check_objectives_static() -> Tuple[bool, str]:
    hits: List[str] = []
    for path in _list_py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for keyword in node.keywords:
                    if keyword.arg == "mpc_objectives" and _tuple_has_tax(keyword.value):
                        hits.append(f"{path.relative_to(ROOT).as_posix()}:mpc_objectives")
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and "OBJECTIVES" in target.id.upper():
                        if _tuple_has_tax(node.value):
                            hits.append(f"{path.relative_to(ROOT).as_posix()}:{target.id}")
    return not hits, f"hits={hits}"


CHECKS = (
    ("法定税负不可优化", _check_tax_not_optimizable),
    ("候选白名单均有官方依据", _check_candidates_legal_basis),
    ("法定税负下界校验", _check_tax_floor),
    ("涉税输出免责声明", _check_disclaimers),
    ("README §0 拒绝清单", _check_readme),
    ("禁止优化税负的函数/类名", _check_forbidden_names),
    ("MPC 目标不得含 tax", _check_objectives_static),
)


def run_checks() -> List[Dict[str, str]]:
    results = []
    for name, function in CHECKS:
        ok, detail = function()
        results.append({"check": name, "ok": "PASS" if ok else "FAIL", "detail": detail})
    return results


def main() -> int:
    results = run_checks()
    failed = [item for item in results if item["ok"] == "FAIL"]
    lines = [
        "# 第一原则护栏检查（机器可验证）",
        "",
        f"- 生成时间：{datetime.now().isoformat(timespec='seconds')}",
        "- 性质：静态/动态检查，不判断业务正确性；只验证『不做违法乱纪帮凶』的工程约束是否在位。",
        "- 依据：README §0、`legalwiki-kb/docs/哲学基础.md`、`loop_mvp/tax_planning.py` 硬边界。",
        "",
        "| 检查 | 结果 | 细节 |",
        "|---|---|---|",
    ]
    for item in results:
        lines.append(f"| {item['check']} | {item['ok']} | {item['detail']} |")
    lines.extend(["", f"结论：{'全部通过' if not failed else '存在失败项'}（{len(results) - len(failed)}/{len(results)} PASS）"])
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")

    for item in results:
        print(f"[{item['ok']}] {item['check']}: {item['detail']}")
    print(f"report: {OUT.relative_to(ROOT)}")
    print("result:", "PASS" if not failed else "FAIL")
    return 0 if not failed else 2


if __name__ == "__main__":
    raise SystemExit(main())
