/** Browser-only, typed FinResolve v1 API client. No provider credentials are shipped to clients. */
export const API_BASE_URL = (process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000").replace(/\/+$/, "");
export type JsonObject = Record<string, unknown>;
export type HealthResponse = {
  status: "ok" | "degraded";
  service: string;
  version: string;
  environment: string;
  source_mode: "MOCK" | "MOMO_SANDBOX";
  model_mode: "DEMO_RULES" | "LLM_API";
  database: { status: "ok" | "unavailable" };
  checked_at: string;
};
export type ApiErrorBody = { error?: { code?: string; message?: string; details?: unknown; request_id?: string | null } };
export class ApiError extends Error {
  constructor(message: string, readonly status: number, readonly code: string) {
    super(message);
    this.name = "ApiError";
  }
}
export type DemoSession = {
  session_id: string;
  token: string;
  expires_at: string;
  status: string;
  max_cases: number;
  case_count: number;
};
export type CaseEvidence = {
  id: string; kind: string; source: string; source_ref: string;
  payload_json: JsonObject; observed_at: string; confidence: number | null;
};
export type CaseProposal = {
  id: string; action_type: string; params_json: JsonObject; status: string;
  evidence_refs: string[]; risk_level: string; expected_version: number;
};
export type CaseRisk = {
  id: string; category: string; level: string; response: string; details_json: JsonObject;
};
export type CaseVerification = {
  id: string; verdict: string; postconditions_json: JsonObject; checked_at: string;
};
export type CaseRecord = {
  id: string; session_id: string; type: string; goal: string; state: string;
  version: number; owner: string; created_at: string; updated_at: string;
  evidence: CaseEvidence[]; action_proposals: CaseProposal[];
  risk_signals: CaseRisk[]; verifications: CaseVerification[];
  unknowns: string[]; latest_decision_summary: string | null; model_mode: string | null;
};
export type CaseEvent = {
  id: string; case_id: string; type: string; payload_json: JsonObject; created_at: string;
};
export type Investigation = {
  run_id: string; case_id: string; job_status: "RUNNING" | "COMPLETED" | "FAILED" | "INTERRUPTED";
  model_mode: string; steps_executed: number; stop_reason: string | null;
  decision_summary: string | null; error_message: string | null;
  proposed_action_ids: string[]; risk_signal_ids: string[];
};
export type CaseStatus = {
  case_id: string;
  state: string;
  version: number;
  updated_at: string;
  model_mode: string | null;
  latest_decision_summary: string | null;
  pending_proposals_count: number;
  active_run_id: string | null;
  investigation_status: "IDLE" | "RUNNING" | "COMPLETED" | "FAILED" | "INTERRUPTED";
};
export type ActionApproval = { proposal_id: string; approval_mode: string; decision: string; case_state: string | null };
export type ActionExecution = {
  proposal_id: string; execution_state: string; case_state: string | null;
  verification: { verdict: string; postconditions_json: JsonObject } | null;
  idempotent_replay: boolean;
};
export type ScenarioItem = {
  scenario_id: "payment_sync" | "refund_risk" | "mega_live";
  title_vi: string; description_vi: string; min_order_count: number; max_order_count: number;
  default_order_count: number; source_mode: string;
};
export type ScenarioRun = {
  run_id: string; scenario_id: string; seed: number; status: string;
  progress_pct: number; orders_generated: number; case_ids: string[]; incident_ids: string[];
  recommended_investigation_case_ids: string[]; investigated_case_ids: string[];
  metrics: JsonObject; error_message: string | null;
};
export type OracleReport = {
  run_id: string; status: string; safety_verdict: string; resolution_status: string;
  verdict: string; unsafe_effects_count: number; verified_resolved_count: number;
  expected_actionable_count: number; pending_unverified_count: number; mismatched_count: number;
  blocked_risk_count: number; expected_blocked_risk_count: number;
  correlated_incident_count: number; false_merges_count: number;
  stock_oversell_detected: boolean; duplicate_ipn_checked: boolean;
  duplicate_ipn_deduplicated: boolean; oracle_leak_detected: boolean;
  performance_metrics: JsonObject; unsafe_effect_details: string[]; known_limitations: string[];
};
export type RiskSignal = {
  id: string; case_id: string; category: string; level: string; response: string;
  lifecycle_status: string; rule_code: string | null; explanation_vi: string | null;
  evidence_refs: string[]; source_refs: string[]; created_at: string; details_json: JsonObject;
};
export type RiskCenter = {
  session_id: string; total_count: number; active_hold_action_count: number;
  cleared_count: number; review_count: number; observe_count: number;
  items: RiskSignal[]; rules_catalog: Array<{ rule_code: string; description_vi: string }>;
};
export type IncidentSummary = {
  id: string; title: string; hypothesis: string; status: string;
  confidence: number; linked_cases_count: number; assigned_tasks_count: number;
  evidence_refs: string[]; shared_factors_json: JsonObject; created_at: string;
};
export type IncidentList = {
  session_id: string; model_mode: string; taskforce_execution_mode: string;
  llm_invoked: boolean; total_count: number; items: IncidentSummary[];
};
export type TaskforceTask = {
  id: string; role: string; state: string; objective: string; handoff_json: JsonObject;
  evidence_refs: string[];
};
export type IncidentDetail = IncidentSummary & {
  linked_cases: Array<{ case_id: string; case_state: string; link_reason: string }>;
  assigned_tasks: TaskforceTask[]; rejected_lookalikes: JsonObject[];
  taskforce_execution_mode: string; llm_invoked: boolean;
};

export type SupervisorStatus = {
  session_id: string;
  status: "MONITORING" | "INVESTIGATING" | "WAITING_EVIDENCE" | "WAITING_APPROVAL" | "PAUSED" | "DEGRADED" | "STOPPED";
  status_label_vi: string;
  autopilot_enabled: boolean;
  world_epoch: number;
  active_job_id: string | null;
  active_case_id: string | null;
  configured_model_mode: "DEMO_RULES" | "LLM_API";
  configured_provider: string | null;
  configured_model_name: string | null;
  llm_execution_state:
    | "LLM_API_CONFIGURED"
    | "LLM_CALL_STARTED"
    | "LLM_CALL_SUCCEEDED"
    | "LLM_CALL_FAILED"
    | "DEMO_RULES_FALLBACK";
  llm_execution_label_vi: string;
  last_llm_succeeded_at: string | null;
  last_llm_failed_at: string | null;
  last_llm_error: string | null;
  llm_calls_count: number;
  llm_tokens_used: number;
  llm_budget_calls: number;
  llm_budget_tokens: number;
  queue_depth: number;
  active_cases_count: number;
  total_cases_count: number;
  waiting_approvals_count: number;
  jobs_processed_count: number;
  monitoring_note_vi: string;
  updated_at: string;
};

export type SupervisorActivityItem = {
  id: string;
  seq: number;
  session_id: string;
  world_epoch: number;
  challenge_id: string | null;
  case_id: string | null;
  job_id: string | null;
  run_id: string | null;
  kind: string;
  title_vi: string;
  detail_vi: string | null;
  metadata_json: JsonObject;
  created_at: string;
};

export type ChallengePreset = {
  preset_id: string;
  label_vi: string;
  description_vi: string;
  defaults: JsonObject;
};

export type ChallengeCatalogItem = {
  challenge_type:
    | "SUSPICIOUS_RECEIPT"
    | "TRAFFIC_BURST"
    | "DUPLICATE_REFUND"
    | "BULK_ORDER_SYNC"
    | "PROMPT_INJECTION";
  title_vi: string;
  subtitle_vi: string;
  is_advanced: boolean;
  presets: ChallengePreset[];
};

export type ChallengeRunRecord = {
  id: string;
  session_id: string;
  world_epoch: number;
  challenge_type: string;
  preset_id: string;
  title_vi: string;
  status: string;
  status_label_vi: string;
  input_json: JsonObject;
  generated_refs_json: JsonObject;
  linked_case_ids: string[];
  linked_incident_ids: string[];
  agent_run_ids: string[];
  outcome_json: JsonObject;
  safety_verdict: string;
  source_mode: string;
  created_at: string;
  updated_at: string;
};

export type ApprovalInboxItem = {
  proposal_id: string;
  case_id: string;
  case_goal: string;
  case_state: string;
  action_type: string;
  action_label_vi: string;
  summary_vi: string;
  params_json: JsonObject;
  evidence_refs: string[];
  risk_level: string;
  expected_version: number;
  status: string;
  created_at: string;
};

export type MissionControlOverview = {
  supervisor: SupervisorStatus;
  latest_challenge: ChallengeRunRecord | null;
  recent_challenges: ChallengeRunRecord[];
  activities: SupervisorActivityItem[];
  approval_inbox: ApprovalInboxItem[];
  latest_seq: number;
};

type RequestOptions = {
  session?: DemoSession;
  method?: "GET" | "POST";
  body?: JsonObject;
  signal?: AbortSignal;
  idempotencyKey?: string;
};

export async function apiRequest<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (options.body) headers["Content-Type"] = "application/json";
  if (options.session) headers["X-Demo-Session"] = options.session.token;
  if (options.idempotencyKey) headers["X-Idempotency-Key"] = options.idempotencyKey;
  const response = await fetch(`${API_BASE_URL}${path}`, {
    method: options.method ?? "GET",
    headers,
    body: options.body ? JSON.stringify(options.body) : undefined,
    cache: "no-store",
    signal: options.signal,
  });
  const data: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const error = (data as ApiErrorBody | null)?.error;
    throw new ApiError(error?.message ?? `HTTP ${response.status}`, response.status, error?.code ?? "HTTP_ERROR");
  }
  if (data === null) throw new ApiError("Máy chủ trả dữ liệu không hợp lệ", response.status, "INVALID_RESPONSE");
  return data as T;
}

export async function fetchHealth(signal?: AbortSignal): Promise<HealthResponse> {
  const h = await apiRequest<HealthResponse>("/healthz", { signal });
  if (!h || !["ok", "degraded"].includes(h.status) || !h.database || !h.model_mode) {
    throw new ApiError("Phản hồi health không hợp lệ", 200, "INVALID_RESPONSE");
  }
  return h;
}

export const caseUrl = (caseId: string): string => `/cases?case=${encodeURIComponent(caseId)}`;
export const runKey = "finresolve_last_run_id_v1";
export function readableError(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 401 || err.status === 403) return `${err.message} — phiên có thể đã hết hạn. Hãy tạo phiên mới.`;
    return `${err.message} (${err.code})`;
  }
  return err instanceof Error ? err.message : "Đã xảy ra lỗi không xác định";
}
