"use client";

import Link from "next/link";
import { useCallback, useState, type FormEvent } from "react";
import { ActivityTimelinePanel } from "@/components/mission/ActivityTimelinePanel";
import { ApprovalInboxPanel } from "@/components/mission/ApprovalInboxPanel";
import { ContinuousMonitorPanel } from "@/components/mission/ContinuousMonitorPanel";
import { SupervisorStatusPanel } from "@/components/mission/SupervisorStatusPanel";
import { Panel, StatusPill, primaryButton, secondaryButton } from "@/components/StatusPill";
import {
  apiRequest,
  caseUrl,
  readableError,
  type ChallengeCatalogItem,
  type JsonObject,
} from "@/lib/api";
import { useMissionControl } from "@/lib/useMissionControl";

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

const SAMPLE_MOCK_RECEIPT_TEXT = `=== BIÊN LAI THANH TOÁN ĐIỆN TỬ (MOCK / TEST SAMPLE) ===
Đơn vị thụ hưởng: FinResolve Demo Store
Mã đơn hàng: ORD-RCPT-DEMO01
Mã giao dịch: PAY-MOMO-GHOST-991
Số tiền: 450000 VND
Trạng thái hiển thị: THÀNH CÔNG
Ghi chú: Mẫu văn bản có cấu trúc đóng dấu MOCK / TEST (không phải ảnh OCR).`;

function formatMoneyVnd(val: unknown): string {
  const num = typeof val === "number" ? val : Number(val);
  if (!Number.isFinite(num)) return String(val ?? "—");
  return `${num.toLocaleString("vi-VN")} ₫`;
}

export function DemoDashboard() {
  const mc = useMissionControl();

  const [activeType, setActiveType] = useState<string>("SUSPICIOUS_RECEIPT");
  const [activePresetId, setActivePresetId] = useState<string>("NO_LEDGER_TX");
  const [claimedPaymentId, setClaimedPaymentId] = useState<string>("PAY-MOMO-GHOST-991");
  const [claimedAmount, setClaimedAmount] = useState<number>(450000);
  const [receiptNote, setReceiptNote] = useState<string>(
    "Đã chuyển khoản MoMo thành công 450.000đ, đề nghị xác nhận đơn ngay.",
  );
  const [sampleReceiptRaw, setSampleReceiptRaw] = useState<string>(SAMPLE_MOCK_RECEIPT_TEXT);
  const [extractedReceiptNotice, setExtractedReceiptNotice] = useState<string | null>(null);
  const [burstEvents, setBurstEvents] = useState<number>(90);
  const [burstDuration, setBurstDuration] = useState<number>(8);
  const [freeClaimText, setFreeClaimText] = useState<string>("");

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

  const handleSelectChallengeType = (cType: string) => {
    setActiveType(cType);
    const cat = mc.catalog.find((x) => x.challenge_type === cType);
    const firstPreset = cat?.presets[0]?.preset_id ?? "";
    setActivePresetId(firstPreset);
    syncPresetDefaults(cType, firstPreset, mc.catalog);
    setExtractedReceiptNotice(null);
  };

  const handleSelectPreset = (pId: string) => {
    setActivePresetId(pId);
    syncPresetDefaults(activeType, pId, mc.catalog);
  };

  const handleExtractMockReceipt = async () => {
    if (!mc.session) return;
    try {
      const res = await apiRequest<{
        claimed_payment_id: string;
        claimed_amount: number;
        notice_vi: string;
      }>("/api/v1/challenges/receipt-extract", {
        session: mc.session,
        method: "POST",
        body: { raw_content: sampleReceiptRaw, filename: "mock_receipt_sample.txt" },
      });
      setClaimedPaymentId(res.claimed_payment_id);
      setClaimedAmount(res.claimed_amount);
      setExtractedReceiptNotice(res.notice_vi);
    } catch (e) {
      mc.setError(readableError(e));
    }
  };

  const handleLaunch = () => {
    const parameters: JsonObject = {};
    if (activeType === "SUSPICIOUS_RECEIPT") {
      parameters.claimed_payment_id = claimedPaymentId.trim() || "PAY-MOMO-GHOST-991";
      parameters.claimed_amount = Number(claimedAmount) || 450000;
      parameters.receipt_note = receiptNote.trim();
    } else if (activeType === "TRAFFIC_BURST") {
      parameters.event_count = Math.min(120, Math.max(1, Number(burstEvents) || 45));
      parameters.duration_seconds = Math.min(30, Math.max(1, Number(burstDuration) || 5));
    }
    void mc.launchChallenge(activeType, activePresetId, parameters);
  };

  const handleFreeTextSubmit = (e: FormEvent) => {
    e.preventDefault();
    if (!freeClaimText.trim()) return;
    void mc.submitFreeTextCase(freeClaimText);
    setFreeClaimText("");
  };

  const activeCatalogItem = mc.catalog.find((c) => c.challenge_type === activeType);
  const primaryCatalog = mc.catalog.filter((c) =>
    (PRIMARY_CHALLENGES as readonly string[]).includes(c.challenge_type),
  );
  const advancedCatalog = mc.catalog.filter((c) => c.is_advanced);
  const selected = mc.selectedChallenge;
  const outcome = (selected?.outcome_json ?? {}) as Record<string, unknown>;
  const toolsRan = Array.isArray(outcome.tools_ran) ? (outcome.tools_ran as string[]) : [];
  const observationsVi = Array.isArray(outcome.observations_vi)
    ? (outcome.observations_vi as string[])
    : [];
  const trafficMetrics = (selected?.generated_refs_json?.traffic_metrics ??
    null) as Record<string, unknown> | null;

  return (
    <div className="space-y-6">
      {/* Header & Controls */}
      <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-xs">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="space-y-1">
            <div className="flex flex-wrap items-center gap-2">
              <span className="inline-flex items-center gap-1.5 rounded-full bg-indigo-50 px-3 py-1 text-xs font-semibold text-indigo-800">
                <span
                  className={`h-2 w-2 rounded-full ${
                    !mc.supervisor?.worker_healthy
                      ? "bg-rose-500"
                      : mc.isBusyMonitoring
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
              Trung tâm Điều hành &amp; Giám sát Agent Tự hành
            </h1>
            <p className="max-w-3xl text-sm text-slate-600">
              Sự kiện bất thường được xếp hàng đợi bền vững trong PostgreSQL (HTTP 202 Accepted) để
              Worker nền độc lập tự động điều tra, đối chiếu sổ cái và dừng tại chốt chặn phê duyệt.
            </p>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              disabled={mc.busy || mc.sessionLoading || !mc.supervisor}
              onClick={() => void mc.toggleAutopilot()}
              className={
                mc.supervisor?.autopilot_enabled
                  ? "rounded-lg border border-amber-300 bg-amber-50 px-3.5 py-2 text-xs font-semibold text-amber-900 hover:bg-amber-100 disabled:opacity-50"
                  : primaryButton
              }
            >
              {mc.supervisor?.autopilot_enabled ? "⏸ Tạm dừng AI (Pause)" : "▶ Tiếp tục AI (Resume)"}
            </button>
            <button
              type="button"
              disabled={mc.busy || mc.sessionLoading}
              onClick={() => void mc.resetWorld()}
              className={secondaryButton}
            >
              Đặt lại dữ liệu
            </button>
            <button
              type="button"
              disabled={mc.busy || mc.sessionLoading}
              onClick={() => mc.createNewSession()}
              className={secondaryButton}
            >
              Phiên mới
            </button>
          </div>
        </div>

        {(mc.error || mc.sessionError) && (
          <div
            role="alert"
            className="mt-4 rounded-xl border border-rose-300 bg-rose-50 px-4 py-3 text-sm text-rose-900"
          >
            {mc.error ?? mc.sessionError}
          </div>
        )}
        {mc.notice && (
          <div
            role="status"
            className="mt-4 rounded-xl border border-emerald-300 bg-emerald-50 px-4 py-3 text-sm text-emerald-950"
          >
            {mc.notice}
          </div>
        )}
      </div>

      {/* Section 1: Worker Health & Supervisor Status */}
      <SupervisorStatusPanel supervisor={mc.supervisor} />

      {/* Section 5: Human Approval Inbox & Recent Verifications */}
      <ApprovalInboxPanel
        approvalInbox={mc.approvalInbox}
        recentVerifications={mc.recentVerifications}
        approvingId={mc.approvingId}
        busy={mc.busy}
        onApprove={(item) => void mc.approveProposal(item)}
        onReject={(item) => void mc.rejectProposal(item)}
        onQuickSelectBulkSync={() => handleSelectChallengeType("BULK_ORDER_SYNC")}
        onQuickSelectLegitRefund={() => {
          handleSelectChallengeType("DUPLICATE_REFUND");
          handleSelectPreset("LEGITIMATE_PARTIAL");
        }}
      />

      {/* Section 2: Challenge Lab (Explicitly Labeled Regression Fixtures) */}
      <Panel
        title="2. Phòng thử nghiệm tình huống (Challenge Lab — Bộ kịch bản kiểm thử MOCK)"
        subtitle="Gửi bất đồng bộ (202 Accepted) — Worker nền tự động nhận việc và cập nhật dòng thời gian thực"
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

        {activeCatalogItem && (
          <div className="mt-4 rounded-xl border border-slate-200 bg-slate-50/70 p-4">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <h3 className="text-sm font-bold text-slate-900">
                  Tùy chỉnh: {activeCatalogItem.title_vi}
                </h3>
                <p className="text-xs text-slate-600">
                  Kết luận của AI được rút ra hoàn toàn từ dữ liệu sổ cái &amp; Risk Engine thực tế,
                  không suy diễn theo tên kịch bản.
                </p>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                {selected && (selected.status === "RUNNING" || selected.status === "QUEUED") && (
                  <button
                    type="button"
                    disabled={mc.busy}
                    onClick={() => void mc.cancelChallenge(selected.id)}
                    className="rounded-lg border border-rose-300 bg-rose-50 px-3 py-2 text-xs font-semibold text-rose-900 hover:bg-rose-100"
                  >
                    ⏹ Dừng thử thách
                  </button>
                )}
                <button
                  type="button"
                  disabled={mc.busy || mc.sessionLoading || !mc.session}
                  onClick={handleLaunch}
                  className={primaryButton}
                >
                  {mc.busy ? "Đang gửi sự kiện…" : `⚡ Kích hoạt "${activeCatalogItem.title_vi}"`}
                </button>
              </div>
            </div>

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
                  Lời khai / ghi chú đính kèm
                  <input
                    type="text"
                    value={receiptNote}
                    onChange={(e) => setReceiptNote(e.target.value)}
                    className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-sm"
                  />
                </label>

                <details className="sm:col-span-3">
                  <summary className="cursor-pointer text-xs font-semibold text-indigo-700 hover:text-indigo-900">
                    Phân tích văn bản biên lai mẫu MOCK / TEST (Bộ phân tích văn bản — không phải
                    OCR ảnh)
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
                        Trình phân tích văn bản có cấu trúc (OCR ảnh chưa hỗ trợ trong phiên bản
                        này). Mọi trường đều là UNTRUSTED_CLAIM.
                      </span>
                      <button
                        type="button"
                        disabled={mc.busy}
                        onClick={() => void handleExtractMockReceipt()}
                        className={secondaryButton}
                      >
                        Phân tích văn bản MOCK
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

            {activeType === "TRAFFIC_BURST" && (
              <div className="mt-4 grid gap-3 border-t border-slate-200 pt-4 sm:grid-cols-2">
                <label className="text-xs font-medium text-slate-700">
                  Số sự kiện mô phỏng (giới hạn 1–120 — tính theo công thức mô phỏng)
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
                  Thời lượng mô phỏng (1–30 giây)
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

        <details className="mt-4 rounded-xl border border-slate-200 bg-white p-3.5">
          <summary className="cursor-pointer text-xs font-semibold text-slate-700 hover:text-slate-900">
            Tùy chọn thử thách nâng cao (Kiểm thử Prompt Injection &amp; Khiếu nại tự do)
          </summary>
          <div className="mt-3 grid gap-4 border-t border-slate-100 pt-3 lg:grid-cols-2">
            <div className="space-y-2">
              <div className="text-xs font-bold text-slate-900">
                Kiểm thử chống thao túng câu lệnh (Prompt Injection)
              </div>
              <div className="flex flex-wrap gap-2">
                {advancedCatalog.map((adv) =>
                  adv.presets.map((p) => (
                    <button
                      key={p.preset_id}
                      type="button"
                      disabled={mc.busy}
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

            <form onSubmit={handleFreeTextSubmit} className="space-y-2">
              <div className="text-xs font-bold text-slate-900">
                Gửi khiếu nại tiếng Việt bất kỳ (xếp hàng đợi Worker tự điều tra)
              </div>
              <div className="flex gap-2">
                <input
                  type="text"
                  value={freeClaimText}
                  onChange={(e) => setFreeClaimText(e.target.value)}
                  placeholder="Ví dụ: Tôi đã chuyển tiền MoMo 350.000đ nhưng đơn chưa xác nhận..."
                  className="flex-1 rounded-lg border border-slate-300 px-3 py-1.5 text-xs"
                />
                <button
                  type="submit"
                  disabled={mc.busy || !freeClaimText.trim()}
                  className={secondaryButton}
                >
                  Gửi
                </button>
              </div>
            </form>
          </div>
        </details>
      </Panel>

      {/* Section 3 (Live Activity) & Section 4 (Evidence-Grounded Results) */}
      <div className="grid gap-6 lg:grid-cols-12">
        <div className="lg:col-span-5">
          <ActivityTimelinePanel
            activities={mc.activities}
            activeJobs={mc.activeJobs}
            busy={mc.busy}
            onCancelJob={(id) => void mc.cancelJob(id)}
          />
        </div>

        <div className="lg:col-span-7">
          <Panel
            title="Kết quả &amp; Bằng chứng điều tra"
            subtitle="Kết luận hoàn toàn dựa trên dữ liệu sổ cái, Risk Engine, Policy và Verifier thực tế"
            right={selected ? <StatusPill status={selected.status} /> : undefined}
          >
            {!selected ? (
              <div className="rounded-xl border border-dashed border-slate-200 bg-slate-50/50 px-4 py-10 text-center text-sm text-slate-600">
                Chưa có thử thách nào được chạy. Hãy chọn tình huống ở mục 2 và bấm{" "}
                <span className="font-semibold text-indigo-700">Kích hoạt</span>.
              </div>
            ) : (
              <div className="space-y-4">
                {mc.recentChallenges.length > 1 && (
                  <div className="flex flex-wrap items-center gap-1.5 text-xs">
                    <span className="font-medium text-slate-500">Lịch sử:</span>
                    {mc.recentChallenges.map((c, idx) => (
                      <button
                        key={c.id}
                        type="button"
                        onClick={() => mc.setSelectedChallenge(c)}
                        className={`rounded-md border px-2.5 py-1 font-medium ${
                          selected.id === c.id
                            ? "border-indigo-600 bg-indigo-50 text-indigo-900"
                            : "border-slate-200 bg-white text-slate-700 hover:bg-slate-50"
                        }`}
                      >
                        #{mc.recentChallenges.length - idx}: {c.challenge_type}
                      </button>
                    ))}
                  </div>
                )}

                <div
                  className={`rounded-xl border p-4 ${
                    selected.safety_verdict === "VERIFIED_RESOLVED"
                      ? "border-emerald-300 bg-emerald-50 text-emerald-950"
                      : selected.safety_verdict === "WAITING_HUMAN_APPROVAL"
                        ? "border-amber-300 bg-amber-50 text-amber-950"
                        : "border-indigo-200 bg-indigo-50/70 text-slate-900"
                  }`}
                >
                  <div className="text-xs font-bold uppercase tracking-wide text-slate-500">
                    Kết luận dựa trên bằng chứng • {selected.status_label_vi} (
                    {selected.safety_verdict})
                  </div>
                  <div className="mt-1 text-base font-bold">
                    {typeof outcome.summary_vi === "string" && outcome.summary_vi
                      ? outcome.summary_vi
                      : "Worker nền đang thu thập và đối chiếu bằng chứng…"}
                  </div>
                </div>

                <div className="grid gap-3 sm:grid-cols-2">
                  <div className="rounded-xl border border-slate-200 bg-slate-50/70 p-3.5 text-xs">
                    <div className="font-bold text-slate-900">Dữ liệu đầu vào</div>
                    <div className="mt-1.5 space-y-1 text-slate-700">
                      <div className="font-semibold">{selected.title_vi}</div>
                      {selected.input_json.claimed_payment_id ? (
                        <div>
                          Mã GD khai báo:{" "}
                          <code className="font-mono font-semibold">
                            {String(selected.input_json.claimed_payment_id)}
                          </code>
                        </div>
                      ) : null}
                      {selected.input_json.claimed_amount ? (
                        <div>
                          Số tiền khai báo: {formatMoneyVnd(selected.input_json.claimed_amount)}
                        </div>
                      ) : null}
                      {selected.input_json.requested_refund ? (
                        <div>
                          Yêu cầu hoàn: {formatMoneyVnd(selected.input_json.requested_refund)}
                        </div>
                      ) : null}
                    </div>
                  </div>

                  <div className="rounded-xl border border-slate-200 bg-slate-50/70 p-3.5 text-xs">
                    <div className="font-bold text-slate-900">Công cụ đọc AI đã chạy</div>
                    {toolsRan.length === 0 ? (
                      <p className="mt-1.5 text-slate-500">Chưa có công cụ nào hoàn tất.</p>
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
                      Quan sát thực tế từ sổ cái &amp; hệ thống
                    </div>
                    {observationsVi.length > 0 ? (
                      <ul className="mt-1.5 list-disc space-y-1 pl-4 text-slate-700">
                        {observationsVi.map((obs, idx) => (
                          <li key={idx}>{obs}</li>
                        ))}
                      </ul>
                    ) : (
                      <p className="mt-1 text-slate-600">
                        Đang chờ kết quả quan sát từ Worker nền.
                      </p>
                    )}

                    {trafficMetrics && (
                      <div className="mt-3 space-y-2">
                        <div className="rounded-md border border-amber-200 bg-amber-50 px-2.5 py-1.5 text-[11px] font-medium text-amber-900">
                          Số liệu mô phỏng (SIMULATED_FORMULA_ESTIMATE) — không phải phản hồi HTTP
                          429 thực tế từ mạng.
                        </div>
                        <div className="grid grid-cols-2 gap-2 rounded-lg border border-slate-200 bg-white p-2.5 sm:grid-cols-4">
                          <div>
                            <div className="text-[11px] text-slate-500">Sự kiện mô phỏng</div>
                            <div className="text-sm font-bold text-slate-900">
                              {String(trafficMetrics.generated_events)}
                            </div>
                          </div>
                          <div>
                            <div className="text-[11px] text-slate-500">Chấp nhận (mô phỏng)</div>
                            <div className="text-sm font-bold text-emerald-700">
                              {String(trafficMetrics.accepted_events)}
                            </div>
                          </div>
                          <div>
                            <div className="text-[11px] text-slate-500">Trùng lặp (mô phỏng)</div>
                            <div className="text-sm font-bold text-indigo-700">
                              {String(trafficMetrics.duplicate_events)}
                            </div>
                          </div>
                          <div>
                            <div className="text-[11px] text-slate-500">
                              Vượt ngưỡng (mô phỏng)
                            </div>
                            <div className="text-sm font-bold text-amber-700">
                              {String(trafficMetrics.rate_limited_events)}
                            </div>
                          </div>
                        </div>
                      </div>
                    )}
                  </div>
                </div>

                <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-slate-200 bg-white p-3 text-xs">
                  <div className="flex flex-wrap items-center gap-3">
                    {selected.linked_case_ids[0] && (
                      <Link
                        href={caseUrl(selected.linked_case_ids[0])}
                        className="font-semibold text-indigo-700 underline hover:text-indigo-900"
                      >
                        Mở bằng chứng hồ sơ →
                      </Link>
                    )}
                    {selected.linked_incident_ids.length > 0 && (
                      <Link
                        href="/live"
                        className="font-semibold text-indigo-700 underline hover:text-indigo-900"
                      >
                        Xem Incident &amp; Taskforce →
                      </Link>
                    )}
                  </div>
                  <span className="text-slate-500">
                    Verifier:{" "}
                    <strong className="text-emerald-700">
                      {outcome.postconditions_verified ? "PASSED" : "Chưa thực thi"}
                    </strong>
                  </span>
                </div>

                <details className="rounded-xl border border-slate-200 bg-slate-50 p-3 text-xs">
                  <summary className="cursor-pointer font-semibold text-slate-700 hover:text-slate-900">
                    Chi tiết kỹ thuật nâng cao (Case ID, Evidence IDs, Token Usage, Audit JSON)
                  </summary>
                  <div className="mt-2 space-y-1.5 text-slate-600">
                    <div>
                      Challenge ID: <code className="font-mono">{selected.id}</code> • World Epoch:{" "}
                      <code className="font-mono">{selected.world_epoch}</code>
                    </div>
                    <div>
                      Linked Cases:{" "}
                      <code className="font-mono">{selected.linked_case_ids.join(", ") || "—"}</code>
                    </div>
                    <pre className="mt-2 max-h-48 overflow-auto rounded-lg bg-slate-900 p-2.5 font-mono text-[11px] text-slate-100">
                      {JSON.stringify(selected.outcome_json, null, 2)}
                    </pre>
                  </div>
                </details>
              </div>
            )}
          </Panel>
        </div>
      </div>

      <ContinuousMonitorPanel session={mc.session} />
    </div>
  );
}
