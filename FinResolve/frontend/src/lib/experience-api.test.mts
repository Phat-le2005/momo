import assert from "node:assert/strict";
import test from "node:test";
import {
  computeAdaptivePollIntervalMs,
  describeVnpayCapability,
  detectOverviewToasts,
  formatMetricOrEmpty,
  formatPercentOrEmpty,
  isStaleResponse,
  translateBusinessEventVi,
  type OverviewToastInput,
} from "./experience-utils.ts";

test("Guardian separates VNPAY configuration, verified evidence, and unavailable QueryDR", () => {
  const capability = {
    vnpay_configured: true,
    vnpay_evidence_sources: ["VNPAY_IPN" as const],
    vnpay_querydr_available: false,
  };
  assert.deepEqual(describeVnpayCapability(capability, false), {
    configuration: "Đã cấu hình Sandbox",
    evidence: "Đã xác minh chữ ký qua IPN",
    querydr: "Không khả dụng trong cấu hình hiện tại",
  });
  assert.equal(describeVnpayCapability(capability, true).configuration, "Chế độ chỉ xem");
});

test("Guardian distinguishes unknown capability from enabled QueryDR without live evidence", () => {
  assert.deepEqual(describeVnpayCapability(undefined, false), {
    configuration: "Chưa rõ cấu hình",
    evidence: "Chưa đọc được bằng chứng",
    querydr: "Chưa rõ cấu hình",
  });
  const configured = {
    vnpay_configured: true,
    vnpay_querydr_available: true,
  };
  assert.equal(
    describeVnpayCapability({ ...configured, vnpay_evidence_sources: [] }, false).querydr,
    "Đã bật trong cấu hình, chưa xác minh live",
  );
  assert.equal(
    describeVnpayCapability({ ...configured, vnpay_evidence_sources: ["VNPAY_QUERYDR"] }, false).querydr,
    "Đã ghi nhận phản hồi có chữ ký hợp lệ",
  );
});

function makeOverview(
  overrides: Partial<OverviewToastInput> = {},
): OverviewToastInput {
  return {
    session_id: "sess_test_1",
    world_epoch: 1,
    updated_at: "2026-11-26T12:00:00Z",
    kpis: {
      total_anomalies_detected: 0,
      verified_recoveries: 0,
    },
    ...overrides,
  };
}

test("Prompt 15: adaptive poll interval speeds up when active and backs off on errors", () => {
  assert.equal(computeAdaptivePollIntervalMs("MONITORING", 0, 0), 10_000);
  assert.equal(computeAdaptivePollIntervalMs("INVESTIGATING", 0, 0), 4_000);
  assert.equal(computeAdaptivePollIntervalMs("RECOVERING", 0, 0), 4_000);
  assert.equal(computeAdaptivePollIntervalMs("MONITORING", 2, 0), 4_000);
  assert.equal(computeAdaptivePollIntervalMs("MONITORING", 0, 1), 8_000);
  assert.equal(computeAdaptivePollIntervalMs("MONITORING", 0, 5), 30_000);
});

test("Prompt 15: stale response guard prevents out-of-order responses from overwriting state", () => {
  assert.equal(isStaleResponse(1, 2), true);
  assert.equal(isStaleResponse(2, 2), false);
  assert.equal(isStaleResponse(3, 2), false);
});

test("Prompt 15: metric formatters never fabricate numbers for null/undefined values", () => {
  assert.equal(formatMetricOrEmpty(null), "Chưa có dữ liệu");
  assert.equal(formatMetricOrEmpty(undefined, " ms"), "Chưa có dữ liệu");
  assert.equal(formatMetricOrEmpty(120, " ms"), "120 ms");
  assert.equal(formatPercentOrEmpty(null), "Chưa có dữ liệu");
  assert.equal(formatPercentOrEmpty(0.125), "12.5%");
});

test("Prompt 15: toast detector emits alerts only within same session & world_epoch", () => {
  const prev = makeOverview();
  const nextSame = makeOverview({
    updated_at: "2026-11-26T12:00:10Z",
    kpis: { ...prev.kpis, total_anomalies_detected: 1, verified_recoveries: 1 },
  });
  const toasts = detectOverviewToasts(prev, nextSame);
  assert.equal(toasts.length, 2);
  assert.equal(toasts[0].tone, "warning");
  assert.equal(toasts[1].tone, "success");

  // Cross-epoch reset must not fire misleading toasts
  const nextEpoch = makeOverview({
    world_epoch: 2,
    kpis: { ...prev.kpis, total_anomalies_detected: 3 },
  });
  assert.equal(detectOverviewToasts(prev, nextEpoch).length, 0);
});

test("Prompt 15: business event translation returns Vietnamese labels with safe fallback", () => {
  assert.equal(translateBusinessEventVi("ORDER_CREATED"), "Khách tạo đơn hàng mới");
  assert.equal(translateBusinessEventVi("CUSTOM_RAW_EVENT"), "CUSTOM_RAW_EVENT");
});
