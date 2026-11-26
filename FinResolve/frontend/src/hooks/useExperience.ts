"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  bootstrapExperience,
  computeAdaptivePollIntervalMs,
  detectOverviewToasts,
  EXPERIENCE_TOKEN_STORAGE_KEY,
  getExperienceOverview,
  isStaleResponse,
  resetExperienceSession,
  triggerExperienceScan,
  type ExperienceBootstrapResponse,
  type ExperienceOverviewResponse,
  type ExperienceToast,
} from "@/lib/experience-api";

export type UseExperienceState = {
  loading: boolean;
  error: string | null;
  bootstrap: ExperienceBootstrapResponse | null;
  overview: ExperienceOverviewResponse | null;
  scanning: boolean;
  resetting: boolean;
  toasts: ExperienceToast[];
  dismissToast: (id: string) => void;
  refreshNow: () => Promise<void>;
  scanNow: () => Promise<void>;
  resetSandbox: () => Promise<void>;
  retryBootstrap: () => Promise<void>;
};

export function useExperience(): UseExperienceState {
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [bootstrap, setBootstrap] = useState<ExperienceBootstrapResponse | null>(null);
  const [overview, setOverview] = useState<ExperienceOverviewResponse | null>(null);
  const [scanning, setScanning] = useState(false);
  const [resetting, setResetting] = useState(false);
  const [toasts, setToasts] = useState<ExperienceToast[]>([]);

  const tokenRef = useRef<string | null>(null);
  const seqRef = useRef(0);
  const appliedSeqRef = useRef(0);
  const consecutiveErrorsRef = useRef(0);
  const pollTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pollAbortRef = useRef<AbortController | null>(null);
  const overviewRef = useRef<ExperienceOverviewResponse | null>(null);
  const mountedRef = useRef(true);

  const pushToast = useCallback((toast: ExperienceToast) => {
    setToasts((prev) => [toast, ...prev].slice(0, 4));
  }, []);

  const dismissToast = useCallback((id: string) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  const clearScheduledPoll = useCallback(() => {
    if (pollTimerRef.current !== null) {
      clearTimeout(pollTimerRef.current);
      pollTimerRef.current = null;
    }
    if (pollAbortRef.current !== null) {
      pollAbortRef.current.abort();
      pollAbortRef.current = null;
    }
  }, []);

  const fetchOverviewOnce = useCallback(
    async (token: string): Promise<ExperienceOverviewResponse | null> => {
      if (pollAbortRef.current) {
        pollAbortRef.current.abort();
      }
      const controller = new AbortController();
      pollAbortRef.current = controller;
      const mySeq = ++seqRef.current;

      try {
        const data = await getExperienceOverview(token, controller.signal);
        if (!mountedRef.current || isStaleResponse(mySeq, appliedSeqRef.current)) {
          return null;
        }
        appliedSeqRef.current = mySeq;
        consecutiveErrorsRef.current = 0;

        const newToasts = detectOverviewToasts(overviewRef.current, data);
        if (newToasts.length > 0) {
          setToasts((prev) => [...newToasts, ...prev].slice(0, 4));
        }

        overviewRef.current = data;
        setOverview(data);
        setError(null);
        return data;
      } catch (err) {
        if (controller.signal.aborted || !mountedRef.current) {
          return null;
        }
        consecutiveErrorsRef.current += 1;
        const msg =
          err instanceof Error ? err.message : "Không thể tải dữ liệu từ FinResolve Backend.";
        setError(msg);
        return null;
      }
    },
    [],
  );

  const scheduleNextPoll = useCallback(
    (token: string, latest: ExperienceOverviewResponse | null) => {
      const enqueue = (t: string, snap: ExperienceOverviewResponse | null) => {
        if (!mountedRef.current) return;
        if (typeof document !== "undefined" && document.visibilityState === "hidden") {
          return;
        }
        if (pollTimerRef.current !== null) {
          clearTimeout(pollTimerRef.current);
        }
        const delay = computeAdaptivePollIntervalMs(
          snap?.guardian.visual_state ?? null,
          snap?.guardian.queued_jobs ?? 0,
          consecutiveErrorsRef.current,
        );
        pollTimerRef.current = setTimeout(async () => {
          if (!mountedRef.current || tokenRef.current !== t) return;
          const nextData = await fetchOverviewOnce(t);
          if (mountedRef.current && tokenRef.current === t) {
            enqueue(t, nextData ?? overviewRef.current);
          }
        }, delay);
      };
      enqueue(token, latest);
    },
    [fetchOverviewOnce],
  );

  const executeBootstrap = useCallback(async () => {
    clearScheduledPoll();
    const controller = new AbortController();
    pollAbortRef.current = controller;

    try {
      const savedToken =
        typeof window !== "undefined"
          ? window.sessionStorage.getItem(EXPERIENCE_TOKEN_STORAGE_KEY)
          : null;

      const boot = await bootstrapExperience(savedToken, controller.signal);
      if (!mountedRef.current) return;

      tokenRef.current = boot.session.token;
      if (typeof window !== "undefined") {
        window.sessionStorage.setItem(EXPERIENCE_TOKEN_STORAGE_KEY, boot.session.token);
      }
      setBootstrap(boot);

      const firstOverview = await fetchOverviewOnce(boot.session.token);
      if (!mountedRef.current) return;
      setLoading(false);
      scheduleNextPoll(boot.session.token, firstOverview);
    } catch (err) {
      if (controller.signal.aborted || !mountedRef.current) return;
      const msg =
        err instanceof Error ? err.message : "Không thể khởi tạo phiên trải nghiệm FinResolve.";
      setError(msg);
      setLoading(false);
    }
  }, [clearScheduledPoll, fetchOverviewOnce, scheduleNextPoll]);

  const retryBootstrap = useCallback(async () => {
    setLoading(true);
    setError(null);
    await executeBootstrap();
  }, [executeBootstrap]);

  useEffect(() => {
    mountedRef.current = true;
    const initTimer = setTimeout(() => {
      void executeBootstrap();
    }, 0);

    const onVisibilityChange = () => {
      const token = tokenRef.current;
      if (!token) return;
      if (document.visibilityState === "hidden") {
        if (pollTimerRef.current !== null) {
          clearTimeout(pollTimerRef.current);
          pollTimerRef.current = null;
        }
      } else {
        void fetchOverviewOnce(token).then((data) => {
          if (mountedRef.current && tokenRef.current === token) {
            scheduleNextPoll(token, data ?? overviewRef.current);
          }
        });
      }
    };

    if (typeof document !== "undefined") {
      document.addEventListener("visibilitychange", onVisibilityChange);
    }

    return () => {
      mountedRef.current = false;
      clearTimeout(initTimer);
      clearScheduledPoll();
      if (typeof document !== "undefined") {
        document.removeEventListener("visibilitychange", onVisibilityChange);
      }
    };
  }, [clearScheduledPoll, executeBootstrap, fetchOverviewOnce, scheduleNextPoll]);

  const refreshNow = useCallback(async () => {
    const token = tokenRef.current;
    if (!token) return;
    const data = await fetchOverviewOnce(token);
    if (mountedRef.current && tokenRef.current === token) {
      scheduleNextPoll(token, data ?? overviewRef.current);
    }
  }, [fetchOverviewOnce, scheduleNextPoll]);

  const scanNow = useCallback(async () => {
    const token = tokenRef.current;
    if (!token || scanning) return;
    setScanning(true);
    try {
      const res = await triggerExperienceScan(token);
      if (!mountedRef.current) return;
      if (!res.scanned) {
        pushToast({
          id: `scan-${Date.now()}`,
          tone: "warning",
          title: "Chưa kết nối Commerce Sandbox",
          detail: "Backend chưa cấu hình Commerce binding nên chưa thể quét dữ liệu.",
        });
      } else if (res.error) {
        pushToast({
          id: `scan-${Date.now()}`,
          tone: "warning",
          title: "Quét hoàn tất kèm cảnh báo kết nối",
          detail: res.error,
        });
      } else {
        pushToast({
          id: `scan-${Date.now()}`,
          tone: "info",
          title: "Đã hoàn tất lượt quan sát mới",
          detail: `Kéo ${res.events_pulled ?? 0} sự kiện, phát hiện ${res.anomalies_detected ?? 0} bất thường.`,
        });
      }
      await refreshNow();
    } catch (err) {
      if (!mountedRef.current) return;
      pushToast({
        id: `scan-err-${Date.now()}`,
        tone: "warning",
        title: "Không thể kích hoạt lượt quét",
        detail: err instanceof Error ? err.message : "Lỗi kết nối API.",
      });
    } finally {
      if (mountedRef.current) setScanning(false);
    }
  }, [pushToast, refreshNow, scanning]);

  const resetSandbox = useCallback(async () => {
    const token = tokenRef.current;
    if (!token || resetting) return;
    setResetting(true);
    try {
      const res = await resetExperienceSession(token);
      if (!mountedRef.current) return;
      pushToast({
        id: `reset-${Date.now()}`,
        tone: "info",
        title: `Đã làm mới phiên (Epoch #${res.world_epoch})`,
        detail: "Phạm vi dữ liệu của riêng phiên này đã được làm sạch an toàn.",
      });
      await refreshNow();
    } catch (err) {
      if (!mountedRef.current) return;
      pushToast({
        id: `reset-err-${Date.now()}`,
        tone: "warning",
        title: "Không thể reset phiên",
        detail: err instanceof Error ? err.message : "Lỗi kết nối API.",
      });
    } finally {
      if (mountedRef.current) setResetting(false);
    }
  }, [pushToast, refreshNow, resetting]);

  return {
    loading,
    error,
    bootstrap,
    overview,
    scanning,
    resetting,
    toasts,
    dismissToast,
    refreshNow,
    scanNow,
    resetSandbox,
    retryBootstrap,
  };
}
