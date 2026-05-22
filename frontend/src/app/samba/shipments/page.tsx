'use client'

import React, { useEffect, useState, useCallback, useRef, useMemo } from 'react'
import { useSearchParams } from 'next/navigation'
import {
  shipmentApi,
  accountApi,
  collectorApi,
  policyApi,
  categoryApi,
  type SambaMarketAccount,
  type SambaCollectedProduct,
  type SambaSearchFilter,
  type SambaPolicy,
} from '@/lib/samba/api/commerce'
import { fetchWithAuth } from '@/lib/samba/api/shared'
import { tetrisApi } from '@/lib/samba/api/tetris'
import { showAlert, showConfirm } from '@/components/samba/Modal'
import { SITE_COLORS } from '@/lib/samba/constants'
import { inputStyle, fmtNum, fmtTextNumbers } from '@/lib/samba/styles'
import { fmtTime } from '@/lib/samba/utils'

const JOB_POLL_INTERVAL_MS = 1000
const DELETE_POLL_INTERVAL_MS = 1000
const BG_LOG_POLL_INTERVAL_MS = 5000

const SOURCE_SITES = ['전체', 'MUSINSA', 'KREAM', 'FashionPlus', 'Nike', 'Adidas', 'ABCmart', 'REXMONDE', 'SSG', 'LOTTEON', 'GSShop', 'ElandMall', 'SSF']

function appendShipmentLog(
  setLogMessages: React.Dispatch<React.SetStateAction<string[]>>,
  msg: string,
) {
  const normalizeLog = (value: string) => value.replace(/^\[\d{2}:\d{2}:\d{2}\]\s*/, '')
  const formattedMsg = fmtTextNumbers(msg)
  setLogMessages(prev => {
    const last = prev[prev.length - 1]
    const normalizedMsg = normalizeLog(formattedMsg)
    const normalizedLast = last ? normalizeLog(last) : ''
    const isDuplicateCompletion =
      normalizedMsg === normalizedLast &&
      (normalizedMsg.includes('전송 완료') || normalizedMsg.includes('전송 실패') || normalizedMsg.includes('전송 중단') || normalizedMsg.includes('작업중지'))
    if (isDuplicateCompletion) { return prev }
    return [...prev, formattedMsg].slice(-30)
  })
}
// 영문 market_type → 한글 정책 키 (markets.ts에서 import)

function appendShipmentLogs(
  setLogMessages: React.Dispatch<React.SetStateAction<string[]>>,
  messages: string[],
) {
  if (messages.length === 0) return

  setLogMessages(prev => {
    // 폴러 간 fetch race로 동일 메시지가 다시 들어오는 경우 방어 —
    // 직전 N줄(<=30)에 이미 존재하는 라인은 스킵 (같은 타임스탬프+같은 본문)
    const seen = new Set(prev)
    const next = [...prev]
    for (const raw of messages) {
      const formatted = fmtTextNumbers(raw)
      if (seen.has(formatted)) continue
      seen.add(formatted)
      next.push(formatted)
    }
    return next.slice(-30)
  })
}

async function fetchProductsByIdsChunked(ids: string[]) {
  // 500개씩 청크 분할 후 병렬 호출 (1000개면 2개 청크 동시 요청)
  const chunks: string[][] = []
  for (let i = 0; i < ids.length; i += 500) chunks.push(ids.slice(i, i + 500))
  const results = await Promise.all(chunks.map(c => collectorApi.getProductsByIds(c)))
  const merged: SambaCollectedProduct[] = []
  for (const rows of results) if (Array.isArray(rows)) merged.push(...rows)
  return merged
}

export default function ShipmentsPage() {
  useEffect(() => { document.title = 'SAMBA-상품전송삭제' }, [])
  const searchParams = useSearchParams()
  // 상품관리에서 selected/fromStorage로 넘어온 경우 즉시 로드, 그 외엔 검색버튼 클릭 후 로드
  const hasSearchedRef = useRef(!!(
    searchParams.get('selected') ||
    searchParams.get('fromStorage') === '1'
  ))
  const [products, setProducts] = useState<SambaCollectedProduct[]>([])
  const [accounts, setAccounts] = useState<SambaMarketAccount[]>([])
  const [, setFilters] = useState<SambaSearchFilter[]>([])
  const [policies, setPolicies] = useState<SambaPolicy[]>([])
  const [loading, setLoading] = useState(true)

  // 필터 (입력 단계 — 검색버튼 누르기 전 사용자가 편집 중인 값)
  const [searchField, setSearchField] = useState('group')
  const [searchText, setSearchText] = useState('')
  const [pageSize, setPageSize] = useState(20)
  const [currentPage, setCurrentPage] = useState(1)
  const [siteFilter, setSiteFilter] = useState('전체')
  const [soldOutFilter, setSoldOutFilter] = useState('전체')
  const [registrationFilter, setRegistrationFilter] = useState('전체')
  const [sortBy, setSortBy] = useState('update-desc')
  const [totalCount, setTotalCount] = useState(0)
  const [clientPagingMode, setClientPagingMode] = useState(false)

  // 적용된 필터 (검색버튼 클릭 시점에만 갱신 — 실제 서버 조회/하위 동작은 이 값 사용)
  const [appliedSearchField, setAppliedSearchField] = useState('group')
  const [appliedSearchText, setAppliedSearchText] = useState('')
  const [appliedSiteFilter, setAppliedSiteFilter] = useState('전체')
  const [appliedSoldOutFilter, setAppliedSoldOutFilter] = useState('전체')
  const [appliedRegistrationFilter, setAppliedRegistrationFilter] = useState('전체')

  // 선택
  const [selectedProducts, setSelectedProducts] = useState<string[]>([])
  const [selectedAccounts, setSelectedAccounts] = useState<string[]>([])
  const [selectedMarkets, setSelectedMarkets] = useState<string[]>([])
  // 마켓 타입 → 해당 마켓의 모든 계정 ID
  const getAccountIdsByMarkets = useCallback((marketTypes: string[]) =>
    accounts.filter(a => marketTypes.includes(a.market_type)).map(a => a.id),
  [accounts])
  const [updateItems, setUpdateItems] = useState({ all: true, price: true, thumb: true, detail: true })
  const [skipEnabled] = useState(false)
  const [selectedSites, setSelectedSites] = useState<string[]>([])

  // 전송 로그
  const [logMessages, setLogMessages] = useState<string[]>(['— 전송 시작 버튼을 누르면 로그가 여기에 실시간으로 표시됩니다 —'])
  const [transmitting, setTransmitting] = useState(false)
  const deleting = false
  const [stopping, setStopping] = useState('')  // '' | 'cancel' | 'emergency'
  const [pausedJobPayload, setPausedJobPayload] = useState<Record<string, unknown> | null>(null)
  const [, setProgress] = useState({ current: 0, total: 0 })
  const progressRef = useRef<NodeJS.Timeout | null>(null)
  const abortRef = useRef(false)
  const activeJobIdRef = useRef('')
  const jobPollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const deletePollRef = useRef<ReturnType<typeof setInterval> | null>(null)  // 삭제 중 500ms 폴링
  const bgPollRef = useRef<ReturnType<typeof setInterval> | null>(null)  // 상시 2s 백그라운드 폴링 (다른 창 공유용)
  const sinceIdxRef = useRef(0)  // 링 버퍼 폴링용
  const headInitDoneRef = useRef(false)  // 마운트 시 sinceIdx 헤드 초기화 완료 여부 — 과거 잡 로그 재유입 차단용
  const logContainerRef = useRef<HTMLDivElement>(null)
  const stickToBottomRef = useRef<boolean>(true)

  // 실시간 Job 큐 상태
  type JobQueueItem = { id?: string, status?: string, kind?: 'transmit' | 'delete', markets: string, source_sites?: string[], brands?: string[], product_count: number, current: number, total: number, started_at?: string | null, per_item_sec?: number | null }
  const [jobQueueStatus, setJobQueueStatus] = useState<{
    running: JobQueueItem[],
    pending: JobQueueItem[],
  }>({ running: [], pending: [] })
  // 로컬(프론트엔드 루프 기반) 마켓삭제 잡 — 백엔드 Job이 아니므로 별도 상태로 관리해 잡진행상황에 합쳐 표시
  const [localDeleteJobs, setLocalDeleteJobs] = useState<JobQueueItem[]>([])
  const cancelLocalDeleteIdsRef = useRef<Set<string>>(new Set())
  const [cancellingJobIds, setCancellingJobIds] = useState<string[]>([])
  const jobQueuePollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const startBackgroundLogPolling = useCallback(async () => {
    if (bgPollRef.current) return
    const { API_BASE_URL: apiBase } = await import('@/config/api')
    let bgPolling = false
    bgPollRef.current = setInterval(async () => {
      if (!headInitDoneRef.current) return  // 헤드 초기화 전엔 과거 로그 재유입 차단
      if (jobPollRef.current || deletePollRef.current || bgPolling) return
      bgPolling = true
      try {
        const lr = await fetchWithAuth(`${apiBase}/api/v1/samba/jobs/shipment-logs?since_idx=${sinceIdxRef.current}`)
        const logData = await lr.json()
        const newLogs = (logData.logs || []) as string[]
        sinceIdxRef.current = logData.current_idx || sinceIdxRef.current
        appendShipmentLogs(setLogMessages, newLogs)
      } catch { /* ignore */ }
      bgPolling = false
    }, BG_LOG_POLL_INTERVAL_MS)
  }, [])

  const stopBackgroundLogPolling = useCallback(() => {
    if (bgPollRef.current) {
      clearInterval(bgPollRef.current)
      bgPollRef.current = null
    }
  }, [])

  const ensureDeleteLogPolling = useCallback(async () => {
    if (deletePollRef.current) return
    if (jobPollRef.current) return  // 전송 잡 폴링이 이미 로그를 가져오는 중이면 중복 실행 방지
    stopBackgroundLogPolling()
    const { API_BASE_URL: apiBase } = await import('@/config/api')
    let delPolling = false
    deletePollRef.current = setInterval(async () => {
      if (!headInitDoneRef.current) return  // 헤드 초기화 전엔 과거 로그 재유입 차단
      if (jobPollRef.current) return  // 전송 잡이 중간에 시작되면 즉시 양보
      if (delPolling) return
      delPolling = true
      try {
        const lr = await fetchWithAuth(`${apiBase}/api/v1/samba/jobs/shipment-logs?since_idx=${sinceIdxRef.current}`)
        const logData = await lr.json()
        const newLogs = (logData.logs || []) as string[]
        sinceIdxRef.current = logData.current_idx || sinceIdxRef.current
        appendShipmentLogs(setLogMessages, newLogs)
      } catch { /* ignore */ }
      delPolling = false
    }, DELETE_POLL_INTERVAL_MS)
  }, [stopBackgroundLogPolling])

  // 마운트 시 계정 목록 즉시 로드 (검색 전에도 마켓등록 드롭박스에 표시).
  // localStorage 캐시로 즉시 그려준 뒤 백그라운드 갱신.
  useEffect(() => {
    accountApi.listActiveCached(a => setAccounts(a ?? []))
  }, [])

  // 마운트 시 sinceIdx 를 현재 버퍼 끝(current_idx)으로 초기화 — 과거 transmit 잡 로그 재유입 차단
  // 이 작업이 끝나기 전엔 모든 백그라운드/삭제 폴링이 헤드 가드(headInitDoneRef)로 대기
  useEffect(() => {
    (async () => {
      try {
        const { API_BASE_URL: apiBase } = await import('@/config/api')
        const res = await fetchWithAuth(`${apiBase}/api/v1/samba/jobs/shipment-logs?since_idx=999999999`)
        const data = await res.json()
        sinceIdxRef.current = data.current_idx || 0
      } catch { /* ignore — sinceIdx 0 유지, DB fallback 받아도 dedupe로 제한 */ }
      finally {
        headInitDoneRef.current = true
      }
    })()
  }, [])

  // 전송 로그 자동 스크롤 — 사용자가 끝에 붙어있을 때만 맨 아래로
  useEffect(() => {
    if (!stickToBottomRef.current) return
    const el = logContainerRef.current
    if (!el) return
    el.scrollTop = el.scrollHeight
  }, [logMessages])

  // 컴포넌트 언마운트 시 잡 폴링 정리
  useEffect(() => {
    return () => {
      if (jobPollRef.current) clearInterval(jobPollRef.current)
      if (deletePollRef.current) clearInterval(deletePollRef.current)
      if (bgPollRef.current) clearInterval(bgPollRef.current)
      if (jobQueuePollRef.current) clearInterval(jobQueuePollRef.current)
    }
  }, [])

  // Job 큐 상태 폴링 (5초 간격)
  useEffect(() => {
    const fetchJobQueue = async () => {
      // 탭이 백그라운드면 폴링 스킵 — 다른 탭/창에서 부하 누적 방지
      if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return
      try {
        const { API_BASE_URL: apiBase } = await import('@/config/api')
        const res = await fetchWithAuth(`${apiBase}/api/v1/samba/jobs/transmit-queue-status`)
        const data = await res.json()
        setJobQueueStatus({
          running: Array.isArray(data.running) ? data.running : [],
          pending: Array.isArray(data.pending) ? data.pending : [],
        })
      } catch { /* ignore */ }
    }
    // 초기 로딩 차단 방지: 3초 후 첫 호출
    const delayTimer = setTimeout(() => {
      fetchJobQueue()
      jobQueuePollRef.current = setInterval(fetchJobQueue, 5000)
    }, 3000)
    return () => { clearTimeout(delayTimer); if (jobQueuePollRef.current) clearInterval(jobQueuePollRef.current) }
  }, [])

  useEffect(() => {
    if (localDeleteJobs.length > 0) {
      void ensureDeleteLogPolling()
      return
    }
    if (deletePollRef.current) {
      clearInterval(deletePollRef.current)
      deletePollRef.current = null
    }
    void startBackgroundLogPolling()
  }, [ensureDeleteLogPolling, localDeleteJobs.length, startBackgroundLogPolling])

  // 마운트 시 실행 중인 Job 감지 → 자동 폴링 (오토튠과 동일 패턴)
  useEffect(() => {
    (async () => {
      try {
        const { API_BASE_URL: apiBase } = await import('@/config/api')
        // 실행 중인 Job 확인 (가벼운 호출만)
        const res = await fetchWithAuth(`${apiBase}/api/v1/samba/jobs?status=running&limit=1`)
        const jobs = await res.json()
        const job = Array.isArray(jobs) ? jobs.find((j: Record<string, unknown>) => j.job_type === 'transmit') : null
        if (!job) return
        if (jobPollRef.current || activeJobIdRef.current) return
        const jobId = job.id as string
        activeJobIdRef.current = jobId
        setTransmitting(true)
        setProgress({ current: (job.current || 0) as number, total: (job.total || 0) as number })
        // 이전 잡 로그 잔재 노출 방지 — 현재 버퍼 끝(current_idx)부터 폴링 시작
        try {
          const headLr = await fetchWithAuth(`${apiBase}/api/v1/samba/jobs/shipment-logs?since_idx=999999999`)
          const headData = await headLr.json()
          sinceIdxRef.current = headData.current_idx || 0
        } catch { /* ignore */ }
        // 증분 폴링 즉시 시작 (500ms)
        let polling = false
        const poll = async () => {
          if (polling) return
          polling = true
          try {
            const [jr, lr] = await Promise.all([
              fetchWithAuth(`${apiBase}/api/v1/samba/jobs/${jobId}`),
              fetchWithAuth(`${apiBase}/api/v1/samba/jobs/shipment-logs?since_idx=${sinceIdxRef.current}`),
            ])
            const j = await jr.json()
            const logData = await lr.json()
            setProgress({ current: j.current || 0, total: j.total || 0 })
            const newLogs = (logData.logs || []) as string[]
            sinceIdxRef.current = logData.current_idx || sinceIdxRef.current
            appendShipmentLogs(setLogMessages, newLogs)
            if (j.status === 'completed' || j.status === 'failed' || j.status === 'cancelled') {
              if (jobPollRef.current) { clearInterval(jobPollRef.current); jobPollRef.current = null }
              // Job 결과를 프론트 로그에 직접 표시 (링 버퍼 인스턴스 격리 시 누락 방지)
              const r = (j.result || {}) as Record<string, number>
              const _ts = fmtTime()
              const statusLabel = j.status === 'completed' ? '전송 완료' : j.status === 'failed' ? '일시정지(이어하기 가능)' : '전송 중단'
              appendShipmentLog(setLogMessages, `[${_ts}] ${statusLabel} — 성공 ${fmtNum(r.success || 0)}건, 스킵 ${fmtNum(r.skipped || 0)}건, 실패 ${fmtNum(r.failed || 0)}건`)
              setTransmitting(false)
              activeJobIdRef.current = ''
              // FAILED는 일시정지 = 재개 가능 — payload는 페이로드 복원이 필요하면 별도 endpoint
              load()
            }
          } catch { /* ignore */ }
          polling = false
        }
        poll()
        jobPollRef.current = setInterval(poll, JOB_POLL_INTERVAL_MS)
      } catch { /* ignore */ }
    })()
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  // 일시정지된 transmit 잡 복원 — 새로고침/재진입 후에도 이어하기 가능하도록
  // FAILED + current<total 조건을 백엔드가 검증, CANCELLED(작업중지)는 제외됨
  useEffect(() => {
    (async () => {
      try {
        const { API_BASE_URL: apiBase } = await import('@/config/api')
        const res = await fetchWithAuth(`${apiBase}/api/v1/samba/jobs/last-resumable-transmit`)
        if (!res.ok) return
        const data = await res.json()
        if (!data || !data.payload) return
        // 사용자가 이미 새 전송을 시작했거나 이어하기 진행 중이면 덮어쓰지 않음
        if (transmittingRef.current || activeJobIdRef.current) return
        setPausedJobPayload({
          job_type: data.job_type || 'transmit',
          payload: data.payload,
        })
        const cur = data.current || 0
        const tot = data.total || 0
        appendShipmentLog(setLogMessages, `[${fmtTime()}] 일시정지된 전송 발견 — ${fmtNum(cur)}/${fmtNum(tot)}부터 이어하기 가능`)
      } catch { /* ignore */ }
    })()
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  // (제거됨) 상시 백그라운드 폴링 — startBackgroundLogPolling(L147)이 동일 역할 수행.
  // 두 useEffect가 모두 bgPollRef.current에 setInterval을 세팅하던 탓에
  // 한쪽이 ref를 덮어쓰면 다른 쪽은 고아 인터벌로 계속 살아남아
  // 같은 since_idx로 shipment-logs를 두 번 가져와 화면에 같은 줄이 2번 찍히는 원인이었음.

  // handleStart 최신 참조를 유지하는 ref (stale closure 방지)
  const handleStartRef = useRef<(targetIds?: string[]) => Promise<void>>(async () => {})

  // 페이지 이탈해도 서버 잡은 계속 실행 (오토튠과 동일 — 다시 열면 자동 연결)
  const transmittingRef = useRef(false)
  useEffect(() => { transmittingRef.current = transmitting }, [transmitting])

  // 카테고리 매핑 데이터
  const [categoryMappings, setCategoryMappings] = useState<{ source_site: string; source_category: string; target_mappings: Record<string, string> }[]>([])
  const productsById = useMemo(() => new Map(products.map(product => [product.id, product] as const)), [products])
  const accountsById = useMemo(() => new Map(accounts.map(account => [account.id, account] as const)), [accounts])
  const policiesById = useMemo(() => new Map(policies.map(policy => [policy.id, policy] as const)), [policies])
  const categoryMappingByKey = useMemo(() => {
    const map = new Map<string, Record<string, string>>()
    for (const mapping of categoryMappings) {
      map.set(`${mapping.source_site}::${mapping.source_category}`, mapping.target_mappings || {})
    }
    return map
  }, [categoryMappings])
  const availableMarketTypes = useMemo(() => new Set(accounts.map(account => account.market_type)), [accounts])

  // 검색 필터가 사용자에 의해 변경되었는지 추적
  const userFilterChangedRef = useRef(false)
  // by-ids 모드(상품관리에서 N개 선택 후 진입) 1회 로드 완료 여부 — 페이지 이동 시 재로드 방지
  const byIdsLoadedRef = useRef(false)

  // 필터 변경 시 URL selected 파라미터 무시 + 선택 초기화
  const onFilterChange = useCallback(() => {
    userFilterChangedRef.current = true
    byIdsLoadedRef.current = false  // 일반 검색 모드 전환 → 재로드 허용
    setSelectedProducts([])
    // URL에서 selected 파라미터 제거
    const url = new URL(window.location.href)
    if (url.searchParams.has('selected') || url.searchParams.has('fromStorage')) {
      url.searchParams.delete('selected')
      url.searchParams.delete('sites')
      url.searchParams.delete('fromStorage')
      url.searchParams.delete('autoAll')
      url.searchParams.delete('priceOnly')
      sessionStorage.removeItem('shipment_selected')
      sessionStorage.removeItem('shipment_sites')
      window.history.replaceState({}, '', url.toString())
    }
  }, [])

  const load = useCallback(async () => {
    if (!hasSearchedRef.current) { setLoading(false); return }
    // URL에서 선택된 상품 ID 먼저 확인 (단, 사용자가 필터를 변경했으면 무시)
    const urlParams = new URLSearchParams(window.location.search)
    const preIds = userFilterChangedRef.current
      ? []
      : urlParams.get('selected')?.split(',').filter(Boolean)
        || (urlParams.get('fromStorage') === '1' ? sessionStorage.getItem('shipment_selected')?.split(',').filter(Boolean) : null)
        || []

    // by-ids 모드 페이지 이동 시: 이미 1회 로드 완료 → 서버 재호출 스킵 (클라이언트 슬라이스만 사용)
    if (byIdsLoadedRef.current && preIds.length === 0 && !userFilterChangedRef.current) {
      return
    }

    setLoading(true)

    // 검색 조건에 따라 서버 API 파라미터 구성 — 입력 중 값이 아닌 "검색버튼으로 적용된 값" 사용
    const scrollParams: Record<string, string | number> = { skip: (currentPage - 1) * pageSize, limit: pageSize }
    if (appliedSearchText.trim()) {
      scrollParams.search = appliedSearchText.trim()
      const typeMap: Record<string, string> = { name: 'name', brand: 'brand', name_all: 'name_all', group: 'filter', no: 'no', policy: 'policy' }
      scrollParams.search_type = typeMap[appliedSearchField] || 'name'
    }
    if (appliedSiteFilter !== '전체') scrollParams.source_site = appliedSiteFilter
    if (appliedSoldOutFilter !== '전체') scrollParams.sold_out_filter = appliedSoldOutFilter === '품절' ? 'sold_out' : 'not_sold_out'
    if (appliedRegistrationFilter !== '전체') {
      if (appliedRegistrationFilter.startsWith('reg_') || appliedRegistrationFilter.startsWith('unreg_') || appliedRegistrationFilter.startsWith('mtype_')) {
        scrollParams.status = appliedRegistrationFilter
      } else {
        scrollParams.status = appliedRegistrationFilter === '등록' ? 'market_registered' : appliedRegistrationFilter === '미등록' ? 'market_unregistered' : ''
      }
    }
    if (sortBy) scrollParams.sort_by = sortBy

    // 선택된 상품이 있으면 해당 상품만 조회, 없으면 scroll API
    const productPromise = preIds.length > 0
      ? fetchProductsByIdsChunked(preIds).catch(() => [] as SambaCollectedProduct[])
      : collectorApi.scrollProducts(scrollParams).then(r => { setTotalCount(r.total || 0); return r.items }).catch(() => [] as SambaCollectedProduct[])

    // 필수 데이터(상품+계정)만 먼저 로드 → 즉시 화면 표시
    const [p, a] = await Promise.all([
      productPromise,
      accountApi.listActive().catch(() => []),
    ])
    if (preIds.length > 0) {
      setTotalCount(p.length)
      byIdsLoadedRef.current = true  // 페이지 이동 시 재로드 방지
    }
    if (preIds.length > 0) {
      setClientPagingMode(true)
    } else {
      setClientPagingMode(false)
    }
    setProducts(p)
    setAccounts(a)
    setLoading(false)

    // 나머지는 백그라운드 로드 (화면 차단 없음)
    Promise.all([
      collectorApi.listFilters().catch(() => []),
      policyApi.list().catch(() => []),
      categoryApi.listMappings().catch(() => []),
    ]).then(([f, pol, cm]) => {
      setFilters(f)
      setPolicies(pol)
      setCategoryMappings(Array.isArray(cm) ? cm as typeof categoryMappings : [])
    })
  }, [appliedSearchText, appliedSearchField, appliedSiteFilter, appliedSoldOutFilter, appliedRegistrationFilter, sortBy, currentPage, pageSize]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => { load() }, [load])

  // 검색버튼 클릭 시 입력값을 적용값으로 복사 + 선택/URL 초기화 + 1페이지로 리셋
  const handleSearch = useCallback(() => {
    onFilterChange()
    hasSearchedRef.current = true
    setAppliedSearchField(searchField)
    setAppliedSearchText(searchText)
    setAppliedSiteFilter(siteFilter)
    setAppliedSoldOutFilter(soldOutFilter)
    setAppliedRegistrationFilter(registrationFilter)
    setCurrentPage(1)
  }, [onFilterChange, searchField, searchText, siteFilter, soldOutFilter, registrationFilter])

  // 초기화 — 입력값/적용값 모두 리셋 후 즉시 재조회
  const handleResetSearch = useCallback(() => {
    onFilterChange()
    setSearchField('name')
    setSearchText('')
    setSiteFilter('전체')
    setSoldOutFilter('전체')
    setRegistrationFilter('전체')
    setAppliedSearchField('name')
    setAppliedSearchText('')
    setAppliedSiteFilter('전체')
    setAppliedSoldOutFilter('전체')
    setAppliedRegistrationFilter('전체')
    setCurrentPage(1)
  }, [onFilterChange])
  useEffect(() => { return () => { if (progressRef.current) clearInterval(progressRef.current) } }, [])

  // sessionStorage 또는 URL에서 선택된 상품 ID 자동 적용 + 필터링
  const fromStorage = searchParams.get('fromStorage') === '1'
  const preSelectedIds = fromStorage
    ? (sessionStorage.getItem('shipment_selected')?.split(',') || [])
    : (searchParams.get('selected')?.split(',') || [])
  const preSelectedSites = fromStorage
    ? (sessionStorage.getItem('shipment_sites')?.split(',') || [])
    : (searchParams.get('sites')?.split(',') || [])
  const autoAll = searchParams.get('autoAll') === '1'
  const priceOnly = searchParams.get('priceOnly') === '1'
  const initializedRef = useRef(false)
  const importedSelectionRef = useRef<string[]>([])
  useEffect(() => {
    if (initializedRef.current) return
    if (products.length === 0 || policiesById.size === 0) return
    initializedRef.current = true
    // sessionStorage 정리
    if (fromStorage) {
      sessionStorage.removeItem('shipment_selected')
      sessionStorage.removeItem('shipment_sites')
      const url = new URL(window.location.href)
      url.searchParams.delete('fromStorage')
      window.history.replaceState({}, '', url.toString())
    }

    if (preSelectedIds.length > 0) {
      const ids = preSelectedIds.filter(id => productsById.has(id))
      if (ids.length > 0) {
        importedSelectionRef.current = ids
        setSelectedProducts(ids)
      }
    }
    if (preSelectedSites.length > 0) {
      setSelectedSites(preSelectedSites.filter(s => s))
    }
    if (autoAll && accounts.length > 0) {
      setUpdateItems(priceOnly
        ? { all: false, price: true, thumb: false, detail: false }
        : { all: false, price: false, thumb: false, detail: false }
      )
      // 선택된 상품의 카테고리 매핑에 연결된 마켓만 체크
      const selectedProds = preSelectedIds
        .map(id => productsById.get(id))
        .filter((product): product is SambaCollectedProduct => Boolean(product))
      const mappedMarketTypes = new Set<string>()
      for (const prod of selectedProds) {
        const cats = [prod.category1, prod.category2, prod.category3, prod.category4].filter(Boolean)
        const catPath = cats.join(' > ')
        if (!catPath) continue
        const targetMappings = categoryMappingByKey.get(`${prod.source_site}::${catPath}`)
        if (targetMappings) {
          for (const marketKey of Object.keys(targetMappings)) {
            if (targetMappings[marketKey]) {
              mappedMarketTypes.add(marketKey)
            }
          }
        }
      }
      // 정책에 연결된 마켓: 카테고리 매핑 불필요 — 정책에 연결되어 있으면 자동 체크
      for (const prod of selectedProds) {
        if (!prod?.applied_policy_id) continue
        const policy = policiesById.get(prod.applied_policy_id)
        if (!policy?.market_policies || typeof policy.market_policies !== 'object') continue
        const mp = policy.market_policies as Record<string, { accountId?: string; accountIds?: string[] }>
        for (const marketPolicy of Object.values(mp)) {
          const ids = Array.isArray(marketPolicy.accountIds)
            ? marketPolicy.accountIds
            : marketPolicy.accountId ? [marketPolicy.accountId] : []
          for (const aid of ids) {
            const acc = accountsById.get(aid)
            if (acc?.market_type) mappedMarketTypes.add(acc.market_type)
          }
        }
      }
      const targetTypes = mappedMarketTypes.size > 0
        ? [...mappedMarketTypes].filter(type => availableMarketTypes.has(type))
        : [...availableMarketTypes]
      setSelectedMarkets(targetTypes)
      setSelectedAccounts(getAccountIdsByMarkets(targetTypes))
    }
  }, [products, accounts, fromStorage, preSelectedIds, preSelectedSites, autoAll, priceOnly, productsById, categoryMappingByKey, policiesById, accountsById, availableMarketTypes, getAccountIdsByMarkets])

  const toggleProduct = (id: string) => setSelectedProducts(prev => prev.includes(id) ? prev.filter(x => x !== id) : [...prev, id])
  const toggleAllProducts = () => {
    const pageIds = pageProducts.map(p => p.id)
    const allChecked = pageIds.every(id => selectedProducts.includes(id))
    setSelectedProducts(allChecked ? [] : pageIds)
  }
  const allSites = SOURCE_SITES.filter(s => s !== '전체')
  const toggleSite = (site: string) => setSelectedSites(prev => prev.includes(site) ? prev.filter(x => x !== site) : [...prev, site])
  const toggleAllSites = () => setSelectedSites(prev => prev.length === allSites.length ? [] : [...allSites])

  // 서버에서 필터/정렬/페이지네이션 완료된 상태 — 프론트 필터링 불필요
  const filteredProducts = products

  // by-ids 진입(상품관리에서 N개 선택 후 이동) 시 products에 전체가 한 번에 들어옴 → 클라이언트에서 페이지 슬라이스
  const pageProducts = useMemo(
    () => clientPagingMode
      ? filteredProducts.slice((currentPage - 1) * pageSize, currentPage * pageSize)
      : filteredProducts,
    [clientPagingMode, filteredProducts, currentPage, pageSize],
  )

  // 등록된 마켓 목록 (동적)
  const registeredMarkets = useMemo(() => {
    const marketSet = new Set<string>()
    for (const p of products) {
      for (const aid of (p.registered_accounts || [])) {
        const acc = accountsById.get(aid)
        if (acc) marketSet.add(acc.market_type)
      }
    }
    const marketNameMap: Record<string, string> = {}
    for (const acc of accounts) {
      if (!marketNameMap[acc.market_type]) marketNameMap[acc.market_type] = acc.market_name
    }
    return [...marketSet].map(type => ({ type, name: marketNameMap[type] || type }))
  }, [products, accounts, accountsById])

  const handleMarketDelete = async () => {
    if (selectedAccounts.length === 0) { showAlert('마켓 계정을 선택해주세요'); return }
    // 비상정지 해제 (이전 중단 상태 초기화)
    try {
      const { API_BASE_URL: apiBase } = await import('@/config/api')
      await fetchWithAuth(`${apiBase}/api/v1/samba/shipments/emergency-clear`, { method: 'POST' })
    } catch { /* ignore */ }
    // 등록된 마켓이 있는 상품만 필터
    const selectedAccountSet = new Set(selectedAccounts)
    const targetProducts = selectedProducts.filter(pid => {
      const p = productsById.get(pid)
      return p && (p.registered_accounts || []).some(aid => selectedAccountSet.has(aid))
    })
    if (targetProducts.length === 0) { showAlert('선택된 소싱사이트/마켓에 해당하는 등록 상품이 없습니다'); return }

    // 실제 등록된 계정만 추출 (선택 계정 ∩ 상품별 registered_accounts)
    const effectiveDeleteAccIds = new Set<string>()
    for (const pid of targetProducts) {
      const p = productsById.get(pid)
      for (const aid of (p?.registered_accounts || [])) {
        if (selectedAccountSet.has(aid)) effectiveDeleteAccIds.add(aid)
      }
    }
    const effectiveDeleteList = [...effectiveDeleteAccIds]
    const targetLabels = effectiveDeleteList.map(aid => {
      const acc = accountsById.get(aid)
      return acc ? `${acc.market_name}(${acc.seller_id || '-'})` : aid
    }).join(', ')
    if (!await showConfirm(`${fmtNum(targetProducts.length)}개 상품을 ${targetLabels || '선택 계정'}에서 마켓삭제하시겠습니까?`)) return

    const ts = fmtTime
    const addLog = (msg: string) => appendShipmentLog(setLogMessages, msg)
    const accLabelMap: Record<string, string> = {}
    for (const acc of accounts) {
      accLabelMap[acc.id] = `${acc.market_name}(${acc.seller_id || acc.business_name || '-'})`
    }
    const targetAccLabels = effectiveDeleteList.map(aid => accLabelMap[aid] || aid).join(', ')
    addLog(`[${ts()}] 마켓삭제 시작 — 상품 ${fmtNum(targetProducts.length)}개, ${targetAccLabels}`)

    // 가상 잡 등록 — 잡진행상황 패널에 전송잡과 동일한 형태로 노출
    const localJobId = `local-delete-${Date.now()}`
    setLocalDeleteJobs(prev => [...prev, {
      id: localJobId, kind: 'delete', markets: targetAccLabels, source_sites: [], brands: [],
      product_count: targetProducts.length, current: 0, total: targetProducts.length,
      started_at: new Date().toISOString(),
    }])

    // 삭제 중 500ms 링 버퍼 폴링 시작 — 다른 창에서도 실시간 로그 공유
    let cancelledMid = false
    for (let i = 0; i < targetProducts.length; i++) {
      if (cancelLocalDeleteIdsRef.current.has(localJobId)) { cancelledMid = true; break }
      const pid = targetProducts[i]
      const prod = productsById.get(pid)
      // 이 상품에 등록된 계정만 삭제 대상
      const prodAccIds = (prod?.registered_accounts || []).filter(aid => selectedAccountSet.has(aid))
      if (prodAccIds.length === 0) continue
      try {
        // log_to_buffer=true: 이 페이지의 링 버퍼 폴링으로 실시간 로그 표시
        await shipmentApi.marketDelete([pid], prodAccIds, i + 1, targetProducts.length, true)
      } catch { /* 개별 실패는 백엔드가 링 버퍼에 기록 */ }
      setLocalDeleteJobs(prev => prev.map(j => j.id === localJobId ? { ...j, current: i + 1 } : j))
    }

    // 가상 잡 정리
    setLocalDeleteJobs(prev => prev.filter(j => j.id !== localJobId))
    cancelLocalDeleteIdsRef.current.delete(localJobId)

    // 폴링 종료 후 백그라운드 폴링 복원

    addLog(`[${ts()}] 마켓삭제 ${cancelledMid ? '중지됨' : '완료'}`)

    // 백그라운드 폴링 재시작
    await load()
  }

  const handleSearchDelete = async () => {
    if (selectedAccounts.length === 0) { showAlert('마켓 계정을 선택해주세요'); return }
    if (selectedSites.length === 0) { showAlert('소싱사이트를 선택해주세요'); return }

    // 현재 검색 조건(적용된 값)으로 전체 상품 ID 조회 (경량) → 청크 분할 상세 조회
    const idParams: Parameters<typeof collectorApi.getProductIds>[0] = {}
    if (appliedSearchText.trim()) {
      idParams.search = appliedSearchText.trim()
      const typeMap: Record<string, string> = { name: 'name', brand: 'brand', name_all: 'name_all', group: 'filter', no: 'no', policy: 'policy' }
      idParams.search_type = typeMap[appliedSearchField] || 'name'
    }
    if (appliedSiteFilter !== '전체') idParams.source_site = appliedSiteFilter
    if (appliedSoldOutFilter !== '전체') idParams.sold_out_filter = appliedSoldOutFilter === '품절' ? 'sold_out' : 'not_sold_out'
    if (appliedRegistrationFilter !== '전체') {
      if (appliedRegistrationFilter.startsWith('reg_') || appliedRegistrationFilter.startsWith('unreg_') || appliedRegistrationFilter.startsWith('mtype_')) {
        idParams.status = appliedRegistrationFilter
      } else {
        idParams.status = appliedRegistrationFilter === '등록' ? 'market_registered' : appliedRegistrationFilter === '미등록' ? 'market_unregistered' : appliedRegistrationFilter === '품절' ? 'sold_out' : ''
      }
    }

    let allItems
    try {
      const idResult = await collectorApi.getProductIds(idParams)
      const siteSet = new Set(selectedSites)
      const allProds = await fetchProductsByIdsChunked(idResult.ids)
      allItems = allProds.filter(p => siteSet.has(p.source_site))
    } catch (e) {
      showAlert('상품 조회 실패: ' + (e instanceof Error ? e.message : ''), 'error')
      return
    }

    // 선택 계정에 등록된 상품만 필터
    const selectedSet = new Set(selectedAccounts)
    const targetProducts = allItems.filter(p =>
      (p.registered_accounts || []).some(aid => selectedSet.has(aid))
    )
    if (targetProducts.length === 0) { showAlert('선택된 소싱사이트/마켓에 해당하는 등록 상품이 없습니다'); return }

    const accLabelMap: Record<string, string> = {}
    for (const acc of accounts) {
      accLabelMap[acc.id] = `${acc.market_name}(${acc.seller_id || acc.business_name || '-'})`
    }
    const effectiveDeleteAccIds = new Set<string>()
    for (const p of targetProducts) {
      for (const aid of (p.registered_accounts || [])) {
        if (selectedSet.has(aid)) effectiveDeleteAccIds.add(aid)
      }
    }
    const effectiveDeleteList = [...effectiveDeleteAccIds]
    const targetLabels = effectiveDeleteList.map(aid => accLabelMap[aid] || aid).join(', ')

    if (!await showConfirm(`검색결과 ${fmtNum(targetProducts.length)}개 상품을 ${targetLabels || '선택 계정'}에서 마켓삭제하시겠습니까?`)) return

    // 비상정지 해제
    try {
      const { API_BASE_URL: apiBase } = await import('@/config/api')
      await fetchWithAuth(`${apiBase}/api/v1/samba/shipments/emergency-clear`, { method: 'POST' })
    } catch { /* ignore */ }

    const ts = fmtTime
    const addLog = (msg: string) => appendShipmentLog(setLogMessages, msg)
    addLog(`[${ts()}] 검색결과 마켓삭제 시작 — 상품 ${fmtNum(targetProducts.length)}개, ${targetLabels}`)

    // 가상 잡 등록 — 잡진행상황 패널에 전송잡과 동일한 형태로 노출
    const localJobIdSearch = `local-delete-${Date.now()}`
    setLocalDeleteJobs(prev => [...prev, {
      id: localJobIdSearch, kind: 'delete', markets: targetLabels, source_sites: [], brands: [],
      product_count: targetProducts.length, current: 0, total: targetProducts.length,
      started_at: new Date().toISOString(),
    }])

    // 삭제 중 500ms 링 버퍼 폴링 시작 — 다른 창에서도 실시간 로그 공유
    let cancelledMidSearch = false
    for (let i = 0; i < targetProducts.length; i++) {
      if (cancelLocalDeleteIdsRef.current.has(localJobIdSearch)) { cancelledMidSearch = true; break }
      const prod = targetProducts[i]
      const prodAccIds = (prod.registered_accounts || []).filter(aid => selectedSet.has(aid))
      if (prodAccIds.length === 0) continue
      try {
        // log_to_buffer=true: 이 페이지의 링 버퍼 폴링으로 실시간 로그 표시
        await shipmentApi.marketDelete([prod.id], prodAccIds, i + 1, targetProducts.length, true)
      } catch { /* 개별 실패는 백엔드가 링 버퍼에 기록 */ }
      setLocalDeleteJobs(prev => prev.map(j => j.id === localJobIdSearch ? { ...j, current: i + 1 } : j))
    }

    // 가상 잡 정리
    setLocalDeleteJobs(prev => prev.filter(j => j.id !== localJobIdSearch))
    cancelLocalDeleteIdsRef.current.delete(localJobIdSearch)

    // 폴링 종료 후 백그라운드 폴링 복원

    addLog(`[${ts()}] 검색결과 마켓삭제 ${cancelledMidSearch ? '중지됨' : '완료'}`)

    // 백그라운드 폴링 재시작
    await load()
  }

  const handleStart = async (targetIds?: string[]) => {
    // 중복 클릭 방지 — 이미 전송 중이거나 Job 진행 중이면 무시
    if (transmittingRef.current || activeJobIdRef.current) return

    const inputIds = targetIds || selectedProducts
    if (inputIds.length === 0) { showAlert('상품을 선택해주세요'); return }
    if (selectedAccounts.length === 0) { showAlert('마켓 계정을 선택해주세요'); return }
    if (selectedSites.length === 0) { showAlert('소싱사이트를 선택해주세요'); return }

    // 비상정지 해제 (이전 중단 상태 초기화)
    try {
      const { API_BASE_URL: apiBase } = await import('@/config/api')
      await fetchWithAuth(`${apiBase}/api/v1/samba/shipments/emergency-clear`, { method: 'POST' })
    } catch { /* ignore */ }

    // 소싱사이트 체크 + 현재 필터에 표시된 상품만 전송
    const siteSet = new Set(selectedSites)
    const filteredSet = new Set(filteredProducts.map(p => p.id))
    const visibleSelected = (targetIds || inputIds).filter(id => {
      if (!targetIds && !filteredSet.has(id)) return false
      const prod = productsById.get(id)
      return prod ? siteSet.has(prod.source_site) : false
    })
    if (visibleSelected.length === 0) { showAlert('선택된 소싱사이트에 해당하는 상품이 없습니다'); return }

    setPausedJobPayload(null)
    setTransmitting(true)

    const ts = fmtTime
    const addLog = (msg: string) => appendShipmentLog(setLogMessages, msg)

    // 계정 ID → 표시명 매핑
    const accountLabelMap: Record<string, string> = {}
    for (const acc of accounts) {
      accountLabelMap[acc.id] = `${acc.market_name}(${acc.seller_id || acc.account_label || acc.business_name || '-'})`
    }

    // 정책 적용된 상품만 전송 대상 (미적용 상품은 사전 제외)
    const policyProducts = visibleSelected.filter(pid => {
      const prod = productsById.get(pid)
      return !!prod?.applied_policy_id
    })
    const noPolicyCount = visibleSelected.length - policyProducts.length
    const total = policyProducts.length

    if (total === 0) {
      addLog(`[${ts()}] 전송 대상 없음 — 선택된 ${fmtNum(visibleSelected.length)}개 상품 중 정책 적용된 상품이 없습니다`)
      setTransmitting(false)
      return
    }

    if (noPolicyCount > 0) {
      addLog(`[${ts()}] 정책 미적용 ${fmtNum(noPolicyCount)}개 제외 (선택 ${fmtNum(visibleSelected.length)}개 → 전송 대상 ${fmtNum(total)}개)`)
    }

    setProgress({ current: 0, total })

    // 정책 연결 계정 ∩ UI 선택 마켓 = 실제 전송 대상
    const selectedSet = new Set(selectedAccounts)
    const effectiveAccountSet = new Set<string>()
    for (const pid of policyProducts) {
      const prod = productsById.get(pid)
      const policy = prod?.applied_policy_id ? policiesById.get(prod.applied_policy_id) : undefined
      if (!policy?.market_policies || typeof policy.market_policies !== 'object') continue
      const mp = policy.market_policies as Record<string, { accountId?: string; accountIds?: string[] }>
      for (const marketPolicy of Object.values(mp)) {
        // accountIds 배열이 존재하면 그것만 사용 (빈 배열 = 연결 없음), 없으면 레거시 accountId 폴백
        const ids = Array.isArray(marketPolicy.accountIds)
          ? marketPolicy.accountIds
          : (marketPolicy.accountId ? [marketPolicy.accountId] : [])
        ids.forEach((id: string) => { if (selectedSet.has(id)) effectiveAccountSet.add(id) })
      }
    }
    const effectiveLabels = [...effectiveAccountSet].map(aid => accountLabelMap[aid] || aid)
    abortRef.current = false
    addLog(`[${ts()}] 전송 시작 — 상품 ${fmtNum(total)}개, ${effectiveLabels.length > 0 ? effectiveLabels.join(', ') : '연결 계정 없음'}`)

    const items: string[] = []
    if (updateItems.price) items.push('price', 'stock')
    if (updateItems.thumb) items.push('image')
    if (updateItems.detail) items.push('description')

    // 전송 태스크 사전 준비
    type TransmitTask = { idx: number, pid: string, prodLabel: string, targetAccIds: string[] }
    const tasks: TransmitTask[] = []
    let skipCount = 0
    for (let i = 0; i < policyProducts.length; i++) {
      const pid = policyProducts[i]
      const prod = productsById.get(pid)
      const prodName = prod?.name || pid
      const policy = prod?.applied_policy_id ? policiesById.get(prod.applied_policy_id) : undefined
      const targetAccIds: string[] = []
      if (policy?.market_policies && typeof policy.market_policies === 'object') {
        const mp = policy.market_policies as Record<string, { accountId?: string; accountIds?: string[] }>
        for (const marketPolicy of Object.values(mp)) {
          // accountIds 배열이 존재하면 그것만 사용 (빈 배열 = 연결 없음), 없으면 레거시 accountId 폴백
          const ids = Array.isArray(marketPolicy.accountIds)
            ? marketPolicy.accountIds
            : (marketPolicy.accountId ? [marketPolicy.accountId] : [])
          ids.forEach((id: string) => { if (selectedSet.has(id) && !targetAccIds.includes(id)) targetAccIds.push(id) })
        }
      }
      if (targetAccIds.length === 0) { skipCount++; continue }
      const siteProductId = prod?.site_product_id || ''
      const prodLabel = siteProductId ? `${prodName} (${siteProductId})` : prodName
      tasks.push({ idx: i + 1, pid, prodLabel, targetAccIds })
    }

    if (skipCount > 0) {
      addLog(`[${ts()}] 선택 마켓 미연결 ${fmtNum(skipCount)}개 스킵 → 실제 전송 ${fmtNum(tasks.length)}개`)
    }
    setProgress({ current: 0, total: tasks.length })

    // 테트리스 배치 조회 — 브랜드당 마켓별로 여러 계정 누적 (Map.set 덮어쓰기 금지)
    const tetrisMap: Map<string, string[]> = new Map()
    try {
      const tetrisAssignments = await tetrisApi.listAssignments()
      for (const a of tetrisAssignments) {
        const key = `${a.source_site}:${a.brand_name}`
        const arr = tetrisMap.get(key) || []
        arr.push(a.market_account_id)
        tetrisMap.set(key, arr)
      }
    } catch { /* 조회 실패 시 기존 정책 로직으로 폴백 */ }

    // 테트리스 배치가 있으면 마켓별로 정책 계정을 테트리스 계정으로 교체, 미배정 마켓은 정책 계정 유지
    let hasTetrisOverride = false
    const tetrisTasks = tasks.map(task => {
      const prod = productsById.get(task.pid)
      if (!prod) return task
      const tetrisAccIds = tetrisMap.get(`${prod.source_site}:${prod.brand}`) || []
      const applicable = tetrisAccIds.filter(aid => selectedSet.has(aid) && accountsById.get(aid))
      if (applicable.length === 0) return task
      const overrideMarkets = new Set(applicable.map(aid => accountsById.get(aid)!.market_type))
      const keptPolicy = task.targetAccIds.filter(aid => {
        const m = accountsById.get(aid)?.market_type
        return m && !overrideMarkets.has(m)
      })
      const newAccIds = [...new Set([...keptPolicy, ...applicable])]
      if (newAccIds.join(',') !== task.targetAccIds.join(',')) hasTetrisOverride = true
      return { ...task, targetAccIds: newAccIds }
    })

    // Job 큐로 백그라운드 전송 (건수 무관)
    try {
      const allPids = tetrisTasks.map(t => t.pid)
      // 테트리스 오버라이드 포함 시 모든 실제 대상 계정 ID 수집
      const finalAccIds = hasTetrisOverride
        ? [...new Set(tetrisTasks.flatMap(t => t.targetAccIds))]
        : [...effectiveAccountSet]
      const allAccIds = finalAccIds
      const jobPayload = {
        job_type: 'transmit',
        payload: {
          product_ids: allPids,
          update_items: items,
          target_account_ids: allAccIds,
          skip_unchanged: skipEnabled,
          skip_policy_account_filter: hasTetrisOverride,
        },
      }
      setPausedJobPayload(jobPayload)
      const { API_BASE_URL: apiBase } = await import('@/config/api')
      const res = await fetchWithAuth(`${apiBase}/api/v1/samba/jobs`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(jobPayload),
      })
      const jobData = await res.json()
      const jobId = jobData.id || ''
      activeJobIdRef.current = jobId
      // 중복 Job 반환 시 알림 (이미 진행 중인 잡을 추적)
      if (jobData.duplicate) {
        addLog(`[${ts()}] 이미 진행 중인 전송이 있어 해당 작업을 계속 추적합니다`)
      }
      setProgress({ current: jobData.current || 0, total: jobData.total || tasks.length })
      // 전송 진행 폴링 + 링 버퍼 기반 실시간 로그
      let polling = false
      if (jobPollRef.current) clearInterval(jobPollRef.current)
      jobPollRef.current = setInterval(async () => {
        if (polling) return
        polling = true
        try {
          const [jr, lr] = await Promise.all([
            fetchWithAuth(`${apiBase}/api/v1/samba/jobs/${jobId}`),
            fetchWithAuth(`${apiBase}/api/v1/samba/jobs/shipment-logs?since_idx=${sinceIdxRef.current}`),
          ])
          const j = await jr.json()
          const logData = await lr.json()
          const cur = j.current || 0
          const tot = j.total || tasks.length
          setProgress({ current: cur, total: tot })
          const newLogs = (logData.logs || []) as string[]
          sinceIdxRef.current = logData.current_idx || sinceIdxRef.current
          appendShipmentLogs(setLogMessages, newLogs)
          if (j.status === 'completed' || j.status === 'failed' || j.status === 'cancelled') {
            if (jobPollRef.current) { clearInterval(jobPollRef.current); jobPollRef.current = null }
            const _ts = fmtTime()
            if (j.error) addLog(`[${_ts}] ${j.error}`)
            // Job 결과를 프론트 로그에 직접 표시 (링 버퍼 인스턴스 격리 시 누락 방지)
            const r = (j.result || {}) as Record<string, number>
            const statusLabel = j.status === 'completed' ? '전송 완료' : j.status === 'failed' ? '일시정지(이어하기 가능)' : '전송 중단'
            appendShipmentLog(setLogMessages, `[${_ts}] ${statusLabel} — 성공 ${fmtNum(r.success || 0)}건, 스킵 ${fmtNum(r.skipped || 0)}건, 실패 ${fmtNum(r.failed || 0)}건`)
            // FAILED는 일시정지 = 재개 가능 → pausedJobPayload 유지
            if (j.status !== 'failed') setPausedJobPayload(null)
            setTransmitting(false)
            activeJobIdRef.current = ''
            load()
          }
        } catch { /* ignore */ }
        polling = false
      }, JOB_POLL_INTERVAL_MS)
    } catch (e) {
      addLog(`[${ts()}] 전송 실패: ${e instanceof Error ? e.message : '오류'}`)
      setTransmitting(false)
    }
  }

  // 일시정지된 전송을 이어하기
  const handleResume = async () => {
    if (!pausedJobPayload) return
    // 비상정지 해제
    try {
      const { API_BASE_URL: apiBase } = await import('@/config/api')
      await fetchWithAuth(`${apiBase}/api/v1/samba/shipments/emergency-clear`, { method: 'POST' })
    } catch { /* ignore */ }

    setTransmitting(true)
    abortRef.current = false
    const ts = fmtTime
    const addLog = (msg: string) => appendShipmentLog(setLogMessages, msg)

    try {
      const { API_BASE_URL: apiBase } = await import('@/config/api')
      const res = await fetchWithAuth(`${apiBase}/api/v1/samba/jobs`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(pausedJobPayload),
      })
      const jobData = await res.json()
      const jobId = jobData.id || ''
      activeJobIdRef.current = jobId
      const resumedFrom = jobData.resumed_from || 0
      const pl = pausedJobPayload as { payload?: { product_ids?: string[] } }
      const total = pl.payload?.product_ids?.length || 0
      setProgress({ current: resumedFrom, total })
      addLog(`[${ts()}] 이어하기 — ${fmtNum(resumedFrom)}/${fmtNum(total)}부터 재개`)

      // 전송 진행 폴링 + 링 버퍼 기반 실시간 로그
      let polling = false
      if (jobPollRef.current) clearInterval(jobPollRef.current)
      jobPollRef.current = setInterval(async () => {
        if (polling) return
        polling = true
        try {
          const [jr, lr] = await Promise.all([
            fetchWithAuth(`${apiBase}/api/v1/samba/jobs/${jobId}`),
            fetchWithAuth(`${apiBase}/api/v1/samba/jobs/shipment-logs?since_idx=${sinceIdxRef.current}`),
          ])
          const j = await jr.json()
          const logData = await lr.json()
          const cur = j.current || 0
          const tot = j.total || total
          setProgress({ current: cur, total: tot })
          const newLogs = (logData.logs || []) as string[]
          sinceIdxRef.current = logData.current_idx || sinceIdxRef.current
          appendShipmentLogs(setLogMessages, newLogs)
          if (j.status === 'completed' || j.status === 'failed' || j.status === 'cancelled') {
            if (jobPollRef.current) { clearInterval(jobPollRef.current); jobPollRef.current = null }
            const _ts = fmtTime()
            if (j.error) addLog(`[${_ts}] ${j.error}`)
            const r = (j.result || {}) as Record<string, number>
            const statusLabel = j.status === 'completed' ? '전송 완료' : j.status === 'failed' ? '일시정지(이어하기 가능)' : '작업중지됨'
            appendShipmentLog(setLogMessages, `[${_ts}] ${statusLabel} — 성공 ${fmtNum(r.success || 0)}건, 스킵 ${fmtNum(r.skipped || 0)}건, 실패 ${fmtNum(r.failed || 0)}건`)
            // FAILED는 일시정지 = 재개 가능 → pausedJobPayload 유지
            if (j.status !== 'failed') setPausedJobPayload(null)
            setTransmitting(false)
            activeJobIdRef.current = ''
            load()
          }
        } catch { /* ignore */ }
        polling = false
      }, JOB_POLL_INTERVAL_MS)
    } catch (e) {
      addLog(`[${ts()}] 이어하기 실패: ${e instanceof Error ? e.message : '오류'}`)
      setTransmitting(false)
    }
  }

  // handleStart가 항상 최신 클로저를 참조하도록 ref 갱신
  handleStartRef.current = handleStart

  // 개별 잡 취소 — 진행 중이거나 대기 중인 잡 하나만 CANCELLED 처리
  const handleCancelSingleJob = async (jobId: string, label: string) => {
    if (!jobId) return
    const ok = await showConfirm(`[${label}] 잡 1건을 취소합니다. 계속할까요?`)
    if (!ok) return
    // 로컬(프론트엔드 루프) 마켓삭제 잡은 백엔드 Job이 아니므로 인메모리 플래그로 중단
    if (jobId.startsWith('local-delete-')) {
      cancelLocalDeleteIdsRef.current.add(jobId)
      setCancellingJobIds(prev => prev.includes(jobId) ? prev : [...prev, jobId])
      const tsLocal = fmtTime()
      appendShipmentLog(setLogMessages, `[${tsLocal}] 마켓삭제 중지 요청 — ${label}`)
      // UI 즉시 제거 (다음 루프 반복 시 실제 break)
      setLocalDeleteJobs(prev => prev.filter(j => j.id !== jobId))
      setCancellingJobIds(prev => prev.filter(id => id !== jobId))
      return
    }
    setCancellingJobIds(prev => prev.includes(jobId) ? prev : [...prev, jobId])
    const ts = fmtTime()
    const log = (msg: string) => appendShipmentLog(setLogMessages, msg)
    try {
      const { API_BASE_URL: apiBase } = await import('@/config/api')
      const res = await fetchWithAuth(`${apiBase}/api/v1/samba/jobs/${jobId}`, { method: 'DELETE' })
      if (!res.ok) {
        const msg = await res.text().catch(() => '')
        log(`[${ts}] 잡 취소 실패 (${label}) — ${msg || res.status}`)
        return
      }
      // 낙관적 업데이트 — 서버 폴링이 반영될 때까지 미리 목록에서 제거
      setJobQueueStatus(prev => ({
        running: prev.running.filter(j => j.id !== jobId),
        pending: prev.pending.filter(j => j.id !== jobId),
      }))
      log(`[${ts}] 잡 취소 완료 — ${label}`)
      // 현재 활성 잡이면 로컬 상태도 정리
      if (activeJobIdRef.current === jobId) {
        activeJobIdRef.current = ''
        if (jobPollRef.current) { clearInterval(jobPollRef.current); jobPollRef.current = null }
        setTransmitting(false)
      }
    } catch (e) {
      log(`[${ts}] 잡 취소 오류 — ${e instanceof Error ? e.message : '오류'}`)
    } finally {
      setCancellingJobIds(prev => prev.filter(id => id !== jobId))
    }
  }

  return (
    <div style={{ color: '#E5E5E5' }}>
      {/* 이전 단계 연결 */}
      <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '0.5rem', marginBottom: '0.5rem' }}>
        <a href="/samba/policies" style={{ fontSize: '0.75rem', color: '#888', textDecoration: 'none' }}>← 정책관리</a>
        <a href="/samba/categories" style={{ fontSize: '0.75rem', color: '#888', textDecoration: 'none' }}>← 카테고리매핑</a>
        <a href="/samba/products" style={{ fontSize: '0.75rem', color: '#888', textDecoration: 'none' }}>← 상품관리</a>
      </div>

      {/* 전송 설정 패널 */}
      <div style={{ background: 'rgba(14,14,20,0.98)', border: '1px solid #1E2030', borderRadius: '8px', marginBottom: '10px', fontSize: '0.8rem' }}>
        {/* 검색항목 */}
        <div style={{ display: 'flex', alignItems: 'center', padding: '8px 16px', borderBottom: '1px solid #181C28', gap: '8px' }}>
          <span style={{ minWidth: '72px', color: '#666', fontSize: '0.78rem' }}>검색항목</span>
          <select value={searchField} onChange={e => setSearchField(e.target.value)} style={{ ...inputStyle, width: '100px' }}>
            <option value="name">검색항목</option>
            <option value="brand">브랜드</option>
            <option value="name_all">상품명</option>
            <option value="group">그룹</option>
            <option value="no">상품번호</option>
            <option value="policy">정책</option>
          </select>
          <input type="text" value={searchText} onChange={e => setSearchText(e.target.value)} onKeyDown={e => { if (e.key === 'Enter') handleSearch() }} placeholder={searchField === 'name' ? '상품명 검색' : searchField === 'no' ? '상품번호 검색 (콤마로 다중)' : '그룹명 검색'} style={{ ...inputStyle, width: '200px' }} />
        </div>
        {/* 소싱사이트 필터 */}
        <div style={{ display: 'flex', alignItems: 'center', padding: '8px 16px', borderBottom: '1px solid #181C28', gap: '8px' }}>
          <span style={{ minWidth: '72px', color: '#666', fontSize: '0.78rem' }}>소싱사이트</span>
          <select value={siteFilter} onChange={e => setSiteFilter(e.target.value)} style={{ ...inputStyle, width: '140px' }}>
            {SOURCE_SITES.map(s => <option key={s} value={s}>{s}</option>)}
          </select>
        </div>
        {/* 품절여부 */}
        <div style={{ display: 'flex', alignItems: 'center', padding: '8px 16px', borderBottom: '1px solid #181C28', gap: '8px' }}>
          <span style={{ minWidth: '72px', color: '#666', fontSize: '0.78rem' }}>품절여부</span>
          <select value={soldOutFilter} onChange={e => setSoldOutFilter(e.target.value)} style={{ ...inputStyle, width: '140px' }}>
            <option value="전체">전체</option>
            <option value="품절">품절</option>
            <option value="비품절">비품절</option>
          </select>
        </div>
        {/* 마켓등록 */}
        <div style={{ display: 'flex', alignItems: 'center', padding: '8px 16px', borderBottom: '1px solid #181C28', gap: '8px' }}>
          <span style={{ minWidth: '72px', color: '#666', fontSize: '0.78rem' }}>마켓등록</span>
          <select value={registrationFilter} onChange={e => setRegistrationFilter(e.target.value)} style={{ ...inputStyle, width: '180px' }}>
            <option value="전체">전체</option>
            <optgroup label="── 전체 ──">
              <option value="미등록">미등록상품</option>
              <option value="등록">등록상품</option>
            </optgroup>
            {(() => {
              const marketTypes = [...new Map(accounts.map(a => [a.market_type, a.market_name] as const)).entries()]
              return marketTypes.length > 0 ? (
                <optgroup label="── 마켓구분 ──">
                  {marketTypes.map(([type, name]) => (
                    <React.Fragment key={type}>
                      <option value={`mtype_reg_${type}`}>{name} 등록</option>
                      <option value={`mtype_unreg_${type}`}>{name} 미등록</option>
                    </React.Fragment>
                  ))}
                </optgroup>
              ) : null
            })()}
            {accounts.length > 0 && (
              <optgroup label="── 계정구분 ──">
                {accounts.map(a => (
                  <React.Fragment key={a.id}>
                    <option value={`reg_${a.id}`}>{a.market_name}({a.account_label}) 등록</option>
                    <option value={`unreg_${a.id}`}>{a.market_name}({a.account_label}) 미등록</option>
                  </React.Fragment>
                ))}
              </optgroup>
            )}
          </select>
        </div>
        {/* 검색하기 버튼 */}
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '10px 16px', borderBottom: '2px solid #181C28', flexWrap: 'wrap', gap: '8px' }}>
          <div style={{ display: 'flex', gap: '8px' }}>
            <button onClick={handleSearch} style={{ padding: '6px 40px', fontSize: '0.875rem', fontWeight: 700, background: '#2A2F3E', border: '1px solid #3D4560', color: '#E5E5E5', borderRadius: '6px', cursor: 'pointer' }}>검색하기</button>
            <button onClick={handleResetSearch} style={{ padding: '6px 24px', fontSize: '0.875rem', background: 'transparent', border: '1px solid #2A3040', color: '#9AA5C0', borderRadius: '6px', cursor: 'pointer' }}>초기화</button>
          </div>
        </div>

        {/* 소싱사이트 체크박스 */}
        <div style={{ padding: '10px 16px 12px', borderBottom: '1px solid #181C28' }}>
          <div style={{ fontSize: '0.85rem', fontWeight: 600, color: '#C5C5C5', marginBottom: '10px', paddingBottom: '6px', borderBottom: '1px solid #1C1E2A' }}>소싱사이트</div>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: '8px 24px' }}>
            <label style={{ display: 'inline-flex', alignItems: 'center', gap: '5px', fontSize: '0.8rem', color: '#C5C5C5', cursor: 'pointer', fontWeight: 600 }}>
              <input type="checkbox" checked={selectedSites.length === allSites.length} onChange={toggleAllSites} style={{ accentColor: '#FF8C00', width: '14px', height: '14px' }} />
              전체
            </label>
            {allSites.map(site => (
              <label key={site} style={{ display: 'inline-flex', alignItems: 'center', gap: '5px', fontSize: '0.8rem', color: '#8A95B0', cursor: 'pointer' }}>
                <input type="checkbox" checked={selectedSites.includes(site)} onChange={() => toggleSite(site)} style={{ accentColor: '#FF8C00', width: '14px', height: '14px' }} />
                {site}
              </label>
            ))}
          </div>
        </div>

        {/* 마켓 체크박스 (마켓별 통합 — 선택 시 해당 마켓의 모든 계정에 전송) */}
        <div style={{ padding: '10px 16px 12px' }}>
          <div style={{ fontSize: '0.85rem', fontWeight: 600, color: '#C5C5C5', marginBottom: '8px', paddingBottom: '6px', borderBottom: '1px solid #1C1E2A' }}>마켓</div>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px 20px' }}>
            {accounts.length === 0 ? (
              <span style={{ color: '#555', fontSize: '0.8rem' }}>설정 탭에서 마켓을 등록해주세요</span>
            ) : (
              <>
                <label style={{ display: 'inline-flex', alignItems: 'center', gap: '5px', fontSize: '0.8rem', color: '#FF8C00', cursor: 'pointer', fontWeight: 600 }}>
                  <input type="checkbox"
                    checked={(() => {
                      const allTypes = [...new Set(accounts.map(a => a.market_type))]
                      return allTypes.length > 0 && allTypes.every(t => selectedMarkets.includes(t))
                    })()}
                    onChange={() => {
                      const allTypes = [...new Set(accounts.map(a => a.market_type))]
                      const allSelected = allTypes.every(t => selectedMarkets.includes(t))
                      if (allSelected) {
                        setSelectedMarkets([])
                        setSelectedAccounts([])
                      } else {
                        setSelectedMarkets(allTypes)
                        setSelectedAccounts(getAccountIdsByMarkets(allTypes))
                      }
                    }}
                    style={{ accentColor: '#FF8C00', width: '14px', height: '14px' }} />
                  전체
                </label>
                {[...new Map(accounts.map(a => [a.market_type, a.market_name])).entries()].map(([type, name]) => (
                  <label key={type} style={{ display: 'inline-flex', alignItems: 'center', gap: '5px', fontSize: '0.8rem', color: '#8A95B0', cursor: 'pointer' }}>
                    <input type="checkbox" checked={selectedMarkets.includes(type)}
                      onChange={() => {
                        const next = selectedMarkets.includes(type) ? selectedMarkets.filter(m => m !== type) : [...selectedMarkets, type]
                        setSelectedMarkets(next)
                        setSelectedAccounts(getAccountIdsByMarkets(next))
                      }}
                      style={{ accentColor: '#FF8C00', width: '14px', height: '14px' }} />
                    {name}
                  </label>
                ))}
              </>
            )}
          </div>
        </div>
      </div>

      {/* 진행 중인 전송/삭제 Job 요약 */}
      {(jobQueueStatus.running.length > 0 || jobQueueStatus.pending.length > 0 || localDeleteJobs.length > 0) && (() => {
        const runningAll: JobQueueItem[] = [
          ...jobQueueStatus.running.map(j => ({ ...j, kind: (j.kind || 'transmit') as 'transmit' | 'delete' })),
          ...localDeleteJobs,
        ]
        const sortedPending = [...jobQueueStatus.pending].sort((a, b) => {
          // 1) 마켓·계정명 그룹핑
          const byMarket = (a.markets || '').localeCompare(b.markets || '')
          if (byMarket !== 0) return byMarket
          // 2) 같은 계정 내에서는 상품 수 적은 잡 먼저 (백엔드 픽업 순서와 일치)
          const byCount = (a.product_count || 0) - (b.product_count || 0)
          if (byCount !== 0) return byCount
          return (a.id || '').localeCompare(b.id || '')
        })
        const transmitCount = runningAll.filter(j => j.kind !== 'delete').length
        const deleteCount = runningAll.filter(j => j.kind === 'delete').length
        return (
          <div style={{ background: 'rgba(8,10,16,0.98)', border: '1px solid #1C1E2A', borderRadius: '8px', marginBottom: '8px', overflow: 'visible' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px', padding: '8px 14px', background: '#0A0D14', borderBottom: '1px solid #1C1E2A' }}>
              <span style={{ width: '6px', height: '6px', borderRadius: '50%',
                background: runningAll.length > 0 ? '#51CF66' : '#FAB005' }} />
              <span style={{ fontSize: '0.82rem', fontWeight: 600, color: '#9AA5C0' }}>
                Job {'진행상황'}
                {transmitCount > 0 && ` — 전송 중 ${fmtNum(transmitCount)}건`}
                {deleteCount > 0 && `${transmitCount > 0 ? ' · ' : ' — '}삭제 중 ${fmtNum(deleteCount)}건`}
                {jobQueueStatus.pending.length > 0 && ` · 대기 ${fmtNum(jobQueueStatus.pending.length)}건`}
              </span>
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '4px', padding: '8px 14px' }}>
              {runningAll.map((j, idx) => {
                const started = j.started_at ? new Date(j.started_at) : null
                const startedStr = started
                  ? `${String(started.getHours()).padStart(2, '0')}:${String(started.getMinutes()).padStart(2, '0')}:${String(started.getSeconds()).padStart(2, '0')}`
                  : '-'
                const pct = j.total > 0 ? Math.floor((j.current / j.total) * 100) : 0
                const elapsedMs = started ? Math.max(0, Date.now() - started.getTime()) : 0
                // 백엔드 최근 윈도우 기준 건당 처리시간 우선 사용 — 누적평균(시작이후 전체 elapsed/current)은 초기 오버헤드 때문에 부풀려짐
                const perItemSec = (typeof j.per_item_sec === 'number' && j.per_item_sec > 0)
                  ? j.per_item_sec
                  : (j.current > 0 && elapsedMs > 0 ? elapsedMs / 1000 / j.current : 0)
                const perItemStr = perItemSec > 0
                  ? (perItemSec >= 10 ? `${fmtNum(Math.round(perItemSec))}초/1건` : `${perItemSec.toFixed(1)}초/1건`)
                  : '—'
                const busy = !!(j.id && cancellingJobIds.includes(j.id))
                const sites = j.source_sites ?? []
                const brands = j.brands ?? []
                const sitesStr = sites.length > 0 ? (sites.length <= 3 ? sites.join(' · ') : `${sites.slice(0, 3).join(' · ')} 외 ${fmtNum(sites.length - 3)}`) : ''
                const brandsStr = brands.length > 0 ? (brands.length <= 3 ? brands.join(' · ') : `${brands.slice(0, 3).join(' · ')} 외 ${fmtNum(brands.length - 3)}`) : ''
                return (
                  <div key={`r-${j.id || idx}`} style={{ display: 'flex', alignItems: 'center', gap: '10px', fontSize: '0.75rem', color: '#C4CAD8', borderBottom: '1px solid #151822', paddingBottom: '4px', marginBottom: '0' }}>
                    <span style={{ color: j.kind === 'delete' ? '#FF6B6B' : '#51CF66', fontWeight: 600, minWidth: '40px' }}>
                      {j.kind === 'delete' ? '삭제중' : '전송중'}
                    </span>
                    <span style={{ color: '#8A95B0', minWidth: '92px', flexShrink: 0 }}>{'시작'} {startedStr}</span>
                    <span style={{ color: '#C4CAD8', whiteSpace: 'nowrap', flexShrink: 0, overflow: 'visible' }} title={j.markets}>
                      {j.markets}
                    </span>
                    <span style={{ flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={[sitesStr, brandsStr].filter(Boolean).join(' / ')}>
                      {sitesStr && <span style={{ color: '#7BB0FF' }}>{sitesStr}</span>}
                      {sitesStr && brandsStr && <span style={{ color: '#3A4258' }}>{' · '}</span>}
                      {brandsStr && <span style={{ color: '#A78BFA' }}>{brandsStr}</span>}
                    </span>
                    <span style={{ color: '#9AA5C0', minWidth: '92px', textAlign: 'right', flexShrink: 0 }}>
                      {fmtNum(j.current)} / {fmtNum(j.total)} ({pct}%)
                    </span>
                    <span style={{ color: '#6E7A95', minWidth: '56px', textAlign: 'right', fontSize: '0.7rem', flexShrink: 0 }}>
                      {perItemStr}
                    </span>
                    <button
                      onClick={() => j.id && handleCancelSingleJob(j.id, j.markets)}
                      disabled={!j.id || busy}
                      title={'이 잡만 취소'}
                      style={{ padding: '2px 8px', fontSize: '0.7rem', background: busy ? 'rgba(255,80,80,0.3)' : 'rgba(255,80,80,0.12)', color: '#FF6B6B', border: '1px solid rgba(255,80,80,0.4)', borderRadius: '3px', cursor: (!j.id || busy) ? 'not-allowed' : 'pointer', fontWeight: 600, minWidth: '40px', flexShrink: 0 }}
                    >{busy ? '취소중' : '취소'}</button>
                  </div>
                )
              })}
              {sortedPending.map((j, idx) => {
                const busy = !!(j.id && cancellingJobIds.includes(j.id))
                const sites = j.source_sites ?? []
                const brands = j.brands ?? []
                const sitesStr = sites.length > 0 ? (sites.length <= 3 ? sites.join(' · ') : `${sites.slice(0, 3).join(' · ')} 외 ${fmtNum(sites.length - 3)}`) : ''
                const brandsStr = brands.length > 0 ? (brands.length <= 3 ? brands.join(' · ') : `${brands.slice(0, 3).join(' · ')} 외 ${fmtNum(brands.length - 3)}`) : ''
                return (
                  <div key={`p-${j.id || idx}`} style={{ display: 'flex', alignItems: 'center', gap: '10px', fontSize: '0.75rem', color: '#8A95B0', borderBottom: '1px solid #151822', paddingBottom: '4px', marginBottom: '0' }}>
                    <span style={{ color: '#FAB005', fontWeight: 600, minWidth: '40px' }}>{'대기'}</span>
                    <span style={{ minWidth: '92px', flexShrink: 0 }}>{'—'}</span>
                    <span style={{ color: '#C4CAD8', whiteSpace: 'nowrap', flexShrink: 0, overflow: 'visible' }} title={j.markets}>
                      {j.markets}
                    </span>
                    <span style={{ flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={[sitesStr, brandsStr].filter(Boolean).join(' / ')}>
                      {sitesStr && <span style={{ color: '#7BB0FF' }}>{sitesStr}</span>}
                      {sitesStr && brandsStr && <span style={{ color: '#3A4258' }}>{' · '}</span>}
                      {brandsStr && <span style={{ color: '#A78BFA' }}>{brandsStr}</span>}
                    </span>
                    <span style={{ minWidth: '110px', textAlign: 'right', flexShrink: 0 }}>{fmtNum(j.product_count)}{'건'}</span>
                    <button
                      onClick={() => j.id && handleCancelSingleJob(j.id, j.markets)}
                      disabled={!j.id || busy}
                      title={'이 잡만 취소'}
                      style={{ padding: '2px 8px', fontSize: '0.7rem', background: busy ? 'rgba(255,80,80,0.3)' : 'rgba(255,80,80,0.12)', color: '#FF6B6B', border: '1px solid rgba(255,80,80,0.4)', borderRadius: '3px', cursor: (!j.id || busy) ? 'not-allowed' : 'pointer', fontWeight: 600, minWidth: '40px', flexShrink: 0 }}
                    >{busy ? '취소중' : '취소'}</button>
                  </div>
                )
              })}
            </div>
          </div>
        )
      })()}

      {/* 전송 로그 */}
      <div style={{ background: 'rgba(8,10,16,0.98)', border: '1px solid #1C1E2A', borderRadius: '8px', marginBottom: '12px', overflow: 'hidden' }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '8px 14px', background: '#0A0D14', borderBottom: '1px solid #1C1E2A' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
            <span style={{ fontSize: '0.82rem', fontWeight: 600, color: '#9AA5C0' }}>전송 로그</span>
          </div>
          <div style={{ display: 'flex', gap: '6px', alignItems: 'center' }}>
            <button onClick={() => navigator.clipboard.writeText(logMessages.join('\n'))} style={{ padding: '3px 10px', fontSize: '0.72rem', background: 'transparent', border: '1px solid #252B3B', color: '#666', borderRadius: '4px', cursor: 'pointer' }}>복사</button>
            <button onClick={async () => {
              setLogMessages(['로그가 초기화되었습니다.'])
              sinceIdxRef.current = 0
              try {
                const { API_BASE_URL: apiBase } = await import('@/config/api')
                await fetchWithAuth(`${apiBase}/api/v1/samba/jobs/shipment-logs/clear`, { method: 'POST' })
              } catch { /* ignore */ }
            }} style={{ padding: '3px 10px', fontSize: '0.72rem', background: 'transparent', border: '1px solid #252B3B', color: '#666', borderRadius: '4px', cursor: 'pointer' }}>초기화</button>
            <button onClick={handleMarketDelete}
              style={{ padding: '4px 14px', fontSize: '0.78rem', background: 'rgba(255,107,107,0.12)', border: '1px solid rgba(255,107,107,0.35)', color: '#FF6B6B', borderRadius: '4px', cursor: deleting ? 'not-allowed' : 'pointer', fontWeight: 600 }}>마켓삭제</button>
            <button disabled={!!stopping} onClick={async () => {
                setStopping('cancel')
                const ts = fmtTime()
                setLogMessages(prev => [...prev, `[${ts}] 일시정지 요청...`].slice(-30))
                abortRef.current = true
                if (jobPollRef.current) { clearInterval(jobPollRef.current); jobPollRef.current = null }
                try {
                  const { API_BASE_URL: apiBase } = await import('@/config/api')
                  await fetchWithAuth(`${apiBase}/api/v1/samba/shipments/cancel`, { method: 'POST' })
                  activeJobIdRef.current = ''
                  setJobQueueStatus({ running: [], pending: [] })
                  setLogMessages(prev => [...prev, `[${ts}] 일시정지 완료 — 이어하기로 재개 가능`].slice(-30))
                  // 백엔드 워커가 잡 상태를 'failed'로 마킹할 때까지 잠깐 대기 후 페이로드 복원 시도
                  // (mount 폴링으로 진입한 세션은 pausedJobPayload가 비어있어 이어하기 버튼이 비활성화되는 문제 방지)
                  for (let attempt = 0; attempt < 10; attempt++) {
                    await new Promise(r => setTimeout(r, 500))
                    try {
                      const r2 = await fetchWithAuth(`${apiBase}/api/v1/samba/jobs/last-resumable-transmit`)
                      if (!r2.ok) continue
                      const data = await r2.json()
                      if (data && data.payload) {
                        setPausedJobPayload({
                          job_type: data.job_type || 'transmit',
                          payload: data.payload,
                        })
                        break
                      }
                    } catch { /* 다음 시도 */ }
                  }
                } catch {
                  setLogMessages(prev => [...prev, `[${ts}] 일시정지 실패`].slice(-30))
                }
                setTransmitting(false)
                setStopping('')
              }}
                style={{ padding: '4px 14px', fontSize: '0.78rem', background: stopping === 'cancel' ? 'rgba(255,180,0,0.4)' : 'rgba(255,180,0,0.15)', color: '#FFB800', border: '1px solid rgba(255,180,0,0.4)', borderRadius: '4px', cursor: stopping ? 'not-allowed' : 'pointer', fontWeight: 600, opacity: stopping ? 0.7 : 1 }}
              >{stopping === 'cancel' ? '일시정지중...' : '일시정지'}</button>
            <button disabled={!pausedJobPayload || transmitting || !!stopping} onClick={handleResume}
                style={{ padding: '4px 14px', fontSize: '0.78rem', background: pausedJobPayload && !transmitting && !stopping ? 'rgba(76,175,80,0.15)' : 'rgba(76,175,80,0.06)', color: pausedJobPayload && !transmitting && !stopping ? '#4CAF50' : '#4CAF5055', border: `1px solid ${pausedJobPayload && !transmitting && !stopping ? 'rgba(76,175,80,0.4)' : 'rgba(76,175,80,0.15)'}`, borderRadius: '4px', cursor: pausedJobPayload && !transmitting && !stopping ? 'pointer' : 'not-allowed', fontWeight: 600, opacity: pausedJobPayload && !transmitting && !stopping ? 1 : 0.5 }}
              >이어하기</button>
            <button disabled={!!stopping} onClick={async () => {
                setStopping('emergency')
                const ts = fmtTime()
                setLogMessages(prev => [...prev, `[${ts}] 작업중지 요청...`].slice(-30))
                abortRef.current = true
                if (jobPollRef.current) { clearInterval(jobPollRef.current); jobPollRef.current = null }
                try {
                  const { API_BASE_URL: apiBase } = await import('@/config/api')
                  await fetchWithAuth(`${apiBase}/api/v1/samba/shipments/cancel`, { method: 'POST' })
                  await fetchWithAuth(`${apiBase}/api/v1/samba/jobs/cancel-transmit`, { method: 'POST' })
                  activeJobIdRef.current = ''
                  setPausedJobPayload(null)
                  setJobQueueStatus({ running: [], pending: [] })
                  setLogMessages(prev => [...prev, `[${ts}] 작업중지 완료`].slice(-30))
                } catch {
                  setLogMessages(prev => [...prev, `[${ts}] 작업중지 실패`].slice(-30))
                }
                setTransmitting(false)
                setStopping('')
              }}
                style={{ padding: '4px 14px', fontSize: '0.78rem', background: stopping === 'emergency' ? 'rgba(255,50,50,0.6)' : 'rgba(255,50,50,0.3)', color: '#FF4444', border: '1px solid rgba(255,50,50,0.6)', borderRadius: '4px', cursor: stopping ? 'not-allowed' : 'pointer', fontWeight: 700, opacity: stopping ? 0.7 : 1 }}
              >{stopping === 'emergency' ? '취소중...' : '작업취소'}</button>
            {<>
              <button onClick={() => handleStart()}
                style={{ padding: '4px 14px', fontSize: '0.78rem', background: 'rgba(255,140,0,0.15)', color: '#FF8C00', border: '1px solid rgba(255,140,0,0.4)', borderRadius: '4px', cursor: 'pointer', fontWeight: 600 }}
              >선택전송</button>
              <button onClick={async () => {
                // 전체 상품 ID를 서버에서 조회 → Job에 직접 전달 (프론트 필터링 스킵)
                if (selectedAccounts.length === 0) { showAlert('마켓 계정을 선택해주세요'); return }
                if (selectedSites.length === 0) { showAlert('소싱사이트를 선택해주세요'); return }
                try {
                  const { API_BASE_URL: apiBase } = await import('@/config/api')
                  await fetchWithAuth(`${apiBase}/api/v1/samba/shipments/emergency-clear`, { method: 'POST' })
                } catch { /* ignore */ }
                const idParams: { search?: string; search_type?: string; source_site?: string; source_sites?: string; status?: string; sold_out_filter?: string } = {}
                if (appliedSearchText.trim()) {
                  idParams.search = appliedSearchText.trim()
                  const typeMap: Record<string, string> = { name: 'name', brand: 'brand', name_all: 'name_all', group: 'filter', no: 'no', policy: 'policy' }
                  idParams.search_type = typeMap[appliedSearchField] || 'name'
                }
                if (appliedSiteFilter !== '전체') {
                  idParams.source_site = appliedSiteFilter
                } else if (selectedSites.length > 0) {
                  idParams.source_sites = selectedSites.join(',')
                }
                if (appliedSoldOutFilter !== '전체') {
                  idParams.sold_out_filter = appliedSoldOutFilter === '품절' ? 'sold_out' : 'not_sold_out'
                }
                if (appliedRegistrationFilter !== '전체') {
                  if (appliedRegistrationFilter.startsWith('reg_') || appliedRegistrationFilter.startsWith('unreg_') || appliedRegistrationFilter.startsWith('mtype_')) {
                    idParams.status = appliedRegistrationFilter
                  } else {
                    idParams.status = appliedRegistrationFilter === '등록' ? 'market_registered' : appliedRegistrationFilter === '미등록' ? 'market_unregistered' : appliedRegistrationFilter === '품절' ? 'sold_out' : ''
                  }
                }
                try {
                  let allIds: string[]
                  const importedSelectedIds = importedSelectionRef.current.filter(id => selectedProducts.includes(id))
                  if (!userFilterChangedRef.current && importedSelectedIds.length > 0) {
                    allIds = products
                      .filter(p => importedSelectedIds.includes(p.id) && new Set(selectedSites).has(p.source_site))
                      .map(p => p.id)
                  } else {
                    // ID만 조회 (전체 상품 데이터 다운로드 없이 경량 요청)
                    const result = await collectorApi.getProductIds(idParams)
                    allIds = result.ids
                  }
                  if (allIds.length === 0) { showAlert('선택된 소싱사이트에 해당하는 상품이 없습니다'); return }
                  // Job 직접 생성
                  setTransmitting(true)
                  const ts = fmtTime
                  const addLog = (msg: string) => appendShipmentLog(setLogMessages, msg)
                  const items: string[] = []
                  if (updateItems.price) items.push('price', 'stock')
                  if (updateItems.thumb) items.push('image')
                  if (updateItems.detail) items.push('description')
                  // 로드된 상품의 정책 기반 계정 필터링 (선택전송과 동일 로직)
                  const selectedSet = new Set(selectedAccounts)
                  const effectiveAccIds = new Set<string>()
                  for (const prod of products) {
                    if (!prod.applied_policy_id) continue
                    const policy = policiesById.get(prod.applied_policy_id)
                    if (!policy?.market_policies || typeof policy.market_policies !== 'object') continue
                    const mp = policy.market_policies as Record<string, { accountId?: string; accountIds?: string[] }>
                    for (const marketPolicy of Object.values(mp)) {
                      const ids = Array.isArray(marketPolicy.accountIds)
                        ? marketPolicy.accountIds
                        : (marketPolicy.accountId ? [marketPolicy.accountId] : [])
                      ids.forEach((id: string) => { if (selectedSet.has(id)) effectiveAccIds.add(id) })
                    }
                  }
                  // 테트리스 배치 조회 — 배치 계정이 있으면 target_account_ids에 포함
                  const bulkTetrisMap: Map<string, string> = new Map()
                  let bulkHasTetris = false
                  try {
                    const tetrisAssignments = await tetrisApi.listAssignments()
                    for (const a of tetrisAssignments) {
                      bulkTetrisMap.set(`${a.source_site}:${a.brand_name}`, a.market_account_id)
                    }
                    bulkHasTetris = bulkTetrisMap.size > 0
                  } catch { /* 조회 실패 시 정책 로직으로 폴백 */ }

                  const baseAccList = effectiveAccIds.size > 0 ? [...effectiveAccIds] : [...selectedSet]
                  // 테트리스 배치 계정을 target_account_ids에 추가 (워커가 상품별로 올바른 계정 선택)
                  // 사용자가 UI에서 선택한 마켓 계정(selectedSet)으로 반드시 필터링 — 미필터링 시 선택 안 한 마켓까지 전송되는 버그 발생
                  const tetrisAccIds = bulkHasTetris
                    ? [...new Set(bulkTetrisMap.values())].filter(id => selectedSet.has(id))
                    : []
                  const effectiveAccList = [...new Set([...baseAccList, ...tetrisAccIds])]
                  const accLabels = effectiveAccList.map(aid => {
                    const acc = accountsById.get(aid)
                    return acc ? `${acc.market_name}(${acc.seller_id || '-'})` : aid
                  }).join(', ')
                  addLog(`[${ts()}] 전송 시작 — 상품 ${fmtNum(allIds.length)}개, ${accLabels || '연결 계정 없음'}`)
                  const { API_BASE_URL: apiBase } = await import('@/config/api')
                  const res = await fetchWithAuth(`${apiBase}/api/v1/samba/jobs`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                      job_type: 'transmit',
                      payload: { product_ids: allIds, update_items: items, target_account_ids: effectiveAccList, skip_unchanged: skipEnabled, skip_policy_account_filter: bulkHasTetris },
                    }),
                  })
                  const jobData = await res.json()
                  const jobId = jobData.id || ''
                  activeJobIdRef.current = jobId
                  setProgress({ current: 0, total: allIds.length })
                  let polling = false
                  if (jobPollRef.current) clearInterval(jobPollRef.current)
                  jobPollRef.current = setInterval(async () => {
                    if (polling) return
                    polling = true
                    try {
                      const [jr, lr] = await Promise.all([
                        fetchWithAuth(`${apiBase}/api/v1/samba/jobs/${jobId}`),
                        fetchWithAuth(`${apiBase}/api/v1/samba/jobs/shipment-logs?since_idx=${sinceIdxRef.current}`),
                      ])
                      const j = await jr.json()
                      const logData = await lr.json()
                      setProgress({ current: j.current || 0, total: j.total || allIds.length })
                      const newLogs = (logData.logs || []) as string[]
                      sinceIdxRef.current = logData.current_idx || sinceIdxRef.current
                      appendShipmentLogs(setLogMessages, newLogs)
                      if (j.status === 'completed' || j.status === 'failed') {
                        if (jobPollRef.current) { clearInterval(jobPollRef.current); jobPollRef.current = null }
                        // Job 결과를 프론트 로그에 직접 표시 (링 버퍼 인스턴스 격리 시 누락 방지)
                        const r = (j.result || {}) as Record<string, number>
                        const _ts = fmtTime()
                        const statusLabel = j.status === 'completed' ? '전송 완료' : j.status === 'failed' ? '전송 실패' : '전송 중단'
                        appendShipmentLog(setLogMessages, `[${_ts}] ${statusLabel} — 성공 ${fmtNum(r.success || 0)}건, 스킵 ${fmtNum(r.skipped || 0)}건, 실패 ${fmtNum(r.failed || 0)}건`)
                        setTransmitting(false)
                        activeJobIdRef.current = ''
                        load()
                      }
                    } catch { /* ignore */ }
                    polling = false
                  }, JOB_POLL_INTERVAL_MS)
                } catch (e) { showAlert(e instanceof Error ? e.message : '전송 실패', 'error') }
              }}
                style={{ padding: '4px 14px', fontSize: '0.78rem', background: 'linear-gradient(135deg,#FF8C00,#FFB84D)', color: '#fff', border: 'none', borderRadius: '4px', cursor: 'pointer', fontWeight: 600 }}
              >검색결과전송 ({fmtNum(totalCount)})</button>
              <button onClick={handleSearchDelete}
                style={{ padding: '4px 14px', fontSize: '0.78rem', background: 'rgba(255,107,107,0.12)', border: '1px solid rgba(255,107,107,0.35)', color: '#FF6B6B', borderRadius: '4px', cursor: deleting ? 'not-allowed' : 'pointer', fontWeight: 600 }}
              >검색결과삭제 ({fmtNum(totalCount)})</button>
            </>}
          </div>
        </div>
        <div
          ref={logContainerRef}
          onScroll={() => {
            const el = logContainerRef.current
            if (!el) return
            // 바닥에서 40px 이내면 "끝에 붙어있음"으로 간주 — 사용자가 위로 스크롤하면 자동 따라감 해제
            stickToBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40
          }}
          style={{ height: '250px', overflowY: 'auto', padding: '10px 14px', fontFamily: "'Courier New', monospace", fontSize: '0.73rem', lineHeight: 1.8, color: '#DCE0E8' }}
        >
          {logMessages.map((msg, i) => (
            <div key={i} style={{ color: '#DCE0E8' }}>{fmtTextNumbers(msg)}</div>
          ))}
        </div>
        {/* 프로그레스바 */}
        {/* 진행률 바 제거 — 멀티 잡 시 왔다갔다 문제 */}
      </div>

      {/* 상품 목록 테이블 */}
      <div style={{ background: 'rgba(30,30,30,0.5)', border: '1px solid #2D2D2D', borderRadius: '12px', overflow: 'hidden' }}>
        {/* 상단 탭 */}
        <div style={{ display: 'flex', alignItems: 'center', padding: '8px 16px', background: 'rgba(255,255,255,0.02)', borderBottom: '1px solid #2D2D2D', gap: '8px' }}>
          <span style={{ fontSize: '0.8rem', color: '#888' }}>총 <span style={{ color: '#FF8C00', fontWeight: 600 }}>{fmtNum(totalCount)}</span> 개의 상품이 검색되었습니다.</span>
          <select value={sortBy} onChange={e => { onFilterChange(); setSortBy(e.target.value) }} style={{ ...inputStyle, width: '250px', marginLeft: 'auto' }}>
            <option value="update-desc">상품업데이트 날짜순 ▼</option>
            <option value="update-asc">상품업데이트 날짜순 ▲</option>
            <option value="collect-desc">상품수집 날짜순 ▼</option>
            <option value="collect-asc">상품수집 날짜순 ▲</option>
            {registeredMarkets.flatMap(m => [
              <option key={`${m.type}_asc`} value={`market_${m.type}_asc`}>{m.name} ▲</option>,
              <option key={`${m.type}_desc`} value={`market_${m.type}_desc`}>{m.name} ▼</option>,
            ])}
          </select>
          <select value={pageSize} onChange={e => { setPageSize(Number(e.target.value)); setCurrentPage(1) }} style={{ ...inputStyle, width: '80px' }}>
            <option value={20}>20개</option>
            <option value={50}>50개</option>
            <option value={100}>100개</option>
            <option value={200}>200개</option>
            <option value={10000}>전체</option>
          </select>
        </div>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.8rem' }}>
          <thead>
            <tr style={{ background: 'rgba(255,255,255,0.03)', borderBottom: '1px solid #2D2D2D' }}>
              <th style={{ width: '36px', padding: '0.625rem' }}><input type="checkbox" checked={pageProducts.length > 0 && pageProducts.every(p => selectedProducts.includes(p.id))} onChange={toggleAllProducts} style={{ accentColor: '#F59E0B' }} /></th>
              <th style={{ padding: '0.625rem 0.5rem', textAlign: 'center', fontSize: '0.72rem', color: '#888', width: '40px' }}>No</th>
              <th style={{ padding: '0.625rem 0.5rem', textAlign: 'left', fontSize: '0.72rem', color: '#888' }}>상품번호</th>
              <th style={{ padding: '0.625rem 0.5rem', textAlign: 'left', fontSize: '0.72rem', color: '#888' }}>사이트</th>
              <th style={{ padding: '0.625rem 0.5rem', textAlign: 'left', fontSize: '0.72rem', color: '#888' }}>상품명</th>
              <th style={{ padding: '0.625rem 0.5rem', textAlign: 'center', fontSize: '0.72rem', color: '#888' }}>상품업데이트</th>
              <th style={{ padding: '0.625rem 0.5rem', textAlign: 'center', fontSize: '0.72rem', color: '#888' }}>마켓 전송</th>
            </tr>
          </thead>
          <tbody>
            {!hasSearchedRef.current ? (
              <tr><td colSpan={7} style={{ padding: '3rem', textAlign: 'center', color: '#555' }}>검색 조건을 입력하고 검색 버튼을 눌러주세요</td></tr>
            ) : loading ? (
              <tr><td colSpan={7} style={{ padding: '3rem', textAlign: 'center', color: '#555' }}>로딩 중...</td></tr>
            ) : products.length === 0 ? (
              <tr><td colSpan={7} style={{ padding: '3rem', textAlign: 'center', color: '#555' }}>상품이 없습니다</td></tr>
            ) : pageProducts.map((p, idx) => {
              const regAccounts = p.registered_accounts || []
              const regMarkets = regAccounts.map(aid => accountsById.get(aid)?.market_name).filter(Boolean)
              const optCount = (p.options || []).length
              return (
                <tr key={p.id} style={{ borderBottom: '1px solid rgba(45,45,45,0.5)', verticalAlign: 'top' }}
                  onMouseEnter={e => (e.currentTarget.style.background = 'rgba(255,255,255,0.02)')}
                  onMouseLeave={e => (e.currentTarget.style.background = 'transparent')}
                >
                  <td style={{ padding: '0.625rem 0.5rem', textAlign: 'center' }}>
                    <input type="checkbox" checked={selectedProducts.includes(p.id)} onChange={() => toggleProduct(p.id)} style={{ accentColor: '#F59E0B' }} />
                  </td>
                  <td style={{ padding: '0.625rem 0.5rem', textAlign: 'center', color: '#666', fontSize: '0.72rem' }}>{(currentPage - 1) * pageSize + idx + 1}</td>
                  <td style={{ padding: '0.625rem 0.5rem', color: '#666', fontSize: '0.72rem' }}>{p.site_product_id || '-'}</td>
                  <td style={{ padding: '0.625rem 0.5rem' }}>
                    <span style={{ display: 'inline-block', padding: '1px 8px', borderRadius: '4px', fontSize: '0.68rem', fontWeight: 600, color: SITE_COLORS[p.source_site] || '#888', background: `${SITE_COLORS[p.source_site] || '#888'}18`, border: `1px solid ${SITE_COLORS[p.source_site] || '#888'}40` }}>{p.source_site}</span>
                  </td>
                  <td style={{ padding: '0.625rem 0.5rem' }}>
                    <div>
                      <a href={`/samba/products?highlight=${p.id}`} style={{ color: '#DCE0E8', textDecoration: 'none', fontSize: '0.8rem', cursor: 'pointer' }}
                        onMouseEnter={e => (e.currentTarget.style.textDecoration = 'underline')}
                        onMouseLeave={e => (e.currentTarget.style.textDecoration = 'none')}
                      >
                        [{p.site_product_id || ''}] {p.brand ? <span style={{ color: '#A78BFA', fontWeight: 600 }}>[{p.brand}]</span> : ''}{p.brand ? ' ' : ''}{p.name} {optCount > 0 ? <span style={{ color: '#DCE0E8' }}>[옵션수:{fmtNum(optCount)}]</span> : ''}
                      </a>
                    </div>
                    {regMarkets.length > 0 && (
                      <div style={{ fontSize: '0.72rem', color: '#888', marginTop: '2px' }}>
                        (등록된 마켓 : {regMarkets.map((m, i) => (
                          <span key={i}><span style={{ color: '#FF8C00' }}>{m}</span>{i < regMarkets.length - 1 ? ' / ' : ''}</span>
                        ))})
                      </div>
                    )}
                  </td>
                  <td style={{ padding: '0.625rem 0.5rem', textAlign: 'center', fontSize: '0.72rem' }}>
                    {(() => {
                      const ts = p.last_refreshed_at || p.updated_at
                      if (!ts) return '-'
                      const d = new Date(ts)
                      return (
                        <span style={{ color: '#AAB0BC' }}>{d.getFullYear()}-{String(d.getMonth() + 1).padStart(2, '0')}-{String(d.getDate()).padStart(2, '0')} {String(d.getHours()).padStart(2, '0')}:{String(d.getMinutes()).padStart(2, '0')}:{String(d.getSeconds()).padStart(2, '0')}</span>
                      )
                    })()}
                  </td>
                  <td style={{ padding: '0.625rem 0.5rem', textAlign: 'center', fontSize: '0.72rem' }}>
                    {(() => {
                      const regAccs = (p.registered_accounts || [])
                      if (regAccs.length === 0) return <span style={{ color: '#555' }}>-</span>
                      const sent = p.last_sent_data || {}
                      return regAccs.map(aid => {
                        const acc = accountsById.get(aid)
                        if (!acc) return null
                        const sentAt = sent[aid]?.sent_at
                        const timeLabel = sentAt ? (() => {
                          const d = new Date(sentAt)
                          return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
                        })() : ''
                        return (
                          <div key={aid} style={{ marginBottom: '2px', fontSize: '0.68rem' }}>
                            <span style={{ color: '#51CF66' }}>{acc.market_name}({acc.seller_id || acc.account_label || '-'})</span>
                            {timeLabel && <span style={{ color: '#AAB0BC', marginLeft: '6px' }}>{timeLabel}</span>}
                          </div>
                        )
                      })
                    })()}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>

        {/* 페이지네이션 */}
        {totalCount > pageSize && (
          <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', gap: '6px', padding: '12px 0' }}>
            <button
              disabled={currentPage <= 1}
              onClick={() => setCurrentPage(p => Math.max(1, p - 1))}
              style={{ padding: '4px 10px', fontSize: '0.78rem', background: 'transparent', border: '1px solid #2D2D2D', borderRadius: '4px', color: currentPage <= 1 ? '#444' : '#C5C5C5', cursor: currentPage <= 1 ? 'default' : 'pointer' }}
            >◀</button>
            {Array.from({ length: Math.ceil(totalCount / pageSize) }, (_, i) => i + 1)
              .filter(page => Math.abs(page - currentPage) <= 2 || page === 1 || page === Math.ceil(totalCount / pageSize))
              .map((page, i, arr) => (
                <span key={page}>
                  {i > 0 && arr[i - 1] !== page - 1 && <span style={{ color: '#555' }}>…</span>}
                  <button
                    onClick={() => setCurrentPage(page)}
                    style={{
                      padding: '4px 10px', fontSize: '0.78rem', borderRadius: '4px', cursor: 'pointer',
                      background: page === currentPage ? 'rgba(255,140,0,0.2)' : 'transparent',
                      border: page === currentPage ? '1px solid #FF8C00' : '1px solid #2D2D2D',
                      color: page === currentPage ? '#FF8C00' : '#C5C5C5',
                      fontWeight: page === currentPage ? 600 : 400,
                    }}
                  >{page}</button>
                </span>
              ))}
            <button
              disabled={currentPage >= Math.ceil(totalCount / pageSize)}
              onClick={() => setCurrentPage(p => p + 1)}
              style={{ padding: '4px 10px', fontSize: '0.78rem', background: 'transparent', border: '1px solid #2D2D2D', borderRadius: '4px', color: currentPage >= Math.ceil(totalCount / pageSize) ? '#444' : '#C5C5C5', cursor: currentPage >= Math.ceil(totalCount / pageSize) ? 'default' : 'pointer' }}
            >▶</button>
            <span style={{ fontSize: '0.72rem', color: '#666', marginLeft: '8px' }}>
              {fmtNum(totalCount)}개 중 {fmtNum((currentPage - 1) * pageSize + 1)}~{fmtNum(Math.min(currentPage * pageSize, totalCount))}
            </span>
          </div>
        )}
      </div>
    </div>
  )
}
