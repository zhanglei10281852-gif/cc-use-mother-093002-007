"""命令行冒烟：演示一轮完整的奖学金名额治理流程。"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from scholarship_pool import (
    CountryQuota,
    Dimension,
    Earmark,
    EventStore,
    ProgramQuota,
    RuleBook,
    ScholarshipGovernanceService,
    replay,
)


def main() -> None:
    tmp = tempfile.TemporaryDirectory()
    journal = str(Path(tmp.name) / "journal.jsonl")
    svc = ScholarshipGovernanceService(EventStore(journal))

    rulebook = RuleBook(
        revision=1,
        countries=frozenset({"VN", "TH", "LA"}),
        channels=frozenset({"university", "online"}),
        required_materials=frozenset({"passport", "diploma", "statement"}),
        dimensions=(Dimension("academic", 0.5), Dimension("language", 0.3),
                    Dimension("potential", 0.2)),
        min_reviewers=2,
        country_quotas=(CountryQuota("VN", 1), CountryQuota("TH", 1),
                        CountryQuota("LA", 1)),
        programs=(ProgramQuota("P-MA", seats=3, priority=1),
                  ProgramQuota("P-BA", seats=2, priority=2)),
        earmarks=(Earmark("E-LA", seats=1, country="LA", program_id=None,
                          purpose="湄公河定向资助"),),
        general_seats=2,
    )
    svc.publish_rulebook(rulebook)

    materials = {"passport": "pp", "diploma": "dp", "statement": "st"}
    applicants = {
        "APP-VN-1": ("VN", "P-MA", "university", 92),
        "APP-VN-2": ("VN", "P-BA", "online", 65),
        "APP-TH-1": ("TH", "P-MA", "university", 85),
        "APP-LA-1": ("LA", "P-MA", "university", 78),
    }
    for app_id, (country, program, channel, mark) in applicants.items():
        svc.register_application(
            app_id, country=country, program_id=program, channel=channel,
            materials=materials, sensitive={"id_number": "INTERNAL-SECRET"},
        )

    svc.register_reviewer("R-01")
    svc.register_reviewer("R-02")
    # R-02 与 APP-TH-1 存在合作关系，申报后自动回避
    svc.declare_conflict("R-02", "APP-TH-1")

    svc.open_round("ROUND-2026-1", rule_revision=1)
    for app_id, (_c, _p, _ch, mark) in applicants.items():
        svc.submit_score("ROUND-2026-1", "R-01", app_id,
                         {"academic": mark, "language": mark, "potential": mark})
        if app_id != "APP-TH-1":  # 回避评委不评分；另配 R-03 保证有效评委数
            svc.submit_score("ROUND-2026-1", "R-02", app_id,
                             {"academic": mark, "language": mark, "potential": mark})
    svc.register_reviewer("R-03")
    svc.submit_score("ROUND-2026-1", "R-03", "APP-TH-1",
                     {"academic": 85, "language": 85, "potential": 85})
    svc.close_scoring("ROUND-2026-1")

    result = svc.run_allocation("ROUND-2026-1")
    summary = {
        "规则修订": 1,
        "规则哈希": rulebook.content_hash[:12] + "...",
        "录取": [
            {"申请": d.application_id, "资金池": d.pool_id, "分数": d.weighted_score}
            for d in result.decisions if d.outcome == "admitted"
        ],
        "候补顺序": result.waitlist,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    # 放弃录取者 → 按冻结候补顺序自动递补
    svc.withdraw("APP-VN-1", "个人原因")
    report = replay(journal, "ROUND-2026-1")
    print(json.dumps({
        "审计重放": {
            "哈希链校验": report.chain_ok,
            "分配重算一致": report.recompute_match,
            "回避记录": report.conflicts_applied,
            "最终录取": report.final_state["admitted"],
        }
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
