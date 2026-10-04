"""评委评分与利益冲突回避。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Mapping, Sequence, Tuple


@dataclass(frozen=True)
class ConflictDeclaration:
    """评委与申请人之间的利益冲突声明。

    声明绑定身份令牌而非申请编号：同一申请人换渠道重复报名，
    回避关系依然生效。
    """

    judge_id: str
    identity_token: str
    relation: str

    def __post_init__(self) -> None:
        if not self.judge_id or not self.identity_token or not self.relation:
            raise ValueError("回避声明信息不完整")


@dataclass(frozen=True)
class ScoreEntry:
    judge_id: str
    application_id: str
    score: int
    submitted_at: int

    def __post_init__(self) -> None:
        if not self.judge_id or not self.application_id:
            raise ValueError("评分信息不完整")
        if not 0 <= self.score <= 100:
            raise ValueError("评分必须介于 0 与 100 之间")


class ScoreBook:
    """评分登记簿：评委独立提交，同一评委对同一申请只能提交一次。"""

    def __init__(self) -> None:
        self._entries: Dict[Tuple[str, str], ScoreEntry] = {}

    def submit(self, entry: ScoreEntry) -> None:
        key = (entry.judge_id, entry.application_id)
        if key in self._entries:
            raise ValueError("同一评委对同一申请只能提交一次评分")
        self._entries[key] = entry

    def by_application(self) -> Dict[str, List[ScoreEntry]]:
        grouped: Dict[str, List[ScoreEntry]] = {}
        for entry in self._entries.values():
            grouped.setdefault(entry.application_id, []).append(entry)
        return grouped


def partition_scores(
    entries: Sequence[ScoreEntry],
    conflicts_for_identity: Sequence[ConflictDeclaration],
) -> Tuple[List[ScoreEntry], List[Tuple[ScoreEntry, str]]]:
    """按回避名单把评分拆成有效评分与被回避评分。"""
    relation_by_judge = {d.judge_id: d.relation for d in conflicts_for_identity}
    valid: List[ScoreEntry] = []
    recused: List[Tuple[ScoreEntry, str]] = []
    for entry in entries:
        if entry.judge_id in relation_by_judge:
            recused.append((entry, relation_by_judge[entry.judge_id]))
        else:
            valid.append(entry)
    return valid, recused


def conflict_to_dict(declaration: ConflictDeclaration) -> dict:
    return {
        "judge_id": declaration.judge_id,
        "identity_token": declaration.identity_token,
        "relation": declaration.relation,
    }


def conflict_from_dict(data: Mapping) -> ConflictDeclaration:
    return ConflictDeclaration(
        judge_id=data["judge_id"],
        identity_token=data["identity_token"],
        relation=data["relation"],
    )


def score_to_dict(entry: ScoreEntry) -> dict:
    return {
        "judge_id": entry.judge_id,
        "application_id": entry.application_id,
        "score": entry.score,
        "submitted_at": entry.submitted_at,
    }


def score_from_dict(data: Mapping) -> ScoreEntry:
    return ScoreEntry(
        judge_id=data["judge_id"],
        application_id=data["application_id"],
        score=int(data["score"]),
        submitted_at=int(data["submitted_at"]),
    )
