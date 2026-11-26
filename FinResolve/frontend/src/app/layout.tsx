import type { Metadata } from "next";
import { Be_Vietnam_Pro } from "next/font/google";
import { AppShell } from "@/components/AppShell";
import "./globals.css";

const beVietnamPro = Be_Vietnam_Pro({
  subsets: ["vietnamese", "latin"],
  weight: ["400", "500", "600", "700"],
  variable: "--font-be-vietnam-pro",
  display: "swap",
});

export const metadata: Metadata = {
  title: "FinResolve AI — Autonomous Guardian for Payment & Commerce",
  description:
    "Doanh nghiệp vận hành. AI âm thầm bảo vệ. Giám sát giao dịch, phát hiện bất thường, điều tra dựa trên bằng chứng và phục hồi an toàn (Sandbox MOCK).",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="vi" className={beVietnamPro.variable}>
      <body className="min-h-screen font-sans">
        <a
          href="#main"
          className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-50 focus:rounded-lg focus:bg-white focus:px-3 focus:py-2 focus:text-sm focus:font-medium focus:text-blue-600 focus:shadow"
        >
          Bỏ qua điều hướng
        </a>
        <AppShell>{children}</AppShell>
      </body>
    </html>
  );
}
