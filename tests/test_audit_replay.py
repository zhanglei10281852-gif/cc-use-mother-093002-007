"""审计离线重放与重启一致性测试。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from scholarship_pool import EventStore, ScholarshipGovernanceService, replay
from scenario_helper import make_service, setup_scored_round


class AuditReplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "journal.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def _full_lifecycle(self):
        svc = ScholarshipGovernanceService(EventStore(self.path))
        setup_scored_round(svc)
        svc.run_allocation("ROUND-1")
        appeal = svc.file_appeal("ROUND-1", "APP-04", "申请复核材料版本")
        svc.reject_appeal(appeal, "材料版本核对无误")
        svc.withdraw("APP-01", "个人原因放弃")  # 触发 APP-06 递补
        svc.confirm_admission("ROUND-1", "APP-02")
        return svc

    def test_replay_recomputes_and_verifies_chain(self):
        self._full_lifecycle()
        report = replay(self.path, "ROUND-1")
        self.assertTrue(report.chain_ok)
        self.assertTrue(report.recompute_match)
        self.assertEqual(report.rule_revision, 1)
        # 台账逐条还原：录取、放弃、释放、递补、申诉、改判依据
        types = {e["type"] for e in report.ledger}
        self.assertIn("round_allocated", types)
        self.assertIn("seat_released", types)
        self.assertIn("seat_promoted", types)
        self.assertIn("appeal_filed", types)
        promoted = next(e for e in report.ledger if e["type"] == "seat_promoted")
        self.assertEqual(promoted["payload"]["promoted_application_id"], "APP-06")
        self.assertIn("不允许人工插队", promoted["payload"]["rule_basis"])
        # 回避处理可逐条列出
        self.assertIsInstance(report.conflicts_applied, list)
        # 配额视图包含国别、项目、定向资金三类额度
        self.assertEqual(report.quota_view["country_quotas"]["VN"], 2)
        pool_ids = {p["pool_id"] for p in report.quota_view["pools"]}
        self.assertEqual(pool_ids, {"E-MEKONG", "E-MA-VN", "GENERAL"})

    def test_restart_preserves_waitlist_holds_and_promotions(self):
        self._full_lifecycle()
        svc = ScholarshipGovernanceService(EventStore(self.path))
        before = svc.rounds["ROUND-1"]
        restarted = ScholarshipGovernanceService(EventStore(self.path))
        after = restarted.rounds["ROUND-1"]
        self.assertEqual(
            before.result.to_dict()["waitlist"],
            after.result.to_dict()["waitlist"],
        )
        self.assertEqual(before.promoted, after.promoted)
        self.assertEqual(before.vacated, after.vacated)
        self.assertEqual(before.confirmed, after.confirmed)
        self.assertEqual(before.vacant_pools, after.vacant_pools)
        self.assertEqual(before.result_hash, after.result_hash)
        # 重启后再做一次资格撤销：候补 APP-06 已递补录取，无后续候补，
        # GENERAL 池名额空置且候补顺序保持不变
        restarted.disqualify("APP-02", "资格复查不通过")
        round_ = restarted.rounds["ROUND-1"]
        self.assertIn("APP-02", round_.vacated)
        self.assertEqual(round_.result.waitlist, ["APP-06"])
        self.assertEqual(round_.vacant_pools.get("GENERAL"), 1)

    def test_restart_mid_promotion_keeps_order(self):
        svc = ScholarshipGovernanceService(EventStore(self.path))
        setup_scored_round(svc)
        svc.run_allocation("ROUND-1")
        hold_appeal = svc.file_appeal("ROUND-1", "APP-06", "候补位申诉冻结")
        svc.withdraw("APP-01")  # 首位候补冻结，名额挂起
        mid = ScholarshipGovernanceService(EventStore(self.path))
        mid_round = mid.rounds["ROUND-1"]
        self.assertIn("APP-06", mid_round.holds)
        self.assertEqual(mid_round.promoted, {})
        self.assertEqual(mid_round.vacant_pools.get("E-MA-VN"), 1)
        # 重启后了结申诉，照样按原顺序递补
        mid.reject_appeal(hold_appeal, "驳回")
        self.assertEqual(mid_round.promoted.get("APP-06"), "E-MA-VN")

    def test_tampered_journal_is_detected(self):
        self._full_lifecycle()
        lines = Path(self.path).read_text(encoding="utf-8").splitlines()
        # 篡改一条评分记录中的评委编号（仍是合法 JSON，但内容变了）
        for i, line in enumerate(lines):
            if '"score_submitted"' in line and '"R-01"' in line:
                lines[i] = line.replace('"R-01"', '"R-99"', 1)
                break
        Path(self.path).write_text("\n".join(lines) + "\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            EventStore(self.path)  # 哈希链校验失败

    def test_report_is_json_serializable_and_uses_pseudonyms(self):
        self._full_lifecycle()
        report = replay(self.path, "ROUND-1").to_dict()
        encoded = json.dumps(report, ensure_ascii=False)
        self.assertIn("seat_promoted", encoded)
        self.assertNotIn("SECRET-APP", encoded)  # 证件信息绝不进入审计报告


if __name__ == "__main__":
    unittest.main()
