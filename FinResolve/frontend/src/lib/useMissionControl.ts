"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  apiRequest,
  readableError,
  type ActionApproval,
  type ApprovalInboxItem,
  type CaseRecord,
  type ChallengeCatalogItem,
  type ChallengeRunRecord,
  type JsonObject,
  type MissionControlOverview,
  type RecentVerificationItem,
  type SupervisorActivityItem,
  type SupervisorJobSummary,
  type SupervisorStatus,
} from "@/lib/api";
import { clearSessionRunId, mergeSupervisorActivities } from "@/lib/session-state";
import { useDemoSession } from "@/lib/useDemoSession";

export function useMissionControl() {
  const { session, loading: sessionLoading, error: sessionError, reload } = useDemoSession();
  const activeSessionIdRef = useRef<string | null>(null);
  const latestSeqRef = useRef<number>(0);
  const worldEpochRef = useRef<number>(1);

  const [catalog, setCatalog] = useState<ChallengeCatalogItem[]>([]);
  const [supervisor, setSupervisor] = useState<SupervisorStatus | null>(null);
  const [activities, setActivities] = useState<SupervisorActivityItem[]>([]);
  const [activeJobs, setActiveJobs] = useState<SupervisorJobSummary[]>([]);
  const [selectedChallenge, setSelectedChallenge] = useState<ChallengeRunRecord | null>(null);
  const [recentChallenges, setRecentChallenges] = useState<ChallengeRunRecord[]>([]);
  const [approvalInbox, setApprovalInbox] = useState<ApprovalInboxItem[]>([]);
  const [recentVerifications, setRecentVerifications] = useState<RecentVerificationItem[]>([]);

  const [busy, setBusy] = useState<boolean>(false);
  const [approvingId, setApprovingId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    void apiRequest<ChallengeCatalogItem[]>("/api/v1/challenges/catalog")
      .then((items) => {
        if (active) setCatalog(items);
      })
      .catch((e: unknown) => {
        if (active) setError(readableError(e));
      });
    return () => {
      active = false;
    };
  }, []);

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
        setActiveJobs(data.active_jobs ?? []);
        setApprovalInbox(data.approval_inbox);
        setRecentVerifications(data.recent_verifications ?? []);
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

  const sessionId = session?.session_id ?? null;
  useEffect(() => {
    activeSessionIdRef.current = sessionId;
    latestSeqRef.current = 0;
    worldEpochRef.current = 1;
    if (session && sessionId) {
      void loadOverview(false);
    }
  }, [session, sessionId, loadOverview]);

  const isBusyMonitoring =
    supervisor?.status === "INVESTIGATING" ||
    (supervisor?.queue_depth ?? 0) > 0 ||
    activeJobs.some((j) => j.status === "QUEUED" || j.status === "RUNNING" || j.status === "CANCELLING");

  useEffect(() => {
    if (!session || !sessionId) return;
    const intervalMs = isBusyMonitoring ? 1500 : 6000;
    const timer = window.setInterval(() => {
      void loadOverview(true);
    }, intervalMs);
    return () => {
      window.clearInterval(timer);
    };
  }, [session, sessionId, isBusyMonitoring, loadOverview]);

  const toggleAutopilot = async () => {
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
          ? "Đã tạm dừng AI Supervisor. Tác vụ mới sẽ chờ trong hàng đợi PostgreSQL."
          : "Đã bật lại AI Supervisor và đánh thức Worker nền xử lý hàng đợi.",
      );
    } catch (e) {
      setError(readableError(e));
    } finally {
      setBusy(false);
    }
  };

  const resetWorld = async () => {
    if (!session) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await apiRequest("/api/v1/demo/sessions/reset", { session, method: "POST" });
      clearSessionRunId(window.sessionStorage, session.session_id);
      latestSeqRef.current = 0;
      await loadOverview(false);
      setNotice("Đã đặt lại môi trường MOCK sang world_epoch mới. Các tác vụ cũ đã bị hủy hiệu lực.");
    } catch (e) {
      setError(readableError(e));
    } finally {
      setBusy(false);
    }
  };

  const createNewSession = () => {
    setSupervisor(null);
    setActivities([]);
    setActiveJobs([]);
    setSelectedChallenge(null);
    setRecentChallenges([]);
    setApprovalInbox([]);
    setRecentVerifications([]);
    setError(null);
    setNotice(null);
    void reload(true);
  };

  const launchChallenge = async (
    challengeType: string,
    presetId: string,
    parameters: JsonObject,
  ) => {
    if (!session) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const launched = await apiRequest<ChallengeRunRecord>("/api/v1/challenges", {
        session,
        method: "POST",
        body: {
          challenge_type: challengeType,
          preset_id: presetId,
          parameters,
        },
      });
      setSelectedChallenge(launched);
      await loadOverview(false);
      setNotice(
        supervisor?.worker_healthy
          ? `Đã tiếp nhận (202 Accepted) thử thách "${launched.title_vi}" — Supervisor Worker đang xử lý bất đồng bộ.`
          : `Đã xếp hàng đợi thử thách "${launched.title_vi}" trong PostgreSQL (lưu ý: cần bật Supervisor Worker nền để xử lý tự động).`,
      );
    } catch (e) {
      setError(readableError(e));
    } finally {
      setBusy(false);
    }
  };

  const cancelChallenge = async (challengeId: string) => {
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
      setNotice("Đã gửi yêu cầu dừng hợp tác cho thử thách và các tác vụ liên quan.");
    } catch (e) {
      setError(readableError(e));
    } finally {
      setBusy(false);
    }
  };

  const cancelJob = async (jobId: string) => {
    if (!session) return;
    setBusy(true);
    setError(null);
    try {
      await apiRequest<SupervisorJobSummary>(
        `/api/v1/supervisor/jobs/${encodeURIComponent(jobId)}/cancel`,
        { session, method: "POST" },
      );
      await loadOverview(false);
      setNotice(`Đã gửi yêu cầu dừng hợp tác cho tác vụ ${jobId}.`);
    } catch (e) {
      setError(readableError(e));
    } finally {
      setBusy(false);
    }
  };

  const submitFreeTextCase = async (text: string) => {
    if (!session || !text.trim()) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const created = await apiRequest<CaseRecord>("/api/v1/cases", {
        session,
        method: "POST",
        body: { text: text.trim(), type: "PAYMENT_DISPUTE" },
      });
      await loadOverview(false);
      setNotice(
        `Đã tạo hồ sơ ${created.id} và xếp hàng đợi điều tra tự động cho Supervisor Worker.`,
      );
    } catch (err) {
      setError(readableError(err));
    } finally {
      setBusy(false);
    }
  };

  const approveProposal = async (item: ApprovalInboxItem) => {
    if (!session) return;
    setApprovingId(item.proposal_id);
    setError(null);
    setNotice(null);
    try {
      // Single explicit approval action: backend durably enqueues automatic execution + Verifier!
      await apiRequest<ActionApproval>(
        `/api/v1/actions/${encodeURIComponent(item.proposal_id)}/simulate-demo-approval`,
        {
          session,
          method: "POST",
          body: {
            reason: "Phê duyệt tường minh từ Hộp thư phê duyệt AI Mission Control (MOCK)",
            expiry_minutes: 60,
          },
        },
      );
      await loadOverview(false);
      setNotice(
        `Đã ghi nhận phê duyệt ${item.action_label_vi}. Backend đã tự động xếp hàng đợi thực thi & chạy Verifier kiểm chứng hậu điều kiện.`,
      );
    } catch (e) {
      setError(readableError(e));
    } finally {
      setApprovingId(null);
    }
  };

  const rejectProposal = async (item: ApprovalInboxItem) => {
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

  return {
    session,
    sessionLoading,
    sessionError,
    catalog,
    supervisor,
    activities,
    activeJobs,
    selectedChallenge,
    setSelectedChallenge,
    recentChallenges,
    approvalInbox,
    recentVerifications,
    busy,
    approvingId,
    error,
    setError,
    notice,
    isBusyMonitoring,
    toggleAutopilot,
    resetWorld,
    createNewSession,
    launchChallenge,
    cancelChallenge,
    cancelJob,
    submitFreeTextCase,
    approveProposal,
    rejectProposal,
  };
}
