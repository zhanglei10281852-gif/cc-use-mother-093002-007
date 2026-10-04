"""领域基础契约与通用工具。

保留首轮建立的两个基础契约：
- ``FundingRuleVersion``：版本化实体标识；
- ``ApplicationRecord``：申请记录与实体的关联。

治理服务的其余模块在此基础上扩展。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FundingRuleVersion:
    entity_id: str
    display_name: str
    revision: int

    def __post_init__(self) -> None:
        if not self.entity_id or not self.display_name or self.revision < 1:
            raise ValueError("版本化实体信息不合法")


@dataclass(frozen=True)
class ApplicationRecord:
    record_id: str
    entity_id: str
    category: str

    def __post_init__(self) -> None:
        if not self.record_id or not self.entity_id or not self.category:
            raise ValueError("关联记录信息不完整")


def canonical_json(value: Any) -> bytes:
    """以键排序、无空白的方式序列化，供内容哈希与确定性重放使用。"""
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def sha256_hex(value: bytes | str) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()
