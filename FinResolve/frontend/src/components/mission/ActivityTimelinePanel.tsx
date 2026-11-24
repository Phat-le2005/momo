"use client";

import { Panel, StatusPill } from "@/components/StatusPill";
import type { SupervisorActivityItem, SupervisorJobSummary } from "@/lib/api";

const ACTIVITY_KIND_BADGE: Record<string, { label: string; color: string }> = {
  EVENT_RECEIVED: { label: "Tiếp nhận sự kiện", color: "bg-amber-100 text-amber-900 border-amber-300" },
  CASE_CREATED: { label: "Tạo hồ sơ", color: "bg-slate-100 text-slate-800 border-slate-300" },
  JOB_QUEUED: { label: "Xếp hàng đợi", color: "bg-amber-100 text-amber-900 border-amber-300" },
  JOB_CLAIMED: { label: "Worker nhận việc", color: "bg-indigo-100 text-indigo-900 border-indigo-300" },
  INVESTIGATION_STARTED: { label: "Bắt đầu điều tra", color: "bg-indigo-100 text-indigo-900 border-indigo-300" },
  LLM_CALL_STARTED: { label: "Gọi LLM", color: "bg-violet-100 text-violet-900 border-violet-300" },
  LLM_CALL_SUCCEEDED: { label: "LLM phản hồi", color: "bg-emerald-100 text-emerald-900 border-emerald-300" },
  LLM_CALL_FAILED: { label: "LLM lỗi", color: "bg-rose-100 text-rose-900 border-rose-300" },
  BUDGET_EXHAUSTED: { label: "Hết ngân sách", color: "bg-rose-100 text-rose-900 border-rose-300" },
  TOOL_INVOKED: { label: "Gọi công cụ đọc", color: "bg-sky-100 text-sky-900 border-sky-300" },
  TOOL_RESULT_RECEIVED: { label: "Kết quả công cụ", color: "bg-sky-50 text-sky-900 border-sky-200" },
  PLAN_REVISED: { label: "Điều chỉnh kế hoạch", color: "bg-violet-50 text-violet-900 border-violet-200" },
  TASKFORCE_ACTIVATED: { label: "Taskforce", color: "bg-indigo-100 text-indigo-900 border-indigo-300" },
  RISK_EVALUATED: { label: "Đánh giá rủi ro", color: "bg-rose-100 text-rose-900 border-rose-300" },
  ACTION_PROPOSED: { label: "Đề xuất hành động", color: "bg-indigo-100 text-indigo-900 border-indigo-300" },
  WAITING_APPROVAL: { label: "Phê duyệt", color: "bg-amber-100 text-amber-900 border-amber-300" },
  ACTION_EXECUTED: { label: "Đã thực thi", color: "bg-emerald-100 text-emerald-900 border-emerald-300" },
  VERIFICATION_COMPLETED: { label: "Verifier kiểm chứng", color: "bg-emerald-100 text-emerald-900 border-emerald-300" },
  INVESTIGATION_COMPLETED: { label: "Hoàn tất", color: "bg-emerald-100 text-emerald-900 border-emerald-300" },
  INVESTIGATION_FAILED: { label: "Thất bại", color: "bg-rose-100 text-rose-900 border-rose-300" },
  JOB_CANCELLED: { label: "Đã hủy", color: "bg-slate-200 text-slate-800 border-slate-300" },
  WORKER_RECOVERED: { label: "Khôi phục worker", color: "bg-amber-100 text-amber-900 border-amber-300" },
  SUPERVISOR_STATE: { label: "Trạng thái AI", color: "bg-slate-100 text-slate-800 border-slate-300" },
};

function formatTimeVi(iso: string): string {
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

export function ActivityTimelinePanel({
  activities,
  activeJobs,
  busy,
  onCancelJob,
}: {
  activities: SupervisorActivityItem[];
  activeJobs: SupervisorJobSummary[];
  busy: boolean;
  onCancelJob: (jobId: string) => void;
}) {
  const inflight = activeJobs.filter(
    (j) => j.status === "QUEUED" || j.status === "RUNNING" || j.status === "CANCELLING",
  );

  return (
    <Panel
      title="Hoạt động Agent thời gian thực"
      subtitle="Sự kiện bền vững được ghi từng bước trực tiếp trong lúc Worker đang thực thi"
    >
      {inflight.length > 0 && (
        <div className="mb-3 space-y-2 rounded-xl border border-indigo-200 bg-indigo-50/50 p-3">
          <div className="text-xs font-bold text-indigo-950">
            Tác vụ đang trong hàng đợi / đang chạy ({inflight.length})
          </div>
          {inflight.map((job) => (
            <div
              key={job.id}
              className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-indigo-200 bg-white px-2.5 py-2 text-xs"
            >
              <div>
                <span className="font-semibold text-slate-900">{job.job_type}</span>{" "}
                <span className="font-mono text-[11px] text-slate-500">({job.id})</span>
              </div>
              <div className="flex items-center gap-2">
                <StatusPill value={job.status} />
                {job.status !== "CANCELLING" && (
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => onCancelJob(job.id)}
                    className="rounded border border-rose-300 bg-rose-50 px-2 py-0.5 text-[11px] font-semibold text-rose-900 hover:bg-rose-100"
                  >
                    Hủy
                  </button>
                )}
              </div>
            </div>
          ))}
        </div>
      )}

      {activities.length === 0 ? (
        <p className="py-6 text-center text-sm text-slate-500">
          Chưa có hoạt động nào trong phiên này. Hãy chọn một thử thách để xem Worker tự động điều
          tra theo từng bước.
        </p>
      ) : (
        <ol className="max-h-[480px] space-y-2.5 overflow-y-auto pr-1">
          {activities.map((act) => {
            const badge = ACTIVITY_KIND_BADGE[act.kind] ?? {
              label: act.kind,
              color: "bg-slate-100 text-slate-800 border-slate-300",
            };
            return (
              <li
                key={act.id}
                className="rounded-xl border border-slate-200 bg-slate-50/70 p-3 text-xs"
              >
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="font-mono text-[11px] font-semibold text-slate-500">
                    #{act.seq} • {formatTimeVi(act.created_at)}
                  </span>
                  <span
                    className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold ${badge.color}`}
                  >
                    {badge.label}
                  </span>
                </div>
                <div className="mt-1 font-semibold text-slate-900">{act.title_vi}</div>
                {act.detail_vi && <div className="mt-0.5 text-slate-600">{act.detail_vi}</div>}
              </li>
            );
          })}
        </ol>
      )}
    </Panel>
  );
}
