import assert from "node:assert/strict";
import test from "node:test";
import {
  formatCountdown,
  formatDiscountBadge,
  formatVnd,
  orderStatusLabelVi,
  paymentStatusLabelVi,
} from "./commerce-utils.ts";

test("formatVnd formats integer VND cleanly", () => {
  assert.match(formatVnd(249000), /^249[.,]000đ$/);
  assert.equal(formatVnd(0), "0đ");
});

test("formatDiscountBadge calculates integer percentage discount", () => {
  assert.equal(formatDiscountBadge(500000, 350000), "-30%");
  assert.equal(formatDiscountBadge(200000, 200000), null);
});

test("formatCountdown returns HH:MM:SS or expired label", () => {
  const now = Date.parse("2026-11-24T10:00:00Z");
  assert.equal(formatCountdown("2026-11-24T11:05:09Z", now), "01:05:09");
  assert.equal(formatCountdown("2026-11-24T09:00:00Z", now), "Đã kết thúc");
});

test("order and payment status labels remain strictly separate", () => {
  assert.equal(orderStatusLabelVi("PENDING_PAYMENT"), "Chờ thanh toán");
  assert.equal(orderStatusLabelVi("CONFIRMED"), "Đã xác nhận");
  assert.equal(paymentStatusLabelVi("SUCCEEDED"), "Đã thanh toán (MOCK)");
  assert.equal(paymentStatusLabelVi("UNKNOWN"), "Chưa xác định (UNKNOWN)");
});
