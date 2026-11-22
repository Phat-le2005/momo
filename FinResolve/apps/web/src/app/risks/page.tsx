import type { Metadata } from "next";
import { PlaceholderPage } from "@/components/PlaceholderPage";

export const metadata: Metadata = { title: "Trung tâm Rủi ro — FinResolve AI" };

export default function RisksPage() {
  return (
    <PlaceholderPage
      title="Trung tâm Rủi ro"
      description="Tín hiệu rủi ro theo phạm vi (trùng hoàn tiền, vượt hạn mức, giao dịch đang chờ, quyền hạn) với phản hồi OBSERVE / REVIEW / HOLD_ACTION. Tín hiệu rủi ro không phải bằng chứng gian lận."
    />
  );
}
