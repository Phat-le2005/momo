"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { MockBadge } from "@/components/MockBadge";
import { Panel, StatusPill, Timestamp, secondaryButton } from "@/components/StatusPill";
import {
  ApiError,
  apiRequest,
  caseUrl,
  readableError,
  type IncidentDetail,
  type IncidentList,
  type OracleReport,
  type ScenarioRun,
} from "@/lib/api";
import {
  clearSessionRunId,
  loadSessionRunId,
  resolveTaskforceModeLabel,
} from "@/lib/session-state";
import { useDemoSession } from "@/lib/useDemoSession";

export function LiveWorkspace() {
  const { session, loading, error: sessionError, reload: reloadSession } = useDemoSession();
  const activeSessionIdRef = useRef<string | null>(null);
  const [run, setRun] = useState<ScenarioRun | null>(null);
  const [report, setReport] = useState<OracleReport | null>(null);
  const [incidents, setIncidents] = useState<IncidentList | null>(null);
  const [incidentId, setIncidentId] = useState<string | null>(null);
  const [incident, setIncident] = useState<IncidentDetail | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const sessionId = session?.session_id ?? null;

  const reload = useCallback(async () => {
    if (!session || !sessionId) return;
    const reqSessionId = sessionId;
    setBusy(true);
    try {
      const list = await apiRequest<IncidentList>("/api/v1/incidents", { session });
      if (activeSessionIdRef.current !== reqSessionId) return;
      setIncidents(list);
      setIncidentId((old) =>
        old && list.items.some((item) => item.id === old) ? old : (list.items[0]?.id ?? null),
      );
      const savedId = loadSessionRunId(window.sessionStorage, reqSessionId);
      if (savedId) {
        try {
          const next = await apiRequest<ScenarioRun>(
            `/api/v1/demo/runs/${encodeURIComponent(savedId)}/status`,
            { session },
          );
          if (activeSessionIdRef.current !== reqSessionId) return;
          setRun(next);
          if (next.status !== "RUNNING") {
            const rep = await apiRequest<OracleReport>(
              `/api/v1/demo/runs/${encodeURIComponent(savedId)}/report`,
              { session },
            );
            if (activeSessionIdRef.current === reqSessionId) setReport(rep);
          }
        } catch (e) {
          if (activeSessionIdRef.current !== reqSessionId) return;
          if (e instanceof ApiError && (e.status === 401 || e.status === 403 || e.status === 404)) {
            clearSessionRunId(window.sessionStorage, reqSessionId);
          }
          setRun(null);
          setReport(null);
        }
      } else {
        setRun(null);
        setReport(null);
      }
      setError(null);
    } catch (e) {
      if (activeSessionIdRef.current === reqSessionId) setError(readableError(e));
    } finally {
      setBusy(false);
    }
  }, [session, sessionId]);

  useEffect(() => {
    activeSessionIdRef.current = sessionId;
    if (!session || !sessionId) return;
    let active = true;
    const reqSessionId = sessionId;
    const savedId = loadSessionRunId(window.sessionStorage, reqSessionId);
    void (async () => {
      try {
        const list = await apiRequest<IncidentList>("/api/v1/incidents", { session });
        if (!active || activeSessionIdRef.current !== reqSessionId) return;
        setIncidents(list);
        setIncidentId(list.items[0]?.id ?? null);
        if (savedId) {
          try {
            const next = await apiRequest<ScenarioRun>(
              `/api/v1/demo/runs/${encodeURIComponent(savedId)}/status`,
              { session },
            );
            if (!active || activeSessionIdRef.current !== reqSessionId) return;
            setRun(next);
            if (next.status !== "RUNNING") {
              const rep = await apiRequest<OracleReport>(
                `/api/v1/demo/runs/${encodeURIComponent(savedId)}/report`,
                { session },
              );
              if (active && activeSessionIdRef.current === reqSessionId) setReport(rep);
            }
          } catch {
            if (active && activeSessionIdRef.current === reqSessionId) {
              setRun(null);
              setReport(null);
              clearSessionRunId(window.sessionStorage, reqSessionId);
            }
          }
        } else {
          setRun(null);
          setReport(null);
        }
        if (active && activeSessionIdRef.current === reqSessionId) setError(null);
      } catch (e) {
        if (active && activeSessionIdRef.current === reqSessionId) setError(readableError(e));
      }
    })();
    return () => {
      active = false;
    };
  }, [session, sessionId]);

  useEffect(() => {
    if (!session || !sessionId || !incidentId) return;
    let cancelled = false;
    const reqSessionId = sessionId;
    void apiRequest<IncidentDetail>(`/api/v1/incidents/${encodeURIComponent(incidentId)}`, {
      session,
    })
      .then((value) => {
        if (!cancelled && activeSessionIdRef.current === reqSessionId) setIncident(value);
      })
      .catch((e: unknown) => {
        if (!cancelled && activeSessionIdRef.current === reqSessionId) setError(readableError(e));
      });
    return () => {
      cancelled = true;
    };
  }, [session, sessionId, incidentId]);

  const runId = run?.run_id;
  const runStatus = run?.status;
  useEffect(() => {
    if (!session || !sessionId || !runId || runStatus !== "RUNNING") return;
    const timer = window.setInterval(() => {
      void reload();
    }, 2500);
    return () => window.clearInterval(timer);
  }, [session, sessionId, runId, runStatus, reload]);

  async function handleNewSession() {
    if (loading || busy) return;
    activeSessionIdRef.current = null;
    clearSessionRunId(window.sessionStorage, sessionId);
    setRun(null);
    setReport(null);
    setIncidents(null);
    setIncidentId(null);
    setIncident(null);
    setError(null);
    await reloadSession(true);
  }

  const tfDisplay = incident
    ? resolveTaskforceModeLabel(incident.taskforce_execution_mode, incident.llm_invoked)
    : null;

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <MockBadge compact />
          <h1 className="mt-3 text-3xl font-bold">Mega Live & Incident Taskforce</h1>
          <p className="mt-2 max-w-3xl text-sm text-slate-600">
            Quan sát sự cố và nhiệm vụ phối hợp trên dữ liệu MOCK. Taskforce chạy thuật toán xác
            định, không tự nhận là LLM Agent.
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button
            className={secondaryButton}
            onClick={() => void reload()}
            disabled={!session || busy}
          >
            {busy ? "Đang tải…" : "Làm mới dữ liệu"}
          </button>
          <button
            className={secondaryButton}
            onClick={() => void handleNewSession()}
            disabled={loading || busy}
          >
            Tạo phiên mới
          </button>
        </div>
      </div>
      {loading && (
        <p role="status" className="text-sm">
          Đang kết nối phiên…
        </p>
      )}
      {sessionError && (
        <p role="alert" className="text-sm text-rose-700">
          {sessionError}
        </p>
      )}
      {error && (
        <p role="alert" className="rounded-lg bg-rose-50 p-3 text-sm text-rose-700">
          {error}
        </p>
      )}
      <Panel title="Lượt chạy gần nhất của phiên hiện tại">
        {!run && (
          <p className="text-sm text-slate-600">
            Chưa có lượt chạy nào cho phiên hiện tại.{" "}
            <Link href="/demo" className="text-indigo-700 underline">
              Chạy kịch bản Mega Live
            </Link>{" "}
            rồi quay lại.
          </p>
        )}
        {run && (
          <div className="space-y-3">
            <div className="flex flex-wrap items-center gap-2">
              <code className="break-all text-xs">{run.run_id}</code>
              <StatusPill value={run.status} />
              <span className="text-sm">
                {run.orders_generated} đơn · {run.case_ids.length} Case · {run.incident_ids.length}{" "}
                Incident
              </span>
            </div>
            {run.metrics && Object.keys(run.metrics).length > 0 && (
              <div className="flex flex-wrap gap-3 rounded-lg bg-slate-50 px-3 py-2 text-xs text-slate-600">
                {typeof run.metrics.healthy_orders_processed_in_batch === "number" && (
                  <span>
                    Đơn hợp lệ (batch):{" "}
                    <strong>{run.metrics.healthy_orders_processed_in_batch}</strong>
                  </span>
                )}
                {typeof run.metrics.llm_calls_for_bulk_orders === "number" && (
                  <span>
                    LLM bulk calls: <strong>{run.metrics.llm_calls_for_bulk_orders}</strong>
                  </span>
                )}
                {typeof run.metrics.taskforce_mode === "string" && (
                  <span>
                    Taskforce: <strong>{run.metrics.taskforce_mode}</strong>
                  </span>
                )}
                {typeof run.metrics.total_run_ms === "number" && (
                  <span>
                    Thời gian chạy: <strong>{run.metrics.total_run_ms} ms</strong>
                  </span>
                )}
              </div>
            )}
            {run.status === "RUNNING" && (
              <progress
                className="w-full"
                value={run.progress_pct}
                max={100}
                aria-label="Tiến độ chạy"
              />
            )}
            {run.error_message && <p className="text-sm text-rose-700">{run.error_message}</p>}
            {report && (
              <div className="grid gap-3 sm:grid-cols-3">
                <div>
                  <p className="text-xs text-slate-500">Safety</p>
                  <StatusPill value={report.safety_verdict} />
                </div>
                <div>
                  <p className="text-xs text-slate-500">Resolution</p>
                  <StatusPill value={report.resolution_status} />
                </div>
                <div>
                  <p className="text-xs text-slate-500">Kết luận</p>
                  <StatusPill value={report.verdict} />
                </div>
                <div className="rounded-lg bg-slate-50 p-3 text-sm">
                  Đơn chưa đồng bộ: <strong>{report.mismatched_count}</strong>
                </div>
                <div className="rounded-lg bg-slate-50 p-3 text-sm">
                  HOLD:{" "}
                  <strong>
                    {report.blocked_risk_count}/{report.expected_blocked_risk_count}
                  </strong>
                </div>
                <div className="rounded-lg bg-slate-50 p-3 text-sm">
                  Case verified:{" "}
                  <strong>
                    {report.verified_resolved_count}/{report.expected_actionable_count}
                  </strong>
                </div>
                {report.unsafe_effect_details.map((item, i) => (
                  <p className="col-span-full text-sm text-rose-700" key={i}>
                    {item}
                  </p>
                ))}
                <p className="col-span-full text-xs text-slate-500">
                  INCOMPLETE không phải thất bại an toàn: một hoặc nhiều Case chưa được xác minh
                  hoàn tất.
                </p>
              </div>
            )}
            {!!run.recommended_investigation_case_ids.length && (
              <p className="text-sm">
                Case ngoại lệ được gợi ý điều tra:{" "}
                {run.recommended_investigation_case_ids.map((id) => (
                  <Link
                    key={id}
                    href={caseUrl(id)}
                    className="ml-2 break-all text-indigo-700 underline"
                  >
                    {id}
                  </Link>
                ))}
              </p>
            )}
          </div>
        )}
      </Panel>
      <div className="grid gap-5 lg:grid-cols-[310px_minmax(0,1fr)]">
        <aside className="space-y-3">
          <h2 className="font-semibold">Incidents ({incidents?.total_count ?? 0})</h2>
          {(incidents?.items || []).map((item) => (
            <button
              key={item.id}
              onClick={() => {
                setIncident(null);
                setIncidentId(item.id);
              }}
              type="button"
              aria-current={incidentId === item.id ? "true" : undefined}
              className={`w-full rounded-xl border p-4 text-left hover:bg-indigo-50 ${
                incidentId === item.id
                  ? "border-indigo-400 bg-indigo-50"
                  : "border-slate-200 bg-white"
              }`}
            >
              <p className="break-all font-mono text-xs text-slate-500">{item.id}</p>
              <p className="mt-1 text-sm font-semibold">{item.title}</p>
              <p className="mt-2 text-xs text-slate-600">
                {item.linked_cases_count} Case · {item.assigned_tasks_count} Task
              </p>
              <div className="mt-2">
                <StatusPill value={item.status} />
              </div>
            </button>
          ))}
          {!incidents?.items.length && (
            <p className="rounded-lg border border-dashed border-slate-300 p-3 text-sm text-slate-500">
              Chưa có Incident ở phiên này.
            </p>
          )}
        </aside>
        <div className="min-w-0 space-y-4">
          {!incidentId && (
            <Panel title="Chi tiết sự cố">
              Chọn Incident bên trái để xem bằng chứng và Taskforce.
            </Panel>
          )}
          {incidentId && (!incident || incident.id !== incidentId) && (
            <Panel title="Đang tải Incident">Đang đọc bản ghi từ backend…</Panel>
          )}
          {incident && incident.id === incidentId && tfDisplay && (
            <>
              <Panel title={incident.title}>
                <div className="flex flex-wrap items-center gap-2">
                  <StatusPill value={incident.status} />
                  <StatusPill value={tfDisplay.badge} />
                </div>
                <p className="mt-2 text-xs text-indigo-700">{tfDisplay.note}</p>
                <p className="mt-3 text-sm text-slate-700">{incident.hypothesis}</p>
                <p className="mt-2 text-xs text-slate-500">
                  Confidence bằng chứng: {Math.round(incident.confidence * 100)}% · Tạo:{" "}
                  <Timestamp value={incident.created_at} /> · LLM Invoked:{" "}
                  {incident.llm_invoked ? "Có" : "Không"}
                </p>
                <details className="mt-3 text-xs">
                  <summary className="cursor-pointer text-indigo-700">Chi tiết correlation</summary>
                  <pre className="mt-2 overflow-x-auto whitespace-pre-wrap break-words rounded bg-slate-50 p-3">
                    {JSON.stringify(incident.shared_factors_json, null, 2)}
                  </pre>
                </details>
              </Panel>
              <Panel title={`Case liên kết (${incident.linked_cases.length})`}>
                <div className="space-y-2">
                  {incident.linked_cases.map((c) => (
                    <div
                      key={c.case_id}
                      className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-slate-200 p-3"
                    >
                      <Link
                        href={caseUrl(c.case_id)}
                        className="break-all text-sm text-indigo-700 underline"
                      >
                        {c.case_id}
                      </Link>
                      <StatusPill value={c.case_state} />
                    </div>
                  ))}
                </div>
                <p className="mt-3 text-xs text-slate-500">
                  Các Case con KHÔNG tự RESOLVED do Incident. Mỗi Case cần Approval & Verifier độc
                  lập.
                </p>
              </Panel>
              <Panel title={`Role Handoffs (${incident.assigned_tasks.length})`}>
                {incident.assigned_tasks.map((t) => {
                  const hj = t.handoff_json || {};
                  const checkedIds = Array.isArray(hj.checked_case_ids) ? hj.checked_case_ids : [];
                  const uncheckedIds = Array.isArray(hj.unchecked_case_ids)
                    ? hj.unchecked_case_ids
                    : [];
                  const contradictions = Array.isArray(hj.contradictions)
                    ? (hj.contradictions as string[])
                    : [];
                  const unknowns = Array.isArray(hj.unknowns) ? (hj.unknowns as string[]) : [];
                  const handoffTo = typeof hj.handoff_to === "string" ? hj.handoff_to : null;
                  const findings = typeof hj.findings_vi === "string" ? hj.findings_vi : null;
                  const replanTriggered = hj.replan_triggered === true;
                  const taskLlm = hj.llm_invoked === true;
                  return (
                    <article key={t.id} className="mb-3 rounded-lg border border-slate-200 p-4">
                      <div className="flex flex-wrap items-center gap-2">
                        <strong>{t.role}</strong>
                        <StatusPill value={t.state} />
                        <StatusPill value={taskLlm ? "LLM_API" : "DEMO_RULES"} />
                        {replanTriggered && <StatusPill value="REPLAN" />}
                        {handoffTo && (
                          <span className="text-xs text-slate-500">
                            Chuyển giao → <strong>{handoffTo}</strong>
                          </span>
                        )}
                      </div>
                      <p className="mt-1 text-sm text-slate-700">{t.objective}</p>
                      {findings && (
                        <p className="mt-2 rounded bg-slate-50 p-2.5 text-sm text-slate-800">
                          {findings}
                        </p>
                      )}
                      <p className="mt-2 text-xs text-slate-500">
                        Đã kiểm tra trực tiếp: <strong>{checkedIds.length}</strong> Case · Chưa kiểm
                        tra: <strong>{uncheckedIds.length}</strong> Case
                      </p>
                      {contradictions.length > 0 && (
                        <p className="mt-1 text-xs text-rose-700">
                          Mâu thuẫn phát hiện: {contradictions.join("; ")}
                        </p>
                      )}
                      {unknowns.length > 0 && (
                        <p className="mt-1 text-xs text-amber-800">
                          Điểm chưa rõ: {unknowns.join("; ")}
                        </p>
                      )}
                      <details className="mt-2 text-sm">
                        <summary className="cursor-pointer text-indigo-700">
                          Handoff chi tiết &amp; bằng chứng
                        </summary>
                        <pre className="mt-2 overflow-x-auto whitespace-pre-wrap break-words rounded bg-slate-50 p-3 text-xs">
                          {JSON.stringify(t.handoff_json, null, 2)}
                        </pre>
                      </details>
                    </article>
                  );
                })}
              </Panel>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
