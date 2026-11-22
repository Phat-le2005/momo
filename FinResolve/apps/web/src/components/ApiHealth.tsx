"use client";

import { useEffect, useState } from "react";
import { API_BASE_URL, fetchHealth, type HealthResponse } from "@/lib/api";

type HealthState =
  | { kind: "loading" }
  | { kind: "ok"; data: HealthResponse }
  | { kind: "error"; message: string };

const POLL_MS = 15_000;
const TIMEOUT_MS = 5_000;

function useApiHealth(): HealthState {
  const [state, setState] = useState<HealthState>({ kind: "loading" });

  useEffect(() => {
    let cancelled = false;
    let controller: AbortController | null = null;

    const check = async () => {
      controller?.abort();
      controller = new AbortController();
      const timer = setTimeout(() => controller?.abort(), TIMEOUT_MS);
      try {
        const data = await fetchHealth(controller.signal);
        if (!cancelled) setState({ kind: "ok", data });
      } catch (e) {
        if (!cancelled) {
          const message = e instanceof Error && e.name !== "AbortError" ? e.message : "Hết thời gian chờ";
          setState({ kind: "error", message });
        }
      } finally {
        clearTimeout(timer);
      }
    };

    void check();
    const interval = setInterval(() => void check(), POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(interval);
      controller?.abort();
    };
  }, []);

  return state;
}

function describe(state: HealthState): { dot: string; label: string } {
  if (state.kind === "loading") return { dot: "bg-slate-400", label: "Đang kiểm tra API…" };
  if (state.kind === "error") return { dot: "bg-red-500", label: "Không kết nối được API" };
  if (state.data.status === "ok") return { dot: "bg-emerald-500", label: "API hoạt động" };
  return { dot: "bg-amber-500", label: "API suy giảm (CSDL không khả dụng)" };
}

/** Compact header indicator. */
export function ApiHealthIndicator() {
  const state = useApiHealth();
  const { dot, label } = describe(state);
  return (
    <div className="flex items-center gap-2 text-xs text-slate-600" role="status" aria-live="polite">
      <span className={`inline-block h-2.5 w-2.5 rounded-full ${dot}`} aria-hidden="true" />
      <span>{label}</span>
    </div>
  );
}

/** Detailed panel for the demo page. */
export function ApiHealthPanel() {
  const state = useApiHealth();
  const { dot, label } = describe(state);

  return (
    <section
      aria-labelledby="health-heading"
      className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm"
    >
      <h2 id="health-heading" className="mb-3 text-base font-semibold">
        Trạng thái hệ thống
      </h2>
      <div className="flex items-center gap-2" role="status" aria-live="polite">
        <span className={`inline-block h-3 w-3 rounded-full ${dot}`} aria-hidden="true" />
        <span className="font-medium">{label}</span>
      </div>
      {state.kind === "ok" && (
        <dl className="mt-3 grid grid-cols-2 gap-x-6 gap-y-1 text-sm sm:grid-cols-4">
          <div>
            <dt className="text-slate-500">Nguồn dữ liệu</dt>
            <dd className="font-mono">{state.data.source_mode}</dd>
          </div>
          <div>
            <dt className="text-slate-500">Chế độ suy luận</dt>
            <dd className="font-mono">
              {state.data.model_mode}
              {state.data.model_mode === "DEMO_RULES" && (
                <span className="ml-1 font-sans text-xs text-slate-500">(luật xác định, không phải AI sinh)</span>
              )}
            </dd>
          </div>
          <div>
            <dt className="text-slate-500">Cơ sở dữ liệu</dt>
            <dd className="font-mono">{state.data.database.status}</dd>
          </div>
          <div>
            <dt className="text-slate-500">Phiên bản API</dt>
            <dd className="font-mono">{state.data.version}</dd>
          </div>
        </dl>
      )}
      {state.kind === "error" && (
        <p className="mt-2 text-sm text-red-700">
          {state.message}. Kiểm tra backend tại <code className="font-mono">{API_BASE_URL}</code>; trang sẽ tự
          thử lại sau {POLL_MS / 1000} giây.
        </p>
      )}
    </section>
  );
}
