"""区域奖学金名额治理领域包。"""
from .allocation import (
    ADMITTED,
    WAITLISTED,
    EXCLUDED,
    AllocationResult,
    CandidateInput,
    Decision,
    Ranked,
    allocate,
)
from .applications import (
    DISQUALIFIED,
    ELIGIBLE,
    SUPERSEDED,
    WITHDRAWN,
    ApplicationRegistry,
)
from .audit import ReplayReport, replay
from .errors import (
    CapacityExceededError,
    ConflictOfInterestError,
    GovernanceError,
    NotFoundError,
    RoundStateError,
    RuleRevisionError,
    SeatHeldError,
    UnresolvedDuplicateError,
    ValidationError,
)
from .identity import IdentityRegistry
from .journal import EventStore
from .review import ReviewBoard, ScoringSession
from .rules import (
    GENERAL_POOL,
    CountryQuota,
    Dimension,
    Earmark,
    ProgramQuota,
    RuleBook,
)
from .service import ScholarshipGovernanceService

__all__ = [
    "ADMITTED",
    "WAITLISTED",
    "EXCLUDED",
    "AllocationResult",
    "CandidateInput",
    "Decision",
    "Ranked",
    "allocate",
    "ApplicationRegistry",
    "DISQUALIFIED",
    "ELIGIBLE",
    "SUPERSEDED",
    "WITHDRAWN",
    "ReplayReport",
    "replay",
    "CapacityExceededError",
    "ConflictOfInterestError",
    "GovernanceError",
    "NotFoundError",
    "RoundStateError",
    "RuleRevisionError",
    "SeatHeldError",
    "UnresolvedDuplicateError",
    "ValidationError",
    "IdentityRegistry",
    "EventStore",
    "ReviewBoard",
    "ScoringSession",
    "GENERAL_POOL",
    "CountryQuota",
    "Dimension",
    "Earmark",
    "ProgramQuota",
    "RuleBook",
    "ScholarshipGovernanceService",
]
