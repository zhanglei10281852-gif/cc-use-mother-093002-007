import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from scholarship_pool import (
    ADMITTED,
    REJECTED,
    WAITLISTED,
    DomainError,
    FundPoolRule,
    GovernanceService,
    MaterialVersion,
    ReplayIntegrityError,
    RuleSet,
    SLOT_CONFIRMED,
    SLOT_FROZEN,
    SLOT_PENDING,
    replay_round,
)

SECRET = b"test-secret"
ROUND = "R1"


def general_fund(total=500, award=100):
    return FundPoolRule("F-GEN", "区域通用基金", total, award)


class ServiceCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.log_path = Path(self._tmp.name) / "events.jsonl"
        self.svc = GovernanceService(self.log_path, identity_secret=SECRET)

    def restart(self):
        """模拟服务重启：从同一事件日志恢复出全新实例。"""
        self.svc = GovernanceService(self.log_path, identity_secret=SECRET)
        return self.svc

    def commit(self, ruleset=None, **overrides):
        if ruleset is None:
            ruleset = RuleSet(
                revision=overrides.get("revision", 1),
                country_quotas=overrides.get("country_quotas", {}),
                program_quotas=overrides.get("program_quotas", {}),
                funds=overrides.get("funds", (general_fund(),)),
                required_materials=overrides.get("required_materials", ("学历证明",)),
                min_valid_scores=overrides.get("min_valid_scores", 2),
            )
        return self.svc.commit_ruleset(ruleset)

    def open(self, round_id=ROUND, revision=1):
        self.svc.open_round(round_id, revision)

    def submit(self, app_id, *, doc=None, at=1, country="VN", program="CLS",
               channel="官网", scores=(), materials=None, round_id=ROUND):
        self.svc.submit_application(
            round_id, app_id,
            id_document=doc or f"DOC-{app_id}",
            display_name=f"申请人{app_id}",
            channel=channel, country=country, program=program,
            submitted_at=at,
            materials=materials if materials is not None
            else [MaterialVersion("学历证明", 1, f"digest-{app_id}", True)],
        )
        for judge_id, score in scores:
            self.svc.submit_score(
                round_id, judge_id=judge_id, application_id=app_id,
                score=score, submitted_at=at,
            )


class RuleGovernanceTests(ServiceCase):
    def test_invalid_ruleset_rejected(self):
        with self.assertRaises(ValueError):
            RuleSet(0, {}, {}, (general_fund(),), ("学历证明",))
        with self.assertRaises(ValueError):
            FundPoolRule("F", "坏资金", 100, 200)
        with self.assertRaises(ValueError):
            RuleSet(1, {}, {}, (), ("学历证明",))

    def test_duplicate_revision_rejected(self):
        self.commit()
        with self.assertRaises(DomainError):
            self.svc.commit_ruleset(RuleSet(1, {}, {}, (general_fund(),), ("学历证明",)))

    def test_rule_revision_only_produces_new_round_results(self):
        self.commit(country_quotas={"VN": 1})
        self.open()
        self.submit("A", at=1, scores=[("J1", 90), ("J2", 90)])
        self.submit("B", at=2, scores=[("J1", 80), ("J2", 80)])
        result1 = self.svc.run_allocation(ROUND)
        self.assertEqual([d.application_id for d in result1.admitted()], ["A"])
        self.assertEqual([d.application_id for d in result1.waitlisted()], ["B"])

        # 修订规则：越南名额升至 2，只产生新版本号
        revised = self.svc.revise_ruleset(1, country_quotas={"VN": 2})
        self.assertEqual(revised.revision, 2)

        # 旧轮次结果封存：不可重跑、不受新规则影响
        with self.assertRaises(DomainError):
            self.svc.run_allocation(ROUND)
        self.assertEqual(
            [d.application_id for d in self.svc.round_result(ROUND).admitted()], ["A"]
        )

        # 新规则只能开启新一轮分配
        self.svc.open_round("R2", 2)
        self.submit("A2", at=1, scores=[("J1", 90), ("J2", 90)], round_id="R2")
        self.submit("B2", at=2, scores=[("J1", 80), ("J2", 80)], round_id="R2")
        result2 = self.svc.run_allocation("R2")
        self.assertEqual(
            sorted(d.application_id for d in result2.admitted()), ["A2", "B2"]
        )
        self.assertEqual(
            [d.application_id for d in self.svc.round_result(ROUND).admitted()], ["A"]
        )

    def test_sealed_round_rejects_late_inputs(self):
        self.commit()
        self.open()
        self.submit("A", scores=[("J1", 90), ("J2", 90)])
        self.svc.run_allocation(ROUND)
        with self.assertRaises(DomainError):
            self.submit("B", scores=[("J1", 80), ("J2", 80)])
        with self.assertRaises(DomainError):
            self.svc.submit_score(ROUND, judge_id="J3", application_id="A",
                                  score=70, submitted_at=9)
        with self.assertRaises(DomainError):
            self.svc.declare_conflict(ROUND, judge_id="J3",
                                      id_document="DOC-A", relation="同事")


class IdentityMergeTests(ServiceCase):
    def test_duplicate_across_channels_merged(self):
        self.commit()
        self.open()
        self.submit("APP-1", doc="PASSPORT-7788", at=1, channel="院校A推荐",
                    scores=[("J1", 90), ("J2", 90)])
        self.submit("APP-2", doc=" passport-7788 ", at=2, channel="院校B推荐",
                    scores=[("J1", 60), ("J2", 60)])
        result = self.svc.run_allocation(ROUND)
        self.assertEqual([d.application_id for d in result.admitted()], ["APP-1"])
        merged = result.decision_for("APP-2")
        self.assertEqual(merged.outcome, REJECTED)
        self.assertIn("重复报名已合并", merged.explanation.reason)
        self.assertIn("APP-1", merged.explanation.reason)

    def test_merge_report_and_log_hide_personal_information(self):
        self.commit()
        self.open()
        self.submit("APP-1", doc="PASSPORT-7788", at=1, channel="院校A推荐")
        self.submit("APP-2", doc="PASSPORT-7788", at=2, channel="院校B推荐")
        report = self.svc.duplicate_report(ROUND)
        self.assertEqual(len(report), 1)
        self.assertEqual(report[0]["surviving_application_id"], "APP-1")
        statuses = {a["application_id"]: a["status"] for a in report[0]["applications"]}
        self.assertEqual(statuses["APP-2"], "MERGED_DUPLICATE")
        blob = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("申请人APP-1", blob)
        self.assertNotIn("PASSPORT", blob.upper())
        log_text = self.log_path.read_text(encoding="utf-8")
        self.assertNotIn("申请人APP-1", log_text)
        self.assertNotIn("PASSPORT-7788", log_text)

    def test_identity_token_stable_across_restart(self):
        self.commit()
        self.open()
        self.submit("APP-1", doc="PASSPORT-7788", at=1)
        token_before = self.svc.duplicate_report(ROUND)
        svc2 = self.restart()
        self.assertEqual(
            svc2.identity_token_for("PASSPORT-7788"),
            self.svc.identity_token_for("PASSPORT-7788"),
        )
        self.assertEqual(len(token_before), 0)  # 单人单报，无重复分组

    def test_conflict_bound_to_identity_covers_every_channel(self):
        self.commit()
        self.open()
        self.submit("APP-1", doc="PASSPORT-7788", at=1, channel="院校A推荐",
                    scores=[("J1", 95), ("J2", 80), ("J3", 70)])
        self.submit("APP-2", doc="PASSPORT-7788", at=2, channel="官网",
                    scores=[("J1", 95), ("J2", 80), ("J3", 70)])
        self.svc.declare_conflict(ROUND, judge_id="J1",
                                  id_document="PASSPORT-7788", relation="合作导师")
        result = self.svc.run_allocation(ROUND)
        decision = result.decision_for("APP-1")
        self.assertEqual(decision.explanation.valid_judges, ("J2", "J3"))
        self.assertEqual(decision.explanation.recused_judges, (("J1", "合作导师"),))


class ScoringRecusalTests(ServiceCase):
    def test_recused_score_excluded_from_aggregate(self):
        self.commit()
        self.open()
        self.submit("A", scores=[("J1", 100), ("J2", 60), ("J3", 80)])
        self.svc.declare_conflict(ROUND, judge_id="J1",
                                  id_document="DOC-A", relation="同一单位")
        result = self.svc.run_allocation(ROUND)
        decision = result.decision_for("A")
        self.assertEqual(decision.outcome, ADMITTED)
        self.assertEqual(str(decision.explanation.aggregate_score), "70")
        self.assertEqual(decision.explanation.valid_judges, ("J2", "J3"))

    def test_insufficient_valid_scores_after_recusal_rejected(self):
        self.commit()
        self.open()
        self.submit("A", scores=[("J1", 90), ("J2", 80)])
        self.svc.declare_conflict(ROUND, judge_id="J1",
                                  id_document="DOC-A", relation="亲属")
        result = self.svc.run_allocation(ROUND)
        decision = result.decision_for("A")
        self.assertEqual(decision.outcome, REJECTED)
        self.assertIn("有效评分不足", decision.explanation.reason)

    def test_duplicate_score_and_out_of_range_rejected(self):
        self.commit()
        self.open()
        self.submit("A", scores=[("J1", 90)])
        with self.assertRaises(DomainError):
            self.svc.submit_score(ROUND, judge_id="J1", application_id="A",
                                  score=80, submitted_at=2)
        with self.assertRaises(ValueError):
            self.svc.submit_score(ROUND, judge_id="J2", application_id="A",
                                  score=120, submitted_at=2)


class AllocationTests(ServiceCase):
    def test_country_and_program_quotas_enforced(self):
        self.commit(country_quotas={"VN": 1}, program_quotas={"CLS": 3},
                    funds=(general_fund(500),))
        self.open()
        self.submit("VN-1", at=1, country="VN", scores=[("J1", 90), ("J2", 90)])
        self.submit("VN-2", at=2, country="VN", scores=[("J1", 80), ("J2", 80)])
        self.submit("TH-1", at=3, country="TH", scores=[("J1", 85), ("J2", 85)])
        self.submit("TH-2", at=4, country="TH", program="CLS",
                    scores=[("J1", 70), ("J2", 70)])
        self.submit("TH-3", at=5, country="TH", program="CLS",
                    scores=[("J1", 60), ("J2", 60)])
        result = self.svc.run_allocation(ROUND)
        admitted = {d.application_id for d in result.admitted()}
        self.assertEqual(admitted, {"VN-1", "TH-1", "TH-2"})
        vn2 = result.decision_for("VN-2")
        self.assertEqual(vn2.outcome, WAITLISTED)
        self.assertIn("国别名额已满（VN）", vn2.explanation.blockers)
        th3 = result.decision_for("TH-3")
        self.assertIn("项目名额已满（CLS）", th3.explanation.blockers)

    def test_directed_fund_preferred_and_restricted(self):
        funds = (
            FundPoolRule("F-VN", "越南定向基金", 100, 100, only_countries={"VN"}),
            general_fund(200),
        )
        self.commit(funds=funds)
        self.open()
        self.submit("LA-1", at=1, country="LA", scores=[("J1", 95), ("J2", 95)])
        self.submit("VN-1", at=2, country="VN", scores=[("J1", 90), ("J2", 90)])
        self.submit("TH-1", at=3, country="TH", scores=[("J1", 85), ("J2", 85)])
        self.submit("VN-2", at=4, country="VN", scores=[("J1", 80), ("J2", 80)])
        result = self.svc.run_allocation(ROUND)
        funds_by_app = {
            d.application_id: d.explanation.fund_id for d in result.admitted()
        }
        self.assertEqual(funds_by_app["VN-1"], "F-VN")   # 定向资金优先用于定向对象
        self.assertEqual(funds_by_app["LA-1"], "F-GEN")  # 定向资金不接受老挝申请人
        self.assertEqual(funds_by_app["TH-1"], "F-GEN")
        vn2 = result.decision_for("VN-2")
        self.assertEqual(vn2.outcome, WAITLISTED)
        self.assertIn("无可用资金", vn2.explanation.blockers[-1])

    def test_tie_break_is_deterministic(self):
        self.commit(funds=(general_fund(100),))
        self.open()
        self.submit("APP-Z", at=1, scores=[("J1", 80), ("J2", 80)])
        self.submit("APP-Y", at=1, scores=[("J1", 80), ("J2", 80)])
        self.submit("APP-X", at=2, scores=[("J1", 80), ("J2", 80)])
        result = self.svc.run_allocation(ROUND)
        # 同分：先提交者优先；再并列按申请编号字典序
        self.assertEqual([d.application_id for d in result.admitted()], ["APP-Y"])
        self.assertEqual(
            [d.application_id for d in result.waitlisted()], ["APP-Z", "APP-X"]
        )

    def test_missing_or_unverified_materials_rejected(self):
        self.commit(required_materials=("学历证明", "语言成绩"))
        self.open()
        self.submit("A", scores=[("J1", 90), ("J2", 90)], materials=[
            MaterialVersion("学历证明", 1, "dg-a1", True),
            MaterialVersion("语言成绩", 2, "dg-a2", False),  # 未核验视为缺失
        ])
        self.submit("B", scores=[("J1", 80), ("J2", 80)], materials=[
            MaterialVersion("学历证明", 1, "dg-b1", True),
            MaterialVersion("语言成绩", 1, "dg-b2", True),
        ])
        result = self.svc.run_allocation(ROUND)
        decision = result.decision_for("A")
        self.assertEqual(decision.outcome, REJECTED)
        self.assertIn("语言成绩", decision.explanation.reason)
        self.assertEqual(result.decision_for("B").outcome, ADMITTED)
        # 材料版本与摘要随事件落账，审计可核对当时使用的材料版本
        log_text = self.log_path.read_text(encoding="utf-8")
        self.assertIn("dg-a2", log_text)


class WaitlistTests(ServiceCase):
    def _four_candidates_two_slots(self):
        self.commit(funds=(general_fund(200),))
        self.open()
        self.submit("A", at=1, scores=[("J1", 90), ("J2", 90)])
        self.submit("B", at=2, scores=[("J1", 80), ("J2", 80)])
        self.submit("C", at=3, scores=[("J1", 70), ("J2", 70)])
        self.submit("D", at=4, scores=[("J1", 60), ("J2", 60)])
        self.svc.run_allocation(ROUND)

    def test_withdrawal_promotes_next_in_order(self):
        self._four_candidates_two_slots()
        self.assertEqual(self.svc.waitlist(ROUND), ["C", "D"])
        self.svc.withdraw(ROUND, "A")
        self.assertEqual(self.svc.slot_status(ROUND, "C"), SLOT_PENDING)
        self.assertEqual(self.svc.waitlist(ROUND), ["D"])
        self.svc.confirm_admission(ROUND, "C")

    def test_revocation_triggers_promotion(self):
        self._four_candidates_two_slots()
        self.svc.revoke_qualification(ROUND, "B", reason="材料造假")
        self.assertEqual(self.svc.slot_status(ROUND, "C"), SLOT_PENDING)

    def test_waitlisted_cannot_jump_queue_by_confirming(self):
        self._four_candidates_two_slots()
        with self.assertRaises(DomainError):
            self.svc.confirm_admission(ROUND, "C")  # 候补不能直接确认，杜绝人工插队
        self.svc.withdraw(ROUND, "A")
        with self.assertRaises(DomainError):
            self.svc.confirm_admission(ROUND, "D")  # 仍排在其后的候补同样不能确认
        self.svc.confirm_admission(ROUND, "C")

    def test_promotion_skips_candidates_not_matching_directed_fund(self):
        funds = (
            FundPoolRule("F-VN", "越南定向基金", 100, 100, only_countries={"VN"}),
            general_fund(100),
        )
        self.commit(funds=funds)
        self.open()
        self.submit("TH-1", at=1, country="TH", scores=[("J1", 95), ("J2", 95)])
        self.submit("TH-2", at=2, country="TH", scores=[("J1", 90), ("J2", 90)])
        self.submit("VN-1", at=3, country="VN", scores=[("J1", 85), ("J2", 85)])
        self.submit("VN-2", at=4, country="VN", scores=[("J1", 80), ("J2", 80)])
        self.svc.run_allocation(ROUND)
        self.assertEqual(self.svc.waitlist(ROUND), ["TH-2", "VN-2"])
        # 越南定向名额释放：排在前面的泰国申请人不匹配，被跳过并留痕
        self.svc.withdraw(ROUND, "VN-1")
        self.assertEqual(self.svc.slot_status(ROUND, "VN-2"), SLOT_PENDING)
        self.assertEqual(self.svc.waitlist(ROUND), ["TH-2"])
        report = replay_round(self.log_path, ROUND)
        promoted = [t for t in report.traces if t.action == "PROMOTED"]
        self.assertEqual(promoted[0].application_id, "VN-2")
        self.assertEqual(promoted[0].detail["skipped"][0]["application_id"], "TH-2")
        self.assertIn("无可用资金", promoted[0].detail["skipped"][0]["reason"])

    def test_restart_preserves_waitlist_order(self):
        self._four_candidates_two_slots()
        self.svc.withdraw(ROUND, "A")  # C 递补
        svc2 = self.restart()
        self.assertEqual(svc2.waitlist(ROUND), ["D"])
        svc2.withdraw(ROUND, "B")      # 重启后续操作仍按原顺序递补
        self.assertEqual(svc2.slot_status(ROUND, "D"), SLOT_PENDING)
        self.assertEqual(svc2.waitlist(ROUND), [])


class AppealTests(ServiceCase):
    def _allocated(self):
        self.commit(funds=(general_fund(200),))
        self.open()
        self.submit("A", at=1, scores=[("J1", 90), ("J2", 90)])
        self.submit("B", at=2, scores=[("J1", 80), ("J2", 80)])
        self.submit("C", at=3, scores=[("J1", 70), ("J2", 70)])
        self.svc.run_allocation(ROUND)

    def test_appeal_freezes_only_affected_slot(self):
        self._allocated()
        self.svc.file_appeal(ROUND, "AP-1", target_application_id="A",
                             grounds="合作院校质疑国别名额")
        self.assertEqual(self.svc.slot_status(ROUND, "A"), SLOT_FROZEN)
        with self.assertRaises(DomainError):
            self.svc.confirm_admission(ROUND, "A")
        # 其他录取与递补不受影响
        self.svc.confirm_admission(ROUND, "B")
        self.svc.withdraw(ROUND, "B")
        self.assertEqual(self.svc.slot_status(ROUND, "C"), SLOT_PENDING)
        self.assertEqual(self.svc.slot_status(ROUND, "A"), SLOT_FROZEN)

    def test_dismissed_appeal_unfreezes_slot(self):
        self._allocated()
        self.svc.file_appeal(ROUND, "AP-1", target_application_id="A", grounds="质疑")
        self.svc.resolve_appeal(ROUND, "AP-1", upheld=False, note="证据不足")
        self.assertEqual(self.svc.slot_status(ROUND, "A"), SLOT_PENDING)
        self.svc.confirm_admission(ROUND, "A")
        self.assertEqual(self.svc.slot_status(ROUND, "A"), SLOT_CONFIRMED)

    def test_upheld_appeal_reverses_admission_and_promotes(self):
        self._allocated()
        self.svc.file_appeal(ROUND, "AP-1", target_application_id="A",
                             grounds="评委未回避合作关系")
        self.svc.resolve_appeal(ROUND, "AP-1", upheld=True, note="查明确应回避")
        self.assertEqual(self.svc.slot_status(ROUND, "A"), "CLOSED")
        self.assertEqual(self.svc.slot_status(ROUND, "C"), SLOT_PENDING)
        self.assertEqual(self.svc.waitlist(ROUND), [])
        report = replay_round(self.log_path, ROUND)
        reversed_trace = [t for t in report.traces if t.action == "DECISION_REVERSED"]
        self.assertEqual(reversed_trace[0].detail["appeal_id"], "AP-1")

    def test_reinstate_with_corrected_score_reorders_waitlist(self):
        self.commit(funds=(general_fund(100),))
        self.open()
        self.submit("A", at=1, scores=[("J1", 90), ("J2", 90)])
        self.submit("B", at=2, scores=[("J1", 85), ("J2", 85)])
        self.submit("C", at=3, scores=[("J1", 80), ("J2", 80)])
        self.svc.run_allocation(ROUND)
        self.assertEqual(self.svc.waitlist(ROUND), ["B", "C"])
        # C 申诉：一名评委被错误回避，更正成绩为 95
        self.svc.file_appeal(ROUND, "AP-1", target_application_id="C",
                             grounds="评分被错误排除")
        self.svc.resolve_appeal(ROUND, "AP-1", upheld=True,
                                note="恢复被错排的评分", corrected_score=95)
        self.assertEqual(self.svc.waitlist(ROUND), ["C", "B"])
        self.svc.withdraw(ROUND, "A")
        self.assertEqual(self.svc.slot_status(ROUND, "C"), SLOT_PENDING)

    def test_reinstate_admits_when_capacity_available(self):
        self.commit(funds=(general_fund(200),))
        self.open()
        self.submit("A", at=1, scores=[("J1", 90), ("J2", 90)])
        self.submit("B", at=2, scores=[("J1", 80)])  # 有效评分不足被拒
        self.svc.run_allocation(ROUND)
        self.assertEqual(self.svc.round_result(ROUND).decision_for("B").outcome, REJECTED)
        self.svc.file_appeal(ROUND, "AP-1", target_application_id="B",
                             grounds="漏登一份评委评分")
        self.svc.resolve_appeal(ROUND, "AP-1", upheld=True,
                                note="补登后达标", corrected_score=82)
        self.assertEqual(self.svc.slot_status(ROUND, "B"), SLOT_PENDING)
        self.svc.confirm_admission(ROUND, "B")

    def test_frozen_slot_cannot_be_withdrawn(self):
        self._allocated()
        self.svc.file_appeal(ROUND, "AP-1", target_application_id="A", grounds="质疑")
        with self.assertRaises(DomainError):
            self.svc.withdraw(ROUND, "A")


class ConcurrencyTests(ServiceCase):
    def test_concurrent_confirm_same_slot_only_one_succeeds(self):
        self.commit(funds=(general_fund(100),))
        self.open()
        self.submit("A", scores=[("J1", 90), ("J2", 90)])
        self.svc.run_allocation(ROUND)
        errors = []

        def worker():
            try:
                self.svc.confirm_admission(ROUND, "A")
            except DomainError:
                errors.append("dup")

        threads = [threading.Thread(target=worker) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(errors), 11)
        self.assertEqual(self.svc.fund_usage(ROUND)["F-GEN"]["confirmed"], 100)

    def test_concurrent_confirms_respect_fund_cap(self):
        self.commit(funds=(general_fund(300),))  # 恰好 3 个名额
        self.open()
        for index, app_id in enumerate(["A", "B", "C", "D", "E"], 1):
            self.submit(app_id, at=index,
                        scores=[("J1", 100 - 10 * index), ("J2", 100 - 10 * index)])
        self.svc.run_allocation(ROUND)
        admitted = ["A", "B", "C"]
        outcomes = []

        def worker(app_id):
            try:
                self.svc.confirm_admission(ROUND, app_id)
                outcomes.append("ok")
            except DomainError:
                outcomes.append("err")

        threads = [
            threading.Thread(target=worker, args=(app_id,))
            for app_id in admitted * 2  # 每个名额两个线程抢确认
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(outcomes.count("ok"), 3)
        usage = self.svc.fund_usage(ROUND)["F-GEN"]
        self.assertEqual(usage["confirmed"], 300)
        self.assertLessEqual(usage["confirmed"], usage["total"])

    def test_withdraw_and_confirm_race_stays_consistent(self):
        self.commit(funds=(general_fund(100),))
        self.open()
        self.submit("A", at=1, scores=[("J1", 90), ("J2", 90)])
        self.submit("W", at=2, scores=[("J1", 80), ("J2", 80)])
        self.svc.run_allocation(ROUND)
        errors = []

        def do_withdraw():
            try:
                self.svc.withdraw(ROUND, "A")
            except DomainError:
                errors.append("withdraw")

        def do_confirm():
            try:
                self.svc.confirm_admission(ROUND, "A")
            except DomainError:
                errors.append("confirm")

        threads = [threading.Thread(target=do_withdraw),
                   threading.Thread(target=do_confirm)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        # 无论哪种时序：A 的名额最终关闭，W 按规则递补，资金不破上限
        self.assertEqual(self.svc.slot_status(ROUND, "A"), "CLOSED")
        self.assertEqual(self.svc.slot_status(ROUND, "W"), SLOT_PENDING)
        self.assertLessEqual(self.svc.fund_usage(ROUND)["F-GEN"]["confirmed"], 100)


class ReplayTests(ServiceCase):
    def _rich_round(self):
        funds = (
            FundPoolRule("F-VN", "越南定向基金", 100, 100, only_countries={"VN"}),
            general_fund(200),
        )
        self.commit(funds=funds, country_quotas={"VN": 2})
        self.open()
        self.submit("A1", at=1, country="VN",
                    scores=[("J1", 90), ("J2", 88), ("J3", 92)])
        self.submit("A2", at=2, country="VN", scores=[("J2", 80), ("J3", 84)])
        self.submit("A3", at=3, country="TH", scores=[("J1", 85), ("J2", 87)])
        self.submit("A4", at=4, country="TH", scores=[("J1", 70), ("J2", 72)])
        self.svc.declare_conflict(ROUND, judge_id="J1",
                                  id_document="DOC-A1", relation="合作导师")
        self.svc.run_allocation(ROUND)
        self.svc.withdraw(ROUND, "A3")                      # A4 递补进通用资金
        self.svc.file_appeal(ROUND, "AP-1", target_application_id="A1",
                             grounds="合作院校质疑评审回避")
        self.svc.resolve_appeal(ROUND, "AP-1", upheld=True, note="查明确应回避")
        self.svc.confirm_admission(ROUND, "A2")
        self.svc.confirm_admission(ROUND, "A4")

    def test_replay_reconstructs_every_decision(self):
        self._rich_round()
        report = replay_round(self.log_path, ROUND)
        self.assertTrue(report.verified)
        self.assertEqual(report.rule_revision, 1)

        a1_traces = report.traces_for("A1")
        admitted = [t for t in a1_traces if t.action == ADMITTED][0]
        self.assertEqual(admitted.detail["fund_id"], "F-VN")
        self.assertEqual(admitted.detail["recused_judges"], [["J1", "合作导师"]])
        self.assertEqual(admitted.detail["valid_judges"], ["J2", "J3"])
        reversed_trace = [t for t in a1_traces if t.action == "DECISION_REVERSED"]
        self.assertEqual(reversed_trace[0].detail["appeal_id"], "AP-1")

        a4_traces = report.traces_for("A4")
        promoted = [t for t in a4_traces if t.action == "PROMOTED"][0]
        self.assertEqual(promoted.detail["freed_by"], "A3")
        self.assertEqual(promoted.detail["fund_id"], "F-GEN")
        self.assertIn("fund", promoted.detail["remaining_after"])

        self.assertEqual(report.final_waitlist, ())
        self.assertEqual(report.fund_usage["F-GEN"], 200)
        self.assertEqual(report.fund_usage["F-VN"], 0)

    def test_replay_matches_live_state_after_restart(self):
        self._rich_round()
        svc2 = self.restart()
        report = replay_round(self.log_path, ROUND)
        self.assertEqual(tuple(svc2.waitlist(ROUND)), report.final_waitlist)
        usage = svc2.fund_usage(ROUND)
        self.assertEqual(usage["F-GEN"]["confirmed"], report.fund_usage["F-GEN"])
        self.assertEqual(svc2.slot_status(ROUND, "A1"), "CLOSED")
        self.assertEqual(svc2.slot_status(ROUND, "A2"), SLOT_CONFIRMED)

    def test_replay_detects_tampered_score(self):
        self._rich_round()
        lines = self.log_path.read_text(encoding="utf-8").splitlines()
        tampered = []
        for line in lines:
            event = json.loads(line)
            if event["kind"] == "SCORE_SUBMITTED" and event["payload"]["score"]["judge_id"] == "J2":
                event["payload"]["score"]["score"] = 99
            tampered.append(json.dumps(event, ensure_ascii=False, sort_keys=True))
        self.log_path.write_text("\n".join(tampered) + "\n", encoding="utf-8")
        with self.assertRaises(ReplayIntegrityError):
            replay_round(self.log_path, ROUND)

    def test_replay_detects_deleted_event(self):
        self._rich_round()
        lines = self.log_path.read_text(encoding="utf-8").splitlines()
        del lines[3]  # 删除中间一条事件
        self.log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            replay_round(self.log_path, ROUND)


if __name__ == "__main__":
    unittest.main()
