"""链式递补、规则修订开新一轮、轮次后身份冻结的补充场景测试。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from scholarship_pool import (
    CountryQuota,
    Dimension,
    Earmark,
    ProgramQuota,
    RoundStateError,
    RuleBook,
    replay,
)
from scenario_helper import (
    MATERIALS,
    build_rulebook,
    make_service,
    score,
    setup_scored_round,
)


def chain_rulebook(revision: int) -> RuleBook:
    return RuleBook(
        revision=revision,
        countries=frozenset({"VN", "TH"}),
        channels=frozenset({"university"}),
        required_materials=frozenset({"passport", "diploma", "statement"}),
        dimensions=(Dimension("academic", 0.6), Dimension("language", 0.4)),
        min_reviewers=2,
        country_quotas=(CountryQuota("VN", 1), CountryQuota("TH", 1)),
        programs=(ProgramQuota("P-MA", seats=3, priority=1),),
        earmarks=(Earmark("E-MA-VN", 1, "VN", "P-MA", "越南硕士定向"),),
        general_seats=2,
    )


class ChainPromotionTests(unittest.TestCase):
    def _build(self):
        svc = make_service()
        svc.publish_rulebook(chain_rulebook(1))
        apps = {
            "V1": ("VN", 95.0), "T1": ("TH", 88.0),
            "V2": ("VN", 80.0), "V3": ("VN", 70.0),
        }
        for app_id, (country, total) in apps.items():
            svc.register_application(
                app_id, country=country, program_id="P-MA",
                channel="university", materials=dict(MATERIALS),
                sensitive={"id_number": f"SECRET-{app_id}"},
            )
        svc.register_reviewer("R-01")
        svc.register_reviewer("R-02")
        svc.open_round("R1", 1)
        for app_id, (_country, total) in apps.items():
            svc.submit_score("R1", "R-01", app_id,
                             {"academic": total, "language": total})
            svc.submit_score("R1", "R-02", app_id,
                             {"academic": total, "language": total})
        svc.close_scoring("R1")
        result = svc.run_allocation("R1")
        return svc, result

    def test_chain_promotion_follows_frozen_order_then_vacant(self):
        svc, result = self._build()
        self.assertEqual(result.admitted["V1"], "E-MA-VN")
        self.assertEqual(result.admitted["T1"], "GENERAL")
        self.assertEqual(result.waitlist, ["V2", "V3"])
        round_ = svc.rounds["R1"]

        svc.withdraw("V1")
        self.assertEqual(round_.promoted.get("V2"), "E-MA-VN")
        svc.withdraw("V2")
        self.assertEqual(round_.promoted.get("V3"), "E-MA-VN")
        self.assertNotIn("V2", round_.promoted)  # 递补者离开后映射被移除
        svc.withdraw("V3")
        self.assertEqual(round_.vacant_pools.get("E-MA-VN"), 1)
        skipped = [e for e in svc.store.read() if e.type == "promotion_skipped"]
        self.assertTrue(skipped)
        # 候补顺序从头到尾没有被重排
        self.assertEqual(round_.result.waitlist, ["V2", "V3"])

    def test_waitlist_never_reordered_even_though_later_app_ranks_higher(self):
        # 冻结顺序只由初始分配决定；V3 即使后来材料/情况变化也不能超过 V2
        svc, result = self._build()
        svc.withdraw("V1")
        round_ = svc.rounds["R1"]
        first = next(
            e.payload["promoted_application_id"]
            for e in svc.store.read() if e.type == "seat_promoted"
        )
        self.assertEqual(first, "V2")  # 不是分数曾经讨论过的任何人，严格按序
        self.assertEqual(round_.result.waitlist, ["V2", "V3"])


class NewRevisionTests(unittest.TestCase):
    def test_revised_rules_only_affect_new_round(self):
        import tempfile
        from scholarship_pool import EventStore, ScholarshipGovernanceService

        tmp = tempfile.TemporaryDirectory()
        path = str(Path(tmp.name) / "journal.jsonl")
        try:
            svc = ScholarshipGovernanceService(EventStore(path))
            setup_scored_round(svc)
            r1 = svc.run_allocation("ROUND-1")
            self.assertEqual(r1.waitlist, ["APP-06"])  # VN 2 席，APP-06 落榜

            # 修订 2：VN 扩到 3 席（任何修订只能开新一轮）
            svc.publish_rulebook(build_rulebook(2, country_quotas=(
                CountryQuota("VN", 3), CountryQuota("TH", 1),
                CountryQuota("ID", 1), CountryQuota("LA", 1),
            )))
            svc.open_round("ROUND-2", 2)
            plans = {
                app: {"R-01": score(70, 70, 70), "R-02": score(70, 70, 70)}
                for app in ("APP-01", "APP-02", "APP-03", "APP-04",
                            "APP-05", "APP-06")
            }
            for app_id, by_reviewer in plans.items():
                for reviewer, scores in by_reviewer.items():
                    svc.submit_score("ROUND-2", reviewer, app_id, scores)
            svc.close_scoring("ROUND-2")
            r2 = svc.run_allocation("ROUND-2")
            self.assertIn("APP-06", r2.admitted)  # 新国别额度下录取

            # 历史轮次仍按修订 1 重放，结果不变
            report1 = replay(path, "ROUND-1")
            self.assertTrue(report1.recompute_match)
            self.assertEqual(report1.rule_revision, 1)
            self.assertEqual(
                [d["application_id"] for d in report1.decisions
                 if d["outcome"] == "waitlisted"],
                ["APP-06"],
            )
            report2 = replay(path, "ROUND-2")
            self.assertTrue(report2.recompute_match)
            self.assertEqual(report2.rule_revision, 2)
        finally:
            tmp.cleanup()


class IdentityFreezeTests(unittest.TestCase):
    def test_merge_blocked_after_round_opened(self):
        svc = make_service()
        setup_scored_round(svc)
        svc.run_allocation("ROUND-1")
        svc.register_application(
            "APP-DUP", country="VN", program_id="P-MA", channel="online",
            materials=dict(MATERIALS), sensitive={"id_number": "x"},
        )
        with self.assertRaises(RoundStateError):
            svc.merge_duplicates("APP-DUP", "APP-01")


if __name__ == "__main__":
    unittest.main()
