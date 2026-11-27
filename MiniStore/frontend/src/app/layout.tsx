import type { Metadata } from "next";
import { Be_Vietnam_Pro } from "next/font/google";
import type { ReactNode } from "react";
import "./globals.css";

const beVietnamPro = Be_Vietnam_Pro({
  subsets: ["vietnamese", "latin"],
  weight: ["400", "500", "600", "700"],
  variable: "--font-be-vietnam-pro",
  display: "swap",
});

export const metadata: Metadata = {
  title: "FinShop LIVE — Mua sắm trực tuyến & Live Commerce",
  description:
    "Nền tảng mua sắm trực tuyến & Live Shopping mô phỏng (MOCK) của FinShop LIVE, được bảo vệ bởi FinResolve AI.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="vi" className={beVietnamPro.variable}>
      <body className="min-h-screen bg-[#f8faff] text-[#0b1739]">{children}</body>
    </html>
  );
}
