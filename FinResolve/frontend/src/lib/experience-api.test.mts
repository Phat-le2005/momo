import assert from "node:assert/strict";
import test from "node:test";
import {
  computeAdaptivePollIntervalMs,
  detectOverviewToasts,
  formatMetricOrEmpty,
  formatPercentOrEmpty,
  isStaleResponse,
  translateBusinessEventVi,
  type OverviewToastInput,
} from "./experience-utils.ts";

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
