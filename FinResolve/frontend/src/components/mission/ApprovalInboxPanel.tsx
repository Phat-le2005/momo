"use client";

import Link from "next/link";
import { Panel, StatusPill, primaryButton, secondaryButton } from "@/components/StatusPill";
import {
  caseUrl,
  type ApprovalInboxItem,
  type RecentVerificationItem,
} from "@/lib/api";

export function ApprovalInboxPanel({
  approvalInbox,
  recentVerifications,
  approvingId,
  busy,
  onApprove,
  onReject,
  onQuickSelectBulkSync,
  onQuickSelectLegitRefund,
}: {
  approvalInbox: ApprovalInboxItem[];
  recentVerifications: RecentVerificationItem[];
  approvingId: string | null;
  busy: boolean;
  onApprove: (item: ApprovalInboxItem) => void;
  onReject: (item: ApprovalInboxItem) => void;
  onQuickSelectBulkSync: () => void;
  onQuickSelectLegitRefund: () => void;
}) {
  return (
    <div className="space-y-4">
      <Panel
        title={`Hộp thư phê duyệt của con người (${approvalInbox.length})`}
        subtitle="AI dừng tại đây khi cần thực hiện hành động tài chính — sau khi bạn duyệt, backend tự động tiếp tục thực thi & kiểm chứng hậu điều kiện"
      >
        {approvalInbox.length === 0 ? (
          <div className="rounded-xl border border-dashed border-slate-200 bg-slate-50/50 px-4 py-5 text-center text-sm text-slate-600">
            Hiện không có đề xuất nào chờ phê duyệt. Hãy chạy thử thách{" "}
            <button
              type="button"
              onClick={onQuickSelectBulkSync}
              className="font-semibold text-indigo-700 underline hover:text-indigo-900"
            >
              Nghẽn đơn hàng
            </button>{" "}
            hoặc{" "}
            <button
              type="button"
              onClick={onQuickSelectLegitRefund}
              className="font-semibold text-indigo-700 underline hover:text-indigo-900"
            >
              Hoàn tiền một phần hợp lệ
            </button>{" "}
            để trải nghiệm quy trình AI đề xuất → Con người phê duyệt → Backend tự thực thi &amp;
            Verifier kiểm chứng.
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
                    <span className="text-xs text-slate-600">• Rủi ro: {item.risk_level}</span>
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
                    onClick={() => onApprove(item)}
                    className={primaryButton}
                  >
                    {approvingId === item.proposal_id
                      ? "Đang ghi nhận phê duyệt…"
                      : "✓ Phê duyệt (MOCK)"}
                  </button>
                  <button
                    type="button"
                    disabled={approvingId === item.proposal_id || busy}
                    onClick={() => onReject(item)}
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

      {recentVerifications.length > 0 && (
        <Panel
          title={`Kết quả kiểm chứng hậu điều kiện gần đây (${recentVerifications.length})`}
          subtitle="Verifier độc lập đọc lại trạng thái cơ sở dữ liệu sau khi thực thi — Case chỉ RESOLVED khi PASSED"
        >
          <div className="grid gap-2 sm:grid-cols-2">
            {recentVerifications.slice(0, 4).map((v) => (
              <div
                key={v.verification_id}
                className="flex items-center justify-between gap-3 rounded-xl border border-slate-200 bg-slate-50/70 p-3 text-xs"
              >
                <div>
                  <div className="font-semibold text-slate-900">
                    Hồ sơ{" "}
                    <Link
                      href={caseUrl(v.case_id)}
                      className="text-indigo-700 underline hover:text-indigo-900"
                    >
                      {v.case_id}
                    </Link>
                  </div>
                  <div className="mt-0.5 text-slate-500">
                    Kiểm chứng lúc {new Date(v.checked_at).toLocaleTimeString("vi-VN")}
                  </div>
                </div>
                <StatusPill value={v.verdict} />
              </div>
            ))}
          </div>
        </Panel>
      )}
    </div>
  );
}
