'use client'

import { useEffect, useState, useCallback, useMemo } from 'react'
import { useSearchParams } from 'next/navigation'
import {
  orderApi,
  channelApi,
  accountApi,
  proxyApi,
  collectorApi,
  forbiddenApi,
  type SambaOrder,
  type SambaChannel,
  type SambaMarketAccount,
} from '@/lib/samba/api/commerce'
import { sourcingAccountApi, type SambaSourcingAccount } from '@/lib/samba/api/operations'
import { fmtTime, formatDateInput, getKstTodayDate } from '@/lib/samba/utils'
import { fmtNum } from '@/lib/samba/styles'
import OrdersTable from './components/OrdersTable'
import { useSmsMessage } from './hooks/useSmsMessage'
import { useOrderSync } from './hooks/useOrderSync'
import { useOrderLinks } from './hooks/useOrderLinks'
import { useOrderActions } from './hooks/useOrderActions'
import { useUrlModal } from './hooks/useUrlModal'
import { renderCopyableText, splitCustomerAddress } from './utils/copyHelpers'
import { formatSourceSiteLabel, normalizeSourceSiteName } from './utils/siteAlias'
import { parsePlayautoAliasEntry } from '@/lib/samba/playautoAlias'
import OrdersFilterBar from './components/OrdersFilterBar'
import OrdersTopBar from './components/OrdersTopBar'
import OrdersPagination from './components/OrdersPagination'
import PriceHistoryModal from './components/PriceHistoryModal'
import MessageModal from './components/MessageModal'
import OrderEditModal from './components/OrderEditModal'
import UrlInputModal from './components/UrlInputModal'
import SmsTemplateEditModal from './components/SmsTemplateEditModal'
import AlarmSettingModal from './components/AlarmSettingModal'
import TrackingModal from './components/TrackingModal'
import { showConfirm, showAlert } from '@/components/samba/Modal'

interface OrderForm {
  channel_id: string; product_name: string; customer_name: string; customer_phone: string
  customer_address: string; sale_price: number; cost: number; fee_rate: number
  shipping_company: string; tracking_number: string; notes: string
}

const emptyForm: OrderForm = {
  channel_id: '', product_name: '', customer_name: '', customer_phone: '',
  customer_address: '', sale_price: 0, cost: 0, fee_rate: 0,
  shipping_company: '', tracking_number: '', notes: '',
}

export default function OrdersPage() {
  useEffect(() => { document.title = 'SAMBA-주문관리' }, [])
  const searchParams = useSearchParams()
  const cpId = searchParams.get('cpId')
  const cpName = searchParams.get('cpName')
  const isProductMode = !!cpId
  const [orders, setOrders] = useState<SambaOrder[]>([])
  const [channels, setChannels] = useState<SambaChannel[]>([])
  const [accounts, setAccounts] = useState<SambaMarketAccount[]>([])
  const [sourcingAccounts, setSourcingAccounts] = useState<SambaSourcingAccount[]>([])
  const [loading, setLoading] = useState(true)
  const [period, setPeriod] = useState('today')
  const [marketFilter, setMarketFilter] = useState('')
  const [marketStatus, setMarketStatus] = useState('')
  const [siteFilter, setSiteFilter] = useState('')
  const [accountFilter, setAccountFilter] = useState('')
  const [registrationFilter, setRegistrationFilter] = useState('registered')
  const [inputFilter, setInputFilter] = useState('')
  const [invoiceFilter, setInvoiceFilter] = useState('')
  const [statusFilter, setStatusFilter] = useState('cancel_return_excluded')
  // CS 페이지 등 외부에서 ?search=...&search_type=... 로 진입 시 자동 검색
  const initialSearch = searchParams.get('search') || ''
  const [searchText, setSearchText] = useState(initialSearch)
  const [appliedSearchText, setAppliedSearchText] = useState(initialSearch)
  const [pageSize, setPageSize] = useState(20)
  const [currentPage, setCurrentPage] = useState(1)
  const [totalCount, setTotalCount] = useState(0)
  const [totalSale, setTotalSale] = useState(0)
  const [pendingCount, setPendingCount] = useState(0)
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set())
  const [bulkStatus, setBulkStatus] = useState('')
  const [bulkUpdating, setBulkUpdating] = useState(false)
  const [sortBy, setSortBy] = useState('date_desc')
  const [logMessages, _setLogMessagesRaw] = useState<string[]>(['[대기] 주문 가져오기 결과가 여기에 표시됩니다...'])
  const setLogMessages: typeof _setLogMessagesRaw = (v) => _setLogMessagesRaw(prev => {
    const next = typeof v === 'function' ? v(prev) : v
    return next.slice(-30)
  })
  const [smsRemain, setSmsRemain] = useState<{ SMS_CNT?: number; LMS_CNT?: number; MMS_CNT?: number } | null>(null)
  const [showForm, setShowForm] = useState(false)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [form, setForm] = useState<OrderForm>({ ...emptyForm })

  const [editingCosts, setEditingCosts] = useState<Record<string, string>>({})
  const [editingTrackings, setEditingTrackings] = useState<Record<string, string>>({})
  const [editingShipFees, setEditingShipFees] = useState<Record<string, string>>({})
  const [editingOrderNumbers, setEditingOrderNumbers] = useState<Record<string, string>>({})

  const [activeActions, setActiveActions] = useState<Record<string, string | null>>({})
  const [collectedProductCosts, setCollectedProductCosts] = useState<Record<string, number>>({})
  const [collectedProductSourceSites, setCollectedProductSourceSites] = useState<Record<string, string>>({})

  const [notifications, setNotifications] = useState<{id: number, message: string, type: string}[]>([])

  const showNotification = (message: string, type: string = 'warning') => {
    const id = Date.now()
    setNotifications(prev => [...prev, { id, message, type }])
  }


  const [refreshLog, setRefreshLog] = useState<Record<string, string>>({})


  const [priceHistoryModal, setPriceHistoryModal] = useState(false)
  const [priceHistoryData, setPriceHistoryData] = useState<Record<string, unknown>[]>([])
  const [priceHistoryProduct, setPriceHistoryProduct] = useState<{ name: string; source_site: string }>({ name: '', source_site: '' })


  const sms = useSmsMessage(accounts)
  const {
    msgModal, setMsgModal,
    msgText, setMsgText,
    msgSending, msgTextRef, msgHistory,
    sentFlags, setSentFlags,
    smsTemplates,
    templateEditModal, setTemplateEditModal,
    isNewTemplate,
    openNewTemplate, openEditTemplate, saveTemplate, deleteTemplate,
    insertMsgTag, openMsgModal, handleSendMsg,
  } = sms


  const [showAlarmSetting, setShowAlarmSetting] = useState(searchParams.get('alarm') === '1')
  const [alarmHour, setAlarmHour] = useState('0')
  const [alarmMin, setAlarmMin] = useState('5')
  const [sleepStart, setSleepStart] = useState('00:00')
  const [sleepEnd, setSleepEnd] = useState('09:00')


  const initialSearchType = searchParams.get('search_type') || 'customer'
  const [searchCategory, setSearchCategory] = useState(initialSearchType)

  const [dateLocked, setDateLocked] = useState(false)
  const [customStart, setCustomStart] = useState(() => formatDateInput(getKstTodayDate()))
  const [startLocked, setStartLocked] = useState(false)
  const [customEnd, setCustomEnd] = useState(() => formatDateInput(getKstTodayDate()))
  const loadOrders = useCallback(async () => {
    setLoading(true)
    try {
      const data = isProductMode
        ? await orderApi.listByCollectedProductPaged({
            collectedProductId: cpId!,
            skip: (currentPage - 1) * pageSize,
            limit: pageSize,
            market_filter: marketFilter,
            site_filter: siteFilter,
            account_filter: accountFilter,
            market_status: marketStatus,
            status_filter: statusFilter,
            input_filter: inputFilter,
            invoice_filter: invoiceFilter,
            registration_filter: registrationFilter,
            search_text: appliedSearchText,
            search_category: searchCategory,
            sort_by: sortBy,
          })
        : await orderApi.listByDateRangePaged({
            start: customStart,
            end: customEnd,
            skip: (currentPage - 1) * pageSize,
            limit: pageSize,
            market_filter: marketFilter,
            site_filter: siteFilter,
            account_filter: accountFilter,
            market_status: marketStatus,
            status_filter: statusFilter,
            input_filter: inputFilter,
            invoice_filter: invoiceFilter,
            registration_filter: registrationFilter,
            search_text: appliedSearchText,
            search_category: searchCategory,
            sort_by: sortBy,
          })
      setOrders(data.items)
      setTotalCount(data.total_count)
      setTotalSale(data.total_sale)
      setPendingCount(data.pending_count)
      setEditingTrackings({})

      const actions: Record<string, string | null> = {}
      for (const o of data.items) {
        if (o.action_tag) actions[o.id] = o.action_tag
      }
      setActiveActions(actions)

      if (data.items.length > 0) {
        proxyApi.fetchSentFlags(data.items.map(o => o.id)).then(flags => {
          setSentFlags(flags)
        }).catch(() => {})
      } else {
        setSentFlags({})
      }
    } catch (e) {
      console.error('주문 조회 실패:', e)
      setLogMessages(prev => [...prev, `[${fmtTime()}] 주문 조회 실패: ${e instanceof Error ? e.message : '알 수 없는 오류'}`])
    }
    setLoading(false)
  }, [isProductMode, cpId, currentPage, pageSize, marketFilter, siteFilter, accountFilter, marketStatus, statusFilter, inputFilter, invoiceFilter, registrationFilter, appliedSearchText, searchCategory, sortBy, customStart, customEnd, setSentFlags])

  const patchOrder = useCallback((id: string, patch: Partial<SambaOrder>) => {
    setOrders(prev => prev.map(order => (
      order.id === id ? { ...order, ...patch } : order
    )))
  }, [])

  const applySearch = useCallback(() => {
    setCurrentPage(1)
    const trimmed = searchText.trim()
    // 검색어가 바뀌면 state 변경만 → loadOrders가 새 값으로 재생성되고 useEffect가 자동 재조회
    // 같은 검색어로 다시 누르면 state가 안 바뀌므로 강제 호출
    if (trimmed === appliedSearchText) loadOrders()
    else setAppliedSearchText(trimmed)
  }, [searchText, appliedSearchText, loadOrders])


  const [siteAliasMap, setSiteAliasMap] = useState<Record<string, string>>({})
  const siteOptions = useMemo(() => {
    const knownSites = ['MUSINSA', 'KREAM', 'FashionPlus', 'Nike', 'Adidas', 'ABCmart', 'REXMONDE', 'SSG', 'LOTTEON', 'GSSHOP', 'ElandMall', 'SSF']
    const options = new Map<string, string>()

    const formatSiteLabel = (site: string) => {
      const formatted = formatSourceSiteLabel(site, siteAliasMap)
      if (formatted) return formatted
      return normalizeSourceSiteName(site)
    }

    for (const site of knownSites) {
      options.set(site, formatSiteLabel(site))
    }
    for (const order of orders) {
      const site = order.source_site?.trim()
      if (!site) continue
      // 괄호 안은 플레이오토 마켓 계정명(예: GS이숍(고경))이 source_site에 섞이는 케이스 — base만 노출
      const baseRaw = site.match(/^(.+?)\(/)?.[1]?.trim() || site
      const baseSite = normalizeSourceSiteName(baseRaw)
      options.set(baseSite, formatSiteLabel(baseSite))
    }

    return [...options.entries()]
      .map(([value, label]) => ({ value, label }))
      .sort((a, b) => a.label.localeCompare(b.label, 'ko'))
  }, [orders, siteAliasMap])
  useEffect(() => { loadOrders() }, [loadOrders])
  useEffect(() => {
    setCurrentPage(1)
  }, [pageSize, customStart, customEnd, marketFilter, siteFilter, accountFilter, marketStatus, statusFilter, registrationFilter, inputFilter, invoiceFilter, searchCategory, sortBy, isProductMode, cpId])
  useEffect(() => {
    const ids = [...new Set(orders.map(o => o.collected_product_id).filter((id): id is string => !!id))]
    if (ids.length === 0) {
      setCollectedProductCosts({})
      setCollectedProductSourceSites({})
      return
    }
    let cancelled = false
    ;(async () => {
      try {
        const rows = await collectorApi.getProductsByIds(ids)
        if (cancelled) return
        const next: Record<string, number> = {}
        const nextSourceSites: Record<string, string> = {}
        for (const row of rows) {
          next[row.id] = row.cost ?? row.sale_price ?? row.original_price ?? 0
          if (row.source_site) nextSourceSites[row.id] = row.source_site
        }
        setCollectedProductCosts(next)
        setCollectedProductSourceSites(nextSourceSites)
      } catch {
        if (!cancelled) {
          setCollectedProductCosts({})
          setCollectedProductSourceSites({})
        }
      }
    })()
    return () => { cancelled = true }
  }, [orders])

  useEffect(() => {
    orderApi.getAlarmSettings().then(d => {
      setAlarmHour(String(d.hour))
      setAlarmMin(String(d.min))
      setSleepStart(d.sleep_start)
      setSleepEnd(d.sleep_end)
    }).catch(() => {})
  }, [])

  useEffect(() => {
    if (searchParams.get('alarm') === '1') setShowAlarmSetting(true)
  }, [searchParams])

  useEffect(() => {
    const handler = () => setShowAlarmSetting(true)
    window.addEventListener('open-alarm-setting', handler)
    return () => window.removeEventListener('open-alarm-setting', handler)
  }, [])

  // 알람 모달 "지금 확인하기"로 들어왔을 때 — cancel_alert 필터 + 전체 기간으로 세팅
  useEffect(() => {
    if (searchParams.get('cancel_alert') === '1') {
      setStatusFilter('cancel_alert')
      setMarketStatus('')
      setRegistrationFilter('')
      setInputFilter('')
      setInvoiceFilter('')
      setCustomStart('2020-01-01')
      setCustomEnd(formatDateInput(getKstTodayDate()))
      setPeriod('')
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams])

  // 알람 모달 X 버튼 → 디폴트 오늘 주문 화면으로 복귀 (초기 진입 상태와 동일)
  useEffect(() => {
    const handler = () => {
      setStatusFilter('cancel_return_excluded')
      setMarketStatus('')
      setRegistrationFilter('registered')
      setInputFilter('')
      setInvoiceFilter('')
      setAppliedSearchText('')
      setSearchText('')
      setPeriod('today')
      const today = formatDateInput(getKstTodayDate())
      setCustomStart(today)
      setCustomEnd(today)
    }
    window.addEventListener('reset-orders-filter', handler)
    return () => window.removeEventListener('reset-orders-filter', handler)
  }, [])
  // 마운트 1회 — 5개 메타 API를 하나의 useEffect에서 동시 호출 (DB 커넥션 경합 최소화)
  useEffect(() => {
    Promise.all([
      channelApi.list().then(setChannels).catch(() => {}),
      accountApi.listActive().then(setAccounts).catch(() => {}),
      sourcingAccountApi.list().then(accs => setSourcingAccounts(accs.filter(a => a.is_active))).catch(() => {}),
      proxyApi.aligoRemain().then(r => { if (r.success) setSmsRemain(r) }).catch(() => {}),
      forbiddenApi.getSetting('store_playauto').then(data => {
        const d = data as Record<string, string> | null
        if (!d) return
        const map: Record<string, string> = {}
        for (const k of ['alias1', 'alias2', 'alias3', 'alias4', 'alias5']) {
          const v = d[k] || ''
          const { code, alias } = parsePlayautoAliasEntry(v)
          if (code && alias) map[code] = alias
        }
        setSiteAliasMap(map)
      }).catch(() => {}),
    ])
  }, [])

  const { syncing, syncAccountId, setSyncAccountId, handleFetch } = useOrderSync({
    accounts, period, setLogMessages, showNotification, loadOrders,
  })


  const {
    handleSubmit, handleStatusChange, handleDelete,
    handleCostSave, handleShipFeeSave, calcProfit, calcProfitRate, calcFeeRate,
    handleCopyOrderNumber, handleDanawa, handleNaver, handleTracking,
    toggleAction, handleBulkAction,
  } = useOrderActions({
    channels, form, emptyForm, editingId,
    setShowForm, setEditingId, setForm, loadOrders, patchOrder,
    editingCosts, setEditingCosts,
    editingShipFees, setEditingShipFees,
    activeActions, setActiveActions,
    bulkStatus, setBulkStatus, bulkUpdating, setBulkUpdating,
    selectedIds, setSelectedIds,
    setLogMessages,
    openTrackingModal: (o: SambaOrder) => setTrackingOrder(o),
  })
  const [trackingOrder, setTrackingOrder] = useState<SambaOrder | null>(null)
  const [trackingSyncing, setTrackingSyncing] = useState(false)
  // 주문 자동실행 인터벌 (분 단위, 0=OFF)
  const [autoSyncIntervalInput, setAutoSyncIntervalInput] = useState<number>(60)
  const [autoSyncEnabled, setAutoSyncEnabled] = useState<boolean>(false)
  const [autoSyncSaving, setAutoSyncSaving] = useState<boolean>(false)
  type AutoSyncHistoryItem = {
    job_id: string
    status: string
    created_at: string | null
    started_at: string | null
    completed_at: string | null
    duration_sec: number | null
    total_synced: number
    per_market: Array<{ account: string; status: string; synced: number; fetched: number; message: string }>
    tracking_sync: {
      success: boolean
      queued: number
      skipped: number
      jobs: number
      errors: string[]
      ran_at: string | null
    } | null
    error: string | null
  }
  const [autoSyncHistory, setAutoSyncHistory] = useState<AutoSyncHistoryItem[]>([])
  useEffect(() => {
    orderApi.getAutoSyncInterval()
      .then(res => {
        if (res.interval_minutes > 0) {
          setAutoSyncIntervalInput(res.interval_minutes)
          setAutoSyncEnabled(true)
        }
      })
      .catch(() => {})
  }, [])
  // 최근 자동실행 이력 2건 — 30초마다 폴링 (러닝 상태도 추적)
  useEffect(() => {
    let cancelled = false
    const load = () => {
      orderApi.getAutoSyncHistory(2)
        .then(res => { if (!cancelled) setAutoSyncHistory(res.items || []) })
        .catch(() => {})
    }
    load()
    const t = setInterval(load, 30_000)
    return () => { cancelled = true; clearInterval(t) }
  }, [])
  const handleToggleAutoSync = async () => {
    if (autoSyncSaving) return
    const nextValue = !autoSyncEnabled
    setAutoSyncSaving(true)
    try {
      const minutes = nextValue ? Math.max(5, autoSyncIntervalInput) : 0
      const res = await orderApi.setAutoSyncInterval(minutes)
      setAutoSyncEnabled(res.interval_minutes > 0)
      setLogMessages(prev => [
        ...prev,
        res.interval_minutes > 0
          ? `[자동실행] ON — ${fmtNum(res.interval_minutes)}분 간격으로 주문가져오기+송장수집 자동 실행`
          : '[자동실행] OFF',
      ])
    } catch (err) {
      showAlert('주문 자동실행 설정 저장 실패: ' + ((err as Error)?.message || String(err)))
    } finally {
      setAutoSyncSaving(false)
    }
  }
  const [trackingStatusOpen, setTrackingStatusOpen] = useState(false)
  const [trackingStatusData, setTrackingStatusData] = useState<{
    counts: Record<string, number>
    recent: Array<{
      id: string; orderId: string; orderNumber: string; customerName: string
      channelName: string; site: string; sourcingOrderNumber: string; sourcingAccountLabel: string
      status: string; courier?: string | null; tracking?: string | null
      lastError?: string | null; attempts: number; updatedAt?: string | null
      paidAt?: string | null; actionTag?: string | null
    }>
  } | null>(null)
  // 이번 송장수집 배치 잡 id 목록 — 모달이 이 id들만 고정 표시하기 위한 키
  // 비어있으면 기존 recent 풀(7일 미입력 전체) 폴백
  const [trackingBatchIds, setTrackingBatchIds] = useState<string[]>([])
  const refreshTrackingStatus = useCallback(async () => {
    try {
      const data = trackingBatchIds.length > 0
        ? await orderApi.listTrackingSyncJobsByIds(trackingBatchIds)
        : await orderApi.listRecentTrackingSyncJobs(50)
      setTrackingStatusData(data)
    } catch (err) {
      setLogMessages(prev => [...prev, `[송장상태] 조회 실패: ${(err as Error).message}`])
    }
  }, [trackingBatchIds])

  // 모달이 열려있고 처리 중인 잡(PENDING/DISPATCHED)이 있으면 5초 폴링
  // 배치 id로만 조회하므로 행 추가/제거 없이 셀 값만 갱신 — 리스트 출렁임 없음
  useEffect(() => {
    if (!trackingStatusOpen) return
    refreshTrackingStatus()
    const interval = setInterval(() => {
      const inFlight = (trackingStatusData?.counts.PENDING || 0)
        + (trackingStatusData?.counts.DISPATCHED || 0)
      if (inFlight > 0) refreshTrackingStatus()
    }, 5000)
    return () => clearInterval(interval)
  }, [trackingStatusOpen, trackingStatusData, refreshTrackingStatus])

  const handleTrackingSyncOne = async (o: SambaOrder) => {
    try {
      const res = await orderApi.syncTracking(o.id)
      if (res.skipped) {
        setLogMessages(prev => [...prev, `[송장] 스킵: ${res.reason || '이미 처리됨'}`])
      } else if (res.success) {
        setLogMessages(prev => [...prev, `[송장] 큐 적재 완료 (요청ID ${res.requestId}) — 확장앱이 처리 중...`])
      } else {
        setLogMessages(prev => [...prev, `[송장] 실패: ${res.error || '알 수 없음'}`])
      }
    } catch (err) {
      setLogMessages(prev => [...prev, `[송장] 오류: ${(err as Error).message}`])
    }
  }

  const handleTrackingSyncBulk = async () => {
    if (!await showConfirm('최근 7일 내 미발송 주문(소싱처 주문번호 있고 송장 미입력)의 송장을 일괄 동기화합니다.\n기존 처리 안 된(좀비 PENDING 포함) 잡은 닫고 새로 큐잉합니다. 진행할까요?')) return
    setTrackingSyncing(true)
    try {
      const res = await orderApi.syncTrackingBulk(500, 7, true)
      setLogMessages(prev => [
        ...prev,
        `[송장 일괄] 큐 적재 ${fmtNum(res.queued)}건 / 스킵 ${fmtNum(res.skipped)}건 / 오류 ${fmtNum(res.errors.length)}건`,
        ...res.errors.slice(0, 5).map(e => `  · ${e}`),
      ])
      // 이번 배치 잡 id 목록 저장 — 모달이 이 batch 의 잡들만 고정 표시 (status 변화 추적).
      // SCRAPED/NO_TRACKING 등 처리 완료된 잡도 같은 batch 안에선 계속 표시되어 사라지지 않음.
      // 새 송장수집 trigger 시 새 batch 로 replace — 옛 batch 의 잡은 모달에서 빠짐 (의도).
      setTrackingBatchIds(res.job_ids || [])
      setTrackingStatusOpen(true)
      setTimeout(() => { loadOrders() }, 60000)
    } catch (err) {
      setLogMessages(prev => [...prev, `[송장 일괄] 오류: ${(err as Error).message}`])
    } finally {
      setTrackingSyncing(false)
    }
  }
  const { handleSourceLink, handleMarketLink } = useOrderLinks(accounts)

  const {
    showUrlModal, setShowUrlModal,
    urlModalInput, setUrlModalInput,
    urlModalImageInput, setUrlModalImageInput,
    urlModalSaving,
    openUrlModal, handleUrlSubmit,
  } = useUrlModal({ orders, loadOrders })

  const handleImageClick = (o: SambaOrder) => {
    if (o.product_id && o.product_id.startsWith('http')) { window.open(o.product_id, '_blank'); return }
    if (o.product_id && o.channel_id) { handleMarketLink(o); return }
    if (o.product_image && o.product_image.startsWith('http')) window.open(o.product_image, '_blank')
  }


  const currentPageIds = useMemo(() => orders.map(o => o.id), [orders])


  const toggleSelectAll = () => {
    if (currentPageIds.every(id => selectedIds.has(id))) {
      setSelectedIds(prev => { const next = new Set(prev); currentPageIds.forEach(id => next.delete(id)); return next })
    } else {
      setSelectedIds(prev => { const next = new Set(prev); currentPageIds.forEach(id => next.add(id)); return next })
    }
  }

  
  return (
    <div style={{ color: '#E5E5E5' }}>
      <OrdersTopBar
        notifications={notifications}
        setNotifications={setNotifications}
        setStatusFilter={setStatusFilter}
        setMarketStatus={setMarketStatus}
        setCustomStart={setCustomStart}
        setCustomEnd={setCustomEnd}
        setPeriod={setPeriod}
        isProductMode={isProductMode}
        cpId={cpId}
        cpName={cpName}
        filteredOrdersCount={totalCount}
        pendingCount={pendingCount}
        smsRemain={smsRemain}
        logMessages={logMessages}
        setLogMessages={setLogMessages}
      />

      <OrdersFilterBar
        isProductMode={isProductMode}
        period={period} setPeriod={setPeriod}
        customStart={customStart} setCustomStart={setCustomStart}
        customEnd={customEnd} setCustomEnd={setCustomEnd}
        startLocked={startLocked} setStartLocked={setStartLocked}
        dateLocked={dateLocked} setDateLocked={setDateLocked}
        syncAccountId={syncAccountId} setSyncAccountId={setSyncAccountId}
        syncing={syncing} handleFetch={handleFetch}
        bulkStatus={bulkStatus} setBulkStatus={setBulkStatus}
        bulkUpdating={bulkUpdating} handleBulkAction={handleBulkAction}
        selectedIdsSize={selectedIds.size}
        filteredOrdersCount={totalCount}
        filteredOrdersTotalSale={totalSale}
        searchCategory={searchCategory} setSearchCategory={setSearchCategory}
        searchText={searchText} setSearchText={setSearchText}
        loadOrders={applySearch}
        marketFilter={marketFilter} setMarketFilter={setMarketFilter}
        siteFilter={siteFilter} setSiteFilter={setSiteFilter}
        accountFilter={accountFilter} setAccountFilter={setAccountFilter}
        marketStatus={marketStatus} setMarketStatus={setMarketStatus}
        registrationFilter={registrationFilter} setRegistrationFilter={setRegistrationFilter}
        inputFilter={inputFilter} setInputFilter={setInputFilter}
        invoiceFilter={invoiceFilter} setInvoiceFilter={setInvoiceFilter}
        statusFilter={statusFilter} setStatusFilter={setStatusFilter}
        sortBy={sortBy} setSortBy={setSortBy}
        pageSize={pageSize} setPageSize={setPageSize}
        accounts={accounts} sourcingAccounts={sourcingAccounts}
        siteOptions={siteOptions}
      />

      {/* 주문 자동실행 토글바 — 주문가져오기 + 송장수집 인터벌 자동 실행 */}
      <div style={{
        padding: '0.75rem 1rem', margin: '6px 0',
        background: autoSyncEnabled ? 'rgba(34,197,94,0.08)' : 'rgba(255,140,0,0.08)',
        border: autoSyncEnabled ? '1px solid rgba(34,197,94,0.25)' : '1px solid rgba(255,140,0,0.25)',
        borderRadius: 10,
      }}>
        <div style={{
          display: 'flex', alignItems: 'center', justifyContent: 'space-between',
          gap: '1rem',
        }}>
          <div>
            <div style={{ fontSize: '0.9375rem', fontWeight: 700, color: '#E5E5E5', marginBottom: '0.2rem' }}>
              🔄 주문 자동실행
            </div>
            <div style={{ fontSize: '0.75rem', color: '#888' }}>
              ON이면 설정한 분 간격마다 서버에서 전체 주문가져오기 → 송장수집을 자동 실행합니다.
            </div>
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexShrink: 0 }}>
            <input
              type="number"
              value={autoSyncIntervalInput}
              onChange={e => setAutoSyncIntervalInput(Math.max(5, Number(e.target.value)))}
              min={5}
              max={1440}
              style={{
                width: 56,
                background: '#2A2A2A',
                border: '1px solid #444',
                color: '#ccc',
                borderRadius: 6,
                padding: '4px 6px',
                fontSize: '0.8125rem',
                textAlign: 'center',
              }}
            />
            <span style={{ color: '#888', fontSize: '0.8125rem' }}>분</span>
            <button
              onClick={handleToggleAutoSync}
              disabled={autoSyncSaving}
              style={{
                minWidth: '92px',
                padding: '0.5rem 0.875rem',
                borderRadius: '999px',
                border: autoSyncEnabled ? '1px solid rgba(34,197,94,0.35)' : '1px solid rgba(255,140,0,0.35)',
                background: autoSyncEnabled ? '#22C55E' : '#2A2A2A',
                color: autoSyncEnabled ? '#06130A' : '#FFB84D',
                fontSize: '0.8125rem',
                fontWeight: 700,
                cursor: autoSyncSaving ? 'not-allowed' : 'pointer',
                opacity: autoSyncSaving ? 0.7 : 1,
              }}
            >
              {autoSyncSaving ? '저장 중...' : autoSyncEnabled ? 'ON' : 'OFF'}
            </button>
          </div>
        </div>

        {/* 최근 자동실행 이력 2건 요약 — 작동 여부 확인용 */}
        {autoSyncHistory.length > 0 && (
          <div style={{
            marginTop: 10, paddingTop: 8, borderTop: '1px solid rgba(255,255,255,0.06)',
            display: 'flex', flexDirection: 'column', gap: 6,
          }}>
            <div style={{ fontSize: '0.7rem', color: '#888', fontWeight: 600 }}>최근 자동실행 이력</div>
            {autoSyncHistory.map(item => {
              const ts = item.created_at ? new Date(item.created_at) : null
              const tsLabel = ts ? ts.toLocaleString('ko-KR', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }) : '-'
              const statusColor = item.status === 'completed' ? '#22C55E'
                : item.status === 'running' ? '#FFB84D'
                : item.status === 'pending' ? '#888'
                : '#FF6B6B'
              const statusLabel = item.status === 'completed' ? '완료'
                : item.status === 'running' ? '실행중'
                : item.status === 'pending' ? '대기'
                : item.status === 'failed' ? '실패'
                : item.status === 'cancelled' ? '취소' : item.status
              const okMarkets = item.per_market.filter(m => m.status === 'success').length
              const errMarkets = item.per_market.filter(m => m.status !== 'success' && m.status !== 'skip').length
              const tsync = item.tracking_sync
              return (
                <div key={item.job_id} style={{
                  display: 'flex', flexDirection: 'column', gap: 4,
                  padding: '6px 8px', background: 'rgba(0,0,0,0.2)', borderRadius: 6,
                }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap', fontSize: '0.75rem', color: '#CCC' }}>
                    <span style={{ color: '#888', fontFamily: 'monospace' }}>{tsLabel}</span>
                    <span style={{
                      color: statusColor, fontWeight: 700,
                      padding: '1px 6px', borderRadius: 4,
                      background: `${statusColor}15`, border: `1px solid ${statusColor}30`,
                    }}>{statusLabel}</span>
                    <span style={{ color: '#4C9AFF', fontWeight: 600 }}>① 주문가져오기</span>
                    <span>신규 <span style={{ color: '#fff', fontWeight: 700 }}>{fmtNum(item.total_synced)}</span>건</span>
                    <span style={{ color: '#888' }}>마켓 성공 {okMarkets} / 실패 {errMarkets}</span>
                    {item.duration_sec !== null && (
                      <span style={{ color: '#888' }}>소요 {item.duration_sec}s</span>
                    )}
                    {item.error && (
                      <span style={{ color: '#FF6B6B' }} title={item.error}>오류: {item.error.slice(0, 80)}</span>
                    )}
                  </div>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap', fontSize: '0.75rem', color: '#CCC', paddingLeft: 2 }}>
                    <span style={{ color: tsync ? '#22C55E' : '#666', fontWeight: 600 }}>② 송장수집</span>
                    {tsync ? (
                      <>
                        <span>큐 <span style={{ color: '#fff', fontWeight: 700 }}>{fmtNum(tsync.queued)}</span>건</span>
                        <span style={{ color: '#888' }}>스킵 {fmtNum(tsync.skipped)}</span>
                        <span style={{ color: '#888' }}>잡 {fmtNum(tsync.jobs)}개</span>
                        {tsync.errors.length > 0 && (
                          <span style={{ color: '#FF6B6B' }} title={tsync.errors.join(' / ')}>
                            오류 {tsync.errors.length}건
                          </span>
                        )}
                      </>
                    ) : (
                      <span style={{ color: '#666' }}>
                        {item.status === 'running' || item.status === 'pending' ? '주문가져오기 종료 후 실행' : '결과 없음'}
                      </span>
                    )}
                  </div>
                </div>
              )
            })}
          </div>
        )}
      </div>

      {/* 송장 자동전송 미니바 — 일괄 트리거 + 안내 */}
      <div style={{
        display: 'flex', alignItems: 'center', gap: 12,
        padding: '8px 14px', margin: '6px 0',
        background: '#1a1a1a', border: '1px solid #333', borderRadius: 6,
        fontSize: 13, color: '#ccc',
      }}>
        <span style={{ fontWeight: 600, color: '#fff' }}>📦 송장 자동전송</span>
        <span style={{ color: '#888' }}>
          미발송 주문을 소싱처(무신사/롯데/SSG/ABC/GS/패션플러스/나이키/올리브영)에서 추출 → 마켓 전송
        </span>
        <button
          onClick={() => { setTrackingBatchIds([]); setTrackingStatusOpen(true) }}
          style={{
            marginLeft: 'auto',
            padding: '6px 14px',
            background: '#374151', color: '#fff', border: 'none', borderRadius: 4,
            cursor: 'pointer', fontSize: 13, fontWeight: 600,
          }}
        >
          📊 진행 현황
        </button>
        <button
          onClick={handleTrackingSyncBulk}
          disabled={trackingSyncing}
          style={{
            padding: '6px 14px',
            background: trackingSyncing ? '#444' : '#2563eb',
            color: '#fff', border: 'none', borderRadius: 4,
            cursor: trackingSyncing ? 'not-allowed' : 'pointer',
            fontSize: 13, fontWeight: 600,
          }}
        >
          {trackingSyncing ? '큐 적재 중...' : '송장수집'}
        </button>
        {selectedIds.size > 0 && (
          <button
            onClick={async () => {
              const ids = Array.from(selectedIds)
              setLogMessages(prev => [...prev, `[송장] 선택한 ${fmtNum(ids.length)}건 큐 적재 시작...`])
              for (const id of ids) {
                const ord = orders.find(o => o.id === id)
                if (ord) await handleTrackingSyncOne(ord)
              }
            }}
            style={{
              padding: '6px 14px',
              background: '#16a34a', color: '#fff', border: 'none', borderRadius: 4,
              cursor: 'pointer', fontSize: 13, fontWeight: 600,
            }}
          >
            선택 {fmtNum(selectedIds.size)}건 송장수집
          </button>
        )}
      </div>

      <OrdersTable
        loading={loading}
        filteredOrders={orders}
        currentPage={currentPage}
        pageSize={pageSize}
        currentPageIds={currentPageIds}
        selectedIds={selectedIds}
        setSelectedIds={setSelectedIds}
        toggleSelectAll={toggleSelectAll}
        editingCosts={editingCosts}
        setEditingCosts={setEditingCosts}
        editingShipFees={editingShipFees}
        setEditingShipFees={setEditingShipFees}
        editingTrackings={editingTrackings}
        setEditingTrackings={setEditingTrackings}
        editingOrderNumbers={editingOrderNumbers}
        setEditingOrderNumbers={setEditingOrderNumbers}
        activeActions={activeActions}
        collectedProductCosts={collectedProductCosts}
        collectedProductSourceSites={collectedProductSourceSites}
        refreshLog={refreshLog}
        setRefreshLog={setRefreshLog}
        sentFlags={sentFlags}
        siteAliasMap={siteAliasMap}
        sourcingAccounts={sourcingAccounts}
        setPriceHistoryProduct={setPriceHistoryProduct}
        setPriceHistoryData={setPriceHistoryData}
        setPriceHistoryModal={setPriceHistoryModal}
        setLogMessages={setLogMessages}
        calcProfit={calcProfit}
        calcProfitRate={calcProfitRate}
        calcFeeRate={calcFeeRate}
        splitCustomerAddress={splitCustomerAddress}
        renderCopyableText={renderCopyableText}
        handleDelete={handleDelete}
        handleImageClick={handleImageClick}
        handleCopyOrderNumber={handleCopyOrderNumber}
        openMsgModal={openMsgModal}
        handleDanawa={handleDanawa}
        handleNaver={handleNaver}
        handleSourceLink={handleSourceLink}
        handleMarketLink={handleMarketLink}
        openUrlModal={openUrlModal}
        handleTracking={handleTracking}
        loadOrders={loadOrders}
        patchOrder={patchOrder}
        handleStatusChange={handleStatusChange}
        handleCostSave={handleCostSave}
        handleShipFeeSave={handleShipFeeSave}
        toggleAction={toggleAction}
      />


      <OrdersPagination
        totalCount={totalCount}
        pageSize={pageSize}
        currentPage={currentPage}
        setCurrentPage={setCurrentPage}
      />


      <OrderEditModal
        open={showForm}
        editingId={editingId}
        form={form}
        setForm={setForm}
        onClose={() => { setShowForm(false); setEditingId(null) }}
        onSubmit={handleSubmit}
      />


      <UrlInputModal
        open={showUrlModal}
        urlInput={urlModalInput}
        setUrlInput={setUrlModalInput}
        imageInput={urlModalImageInput}
        setImageInput={setUrlModalImageInput}
        saving={urlModalSaving}
        onClose={() => setShowUrlModal(false)}
        onSubmit={handleUrlSubmit}
      />


      <PriceHistoryModal
        open={priceHistoryModal}
        product={priceHistoryProduct}
        history={priceHistoryData}
        onClose={() => setPriceHistoryModal(false)}
      />


      <MessageModal
        msgModal={msgModal}
        setMsgModal={setMsgModal}
        msgText={msgText}
        setMsgText={setMsgText}
        msgTextRef={msgTextRef}
        msgSending={msgSending}
        msgHistory={msgHistory}
        smsTemplates={smsTemplates}
        insertMsgTag={insertMsgTag}
        openEditTemplate={openEditTemplate}
        openNewTemplate={openNewTemplate}
        deleteTemplate={deleteTemplate}
        handleSendMsg={handleSendMsg}
      />


      <SmsTemplateEditModal
        template={templateEditModal}
        setTemplate={setTemplateEditModal}
        isNew={isNewTemplate}
        onSave={saveTemplate}
      />


      <AlarmSettingModal
        open={showAlarmSetting}
        onClose={() => setShowAlarmSetting(false)}
        alarmHour={alarmHour}
        setAlarmHour={setAlarmHour}
        alarmMin={alarmMin}
        setAlarmMin={setAlarmMin}
        sleepStart={sleepStart}
        setSleepStart={setSleepStart}
        sleepEnd={sleepEnd}
        setSleepEnd={setSleepEnd}
      />

      <TrackingModal
        open={!!trackingOrder}
        order={trackingOrder}
        onClose={() => setTrackingOrder(null)}
      />

      {/* 송장 자동전송 진행 현황 모달 */}
      {trackingStatusOpen && (
        <div
          onClick={() => {
            // 모달 닫기 = 단순 UI 닫기. 백그라운드 잡 처리는 계속 진행.
            // (이전 회귀: 모달 닫기에서 배치 취소 자동 호출 → 처리 중 잡 전부 cancelled
            //  되어 사용자가 결과 보러 닫을 때마다 송장수집 무효화되던 사고 차단)
            // 진짜 취소 의도는 별도 "취소" 버튼 명시 클릭만 인정.
            setTrackingStatusOpen(false)
          }}
          style={{
            position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.6)',
            display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 9999,
          }}
        >
          <div
            onClick={(e) => e.stopPropagation()}
            style={{
              background: '#1f2937', color: '#e5e7eb',
              width: 1612, maxWidth: '98vw', maxHeight: '85vh',
              borderRadius: 8, padding: 20, overflow: 'auto',
              border: '1px solid #374151',
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 14 }}>
              <h3 style={{ margin: 0, fontSize: 16, fontWeight: 700 }}>📦 송장 자동전송 진행 현황</h3>
              <div style={{ display: 'flex', gap: 8 }}>
                <button
                  onClick={async () => {
                    if (trackingBatchIds.length === 0) {
                      showAlert('중단할 배치가 없습니다', 'info')
                      return
                    }
                    if (!await showConfirm('진행 중인 송장수집을 모두 중단합니다.\n자동 로그인 + 잡 처리가 즉시 종료됩니다. 진행할까요?')) return
                    try {
                      const res = await orderApi.cancelTrackingSyncBatch(trackingBatchIds)
                      if ((res?.cancelled || 0) > 0) {
                        setLogMessages(prev => [...prev, `[송장] 중단: ${fmtNum(res.cancelled)}건`])
                      }
                      refreshTrackingStatus()
                    } catch (err) {
                      setLogMessages(prev => [...prev, `[송장] 중단 실패: ${(err as Error).message}`])
                    }
                  }}
                  style={{ padding: '4px 10px', background: '#ef4444', color: '#fff', border: 'none', borderRadius: 4, cursor: 'pointer', fontSize: 12, fontWeight: 700 }}
                  title="진행 중인 송장수집 즉시 중단 (확인 다이얼로그 후)"
                >⏹ 중단</button>
                <button
                  onClick={async () => {
                    if (!await showConfirm('자동 마켓전송이 실패한 잡들(SCRAPED + 송장전송실패)을 다시 시도합니다.\n도혜연 같은 ext_order_number 누락 케이스는 운영자가 먼저 보강 후 재시도하세요. 진행할까요?')) return
                    try {
                      const res = await orderApi.dispatchTrackingBulk(false)
                      setLogMessages(prev => [
                        ...prev,
                        `[마켓 재전송] 총 ${fmtNum(res.total)}건 / 성공 ${fmtNum(res.sent)}건 / 실패 ${fmtNum(res.failed)}건`,
                        ...res.errors.slice(0, 5).map(e => `  · ${e}`),
                      ])
                      refreshTrackingStatus()
                    } catch (err) {
                      setLogMessages(prev => [...prev, `[마켓 재전송] 오류: ${(err as Error).message}`])
                    }
                  }}
                  style={{ padding: '4px 10px', background: '#16a34a', color: '#fff', border: 'none', borderRadius: 4, cursor: 'pointer', fontSize: 12 }}
                  title="자동 dispatch가 실패한 SCRAPED/송장전송실패 잡 일괄 재시도 (자동 dispatch는 SCRAPED 직후 1회 시도, 실패 시 이 버튼으로 수동 재시도)"
                >마켓전송 재시도</button>
                <button
                  onClick={refreshTrackingStatus}
                  style={{ padding: '4px 10px', background: '#2563eb', color: '#fff', border: 'none', borderRadius: 4, cursor: 'pointer', fontSize: 12 }}
                >새로고침</button>
                <button
                  onClick={() => setTrackingStatusOpen(false)}
                  style={{ padding: '4px 10px', background: '#4b5563', color: '#fff', border: 'none', borderRadius: 4, cursor: 'pointer', fontSize: 12 }}
                  title="모달만 닫기 (백그라운드 처리는 계속)"
                >닫기</button>
              </div>
            </div>

            {/* 상태 카운트 카드 */}
            <div style={{ display: 'flex', gap: 8, marginBottom: 14, flexWrap: 'wrap' }}>
              {[
                { key: 'PENDING', label: '대기', color: '#6b7280' },
                { key: 'DISPATCHED', label: '추출중', color: '#0ea5e9' },
                { key: 'SCRAPED', label: '추출완료', color: '#16a34a' },
                { key: 'SENT_TO_MARKET', label: '마켓전송', color: '#22c55e' },
                { key: 'DISPATCH_FAILED', label: '송장전송실패', color: '#dc2626' },
                { key: 'NO_TRACKING', label: '미발송', color: '#f59e0b' },
                { key: 'WRONG_ACCOUNT', label: '계정불일치', color: '#fb923c' },
                { key: 'CANCELLED', label: '원주문취소', color: '#a855f7' },
                { key: 'FAILED', label: '실패', color: '#ef4444' },
              ].map(({ key, label, color }) => {
                const cnt = trackingStatusData?.counts[key] || 0
                return (
                  <div key={key} style={{
                    flex: 1, minWidth: 110, padding: '10px 12px',
                    background: '#111827', border: `1px solid ${color}`, borderRadius: 6,
                  }}>
                    <div style={{ color, fontSize: 11, fontWeight: 600 }}>{label}</div>
                    <div style={{ fontSize: 22, fontWeight: 700, marginTop: 2 }}>{fmtNum(cnt)}</div>
                  </div>
                )
              })}
            </div>

            {/* 최근 잡 목록 */}
            <div style={{ background: '#111827', borderRadius: 6, overflow: 'hidden', border: '1px solid #374151' }}>
              <div style={{
                display: 'grid', gridTemplateColumns: '36px 88px 110px 150px 160px 200px 80px 140px 90px 90px 120px 266px',
                padding: '8px 10px', background: '#0f172a', fontSize: 11, fontWeight: 700, color: '#9ca3af',
              }}>
                <div>#</div>
                <div>상태</div>
                <div>결제일</div>
                <div>상품주문번호</div>
                <div>고객명</div>
                <div>판매처</div>
                <div>소싱처</div>
                <div>소싱주문번호</div>
                <div>소싱처계정</div>
                <div>택배사</div>
                <div>송장번호</div>
                <div>오류/메모</div>
              </div>
              {(trackingStatusData?.recent || []).map((j, idx) => {
                const statusColor: Record<string, string> = {
                  PENDING: '#6b7280', DISPATCHED: '#0ea5e9', SCRAPED: '#16a34a',
                  SENT_TO_MARKET: '#22c55e', DISPATCH_FAILED: '#dc2626',
                  NO_TRACKING: '#f59e0b', WRONG_ACCOUNT: '#fb923c', CANCELLED: '#a855f7', FAILED: '#ef4444',
                }
                // 소싱처 원주문링크 URL 매핑 (대소문자/한글 변형 모두 대응)
                // 롯데ON 선물주문은 일반 orderDetail 페이지에서 조회 안 됨 → giftBoxDetail 사용
                const buildSourcingOrderUrl = (site: string, srcNo: string, actionTag: string): string | null => {
                  if (!srcNo) return null
                  const raw = (site || '').split('(')[0].trim().toUpperCase()
                  // 한글 → 코드 정규화
                  const aliasMap: Record<string, string> = {
                    'GS이숍': 'GSSHOP',
                    'GS샵': 'GSSHOP',
                    '롯데ON': 'LOTTEON',
                    '롯데온': 'LOTTEON',
                    '무신사': 'MUSINSA',
                    '크림': 'KREAM',
                    '나이키': 'NIKE',
                    '패션플러스': 'FASHIONPLUS',
                    '올리브영': 'OLIVEYOUNG',
                  }
                  const code = aliasMap[raw] || raw
                  void actionTag
                  const map: Record<string, string> = {
                    MUSINSA: `https://www.musinsa.com/order/order-detail/${srcNo}`,
                    KREAM: `https://kream.co.kr/my/purchasing/${srcNo}`,
                    FASHIONPLUS: `https://www.fashionplus.co.kr/mypage/order/detail/${srcNo}`,
                    ABCMART: `https://abcmart.a-rt.com/mypage/order/read-order-detail?orderNo=${srcNo}`,
                    GRANDSTAGE: `https://grandstage.a-rt.com/mypage/order/read-order-detail?orderNo=${srcNo}`,
                    NIKE: `https://www.nike.com/kr/orders/${srcNo}`,
                    SSG: `https://pay.ssg.com/myssg/orderInfoDetail.ssg?orordNo=${encodeURIComponent(srcNo)}&viewType=Ssg`,
                    LOTTEON: `https://www.lotteon.com/p/order/claim/giftBoxDetail?odNo=${srcNo}&type=snd`,
                    GSSHOP: `https://www.gsshop.com/ord/dlvcursta/popup/ordDtl.gs?orderNo=${srcNo}`,
                    OLIVEYOUNG: `https://www.oliveyoung.co.kr/store/mypage/getOrderDetail.do?dlvNo=${srcNo}`,
                  }
                  return map[code] || null
                }
                const sourcingUrl = buildSourcingOrderUrl(j.site, j.sourcingOrderNumber || '', j.actionTag || '')
                return (
                  <div key={j.id} style={{
                    display: 'grid', gridTemplateColumns: '36px 88px 110px 150px 160px 200px 80px 140px 90px 90px 120px 266px',
                    padding: '6px 10px', borderTop: '1px solid #1f2937', fontSize: 12,
                  }}>
                    <div style={{ color: '#6b7280', fontSize: 11 }}>{fmtNum(idx + 1)}</div>
                    <div>
                      <span style={{
                        padding: '2px 6px', borderRadius: 3, fontSize: 10, fontWeight: 700,
                        background: statusColor[j.status] || '#374151', color: '#fff',
                      }}>{j.status}</span>
                    </div>
                    <div style={{ fontSize: 11, color: '#9ca3af' }}>
                      {j.paidAt ? new Date(j.paidAt).toLocaleString('ko-KR', { year: '2-digit', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }) : '-'}
                    </div>
                    <div style={{ fontFamily: 'monospace', fontSize: 11 }}>{j.orderNumber || j.orderId}</div>
                    <div>{j.customerName || '-'}</div>
                    <div>{j.channelName || '-'}</div>
                    <div>{j.site}</div>
                    <div style={{ fontFamily: 'monospace', fontSize: 11 }}>{j.sourcingOrderNumber || '-'}</div>
                    <div>{j.sourcingAccountLabel || '-'}</div>
                    <div>{j.courier || '-'}</div>
                    <div style={{ fontFamily: 'monospace' }}>{j.tracking || '-'}</div>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 6, minWidth: 0 }}>
                      <span style={{ color: '#9ca3af', fontSize: 11, flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={j.lastError || ''}>{j.lastError || ''}</span>
                      <button
                        onClick={() => {
                          if (!sourcingUrl) {
                            setLogMessages(prev => [...prev, `[원주문링크] ${j.site} 소싱처는 지원하지 않거나 소싱주문번호가 없습니다`])
                            return
                          }
                          window.open(sourcingUrl, '_blank')
                        }}
                        disabled={!sourcingUrl}
                        style={{
                          padding: '2px 6px', fontSize: 10, borderRadius: 3,
                          background: sourcingUrl ? '#374151' : '#1f2937',
                          color: sourcingUrl ? '#e5e7eb' : '#4b5563',
                          border: '1px solid #4b5563',
                          cursor: sourcingUrl ? 'pointer' : 'not-allowed',
                          whiteSpace: 'nowrap', flexShrink: 0,
                        }}
                      >원주문링크</button>
                    </div>
                  </div>
                )
              })}
              {(!trackingStatusData?.recent || trackingStatusData.recent.length === 0) && (
                <div style={{ padding: 20, textAlign: 'center', color: '#6b7280', fontSize: 12 }}>
                  아직 적재된 송장 잡이 없습니다.
                </div>
              )}
            </div>

            <div style={{ marginTop: 12, fontSize: 11, color: '#6b7280' }}>
              💡 송장수집 클릭 시점의 미입력건 큐잉 상태입니다. 자동 갱신 안 함 — 결과는 주문 테이블에서 확인하세요.
              <div style={{ marginTop: 4 }}>
                <span style={{ color: '#f59e0b' }}>미발송</span> = 소싱처에 송장 아직 미도착(시간 지나면 자동 재시도) ·{' '}
                <span style={{ color: '#fb923c' }}>계정불일치</span> = 현재 로그인된 소싱처 계정과 주문 계정이 다름(해당 계정 PC에서 재시도 필요)
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

