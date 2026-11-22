"""Pydantic schemas for DemoSession."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class CreateSessionRequest(BaseModel):
    ttl_minutes: int = Field(default=60, ge=5, le=1440, description="Session TTL in minutes")
    max_cases: int = Field(
        default=20, ge=1, le=100, description="Max cases quota for this guest session"
    )
    seed_fixtures: bool = Field(default=True, description="Seed synthetic MOCK orders/payments")


class DemoSessionResponse(BaseModel):
    session_id: str
    token: str
    status: str
    max_cases: int
    case_count: int
    expires_at: datetime
    created_at: datetime
