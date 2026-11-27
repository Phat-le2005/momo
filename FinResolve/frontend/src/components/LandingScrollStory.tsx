"use client";

import { useEffect, useRef, useState } from "react";
import type { GuardianVisualState } from "@/components/GuardianBadge";
import { GuardianCore } from "@/components/GuardianCore";

interface StoryChapter {
  id: string;
  step: string;
  title: string;
  subtitle: string;
  description: string;
  state: GuardianVisualState;
  activeStreams: string[];
  evidenceNote: string;
}

const CHAPTERS: StoryChapter[] = [
  {
    id: "operating",
    step: "01",
    title: "Doanh nghiệp đang hoạt động",
    subtitle: "Hàng loạt đơn hàng, thanh toán và cập nhật tồn kho diễn ra liên tục.",
    description:
      "FinResolve AI âm thầm kết nối qua kênh chỉ đọc có xác thực, thiết lập baseline vận hành bình thường mà không làm chậm trải nghiệm mua sắm của khách hàng.",
    state: "MONITORING",
    activeStreams: ["Orders", "Payments", "Customers", "Inventory"],
    evidenceNote: "Baseline vận hành bình thường được học từ telemetry thực.",
  },
  {
    id: "anomaly",
    step: "02",
    title: "Một bất thường xuất hiện",
    subtitle: "Khách đã thanh toán thành công nhưng đơn hàng vẫn kẹt ở trạng thái chờ.",
    description:
      "Trong thương mại điện tử thực tế, rớt webhook hoặc trễ đồng bộ dễ khiến khách bị trừ tiền mà đơn chưa xác nhận — dẫn tới khiếu nại và mất niềm tin.",
    state: "ANOMALY_DETECTED",
    activeStreams: ["Orders", "Payments"],
    evidenceNote: "Chênh lệch trạng thái giữa bản ghi thanh toán và bản ghi đơn hàng.",
  },
  {
    id: "detected",
    step: "03",
    title: "Guardian phát hiện vấn đề",
    subtitle: "Bộ phát hiện lai (Rule + Behavioral Baseline) khoanh vùng chính xác đối tượng.",
    description:
      "Thay vì chờ khách hàng gửi khiếu nại thủ công, Continuous Monitor phát hiện độ lệch chỉ sau vài giây và khởi tạo hồ sơ điều tra cô lập theo phiên.",
    state: "ANOMALY_DETECTED",
    activeStreams: ["Orders", "Payments", "Event Outbox"],
    evidenceNote: "Định vị chính xác mã đơn hàng, mã giao dịch và phiên bản dữ liệu.",
  },
  {
    id: "investigating",
    step: "04",
    title: "AI điều tra bằng chứng",
    subtitle: "Gemini đọc trực tiếp dữ liệu xác thực từ nhiều nguồn trước khi đề xuất.",
    description:
      "AI không bao giờ tin lời khai một phía hay ảnh chụp biên lai chưa kiểm chứng. Hệ thống gọi công cụ đọc độc lập để đối chiếu đơn hàng, thanh toán và chính sách.",
    state: "INVESTIGATING",
    activeStreams: ["Orders", "Payments", "Policies"],
    evidenceNote: "Bắt buộc thu thập đủ bằng chứng đơn hàng + thanh toán trong cùng lượt chạy.",
  },
  {
    id: "recovering",
    step: "05",
    title: "Hệ thống thực hiện hành động an toàn",
    subtitle: "Policy Engine thẩm định và ký lệnh HMAC-SHA256 với kiểm soát phiên bản.",
    description:
      "Mọi lệnh ghi đều đi qua Policy Engine, khóa bất biến idempotency và kiểm tra optimistic version. Trường hợp rủi ro cao luôn dừng lại chờ con người phê duyệt.",
    state: "RECOVERING",
    activeStreams: ["Policies", "Orders"],
    evidenceNote: "Chữ ký HMAC-SHA256 · Khóa chống ghi trùng · Fail-closed khi thiếu quyền.",
  },
  {
    id: "verified",
    step: "06",
    title: "Kết quả được kiểm chứng",
    subtitle: "Independent Verifier đọc lại trạng thái thật trước khi đóng sự cố.",
    description:
      "FinResolve không bao giờ đánh dấu thành công chỉ vì nhận HTTP 200. Verifier độc lập đọc lại đơn hàng ở trạng thái CONFIRMED để chắc chắn doanh nghiệp đã ổn định.",
    state: "VERIFIED",
    activeStreams: ["Orders", "Payments", "Customers", "Policies"],
    evidenceNote: "Trạng thái VERIFIED chỉ bật khi Verifier độc lập xác nhận phục hồi thật.",
  },
];

/**
 * 6-Chapter Scroll Storytelling for FinResolve Landing Page.
 * Non-scroll-locking IntersectionObserver + manual click selection.
 * Explicitly labeled as product illustration ("Minh họa sản phẩm").
 */
export function LandingScrollStory() {
  const [activeIndex, setActiveIndex] = useState(0);
  const refs = useRef<Array<HTMLDivElement | null>>([]);

  useEffect(() => {
    if (typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) {
            const idx = Number(entry.target.getAttribute("data-chapter-idx"));
            if (!Number.isNaN(idx)) {
              setActiveIndex(idx);
            }
          }
        }
      },
      { rootMargin: "-35% 0px -45% 0px", threshold: 0.1 },
    );

    for (const el of refs.current) {
      if (el) observer.observe(el);
    }
    return () => observer.disconnect();
  }, []);

  const current = CHAPTERS[activeIndex] ?? CHAPTERS[0];

  return (
    <section
      id="story"
      aria-label="Hành trình bảo vệ doanh nghiệp của FinResolve AI"
      className="border-y border-[#DBEAFE]/80 bg-white py-20 lg:py-28"
    >
      <div className="mx-auto max-w-6xl px-4 sm:px-6">
        <div className="flex flex-wrap items-end justify-between gap-4">
          <div className="max-w-2xl">
            <p className="text-xs font-bold uppercase tracking-[0.16em] text-[#2563EB]">
              Khám phá cách hoạt động
            </p>
            <h2 className="mt-2.5 text-3xl font-bold tracking-tight text-[#0B1739] sm:text-4xl">
              Từ lúc bất thường xuất hiện đến khi doanh nghiệp phục hồi hoàn toàn
            </h2>
          </div>
          <span className="inline-flex items-center gap-1.5 rounded-full border border-[#DBEAFE] bg-[#EAF3FF] px-3 py-1 text-xs font-semibold text-[#1D4ED8]">
            Minh họa sản phẩm
          </span>
        </div>

        <div className="mt-12 grid items-start gap-10 lg:grid-cols-12">
          {/* Left: Scrollable 6 Chapters */}
          <div className="space-y-4 lg:col-span-6">
            {CHAPTERS.map((ch, idx) => {
              const isActive = idx === activeIndex;
              return (
                <div
                  key={ch.id}
                  ref={(el) => {
                    refs.current[idx] = el;
                  }}
                  data-chapter-idx={idx}
                  onClick={() => setActiveIndex(idx)}
                  role="button"
                  tabIndex={0}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      setActiveIndex(idx);
                    }
                  }}
                  className={`cursor-pointer rounded-2xl border p-5 transition-all duration-300 ${
                    isActive
                      ? "border-[#2563EB] bg-gradient-to-br from-[#EAF3FF]/70 via-white to-white shadow-md shadow-blue-950/5"
                      : "border-[#E2E8F0] bg-[#F8FAFF]/60 hover:border-[#BFDBFE] hover:bg-white"
                  }`}
                >
                  <div className="flex items-center gap-3">
                    <span
                      className={`inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-xl text-xs font-bold transition-colors ${
                        isActive
                          ? "bg-[#2563EB] text-white"
                          : "bg-[#EAF3FF] text-[#1D4ED8]"
                      }`}
                    >
                      {ch.step}
                    </span>
                    <div>
                      <h3 className="text-base font-bold text-[#0B1739]">{ch.title}</h3>
                      <p className="text-xs font-medium text-[#2563EB]">{ch.subtitle}</p>
                    </div>
                  </div>
                  <p className="mt-3 text-sm leading-relaxed text-[#475569]">{ch.description}</p>
                </div>
              );
            })}
          </div>

          {/* Right: Sticky Live-State Visual Stage */}
          <div className="lg:sticky lg:top-24 lg:col-span-6">
            <div className="fr-grid-surface relative overflow-hidden rounded-3xl border border-[#DBEAFE] bg-gradient-to-b from-[#F8FAFF] via-white to-[#EAF3FF]/40 p-6 shadow-xl shadow-blue-950/5 sm:p-8">
              <div className="flex items-center justify-between gap-2">
                <span className="font-mono text-xs font-bold text-[#2563EB]">
                  GIAI ĐOẠN {current.step} / 06
                </span>
                <span className="rounded-full border border-[#DBEAFE] bg-white px-2.5 py-0.5 text-[11px] font-semibold text-[#64748B]">
                  Minh họa sản phẩm
                </span>
              </div>

              <div className="my-6 flex justify-center">
                <GuardianCore state={current.state} size="lg" showCaption />
              </div>

              <div className="rounded-2xl border border-[#DBEAFE] bg-white/95 p-4 shadow-xs">
                <p className="text-[11px] font-bold uppercase tracking-wider text-[#64748B]">
                  Luồng dữ liệu liên quan
                </p>
                <div className="mt-2.5 flex flex-wrap gap-2">
                  {["Orders", "Payments", "Customers", "Refunds", "Inventory", "Policies"].map(
                    (stream) => {
                      const active = current.activeStreams.includes(stream);
                      return (
                        <span
                          key={stream}
                          className={`rounded-lg border px-2.5 py-1 text-xs font-semibold transition-all ${
                            active
                              ? current.state === "ANOMALY_DETECTED"
                                ? "border-[#FECACA] bg-[#FEF2F2] text-[#B91C1C]"
                                : current.state === "VERIFIED"
                                  ? "border-[#A7F3D0] bg-[#ECFDF5] text-[#047857]"
                                  : "border-[#BFDBFE] bg-[#EAF3FF] text-[#1D4ED8]"
                              : "border-[#E2E8F0] bg-[#F8FAFF] text-[#94A3B8]"
                          }`}
                        >
                          {stream}
                        </span>
                      );
                    },
                  )}
                </div>
                <p className="mt-3 border-t border-[#F1F5F9] pt-2.5 text-xs text-[#475569]">
                  {current.evidenceNote}
                </p>
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}
