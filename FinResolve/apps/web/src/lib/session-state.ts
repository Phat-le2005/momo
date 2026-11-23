/** Pure session-scoping & honest AI-mode helpers (Prompt 08).
 *
 * Kept pure so both React components and automated Node/TS regression tests
 * exercise the exact same isolation and labeling logic.
 */

const RUN_KEY_PREFIX = "finresolve_session_run_v2:";
const LEGACY_RUN_KEY = "finresolve_last_run_id_v1";

export type StorageLike = Pick<Storage, "getItem" | "setItem" | "removeItem">;

export function sessionRunStorageKey(sessionId: string): string {
  return `${RUN_KEY_PREFIX}${sessionId}`;
}

export function saveSessionRunId(
  storage: StorageLike | null | undefined,
  sessionId: string,
  runId: string,
): void {
  if (!storage || !sessionId || !runId) return;
  storage.removeItem(LEGACY_RUN_KEY);
  storage.setItem(sessionRunStorageKey(sessionId), JSON.stringify({ session_id: sessionId, run_id: runId }));
}

export function loadSessionRunId(
  storage: StorageLike | null | undefined,
  sessionId: string,
): string | null {
  if (!storage || !sessionId) return null;
  storage.removeItem(LEGACY_RUN_KEY);
  const raw = storage.getItem(sessionRunStorageKey(sessionId));
  if (!raw) return null;
  try {
    const parsed: unknown = JSON.parse(raw);
    if (
      parsed &&
      typeof parsed === "object" &&
      "session_id" in parsed &&
      "run_id" in parsed &&
      (parsed as { session_id: unknown }).session_id === sessionId &&
      typeof (parsed as { run_id: unknown }).run_id === "string" &&
      (parsed as { run_id: string }).run_id.length > 0
    ) {
      return (parsed as { run_id: string }).run_id;
    }
  } catch {
    // Ignore malformed entries and clean them up
  }
  storage.removeItem(sessionRunStorageKey(sessionId));
  return null;
}

export function clearSessionRunId(
  storage: StorageLike | null | undefined,
  sessionId?: string | null,
): void {
  if (!storage) return;
  storage.removeItem(LEGACY_RUN_KEY);
  if (sessionId) {
    storage.removeItem(sessionRunStorageKey(sessionId));
  }
}

/** Pick a valid Case ID belonging to the current session's Case list; drops stale cross-session IDs. */
export function resolveSelectedCaseId(
  currentSelectedId: string | null,
  urlCaseId: string | null,
  sessionCaseIds: readonly string[],
): string | null {
  if (sessionCaseIds.length === 0) return null;
  const allowed = new Set(sessionCaseIds);
  if (currentSelectedId && allowed.has(currentSelectedId)) return currentSelectedId;
  if (urlCaseId && allowed.has(urlCaseId)) return urlCaseId;
  return sessionCaseIds[0] ?? null;
}

export type AiModeDisplay = {
  badge: string;
  note: string;
  isInvestigated: boolean;
  isLlm: boolean;
};

/** Honest Case AI reasoning label: never falls back to DEMO_RULES before investigation exists. */
export function resolveCaseAiModeLabel(modelMode: string | null | undefined): AiModeDisplay {
  const normalized = (modelMode || "").trim().toUpperCase();
  if (!normalized) {
    return {
      badge: "CHƯA ĐIỀU TRA",
      note: "Chưa chạy điều tra Agent cho Case này. Chưa có kết luận từ DEMO_RULES hay LLM_API.",
      isInvestigated: false,
      isLlm: false,
    };
  }
  if (normalized === "LLM_API") {
    return {
      badge: "LLM_API",
      note: "Đã điều tra bằng LLM API — mọi hành động vẫn do Policy, Approval và Verifier kiểm soát.",
      isInvestigated: true,
      isLlm: true,
    };
  }
  return {
    badge: "DEMO_RULES",
    note: "Chế độ luật xác định (DEMO_RULES) — không phải suy luận từ LLM.",
    isInvestigated: true,
    isLlm: false,
  };
}

export type TaskforceModeDisplay = {
  badge: string;
  note: string;
  isLlm: boolean;
};

/** Honest Taskforce label: never presents deterministic Taskforce as LLM-based reasoning. */
export function resolveTaskforceModeLabel(
  mode: string | null | undefined,
  llmInvoked: boolean,
): TaskforceModeDisplay {
  const normalized = (mode || "").trim().toUpperCase();
  if (llmInvoked && normalized === "LLM_API") {
    return {
      badge: "TASKFORCE LLM_API",
      note: "Taskforce có gọi LLM API — chỉ dùng công cụ đọc, không tự duyệt hay ghi tài chính.",
      isLlm: true,
    };
  }
  return {
    badge: normalized || "DETERMINISTIC_TASKFORCE",
    note: "Taskforce chạy thuật toán xác định (0 LLM calls) — không phải suy luận từ LLM.",
    isLlm: false,
  };
}

export type RestoredInvestigationDecision = {
  jobId: string | null;
  shouldPoll: boolean;
  notice: string | null;
};

/** Decide whether to restore active investigation polling after page refresh or Case switch.
 * Never starts a new investigation automatically and never polls terminal statuses.
 */
export function resolveRestoredInvestigationJob(
  status:
    | {
        active_run_id?: string | null;
        investigation_status?: string | null;
      }
    | null
    | undefined,
): RestoredInvestigationDecision {
  if (!status) return { jobId: null, shouldPoll: false, notice: null };
  const invStatus = (status.investigation_status || "IDLE").toUpperCase();
  const runId = typeof status.active_run_id === "string" ? status.active_run_id.trim() : "";

  if (invStatus === "RUNNING" && runId.length > 0) {
    return {
      jobId: runId,
      shouldPoll: true,
      notice: `Đang khôi phục theo dõi phiên điều tra đang chạy (${runId})…`,
    };
  }
  if (invStatus === "INTERRUPTED") {
    return {
      jobId: null,
      shouldPoll: false,
      notice:
        "Phiên điều tra trước đó bị gián đoạn (INTERRUPTED) và Case đã được khôi phục an toàn. Bạn có thể xem chi tiết hoặc chạy lại điều tra nếu được phép.",
    };
  }
  return { jobId: null, shouldPoll: false, notice: null };
}
