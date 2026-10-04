"""递补、申诉冻结、改判与并发资金上限测试。"""
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from scholarship_pool import SeatHeldError
from scenario_helper import make_service, setup_scored_round


class PromotionTests(unittest.TestCase):
    def test_withdraw_admit_auto_promotes_in_waitlist_order(self):
        svc = make_service()
        setup_scored_round(svc)
        result = svc.run_allocation("ROUND-1")
        self.assertEqual(result.waitlist, ["APP-06"])
        # APP-01（VN，E-MA-VN 定向池）放弃 → VN 国别名额与定向池同时腾出，
        # 候补首位 APP-06（VN，P-MA）自动递补，不允许人工插队
        svc.withdraw("APP-01", "个人原因放弃")
        round_ = svc.rounds["ROUND-1"]
        self.assertIn("APP-01", round_.vacated)
        self.assertEqual(round_.promoted.get("APP-06"), "E-MA-VN")
        promotion = next(
            e for e in svc.store.read() if e.type == "seat_promoted"
        )
        self.assertEqual(promotion.payload["promoted_application_id"], "APP-06")
        self.assertEqual(promotion.payload["waitlist_position"], 1)
        self.assertIn("不允许人工插队", promotion.payload["rule_basis"])

    def test_disqualification_releases_and_promotes(self):
        svc = make_service()
        setup_scored_round(svc)
        svc.run_allocation("ROUND-1")
        svc.disqualify("APP-03", "材料造假核实")
        # TH 只有 APP-03 一名申请人，候补 APP-06 是 VN，无法使用 TH 释放出的
        # GENERAL 席（国别 VN 已满）→ 名额空置但候补顺序不动
        round_ = svc.rounds["ROUND-1"]
        self.assertEqual(round_.waitlist if hasattr(round_, "waitlist") else
                         round_.result.waitlist, ["APP-06"])
        skipped = [e for e in svc.store.read() if e.type == "promotion_skipped"]
        self.assertTrue(skipped)
        self.assertNotIn("APP-06", round_.promoted)

    def test_no_queue_jumping_when_front_is_held(self):
        svc = make_service()
        setup_scored_round(svc)
        svc.run_allocation("ROUND-1")
        # APP-06 就候补位申诉（冻结其候补位置）
        appeal_id = svc.file_appeal("ROUND-1", "APP-06", "怀疑评分计算错误")
        svc.withdraw("APP-01")
        round_ = svc.rounds["ROUND-1"]
        # 候补首位被冻结 → 不能跳过它把别人提上来（也没有别人）
        self.assertEqual(round_.promoted, {})
        # 申诉驳回、冻结解除后，自动按原顺序递补
        svc.reject_appeal(appeal_id, "核算无误")
        self.assertEqual(round_.promoted.get("APP-06"), "E-MA-VN")

    def test_promoted_admit_withdrawing_frees_again(self):
        svc = make_service()
        setup_scored_round(svc)
        svc.run_allocation("ROUND-1")
        svc.withdraw("APP-01")
        round_ = svc.rounds["ROUND-1"]
        self.assertEqual(round_.promoted.get("APP-06"), "E-MA-VN")
        # 递补者再放弃：名额再次空置，候补中已无人
        svc.withdraw("APP-06")
        self.assertIn("APP-06", round_.vacated)
        self.assertNotIn("APP-06", round_.promoted)


class AppealTests(unittest.TestCase):
    def test_appeal_freezes_only_affected_seat(self):
        svc = make_service()
        setup_scored_round(svc)
        svc.run_allocation("ROUND-1")
        svc.confirm_admission("ROUND-1", "APP-02")
        appeal_id = svc.file_appeal("ROUND-1", "APP-01", "对回避情况有异议")
        round_ = svc.rounds["ROUND-1"]
        self.assertEqual(round_.holds, {"APP-01"})
        # 其他名额的确认不受影响
        svc.confirm_admission("ROUND-1", "APP-03")
        # 被冻结的名额不能确认也不能放弃
        with self.assertRaises(SeatHeldError):
            svc.confirm_admission("ROUND-1", "APP-01")
        with self.assertRaises(SeatHeldError):
            svc.withdraw("APP-01")
        # 申诉成立：撤位并自动递补
        svc.uphold_appeal(appeal_id, "核实存在应回避未回避")
        self.assertIn("APP-01", round_.vacated)
        self.assertEqual(round_.promoted.get("APP-06"), "E-MA-VN")

    def test_regrade_keeps_seat_when_score_still_above_cutoff(self):
        svc = make_service()
        setup_scored_round(svc)
        svc.run_allocation("ROUND-1")
        # APP-01 分数远高于其他人，剔除一名评委后仍居首
        appeal_id = svc.file_appeal("ROUND-1", "APP-01", "R-03 存在合作关系")
        svc.regrade_appeal(appeal_id, excluded_reviewer="R-03",
                           note="补充申报的利益冲突")
        round_ = svc.rounds["ROUND-1"]
        self.assertNotIn("APP-01", round_.vacated)
        amended = next(e for e in svc.store.read() if e.type == "round_amended")
        self.assertTrue(amended.payload["keep_seat"])
        # 候补顺序原样保留
        self.assertEqual(
            amended.payload["waitlist_unchanged"], round_.result.waitlist
        )

    def test_regrade_keeps_earmark_seat_when_nobody_can_replace(self):
        svc = make_service()
        setup_scored_round(svc)
        svc.run_allocation("ROUND-1")
        # APP-05 占用 LA 定向池 E-MEKONG；剔除 R-01 后分数下滑，
        # 但候补 APP-06 是 VN，不满足 LA 定向限制，无人可接替：
        # 名额不得空置，APP-05 保位，事件中记录候补不可接替的判定
        appeal_id = svc.file_appeal("ROUND-1", "APP-05", "评委 R-01 系亲属")
        svc.regrade_appeal(appeal_id, excluded_reviewer="R-01")
        round_ = svc.rounds["ROUND-1"]
        self.assertNotIn("APP-05", round_.vacated)
        amended = next(e for e in svc.store.read() if e.type == "round_amended")
        self.assertTrue(amended.payload["keep_seat"])
        self.assertIsNone(amended.payload["cutoff_score"])  # 无可接替者
        self.assertEqual(round_.result.waitlist, ["APP-06"])

    def test_regrade_with_insufficient_reviewers_vacates_and_promotes(self):
        # 定制场景：A 靠受污染评委的高分险胜 W；剔除后有效评委不足 → 撤位，
        # W 按冻结候补顺序自动递补
        from scholarship_pool import (
            CountryQuota, Dimension, Earmark, ProgramQuota, RuleBook,
        )
        rulebook = RuleBook(
            revision=1,
            countries=frozenset({"VN"}),
            channels=frozenset({"university"}),
            required_materials=frozenset({"passport"}),
            dimensions=(Dimension("academic", 0.6), Dimension("language", 0.4)),
            min_reviewers=2,
            country_quotas=(CountryQuota("VN", 1),),
            programs=(ProgramQuota("P-MA", seats=2, priority=1),),
            earmarks=(),
            general_seats=1,
        )
        svc = make_service()
        svc.publish_rulebook(rulebook)
        mats = {"passport": "pp"}
        svc.register_application("A", "VN", "P-MA", "university", mats, {"id": "a"})
        svc.register_application("W", "VN", "P-MA", "university", mats, {"id": "w"})
        svc.register_reviewer("R-01")
        svc.register_reviewer("R-02")
        svc.open_round("R1", 1)
        # A: R-01 打高分（污染）, R-02 打低分 → 均值 77.5
        svc.submit_score("R1", "R-01", "A", {"academic": 95, "language": 95})
        svc.submit_score("R1", "R-02", "A", {"academic": 60, "language": 60})
        # W: 两位评委都打 75 → 75
        svc.submit_score("R1", "R-01", "W", {"academic": 75, "language": 75})
        svc.submit_score("R1", "R-02", "W", {"academic": 75, "language": 75})
        svc.close_scoring("R1")
        result = svc.run_allocation("R1")
        self.assertEqual(list(result.admitted), ["A"])
        self.assertEqual(result.waitlist, ["W"])

        appeal_id = svc.file_appeal("R1", "A", "R-01 为指导教师未回避")
        svc.regrade_appeal(appeal_id, excluded_reviewer="R-01",
                           note="指导教师关系，评分剔除")
        round_ = svc.rounds["R1"]
        self.assertIn("A", round_.vacated)
        self.assertEqual(round_.promoted.get("W"), "GENERAL")
        amended = next(e for e in svc.store.read() if e.type == "round_amended")
        self.assertFalse(amended.payload["keep_seat"])
        self.assertIsNone(amended.payload["new_weighted_score"])  # 有效评委不足
        # 候补顺序没有被重排
        self.assertEqual(round_.result.waitlist, ["W"])


class RegradeReplayTests(unittest.TestCase):
    def test_regrade_state_survives_restart_and_skip_announced_once(self):
        import tempfile
        from scholarship_pool import EventStore, ScholarshipGovernanceService

        tmp = tempfile.TemporaryDirectory()
        path = str(Path(tmp.name) / "journal.jsonl")
        try:
            svc = ScholarshipGovernanceService(EventStore(path))
            setup_scored_round(svc)
            svc.run_allocation("ROUND-1")
            # APP-05 剔除 R-01 后分数下滑，但 LA 定向池无人可接替 → 保位
            appeal = svc.file_appeal("ROUND-1", "APP-05", "评委系亲属")
            svc.regrade_appeal(appeal, excluded_reviewer="R-01")
            # TH 申请人 APP-03 放弃：GENERAL 池空置，候补 APP-06（VN）
            # 受国别限制不能接替 → 公告一次“无可递补者”
            svc.withdraw("APP-03")
            round_ = svc.rounds["ROUND-1"]
            self.assertEqual(round_.vacant_pools.get("GENERAL"), 1)
            skip_count_before = sum(
                1 for e in svc.store.read() if e.type == "promotion_skipped"
            )
            self.assertGreaterEqual(skip_count_before, 1)
            # 再次触发填补尝试（驳回另一无关申诉）不应重复公告同一空置数
            other = svc.file_appeal("ROUND-1", "APP-04", "核查材料")
            svc.reject_appeal(other, "无误")
            skip_count_after = sum(
                1 for e in svc.store.read() if e.type == "promotion_skipped"
            )
            self.assertEqual(skip_count_before, skip_count_after)
            # 重启后申诉状态、冻结集合、改判分数、空置账本全部还原
            reborn = ScholarshipGovernanceService(EventStore(path))
            rnd = reborn.rounds["ROUND-1"]
            self.assertEqual(rnd.appeals[appeal].status, "regraded")
            self.assertNotIn("APP-05", rnd.holds)
            self.assertIn("APP-05", rnd.amended_scores)
            self.assertNotIn("APP-05", rnd.vacated)
            self.assertEqual(rnd.vacant_pools.get("GENERAL"), 1)
        finally:
            tmp.cleanup()


class ConcurrencyTests(unittest.TestCase):
    def test_parallel_confirms_and_promotions_never_exceed_caps(self):
        svc = make_service()
        setup_scored_round(svc)
        svc.run_allocation("ROUND-1")
        errors = []

        def worker(app_id):
            try:
                svc.confirm_admission("ROUND-1", app_id)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        # APP-01..APP-05 各被两个线程重复确认；只有第一次成功
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = []
            for app_id in ("APP-01", "APP-02", "APP-03", "APP-04", "APP-05"):
                futures.append(pool.submit(worker, app_id))
                futures.append(pool.submit(worker, app_id))
            for f in futures:
                f.result()
        self.assertEqual(len(errors), 5)
        round_ = svc.rounds["ROUND-1"]
        self.assertEqual(
            sorted(round_.confirmed),
            ["APP-01", "APP-02", "APP-03", "APP-04", "APP-05"],
        )
        # 实际占用绝不超过任何额度
        country_used, program_used = svc._usage(round_)
        rulebook = svc.rulebook(1)
        for country, used in country_used.items():
            self.assertLessEqual(used, rulebook.country_seats[country])
        for program, used in program_used.items():
            self.assertLessEqual(used, rulebook.program_seats[program])


if __name__ == "__main__":
    unittest.main()
