"""命令行冒烟：演示一轮完整的奖学金名额治理流程。

场景：面向东盟的中文教育项目首轮奖学金。
两所合作院校与官网三个渠道报名，出现同一申请人多渠道重复报名、
评委与申请人存在合作关系等情况，由治理服务统一处置。
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))
from scholarship_pool import (
    FundPoolRule,
    GovernanceService,
    MaterialVersion,
    RuleSet,
    replay_round,
)

ROUND = "R-2026-01"


def materials(app_id):
    return [
        MaterialVersion("学历证明", 1, f"digest-{app_id}-edu", True),
        MaterialVersion("语言成绩", 1, f"digest-{app_id}-lang", True),
    ]


def main():
    log_path = Path(tempfile.mkdtemp(prefix="scholarship-demo-")) / "events.jsonl"
    svc = GovernanceService(log_path, identity_secret=b"demo-secret")

    # 1) 规则 v1：国别配额 + 越南定向基金 + 区域通用基金
    svc.commit_ruleset(RuleSet(
        revision=1,
        country_quotas={"VN": 2, "TH": 1},
        program_quotas={},
        funds=(
            FundPoolRule("F-VN", "越南定向基金", 100, 100, only_countries={"VN"}),
            FundPoolRule("F-GEN", "区域通用基金", 200, 100),
        ),
        required_materials=("学历证明", "语言成绩"),
        min_valid_scores=2,
    ))
    svc.open_round(ROUND, 1)

    # 2) 三渠道报名；APP-05 与 APP-01 是同一申请人换渠道重复报名
    svc.submit_application(ROUND, "APP-01", id_document="VN-9001", display_name="阮氏梅",
                           channel="院校A推荐", country="VN", program="CLS",
                           submitted_at=1, materials=materials("APP-01"))
    svc.submit_application(ROUND, "APP-02", id_document="TH-3002", display_name="差霖",
                           channel="院校B推荐", country="TH", program="CLS",
                           submitted_at=2, materials=materials("APP-02"))
    svc.submit_application(ROUND, "APP-03", id_document="VN-7003", display_name="黎文强",
                           channel="官网", country="VN", program="CLS",
                           submitted_at=3, materials=materials("APP-03"))
    svc.submit_application(ROUND, "APP-04", id_document="LA-1004", display_name="坎萍",
                           channel="院校A推荐", country="LA", program="CLS",
                           submitted_at=4, materials=materials("APP-04"))
    svc.submit_application(ROUND, "APP-05", id_document=" vn-9001 ", display_name="阮氏梅",
                           channel="官网", country="VN", program="CLS",
                           submitted_at=5, materials=materials("APP-05"))

    # 3) 评委 J1 与阮氏梅有合作关系，申报回避；各评委独立评分
    svc.declare_conflict(ROUND, judge_id="J1", id_document="VN-9001", relation="合作导师")
    for app_id, scores in {
        "APP-01": [("J1", 95), ("J2", 88), ("J3", 90)],
        "APP-02": [("J1", 80), ("J2", 84)],
        "APP-03": [("J2", 86), ("J3", 84)],
        "APP-04": [("J1", 91), ("J3", 93)],
        "APP-05": [("J1", 95), ("J2", 88), ("J3", 90)],
    }.items():
        for judge_id, score in scores:
            svc.submit_score(ROUND, judge_id=judge_id, application_id=app_id,
                             score=score, submitted_at=10)

    # 4) 封存分配：J1 对 APP-01 的 95 分被回避排除；APP-05 因重复报名被合并
    svc.run_allocation(ROUND)

    # 5) APP-03 放弃，通用资金名额按原规则递补给 APP-02
    svc.withdraw(ROUND, "APP-03", reason="接受其他项目录取")

    # 6) 院校B对 APP-01 的录取提出申诉：只冻结该名额，APP-04 照常确认
    svc.file_appeal(ROUND, "AP-1", target_application_id="APP-01",
                    grounds="质疑国别名额计算")
    svc.confirm_admission(ROUND, "APP-04")
    svc.resolve_appeal(ROUND, "AP-1", upheld=False, note="名额核算无误")
    svc.confirm_admission(ROUND, "APP-01")
    svc.confirm_admission(ROUND, "APP-02")

    # 7) 规则修订产生 v2，只影响后续轮次，本轮结果封存不变
    svc.revise_ruleset(1, country_quotas={"VN": 2, "TH": 2})

    # 8) 审计离线重放：独立重算并逐条还原决策依据
    report = replay_round(log_path, ROUND)

    summary = {
        "事件日志": str(log_path),
        "重复报名合并": svc.duplicate_report(ROUND),
        "本轮决策": svc.round_report(ROUND)["decisions"],
        "资金使用": svc.fund_usage(ROUND),
        "重放校验通过": report.verified,
        "重放轨迹条数": len(report.traces),
        "重放后候补": list(report.final_waitlist),
        "规则版本": {"本轮": report.rule_revision, "最新": svc.ruleset(2).revision},
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
