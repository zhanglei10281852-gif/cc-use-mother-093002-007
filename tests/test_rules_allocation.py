"""规则书、配额、定向资金与确定性分配的单元测试。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from scholarship_pool import (
    CandidateInput,
    CountryQuota,
    Dimension,
    Earmark,
    GENERAL_POOL,
    ProgramQuota,
    RuleBook,
    ValidationError,
    allocate,
)
from scholarship_pool.allocation import ADMITTED, WAITLISTED


def book(rev=1, **kw):
    base = dict(
        revision=rev,
        countries=frozenset({"VN", "TH"}),
        channels=frozenset({"web"}),
        required_materials=frozenset({"passport"}),
        dimensions=(Dimension("a", 0.6), Dimension("b", 0.4)),
        min_reviewers=2,
        country_quotas=(CountryQuota("VN", 1), CountryQuota("TH", 2)),
        programs=(
            ProgramQuota("P1", seats=2, priority=1),
            ProgramQuota("P2", seats=2, priority=2),
        ),
        earmarks=(Earmark("E-VN", 1, "VN", None),),
        general_seats=2,
    )
    base.update(kw)
    return RuleBook(**base)


def cand(app, country="VN", program="P1", sheets=None, priority=None,
         excluded=(), score_a=90.0, score_b=90.0):
    if sheets is None:
        sheets = {"R1": {"a": score_a, "b": score_b},
                  "R2": {"a": score_a, "b": score_b}}
    return CandidateInput(
        application_id=app, pseudonym="X-" + app, country=country,
        program_id=program, program_priority=priority or 1,
        material_version=1, material_hash="h1",
        sheets=sheets, excluded_reviewers=tuple(excluded),
    )


class RuleBookTests(unittest.TestCase):
    def test_weights_must_sum_to_one(self):
        with self.assertRaises(ValueError):
            book(dimensions=(Dimension("a", 0.5), Dimension("b", 0.4)))

    def test_duplicate_quota_definitions_rejected(self):
        with self.assertRaises(ValueError):
            book(country_quotas=(CountryQuota("VN", 1), CountryQuota("VN", 2)))

    def test_general_pool_id_reserved(self):
        with self.assertRaises(ValueError):
            Earmark(GENERAL_POOL, 1, None, None)

    def test_content_hash_stable_and_distinct(self):
        b1 = book(1)
        b1_again = book(1)
        b2 = book(2, general_seats=9)
        self.assertEqual(b1.content_hash, b1_again.content_hash)
        self.assertNotEqual(b1.content_hash, b2.content_hash)


class AllocationTests(unittest.TestCase):
    def test_deterministic_ranking_and_pools(self):
        c1 = cand("APP-1", score_a=95)
        c2 = cand("APP-2", "TH", "P2", priority=2, score_a=80)
        result = allocate(book(), [c2, c1])  # 乱序输入
        self.assertEqual([r.application_id for r in result.ranked], ["APP-1", "APP-2"])
        self.assertEqual(result.admitted["APP-1"], "E-VN")  # 定向池优先匹配
        self.assertEqual(result.decisions[0].detail.count("通用资金"), 0)

    def test_country_cap_creates_waitlist_in_rank_order(self):
        # 两名 VN 申请人竞争 VN 唯一国别名额
        c1 = cand("APP-1", score_a=95)
        c2 = cand("APP-2", score_a=70)
        result = allocate(book(), [c1, c2])
        self.assertIn("APP-1", result.admitted)
        self.assertEqual(result.waitlist, ["APP-2"])
        d2 = result.decisions[1]
        self.assertEqual(d2.outcome, WAITLISTED)
        self.assertEqual(d2.waitlist_position, 1)
        self.assertIn("国别 VN 名额 1 已满", d2.detail)

    def test_program_cap_blocks_admission(self):
        b = book(programs=(ProgramQuota("P1", 1, 1), ProgramQuota("P2", 1, 2)),
                 country_quotas=(CountryQuota("TH", 2),),
                 countries=frozenset({"TH"}),
                 earmarks=(), general_seats=2)
        c1 = cand("A1", "TH", "P1", score_a=90)
        c2 = cand("A2", "TH", "P1", score_a=85)
        result = allocate(b, [c1, c2])
        self.assertIn("A1", result.admitted)
        self.assertEqual(result.waitlist, ["A2"])
        self.assertIn("项目 P1 名额 1 已满", result.decisions[1].detail)

    def test_earmark_restriction_does_not_leak_across_country(self):
        # E-VN 只能被 VN 申请人使用；TH 申请人只能使用通用池
        b = book(country_quotas=(CountryQuota("VN", 1), CountryQuota("TH", 1)),
                 earmarks=(Earmark("E-VN", 1, "VN", None),),
                 general_seats=1)
        c1 = cand("T1", "TH", "P1", priority=1, score_a=95)  # 最高分但是 TH
        c2 = cand("V1", "VN", "P1", priority=1, score_a=80)
        result = allocate(b, [c1, c2])
        self.assertEqual(result.admitted["T1"], GENERAL_POOL)
        self.assertEqual(result.admitted["V1"], "E-VN")

    def test_excluded_reviewer_aggregation_and_min_reviewers(self):
        # 三名评委中一名回避，剩两名有效 -> 正常参评
        sheets = {r: {"a": 80.0, "b": 80.0} for r in ("R1", "R2", "R3")}
        c1 = cand("APP-1", sheets=sheets, excluded=[("R3", "利益冲突回避")])
        result = allocate(book(), [c1])
        self.assertEqual(result.ranked[0].reviewer_count, 2)
        # 只剩一名有效评委 -> 不进入排序
        c2 = cand("APP-2", sheets=dict(sheets),
                  excluded=[("R2", "x"), ("R3", "y")])
        result2 = allocate(book(), [c2])
        self.assertEqual(result2.ranked, [])
        self.assertIn("少于规则要求", result2.excluded["APP-2"])

    def test_tie_break_is_deterministic(self):
        # 完全同分：项目优先级（数字小）→ 评委数 → 申请编号
        c_low_prio = cand("ZZZ", priority=1, score_a=80, score_b=80)
        c_high_prio = cand("AAA", priority=2, score_a=80, score_b=80)
        result = allocate(book(), [c_high_prio, c_low_prio])
        self.assertEqual(result.ranked[0].application_id, "ZZZ")

    def test_same_inputs_always_produce_byte_identical_result(self):
        import json
        candidates = [cand(f"APP-{i}", score_a=90 - i) for i in range(1, 4)]
        r1 = allocate(book(), candidates).to_dict()
        r2 = allocate(book(), list(reversed(candidates))).to_dict()
        self.assertEqual(json.dumps(r1, sort_keys=True),
                         json.dumps(r2, sort_keys=True))


if __name__ == "__main__":
    unittest.main()
