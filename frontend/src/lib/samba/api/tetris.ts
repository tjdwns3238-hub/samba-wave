import { request, SAMBA_PREFIX } from './shared'

const BASE = `${SAMBA_PREFIX}/tetris`

// ─── 타입 정의 ───────────────────────────────────────────────────────────────

export interface TetrisBrandBlock {
  id: string | null
  source_site: string
  brand_name: string
  policy_id: string | null
  policy_name: string | null
  policy_color: string
  registered_count: number
  collected_count: number
  ai_tagged_count: number
  position_order: number
  is_legacy: boolean
  excluded?: boolean
}

export interface TetrisAccountBlock {
  account_id: string
  account_label: string
  account_order?: number | null
  max_count: number
  total_registered: number
  total_collected: number
  assignments: TetrisBrandBlock[]
}

export interface TetrisMarketGroup {
  market_type: string
  market_name: string
  accounts: TetrisAccountBlock[]
}

export interface TetrisUnassigned {
  source_site: string
  brand_name: string
  policy_id: string | null
  policy_name: string | null
  policy_color: string | null
  registered_count: number
  collected_count: number
  ai_tagged_count: number
}

export interface TetrisBoardResponse {
  markets: TetrisMarketGroup[]
  unassigned: TetrisUnassigned[]
}

export interface TetrisAssignRequest {
  source_site: string
  brand_name: string
  market_account_id: string
  policy_id: string | null
  position_order: number
}

export interface TetrisAssignResponse {
  id: string
  source_site: string
  brand_name: string
  market_account_id: string
  policy_id: string | null
  position_order: number
}

export interface TetrisMoveRequest {
  market_account_id: string
  policy_id: string | null
  position_order: number
}

export interface TetrisSyncIntervalResponse {
  interval_hours: number
  cancelled?: number
}

export interface TetrisSyncResponse {
  assignments: number
  jobs: number
  triggered: number
  skipped?: boolean
  paused?: boolean
  cancelled_before_sync?: number
}

export interface TetrisAssignmentEntry {
  source_site: string
  brand_name: string
  market_account_id: string
}

// ─── API 클라이언트 ───────────────────────────────────────────────────────────

export const tetrisApi = {
  getBoard: () =>
    request<TetrisBoardResponse>(`${BASE}/board`),

  assign: (body: TetrisAssignRequest) =>
    request<TetrisAssignResponse>(`${BASE}/assign`, {
      method: 'POST',
      body: JSON.stringify(body),
      headers: { 'Content-Type': 'application/json' },
    }),

  remove: (id: string) =>
    request<void>(`${BASE}/assign/${id}`, { method: 'DELETE' }),

  move: (id: string, body: TetrisMoveRequest) =>
    request<TetrisAssignResponse>(`${BASE}/assign/${id}/move`, {
      method: 'PATCH',
      body: JSON.stringify(body),
      headers: { 'Content-Type': 'application/json' },
    }),

  reorder: (id: string, body: { position_order: number }) =>
    request<TetrisAssignResponse>(`${BASE}/assign/${id}/reorder`, {
      method: 'PATCH',
      body: JSON.stringify(body),
      headers: { 'Content-Type': 'application/json' },
    }),

  getSyncInterval: () =>
    request<TetrisSyncIntervalResponse>(`${BASE}/sync-interval`),

  setSyncInterval: (interval_hours: number) =>
    request<TetrisSyncIntervalResponse>(`${BASE}/sync-interval`, {
      method: 'POST',
      body: JSON.stringify({ interval_hours }),
      headers: { 'Content-Type': 'application/json' },
    }),

  runSync: (clearPending = false) =>
    request<TetrisSyncResponse>(
      `${BASE}/sync${clearPending ? '?clear_pending=true' : ''}`,
      { method: 'POST' },
    ),

  removeByBrand: (sourceSite: string, brandName: string, marketAccountId: string) =>
    request<{ pending_cancelled: number; delete_job_products: number }>(`${BASE}/remove-by-brand`, {
      method: 'POST',
      body: JSON.stringify({ source_site: sourceSite, brand_name: brandName, market_account_id: marketAccountId }),
      headers: { 'Content-Type': 'application/json' },
    }),

  listAssignments: () =>
    request<TetrisAssignmentEntry[]>(`${BASE}/assignments`),

  setExcluded: (sourceSite: string, brandName: string, marketAccountId: string, excluded: boolean) =>
    request<{ id: string; excluded: boolean }>(`${BASE}/exclude`, {
      method: 'POST',
      body: JSON.stringify({
        source_site: sourceSite,
        brand_name: brandName,
        market_account_id: marketAccountId,
        excluded,
      }),
      headers: { 'Content-Type': 'application/json' },
    }),
}
