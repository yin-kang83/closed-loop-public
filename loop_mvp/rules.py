"""版本化规则与本体的最小实现（域无关）。

沿用 Fin-Wiki 的判定顺序思路：按业务日 + 主体范围筛版本；
没有命中 → 拒绝（缺参，不默认）；命中多个 → 歧义（交给假设集与闸门，不取最新版）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, Mapping, Optional, Tuple

from .types import fingerprint


@dataclass(frozen=True)
class RuleVersion:
    """一条规则的一个版本；参数用 dict 表达（税率 / 切缝 / 损耗率 …）。"""

    rule_id: str
    version: str
    params: Mapping[str, Any]
    effective_from: date
    effective_to: Optional[date] = None
    entity_scope: str = "ALL"  # ALL / GENERAL / SMALL ...
    citation: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "params", dict(self.params))

    def hash(self) -> str:
        return fingerprint(
            {"rule_id": self.rule_id, "version": self.version, "params": self.params,
             "effective_from": self.effective_from, "effective_to": self.effective_to,
             "entity_scope": self.entity_scope, "citation": self.citation}
        )

    def label(self) -> str:
        return f"{self.rule_id}@{self.version}"


@dataclass
class RuleBook:
    """规则库：只读；版本哈希用于经验 case 的失效判断。"""

    versions: Tuple[RuleVersion, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "versions", tuple(self.versions))

    def rule_ids(self) -> Tuple[str, ...]:
        return tuple(sorted({v.rule_id for v in self.versions}))

    def applicable(
        self,
        rule_id: str,
        business_date: date,
        entity_scope: Optional[str] = None,
    ) -> Tuple[RuleVersion, ...]:
        out = []
        for v in self.versions:
            if v.rule_id != rule_id:
                continue
            if v.effective_from > business_date:
                continue
            if v.effective_to is not None and business_date > v.effective_to:
                continue
            if entity_scope is not None and v.entity_scope not in ("ALL", entity_scope):
                continue
            out.append(v)
        return tuple(sorted(out, key=lambda v: v.version))

    def resolve_one(
        self,
        rule_id: str,
        business_date: date,
        entity_scope: Optional[str] = None,
    ) -> RuleVersion:
        """唯一命中才返回；0 或多于 1 一律拒绝（不默认、不取最新版）。"""
        hits = self.applicable(rule_id, business_date, entity_scope)
        if not hits:
            raise LookupError(f"规则 {rule_id} 在 {business_date} 无适用版本（缺参，拒绝）")
        if len(hits) > 1:
            raise LookupError(f"规则 {rule_id} 在 {business_date} 有 {len(hits)} 个适用版本（歧义，拒绝）")
        return hits[0]

    def hashes(self) -> Dict[str, str]:
        """每个 rule_id 的全版本联合哈希：任一版本变动都会让旧经验失效。"""
        out: Dict[str, str] = {}
        for rid in self.rule_ids():
            vs = sorted((v for v in self.versions if v.rule_id == rid), key=lambda v: v.version)
            out[rid] = fingerprint([v.hash() for v in vs])
        return out
