"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState, type FormEvent } from "react";
import { ApiHealthPanel } from "@/components/ApiHealth";
import { MockBadge } from "@/components/MockBadge";
import { Panel, StatusPill, primaryButton, secondaryButton } from "@/components/StatusPill";
import {
  apiRequest, caseUrl, readableError, runKey,
  type CaseRecord, type OracleReport, type ScenarioItem, type ScenarioRun,
} from "@/lib/api";
import { useDemoSession } from "@/lib/useDemoSession";

const EXPECTATIONS: Record<string, string> = {
  payment_sync: "Đọc bằng chứng → đề xuất đồng bộ → phê duyệt MOCK → xác minh.",
  refund_risk: "Giữ yêu cầu hoàn trùng; cho phép hoàn một phần hợp lệ qua Policy.",
  mega_live: "50–100 đơn, gom Incident theo nguyên nhân và điều tra một Case ngoại lệ.",
};
const demoIds = ["payment_sync", "refund_risk", "mega_live"] as const;
const running = new Set(["RUNNING"]);

export function DemoDashboard() {
  const { session, loading: sessionLoading, error: sessionError, reload } = useDemoSession();
  const router = useRouter();
  const [catalog, setCatalog] = useState<ScenarioItem[]>([]);
  const [run, setRun] = useState<ScenarioRun | null>(null);
  const [report, setReport] = useState<OracleReport | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [claim, setClaim] = useState("");
  const [orderCount, setOrderCount] = useState(60);
  const [seed, setSeed] = useState(20261122);

  useEffect(() => {
    let active = true;
    void apiRequest<ScenarioItem[]>("/api/v1/demo/scenarios")
      .then((items) => { if (active) setCatalog(items); })
      .catch((e: unknown) => { if (active) setError(readableError(e)); });
    return () => { active = false; };
  }, []);

  useEffect(() => {
    if (!session) return;
    const lastId = window.sessionStorage.getItem(runKey);
    if (!lastId) return;
    let active = true;
    void apiRequest<ScenarioRun>(`/api/v1/demo/runs/${encodeURIComponent(lastId)}/status`, { session })
      .then((found) => { if (active) setRun(found); })
      .catch(() => {
        if (active) window.sessionStorage.removeItem(runKey);
      });
    return () => { active = false; };
  }, [session]);

  const runId = run?.run_id;
  const runStatus = run?.status;
  useEffect(() => {
    if (!session || !runId) return;
    let cancelled = false;
    const poll = async () => {
      try {
        const updated = await apiRequest<ScenarioRun>(`/api/v1/demo/runs/${encodeURIComponent(runId)}/status`, { session });
        if (!cancelled) setRun(updated);
      } catch (e) { if (!cancelled) setError(readableError(e)); }
    };
    if (running.has(runStatus || "")) {
      const timer = window.setInterval(() => { void poll(); }, 2400);
      return () => { cancelled = true; window.clearInterval(timer); };
    }
    void apiRequest<OracleReport>(`/api/v1/demo/runs/${encodeURIComponent(runId)}/report`, { session })
      .then((value) => { if (!cancelled) setReport(value); })
      .catch((e: unknown) => { if (!cancelled) setError(readableError(e)); });
    return () => { cancelled = true; };
  }, [runId, runStatus, session]);

  async function runScenario(id: (typeof demoIds)[number]) {
    if (!session || busy) return;
    setBusy(true); setError(null); setMessage(null); setReport(null); setRun(null);
    try {
      const next = await apiRequest<ScenarioRun>(`/api/v1/demo/scenarios/${id}/run`, {
        session, method: "POST",
        body: {
          seed,
          ...(id === "mega_live" ? { order_count: orderCount } : {}),
          run_async: true,
          reset_before_run: true,
          investigate_selected_exception: id === "mega_live",
        },
      });
      window.sessionStorage.setItem(runKey, next.run_id);
      setRun(next);
      setMessage("Đã khởi tạo kịch bản MOCK. Trạng thái sẽ được cập nhật từ backend.");
    } catch (e) { setError(readableError(e)); }
    finally { setBusy(false); }
  }

  async function createComplaint(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!session || busy || claim.trim().length < 3) return;
    setBusy(true); setError(null);
    try {
      const created = await apiRequest<CaseRecord>("/api/v1/cases", {
        session, method: "POST",
        idempotencyKey: `WEB-CASE-${crypto.randomUUID()}`,
        body: { text: claim.trim(), type: "PAYMENT_DISPUTE" },
      });
      router.push(caseUrl(created.id));
    } catch (e) { setError(readableError(e)); }
    finally { setBusy(false); }
  }

  async function resetWorld() {
    if (!session || busy || !window.confirm("Xóa dữ liệu mô phỏng của phiên hiện tại? Các run cũ sẽ được đánh dấu SUPERSEDED.")) return;
    setBusy(true); setError(null);
    try {
      await apiRequest("/api/v1/demo/sessions/reset", { session, method: "POST" });
      window.sessionStorage.removeItem(runKey);
      setRun(null); setReport(null); setMessage("Đã reset dữ liệu MOCK cho phiên hiện tại.");
    } catch (e) { setError(readableError(e)); }
    finally { setBusy(false); }
  }

  return <div className="space-y-7">
    <section className="rounded-3xl bg-gradient-to-br from-indigo-950 to-indigo-700 px-7 py-10 text-white sm:px-10">
      <div className="mb-4 flex flex-wrap items-center gap-3"><MockBadge /><span className="text-sm text-indigo-100">AI-Native · Agentic investigation · MOCK</span></div>
      <h1 className="max-w-3xl text-3xl font-bold tracking-tight sm:text-4xl">Từ khiếu nại đến kết quả được xác minh</h1>
      <p className="mt-4 max-w-3xl text-indigo-100">FinResolve đọc dữ liệu giao dịch mô phỏng, điều tra bằng chứng, áp dụng chính sách trước mọi thay đổi và kiểm tra kết quả sau thực thi. CLAIM không bao giờ được coi là xác nhận thanh toán.</p>
    </section>
    <ApiHealthPanel />
    <section className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-slate-200 bg-white p-4 text-sm">
      <div>{sessionLoading ? "Đang tạo phiên khách…" : session ? <><strong>Phiên khách đã sẵn sàng</strong> · tối đa {session.max_cases} Case · hết hạn: {new Date(session.expires_at).toLocaleTimeString("vi-VN")}</> : "Không tạo được phiên khách"}</div>
      <button className={secondaryButton} type="button" onClick={() => void reload(true)} disabled={sessionLoading || busy}>Tạo phiên mới</button>
    </section>
    {sessionError && <p role="alert" className="rounded-lg bg-rose-50 p-3 text-sm text-rose-700">{sessionError}</p>}
    {error && <p role="alert" className="rounded-lg bg-rose-50 p-3 text-sm text-rose-700">{error}</p>}
    {message && <p role="status" className="rounded-lg bg-emerald-50 p-3 text-sm text-emerald-800">{message}</p>}

    <section aria-labelledby="scenarios-heading" className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div><h2 id="scenarios-heading" className="text-xl font-bold">Kịch bản thực thi</h2><p className="text-sm text-slate-600">Đọc kết quả trực tiếp từ PostgreSQL MOCK, không dựng dữ liệu trên web.</p></div>
        <button className={secondaryButton} onClick={() => void resetWorld()} disabled={!session || busy}>Reset phiên MOCK</button>
      </div>
      <div className="flex flex-wrap items-end gap-4 rounded-xl border border-slate-200 bg-white p-4">
        <label className="flex flex-col gap-1 text-sm font-medium">Seed tái lập
          <input className="rounded-lg border border-slate-300 p-2" type="number" min="1" max="2147483647" value={seed} onChange={(e) => setSeed(Number(e.target.value))} />
        </label>
        <label className="flex flex-col gap-1 text-sm font-medium">Số đơn Mega Live
          <select className="rounded-lg border border-slate-300 p-2" value={orderCount} onChange={(e) => setOrderCount(Number(e.target.value))}>
            <option value={50}>50 đơn</option><option value={60}>60 đơn</option><option value={100}>100 đơn</option>
          </select>
        </label>
        <span className="text-xs text-slate-500">Mỗi lần chạy sẽ reset world hiện tại; lịch sử được đánh dấu SUPERSEDED.</span>
      </div>
      <ul className="grid gap-4 md:grid-cols-3">
        {demoIds.map((id) => {
          const item = catalog.find((s) => s.scenario_id === id);
          return <li key={id} className="flex flex-col rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
            <div className="mb-3 flex justify-between"><code className="rounded bg-slate-100 px-2 py-1 text-xs">{id}</code><MockBadge compact /></div>
            <h3 className="font-semibold">{item?.title_vi || id}</h3>
            <p className="mt-2 flex-1 text-sm leading-relaxed text-slate-600">{item?.description_vi || "Đang tải kịch bản…"}</p>
            <p className="mt-3 text-xs text-indigo-800">{EXPECTATIONS[id]}</p>
            <button className={`${primaryButton} mt-5`} type="button" disabled={!session || busy || running.has(run?.status || "") || !item || !Number.isInteger(seed) || seed < 1} onClick={() => void runScenario(id)}>{busy ? "Đang gửi…" : "Chạy kịch bản"}</button>
          </li>;
        })}
      </ul>
    </section>

    {run && <Panel title="Kết quả kịch bản hiện tại">
      <div className="flex flex-wrap items-center gap-3"><code className="break-all text-xs">{run.run_id}</code><StatusPill value={run.status} /><span className="text-sm">{run.orders_generated} đơn · {run.case_ids.length} Case · {run.incident_ids.length} Incident</span></div>
      {run.status === "RUNNING" && <div className="mt-4"><div className="mb-1 text-xs">Đang xử lý… {run.progress_pct}%</div><progress className="w-full" max={100} value={run.progress_pct} /></div>}
      {run.error_message && <p role="alert" className="mt-3 text-sm text-rose-700">{run.error_message}</p>}
      <div className="mt-4 flex flex-wrap gap-2">
        {run.case_ids.slice(0, 8).map((id) => <Link key={id} href={caseUrl(id)} className="rounded-lg border border-slate-200 px-2.5 py-1.5 text-xs text-indigo-800 hover:bg-indigo-50">{id}</Link>)}
        {run.case_ids.length > 8 && <Link className="text-sm text-indigo-700 underline" href="/cases">Xem tất cả</Link>}
        {run.incident_ids.length > 0 && <Link href="/live" className="text-sm font-semibold text-indigo-700 underline">Mở Mega Live & Taskforce</Link>}
      </div>
      {report && <div className="mt-5 grid gap-3 border-t border-slate-200 pt-4 sm:grid-cols-3">
        <div><p className="mb-1 text-xs text-slate-500">Kiểm tra an toàn</p><StatusPill value={report.safety_verdict} /></div>
        <div><p className="mb-1 text-xs text-slate-500">Tiến độ xử lý</p><StatusPill value={report.resolution_status} /></div>
        <div><p className="mb-1 text-xs text-slate-500">Kết luận tổng hợp</p><StatusPill value={report.verdict} /></div>
        <p className="text-sm text-slate-600">Case cần xử lý: <strong>{report.expected_actionable_count}</strong></p>
        <p className="text-sm text-slate-600">Đã verify: <strong>{report.verified_resolved_count}</strong></p>
        <p className="text-sm text-slate-600">Đơn lệch trạng thái: <strong>{report.mismatched_count}</strong></p>
        {report.unsafe_effect_details.length > 0 && <ul className="col-span-full list-disc pl-5 text-sm text-rose-700">{report.unsafe_effect_details.map((s, i) => <li key={i}>{s}</li>)}</ul>}
        <p className="col-span-full text-xs text-slate-500">INCOMPLETE = chưa xử lý xong, không phải đã thành công. Oracle chỉ chấm dữ liệu MOCK.</p>
      </div>}
    </Panel>}

    <Panel title="Tạo khiếu nại tiếng Việt">
      <form onSubmit={(e) => void createComplaint(e)} className="space-y-3">
        <label htmlFor="claim" className="block text-sm text-slate-700">Nhập nội dung cần AI điều tra (được lưu là CLAIM, không phải bằng chứng thanh toán).</label>
        <textarea id="claim" className="min-h-28 w-full rounded-lg border border-slate-300 p-3 text-sm" minLength={3} maxLength={2000} value={claim} onChange={(e) => setClaim(e.target.value)} placeholder="Tôi đã thanh toán qua MoMo nhưng đơn hàng vẫn chờ xử lý…" />
        <button className={primaryButton} type="submit" disabled={!session || busy || claim.trim().length < 3}>Tạo Case và xem chi tiết</button>
      </form>
    </Panel>
    <p className="text-xs text-slate-500">Không có giao dịch tiền thật. Nếu chưa cấu hình model key, chế độ DEMO_RULES là luật xác định và được ghi nhãn rõ.</p>
  </div>;
}
