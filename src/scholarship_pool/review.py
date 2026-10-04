"""评委登记、利益冲突回避与独立评分。

- 利益冲突按“自然人身份”申报：身份合并后冲突自动覆盖其全部申请；
- 评委对任一申请只能独立提交一次评分，提交后不可修改、不可查看他人评分；
- 每维度得分 0–100，维度集合必须与轮次规则书一致；
- 汇总时按维度对评委取均值再加权，有效评委数不足的申请不进入排序。
"""
from __future__ import annotations

from dataclasses import dataclass

from .errors import (
    ConflictOfInterestError,
    NotFoundError,
    RoundStateError,
    ValidationError,
)

SCORE_MIN = 0.0
SCORE_MAX = 100.0


@dataclass(frozen=True)
class ScoreSheet:
    reviewer_id: str
    application_id: str
    scores: tuple[tuple[str, float], ...]


class ReviewBoard:
    def __init__(self) -> None:
        self._reviewers: set[str] = set()
        # person_id -> 与其存在合作/利害关系、必须回避的评委集合
        self._conflicts: dict[str, set[str]] = {}

    def register_reviewer(self, reviewer_id: str) -> None:
        if not reviewer_id:
            raise ValidationError("评委编号为空")
        self._reviewers.add(reviewer_id)

    def require_reviewer(self, reviewer_id: str) -> None:
        if reviewer_id not in self._reviewers:
            raise NotFoundError(f"评委 {reviewer_id} 未登记")

    def declare_conflict(self, reviewer_id: str, person_id: str) -> None:
        self.require_reviewer(reviewer_id)
        self._conflicts.setdefault(person_id, set()).add(reviewer_id)

    def conflicts_for(self, person_id: str) -> frozenset[str]:
        return frozenset(self._conflicts.get(person_id, set()))

    def is_conflicted(self, reviewer_id: str, person_id: str) -> bool:
        return reviewer_id in self._conflicts.get(person_id, set())

    def require_no_conflict(self, reviewer_id: str, person_id: str) -> None:
        if self.is_conflicted(reviewer_id, person_id):
            raise ConflictOfInterestError(
                f"评委 {reviewer_id} 与该申请人存在已申报利益冲突，必须回避"
            )

    def merge_person(self, source_person: str, target_person: str) -> None:
        """身份合并时把冲突记录迁移到主身份（并集）。"""
        conflicts = self._conflicts.pop(source_person, set())
        if conflicts:
            self._conflicts.setdefault(target_person, set()).update(conflicts)


@dataclass
class ScoringSession:
    """单个轮次内的评分收集状态。"""

    dimension_codes: frozenset[str]
    _sheets: dict[tuple[str, str], ScoreSheet] | None = None

    def __post_init__(self) -> None:
        if self._sheets is None:
            self._sheets = {}

    @property
    def closed(self) -> bool:
        return self._sheets is None

    def submit(
        self,
        reviewer_id: str,
        application_id: str,
        scores: dict[str, float],
        conflicted: bool,
    ) -> ScoreSheet:
        if self.closed:
            raise RoundStateError("评分已截止，不能再提交或修改评分")
        if conflicted:
            raise ConflictOfInterestError("存在利益冲突的评委必须回避，不得提交评分")
        if frozenset(scores) != self.dimension_codes:
            raise ValidationError(
                f"评分维度必须恰好为 {sorted(self.dimension_codes)}"
            )
        key = (reviewer_id, application_id)
        if key in self._sheets:
            raise RoundStateError("评委对该申请已独立提交评分，不能重复提交")
        for dim, value in scores.items():
            if not (SCORE_MIN <= float(value) <= SCORE_MAX):
                raise ValidationError(f"维度 {dim} 得分超出 0–100")
        sheet = ScoreSheet(
            reviewer_id=reviewer_id,
            application_id=application_id,
            scores=tuple(sorted(scores.items())),
        )
        self._sheets[key] = sheet
        return sheet

    def close(self) -> None:
        if self.closed:
            raise RoundStateError("评分已经截止")
        self._sheets = None  # 类型为 None 即冻结；评分已通过事件持久化

    def sheets_for(self, application_id: str) -> list[ScoreSheet]:
        source = self._sheets if self._sheets is not None else {}
        return [s for s in source.values() if s.application_id == application_id]
