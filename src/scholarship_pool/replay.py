"""离线重放：从事件日志还原一轮分配的每一步决策依据。

重放分两步保证可信：
1. 用日志中的输入事件（规则快照、申请、评分、回避、合并）独立重算
   分配结果，与日志封存的结果逐字节比对，不一致即判定日志被篡改；
2. 按事件顺序折叠后续确认、递补、冻结与改判，逐一还原每条决策
   当时所使用的规则版本、额度与利益冲突处理。
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Mapping, Optional, Tuple

from . import events as ev
from .applications import MERGED_DUPLICATE, application_from_dict
from .engine import (
    ADMITTED,
    result_from_dict,
    result_to_dict,
    run_allocation,
)
from .errors import DomainError, ReplayIntegrityError
from .events import EventLog
from .rules import ruleset_from_dict
from .scoring import conflict_from_dict, score_from_dict


@dataclass(frozen=True)
class TraceEntry:
    """一条决策轨迹：谁、在什么序号的事件上、被如何处理、依据是什么。"""

    seq: int
    application_id: Optional[str]
    action: str
    rule_revision: int
    detail: Mapping


@dataclass(frozen=True)
class ReplayReport:
    round_id: str
    rule_revision: int
    verified: bool
    traces: Tuple[TraceEntry, ...]
    final_waitlist: Tuple[str, ...]
    fund_usage: Mapping

    def traces_for(self, application_id: str) -> Tuple[TraceEntry, ...]:
        return tuple(t for t in self.traces if t.application_id == application_id)


def replay_round(log_path, round_id: str) -> ReplayReport:
    log = EventLog(log_path)
    events = log.for_round(round_id)
    if not events:
        raise DomainError(f"轮次不存在：{round_id}")

    ruleset = None
    applications = {}
    conflicts = []
    scores = {}
    recorded = None
    allocation_seq = None
    for event in events:
        payload = event.payload
        if event.kind == ev.ROUND_OPENED:
            ruleset = ruleset_from_dict(payload["ruleset"])
        elif event.kind == ev.APPLICATION_SUBMITTED:
            app = application_from_dict(payload["application"])
            applications[app.application_id] = app
        elif event.kind == ev.APPLICATION_MERGED:
            target = applications[payload["merged_application_id"]]
            applications[target.application_id] = replace(
                target, status=MERGED_DUPLICATE,
                merged_into=payload["surviving_application_id"],
            )
        elif event.kind == ev.CONFLICT_DECLARED:
            conflicts.append(conflict_from_dict(payload["conflict"]))
        elif event.kind == ev.SCORE_SUBMITTED:
            entry = score_from_dict(payload["score"])
            scores.setdefault(entry.application_id, []).append(entry)
        elif event.kind == ev.ALLOCATION_COMPUTED:
            recorded = result_from_dict(payload["result"])
            allocation_seq = event.seq
    if ruleset is None:
        raise DomainError("日志缺少轮次开启事件，无法重放")
    if recorded is None:
        raise DomainError("该轮尚未封存分配结果，无法重放")

    recomputed = run_allocation(
        applications=tuple(applications.values()),
        scores=scores,
        conflicts=tuple(conflicts),
        ruleset=ruleset,
    )
    if result_to_dict(recomputed) != result_to_dict(recorded):
        raise ReplayIntegrityError("重算结果与日志记录不一致：日志可能被篡改")

    traces = []
    for decision in recorded.decisions:
        explanation = decision.explanation
        traces.append(TraceEntry(
            seq=allocation_seq,
            application_id=decision.application_id,
            action=decision.outcome,
            rule_revision=ruleset.revision,
            detail={
                "reason": explanation.reason,
                "fund_id": explanation.fund_id,
                "aggregate_score": (
                    None if explanation.aggregate_score is None
                    else f"{explanation.aggregate_score.numerator}/{explanation.aggregate_score.denominator}"
                ),
                "valid_judges": list(explanation.valid_judges),
                "recused_judges": [list(pair) for pair in explanation.recused_judges],
                "missing_materials": list(explanation.missing_materials),
                "blockers": list(explanation.blockers),
                "rank": decision.rank,
                "waitlist_position": decision.waitlist_position,
            },
        ))

    waitlist = [d.application_id for d in recorded.waitlisted()]
    fund_usage = {fund.fund_id: 0 for fund in ruleset.funds}
    for event in events:
        if event.seq <= allocation_seq:
            continue
        payload = event.payload
        kind = event.kind
        if kind == ev.ADMISSION_CONFIRMED:
            fund_usage[payload["fund_id"]] += payload["amount"]
            traces.append(TraceEntry(
                event.seq, payload["application_id"], "CONFIRMED", ruleset.revision,
                {"fund_id": payload["fund_id"], "amount": payload["amount"],
                 "fund_confirmed_total": payload["fund_confirmed_total"]},
            ))
        elif kind in (ev.ADMISSION_WITHDRAWN, ev.ADMISSION_REVOKED):
            traces.append(TraceEntry(
                event.seq, payload["application_id"], kind, ruleset.revision,
                {"reason": payload["reason"], "fund_id": payload["fund_id"]},
            ))
        elif kind == ev.DECISION_REVERSED:
            traces.append(TraceEntry(
                event.seq, payload["application_id"], kind, ruleset.revision,
                {"reason": payload["reason"], "appeal_id": payload.get("appeal_id"),
                 "fund_id": payload["fund_id"]},
            ))
        elif kind == ev.CANDIDATE_PROMOTED:
            waitlist.remove(payload["application_id"])
            traces.append(TraceEntry(
                event.seq, payload["application_id"], "PROMOTED", ruleset.revision,
                {"fund_id": payload["fund_id"], "freed_by": payload["freed_by"],
                 "skipped": payload["skipped"], "remaining_after": payload["remaining_after"]},
            ))
        elif kind == ev.CANDIDATE_REINSTATED:
            if payload["application_id"] in waitlist:
                waitlist.remove(payload["application_id"])
            if payload["outcome"] != ADMITTED:
                waitlist.insert(payload["waitlist_position"] - 1, payload["application_id"])
            traces.append(TraceEntry(
                event.seq, payload["application_id"], "REINSTATED", ruleset.revision,
                {"appeal_id": payload["appeal_id"], "outcome": payload["outcome"],
                 "aggregate_score": payload["aggregate_score"],
                 "fund_id": payload.get("fund_id"),
                 "waitlist_position": payload.get("waitlist_position"),
                 "blockers": payload.get("blockers")},
            ))
        elif kind == ev.SLOT_FROZEN:
            traces.append(TraceEntry(
                event.seq, payload["application_id"], "SLOT_FROZEN", ruleset.revision,
                {"appeal_id": payload["appeal_id"]},
            ))
        elif kind == ev.SLOT_UNFROZEN:
            traces.append(TraceEntry(
                event.seq, payload["application_id"], "SLOT_UNFROZEN", ruleset.revision,
                {"appeal_id": payload["appeal_id"]},
            ))
        elif kind == ev.APPEAL_FILED:
            traces.append(TraceEntry(
                event.seq, payload["target_application_id"], "APPEAL_FILED", ruleset.revision,
                {"appeal_id": payload["appeal_id"], "grounds": payload["grounds"]},
            ))
        elif kind == ev.APPEAL_RESOLVED:
            traces.append(TraceEntry(
                event.seq, None, "APPEAL_RESOLVED", ruleset.revision,
                {"appeal_id": payload["appeal_id"], "upheld": payload["upheld"],
                 "note": payload["note"]},
            ))
    return ReplayReport(
        round_id=round_id,
        rule_revision=ruleset.revision,
        verified=True,
        traces=tuple(traces),
        final_waitlist=tuple(waitlist),
        fund_usage=fund_usage,
    )
