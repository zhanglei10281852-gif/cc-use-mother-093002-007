"""资金规则与奖学金申请的基础契约。"""
from dataclasses import dataclass


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
