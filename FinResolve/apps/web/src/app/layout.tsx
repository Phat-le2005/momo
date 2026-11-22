import type { Metadata } from "next";
import { SiteHeader } from "@/components/SiteHeader";
import "./globals.css";

export const metadata: Metadata = {
  title: "FinResolve AI — Demo (MOCK)",
  description:
    "Xử lý ngoại lệ và rủi ro thanh toán/thương mại dựa trên bằng chứng. Dữ liệu mô phỏng, không giao dịch tiền thật.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="vi">
      <body className="min-h-screen">
        <a
          href="#main"
          className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:rounded focus:bg-white focus:px-3 focus:py-2"
        >
          Bỏ qua điều hướng
        </a>
        <SiteHeader />
        <main id="main" className="mx-auto w-full max-w-6xl px-4 py-8">
          {children}
        </main>
        <footer className="mx-auto w-full max-w-6xl px-4 pb-8 text-xs text-slate-500">
          Toàn bộ dữ liệu là mô phỏng (MOCK). Không kết nối hệ thống thanh toán thật, không
          hoàn tiền thật. Vui lòng không nhập thông tin tài khoản ngân hàng hay mật khẩu.
        </footer>
      </body>
    </html>
  );
}
