"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { MockBadge } from "@/components/MockBadge";
import { Panel, StatusPill, Timestamp, primaryButton, secondaryButton } from "@/components/StatusPill";
import {
  ApiError,
  apiRequest,
  readableError,
  type CaseEvent,
  type CaseProposal,
  type CaseRecord,
  type CaseStatus,
  type Investigation,
} from "@/lib/api";
import {
  resolveCaseAiModeLabel,
  resolveRestoredInvestigationJob,
  resolveSelectedCaseId,
} from "@/lib/session-state";
import { useDemoSession } from "@/lib/useDemoSession";

const KINDS = ["CLAIM", "OBSERVED_FACT", "HYPOTHESIS", "UNKNOWN"];

export function CasesWorkspace() {
  const { session, loading, error: sessionError, reload } = useDemoSession();
  const activeSessionIdRef = useRef<string | null>(null);
  const [allCases, setAllCases] = useState<CaseRecord[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const selectedIdRef = useRef<string | null>(null);
  const [detail, setDetail] = useState<CaseRecord | null>(null);
  const [events, setEvents] = useState<CaseEvent[]>([]);
  const [jobId, setJobId] = useState<string | null>(null);
  const [job, setJob] = useState<Investigation | null>(null);
  const [busy, setBusy] = useState(false);
  const [approvalAcknowledged, setApprovalAcknowledged] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const sessionId = session?.session_id ?? null;

  const loadCases = useCallback(async () => {
    if (!session || !sessionId) return;
    const reqSessionId = sessionId;
    const list = await apiRequest<CaseRecord[]>("/api/v1/cases", { session });
    if (activeSessionIdRef.current !== reqSessionId) return;
    setAllCases(list);
    const urlCase = new URLSearchParams(window.location.search).get("case");
    const nextId = resolveSelectedCaseId(
      selectedIdRef.current,
      urlCase,
      list.map((c) => c.id),
    );
    selectedIdRef.current = nextId;
    setSelectedId(nextId);
    if (nextId) {
      window.history.replaceState(null, "", `/cases?case=${encodeURIComponent(nextId)}`);
    } else if (urlCase) {
      window.history.replaceState(null, "", "/cases");
    }
  }, [session, sessionId]);

  const loadDetail = useCallback(
    async (caseId: string) => {
      if (!session || !sessionId) return;
      const reqSessionId = sessionId;
      const [caseData, timeline, caseStatus] = await Promise.all([
        apiRequest<CaseRecord>(`/api/v1/cases/${encodeURIComponent(caseId)}`, { session }),
        apiRequest<CaseEvent[]>(`/api/v1/cases/${encodeURIComponent(caseId)}/events`, { session }),
        apiRequest<CaseStatus>(`/api/v1/cases/${encodeURIComponent(caseId)}/status`, { session }),
      ]);
      if (activeSessionIdRef.current === reqSessionId && selectedIdRef.current === caseId) {
        setDetail(caseData);
        setEvents(timeline);
        const restored = resolveRestoredInvestigationJob(caseStatus);
        if (restored.shouldPoll && restored.jobId) {
          setJobId((current) => (current === restored.jobId ? current : restored.jobId));
          setNotice((current) => current || restored.notice);
        } else {
          setJobId((current) => (current ? null : current));
          if (restored.notice) {
            setNotice((current) => current || restored.notice);
          }
        }
      }
    },
    [session, sessionId],
  );

  // Fetch cases for the active session; ignore any stale response from prior sessions
  useEffect(() => {
    activeSessionIdRef.current = sessionId;
    if (!session || !sessionId) return;
    let active = true;
    const reqSessionId = sessionId;
    void apiRequest<CaseRecord[]>("/api/v1/cases", { session })
      .then((list) => {
        if (!active || activeSessionIdRef.current !== reqSessionId) return;
        setAllCases(list);
        const urlCase = new URLSearchParams(window.location.search).get("case");
        const nextId = resolveSelectedCaseId(
          selectedIdRef.current,
          urlCase,
          list.map((c) => c.id),
        );
        selectedIdRef.current = nextId;
        setSelectedId(nextId);
        if (!nextId) {
          setDetail(null);
          setEvents([]);
          if (urlCase) window.history.replaceState(null, "", "/cases");
        }
      })
      .catch((e: unknown) => {
        if (active && activeSessionIdRef.current === reqSessionId) setError(readableError(e));
      });
    return () => {
      active = false;
    };
  }, [session, sessionId]);

  useEffect(() => {
    selectedIdRef.current = selectedId;
    if (!session || !sessionId || !selectedId) return;
    let active = true;
    const reqSessionId = sessionId;
    const refresh = async () => {
      try {
        await loadDetail(selectedId);
      } catch (e) {
        if (!active || activeSessionIdRef.current !== reqSessionId) return;
        if (e instanceof ApiError && e.status === 404) {
          selectedIdRef.current = null;
          setSelectedId(null);
          setDetail(null);
          setEvents([]);
          window.history.replaceState(null, "", "/cases");
        }
        setError(readableError(e));
      }
    };
    void refresh();
    const timer = window.setInterval(() => {
      void refresh();
    }, 4000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [session, sessionId, selectedId, loadDetail]);

  useEffect(() => {
    if (!session || !sessionId || !selectedId || !jobId) return;
    let cancelled = false;
    const reqSessionId = sessionId;
    const poll = async () => {
      try {
        const status = await apiRequest<Investigation>(
          `/api/v1/cases/${encodeURIComponent(selectedId)}/investigations/${encodeURIComponent(jobId)}`,
          { session },
        );
        if (cancelled || activeSessionIdRef.current !== reqSessionId) return;
        setJob(status);
        if (status.job_status !== "RUNNING") {
          setJobId(null);
          await Promise.all([loadDetail(selectedId), loadCases()]);
          if (activeSessionIdRef.current === reqSessionId) {
            setNotice(
              status.job_status === "COMPLETED"
                ? "AI/DEMO_RULES đã kết thúc điều tra. Kiểm tra bằng chứng và đề xuất bên dưới."
                : status.job_status === "INTERRUPTED"
                  ? "Phiên điều tra bị gián đoạn (INTERRUPTED) do thay đổi đồng thời hoặc worker dừng đột ngột. Không có hành động tài chính nào được thực thi."
                  : "Điều tra không hoàn tất. Không có hành động tự động nào được xác nhận.",
            );
          }
        }
      } catch (e) {
        if (!cancelled && activeSessionIdRef.current === reqSessionId) {
          setError(readableError(e));
          setJobId(null);
        }
      }
    };
    void poll();
    const timer = window.setInterval(() => {
      void poll();
    }, 2200);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [jobId, session, sessionId, selectedId, loadCases, loadDetail]);

  async function handleNewSession() {
    if (busy) return;
    activeSessionIdRef.current = null;
    selectedIdRef.current = null;
    setAllCases([]);
    setSelectedId(null);
    setDetail(null);
    setEvents([]);
    setJobId(null);
    setJob(null);
    setApprovalAcknowledged(false);
    setError(null);
    setNotice(null);
    window.history.replaceState(null, "", "/cases");
    await reload(true);
  }

  function choose(id: string) {
    selectedIdRef.current = id;
    setDetail(null);
    setEvents([]);
    setJobId(null);
    setJob(null);
    setApprovalAcknowledged(false);
    setSelectedId(id);
    setError(null);
    setNotice(null);
    window.history.replaceState(null, "", `/cases?case=${encodeURIComponent(id)}`);
  }

  async function investigate() {
    if (!session || !selectedId || busy || jobId) return;
    const reqSessionId = session.session_id;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const response = await apiRequest<{ run_id: string }>(
        `/api/v1/cases/${encodeURIComponent(selectedId)}/investigate`,
        {
          session,
          method: "POST",
          body: { max_steps: 6, run_async: true },
        },
      );
      if (activeSessionIdRef.current !== reqSessionId) return;
      setJobId(response.run_id);
      setNotice("Đã đưa yêu cầu điều tra vào hàng đợi. Theo dõi tiến độ dưới đây.");
    } catch (e) {
      if (activeSessionIdRef.current === reqSessionId) setError(readableError(e));
    } finally {
      setBusy(false);
    }
  }

  async function approve(proposal: CaseProposal) {
    if (!session || !selectedId || busy || !approvalAcknowledged) return;
    const reqSessionId = session.session_id;
    setBusy(true);
    setError(null);
    try {
      const response = await apiRequest<{ approval_mode: string }>(
        `/api/v1/actions/${encodeURIComponent(proposal.id)}/simulate-demo-approval`,
        {
          session,
          method: "POST",
          body: { reason: "Người dùng chủ động phê duyệt MOCK để thử nghiệm FinResolve" },
        },
      );
      if (activeSessionIdRef.current !== reqSessionId) return;
      setNotice(`Phê duyệt thử nghiệm: ${response.approval_mode}. Không phải quyền Manager thật.`);
      await Promise.all([loadDetail(selectedId), loadCases()]);
      if (activeSessionIdRef.current === reqSessionId) setApprovalAcknowledged(false);
    } catch (e) {
      if (activeSessionIdRef.current === reqSessionId) setError(readableError(e));
    } finally {
      setBusy(false);
    }
  }

  async function execute(proposal: CaseProposal) {
    if (!session || !selectedId || busy) return;
    if (
      !window.confirm(
        `Thực thi ${proposal.action_type} trong dữ liệu MOCK? Backend sẽ kiểm tra Policy, Approval và Verifier. Chỉ tiếp tục khi bạn đồng ý.`,
      )
    ) {
      return;
    }
    const reqSessionId = session.session_id;
    setBusy(true);
    setError(null);
    try {
      const result = await apiRequest<{
        case_state: string | null;
        verification: { verdict: string } | null;
      }>(`/api/v1/actions/${encodeURIComponent(proposal.id)}/execute`, {
        session,
        method: "POST",
        idempotencyKey: `WEB-EXEC-${proposal.id}`,
        body: {},
      });
      if (activeSessionIdRef.current !== reqSessionId) return;
      setNotice(
        `Trạng thái Case: ${result.case_state || "UNKNOWN"}; Verification: ${result.verification?.verdict || "CHƯA CÓ"}.`,
      );
      await Promise.all([loadDetail(selectedId), loadCases()]);
    } catch (e) {
      if (activeSessionIdRef.current === reqSessionId) setError(readableError(e));
    } finally {
      setBusy(false);
    }
  }

  async function recover(proposal: CaseProposal) {
    if (!session || !selectedId || busy) return;
    const reqSessionId = session.session_id;
    setBusy(true);
    setError(null);
    try {
      const result = await apiRequest<{
        case_state: string | null;
        verification: { verdict: string } | null;
      }>(`/api/v1/actions/${encodeURIComponent(proposal.id)}/verify`, {
        session,
        method: "POST",
      });
      if (activeSessionIdRef.current !== reqSessionId) return;
      setNotice(
        `Read-back: ${result.verification?.verdict || "UNKNOWN"}; Case: ${result.case_state || "UNKNOWN"}.`,
      );
      await Promise.all([loadDetail(selectedId), loadCases()]);
    } catch (e) {
      if (activeSessionIdRef.current === reqSessionId) setError(readableError(e));
    } finally {
      setBusy(false);
    }
  }

  const aiMode = detail ? resolveCaseAiModeLabel(detail.model_mode) : null;
  const planEvents = events.filter((e) => e.type === "AI_PLAN_GENERATED");
  const latestPlan = planEvents.length > 0 ? planEvents[planEvents.length - 1] : null;
  const toolEvents = events.filter((e) => e.type === "TOOL_READ_COMPLETED");
  const latestHypotheses = Array.isArray(latestPlan?.payload_json?.hypotheses)
    ? (latestPlan.payload_json.hypotheses as Array<{
        label?: string;
        supporting_evidence_refs?: string[];
      }>)
    : [];
  const latestNextStep =
    latestPlan?.payload_json?.next_step && typeof latestPlan.payload_json.next_step === "object"
      ? (latestPlan.payload_json.next_step as {
          kind?: string;
          tool_name?: string;
          action_type?: string;
        })
      : null;

  const pauseExplanation: Record<string, string> = {
    INVESTIGATING: "Agent đang thu thập bằng chứng từ các công cụ đọc được phép.",
    WAITING_EVIDENCE:
      "Workflow dừng ở WAITING_EVIDENCE vì thiếu bằng chứng xác thực từ nguồn dữ liệu gốc (không tự đoán đơn hàng hay thanh toán).",
    WAITING_APPROVAL:
      "Workflow dừng ở WAITING_APPROVAL vì mọi hành động tài chính bắt buộc phải có xác nhận phê duyệt trước khi thực thi.",
    UNKNOWN_OUTCOME:
      "Workflow dừng ở UNKNOWN_OUTCOME do cổng thanh toán/công cụ phản hồi timeout hoặc trạng thái chưa xác định — không tự thử lại mù quáng.",
    ESCALATED:
      "Workflow chuyển ESCALATED để vận hành/rủi ro xem xét thêm thay vì tự thực thi.",
    RESOLVED:
      "Case đã được kiểm chứng độc lập (Verification PASSED) sau thực thi.",
  };

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <div className="mb-2">
            <MockBadge compact />
          </div>
          <h1 className="text-3xl font-bold">Case Center</h1>
          <p className="mt-2 text-sm text-slate-600">
            Điều tra, bằng chứng, phê duyệt và dấu vết xác minh theo từng Case.
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            className={secondaryButton}
            onClick={() => void loadCases().catch((e: unknown) => setError(readableError(e)))}
            disabled={!session || busy}
          >
            Làm mới danh sách
          </button>
          <button
            type="button"
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
          Đang khởi tạo phiên khách…
        </p>
      )}
      {sessionError && (
        <p role="alert" className="rounded-lg bg-rose-50 p-3 text-sm text-rose-700">
          {sessionError}{" "}
          <button onClick={() => void handleNewSession()} className="ml-2 underline">
            Thử lại với phiên mới
          </button>
        </p>
      )}
      {error && (
        <p role="alert" className="rounded-lg bg-rose-50 p-3 text-sm text-rose-700">
          {error}
        </p>
      )}
      {notice && (
        <p role="status" className="rounded-lg bg-indigo-50 p-3 text-sm text-indigo-800">
          {notice}
        </p>
      )}
      <div className="grid gap-5 lg:grid-cols-[310px_minmax(0,1fr)]">
        <aside className="space-y-3">
          <div className="flex items-center justify-between">
            <h2 className="font-semibold">Case của phiên ({allCases.length})</h2>
            <Link href="/demo" className="text-xs text-indigo-700 underline">
              Tạo mới
            </Link>
          </div>
          {allCases.length === 0 && (
            <div className="rounded-xl border border-dashed border-slate-300 p-5 text-sm text-slate-500">
              Chưa có Case.{" "}
              <Link href="/demo" className="text-indigo-700 underline">
                Chạy kịch bản hoặc nhập khiếu nại
              </Link>
              .
            </div>
          )}
          <div className="max-h-[650px] space-y-2 overflow-y-auto">
            {allCases.map((c) => (
              <button
                key={c.id}
                type="button"
                onClick={() => choose(c.id)}
                aria-current={c.id === selectedId ? "true" : undefined}
                className={`w-full rounded-xl border p-3 text-left hover:bg-indigo-50 ${
                  c.id === selectedId
                    ? "border-indigo-400 bg-indigo-50"
                    : "border-slate-200 bg-white"
                }`}
              >
                <span className="block break-all font-mono text-xs text-slate-500">{c.id}</span>
                <span className="mt-1 block line-clamp-2 text-sm font-medium">{c.goal}</span>
                <span className="mt-2 inline-block">
                  <StatusPill value={c.state} />
                </span>
              </button>
            ))}
          </div>
        </aside>
        <section className="min-w-0 space-y-5" aria-label="Chi tiết Case">
          {!selectedId && (
            <Panel title="Chọn Case">
              <p className="text-sm text-slate-600">
                Chọn Case ở bên trái để xem dữ liệu thực từ API.
              </p>
            </Panel>
          )}
          {selectedId && !detail && (
            <Panel title="Đang tải Case">
              <p className="text-sm text-slate-600">Truy vấn dữ liệu và lịch sử điều tra…</p>
            </Panel>
          )}
          {detail && detail.id === selectedId && aiMode && (
            <>
              <Panel title="Trạng thái và quyết định">
                <div className="flex flex-wrap items-center gap-2">
                  <code className="break-all text-xs text-slate-500">{detail.id}</code>
                  <StatusPill value={detail.state} />
                  <StatusPill value={aiMode.badge} />
                </div>
                <p className="mt-3 text-sm font-medium">{detail.goal}</p>
                <p className="mt-2 text-xs text-slate-500">
                  Phiên bản {detail.version} · Cập nhật: <Timestamp value={detail.updated_at} />
                </p>
                <p className="mt-3 text-xs text-indigo-700">{aiMode.note}</p>
                {detail.latest_decision_summary && (
                  <p className="mt-4 rounded-lg bg-slate-50 p-3 text-sm text-slate-700">
                    {detail.latest_decision_summary}
                  </p>
                )}
                {detail.unknowns.length > 0 && (
                  <p className="mt-3 text-sm text-amber-800">
                    Điểm chưa biết: {detail.unknowns.join("; ")}
                  </p>
                )}
                {pauseExplanation[detail.state] && (
                  <p className="mt-3 rounded-lg border border-indigo-100 bg-indigo-50/70 p-3 text-xs text-indigo-900">
                    <strong>Trạng thái luồng Agentic:</strong> {pauseExplanation[detail.state]}
                  </p>
                )}
                {(latestPlan || toolEvents.length > 0) && (
                  <div className="mt-4 space-y-3 rounded-xl border border-slate-200 bg-slate-50/70 p-3 text-xs">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <strong className="text-slate-800">
                        Kế hoạch &amp; giả thuyết hiện tại ({planEvents.length} bước lập kế hoạch ·{" "}
                        {toolEvents.length} lượt đọc tool)
                      </strong>
                      {latestNextStep?.kind && (
                        <span className="rounded bg-white px-2 py-0.5 font-mono text-indigo-800 ring-1 ring-slate-200">
                          {latestNextStep.kind}
                          {latestNextStep.tool_name || latestNextStep.action_type
                            ? ` → ${latestNextStep.tool_name || latestNextStep.action_type}`
                            : ""}
                        </span>
                      )}
                    </div>
                    {latestHypotheses.length > 0 && (
                      <div>
                        <p className="font-medium text-slate-700">Giả thuyết dựa trên bằng chứng:</p>
                        <ul className="mt-1 list-disc space-y-1 pl-4 text-slate-600">
                          {latestHypotheses.map((h, idx) => (
                            <li key={idx}>
                              <span className="font-mono text-slate-800">{h.label || "hypothesis"}</span>
                              {Array.isArray(h.supporting_evidence_refs) &&
                                h.supporting_evidence_refs.length > 0 && (
                                  <span className="ml-1 text-slate-500">
                                    (chứng cứ: {h.supporting_evidence_refs.join(", ")})
                                  </span>
                                )}
                            </li>
                          ))}
                        </ul>
                      </div>
                    )}
                    {toolEvents.length > 0 && (
                      <div>
                        <p className="font-medium text-slate-700">Công cụ đọc đã thực thi:</p>
                        <div className="mt-1 flex flex-wrap gap-1.5">
                          {toolEvents.map((te) => (
                            <span
                              key={te.id}
                              className="inline-flex items-center gap-1 rounded border border-slate-200 bg-white px-2 py-0.5 font-mono text-slate-700"
                            >
                              <span>{String(te.payload_json.tool_name || "read_tool")}</span>
                              <span className="text-slate-400">·</span>
                              <span className="text-emerald-700">
                                {String(te.payload_json.status || "OK")}
                              </span>
                            </span>
                          ))}
                        </div>
                      </div>
                    )}
                  </div>
                )}
                <div className="mt-4 flex flex-wrap items-center gap-2">
                  <button
                    className={primaryButton}
                    type="button"
                    onClick={() => void investigate()}
                    disabled={
                      busy || !!jobId || !["NEW", "WAITING_EVIDENCE", "ESCALATED"].includes(detail.state)
                    }
                  >
                    Điều tra với Agent
                  </button>
                  {job && (
                    <>
                      <StatusPill value={job.job_status} />
                      <span className="text-xs text-slate-500">
                        {job.steps_executed} bước · {job.model_mode}
                      </span>
                    </>
                  )}
                  {!!jobId && (
                    <span role="status" className="text-xs text-slate-600">
                      Đang theo dõi run {jobId}…
                    </span>
                  )}
                </div>
                {job?.error_message && (
                  <p className="mt-2 text-sm text-rose-700">{job.error_message}</p>
                )}
              </Panel>
              <Panel title={`Bằng chứng (${detail.evidence.length})`}>
                <div className="space-y-4">
                  {KINDS.map((kind) => {
                    const rows = detail.evidence.filter((e) => e.kind === kind);
                    if (!rows.length) return null;
                    return (
                      <div key={kind}>
                        <div className="mb-2 flex items-center gap-2">
                          <StatusPill value={kind} />
                          <span className="text-xs text-slate-500">{rows.length} bản ghi</span>
                        </div>
                        {rows.map((ev) => (
                          <details
                            key={ev.id}
                            className="mb-2 rounded-lg border border-slate-200 p-3 text-sm"
                          >
                            <summary className="cursor-pointer break-all font-medium">
                              {ev.source} · {ev.source_ref}
                            </summary>
                            <p className="mt-2 text-xs text-slate-500">
                              <Timestamp value={ev.observed_at} /> · {ev.id}
                            </p>
                            <pre className="mt-2 overflow-x-auto whitespace-pre-wrap break-words rounded bg-slate-50 p-2 text-xs">
                              {JSON.stringify(ev.payload_json, null, 2)}
                            </pre>
                          </details>
                        ))}
                      </div>
                    );
                  })}
                  {!detail.evidence.length && (
                    <p className="text-sm text-slate-500">Chưa có bằng chứng.</p>
                  )}
                </div>
              </Panel>
              <Panel title={`Đề xuất xử lý (${detail.action_proposals.length})`}>
                {detail.action_proposals.length === 0 && (
                  <p className="text-sm text-slate-600">
                    Chưa có đề xuất. Agent có thể đang chờ thêm bằng chứng.
                  </p>
                )}
                <div className="space-y-4">
                  {detail.action_proposals.map((p) => (
                    <article key={p.id} className="rounded-xl border border-slate-200 p-4">
                      <div className="flex flex-wrap items-center gap-2">
                        <strong>{p.action_type}</strong>
                        <StatusPill value={p.status} />
                        <span className="text-xs text-slate-500">Rủi ro: {p.risk_level}</span>
                      </div>
                      <p className="mt-1 break-all font-mono text-xs text-slate-500">{p.id}</p>
                      <pre className="mt-3 overflow-x-auto whitespace-pre-wrap break-words rounded-lg bg-slate-50 p-3 text-xs">
                        {JSON.stringify(p.params_json, null, 2)}
                      </pre>
                      {p.status === "PROPOSED" && detail.state === "WAITING_APPROVAL" && (
                        <div className="mt-3 space-y-3 rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm">
                          <label className="flex cursor-pointer items-start gap-2">
                            <input
                              type="checkbox"
                              checked={approvalAcknowledged}
                              onChange={(e) => setApprovalAcknowledged(e.target.checked)}
                            />{" "}
                            <span>
                              Tôi hiểu đây là <strong>SIMULATED_DEMO_APPROVAL (MOCK)</strong>, không
                              phải phê duyệt Manager thật.
                            </span>
                          </label>
                          <button
                            className={primaryButton}
                            type="button"
                            disabled={busy || !approvalAcknowledged}
                            onClick={() => void approve(p)}
                          >
                            Phê duyệt thử nghiệm (MOCK)
                          </button>
                        </div>
                      )}
                      {p.status === "APPROVED" && (
                        <button
                          className={`${primaryButton} mt-3`}
                          type="button"
                          disabled={busy}
                          onClick={() => void execute(p)}
                        >
                          Thực thi qua Policy & Verifier
                        </button>
                      )}
                      {detail.state === "VERIFYING" && (
                        <button
                          className={`${secondaryButton} mt-3`}
                          type="button"
                          disabled={busy}
                          onClick={() => void recover(p)}
                        >
                          Kiểm tra lại sau commit
                        </button>
                      )}
                    </article>
                  ))}
                </div>
                {detail.verifications.map((v) => (
                  <div className="mt-3 rounded-lg bg-slate-50 p-3 text-sm" key={v.id}>
                    <StatusPill value={v.verdict} />
                    <span className="ml-2">
                      Verification · <Timestamp value={v.checked_at} />
                    </span>
                    <pre className="mt-2 overflow-x-auto whitespace-pre-wrap break-words text-xs">
                      {JSON.stringify(v.postconditions_json, null, 2)}
                    </pre>
                  </div>
                ))}
              </Panel>
              <Panel title={`Decision Trace / Audit (${events.length})`}>
                <ol className="space-y-3 border-l-2 border-slate-200 pl-4">
                  {events.map((ev) => (
                    <li key={ev.id} className="relative text-sm">
                      <span className="absolute -left-[23px] top-2 h-2 w-2 rounded-full bg-indigo-600" />
                      <div className="flex flex-wrap items-center gap-2">
                        <strong>{ev.type}</strong>
                        <span className="text-xs text-slate-500">
                          <Timestamp value={ev.created_at} />
                        </span>
                      </div>
                      <details className="mt-1">
                        <summary className="cursor-pointer text-xs text-indigo-700">
                          Chi tiết bằng chứng/audit
                        </summary>
                        <pre className="mt-1 overflow-x-auto whitespace-pre-wrap break-words rounded bg-slate-50 p-2 text-xs">
                          {JSON.stringify(ev.payload_json, null, 2)}
                        </pre>
                      </details>
                    </li>
                  ))}
                  {!events.length && <li className="text-sm text-slate-500">Chưa có sự kiện.</li>}
                </ol>
              </Panel>
            </>
          )}
        </section>
      </div>
    </div>
  );
}
