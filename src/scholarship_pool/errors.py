"""治理服务的领域异常。"""
from __future__ import annotations


class GovernanceError(Exception):
    """所有治理规则冲突的基类。"""


class ValidationError(GovernanceError):
    """输入不满足格式或前置约束。"""


class NotFoundError(GovernanceError):
    """引用的实体不存在。"""


class RuleRevisionError(GovernanceError):
    """规则版本不可变或引用的修订不存在。"""


class UnresolvedDuplicateError(GovernanceError):
    """开启新一轮前仍存在未合并的重复身份。"""


class ConflictOfInterestError(GovernanceError):
    """评委与申请人存在已申报的利益冲突，必须回避。"""


class RoundStateError(GovernanceError):
    """轮次当前状态不允许该操作（规则修订只能另开新一轮）。"""


class SeatHeldError(GovernanceError):
    """名额处于申诉冻结中，不能确认或释放。"""


class CapacityExceededError(GovernanceError):
    """并发操作将突破资金/名额上限。"""
