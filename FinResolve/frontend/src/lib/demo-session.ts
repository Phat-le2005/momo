import { apiRequest, type DemoSession } from "./api";
import { clearSessionRunId } from "./session-state";

const SESSION_KEY = "finresolve_demo_session_v1";
let inFlight: Promise<DemoSession> | null = null;

/** One guest token per browser tab; no manager capability or model API key is stored. */
export function existingSession(): DemoSession | null {
  if (typeof window === "undefined") return null;
  const raw = window.sessionStorage.getItem(SESSION_KEY);
  if (!raw) return null;
  try {
    const value: unknown = JSON.parse(raw);
    if (value && typeof value === "object") {
      const s = value as DemoSession;
      if (
        typeof s.token === "string" &&
        typeof s.session_id === "string" &&
        Date.parse(s.expires_at) > Date.now() + 10_000
      ) {
        return s;
      }
    }
  } catch {
    /* stale session; create a new guest session */
  }
  window.sessionStorage.removeItem(SESSION_KEY);
  return null;
}

export function forgetSession(): void {
  if (typeof window !== "undefined") {
    const prev = existingSession();
    clearSessionRunId(window.sessionStorage, prev?.session_id ?? null);
    window.sessionStorage.removeItem(SESSION_KEY);
  }
  inFlight = null;
}

export async function getOrCreateSession(): Promise<DemoSession> {
  const saved = existingSession();
  if (saved) return saved;
  if (inFlight) return inFlight;
  inFlight = apiRequest<DemoSession>("/api/v1/demo/sessions", {
    method: "POST",
    body: { ttl_minutes: 60, max_cases: 40, seed_fixtures: false },
  })
    .then((s) => {
      if (typeof window !== "undefined") {
        window.sessionStorage.setItem(SESSION_KEY, JSON.stringify(s));
      }
      return s;
    })
    .finally(() => {
      inFlight = null;
    });
  return inFlight;
}
