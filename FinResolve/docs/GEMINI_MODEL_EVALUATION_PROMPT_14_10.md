# GEMINI MODEL EVALUATION & SELECTION REPORT — PROMPT 14.10

- **Date (UTC)**: 2026-11-27
- **Discovery Endpoint**: `GET https://generativelanguage.googleapis.com/v1beta/models` (live authenticated query; API key redacted)
- **Total `generateContent`-capable models returned**: 49

## 1. Live Candidate Model Evaluation Matrix

Each candidate model was tested directly against `POST https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent` with `responseMimeType: "application/json"`, `temperature: 0.1`, and a FinResolve `Plan` structured JSON prompt requiring strict schema adherence (`case_id`, `goal`, `known_evidence_refs`, `unknowns`, `hypotheses` as objects, `next_step` with `tool_name`, `decision_summary`).

| Model | HTTP Status | Latency (ms) | Valid JSON | Strict `Plan` Schema | Prompt / Output / Total Tokens | Live API Observation |
| --- | :---: | ---: | :---: | :---: | :---: | --- |
| `gemini-3.8-flash` | `503` | 2,021.2 | No | No | — | Transient high-demand `UNAVAILABLE` ("Spikes in demand are usually temporary") |
| `gemini-3.7-flash` | `503` | 19,145.8 | No | No | — | Transient high-demand `UNAVAILABLE` |
| `gemini-3.6-flash` | Timeout | 25,077.7 | No | No | — | Read timeout (>25s) |
| **`gemini-3.5-flash`** | **`200`** | **7,509.0** | **Yes** | **Yes** | **195 / 160 / 559** | **Stable 200 OK, exact schema compliance, reasoning-grade Flash** |
| **`gemini-3.5-flash-lite`** | **`200`** | **1,519.1** | **Yes** | **Yes** | **195 / 208 / 403** | **Fastest stable 3.5-series model, exact schema compliance** |
| **`gemini-3.1-flash-lite`** | **`200`** | **1,193.7** | **Yes** | **Yes** | **195 / 208 / 403** | **Fastest overall (1.19s), exact schema compliance** |
| `gemini-3.1-pro-preview` | `429` | 169.4 | No | No | — | Quota exceeded on active API tier |
| `gemini-2.5-flash` | `404` | 142.2 | No | No | — | Deprecated/unavailable to new users per Google API error |

## 2. Live End-to-End Agent Investigation Comparison

Both `gemini-3.5-flash` and `gemini-3.5-flash-lite` were tested end-to-end inside `BoundedAgentRuntime` against a live MiniStore `D01 PAYMENT_SYNC_MISMATCH` incident:

| Candidate | Live Run ID | Steps Executed | Tool Calls | Tokens Used | Investigation Duration | Outcome |
| --- | --- | :---: | --- | ---: | ---: | --- |
| `gemini-3.5-flash` | `RUN-4D85A9BC7C` | 2 | `get_order`, `get_payment_record` | 8,221 | 69,272.8 ms | `TIME_BUDGET_EXCEEDED` (hit 45s wall-time budget due to ~34s/step latency) |
| **`gemini-3.5-flash-lite`** | **`RUN-D1EA5D1035`** | **3** | **`get_order`, `get_payment_record`** | **9,807** | **8,409.5 ms** | **`ACTION_PROPOSED` → `COMMAND_EXECUTION_VERIFIED` → `CONFIRMED` → `VERIFIED_RECOVERY`** |

## 3. Production Model Selection & Resilience Policy

Based on empirical single-call AND full multi-step live agent verification (zero guesswork):

1. **Selected Primary Model (`MODEL_NAME`)**: `gemini-3.5-flash-lite`
   - Confirmed `200 OK` in `1,519 ms` single-step and **8.41s** full 3-step autonomous investigation + recovery (`RUN-D1EA5D1035`, `PROP-3ECABA8025`, `RCMD-2066366305`, `VERIFIED_RECOVERY`).
   - Avoids deprecated `gemini-2.5-flash` (`404`), quota-blocked `gemini-3.1-pro-preview` (`429`), overloaded `gemini-3.8-flash` / `gemini-3.7-flash` (`503`), and 45s wall-time budget exhaustion observed on `gemini-3.5-flash`.
2. **Selected Verified Fallback Model (`MODEL_FALLBACK_NAME`)**: `gemini-3.1-flash-lite`
   - Confirmed `200 OK` in `1,193.7 ms` with strict `Plan` JSON compliance.
   - Automatically invoked by `LLMModelAdapter` only after bounded transient retries (`MODEL_MAX_RETRIES=2`) on HTTP `429` / `500` / `502` / `503` / `504` or network timeout, while recording exact `model_name`, `retry_count`, and `fallback_reason` in invocation telemetry.
3. **Bounded Transient Retry & Backoff**:
   - Implemented in `FinResolve/backend/app/ai_core/model_adapter.py`: respects `Retry-After` header (capped at 5s), applies exponential backoff with jitter within a strict wall-clock deadline, and never retries non-retryable 4xx validation/auth errors.
4. **Honest Fallback to `DEMO_RULES`**:
   - When `MODEL_API_KEY` is omitted, FinResolve runs in explicit `DEMO_RULES` mode and displays `Chế độ DEMO_RULES` in the UI. Live AI is never fabricated.
