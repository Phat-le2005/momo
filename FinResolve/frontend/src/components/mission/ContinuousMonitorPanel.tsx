"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { Panel, StatusPill, secondaryButton } from "@/components/StatusPill";
import { apiRequest, caseUrl, readableError, type DemoSession } from "@/lib/api";

type MonitoringBinding = {
  id: string;
  merchant_id: string;
  commerce_session_id: string;
  base_url: string;
  is_enabled: boolean;
  connectivity_status: string;
  last_event_cursor: string | null;
  last_heartbeat_at: string | null;
  last_successful_scan_at: string | null;
  last_error: string | null;
  events_observed_count: number;
};

type MonitoringStatus = {
  monitor_enabled_in_config: boolean;
  monitor_state: string;
  connectivity_status: string;
  payment_provider_mode: string;
  vnpay_status: string;
  last_heartbeat_at: string | null;
  last_successful_scan_at: string | null;
  last_error: string | null;
  events_observed_count: number;
  active_anomalies_count: number;
  open_investigations_count: number;
  verified_recoveries_count: number;
  pending_reconciliation_count: number;
  bindings: MonitoringBinding[];
};

type TimelineStage = {
  stage: string;
  completed: boolean;
  timestamp: string | null;
  detail: string | null;
};

type AnomalyItem = {
  id: string;
  detector_code: string;
  anomaly_type: string;
  severity: string;
  lifecycle_state: string;
  merchant_id: string;
  affected_order_id: string | null;
  affected_payment_id: string | null;
  rationale: string;
  observation_count: number;
  linked_case_id: string | null;
  recovery_source: string | null;
  verified_recovered_at: string | null;
  timeline: TimelineStage[];
};

function fmtTime(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleTimeString("vi-VN");
}

export function ContinuousMonitorPanel({ session }: { session: DemoSession | null }) {
  const [status, setStatus] = useState<MonitoringStatus | null>(null);
  const [anomalies, setAnomalies] = useState<AnomalyItem[]>([]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const loadData = useCallback(async (sess: DemoSession, signal?: AbortSignal) => {
    const [st, list] = await Promise.all([
      apiRequest<MonitoringStatus>("/api/v1/monitoring/status", { session: sess, signal }),
      apiRequest<AnomalyItem[]>("/api/v1/monitoring/anomalies?limit=10", { session: sess, signal }),
    ]);
    return { st, list };
  }, []);

  useEffect(() => {
    if (!session) return;
    const ctrl = new AbortController();
    let active = true;

    const refresh = () => {
      loadData(session, ctrl.signal)
        .then(({ st, list }) => {
          if (!active) return;
          setStatus(st);
          setAnomalies(list);
          setErr(null);
        })
        .catch((e: unknown) => {
          if (!active || ctrl.signal.aborted) return;
          setErr(readableError(e));
        });
    };

    refresh();
    const timer = setInterval(refresh, 10000);
    return () => {
      active = false;
      ctrl.abort();
      clearInterval(timer);
    };
  }, [session, loadData]);

  const handleScanNow = async () => {
    if (!session) return;
    setBusy(true);
    setErr(null);
    try {
      await apiRequest("/api/v1/monitoring/scan-now", { session, method: "POST" });
      const { st, list } = await loadData(session);
      setStatus(st);
      setAnomalies(list);
    } catch (e: unknown) {
      setErr(readableError(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Panel
      title="Autonomous Continuous Commerce Monitor"
      subtitle="Giám sát độc lập FinShop LIVE qua HTTP Read Connectors (read-only) • Không truy cập DB trực tiếp"
      right={
        <div className="flex flex-wrap items-center gap-2">
          <StatusPill value={status?.monitor_state ?? "UNCONFIGURED"} />
          <StatusPill value={status?.connectivity_status ?? "UNCONFIGURED"} />
          <span className="rounded-full border border-indigo-200 bg-indigo-50 px-2.5 py-0.5 text-[11px] font-semibold text-indigo-700">
            Payment: {status?.payment_provider_mode ?? "LOCAL_MOCK"}
          </span>
          <span className="rounded-full border border-slate-200 bg-slate-100 px-2.5 py-0.5 text-[11px] font-medium text-slate-600">
            VNPAY: {status?.vnpay_status ?? "SANDBOX_CREDENTIALS_PENDING"}
          </span>
          {session && status && status.bindings.length > 0 && (
            <button
              type="button"
              disabled={busy}
              onClick={handleScanNow}
              className={secondaryButton}
            >
              {busy ? "Đang quét..." : "Quét ngay"}
            </button>
          )}
        </div>
      }
    >
      <div className="space-y-4">
        {err && (
          <div className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">
            {err}
          </div>
        )}

        <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
          <div className="rounded-xl border border-slate-200 bg-slate-50 p-3">
            <div className="text-[11px] text-slate-500">Sự kiện đã quan sát</div>
            <div className="text-lg font-bold text-slate-900">
              {status ? status.events_observed_count : "—"}
            </div>
          </div>
          <div className="rounded-xl border border-slate-200 bg-slate-50 p-3">
            <div className="text-[11px] text-slate-500">Bất thường đang mở</div>
            <div className="text-lg font-bold text-amber-700">
              {status ? status.active_anomalies_count : "—"}
            </div>
          </div>
          <div className="rounded-xl border border-slate-200 bg-slate-50 p-3">
            <div className="text-[11px] text-slate-500">Đang điều tra AI</div>
            <div className="text-lg font-bold text-indigo-700">
              {status ? status.open_investigations_count : "—"}
            </div>
          </div>
          <div className="rounded-xl border border-slate-200 bg-slate-50 p-3">
            <div className="text-[11px] text-slate-500">Chờ đối soát tài chính</div>
            <div className="text-lg font-bold text-rose-700">
              {status ? status.pending_reconciliation_count : "—"}
            </div>
          </div>
          <div className="rounded-xl border border-slate-200 bg-slate-50 p-3">
            <div className="text-[11px] text-slate-500">Đã xác minh phục hồi</div>
            <div className="text-lg font-bold text-emerald-700">
              {status ? status.verified_recoveries_count : "—"}
            </div>
          </div>
        </div>

        <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-slate-500">
          <span>
            Heartbeat cuối:{" "}
            <strong className="text-slate-700">{fmtTime(status?.last_heartbeat_at ?? null)}</strong>{" "}
            • Quét thành công cuối:{" "}
            <strong className="text-slate-700">
              {fmtTime(status?.last_successful_scan_at ?? null)}
            </strong>
          </span>
          {status?.last_error && (
            <span className="text-rose-600">Lỗi kết nối gần nhất: {status.last_error}</span>
          )}
        </div>

        {anomalies.length === 0 ? (
          <div className="rounded-xl border border-dashed border-slate-200 bg-slate-50 p-4 text-center text-xs text-slate-500">
            Chưa phát hiện bất thường nào từ FinShop LIVE trong phiên hiện tại.
          </div>
        ) : (
          <div className="space-y-2.5">
            {anomalies.map((a) => (
              <div
                key={a.id}
                className="space-y-2 rounded-xl border border-slate-200 bg-white p-3 text-xs"
              >
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex flex-wrap items-center gap-2">
                    <StatusPill value={a.detector_code} />
                    <span className="font-semibold text-slate-900">{a.anomaly_type}</span>
                    <StatusPill value={a.lifecycle_state} />
                    {a.recovery_source && (
                      <span className="rounded-full border border-emerald-200 bg-emerald-50 px-2 py-0.5 text-[11px] font-medium text-emerald-700">
                        Recovered: {a.recovery_source}
                      </span>
                    )}
                  </div>
                  {a.linked_case_id && (
                    <Link
                      href={caseUrl(a.linked_case_id)}
                      className="font-semibold text-indigo-700 underline hover:text-indigo-900"
                    >
                      Mở Case {a.linked_case_id} →
                    </Link>
                  )}
                </div>
                <p className="text-slate-600">{a.rationale}</p>
                <div className="flex flex-wrap items-center gap-1.5">
                  {a.timeline.map((step) => (
                    <span
                      key={step.stage}
                      className={`rounded-full border px-2 py-0.5 text-[11px] font-medium ${
                        step.completed
                          ? "border-emerald-200 bg-emerald-50 text-emerald-700"
                          : "border-slate-200 bg-slate-100 text-slate-400"
                      }`}
                    >
                      {step.completed ? "✓ " : "○ "}
                      {step.stage}
                    </span>
                  ))}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </Panel>
  );
}
