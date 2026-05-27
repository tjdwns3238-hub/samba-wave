'use client'

import { useEffect, useState, useCallback, useRef, useMemo } from 'react'
import { useRouter } from 'next/navigation'
import {
  collectorApi,
  policyApi,
  proxyApi,
  accountApi,
  type SambaSearchFilter,
  type SambaPolicy,
  type SambaMarketAccount,
  type RefreshResult,
} from '@/lib/samba/api/commerce'
import { fetchWithAuth, API_BASE } from '@/lib/samba/api/shared'
import { type AISourcingResult } from '@/lib/samba/api/operations'
import { showAlert, showConfirm } from '@/components/samba/Modal'
import { fmtTime } from '@/lib/samba/utils'
import { fmtNum, fmtTextNumbers } from '@/lib/samba/styles'
import AiJobModal from './components/AiJobModal'
import DeleteJobModal from './components/DeleteJobModal'
import MappingModal from './components/MappingModal'
import TagPreviewModal from './components/TagPreviewModal'
import AiSourcingModal from './components/AiSourcingModal'
import SourcingUrlPanel from './components/SourcingUrlPanel'
import DuplicatesModal from './components/DuplicatesModal'
import DrilldownGroupTable from './components/DrilldownGroupTable'
import AiToolsPanel from './components/AiToolsPanel'
import CollectorStatusPanel from './components/CollectorStatusPanel'
import useProxyAuth from './hooks/useProxyAuth'
import useAiTools from './hooks/useAiTools'
import { performCollectGroups, performStopCollect } from './utils/collectActions'
import { performBrandRefresh } from './utils/brandRefreshAction'
import { performCreateGroup } from './utils/createGroupAction'
import { performDeleteSelectedGroups } from './utils/groupActions'
import { useDisplayedFilters, parseGroupName } from './hooks/useDisplayedFilters'
import { performAiTagPreview, performClearAiTags } from './utils/aiTagActions'
import { performSyncRequestedCounts } from './utils/syncRequestedCountsAction'
import { useCollectLogPolling } from './hooks/useCollectLogPolling'
import { performHandleCreateGroup, performHandleBrandConfirm } from './utils/groupCreateHandlers'
import { useCollectQueuePolling } from './hooks/useCollectQueuePolling'
import RefreshResultModal from './components/RefreshResultModal'

export default function CollectorPage() {
  useEffect(() => {
    document.title = 'SAMBA-상품수집'
  }, [])
  const router = useRouter()
  const [filters, setFilters] = useState<SambaSearchFilter[]>([])
  const [policies, setPolicies] = useState<SambaPolicy[]>([])
  const [, setLoading] = useState(true)

  // URL collect
  const [collectUrl, setCollectUrl] = useState('')
  const [collecting, setCollecting] = useState(false)
  const [collectLog, setCollectLog] = useState<string[]>(['[대기] 수집 결과가 여기에 표시됩니다...'])
  const [selectedSite, setSelectedSite] = useState('MUSINSA')
  const [checkedOptions, setCheckedOptions] = useState<Record<string, boolean>>({
    excludePreorder: true,
    excludeBoutique: true,
    maxDiscount: true,
  })

  // 무신사 브랜드 선택 모달
  const [brandSearchResults, setBrandSearchResults] = useState<Array<{ brandCode: string; brandName: string }>>([])
  const [showMusinsaBrandModal, setShowMusinsaBrandModal] = useState(false)
  const [pendingKeyword, setPendingKeyword] = useState('')
  const [detectedBrandCode, setDetectedBrandCode] = useState('')
  const [selectedBrandCodes, setSelectedBrandCodes] = useState<Set<string>>(new Set())
  const [brandModalAction, setBrandModalAction] = useState<'scan' | 'create'>('create')
  const pendingScanGf = useRef('A')

  // 카테고리 자동분류 옵션
  const [brandScanning, setBrandScanning] = useState(false)
  const [brandCategories, setBrandCategories] = useState<
    { categoryCode: string; path: string; count: number; category1: string; category2: string; category3: string }[]
  >([])
  const [brandSelectedCats, setBrandSelectedCats] = useState<Set<string>>(new Set())
  const [brandTotal, setBrandTotal] = useState(0)

  // 롯데ON 브랜드 선택 모달
  const [showBrandModal, setShowBrandModal] = useState(false)
  const [brandModalList, setBrandModalList] = useState<{ name: string; count: number; id?: string }[]>([])
  const [brandModalSelected, setBrandModalSelected] = useState<Set<string>>(new Set())
  const [brandModalKeyword, setBrandModalKeyword] = useState('')
  const [brandModalParsed, setBrandModalParsed] = useState<{ brand: string; keyword: string; gf: string } | null>(null)

  // 일괄 갱신
  const [refreshResult] = useState<RefreshResult | null>(null)
  const [showRefreshModal, setShowRefreshModal] = useState(false)
  // AI 도구 관련 상태(태그 미리보기/이미지 변환/작업 진행 모달/AI 비용)는 useAiTools 훅으로 추출
  const {
    lastAiUsage,
    setLastAiUsage,
    showTagPreview,
    setShowTagPreview,
    tagPreviews,
    setTagPreviews,
    tagPreviewCost,
    setTagPreviewCost,
    tagPreviewLoading,
    setTagPreviewLoading,
    removedTags,
    setRemovedTags,
    aiImgScope,
    setAiImgScope,
    aiImgMode,
    setAiImgMode,
    aiModelPreset,
    setAiModelPreset,
    aiImgTransforming,
    setAiImgTransforming,
    aiPresetList,
    aiJobModal,
    setAiJobModal,
    aiJobTitle,
    setAiJobTitle,
    aiJobLogs,
    setAiJobLogs,
    aiJobDone,
    setAiJobDone,
    aiJobAbortRef,
  } = useAiTools()

  // 그룹 삭제 진행 모달
  const [deleteJobModal, setDeleteJobModal] = useState(false)
  const [deleteJobLogs, setDeleteJobLogs] = useState<string[]>([])
  const [deleteJobDone, setDeleteJobDone] = useState(false)

  // 이미지 필터링 (모델컷/연출컷/배너 제거)
  const [imgFiltering, setImgFiltering] = useState(false)
  const [imgFilterScopes, setImgFilterScopes] = useState<Set<string>>(new Set(['detail_images']))

  // 중복 상품 모달
  const [showDuplicatesModal, setShowDuplicatesModal] = useState(false)

  // 카테고리 매핑 모달
  const [showMappingModal, setShowMappingModal] = useState(false)
  const [mappingFilter, setMappingFilter] = useState<SambaSearchFilter | null>(null)
  const [mappingData, setMappingData] = useState<Record<string, string>>({})
  const [mappingLoading, setMappingLoading] = useState(false)
  const [, setAccounts] = useState<SambaMarketAccount[]>([])

  // Proxy & auth status
  const { proxyStatus, proxyText, musinsaAuth, musinsaAuthText, musinsaCookieUpdatedAt, musinsaAccount, poolInfo, setProxyStatus, setProxyText } =
    useProxyAuth()

  // 트리 + 드릴다운
  const [tree, setTree] = useState<SambaSearchFilter[]>([])
  const [treeCountsLoading, setTreeCountsLoading] = useState(false)
  const loadedSitesRef = useRef<Set<string>>(new Set())
  const [drillSite, setDrillSite] = useState<string | null>(null)
  const [drillBrand, setDrillBrand] = useState<string | null>(null)
  const [drillGroup, setDrillGroup] = useState<string | null>(null)
  const [drillEntry, setDrillEntry] = useState<'site' | 'brand' | null>('site')

  // Group table filters
  const [siteFilter] = useState('')
  const [aiFilter] = useState('')
  const [collectFilter, setCollectFilter] = useState('')
  const [marketRegFilter, setMarketRegFilter] = useState('')
  const [tagRegFilter, setTagRegFilter] = useState('')
  const [policyRegFilter, setPolicyRegFilter] = useState('')
  const [sortBy] = useState('lastCollectedAt_desc')
  const [selectAll, setSelectAll] = useState(false)
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set())

  // AI 소싱기 상태
  const [showAiSourcingModal, setShowAiSourcingModal] = useState(false)
  const [aiSourcingStep, setAiSourcingStep] = useState<'config' | 'analyzing' | 'confirm'>('config')
  const [aiMonth, setAiMonth] = useState(new Date().getMonth() + 1) // 현재월
  const [aiMainCategory, setAiMainCategory] = useState('패션의류')
  const [aiExcelFile, setAiExcelFile] = useState<File | null>(null)
  const [aiTargetCount, setAiTargetCount] = useState(10000)
  const [aiAnalyzing, setAiAnalyzing] = useState(false)
  const [aiLogs, setAiLogs] = useState<string[]>([])
  const [aiResult, setAiResult] = useState<AISourcingResult | null>(null)
  const [aiSelectedCombos, setAiSelectedCombos] = useState<Set<number>>(new Set())
  const [aiExcludedBrands, setAiExcludedBrands] = useState<Set<string>>(new Set())
  const [aiExcludedKeywords, setAiExcludedKeywords] = useState<Set<string>>(new Set())
  const [aiMinCount, setAiMinCount] = useState(0) // 최소 상품수 필터
  const [aiCreating, setAiCreating] = useState(false)
  const [aiSourceSite, setAiSourceSite] = useState('MUSINSA') // 수집 소싱처 선택

  const logRef = useRef<HTMLDivElement>(null)
  const collectAbortRef = useRef<AbortController | null>(null)
  const manualCollectRef = useRef(false)

  const load = useCallback(async () => {
    setLoading(true)
    const pol = await policyApi.list().catch(() => [])
    setPolicies(pol)
    setLoading(false)
  }, [])

  const mergeCountsIntoTree = useCallback((counts: Record<string, Partial<SambaSearchFilter>>) => {
    const mergeTree = (nodes: SambaSearchFilter[]): SambaSearchFilter[] =>
      nodes.map((n) => {
        if (!n.is_folder && counts[n.id]) return { ...n, ...counts[n.id] }
        if (n.children?.length) return { ...n, children: mergeTree(n.children) }
        return n
      })
    setTree((prev) => mergeTree(prev))
    setFilters((prev) => prev.map((f) => (counts[f.id] ? { ...f, ...counts[f.id] } : f)))
  }, [])

  // 모든 사이트의 카운트를 단일 호출로 prefetch — 그룹 클릭 전에도 (N) 표기
  const loadAllCounts = useCallback(async () => {
    setTreeCountsLoading(true)
    try {
      const counts = await collectorApi.getFilterTreeCounts()
      mergeCountsIntoTree(counts)
      // prefetch 완료된 사이트는 이후 클릭 시 재호출 skip
      // — 사이트 ID 가 아닌 source_site 값으로 추적되므로 트리에서 추출
      setTree((prev) => {
        for (const site of prev) {
          const ss = site.source_site || site.name
          if (ss) loadedSitesRef.current.add(ss)
        }
        return prev
      })
    } catch {
      /* 카운트 prefetch 실패 무시 — 클릭 시 lazy load 로 복원 */
    }
    setTreeCountsLoading(false)
  }, [mergeCountsIntoTree])

  const loadTree = useCallback(async () => {
    try {
      const data = await collectorApi.getFilterTree()
      setTree(data)
      loadedSitesRef.current = new Set()
      // 트리에서 리프 노드를 flat하게 추출 — /filters API 호출 대체
      const leaves: SambaSearchFilter[] = []
      const walk = (nodes: SambaSearchFilter[]) => {
        for (const n of nodes) {
          if (!n.is_folder) leaves.push(n)
          if (n.children?.length) walk(n.children)
        }
      }
      walk(data)
      setFilters(leaves)
      // 트리 로드 직후 모든 사이트 카운트 한 번에 prefetch
      await loadAllCounts()
    } catch {
      /* 트리 로드 실패 무시 */
    }
  }, [loadAllCounts])

  const loadSiteCounts = useCallback(
    async (sourceSite: string) => {
      if (loadedSitesRef.current.has(sourceSite)) return
      loadedSitesRef.current.add(sourceSite)
      setTreeCountsLoading(true)
      try {
        const counts = await collectorApi.getFilterTreeCounts(sourceSite)
        mergeCountsIntoTree(counts)
      } catch {
        /* 카운트 로드 실패 무시 */
      }
      setTreeCountsLoading(false)
    },
    [mergeCountsIntoTree],
  )

  useEffect(() => {
    if (!drillSite || !tree.length) return
    const siteNode = tree.find((s) => s.id === drillSite)
    const sourceSite = siteNode?.source_site || siteNode?.name
    if (sourceSite) loadSiteCounts(sourceSite)
  }, [drillSite, tree, loadSiteCounts])

  useEffect(() => {
    load()
    loadTree()
  }, [load, loadTree])
  useEffect(() => {
    accountApi
      .list()
      .then(setAccounts)
      .catch(() => {})
  }, [])
  const addLog = useCallback((msg: string) => {
    const time = fmtTime()
    const line = `[${time}] ${msg}`
    setCollectLog((prev) => [...prev, line].slice(-30))
    setTimeout(() => {
      if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight
    }, 50)
  }, [])

  const { collectLogSinceRef } = useCollectLogPolling({
    collecting,
    brandScanning,
    setCollecting,
    setCollectLog,
    load,
    logRef,
    manualCollectRef,
  })

  const collectQueueStatus = useCollectQueuePolling()
  const [cancellingCollectJobIds, setCancellingCollectJobIds] = useState<string[]>([])

  const handleCancelCollectJob = async (jobId: string) => {
    setCancellingCollectJobIds((prev) => [...prev, jobId])
    try {
      await fetchWithAuth(`${API_BASE}/api/v1/samba/jobs/${jobId}`, { method: 'DELETE' })
    } catch {
      /* 무시 */
    } finally {
      setCancellingCollectJobIds((prev) => prev.filter((id) => id !== jobId))
    }
  }

  const executeCreateGroup = (brandCode?: string) =>
    performCreateGroup({
      brandCode,
      collectUrl,
      selectedSite,
      checkedOptions,
      setCollecting,
      setCollectUrl,
      addLog,
      load,
      loadTree,
    })

  // URL → 그룹 생성 (무신사 평문 키워드 시 브랜드 검색 먼저)
  const handleCreateGroup = () =>
    performHandleCreateGroup({
      collectUrl,
      selectedSite,
      setCollecting,
      addLog,
      setPendingKeyword,
      setBrandSearchResults,
      setSelectedBrandCodes,
      setBrandModalAction,
      setShowMusinsaBrandModal,
      executeCreateGroup,
    })

  const handleBrandConfirm = (codes: Set<string>) =>
    performHandleBrandConfirm({
      codes,
      setShowMusinsaBrandModal,
      setBrandSearchResults,
      setDetectedBrandCode,
      brandModalAction,
      setBrandScanning,
      pendingKeyword,
      pendingScanGf,
      addLog,
      setBrandCategories,
      setBrandTotal,
      setBrandSelectedCats,
      executeCreateGroup,
    })

  // 그룹 삭제 후 drill 상태 초기화 + 트리 리로드 — 그렇지 않으면 사용자가 머물던
  // 사이트(예: LotteON) 에 drill 이 그대로 남아 새 트리에서도 그 사이트만 보임.
  const reloadAfterDelete = useCallback(async () => {
    setDrillSite(null)
    setDrillBrand(null)
    setDrillGroup(null)
    setDrillEntry('site')
    await loadTree()
  }, [loadTree])

  const handleDeleteSelectedGroups = () =>
    performDeleteSelectedGroups({
      displayedFilters,
      selectedIds,
      drillBrand,
      filters,
      siteFilter,
      setDeleteJobLogs,
      setDeleteJobDone,
      setDeleteJobModal,
      setSelectedIds,
      setSelectAll,
      load,
      loadTree: reloadAfterDelete,
    })

  const handleCollectGroups = async () => {
    await performCollectGroups({
      drillGroup,
      displayedFilters,
      selectedIds,
      filters,
      checkedOptions,
      collectAbortRef,
      manualCollectRef,
      setCollecting,
      addLog,
      load,
      loadTree,
    })
    await syncRequestedCounts()
  }

  const syncRequestedCounts = async () => {
    /* 자동 동기화 제거 — 사용자 설정값 보존 */
  }

  const handleStopCollect = () => performStopCollect({ collectAbortRef, addLog, setCollecting })

  const handleClearLog = () => {
    setCollectLog(['로그가 초기화되었습니다.'])
    collectLogSinceRef.current = 0
    fetchWithAuth(`${API_BASE}/api/v1/samba/jobs/collect-logs/clear`, { method: 'POST' }).catch(() => {})
  }
  const handleCopyLog = () => {
    navigator.clipboard.writeText(collectLog.join('\n')).catch(() => {})
  }

  const handlePolicyApply = async (filterId: string, policyId: string) => {
    try {
      await collectorApi.updateFilter(filterId, { applied_policy_id: policyId } as Partial<SambaSearchFilter>)
    } catch (e) {
      console.error('정책 적용 실패:', e)
      showAlert('정책 적용에 실패했습니다.', 'error')
      return
    }
    load()
    loadTree()
  }

  // 요청상품수 수정
  const handleUpdateRequestedCount = async (filterId: string, count: number) => {
    if (isNaN(count) || count < 1) return
    try {
      await collectorApi.updateFilter(filterId, { requested_count: count })
    } catch (e) {
      console.error('요청수 변경 실패:', e)
      showAlert('요청수 변경에 실패했습니다.', 'error')
      return
    }
    // loadTree() 대신 state 직접 업데이트 — loadTree는 5분 캐시로 stale 값(0)을 반환해 덮어쓰는 버그 방지
    setFilters((prev) => prev.map((f) => (f.id === filterId ? { ...f, requested_count: count } : f)))
  }

  // 수집상품수 클릭 → 상품관리 이동
  const handleGoToProducts = (f: SambaSearchFilter) => {
    const count = (f as unknown as Record<string, number>).collected_count ?? 0
    if (count === 0) return
    router.push(`/samba/products?search_filter_id=${f.id}&group_name=${encodeURIComponent(f.name)}`)
  }

  const displayedFilters = useDisplayedFilters({
    filters,
    tree,
    siteFilter,
    drillSite,
    drillBrand,
    aiFilter,
    collectFilter,
    marketRegFilter,
    tagRegFilter,
    policyRegFilter,
    sortBy,
  })

  // 중복상품 모달 필터: 드릴다운 기준만 사용 (selectedSite 탭은 무관)
  const _activeSite = drillSite ? tree.find((s) => s.id === drillSite)?.source_site : undefined
  const _modalFilterIds = drillBrand ? displayedFilters.map((f) => f.id) : undefined

  // 드롭다운 필터 변경 시 drillBrand 활성 상태면 selectedIds를 displayedFilters 기준으로 재동기화

  // 드릴다운 테이블 추가수집 핸들러 (브랜드/카테고리 단위)
  const handleBrandRefresh = () =>
    performBrandRefresh({
      displayedFilters,
      drillBrand,
      drillGroup,
      selectedIds,
      filters,
      checkedOptions,
      collectAbortRef,
      manualCollectRef,
      setCollecting,
      addLog,
      load,
      loadTree,
    })

  const handleAiTagPreview = () =>
    performAiTagPreview({
      selectAll,
      selectedIds,
      displayedFilters,
      setTagPreviewLoading,
      addLog,
      setTagPreviews,
      setTagPreviewCost,
      setRemovedTags,
      setShowTagPreview,
    })
  const handleClearAiTags = () =>
    performClearAiTags({
      selectAll,
      selectedIds,
      displayedFilters,
      addLog,
    })
  const handleSyncRequestedCounts = () =>
    performSyncRequestedCounts({
      selectedIds,
      displayedFilters,
      load,
      loadTree,
    })

  return (
    <div style={{ color: '#E5E5E5' }}>
      <div style={{ display: 'flex', flexDirection: 'column', gap: '0', padding: '0.5rem 1rem' }}>
        <CollectorStatusPanel
          section="status"
          proxyStatus={proxyStatus}
          proxyText={proxyText}
          musinsaAuth={musinsaAuth}
          musinsaAuthText={musinsaAuthText}
          musinsaCookieUpdatedAt={musinsaCookieUpdatedAt}
          musinsaAccount={musinsaAccount}
          poolInfo={poolInfo}
          setProxyStatus={setProxyStatus}
          setProxyText={setProxyText}
        />

        <SourcingUrlPanel
          selectedSite={selectedSite}
          setSelectedSite={setSelectedSite}
          collectUrl={collectUrl}
          setCollectUrl={setCollectUrl}
          checkedOptions={checkedOptions}
          setCheckedOptions={setCheckedOptions}
          brandScanning={brandScanning}
          setBrandScanning={setBrandScanning}
          brandCategories={brandCategories}
          setBrandCategories={setBrandCategories}
          brandSelectedCats={brandSelectedCats}
          setBrandSelectedCats={setBrandSelectedCats}
          brandTotal={brandTotal}
          setBrandTotal={setBrandTotal}
          detectedBrandCode={detectedBrandCode}
          setDetectedBrandCode={setDetectedBrandCode}
          setShowAiSourcingModal={setShowAiSourcingModal}
          setAiSourcingStep={setAiSourcingStep}
          setAiResult={setAiResult}
          setAiLogs={setAiLogs}
          setAiSelectedCombos={setAiSelectedCombos}
          setAiExcludedBrands={setAiExcludedBrands}
          setAiExcludedKeywords={setAiExcludedKeywords}
          showMusinsaBrandModal={showMusinsaBrandModal}
          setShowMusinsaBrandModal={setShowMusinsaBrandModal}
          brandSearchResults={brandSearchResults}
          setBrandSearchResults={setBrandSearchResults}
          pendingKeyword={pendingKeyword}
          setPendingKeyword={setPendingKeyword}
          selectedBrandCodes={selectedBrandCodes}
          setSelectedBrandCodes={setSelectedBrandCodes}
          setBrandModalAction={setBrandModalAction}
          pendingScanGf={pendingScanGf}
          handleBrandConfirm={handleBrandConfirm}
          showBrandModal={showBrandModal}
          setShowBrandModal={setShowBrandModal}
          brandModalList={brandModalList}
          setBrandModalList={setBrandModalList}
          brandModalSelected={brandModalSelected}
          setBrandModalSelected={setBrandModalSelected}
          brandModalKeyword={brandModalKeyword}
          setBrandModalKeyword={setBrandModalKeyword}
          brandModalParsed={brandModalParsed}
          setBrandModalParsed={setBrandModalParsed}
          collecting={collecting}
          setCollecting={setCollecting}
          handleCreateGroup={handleCreateGroup}
          load={load}
          loadTree={loadTree}
          addLog={addLog}
        />

        <CollectorStatusPanel
          section="log"
          collectLog={collectLog}
          collecting={collecting}
          collectQueueStatus={collectQueueStatus}
          cancellingJobIds={cancellingCollectJobIds}
          logRef={logRef}
          handleStopCollect={handleStopCollect}
          handleCancelCollectJob={handleCancelCollectJob}
          handleCopyLog={handleCopyLog}
          handleClearLog={handleClearLog}
          parseGroupName={parseGroupName}
        />

        <AiToolsPanel
          lastAiUsage={lastAiUsage}
          aiImgScope={aiImgScope}
          aiImgMode={aiImgMode}
          aiModelPreset={aiModelPreset}
          aiPresetList={aiPresetList}
          aiImgTransforming={aiImgTransforming}
          imgFiltering={imgFiltering}
          imgFilterScopes={imgFilterScopes}
          selectedIds={selectedIds}
          displayedFilters={displayedFilters}
          tree={tree}
          aiJobAbortRef={aiJobAbortRef}
          setAiImgScope={setAiImgScope}
          setAiImgMode={setAiImgMode}
          setAiModelPreset={setAiModelPreset}
          setAiImgTransforming={setAiImgTransforming}
          setImgFiltering={setImgFiltering}
          setImgFilterScopes={setImgFilterScopes}
          setSelectedIds={setSelectedIds}
          setSelectAll={setSelectAll}
          setLastAiUsage={setLastAiUsage}
          setAiJobModal={setAiJobModal}
          setAiJobTitle={setAiJobTitle}
          setAiJobLogs={setAiJobLogs}
          setAiJobDone={setAiJobDone}
          load={load}
          loadTree={loadTree}
        />

        <DrilldownGroupTable
          filters={filters}
          tree={tree}
          policies={policies}
          drillSite={drillSite}
          drillBrand={drillBrand}
          drillGroup={drillGroup}
          drillEntry={drillEntry}
          setDrillSite={setDrillSite}
          setDrillBrand={setDrillBrand}
          setDrillGroup={setDrillGroup}
          setDrillEntry={setDrillEntry}
          collectFilter={collectFilter}
          marketRegFilter={marketRegFilter}
          tagRegFilter={tagRegFilter}
          policyRegFilter={policyRegFilter}
          setCollectFilter={setCollectFilter}
          setMarketRegFilter={setMarketRegFilter}
          setTagRegFilter={setTagRegFilter}
          setPolicyRegFilter={setPolicyRegFilter}
          selectedIds={selectedIds}
          setSelectedIds={setSelectedIds}
          setShowDuplicatesModal={setShowDuplicatesModal}
          setShowMappingModal={setShowMappingModal}
          setMappingFilter={setMappingFilter}
          setMappingData={setMappingData}
          treeCountsLoading={treeCountsLoading}
          tagPreviewLoading={tagPreviewLoading}
          handleDeleteSelectedGroups={handleDeleteSelectedGroups}
          handleCollectGroups={handleCollectGroups}
          handlePolicyApply={handlePolicyApply}
          handleUpdateRequestedCount={handleUpdateRequestedCount}
          handleGoToProducts={handleGoToProducts}
          handleBrandRefresh={handleBrandRefresh}
          handleAiTagPreview={handleAiTagPreview}
          handleClearAiTags={handleClearAiTags}
          handleSyncRequestedCounts={handleSyncRequestedCounts}
          parseGroupName={parseGroupName}
          load={load}
          loadTree={loadTree}
        />

        <RefreshResultModal open={showRefreshModal} result={refreshResult} onClose={() => setShowRefreshModal(false)} />

        <MappingModal
          open={showMappingModal}
          filter={mappingFilter}
          mappingData={mappingData}
          mappingLoading={mappingLoading}
          setMappingData={setMappingData}
          setMappingLoading={setMappingLoading}
          onClose={() => setShowMappingModal(false)}
          onSaved={() => {
            load()
            loadTree()
          }}
        />

        {/* AI 태그 미리보기 모달 */}
        <TagPreviewModal
          open={showTagPreview}
          tagPreviews={tagPreviews}
          tagPreviewCost={tagPreviewCost}
          removedTags={removedTags}
          setTagPreviews={setTagPreviews}
          setRemovedTags={setRemovedTags}
          setLastAiUsage={setLastAiUsage}
          setSelectedIds={setSelectedIds}
          setSelectAll={setSelectAll}
          onClose={() => setShowTagPreview(false)}
          onApplied={() => {
            load()
            loadTree()
          }}
        />

        {/* AI 소싱기 모달 */}
        <AiSourcingModal
          open={showAiSourcingModal}
          aiSourcingStep={aiSourcingStep}
          aiMonth={aiMonth}
          aiMainCategory={aiMainCategory}
          aiExcelFile={aiExcelFile}
          aiTargetCount={aiTargetCount}
          aiAnalyzing={aiAnalyzing}
          aiLogs={aiLogs}
          aiResult={aiResult}
          aiSelectedCombos={aiSelectedCombos}
          aiExcludedBrands={aiExcludedBrands}
          aiExcludedKeywords={aiExcludedKeywords}
          aiMinCount={aiMinCount}
          aiCreating={aiCreating}
          aiSourceSite={aiSourceSite}
          setAiSourcingStep={setAiSourcingStep}
          setAiMonth={setAiMonth}
          setAiMainCategory={setAiMainCategory}
          setAiExcelFile={setAiExcelFile}
          setAiTargetCount={setAiTargetCount}
          setAiAnalyzing={setAiAnalyzing}
          setAiLogs={setAiLogs}
          setAiResult={setAiResult}
          setAiSelectedCombos={setAiSelectedCombos}
          setAiExcludedBrands={setAiExcludedBrands}
          setAiExcludedKeywords={setAiExcludedKeywords}
          setAiMinCount={setAiMinCount}
          setAiCreating={setAiCreating}
          setAiSourceSite={setAiSourceSite}
          onClose={() => setShowAiSourcingModal(false)}
          onCreated={() => {
            load()
            loadTree()
          }}
        />
      </div>
      {/* 그룹 삭제 진행 모달 */}
      <DeleteJobModal
        open={deleteJobModal}
        logs={deleteJobLogs}
        done={deleteJobDone}
        onClose={() => setDeleteJobModal(false)}
      />
      {/* AI 작업 진행 모달 */}
      <AiJobModal
        open={aiJobModal}
        title={aiJobTitle}
        logs={aiJobLogs}
        done={aiJobDone}
        abortRef={aiJobAbortRef}
        onClose={() => setAiJobModal(false)}
      />
      {/* 중복 상품 모달 */}
      <DuplicatesModal
        open={showDuplicatesModal}
        sourceSite={_activeSite}
        filterIds={_modalFilterIds}
        onClose={() => setShowDuplicatesModal(false)}
        onDeleted={() => {
          load()
          reloadAfterDelete()
        }}
      />
    </div>
  )
}
