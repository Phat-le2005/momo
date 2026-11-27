import Link from "next/link";
import { GuardianCore } from "@/components/GuardianCore";
import { LandingScrollStory } from "@/components/LandingScrollStory";

const HERO_STREAMS = [
  { name: "Orders", labelVi: "Đơn hàng", x: "8%", y: "14%" },
  { name: "Payments", labelVi: "Thanh toán", x: "68%", y: "10%" },
  { name: "Customers", labelVi: "Khách hàng", x: "4%", y: "52%" },
  { name: "Refunds", labelVi: "Hoàn tiền", x: "72%", y: "50%" },
  { name: "Inventory", labelVi: "Kho hàng", x: "10%", y: "84%" },
  { name: "Policies", labelVi: "Chính sách", x: "66%", y: "84%" },
];

const CORE_CAPABILITIES = [
  {
    eyebrow: "01 · CONTINUOUS MONITOR",
    title: "Phát hiện sớm trước khi khách hàng khiếu nại",
    desc: "Theo dõi liên tục độ lệch thanh toán, độ trễ outbox và biến động hiệu năng dựa trên dữ liệu thực — không phụ thuộc nhân sự trực thủ công.",
  },
  {
    eyebrow: "02 · EVIDENCE-GUIDED AI",
    title: "Điều tra đa nguồn, không bao giờ suy đoán cảm tính",
    desc: "AI đối chiếu trực tiếp bản ghi đơn hàng, giao dịch thanh toán, biên lai OCR thực và chính sách trước khi đề xuất bất kỳ phương án nào.",
  },
  {
    eyebrow: "03 · POLICY ENGINE & VERIFIER",
    title: "Thực thi có chữ ký HMAC & kiểm chứng độc lập",
    desc: "Mọi lệnh phục hồi đều có khóa idempotency, kiểm soát phiên bản và được Independent Verifier đọc lại từ nguồn gốc trước khi xác nhận hoàn tất.",
  },
];

export default function LandingPage() {
  return (
    <div className="min-h-screen bg-[#F8FAFF] text-[#0B1739]">
      {/* Refined Translucent Top Navigation */}
      <header className="sticky top-0 z-30 border-b border-[#DBEAFE]/70 bg-white/85 backdrop-blur-md">
        <div className="mx-auto flex max-w-6xl items-center justify-between px-4 py-3.5 sm:px-6">
          <Link href="/" className="flex items-center gap-2.5">
            <span className="flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-[#2563EB] to-[#0284C7] text-white shadow-sm shadow-blue-600/20">
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
              <span className="block text-base font-bold tracking-tight text-[#0B1739]">
                FinResolve AI
              </span>
              <span className="block text-[11px] font-medium text-[#64748B]">
                Autonomous Business Guardian
              </span>
            </div>
          </Link>

          <nav aria-label="Điều hướng trang chủ" className="hidden items-center gap-7 md:flex">
            <a href="#story" className="text-sm font-medium text-[#475569] transition hover:text-[#2563EB]">
              Cách hoạt động
            </a>
            <a
              href="#capabilities"
              className="text-sm font-medium text-[#475569] transition hover:text-[#2563EB]"
            >
              Kiến trúc bảo vệ
            </a>
            <Link
              href="/experience"
              className="text-sm font-medium text-[#475569] transition hover:text-[#2563EB]"
            >
              Command Center
            </Link>
          </nav>

          <div className="flex items-center gap-2.5">
            <Link
              href="/demo"
              className="hidden rounded-xl border border-[#E2E8F0] bg-white px-3.5 py-2 text-xs font-semibold text-[#475569] transition hover:border-[#BFDBFE] hover:text-[#1D4ED8] sm:inline-flex"
            >
              Mission Control
            </Link>
            <Link
              href="/experience"
              className="inline-flex items-center gap-1.5 rounded-xl bg-[#2563EB] px-4 py-2 text-xs font-semibold text-white shadow-sm shadow-blue-600/20 transition hover:bg-[#1D4ED8]"
            >
              Trải nghiệm Guardian
              <span aria-hidden="true">→</span>
            </Link>
          </div>
        </div>
      </header>

      {/* Above-the-Fold Split-Screen Editorial Hero */}
      <section className="relative overflow-hidden border-b border-[#DBEAFE]/80 bg-gradient-to-b from-[#EAF3FF]/85 via-[#F8FAFF] to-white">
        {/* Subtle diffuse blue light orbs */}
        <div
          aria-hidden="true"
          className="pointer-events-none absolute -right-24 -top-24 h-96 w-96 rounded-full bg-[#38BDF8]/15 blur-3xl"
        />
        <div
          aria-hidden="true"
          className="pointer-events-none absolute -left-24 bottom-0 h-80 w-80 rounded-full bg-[#2563EB]/10 blur-3xl"
        />

        <div className="relative mx-auto grid max-w-6xl items-center gap-12 px-4 py-16 sm:px-6 lg:grid-cols-12 lg:py-24">
          {/* Left Editorial Column */}
          <div className="fr-fade-up lg:col-span-6">
            <p className="inline-flex items-center gap-2 rounded-full border border-[#BFDBFE] bg-white/90 px-3.5 py-1 text-xs font-semibold text-[#1D4ED8] shadow-xs">
              <span className="h-2 w-2 rounded-full bg-[#2563EB]" />
              MoMo Tech Contest 2026 · Light Blue Edition
            </p>

            <h1 className="mt-6 text-4xl font-bold leading-[1.1] tracking-tight text-[#0B1739] sm:text-5xl lg:text-[3.35rem]">
              Your business never sleeps.{" "}
              <span className="bg-gradient-to-r from-[#2563EB] via-[#0284C7] to-[#38BDF8] bg-clip-text text-transparent">
                Neither does FinResolve.
              </span>
            </h1>

            <p className="mt-4 text-lg font-semibold text-[#1D4ED8] sm:text-xl">
              Doanh nghiệp không ngừng vận hành. AI không ngừng bảo vệ.
            </p>

            <p className="mt-4 max-w-xl text-base leading-relaxed text-[#475569]">
              FinResolve AI hoạt động ngay bên trong hạ tầng thương mại — liên tục quan sát đơn
              hàng, thanh toán, kho hàng và chính sách; phát hiện bất thường, điều tra bằng chứng
              thật và tự động phục hồi an toàn dưới sự kiểm soát của Policy Engine.
            </p>

            <div className="mt-8 flex flex-wrap items-center gap-3.5">
              <Link
                href="/experience"
                className="inline-flex items-center gap-2 rounded-2xl bg-[#2563EB] px-6 py-3.5 text-sm font-semibold text-white shadow-lg shadow-blue-600/20 transition hover:-translate-y-0.5 hover:bg-[#1D4ED8]"
              >
                Trải nghiệm Guardian
                <span aria-hidden="true">→</span>
              </Link>
              <a
                href="#story"
                className="inline-flex items-center gap-2 rounded-2xl border border-[#CBD5E1] bg-white px-6 py-3.5 text-sm font-semibold text-[#0B1739] transition hover:border-[#93C5FD] hover:bg-[#EAF3FF]/40 hover:text-[#1D4ED8]"
              >
                Khám phá cách hoạt động
              </a>
            </div>

            <div className="mt-8 flex flex-wrap items-center gap-x-6 gap-y-2 border-t border-[#E2E8F0] pt-5 text-xs text-[#64748B]">
              <span>Sandbox LOCAL_MOCK cô lập theo phiên</span>
              <span>·</span>
              <span>VNPAY tắt có chủ đích</span>
              <span>·</span>
              <span>Không lộ khóa quản trị</span>
            </div>
          </div>

          {/* Right Signature Guardian Core Connected to Live Business Streams */}
          <div className="lg:col-span-6">
            <div className="fr-grid-surface relative overflow-hidden rounded-3xl border border-[#DBEAFE] bg-white/95 p-5 shadow-2xl shadow-blue-950/10 sm:p-7">
              <div className="flex items-center justify-between gap-2 border-b border-[#EFF6FF] pb-3">
                <div className="flex items-center gap-2">
                  <span className="h-2 w-2 rounded-full bg-[#2563EB]" />
                  <span className="text-xs font-bold uppercase tracking-wider text-[#0B1739]">
                    Guardian Core · Living Network
                  </span>
                </div>
                <span className="rounded-full border border-[#BFDBFE] bg-[#EAF3FF] px-2.5 py-0.5 text-[11px] font-semibold text-[#1D4ED8]">
                  Minh họa sản phẩm
                </span>
              </div>

              {/* Layered SVG Network + Guardian Core */}
              <div className="relative mx-auto mt-4 h-[330px] w-full max-w-[460px] sm:h-[360px]">
                <svg
                  viewBox="0 0 460 360"
                  className="pointer-events-none absolute inset-0 h-full w-full"
                  aria-hidden="true"
                >
                  {[
                    [82, 65],
                    [378, 55],
                    [65, 190],
                    [395, 185],
                    [92, 305],
                    [370, 305],
                  ].map(([x, y], i) => (
                    <g key={i}>
                      <line
                        x1="230"
                        y1="180"
                        x2={x}
                        y2={y}
                        stroke="#93C5FD"
                        strokeOpacity="0.45"
                        strokeWidth="1.5"
                      />
                      <line
                        x1={x}
                        y1={y}
                        x2="230"
                        y2="180"
                        stroke="#2563EB"
                        strokeWidth="2"
                        strokeLinecap="round"
                        className="fr-signal-line"
                      />
                    </g>
                  ))}
                </svg>

                {/* Center Guardian Core */}
                <div className="absolute left-1/2 top-1/2 z-10 -translate-x-1/2 -translate-y-1/2">
                  <GuardianCore state="MONITORING" size="md" />
                </div>

                {/* 6 Business Stream Pills */}
                {HERO_STREAMS.map((s) => (
                  <div
                    key={s.name}
                    style={{ left: s.x, top: s.y }}
                    className="absolute z-20 rounded-xl border border-[#DBEAFE] bg-white/95 px-3 py-1.5 shadow-sm backdrop-blur-xs"
                  >
                    <p className="text-[11px] font-bold text-[#0B1739]">{s.name}</p>
                    <p className="text-[10px] font-medium text-[#64748B]">{s.labelVi}</p>
                  </div>
                ))}
              </div>

              <div className="mt-2 flex flex-wrap items-center justify-between gap-2 rounded-2xl border border-[#EFF6FF] bg-[#F8FAFF] px-4 py-2.5 text-xs text-[#475569]">
                <span>Kết nối 6 luồng dữ liệu nghiệp vụ · Xác minh độc lập</span>
                <Link
                  href="/experience"
                  className="font-semibold text-[#2563EB] hover:text-[#1D4ED8]"
                >
                  Xem dữ liệu thật →
                </Link>
              </div>
            </div>
          </div>
        </div>
      </section>

      {/* 6-Chapter Scroll Storytelling */}
      <LandingScrollStory />

      {/* Editorial Core Capabilities (3 Spacious Pillars, Zero Card Clutter) */}
      <section id="capabilities" className="mx-auto max-w-6xl px-4 py-20 sm:px-6 lg:py-24">
        <div className="max-w-2xl">
          <p className="text-xs font-bold uppercase tracking-[0.16em] text-[#2563EB]">
            Kiến trúc bảo vệ thực chiến
          </p>
          <h2 className="mt-2.5 text-3xl font-bold tracking-tight text-[#0B1739] sm:text-4xl">
            Được xây dựng cho độ tin cậy tài chính, không phải trình diễn hiệu ứng
          </h2>
        </div>

        <div className="mt-12 grid gap-6 lg:grid-cols-3">
          {CORE_CAPABILITIES.map((cap) => (
            <div
              key={cap.eyebrow}
              className="rounded-3xl border border-[#E2E8F0] bg-white p-7 shadow-xs transition hover:border-[#BFDBFE] hover:shadow-md"
            >
              <p className="font-mono text-[11px] font-bold tracking-wider text-[#2563EB]">
                {cap.eyebrow}
              </p>
              <h3 className="mt-3 text-lg font-bold text-[#0B1739]">{cap.title}</h3>
              <p className="mt-2.5 text-sm leading-relaxed text-[#475569]">{cap.desc}</p>
            </div>
          ))}
        </div>
      </section>

      {/* Clean High-Contrast Final CTA */}
      <section className="mx-auto max-w-6xl px-4 pb-20 sm:px-6">
        <div className="relative overflow-hidden rounded-3xl bg-gradient-to-br from-[#0B1739] via-[#1D4ED8] to-[#0284C7] p-8 text-white shadow-xl sm:p-12">
          <div className="relative z-10 max-w-2xl">
            <p className="font-mono text-xs font-semibold uppercase tracking-widest text-[#38BDF8]">
              FinResolve Guardian Live Experience
            </p>
            <h2 className="mt-3 text-2xl font-bold tracking-tight sm:text-3xl">
              Sẵn sàng quan sát AI Guardian bảo vệ doanh nghiệp trực tiếp?
            </h2>
            <p className="mt-3 text-sm leading-relaxed text-blue-100 sm:text-base">
              Mở Guardian Command Center để theo dõi Living Business Network, mua hàng thực trên
              FinShop LIVE và kiểm chứng từng bằng chứng điều tra từ backend thật.
            </p>
            <div className="mt-7 flex flex-wrap gap-3">
              <Link
                href="/experience"
                className="inline-flex items-center gap-2 rounded-2xl bg-white px-6 py-3 text-sm font-semibold text-[#1D4ED8] shadow-sm transition hover:bg-[#EAF3FF]"
              >
                Trải nghiệm Guardian
                <span aria-hidden="true">→</span>
              </Link>
              <Link
                href="/judge"
                className="inline-flex items-center rounded-2xl border border-white/30 bg-white/10 px-5 py-3 text-sm font-semibold text-white transition hover:bg-white/20"
              >
                Mở Judge Lab
              </Link>
            </div>
          </div>
        </div>
      </section>

      <footer className="border-t border-[#E2E8F0] bg-white py-6">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-2 px-4 text-xs text-[#64748B] sm:px-6">
          <span>FinResolve AI — Autonomous Business Guardian · MoMo Tech Contest 2026</span>
          <span>LOCAL_MOCK Sandbox · VNPAY đang tắt có chủ đích</span>
        </div>
      </footer>
    </div>
  );
}
