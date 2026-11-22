import type { Metadata } from "next";
import { PlaceholderPage } from "@/components/PlaceholderPage";

export const metadata: Metadata = { title: "Mega Live — FinResolve AI" };

export default function LivePage() {
  return (
    <PlaceholderPage
      title="Mega Live (mô phỏng)"
      description="Phiên livestream mô phỏng 50–100 đơn với sự kiện trễ/mất/trùng; gom nhóm Incident và phân công Taskforce. Mỗi Case con vẫn phải được xác minh độc lập."
    />
  );
}
