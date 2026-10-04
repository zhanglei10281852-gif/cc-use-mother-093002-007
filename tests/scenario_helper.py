"""治理服务端到端测试工厂：构造面向东盟的两轮奖学金场景。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from scholarship_pool import (
    CountryQuota,
    Dimension,
    Earmark,
    EventStore,
    ProgramQuota,
    RuleBook,
    ScholarshipGovernanceService,
)

DIM_ACADEMIC = "academic"
DIM_LANGUAGE = "language"
DIM_POTENTIAL = "potential"


def build_rulebook(revision: int = 1, **overrides) -> RuleBook:
    base = dict(
        countries=frozenset({"VN", "TH", "ID", "LA"}),
        channels=frozenset({"university", "online", "embassy"}),
        required_materials=frozenset({"passport", "diploma", "statement"}),
        dimensions=(
            Dimension(DIM_ACADEMIC, 0.5),
            Dimension(DIM_LANGUAGE, 0.3),
            Dimension(DIM_POTENTIAL, 0.2),
        ),
        min_reviewers=2,
        country_quotas=(
            CountryQuota("VN", 2),
            CountryQuota("TH", 1),
            CountryQuota("ID", 1),
            CountryQuota("LA", 1),
        ),
        programs=(
            ProgramQuota("P-MA", seats=4, priority=1),   # 硕士优先项目
            ProgramQuota("P-BA", seats=2, priority=2),
        ),
        earmarks=(
            Earmark("E-MEKONG", seats=1, country="LA", program_id=None,
                    purpose="湄公河流域定向资助"),
            Earmark("E-MA-VN", seats=1, country="VN", program_id="P-MA",
                    purpose="越南硕士定向资助"),
        ),
        general_seats=3,
    )
    base.update(overrides)
    return RuleBook(revision=revision, **base)


MATERIALS = {
    "passport": "pp-data",
    "diploma": "diploma-data",
    "statement": "statement-data",
}


def score(academic: float, language: float, potential: float) -> dict[str, float]:
    return {
        DIM_ACADEMIC: academic,
        DIM_LANGUAGE: language,
        DIM_POTENTIAL: potential,
    }


def make_service(path: str | None = None) -> ScholarshipGovernanceService:
    return ScholarshipGovernanceService(EventStore(path))


def seed_applicants(svc: ScholarshipGovernanceService) -> dict[str, str]:
    """登记 6 名申请人，返回 application_id -> person_id。"""
    data = {
        "APP-01": ("VN", "P-MA", "university"),
        "APP-02": ("VN", "P-BA", "online"),
        "APP-03": ("TH", "P-MA", "university"),
        "APP-04": ("ID", "P-BA", "embassy"),
        "APP-05": ("LA", "P-MA", "university"),
        "APP-06": ("VN", "P-MA", "university"),
    }
    owners = {}
    for app_id, (country, program, channel) in data.items():
        owners[app_id] = svc.register_application(
            app_id,
            country=country,
            program_id=program,
            channel=channel,
            materials=dict(MATERIALS),
            sensitive={"id_number": f"SECRET-{app_id}", "channel_uid": f"u-{app_id}"},
        )
    return owners


REVIEWERS = ("R-01", "R-02", "R-03")


DEFAULT_PLANS = {
    "APP-01": {"R-01": score(95, 90, 92), "R-02": score(94, 91, 90), "R-03": score(96, 89, 93)},
    "APP-02": {"R-01": score(88, 85, 80), "R-02": score(87, 86, 82), "R-03": score(89, 84, 81)},
    "APP-03": {"R-01": score(80, 78, 85), "R-02": score(82, 76, 84), "R-03": score(81, 79, 83)},
    "APP-04": {"R-01": score(75, 80, 70), "R-02": score(76, 79, 72), "R-03": score(74, 81, 71)},
    "APP-05": {"R-01": score(70, 72, 80), "R-02": score(71, 73, 79), "R-03": score(69, 74, 81)},
    "APP-06": {"R-01": score(60, 65, 68), "R-02": score(62, 64, 67), "R-03": score(61, 66, 66)},
}


def setup_scored_round(
    svc: ScholarshipGovernanceService,
    round_id: str = "ROUND-1",
    revision: int = 1,
    rulebook: RuleBook | None = None,
    plans: dict | None = None,
) -> None:
    """标准场景：6 申请人、3 评委、全部评分完成。"""
    svc.publish_rulebook(rulebook or build_rulebook(revision))
    seed_applicants(svc)
    for reviewer in REVIEWERS:
        svc.register_reviewer(reviewer)
    svc.open_round(round_id, revision)
    plans = plans or DEFAULT_PLANS
    for app_id, by_reviewer in plans.items():
        for reviewer, scores in by_reviewer.items():
            svc.submit_score(round_id, reviewer, app_id, scores)
    svc.close_scoring(round_id)
