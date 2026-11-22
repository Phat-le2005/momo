"""Pydantic schemas for Case, Evidence, CaseEvent, and Status."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class CreateCaseRequest(BaseModel):
    text: str = Field(
        ...,
        min_length=3,
        max_length=2000,
        description="Nội dung khiếu nại tiếng Việt từ khách hàng hoặc sự kiện kinh doanh (CLAIM)",
    )
    goal: str | None = Field(
        default=None,
        max_length=512,
        description="Mục tiêu điều tra nếu được chỉ định trước",
    )
    type: str = Field(
        default="PAYMENT_DISPUTE",
        max_length=64,
        description="Loại sự cố (PAYMENT_DISPUTE, REFUND_REQUEST, ORDER_SYNC)",
    )
    idempotency_key: str | None = Field(
        default=None,
        max_length=128,
        description="Khóa chống trùng lặp từ client khi gửi lại request",
    )


class EvidenceResponse(BaseModel):
    id: str
    case_id: str
    kind: str
    source: str
    source_ref: str
    payload_json: dict[str, Any]
    confidence: float | None = None
    observed_at: datetime


class CaseEventResponse(BaseModel):
    id: str
    case_id: str
    type: str
    payload_json: dict[str, Any]
    created_at: datetime


class CaseResponse(BaseModel):
    id: str
    session_id: str
    type: str
    goal: str
    state: str
    version: int
    owner: str
    idempotency_key: str | None = None
    created_at: datetime
    updated_at: datetime
    evidence: list[EvidenceResponse] = []


class CaseStatusResponse(BaseModel):
    case_id: str
    state: str
    version: int
    updated_at: datetime
