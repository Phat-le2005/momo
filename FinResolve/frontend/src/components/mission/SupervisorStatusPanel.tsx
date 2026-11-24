"use client";

import { Panel, StatusPill } from "@/components/StatusPill";
import type { SupervisorStatus } from "@/lib/api";
import {
  resolveSupervisorExecutionBadge,
  resolveWorkerHealthDisplay,
} from "@/lib/session-state";

function formatTimeVi(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleTimeString("vi-VN", {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  } catch {
    return iso;
  }
}

export function SupervisorStatusPanel({
  supervisor,
}: {
  supervisor: SupervisorStatus | null;
}) {
  const execBadge = resolveSupervisorExecutionBadge(
    supervisor?.llm_execution_state,
    supervisor?.configured_model_mode,
  );
  const workerHealth = resolveWorkerHealthDisplay(supervisor);

  return (
    <Panel
      title="1. Trạng thái Worker & AI Supervisor"
      subtitle="Phân tách rõ sức khỏe Worker nền thực tế, chế độ giám sát và số liệu gọi LLM tại Model Adapter"
      right={
        <div className="flex flex-wrap items-center gap-2">
          <span
            className={`inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-semibold ${
              workerHealth.tone === "ok"
                ? "border-emerald-300 bg-emerald-50 text-emerald-900"
                : workerHealth.tone === "paused"
                  ? "border-slate-300 bg-slate-100 text-slate-800"
                  : "border-rose-300 bg-rose-50 text-rose-900"
            }`}
            title={workerHealth.detail}
          >
            {workerHealth.badge}
          </span>
          <StatusPill status={supervisor?.status ?? "DEGRADED"} />
          <span
            className={`inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-semibold ${
              execBadge.isLlmConfirmed
                ? "border-emerald-300 bg-emerald-50 text-emerald-900"
                : execBadge.code === "BUDGET_EXHAUSTED"
                  ? "border-rose-300 bg-rose-50 text-rose-900"
                  : "border-slate-300 bg-slate-100 text-slate-800"
            }`}
            title={execBadge.note}
          >
            {execBadge.badge}
          </span>
        </div>
      }
    >
      {!workerHealth.healthy && supervisor?.autopilot_enabled && (
        <div
          role="alert"
          className="mb-3 rounded-xl border border-amber-300 bg-amber-50 px-3.5 py-2.5 text-xs text-amber-950"
        >
          <strong>Worker nền chưa hoạt động:</strong> {workerHealth.detail}
        </div>
      )}

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <div className="rounded-xl border border-slate-200 bg-slate-50/70 p-3.5">
          <div className="text-xs font-medium text-slate-500">Sức khỏe Worker &amp; Giám sát</div>
          <div className="mt-1 text-base font-bold text-slate-900">
            {supervisor?.status_label_vi ?? "Đang kiểm tra…"}
          </div>
          <div className="mt-1 text-xs text-slate-600">
            Heartbeat gần nhất:{" "}
            <span className="font-medium text-slate-800">
              {formatTimeVi(supervisor?.latest_worker_heartbeat_at)}
            </span>
          </div>
        </div>

        <div className="rounded-xl border border-slate-200 bg-slate-50/70 p-3.5">
          <div className="text-xs font-medium text-slate-500">
            Telemetry gọi LLM (thực đo tại Adapter)
          </div>
          <div className="mt-1 text-sm font-bold text-slate-900">
            {supervisor?.configured_provider
              ? `${supervisor.configured_provider} (${supervisor.configured_model_name})`
              : "Luật xác định (DEMO_RULES)"}
          </div>
          <div className="mt-1 text-xs text-slate-600">
            Thử: <strong>{supervisor?.llm_attempts_count ?? 0}</strong>/{supervisor?.llm_budget_calls ?? 30} •
            OK: <strong>{supervisor?.llm_succeeded_count ?? 0}</strong> •
            Lỗi: <strong>{supervisor?.llm_failed_count ?? 0}</strong> •
            Fallback: <strong>{supervisor?.llm_fallback_count ?? 0}</strong>
          </div>
        </div>

        <div className="rounded-xl border border-slate-200 bg-slate-50/70 p-3.5">
          <div className="text-xs font-medium text-slate-500">Hàng đợi &amp; Hồ sơ</div>
          <div className="mt-1 flex items-baseline gap-2">
            <span className="text-lg font-bold text-slate-900">
              {supervisor?.queue_depth ?? 0} đang chờ/chạy
            </span>
            <span className="text-xs text-slate-600">
              • {supervisor?.active_cases_count ?? 0} hồ sơ mở
            </span>
          </div>
          <div className="mt-1 text-xs text-slate-600">
            Đã hoàn tất: {supervisor?.jobs_processed_count ?? 0} tác vụ • Tokens:{" "}
            {supervisor?.llm_tokens_used ?? 0}/{supervisor?.llm_budget_tokens ?? 40000}
          </div>
        </div>

        <div className="rounded-xl border border-amber-200 bg-amber-50/60 p-3.5">
          <div className="text-xs font-medium text-amber-800">Quyết định chờ bạn phê duyệt</div>
          <div className="mt-1 text-xl font-bold text-amber-950">
            {supervisor?.waiting_approvals_count ?? 0} đề xuất
          </div>
          <div className="mt-1 text-xs text-amber-900">
            Sau khi bạn duyệt, backend tự thực thi idempotent &amp; chạy Verifier
          </div>
        </div>
      </div>

      <p className="mt-3 text-xs text-slate-500">
        {supervisor?.monitoring_note_vi ??
          "Giám sát theo sự kiện bởi Worker nền độc lập."}
        {supervisor?.last_llm_error ? ` • Ghi nhận LLM gần nhất: ${supervisor.last_llm_error}` : ""}
      </p>
    </Panel>
  );
}
