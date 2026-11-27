/** Typed client and pure helpers for the Unified Demo Experience API (/api/v1/experience).
 *
 * Security invariants:
 * - Never sends session tokens in query strings (always uses X-Demo-Session header).
 * - Never fabricates metrics when backend values are null/missing.
 */

import type { GuardianVisualState } from "../components/GuardianBadge";
import { API_BASE_URL, ApiError } from "./api";

export type ExperienceBootstrapResponse = {
  status: "READY" | "DEGRADED" | "PROVISIONING" | "FAILED" | "EXPIRED" | string;
  restored: boolean;
  session: {
    session_id: string;
    token: string;
    status: string;
    expires_at: string;
    world_epoch: number;
    workspace_role?: "CONTROLLER" | "READ_ONLY_PREVIEW" | string;
  };
  sandbox: {
    environment_name: string;
    sandbox_isolation_mode?: string;
    workspace_role?: "CONTROLLER" | "READ_ONLY_PREVIEW" | string;
    merchant_id: string;
    commerce_session_id: string;
    commerce_world_epoch: number;
    binding_id: string | null;
    connectivity_status: "CONNECTED" | "DISCONNECTED" | "UNCONFIGURED" | "READ_ONLY_PREVIEW" | string;
    storefront_url: string;
    vnpay_enabled: boolean;
  };
  guardian: {
    worker_healthy: boolean;
    supervisor_status: string;
    model_mode: string;
    model_provider: string;
    model_name: string;
    autopilot_enabled: boolean;
  };
};

export type ExperienceAnomaly = {
  id: string;
  detector_code: string;
  anomaly_type: string;
  severity: string;
  lifecycle_state: string;
  affected_order_id: string | null;
  affected_payment_id: string | null;
  linked_case_id: string | null;
  rationale: string;
  verification_verdict: string | null;
  recovery_source: string | null;
  first_observed_at: string;
  last_observed_at: string;
  evidence_refs: string[];
};

export type ExperienceApproval = {
  proposal_id: string;
  case_id: string;
  investigation_run_id: string | null;
  action_type: string;
  risk_level: string;
  status: string;
  params: Record<string, unknown>;
  created_at: string;
};

export type ExperienceRecoveryCommand = {
  id: string;
  command: string;
  state: string;
  execution_certainty: string;
  policy_decision: string;
  target_order_id: string | null;
  updated_at: string;
};

export type ExperienceBusinessEvent = {
  event_id: string;
  event_type: string;
  order_id: string | null;
  payment_id: string | null;
  occurred_at: string;
};

export type ExperienceTimelineItem = {
  id: string;
  kind: string;
  label_vi: string;
  detail_vi: string;
  case_id: string | null;
  job_id: string | null;
  timestamp: string;
  technical_trace: Record<string, unknown>;
};

export type ExperienceOverviewResponse = {
  session_id: string;
  world_epoch: number;
  workspace_role?: "CONTROLLER" | "READ_ONLY_PREVIEW" | string;
  sandbox_isolation_mode?: string;
  unresolved_uncertainty_guards?: number;
  updated_at: string;
  guardian: {
    visual_state: GuardianVisualState;
    supervisor_status: string;
    worker_healthy: boolean;
    connectivity_status: "CONNECTED" | "DISCONNECTED" | "UNCONFIGURED" | string;
    model_mode: string;
    model_name: string;
    queued_jobs: number;
    last_scan_at: string | null;
  };
  kpis: {
    orders_observed: number | null;
    payments_observed: number | null;
    active_anomalies: number;
    verified_recoveries: number;
    pending_approvals: number;
    total_anomalies_detected: number;
    total_cases: number;
  };
  business_health: {
    telemetry_available: boolean;
    has_samples: boolean;
    window_id: string | null;
    rps: number | null;
    p95_latency_ms: number | null;
    error_rate: number | null;
    outbox_lag: number | null;
    unsynced_payments: number | null;
    baseline_status: "ESTABLISHED" | "WARMING_UP" | string;
    baseline_message_vi: string;
    baselines: Array<{
      metric_name: string;
      sample_count: number;
      median: number;
      mad: number;
      updated_at: string;
    }>;
  };
  anomalies: ExperienceAnomaly[];
  approvals: ExperienceApproval[];
  recovery_commands: ExperienceRecoveryCommand[];
  recent_business_events: ExperienceBusinessEvent[];
  timeline: ExperienceTimelineItem[];
};

export type ExperienceScanResponse = {
  scanned: boolean;
  connectivity_status: string;
  events_pulled?: number;
  anomalies_detected?: number;
  cases_created?: number;
  recoveries_verified?: number;
  error?: string | null;
};

export type ExperienceResetResponse = {
  session_id: string;
  world_epoch: number;
  binding_id: string | null;
  status: string;
};

export const EXPERIENCE_TOKEN_STORAGE_KEY = "finresolve_experience_session_token_v1";

async function expFetch<T>(
  path: string,
  options: {
    method?: "GET" | "POST";
    token?: string | null;
    operatorToken?: string | null;
    body?: Record<string, unknown>;
    signal?: AbortSignal;
  } = {},
): Promise<T> {
  const headers: Record<string, string> = {
    Accept: "application/json",
  };
  if (options.token && options.token.trim()) {
    headers["X-Demo-Session"] = options.token.trim();
  }
  if (options.operatorToken && options.operatorToken.trim()) {
    headers["X-Operator-Token"] = options.operatorToken.trim();
  }
  if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
  }

  const res = await fetch(`${API_BASE_URL}${path}`, {
    method: options.method ?? "GET",
    headers,
    body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
    cache: "no-store",
    signal: options.signal,
  });

  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    let code = `HTTP_${res.status}`;
    try {
      const errBody = (await res.json()) as {
        error?: { code?: string; message?: string };
        detail?: string;
      };
      if (errBody?.error?.message) msg = errBody.error.message;
      else if (typeof errBody?.detail === "string") msg = errBody.detail;
      if (errBody?.error?.code) code = errBody.error.code;
    } catch {
      // ignore non-JSON error body
    }
    throw new ApiError(msg, res.status, code);
  }

  return (await res.json()) as T;
}

export function bootstrapExperience(
  existingToken?: string | null,
  signal?: AbortSignal,
  operatorToken?: string | null,
): Promise<ExperienceBootstrapResponse> {
  return expFetch<ExperienceBootstrapResponse>("/api/v1/experience/bootstrap", {
    method: "POST",
    token: existingToken ?? undefined,
    operatorToken: operatorToken ?? undefined,
    body: { ttl_minutes: 60 },
    signal,
  });
}

export function getExperienceOverview(
  token: string,
  signal?: AbortSignal,
): Promise<ExperienceOverviewResponse> {
  return expFetch<ExperienceOverviewResponse>("/api/v1/experience/overview", {
    method: "GET",
    token,
    signal,
  });
}

export function triggerExperienceScan(
  token: string,
  signal?: AbortSignal,
): Promise<ExperienceScanResponse> {
  return expFetch<ExperienceScanResponse>("/api/v1/experience/scan", {
    method: "POST",
    token,
    signal,
  });
}

export function resetExperienceSession(
  token: string,
  signal?: AbortSignal,
): Promise<ExperienceResetResponse> {
  return expFetch<ExperienceResetResponse>("/api/v1/experience/reset", {
    method: "POST",
    token,
    signal,
  });
}

export {
  computeAdaptivePollIntervalMs,
  detectOverviewToasts,
  formatMetricOrEmpty,
  formatPercentOrEmpty,
  isStaleResponse,
  translateBusinessEventVi,
  type ExperienceToast,
} from "./experience-utils";
