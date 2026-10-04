"""审计离线重放。

审计人员只需要事件日志文件即可：
1. 校验哈希链（任何删改都会导致校验失败）；
2. 重放全部事件还原状态；
3. 对指定轮次，用冻结的规则修订与材料版本重新执行确定性分配，
   逐字节比对结果哈希，证明“使用的规则、额度、回避处理”可还原；
4. 输出按时间顺序的逐条裁决台账：录取、落选、冻结、释放、递补、改判。
"""
from __future__ import annotations

from dataclasses import dataclass

from .allocation import CandidateInput, allocate
from .contracts import canonical_json, sha256_hex
from .journal import EventStore
from .service import ScholarshipGovernanceService
# 事件在裁决台账中的中文归类
EVENT_NARRATIVE = {
    "rule_published": "规则修订发布（不可变）",
    "round_opened": "轮次开启，冻结规则修订与材料版本",
    "score_submitted": "评委独立评分提交",
    "scoring_closed": "评分截止",
    "round_allocated": "执行确定性分配",
    "seat_confirmed": "录取确认",
    "appeal_filed": "申诉受理，仅冻结受影响名额",
    "appeal_resolved": "申诉裁决",
    "seat_released": "名额释放",
    "seat_promoted": "候补自动递补",
    "promotion_skipped": "无可递补者，名额空置",
    "reviewer_excluded": "改判剔除受污染评分",
    "round_amended": "申诉改判，重算分数（候补顺序不变）",
    "application_withdrawn": "申请人放弃",
    "application_disqualified": "资格撤销",
    "duplicate_merged": "重复身份合并",
    "conflict_declared": "利益冲突申报",
}


@dataclass
class ReplayReport:
    round_id: str
    rule_revision: int
    rule_hash: str
    result_hash: str
    chain_ok: bool
    recompute_match: bool
    ledger: list[dict]
    ranking: list[dict]
    decisions: list[dict]
    quota_view: dict
    conflicts_applied: list[dict]
    final_state: dict

    def to_dict(self) -> dict:
        return {
            "round_id": self.round_id,
            "rule_revision": self.rule_revision,
            "rule_hash": self.rule_hash,
            "result_hash": self.result_hash,
            "chain_ok": self.chain_ok,
            "recompute_match": self.recompute_match,
            "quota_view": self.quota_view,
            "conflicts_applied": self.conflicts_applied,
            "ranking": self.ranking,
            "decisions": self.decisions,
            "ledger": self.ledger,
            "final_state": self.final_state,
        }


def replay(journal_path: str, round_id: str) -> ReplayReport:
    store = EventStore(journal_path, verify=True)  # 哈希链校验
    service = ScholarshipGovernanceService(store)
    round_ = service._round(round_id)
    if round_.result is None:
        raise ValueError(f"轮次 {round_id} 尚未产生分配结果")
    rulebook = service.rulebook(round_.rule_revision)

    # 用分配时刻冻结的输入快照独立重算，与日志中的结果哈希逐字节比对
    frozen_candidates = [
        CandidateInput.from_dict(row) for row in round_.candidate_snapshot
    ]
    recomputed = allocate(rulebook, frozen_candidates)
    recompute_match = (
        sha256_hex(canonical_json(recomputed.to_dict())) == round_.result_hash
    )

    conflicts_applied = []
    for cand in frozen_candidates:
        for reviewer, reason in cand.excluded_reviewers:
            conflicts_applied.append(
                {
                    "application_id": cand.application_id,
                    "pseudonym": cand.pseudonym,
                    "reviewer_id": reviewer,
                    "reason": reason,
                }
            )

    ledger: list[dict] = []
    pinned = set(round_.material_pin)
    pinned_persons = {
        service.applications.get(app_id).person_id for app_id in pinned
    }
    touched = pinned | set(round_.promoted)
    for event in store.read():
        if event.round_id is not None and event.round_id != round_id:
            continue
        if event.type in ("identity_registered", "reviewer_registered"):
            continue
        if event.type == "rule_published":
            if event.payload["rulebook"]["revision"] != round_.rule_revision:
                continue
        elif event.type in ("application_withdrawn", "application_disqualified"):
            if event.payload["application_id"] not in touched:
                continue
        elif event.type == "conflict_declared":
            if event.payload["person_id"] not in pinned_persons:
                continue
        elif event.type == "duplicate_merged":
            if event.payload["target_application_id"] not in pinned:
                continue
        ledger.append(
            {
                "seq": event.seq,
                "type": event.type,
                "narrative": EVENT_NARRATIVE.get(event.type, event.type),
                "payload": event.payload,
                "event_hash": event.event_hash,
            }
        )

    final_admitted = sorted(service._admitted_set(round_))
    final_state = {
        "admitted": [
            {
                "application_id": app_id,
                "pool_id": service._pool_of(round_, app_id),
                "via_promotion": app_id in round_.promoted,
                "confirmed": app_id in round_.confirmed,
            }
            for app_id in final_admitted
        ],
        "waitlist_order_unchanged": list(round_.result.waitlist),
        "holds": sorted(round_.holds),
        "vacated": sorted(round_.vacated),
        "excluded": dict(sorted(round_.result.excluded.items())),
    }

    return ReplayReport(
        round_id=round_id,
        rule_revision=round_.rule_revision,
        rule_hash=rulebook.content_hash,
        result_hash=round_.result_hash,
        chain_ok=True,
        recompute_match=recompute_match,
        ledger=ledger,
        ranking=[
            {
                "rank": r.rank,
                "application_id": r.application_id,
                "pseudonym": r.pseudonym,
                "weighted_score": r.weighted_score,
                "dimension_averages": list(r.dimension_averages),
                "reviewer_count": r.reviewer_count,
                "material_version": r.material_version,
                "material_hash": r.material_hash,
            }
            for r in round_.result.ranked
        ],
        decisions=[d.to_dict() for d in round_.result.decisions],
        quota_view={
            "country_quotas": rulebook.country_seats,
            "program_quotas": {
                p.program_id: {"seats": p.seats, "priority": p.priority}
                for p in rulebook.programs
            },
            "pools": [
                {
                    "pool_id": pid,
                    "seats": seats,
                    "country": country,
                    "program_id": program,
                }
                for pid, seats, country, program in rulebook.ordered_pools()
            ],
        },
        conflicts_applied=conflicts_applied,
        final_state=final_state,
    )
