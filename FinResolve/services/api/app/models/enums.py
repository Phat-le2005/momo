"""Domain enumerations for FinResolve AI."""

from __future__ import annotations

from enum import StrEnum


class SessionStatus(StrEnum):
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"


class CaseState(StrEnum):
    NEW = "NEW"
    INVESTIGATING = "INVESTIGATING"
    WAITING_EVIDENCE = "WAITING_EVIDENCE"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    RESOLVED = "RESOLVED"
    ESCALATED = "ESCALATED"
    FAILED = "FAILED"
    UNKNOWN_OUTCOME = "UNKNOWN_OUTCOME"


class EvidenceKind(StrEnum):
    CLAIM = "CLAIM"
    OBSERVED_FACT = "OBSERVED_FACT"
    HYPOTHESIS = "HYPOTHESIS"
    UNKNOWN = "UNKNOWN"


class RiskCategory(StrEnum):
    DUPLICATE_REFUND = "DUPLICATE_REFUND"
    LIMIT_EXCEEDED = "LIMIT_EXCEEDED"
    UNVERIFIED_PAYMENT = "UNVERIFIED_PAYMENT"
    ANOMALOUS_PATTERN = "ANOMALOUS_PATTERN"
    UNKNOWN = "UNKNOWN"


class RiskLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class RiskResponse(StrEnum):
    OBSERVE = "OBSERVE"
    REVIEW = "REVIEW"
    HOLD_ACTION = "HOLD_ACTION"


class ActionType(StrEnum):
    ORDER_SYNC = "ORDER_SYNC"
    REFUND_EXECUTE = "REFUND_EXECUTE"
    HOLD_ORDER = "HOLD_ORDER"


class ActionProposalStatus(StrEnum):
    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXECUTED = "EXECUTED"
    CANCELLED = "CANCELLED"


class ActionExecutionState(StrEnum):
    PENDING = "PENDING"
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class VerificationVerdict(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    INCONCLUSIVE = "INCONCLUSIVE"


class ApprovalDecision(StrEnum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class OrderStatus(StrEnum):
    PENDING = "PENDING"
    PAID = "PAID"
    CANCELLED = "CANCELLED"
    REFUNDED = "REFUNDED"


class PaymentStatus(StrEnum):
    PENDING = "PENDING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class RefundStatus(StrEnum):
    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
