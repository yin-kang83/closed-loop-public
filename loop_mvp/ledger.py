"""双时态事实账本 + 可回放执行日志（域无关，内核的一部分）。

两本账：

1. **事实账**：只追加、带哈希链；同一业务期同键改值走 ``supersedes``；
   查询按"业务时点 + 已知时点"双时态过滤（当时是否已知）。
2. **执行账**：记录每个动作与观测修正，可按域转移函数完整回放并校验
   ``state_after_hash``；这样"闭眼推演—睁眼修正"的每一步都可审计。

期标签统一用 ``P01``、``P02`` …（零填充，保证字符串序 = 时间序）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .domain import Domain
from .types import (
    ActionOp,
    FactBundle,
    FactRecord,
    Observation,
    StateSnapshot,
    ZERO,
    canonical_json,
    fingerprint,
    q,
    sha256_hex,
)

_LABEL_RE = re.compile(r"^(P\d{2,}|20\d{2})$")


def _check_label(label: str) -> str:
    """期标签：P01/P02…（月）或 2019/2020…（年）；字符串序=时间序。"""
    if not _LABEL_RE.match(label):
        raise ValueError(f"期标签必须形如 P01/P02 或 2019（年）: {label!r}")
    return label


@dataclass
class JournalEntry:
    seq: int
    kind: str  # ACTION / OBS
    period: str
    step: int
    action_name: str
    action: Optional[ActionOp]
    hyp_id: str
    hyp_params: Mapping[str, Any]
    state_after_hash: str
    prev_hash: str
    obs_values: Mapping[str, Any] = None  # type: ignore[assignment]
    record_hash: str = ""

    def payload(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "period": self.period,
            "step": self.step,
            "action_name": self.action_name,
            "action": self.action.fingerprint() if self.action else None,
            "action_params": dict(self.action.params) if self.action else None,
            "hyp_id": self.hyp_id,
            "hyp_params": dict(self.hyp_params),
            "obs_values": dict(self.obs_values or {}),
            "state_after_hash": self.state_after_hash,
        }


class TemporalLedger:
    """事实账 + 执行账 + 哈希链。"""

    def __init__(self) -> None:
        self.facts: List[FactRecord] = []
        self.journal: List[JournalEntry] = []
        self._fact_head = "GENESIS"

    # -- 事实账 ------------------------------------------------------------
    def record_facts(self, bundle: FactBundle, recorded_at: str) -> List[FactRecord]:
        _check_label(recorded_at)
        appended: List[FactRecord] = []
        for key in sorted(bundle.facts):
            value = q(bundle.facts[key])
            current = self._latest_same_period(key, bundle.business_date)
            if current is not None and current.value == value:
                continue  # 幂等：同键同值同业务日不重复追加
            supersedes = current.record_hash if current is not None else None
            payload = {
                "key": key,
                "value": value,
                "unit": "元/量",
                "valid_from": bundle.business_date,
                "valid_to": None,
                "recorded_at": recorded_at,
                "source_hash": bundle.fingerprint(),
                "supersedes": supersedes,
            }
            rec_hash = sha256_hex(canonical_json({"payload": payload, "prev_hash": self._fact_head}))
            rec = FactRecord(
                key=key,
                value=value,
                unit="元/量",
                valid_from=bundle.business_date,
                valid_to=None,
                recorded_at=recorded_at,
                source_hash=bundle.fingerprint(),
                supersedes=supersedes,
                prev_hash=self._fact_head,
                record_hash=rec_hash,
            )
            self.facts.append(rec)
            self._fact_head = rec_hash
            appended.append(rec)
        return appended

    def _latest_same_period(self, key: str, business_date: date) -> Optional[FactRecord]:
        hits = [f for f in self.facts if f.key == key and f.valid_from == business_date]
        if not hits:
            return None
        return max(hits, key=lambda f: f.recorded_at)

    def resolve_fact(self, key: str, as_of: date, knowledge_at: str) -> Optional[FactRecord]:
        """双时态解析：业务有效期 + 已知时点 + supersedes 排除。"""
        _check_label(knowledge_at)
        if as_of is None:
            raise ValueError("as_of 不能为空")
        candidates = [
            f for f in self.facts
            if f.key == key
            and f.valid_from <= as_of
            and (f.valid_to is None or as_of <= f.valid_to)
            and f.recorded_at <= knowledge_at
        ]
        if not candidates:
            return None
        superseded = {f.supersedes for f in candidates if f.supersedes}
        live = [f for f in candidates if f.record_hash not in superseded]
        if not live:
            return None
        latest_label = max(f.recorded_at for f in live)
        latest = [f for f in live if f.recorded_at == latest_label]
        values = {f.value for f in latest}
        if len(values) > 1:
            raise ValueError(f"事实冲突：{key} 在 {as_of} / {knowledge_at} 有多个不同值（拒绝默认取值）")
        return latest[0]

    def facts_at(self, as_of: date, knowledge_at: str, keys: Sequence[str]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for key in keys:
            rec = self.resolve_fact(key, as_of, knowledge_at)
            if rec is not None:
                out[key] = rec.value
        return out

    def fact_values_hash(self, as_of: date, knowledge_at: str, keys: Sequence[str]) -> str:
        values = self.facts_at(as_of, knowledge_at, keys)
        return fingerprint({"as_of": as_of, "knowledge_at": knowledge_at, "values": values})

    def verify_facts(self) -> Tuple[bool, Optional[int]]:
        prev = "GENESIS"
        for i, rec in enumerate(self.facts):
            payload = {
                "key": rec.key,
                "value": rec.value,
                "unit": rec.unit,
                "valid_from": rec.valid_from,
                "valid_to": rec.valid_to,
                "recorded_at": rec.recorded_at,
                "source_hash": rec.source_hash,
                "supersedes": rec.supersedes,
            }
            expected = sha256_hex(canonical_json({"payload": payload, "prev_hash": prev}))
            if rec.prev_hash != prev or rec.record_hash != expected:
                return False, i
            prev = rec.record_hash
        return True, None

    # -- 执行账 ------------------------------------------------------------
    def post_action(
        self,
        period: str,
        step: int,
        action: ActionOp,
        hyp_id: str,
        hyp_params: Mapping[str, Any],
        state_after: StateSnapshot,
    ) -> JournalEntry:
        return self._append_journal(
            kind="ACTION",
            period=period,
            step=step,
            action_name=action.name,
            action=action,
            hyp_id=hyp_id,
            hyp_params=hyp_params,
            state_after=state_after,
        )

    def post_observation(self, observation: Observation, state_after: StateSnapshot) -> JournalEntry:
        return self._append_journal(
            kind="OBS",
            period=observation.period,
            step=-1,
            action_name="OBSERVATION",
            action=None,
            hyp_id="",
            hyp_params={},
            state_after=state_after,
            obs_values=dict(observation.values),
        )

    def _append_journal(self, *, kind: str, period: str, step: int, action_name: str,
                        action: Optional[ActionOp], hyp_id: str, hyp_params: Mapping[str, Any],
                        state_after: StateSnapshot,
                        obs_values: Optional[Mapping[str, Any]] = None) -> JournalEntry:
        prev = self.journal[-1].record_hash if self.journal else "GENESIS"
        entry = JournalEntry(
            seq=len(self.journal),
            kind=kind,
            period=period,
            step=step,
            action_name=action_name,
            action=action,
            hyp_id=hyp_id,
            hyp_params=dict(hyp_params),
            state_after_hash=state_after.fingerprint(),
            prev_hash=prev,
            obs_values=dict(obs_values or {}),
        )
        payload = entry.payload()
        entry.record_hash = sha256_hex(canonical_json({"payload": payload, "prev_hash": prev}))
        self.journal.append(entry)
        return entry

    def verify_journal(self) -> Tuple[bool, Optional[int]]:
        prev = "GENESIS"
        for i, entry in enumerate(self.journal):
            if entry.seq != i or entry.prev_hash != prev:
                return False, i
            expected = sha256_hex(canonical_json({"payload": entry.payload(), "prev_hash": prev}))
            if entry.record_hash != expected:
                return False, i
            prev = entry.record_hash
        return True, None

    def replay(self, domain: Domain) -> StateSnapshot:
        """按执行账回放：动作走域转移，观测修正走域 apply_observation；逐步校验哈希。"""
        state = domain.initial_state()
        for entry in self.journal:
            state = StateSnapshot(period=entry.period, values=state.values)
            if entry.kind == "ACTION":
                if entry.action is None:
                    raise ValueError("ACTION 记录缺动作")
                state = domain.transition(state, entry.action, entry.hyp_params)
            elif entry.kind == "OBS":
                observation = Observation(
                    period=entry.period,
                    values=entry.obs_values or {},
                    source="replay",
                    received_at=entry.period,
                )
                state = domain.apply_observation(state, observation)
            if state.fingerprint() != entry.state_after_hash:
                raise ValueError(f"回放失败：seq={entry.seq} 状态哈希不一致")
        return state
