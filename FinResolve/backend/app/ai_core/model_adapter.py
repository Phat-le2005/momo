"""Swappable ModelAdapter protocol with real LLM API support and deterministic DEMO_RULES fallback.

Hard Domain Invariants:
1. Environment-configured LLM API support via `MODEL_PROVIDER`, `MODEL_NAME`, `MODEL_API_KEY`,
   `MODEL_TIMEOUT_SECONDS`, and `MODEL_BUDGET_TOKENS`. Never hardcodes model IDs as trusted forever.
2. Honest Fallback: When no API key is configured, `DeterministicDemoAdapter` runs with
   `model_mode=ModelMode.DEMO_RULES` and `is_fallback=True`. Every summary is explicitly
   prefixed with `[DEMO_RULES]` and is NEVER represented as generative AI output.
3. No Default Order Guessing: Never defaults generic complaints without reliable identifiers
   to `ORD-SYNC` or `ORD-REF`. Chooses `ASK_USER` when identifiers are missing or ambiguous.
4. Strict `case_id` validation: `plan.case_id` must match the currently investigated Case.
"""

from __future__ import annotations

import json
import time
from typing import Any, Protocol

import httpx
from pydantic import BaseModel

from app.ai_core.context import ContextBuilder, InvestigationContext
from app.ai_core.schemas import (
    Hypothesis,
    IntentCategory,
    NextStep,
    Plan,
    PlanStepKind,
)
from app.core.config import ModelMode, Settings, get_settings
from app.core.rate_limit import llm_concurrency_guard


class ModelInvocationResult(BaseModel):
    """Validated planner invocation result with provenance and token usage metadata."""

    plan: Plan
    model_mode: ModelMode
    adapter_name: str
    provider: str
    model_name: str
    is_fallback: bool
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int = 0


class StructuredJsonInvocationResult(BaseModel):
    """Validated structured JSON LLM invocation result for Taskforce and Correlation."""

    data: dict[str, Any]
    model_mode: ModelMode
    adapter_name: str
    provider: str
    model_name: str
    is_fallback: bool
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int = 0


class ModelAdapter(Protocol):
    """Protocol implemented by both live LLM adapters and deterministic fallback adapters."""

    @property
    def mode(self) -> ModelMode: ...

    @property
    def adapter_name(self) -> str: ...

    def generate_plan(self, context: InvestigationContext) -> ModelInvocationResult: ...

    def generate_structured_json(
        self, system_prompt: str, user_message: str
    ) -> StructuredJsonInvocationResult | None: ...


class LLMModelAdapter:
    """Real external LLM API adapter supporting OpenAI, Gemini, and Anthropic endpoints."""

    def __init__(
        self,
        settings: Settings | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._provider = self._settings.model_provider.strip().lower()
        self._model_name = self._settings.model_name.strip()
        self._api_key = self._settings.model_api_key.get_secret_value().strip()
        self._timeout = self._settings.model_timeout_seconds
        self._http_client = http_client

        if not self._provider or not self._model_name or not self._api_key:
            raise ValueError(
                "LLMModelAdapter requires MODEL_PROVIDER, MODEL_NAME, and MODEL_API_KEY"
            )

    @property
    def mode(self) -> ModelMode:
        return ModelMode.LLM_API

    @property
    def adapter_name(self) -> str:
        return f"LLMModelAdapter({self._provider}:{self._model_name})"

    def _invoke_provider(
        self,
        system_prompt: str,
        user_message: str,
    ) -> tuple[str, int, int, int]:
        """Single shared external LLM provider boundary protected by `llm_concurrency_guard()`.

        Covers OpenAI-compatible, Gemini, and Anthropic providers for both `generate_plan()`
        and `generate_structured_json()`, ensuring bounded concurrency without double-acquiring
        the semaphore and guaranteed release on success, timeout, or exception.
        """
        with llm_concurrency_guard():
            client = self._http_client or httpx.Client(timeout=self._timeout)
            close_client = self._http_client is None
            try:
                if self._provider in ("gemini", "google", "google_gemini"):
                    return self._call_gemini(client, system_prompt, user_message)
                if self._provider == "anthropic":
                    return self._call_anthropic(client, system_prompt, user_message)
                return self._call_openai_compatible(client, system_prompt, user_message)
            finally:
                if close_client:
                    client.close()

    def generate_plan(self, context: InvestigationContext) -> ModelInvocationResult:
        start_ns = time.perf_counter_ns()
        system_prompt, user_message = ContextBuilder.build_llm_messages(context)
        raw_json_str, p_tok, c_tok, t_tok = self._invoke_provider(system_prompt, user_message)

        parsed_obj = self._extract_json_dict(raw_json_str)
        validated_plan = Plan.model_validate(parsed_obj)

        # Hardening 3: Validate that planner.case_id matches the currently investigated Case
        if validated_plan.case_id != context.case_id:
            raise ValueError(
                f"Planner output case_id '{validated_plan.case_id}' does not match "
                f"currently investigated Case '{context.case_id}'"
            )

        latency_ms = max(0, int((time.perf_counter_ns() - start_ns) / 1_000_000))

        return ModelInvocationResult(
            plan=validated_plan,
            model_mode=ModelMode.LLM_API,
            adapter_name=self.adapter_name,
            provider=self._provider,
            model_name=self._model_name,
            is_fallback=False,
            prompt_tokens=p_tok,
            completion_tokens=c_tok,
            total_tokens=t_tok,
            latency_ms=latency_ms,
        )

    def generate_structured_json(
        self,
        system_prompt: str,
        user_message: str,
    ) -> StructuredJsonInvocationResult:
        """Invoke the configured external LLM and return a parsed JSON object with usage."""
        start_ns = time.perf_counter_ns()
        raw_json_str, p_tok, c_tok, t_tok = self._invoke_provider(system_prompt, user_message)

        parsed_obj = self._extract_json_dict(raw_json_str)
        latency_ms = max(0, int((time.perf_counter_ns() - start_ns) / 1_000_000))
        return StructuredJsonInvocationResult(
            data=parsed_obj,
            model_mode=ModelMode.LLM_API,
            adapter_name=self.adapter_name,
            provider=self._provider,
            model_name=self._model_name,
            is_fallback=False,
            prompt_tokens=p_tok,
            completion_tokens=c_tok,
            total_tokens=t_tok,
            latency_ms=latency_ms,
        )

    def _resolve_openai_url(self) -> str:
        base_urls = {
            "openai": "https://api.openai.com/v1/chat/completions",
            "openrouter": "https://openrouter.ai/api/v1/chat/completions",
            "groq": "https://api.groq.com/openai/v1/chat/completions",
            "deepseek": "https://api.deepseek.com/v1/chat/completions",
        }
        if self._provider.startswith("http://") or self._provider.startswith("https://"):
            return self._provider
        return base_urls.get(self._provider, "https://api.openai.com/v1/chat/completions")

    def _call_openai_compatible(
        self,
        client: httpx.Client,
        system_prompt: str,
        user_message: str,
    ) -> tuple[str, int, int, int]:
        url = self._resolve_openai_url()
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload: dict[str, Any] = {
            "model": self._model_name,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }
        response = client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        body = response.json()

        content = str(body["choices"][0]["message"]["content"])
        usage = body.get("usage") or {}
        p_tok = int(usage.get("prompt_tokens") or 0)
        c_tok = int(usage.get("completion_tokens") or 0)
        t_tok = int(usage.get("total_tokens") or (p_tok + c_tok))
        return content, p_tok, c_tok, t_tok

    def _call_gemini(
        self,
        client: httpx.Client,
        system_prompt: str,
        user_message: str,
    ) -> tuple[str, int, int, int]:
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self._model_name}:generateContent"
        )
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": self._api_key,
        }
        payload: dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_message}]}],
            "generationConfig": {
                "temperature": 0.1,
                "responseMimeType": "application/json",
            },
        }
        response = client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        body = response.json()

        content = str(body["candidates"][0]["content"]["parts"][0]["text"])
        usage = body.get("usageMetadata") or {}
        p_tok = int(usage.get("promptTokenCount") or 0)
        c_tok = int(usage.get("candidatesTokenCount") or 0)
        t_tok = int(usage.get("totalTokenCount") or (p_tok + c_tok))
        return content, p_tok, c_tok, t_tok

    def _call_anthropic(
        self,
        client: httpx.Client,
        system_prompt: str,
        user_message: str,
    ) -> tuple[str, int, int, int]:
        url = "https://api.anthropic.com/v1/messages"
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        payload: dict[str, Any] = {
            "model": self._model_name,
            "max_tokens": 1024,
            "temperature": 0.1,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_message}],
        }
        response = client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        body = response.json()

        content = str(body["content"][0]["text"])
        usage = body.get("usage") or {}
        p_tok = int(usage.get("input_tokens") or 0)
        c_tok = int(usage.get("output_tokens") or 0)
        t_tok = p_tok + c_tok
        return content, p_tok, c_tok, t_tok

    @staticmethod
    def _extract_json_dict(raw_text: str) -> dict[str, Any]:
        cleaned = raw_text.strip()
        if cleaned.startswith("```"):
            lines = cleaned.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            cleaned = "\n".join(lines).strip()
        parsed = json.loads(cleaned)
        if not isinstance(parsed, dict):
            raise ValueError("Planner JSON response must be a JSON object")
        return parsed


class DeterministicDemoAdapter:
    """Honest deterministic fallback planner when no LLM API key is configured.

    Invariants:
    1. Always reports `mode=ModelMode.DEMO_RULES` and `is_fallback=True`, and prefixes every
       `decision_summary` with `[DEMO_RULES]` so it is never mistaken for generative AI.
    2. Never defaults generic complaints without reliable identifiers to `ORD-SYNC` or `ORD-REF`.
       Immediately requests clarification (`ASK_USER`) when target order/transaction is missing
       or ambiguous.
    """

    @property
    def mode(self) -> ModelMode:
        return ModelMode.DEMO_RULES

    @property
    def adapter_name(self) -> str:
        return "DeterministicDemoAdapter"

    def generate_plan(self, context: InvestigationContext) -> ModelInvocationResult:
        start_ns = time.perf_counter_ns()
        plan = self._build_deterministic_plan(context)
        latency_ms = max(0, int((time.perf_counter_ns() - start_ns) / 1_000_000))
        return ModelInvocationResult(
            plan=plan,
            model_mode=ModelMode.DEMO_RULES,
            adapter_name=self.adapter_name,
            provider="DEMO_RULES",
            model_name="deterministic-rule-planner-v1",
            is_fallback=True,
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            latency_ms=latency_ms,
        )

    def generate_structured_json(
        self,
        system_prompt: str,
        user_message: str,
    ) -> StructuredJsonInvocationResult | None:
        """Deterministic fallback never pretends to invoke an external LLM."""
        _ = (system_prompt, user_message)
        return None

    def _build_deterministic_plan(self, context: InvestigationContext) -> Plan:
        called_tools = {obs.tool_name: obs for obs in context.tool_observations}
        u = context.understanding

        # Collect explicit search hints from Vietnamese complaint extraction
        hint_order_id = u.extracted_order_ids[0] if u.extracted_order_ids else None
        hint_pay_ref = u.extracted_payment_refs[0] if u.extracted_payment_refs else None
        hint_phone = u.extracted_phones[0] if u.extracted_phones else None
        hint_amount = u.extracted_amounts_vnd[0] if u.extracted_amounts_vnd else None
        hint_sku = u.extracted_skus[0] if u.extracted_skus else None

        # Hardening 2: Never default generic complaints without reliable identifiers
        # to ORD-SYNC or ORD-REF!
        has_order_lookup_id = bool(hint_order_id or hint_phone)
        has_payment_lookup_id = bool(hint_pay_ref)

        if not has_order_lookup_id and not has_payment_lookup_id:
            # If only an amount was mentioned, we can inspect get_order(amount=...) once
            # so the tool observation records the ambiguity / missing identifier in Evidence,
            # and then ask the user for a reliable identifier.
            if hint_amount and "get_order" not in called_tools:
                return Plan(
                    case_id=context.case_id,
                    goal=context.goal,
                    known_evidence_refs=context.known_evidence_refs,
                    unknowns=["order_id", "provider_transaction_ref"],
                    hypotheses=[
                        Hypothesis(
                            label="missing_unique_order_or_payment_identifier",
                            supporting_evidence_refs=context.known_evidence_refs[:1],
                        )
                    ],
                    next_step=NextStep(
                        kind=PlanStepKind.READ_TOOL,
                        tool_name="get_order",
                        args={"amount": hint_amount},
                    ),
                    decision_summary=(
                        "[DEMO_RULES] Khiếu nại chỉ có số tiền mà thiếu mã đơn hàng/mã giao dịch; "
                        "kiểm tra mức độ trùng lặp đơn hàng trước khi yêu cầu bổ sung định danh."
                    ),
                )

            return Plan(
                case_id=context.case_id,
                goal=context.goal,
                known_evidence_refs=context.known_evidence_refs,
                unknowns=["order_id", "provider_transaction_ref"],
                hypotheses=[
                    Hypothesis(
                        label="insufficient_identifiers_in_customer_claim",
                        supporting_evidence_refs=context.known_evidence_refs,
                    )
                ],
                next_step=NextStep(
                    kind=PlanStepKind.ASK_USER,
                    args={
                        "question": (
                            "Nội dung khiếu nại chưa có mã đơn hàng (order_id), số điện thoại "
                            "hoặc mã giao dịch (provider_ref) đủ để định danh duy nhất. "
                            "Vui lòng cung cấp mã đơn hàng và mã giao dịch thanh toán."
                        )
                    },
                ),
                decision_summary=(
                    "[DEMO_RULES] Thiếu mã định danh đáng tin cậy (order_id / provider_ref / SĐT); "
                    "yêu cầu khách hàng bổ sung thông tin (ASK_USER), không tự suy đoán đơn."
                ),
            )

        # If customer provided provider_ref without order_id/phone, query payment candidates first
        if not has_order_lookup_id and has_payment_lookup_id:
            if "find_payment_candidates" not in called_tools:
                return Plan(
                    case_id=context.case_id,
                    goal=context.goal,
                    known_evidence_refs=context.known_evidence_refs,
                    unknowns=["order_id", "payment_verification_status"],
                    hypotheses=[
                        Hypothesis(
                            label="lookup_order_via_payment_ref",
                            supporting_evidence_refs=context.known_evidence_refs[:1],
                        )
                    ],
                    next_step=NextStep(
                        kind=PlanStepKind.READ_TOOL,
                        tool_name="find_payment_candidates",
                        args={"provider_ref": hint_pay_ref},
                    ),
                    decision_summary=(
                        "[DEMO_RULES] Tra cứu bản ghi thanh toán theo mã giao dịch "
                        f"'{hint_pay_ref}' để xác định đơn hàng liên kết."
                    ),
                )

            pay_obs_first = called_tools["find_payment_candidates"]
            if pay_obs_first.status == "TIMEOUT" or pay_obs_first.data.get("timed_out"):
                return Plan(
                    case_id=context.case_id,
                    goal=context.goal,
                    known_evidence_refs=context.known_evidence_refs,
                    unknowns=["provider_payment_outcome_after_timeout"],
                    hypotheses=[],
                    next_step=NextStep(
                        kind=PlanStepKind.STOP,
                        args={"reason": "PROVIDER_TIMEOUT"},
                    ),
                    decision_summary=(
                        "[DEMO_RULES] Truy vấn cổng thanh toán bị quá thời gian (TIMEOUT). "
                        "Giữ nguyên UNKNOWN_OUTCOME, không tự động ghi hoặc thử lại mù quáng."
                    ),
                )

            cands_first = list(pay_obs_first.data.get("candidates") or [])
            if len(cands_first) != 1 or not cands_first[0].get("order_id"):
                return Plan(
                    case_id=context.case_id,
                    goal=context.goal,
                    known_evidence_refs=context.known_evidence_refs,
                    unknowns=["valid_order_id", "disambiguated_provider_transaction_ref"],
                    hypotheses=[],
                    next_step=NextStep(
                        kind=PlanStepKind.ASK_USER,
                        args={
                            "question": (
                                "Không thể xác định duy nhất đơn hàng từ mã giao dịch đã cung cấp. "
                                "Vui lòng cung cấp chính xác mã đơn hàng (order_id)."
                            )
                        },
                    ),
                    decision_summary=(
                        "[DEMO_RULES] Mã giao dịch không liên kết duy nhất với một đơn hàng; "
                        "yêu cầu khách hàng cung cấp mã đơn hàng (ASK_USER)."
                    ),
                )
            hint_order_id = str(cands_first[0]["order_id"])

        # 1. Step 1: Inspect order using explicit order_id or customer_phone
        if "get_order" not in called_tools:
            order_args: dict[str, Any] = {}
            if hint_order_id:
                order_args["order_id"] = hint_order_id
            if hint_phone:
                order_args["customer_phone"] = hint_phone
            if hint_amount:
                order_args["amount"] = hint_amount

            return Plan(
                case_id=context.case_id,
                goal=context.goal,
                known_evidence_refs=context.known_evidence_refs,
                unknowns=["order_record_state", "payment_provider_match", "policy_constraints"],
                hypotheses=[
                    Hypothesis(
                        label="pending_order_or_refund_dispute",
                        supporting_evidence_refs=context.known_evidence_refs[:1],
                    )
                ],
                next_step=NextStep(
                    kind=PlanStepKind.READ_TOOL,
                    tool_name="get_order",
                    args=order_args,
                ),
                decision_summary=(
                    "[DEMO_RULES] Truy vấn thông tin đơn hàng từ cơ sở dữ liệu nghiệp vụ "
                    "trước khi đối chiếu nguồn thanh toán (không tin cậy trực tiếp nội dung CLAIM)."
                ),
            )

        order_obs = called_tools["get_order"]
        order_is_ambiguous = bool(order_obs.data.get("is_ambiguous"))
        order_requires_id = bool(order_obs.data.get("requires_identifier"))
        order_found = bool(order_obs.data.get("found"))

        # Hardening 2: If order lookup is ambiguous or lacks reliable identifier -> ASK_USER
        if order_is_ambiguous or order_requires_id:
            cand_orders = list(order_obs.data.get("candidate_order_ids") or [])
            return Plan(
                case_id=context.case_id,
                goal=context.goal,
                known_evidence_refs=context.known_evidence_refs,
                unknowns=["unambiguous_order_id"],
                hypotheses=[
                    Hypothesis(
                        label="ambiguous_or_unidentified_target_order",
                        supporting_evidence_refs=[order_obs.evidence_id],
                    )
                ],
                next_step=NextStep(
                    kind=PlanStepKind.ASK_USER,
                    args={
                        "question": (
                            f"Có nhiều đơn hàng hoặc thiếu mã định danh duy nhất "
                            f"({', '.join(cand_orders) if cand_orders else 'chưa rõ mã đơn'}). "
                            "Vui lòng cung cấp chính xác mã đơn hàng (order_id)."
                        ),
                        "candidate_refs": cand_orders,
                    },
                ),
                decision_summary=(
                    "[DEMO_RULES] Đơn hàng mục tiêu bị trùng lặp hoặc chưa đủ định danh duy nhất; "
                    "yêu cầu khách hàng xác nhận chính xác mã đơn hàng (ASK_USER)."
                ),
            )

        if not order_found:
            return Plan(
                case_id=context.case_id,
                goal=context.goal,
                known_evidence_refs=context.known_evidence_refs,
                unknowns=["valid_order_id"],
                hypotheses=[
                    Hypothesis(
                        label="order_not_found_in_session",
                        supporting_evidence_refs=[order_obs.evidence_id],
                    )
                ],
                next_step=NextStep(
                    kind=PlanStepKind.ASK_USER,
                    args={
                        "question": (
                            "Không tìm thấy đơn hàng khớp với mã đã cung cấp. "
                            "Vui lòng kiểm tra và gửi lại mã đơn hàng chính xác."
                        )
                    },
                ),
                decision_summary=(
                    "[DEMO_RULES] Không tìm thấy đơn hàng trong hệ thống; "
                    "chuyển sang WAITING_EVIDENCE để yêu cầu mã đơn hợp lệ."
                ),
            )

        resolved_order_id = str(order_obs.data.get("order_id") or "")
        order_amount = int(order_obs.data.get("amount") or 0)
        order_status = str(order_obs.data.get("status") or "")
        order_payment_status = str(order_obs.data.get("payment_status") or "")
        order_version = int(order_obs.data.get("version") or 1)

        # 2. Step 2: Query payment candidates bound to this resolved order
        if "find_payment_candidates" not in called_tools:
            pay_args: dict[str, Any] = {"order_id": resolved_order_id}
            if hint_pay_ref:
                pay_args["provider_ref"] = hint_pay_ref
            if order_amount > 0:
                pay_args["amount"] = order_amount

            return Plan(
                case_id=context.case_id,
                goal=context.goal,
                known_evidence_refs=context.known_evidence_refs,
                unknowns=["payment_provider_match", "payment_verification_status"],
                hypotheses=[
                    Hypothesis(
                        label="missing_order_sync_or_unverified_payment",
                        supporting_evidence_refs=[order_obs.evidence_id],
                    )
                ],
                next_step=NextStep(
                    kind=PlanStepKind.READ_TOOL,
                    tool_name="find_payment_candidates",
                    args=pay_args,
                ),
                decision_summary=(
                    "[DEMO_RULES] Đã đọc trạng thái đơn hàng; tiếp tục truy vấn "
                    "bản ghi thanh toán liên kết với đơn hàng từ nguồn provider."
                ),
            )

        pay_obs = called_tools["find_payment_candidates"]

        # Check for timeout (QA T04)
        if pay_obs.status == "TIMEOUT" or pay_obs.data.get("timed_out"):
            return Plan(
                case_id=context.case_id,
                goal=context.goal,
                known_evidence_refs=context.known_evidence_refs,
                unknowns=["provider_payment_outcome_after_timeout"],
                hypotheses=[
                    Hypothesis(
                        label="provider_gateway_timeout_unknown",
                        supporting_evidence_refs=[pay_obs.evidence_id],
                    )
                ],
                next_step=NextStep(
                    kind=PlanStepKind.STOP,
                    args={"reason": "PROVIDER_TIMEOUT"},
                ),
                decision_summary=(
                    "[DEMO_RULES] Truy vấn cổng thanh toán bị quá thời gian (TIMEOUT). "
                    "Giữ nguyên UNKNOWN_OUTCOME, không tự động ghi hoặc thử lại mù quáng."
                ),
            )

        candidates: list[dict[str, Any]] = list(pay_obs.data.get("candidates") or [])
        is_ambiguous = bool(pay_obs.data.get("is_ambiguous")) or len(candidates) > 1

        # QA T03: Ambiguous multiple candidate transactions -> ASK_USER
        if is_ambiguous:
            candidate_refs = [str(c.get("provider_ref") or c.get("payment_id")) for c in candidates]
            return Plan(
                case_id=context.case_id,
                goal=context.goal,
                known_evidence_refs=context.known_evidence_refs,
                unknowns=["disambiguated_provider_transaction_ref"],
                hypotheses=[
                    Hypothesis(
                        label="multiple_matching_payment_candidates",
                        supporting_evidence_refs=[pay_obs.evidence_id],
                    )
                ],
                next_step=NextStep(
                    kind=PlanStepKind.ASK_USER,
                    args={
                        "question": (
                            f"Hệ thống ghi nhận {len(candidates)} giao dịch cùng khớp thông tin "
                            f"({', '.join(candidate_refs)}). Vui lòng cung cấp mã giao dịch "
                            "(provider_ref) để xác minh."
                        ),
                        "candidate_refs": candidate_refs,
                    },
                ),
                decision_summary=(
                    f"[DEMO_RULES] Phát hiện {len(candidates)} giao dịch ứng viên trùng khớp; "
                    "yêu cầu khách hàng cung cấp mã tham chiếu, tuyệt đối không chọn ngẫu nhiên."
                ),
            )

        primary_payment = candidates[0] if len(candidates) == 1 else None
        payment_id = str(primary_payment.get("payment_id") or "") if primary_payment else ""
        payment_order_id = str(primary_payment.get("order_id") or "") if primary_payment else ""
        payment_status = str(primary_payment.get("status") or "NONE") if primary_payment else "NONE"
        payment_verified = bool(
            primary_payment
            and primary_payment.get("authoritative_success") is True
            and payment_order_id == resolved_order_id
        )
        payment_amount = int(primary_payment.get("amount") or 0) if primary_payment else 0

        # 3. Step 3: For refund disputes or already refunded orders, check refund history
        is_refund_flow = (
            u.intent == IntentCategory.REFUND_DISPUTE
            or order_status == "REFUNDED"
            or order_payment_status in ("REFUNDED", "PARTIALLY_REFUNDED")
        )
        if is_refund_flow and "get_refund_history" not in called_tools:
            ref_args: dict[str, Any] = {"order_id": resolved_order_id}
            if payment_id:
                ref_args["payment_id"] = payment_id

            return Plan(
                case_id=context.case_id,
                goal=context.goal,
                known_evidence_refs=context.known_evidence_refs,
                unknowns=["refund_ledger_history", "remaining_refundable_balance"],
                hypotheses=[
                    Hypothesis(
                        label="potential_duplicate_or_overlimit_refund",
                        supporting_evidence_refs=[order_obs.evidence_id, pay_obs.evidence_id],
                    )
                ],
                next_step=NextStep(
                    kind=PlanStepKind.READ_TOOL,
                    tool_name="get_refund_history",
                    args=ref_args,
                ),
                decision_summary=(
                    "[DEMO_RULES] Kiểm tra lịch sử hoàn tiền (COMPLETED, PENDING, UNKNOWN) "
                    "và hạn mức còn lại trước khi xem xét yêu cầu hoàn tiền."
                ),
            )

        # If inventory intent, inspect inventory
        if (
            u.intent == IntentCategory.INVENTORY_OR_FULFILLMENT
            and "get_inventory" not in called_tools
        ):
            inv_args: dict[str, Any] = {"order_id": resolved_order_id}
            if hint_sku:
                inv_args["sku"] = hint_sku

            return Plan(
                case_id=context.case_id,
                goal=context.goal,
                known_evidence_refs=context.known_evidence_refs,
                unknowns=["warehouse_stock_availability"],
                hypotheses=[
                    Hypothesis(
                        label="inventory_allocation_check",
                        supporting_evidence_refs=[order_obs.evidence_id],
                    )
                ],
                next_step=NextStep(
                    kind=PlanStepKind.READ_TOOL,
                    tool_name="get_inventory",
                    args=inv_args,
                ),
                decision_summary=(
                    "[DEMO_RULES] Kiểm tra số lượng tồn kho khả dụng (on_hand - reserved) "
                    "của các sản phẩm trong đơn hàng."
                ),
            )

        # 4. Step 4: Read authoritative business policy before any terminal decision
        if "get_policy" not in called_tools:
            return Plan(
                case_id=context.case_id,
                goal=context.goal,
                known_evidence_refs=context.known_evidence_refs,
                unknowns=["applicable_policy_constraints"],
                hypotheses=[
                    Hypothesis(
                        label="verify_policy_compliance",
                        supporting_evidence_refs=[order_obs.evidence_id, pay_obs.evidence_id],
                    )
                ],
                next_step=NextStep(
                    kind=PlanStepKind.READ_TOOL,
                    tool_name="get_policy",
                    args={"category": u.intent.value},
                ),
                decision_summary=(
                    "[DEMO_RULES] Truy vấn quy tắc chính sách FinResolve-Policy-v1.2 "
                    "để kiểm tra điều kiện tiên quyết trước khi đưa ra kết luận hoặc đề xuất."
                ),
            )

        policy_obs = called_tools["get_policy"]

        # 5. Terminal Decision Evaluation
        # 5A: Refund Dispute Evaluation (QA T07, T08, T09, T10, T21)
        if is_refund_flow and "get_refund_history" in called_tools:
            ref_obs = called_tools["get_refund_history"]
            remaining_refundable = int(ref_obs.data.get("remaining_refundable_amount") or 0)
            has_completed = bool(ref_obs.data.get("has_completed_refund"))
            has_pending_or_unknown = bool(ref_obs.data.get("has_pending_or_unknown_refund"))
            requested_refund_amt = (
                hint_amount if (hint_amount and hint_amount > 0) else order_amount
            )

            if not payment_verified:
                return Plan(
                    case_id=context.case_id,
                    goal=context.goal,
                    known_evidence_refs=context.known_evidence_refs,
                    unknowns=["verified_payment_proof"],
                    hypotheses=[
                        Hypothesis(
                            label="refund_requested_on_unverified_payment",
                            supporting_evidence_refs=[pay_obs.evidence_id, ref_obs.evidence_id],
                        )
                    ],
                    next_step=NextStep(
                        kind=PlanStepKind.ASK_USER,
                        args={
                            "question": (
                                "Không thể đề xuất hoàn tiền vì giao dịch gốc chưa được xác nhận "
                                f"thành công (trạng thái hiện tại: {payment_status})."
                            )
                        },
                    ),
                    decision_summary=(
                        "[DEMO_RULES] Từ chối đề xuất hoàn tiền do thanh toán gốc chưa ở "
                        "trạng thái SUCCESS đã xác thực."
                    ),
                )

            if (
                remaining_refundable <= 0
                or has_pending_or_unknown
                or requested_refund_amt > remaining_refundable
            ):
                reason_code = (
                    "DUPLICATE_REFUND"
                    if (has_completed and remaining_refundable <= 0)
                    else "PENDING_REFUND_CONFLICT"
                    if has_pending_or_unknown
                    else "LIMIT_EXCEEDED"
                )
                return Plan(
                    case_id=context.case_id,
                    goal=context.goal,
                    known_evidence_refs=context.known_evidence_refs,
                    unknowns=[],
                    hypotheses=[
                        Hypothesis(
                            label=f"refund_blocked_{reason_code.lower()}",
                            supporting_evidence_refs=[
                                order_obs.evidence_id,
                                pay_obs.evidence_id,
                                ref_obs.evidence_id,
                                policy_obs.evidence_id,
                            ],
                        )
                    ],
                    next_step=NextStep(
                        kind=PlanStepKind.ESCALATE,
                        args={
                            "risk_category": reason_code,
                            "payment_id": payment_id,
                            "order_id": resolved_order_id,
                            "requested_amount": requested_refund_amt,
                            "remaining_refundable_amount": remaining_refundable,
                            "reason": (
                                f"Phát hiện xung đột hoàn tiền ({reason_code}): "
                                f"hạn mức khả dụng = {remaining_refundable} VND, "
                                f"yêu cầu = {requested_refund_amt} VND. "
                                "Tạm giữ hoàn tiền (HOLD_ACTION / REVIEW) theo chính sách."
                            ),
                        },
                    ),
                    decision_summary=(
                        f"[DEMO_RULES] Chặn đề xuất hoàn tiền lặp/vượt hạn mức ({reason_code}): "
                        f"số dư hoàn tiền khả dụng là {remaining_refundable} VND. "
                        "Chuyển sang ESCALATED để kiểm soát rủi ro (không quy kết gian lận)."
                    ),
                )

            # Legitimate refundable balance available -> Propose Refund (requires approval)
            return Plan(
                case_id=context.case_id,
                goal=context.goal,
                known_evidence_refs=context.known_evidence_refs,
                unknowns=[],
                hypotheses=[
                    Hypothesis(
                        label="eligible_refund_within_balance",
                        supporting_evidence_refs=[
                            order_obs.evidence_id,
                            pay_obs.evidence_id,
                            ref_obs.evidence_id,
                            policy_obs.evidence_id,
                        ],
                    )
                ],
                next_step=NextStep(
                    kind=PlanStepKind.PROPOSE_ACTION,
                    action_type="propose_refund",
                    tool_name="propose_refund",
                    args={
                        "payment_id": payment_id,
                        "order_id": resolved_order_id,
                        "amount": requested_refund_amt,
                        "reason": f"Đề xuất hoàn tiền có kiểm chứng cho đơn {resolved_order_id}",
                    },
                ),
                decision_summary=(
                    f"[DEMO_RULES] Thanh toán gốc ({payment_id}) đã xác thực SUCCESS và số dư "
                    f"({remaining_refundable} VND) đủ đáp ứng; tạo đề xuất hoàn tiền "
                    f"{requested_refund_amt} VND chờ FINANCE_LEAD phê duyệt."
                ),
            )

        # 5B: Payment Sync / Unverified Claim Evaluation (QA T01, T02)
        if not payment_verified:
            return Plan(
                case_id=context.case_id,
                goal=context.goal,
                known_evidence_refs=context.known_evidence_refs,
                unknowns=["verified_provider_payment_record"],
                hypotheses=[
                    Hypothesis(
                        label="customer_claim_unverified_by_provider",
                        supporting_evidence_refs=[order_obs.evidence_id, pay_obs.evidence_id],
                    )
                ],
                next_step=NextStep(
                    kind=PlanStepKind.ASK_USER,
                    args={
                        "question": (
                            "Bản ghi từ cổng thanh toán chưa xác nhận giao dịch thành công "
                            f"cho đơn {resolved_order_id} (trạng thái: {payment_status}). "
                            "Vui lòng cung cấp mã giao dịch hợp lệ hoặc chờ đối soát."
                        ),
                        "observed_payment_status": payment_status,
                    },
                ),
                decision_summary=(
                    f"[DEMO_RULES] Khách hàng khai báo đã thanh toán (CLAIM), nhưng bản ghi "
                    f"nhà cung cấp cho đơn {resolved_order_id} có trạng thái '{payment_status}' "
                    "(chưa xác thực SUCCESS). Chuyển sang WAITING_EVIDENCE."
                ),
            )

        # Verified payment bound to this exact order exists; check if order needs sync
        if order_status == "PENDING" or order_payment_status == "UNPAID":
            if payment_amount != order_amount:
                return Plan(
                    case_id=context.case_id,
                    goal=context.goal,
                    known_evidence_refs=context.known_evidence_refs,
                    unknowns=["payment_order_amount_discrepancy"],
                    hypotheses=[
                        Hypothesis(
                            label="amount_mismatch_between_payment_and_order",
                            supporting_evidence_refs=[order_obs.evidence_id, pay_obs.evidence_id],
                        )
                    ],
                    next_step=NextStep(
                        kind=PlanStepKind.ESCALATE,
                        args={
                            "order_id": resolved_order_id,
                            "payment_id": payment_id,
                            "order_amount": order_amount,
                            "payment_amount": payment_amount,
                            "reason": "Số tiền thanh toán không khớp với giá trị đơn hàng.",
                        },
                    ),
                    decision_summary=(
                        f"[DEMO_RULES] Số tiền thanh toán ({payment_amount} VND) lệch với "
                        f"giá trị đơn hàng ({order_amount} VND); chuyển ESCALATED để đối soát."
                    ),
                )

            return Plan(
                case_id=context.case_id,
                goal=context.goal,
                known_evidence_refs=context.known_evidence_refs,
                unknowns=[],
                hypotheses=[
                    Hypothesis(
                        label="dropped_order_payment_sync",
                        supporting_evidence_refs=[
                            order_obs.evidence_id,
                            pay_obs.evidence_id,
                            policy_obs.evidence_id,
                        ],
                    )
                ],
                next_step=NextStep(
                    kind=PlanStepKind.PROPOSE_ACTION,
                    action_type="propose_order_sync",
                    tool_name="propose_order_sync",
                    args={
                        "order_id": resolved_order_id,
                        "payment_id": payment_id,
                        "amount": payment_amount,
                        "expected_order_version": order_version,
                    },
                ),
                decision_summary=(
                    f"[DEMO_RULES] Đã xác minh bản ghi thanh toán {payment_id} (SUCCESS, "
                    f"{payment_amount} VND) liên kết trực tiếp với đơn hàng {resolved_order_id} "
                    "đang PENDING; đề xuất hành động đồng bộ trạng thái đơn (propose_order_sync)."
                ),
            )

        # Default terminal stop if order is already paid and no anomaly remains
        return Plan(
            case_id=context.case_id,
            goal=context.goal,
            known_evidence_refs=context.known_evidence_refs,
            unknowns=[],
            hypotheses=[
                Hypothesis(
                    label="order_already_synchronized",
                    supporting_evidence_refs=[order_obs.evidence_id, pay_obs.evidence_id],
                )
            ],
            next_step=NextStep(
                kind=PlanStepKind.STOP,
                args={"order_id": resolved_order_id, "status": order_status},
            ),
            decision_summary=(
                f"[DEMO_RULES] Đơn hàng {resolved_order_id} đã ở trạng thái {order_status} "
                f"({order_payment_status}); không cần đề xuất ghi dữ liệu."
            ),
        )


def get_model_adapter(settings: Settings | None = None) -> ModelAdapter:
    """Return LLMModelAdapter when API credentials exist, else DeterministicDemoAdapter."""
    cfg = settings or get_settings()
    if cfg.model_mode == ModelMode.LLM_API:
        return LLMModelAdapter(cfg)
    return DeterministicDemoAdapter()
