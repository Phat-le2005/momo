/** Minimal typed API client. Domain calls are added from Prompt 07 onward. */

export const API_BASE_URL = (process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000").replace(
  /\/+$/,
  "",
);

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

export type ApiErrorBody = {
  error: { code: string; message: string; details?: unknown; request_id?: string | null };
};

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

function isHealthResponse(value: unknown): value is HealthResponse {
  if (typeof value !== "object" || value === null) return false;
  const v = value as Record<string, unknown>;
  return (
    (v.status === "ok" || v.status === "degraded") &&
    typeof v.source_mode === "string" &&
    typeof v.model_mode === "string" &&
    typeof v.database === "object" &&
    v.database !== null
  );
}

export async function fetchHealth(signal?: AbortSignal): Promise<HealthResponse> {
  const res = await fetch(`${API_BASE_URL}/healthz`, { cache: "no-store", signal });
  const body: unknown = await res.json().catch(() => null);
  if (!res.ok) {
    const err = (body as ApiErrorBody | null)?.error;
    throw new ApiError(err?.message ?? `HTTP ${res.status}`, res.status, err?.code ?? "HTTP_ERROR");
  }
  if (!isHealthResponse(body)) {
    throw new ApiError("Phản hồi health không hợp lệ", res.status, "INVALID_RESPONSE");
  }
  return body;
}
