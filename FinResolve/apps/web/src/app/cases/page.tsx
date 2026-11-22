import type { Metadata } from "next";
import { PlaceholderPage } from "@/components/PlaceholderPage";

export const metadata: Metadata = { title: "Trung tâm Case — FinResolve AI" };

export default function CasesPage() {
  return (
    <PlaceholderPage
      title="Trung tâm Case"
      description="Tạo và theo dõi Case: bằng chứng (CLAIM / OBSERVED_FACT / HYPOTHESIS / UNKNOWN), dấu vết quyết định của AI, trạng thái hiện tại, hành động kế tiếp và các điểm chưa rõ."
    />
  );
}
