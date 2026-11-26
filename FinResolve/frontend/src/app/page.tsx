import Link from "next/link";
import { GuardianBadge } from "@/components/GuardianBadge";

const VALUE_PILLARS = [
  {
    title: "Chủ động phát hiện sự cố",
    desc: "Liên tục theo dõi giao dịch, trạng thái đơn hàng, độ trễ và tỉ lệ lỗi mà không cần nhân viên trực thủ công 24/7.",
    icon: "M12 3l7.5 3.3v5.4c0 4.6-3.1 8.8-7.5 10-4.4-1.2-7.5-5.4-7.5-10V6.3L12 3z",
  },
  {
    title: "Điều tra dựa trên bằng chứng",
    desc: "Đối chiếu dữ liệu đơn hàng, thanh toán và chính sách nghiệp vụ theo từng dấu vết xác thực — không suy đoán cảm tính.",
    icon: "M21 21l-4.35-4.35M11 18a7 7 0 100-14 7 7 0 000 14z",
  },
  {
    title: "Tự động khắc phục an toàn",
    desc: "Mọi hành động đồng bộ đều đi qua Policy Engine, chữ ký bảo mật HMAC-SHA256 và giới hạn quyền tối thiểu.",
    icon: "M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z",
  },
  {
    title: "Xác minh kết quả phục hồi",
    desc: "Không bao giờ tin HTTP 200 đơn thuần — Verifier độc lập đọc lại trạng thái thực tế và kiểm chứng sự ổn định.",
    icon: "M4 12h4l3-7 4 14 3-7h2",
  },
];

const LOOP_STEPS = [
  {
    step: "01",
    name: "Quan sát",
    desc: "Thu thập sự kiện thanh toán, đơn hàng và telemetry thực theo thời gian thực.",
  },
  {
    step: "02",
    name: "Phát hiện",
    desc: "Nhận diện lệch đồng bộ thanh toán, bất thường hiệu năng hoặc khiếu nại mâu thuẫn.",
  },
  {
    step: "03",
    name: "Điều tra",
    desc: "AI thu thập bằng chứng xác thực từ nhiều nguồn và loại trừ giả thuyết sai.",
  },
  {
    step: "04",
    name: "Hành động",
    desc: "Policy Engine thẩm định và thực thi lệnh phục hồi an toàn hoặc mời người dùng duyệt.",
  },
  {
    step: "05",
    name: "Kiểm chứng",
    desc: "Đọc lại dữ liệu gốc độc lập để chắc chắn sự cố đã thật sự được xử lý.",
  },
];

export default function LandingPage() {
  return (
    <div className="min-h-screen bg-[#F8FAFC] text-[#0F172A]">
      {/* Sticky Modern SaaS Navigation */}
      <header className="sticky top-0 z-30 border-b border-slate-200/80 bg-white/90 backdrop-blur-md">
        <div className="mx-auto flex max-w-6xl items-center justify-between px-4 py-3.5 sm:px-6">
          <Link href="/" className="flex items-center gap-2.5">
            <span className="flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-[#2563EB] to-[#0EA5E9] text-white shadow-sm">
              <svg viewBox="0 0 24 24" fill="none" className="h-5 w-5" aria-hidden="true">
                <path
                  d="M12 2.5L4.5 5.8V11.4C4.5 16.2 7.7 20.5 12 21.8C16.3 20.5 19.5 16.2 19.5 11.4V5.8L12 2.5Z"
                  stroke="currentColor"
                  strokeWidth="1.9"
                  strokeLinejoin="round"
                />
                <circle cx="12" cy="11.5" r="2" fill="currentColor" />
              </svg>
            </span>
            <div>
              <span className="block text-base font-bold tracking-tight text-slate-900">
                FinResolve AI
              </span>
              <span className="block text-[11px] font-medium text-slate-500">
                Autonomous Business Guardian
              </span>
            </div>
          </Link>

          <nav aria-label="Điều hướng trang chủ" className="hidden items-center gap-7 md:flex">
            <a href="#overview" className="text-sm font-medium text-slate-600 hover:text-blue-600">
              Tổng quan
            </a>
            <a
              href="#how-it-works"
              className="text-sm font-medium text-slate-600 hover:text-blue-600"
            >
              Cách hoạt động
            </a>
            <Link
              href="/experience"
              className="text-sm font-medium text-slate-600 hover:text-blue-600"
            >
              Trải nghiệm
            </Link>
          </nav>

          <div className="flex items-center gap-3">
            <Link
              href="/demo"
              className="hidden rounded-lg border border-slate-200 bg-white px-3.5 py-2 text-sm font-medium text-slate-700 transition hover:bg-slate-50 sm:inline-flex"
            >
              Mission Control
            </Link>
            <Link
              href="/experience"
              className="inline-flex items-center rounded-lg bg-[#2563EB] px-4 py-2 text-sm font-semibold text-white shadow-sm transition hover:bg-[#1D4ED8]"
            >
              Bắt đầu dùng thử
            </Link>
          </div>
        </div>
      </header>

      {/* Hero Section */}
      <section className="relative overflow-hidden border-b border-slate-200/80 bg-gradient-to-b from-[#EFF6FF]/90 via-white to-[#F8FAFC]">
        <div className="mx-auto grid max-w-6xl items-center gap-12 px-4 py-16 sm:px-6 lg:grid-cols-12 lg:py-24">
          <div className="lg:col-span-6">
            <div className="mb-5 inline-flex items-center gap-2 rounded-full border border-blue-200 bg-blue-50 px-3.5 py-1 text-xs font-semibold text-blue-700">
              <span className="h-2 w-2 rounded-full bg-blue-600" />
              FinResolve AI · Autonomous Guardian for Payment &amp; Commerce
            </div>
            <h1 className="text-3xl font-bold leading-tight tracking-tight text-slate-900 sm:text-5xl">
              Doanh nghiệp vận hành.{" "}
              <span className="bg-gradient-to-r from-[#2563EB] to-[#0EA5E9] bg-clip-text text-transparent">
                AI âm thầm bảo vệ.
              </span>
            </h1>
            <p className="mt-5 text-base leading-relaxed text-slate-600 sm:text-lg">
              FinResolve AI liên tục giám sát giao dịch, phát hiện bất thường, điều tra dựa trên
              bằng chứng và khắc phục sự cố thanh toán — trước khi chúng trở thành vấn đề nghiêm
              trọng cho khách hàng và doanh nghiệp.
            </p>
            <div className="mt-8 flex flex-wrap items-center gap-3.5">
              <Link
                href="/experience"
                className="inline-flex items-center gap-2 rounded-xl bg-[#2563EB] px-6 py-3.5 text-sm font-semibold text-white shadow-sm transition hover:bg-[#1D4ED8]"
              >
                Trải nghiệm ngay
                <span aria-hidden="true">→</span>
              </Link>
              <a
                href="#how-it-works"
                className="inline-flex items-center rounded-xl border border-slate-300 bg-white px-5 py-3.5 text-sm font-semibold text-slate-700 transition hover:border-blue-300 hover:bg-blue-50/40 hover:text-blue-700"
              >
                Khám phá cách hoạt động
              </a>
            </div>
            <p className="mt-5 text-xs text-slate-500">
              Môi trường Sandbox mô phỏng an toàn (LOCAL_MOCK) · Không yêu cầu API key hay token
              quản trị để trải nghiệm.
            </p>
          </div>

          {/* Right-side Product Illustration (explicitly labeled "Minh họa sản phẩm") */}
          <div className="lg:col-span-6">
            <div className="rounded-2xl border border-blue-200/80 bg-white p-5 shadow-lg shadow-blue-950/5">
              <div className="mb-4 flex flex-wrap items-center justify-between gap-2 border-b border-slate-100 pb-3">
                <div className="flex items-center gap-2">
                  <span className="text-sm font-semibold text-slate-900">
                    Guardian Command Center
                  </span>
                  <span className="rounded-md border border-blue-200 bg-blue-50 px-2 py-0.5 text-[11px] font-semibold text-blue-700">
                    Minh họa sản phẩm
                  </span>
                </div>
                <GuardianBadge state="MONITORING" />
              </div>

              <div className="grid grid-cols-3 gap-3">
                <div className="rounded-xl border border-slate-200 bg-slate-50/70 p-3">
                  <p className="text-[11px] font-medium text-slate-500">Giám sát liên tục</p>
                  <p className="mt-1 text-base font-bold text-slate-900">Đơn &amp; Thanh toán</p>
                  <p className="mt-0.5 text-[11px] text-blue-600">Theo thời gian thực</p>
                </div>
                <div className="rounded-xl border border-slate-200 bg-slate-50/70 p-3">
                  <p className="text-[11px] font-medium text-slate-500">Policy Engine</p>
                  <p className="mt-1 text-base font-bold text-slate-900">HMAC-SHA256</p>
                  <p className="mt-0.5 text-[11px] text-emerald-600">Ủy quyền chặt chẽ</p>
                </div>
                <div className="rounded-xl border border-slate-200 bg-slate-50/70 p-3">
                  <p className="text-[11px] font-medium text-slate-500">Independent Verifier</p>
                  <p className="mt-1 text-base font-bold text-slate-900">Đọc lại dữ liệu</p>
                  <p className="mt-0.5 text-[11px] text-sky-600">Xác minh phục hồi</p>
                </div>
              </div>

              <div className="mt-4 space-y-2.5 rounded-xl border border-slate-200 bg-[#F8FAFC] p-3.5 text-xs">
                <p className="font-semibold text-slate-700">
                  Ví dụ luồng bảo vệ tự động (Minh họa sản phẩm)
                </p>
                <div className="flex items-start gap-2.5">
                  <span className="mt-0.5 h-2 w-2 shrink-0 rounded-full bg-blue-600" />
                  <p className="text-slate-600">
                    <strong className="text-slate-900">Phát hiện:</strong> Giao dịch đã thanh toán
                    thành công nhưng đơn hàng chưa được xác nhận.
                  </p>
                </div>
                <div className="flex items-start gap-2.5">
                  <span className="mt-0.5 h-2 w-2 shrink-0 rounded-full bg-sky-500" />
                  <p className="text-slate-600">
                    <strong className="text-slate-900">Điều tra &amp; Đề xuất:</strong> AI đối
                    chiếu bản ghi đơn hàng + thanh toán và đề xuất đồng bộ đúng phiên bản.
                  </p>
                </div>
                <div className="flex items-start gap-2.5">
                  <span className="mt-0.5 h-2 w-2 shrink-0 rounded-full bg-emerald-500" />
                  <p className="text-slate-600">
                    <strong className="text-slate-900">Xác minh:</strong> Verifier đọc lại đơn
                    hàng ở trạng thái CONFIRMED trước khi đóng sự cố.
                  </p>
                </div>
              </div>
            </div>
          </div>
        </div>
      </section>

      {/* Product Value Section */}
      <section id="overview" className="mx-auto max-w-6xl px-4 py-16 sm:px-6 lg:py-20">
        <div className="max-w-2xl">
          <p className="text-xs font-semibold uppercase tracking-wider text-[#2563EB]">
            Năng lực cốt lõi
          </p>
          <h2 className="mt-2 text-2xl font-bold tracking-tight text-slate-900 sm:text-3xl">
            Bảo vệ dòng tiền và trải nghiệm khách hàng ở mọi bước vận hành
          </h2>
        </div>

        <div className="mt-10 grid gap-5 sm:grid-cols-2 lg:grid-cols-4">
          {VALUE_PILLARS.map((item) => (
            <div
              key={item.title}
              className="rounded-2xl border border-slate-200 bg-white p-6 shadow-sm transition hover:border-blue-200 hover:shadow-md"
            >
              <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-[#EFF6FF] text-[#2563EB]">
                <svg viewBox="0 0 24 24" fill="none" className="h-5 w-5" aria-hidden="true">
                  <path
                    d={item.icon}
                    stroke="currentColor"
                    strokeWidth="1.8"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  />
                </svg>
              </div>
              <h3 className="mt-4 text-base font-semibold text-slate-900">{item.title}</h3>
              <p className="mt-2 text-sm leading-relaxed text-slate-600">{item.desc}</p>
            </div>
          ))}
        </div>
      </section>

      {/* How It Works Section */}
      <section
        id="how-it-works"
        className="border-y border-slate-200/80 bg-white py-16 lg:py-20"
      >
        <div className="mx-auto max-w-6xl px-4 sm:px-6">
          <div className="max-w-2xl">
            <p className="text-xs font-semibold uppercase tracking-wider text-[#2563EB]">
              Vòng lặp tự hành
            </p>
            <h2 className="mt-2 text-2xl font-bold tracking-tight text-slate-900 sm:text-3xl">
              Cách FinResolve AI vận hành khép kín
            </h2>
          </div>

          <div className="mt-10 grid gap-4 md:grid-cols-5">
            {LOOP_STEPS.map((s) => (
              <div
                key={s.step}
                className="relative rounded-2xl border border-slate-200 bg-[#F8FAFC] p-5"
              >
                <span className="inline-flex h-7 w-7 items-center justify-center rounded-lg bg-[#EFF6FF] text-xs font-bold text-[#2563EB]">
                  {s.step}
                </span>
                <h3 className="mt-3 text-base font-semibold text-slate-900">{s.name}</h3>
                <p className="mt-1.5 text-xs leading-relaxed text-slate-600">{s.desc}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* Final CTA Section */}
      <section className="mx-auto max-w-6xl px-4 py-16 sm:px-6 lg:py-20">
        <div className="rounded-3xl bg-gradient-to-r from-[#2563EB] to-[#0EA5E9] p-8 text-white shadow-lg sm:p-12">
          <div className="max-w-2xl">
            <h2 className="text-2xl font-bold tracking-tight sm:text-3xl">
              Bạn có thể thử thách doanh nghiệp đang được FinResolve bảo vệ không?
            </h2>
            <p className="mt-3 text-sm leading-relaxed text-blue-50 sm:text-base">
              Khởi tạo ngay môi trường Sandbox được cô lập — mua hàng thử nghiệm, theo dõi AI
              Guardian hoạt động trực tiếp và kiểm chứng từng bằng chứng điều tra.
            </p>
            <div className="mt-7 flex flex-wrap gap-3">
              <Link
                href="/experience"
                className="inline-flex items-center gap-2 rounded-xl bg-white px-6 py-3 text-sm font-semibold text-[#2563EB] shadow-sm transition hover:bg-blue-50"
              >
                Bắt đầu trải nghiệm
                <span aria-hidden="true">→</span>
              </Link>
              <Link
                href="/demo"
                className="inline-flex items-center rounded-xl border border-white/40 bg-white/10 px-5 py-3 text-sm font-semibold text-white transition hover:bg-white/20"
              >
                Mở Mission Control cũ
              </Link>
            </div>
          </div>
        </div>
      </section>

      <footer className="border-t border-slate-200 bg-white py-6">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-2 px-4 text-xs text-slate-500 sm:px-6">
          <span>FinResolve AI — Autonomous Guardian for Payment &amp; Commerce</span>
          <span>Môi trường mô phỏng (MOCK). Không giao dịch tiền thật, không kích hoạt VNPAY.</span>
        </div>
      </footer>
    </div>
  );
}
