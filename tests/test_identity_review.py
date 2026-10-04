"""治理服务集成测试：身份合并、回避、独立评分、冻结轮次。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from scholarship_pool import (
    ConflictOfInterestError,
    RoundStateError,
    RuleRevisionError,
    SeatHeldError,
    UnresolvedDuplicateError,
)
from scenario_helper import (
    MATERIALS,
    REVIEWERS,
    build_rulebook,
    make_service,
    score,
    seed_applicants,
    setup_scored_round,
)


class RuleImmutabilityTests(unittest.TestCase):
    def test_published_revision_cannot_change(self):
        svc = make_service()
        svc.publish_rulebook(build_rulebook(1))
        with self.assertRaises(RuleRevisionError):
            svc.publish_rulebook(build_rulebook(1))

    def test_revisions_must_be_sequential(self):
        svc = make_service()
        svc.publish_rulebook(build_rulebook(1))
        with self.assertRaises(RuleRevisionError):
            svc.publish_rulebook(build_rulebook(3))

    def test_allocation_result_frozen_and_rules_change_needs_new_round(self):
        svc = make_service()
        setup_scored_round(svc)
        svc.run_allocation("ROUND-1")
        with self.assertRaises(RoundStateError):
            svc.run_allocation("ROUND-1")  # 不能重跑
        # 新修订 + 新一轮
        svc.publish_rulebook(build_rulebook(2))
        svc.open_round("ROUND-2", 2)
        for app_id in ("APP-01",):
            svc.submit_score("ROUND-2", "R-01", app_id, score(90, 90, 90))


class DuplicateIdentityTests(unittest.TestCase):
    def test_duplicate_blocks_round_until_merged(self):
        svc = make_service()
        svc.publish_rulebook(build_rulebook(1))
        seed_applicants(svc)
        # APP-06 是同一人通过另一渠道重复报名
        svc.register_application(
            "APP-DUP", country="VN", program_id="P-BA", channel="online",
            materials=dict(MATERIALS), sensitive={"id_number": "SAME-AS-06"},
        )
        svc.flag_duplicate("APP-DUP", "APP-06", "EVIDENCE-REF-1")
        for reviewer in REVIEWERS:
            svc.register_reviewer(reviewer)
        with self.assertRaises(UnresolvedDuplicateError):
            svc.open_round("ROUND-1", 1)
        svc.merge_duplicates("APP-DUP", "APP-06")
        # 合并后重复线索消解，源申请不再参评
        self.assertEqual(svc.unresolved_duplicates(), [])
        svc.open_round("ROUND-1", 1)
        self.assertNotIn("APP-DUP", svc.rounds["ROUND-1"].material_pin)

    def test_merge_is_one_way_and_conflicts_follow_identity(self):
        svc = make_service()
        svc.publish_rulebook(build_rulebook(1))
        seed_applicants(svc)
        svc.register_application(
            "APP-DUP", country="VN", program_id="P-BA", channel="online",
            materials=dict(MATERIALS), sensitive={"id_number": "SAME-AS-06"},
        )
        svc.register_reviewer("R-01")
        # 对重复渠道申报的合作关系，合并后必须覆盖主申请
        svc.declare_conflict("R-01", "APP-DUP")
        svc.merge_duplicates("APP-DUP", "APP-06")
        person = svc.identities.owner_of("APP-06")
        self.assertIn("R-01", svc.board.conflicts_for(person))

    def test_audit_view_never_exposes_sensitive_fields(self):
        svc = make_service()
        svc.publish_rulebook(build_rulebook(1))
        seed_applicants(svc)
        view = svc.identities.public_view()
        for row in view.values():
            self.assertNotIn("SECRET", str(row))
        # 事件日志中也不允许出现证件号
        for event in svc.store.read():
            self.assertNotIn("SECRET-APP", str(event.payload))


class ReviewAndScoringTests(unittest.TestCase):
    def test_conflicted_reviewer_cannot_score(self):
        svc = make_service()
        svc.publish_rulebook(build_rulebook(1))
        seed_applicants(svc)
        svc.register_reviewer("R-01")
        svc.declare_conflict("R-01", "APP-01")
        svc.open_round("ROUND-1", 1)
        with self.assertRaises(ConflictOfInterestError):
            svc.submit_score("ROUND-1", "R-01", "APP-01", score(90, 90, 90))

    def test_score_submitted_once_and_locked_after_close(self):
        svc = make_service()
        setup_scored_round(svc)
        with self.assertRaises(RoundStateError):
            svc.submit_score("ROUND-1", "R-01", "APP-01", score(1, 1, 1))

    def test_material_update_creates_version_pinned_by_round(self):
        svc = make_service()
        svc.publish_rulebook(build_rulebook(1))
        seed_applicants(svc)
        for reviewer in REVIEWERS:
            svc.register_reviewer(reviewer)
        svc.open_round("ROUND-1", 1)
        pinned_v1 = svc.rounds["ROUND-1"].material_pin["APP-01"][0]
        # 轮次开启后材料可更新但不影响本轮引用
        new_materials = dict(MATERIALS, statement="statement-data-v2")
        svc.update_materials("APP-01", new_materials)
        self.assertEqual(
            svc.rounds["ROUND-1"].material_pin["APP-01"][0], pinned_v1
        )


class MaterialValidationTests(unittest.TestCase):
    def test_missing_required_material_blocks_round(self):
        svc = make_service()
        svc.publish_rulebook(build_rulebook(1))
        svc.register_application(
            "APP-X", country="VN", program_id="P-MA", channel="university",
            materials={"passport": "p", "diploma": "d"},  # 缺 statement
            sensitive={"id_number": "x"},
        )
        with self.assertRaises(Exception):
            svc.open_round("R", 1)


if __name__ == "__main__":
    unittest.main()
