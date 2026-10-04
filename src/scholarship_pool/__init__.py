"""区域奖学金名额治理领域包。"""
from .applications import ACTIVE, MERGED_DUPLICATE, Application, MaterialVersion
from .engine import (
    ADMITTED,
    REJECTED,
    WAITLISTED,
    AllocationResult,
    Decision,
    DecisionExplanation,
)
from .errors import DomainError, ReplayIntegrityError
from .identity import IdentityRegistry
from .replay import ReplayReport, TraceEntry, replay_round
from .rules import FundPoolRule, RuleSet
from .service import (
    PHASE_OPEN,
    PHASE_SEALED,
    SLOT_CLOSED,
    SLOT_CONFIRMED,
    SLOT_FROZEN,
    SLOT_PENDING,
    GovernanceService,
)

__all__ = [
    "ACTIVE",
    "ADMITTED",
    "MERGED_DUPLICATE",
    "REJECTED",
    "WAITLISTED",
    "AllocationResult",
    "Application",
    "Decision",
    "DecisionExplanation",
    "DomainError",
    "FundPoolRule",
    "GovernanceService",
    "IdentityRegistry",
    "MaterialVersion",
    "PHASE_OPEN",
    "PHASE_SEALED",
    "ReplayIntegrityError",
    "ReplayReport",
    "RuleSet",
    "SLOT_CLOSED",
    "SLOT_CONFIRMED",
    "SLOT_FROZEN",
    "SLOT_PENDING",
    "TraceEntry",
    "replay_round",
]
