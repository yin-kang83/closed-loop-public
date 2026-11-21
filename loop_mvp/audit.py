"""哈希链审计：任何判定/事实/凭证/经验入链，可校验、可定位篡改。

结构：每条记录 = payload + prev_hash + record_hash，
record_hash = sha256(canonical_json({seq, payload, prev_hash}))。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .types import canonical_json, sha256_hex


def _record_hash(seq: int, payload: Dict[str, Any], prev_hash: str) -> str:
    return sha256_hex(canonical_json({"seq": seq, "payload": payload, "prev_hash": prev_hash}))


@dataclass
class HashChain:
    """append-only 哈希链；``records`` 是唯一事实源。"""

    name: str = "audit"
    records: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def head(self) -> str:
        return self.records[-1]["record_hash"] if self.records else "GENESIS"

    def append(self, payload: Dict[str, Any]) -> str:
        seq = len(self.records)
        prev = self.head
        rec = {
            "seq": seq,
            "payload": payload,
            "prev_hash": prev,
            "record_hash": _record_hash(seq, payload, prev),
        }
        self.records.append(rec)
        return rec["record_hash"]

    def verify(self) -> Tuple[bool, Optional[int]]:
        """返回 (是否完整, 第一个坏记录下标)。"""
        prev = "GENESIS"
        for i, rec in enumerate(self.records):
            if rec.get("seq") != i or rec.get("prev_hash") != prev:
                return False, i
            expected = _record_hash(i, rec["payload"], prev)
            if rec.get("record_hash") != expected:
                return False, i
            prev = rec["record_hash"]
        return True, None

    def find(self, record_hash: str) -> Optional[Dict[str, Any]]:
        for rec in self.records:
            if rec["record_hash"] == record_hash:
                return rec
        return None

    def to_jsonl(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="\n") as fh:
            for rec in self.records:
                fh.write(canonical_json(rec) + "\n")

    @classmethod
    def from_jsonl(cls, path: Path, name: str = "audit") -> "HashChain":
        chain = cls(name=name)
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    chain.records.append(json.loads(line))
        return chain
