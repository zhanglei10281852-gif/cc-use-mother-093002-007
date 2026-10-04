"""名额治理规则集：版本化、不可变，任何修订只产生新的版本号。"""
from __future__ import annotations

from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Mapping, Optional, Tuple


@dataclass(frozen=True)
class FundPoolRule:
    """资金池规则；定向资金通过 only_countries / only_programs 限定用途。"""

    fund_id: str
    display_name: str
    total_amount: int
    award_amount: int
    only_countries: Optional[frozenset] = None
    only_programs: Optional[frozenset] = None

    def __post_init__(self) -> None:
        if not self.fund_id or not self.display_name:
            raise ValueError("资金池标识与名称不能为空")
        if self.total_amount <= 0 or self.award_amount <= 0:
            raise ValueError("资金池额度必须为正数")
        if self.award_amount > self.total_amount:
            raise ValueError("单笔拨款不能超过资金池总额")
        if self.only_countries is not None:
            object.__setattr__(self, "only_countries", frozenset(self.only_countries))
        if self.only_programs is not None:
            object.__setattr__(self, "only_programs", frozenset(self.only_programs))

    @property
    def capacity(self) -> int:
        """按单笔拨款整除得到的可资助名额数。"""
        return self.total_amount // self.award_amount

    @property
    def specificity(self) -> int:
        """定向程度：限制越多越优先被匹配，保证定向资金用于定向对象。"""
        return int(self.only_countries is not None) + int(self.only_programs is not None)

    def accepts(self, country: str, program: str) -> bool:
        if self.only_countries is not None and country not in self.only_countries:
            return False
        if self.only_programs is not None and program not in self.only_programs:
            return False
        return True


@dataclass(frozen=True)
class RuleSet:
    """一轮分配所依据的完整规则快照。

    规则集一旦提交即不可变；任何修订只能通过 revise() 产生携带
    新版本号的全新规则集，已开启轮次继续沿用其开启时的快照。
    """

    revision: int
    country_quotas: Mapping[str, int]
    program_quotas: Mapping[str, int]
    funds: Tuple[FundPoolRule, ...]
    required_materials: Tuple[str, ...]
    min_valid_scores: int = 1

    def __post_init__(self) -> None:
        if self.revision < 1:
            raise ValueError("规则版本号必须为正整数")
        country = MappingProxyType(dict(self.country_quotas))
        program = MappingProxyType(dict(self.program_quotas))
        for label, quotas in (("国别", country), ("项目", program)):
            for key, value in quotas.items():
                if not key or value < 0:
                    raise ValueError(f"{label}配额不合法")
        funds = tuple(self.funds)
        if not funds:
            raise ValueError("至少需要一个资金池")
        fund_ids = [fund.fund_id for fund in funds]
        if len(set(fund_ids)) != len(fund_ids):
            raise ValueError("资金池标识重复")
        if self.min_valid_scores < 1:
            raise ValueError("有效评分份数下限必须为正整数")
        object.__setattr__(self, "country_quotas", country)
        object.__setattr__(self, "program_quotas", program)
        object.__setattr__(self, "funds", funds)
        object.__setattr__(self, "required_materials", tuple(self.required_materials))

    def revise(self, **changes) -> "RuleSet":
        """规则修订：返回全新规则集，原版本保持不变。"""
        return replace(self, **changes)

    def fund_by_id(self, fund_id: str) -> FundPoolRule:
        for fund in self.funds:
            if fund.fund_id == fund_id:
                return fund
        raise KeyError(f"资金池不存在：{fund_id}")


def fund_pool_to_dict(fund: FundPoolRule) -> dict:
    return {
        "fund_id": fund.fund_id,
        "display_name": fund.display_name,
        "total_amount": fund.total_amount,
        "award_amount": fund.award_amount,
        "only_countries": None if fund.only_countries is None else sorted(fund.only_countries),
        "only_programs": None if fund.only_programs is None else sorted(fund.only_programs),
    }


def fund_pool_from_dict(data: Mapping) -> FundPoolRule:
    return FundPoolRule(
        fund_id=data["fund_id"],
        display_name=data["display_name"],
        total_amount=int(data["total_amount"]),
        award_amount=int(data["award_amount"]),
        only_countries=data.get("only_countries"),
        only_programs=data.get("only_programs"),
    )


def ruleset_to_dict(ruleset: RuleSet) -> dict:
    return {
        "revision": ruleset.revision,
        "country_quotas": dict(ruleset.country_quotas),
        "program_quotas": dict(ruleset.program_quotas),
        "funds": [fund_pool_to_dict(fund) for fund in ruleset.funds],
        "required_materials": list(ruleset.required_materials),
        "min_valid_scores": ruleset.min_valid_scores,
    }


def ruleset_from_dict(data: Mapping) -> RuleSet:
    return RuleSet(
        revision=int(data["revision"]),
        country_quotas=dict(data["country_quotas"]),
        program_quotas=dict(data["program_quotas"]),
        funds=tuple(fund_pool_from_dict(item) for item in data["funds"]),
        required_materials=tuple(data["required_materials"]),
        min_valid_scores=int(data["min_valid_scores"]),
    )
