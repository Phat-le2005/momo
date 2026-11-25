/** Typed REST client for FinShop LIVE independent Commerce API (port 8100).
 *
 * Security Invariants (Prompt 10.1):
 * - Bootstrap NEVER returns customer session tokens.
 * - Guest customer sessions are provisioned per-browser via `POST /api/v1/store/customers/guest`
 *   and validated via `GET /api/v1/store/customers/me`.
 * - Merchant Center requires explicit username/password sign-in via `POST /api/v1/merchant/auth/login`
 *   and supports revocable sign-out via `POST /api/v1/merchant/auth/logout`.
 * - Browser NEVER holds payment provider secrets (`X-Mock-Provider-Secret` or `X-Commerce-Service-Key`).
 */

export const COMMERCE_API_BASE =
  process.env.NEXT_PUBLIC_COMMERCE_API_BASE_URL !== undefined
    ? process.env.NEXT_PUBLIC_COMMERCE_API_BASE_URL
    : "http://localhost:8100";

const CUSTOMER_TOKEN_STORAGE_KEY = "finshop_customer_token_v2";
const MERCHANT_TOKEN_STORAGE_KEY = "finshop_merchant_token_v2";

export interface VariantDTO {
  id: string;
  product_id: string;
  sku: string;
  title: string;
  price_vnd: number;
  compare_at_price_vnd: number | null;
  effective_price_vnd: number;
  is_flash_sale: boolean;
  image_url: string;
  on_hand_qty: number;
  reserved_qty: number;
  available_qty: number;
  low_stock_threshold: number;
  is_active: boolean;
}

export interface ProductDTO {
  id: string;
  merchant_id: string;
  slug: string;
  title: string;
  category: string;
  description: string;
  image_urls: string[];
  is_featured: boolean;
  is_active: boolean;
  is_test_data: boolean;
  min_price_vnd: number;
  max_price_vnd: number;
  total_available_qty: number;
  variants: VariantDTO[];
}

export interface VoucherDTO {
  id: string;
  code: string;
  title: string;
  discount_type: string;
  discount_value: number;
  max_discount_vnd: number | null;
  min_order_vnd: number;
  usage_limit: number;
  used_count: number;
  per_customer_limit: number;
  starts_at: string;
  ends_at: string;
  is_active: boolean;
}

export interface CustomerIdentityDTO {
  id: string;
  merchant_id: string;
  commerce_session_id: string;
  display_name: string;
  phone: string;
  email: string;
  default_address: string;
  session_token: string;
  is_test_identity: boolean;
}

export interface PaymentMethodDTO {
  id: string;
  label_vi: string;
  available: boolean;
  is_mock?: boolean;
  supports_redirect?: boolean;
  refund_enabled?: boolean;
}

export interface StoreBootstrapDTO {
  brand: string;
  source_mode: string;
  merchant: { id: string; name: string; description: string };
  commerce_session: { id: string; world_epoch: number };
  categories: string[];
  payment_methods: PaymentMethodDTO[];
}

export interface CartLineDTO {
  variant_id: string;
  product_id: string;
  product_title: string;
  variant_title: string;
  sku: string;
  image_url: string;
  quantity: number;
  unit_price_vnd: number;
  original_price_vnd: number;
  is_flash_sale_price: boolean;
  line_total_vnd: number;
  available_qty: number;
}

export interface CartDTO {
  cart_id: string;
  items: CartLineDTO[];
  subtotal_vnd: number;
  discount_vnd: number;
  shipping_fee_vnd: number;
  total_vnd: number;
  applied_voucher_code: string | null;
  voucher_title: string | null;
  voucher_error: string | null;
}

export interface PaymentDTO {
  id: string;
  order_id: string;
  merchant_id: string;
  provider: string;
  provider_environment: string;
  provider_ref: string;
  provider_txn_no: string | null;
  provider_txn_date: string | null;
  provider_response_code: string | null;
  provider_transaction_status: string | null;
  observation_source: string;
  signature_verified: boolean;
  last_observed_at: string | null;
  redirect_url: string | null;
  correlation_id: string;
  amount_vnd: number;
  refunded_amount_vnd: number;
  refundable_remaining_vnd: number;
  status: string;
  is_synced_to_order: boolean;
  reconciliation_required: boolean;
  reconciliation_note: string | null;
  confirmed_at: string | null;
  created_at: string;
}

export interface PaymentQueryAttemptDTO {
  id: string;
  payment_id: string;
  order_id: string;
  provider: string;
  request_id: string;
  command: string;
  http_status: number | null;
  provider_response_code: string | null;
  provider_transaction_status: string | null;
  signature_verified: boolean;
  mapped_payment_status: string | null;
  observation_provenance: string;
  error_message: string | null;
  created_at: string;
}

export interface OrderItemDTO {
  id: string;
  product_id: string;
  variant_id: string;
  sku_snapshot: string;
  product_title_snapshot: string;
  variant_title_snapshot: string;
  image_url_snapshot: string;
  unit_price_vnd: number;
  original_price_vnd: number;
  is_flash_sale_price: boolean;
  quantity: number;
  line_total_vnd: number;
}

export interface OrderDTO {
  id: string;
  order_code: string;
  merchant_id: string;
  commerce_session_id: string;
  customer_id: string;
  status: string;
  payment_status: string;
  reconciliation_status: string;
  reconciliation_reason: string | null;
  subtotal_vnd: number;
  discount_vnd: number;
  shipping_fee_vnd: number;
  total_vnd: number;
  voucher_code: string | null;
  recipient_name: string;
  recipient_phone: string;
  shipping_address: string;
  customer_note: string;
  payment_method: string;
  correlation_id: string;
  version: number;
  items: OrderItemDTO[];
  payment: PaymentDTO | null;
  created_at: string;
  updated_at: string;
}

export interface LiveProductItemDTO {
  id: string;
  product_id: string;
  variant_id: string;
  product_title: string;
  variant_title: string;
  sku: string;
  image_url: string;
  original_price_vnd: number;
  effective_price_vnd: number;
  flash_sale_price_vnd: number | null;
  flash_sale_stock_limit: number | null;
  flash_sale_reserved_qty: number;
  flash_sale_sold_qty: number;
  flash_sale_remaining: number | null;
  flash_sale_starts_at: string | null;
  flash_sale_ends_at: string | null;
  available_qty: number;
  is_pinned: boolean;
  sort_order: number;
}

export interface LiveCommentDTO {
  id: string;
  author_name: string;
  message: string;
  created_at: string;
}

export interface LiveSessionDTO {
  id: string;
  merchant_id: string;
  merchant_name: string;
  title: string;
  host_name: string;
  status: string;
  is_simulated_stream: boolean;
  media_label_vi: string;
  starts_at: string;
  ends_at: string;
  active_participants_count: number;
  session_orders_count: number;
  pinned_product: LiveProductItemDTO | null;
  products: LiveProductItemDTO[];
  recent_comments: LiveCommentDTO[];
}

export interface ComplaintDTO {
  id: string;
  order_id: string;
  customer_id: string;
  category: string;
  description: string;
  claimed_receipt_ref: string | null;
  evidence_kind: string;
  status: string;
  merchant_response: string;
  created_at: string;
  updated_at: string;
}

export interface RefundRequestDTO {
  id: string;
  order_id: string;
  payment_id: string;
  customer_id: string;
  requested_amount_vnd: number;
  reason: string;
  status: string;
  merchant_note: string;
  created_at: string;
  updated_at: string;
}

export interface MerchantOverviewDTO {
  merchant_id: string;
  commerce_session_id: string;
  source_mode: string;
  metrics_note_vi: string;
  confirmed_revenue_vnd: number;
  order_count: number;
  pending_payments: number;
  pending_fulfillment: number;
  unsynced_payments: number;
  reconciliation_required_count: number;
  outbox_pending_count: number;
  outbox_dead_letter_count: number;
  refund_requests: number;
  open_complaints: number;
  total_available_inventory: number;
  low_stock_products_count: number;
  live_session_orders_count: number;
}

export interface MerchantLoginDTO {
  session_id: string;
  merchant_id: string;
  commerce_session_id: string;
  role: string;
  operator_id: string;
  merchant_token: string;
  expires_at: string;
  source_mode: string;
}

export interface CommerceEventDTO {
  id: string;
  event_id: string;
  event_type: string;
  source: string;
  merchant_id: string;
  commerce_session_id: string;
  world_epoch: number;
  order_id: string | null;
  payment_id: string | null;
  correlation_id: string | null;
  occurred_at: string;
  payload_json: Record<string, unknown>;
  outbox_status: string | null;
  failure_category: string | null;
  finresolve_mapped_type: string | null;
}

export function getStoredCustomerToken(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(CUSTOMER_TOKEN_STORAGE_KEY);
}

export function setStoredCustomerToken(token: string): void {
  if (typeof window !== "undefined") {
    window.localStorage.setItem(CUSTOMER_TOKEN_STORAGE_KEY, token);
  }
}

export function clearStoredCustomerToken(): void {
  if (typeof window !== "undefined") {
    window.localStorage.removeItem(CUSTOMER_TOKEN_STORAGE_KEY);
  }
}

export function getStoredMerchantToken(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(MERCHANT_TOKEN_STORAGE_KEY);
}

export function setStoredMerchantToken(token: string): void {
  if (typeof window !== "undefined") {
    window.localStorage.setItem(MERCHANT_TOKEN_STORAGE_KEY, token);
  }
}

export function clearStoredMerchantToken(): void {
  if (typeof window !== "undefined") {
    window.localStorage.removeItem(MERCHANT_TOKEN_STORAGE_KEY);
  }
}

export async function commerceFetch<T>(
  path: string,
  options: RequestInit & { customerToken?: string | null; merchantToken?: string | null } = {},
): Promise<T> {
  const headers = new Headers(options.headers);
  if (options.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const cTok =
    options.customerToken !== undefined ? options.customerToken : getStoredCustomerToken();
  if (cTok && !headers.has("X-Customer-Token")) {
    headers.set("X-Customer-Token", cTok);
  }
  const mTok =
    options.merchantToken !== undefined ? options.merchantToken : getStoredMerchantToken();
  if (mTok && !headers.has("X-Merchant-Token")) {
    headers.set("X-Merchant-Token", mTok);
  }

  const res = await fetch(`${COMMERCE_API_BASE}${path}`, {
    ...options,
    headers,
    credentials: "include",
    cache: "no-store",
  });

  if (!res.ok) {
    let msg = `Lỗi HTTP ${res.status}`;
    try {
      const errBody = (await res.json()) as { error?: { message?: string }; detail?: string };
      msg = errBody.error?.message || errBody.detail || msg;
    } catch {
      // ignore json parse error
    }
    throw new Error(msg);
  }
  return (await res.json()) as T;
}

/** Load public storefront bootstrap and ensure an isolated guest customer session exists. */
export async function ensureCustomerBootstrap(): Promise<{
  bootstrap: StoreBootstrapDTO;
  activeCustomer: CustomerIdentityDTO;
}> {
  const bootstrap = await commerceFetch<StoreBootstrapDTO>("/api/v1/store/bootstrap", {
    customerToken: null,
  });

  const existingTok = getStoredCustomerToken();
  if (existingTok) {
    try {
      const me = await commerceFetch<CustomerIdentityDTO>("/api/v1/store/customers/me", {
        customerToken: existingTok,
      });
      return { bootstrap, activeCustomer: me };
    } catch {
      clearStoredCustomerToken();
    }
  }

  // Provision a fresh isolated guest customer session for this browser only
  const guest = await commerceFetch<CustomerIdentityDTO>("/api/v1/store/customers/guest", {
    method: "POST",
    customerToken: null,
    body: JSON.stringify({
      display_name: "Khách FinShop",
      phone: "0901234567",
      default_address: "123 Nguyễn Huệ, Quận 1, TP. Hồ Chí Minh",
    }),
  });
  setStoredCustomerToken(guest.session_token);
  return { bootstrap, activeCustomer: guest };
}

export async function loginMerchant(
  username: string,
  password: string,
): Promise<MerchantLoginDTO> {
  const res = await commerceFetch<MerchantLoginDTO>("/api/v1/merchant/auth/login", {
    method: "POST",
    merchantToken: null,
    body: JSON.stringify({ username, password }),
  });
  setStoredMerchantToken(res.merchant_token);
  return res;
}

export async function logoutMerchant(): Promise<void> {
  const tok = getStoredMerchantToken();
  if (tok) {
    try {
      await commerceFetch("/api/v1/merchant/auth/logout", {
        method: "POST",
        merchantToken: tok,
      });
    } catch {
      // ignore if already expired
    }
  }
  clearStoredMerchantToken();
}
