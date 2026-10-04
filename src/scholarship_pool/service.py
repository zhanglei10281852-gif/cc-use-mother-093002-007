"""奖学金名额治理主编排服务。

所有写操作都在同一把全局锁内完成「校验 → 追加哈希链事件 → 应用」，
因此并发确认/递补不可能突破资金上限；状态完全由事件日志重放得到，
进程重启后候补顺序、冻结状态与已确认结果保持不变。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field

from .allocation import (
    CandidateInput,
    AllocationResult,
    allocate,
    find_promotion,
)
from .applications import ApplicationRegistry, ELIGIBLE
from .contracts import canonical_json, sha256_hex
from .errors import (
    CapacityExceededError,
    NotFoundError,
    RoundStateError,
    RuleRevisionError,
    SeatHeldError,
    UnresolvedDuplicateError,
    ValidationError,
)
from .identity import IdentityRegistry
from .journal import EventStore
from .review import ReviewBoard, ScoringSession
from .rules import RuleBook

SCORING = "scoring"
CLOSED = "scoring_closed"
ALLOCATED = "allocated"


@dataclass
class Appeal:
    appeal_id: str
    application_id: str
    round_id: str
    reason: str
    status: str = "pending"  # pending | rejected | upheld | regraded
    resolution: str = ""


@dataclass
class RoundState:
    round_id: str
    rule_revision: int
    material_pin: dict[str, list]
    state: str
    scoring: ScoringSession
    scores: dict[tuple[str, str], dict[str, float]] = field(default_factory=dict)
    result: AllocationResult | None = None
    result_hash: str = ""
    amended_ranked: list | None = None
    holds: set[str] = field(default_factory=set)
    appeals: dict[str, Appeal] = field(default_factory=dict)
    # 改判中追加剔除的评分：app -> {reviewer: reason}
    excluded_sheets: dict[str, dict[str, str]] = field(default_factory=dict)
    amended_scores: dict[str, float] = field(default_factory=dict)
    vacated: set[str] = field(default_factory=set)
    promoted: dict[str, str] = field(default_factory=dict)
    confirmed: set[str] = field(default_factory=set)
    # 已释放但候补冻结/资格不符而暂未补上的空置名额：pool_id -> 数量
    vacant_pools: dict[str, int] = field(default_factory=dict)
    # 已公告过“无可递补者”的空置数量，避免重复记账
    last_skipped_announced: dict[str, int] = field(default_factory=dict)
    # 分配时刻冻结的候选输入快照（评分与回避名单），供审计独立重算
    candidate_snapshot: list | None = None


class ScholarshipGovernanceService:
    def __init__(self, event_store: EventStore | None = None) -> None:
        self._lock = threading.RLock()
        self.store = event_store or EventStore()
        self.rules: dict[int, RuleBook] = {}
        self.identities = IdentityRegistry()
        self.applications = ApplicationRegistry()
        self.board = ReviewBoard()
        self.rounds: dict[str, RoundState] = {}
        self._duplicate_flags: list[dict] = []
        # application_id -> person_id（事件重放身份登记所需）
        self._app_person: dict[str, str] = {}
        for event in self.store.read():
            self._apply(event)

    # ============================ 规则书 ============================
    def publish_rulebook(self, rulebook: RuleBook) -> int:
        with self._lock:
            if rulebook.revision in self.rules:
                raise RuleRevisionError(
                    f"规则修订 {rulebook.revision} 已存在，规则一经发布不可修改；"
                    "请发布新的修订"
                )
            expected = max(self.rules, default=0) + 1
            if rulebook.revision != expected:
                raise RuleRevisionError(
                    f"下一个修订号必须为 {expected}，不能跳号或改写历史"
                )
            self.store.append(
                "rule_published",
                {"rulebook": rulebook.to_dict(), "content_hash": rulebook.content_hash},
            )
            self.rules[rulebook.revision] = rulebook
            return rulebook.revision

    def rulebook(self, revision: int) -> RuleBook:
        try:
            return self.rules[revision]
        except KeyError:
            raise NotFoundError(f"规则修订 {revision} 不存在") from None

    # ============================ 身份与申请 ============================
    def register_application(
        self,
        application_id: str,
        country: str,
        program_id: str,
        channel: str,
        materials: dict[str, str],
        sensitive: dict[str, str],
    ) -> str:
        """登记一份新申请：生成假名身份与首版材料。

        敏感字段（证件号、渠道账号等）只保存在进程内身份登记册，
        不写入事件日志；日志仅记录假名与材料哈希。
        """
        with self._lock:
            person_id = self.identities.register(application_id, sensitive)
            pseudonym = self.identities.pseudonym(person_id)
            self.store.append(
                "identity_registered",
                {
                    "person_id": person_id,
                    "pseudonym": pseudonym,
                    "application_id": application_id,
                },
            )
            version = self.applications.submit(
                application_id, person_id, country, program_id, channel, materials
            )
            self._app_person[application_id] = person_id
            self.store.append(
                "application_submitted",
                {
                    "application_id": application_id,
                    "person_id": person_id,
                    "country": country,
                    "program_id": program_id,
                    "channel": channel,
                    "material_fields": [list(pair) for pair in version.fields],
                    "material_version": version.version,
                    "material_hash": version.content_hash,
                },
            )
            return person_id

    def update_materials(self, application_id: str, materials: dict[str, str]) -> int:
        with self._lock:
            version = self.applications.update_material(application_id, materials)
            self.store.append(
                "material_updated",
                {
                    "application_id": application_id,
                    "material_fields": [list(pair) for pair in version.fields],
                    "material_version": version.version,
                    "material_hash": version.content_hash,
                },
            )
            return version.version

    def flag_duplicate(self, app_a: str, app_b: str, evidence_ref: str) -> None:
        """登记疑似重复报名线索（证据用内部编号引用，不含个人信息）。"""
        with self._lock:
            if app_a == app_b:
                raise ValidationError("重复线索必须指向两份不同申请")
            owner_a = self.identities.owner_of(app_a)
            owner_b = self.identities.owner_of(app_b)
            if owner_a == owner_b:
                return  # 已合并，线索自然消解
            self.store.append(
                "duplicate_flagged",
                {"app_a": app_a, "app_b": app_b, "evidence_ref": evidence_ref},
            )
            self._duplicate_flags.append(
                {"app_a": app_a, "app_b": app_b, "resolved": False}
            )

    def unresolved_duplicates(self) -> list[dict]:
        unresolved = []
        for flag in self._duplicate_flags:
            if flag["resolved"]:
                continue
            if (
                self.identities.owner_of(flag["app_a"])
                == self.identities.owner_of(flag["app_b"])
            ):
                continue
            unresolved.append(flag)
        return unresolved

    def merge_duplicates(self, source_app: str, target_app: str) -> str:
        """合并两个申请背后的自然人身份，单向且不可撤销。

        合并后源申请标记为重复并入，不再参与任何轮次；
        已开启或已分配轮次涉及的申请不能合并，只能在新一轮处理。
        """
        with self._lock:
            for round_ in self.rounds.values():
                pinned = set(round_.material_pin)
                if source_app in pinned or target_app in pinned:
                    raise RoundStateError(
                        f"轮次 {round_.round_id} 已引用相关申请，"
                        "身份合并只能在轮次开启前进行；请在新一轮处理"
                    )
            target = self.identities.merge(source_app, target_app)
            self.applications.supersede(source_app, target_app)
            self.store.append(
                "duplicate_merged",
                {
                    "source_application_id": source_app,
                    "target_application_id": target_app,
                },
            )
            source_person = self._app_person[source_app]
            # 评委冲突记录迁移到主身份
            self.board.merge_person(source_person, target)
            return target

    # ============================ 评委与回避 ============================
    def register_reviewer(self, reviewer_id: str) -> None:
        with self._lock:
            self.board.register_reviewer(reviewer_id)
            self.store.append("reviewer_registered", {"reviewer_id": reviewer_id})

    def declare_conflict(self, reviewer_id: str, application_id: str) -> None:
        """申报评委与申请人（自然人身份）的合作/利害关系。"""
        with self._lock:
            self.board.require_reviewer(reviewer_id)
            person_id = self.identities.owner_of(application_id)
            if self.board.is_conflicted(reviewer_id, person_id):
                return
            self.store.append(
                "conflict_declared",
                {"reviewer_id": reviewer_id, "person_id": person_id},
            )
            self.board.declare_conflict(reviewer_id, person_id)

    # ============================ 轮次 ============================
    def open_round(self, round_id: str, rule_revision: int) -> None:
        with self._lock:
            if round_id in self.rounds:
                raise ValidationError(f"轮次 {round_id} 已存在")
            rulebook = self.rulebook(rule_revision)
            unresolved = self.unresolved_duplicates()
            if unresolved:
                raise UnresolvedDuplicateError(
                    "仍有未合并的重复身份线索，不能开启新一轮："
                    + "; ".join(f"{f['app_a']}≈{f['app_b']}" for f in unresolved)
                )
            pin: dict[str, list] = {}
            for app in self.applications.all():
                if app.status != ELIGIBLE:
                    continue
                if app.country not in rulebook.countries:
                    raise ValidationError(
                        f"申请 {app.application_id} 国别 {app.country} 不在规则范围内"
                    )
                if app.program_id not in rulebook.program_seats:
                    raise ValidationError(
                        f"申请 {app.application_id} 项目 {app.program_id} 不在规则范围内"
                    )
                if app.channel not in rulebook.channels:
                    raise ValidationError(
                        f"申请 {app.application_id} 渠道 {app.channel} 未被规则认可"
                    )
                codes = {c for c, _ in app.current_material.fields}
                missing = rulebook.required_materials - codes
                if missing:
                    raise ValidationError(
                        f"申请 {app.application_id} 缺少必备材料：{sorted(missing)}"
                    )
                pin[app.application_id] = [
                    app.current_material.version, app.current_material.content_hash
                ]
            self.store.append(
                "round_opened",
                {
                    "round_id": round_id,
                    "rule_revision": rule_revision,
                    "rule_hash": rulebook.content_hash,
                    "material_pin": pin,
                },
                round_id=round_id,
            )
            self.rounds[round_id] = RoundState(
                round_id=round_id,
                rule_revision=rule_revision,
                material_pin=pin,
                state=SCORING,
                scoring=ScoringSession(rulebook.dimension_codes),
            )

    def submit_score(
        self,
        round_id: str,
        reviewer_id: str,
        application_id: str,
        scores: dict[str, float],
    ) -> None:
        """评委独立提交评分；提交后不可修改、不可重复提交。"""
        with self._lock:
            round_ = self._round(round_id)
            if round_.state != SCORING:
                raise RoundStateError("该轮评分已截止")
            self.board.require_reviewer(reviewer_id)
            if application_id not in round_.material_pin:
                raise NotFoundError("该申请不在本轮参评范围内")
            person_id = self.identities.owner_of(application_id)
            self.board.require_no_conflict(reviewer_id, person_id)
            if not self.applications.is_live(application_id):
                raise RoundStateError("该申请已放弃或被撤销资格，不能再评分")
            if (reviewer_id, application_id) in round_.scores:
                raise RoundStateError("评委对该申请已独立提交评分，不能重复提交")
            round_.scoring.submit(
                reviewer_id, application_id, dict(scores), conflicted=False
            )
            self.store.append(
                "score_submitted",
                {
                    "round_id": round_id,
                    "reviewer_id": reviewer_id,
                    "application_id": application_id,
                    "scores": {k: float(v) for k, v in sorted(scores.items())},
                },
                round_id=round_id,
            )
            round_.scores[(reviewer_id, application_id)] = dict(scores)

    def close_scoring(self, round_id: str) -> None:
        with self._lock:
            round_ = self._round(round_id)
            if round_.state != SCORING:
                raise RoundStateError("评分已经截止")
            round_.scoring.close()
            round_.state = CLOSED
            self.store.append("scoring_closed", {"round_id": round_id}, round_id=round_id)

    # ============================ 分配 ============================
    def _build_candidates(self, round_: RoundState) -> list[CandidateInput]:
        rulebook = self.rulebook(round_.rule_revision)
        candidates: list[CandidateInput] = []
        for application_id, pin in sorted(round_.material_pin.items()):
            app = self.applications.get(application_id)
            if app.status != ELIGIBLE:
                continue
            person_id = self.identities.owner_of(application_id)
            pseudonym = self.identities.pseudonym(person_id)
            conflicts = self.board.conflicts_for(person_id)
            excluded = tuple(
                sorted((r, "利益冲突回避") for r in conflicts)
            )
            sheets = {
                reviewer: dict(dims)
                for (reviewer, app_id), dims in round_.scores.items()
                if app_id == application_id and reviewer not in conflicts
            }
            candidates.append(
                CandidateInput(
                    application_id=application_id,
                    pseudonym=pseudonym,
                    country=app.country,
                    program_id=app.program_id,
                    program_priority=rulebook.program_priorities[app.program_id],
                    material_version=pin[0],
                    material_hash=pin[1],
                    sheets=sheets,
                    excluded_reviewers=excluded,
                )
            )
        return candidates

    def run_allocation(self, round_id: str) -> AllocationResult:
        """评分截止后执行一次确定性分配；结果冻结，不可重跑。"""
        with self._lock:
            round_ = self._round(round_id)
            if round_.state != CLOSED:
                raise RoundStateError("只有评分截止后的轮次才能执行分配")
            if round_.result is not None:
                raise RoundStateError("本轮分配结果已冻结，不能重跑；改规则请开新一轮")
            rulebook = self.rulebook(round_.rule_revision)
            candidates = self._build_candidates(round_)
            result = allocate(rulebook, candidates)
            result_hash = sha256_hex(canonical_json(result.to_dict()))
            snapshot = [c.to_dict() for c in candidates]
            round_.result = result
            round_.result_hash = result_hash
            round_.candidate_snapshot = snapshot
            round_.state = ALLOCATED
            self.store.append(
                "round_allocated",
                {
                    "round_id": round_id,
                    "rule_revision": round_.rule_revision,
                    "candidates": snapshot,
                    "result": result.to_dict(),
                    "result_hash": result_hash,
                },
                round_id=round_id,
            )
            return result

    def _round(self, round_id: str) -> RoundState:
        try:
            return self.rounds[round_id]
        except KeyError:
            raise NotFoundError(f"轮次 {round_id} 不存在") from None

    # ---------- 名额占用的派生视图（全部从事件状态推导） ----------
    def _admitted_set(self, round_: RoundState) -> set[str]:
        admitted = set(round_.result.admitted) - round_.vacated
        admitted.update(round_.promoted)
        return admitted

    def _pool_of(self, round_: RoundState, application_id: str) -> str | None:
        if application_id in round_.promoted:
            return round_.promoted[application_id]
        if application_id in round_.result.admitted and application_id not in round_.vacated:
            return round_.result.admitted[application_id]
        return None

    def _usage(self, round_: RoundState) -> tuple[dict[str, int], dict[str, int]]:
        country_used: dict[str, int] = {}
        program_used: dict[str, int] = {}
        ranks = {r.application_id: r for r in round_.result.ranked}
        for application_id in self._admitted_set(round_):
            ranked = ranks[application_id]
            country_used[ranked.country] = country_used.get(ranked.country, 0) + 1
            program_used[ranked.program_id] = (
                program_used.get(ranked.program_id, 0) + 1
            )
        return country_used, program_used

    def confirm_admission(self, round_id: str, application_id: str) -> None:
        """并发安全的录取确认：冻结中的名额不能确认。"""
        with self._lock:
            round_ = self._allocated(round_id)
            if application_id in round_.holds:
                raise SeatHeldError("该名额处于申诉冻结中，暂不能确认")
            if self._pool_of(round_, application_id) is None:
                raise NotFoundError("该申请当前不占名额，无法确认")
            if application_id in round_.confirmed:
                raise RoundStateError("该名额已确认，请勿重复操作")
            # 守住资金上限：当前占用不得超过任何一类额度
            self._check_capacity(round_)
            round_.confirmed.add(application_id)
            self.store.append(
                "seat_confirmed",
                {"round_id": round_id, "application_id": application_id},
                round_id=round_id,
            )

    def _check_capacity(self, round_: RoundState) -> None:
        rulebook = self.rulebook(round_.rule_revision)
        country_used, program_used = self._usage(round_)
        for country, used in country_used.items():
            cap = rulebook.country_seats.get(country)
            if cap is not None and used > cap:
                raise CapacityExceededError(f"国别 {country} 资金上限被突破")
        for program_id, used in program_used.items():
            if used > rulebook.program_seats[program_id]:
                raise CapacityExceededError(f"项目 {program_id} 资金上限被突破")
        pool_used: dict[str, int] = {}
        for application_id in self._admitted_set(round_):
            pool_id = self._pool_of(round_, application_id)
            pool_used[pool_id] = pool_used.get(pool_id, 0) + 1
        seats = {pid: seats for pid, seats, _c, _p in rulebook.ordered_pools()}
        for pool_id, used in pool_used.items():
            if used > seats[pool_id]:
                raise CapacityExceededError(f"资金池 {pool_id} 上限被突破")

    # ============================ 放弃 / 撤销与递补 ============================
    def withdraw(self, application_id: str, reason: str = "") -> None:
        with self._lock:
            self._require_not_held(application_id)
            self.applications.withdraw(application_id, reason)
            self.store.append(
                "application_withdrawn",
                {"application_id": application_id, "reason": reason or "申请人放弃"},
            )
            self._release_if_admitted(application_id, "申请人放弃")

    def disqualify(self, application_id: str, reason: str) -> None:
        with self._lock:
            self._require_not_held(application_id)
            self.applications.disqualify(application_id, reason)
            self.store.append(
                "application_disqualified",
                {"application_id": application_id, "reason": reason},
            )
            self._release_if_admitted(application_id, f"资格撤销：{reason}")

    def _require_not_held(self, application_id: str) -> None:
        for round_ in self.rounds.values():
            if round_.result is not None and application_id in round_.holds:
                raise SeatHeldError(
                    f"该名额在轮次 {round_.round_id} 处于申诉冻结中，"
                    "须先了结申诉才能放弃或撤销"
                )

    def _release_if_admitted(self, application_id: str, cause: str) -> None:
        for round_ in self.rounds.values():
            if round_.state != ALLOCATED or round_.result is None:
                continue
            pool_id = self._pool_of(round_, application_id)
            if pool_id is None:
                continue
            if application_id in round_.holds:
                raise SeatHeldError(
                    f"该名额在轮次 {round_.round_id} 处于申诉冻结中，"
                    "须先了结申诉才能释放"
                )
            self._vacate_and_promote(round_, application_id, pool_id, cause)

    def _vacate_and_promote(
        self, round_: RoundState, application_id: str, pool_id: str, cause: str
    ) -> None:
        round_.vacated.add(application_id)
        round_.promoted.pop(application_id, None)
        round_.confirmed.discard(application_id)
        round_.vacant_pools[pool_id] = round_.vacant_pools.get(pool_id, 0) + 1
        self.store.append(
            "seat_released",
            {
                "round_id": round_.round_id,
                "application_id": application_id,
                "pool_id": pool_id,
                "cause": cause,
            },
            round_id=round_.round_id,
        )
        self._fill_vacancies(round_)

    def _fill_vacancies(self, round_: RoundState) -> None:
        """按冻结候补顺序填补所有可补的空置名额；不可插队、不可重排。

        每轮外循环至多递补一人，随后重新计算国别/项目占用，
        保证连续填补多个空置名额时也不会突破任何上限。
        """
        while True:
            candidate_app = None
            candidate_pool = None
            candidate_rank = None
            for pool_id in sorted(p for p, n in round_.vacant_pools.items() if n > 0):
                country_used, program_used = self._usage(round_)
                app_id = find_promotion(
                    self.rulebook(round_.rule_revision),
                    round_.result,
                    pool_id,
                    is_live=self.applications.is_live,
                    is_held=lambda app: app in round_.holds,
                    admitted=self._admitted_set(round_),
                    country_used=country_used,
                    program_used=program_used,
                )
                if app_id is not None:
                    candidate_app = app_id
                    candidate_pool = pool_id
                    candidate_rank = next(
                        r for r in round_.result.ranked
                        if r.application_id == app_id
                    )
                    break
            if candidate_app is None:
                break
            round_.promoted[candidate_app] = candidate_pool
            round_.vacant_pools[candidate_pool] -= 1
            self._check_capacity(round_)  # 先校验：突破上限则不写事件
            self.store.append(
                "seat_promoted",
                {
                    "round_id": round_.round_id,
                    "promoted_application_id": candidate_app,
                    "promoted_pseudonym": candidate_rank.pseudonym,
                    "pool_id": candidate_pool,
                    "waitlist_position": next(
                        d.waitlist_position
                        for d in round_.result.decisions
                        if d.application_id == candidate_app
                    ),
                    "rule_basis": "按冻结候补顺序首位可递补者自动递补，不允许人工插队",
                },
                round_id=round_.round_id,
            )
        for pool_id, count in sorted(round_.vacant_pools.items()):
            if count > 0 and round_.last_skipped_announced.get(pool_id) != count:
                round_.last_skipped_announced[pool_id] = count
                self.store.append(
                    "promotion_skipped",
                    {
                        "round_id": round_.round_id,
                        "pool_id": pool_id,
                        "open_seats": count,
                        "reason": "冻结候补顺序中暂无可递补者",
                    },
                    round_id=round_.round_id,
                )
            if count == 0:
                round_.last_skipped_announced.pop(pool_id, None)

    # ============================ 申诉与改判 ============================
    def file_appeal(self, round_id: str, application_id: str, reason: str) -> str:
        with self._lock:
            round_ = self._allocated(round_id)
            scope = self._appeal_scope(round_, application_id)
            appeal_id = f"AP-{len(round_.appeals) + 1:03d}"
            round_.holds.add(application_id)
            round_.appeals[appeal_id] = Appeal(
                appeal_id=appeal_id,
                application_id=application_id,
                round_id=round_id,
                reason=reason,
            )
            self.store.append(
                "appeal_filed",
                {
                    "appeal_id": appeal_id,
                    "round_id": round_id,
                    "application_id": application_id,
                    "scope": scope,
                    "reason": reason,
                    "frozen_only": [application_id],
                },
                round_id=round_id,
            )
            return appeal_id

    def _appeal_scope(self, round_: RoundState, application_id: str) -> str:
        if self._pool_of(round_, application_id) is not None:
            return "admitted_seat"
        if application_id in round_.result.waitlist:
            return "waitlist_position"
        raise NotFoundError("落选（有效评分不足）的申请不能占用申诉冻结位")

    def _allocated(self, round_id: str) -> RoundState:
        round_ = self._round(round_id)
        if round_.state != ALLOCATED or round_.result is None:
            raise RoundStateError("该轮尚未完成分配")
        return round_

    def reject_appeal(self, appeal_id: str, note: str = "") -> None:
        with self._lock:
            round_, appeal = self._find_appeal(appeal_id)
            appeal.status = "rejected"
            appeal.resolution = note or "申诉不成立"
            round_.holds.discard(appeal.application_id)
            self.store.append(
                "appeal_resolved",
                {
                    "appeal_id": appeal_id,
                    "round_id": round_.round_id,
                    "application_id": appeal.application_id,
                    "resolution": "rejected",
                    "note": note,
                },
                round_id=round_.round_id,
            )
            # 冻结解除：此前因该候补被冻结而挂起的空置名额重新尝试递补
            self._fill_vacancies(round_)

    def uphold_appeal(self, appeal_id: str, note: str = "") -> None:
        """申诉成立且性质为录取不当：仅释放受影响名额并按原规则递补。"""
        with self._lock:
            round_, appeal = self._find_appeal(appeal_id)
            application_id = appeal.application_id
            appeal.status = "upheld"
            appeal.resolution = note or "申诉成立，撤销该名额"
            round_.holds.discard(application_id)
            self.store.append(
                "appeal_resolved",
                {
                    "appeal_id": appeal_id,
                    "round_id": round_.round_id,
                    "application_id": application_id,
                    "resolution": "upheld",
                    "note": note,
                },
                round_id=round_.round_id,
            )
            pool_id = self._pool_of(round_, application_id)
            if pool_id is not None:
                self._vacate_and_promote(round_, application_id, pool_id, "申诉改判撤位")
            else:
                # 候补位申诉成立：位置不变，冻结解除后重试挂起的递补
                self._fill_vacancies(round_)

    def regrade_appeal(
        self, appeal_id: str, excluded_reviewer: str | None = None, note: str = ""
    ) -> None:
        """申诉改判：剔除受污染评分后重算申请人得分。

        - 剔除的评委评分记入 ``reviewer_excluded`` 事件；
        - 重算后仍达到当前录取阈值的，保住名额；
        - 不达标或有效评分不足的，释放名额并按冻结候补顺序递补；
        - 候补顺序本身永不重排（即使重算改变了分数）。
        """
        with self._lock:
            round_, appeal = self._find_appeal(appeal_id)
            application_id = appeal.application_id
            rulebook = self.rulebook(round_.rule_revision)
            ranked = next(
                r for r in round_.result.ranked if r.application_id == application_id
            )
            if excluded_reviewer is not None:
                self.board.require_reviewer(excluded_reviewer)
                if (excluded_reviewer, application_id) not in round_.scores:
                    raise ValidationError("该评委未对本申请提交评分，无法剔除")
                round_.excluded_sheets.setdefault(application_id, {})[
                    excluded_reviewer
                ] = note or "申诉认定评分应回避"
                self.store.append(
                    "reviewer_excluded",
                    {
                        "round_id": round_.round_id,
                        "appeal_id": appeal_id,
                        "application_id": application_id,
                        "reviewer_id": excluded_reviewer,
                        "reason": note or "申诉认定评分应回避",
                    },
                    round_id=round_.round_id,
                )

            excluded_map = {r: reason for r, reason in ranked.excluded_reviewers}
            excluded_map.update(round_.excluded_sheets.get(application_id, {}))
            sheets = {
                reviewer: dims
                for (reviewer, app_id), dims in round_.scores.items()
                if app_id == application_id and reviewer not in excluded_map
            }
            new_score = None
            if len(sheets) >= rulebook.min_reviewers:
                new_score = 0.0
                for code, weight in sorted(rulebook.weights.items()):
                    avg = sum(float(dims[code]) for dims in sheets.values()) / len(sheets)
                    new_score += round(avg, 6) * weight
                new_score = round(new_score, 6)

            was_admitted = self._pool_of(round_, application_id) is not None
            pool_id = self._pool_of(round_, application_id) if was_admitted else None
            # 是否撤位：仅当候补队列中存在“确实能接替该名额（国别/项目/
            # 定向限制均匹配）且改判后分数严格高于本申请人”的对象。
            # 若无人能接替该定向名额，即使分数下降也保留名额，不做无意义空置。
            replacement = None
            cutoff = None
            if was_admitted and new_score is None:
                keep_seat = False
            elif was_admitted:
                replacement = self._first_replaceable_waitlist(
                    round_, application_id, pool_id
                )
                if replacement is None:
                    keep_seat = True
                else:
                    cutoff = replacement.weighted_score
                    # 同分不撤：初始排序的平局规则本来就把本申请人排在前面
                    keep_seat = new_score >= replacement.weighted_score
            else:
                keep_seat = False
            if new_score is not None:
                round_.amended_scores[application_id] = new_score

            amended_ranked = self._amended_ranking(round_)
            round_.amended_ranked = amended_ranked
            appeal.status = "regraded"
            appeal.resolution = note or "剔除受污染评分后重新核算"
            round_.holds.discard(application_id)
            self.store.append(
                "round_amended",
                {
                    "round_id": round_.round_id,
                    "appeal_id": appeal_id,
                    "application_id": application_id,
                    "old_weighted_score": ranked.weighted_score,
                    "new_weighted_score": new_score,
                    "cutoff_score": cutoff,
                    "keep_seat": keep_seat,
                    "excluded_reviewers": sorted(excluded_map.items()),
                    "amended_ranked": amended_ranked,
                    "waitlist_unchanged": list(round_.result.waitlist),
                    "note": note,
                },
                round_id=round_.round_id,
            )
            if was_admitted and not keep_seat:
                pool_id = self._pool_of(round_, application_id)
                self._vacate_and_promote(
                    round_, application_id, pool_id, "申诉改判后未达录取阈值"
                )
            else:
                # 保住名额时也可能解除了挂起递补的冻结
                self._fill_vacancies(round_)

    def _amended_ranking(self, round_: RoundState) -> list:
        """用基础回避 + 改判剔除重新核算整张排名表（仅用于审计留痕）。"""
        rulebook = self.rulebook(round_.rule_revision)
        rows = []
        for ranked in round_.result.ranked:
            excluded_map = {r: reason for r, reason in ranked.excluded_reviewers}
            excluded_map.update(round_.excluded_sheets.get(ranked.application_id, {}))
            sheets = {
                reviewer: dims
                for (reviewer, app_id), dims in round_.scores.items()
                if app_id == ranked.application_id and reviewer not in excluded_map
            }
            score = ranked.weighted_score
            if len(sheets) >= rulebook.min_reviewers:
                score = 0.0
                for code, weight in sorted(rulebook.weights.items()):
                    avg = sum(float(dims[code]) for dims in sheets.values()) / len(sheets)
                    score += round(avg, 6) * weight
                score = round(score, 6)
            rows.append(
                {
                    "application_id": ranked.application_id,
                    "score": score,
                    "reviewer_count": len(sheets),
                    "excluded_reviewers": sorted(excluded_map.items()),
                }
            )
        rows.sort(
            key=lambda r: (
                -r["score"],
                next(
                    x.program_priority
                    for x in round_.result.ranked
                    if x.application_id == r["application_id"]
                ),
                -r["reviewer_count"],
                r["application_id"],
            )
        )
        for i, row in enumerate(rows, start=1):
            row["rank"] = i
        return rows

    def _first_replaceable_waitlist(
        self, round_: RoundState, application_id: str, pool_id: str
    ):
        """假设 application_id 的名额释放，按候补顺序找第一个能接替者。"""
        rulebook = self.rulebook(round_.rule_revision)
        ranks = {r.application_id: r for r in round_.result.ranked}
        restriction = {
            pid: (c, p) for pid, _s, c, p in rulebook.ordered_pools()
        }
        want_country, want_program = restriction[pool_id]
        country_used, program_used = self._usage(round_)
        holder = ranks[application_id]
        # 假设释放本申请人占用的国别/项目额度
        hyp_country = dict(country_used)
        hyp_country[holder.country] = hyp_country.get(holder.country, 1) - 1
        hyp_program = dict(program_used)
        hyp_program[holder.program_id] = hyp_program.get(holder.program_id, 1) - 1
        admitted = self._admitted_set(round_) - {application_id}
        for candidate_id in round_.result.waitlist:
            if candidate_id in admitted:
                continue
            if candidate_id in round_.holds:
                continue
            if not self.applications.is_live(candidate_id):
                continue
            candidate = ranks[candidate_id]
            if want_country is not None and want_country != candidate.country:
                continue
            if want_program is not None and want_program != candidate.program_id:
                continue
            cap = rulebook.country_seats.get(candidate.country)
            if cap is not None and hyp_country.get(candidate.country, 0) >= cap:
                continue
            if hyp_program.get(candidate.program_id, 0) >= rulebook.program_seats[
                candidate.program_id
            ]:
                continue
            return candidate
        return None

    def _find_appeal(self, appeal_id: str) -> tuple[RoundState, Appeal]:
        for round_ in self.rounds.values():
            appeal = round_.appeals.get(appeal_id)
            if appeal is not None:
                if appeal.status != "pending":
                    raise RoundStateError(f"申诉 {appeal_id} 已了结：{appeal.status}")
                return round_, appeal
        raise NotFoundError(f"申诉 {appeal_id} 不存在")

    # ============================ 事件重放 ============================
    def _apply(self, event) -> None:  # noqa: C901 - 事件分派集中于此
        p = event.payload
        if event.type == "rule_published":
            book = RuleBook.from_dict(p["rulebook"])
            if book.content_hash != p["content_hash"]:
                raise ValueError("规则书内容哈希与日志记录不符")
            self.rules[book.revision] = book
        elif event.type == "identity_registered":
            from .identity import Identity

            identity = self.identities
            identity._identities[p["person_id"]] = Identity(
                person_id=p["person_id"],
                display_pseudonym=p["pseudonym"],
                application_ids={p["application_id"]},
            )
            identity._app_owner[p["application_id"]] = p["person_id"]
            self._app_person[p["application_id"]] = p["person_id"]
        elif event.type == "application_submitted":
            from .applications import Application, MaterialVersion

            fields = tuple((k, v) for k, v in p["material_fields"])
            material = MaterialVersion(
                version=p["material_version"],
                content_hash=p["material_hash"],
                fields=fields,
            )
            if material.content_hash != p["material_hash"]:
                raise ValueError("申请材料内容哈希与日志记录不符")
            app = Application(
                application_id=p["application_id"],
                person_id=p["person_id"],
                country=p["country"],
                program_id=p["program_id"],
                channel=p["channel"],
                material_versions=[material],
            )
            self.applications._apps[p["application_id"]] = app
        elif event.type == "material_updated":
            from .applications import MaterialVersion

            fields = tuple((k, v) for k, v in p["material_fields"])
            material = MaterialVersion(
                version=p["material_version"],
                content_hash=p["material_hash"],
                fields=fields,
            )
            if material.content_hash != p["material_hash"]:
                raise ValueError("材料版本内容哈希与日志记录不符")
            self.applications.get(p["application_id"]).material_versions.append(material)
        elif event.type == "duplicate_flagged":
            self._duplicate_flags.append(
                {"app_a": p["app_a"], "app_b": p["app_b"], "resolved": False}
            )
        elif event.type == "duplicate_merged":
            source_person = self._app_person[p["source_application_id"]]
            target_person = self.identities.merge(
                p["source_application_id"], p["target_application_id"]
            )
            self.applications.supersede(
                p["source_application_id"], p["target_application_id"]
            )
            self.board.merge_person(source_person, target_person)
        elif event.type == "reviewer_registered":
            self.board._reviewers.add(p["reviewer_id"])
        elif event.type == "conflict_declared":
            self.board.declare_conflict(p["reviewer_id"], p["person_id"])
        elif event.type == "application_withdrawn":
            self.applications.withdraw(p["application_id"], p["reason"])
        elif event.type == "application_disqualified":
            self.applications.disqualify(p["application_id"], p["reason"])
        elif event.type == "round_opened":
            rulebook = self.rules[p["rule_revision"]]
            if rulebook.content_hash != p["rule_hash"]:
                raise ValueError("轮次引用的规则哈希与已发布规则不符")
            round_id = p["round_id"]
            self.rounds[round_id] = RoundState(
                round_id=round_id,
                rule_revision=p["rule_revision"],
                material_pin={k: tuple(v) for k, v in p["material_pin"].items()},
                state=SCORING,
                scoring=ScoringSession(rulebook.dimension_codes),
            )
        elif event.type == "score_submitted":
            round_id = p["round_id"]
            round_ = self.rounds[round_id]
            round_.scores[(p["reviewer_id"], p["application_id"])] = dict(p["scores"])
        elif event.type == "scoring_closed":
            round_ = self.rounds[p["round_id"]]
            round_.state = CLOSED
        elif event.type == "round_allocated":
            round_ = self.rounds[p["round_id"]]
            result = AllocationResult.from_dict(p["result"])
            if sha256_hex(canonical_json(result.to_dict())) != p["result_hash"]:
                raise ValueError("分配结果哈希与日志记录不符")
            round_.result = result
            round_.result_hash = p["result_hash"]
            round_.candidate_snapshot = p.get("candidates")
            round_.state = ALLOCATED
        elif event.type == "seat_confirmed":
            self.rounds[p["round_id"]].confirmed.add(p["application_id"])
        elif event.type == "seat_released":
            round_ = self.rounds[p["round_id"]]
            round_.vacated.add(p["application_id"])
            round_.confirmed.discard(p["application_id"])
            pool_id = p["pool_id"]
            round_.vacant_pools[pool_id] = round_.vacant_pools.get(pool_id, 0) + 1
        elif event.type == "seat_promoted":
            round_ = self.rounds[p["round_id"]]
            pool_id = p["pool_id"]
            round_.promoted[p["promoted_application_id"]] = pool_id
            round_.vacant_pools[pool_id] = max(
                0, round_.vacant_pools.get(pool_id, 0) - 1
            )
            if round_.vacant_pools[pool_id] == 0:
                round_.last_skipped_announced.pop(pool_id, None)
        elif event.type == "round_amended":
            round_ = self.rounds[p["round_id"]]
            round_.amended_ranked = p["amended_ranked"]
            if p["new_weighted_score"] is not None:
                round_.amended_scores[p["application_id"]] = p["new_weighted_score"]
            # 改判即申诉了结：还原申诉状态与冻结解除
            appeal = round_.appeals[p["appeal_id"]]
            appeal.status = "regraded"
            appeal.resolution = p.get("note", "") or "剔除受污染评分后重新核算"
            round_.holds.discard(p["application_id"])
        elif event.type == "reviewer_excluded":
            round_ = self.rounds[p["round_id"]]
            round_.excluded_sheets.setdefault(p["application_id"], {})[
                p["reviewer_id"]
            ] = p["reason"]
        elif event.type == "appeal_filed":
            round_ = self.rounds[p["round_id"]]
            round_.holds.add(p["application_id"])
            round_.appeals[p["appeal_id"]] = Appeal(
                appeal_id=p["appeal_id"],
                application_id=p["application_id"],
                round_id=p["round_id"],
                reason=p["reason"],
            )
        elif event.type == "appeal_resolved":
            round_ = self.rounds[p["round_id"]]
            appeal = round_.appeals[p["appeal_id"]]
            appeal.status = {"rejected": "rejected", "upheld": "upheld"}[
                p["resolution"]
            ]
            appeal.resolution = p.get("note", "")
            round_.holds.discard(p["application_id"])
        elif event.type in ("promotion_skipped",):
            round_ = self.rounds[p["round_id"]]
            round_.last_skipped_announced[p["pool_id"]] = p["open_seats"]
        else:
            raise ValueError(f"未知事件类型，无法重放：{event.type}")
