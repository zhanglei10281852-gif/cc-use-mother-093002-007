"""确定性名额分配引擎。

引擎不读取时钟、随机数或任何外部状态：相同输入必然产生相同输出。
因此审计人员可以离线重放任意一轮分配，逐条核对录取、落选、候补
与递补所使用的规则、额度与利益冲突处理。
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Mapping, Optional, Sequence, Tuple

from .applications import ACTIVE, Application
from .rules import FundPoolRule, RuleSet
from .scoring import ConflictDeclaration, ScoreEntry, partition_scores

ADMITTED = "ADMITTED"
WAITLISTED = "WAITLISTED"
REJECTED = "REJECTED"


@dataclass(frozen=True)
class DecisionExplanation:
    """一条决策的全部依据：规则版本、成绩、回避、材料、资金与阻塞原因。"""

    rule_revision: int
    reason: str
    aggregate_score: Optional[Fraction]
    valid_judges: Tuple[str, ...]
    recused_judges: Tuple[Tuple[str, str], ...]
    missing_materials: Tuple[str, ...]
    fund_id: Optional[str]
    blockers: Tuple[str, ...]


@dataclass(frozen=True)
class Decision:
    application_id: str
    identity_token: str
    outcome: str
    rank: Optional[int]
    waitlist_position: Optional[int]
    explanation: DecisionExplanation


@dataclass(frozen=True)
class AllocationResult:
    rule_revision: int
    decisions: Tuple[Decision, ...]

    def admitted(self) -> Tuple[Decision, ...]:
        return tuple(d for d in self.decisions if d.outcome == ADMITTED)

    def waitlisted(self) -> Tuple[Decision, ...]:
        return tuple(d for d in self.decisions if d.outcome == WAITLISTED)

    def rejected(self) -> Tuple[Decision, ...]:
        return tuple(d for d in self.decisions if d.outcome == REJECTED)

    def decision_for(self, application_id: str) -> Decision:
        for decision in self.decisions:
            if decision.application_id == application_id:
                return decision
        raise KeyError(f"申请没有决策记录：{application_id}")


def pick_fund(
    funds: Sequence[FundPoolRule],
    remaining_fund: Mapping[str, int],
    country: str,
    program: str,
) -> Optional[FundPoolRule]:
    """在可承受且接受该国别/项目的资金池中，优先选择定向程度最高者。"""
    candidates = [
        fund
        for fund in funds
        if remaining_fund.get(fund.fund_id, 0) >= fund.award_amount
        and fund.accepts(country, program)
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda fund: (-fund.specificity, fund.fund_id))
    return candidates[0]


def slot_blockers(
    application: Application,
    remaining_country: Mapping[str, int],
    remaining_program: Mapping[str, int],
    remaining_fund: Mapping[str, int],
    ruleset: RuleSet,
) -> Tuple[str, ...]:
    """列出当前额度下阻止该申请获得名额的全部约束。"""
    blockers = []
    if (
        application.country in remaining_country
        and remaining_country[application.country] <= 0
    ):
        blockers.append(f"国别名额已满（{application.country}）")
    if (
        application.program in remaining_program
        and remaining_program[application.program] <= 0
    ):
        blockers.append(f"项目名额已满（{application.program}）")
    if pick_fund(ruleset.funds, remaining_fund, application.country, application.program) is None:
        blockers.append("无可用资金：定向资金不匹配或资金池已用尽")
    return tuple(blockers)


def run_allocation(
    *,
    applications: Sequence[Application],
    scores: Mapping[str, Sequence[ScoreEntry]],
    conflicts: Sequence[ConflictDeclaration],
    ruleset: RuleSet,
) -> AllocationResult:
    """对一轮申请执行完整分配，返回可解释的决策序列。"""
    conflicts_by_token = {}
    for declaration in conflicts:
        conflicts_by_token.setdefault(declaration.identity_token, []).append(declaration)

    ranked = []
    rejected = []
    for app in sorted(applications, key=lambda a: (a.submitted_at, a.application_id)):
        entries = list(scores.get(app.application_id, ()))
        valid, recused = partition_scores(
            entries, conflicts_by_token.get(app.identity_token, ())
        )
        valid_judges = tuple(sorted(entry.judge_id for entry in valid))
        recused_judges = tuple(sorted((entry.judge_id, rel) for entry, rel in recused))
        aggregate = (
            Fraction(sum(entry.score for entry in valid), len(valid)) if valid else None
        )
        missing = tuple(
            m for m in ruleset.required_materials if m not in app.verified_material_types()
        )

        def explain(reason: str) -> DecisionExplanation:
            return DecisionExplanation(
                rule_revision=ruleset.revision,
                reason=reason,
                aggregate_score=aggregate,
                valid_judges=valid_judges,
                recused_judges=recused_judges,
                missing_materials=missing,
                fund_id=None,
                blockers=(),
            )

        if app.status != ACTIVE:
            rejected.append(Decision(
                app.application_id, app.identity_token, REJECTED, None, None,
                explain(f"重复报名已合并，保留申请 {app.merged_into}"),
            ))
        elif missing:
            rejected.append(Decision(
                app.application_id, app.identity_token, REJECTED, None, None,
                explain(f"材料缺失或未核验：{'、'.join(missing)}"),
            ))
        elif len(valid) < ruleset.min_valid_scores:
            rejected.append(Decision(
                app.application_id, app.identity_token, REJECTED, None, None,
                explain(f"有效评分不足：需 {ruleset.min_valid_scores} 份，回避后仅余 {len(valid)} 份"),
            ))
        else:
            ranked.append((app, aggregate, valid_judges, recused_judges))

    # 名次：总分降序；并列时先提交者优先，再按申请编号字典序，保证确定性。
    ranked.sort(key=lambda item: (-item[1], item[0].submitted_at, item[0].application_id))

    remaining_country = dict(ruleset.country_quotas)
    remaining_program = dict(ruleset.program_quotas)
    remaining_fund = {fund.fund_id: fund.total_amount for fund in ruleset.funds}

    decisions = []
    waitlist_position = 0
    for rank, (app, aggregate, valid_judges, recused_judges) in enumerate(ranked, 1):
        blockers = slot_blockers(
            app, remaining_country, remaining_program, remaining_fund, ruleset
        )
        if blockers:
            waitlist_position += 1
            decisions.append(Decision(
                app.application_id, app.identity_token, WAITLISTED, rank, waitlist_position,
                DecisionExplanation(
                    rule_revision=ruleset.revision,
                    reason="名额或资金不足，进入候补",
                    aggregate_score=aggregate,
                    valid_judges=valid_judges,
                    recused_judges=recused_judges,
                    missing_materials=(),
                    fund_id=None,
                    blockers=blockers,
                ),
            ))
            continue
        fund = pick_fund(ruleset.funds, remaining_fund, app.country, app.program)
        if app.country in remaining_country:
            remaining_country[app.country] -= 1
        if app.program in remaining_program:
            remaining_program[app.program] -= 1
        remaining_fund[fund.fund_id] -= fund.award_amount
        decisions.append(Decision(
            app.application_id, app.identity_token, ADMITTED, rank, None,
            DecisionExplanation(
                rule_revision=ruleset.revision,
                reason="录取",
                aggregate_score=aggregate,
                valid_judges=valid_judges,
                recused_judges=recused_judges,
                missing_materials=(),
                fund_id=fund.fund_id,
                blockers=(),
            ),
        ))
    decisions.extend(rejected)
    return AllocationResult(ruleset.revision, tuple(decisions))


@dataclass(frozen=True)
class Promotion:
    """一次候补递补：被跳过者及其原因一并记录，保证递补同样可解释。"""

    application_id: str
    fund_id: str
    skipped: Tuple[Tuple[str, str], ...]


def find_promotion(
    *,
    waitlist: Sequence[str],
    applications: Mapping[str, Application],
    remaining_country: Mapping[str, int],
    remaining_program: Mapping[str, int],
    remaining_fund: Mapping[str, int],
    ruleset: RuleSet,
) -> Optional[Promotion]:
    """按候补顺序扫描，把释放出的容量交给第一个满足原规则的候补者。"""
    skipped = []
    for application_id in waitlist:
        app = applications[application_id]
        blockers = slot_blockers(
            app, remaining_country, remaining_program, remaining_fund, ruleset
        )
        if blockers:
            skipped.append((application_id, "；".join(blockers)))
            continue
        fund = pick_fund(ruleset.funds, remaining_fund, app.country, app.program)
        return Promotion(application_id, fund.fund_id, tuple(skipped))
    return None


def _fraction_to_str(value: Optional[Fraction]) -> Optional[str]:
    return None if value is None else f"{value.numerator}/{value.denominator}"


def _fraction_from_str(value: Optional[str]) -> Optional[Fraction]:
    return None if value is None else Fraction(value)


def explanation_to_dict(explanation: DecisionExplanation) -> dict:
    return {
        "rule_revision": explanation.rule_revision,
        "reason": explanation.reason,
        "aggregate_score": _fraction_to_str(explanation.aggregate_score),
        "valid_judges": list(explanation.valid_judges),
        "recused_judges": [list(pair) for pair in explanation.recused_judges],
        "missing_materials": list(explanation.missing_materials),
        "fund_id": explanation.fund_id,
        "blockers": list(explanation.blockers),
    }


def explanation_from_dict(data: Mapping) -> DecisionExplanation:
    return DecisionExplanation(
        rule_revision=int(data["rule_revision"]),
        reason=data["reason"],
        aggregate_score=_fraction_from_str(data.get("aggregate_score")),
        valid_judges=tuple(data["valid_judges"]),
        recused_judges=tuple(tuple(pair) for pair in data["recused_judges"]),
        missing_materials=tuple(data["missing_materials"]),
        fund_id=data.get("fund_id"),
        blockers=tuple(data["blockers"]),
    )


def decision_to_dict(decision: Decision) -> dict:
    return {
        "application_id": decision.application_id,
        "identity_token": decision.identity_token,
        "outcome": decision.outcome,
        "rank": decision.rank,
        "waitlist_position": decision.waitlist_position,
        "explanation": explanation_to_dict(decision.explanation),
    }


def decision_from_dict(data: Mapping) -> Decision:
    return Decision(
        application_id=data["application_id"],
        identity_token=data["identity_token"],
        outcome=data["outcome"],
        rank=data.get("rank"),
        waitlist_position=data.get("waitlist_position"),
        explanation=explanation_from_dict(data["explanation"]),
    )


def result_to_dict(result: AllocationResult) -> dict:
    return {
        "rule_revision": result.rule_revision,
        "decisions": [decision_to_dict(d) for d in result.decisions],
    }


def result_from_dict(data: Mapping) -> AllocationResult:
    return AllocationResult(
        rule_revision=int(data["rule_revision"]),
        decisions=tuple(decision_from_dict(item) for item in data["decisions"]),
    )
