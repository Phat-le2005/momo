"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { Panel, StatusPill, primaryButton, secondaryButton } from "@/components/StatusPill";
import {
  apiRequest,
  caseUrl,
  readableError,
  type ActionApproval,
  type ActionExecution,
  type ApprovalInboxItem,
  type CaseRecord,
  type ChallengeCatalogItem,
  type ChallengeRunRecord,
  type JsonObject,
  type MissionControlOverview,
  type SupervisorActivityItem,
  type SupervisorStatus,
} from "@/lib/api";
import {
  clearSessionRunId,
  mergeSupervisorActivities,
  resolveSupervisorExecutionBadge,
} from "@/lib/session-state";
import { useDemoSession } from "@/lib/useDemoSession";

const PRIMARY_CHALLENGES = [
  "SUSPICIOUS_RECEIPT",
  "TRAFFIC_BURST",
  "DUPLICATE_REFUND",
  "BULK_ORDER_SYNC",
] as const;

const TOOL_NAME_VI: Record<string, string> = {
  get_order: "Kiểm tra đơn hàng",
  find_payment_candidates: "Tìm giao dịch khả dĩ",
  get_payment_record: "Đối chiếu sổ cái thanh toán",
  get_refund_history: "Kiểm tra lịch sử hoàn tiền",
  get_inventory: "Kiểm tra tồn kho",
  get_policy: "Tra cứu chính sách",
  get_order_events: "Đọc sự kiện đồng bộ",
};

const ACTIVITY_KIND_BADGE: Record<string, { label: string; color: string }> = {
  EVENT_DETECTED: { label: "Phát hiện sự kiện", color: "bg-amber-100 text-amber-900 border-amber-300" },
  JOB_CLAIMED: { label: "Bắt đầu điều tra", color: "bg-indigo-100 text-indigo-900 border-indigo-300" },
  LLM_CALL_STARTED: { label: "LLM suy luận", color: "bg-violet-100 text-violet-900 border-violet-300" },
  LLM_CALL_SUCCEEDED: { label: "LLM hoàn tất", color: "bg-emerald-100 text-emerald-900 border-emerald-300" },
  LLM_CALL_FAILED: { label: "Chuyển DEMO_RULES", color: "bg-amber-100 text-amber-900 border-amber-300" },
  TOOL_INVOKED: { label: "Đọc dữ liệu", color: "bg-sky-100 text-sky-900 border-sky-300" },
  RISK_EVALUATED: { label: "Đánh giá rủi ro", color: "bg-rose-100 text-rose-900 border-rose-300" },
  PROPOSAL_CREATED: { label: "Đề xuất hành động", color: "bg-indigo-100 text-indigo-900 border-indigo-300" },
  WAITING_APPROVAL: { label: "Phê duyệt", color: "bg-amber-100 text-amber-900 border-amber-300" },
  DECISION_FINALIZED: { label: "Kết luận", color: "bg-emerald-100 text-emerald-900 border-emerald-300" },
  WORKER_RECOVERED: { label: "Khôi phục an toàn", color: "bg-amber-100 text-amber-900 border-amber-300" },
  SUPERVISOR_STATE: { label: "Trạng thái AI", color: "bg-slate-100 text-slate-800 border-slate-300" },
};

const SAMPLE_MOCK_RECEIPT_TEXT = `=== BIÊN LAI THANH TOÁN ĐIỆN TỬ (MOCK / TEST SAMPLE) ===
Đơn vị thụ hưởng: FinResolve Demo Store
Mã đơn hàng: ORD-RCPT-DEMO01
Mã giao dịch: PAY-MOMO-GHOST-991
Số tiền: 450000 VND
Trạng thái hiển thị trên ảnh: THÀNH CÔNG
Ghi chú: Bản mẫu tổng hợp có đóng dấu MOCK / TEST để kiểm thử AI đối chiếu sổ cái.`;

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

function formatMoneyVnd(val: unknown): string {
  const num = typeof val === "number" ? val : Number(val);
  if (!Number.isFinite(num)) return String(val ?? "—");
  return `${num.toLocaleString("vi-VN")} ₫`;
}

export function DemoDashboard() {
  const { session, loading: sessionLoading, error: sessionError, reload } = useDemoSession();
  const activeSessionIdRef = useRef<string | null>(null);
  const latestSeqRef = useRef<number>(0);
  const worldEpochRef = useRef<number>(1);

  const [catalog, setCatalog] = useState<ChallengeCatalogItem[]>([]);
  const [supervisor, setSupervisor] = useState<SupervisorStatus | null>(null);
  const [activities, setActivities] = useState<SupervisorActivityItem[]>([]);
  const [selectedChallenge, setSelectedChallenge] = useState<ChallengeRunRecord | null>(null);
  const [recentChallenges, setRecentChallenges] = useState<ChallengeRunRecord[]>([]);
  const [approvalInbox, setApprovalInbox] = useState<ApprovalInboxItem[]>([]);

  const [activeType, setActiveType] = useState<string>("SUSPICIOUS_RECEIPT");
  const [activePresetId, setActivePresetId] = useState<string>("NO_LEDGER_TX");

  // Receipt customization state
  const [claimedPaymentId, setClaimedPaymentId] = useState<string>("PAY-MOMO-GHOST-991");
  const [claimedAmount, setClaimedAmount] = useState<number>(450000);
  const [receiptNote, setReceiptNote] = useState<string>(
    "Đã chuyển khoản MoMo thành công 450.000đ, đề nghị xác nhận đơn ngay.",
  );
  const [sampleReceiptRaw, setSampleReceiptRaw] = useState<string>(SAMPLE_MOCK_RECEIPT_TEXT);
  const [extractedReceiptNotice, setExtractedReceiptNotice] = useState<string | null>(null);

  // Traffic burst customization state
  const [burstEvents, setBurstEvents] = useState<number>(90);
  const [burstDuration, setBurstDuration] = useState<number>(8);

  // Free-text complaint in advanced options
  const [freeClaimText, setFreeClaimText] = useState<string>("");

  const [busy, setBusy] = useState<boolean>(false);
  const [approvingId, setApprovingId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  // Load Challenge Lab catalog once
  useEffect(() => {
    let active = true;
    void apiRequest<ChallengeCatalogItem[]>("/api/v1/challenges/catalog")
      .then((items) => {
        if (!active) return;
        setCatalog(items);
      })
      .catch((e: unknown) => {
        if (active) setError(readableError(e));
      });
    return () => {
      active = false;
    };
  }, []);

  const syncPresetDefaults = useCallback(
    (cType: string, pId: string, catList: ChallengeCatalogItem[]) => {
      const cat = catList.find((x) => x.challenge_type === cType);
      const preset = cat?.presets.find((p) => p.preset_id === pId) ?? cat?.presets[0];
      if (!preset) return;
      const defs = preset.defaults || {};
      if (cType === "SUSPICIOUS_RECEIPT") {
        setClaimedPaymentId(
          typeof defs.claimed_payment_id === "string"
            ? defs.claimed_payment_id
            : `PAY-RCPT-${pId.slice(0, 6)}`,
        );
        setClaimedAmount(typeof defs.claimed_amount === "number" ? defs.claimed_amount : 450000);
        setReceiptNote(
          typeof defs.receipt_note === "string"
            ? defs.receipt_note
            : "Biên lai thanh toán qua ví MoMo.",
        );
      } else if (cType === "TRAFFIC_BURST") {
        setBurstEvents(typeof defs.event_count === "number" ? defs.event_count : 90);
        setBurstDuration(typeof defs.duration_seconds === "number" ? defs.duration_seconds : 8);
      }
    },
    [],
  );

  const loadOverview = useCallback(
    async (incremental = false) => {
      if (!session) return;
      const reqSessionId = session.session_id;
      const afterSeq = incremental ? latestSeqRef.current : 0;
      try {
        const data = await apiRequest<MissionControlOverview>(
          `/api/v1/supervisor/overview?after_seq=${afterSeq}`,
          { session },
        );
        if (activeSessionIdRef.current !== reqSessionId) return;

        const nextEpoch = data.supervisor.world_epoch;
        const epochChanged = worldEpochRef.current !== nextEpoch;
        worldEpochRef.current = nextEpoch;
        latestSeqRef.current = data.latest_seq;

        setSupervisor(data.supervisor);
        setRecentChallenges(data.recent_challenges);
        setApprovalInbox(data.approval_inbox);
        setSelectedChallenge((prev) => {
          if (epochChanged || !prev) return data.latest_challenge;
          const refreshed = data.recent_challenges.find((c) => c.id === prev.id);
          return refreshed ?? data.latest_challenge ?? prev;
        });
        setActivities((prev) =>
          incremental && !epochChanged
            ? mergeSupervisorActivities(prev, data.activities, nextEpoch)
            : mergeSupervisorActivities([], data.activities, nextEpoch),
        );
      } catch (e) {
        if (activeSessionIdRef.current === reqSessionId) {
          setError(readableError(e));
        }
      }
    },
    [session],
  );

  // Sync activeSessionIdRef and fetch overview when session changes
  const sessionId = session?.session_id ?? null;
  useEffect(() => {
    activeSessionIdRef.current = sessionId;
    latestSeqRef.current = 0;
    worldEpochRef.current = 1;
    if (session && sessionId) {
      void loadOverview(false);
    }
  }, [session, sessionId, loadOverview]);

  // Bounded live polling: fast (2.2s) only when Supervisor is investigating or queue_depth > 0
  const isBusyMonitoring =
    supervisor?.status === "INVESTIGATING" || (supervisor?.queue_depth ?? 0) > 0;
  useEffect(() => {
    if (!session || !sessionId) return;
    const intervalMs = isBusyMonitoring ? 2200 : 8000;
    const timer = window.setInterval(() => {
      void loadOverview(true);
    }, intervalMs);
    return () => {
      window.clearInterval(timer);
    };
  }, [session, sessionId, isBusyMonitoring, loadOverview]);

  const handleToggleAutopilot = async () => {
    if (!session || !supervisor) return;
    setBusy(true);
    setError(null);
    try {
      const endpoint = supervisor.autopilot_enabled
        ? "/api/v1/supervisor/pause"
        : "/api/v1/supervisor/resume";
      await apiRequest<SupervisorStatus>(endpoint, { session, method: "POST" });
      await loadOverview(false);
      setNotice(
        supervisor.autopilot_enabled
          ? "Đã tạm dừng AI Supervisor. Các sự kiện mới sẽ nằm chờ trong hàng đợi."
          : "Đã bật lại AI Supervisor và tự động xử lý các sự kiện trong hàng đợi.",
      );
    } catch (e) {
      setError(readableError(e));
    } finally {
      setBusy(false);
    }
  };

  const handleResetWorld = async () => {
    if (!session) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await apiRequest("/api/v1/demo/sessions/reset", { session, method: "POST" });
      clearSessionRunId(window.sessionStorage, session.session_id);
      latestSeqRef.current = 0;
      await loadOverview(false);
      setNotice("Đã đặt lại môi trường MOCK về trạng thái sạch. AI Supervisor đang giám sát.");
    } catch (e) {
      setError(readableError(e));
    } finally {
      setBusy(false);
    }
  };

  const handleSelectChallengeType = (cType: string) => {
    setActiveType(cType);
    const cat = catalog.find((x) => x.challenge_type === cType);
    const firstPreset = cat?.presets[0]?.preset_id ?? "";
    setActivePresetId(firstPreset);
    syncPresetDefaults(cType, firstPreset, catalog);
    setExtractedReceiptNotice(null);
  };

  const handleSelectPreset = (pId: string) => {
    setActivePresetId(pId);
    syncPresetDefaults(activeType, pId, catalog);
  };

  const handleExtractMockReceipt = async () => {
    if (!session) return;
    setBusy(true);
    setError(null);
    try {
      const res = await apiRequest<{
        claimed_payment_id: string;
        claimed_amount: number;
        notice_vi: string;
      }>("/api/v1/challenges/receipt-extract", {
        session,
        method: "POST",
        body: {
          raw_content: sampleReceiptRaw,
          filename: "mock_receipt_sample.txt",
        },
      });
      setClaimedPaymentId(res.claimed_payment_id);
      setClaimedAmount(res.claimed_amount);
      setExtractedReceiptNotice(res.notice_vi);
    } catch (e) {
      setError(readableError(e));
    } finally {
      setBusy(false);
    }
  };

  const handleLaunchChallenge = async () => {
    if (!session) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const parameters: JsonObject = {};
      if (activeType === "SUSPICIOUS_RECEIPT") {
        parameters.claimed_payment_id = claimedPaymentId.trim() || "PAY-MOMO-GHOST-991";
        parameters.claimed_amount = Number(claimedAmount) || 450000;
        parameters.receipt_note = receiptNote.trim();
      } else if (activeType === "TRAFFIC_BURST") {
        parameters.event_count = Math.min(120, Math.max(1, Number(burstEvents) || 45));
        parameters.duration_seconds = Math.min(30, Math.max(1, Number(burstDuration) || 5));
      }

      const launched = await apiRequest<ChallengeRunRecord>("/api/v1/challenges", {
        session,
        method: "POST",
        body: {
          challenge_type: activeType,
          preset_id: activePresetId,
          parameters,
          auto_process: true,
        },
      });
      setSelectedChallenge(launched);
      await loadOverview(false);
      setNotice(
        supervisor?.autopilot_enabled === false
          ? `Đã tạo thử thách "${launched.title_vi}" vào hàng đợi (AI đang tạm dừng — bấm Tiếp tục AI để xử lý).`
          : `AI Supervisor đã tự động phát hiện và xử lý thử thách "${launched.title_vi}".`,
      );
    } catch (e) {
      setError(readableError(e));
    } finally {
      setBusy(false);
    }
  };

  const handleCancelChallenge = async (challengeId: string) => {
    if (!session) return;
    setBusy(true);
    setError(null);
    try {
      const cancelled = await apiRequest<ChallengeRunRecord>(
        `/api/v1/challenges/${encodeURIComponent(challengeId)}/cancel`,
        { session, method: "POST" },
      );
      setSelectedChallenge(cancelled);
      await loadOverview(false);
      setNotice("Đã dừng thử thách và hủy các tác vụ đang chờ an toàn.");
    } catch (e) {
      setError(readableError(e));
    } finally {
      setBusy(false);
    }
  };

  const handleFreeTextSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (!session || !freeClaimText.trim()) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const created = await apiRequest<CaseRecord>("/api/v1/cases", {
        session,
        method: "POST",
        body: {
          text: freeClaimText.trim(),
          type: "PAYMENT_DISPUTE",
          auto_investigate: true,
        },
      });
      setFreeClaimText("");
      await loadOverview(false);
      setNotice(
        `Đã gửi khiếu nại và AI Supervisor tự động điều tra hồ sơ ${created.id} (trạng thái: ${created.state}).`,
      );
    } catch (err) {
      setError(readableError(err));
    } finally {
      setBusy(false);
    }
  };

  const handleApproveAndExecute = async (item: ApprovalInboxItem) => {
    if (!session) return;
    setApprovingId(item.proposal_id);
    setError(null);
    setNotice(null);
    try {
      await apiRequest<ActionApproval>(
        `/api/v1/actions/${encodeURIComponent(item.proposal_id)}/simulate-demo-approval`,
        {
          session,
          method: "POST",
          body: {
            reason: "Phê duyệt từ Hộp thư phê duyệt AI Mission Control (MOCK)",
            expiry_minutes: 60,
          },
        },
      );
      const execRes = await apiRequest<ActionExecution>(
        `/api/v1/actions/${encodeURIComponent(item.proposal_id)}/execute`,
        {
          session,
          method: "POST",
          idempotencyKey: `mc-exec-${item.proposal_id}`,
          body: {
            simulate_timeout: false,
            simulate_verification_failure: false,
          },
        },
      );
      await loadOverview(false);
      const verdict = execRes.verification?.verdict ?? "UNKNOWN";
      setNotice(
        `Đã phê duyệt & thực thi ${item.action_label_vi}. Kết quả Verifier kiểm chứng độc lập: ${verdict} (Hồ sơ → ${execRes.case_state}).`,
      );
    } catch (e) {
      setError(readableError(e));
    } finally {
      setApprovingId(null);
    }
  };

  const handleRejectProposal = async (item: ApprovalInboxItem) => {
    if (!session) return;
    setApprovingId(item.proposal_id);
    setError(null);
    setNotice(null);
    try {
      await apiRequest<ActionApproval>(
        `/api/v1/actions/${encodeURIComponent(item.proposal_id)}/simulate-demo-reject`,
        {
          session,
          method: "POST",
          body: {
            reason: "Người vận hành từ chối đề xuất từ Hộp thư phê duyệt Mission Control",
            expiry_minutes: 60,
          },
        },
      );
      await loadOverview(false);
      setNotice(`Đã từ chối đề xuất ${item.action_label_vi} — không có thay đổi tài chính nào được thực thi.`);
    } catch (e) {
      setError(readableError(e));
    } finally {
      setApprovingId(null);
    }
  };

  const execBadge = resolveSupervisorExecutionBadge(
    supervisor?.llm_execution_state,
    supervisor?.configured_model_mode,
  );
  const activeCatalogItem = catalog.find((c) => c.challenge_type === activeType);
  const primaryCatalog = catalog.filter((c) =>
    (PRIMARY_CHALLENGES as readonly string[]).includes(c.challenge_type),
  );
  const advancedCatalog = catalog.filter((c) => c.is_advanced);
  const outcome = (selectedChallenge?.outcome_json ?? {}) as Record<string, unknown>;
  const toolsRan = Array.isArray(outcome.tools_ran) ? (outcome.tools_ran as string[]) : [];
  const observationsVi = Array.isArray(outcome.observations_vi)
    ? (outcome.observations_vi as string[])
    : [];
  const trafficMetrics = (selectedChallenge?.generated_refs_json?.traffic_metrics ??
    null) as Record<string, unknown> | null;

  return (
    <div className="space-y-6">
      {/* Top Hero Header */}
      <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-xs">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="space-y-1">
            <div className="flex flex-wrap items-center gap-2">
              <span className="inline-flex items-center gap-1.5 rounded-full bg-indigo-50 px-3 py-1 text-xs font-semibold text-indigo-800">
                <span
                  className={`h-2 w-2 rounded-full ${
                    supervisor?.autopilot_enabled === false
                      ? "bg-amber-500"
                      : execBadge.isThinking || isBusyMonitoring
                        ? "animate-ping bg-indigo-600"
                        : "bg-emerald-500"
                  }`}
                />
                AI MISSION CONTROL
              </span>
              <span className="rounded-full border border-slate-200 bg-slate-50 px-2.5 py-0.5 text-xs font-medium text-slate-700">
                Môi trường dữ liệu mô phỏng (MOCK)
              </span>
            </div>
            <h1 className="text-2xl font-bold tracking-tight text-slate-900">
              Trung tâm Điều hành &amp; Thử nghiệm Agent Tự hành
            </h1>
            <p className="max-w-3xl text-sm text-slate-600">
              Tạo tình huống bất thường (hóa đơn giả, hoàn tiền trùng, nghẽn đơn, đột biến lưu lượng)
              và quan sát AI Supervisor tự động phát hiện, tra cứu sổ cái, đánh giá rủi ro và dừng
              tại chốt chặn phê duyệt của con người.
            </p>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              disabled={busy || sessionLoading || !supervisor}
              onClick={() => void handleToggleAutopilot()}
              className={
                supervisor?.autopilot_enabled
                  ? "rounded-lg border border-amber-300 bg-amber-50 px-3.5 py-2 text-xs font-semibold text-amber-900 hover:bg-amber-100 disabled:opacity-50"
                  : primaryButton
              }
            >
              {supervisor?.autopilot_enabled ? "⏸ Tạm dừng AI (Pause)" : "▶ Tiếp tục AI (Resume)"}
            </button>
            <button
              type="button"
              disabled={busy || sessionLoading}
              onClick={() => void handleResetWorld()}
              className={secondaryButton}
            >
              Đặt lại dữ liệu
            </button>
            <button
              type="button"
              disabled={busy || sessionLoading}
              onClick={() => {
                setSupervisor(null);
                setActivities([]);
                setSelectedChallenge(null);
                setRecentChallenges([]);
                setApprovalInbox([]);
                setError(null);
                setNotice(null);
                void reload(true);
              }}
              className={secondaryButton}
            >
              Phiên mới
            </button>
          </div>
        </div>

        {(error || sessionError) && (
          <div
            role="alert"
            className="mt-4 rounded-xl border border-rose-300 bg-rose-50 px-4 py-3 text-sm text-rose-900"
          >
            {error ?? sessionError}
          </div>
        )}
        {notice && (
          <div
            role="status"
            className="mt-4 rounded-xl border border-emerald-300 bg-emerald-50 px-4 py-3 text-sm text-emerald-950"
          >
            {notice}
          </div>
        )}
      </div>

      {/* SECTION 1 — AI STATUS */}
      <Panel
        title="1. Trạng thái AI Supervisor"
        subtitle="Giám sát sự kiện tự động — không gọi LLM lặp vô hạn khi hệ thống đang rảnh"
        right={
          <div className="flex flex-wrap items-center gap-2">
            <StatusPill status={supervisor?.status ?? "MONITORING"} />
            <span
              className={`inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-semibold ${
                execBadge.isLlmConfirmed
                  ? "border-emerald-300 bg-emerald-50 text-emerald-900"
                  : "border-slate-300 bg-slate-100 text-slate-800"
              }`}
              title={execBadge.note}
            >
              {execBadge.badge}
            </span>
          </div>
        }
      >
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <div className="rounded-xl border border-slate-200 bg-slate-50/70 p-3.5">
            <div className="text-xs font-medium text-slate-500">Trạng thái giám sát</div>
            <div className="mt-1 text-base font-bold text-slate-900">
              {supervisor?.status_label_vi ?? "Đang khởi tạo…"}
            </div>
            <div className="mt-1 text-xs text-slate-600">
              {supervisor?.autopilot_enabled
                ? "Tự động điều tra khi có sự kiện mới"
                : "Đang tạm dừng theo yêu cầu"}
            </div>
          </div>

          <div className="rounded-xl border border-slate-200 bg-slate-50/70 p-3.5">
            <div className="text-xs font-medium text-slate-500">Chế độ suy luận &amp; LLM</div>
            <div className="mt-1 text-sm font-bold text-slate-900">
              {supervisor?.configured_provider
                ? `${supervisor.configured_provider} (${supervisor.configured_model_name})`
                : "Luật xác định (DEMO_RULES)"}
            </div>
            <div className="mt-1 text-xs text-slate-600">
              LLM thành công gần nhất:{" "}
              <span className="font-medium text-slate-800">
                {formatTimeVi(supervisor?.last_llm_succeeded_at)}
              </span>{" "}
              ({supervisor?.llm_calls_count ?? 0}/{supervisor?.llm_budget_calls ?? 30} lượt)
            </div>
          </div>

          <div className="rounded-xl border border-slate-200 bg-slate-50/70 p-3.5">
            <div className="text-xs font-medium text-slate-500">Hàng đợi &amp; Hồ sơ</div>
            <div className="mt-1 flex items-baseline gap-3">
              <span className="text-lg font-bold text-slate-900">
                {supervisor?.queue_depth ?? 0} chờ
              </span>
              <span className="text-xs text-slate-600">
                • {supervisor?.active_cases_count ?? 0} hồ sơ đang xử lý (tổng{" "}
                {supervisor?.total_cases_count ?? 0})
              </span>
            </div>
            <div className="mt-1 text-xs text-slate-600">
              Đã tự động hoàn tất: {supervisor?.jobs_processed_count ?? 0} tác vụ
            </div>
          </div>

          <div className="rounded-xl border border-amber-200 bg-amber-50/60 p-3.5">
            <div className="text-xs font-medium text-amber-800">Quyết định chờ bạn phê duyệt</div>
            <div className="mt-1 text-xl font-bold text-amber-950">
              {supervisor?.waiting_approvals_count ?? 0} đề xuất
            </div>
            <div className="mt-1 text-xs text-amber-900">
              AI không bao giờ tự ý duyệt chi hay ghi nhận thanh toán
            </div>
          </div>
        </div>

        <p className="mt-3 text-xs text-slate-500">
          {supervisor?.monitoring_note_vi ??
            "Giám sát theo sự kiện: khi không có sự kiện mới, AI không gọi LLM liên tục."}
          {supervisor?.last_llm_error ? ` • Cảnh báo LLM gần nhất: ${supervisor.last_llm_error}` : ""}
        </p>
      </Panel>

      {/* SECTION 5 (Prominent when items exist) — HUMAN APPROVAL INBOX */}
      <Panel
        title={`5. Hộp thư phê duyệt của con người (${approvalInbox.length})`}
        subtitle="AI dừng tại đây khi cần thực hiện hành động tài chính hoặc đồng bộ đơn hàng — chỉ thực thi sau khi bạn phê duyệt"
      >
        {approvalInbox.length === 0 ? (
          <div className="rounded-xl border border-dashed border-slate-200 bg-slate-50/50 px-4 py-5 text-center text-sm text-slate-600">
            Hiện không có đề xuất nào chờ phê duyệt. Hãy chạy thử thách{" "}
            <button
              type="button"
              onClick={() => handleSelectChallengeType("BULK_ORDER_SYNC")}
              className="font-semibold text-indigo-700 underline hover:text-indigo-900"
            >
              Nghẽn đơn hàng
            </button>{" "}
            hoặc{" "}
            <button
              type="button"
              onClick={() => {
                handleSelectChallengeType("DUPLICATE_REFUND");
                handleSelectPreset("LEGITIMATE_PARTIAL");
              }}
              className="font-semibold text-indigo-700 underline hover:text-indigo-900"
            >
              Hoàn tiền một phần hợp lệ
            </button>{" "}
            để trải nghiệm quy trình AI đề xuất → Con người phê duyệt → Verifier kiểm chứng.
          </div>
        ) : (
          <div className="space-y-3">
            {approvalInbox.map((item) => (
              <div
                key={item.proposal_id}
                className="flex flex-wrap items-center justify-between gap-4 rounded-xl border border-amber-300 bg-amber-50/70 p-4"
              >
                <div className="space-y-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="rounded-md bg-amber-200/80 px-2 py-0.5 text-xs font-bold text-amber-950">
                      CHỜ PHÊ DUYỆT
                    </span>
                    <span className="text-sm font-bold text-slate-900">{item.action_label_vi}</span>
                    <span className="text-xs text-slate-600">• Mức rủi ro: {item.risk_level}</span>
                  </div>
                  <p className="text-sm font-medium text-slate-800">{item.summary_vi}</p>
                  <div className="flex flex-wrap items-center gap-3 text-xs text-slate-600">
                    <span>Bằng chứng đã kiểm tra: {item.evidence_refs.length} bản ghi</span>
                    <Link
                      href={caseUrl(item.case_id)}
                      className="font-semibold text-indigo-700 underline hover:text-indigo-900"
                    >
                      Xem chi tiết bằng chứng hồ sơ →
                    </Link>
                  </div>
                </div>

                <div className="flex flex-wrap items-center gap-2">
                  <button
                    type="button"
                    disabled={approvingId === item.proposal_id || busy}
                    onClick={() => void handleApproveAndExecute(item)}
                    className={primaryButton}
                  >
                    {approvingId === item.proposal_id
                      ? "Đang duyệt & kiểm chứng…"
                      : "✓ Phê duyệt & Thực thi (MOCK)"}
                  </button>
                  <button
                    type="button"
                    disabled={approvingId === item.proposal_id || busy}
                    onClick={() => void handleRejectProposal(item)}
                    className={secondaryButton}
                  >
                    Từ chối
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </Panel>

      {/* SECTION 2 — CHALLENGE LAB */}
      <Panel
        title="2. Phòng thử nghiệm tình huống (Challenge Lab)"
        subtitle="Chọn 1 tình huống để tạo sự kiện bất thường — AI Supervisor sẽ tự động phát hiện và điều tra ngay lập tức"
      >
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {primaryCatalog.map((item) => {
            const isSelected = activeType === item.challenge_type;
            return (
              <button
                key={item.challenge_type}
                type="button"
                onClick={() => handleSelectChallengeType(item.challenge_type)}
                className={`flex flex-col justify-between rounded-xl border p-4 text-left transition ${
                  isSelected
                    ? "border-indigo-600 bg-indigo-50/60 ring-2 ring-indigo-600/20"
                    : "border-slate-200 bg-white hover:border-slate-300 hover:bg-slate-50"
                }`}
              >
                <div>
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-base font-bold text-slate-900">{item.title_vi}</span>
                    {isSelected && (
                      <span className="rounded-full bg-indigo-600 px-2 py-0.5 text-[11px] font-semibold text-white">
                        Đang chọn
                      </span>
                    )}
                  </div>
                  <p className="mt-1.5 text-xs text-slate-600">{item.subtitle_vi}</p>
                </div>
                <div className="mt-3 text-xs font-semibold text-indigo-700">
                  {item.presets.length} kịch bản mẫu →
                </div>
              </button>
            );
          })}
        </div>

        {/* Selected Challenge Configurator */}
        {activeCatalogItem && (
          <div className="mt-4 rounded-xl border border-slate-200 bg-slate-50/70 p-4">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <h3 className="text-sm font-bold text-slate-900">
                  Tùy chỉnh tình huống: {activeCatalogItem.title_vi}
                </h3>
                <p className="text-xs text-slate-600">
                  Chọn mẫu có sẵn hoặc chỉnh thông số rồi bấm nút chạy — không cần tự tạo Case hay
                  bấm điều tra thủ công.
                </p>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                {selectedChallenge &&
                  (selectedChallenge.status === "RUNNING" ||
                    selectedChallenge.status === "QUEUED") && (
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => void handleCancelChallenge(selectedChallenge.id)}
                      className="rounded-lg border border-rose-300 bg-rose-50 px-3 py-2 text-xs font-semibold text-rose-900 hover:bg-rose-100"
                    >
                      ⏹ Dừng thử thách
                    </button>
                  )}
                <button
                  type="button"
                  disabled={busy || sessionLoading || !session}
                  onClick={() => void handleLaunchChallenge()}
                  className={primaryButton}
                >
                  {busy
                    ? "AI đang tự động xử lý…"
                    : `⚡ Kích hoạt "${activeCatalogItem.title_vi}"`}
                </button>
              </div>
            </div>

            {/* Preset radio pills */}
            <div className="mt-3 grid gap-2 sm:grid-cols-2">
              {activeCatalogItem.presets.map((p) => {
                const checked = activePresetId === p.preset_id;
                return (
                  <button
                    key={p.preset_id}
                    type="button"
                    onClick={() => handleSelectPreset(p.preset_id)}
                    className={`rounded-lg border p-3 text-left text-xs transition ${
                      checked
                        ? "border-indigo-600 bg-white font-medium text-slate-900 shadow-2xs"
                        : "border-slate-200 bg-white/70 text-slate-700 hover:bg-white"
                    }`}
                  >
                    <div className="font-bold text-slate-900">{p.label_vi}</div>
                    <div className="mt-1 text-slate-600">{p.description_vi}</div>
                  </button>
                );
              })}
            </div>

            {/* Structured receipt editor for SUSPICIOUS_RECEIPT */}
            {activeType === "SUSPICIOUS_RECEIPT" && (
              <div className="mt-4 grid gap-3 border-t border-slate-200 pt-4 sm:grid-cols-3">
                <label className="text-xs font-medium text-slate-700">
                  Mã giao dịch khai trên hóa đơn
                  <input
                    type="text"
                    value={claimedPaymentId}
                    onChange={(e) => setClaimedPaymentId(e.target.value)}
                    className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-sm"
                  />
                </label>
                <label className="text-xs font-medium text-slate-700">
                  Số tiền khai trên hóa đơn (VND)
                  <input
                    type="number"
                    min={10000}
                    max={50000000}
                    step={10000}
                    value={claimedAmount}
                    onChange={(e) => setClaimedAmount(Number(e.target.value))}
                    className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-sm"
                  />
                </label>
                <label className="text-xs font-medium text-slate-700">
                  Lời khai / ghi chú đính kèm hóa đơn
                  <input
                    type="text"
                    value={receiptNote}
                    onChange={(e) => setReceiptNote(e.target.value)}
                    className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-sm"
                  />
                </label>

                <details className="sm:col-span-3">
                  <summary className="cursor-pointer text-xs font-semibold text-indigo-700 hover:text-indigo-900">
                    Nạp từ mẫu văn bản biên lai tổng hợp (có đóng dấu MOCK / TEST)
                  </summary>
                  <div className="mt-2 space-y-2 rounded-lg border border-slate-200 bg-white p-3">
                    <textarea
                      rows={4}
                      value={sampleReceiptRaw}
                      onChange={(e) => setSampleReceiptRaw(e.target.value)}
                      className="w-full rounded-md border border-slate-300 p-2 font-mono text-xs"
                    />
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <span className="text-[11px] text-slate-500">
                        Mọi trường trích xuất từ biên lai đều được gắn nhãn UNTRUSTED_CLAIM (chưa
                        xác thực).
                      </span>
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => void handleExtractMockReceipt()}
                        className={secondaryButton}
                      >
                        Trích xuất trường biên lai MOCK
                      </button>
                    </div>
                    {extractedReceiptNotice && (
                      <div className="rounded-md bg-indigo-50 px-3 py-2 text-xs text-indigo-900">
                        {extractedReceiptNotice}
                      </div>
                    )}
                  </div>
                </details>
              </div>
            )}

            {/* Traffic burst bounded controls */}
            {activeType === "TRAFFIC_BURST" && (
              <div className="mt-4 grid gap-3 border-t border-slate-200 pt-4 sm:grid-cols-2">
                <label className="text-xs font-medium text-slate-700">
                  Số lượng sự kiện mô phỏng (giới hạn an toàn 1–120)
                  <input
                    type="number"
                    min={1}
                    max={120}
                    value={burstEvents}
                    onChange={(e) => setBurstEvents(Number(e.target.value))}
                    className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-sm"
                  />
                </label>
                <label className="text-xs font-medium text-slate-700">
                  Thời lượng đợt tăng tải (1–30 giây)
                  <input
                    type="number"
                    min={1}
                    max={30}
                    value={burstDuration}
                    onChange={(e) => setBurstDuration(Number(e.target.value))}
                    className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-sm"
                  />
                </label>
              </div>
            )}
          </div>
        )}

        {/* Advanced Challenge Options (Prompt Injection + Free-text intake) */}
        <details className="mt-4 rounded-xl border border-slate-200 bg-white p-3.5">
          <summary className="cursor-pointer text-xs font-semibold text-slate-700 hover:text-slate-900">
            Tùy chọn thử thách nâng cao (Kiểm thử Prompt Injection &amp; Khiếu nại tự do)
          </summary>
          <div className="mt-3 grid gap-4 border-t border-slate-100 pt-3 lg:grid-cols-2">
            <div className="space-y-2">
              <div className="text-xs font-bold text-slate-900">
                Kiểm thử chống thao túng câu lệnh (Prompt Injection)
              </div>
              <p className="text-xs text-slate-600">
                Thử chèn mệnh lệnh giả mạo quyền hệ thống để kiểm tra xem AI có tự ý bỏ qua sổ cái
                hoặc tự xác nhận thanh toán hay không.
              </p>
              <div className="flex flex-wrap gap-2">
                {advancedCatalog.map((adv) =>
                  adv.presets.map((p) => (
                    <button
                      key={p.preset_id}
                      type="button"
                      disabled={busy}
                      onClick={() => {
                        setActiveType(adv.challenge_type);
                        setActivePresetId(p.preset_id);
                      }}
                      className={secondaryButton}
                    >
                      Chọn: {p.label_vi}
                    </button>
                  )),
                )}
              </div>
            </div>

            <form onSubmit={(e) => void handleFreeTextSubmit(e)} className="space-y-2">
              <div className="text-xs font-bold text-slate-900">
                Gửi khiếu nại tiếng Việt bất kỳ (AI tự động điều tra)
              </div>
              <div className="flex gap-2">
                <input
                  type="text"
                  value={freeClaimText}
                  onChange={(e) => setFreeClaimText(e.target.value)}
                  placeholder="Ví dụ: Tôi đã chuyển tiền MoMo 350.000đ cho đơn hàng nhưng chưa thấy xác nhận..."
                  className="flex-1 rounded-lg border border-slate-300 px-3 py-1.5 text-xs"
                />
                <button
                  type="submit"
                  disabled={busy || !freeClaimText.trim()}
                  className={secondaryButton}
                >
                  Gửi &amp; Tự điều tra
                </button>
              </div>
            </form>
          </div>
        </details>
      </Panel>

      {/* Two-column layout: SECTION 3 (LIVE AGENT ACTIVITY) & SECTION 4 (CHALLENGE RESULTS) */}
      <div className="grid gap-6 lg:grid-cols-12">
        {/* SECTION 3 — LIVE AGENT ACTIVITY */}
        <div className="lg:col-span-5">
          <Panel
            title="3. Hoạt động Agent thời gian thực"
            subtitle="Dòng thời gian lấy trực tiếp từ các bước điều tra và công cụ đọc thực tế tại backend"
          >
            {activities.length === 0 ? (
              <p className="py-6 text-center text-sm text-slate-500">
                Chưa có hoạt động nào trong phiên này. Hãy chọn một thử thách ở mục 2 để xem AI tự
                động làm việc.
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
                          {formatTimeVi(act.created_at)}
                        </span>
                        <span
                          className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold ${badge.color}`}
                        >
                          {badge.label}
                        </span>
                      </div>
                      <div className="mt-1 font-semibold text-slate-900">{act.title_vi}</div>
                      {act.detail_vi && (
                        <div className="mt-0.5 text-slate-600">{act.detail_vi}</div>
                      )}
                    </li>
                  );
                })}
              </ol>
            )}
          </Panel>
        </div>

        {/* SECTION 4 — CHALLENGE RESULTS */}
        <div className="lg:col-span-7">
          <Panel
            title="4. Kết quả &amp; Bằng chứng điều tra"
            subtitle="Tổng hợp những gì bạn vừa thử, những gì AI quan sát được từ sổ cái và quyết định an toàn cuối cùng"
            right={
              selectedChallenge ? <StatusPill status={selectedChallenge.status} /> : undefined
            }
          >
            {!selectedChallenge ? (
              <div className="rounded-xl border border-dashed border-slate-200 bg-slate-50/50 px-4 py-10 text-center text-sm text-slate-600">
                Chưa có thử thách nào được chạy. Hãy bấm{" "}
                <span className="font-semibold text-indigo-700">Kích hoạt</span> ở mục 2 để xem AI
                tự động điều tra và hiển thị bằng chứng tại đây.
              </div>
            ) : (
              <div className="space-y-4">
                {/* Recent challenge switcher if multiple challenges were run */}
                {recentChallenges.length > 1 && (
                  <div className="flex flex-wrap items-center gap-1.5 text-xs">
                    <span className="font-medium text-slate-500">Lịch sử thử thách:</span>
                    {recentChallenges.map((c, idx) => (
                      <button
                        key={c.id}
                        type="button"
                        onClick={() => setSelectedChallenge(c)}
                        className={`rounded-md border px-2.5 py-1 font-medium ${
                          selectedChallenge.id === c.id
                            ? "border-indigo-600 bg-indigo-50 text-indigo-900"
                            : "border-slate-200 bg-white text-slate-700 hover:bg-slate-50"
                        }`}
                      >
                        #{recentChallenges.length - idx}: {c.challenge_type}
                      </button>
                    ))}
                  </div>
                )}

                {/* Prominent Vietnamese Conclusion Banner */}
                <div
                  className={`rounded-xl border p-4 ${
                    selectedChallenge.safety_verdict === "VERIFIED_RESOLVED"
                      ? "border-emerald-300 bg-emerald-50 text-emerald-950"
                      : selectedChallenge.safety_verdict === "WAITING_HUMAN_APPROVAL"
                        ? "border-amber-300 bg-amber-50 text-amber-950"
                        : "border-indigo-200 bg-indigo-50/70 text-slate-900"
                  }`}
                >
                  <div className="text-xs font-bold uppercase tracking-wide text-slate-500">
                    Kết luận của AI Supervisor • {selectedChallenge.status_label_vi}
                  </div>
                  <div className="mt-1 text-base font-bold">
                    {typeof outcome.summary_vi === "string" && outcome.summary_vi
                      ? outcome.summary_vi
                      : "AI đang thu thập và đối chiếu bằng chứng…"}
                  </div>
                  {typeof outcome.decision_summary === "string" && outcome.decision_summary && (
                    <p className="mt-1 text-xs text-slate-700">{outcome.decision_summary}</p>
                  )}
                </div>

                {/* 4 clear breakdown cards */}
                <div className="grid gap-3 sm:grid-cols-2">
                  <div className="rounded-xl border border-slate-200 bg-slate-50/70 p-3.5 text-xs">
                    <div className="font-bold text-slate-900">Dữ liệu bạn đã đưa vào</div>
                    <div className="mt-1.5 space-y-1 text-slate-700">
                      <div>
                        Loại thử thách:{" "}
                        <span className="font-semibold">{selectedChallenge.title_vi}</span>
                      </div>
                      {selectedChallenge.input_json.claimed_payment_id ? (
                        <div>
                          Mã GD khai báo:{" "}
                          <span className="font-mono font-semibold">
                            {String(selectedChallenge.input_json.claimed_payment_id)}
                          </span>
                        </div>
                      ) : null}
                      {selectedChallenge.input_json.claimed_amount ? (
                        <div>
                          Số tiền khai báo:{" "}
                          <span className="font-semibold">
                            {formatMoneyVnd(selectedChallenge.input_json.claimed_amount)}
                          </span>
                        </div>
                      ) : null}
                      {selectedChallenge.input_json.requested_refund ? (
                        <div>
                          Số tiền yêu cầu hoàn:{" "}
                          <span className="font-semibold">
                            {formatMoneyVnd(selectedChallenge.input_json.requested_refund)}
                          </span>
                        </div>
                      ) : null}
                      {selectedChallenge.input_json.event_count ? (
                        <div>
                          Lưu lượng gửi vào:{" "}
                          <span className="font-semibold">
                            {String(selectedChallenge.input_json.event_count)} sự kiện /{" "}
                            {String(selectedChallenge.input_json.duration_seconds)} giây
                          </span>
                        </div>
                      ) : null}
                    </div>
                  </div>

                  <div className="rounded-xl border border-slate-200 bg-slate-50/70 p-3.5 text-xs">
                    <div className="font-bold text-slate-900">Công cụ đọc dữ liệu AI đã chạy</div>
                    {toolsRan.length === 0 ? (
                      <p className="mt-1.5 text-slate-500">Chưa ghi nhận lệnh gọi công cụ.</p>
                    ) : (
                      <div className="mt-1.5 flex flex-wrap gap-1.5">
                        {toolsRan.map((t, idx) => (
                          <span
                            key={`${t}-${idx}`}
                            className="rounded-md border border-sky-200 bg-sky-50 px-2 py-1 font-medium text-sky-900"
                          >
                            {TOOL_NAME_VI[t] ?? t}
                          </span>
                        ))}
                      </div>
                    )}
                  </div>

                  <div className="rounded-xl border border-slate-200 bg-slate-50/70 p-3.5 text-xs sm:col-span-2">
                    <div className="font-bold text-slate-900">
                      Những gì AI quan sát được từ dữ liệu xác thực
                    </div>
                    {observationsVi.length > 0 ? (
                      <ul className="mt-1.5 list-disc space-y-1 pl-4 text-slate-700">
                        {observationsVi.map((obs, idx) => (
                          <li key={idx}>{obs}</li>
                        ))}
                      </ul>
                    ) : (
                      <p className="mt-1 text-slate-600">
                        Đã đối chiếu dữ liệu hồ sơ và sổ cái giao dịch trong phiên hiện tại.
                      </p>
                    )}

                    {/* Traffic burst measured metrics */}
                    {trafficMetrics && (
                      <div className="mt-3 grid grid-cols-2 gap-2 rounded-lg border border-slate-200 bg-white p-2.5 sm:grid-cols-4">
                        <div>
                          <div className="text-[11px] text-slate-500">Tổng request sinh ra</div>
                          <div className="text-sm font-bold text-slate-900">
                            {String(trafficMetrics.generated_events)}
                          </div>
                        </div>
                        <div>
                          <div className="text-[11px] text-slate-500">Được chấp nhận</div>
                          <div className="text-sm font-bold text-emerald-700">
                            {String(trafficMetrics.accepted_events)}
                          </div>
                        </div>
                        <div>
                          <div className="text-[11px] text-slate-500">Chặn trùng lặp (Dedup)</div>
                          <div className="text-sm font-bold text-indigo-700">
                            {String(trafficMetrics.duplicate_events)}
                          </div>
                        </div>
                        <div>
                          <div className="text-[11px] text-slate-500">Chặn Rate Limit (429)</div>
                          <div className="text-sm font-bold text-amber-700">
                            {String(trafficMetrics.rate_limited_events)}
                          </div>
                        </div>
                      </div>
                    )}
                  </div>
                </div>

                {/* Safety & Postcondition Verification Pills */}
                <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-slate-200 bg-white p-3 text-xs">
                  <div className="flex flex-wrap items-center gap-3">
                    <span>
                      Chặn hành động rủi ro:{" "}
                      <strong className="text-slate-900">
                        {outcome.action_blocked ? "CÓ (Đã chặn an toàn)" : "Không"}
                      </strong>
                    </span>
                    <span>•</span>
                    <span>
                      Cần con người phê duyệt:{" "}
                      <strong className="text-slate-900">
                        {outcome.requires_human_approval ? "CÓ (Xem mục 5)" : "Không"}
                      </strong>
                    </span>
                    <span>•</span>
                    <span>
                      Verifier kiểm chứng hậu điều kiện:{" "}
                      <strong className="text-emerald-700">
                        {outcome.postconditions_verified ? "PASSED (Đã xác minh)" : "Chưa thực thi"}
                      </strong>
                    </span>
                  </div>

                  <div className="flex flex-wrap items-center gap-3">
                    {selectedChallenge.linked_case_ids[0] && (
                      <Link
                        href={caseUrl(selectedChallenge.linked_case_ids[0])}
                        className="font-semibold text-indigo-700 underline hover:text-indigo-900"
                      >
                        Mở bằng chứng hồ sơ →
                      </Link>
                    )}
                    {selectedChallenge.linked_incident_ids.length > 0 && (
                      <Link
                        href="/live"
                        className="font-semibold text-indigo-700 underline hover:text-indigo-900"
                      >
                        Xem Incident &amp; Taskforce →
                      </Link>
                    )}
                  </div>
                </div>

                {/* Collapsible Advanced Technical Details */}
                <details className="rounded-xl border border-slate-200 bg-slate-50 p-3 text-xs">
                  <summary className="cursor-pointer font-semibold text-slate-700 hover:text-slate-900">
                    Chi tiết kỹ thuật nâng cao (Case ID, Evidence IDs, Token Usage, Audit JSON)
                  </summary>
                  <div className="mt-2 space-y-1.5 text-slate-600">
                    <div>
                      Challenge ID: <code className="font-mono">{selectedChallenge.id}</code> •
                      World Epoch: <code className="font-mono">{selectedChallenge.world_epoch}</code>
                    </div>
                    <div>
                      Linked Case IDs:{" "}
                      <code className="font-mono">
                        {selectedChallenge.linked_case_ids.join(", ") || "—"}
                      </code>
                    </div>
                    <div>
                      Linked Incident IDs:{" "}
                      <code className="font-mono">
                        {selectedChallenge.linked_incident_ids.join(", ") || "—"}
                      </code>
                    </div>
                    <div>
                      Chế độ thực thi AI:{" "}
                      <code className="font-mono">
                        {String(outcome.llm_execution_state ?? supervisor?.llm_execution_state)}
                      </code>{" "}
                      • Bước suy luận: <code className="font-mono">{String(outcome.steps_executed ?? 0)}</code>{" "}
                      • Tokens: <code className="font-mono">{String(outcome.total_tokens_used ?? 0)}</code>
                    </div>
                    <pre className="mt-2 max-h-48 overflow-auto rounded-lg bg-slate-900 p-2.5 font-mono text-[11px] text-slate-100">
                      {JSON.stringify(selectedChallenge.outcome_json, null, 2)}
                    </pre>
                  </div>
                </details>
              </div>
            )}
          </Panel>
        </div>
      </div>
    </div>
  );
}
