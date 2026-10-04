"""奖学金名额治理服务。

统一受理申请资格、材料版本、国别与项目配额、定向资金限制与评审回避；
评委独立评分后由确定性引擎形成可解释的排序与分配方案；
候补递补、申诉冻结与改判、并发确认全部经由追加式事件日志落账，
任何规则修订只能开启新一轮分配，历史轮次结果封存不变。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field, replace
from fractions import Fraction
from typing import Dict, List, Optional, Sequence

from . import events as ev
from .applications import (
    ACTIVE,
    MERGED_DUPLICATE,
    Application,
    MaterialVersion,
    application_from_dict,
    application_to_dict,
)
from .engine import (
    ADMITTED,
    WAITLISTED,
    AllocationResult,
    find_promotion,
    pick_fund,
    result_from_dict,
    result_to_dict,
    run_allocation,
    slot_blockers,
)
from .errors import DomainError
from .events import Event, EventLog
from .identity import IdentityRegistry
from .rules import RuleSet, ruleset_from_dict, ruleset_to_dict
from .scoring import (
    ConflictDeclaration,
    ScoreBook,
    ScoreEntry,
    conflict_from_dict,
    conflict_to_dict,
    score_from_dict,
    score_to_dict,
)

PHASE_OPEN = "OPEN"      # 受理申请、评分与回避登记
PHASE_SEALED = "SEALED"  # 分配结果已封存，进入确认、递补与申诉阶段

SLOT_PENDING = "PENDING_CONFIRMATION"
SLOT_CONFIRMED = "CONFIRMED"
SLOT_FROZEN = "FROZEN"
SLOT_CLOSED = "CLOSED"

APPEAL_PENDING = "PENDING"
APPEAL_UPHELD = "UPHELD"
APPEAL_DISMISSED = "DISMISSED"


@dataclass
class SlotState:
    application_id: str
    status: str
    fund_id: str
    pre_freeze_status: Optional[str] = None


@dataclass
class AppealCase:
    appeal_id: str
    target_application_id: str
    grounds: str
    status: str = APPEAL_PENDING


@dataclass
class RoundState:
    """单个轮次的全部可变状态；由事件日志重建，重启后保持一致。"""

    round_id: str
    ruleset: RuleSet
    phase: str = PHASE_OPEN
    applications: Dict[str, Application] = field(default_factory=dict)
    conflicts: List[ConflictDeclaration] = field(default_factory=list)
    scores: ScoreBook = field(default_factory=ScoreBook)
    result: Optional[AllocationResult] = None
    aggregates: Dict[str, Fraction] = field(default_factory=dict)
    remaining_country: Dict[str, int] = field(default_factory=dict)
    remaining_program: Dict[str, int] = field(default_factory=dict)
    remaining_fund: Dict[str, int] = field(default_factory=dict)
    waitlist: List[str] = field(default_factory=list)
    slots: Dict[str, SlotState] = field(default_factory=dict)
    confirmed_amounts: Dict[str, int] = field(default_factory=dict)
    appeals: Dict[str, AppealCase] = field(default_factory=dict)


class GovernanceService:
    """名额治理服务门面；所有公开操作都在锁内完成，确认不破资金上限。"""

    def __init__(self, log_path, *, identity_secret: bytes) -> None:
        self._lock = threading.RLock()
        self._log = EventLog(log_path)
        self._identity = IdentityRegistry(identity_secret)
        self._rulesets: Dict[int, RuleSet] = {}
        self._rounds: Dict[str, RoundState] = {}
        for event in self._log:
            self._apply(event)

    # ------------------------------------------------------------------
    # 规则治理：修订只产生新版本，旧版本与旧轮次不受影响
    # ------------------------------------------------------------------
    def commit_ruleset(self, ruleset: RuleSet) -> RuleSet:
        with self._lock:
            if ruleset.revision in self._rulesets:
                raise DomainError("规则版本已存在：修订规则请产生新的版本号")
            self._rulesets[ruleset.revision] = ruleset
            self._log.append(ev.RULESET_COMMITTED, None, {"ruleset": ruleset_to_dict(ruleset)})
            return ruleset

    def revise_ruleset(self, base_revision: int, **changes) -> RuleSet:
        """在既有版本上修订，得到下一版本号的全新规则集。"""
        with self._lock:
            base = self._rulesets.get(base_revision)
            if base is None:
                raise DomainError(f"规则版本不存在：{base_revision}")
            revised = base.revise(revision=max(self._rulesets) + 1, **changes)
            return self.commit_ruleset(revised)

    def ruleset(self, revision: int) -> RuleSet:
        with self._lock:
            if revision not in self._rulesets:
                raise DomainError(f"规则版本不存在：{revision}")
            return self._rulesets[revision]

    # ------------------------------------------------------------------
    # 轮次与受理
    # ------------------------------------------------------------------
    def open_round(self, round_id: str, rule_revision: int) -> None:
        with self._lock:
            if round_id in self._rounds:
                raise DomainError(f"轮次编号已存在：{round_id}")
            ruleset = self._rulesets.get(rule_revision)
            if ruleset is None:
                raise DomainError(f"规则版本不存在：{rule_revision}")
            self._rounds[round_id] = RoundState(round_id, ruleset)
            # 规则快照随轮次事件落账，离线重放不依赖外部规则库状态。
            self._log.append(ev.ROUND_OPENED, round_id, {"ruleset": ruleset_to_dict(ruleset)})

    def submit_application(
        self,
        round_id: str,
        application_id: str,
        *,
        id_document: str,
        display_name: str,
        channel: str,
        country: str,
        program: str,
        submitted_at: int,
        materials: Sequence[MaterialVersion],
    ) -> Application:
        with self._lock:
            state = self._open_round(round_id)
            if application_id in state.applications:
                raise DomainError(f"申请编号已存在：{application_id}")
            profile = self._identity.register(
                id_document=id_document, display_name=display_name
            )
            application = Application(
                application_id=application_id,
                identity_token=profile.token,
                channel=channel,
                country=country,
                program=program,
                submitted_at=submitted_at,
                materials=tuple(materials),
            )
            state.applications[application_id] = application
            self._log.append(
                ev.APPLICATION_SUBMITTED, round_id,
                {"application": application_to_dict(application)},
            )
            self._merge_duplicates(state)
            return application

    def _merge_duplicates(self, state: RoundState) -> None:
        """同一身份令牌的多渠道重复报名：保留最早提交者，其余合并。

        合并依据只是令牌相等，全程不触碰姓名、证件号等无关个人信息。
        """
        by_token: Dict[str, List[Application]] = {}
        for app in state.applications.values():
            by_token.setdefault(app.identity_token, []).append(app)
        for token, group in by_token.items():
            if len(group) < 2:
                continue
            survivor = min(group, key=lambda a: (a.submitted_at, a.application_id))
            for loser in group:
                if loser.application_id == survivor.application_id or loser.status != ACTIVE:
                    continue
                state.applications[loser.application_id] = replace(
                    loser, status=MERGED_DUPLICATE, merged_into=survivor.application_id
                )
                self._log.append(ev.APPLICATION_MERGED, state.round_id, {
                    "identity_token": token,
                    "merged_application_id": loser.application_id,
                    "surviving_application_id": survivor.application_id,
                    "reason": "同一身份令牌多渠道重复报名",
                })

    def declare_conflict(
        self, round_id: str, *, judge_id: str, id_document: str, relation: str
    ) -> None:
        """登记评委与申请人的利益冲突；按身份令牌生效，换渠道重报也回避。"""
        with self._lock:
            state = self._open_round(round_id)
            declaration = ConflictDeclaration(
                judge_id=judge_id,
                identity_token=self._identity.token_for(id_document),
                relation=relation,
            )
            if declaration in state.conflicts:
                return
            state.conflicts.append(declaration)
            self._log.append(
                ev.CONFLICT_DECLARED, round_id, {"conflict": conflict_to_dict(declaration)}
            )

    def submit_score(
        self, round_id: str, *, judge_id: str, application_id: str, score: int, submitted_at: int
    ) -> None:
        with self._lock:
            state = self._open_round(round_id)
            if application_id not in state.applications:
                raise DomainError(f"申请不存在：{application_id}")
            entry = ScoreEntry(judge_id, application_id, score, submitted_at)
            try:
                state.scores.submit(entry)
            except ValueError as exc:
                raise DomainError(str(exc)) from exc
            self._log.append(ev.SCORE_SUBMITTED, round_id, {"score": score_to_dict(entry)})

    # ------------------------------------------------------------------
    # 分配封存
    # ------------------------------------------------------------------
    def run_allocation(self, round_id: str) -> AllocationResult:
        """封存本轮分配结果；每轮只能封存一次，结果不可更改。"""
        with self._lock:
            state = self._open_round(round_id)
            result = run_allocation(
                applications=tuple(state.applications.values()),
                scores=state.scores.by_application(),
                conflicts=tuple(state.conflicts),
                ruleset=state.ruleset,
            )
            self._seal_round(state, result)
            self._log.append(
                ev.ALLOCATION_COMPUTED, round_id, {"result": result_to_dict(result)}
            )
            return result

    def _seal_round(self, state: RoundState, result: AllocationResult) -> None:
        state.phase = PHASE_SEALED
        state.result = result
        state.remaining_country = dict(state.ruleset.country_quotas)
        state.remaining_program = dict(state.ruleset.program_quotas)
        state.remaining_fund = {f.fund_id: f.total_amount for f in state.ruleset.funds}
        state.aggregates = {}
        state.waitlist = []
        state.slots = {}
        state.confirmed_amounts = {}
        for decision in result.decisions:
            if decision.explanation.aggregate_score is not None:
                state.aggregates[decision.application_id] = decision.explanation.aggregate_score
            if decision.outcome == ADMITTED:
                app = state.applications[decision.application_id]
                self._consume(state, app, decision.explanation.fund_id)
                state.slots[decision.application_id] = SlotState(
                    decision.application_id, SLOT_PENDING, decision.explanation.fund_id
                )
            elif decision.outcome == WAITLISTED:
                state.waitlist.append(decision.application_id)

    # ------------------------------------------------------------------
    # 确认、放弃与撤销：并发安全，守住资金上限
    # ------------------------------------------------------------------
    def confirm_admission(self, round_id: str, application_id: str) -> None:
        with self._lock:
            state = self._sealed_round(round_id)
            slot = state.slots.get(application_id)
            if slot is None:
                raise DomainError("该申请没有可确认的名额（候补须等待递补，不能插队）")
            if slot.status == SLOT_CONFIRMED:
                raise DomainError("名额已确认，不可重复确认")
            if slot.status == SLOT_FROZEN:
                raise DomainError("名额因申诉被冻结，暂不能确认")
            if slot.status != SLOT_PENDING:
                raise DomainError("名额已关闭")
            fund = state.ruleset.fund_by_id(slot.fund_id)
            confirmed = state.confirmed_amounts.get(fund.fund_id, 0)
            if confirmed + fund.award_amount > fund.total_amount:
                raise DomainError("确认将突破资金池上限，已拒绝")
            state.confirmed_amounts[fund.fund_id] = confirmed + fund.award_amount
            slot.status = SLOT_CONFIRMED
            self._log.append(ev.ADMISSION_CONFIRMED, round_id, {
                "application_id": application_id,
                "fund_id": fund.fund_id,
                "amount": fund.award_amount,
                "fund_confirmed_total": state.confirmed_amounts[fund.fund_id],
            })

    def withdraw(self, round_id: str, application_id: str, *, reason: str = "申请人放弃") -> None:
        self._close_slot(round_id, application_id, kind=ev.ADMISSION_WITHDRAWN, reason=reason)

    def revoke_qualification(self, round_id: str, application_id: str, *, reason: str) -> None:
        self._close_slot(round_id, application_id, kind=ev.ADMISSION_REVOKED, reason=reason)

    def _close_slot(
        self,
        round_id: str,
        application_id: str,
        *,
        kind: str,
        reason: str,
        appeal_id: Optional[str] = None,
    ) -> None:
        with self._lock:
            state = self._sealed_round(round_id)
            slot = state.slots.get(application_id)
            if slot is None:
                raise DomainError("该申请没有名额可关闭")
            closable = {SLOT_PENDING, SLOT_CONFIRMED}
            if kind == ev.DECISION_REVERSED:
                closable.add(SLOT_FROZEN)
            if slot.status not in closable:
                if slot.status == SLOT_FROZEN:
                    raise DomainError("名额因申诉被冻结，须先结案申诉")
                raise DomainError("名额已关闭")
            self._mutate_close_slot(state, application_id)
            payload = {
                "application_id": application_id,
                "fund_id": slot.fund_id,
                "reason": reason,
            }
            if appeal_id is not None:
                payload["appeal_id"] = appeal_id
            self._log.append(kind, round_id, payload)
            self._promote_next(state, freed_by=application_id)

    def _mutate_close_slot(self, state: RoundState, application_id: str) -> None:
        slot = state.slots[application_id]
        fund = state.ruleset.fund_by_id(slot.fund_id)
        if slot.status == SLOT_CONFIRMED:
            state.confirmed_amounts[slot.fund_id] -= fund.award_amount
        app = state.applications[application_id]
        self._release(state, app, slot.fund_id)
        slot.status = SLOT_CLOSED
        slot.pre_freeze_status = None

    def _promote_next(self, state: RoundState, *, freed_by: str) -> None:
        """按本轮原规则自动递补；这是候补获得名额的唯一通道。"""
        promotion = find_promotion(
            waitlist=state.waitlist,
            applications=state.applications,
            remaining_country=state.remaining_country,
            remaining_program=state.remaining_program,
            remaining_fund=state.remaining_fund,
            ruleset=state.ruleset,
        )
        if promotion is None:
            return
        self._mutate_promote(state, promotion.application_id, promotion.fund_id)
        self._log.append(ev.CANDIDATE_PROMOTED, state.round_id, {
            "application_id": promotion.application_id,
            "fund_id": promotion.fund_id,
            "freed_by": freed_by,
            "skipped": [
                {"application_id": app_id, "reason": reason}
                for app_id, reason in promotion.skipped
            ],
            "remaining_after": {
                "country": dict(state.remaining_country),
                "program": dict(state.remaining_program),
                "fund": dict(state.remaining_fund),
            },
        })

    def _mutate_promote(self, state: RoundState, application_id: str, fund_id: str) -> None:
        state.waitlist.remove(application_id)
        app = state.applications[application_id]
        self._consume(state, app, fund_id)
        state.slots[application_id] = SlotState(application_id, SLOT_PENDING, fund_id)

    # ------------------------------------------------------------------
    # 申诉：只冻结受影响的名额，其余录取照常推进
    # ------------------------------------------------------------------
    def file_appeal(
        self, round_id: str, appeal_id: str, *, target_application_id: str, grounds: str
    ) -> None:
        with self._lock:
            state = self._sealed_round(round_id)
            if appeal_id in state.appeals:
                raise DomainError(f"申诉编号已存在：{appeal_id}")
            try:
                state.result.decision_for(target_application_id)
            except KeyError:
                raise DomainError("该申请在本轮没有决策记录，无法申诉") from None
            state.appeals[appeal_id] = AppealCase(appeal_id, target_application_id, grounds)
            self._log.append(ev.APPEAL_FILED, round_id, {
                "appeal_id": appeal_id,
                "target_application_id": target_application_id,
                "grounds": grounds,
            })
            slot = state.slots.get(target_application_id)
            if slot is not None and slot.status in (SLOT_PENDING, SLOT_CONFIRMED):
                slot.pre_freeze_status = slot.status
                slot.status = SLOT_FROZEN
                self._log.append(ev.SLOT_FROZEN, round_id, {
                    "application_id": target_application_id,
                    "appeal_id": appeal_id,
                })

    def resolve_appeal(
        self,
        round_id: str,
        appeal_id: str,
        *,
        upheld: bool,
        note: str,
        corrected_score: Optional[int] = None,
        materials_completed: bool = False,
    ) -> None:
        """结案申诉。驳回则解冻；成立则改判：撤销涉事录取并递补，
        或以更正后的成绩把候选人按确定性位置重新纳入分配。"""
        with self._lock:
            state = self._sealed_round(round_id)
            appeal = state.appeals.get(appeal_id)
            if appeal is None:
                raise DomainError(f"申诉不存在：{appeal_id}")
            if appeal.status != APPEAL_PENDING:
                raise DomainError("申诉已结案")
            appeal.status = APPEAL_UPHELD if upheld else APPEAL_DISMISSED
            self._log.append(ev.APPEAL_RESOLVED, round_id, {
                "appeal_id": appeal_id,
                "upheld": upheld,
                "note": note,
                "corrected_score": corrected_score,
                "materials_completed": materials_completed,
            })
            target = appeal.target_application_id
            slot = state.slots.get(target)
            if not upheld:
                if slot is not None and slot.status == SLOT_FROZEN:
                    slot.status = slot.pre_freeze_status or SLOT_PENDING
                    slot.pre_freeze_status = None
                    self._log.append(ev.SLOT_UNFROZEN, round_id, {
                        "application_id": target,
                        "appeal_id": appeal_id,
                    })
                return
            if slot is not None and slot.status in (SLOT_FROZEN, SLOT_PENDING, SLOT_CONFIRMED):
                self._close_slot(
                    round_id, target,
                    kind=ev.DECISION_REVERSED,
                    reason=f"申诉 {appeal_id} 成立：{note}",
                    appeal_id=appeal_id,
                )
            elif slot is not None:
                raise DomainError("名额已关闭，无需改判")
            else:
                self._reinstate(state, appeal, corrected_score, materials_completed, note)

    def _reinstate(
        self,
        state: RoundState,
        appeal: AppealCase,
        corrected_score: Optional[int],
        materials_completed: bool,
        note: str,
    ) -> None:
        target = appeal.target_application_id
        app = state.applications[target]
        if app.status != ACTIVE:
            raise DomainError("重复报名已合并的申请不能通过申诉恢复")
        decision = state.result.decision_for(target)
        if corrected_score is not None:
            if not 0 <= corrected_score <= 100:
                raise DomainError("更正成绩超出范围")
            aggregate = Fraction(corrected_score, 1)
        else:
            aggregate = state.aggregates.get(target)
        if aggregate is None:
            raise DomainError("该申请没有可用成绩，申诉改判须提供更正成绩")
        if decision.explanation.missing_materials and not materials_completed:
            raise DomainError("材料缺失未补正，不能恢复候选资格")
        state.aggregates[target] = aggregate
        blockers = slot_blockers(
            app, state.remaining_country, state.remaining_program,
            state.remaining_fund, state.ruleset,
        )
        payload = {
            "application_id": target,
            "appeal_id": appeal.appeal_id,
            "aggregate_score": f"{aggregate.numerator}/{aggregate.denominator}",
            "note": note,
        }
        if not blockers:
            fund = pick_fund(state.ruleset.funds, state.remaining_fund, app.country, app.program)
            if target in state.waitlist:
                state.waitlist.remove(target)
            self._consume(state, app, fund.fund_id)
            state.slots[target] = SlotState(target, SLOT_PENDING, fund.fund_id)
            payload.update({
                "outcome": ADMITTED, "fund_id": fund.fund_id,
                "waitlist_position": None, "blockers": [],
            })
        else:
            if target in state.waitlist:
                state.waitlist.remove(target)
            index = self._waitlist_insert_index(state, app, aggregate)
            state.waitlist.insert(index, target)
            payload.update({
                "outcome": WAITLISTED, "fund_id": None,
                "waitlist_position": index + 1, "blockers": list(blockers),
            })
        self._log.append(ev.CANDIDATE_REINSTATED, state.round_id, payload)

    @staticmethod
    def _waitlist_insert_index(state: RoundState, app: Application, aggregate: Fraction) -> int:
        key = (-aggregate, app.submitted_at, app.application_id)
        index = 0
        for other_id in state.waitlist:
            other = state.applications[other_id]
            other_key = (-state.aggregates[other_id], other.submitted_at, other.application_id)
            if other_key < key:
                index += 1
            else:
                break
        return index

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def round_result(self, round_id: str) -> AllocationResult:
        with self._lock:
            state = self._sealed_round(round_id)
            return state.result

    def waitlist(self, round_id: str) -> List[str]:
        with self._lock:
            return list(self._round(round_id).waitlist)

    def slot_status(self, round_id: str, application_id: str) -> Optional[str]:
        with self._lock:
            slot = self._round(round_id).slots.get(application_id)
            return None if slot is None else slot.status

    def appeal_status(self, round_id: str, appeal_id: str) -> str:
        with self._lock:
            appeal = self._round(round_id).appeals.get(appeal_id)
            if appeal is None:
                raise DomainError(f"申诉不存在：{appeal_id}")
            return appeal.status

    def fund_usage(self, round_id: str) -> Dict[str, Dict[str, int]]:
        with self._lock:
            state = self._round(round_id)
            return {
                fund.fund_id: {
                    "total": fund.total_amount,
                    "confirmed": state.confirmed_amounts.get(fund.fund_id, 0),
                    "reserved_remaining": state.remaining_fund.get(fund.fund_id, 0),
                }
                for fund in state.ruleset.funds
            }

    def duplicate_report(self, round_id: str) -> List[dict]:
        """重复报名合并报告：只含身份令牌与申请编号，不含任何明文个人信息。"""
        with self._lock:
            state = self._round(round_id)
            by_token: Dict[str, List[Application]] = {}
            for app in state.applications.values():
                by_token.setdefault(app.identity_token, []).append(app)
            report = []
            for token, group in by_token.items():
                if len(group) < 2:
                    continue
                group.sort(key=lambda a: (a.submitted_at, a.application_id))
                report.append({
                    "identity_token": token,
                    "surviving_application_id": group[0].application_id,
                    "applications": [{
                        "application_id": app.application_id,
                        "channel": app.channel,
                        "status": app.status,
                        "merged_into": app.merged_into,
                    } for app in group],
                })
            return report

    def identity_token_for(self, id_document: str) -> str:
        return self._identity.token_for(id_document)

    def round_report(self, round_id: str) -> dict:
        with self._lock:
            state = self._round(round_id)
            report = {
                "round_id": round_id,
                "rule_revision": state.ruleset.revision,
                "phase": state.phase,
                "waitlist": list(state.waitlist),
                "funds": self.fund_usage(round_id),
                "appeals": {aid: case.status for aid, case in state.appeals.items()},
            }
            if state.result is not None:
                report["decisions"] = [{
                    "application_id": d.application_id,
                    "outcome": d.outcome,
                    "rank": d.rank,
                    "waitlist_position": d.waitlist_position,
                    "reason": d.explanation.reason,
                    "fund_id": d.explanation.fund_id,
                    "aggregate_score": (
                        None if d.explanation.aggregate_score is None
                        else f"{d.explanation.aggregate_score.numerator}/{d.explanation.aggregate_score.denominator}"
                    ),
                    "recused_judges": [list(pair) for pair in d.explanation.recused_judges],
                    "blockers": list(d.explanation.blockers),
                } for d in state.result.decisions]
                report["slot_status"] = {
                    app_id: slot.status for app_id, slot in state.slots.items()
                }
            return report

    # ------------------------------------------------------------------
    # 额度变更原语
    # ------------------------------------------------------------------
    @staticmethod
    def _consume(state: RoundState, app: Application, fund_id: str) -> None:
        if app.country in state.remaining_country:
            state.remaining_country[app.country] -= 1
        if app.program in state.remaining_program:
            state.remaining_program[app.program] -= 1
        fund = state.ruleset.fund_by_id(fund_id)
        state.remaining_fund[fund_id] -= fund.award_amount

    @staticmethod
    def _release(state: RoundState, app: Application, fund_id: str) -> None:
        if app.country in state.remaining_country:
            state.remaining_country[app.country] += 1
        if app.program in state.remaining_program:
            state.remaining_program[app.program] += 1
        fund = state.ruleset.fund_by_id(fund_id)
        state.remaining_fund[fund_id] += fund.award_amount

    # ------------------------------------------------------------------
    # 状态恢复：重放事件日志，重启后候补顺序与额度保持一致
    # ------------------------------------------------------------------
    def _round(self, round_id: str) -> RoundState:
        state = self._rounds.get(round_id)
        if state is None:
            raise DomainError(f"轮次不存在：{round_id}")
        return state

    def _open_round(self, round_id: str) -> RoundState:
        state = self._round(round_id)
        if state.phase != PHASE_OPEN:
            raise DomainError("本轮分配结果已封存，不再受理变更")
        return state

    def _sealed_round(self, round_id: str) -> RoundState:
        state = self._round(round_id)
        if state.phase != PHASE_SEALED:
            raise DomainError("本轮尚未封存分配结果")
        return state

    def _apply(self, event: Event) -> None:
        kind = event.kind
        payload = event.payload
        if kind == ev.RULESET_COMMITTED:
            ruleset = ruleset_from_dict(payload["ruleset"])
            self._rulesets[ruleset.revision] = ruleset
            return
        if kind == ev.ROUND_OPENED:
            ruleset = ruleset_from_dict(payload["ruleset"])
            self._rounds[event.round_id] = RoundState(event.round_id, ruleset)
            return
        state = self._rounds[event.round_id]
        if kind == ev.APPLICATION_SUBMITTED:
            app = application_from_dict(payload["application"])
            state.applications[app.application_id] = app
        elif kind == ev.APPLICATION_MERGED:
            app = state.applications[payload["merged_application_id"]]
            state.applications[app.application_id] = replace(
                app, status=MERGED_DUPLICATE,
                merged_into=payload["surviving_application_id"],
            )
        elif kind == ev.CONFLICT_DECLARED:
            state.conflicts.append(conflict_from_dict(payload["conflict"]))
        elif kind == ev.SCORE_SUBMITTED:
            state.scores.submit(score_from_dict(payload["score"]))
        elif kind == ev.ALLOCATION_COMPUTED:
            self._seal_round(state, result_from_dict(payload["result"]))
        elif kind == ev.ADMISSION_CONFIRMED:
            slot = state.slots[payload["application_id"]]
            slot.status = SLOT_CONFIRMED
            state.confirmed_amounts[payload["fund_id"]] = (
                state.confirmed_amounts.get(payload["fund_id"], 0) + payload["amount"]
            )
        elif kind in (ev.ADMISSION_WITHDRAWN, ev.ADMISSION_REVOKED, ev.DECISION_REVERSED):
            self._mutate_close_slot(state, payload["application_id"])
        elif kind == ev.CANDIDATE_PROMOTED:
            self._mutate_promote(state, payload["application_id"], payload["fund_id"])
        elif kind == ev.APPEAL_FILED:
            state.appeals[payload["appeal_id"]] = AppealCase(
                payload["appeal_id"], payload["target_application_id"], payload["grounds"]
            )
        elif kind == ev.SLOT_FROZEN:
            slot = state.slots[payload["application_id"]]
            slot.pre_freeze_status = slot.status
            slot.status = SLOT_FROZEN
        elif kind == ev.SLOT_UNFROZEN:
            slot = state.slots[payload["application_id"]]
            slot.status = slot.pre_freeze_status or SLOT_PENDING
            slot.pre_freeze_status = None
        elif kind == ev.APPEAL_RESOLVED:
            appeal = state.appeals[payload["appeal_id"]]
            appeal.status = APPEAL_UPHELD if payload["upheld"] else APPEAL_DISMISSED
        elif kind == ev.CANDIDATE_REINSTATED:
            target = payload["application_id"]
            state.aggregates[target] = Fraction(payload["aggregate_score"])
            if target in state.waitlist:
                state.waitlist.remove(target)
            if payload["outcome"] == ADMITTED:
                app = state.applications[target]
                self._consume(state, app, payload["fund_id"])
                state.slots[target] = SlotState(target, SLOT_PENDING, payload["fund_id"])
            else:
                state.waitlist.insert(payload["waitlist_position"] - 1, target)
        else:
            raise ValueError(f"未知事件类型：{kind}")
