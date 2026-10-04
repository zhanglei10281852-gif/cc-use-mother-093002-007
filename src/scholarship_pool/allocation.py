"""确定性的评分聚合、排序与名额分配引擎。

本模块全部为纯函数：相同的规则书、候选材料与评分输入必然产生
完全相同的排序、录取、落选原因与候补顺序。资金池/国别/项目三类
额度的每一次占用都会生成可逐条解释的决策记录。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .rules import GENERAL_POOL

ADMITTED = "admitted"
WAITLISTED = "waitlisted"
EXCLUDED = "excluded"


@dataclass(frozen=True)
class CandidateInput:
    application_id: str
    pseudonym: str
    country: str
    program_id: str
    program_priority: int
    material_version: int
    material_hash: str
    # 评委编号 -> {维度: 分}
    sheets: dict[str, dict[str, float]]
    # 被排除的评委（利益冲突回避或申诉改判剔除）及原因
    excluded_reviewers: tuple[tuple[str, str], ...] = ()

    def to_dict(self) -> dict:
        return {
            "application_id": self.application_id,
            "pseudonym": self.pseudonym,
            "country": self.country,
            "program_id": self.program_id,
            "program_priority": self.program_priority,
            "material_version": self.material_version,
            "material_hash": self.material_hash,
            "sheets": {r: dict(dims) for r, dims in sorted(self.sheets.items())},
            "excluded_reviewers": [list(x) for x in self.excluded_reviewers],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CandidateInput":
        return cls(
            application_id=data["application_id"],
            pseudonym=data["pseudonym"],
            country=data["country"],
            program_id=data["program_id"],
            program_priority=data["program_priority"],
            material_version=data["material_version"],
            material_hash=data["material_hash"],
            sheets={r: dict(dims) for r, dims in data["sheets"].items()},
            excluded_reviewers=tuple(
                (x[0], x[1]) for x in data.get("excluded_reviewers", [])
            ),
        )


@dataclass(frozen=True)
class Ranked:
    rank: int
    application_id: str
    pseudonym: str
    country: str
    program_id: str
    program_priority: int
    material_version: int
    material_hash: str
    weighted_score: float
    dimension_averages: tuple[tuple[str, float], ...]
    reviewer_count: int
    excluded_reviewers: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class Decision:
    application_id: str
    pseudonym: str
    rank: int
    weighted_score: float
    outcome: str
    detail: str
    pool_id: str | None = None
    waitlist_position: int | None = None

    def to_dict(self) -> dict:
        return {
            "application_id": self.application_id,
            "pseudonym": self.pseudonym,
            "rank": self.rank,
            "weighted_score": self.weighted_score,
            "outcome": self.outcome,
            "detail": self.detail,
            "pool_id": self.pool_id,
            "waitlist_position": self.waitlist_position,
        }


@dataclass
class AllocationResult:
    ranked: list[Ranked]
    decisions: list[Decision]
    admitted: dict[str, str]                      # application_id -> pool_id
    waitlist: list[str]                          # 冻结的候补顺序
    excluded: dict[str, str]                     # application_id -> 原因

    def to_dict(self) -> dict:
        return {
            "ranked": [
                {
                    "rank": r.rank,
                    "application_id": r.application_id,
                    "pseudonym": r.pseudonym,
                    "country": r.country,
                    "program_id": r.program_id,
                    "program_priority": r.program_priority,
                    "material_version": r.material_version,
                    "material_hash": r.material_hash,
                    "weighted_score": r.weighted_score,
                    "dimension_averages": list(r.dimension_averages),
                    "reviewer_count": r.reviewer_count,
                    "excluded_reviewers": list(r.excluded_reviewers),
                }
                for r in self.ranked
            ],
            "decisions": [d.to_dict() for d in self.decisions],
            "admitted": dict(sorted(self.admitted.items())),
            "waitlist": list(self.waitlist),
            "excluded": dict(sorted(self.excluded.items())),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AllocationResult":
        ranked = [
            Ranked(
                rank=r["rank"],
                application_id=r["application_id"],
                pseudonym=r["pseudonym"],
                country=r["country"],
                program_id=r["program_id"],
                program_priority=r["program_priority"],
                material_version=r["material_version"],
                material_hash=r["material_hash"],
                weighted_score=r["weighted_score"],
                dimension_averages=tuple(
                    (d, float(v)) for d, v in r["dimension_averages"]
                ),
                reviewer_count=r["reviewer_count"],
                excluded_reviewers=tuple(
                    (x, y) for x, y in r.get("excluded_reviewers", [])
                ),
            )
            for r in data["ranked"]
        ]
        decisions = [
            Decision(
                application_id=d["application_id"],
                pseudonym=d["pseudonym"],
                rank=d["rank"],
                weighted_score=d["weighted_score"],
                outcome=d["outcome"],
                detail=d["detail"],
                pool_id=d.get("pool_id"),
                waitlist_position=d.get("waitlist_position"),
            )
            for d in data["decisions"]
        ]
        return cls(
            ranked=ranked,
            decisions=decisions,
            admitted=dict(data["admitted"]),
            waitlist=list(data["waitlist"]),
            excluded=dict(data["excluded"]),
        )


def rank_candidates(rulebook, candidates: list[CandidateInput]) -> tuple[list[Ranked], dict[str, str]]:
    """聚合评分并排序，返回 (排名表, 被排除申请及原因)。

    排序键（全部确定性）：加权总分降序 → 项目优先级升序（数字小优先）
    → 有效评委数降序 → 申请编号升序。
    """
    weights = rulebook.weights
    scored: list[Ranked] = []
    excluded: dict[str, str] = {}
    for cand in candidates:
        effective = {
            reviewer: dims
            for reviewer, dims in cand.sheets.items()
            if reviewer not in {r for r, _ in cand.excluded_reviewers}
        }
        if len(effective) < rulebook.min_reviewers:
            excluded[cand.application_id] = (
                f"有效评委 {len(effective)} 名，少于规则要求的 "
                f"{rulebook.min_reviewers} 名"
            )
            continue
        dim_avgs: list[tuple[str, float]] = []
        for code in sorted(weights):
            values = [float(dims[code]) for dims in effective.values()]
            dim_avgs.append((code, round(sum(values) / len(values), 6)))
        total = round(sum(avg * weights[code] for code, avg in dim_avgs), 6)
        scored.append(
            Ranked(
                rank=0,
                application_id=cand.application_id,
                pseudonym=cand.pseudonym,
                country=cand.country,
                program_id=cand.program_id,
                program_priority=cand.program_priority,
                material_version=cand.material_version,
                material_hash=cand.material_hash,
                weighted_score=total,
                dimension_averages=tuple(dim_avgs),
                reviewer_count=len(effective),
                excluded_reviewers=cand.excluded_reviewers,
            )
        )
    scored.sort(
        key=lambda r: (
            -r.weighted_score,
            r.program_priority,
            -r.reviewer_count,
            r.application_id,
        )
    )
    ordered = [
        Ranked(rank=i + 1, **{k: getattr(r, k) for k in r.__dataclass_fields__ if k != "rank"})
        for i, r in enumerate(scored)
    ]
    return ordered, excluded


def _pool_order_key(pool: tuple[str, int, str | None, str | None]) -> tuple:
    pool_id, _seats, country, program = pool
    restrictiveness = (country is not None) + (program is not None)
    # 限制越严越早消耗（保留通用池给只能使用它的人），同为定向池按编号
    if pool_id == GENERAL_POOL:
        return (2, 0, pool_id)
    return (-restrictiveness, 0, pool_id)


def allocate(rulebook, candidates: list[CandidateInput]) -> AllocationResult:
    ranked, excluded = rank_candidates(rulebook, candidates)

    remaining: dict[str, int] = {}
    restriction: dict[str, tuple[str | None, str | None]] = {}
    for pool_id, seats, country, program in rulebook.ordered_pools():
        remaining[pool_id] = seats
        restriction[pool_id] = (country, program)

    country_used: dict[str, int] = {c: 0 for c in rulebook.country_seats}
    program_used: dict[str, int] = {p: 0 for p in rulebook.program_seats}

    admitted: dict[str, str] = {}
    waitlist: list[str] = []
    decisions: list[Decision] = []

    for cand in ranked:
        blockers: list[str] = []
        chosen_pool: str | None = None
        if cand.country in rulebook.country_seats:
            if country_used[cand.country] >= rulebook.country_seats[cand.country]:
                blockers.append(
                    f"国别 {cand.country} 名额 {rulebook.country_seats[cand.country]} 已满"
                )
        if program_used[cand.program_id] >= rulebook.program_seats[cand.program_id]:
            blockers.append(
                f"项目 {cand.program_id} 名额 {rulebook.program_seats[cand.program_id]} 已满"
            )
        pools = sorted(rulebook.ordered_pools(), key=_pool_order_key)
        for pool_id, _seats, _country, _program in pools:
            if remaining[pool_id] <= 0:
                continue
            want_country, want_program = restriction[pool_id]
            if want_country is not None and want_country != cand.country:
                continue
            if want_program is not None and want_program != cand.program_id:
                continue
            if cand.country in rulebook.country_seats and (
                country_used[cand.country] >= rulebook.country_seats[cand.country]
            ):
                continue
            if program_used[cand.program_id] >= rulebook.program_seats[cand.program_id]:
                continue
            chosen_pool = pool_id
            break
        if chosen_pool is None:
            full_pools = [
                f"{p[0]} 池余 {remaining[p[0]]}"
                for p in pools
                if remaining[p[0]] > 0
            ]
            if not blockers and full_pools:
                blockers.append(
                    "有余量资金池均不匹配该国别/项目：" + "；".join(full_pools)
                )
            if not blockers:
                blockers.append("全部资金池名额已满")
            position = len(waitlist) + 1
            waitlist.append(cand.application_id)
            decisions.append(
                Decision(
                    application_id=cand.application_id,
                    pseudonym=cand.pseudonym,
                    rank=cand.rank,
                    weighted_score=cand.weighted_score,
                    outcome=WAITLISTED,
                    detail="；".join(blockers),
                    waitlist_position=position,
                )
            )
        else:
            remaining[chosen_pool] -= 1
            country_used[cand.country] = country_used.get(cand.country, 0) + 1
            program_used[cand.program_id] += 1
            wanted = restriction[chosen_pool]
            scope = []
            if wanted[0]:
                scope.append(f"限定国别 {wanted[0]}")
            if wanted[1]:
                scope.append(f"限定项目 {wanted[1]}")
            scope_text = f"（{'，'.join(scope)}）" if scope else "（通用资金）"
            decisions.append(
                Decision(
                    application_id=cand.application_id,
                    pseudonym=cand.pseudonym,
                    rank=cand.rank,
                    weighted_score=cand.weighted_score,
                    outcome=ADMITTED,
                    detail=(
                        f"按排序第 {cand.rank} 位录取，占用资金池 "
                        f"{chosen_pool}{scope_text}，"
                        f"国别 {cand.country} 已用 "
                        f"{country_used[cand.country]}"
                        + (
                            f"/{rulebook.country_seats[cand.country]}"
                            if cand.country in rulebook.country_seats
                            else ""
                        )
                        + f"，项目 {cand.program_id} 已用 "
                        f"{program_used[cand.program_id]}"
                        f"/{rulebook.program_seats[cand.program_id]}"
                    ),
                    pool_id=chosen_pool,
                )
            )
            admitted[cand.application_id] = chosen_pool

    return AllocationResult(
        ranked=ranked,
        decisions=decisions,
        admitted=admitted,
        waitlist=waitlist,
        excluded=excluded,
    )


def find_promotion(
    rulebook,
    result: AllocationResult,
    freed_pool_id: str,
    is_live,
    is_held,
    admitted: set[str],
    country_used: dict[str, int],
    program_used: dict[str, int],
) -> str | None:
    """按冻结的候补顺序寻找第一个可递补者，不得人工插队。

    被冻结（申诉中）或已不具备资格的候补条目跳过但不改变其位置。
    """
    want_country, want_program = {
        pid: (c, p) for pid, _s, c, p in rulebook.ordered_pools()
    }[freed_pool_id]
    for application_id in result.waitlist:
        if application_id in admitted:
            continue
        if is_held(application_id):
            continue
        if not is_live(application_id):
            continue
        ranked = next(r for r in result.ranked if r.application_id == application_id)
        if want_country is not None and want_country != ranked.country:
            continue
        if want_program is not None and want_program != ranked.program_id:
            continue
        country_cap = rulebook.country_seats.get(ranked.country)
        if country_cap is not None and country_used.get(ranked.country, 0) >= country_cap:
            continue
        if program_used.get(ranked.program_id, 0) >= rulebook.program_seats[ranked.program_id]:
            continue
        return application_id
    return None
