import type { Metadata } from "next";
import type { ReactNode } from "react";
import "./globals.css";

export const metadata: Metadata = {
  title: "FinShop LIVE — Mua sắm trực tuyến & Live Commerce",
  description:
    "Nền tảng mua sắm trực tuyến & Live Shopping mô phỏng (MOCK) của FinShop LIVE.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="vi">
      <body className="min-h-screen bg-[#f7f7f9] text-[#161823]">{children}</body>
    </html>
  );
}
