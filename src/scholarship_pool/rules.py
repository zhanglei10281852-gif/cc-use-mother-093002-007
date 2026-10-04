"""版本化、内容寻址的规则书。

规则书一经发布即不可变；国别名额、项目名额、定向资金池、资格条件、
评分维度全部冻结在某一修订内。任何修改都只能发布新的修订，
由新一轮结果引用，历史轮次永远按其引用的修订重放。
"""
from __future__ import annotations

from dataclasses import dataclass

from .contracts import canonical_json, sha256_hex

GENERAL_POOL = "GENERAL"
"""无定向限制的通用资金池编号。"""


@dataclass(frozen=True)
class Dimension:
    code: str
    weight: float

    def __post_init__(self) -> None:
        if not self.code or self.weight <= 0:
            raise ValueError("评分维度不合法")


@dataclass(frozen=True)
class CountryQuota:
    country: str
    seats: int


@dataclass(frozen=True)
class ProgramQuota:
    program_id: str
    seats: int
    # 项目优先级：数字越小优先级越高，同分时优先保障高优先级项目
    priority: int = 100


@dataclass(frozen=True)
class Earmark:
    """定向资金池：国家/项目限制为 ``None`` 表示不限制该维度。"""

    pool_id: str
    seats: int
    country: str | None
    program_id: str | None
    purpose: str = ""

    def __post_init__(self) -> None:
        if not self.pool_id or self.seats < 0:
            raise ValueError("定向资金池不合法")
        if self.pool_id == GENERAL_POOL:
            raise ValueError(f"资金池编号 {GENERAL_POOL} 为保留编号")

    def matches(self, country: str, program_id: str) -> bool:
        if self.country is not None and self.country != country:
            return False
        if self.program_id is not None and self.program_id != program_id:
            return False
        return True


@dataclass(frozen=True)
class RuleBook:
    revision: int
    countries: frozenset[str]
    channels: frozenset[str]
    required_materials: frozenset[str]
    dimensions: tuple[Dimension, ...]
    min_reviewers: int
    country_quotas: tuple[CountryQuota, ...]
    programs: tuple[ProgramQuota, ...]
    earmarks: tuple[Earmark, ...]
    general_seats: int

    def __post_init__(self) -> None:
        if self.revision < 1:
            raise ValueError("规则修订号必须从 1 开始")
        if not self.countries:
            raise ValueError("至少包含一个面向国别")
        if not self.programs:
            raise ValueError("至少包含一个项目")
        if self.min_reviewers < 1:
            raise ValueError("每轮至少需要 1 名评委")
        if abs(sum(d.weight for d in self.dimensions) - 1.0) > 1e-9:
            raise ValueError("评分维度权重之和必须为 1")
        if len({d.code for d in self.dimensions}) != len(self.dimensions):
            raise ValueError("评分维度编号重复")
        if len({q.country for q in self.country_quotas}) != len(self.country_quotas):
            raise ValueError("国别名额重复定义")
        if len({p.program_id for p in self.programs}) != len(self.programs):
            raise ValueError("项目名额重复定义")
        if len({e.pool_id for e in self.earmarks}) != len(self.earmarks):
            raise ValueError("定向资金池编号重复")
        if self.general_seats < 0:
            raise ValueError("通用资金池额度不能为负")

    @property
    def weights(self) -> dict[str, float]:
        return {d.code: d.weight for d in self.dimensions}

    @property
    def dimension_codes(self) -> frozenset[str]:
        return frozenset(self.weights)

    @property
    def country_seats(self) -> dict[str, int]:
        return {q.country: q.seats for q in self.country_quotas}

    @property
    def program_seats(self) -> dict[str, int]:
        return {p.program_id: p.seats for p in self.programs}

    @property
    def program_priorities(self) -> dict[str, int]:
        return {p.program_id: p.priority for p in self.programs}

    def ordered_pools(self) -> list[tuple[str, int, str | None, str | None]]:
        """返回确定性的资金池顺序：定向池按编号排序，通用池最后。"""
        pools = [
            (e.pool_id, e.seats, e.country, e.program_id)
            for e in sorted(self.earmarks, key=lambda e: e.pool_id)
        ]
        pools.append((GENERAL_POOL, self.general_seats, None, None))
        return pools

    def to_dict(self) -> dict:
        return {
            "revision": self.revision,
            "countries": sorted(self.countries),
            "channels": sorted(self.channels),
            "required_materials": sorted(self.required_materials),
            "dimensions": [
                {"code": d.code, "weight": d.weight}
                for d in sorted(self.dimensions, key=lambda d: d.code)
            ],
            "min_reviewers": self.min_reviewers,
            "country_quotas": [
                {"country": q.country, "seats": q.seats}
                for q in sorted(self.country_quotas, key=lambda q: q.country)
            ],
            "programs": [
                {
                    "program_id": p.program_id,
                    "seats": p.seats,
                    "priority": p.priority,
                }
                for p in sorted(self.programs, key=lambda p: p.program_id)
            ],
            "earmarks": [
                {
                    "pool_id": e.pool_id,
                    "seats": e.seats,
                    "country": e.country,
                    "program_id": e.program_id,
                    "purpose": e.purpose,
                }
                for e in sorted(self.earmarks, key=lambda e: e.pool_id)
            ],
            "general_seats": self.general_seats,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RuleBook":
        return cls(
            revision=data["revision"],
            countries=frozenset(data["countries"]),
            channels=frozenset(data["channels"]),
            required_materials=frozenset(data["required_materials"]),
            dimensions=tuple(
                Dimension(d["code"], float(d["weight"]))
                for d in data["dimensions"]
            ),
            min_reviewers=data["min_reviewers"],
            country_quotas=tuple(
                CountryQuota(q["country"], q["seats"]) for q in data["country_quotas"]
            ),
            programs=tuple(
                ProgramQuota(p["program_id"], p["seats"], p.get("priority", 100))
                for p in data["programs"]
            ),
            earmarks=tuple(
                Earmark(
                    e["pool_id"], e["seats"], e["country"], e["program_id"],
                    e.get("purpose", ""),
                )
                for e in data["earmarks"]
            ),
            general_seats=data["general_seats"],
        )

    @property
    def content_hash(self) -> str:
        return sha256_hex(canonical_json(self.to_dict()))
