"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { MockBadge } from "@/components/MockBadge";
import { Panel, StatusPill, Timestamp, secondaryButton } from "@/components/StatusPill";
import { apiRequest, caseUrl, readableError, type RiskCenter } from "@/lib/api";
import { useDemoSession } from "@/lib/useDemoSession";

export function RiskWorkspace() {
  const { session, loading, error: sessionError, reload: reloadSession } = useDemoSession();
  const activeSessionIdRef = useRef<string | null>(null);
  const [data, setData] = useState<RiskCenter | null>(null);
  const [filter, setFilter] = useState("ALL");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const sessionId = session?.session_id ?? null;

  const load = useCallback(async () => {
    if (!session || !sessionId) return;
    const reqSessionId = sessionId;
    setBusy(true);
    try {
      const next = await apiRequest<RiskCenter>("/api/v1/risks", { session });
      if (activeSessionIdRef.current !== reqSessionId) return;
      setData(next);
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
    void apiRequest<RiskCenter>("/api/v1/risks", { session })
      .then((res) => {
        if (active && activeSessionIdRef.current === reqSessionId) {
          setData(res);
          setError(null);
        }
      })
      .catch((e: unknown) => {
        if (active && activeSessionIdRef.current === reqSessionId) setError(readableError(e));
      });
    return () => {
      active = false;
    };
  }, [session, sessionId]);

  async function handleNewSession() {
    if (loading || busy) return;
    activeSessionIdRef.current = null;
    setData(null);
    setError(null);
    await reloadSession(true);
  }

  const visible = (data?.items ?? []).filter((s) => filter === "ALL" || s.response === filter);
  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <MockBadge compact />
          <h1 className="mt-3 text-3xl font-bold">Risk Center</h1>
          <p className="mt-2 max-w-2xl text-sm text-slate-600">
            Đối chiếu hoàn tiền và các tín hiệu REVIEW/HOLD_ACTION. Tín hiệu rủi ro không phải cáo
            buộc gian lận.
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button
            className={secondaryButton}
            onClick={() => void load()}
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
          Đang tạo phiên khách…
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
      <div className="grid gap-3 sm:grid-cols-4">
        {[
          ["Tổng tín hiệu", data?.total_count ?? 0],
          ["HOLD đang hoạt động", data?.active_hold_action_count ?? 0],
          ["REVIEW", data?.review_count ?? 0],
          ["Đã kết thúc HOLD", data?.cleared_count ?? 0],
        ].map(([label, n]) => (
          <div key={label} className="rounded-xl border border-slate-200 bg-white p-5">
            <p className="text-xs text-slate-500">{label}</p>
            <strong className="mt-2 block text-3xl">{n}</strong>
          </div>
        ))}
      </div>
      <Panel title="Tín hiệu rủi ro thực từ API">
        <div className="mb-4 flex flex-wrap items-center gap-3">
          <label htmlFor="risk-filter" className="text-sm font-medium">
            Lọc phản hồi
          </label>
          <select
            id="risk-filter"
            className="rounded-lg border border-slate-300 px-3 py-2 text-sm"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          >
            <option value="ALL">Tất cả</option>
            <option value="HOLD_ACTION">HOLD_ACTION</option>
            <option value="REVIEW">REVIEW</option>
            <option value="OBSERVE">OBSERVE</option>
          </select>
        </div>
        {visible.length === 0 && (
          <p className="text-sm text-slate-500">
            Không có tín hiệu phù hợp trong phiên này.{" "}
            <Link href="/demo" className="text-indigo-700 underline">
              Chạy kịch bản refund_risk
            </Link>
            .
          </p>
        )}
        <div className="space-y-3">
          {visible.map((s) => (
            <article key={s.id} className="rounded-xl border border-slate-200 p-4">
              <div className="flex flex-wrap items-center gap-2">
                <StatusPill value={s.response} />
                <StatusPill value={s.lifecycle_status} />
                <span className="text-xs text-slate-500">
                  {s.category} · {s.level}
                </span>
              </div>
              <div className="mt-2 flex flex-wrap items-center gap-2">
                <span className="text-sm font-semibold">{s.rule_code || "RISK_SIGNAL"}</span>
                <Link className="break-all text-xs text-indigo-700 underline" href={caseUrl(s.case_id)}>
                  {s.case_id}
                </Link>
              </div>
              <p className="mt-2 text-sm text-slate-700">
                {s.explanation_vi || "Xem chi tiết bằng chứng trước khi quyết định."}
              </p>
              <p className="mt-2 text-xs text-slate-500">
                Ghi nhận: <Timestamp value={s.created_at} /> · Nguồn:{" "}
                {s.source_refs.join(", ") || "Chưa đủ nguồn"}
              </p>
              <details className="mt-2 text-xs">
                <summary className="cursor-pointer text-indigo-700">
                  Chi tiết quy tắc và chứng cứ
                </summary>
                <pre className="mt-2 overflow-x-auto whitespace-pre-wrap break-words rounded bg-slate-50 p-3">
                  {JSON.stringify(s.details_json, null, 2)}
                </pre>
              </details>
            </article>
          ))}
        </div>
      </Panel>
      <Panel title="Các nguyên tắc được áp dụng">
        <ul className="grid gap-3 md:grid-cols-2">
          {(data?.rules_catalog || []).map((rule) => (
            <li key={rule.rule_code} className="rounded-lg border border-slate-200 p-3">
              <code className="text-xs text-indigo-700">{rule.rule_code}</code>
              <p className="mt-1 text-sm text-slate-600">{rule.description_vi}</p>
            </li>
          ))}
        </ul>
        <p className="mt-4 text-xs text-slate-500">
          Không cho phép xóa HOLD_ACTION từ trang công khai: thao tác này cần quyền Manager thật,
          được kiểm tra ở backend.
        </p>
      </Panel>
    </div>
  );
}
