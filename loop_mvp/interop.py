"""与既有资产（math-gate / fin-agent-platform）的对接层：判定语义映射 + 审计包。

只做**结构映射**，不改变闭环判定本身：
- 与 math-gate 的四档语义对齐（判不了不放行）：ALLOW→PASS/L1_EXACT，REVIEW→UNKNOWN/L3_UNKNOWN，
  BLOCK→FAIL_EQUIVALENCE/R_REFUTED；退出码 0/3/2 与 math-gate CLI 约定一致，便于接 CI 旁路；
- 与 fin-agent-platform 的「判断层（提议+证据+四眼复核）→ 签字层（人签字负责）→ 审计包」结构对齐：
  每个闸门事件输出 proposal/evidence/human_review_required/signoff 占位；
- 审计包：确定性 JSON（金额一律字符串）+ 内容哈希；可校验、可定位篡改；
- **底线**：任何 REVIEW/BLOCK 都必须人复核；不把"通过"当"绝对正确"，不替人做签字决定。
"""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256
from typing import Any, Dict, Iterable, List, Mapping, Optional, Union

from .types import GateDecision, canonical_json, fingerprint

AUDIT_PACK_SCHEMA = "closed-loop-audit-pack/v1"

# 判定语义对齐：verdict / evidence / gate decision / exit code / 是否必须人复核
MATH_GATE_TIERS: Mapping[str, Mapping[str, Any]] = {
    "ALLOW": {"verdict": "PASS", "evidence": "L1_EXACT", "decision": "ALLOW",
              "exit_code": 0, "human_review_required": False},
    "REVIEW": {"verdict": "UNKNOWN", "evidence": "L3_UNKNOWN", "decision": "REVIEW",
               "exit_code": 3, "human_review_required": True},
    "BLOCK": {"verdict": "FAIL_EQUIVALENCE", "evidence": "R_REFUTED", "decision": "BLOCK",
              "exit_code": 2, "human_review_required": True},
}

DISCLAIMER = (
    "本文件只做确定性复算、证据留痕与判定语义映射；不构成税务意见/审计意见/法律意见；"
    "REVIEW/BLOCK 必须人工复核；任何对外执行以人签字为准；不做税务筹划/避税建议。"
)


def _verdict_of(decision: Union[GateDecision, str]) -> str:
    verdict = decision.verdict if isinstance(decision, GateDecision) else str(decision)
    if verdict not in MATH_GATE_TIERS:
        raise ValueError(f"未知判定: {verdict!r}")
    return verdict


def to_math_gate(decision: Union[GateDecision, str]) -> Dict[str, Any]:
    """把闭环闸门判定映射为 math-gate 四档语义（纯映射，不重算）。"""
    verdict = _verdict_of(decision)
    tier = dict(MATH_GATE_TIERS[verdict])
    tier["source_verdict"] = verdict
    if isinstance(decision, GateDecision):
        tier["codes"] = list(decision.codes)
        tier["detail"] = decision.detail
        tier["checked_hypotheses"] = decision.checked_hypotheses
    return tier


def to_fin_agent_judgment(
    decision: GateDecision,
    *,
    period: str,
    proposal: Mapping[str, Any],
    evidence: Mapping[str, Any],
    policy_requires_signoff: bool = True,
) -> Dict[str, Any]:
    """映射为 fin-agent-platform 的判断/签字结构：提议 + 证据 + 四眼 + 人签字占位。"""
    verdict = _verdict_of(decision)
    math_gate = to_math_gate(decision)
    human_required = bool(math_gate["human_review_required"]) or policy_requires_signoff
    return {
        "period": period,
        "decision": verdict,
        "proposal": dict(proposal),
        "evidence": dict(evidence),
        "review": {
            "four_eyes_required": human_required,
            "four_eyes_status": "PENDING" if human_required else "NOT_REQUIRED",
            "human_review_required": human_required,
        },
        "signoff": {
            "required": human_required,
            "status": "PENDING" if human_required else "NOT_REQUIRED",
            "signed_by": None,       # 人签字，工具不代签
            "signed_at": None,
            "content_hash": None,
        },
        "math_gate": math_gate,
        "disclaimer": DISCLAIMER,
    }


def _jsonable(value: Any) -> Any:
    from decimal import Decimal

    if isinstance(value, Decimal):
        return format(value.normalize(), "f") if value != 0 else "0"
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def build_audit_pack(
    report: Any,
    *,
    sources: Optional[Mapping[str, str]] = None,
    generated_at: Optional[str] = None,
    audit_chain: Optional[Iterable[Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    """由 LoopReport 构建确定性审计包（含内容哈希；不包含任何隐私数据）。"""
    episodes: List[Dict[str, Any]] = []
    for period in report.periods:
        steps = []
        for step in period.steps:
            math_gate = to_math_gate(step.verdict)
            judgment = to_fin_agent_judgment(
                GateDecision(step.verdict, tuple(step.codes), "",
                             len(period.steps)),
                period=period.period,
                proposal={"step": step.step, "action": step.action,
                          "hyp_id": step.hyp_id},
                evidence={"codes": list(step.codes), "executed": step.executed},
            )
            steps.append({
                "step": step.step,
                "action": step.action,
                "verdict": step.verdict,
                "codes": list(step.codes),
                "executed": step.executed,
                "hyp_id": step.hyp_id,
                "math_gate": math_gate,
                "judgment": judgment,
            })
        residual = None
        if period.residual is not None:
            residual = {
                "in_bound": period.residual.in_bound,
                "rows": [
                    {"key": row.key, "interval": row.interval.to_dict(),
                     "observed": format(row.observed, "f"),
                     "delta": format(row.delta, "f"), "in_bound": row.in_bound}
                    for row in period.residual.rows
                ],
            }
        episodes.append({
            "period": period.period,
            "closed": period.closed,
            "blocked": period.blocked,
            "escalated": period.escalated,
            "reuse_mode": period.reuse_mode,
            "hypotheses_before": period.hypotheses_before,
            "hypotheses_after": period.hypotheses_after,
            "steps": steps,
            "residual": residual,
            "notes": list(period.notes),
        })
    pack: Dict[str, Any] = {
        "schema": AUDIT_PACK_SCHEMA,
        "domain": report.domain,
        "generated_at": generated_at or datetime.now().isoformat(timespec="seconds"),
        "sources": dict(sources or {}),
        "metrics": _jsonable(report.metrics),
        "episodes": episodes,
        "audit_chain": _jsonable(list(audit_chain or [])),
        "disclaimer": DISCLAIMER,
        "philosophy": {
            "first_principle": "不能成为违法乱纪者的帮凶（不做逃税/造假/规避监管）",
            "human_final_decision": True,
            "no_tax_planning": True,
        },
    }
    pack["content_hash"] = sha256_hex(canonical_json(pack))
    return pack


def verify_audit_pack(pack: Mapping[str, Any]) -> bool:
    """重算内容哈希，校验审计包是否被篡改。"""
    body = {key: value for key, value in pack.items() if key != "content_hash"}
    expected = sha256_hex(canonical_json(body))
    return pack.get("content_hash") == expected


def sha256_hex(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


def shadow_events(report: Any) -> List[Dict[str, Any]]:
    """shadow 模式事件流（只读推演，不产生真实副作用）：一行一个 JSON 对象。"""
    events: List[Dict[str, Any]] = []
    for period in report.periods:
        for step in period.steps:
            events.append({
                "schema": AUDIT_PACK_SCHEMA,
                "mode": "shadow",
                "domain": report.domain,
                "period": period.period,
                "action": step.action,
                "source_verdict": step.verdict,
                "math_gate": to_math_gate(step.verdict),
                "executed": False,
            })
    return events
