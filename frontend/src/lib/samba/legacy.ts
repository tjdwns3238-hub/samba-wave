/**
 * SambaWave API client — JWT 인증 필수
 */

import { API_BASE_URL, API_GATEWAY_KEY } from '@/config/api'
import { STORAGE_KEYS } from '@/lib/samba/constants'
import { getDeviceId } from '@/lib/samba/deviceId'

export const API_BASE = API_BASE_URL

export const SAMBA_PREFIX = `${API_BASE}/api/v1/samba`;

/** localStorage에서 JWT 액세스 토큰 추출 */
function getAccessToken(): string | null {
  if (typeof window === 'undefined') return null
  try {
    const raw = localStorage.getItem(STORAGE_KEYS.SAMBA_USER)
    if (!raw) return null
    const user = JSON.parse(raw)
    return user?.access_token ?? null
  } catch {
    return null
  }
}

/** JWT 인증이 포함된 fetch — Response 그대로 반환 (SSE·FormData 등 raw 응답 필요 시 사용) */
export async function fetchWithAuth(url: string, init?: RequestInit): Promise<Response> {
  const token = getAccessToken()
  const headers: Record<string, string> = {
    ...(init?.headers as Record<string, string>),
  }
  if (API_GATEWAY_KEY) {
    headers['X-Api-Key'] = API_GATEWAY_KEY
  }
  if (token) {
    headers['Authorization'] = `Bearer ${token}`
  }
  const res = await fetch(url, { cache: 'no-store', ...init, headers })
  // 401 토큰 만료 시 자동 로그아웃 — never-resolving promise로 .catch 우회
  if (res.status === 401 && typeof window !== 'undefined') {
    localStorage.removeItem(STORAGE_KEYS.SAMBA_USER)
    document.cookie = 'samba_user=; path=/; max-age=0'
    window.location.href = '/samba/login'
    return new Promise<Response>(() => {})
  }
  return res
}

export async function request<T>(url: string, init?: RequestInit, options?: { timeoutMs?: number }): Promise<T> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(init?.headers as Record<string, string>),
  }

  // API Gateway Key
  if (API_GATEWAY_KEY) {
    headers['X-Api-Key'] = API_GATEWAY_KEY
  }
  // JWT 인증 헤더 자동 추가
  const token = getAccessToken()
  if (token) {
    headers['Authorization'] = `Bearer ${token}`
  }
  // 트리거 PC 의 확장앱 deviceId 자동 첨부 — 백엔드 enrich/sourcing 엔드포인트가
  // 이 헤더로 owner 박아 해당 PC 확장앱에서만 SSG/롯데온 등 탭이 열리도록 라우팅한다.
  // 헤더가 이미 명시 설정되어 있으면 덮어쓰지 않는다.
  if (!headers['X-Device-Id']) {
    const _did = getDeviceId()
    if (_did) headers['X-Device-Id'] = _did
  }

  const timeoutMs = options?.timeoutMs
  const controller = timeoutMs ? new AbortController() : null
  const timer = controller && timeoutMs ? setTimeout(() => controller.abort(), timeoutMs) : null

  let res: Response
  try {
    res = await fetch(url, {
      cache: 'no-store',
      ...init,
      headers,
      ...(controller ? { signal: controller.signal } : {}),
    })
  } finally {
    if (timer) clearTimeout(timer)
  }
  if (!res.ok) {
    // 401이면 토큰 만료 — 로그인 페이지로 강제 리다이렉트
    // .catch(() => []) 에 삼켜지지 않도록 never-resolving promise 반환
    if (res.status === 401 && typeof window !== 'undefined') {
      localStorage.removeItem(STORAGE_KEYS.SAMBA_USER)
      document.cookie = 'samba_user=; path=/; max-age=0'
      window.location.href = '/samba/login'
      return new Promise<T>(() => {})
    }
    const data = await res.json().catch(() => null);
    const detail = data?.detail
    const msg = typeof detail === 'string' ? detail : Array.isArray(detail) ? detail.map((d: Record<string, unknown>) => d.msg || JSON.stringify(d)).join(', ') : `HTTP ${res.status}`
    throw new Error(msg);
  }
  const text = await res.text();
  if (!text) return {} as T;
  try {
    return JSON.parse(text) as T;
  } catch {
    throw new Error(`응답 JSON 파싱 실패: ${text.slice(0, 100)}`);
  }
}

// ── Orders ──

export interface OrderDashboardStats {
  thisMonth: { count: number; sales: number; fulfillmentSales: number; fulfillmentCount: number; fulfillment: number }
  lastMonth: { count: number; sales: number; fulfillmentSales: number; fulfillmentCount: number; fulfillment: number }
  salesChange: number
  weekly: { date: string; sales: number; count: number; fulfillmentSales: number; fulfillmentCount: number; newRegistered: number; marketDeleted: number; registeredCount: number; collectedCount: number }[]
  monthly: { month: number; sales: number; fulfillmentSales: number }[]
  marketRegisteredCount: number
}

export interface PaginatedOrderList {
  items: SambaOrder[]
  total_count: number
  total_sale: number
  pending_count: number
}

export interface SambaOrder {
  id: string;
  order_number: string;
  channel_id?: string;
  channel_name?: string;
  product_id?: string;
  product_name?: string;
  product_image?: string;
  product_option?: string;
  coupang_display_name?: string;
  source_url?: string;
  source_site?: string;
  sales_channel_alias?: string;
  collected_product_id?: string;
  customer_name?: string;
  orderer_name?: string;
  customer_phone?: string;
  customer_address?: string;
  customer_address_detail?: string;
  customer_postal_code?: string;
  quantity: number;
  sale_price: number;
  /**
   * 고객결제금액 (할인 적용 후 실제 결제). 미설정이면 sale_price 폴백.
   * 롯데ON: slAmt − fvrAmtSum
   */
  total_payment_amount?: number | null;
  cost: number;
  shipping_fee: number;
  fee_rate: number;
  revenue: number;
  profit: number;
  profit_rate?: string;
  status: string;
  payment_status: string;
  shipping_status: string;
  shipping_company?: string;
  tracking_number?: string;
  notes?: string;
  ext_order_number?: string;
  sourcing_order_number?: string;
  sourcing_account_id?: string;
  source?: string;
  shipment_id?: string;
  action_tag?: string;
  customer_note?: string;
  paid_at?: string;
  created_at: string;
  updated_at: string;
  has_sms_sent?: boolean;
  has_kakao_sent?: boolean;
}

export interface MessageLog {
  id: string;
  message_type: 'sms' | 'kakao';
  rendered_message: string;
  receiver: string;
  sent_at: string;
  success: boolean;
  result_message?: string;
}

export const orderApi = {
  list: (skip = 0, limit = 50, status?: string) => {
    const params = new URLSearchParams({ skip: String(skip), limit: String(limit) });
    if (status) params.set("status", status);
    return request<SambaOrder[]>(`${SAMBA_PREFIX}/orders?${params}`);
  },
  listByDateRange: (start: string, end: string) =>
    request<SambaOrder[]>(`${SAMBA_PREFIX}/orders/by-date-range?start=${start}&end=${end}`),
  listByDateRangePaged: (params: {
    start: string
    end: string
    skip?: number
    limit?: number
    market_filter?: string
    site_filter?: string
    account_filter?: string
    market_status?: string
    status_filter?: string
    input_filter?: string
    invoice_filter?: string
    registration_filter?: string
    search_text?: string
    search_category?: string
    sort_by?: string
  }) => {
    const q = new URLSearchParams({
      start: params.start,
      end: params.end,
      skip: String(params.skip ?? 0),
      limit: String(params.limit ?? 20),
      market_filter: params.market_filter ?? '',
      site_filter: params.site_filter ?? '',
      account_filter: params.account_filter ?? '',
      market_status: params.market_status ?? '',
      status_filter: params.status_filter ?? '',
      input_filter: params.input_filter ?? '',
      invoice_filter: params.invoice_filter ?? '',
      registration_filter: params.registration_filter ?? '',
      search_text: params.search_text ?? '',
      search_category: params.search_category ?? 'customer',
      sort_by: params.sort_by ?? 'date_desc',
    })
    return request<PaginatedOrderList>(`${SAMBA_PREFIX}/orders/by-date-range-paged?${q}`)
  },
  listByCollectedProduct: (collectedProductId: string) =>
    request<SambaOrder[]>(`${SAMBA_PREFIX}/orders/by-collected-product?collected_product_id=${encodeURIComponent(collectedProductId)}`),
  listByCollectedProductPaged: (params: {
    collectedProductId: string
    skip?: number
    limit?: number
    market_filter?: string
    site_filter?: string
    account_filter?: string
    market_status?: string
    status_filter?: string
    input_filter?: string
    invoice_filter?: string
    registration_filter?: string
    search_text?: string
    search_category?: string
    sort_by?: string
  }) => {
    const q = new URLSearchParams({
      collected_product_id: params.collectedProductId,
      skip: String(params.skip ?? 0),
      limit: String(params.limit ?? 20),
      market_filter: params.market_filter ?? '',
      site_filter: params.site_filter ?? '',
      account_filter: params.account_filter ?? '',
      market_status: params.market_status ?? '',
      status_filter: params.status_filter ?? '',
      input_filter: params.input_filter ?? '',
      invoice_filter: params.invoice_filter ?? '',
      registration_filter: params.registration_filter ?? '',
      search_text: params.search_text ?? '',
      search_category: params.search_category ?? 'customer',
      sort_by: params.sort_by ?? 'date_desc',
    })
    return request<PaginatedOrderList>(`${SAMBA_PREFIX}/orders/by-collected-product-paged?${q}`)
  },
  dashboardStats: () => request<OrderDashboardStats>(`${SAMBA_PREFIX}/orders/dashboard-stats`),
  get: (id: string) => request<SambaOrder>(`${SAMBA_PREFIX}/orders/${id}`),
  search: (q: string) => request<SambaOrder[]>(`${SAMBA_PREFIX}/orders/search?q=${encodeURIComponent(q)}`),
  create: (data: Partial<SambaOrder>) =>
    request<SambaOrder>(`${SAMBA_PREFIX}/orders`, { method: "POST", body: JSON.stringify(data) }),
  update: (id: string, data: Partial<SambaOrder>) =>
    request<SambaOrder>(`${SAMBA_PREFIX}/orders/${id}`, { method: "PUT", body: JSON.stringify(data) }),
  updateStatus: (id: string, status: string) =>
    request<SambaOrder>(`${SAMBA_PREFIX}/orders/${id}/status`, { method: "PUT", body: JSON.stringify({ status }) }),
  delete: (id: string) =>
    request<{ ok: boolean }>(`${SAMBA_PREFIX}/orders/${id}`, { method: "DELETE" }),
  syncFromMarkets: (days = 7, accountId?: string) =>
    request<{ total_synced: number; results: { account: string; status: string; message?: string; fetched?: number; synced?: number }[] }>(
      `${SAMBA_PREFIX}/orders/sync-from-markets`, { method: "POST", body: JSON.stringify({ days, account_id: accountId || undefined }) }),
  approveCancel: (id: string) =>
    request<{ ok: boolean; message: string }>(`${SAMBA_PREFIX}/orders/${id}/approve-cancel`, { method: "POST" }),
  sellerCancel: (id: string, reasonCode: string, reasonText?: string) =>
    request<{ ok: boolean; message: string; detail?: string }>(`${SAMBA_PREFIX}/orders/${id}/seller-cancel`, {
      method: "POST", body: JSON.stringify({ reason_code: reasonCode, reason_text: reasonText || "" }),
    }),
  confirmOrder: (id: string) =>
    request<{ ok: boolean; message: string }>(`${SAMBA_PREFIX}/orders/${id}/confirm`, { method: "POST" }),
  marketDelete: (id: string) =>
    request<{ ok: boolean; message: string; detail?: unknown }>(`${SAMBA_PREFIX}/orders/${id}/market-delete`, { method: "POST" }),
  exchangeAction: (id: string, action: string, reason?: string, extra?: { tracking_number?: string; shipping_company?: string; clm_no?: string }) =>
    request<{ ok: boolean; message: string }>(`${SAMBA_PREFIX}/orders/${id}/exchange-action`, {
      method: "POST", body: JSON.stringify({ action, reason, ...extra }),
    }),
  returnAction: (id: string, action: string, reason?: string) =>
    request<{ ok: boolean; message: string }>(`${SAMBA_PREFIX}/orders/${id}/return-action`, {
      method: "POST", body: JSON.stringify({ action, reason }),
    }),
  findByOrderNumber: (orderNumber: string) =>
    request<{ id: string; order_number: string } | null>(`${SAMBA_PREFIX}/orders/find-by-number?order_number=${encodeURIComponent(orderNumber)}`),
  shipOrder: (id: string, shippingCompany: string, trackingNumber: string) =>
    request<{ ok: boolean; market_sent: boolean; message: string }>(`${SAMBA_PREFIX}/orders/${id}/ship`, {
      method: "POST", body: JSON.stringify({ shipping_company: shippingCompany, tracking_number: trackingNumber }),
    }),
  linkProduct: (orderId: string, collectedProductId: string) =>
    request<{ ok: boolean }>(`${SAMBA_PREFIX}/orders/${orderId}/link-product`, {
      method: "PATCH", body: JSON.stringify({ collected_product_id: collectedProductId }),
    }),
  fetchProductImage: (url: string) =>
    request<{ image_url: string }>(`${SAMBA_PREFIX}/orders/fetch-product-image`, {
      method: "POST", body: JSON.stringify({ url }),
    }),
  cancelSourceOrder: (orderNumber: string, reason?: string) =>
    request<{ ok: boolean; message: string }>(`${SAMBA_PREFIX}/orders/cancel-source-order`, {
      method: "POST", body: JSON.stringify({ order_number: orderNumber, reason: reason || '단순변심' }),
    }),
  getCancelAlertCount: () =>
    request<{ count: number }>(`${SAMBA_PREFIX}/orders/cancel-alert-count`),
  syncTracking: (orderId: string, force = false) =>
    request<{ success: boolean; jobId?: string; requestId?: string; skipped?: boolean; reason?: string; error?: string }>(
      `${SAMBA_PREFIX}/orders/${orderId}/sync-tracking?force=${force}`,
      { method: 'POST' },
    ),
  syncTrackingBulk: (limit = 500, days = 7, force = false) =>
    request<{ success: boolean; queued: number; skipped: number; errors: string[]; job_ids: string[] }>(
      `${SAMBA_PREFIX}/orders/sync-tracking/bulk?limit=${limit}&days=${days}&force=${force}`,
      { method: 'POST' },
    ),
  retryFailedTrackingJobs: (days = 7) =>
    request<{ success: boolean; target: number; queued: number; skipped: number; errors: string[]; job_ids: string[] }>(
      `${SAMBA_PREFIX}/orders/tracking-sync/retry-failed?days=${days}`,
      { method: 'POST' },
    ),
  getAutoSyncInterval: () =>
    request<{ interval_minutes: number }>(`${SAMBA_PREFIX}/orders/auto-sync-interval`),
  setAutoSyncInterval: (interval_minutes: number) =>
    request<{ interval_minutes: number }>(
      `${SAMBA_PREFIX}/orders/auto-sync-interval`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ interval_minutes }),
      },
    ),
  getAutoSyncHistory: (limit = 2) =>
    request<{
      items: Array<{
        job_id: string
        status: string
        created_at: string | null
        started_at: string | null
        completed_at: string | null
        duration_sec: number | null
        total_synced: number
        per_market: Array<{
          account: string
          status: string
          synced: number
          fetched: number
          message: string
        }>
        tracking_sync: {
          success: boolean
          queued: number
          skipped: number
          jobs: number
          errors: string[]
          ran_at: string | null
        } | null
        error: string | null
      }>
    }>(`${SAMBA_PREFIX}/orders/auto-sync-history?limit=${limit}`),
  dispatchTrackingToMarket: (jobId: string, dryRun = false) =>
    request<{ success: boolean; dryRun?: boolean; channel?: string; courier?: string; tracking?: string; error?: string }>(
      `${SAMBA_PREFIX}/orders/tracking-sync/${jobId}/dispatch?dry_run=${dryRun}`,
      { method: 'POST' },
    ),
  dispatchTrackingBulk: (dryRun = false) =>
    request<{ success: boolean; total: number; sent: number; failed: number; errors: string[] }>(
      `${SAMBA_PREFIX}/orders/tracking-sync/dispatch/bulk?dry_run=${dryRun}`,
      { method: 'POST' },
    ),
  listRecentTrackingSyncJobs: (limit = 50) =>
    request<{
      counts: Record<string, number>
      recent: Array<{
        id: string
        orderId: string
        orderNumber: string
        customerName: string
        channelName: string
        site: string
        sourcingOrderNumber: string
        sourcingAccountLabel: string
        status: string
        courier?: string | null
        tracking?: string | null
        lastError?: string | null
        attempts: number
        updatedAt?: string | null
        actionTag?: string | null
      }>
    }>(`${SAMBA_PREFIX}/orders/tracking-sync/recent?limit=${limit}`),
  listTrackingSyncJobsByIds: (jobIds: string[]) =>
    request<{
      counts: Record<string, number>
      recent: Array<{
        id: string
        orderId: string
        orderNumber: string
        customerName: string
        channelName: string
        site: string
        sourcingOrderNumber: string
        sourcingAccountLabel: string
        status: string
        courier?: string | null
        tracking?: string | null
        lastError?: string | null
        attempts: number
        updatedAt?: string | null
        paidAt?: string | null
        actionTag?: string | null
      }>
    }>(`${SAMBA_PREFIX}/orders/tracking-sync/by-ids`, {
      method: 'POST',
      body: JSON.stringify({ job_ids: jobIds }),
    }),
  cancelTrackingSyncBatch: (jobIds: string[]) =>
    request<{ cancelled: number }>(`${SAMBA_PREFIX}/orders/tracking-sync/cancel-batch`, {
      method: 'POST',
      body: JSON.stringify({ job_ids: jobIds }),
    }),
  getAlarmSettings: () =>
    request<{ hour: number; min: number; sleep_start: string; sleep_end: string }>(`${SAMBA_PREFIX}/orders/alarm-settings`),
  saveAlarmSettings: (data: { hour: number; min: number; sleep_start: string; sleep_end: string }) =>
    request<{ ok: boolean }>(`${SAMBA_PREFIX}/orders/alarm-settings`, {
      method: 'POST', body: JSON.stringify(data),
    }),
  getTracking: (carrier: string, invoice: string) =>
    request<TrackingInfo>(`${SAMBA_PREFIX}/orders/tracking?carrier=${encodeURIComponent(carrier)}&invoice=${encodeURIComponent(invoice)}`),
};

export interface TrackingEvent {
  time: string | null
  status: string | null
  status_code: string | null
  location: string | null
  description: string | null
}

export interface TrackingInfo {
  carrier_name: string
  carrier_id: string
  invoice: string
  from_name: string | null
  to_name: string | null
  state: string | null
  events: TrackingEvent[]
}

// ── Channels ──

export interface SambaChannel {
  id: string;
  name: string;
  type: string;
  platform: string;
  fee_rate: number;
  products?: string[];
  status: string;
  created_at: string;
  updated_at: string;
}

export const channelApi = {
  list: (skip = 0, limit = 50) =>
    request<SambaChannel[]>(`${SAMBA_PREFIX}/channels?skip=${skip}&limit=${limit}`),
  get: (id: string) => request<SambaChannel>(`${SAMBA_PREFIX}/channels/${id}`),
  create: (data: Partial<SambaChannel>) =>
    request<SambaChannel>(`${SAMBA_PREFIX}/channels`, { method: "POST", body: JSON.stringify(data) }),
  update: (id: string, data: Partial<SambaChannel>) =>
    request<SambaChannel>(`${SAMBA_PREFIX}/channels/${id}`, { method: "PUT", body: JSON.stringify(data) }),
  delete: (id: string) =>
    request<{ ok: boolean }>(`${SAMBA_PREFIX}/channels/${id}`, { method: "DELETE" }),
};

// ── Policies ──

export interface SambaPolicy {
  id: string;
  name: string;
  site_name?: string;
  pricing?: Record<string, unknown>;
  market_policies?: Record<string, unknown>;
  extras?: {
    detail_template_id?: string;
    market_detail_templates?: Record<string, string>;
    name_rule_id?: string;
    forbidden_text?: string;
    deletion_text?: string;
    forbidden_template_id?: string;
    deletion_template_id?: string;
    color?: string;
  };
  created_at: string;
  updated_at: string;
}

export interface PricePreview {
  cost: number;
  market_price: number;
  profit: number;
  profit_rate: number;
}

export const policyApi = {
  list: (skip = 0, limit = 50) =>
    request<SambaPolicy[]>(`${SAMBA_PREFIX}/policies?skip=${skip}&limit=${limit}`),
  get: (id: string) => request<SambaPolicy>(`${SAMBA_PREFIX}/policies/${id}`),
  create: (data: Partial<SambaPolicy>) =>
    request<SambaPolicy>(`${SAMBA_PREFIX}/policies`, { method: "POST", body: JSON.stringify(data) }),
  update: (id: string, data: Partial<SambaPolicy>) =>
    request<SambaPolicy>(`${SAMBA_PREFIX}/policies/${id}`, { method: "PUT", body: JSON.stringify(data) }),
  delete: (id: string) =>
    request<{ ok: boolean }>(`${SAMBA_PREFIX}/policies/${id}`, { method: "DELETE" }),
  calculatePrice: (id: string, cost: number, feeRate = 0) =>
    request<PricePreview>(`${SAMBA_PREFIX}/policies/${id}/calculate-price`, {
      method: "POST",
      body: JSON.stringify({ cost, fee_rate: feeRate }),
    }),
  aiChange: (command: string) =>
    request<{ ok: boolean; applied: number; changes: { policy_id: string; policy_name: string; field: string; market: string; before: number; after: number }[] }>(
      `${SAMBA_PREFIX}/policies/ai-change`,
      { method: "POST", body: JSON.stringify({ command }) }),
};

// ── Collector (수집 필터 + 수집 상품) ──

export interface SambaSearchFilter {
  id: string;
  source_site: string;
  name: string;
  keyword?: string;
  category_filter?: string;
  min_price?: number;
  max_price?: number;
  exclude_sold_out: boolean;
  is_active: boolean;
  requested_count?: number;
  applied_policy_id?: string;
  last_collected_at?: string;
  ss_brand_id?: number;
  ss_brand_name?: string;
  ss_manufacturer_id?: number;
  ss_manufacturer_name?: string;
  target_mappings?: Record<string, string>;
  created_at: string;
  // 트리 구조
  parent_id?: string | null;
  is_folder?: boolean;
  collected_count?: number;
  children?: SambaSearchFilter[];
}

export interface SambaCollectedProduct {
  id: string;
  source_site: string;
  search_filter_id?: string;
  site_product_id?: string;
  name: string;
  name_en?: string;
  name_ja?: string;
  brand?: string;
  original_price: number;
  sale_price: number;
  cost?: number;
  images?: string[];
  coupang_main_image?: string;
  options?: unknown[];
  // 추가구성상품 (메인 옵션과 별개 차원 — 스마트스토어 productAddItems 등으로 매핑)
  addon_options?: Array<{
    no?: number
    group?: string
    name: string
    add_price?: number
    stock?: number
    is_required?: boolean
    is_none_choice?: boolean
  }>;
  // 메인 옵션 그룹명 (예: ["색상","사이즈"])
  option_group_names?: string[];
  category?: string;
  category1?: string;
  category2?: string;
  category3?: string;
  category4?: string;
  detail_html?: string;
  detail_images?: string[];
  manufacturer?: string;
  origin?: string;
  material?: string;
  color?: string;
  status: string;
  applied_policy_id?: string;
  market_prices?: Record<string, number>;
  market_enabled?: Record<string, boolean>;
  registered_accounts?: string[];
  market_product_nos?: Record<string, string>;
  market_names?: Record<string, string>;
  is_sold_out: boolean;
  sale_status?: string;
  kream_data?: Record<string, unknown>;
  style_code?: string;
  sex?: string;
  season?: string;
  care_instructions?: string;
  quality_guarantee?: string;
  price_before_change?: number;
  price_changed_at?: string;
  price_history?: Array<{
    date: string;
    sale_price: number;
    original_price: number;
    cost?: number;
    kream_fast_min?: number;
    kream_general_min?: number;
    options?: unknown[];
  }>;
  lock_delete?: boolean;
  lock_stock?: boolean;
  tags?: string[];
  monitor_priority?: string;
  last_sent_data?: Record<string, { sale_price?: number; cost?: number; options?: { name: string; price: number; stock: number }[]; sent_at?: string }>;
  last_refreshed_at?: string;
  refresh_error_count?: number;
  group_key?: string | null;
  group_product_no?: number | null;
  video_url?: string;
  source_url?: string;
  extra_data?: Record<string, unknown>;
  seo_keywords?: string[];
  free_shipping?: boolean;
  same_day_delivery?: boolean;
  sourcing_shipping_fee?: number;
  is_point_restricted?: boolean | null;
  created_at: string;
  updated_at?: string;
}

export interface RefreshDetail {
  time: string
  brand: string
  name: string
  status: 'changed' | 'unchanged' | 'error' | 'stock_changed'
  detail: string
  retransmitted?: boolean
}

export interface RefreshResult {
  total: number
  refreshed: number
  changed: number
  sold_out: number
  deleted: number
  retransmitted: number
  needs_extension: string[]
  errors: number
  details?: RefreshDetail[]
}

export interface BrandScanResult {
  categories: { categoryCode: string; path: string; count: number; category1: string; category2: string; category3: string }[]
  total: number
  groupCount: number
}

export interface BrandScanProgress {
  job_id: string
  status: 'running' | 'done' | 'error'
  result?: BrandScanResult | null
  error?: string | null
  meta?: Record<string, unknown>
}

export const collectorApi = {
  // Filters
  listFilters: () => request<SambaSearchFilter[]>(`${SAMBA_PREFIX}/collector/filters`),
  getFilterTree: () => request<SambaSearchFilter[]>(`${SAMBA_PREFIX}/collector/filters/tree`),
  getFilterTreeCounts: (sourceSite?: string) => {
    const qs = sourceSite ? `?source_site=${encodeURIComponent(sourceSite)}` : ''
    return request<Record<string, { collected_count: number; market_registered_count: number; ai_tagged_count: number; ai_image_count: number; tag_applied_count: number; policy_applied_count: number }>>(`${SAMBA_PREFIX}/collector/filters/tree/counts${qs}`)
  },
  createFilter: (data: Partial<SambaSearchFilter>) =>
    request<SambaSearchFilter>(`${SAMBA_PREFIX}/collector/filters`, { method: "POST", body: JSON.stringify(data) }),
  createFolder: (sourceSite: string, name: string, parentId?: string) =>
    request<SambaSearchFilter>(`${SAMBA_PREFIX}/collector/filters/folder`, {
      method: 'POST', body: JSON.stringify({ source_site: sourceSite, name, parent_id: parentId }),
    }),
  moveFilter: (id: string, parentId: string | null) =>
    request<SambaSearchFilter>(`${SAMBA_PREFIX}/collector/filters/${id}/move`, {
      method: 'PATCH', body: JSON.stringify({ parent_id: parentId }),
    }),
  updateFilter: (id: string, data: Partial<SambaSearchFilter>) =>
    request<SambaSearchFilter>(`${SAMBA_PREFIX}/collector/filters/${id}`, { method: "PUT", body: JSON.stringify(data) }),
  deleteFilter: (id: string) =>
    request<{ ok: boolean }>(`${SAMBA_PREFIX}/collector/filters/${id}`, { method: "DELETE" }),
  deleteBrandScope: (sourceSite: string, brandName: string) =>
    request<{ ok: boolean; deleted_products: number; deleted_filters: number }>(
      `${SAMBA_PREFIX}/collector/brands/delete`,
      {
        method: 'POST',
        body: JSON.stringify({ source_site: sourceSite, brand_name: brandName }),
      },
    ),
  bulkApplyPolicy: (filterIds: string[], policyId: string) =>
    request<{ applied: number }>(`${SAMBA_PREFIX}/collector/filters/bulk-apply-policy`, {
      method: 'POST', body: JSON.stringify({ filter_ids: filterIds, policy_id: policyId }),
    }),

  // Brand Sourcing
  brandDiscover: (keyword: string, source_site?: string) =>
    request<{ brands: { name: string; count: number; id?: string }[]; total: number }>(
      `${SAMBA_PREFIX}/collector/brand-discover`, { method: "POST", body: JSON.stringify({ keyword, source_site: source_site || 'LOTTEON' }) }),
  gsshopScanProgress: () =>
    request<{ stage: string; keyword?: string; page?: number; products?: number; detail_ok?: number; detail_fail?: number; detail_total?: number }>(
      `${SAMBA_PREFIX}/collector/gsshop-scan-progress`),
  // SSG 는 비동기 job 응답 ({job_id, status}) — Cloudflare 100s origin timeout 우회.
  // 그 외 사이트는 기존 동기 응답 ({categories, total, groupCount}).
  // brandScan 호출자는 항상 BrandScanResult 를 받음 — job_id 응답이면 polling 으로 변환.
  brandScan: async (brand: string, gf?: string, keyword?: string, source_site?: string, selected_brands?: string[], brand_ids?: string[], brand_total?: number, options?: Record<string, boolean>): Promise<BrandScanResult> => {
    const res = await request<BrandScanResult | { job_id: string; status: 'running' }>(
      `${SAMBA_PREFIX}/collector/brand-scan`,
      { method: "POST", body: JSON.stringify({ brand, gf: gf || 'A', keyword: keyword || '', source_site: source_site || 'MUSINSA', selected_brands: selected_brands || [], brand_ids: brand_ids || [], brand_total: brand_total || 0, options: options || {} }) },
      { timeoutMs: 600_000 },
    )
    if (!('job_id' in res)) return res
    // 2s 간격 polling. 최대 600s 대기.
    const jobId = res.job_id
    const maxAttempts = 300
    for (let attempts = 0; attempts < maxAttempts; attempts += 1) {
      await new Promise<void>(r => setTimeout(r, 2000))
      const p = await request<BrandScanProgress>(
        `${SAMBA_PREFIX}/collector/brand-scan-progress/${encodeURIComponent(jobId)}`,
      )
      if (p.status === 'done' && p.result) return p.result
      if (p.status === 'error') throw new Error(p.error || '스캔 실패')
    }
    throw new Error('스캔 시간 초과 (600초)')
  },
  brandCreateGroups: (data: { brand: string; brand_name?: string; gf?: string; categories: { categoryCode: string; path: string; count: number }[]; requested_count_per_group?: number; real_total?: number; applied_policy_id?: string; options?: Record<string, boolean>; source_site?: string; selected_brands?: string[]; brand_ids?: string[] }) =>
    request<{ created: number; groups: { id: string; name: string; count: number; path: string }[] }>(
      `${SAMBA_PREFIX}/collector/brand-create-groups`,
      { method: "POST", body: JSON.stringify(data) },
      { timeoutMs: 600_000 },
    ),
  brandRefresh: (data: { brand: string; brand_name?: string; gf?: string; options?: Record<string, boolean>; source_site?: string; categories?: string[] }) =>
    request<{ scanned: number; new_groups: number; updated_groups: number; filter_ids: string[]; message: string }>(
      `${SAMBA_PREFIX}/collector/brand-refresh`,
      { method: "POST", body: JSON.stringify(data) },
      { timeoutMs: 600_000 },
    ),

  brandPolicyApply: (sourceSite: string, brandName: string, policyId: string | null) =>
    request<{ products_updated: number; filters_updated: number; assignments_updated: number }>(
      `${SAMBA_PREFIX}/collector/brand-policy-apply`,
      {
        method: 'POST',
        body: JSON.stringify({ source_site: sourceSite, brand_name: brandName, policy_id: policyId }),
      },
    ),

  // 상태 확인
  proxyStatus: () =>
    request<{ status: string; message: string }>(`${SAMBA_PREFIX}/collector/proxy-status`),
  musinsaAuthStatus: () =>
    request<{ status: string; message: string }>(`${SAMBA_PREFIX}/collector/musinsa-auth-status`),

  // Collected Products
  listProducts: (skip = 0, limit = 50, status?: string, source_site?: string, category?: string) => {
    const p = new URLSearchParams({ skip: String(skip), limit: String(limit) });
    if (status) p.set("status", status);
    if (source_site) p.set("source_site", source_site);
    if (category) p.set("category", category);
    return request<SambaCollectedProduct[]>(`${SAMBA_PREFIX}/collector/products?${p}`);
  },
  getProductsByIds: (ids: string[]) =>
    request<SambaCollectedProduct[]>(`${SAMBA_PREFIX}/collector/products/by-ids`, {
      method: 'POST',
      body: JSON.stringify({ ids }),
    }),
  lookupByMarketNo: (marketProductNo: string) =>
    request<{ found: boolean; id?: string; source_site?: string; site_product_id?: string; name?: string; original_link?: string; product_image?: string; market_product_nos?: Record<string, string | number | { originProductNo?: string | number }> }>(
      `${SAMBA_PREFIX}/collector/products/lookup-by-market-no/${marketProductNo}`),
  scrollProducts: (params: {
    skip?: number; limit?: number; search?: string; search_type?: string;
    source_site?: string; status?: string; sold_out_filter?: string; ai_filter?: string;
    search_filter_id?: string; sort_by?: string;
  }) => {
    const p = new URLSearchParams()
    p.set('skip', String(params.skip ?? 0))
    p.set('limit', String(params.limit ?? 50))
    if (params.search) p.set('search', params.search)
    if (params.search_type) p.set('search_type', params.search_type)
    if (params.source_site) p.set('source_site', params.source_site)
    if (params.status) p.set('status', params.status)
    if (params.sold_out_filter) p.set('sold_out_filter', params.sold_out_filter)
    if (params.ai_filter) p.set('ai_filter', params.ai_filter)
    if (params.search_filter_id) p.set('search_filter_id', params.search_filter_id)
    if (params.sort_by) p.set('sort_by', params.sort_by)
    return request<{
      items: SambaCollectedProduct[];
      total: number;
      sites: string[];
      counts: { total: number; registered: number; policy_applied: number; sold_out: number };
    }>(
      `${SAMBA_PREFIX}/collector/products/scroll?${p}`
    )
  },
  getProductIds: (params: {
    search?: string; search_type?: string;
    source_site?: string; source_sites?: string; status?: string;
    sold_out_filter?: string; ai_filter?: string; search_filter_id?: string;
  }) => {
    const p = new URLSearchParams()
    p.set('ids_only', 'true')
    if (params.search) p.set('search', params.search)
    if (params.search_type) p.set('search_type', params.search_type)
    if (params.source_sites) p.set('source_sites', params.source_sites)
    else if (params.source_site) p.set('source_site', params.source_site)
    if (params.status) p.set('status', params.status)
    if (params.sold_out_filter) p.set('sold_out_filter', params.sold_out_filter)
    if (params.ai_filter) p.set('ai_filter', params.ai_filter)
    if (params.search_filter_id) p.set('search_filter_id', params.search_filter_id)
    return request<{ ids: string[]; total: number }>(
      `${SAMBA_PREFIX}/collector/products/scroll?${p}`
    )
  },
  // 초기 메타데이터 통합 API (8개 API → 1개)
  initData: () =>
    request<{
      policies: SambaPolicy[];
      filters: SambaSearchFilter[];
      deletion_words: string[];
      accounts: SambaMarketAccount[];
      order_product_ids: string[];
      name_rules: SambaNameRule[];
      category_mappings: { source_site: string; source_category: string; target_mappings: Record<string, string> }[];
      detail_templates: SambaDetailTemplate[];
    }>(`${SAMBA_PREFIX}/collector/products/init-data`),
  getProductIdsWithOrders: () =>
    request<string[]>(`${SAMBA_PREFIX}/collector/products/with-orders`),
  productCounts: () =>
    request<{ total: number; registered: number; policy_applied: number; sold_out: number }>(`${SAMBA_PREFIX}/collector/products/counts`),
  dashboardStats: () =>
    request<{
      by_source: { source_site: string; total: number; registered: number; sold_out: number; brands: { brand: string; total: number; registered: number; sold_out: number }[] }[]
      by_account: { account_id: string; market_name: string; account_label: string; registered: number; brands: { source_site: string; brand: string; registered: number }[] }[]
    }>(`${SAMBA_PREFIX}/collector/products/dashboard-stats`),
  categoryTree: () =>
    request<{ source_site: string; category: string; count: number }[]>(`${SAMBA_PREFIX}/collector/products/category-tree`),
  searchProducts: (q: string) =>
    request<SambaCollectedProduct[]>(`${SAMBA_PREFIX}/collector/products/search?q=${encodeURIComponent(q)}`),
  getProduct: (id: string) => request<SambaCollectedProduct>(`${SAMBA_PREFIX}/collector/products/${id}`),
  getPriceHistory: (id: string) => request<Array<Record<string, unknown>>>(`${SAMBA_PREFIX}/collector/products/${id}/price-history`),
  createProduct: (data: Partial<SambaCollectedProduct>) =>
    request<SambaCollectedProduct>(`${SAMBA_PREFIX}/collector/products`, { method: "POST", body: JSON.stringify(data) }),
  bulkCreate: (items: Partial<SambaCollectedProduct>[]) =>
    request<{ created: number }>(`${SAMBA_PREFIX}/collector/products/bulk`, { method: "POST", body: JSON.stringify({ items }) }),
  updateProduct: (id: string, data: Partial<SambaCollectedProduct>) =>
    request<SambaCollectedProduct>(`${SAMBA_PREFIX}/collector/products/${id}`, { method: "PUT", body: JSON.stringify(data) }),
  deleteProduct: (id: string) =>
    request<{ ok: boolean }>(`${SAMBA_PREFIX}/collector/products/${id}`, { method: "DELETE" }),
  bulkDeleteProducts: (ids: string[]) =>
    request<{ deleted: number }>(`${SAMBA_PREFIX}/collector/products/bulk-delete`, { method: "POST", body: JSON.stringify({ ids }) }),
  blockAndDelete: (productIds: string[]) =>
    request<{ ok: boolean; blocked: number; deleted: number }>(
      `${SAMBA_PREFIX}/collector/products/block-and-delete`, { method: "POST", body: JSON.stringify({ product_ids: productIds }) }),
  resetRegistration: (id: string) =>
    request<{ ok: boolean }>(`${SAMBA_PREFIX}/collector/products/${id}/reset-registration`, { method: "POST" }),
  bulkResetRegistration: (ids: string[], accountIds?: string[]) =>
    request<{ reset: number }>(`${SAMBA_PREFIX}/collector/products/bulk-reset-registration`, {
      method: "POST",
      body: JSON.stringify(accountIds && accountIds.length ? { ids, account_ids: accountIds } : { ids }),
    }),
  bulkRemoveImage: (imageUrl: string, fields: string[] = ['images']) =>
    request<{ removed: number }>(`${SAMBA_PREFIX}/collector/products/images/bulk-remove`, { method: "POST", body: JSON.stringify({ image_url: imageUrl, fields }) }),
  bulkUpdateTags: (ids: string[], tags: string[] | null, seoKeywords: string[] | null) =>
    request<{ updated: number }>(`${SAMBA_PREFIX}/collector/products/bulk-update-tags`, {
      method: "POST",
      body: JSON.stringify({ ids, tags, seo_keywords: seoKeywords }),
    }),
  bulkAddAccount: () =>
    request<{ pa_products: number; matched: number; updated: number; already: number }>(`${SAMBA_PREFIX}/collector/products/bulk-add-account`, { method: "POST" }),

  getDuplicates: (sourceSite?: string, filterIds?: string[]) => {
    const p = new URLSearchParams()
    if (sourceSite) p.set('source_site', sourceSite)
    if (filterIds && filterIds.length > 0) p.set('filter_ids', filterIds.join(','))
    const qs = p.toString() ? `?${p}` : ''
    return request<{
      groups: Array<{
        name: string
        total: number
        registered: Array<{ id: string; name: string; source_site: string; brand: string | null; sale_price: number; images: string[]; registered_accounts: unknown; status: string }>
        duplicates: Array<{ id: string; name: string; source_site: string; brand: string | null; sale_price: number; images: string[]; registered_accounts: unknown; status: string }>
      }>
      total: number
    }>(`${SAMBA_PREFIX}/collector/products/duplicates${qs}`)
  },

  // 재고/가격 갱신
  refresh: (productIds?: string[], autoRetransmit = true, searchFilterIds?: string[]) =>
    request<RefreshResult>(`${SAMBA_PREFIX}/collector/products/refresh`, {
      method: 'POST',
      body: JSON.stringify({ product_ids: productIds, search_filter_ids: searchFilterIds, auto_retransmit: autoRetransmit }),
    }),

  // Ken Burns 영상 생성 (R2/로컬 저장 후 URL 반환)
  generateVideo: (productId: string, maxImages = 3, durationPerImage = 1.0) =>
    request<{ success: boolean, video_url: string }>(`${SAMBA_PREFIX}/collector/products/generate-video`, {
      method: 'POST',
      body: JSON.stringify({ product_id: productId, max_images: maxImages, duration_per_image: durationPerImage }),
    }),

  // Probe (소싱처/마켓 헬스체크)
  probeStatus: () =>
    request<Record<string, unknown>>(`${SAMBA_PREFIX}/collector/probe/status`),
  probeRun: () =>
    request<Record<string, unknown>>(`${SAMBA_PREFIX}/collector/probe/run`, { method: 'POST' }),
  autotuneStart: (target: string = 'all', targetProductNo?: string, deviceId?: string) =>
    request<{ ok: boolean; status: string; error?: string }>(`${SAMBA_PREFIX}/collector/autotune/start`, { method: 'POST', body: JSON.stringify({ target, target_product_no: targetProductNo || undefined, device_id: deviceId || undefined }) }),
  autotuneRefreshOne: (productNo: string) =>
    request<{ ok: boolean; error?: string; product_id?: string; brand?: string; name?: string; time?: string; status?: string; detail?: string }>(`${SAMBA_PREFIX}/collector/autotune/refresh-one`, { method: 'POST', body: JSON.stringify({ product_no: productNo }) }),
  autotuneStop: (deviceId?: string) =>
    request<{ ok: boolean; status: string }>(`${SAMBA_PREFIX}/collector/autotune/stop`, { method: 'POST', body: JSON.stringify({ device_id: deviceId || undefined }) }),
  autotuneStatus: (deviceId?: string) =>
    request<{ running: boolean; last_tick: string | null; cycle_count: number; restart_count: number; target: string; refreshed_count: number; breaker_tripped: Record<string, number>; site_intervals?: Record<string, number>; site_autotune_concurrency?: Record<string, number>; running_pcs?: string[]; traffic?: { collecting: boolean; transmitting: boolean; busy: boolean } }>(`${SAMBA_PREFIX}/collector/autotune/status${deviceId ? `?device_id=${encodeURIComponent(deviceId)}` : ''}`),
  autotuneUpdateInterval: (site: string, interval: number) =>
    request<{ ok: boolean; site: string; interval: number }>(`${SAMBA_PREFIX}/collector/autotune/interval`, { method: 'POST', body: JSON.stringify({ site, interval }) }),
  autotuneGetConcurrency: () =>
    request<{ ok: boolean; concurrency: Record<string, number> }>(`${SAMBA_PREFIX}/collector/autotune/concurrency`),
  autotuneUpdateConcurrency: (site: string, value: number) =>
    request<{ ok: boolean; site: string; value: number }>(`${SAMBA_PREFIX}/collector/autotune/concurrency`, { method: 'POST', body: JSON.stringify({ site, value }) }),
  autotuneGetFilters: () =>
    request<{
      enabled_sources: string[] | null
      enabled_markets: string[] | null
      available_sources: string[]
      available_markets: string[]
    }>(`${SAMBA_PREFIX}/collector/autotune/filters`),
  autotuneSetFilters: (enabledSources: string[] | null, enabledMarkets: string[] | null) =>
    request<{ ok: boolean }>(`${SAMBA_PREFIX}/collector/autotune/filters`, { method: 'PUT', body: JSON.stringify({ enabled_sources: enabledSources, enabled_markets: enabledMarkets }) }),
  autotuneGetPriority: () =>
    request<{ ok: boolean; priority_enabled: boolean }>(`${SAMBA_PREFIX}/collector/autotune/priority`),
  autotuneSetPriority: (enabled: boolean) =>
    request<{ ok: boolean; priority_enabled: boolean }>(`${SAMBA_PREFIX}/collector/autotune/priority`, { method: 'POST', body: JSON.stringify({ enabled }) }),
  collectSingleMusinsa: (url: string) =>
    request<{ saved: number; updated: boolean; product_no: string; brand: string; filter_name: string }>(
      `${SAMBA_PREFIX}/collector/collect-single-musinsa`,
      { method: 'POST', body: JSON.stringify({ url }) }
    ),
}

// ── Market Accounts ──

export interface SambaMarketAccount {
  id: string;
  market_type: string;
  market_name: string;
  account_label: string;
  seller_id?: string;
  business_name?: string;
  is_active: boolean;
  additional_fields?: Record<string, unknown>;
  created_at: string;
}

export const accountApi = {
  list: () => request<SambaMarketAccount[]>(`${SAMBA_PREFIX}/accounts`),
  listActive: () => request<SambaMarketAccount[]>(`${SAMBA_PREFIX}/accounts/active`),
  getMarkets: () => request<unknown[]>(`${SAMBA_PREFIX}/accounts/markets`),
  get: (id: string) => request<SambaMarketAccount>(`${SAMBA_PREFIX}/accounts/${id}`),
  create: (data: Partial<SambaMarketAccount>) =>
    request<SambaMarketAccount>(`${SAMBA_PREFIX}/accounts`, { method: "POST", body: JSON.stringify(data) }),
  update: (id: string, data: Partial<SambaMarketAccount>) =>
    request<SambaMarketAccount>(`${SAMBA_PREFIX}/accounts/${id}`, { method: "PUT", body: JSON.stringify(data) }),
  toggle: (id: string) =>
    request<SambaMarketAccount>(`${SAMBA_PREFIX}/accounts/${id}/toggle`, { method: "PUT" }),
  delete: (id: string) =>
    request<{ ok: boolean }>(`${SAMBA_PREFIX}/accounts/${id}`, { method: "DELETE" }),
  getSecrets: (id: string) =>
    request<Record<string, string>>(`${SAMBA_PREFIX}/accounts/${id}/secrets`),
};

// ── Shipments ──

export interface SambaShipment {
  id: string;
  product_id: string;
  account_id?: string;
  target_account_ids?: string[];
  update_items?: string[];
  status: string;
  transmit_result?: Record<string, string>;
  completed_at?: string;
  created_at: string;
}

// ── 스스그룹 타입 ──

export interface GroupPreviewProduct {
  id: string
  name: string
  color: string | null
  sale_price: number | null
  thumbnail: string | null
  existing_product_no: string | null
  free_shipping?: boolean
  same_day_delivery?: boolean
}

export interface GroupPreviewGroup {
  group_key: string
  group_name: string
  products: GroupPreviewProduct[]
}

export interface GroupPreviewResponse {
  groups: GroupPreviewGroup[]
  singles: GroupPreviewProduct[]
  delete_count: number
  group_count: number
  single_count: number
}

export interface GroupSendResponse {
  group_results: { group_key: string; status: string; error?: string; group_product_no?: number }[]
  single_results: Record<string, unknown>
}

export const shipmentApi = {
  list: (skip = 0, limit = 50, status?: string) => {
    const p = new URLSearchParams({ skip: String(skip), limit: String(limit) });
    if (status) p.set("status", status);
    return request<SambaShipment[]>(`${SAMBA_PREFIX}/shipments?${p}`);
  },
  listByProduct: (productId: string) =>
    request<SambaShipment[]>(`${SAMBA_PREFIX}/shipments/product/${productId}`),
  get: (id: string) => request<SambaShipment>(`${SAMBA_PREFIX}/shipments/${id}`),
  start: (productIds: string[], updateItems: string[], targetAccountIds: string[], skipUnchanged = false) =>
    request<{
      processed: number
      skipped: number
      results: {
        product_id: string
        status: string
        transmit_result?: Record<string, string>
        transmit_error?: Record<string, string>
        error?: string
      }[]
    }>(`${SAMBA_PREFIX}/shipments/start`, {
      method: "POST",
      body: JSON.stringify({ product_ids: productIds, update_items: updateItems, target_account_ids: targetAccountIds, skip_unchanged: skipUnchanged }),
    }),
  retry: (id: string) =>
    request<SambaShipment>(`${SAMBA_PREFIX}/shipments/${id}/retry`, { method: "POST" }),
  marketDelete: (productIds: string[], targetAccountIds: string[], currentIdx?: number, totalCount?: number, logToBuffer = false, signal?: AbortSignal) =>
    request<{ processed: number; results: { product_id: string; delete_results: Record<string, string>; success_count: number }[] }>(
      `${SAMBA_PREFIX}/shipments/market-delete`, {
        method: "POST",
        signal,
        body: JSON.stringify({
          product_ids: productIds,
          target_account_ids: targetAccountIds,
          current_idx: currentIdx,
          total_count: totalCount,
          log_to_buffer: logToBuffer,
        }),
      }
    ),
  marketDeleteByAccount: (accountId: string, dryRun = false) =>
    request<{ dry_run?: boolean; account_id: string; account_label: string; market_type: string; total_products: number; estimated_seconds?: number; processed?: number; results?: { product_id: string; delete_results: Record<string, string>; success_count: number }[] }>(
      `${SAMBA_PREFIX}/shipments/market-delete-by-account`, {
        method: "POST",
        body: JSON.stringify({ account_id: accountId, dry_run: dryRun }),
      }
    ),

  // 스스그룹 미리보기
  groupPreview: (searchFilterIds: string[], accountId: string) =>
    request<GroupPreviewResponse>(`${SAMBA_PREFIX}/shipments/group-preview`, {
      method: 'POST',
      body: JSON.stringify({ search_filter_ids: searchFilterIds, account_id: accountId }),
    }),

  // 스스그룹 전송
  groupSend: (groups: { group_key: string; product_ids: string[] }[], singles: string[], accountId: string) =>
    request<GroupSendResponse>(`${SAMBA_PREFIX}/shipments/group-send`, {
      method: 'POST',
      body: JSON.stringify({ groups, singles, account_id: accountId }),
    }),

  // 스마트스토어 고아 상품 정리 (DB에 없는 Naver 등록 상품 탐지/삭제)
  cleanupSmartstoreOrphans: (dryRun = true, maxDelete = 50, accountId?: string, productIds?: string[]) => {
    const params = new URLSearchParams()
    params.set('dry_run', String(dryRun))
    params.set('max_delete', String(maxDelete))
    if (accountId) params.set('account_id', accountId)
    return request<{
      ok: boolean
      dry_run: boolean
      db_no_count: number
      style_code_count: number
      total_naver: number
      total_orphans: number
      total_stale_db: number
      total_stale_cleared?: number
      total_deleted: number
      max_delete: number
      accounts: {
        account_id: string
        naver_count?: number
        orphan_count?: number
        orphans?: { origin_no: string; name: string; mgmt_code?: string }[]
        stale_db_count?: number
        stale_db?: { db_id: string; site_product_id?: string; style_code: string; mapped_origin_no: string; product_name: string }[]
        stale_cleared?: string[]
        deleted?: string[]
        failed?: { origin_no: string; error: string }[]
        failed_pages?: number[]
        total_pages?: number
        error?: string
      }[]
    }>(`${SAMBA_PREFIX}/shipments/smartstore/cleanup-orphans?${params.toString()}`, {
      method: 'POST',
      body: JSON.stringify({ product_ids: productIds && productIds.length > 0 ? productIds : null }),
    })
  },

  // 유령 감지 요약 (최근 N시간) — 상품관리 페이지 배너용
  ghostSummary: (hours = 48) => {
    const params = new URLSearchParams()
    params.set('hours', String(hours))
    return request<{
      ok: boolean
      hours: number
      total_count: number
      markets: {
        market: string
        event_type: string
        severity: string
        summary: string
        count: number
        created_at: string | null
      }[]
    }>(`${SAMBA_PREFIX}/shipments/ghost-summary?${params.toString()}`)
  },

  // 11번가 prdNo 누락 매핑 정리 (registered만 있고 prdNo 없는 케이스)
  cleanupElevenstMissingPrdno: (dryRun = true, maxCheck = 500, accountId?: string, productIds?: string[]) => {
    const params = new URLSearchParams()
    params.set('dry_run', String(dryRun))
    params.set('max_check', String(maxCheck))
    if (accountId) params.set('account_id', accountId)
    return request<{
      ok: boolean
      dry_run: boolean
      max_check: number
      total_checked: number
      total_alive: number
      total_dead: number
      total_missing: number
      total_recovered: number
      total_db_cleared: number
      total_failed: number
      accounts: {
        account_id: string
        label?: string
        error?: string
        checked?: number
        alive_count?: number
        alive?: { product_id: string; name: string; seller_code: string; prd_no: string; sel_stat_cd: string; sel_stat_nm: string }[]
        dead_count?: number
        dead?: { product_id: string; name: string; seller_code: string; prd_no: string; sel_stat_cd: string; sel_stat_nm: string }[]
        missing_count?: number
        missing?: { product_id: string; name: string; seller_code: string; prd_no: string; sel_stat_cd: string; sel_stat_nm: string }[]
        recovered_count?: number
        db_cleared_count?: number
        failed_count?: number
        failed?: { product_id: string; error: string }[]
      }[]
    }>(`${SAMBA_PREFIX}/shipments/elevenst/cleanup-missing-prdno?${params.toString()}`, {
      method: 'POST',
      body: JSON.stringify({ product_ids: productIds && productIds.length > 0 ? productIds : null }),
    })
  },

  // 쿠팡 유령삭제 양방향 (list_seller_products 기반 — orphan 삭제 + stale DB 정리)
  cleanupCoupangOrphans: (dryRun = true, maxDelete = 50, accountId?: string, productIds?: string[], full = false) => {
    const params = new URLSearchParams()
    params.set('dry_run', String(dryRun))
    params.set('max_delete', String(maxDelete))
    if (accountId) params.set('account_id', accountId)
    if (full) params.set('full', 'true')
    return request<{
      ok: boolean
      dry_run: boolean
      total_market: number
      total_orphans: number
      total_stale_db: number
      total_deleted: number
      total_stale_cleared: number
      max_delete: number
      accounts: {
        account_id: string
        label?: string
        error?: string
        market_count?: number
        orphan_count?: number
        orphans?: { spid: string; name: string; status_name: string }[]
        stale_db_count?: number
        stale_db?: { db_id: string; style_code: string; mapped_spid: string; product_name: string }[]
        stale_cleared?: string[]
        deleted?: string[]
        failed?: { spid: string; error: string }[]
      }[]
    }>(`${SAMBA_PREFIX}/shipments/coupang/cleanup-orphans?${params.toString()}`, {
      method: 'POST',
      body: JSON.stringify({ product_ids: productIds && productIds.length > 0 ? productIds : null }),
    })
  },

  // 쿠팡 단건 stale 정리 (DB만, 빠름)
  clearCoupangStaleMapping: (accountId: string, dbId: string) =>
    request<{ ok: boolean; cleared?: boolean; error?: string }>(
      `${SAMBA_PREFIX}/shipments/coupang/clear-stale-mapping`,
      { method: 'POST', body: JSON.stringify({ account_id: accountId, db_id: dbId }) },
    ),

  // 쿠팡 단건 orphan 삭제 (dispatcher 위임 — stop-then-delete 우회 포함)
  deleteCoupangOrphan: (accountId: string, spid: string) =>
    request<{ ok: boolean; error?: string; message?: string; ghost_cleanup?: boolean }>(
      `${SAMBA_PREFIX}/shipments/coupang/delete-orphan`,
      { method: 'POST', body: JSON.stringify({ account_id: accountId, spid }) },
    ),

  // 11번가 유령삭제 양방향 v2 (list_seller_products 기반)
  cleanupElevenstOrphansV2: (dryRun = true, maxDelete = 50, accountId?: string, productIds?: string[]) => {
    const params = new URLSearchParams()
    params.set('dry_run', String(dryRun))
    params.set('max_delete', String(maxDelete))
    if (accountId) params.set('account_id', accountId)
    return request<{
      ok: boolean
      dry_run: boolean
      total_market: number
      total_orphans: number
      total_stale_db: number
      total_deleted: number
      total_stale_cleared: number
      max_delete: number
      accounts: {
        account_id: string
        label?: string
        error?: string
        market_count?: number
        orphan_count?: number
        orphans?: { prd_no: string; name: string; seller_code: string }[]
        stale_db_count?: number
        stale_db?: { db_id: string; style_code: string; mapped_prdno: string; product_name: string }[]
        stale_cleared?: string[]
        deleted?: string[]
        failed?: { prd_no: string; error: string }[]
        recovered_via_seller_code?: number
      }[]
    }>(`${SAMBA_PREFIX}/shipments/elevenst/cleanup-orphans-v2?${params.toString()}`, {
      method: 'POST',
      body: JSON.stringify({ product_ids: productIds && productIds.length > 0 ? productIds : null }),
    })
  },

  // 롯데ON 유령삭제 양방향
  cleanupLotteonOrphans: (dryRun = true, maxDelete = 50, accountId?: string, productIds?: string[]) => {
    const params = new URLSearchParams()
    params.set('dry_run', String(dryRun))
    params.set('max_delete', String(maxDelete))
    if (accountId) params.set('account_id', accountId)
    return request<{
      ok: boolean
      dry_run: boolean
      total_market: number
      total_orphans: number
      total_stale_db: number
      total_deleted: number
      total_stale_cleared: number
      max_delete: number
      accounts: {
        account_id: string
        label?: string
        error?: string
        market_count?: number
        orphan_count?: number
        orphans?: { spd_no: string; name: string; sl_stat_cd: string }[]
        stale_db_count?: number
        stale_db?: { db_id: string; style_code: string; mapped_spd: string; product_name: string }[]
        stale_cleared?: string[]
        deleted?: string[]
        failed?: { spd_no: string; error: string }[]
      }[]
    }>(`${SAMBA_PREFIX}/shipments/lotteon/cleanup-orphans?${params.toString()}`, {
      method: 'POST',
      body: JSON.stringify({ product_ids: productIds && productIds.length > 0 ? productIds : null }),
    })
  },
};

// ── Forbidden Words ──

export interface SambaForbiddenWord {
  id: string;
  word: string;
  type: string;
  scope: string;
  is_active: boolean;
  created_at: string;
}

export const forbiddenApi = {
  listWords: (type?: string) => {
    const p = type ? `?type=${type}` : "";
    return request<SambaForbiddenWord[]>(`${SAMBA_PREFIX}/forbidden/words${p}`);
  },
  createWord: (data: Partial<SambaForbiddenWord>) =>
    request<SambaForbiddenWord>(`${SAMBA_PREFIX}/forbidden/words`, { method: "POST", body: JSON.stringify(data) }),
  updateWord: (id: string, data: Partial<SambaForbiddenWord>) =>
    request<SambaForbiddenWord>(`${SAMBA_PREFIX}/forbidden/words/${id}`, { method: "PUT", body: JSON.stringify(data) }),
  toggleWord: (id: string) =>
    request<SambaForbiddenWord>(`${SAMBA_PREFIX}/forbidden/words/${id}/toggle`, { method: "PUT" }),
  deleteWord: (id: string) =>
    request<{ ok: boolean }>(`${SAMBA_PREFIX}/forbidden/words/${id}`, { method: "DELETE" }),
  bulkSaveWords: (type: string, words: string[]) =>
    request<{ ok: boolean; created: number }>(`${SAMBA_PREFIX}/forbidden/words/bulk`, { method: "POST", body: JSON.stringify({ type, words }) }),
  validate: (name: string) =>
    request<{ is_valid: boolean; forbidden_found: string[]; deletion_found: string[]; clean_name: string }>(
      `${SAMBA_PREFIX}/forbidden/validate`, { method: "POST", body: JSON.stringify({ name }) }),
  clean: (name: string) =>
    request<{ clean_name: string }>(`${SAMBA_PREFIX}/forbidden/clean`, { method: "POST", body: JSON.stringify({ name }) }),

  // Settings
  getSetting: (key: string) => request<unknown>(`${SAMBA_PREFIX}/forbidden/settings/${key}`),
  saveSetting: (key: string, value: unknown) =>
    request<unknown>(`${SAMBA_PREFIX}/forbidden/settings/${key}`, { method: "PUT", body: JSON.stringify({ value }) }),
  getExchangeRates: (forceRefresh = false) =>
    request<{
      provider: string
      base: string
      fetchedAt?: string
      publishedAt?: string
      currencies: Record<string, {
        code: string
        label: string
        baseRate: number
        adjustment: number
        fixedRate: number
        effectiveRate: number
        useFixed: boolean
      }>
    }>(`${SAMBA_PREFIX}/forbidden/exchange-rates${forceRefresh ? "?force_refresh=true" : ""}`),

  // 태그 금지어 통합 조회
  getTagBannedWords: () =>
    request<{ rejected: string[]; brands: string[]; source_sites: string[] }>(`${SAMBA_PREFIX}/forbidden/tag-banned-words`),
};

// ── Proxy Config (프록시 설정 관리) ──

export type ProxyPurpose = 'transmit' | 'collect' | 'autotune'

export interface ProxyConfigItem {
  name: string
  url: string       // 비어있으면 메인 IP (직접 연결)
  purposes: ProxyPurpose[]
  enabled: boolean
}

export const proxyConfigApi = {
  list: () => request<ProxyConfigItem[]>(`${SAMBA_PREFIX}/proxy/config/proxies`),
  save: (proxies: ProxyConfigItem[]) =>
    request<{ ok: boolean; count: number }>(`${SAMBA_PREFIX}/proxy/config/proxies`, {
      method: 'PUT',
      body: JSON.stringify({ proxies }),
    }),
  test: (url: string) => {
    const form = new FormData()
    form.append('url', url)
    return fetchWithAuth(`${SAMBA_PREFIX}/proxy/config/proxies/test`, { method: 'POST', body: form })
      .then(r => r.json() as Promise<{ success: boolean; ip?: string; message?: string }>)
  },
  myIp: () => request<{ ipv4: string; ipv6: string }>(`${SAMBA_PREFIX}/proxy/myip`),
}

// ── Proxy (외부 API 프록시) ──

export const proxyApi = {
  aligoRemain: () =>
    request<{ success: boolean; message: string; SMS_CNT?: number; LMS_CNT?: number; MMS_CNT?: number }>(
      `${SAMBA_PREFIX}/proxy/aligo/remain`, { method: 'POST' }),
  sendSms: (receiver: string, message: string, title?: string, orderId?: string, templateRaw?: string) =>
    request<{ success: boolean; message: string; msg_id?: string; msg_type?: string }>(
      `${SAMBA_PREFIX}/proxy/aligo/send-sms`, { method: 'POST', body: JSON.stringify({ receiver, message, title: title || '', order_id: orderId, template_raw: templateRaw }) }),
  sendKakao: (receiver: string, message: string, templateCode?: string, subject?: string, orderId?: string, templateRaw?: string) =>
    request<{ success: boolean; message: string; msg_type?: string }>(
      `${SAMBA_PREFIX}/proxy/aligo/send-kakao`, { method: 'POST', body: JSON.stringify({ receiver, message, template_code: templateCode || '', subject: subject || '', order_id: orderId, template_raw: templateRaw }) }),
  fetchMessageHistory: (orderId: string) =>
    request<MessageLog[]>(`${SAMBA_PREFIX}/proxy/messages/by-order/${encodeURIComponent(orderId)}`),
  fetchSentFlags: (orderIds: string[]) =>
    request<Record<string, { sms: boolean; kakao: boolean }>>(`${SAMBA_PREFIX}/proxy/messages/sent-flags?order_ids=${orderIds.map(encodeURIComponent).join(',')}`),
  playautoAuthTest: () =>
    request<{ success: boolean; message: string }>(
      `${SAMBA_PREFIX}/proxy/playauto/auth-test`, { method: 'POST' }),
  smartstoreAuthTest: () =>
    request<{ success: boolean; message: string; token_preview?: string }>(
      `${SAMBA_PREFIX}/proxy/smartstore/auth-test`, { method: 'POST' }),
  elevenstAuthTest: () =>
    request<{ success: boolean; message: string }>(
      `${SAMBA_PREFIX}/proxy/11st/auth-test`, { method: 'POST' }),
  elevenstSellerInfo: () =>
    request<{
      success: boolean
      message: string
      data?: {
        shipFromAddress?: string
        returnAddress?: string
        returnFee?: string
        exchangeFee?: string
        dispatchTemplateNo?: string
        dispatchTemplateName?: string
        dispatchTemplateList?: Array<{ tmpltNo: string; tmpltNm: string; reprYn?: string }>
        outboundList?: unknown
        inboundList?: unknown
      }
    }>(
      `${SAMBA_PREFIX}/proxy/11st/seller-info`, { method: 'POST' }),
  coupangAuthTest: () =>
    request<{ success: boolean; message: string }>(
      `${SAMBA_PREFIX}/proxy/coupang/auth-test`, { method: 'POST' }),
  coupangShippingPlaces: (accountId?: string) =>
    request<{
      success: boolean
      message: string
      data?: {
        outboundList: Array<{ code: string; name: string; address: string }>
        inboundList: Array<{ code: string; name: string; address: string; address_detail: string; zipcode: string; phone: string }>
      } | null
    }>(
      `${SAMBA_PREFIX}/proxy/coupang/shipping-places`,
      { method: 'POST', body: JSON.stringify({ account_id: accountId || null }) }
    ),
  lotteonAuthTest: () =>
    request<{ success: boolean; message: string; data?: Record<string, string> }>(
      `${SAMBA_PREFIX}/proxy/lotteon/auth-test`, { method: 'POST' }),
  lotteonDeliveryPolicies: () =>
    request<{ success: boolean; policies: { value: string; label: string }[] }>(
      `${SAMBA_PREFIX}/proxy/lotteon/delivery-policies`),
  lotteonWarehouses: () =>
    request<{
      success: boolean
      departure: { value: string; label: string }[]
      return_: { value: string; label: string }[]
    }>(`${SAMBA_PREFIX}/proxy/lotteon/warehouses`),
  ssgAuthTest: () =>
    request<{ success: boolean; message: string }>(
      `${SAMBA_PREFIX}/proxy/ssg/auth-test`, { method: 'POST' }),
  ssgShippingPolicies: (accountId?: string) =>
    request<{ success: boolean; policies: { shppcstId: string; feeAmt: number; prpayCodDivNm: string; shppcstAplUnitNm: string; divCd: number }[] }>(
      `${SAMBA_PREFIX}/proxy/ssg/shipping-policies${accountId ? `?account_id=${encodeURIComponent(accountId)}` : ''}`),
  ssgAddresses: (accountId?: string) =>
    request<{ success: boolean; addresses: { grpAddrId: string; addrNm: string; bascAddr: string }[] }>(
      `${SAMBA_PREFIX}/proxy/ssg/addresses${accountId ? `?account_id=${encodeURIComponent(accountId)}` : ''}`),
  ssgBrands: (accountId?: string) =>
    request<{ success: boolean; brands: { brandId: string; brandNm: string }[] }>(
      `${SAMBA_PREFIX}/proxy/ssg/brands${accountId ? `?account_id=${encodeURIComponent(accountId)}` : ''}`),
  esmDeliveryInfo: (market: string, accountId?: string) =>
    request<{ success: boolean; places: { placeNo: number; placeNm: string; placeType: number }[]; dispatchPolicies: { dispatchPolicyNo: number; policyNm: string }[]; message?: string }>(
      `${SAMBA_PREFIX}/proxy/esm/${market}/delivery-info${accountId ? `?account_id=${encodeURIComponent(accountId)}` : ''}`),
  gsshopAuthTest: () =>
    request<{ success: boolean; message: string }>(
      `${SAMBA_PREFIX}/proxy/gsshop/auth-test`, { method: 'POST' }),
  marketAuthTest: (marketKey: string) =>
    request<{ success: boolean; message: string }>(
      `${SAMBA_PREFIX}/proxy/market/auth-test/${marketKey}`, { method: 'POST' }),
  lottehomeAuth: (body: { userId: string; password: string; agncNo?: string; env?: string }) =>
    request<{ success: boolean; message: string; certKey?: string }>(
      `${SAMBA_PREFIX}/proxy/lottehome/auth`, { method: 'POST', body: JSON.stringify(body) }),
  lottehomeDeliveryPolicies: () =>
    request<{ success: boolean; policies?: { no: string; nm: string }[]; extra_policies?: { no: string; nm: string }[] }>(`${SAMBA_PREFIX}/proxy/lottehome/delivery-policies`),
  lottehomePlaces: () =>
    request<{ success: boolean; data: { shipping_places: { code: string; name: string; address?: string }[]; return_places: { code: string; name: string; address?: string }[] } }>(`${SAMBA_PREFIX}/proxy/lottehome/delivery-places`),
  claudeTest: () =>
    request<{ success: boolean; message: string }>(
      `${SAMBA_PREFIX}/proxy/claude/test`, { method: 'POST' }),
  geminiTest: () =>
    request<{ success: boolean; message: string }>(
      `${SAMBA_PREFIX}/proxy/gemini/test`, { method: 'POST' }),
  r2Test: () =>
    request<{ success: boolean; message: string }>(
      `${SAMBA_PREFIX}/proxy/r2/test`, { method: 'POST' }),
  // 무신사 검색 상품 수 조회 (size=1로 totalCount만 가져옴)
  musinsaSearchCount: (keyword: string, params?: Record<string, string>) => {
    const qs = new URLSearchParams({ keyword, size: '1', ...params })
    return request<{ success: boolean; totalCount: number }>(`${SAMBA_PREFIX}/proxy/musinsa/search-api?${qs}`)
  },
  // 무신사 브랜드 코드 검색
  brandSearch: (keyword: string, gf?: string) => {
    const qs = new URLSearchParams({ keyword, gf: gf || 'A' })
    return request<{ brands: Array<{ brandCode: string; brandName: string }> }>(`${SAMBA_PREFIX}/proxy/brand-search?${qs}`)
  },
  // 범용 소싱처 검색 카운트
  searchCount: (sourceSite: string, keyword: string, url?: string) => {
    const qs = new URLSearchParams({ source_site: sourceSite, keyword })
    if (url) qs.set('url', url)
    return request<{ totalCount: number }>(`${SAMBA_PREFIX}/proxy/search-count?${qs}`)
  },
  falStatus: () =>
    request<{ status: string; message: string }>(
      `${SAMBA_PREFIX}/proxy/fal/status`),
  listPresets: () =>
    request<{ success: boolean; presets: { key: string; label: string; desc: string; image: string | null }[] }>(
      `${SAMBA_PREFIX}/proxy/preset-images/list`),
  regeneratePreset: (presetKey: string, desc?: string, label?: string, saveOnly?: boolean) =>
    request<{ success: boolean; message: string; image?: string }>(
      `${SAMBA_PREFIX}/proxy/preset-images/regenerate`, {
        method: 'POST',
        body: JSON.stringify({ preset_key: presetKey, desc, label, save_only: saveOnly }),
      }),
  uploadPresetImage: async (presetKey: string, file: File) => {
    const formData = new FormData()
    formData.append('preset_key', presetKey)
    formData.append('file', file)
    const res = await fetchWithAuth(`${SAMBA_PREFIX}/proxy/preset-images/upload`, { method: 'POST', body: formData })
    return res.json() as Promise<{ success: boolean; message: string; image?: string }>
  },
  transformImages: (productIds: string[], scope: { thumbnail: boolean; additional: boolean; detail: boolean }, mode: string, modelPreset?: string) =>
    request<{ success: boolean; status?: string; job_id?: string; message: string; total_transformed: number; total_failed: number }>(
      `${SAMBA_PREFIX}/proxy/images/transform`, {
        method: 'POST',
        body: JSON.stringify({ product_ids: productIds, scope, mode, model_preset: modelPreset }),
      }),
  bgJobStatus: (jobId: string) =>
    request<{ status: string; total: number; current: number; total_transformed: number; total_failed: number; image_current?: number; image_total?: number; current_product_id?: string }>(
      `${SAMBA_PREFIX}/proxy/bg-jobs/${jobId}/status`),
  bgJobsActive: () =>
    request<{ jobs: { job_id: string; status: string; total: number; current: number; created_at: string | null; started_at: string | null }[]; worker_alive: boolean; worker_last_seen: string | null }>(
      `${SAMBA_PREFIX}/proxy/bg-jobs/active`),
  bgJobCancel: (jobId: string) =>
    request<{ success: boolean; job_id?: string; status?: string; message?: string }>(
      `${SAMBA_PREFIX}/proxy/bg-jobs/${jobId}/cancel`, { method: 'POST', body: JSON.stringify({}) }),
  transformByGroups: (groupIds: string[], scope: { thumbnail: boolean; additional: boolean; detail: boolean }, mode: string, modelPreset?: string) =>
    request<{ success: boolean; message: string; total_transformed: number; total_failed: number }>(
      `${SAMBA_PREFIX}/proxy/images/transform`, {
        method: 'POST',
        body: JSON.stringify({ group_ids: groupIds, scope, mode, model_preset: modelPreset }),
      }),
  generateAiTagsByGroups: (groupIds: string[]) =>
    request<{ success: boolean; message: string; total_tagged: number; api_calls: number; input_tokens: number; output_tokens: number; cost_krw: number }>(
      `${SAMBA_PREFIX}/proxy/ai-tags/generate`, {
        method: 'POST',
        body: JSON.stringify({ group_ids: groupIds }),
      }),
  // AI 태그 미리보기 (20개 생성 → 적용 안 함)
  previewAiTags: (productIds: string[], groupIds?: string[]) =>
    request<{
      success: boolean; message: string;
      previews: { group_id: string; group_name: string; product_count: number; rep_name: string; tags: string[]; seo_keywords: string[] }[];
      api_calls: number; input_tokens: number; output_tokens: number; cost_krw: number;
    }>(`${SAMBA_PREFIX}/proxy/ai-tags/preview`, {
      method: 'POST',
      body: JSON.stringify({ product_ids: productIds, group_ids: groupIds || [] }),
    }),
  // AI 태그 확정 적용 (삭제된 태그는 금지태그에 추가)
  applyAiTags: (groups: { group_id: string; tags: string[]; seo_keywords?: string[] }[], removedTags?: string[]) =>
    request<{ success: boolean; message: string; total_tagged: number }>(
      `${SAMBA_PREFIX}/proxy/ai-tags/apply`, {
        method: 'POST',
        body: JSON.stringify({ groups, removed_tags: removedTags || [] }),
      }),
  // 그룹 전체 AI 태그 초기화
  clearAiTags: (groupIds: string[]) =>
    request<{ success: boolean; message: string; total_cleared: number }>(
      `${SAMBA_PREFIX}/proxy/ai-tags/clear`, {
        method: 'POST',
        body: JSON.stringify({ group_ids: groupIds }),
      }),
  // 이미지 필터링 (모델컷/연출컷/배너 자동 제거)
  filterProductImages: (productIds: string[], filterId?: string, scope?: string, filterMethod?: string) =>
    request<{ success: boolean; results: Record<string, { action: string; removed?: number; kept?: number; count?: number }>; total: number; total_removed?: number; errors: Record<string, string> }>(
      `${SAMBA_PREFIX}/proxy/image-filter/filter`, {
        method: 'POST',
        body: JSON.stringify({ product_ids: productIds, filter_id: filterId || '', scope: scope || 'images', method: filterMethod || 'gemma' }),
      }),
  // 소싱처 검색/상세
  sourcingSearch: (site: string, keyword: string, page = 1) =>
    request<{ products: SambaCollectedProduct[]; total: number }>(
      `${SAMBA_PREFIX}/proxy/sourcing/${site}/search?keyword=${encodeURIComponent(keyword)}&page=${page}`),
  sourcingDetail: (site: string, productId: string) =>
    request<SambaCollectedProduct>(
      `${SAMBA_PREFIX}/proxy/sourcing/${site}/detail/${productId}`),
}

// ── Categories ──

export const categoryApi = {
  getMarketCategoryCounts: () => request<Record<string, number>>(`${SAMBA_PREFIX}/categories/markets/counts`),
  listMappings: () => request<unknown[]>(`${SAMBA_PREFIX}/categories/mappings`),
  createMapping: (data: { source_site: string; source_category: string; target_mappings?: unknown }) =>
    request<unknown>(`${SAMBA_PREFIX}/categories/mappings`, { method: "POST", body: JSON.stringify(data) }),
  updateMapping: (id: string, data: unknown) =>
    request<unknown>(`${SAMBA_PREFIX}/categories/mappings/${id}`, { method: "PUT", body: JSON.stringify(data) }),
  deleteMapping: (id: string) =>
    request<{ ok: boolean }>(`${SAMBA_PREFIX}/categories/mappings/${id}`, { method: "DELETE" }),
  findMapping: (sourceSite: string, sourceCategory: string) =>
    request<unknown>(`${SAMBA_PREFIX}/categories/mappings/find?source_site=${encodeURIComponent(sourceSite)}&source_category=${encodeURIComponent(sourceCategory)}`),
  suggest: (sourceCategory: string, targetMarket: string) =>
    request<string[]>(`${SAMBA_PREFIX}/categories/suggest?source_category=${encodeURIComponent(sourceCategory)}&target_market=${encodeURIComponent(targetMarket)}`),
  getTree: (siteName: string) => request<unknown>(`${SAMBA_PREFIX}/categories/tree/${siteName}`),
  saveTree: (siteName: string, data: unknown) =>
    request<unknown>(`${SAMBA_PREFIX}/categories/tree/${siteName}`, { method: "PUT", body: JSON.stringify(data) }),
  deleteTree: (siteName: string) =>
    request<{ ok: boolean }>(`${SAMBA_PREFIX}/categories/tree/${siteName}`, { method: "DELETE" }),
  aiSuggest: (data: { source_site: string; source_category: string; sample_products: string[]; sample_tags?: string[]; target_markets?: string[] }) =>
    request<Record<string, string>>(`${SAMBA_PREFIX}/categories/ai-suggest`, { method: "POST", body: JSON.stringify(data) }),
  aiSuggestBulk: (targetMarkets?: string[], sourceSite?: string, categoryPrefix?: string, signal?: AbortSignal) =>
    request<{ mapped: number; updated: number; skipped: number; errors: string[] }>(
      `${SAMBA_PREFIX}/categories/ai-suggest-bulk`, {
        method: 'POST',
        body: JSON.stringify({
          ...(targetMarkets ? { target_markets: targetMarkets } : {}),
          ...(sourceSite ? { source_site: sourceSite } : {}),
          ...(categoryPrefix ? { category_prefix: categoryPrefix } : {}),
        }),
        signal,
      }),
  seedMarketCategories: () =>
    request<{ ok: boolean; markets: Record<string, number> }>(
      `${SAMBA_PREFIX}/categories/markets/seed`, { method: 'POST' }),
  syncSmartstoreCategories: () =>
    request<{ ok: boolean; count: number; has_codes: boolean }>(
      `${SAMBA_PREFIX}/categories/markets/sync-smartstore`, { method: 'POST' }),
  checkMarketRegistered: (market: string, mappingIds: string[]) =>
    request<{ registered_count: number }>(
      `${SAMBA_PREFIX}/categories/mappings/check-registered`,
      { method: 'POST', body: JSON.stringify({ market, mapping_ids: mappingIds }) }),
  checkAllMarketsRegistered: (mappingIds: string[]) =>
    request<{ blocked: Record<string, number> }>(
      `${SAMBA_PREFIX}/categories/mappings/check-registered-all`,
      { method: 'POST', body: JSON.stringify({ mapping_ids: mappingIds }) }),
  checkRegisteredPerMapping: (mappingIds: string[]) =>
    request<{ registered_ids: string[] }>(
      `${SAMBA_PREFIX}/categories/mappings/check-registered-per-mapping`,
      { method: 'POST', body: JSON.stringify({ mapping_ids: mappingIds }) }),
  bulkDeleteMappings: (mappingIds: string[]) =>
    request<{ ok: boolean; deleted: number }>(
      `${SAMBA_PREFIX}/categories/mappings/bulk-delete`,
      { method: 'POST', body: JSON.stringify({ mapping_ids: mappingIds }) }),
  clearMarketColumn: (market: string, mappingIds: string[]) =>
    request<{ ok: boolean; cleared: number }>(
      `${SAMBA_PREFIX}/categories/mappings/clear-market`,
      { method: 'POST', body: JSON.stringify({ market, mapping_ids: mappingIds }) }),
  aiSeedMarket: (marketType: string) =>
    request<{ ok: boolean; market: string; count: number }>(
      `${SAMBA_PREFIX}/categories/markets/ai-seed/${marketType}`, { method: 'POST' }),
  aiSeedAll: () =>
    request<{ ok: boolean; results: Record<string, { ok: boolean; count?: number; error?: string }> }>(
      `${SAMBA_PREFIX}/categories/markets/ai-seed-all`, { method: 'POST' }),
  syncMarket: (marketType: string) =>
    request<{ ok: boolean; market: string; count: number }>(
      `${SAMBA_PREFIX}/categories/markets/sync/${marketType}`, { method: 'POST' }),
  syncAll: () =>
    request<{ ok: boolean; results: Record<string, unknown> }>(
      `${SAMBA_PREFIX}/categories/markets/sync-all`, { method: 'POST' }),
  copyEsmMapping: (fromMarket: string, toMarket: string, mappingIds?: string[]) =>
    request<{ copied: number; skipped: number; failed: number }>(
      `${SAMBA_PREFIX}/categories/mappings/copy-esm`, {
        method: 'POST',
        body: JSON.stringify({ from_market: fromMarket, to_market: toMarket, mapping_ids: mappingIds }),
      }),
};

// ── Returns ──

export interface SambaReturn {
  id: string;
  order_id: string;
  order_number?: string;
  product_image?: string;
  type: string;
  reason?: string;
  description?: string;
  quantity: number;
  requested_amount?: number;
  status: string;
  timeline?: { date: string; status: string; message: string }[];
  product_name?: string;
  customer_name?: string;
  business_name?: string;
  market?: string;
  confirmed?: boolean;
  market_order_status?: string;
  order_date?: string;
  settlement_amount?: number;
  recovery_amount?: number;
  customer_id?: string;
  company?: string;
  completion_detail?: string;
  check_date?: string;
  customer_phone?: string;
  region?: string;
  memo?: string;
  return_link?: string;
  return_request_date?: string;
  product_location?: string;
  customer_address?: string;
  return_source?: string;
  customer_order_no?: string;
  original_order_no?: string;
  created_at: string;
}


export const returnApi = {
  list: (orderId?: string, status?: string, type?: string, limit = 500, startDate?: string, endDate?: string) => {
    const p = new URLSearchParams();
    if (orderId) p.set("order_id", orderId);
    if (status) p.set("status", status);
    if (type) p.set("type", type);
    if (startDate) p.set("start_date", startDate);
    if (endDate) p.set("end_date", endDate);
    p.set("limit", String(limit));
    return request<SambaReturn[]>(`${SAMBA_PREFIX}/returns?${p}`);
  },
  create: (data: Partial<SambaReturn>) =>
    request<SambaReturn>(`${SAMBA_PREFIX}/returns`, { method: "POST", body: JSON.stringify(data) }),
  get: (id: string) => request<SambaReturn>(`${SAMBA_PREFIX}/returns/${id}`),
  approve: (id: string) => request<SambaReturn>(`${SAMBA_PREFIX}/returns/${id}/approve`, { method: "PUT" }),
  reject: (id: string, reason: string) =>
    request<SambaReturn>(`${SAMBA_PREFIX}/returns/${id}/reject`, { method: "PUT", body: JSON.stringify({ reason }) }),
  complete: (id: string) => request<SambaReturn>(`${SAMBA_PREFIX}/returns/${id}/complete`, { method: "PUT" }),
  cancel: (id: string) => request<SambaReturn>(`${SAMBA_PREFIX}/returns/${id}/cancel`, { method: "PUT" }),
  addNote: (id: string, note: string) =>
    request<SambaReturn>(`${SAMBA_PREFIX}/returns/${id}/note`, { method: "POST", body: JSON.stringify({ note }) }),
  getStats: () => request<Record<string, number>>(`${SAMBA_PREFIX}/returns/stats`),
  getReasons: () => request<Record<string, { value: string; label: string }[]>>(`${SAMBA_PREFIX}/returns/reasons`),
  syncFromMarkets: (days = 30, accountId?: string) => {
    const body: Record<string, unknown> = { days }
    if (accountId) body.account_id = accountId
    return request<{ total_synced: number; results: { account: string; status: string; fetched?: number; synced?: number; message?: string }[] }>(
      `${SAMBA_PREFIX}/returns/sync-from-markets`, { method: "POST", body: JSON.stringify(body) }
    )
  },
  patch: (id: string, data: { confirmed?: boolean; settlement_amount?: number; recovery_amount?: number; check_date?: string; memo?: string; product_location?: string; completion_detail?: string; status?: string; customer_order_no?: string; original_order_no?: string; type?: string; market_order_status?: string; return_source?: string }) =>
    request<SambaReturn>(`${SAMBA_PREFIX}/returns/${id}`, { method: "PATCH", body: JSON.stringify(data) }),
  exchangeAction: (id: string, action: string, reason?: string, extra?: { tracking_number?: string; shipping_company?: string; clm_no?: string }) =>
    request<{ ok: boolean; message: string }>(`${SAMBA_PREFIX}/returns/${id}/exchange-action`, {
      method: "POST", body: JSON.stringify({ action, reason, ...extra }),
    }),
};

// ── CS Inquiries ──

export interface SambaCSInquiry {
  id: string
  market: string
  market_order_id?: string
  market_product_no?: string
  account_name?: string
  inquiry_type: string
  questioner?: string
  collected_product_id?: string
  product_name?: string
  product_image?: string
  product_link?: string
  market_link?: string
  original_link?: string
  content: string
  reply?: string
  reply_status: string
  replied_at?: string
  inquiry_date?: string
  market_inquiry_no?: string
  collected_at: string
  created_at: string
}

export interface CSInquiryListResponse {
  items: SambaCSInquiry[]
  total: number
}

export interface CSReplyTemplate {
  name: string
  content: string
}

export interface CSSyncResultItem {
  account: string
  synced: number
  error?: string
}

export const csInquiryApi = {
  list: (params?: {
    skip?: number
    limit?: number
    market?: string
    inquiry_type?: string
    reply_status?: string
    search?: string
    sort_field?: string
    sort_desc?: boolean
    start_date?: string
    end_date?: string
  }) => {
    const p = new URLSearchParams()
    if (params?.skip) p.set('skip', String(params.skip))
    if (params?.limit) p.set('limit', String(params.limit))
    if (params?.market) p.set('market', params.market)
    if (params?.inquiry_type) p.set('inquiry_type', params.inquiry_type)
    if (params?.reply_status) p.set('reply_status', params.reply_status)
    if (params?.search) p.set('search', params.search)
    if (params?.sort_field) p.set('sort_field', params.sort_field)
    if (params?.sort_desc !== undefined) p.set('sort_desc', String(params.sort_desc))
    if (params?.start_date) p.set('start_date', params.start_date)
    if (params?.end_date) p.set('end_date', params.end_date)
    return request<CSInquiryListResponse>(`${SAMBA_PREFIX}/cs-inquiries?${p}`)
  },
  get: (id: string) => request<SambaCSInquiry>(`${SAMBA_PREFIX}/cs-inquiries/${id}`),
  create: (data: Partial<SambaCSInquiry>) =>
    request<SambaCSInquiry>(`${SAMBA_PREFIX}/cs-inquiries`, { method: 'POST', body: JSON.stringify(data) }),
  reply: (id: string, reply: string) =>
    request<SambaCSInquiry>(`${SAMBA_PREFIX}/cs-inquiries/${id}/reply`, { method: 'POST', body: JSON.stringify({ reply }) }),
  delete: (id: string) =>
    request<{ ok: boolean }>(`${SAMBA_PREFIX}/cs-inquiries/${id}`, { method: 'DELETE' }),
  hide: (id: string) =>
    request<{ ok: boolean }>(`${SAMBA_PREFIX}/cs-inquiries/${id}/hide`, { method: 'POST' }),
  batchDelete: (ids: string[]) =>
    request<{ deleted: number }>(`${SAMBA_PREFIX}/cs-inquiries/batch-delete`, { method: 'POST', body: JSON.stringify({ ids }) }),
  getStats: () => request<Record<string, unknown>>(`${SAMBA_PREFIX}/cs-inquiries/stats`),
  getTemplates: () => request<Record<string, CSReplyTemplate>>(`${SAMBA_PREFIX}/cs-inquiries/templates`),
  addTemplate: (key: string, name: string, content: string) =>
    request<{ ok: boolean }>(`${SAMBA_PREFIX}/cs-inquiries/templates`, { method: 'POST', body: JSON.stringify({ key, name, content }) }),
  deleteTemplate: (key: string) =>
    request<{ ok: boolean }>(`${SAMBA_PREFIX}/cs-inquiries/templates/${key}`, { method: 'DELETE' }),
  syncFromMarkets: (marketName?: string, accountId?: string) =>
    request<{ success: boolean; synced: number; errors: string[]; message: string; results?: CSSyncResultItem[] }>(
      `${SAMBA_PREFIX}/cs-inquiries/sync-from-markets`,
      { method: 'POST', body: JSON.stringify({ market_name: marketName || undefined, account_id: accountId || undefined }) }
    ),
  sendReply: (id: string, reply: string) =>
    request<{ success: boolean; message: string }>(
      `${SAMBA_PREFIX}/cs-inquiries/${id}/send-reply`, { method: 'POST', body: JSON.stringify({ reply }) }
    ),
}

// ── Analytics ──

export interface AnalyticsStats {
  total_sales: number;
  total_orders: number;
  total_profit: number;
  avg_order_value: number;
  profit_rate: number;
}


// ── Detail Templates ──

export interface SambaDetailTemplate {
  id: string;
  name: string;
  main_image_index: number;
  top_html?: string;
  bottom_html?: string;
  top_image_s3_key?: string;
  bottom_image_s3_key?: string;
  img_checks?: Record<string, boolean>;
  img_order?: string[];
  created_at: string;
  updated_at: string;
}

export const detailTemplateApi = {
  list: (skip = 0, limit = 50) =>
    request<SambaDetailTemplate[]>(`${SAMBA_PREFIX}/policies/detail-templates?skip=${skip}&limit=${limit}`),
  get: (id: string) =>
    request<SambaDetailTemplate>(`${SAMBA_PREFIX}/policies/detail-templates/${id}`),
  create: (data: Partial<SambaDetailTemplate>) =>
    request<SambaDetailTemplate>(`${SAMBA_PREFIX}/policies/detail-templates`, {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  update: (id: string, data: Partial<SambaDetailTemplate>) =>
    request<SambaDetailTemplate>(`${SAMBA_PREFIX}/policies/detail-templates/${id}`, {
      method: 'PUT',
      body: JSON.stringify(data),
    }),
  delete: (id: string) =>
    request<{ ok: boolean }>(`${SAMBA_PREFIX}/policies/detail-templates/${id}`, { method: 'DELETE' }),
};

// ── Name Rules ──

export interface SambaNameRule {
  id: string;
  name: string;
  prefix?: string;
  suffix?: string;
  replacements?: Array<{ from: string; to: string; caseInsensitive?: boolean }>;
  replace_mode?: string;
  option_rules?: Array<{ from: string; to: string }>;
  name_composition?: string[];
  market_name_compositions?: Record<string, string[]>;
  brand_display?: string;
  dedup_enabled?: boolean;
  created_at: string;
  updated_at: string;
}

export const nameRuleApi = {
  list: (skip = 0, limit = 50) =>
    request<SambaNameRule[]>(`${SAMBA_PREFIX}/policies/name-rules?skip=${skip}&limit=${limit}`),
  get: (id: string) =>
    request<SambaNameRule>(`${SAMBA_PREFIX}/policies/name-rules/${id}`),
  create: (data: Partial<SambaNameRule>) =>
    request<SambaNameRule>(`${SAMBA_PREFIX}/policies/name-rules`, {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  update: (id: string, data: Partial<SambaNameRule>) =>
    request<SambaNameRule>(`${SAMBA_PREFIX}/policies/name-rules/${id}`, {
      method: 'PUT',
      body: JSON.stringify(data),
    }),
  delete: (id: string) =>
    request<{ ok: boolean }>(`${SAMBA_PREFIX}/policies/name-rules/${id}`, { method: 'DELETE' }),
};

// ── Monitor (오토튠) ──

export interface MonitorEvent {
  id: string
  event_type: string
  severity: string
  source_site?: string
  market_type?: string
  product_id?: string
  product_name?: string
  summary: string
  detail?: Record<string, unknown>
  created_at: string
}

export interface DashboardStats {
  product_stats: {
    total: number
    registered?: number
    by_source: Record<string, number>
    by_priority: Record<string, number>
    by_sale_status: Record<string, number>
  }
  refresh_stats: {
    last_refreshed_at: string | null
    refreshed_1h: number
    refreshed_24h: number
    error_products: number
  }
  price_change_stats: {
    changes_24h: number
    avg_change_pct: number
    top_changes: Array<{
      product_id: string
      name: string
      old: number
      new: number
      pct: number
      at: string
    }>
  }
  site_health: Record<string, {
    interval: number
    errors: number
    probe_ok: boolean | null
    latency_ms: number | null
    checked_at: string | null
  }>
  market_health: Record<string, {
    probe_ok: boolean | null
    latency_ms: number
    error?: string
    checked_at: string | null
  }>
  event_summary: {
    counts_24h: Record<string, number>
    recent_critical: MonitorEvent[]
    recent_warnings: MonitorEvent[]
  }
  hourly_changes: number[]
}

export interface RefreshLogEntry {
  ts: string
  site: string
  product_id: string
  name: string
  msg: string
  level: string
}

export interface RefreshLogsResponse {
  logs: RefreshLogEntry[]
  current_idx: number
  intervals: {
    intervals: Record<string, number>
    errors: Record<string, number>
    safe_intervals: Record<string, number>
  }
}

// ── Job 큐 ──

// 백엔드 backend/domain/samba/job/model.py 와 1:1 정합.
// 변경 시 backend job/model.py · routers/samba/job.py 응답 매핑 동시 점검.
export interface SambaJob {
  id: string
  tenant_id?: string | null
  job_type: string
  status: 'pending' | 'running' | 'completed' | 'failed' | 'cancelled' | string
  progress: number
  current: number
  total: number
  attempt?: number
  payload?: Record<string, unknown> | null
  result?: Record<string, unknown> | null
  error?: string | null
  logs?: unknown[] | null
  created_at: string
  started_at?: string | null
  completed_at?: string | null
}

export interface QueueStatus {
  running: SambaJob | null
  pending: SambaJob[]
  completed_today: number
  failed_today: number
}


export const jobApi = {
  list: (status?: string, limit = 20) => {
    const p = new URLSearchParams({ limit: String(limit) })
    if (status) p.set('status', status)
    return request<SambaJob[]>(`${SAMBA_PREFIX}/jobs?${p}`)
  },
  get: (id: string) => request<SambaJob>(`${SAMBA_PREFIX}/jobs/${id}`),
  create: (body: { job_type: string; payload?: Record<string, unknown>; tenant_id?: string | null }) =>
    request<{ id: string; status: string; job_type: string; duplicate?: boolean; current?: number; total?: number }>(
      `${SAMBA_PREFIX}/jobs`,
      { method: 'POST', body: JSON.stringify({ payload: {}, ...body }) },
    ),
  jobLogs: (jobId: string, since = 0) =>
    request<{ logs: string[] }>(`${SAMBA_PREFIX}/jobs/${jobId}/logs?since=${since}`),
  cancel: (id: string) => request<{ ok: boolean }>(`${SAMBA_PREFIX}/jobs/${id}`, { method: 'DELETE' }),
  cancelAll: () => request<{ ok: boolean }>(`${SAMBA_PREFIX}/jobs/cancel-all`, { method: 'POST' }),
  collectQueue: () => request<QueueStatus>(`${SAMBA_PREFIX}/jobs/collect-queue-status`),
  transmitQueue: () => request<QueueStatus>(`${SAMBA_PREFIX}/jobs/transmit-queue-status`),
  collectLogs: (sinceIdx = 0) => request<{ logs: string[]; next_idx: number }>(`${SAMBA_PREFIX}/jobs/collect-logs?since_idx=${sinceIdx}`),
  shipmentLogs: (sinceIdx = 0) => request<{ logs: string[]; next_idx: number }>(`${SAMBA_PREFIX}/jobs/shipment-logs?since_idx=${sinceIdx}`),
}

export const monitorApi = {
  dashboard: () =>
    request<DashboardStats>(`${SAMBA_PREFIX}/monitor/dashboard`),
  events: (params?: { type?: string; severity?: string; limit?: number }) => {
    const qs = new URLSearchParams()
    if (params?.type) qs.set('event_type', params.type)
    if (params?.severity) qs.set('severity', params.severity)
    if (params?.limit) qs.set('limit', String(params.limit))
    return request<MonitorEvent[]>(`${SAMBA_PREFIX}/monitor/events?${qs}`)
  },
  recentEvents: (limit = 50) =>
    request<MonitorEvent[]>(`${SAMBA_PREFIX}/monitor/events/recent?limit=${limit}`),
  priceChanges: () =>
    request<MonitorEvent[]>(`${SAMBA_PREFIX}/monitor/price-changes`),
  siteHealth: () =>
    request<{ sources: DashboardStats['site_health']; markets: DashboardStats['market_health'] }>(
      `${SAMBA_PREFIX}/monitor/site-health`,
    ),
  refreshLogs: (sinceIdx = 0) =>
    request<RefreshLogsResponse>(`${SAMBA_PREFIX}/monitor/refresh-logs?since_idx=${sinceIdx}`),
  storeScores: () =>
    request<Record<string, { account_id: string; account_label: string; market_type: string; grade: string; grade_code: string; good_service: Record<string, number> | null; penalty: number | null; penalty_rate: number | null; updated_at: string }>>(`${SAMBA_PREFIX}/monitor/store-scores`),
  refreshStoreScores: () =>
    request<{ success: boolean; accounts: number }>(`${SAMBA_PREFIX}/monitor/store-scores/refresh`, { method: 'POST' }),
  siteChanges: (limit = 5) =>
    request<Record<string, Record<string, Array<{ id: string; product_id: string | null; product_name: string | null; detail: Record<string, unknown> | null; created_at: string }>>>>(
      `${SAMBA_PREFIX}/monitor/events/site-changes?limit=${limit}`,
    ),
  marketChanges: (limit = 5) =>
    request<Record<string, Record<string, Array<{ id: string; event_id: string; created_at: string; source_site: string | null; market_product_no: string | null; site_product_id: string | null; account_id: string; account_label: string; product_id: string | null; product_name: string | null; detail: Record<string, unknown> | null }>>>>(
      `${SAMBA_PREFIX}/monitor/events/market-changes?limit=${limit}`,
    ),
}

// ── S3 이미지 헬퍼 ──

const S3_BUCKET = process.env.NEXT_PUBLIC_S3_BUCKET || ''
const S3_REGION = process.env.NEXT_PUBLIC_S3_REGION || 'ap-northeast-2'

/** S3 key → 공개 URL 변환 */
export function getS3Url(key: string): string {
  return `https://${S3_BUCKET}.s3.${S3_REGION}.amazonaws.com/${key}`
}

/** Presigned PUT URL로 파일 직접 업로드 */
export async function uploadToS3(presignedUrl: string, file: File): Promise<void> {
  const res = await fetch(presignedUrl, {
    method: 'PUT',
    headers: { 'Content-Type': file.type },
    body: file,
  })
  if (!res.ok) throw new Error(`S3 업로드 실패: ${res.status}`)
}

// ── 사용자(로그인 계정) 관리 ──

export interface SambaUser {
  id: string
  email?: string
  name?: string
  is_admin: boolean
  status: string
  access_token?: string
  created_at: string
  updated_at: string
  token?: string
  tenant_id?: string
}

export const userApi = {
  list: (skip = 0, limit = 50) =>
    request<SambaUser[]>(`${SAMBA_PREFIX}/users?skip=${skip}&limit=${limit}`),
  create: (data: { email: string; password: string; name: string; invite_code?: string; is_admin?: boolean }) =>
    request<SambaUser>(`${SAMBA_PREFIX}/users`, { method: 'POST', body: JSON.stringify(data) }),
  update: (id: string, data: { name?: string; email?: string; password?: string; is_admin?: boolean; status?: string }) =>
    request<SambaUser>(`${SAMBA_PREFIX}/users/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  delete: (id: string) =>
    request<{ ok: boolean }>(`${SAMBA_PREFIX}/users/${id}`, { method: 'DELETE' }),
  login: (email: string, password: string) =>
    request<SambaUser>(
      `${SAMBA_PREFIX}/users/login`, { method: 'POST', body: JSON.stringify({ email, password }) }
    ),
  loginHistory: (start?: string, end?: string, limit = 100) => {
    const p = new URLSearchParams({ limit: String(limit) })
    if (start) p.set('start', start)
    if (end) p.set('end', end)
    return request<{ id: string; email: string; ip_address: string | null; region: string | null; created_at: string }[]>(
      `${SAMBA_PREFIX}/users/login-history?${p}`
    )
  },
}

// ── AI Sourcing (AI 소싱기) ──

export interface AISourcingBrand {
  brand: string
  count: number
  score: number
  total_sales: number
  avg_profit_rate: number
  categories: string[]
  keywords: string[]
  source: string
  is_safe: boolean
  safety_reason: string
}

export interface AISourcingCombination {
  source_site: string
  brand: string
  keyword: string
  category: string
  category_code: string
  estimated_count: number
  search_url: string
  is_safe: boolean
  safety_reason: string
}

export interface AISourcingSummary {
  total_brands_found: number
  safe_brands: number
  unsafe_brands: number
  total_combinations: number
  total_estimated_products: number
  target_count: number
  total_pairs: number
}

export interface AISourcingResult {
  brands: AISourcingBrand[]
  combinations: AISourcingCombination[]
  summary: AISourcingSummary
  forbidden_words?: string[]
}

export const aiSourcingApi = {
  // 카테고리 목록
  getCategories: () =>
    request<{
      musinsa: { id: string; name: string }[]
      naver: { id: string; name: string }[]
    }>(`${SAMBA_PREFIX}/ai-sourcing/categories`),

  // AI 분석 (SSE 스트리밍) - JSON body
  analyze: (data: {
    use_musinsa: boolean
    use_naver: boolean
    musinsa_categories?: string[]
    naver_categories?: string[]
    target_count: number
  }) =>
    fetchWithAuth(`${SAMBA_PREFIX}/ai-sourcing/analyze`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    }),

  // 월+대카테고리 기반 통합 분석 (SSE 스트리밍)
  analyzeFull: (data: {
    month: number
    main_category: string
    target_count: number
    file?: File
  }) => {
    const formData = new FormData()
    formData.append('month', String(data.month))
    formData.append('main_category', data.main_category)
    formData.append('target_count', String(data.target_count))
    if (data.file) formData.append('file', data.file)
    return fetchWithAuth(`${SAMBA_PREFIX}/ai-sourcing/analyze-full`, {
      method: 'POST',
      body: formData,
    })
  },

  // 엑셀만 분석
  analyzeExcel: async (file: File) => {
    const formData = new FormData()
    formData.append('file', file)
    const res = await fetchWithAuth(`${SAMBA_PREFIX}/ai-sourcing/analyze-excel`, {
      method: 'POST',
      body: formData,
    })
    return res.json() as Promise<{ brands: AISourcingBrand[]; total: number }>
  },

  // 검색그룹 일괄 생성
  createGroups: (combinations: AISourcingCombination[]) =>
    request<{ created: number; ids: string[] }>(
      `${SAMBA_PREFIX}/ai-sourcing/create-groups`,
      { method: 'POST', body: JSON.stringify({ combinations }) }
    ),
}

// ── 스토어케어 (가구매 관리) ──

export interface StoreCareSchedule {
  id: string
  tenant_id?: string
  market_type: string
  account_id: string
  account_label: string
  interval_hours: number
  daily_target: number
  daily_done: number
  product_selection: string
  product_ids?: string[]
  min_price: number
  max_price: number
  status: string
  next_run_at?: string
  last_run_at?: string
  is_active: boolean
  created_at: string
  updated_at: string
}

export interface StoreCarePurchase {
  id: string
  tenant_id?: string
  schedule_id?: string
  market_type: string
  account_id: string
  product_id?: string
  product_name: string
  product_no?: string
  amount: number
  order_no?: string
  buyer_account?: string
  status: string
  error?: string
  created_at: string
  completed_at?: string
}

export const storeCareApi = {
  // 통계
  stats: () =>
    request<{ total: number; success: number; failed: number; total_amount: number }>(
      `${SAMBA_PREFIX}/store-care/stats`
    ),
  // 스케줄
  listSchedules: () =>
    request<StoreCareSchedule[]>(`${SAMBA_PREFIX}/store-care/schedules`),
  createSchedule: (data: Partial<StoreCareSchedule>) =>
    request<StoreCareSchedule>(`${SAMBA_PREFIX}/store-care/schedules`, {
      method: 'POST', body: JSON.stringify(data),
    }),
  updateSchedule: (id: string, data: Partial<StoreCareSchedule>) =>
    request<StoreCareSchedule>(`${SAMBA_PREFIX}/store-care/schedules/${id}`, {
      method: 'PUT', body: JSON.stringify(data),
    }),
  toggleSchedule: (id: string) =>
    request<StoreCareSchedule>(`${SAMBA_PREFIX}/store-care/schedules/${id}/toggle`, { method: 'POST' }),
  deleteSchedule: (id: string) =>
    request<{ ok: boolean }>(`${SAMBA_PREFIX}/store-care/schedules/${id}`, { method: 'DELETE' }),
  // 구매 이력
  listPurchases: (limit = 50, marketType?: string) => {
    const p = new URLSearchParams({ limit: String(limit) })
    if (marketType) p.set('market_type', marketType)
    return request<StoreCarePurchase[]>(`${SAMBA_PREFIX}/store-care/purchases?${p}`)
  },
}

export const snsApi = {
  // WP 사이트
  connectWp: (data: { site_url: string, username: string, app_password: string }) =>
    request(`${SAMBA_PREFIX}/sns/wordpress/connect`, { method: 'POST', body: JSON.stringify(data) }),
  listWpSites: () => request(`${SAMBA_PREFIX}/sns/wordpress/sites`),

  // 키워드 그룹
  createKeywordGroup: (data: { name: string, category: string, keywords: string[] }) =>
    request(`${SAMBA_PREFIX}/sns/keywords`, { method: 'POST', body: JSON.stringify(data) }),
  listKeywordGroups: () => request(`${SAMBA_PREFIX}/sns/keywords`),
  deleteKeywordGroup: (id: string) =>
    request(`${SAMBA_PREFIX}/sns/keywords/${id}`, { method: 'DELETE' }),

  // 이슈 검색
  searchIssues: (data: { category: string, keywords?: string[] }) =>
    request(`${SAMBA_PREFIX}/sns/issue-search`, { method: 'POST', body: JSON.stringify(data) }),

  // 발행
  publish: (data: { wp_site_id: string, issue: Record<string, string>, category: string, language?: string }) =>
    request(`${SAMBA_PREFIX}/sns/publish`, { method: 'POST', body: JSON.stringify(data) }),

  // 자동 포스팅
  saveAutoConfig: (data: { wp_site_id: string, interval_minutes?: number, max_daily_posts?: number, language?: string, product_banner_html?: string }) =>
    request(`${SAMBA_PREFIX}/sns/auto-posting/config`, { method: 'POST', body: JSON.stringify(data) }),
  getAutoPostingUrl: (wpSiteId: string) => `${SAMBA_PREFIX}/sns/auto-posting/start/${wpSiteId}`,
  stopAutoPosting: (wpSiteId: string) =>
    request(`${SAMBA_PREFIX}/sns/auto-posting/stop/${wpSiteId}`, { method: 'POST' }),

  // 이력 + 대시보드
  listPosts: (page?: number, status?: string) =>
    request(`${SAMBA_PREFIX}/sns/posts?page=${page || 1}${status ? '&status=' + status : ''}`),
  getDashboard: () => request(`${SAMBA_PREFIX}/sns/dashboard`),
}

export const wholesaleApi = {
  search: (data: { source: string, keyword: string, page?: number }) =>
    request(`${SAMBA_PREFIX}/wholesale/search`, { method: 'POST', body: JSON.stringify(data) }),
  listProducts: (params?: { source?: string, keyword?: string, page?: number }) => {
    const q = new URLSearchParams()
    if (params?.source) q.set('source', params.source)
    if (params?.keyword) q.set('keyword', params.keyword)
    q.set('page', String(params?.page || 1))
    return request(`${SAMBA_PREFIX}/wholesale/products?${q.toString()}`)
  },
}

// ── Sourcing Accounts ──

export interface SambaSourcingAccount {
  id: string
  site_name: string
  account_label: string
  username: string
  password: string
  chrome_profile?: string
  memo?: string
  balance?: number
  balance_updated_at?: string
  is_active: boolean
  is_login_default?: boolean
  additional_fields?: Record<string, unknown>
  created_at: string
  updated_at: string
}

export interface ChromeProfile {
  directory: string    // 하위 호환 (email 값)
  name: string         // 하위 호환 (display_name 값)
  gaia_name: string    // 하위 호환
  email: string
  display_name: string
}

export interface BalanceResult {
  id: string
  label: string
  balance: number | null
  status: string
  message?: string
}

export const sourcingAccountApi = {
  list: (siteName?: string) => {
    const p = new URLSearchParams()
    if (siteName) p.set('site_name', siteName)
    return request<SambaSourcingAccount[]>(`${SAMBA_PREFIX}/sourcing-accounts?${p}`)
  },
  getSites: () => request<{ id: string; name: string; group: string }[]>(`${SAMBA_PREFIX}/sourcing-accounts/sites`),
  getChromeProfiles: () => request<ChromeProfile[]>(`${SAMBA_PREFIX}/sourcing-accounts/chrome-profiles`),
  get: (id: string) => request<SambaSourcingAccount>(`${SAMBA_PREFIX}/sourcing-accounts/${id}`),
  create: (data: Partial<SambaSourcingAccount>) =>
    request<SambaSourcingAccount>(`${SAMBA_PREFIX}/sourcing-accounts`, { method: 'POST', body: JSON.stringify(data) }),
  update: (id: string, data: Partial<SambaSourcingAccount>) =>
    request<SambaSourcingAccount>(`${SAMBA_PREFIX}/sourcing-accounts/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  toggle: (id: string) =>
    request<SambaSourcingAccount>(`${SAMBA_PREFIX}/sourcing-accounts/${id}/toggle`, { method: 'PUT' }),
  setLoginDefault: (id: string) =>
    request<SambaSourcingAccount>(`${SAMBA_PREFIX}/sourcing-accounts/${id}/set-login-default`, { method: 'PUT' }),
  delete: (id: string) =>
    request<{ ok: boolean }>(`${SAMBA_PREFIX}/sourcing-accounts/${id}`, { method: 'DELETE' }),
  getBalance: (id: string) =>
    request<{ balance: number; mileage: number; balance_updated_at: string; has_cookie: boolean }>(`${SAMBA_PREFIX}/sourcing-accounts/${id}/balance`),
  revealPassword: (id: string) =>
    request<{ password: string }>(`${SAMBA_PREFIX}/sourcing-accounts/${id}/reveal-password`),
  requestBalanceCheck: () =>
    request<{ ok: boolean }>(`${SAMBA_PREFIX}/sourcing-accounts/request-balance-check`, { method: 'POST' }),
  requestChromeProfileSync: () =>
    request<{ ok: boolean }>(`${SAMBA_PREFIX}/sourcing-accounts/request-chrome-profile-sync`, { method: 'POST' }),
}

// ── Rewards (적립금 자동 적립) ──

export interface RewardActionMeta {
  id: string
  site: string
  label: string
}

export interface RewardAccountRow {
  id: string
  site_name: string
  account_label: string
  username: string
  is_active: boolean
  is_login_default: boolean
  balance: number | null
  balance_updated_at: string | null
  mileage: number | null
  last_musinsa_attendance_at: string | null
  last_musinsa_attendance_reward: number | null
  musinsa_attendance_streak: number | null
  last_musinsa_snap_like_at: string | null
  last_musinsa_snap_reward: number | null
  last_abcmart_attendance_at: string | null
  abcmart_stamp_count: number | null
  abcmart_stamp_score: number | null
  // 리뷰 자동작성 누적
  last_musinsa_review_at: string | null
  musinsa_review_total: number | null
  last_musinsa_review_count: number | null
  last_abcmart_review_at: string | null
  abcmart_review_total: number | null
  last_abcmart_review_count: number | null
  last_ssg_review_at: string | null
  ssg_review_total: number | null
  last_ssg_review_count: number | null
  last_gs_review_at: string | null
  gs_review_total: number | null
  last_gs_review_count: number | null
  last_lotteon_review_at: string | null
  lotteon_review_total: number | null
  last_lotteon_review_count: number | null
  last_naver_review_at: string | null
  naver_review_total: number | null
  last_naver_review_count: number | null
  last_kream_review_at: string | null
  kream_review_total: number | null
  last_kream_review_count: number | null
  cookie_expired: boolean
}

export interface RewardsStatus {
  actions: RewardActionMeta[]
  accounts: RewardAccountRow[]
  auto_interval_hours: number
  last_auto_run_at: string | null
}

export const rewardsApi = {
  status: () => request<RewardsStatus>(`${SAMBA_PREFIX}/sourcing-accounts/rewards/status`),
  runNow: (actions?: string[]) =>
    request<{ ok: boolean; summary: unknown[] }>(`${SAMBA_PREFIX}/sourcing-accounts/rewards/run-now`, {
      method: 'POST',
      body: JSON.stringify({ actions: actions ?? null }),
    }),
  runAccount: (accountId: string, actions?: string[]) =>
    request<{ ok: boolean; account_id: string; enqueued: unknown[] }>(
      `${SAMBA_PREFIX}/sourcing-accounts/rewards/run-account/${accountId}`,
      { method: 'POST', body: JSON.stringify({ actions: actions ?? null }) },
    ),
  setAutoSettings: (intervalHours: number) =>
    request<{ ok: boolean; interval_hours: number }>(`${SAMBA_PREFIX}/sourcing-accounts/rewards/auto-settings`, {
      method: 'POST',
      body: JSON.stringify({ interval_hours: intervalHours }),
    }),
}

// ── Tenant (티어/사용량) ──

export interface TenantInfo {
  id: string
  name: string
  plan: string
  limits: { max_products: number; max_markets: number; max_sourcing: number }
  autotune_enabled: boolean
  subscription_start: string | null
  subscription_end: string | null
  is_active: boolean
}

export interface TenantUsage {
  plan: string
  autotune_enabled: boolean
  subscription_end: string | null
  usage: {
    products: { current: number; max: number }
    markets: { current: number; max: number }
    sourcing: { current: number; max: number }
  }
}

export const tenantApi = {
  getMyInfo: () => request<{ tenant: TenantInfo | null }>(`${SAMBA_PREFIX}/tenants/me/info`),
  getMyUsage: () => request<TenantUsage>(`${SAMBA_PREFIX}/tenants/me/usage`),
}

// ── Analytics ──

export interface SourcingRoi {
  source_site: string
  total_cost: number
  total_revenue: number
  total_profit: number
  order_count: number
  avg_profit_per_order: number
  avg_margin_rate: number
  roi: number
}

export interface ProductPerformance {
  product_name: string
  source_site: string
  sales: number
  profit: number
  orders: number
  units: number
}

export interface BrandSales {
  brand: string
  sales: number
  profit: number
  orders: number
  avg_margin_rate: number
}

export const analyticsApi = {
  channels: () => request<{ channel_name: string; sales: number; orders: number; profit: number }[]>(`${SAMBA_PREFIX}/analytics/channels`),
  daily: (days = 30) => request<{ date: string; sales: number; orders: number; profit: number }[]>(`${SAMBA_PREFIX}/analytics/daily?days=${days}`),
  monthly: () => request<{ month: string; sales: number; orders: number; profit: number }[]>(`${SAMBA_PREFIX}/analytics/monthly`),
  sourcingRoi: (start?: string, end?: string) => {
    const p = new URLSearchParams()
    if (start) p.set('start_date', start)
    if (end) p.set('end_date', end)
    return request<SourcingRoi[]>(`${SAMBA_PREFIX}/analytics/sourcing-roi?${p}`)
  },
  bestSellers: (limit = 10, days = 30) =>
    request<ProductPerformance[]>(`${SAMBA_PREFIX}/analytics/best-sellers?limit=${limit}&days=${days}`),
  worstSellers: (limit = 10, days = 30) =>
    request<ProductPerformance[]>(`${SAMBA_PREFIX}/analytics/worst-sellers?limit=${limit}&days=${days}`),
  brands: (start?: string, end?: string) => {
    const p = new URLSearchParams()
    if (start) p.set('start_date', start)
    if (end) p.set('end_date', end)
    return request<BrandSales[]>(`${SAMBA_PREFIX}/analytics/brands?${p}`)
  },
}

export const manualProductApi = {
  list: (params?: { skip?: number; limit?: number }) => {
    const qs = new URLSearchParams({
      source_site: 'manual',
      skip: String(params?.skip ?? 0),
      limit: String(params?.limit ?? 50),
    })
    return request<SambaCollectedProduct[]>(`${SAMBA_PREFIX}/collector/products?${qs}`)
  },

  create: (data: {
    name: string
    name_en?: string
    name_ja?: string
    brand?: string
    original_price?: number
    sale_price?: number
    cost?: number
    images?: string[]
    detail_images?: string[]
    options?: { name: string; stock: number; price?: number }[]
    category1?: string
    category2?: string
    category3?: string
    category4?: string
    manufacturer?: string
    style_code?: string
    origin?: string
    sex?: string
    season?: string
    color?: string
    material?: string
    tags?: string[]
  }) => {
    return request<SambaCollectedProduct>(`${SAMBA_PREFIX}/collector/products`, {
      method: 'POST',
      body: JSON.stringify({ ...data, source_site: 'manual', status: 'collected' }),
    })
  },

  update: (id: string, data: Partial<SambaCollectedProduct>) => {
    return request<SambaCollectedProduct>(`${SAMBA_PREFIX}/collector/products/${id}`, {
      method: 'PUT',
      body: JSON.stringify(data),
    })
  },

  delete: (id: string) => {
    return request<void>(`${SAMBA_PREFIX}/collector/products/${id}`, { method: 'DELETE' })
  },
}
