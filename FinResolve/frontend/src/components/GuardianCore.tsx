"use client";

import { useState } from "react";
import type { GuardianVisualState } from "@/components/GuardianBadge";

type CorePalette = {
  primary: string;
  secondary: string;
  glow: string;
  orbit: string;
  surface: string;
  labelVi: string;
  sublabelVi: string;
  activeOrbit: boolean;
  fastOrbit: boolean;
  verifyRipple: boolean;
};

const CORE_PALETTES: Record<GuardianVisualState, CorePalette> = {
  MONITORING: {
    primary: "#2563EB",
    secondary: "#38BDF8",
    glow: "rgba(37, 99, 235, 0.22)",
    orbit: "#93C5FD",
    surface: "#EAF3FF",
    labelVi: "AI Guardian đang giám sát",
    sublabelVi: "Quan sát liên tục luồng dữ liệu kinh doanh",
    activeOrbit: true,
    fastOrbit: false,
    verifyRipple: false,
  },
  ANOMALY_DETECTED: {
    primary: "#EF4444",
    secondary: "#F59E0B",
    glow: "rgba(239, 68, 68, 0.24)",
    orbit: "#FCA5A5",
    surface: "#FEF2F2",
    labelVi: "Phát hiện tín hiệu bất thường",
    sublabelVi: "Đã định vị vùng ảnh hưởng để phân tích",
    activeOrbit: true,
    fastOrbit: true,
    verifyRipple: false,
  },
  INVESTIGATING: {
    primary: "#0284C7",
    secondary: "#38BDF8",
    glow: "rgba(2, 132, 199, 0.26)",
    orbit: "#38BDF8",
    surface: "#E0F2FE",
    labelVi: "Đang điều tra bằng chứng",
    sublabelVi: "Đối chiếu dữ liệu xác thực từ các nguồn nghiệp vụ",
    activeOrbit: true,
    fastOrbit: true,
    verifyRipple: false,
  },
  RECOVERING: {
    primary: "#1D4ED8",
    secondary: "#2563EB",
    glow: "rgba(29, 78, 216, 0.28)",
    orbit: "#60A5FA",
    surface: "#DBEAFE",
    labelVi: "Thực thi phục hồi an toàn",
    sublabelVi: "Policy Engine đã ký lệnh và đang đồng bộ",
    activeOrbit: true,
    fastOrbit: true,
    verifyRipple: false,
  },
  WAITING_APPROVAL: {
    primary: "#F59E0B",
    secondary: "#FBBF24",
    glow: "rgba(245, 158, 11, 0.24)",
    orbit: "#FCD34D",
    surface: "#FFFBEB",
    labelVi: "Chờ con người phê duyệt",
    sublabelVi: "Cần xác nhận trước khi thực thi thay đổi nhạy cảm",
    activeOrbit: false,
    fastOrbit: false,
    verifyRipple: false,
  },
  VERIFIED: {
    primary: "#10B981",
    secondary: "#34D399",
    glow: "rgba(16, 185, 129, 0.24)",
    orbit: "#6EE7B7",
    surface: "#ECFDF5",
    labelVi: "Đã xác minh phục hồi",
    sublabelVi: "Verifier độc lập xác nhận hệ thống đã ổn định",
    activeOrbit: true,
    fastOrbit: false,
    verifyRipple: true,
  },
  DISCONNECTED: {
    primary: "#94A3B8",
    secondary: "#EF4444",
    glow: "rgba(148, 163, 184, 0.16)",
    orbit: "#CBD5E1",
    surface: "#F1F5F9",
    labelVi: "Gián đoạn kết nối giám sát",
    sublabelVi: "Không thể xác nhận trạng thái từ nguồn dữ liệu",
    activeOrbit: false,
    fastOrbit: false,
    verifyRipple: false,
  },
};

export interface GuardianCoreProps {
  state: GuardianVisualState;
  size?: "sm" | "md" | "lg";
  interactive?: boolean;
  showCaption?: boolean;
  className?: string;
}

const SIZE_MAP = {
  sm: 112,
  md: 176,
  lg: 232,
};

/**
 * FinResolve Signature AI Guardian Core — pure vector SVG + CSS GPU-accelerated emblem.
 * Zero WebGL overhead; degrades gracefully under prefers-reduced-motion.
 */
export function GuardianCore({
  state,
  size = "md",
  interactive = true,
  showCaption = false,
  className = "",
}: GuardianCoreProps) {
  const palette = CORE_PALETTES[state] ?? CORE_PALETTES.DISCONNECTED;
  const px = SIZE_MAP[size];
  const [tilt, setTilt] = useState({ x: 0, y: 0 });

  const handleMouseMove = (e: React.MouseEvent<HTMLDivElement>) => {
    if (!interactive) return;
    const rect = e.currentTarget.getBoundingClientRect();
    const nx = ((e.clientX - rect.left) / rect.width - 0.5) * 2;
    const ny = ((e.clientY - rect.top) / rect.height - 0.5) * 2;
    setTilt({ x: ny * -5, y: nx * 5 });
  };

  const resetTilt = () => setTilt({ x: 0, y: 0 });

  return (
    <div
      className={`inline-flex flex-col items-center justify-center select-none ${className}`}
      onMouseMove={handleMouseMove}
      onMouseLeave={resetTilt}
      role="img"
      aria-label={`Guardian Core — ${palette.labelVi}`}
    >
      <div
        className="relative flex items-center justify-center transition-transform duration-300 ease-out"
        style={{
          width: px,
          height: px,
          transform: interactive
            ? `perspective(600px) rotateX(${tilt.x}deg) rotateY(${tilt.y}deg)`
            : undefined,
        }}
      >
        {/* Ambient radial light */}
        <div
          aria-hidden="true"
          className={`pointer-events-none absolute inset-2 rounded-full blur-2xl ${
            palette.activeOrbit ? "fr-ambient-glow" : ""
          }`}
          style={{ background: palette.glow }}
        />

        {/* Verification ripple ring */}
        {palette.verifyRipple && (
          <div
            aria-hidden="true"
            className="fr-verify-ring pointer-events-none absolute inset-4 rounded-full border-2"
            style={{ borderColor: palette.primary }}
          />
        )}

        <svg
          viewBox="0 0 220 220"
          fill="none"
          className="relative z-10 h-full w-full drop-shadow-sm"
          aria-hidden="true"
        >
          <defs>
            <linearGradient id={`gc-shield-${state}`} x1="60" y1="45" x2="160" y2="175" gradientUnits="userSpaceOnUse">
              <stop offset="0%" stopColor={palette.primary} />
              <stop offset="100%" stopColor={palette.secondary} />
            </linearGradient>
            <radialGradient id={`gc-core-${state}`} cx="50%" cy="45%" r="55%">
              <stop offset="0%" stopColor="#FFFFFF" stopOpacity="0.98" />
              <stop offset="65%" stopColor={palette.surface} stopOpacity="0.92" />
              <stop offset="100%" stopColor={palette.orbit} stopOpacity="0.35" />
            </radialGradient>
          </defs>

          {/* Outer precision telemetry ring */}
          <g
            style={{ transformOrigin: "110px 110px" }}
            className={
              palette.activeOrbit
                ? palette.fastOrbit
                  ? "fr-spin-medium"
                  : "fr-spin-slow"
                : ""
            }
          >
            <circle
              cx="110"
              cy="110"
              r="98"
              stroke={palette.orbit}
              strokeWidth="1.2"
              strokeDasharray="4 8"
              opacity="0.7"
            />
            <path
              d="M 110 12 A 98 98 0 0 1 195 61"
              stroke={palette.primary}
              strokeWidth="2.2"
              strokeLinecap="round"
            />
            <circle cx="195" cy="61" r="3.5" fill={palette.primary} />
            <circle cx="25" cy="159" r="2.8" fill={palette.secondary} />
          </g>

          {/* Middle counter-rotating orbital ring */}
          <g
            style={{ transformOrigin: "110px 110px" }}
            className={palette.activeOrbit ? "fr-spin-reverse-slow" : ""}
          >
            <circle
              cx="110"
              cy="110"
              r="78"
              stroke={palette.primary}
              strokeOpacity="0.22"
              strokeWidth="1.25"
            />
            <path
              d="M 110 188 A 78 78 0 0 1 42 149"
              stroke={palette.secondary}
              strokeWidth="2"
              strokeLinecap="round"
            />
            <circle cx="110" cy="32" r="2.8" fill={palette.primary} />
            <circle cx="177" cy="149" r="2.5" fill={palette.secondary} />
          </g>

          {/* Inner luminous sphere */}
          <circle
            cx="110"
            cy="110"
            r="60"
            fill={`url(#gc-core-${state})`}
            stroke={palette.primary}
            strokeOpacity="0.24"
            strokeWidth="1.5"
          />

          {/* Signature Futuristic Shield + Neural Lattice */}
          <g className={palette.activeOrbit ? "fr-pulse-soft" : ""} style={{ transformOrigin: "110px 110px" }}>
            <path
              d="M110 64L74 79V109C74 133.5 89.8 154.8 110 162C130.2 154.8 146 133.5 146 109V79L110 64Z"
              fill="#FFFFFF"
              fillOpacity="0.92"
              stroke={`url(#gc-shield-${state})`}
              strokeWidth="3.2"
              strokeLinejoin="round"
            />
            {/* Internal AI network mesh */}
            <path
              d="M110 92L93 112M110 92L127 112M93 112L110 132M127 112L110 132M93 112H127"
              stroke={palette.primary}
              strokeOpacity="0.55"
              strokeWidth="1.6"
              strokeLinecap="round"
            />
            <circle cx="110" cy="92" r="4.5" fill={palette.primary} />
            <circle cx="93" cy="112" r="3.6" fill={palette.secondary} />
            <circle cx="127" cy="112" r="3.6" fill={palette.secondary} />
            <circle cx="110" cy="132" r="4" fill={palette.primary} />

            {/* Disconnected slash indicator */}
            {state === "DISCONNECTED" && (
              <path
                d="M84 138L136 86"
                stroke="#EF4444"
                strokeWidth="3"
                strokeLinecap="round"
              />
            )}
            {/* Verified check mark overlay */}
            {state === "VERIFIED" && (
              <path
                d="M99 112L107 120L123 102"
                stroke="#10B981"
                strokeWidth="3.2"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            )}
          </g>
        </svg>
      </div>

      {showCaption && (
        <div className="mt-3 text-center">
          <p className="text-sm font-bold text-[#0B1739]">{palette.labelVi}</p>
          <p className="mt-0.5 text-xs text-[#64748B]">{palette.sublabelVi}</p>
        </div>
      )}
    </div>
  );
}
