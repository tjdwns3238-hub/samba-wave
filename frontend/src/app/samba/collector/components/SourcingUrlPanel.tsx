'use client'

import type { Dispatch, SetStateAction } from 'react'
import { fmtNum } from '@/lib/samba/styles'
import { collectorApi, proxyApi } from '@/lib/samba/api/commerce'
import { showAlert } from '@/components/samba/Modal'
import { SITES, SITE_OPTIONS } from '../constants'
import MusinsaBrandModal from './MusinsaBrandModal'
import LotteOnBrandModal from './LotteOnBrandModal'

type BrandCategory = { categoryCode: string; path: string; count: number; category1: string; category2: string; category3: string }
type BrandModalEntry = { name: string; count: number; id?: string }
type BrandModalParsed = { brand: string; keyword: string; gf: string } | null
type MusinsaBrand = { brandCode: string; brandName: string }
const FIXED_REQUESTED_COUNT = 1000

interface SourcingUrlPanelProps {
  selectedSite: string
  setSelectedSite: Dispatch<SetStateAction<string>>
  collectUrl: string
  setCollectUrl: Dispatch<SetStateAction<string>>
  checkedOptions: Record<string, boolean>
  setCheckedOptions: Dispatch<SetStateAction<Record<string, boolean>>>
  brandScanning: boolean
  setBrandScanning: Dispatch<SetStateAction<boolean>>
  brandCategories: BrandCategory[]
  setBrandCategories: Dispatch<SetStateAction<BrandCategory[]>>
  brandSelectedCats: Set<string>
  setBrandSelectedCats: Dispatch<SetStateAction<Set<string>>>
  brandTotal: number
  setBrandTotal: Dispatch<SetStateAction<number>>
  detectedBrandCode: string
  setDetectedBrandCode: Dispatch<SetStateAction<string>>

  // AI 소싱 모달 트리거
  setShowAiSourcingModal: Dispatch<SetStateAction<boolean>>
  setAiSourcingStep: Dispatch<SetStateAction<'config' | 'analyzing' | 'confirm'>>
  setAiResult: (v: null) => void
  setAiLogs: Dispatch<SetStateAction<string[]>>
  setAiSelectedCombos: Dispatch<SetStateAction<Set<number>>>
  setAiExcludedBrands: Dispatch<SetStateAction<Set<string>>>
  setAiExcludedKeywords: Dispatch<SetStateAction<Set<string>>>

  // 무신사 브랜드 모달 상태
  showMusinsaBrandModal: boolean
  setShowMusinsaBrandModal: Dispatch<SetStateAction<boolean>>
  brandSearchResults: MusinsaBrand[]
  setBrandSearchResults: Dispatch<SetStateAction<MusinsaBrand[]>>
  pendingKeyword: string
  setPendingKeyword: Dispatch<SetStateAction<string>>
  selectedBrandCodes: Set<string>
  setSelectedBrandCodes: Dispatch<SetStateAction<Set<string>>>
  setBrandModalAction: Dispatch<SetStateAction<'scan' | 'create'>>
  pendingScanGf: React.MutableRefObject<string>
  handleBrandConfirm: (codes: Set<string>) => void

  // 롯데ON / SSG 브랜드 모달 상태
  showBrandModal: boolean
  setShowBrandModal: Dispatch<SetStateAction<boolean>>
  brandModalList: BrandModalEntry[]
  setBrandModalList: Dispatch<SetStateAction<BrandModalEntry[]>>
  brandModalSelected: Set<string>
  setBrandModalSelected: Dispatch<SetStateAction<Set<string>>>
  brandModalKeyword: string
  setBrandModalKeyword: Dispatch<SetStateAction<string>>
  brandModalParsed: BrandModalParsed
  setBrandModalParsed: Dispatch<SetStateAction<BrandModalParsed>>

  // 수집 상태/액션
  collecting: boolean
  setCollecting: Dispatch<SetStateAction<boolean>>
  handleCreateGroup: () => void
  load: () => void
  loadTree: () => void
  addLog: (msg: string) => void
}

export default function SourcingUrlPanel(props: SourcingUrlPanelProps) {
  const {
    selectedSite, setSelectedSite,
    collectUrl, setCollectUrl,
    checkedOptions, setCheckedOptions,
    brandScanning, setBrandScanning,
    brandCategories, setBrandCategories,
    brandSelectedCats, setBrandSelectedCats,
    brandTotal, setBrandTotal,
    detectedBrandCode, setDetectedBrandCode,
    setShowAiSourcingModal, setAiSourcingStep, setAiResult, setAiLogs,
    setAiSelectedCombos, setAiExcludedBrands, setAiExcludedKeywords,
    showMusinsaBrandModal, setShowMusinsaBrandModal,
    brandSearchResults, pendingKeyword, setPendingKeyword,
    selectedBrandCodes, setSelectedBrandCodes,
    setBrandSearchResults, setBrandModalAction, pendingScanGf, handleBrandConfirm,
    showBrandModal, setShowBrandModal,
    brandModalList, brandModalSelected, brandModalKeyword, brandModalParsed,
    setBrandModalSelected,
    collecting, setCollecting,
    handleCreateGroup, load, loadTree, addLog,
  } = props

  return (
    <>
      {/* 소싱처 선택 + URL 입력 영역 */}
      <div style={{
        background: 'rgba(30,30,30,0.5)', border: '1px solid #2D2D2D', borderRadius: '8px',
        padding: '1.25rem', marginBottom: '1rem',
      }}>
        {/* 소싱처 선택 버튼 */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: '8px', marginBottom: '0.875rem' }}>
          {/* 1행: 소싱처 버튼 + AI소싱기 */}
          <div style={{ display: 'flex', gap: '6px', flexWrap: 'wrap', alignItems: 'center' }}>
            {SITES.map((site) => (
              <button
                key={site.id}
                disabled={site.disabled}
                onClick={() => {
                  if (site.disabled) return
                  setSelectedSite(site.id)
                  setCollectUrl('')
                  setCheckedOptions(Object.fromEntries(
                    (SITE_OPTIONS[site.id] || []).map(opt => [opt.id, opt.id === 'includeSoldOut' ? false : true])
                  ))
                }}
                style={{
                  padding: '0.35rem 0.875rem', borderRadius: '20px', fontSize: '0.8rem',
                  fontWeight: selectedSite === site.id ? 700 : 400,
                  cursor: site.disabled ? 'not-allowed' : 'pointer',
                  border: site.disabled ? '1px solid #2A2A2A' : selectedSite === site.id ? '1px solid #FF8C00' : '1px solid #3D3D3D',
                  background: site.disabled ? 'transparent' : selectedSite === site.id ? 'rgba(255,140,0,0.15)' : 'transparent',
                  color: site.disabled ? '#555' : selectedSite === site.id ? '#FF8C00' : '#C5C5C5',
                  opacity: site.disabled ? 0.6 : 1,
                  transition: 'all 0.15s',
                }}
              >{site.label}{site.disabled ? ' (예정)' : ''}</button>
            ))}
            <button
              onClick={() => {
                setShowAiSourcingModal(true)
                setAiSourcingStep('config')
                setAiResult(null)
                setAiLogs([])
                setAiSelectedCombos(new Set())
                setAiExcludedBrands(new Set())
                setAiExcludedKeywords(new Set())
              }}
              style={{
                marginLeft: 'auto', padding: '0.6rem 1.2rem', borderRadius: '6px',
                background: 'linear-gradient(135deg, #6C5CE7, #A29BFE)',
                color: '#fff', fontWeight: 600, fontSize: '0.82rem',
                border: 'none', cursor: 'pointer', whiteSpace: 'nowrap',
              }}
            >
              AI 소싱기
            </button>
          </div>
          {/* 2행: 선택된 소싱처 검색 조건 체크박스 (동적) */}
          {(SITE_OPTIONS[selectedSite] || []).length > 0 && (
            <div style={{ display: 'flex', gap: '14px', paddingLeft: '4px', alignItems: 'center' }}>
              {(SITE_OPTIONS[selectedSite] || []).map((opt) => (
                <label key={opt.id} style={{ display: 'flex', alignItems: 'center', gap: '5px', cursor: 'pointer' }}>
                  <input
                    type='checkbox'
                    checked={!!checkedOptions[opt.id]}
                    onChange={(e) => setCheckedOptions((prev) => ({ ...prev, [opt.id]: e.target.checked }))}
                    style={{ accentColor: '#FF8C00', width: '13px', height: '13px', cursor: 'pointer' }}
                  />
                  <span style={{ fontSize: '0.78rem', color: '#999' }}>{opt.label}</span>
                  {opt.warn && checkedOptions[opt.id] && (
                    <span style={{ fontSize: '0.7rem', color: '#FF6B35' }}>{opt.warn}</span>
                  )}
                </label>
              ))}
            </div>
          )}
        </div>

        {/* URL 입력 */}
        <div style={{ display: 'flex', gap: '0.75rem', marginBottom: '0.625rem' }}>
          <input
            type='text'
            value={collectUrl}
            onChange={(e) => { setCollectUrl(e.target.value); setDetectedBrandCode('') }}
            onKeyDown={(e) => { if (e.key === 'Enter') e.preventDefault() }}
            placeholder={
              selectedSite === 'MUSINSA' ? '키워드 또는 URL (예: 나이키, https://www.musinsa.com/search/goods?keyword=나이키)' :
              selectedSite === 'KREAM' ? '키워드 또는 URL (예: 나이키, https://kream.co.kr/search?keyword=나이키)' :
              selectedSite === 'DANAWA' ? '키워드 또는 URL (예: 에어팟, https://search.danawa.com/dsearch.php?keyword=에어팟)' :
              selectedSite === 'FashionPlus' ? '키워드 또는 URL (예: 나이키, https://www.fashionplus.co.kr/search/goods/result?searchWord=나이키)' :
              selectedSite === 'Nike' ? '키워드 또는 URL (예: 에어포스, https://www.nike.com/kr/w?q=에어포스)' :
              selectedSite === 'Adidas' ? '키워드 또는 URL (예: 삼바, https://www.adidas.co.kr/search?q=삼바)' :
              selectedSite === 'ABCmart' ? '키워드 또는 URL (예: 나이키, https://www.a-rt.com/abc/display/search?keyword=나이키)' :
              selectedSite === 'REXMONDE' ? '키워드 또는 URL (예: 나이키, https://www.okmall.com/search?keyword=나이키)' :
              selectedSite === 'SSG' ? '키워드 또는 URL (예: 나이키, https://www.ssg.com/search.ssg?query=나이키)' :
              selectedSite === 'LOTTEON' ? '키워드 또는 URL (예: 나이키, https://www.lotteon.com/search?query=나이키)' :
              selectedSite === 'GSShop' ? '키워드 또는 URL (예: 내셔널지오그래픽, https://www.gsshop.com/search?tq=내셔널지오그래픽)' :
              selectedSite === 'ElandMall' ? '키워드 또는 URL (예: 나이키, https://www.elandmall.com/search?kwd=나이키)' :
              selectedSite === 'SSF' ? '키워드 또는 URL (예: 나이키, https://www.ssfshop.com/search?keyword=나이키)' :
              '키워드 또는 URL을 입력하세요'
            }
            style={{
              flex: 1, padding: '0.6rem 0.8rem', fontSize: '0.82rem',
              background: 'rgba(30,30,30,0.5)', border: '1px solid #2D2D2D', borderRadius: '6px',
              color: '#E5E5E5', outline: 'none',
            }}
          />
          {(selectedSite === 'MUSINSA' || selectedSite === 'LOTTEON' || selectedSite === 'GSShop' || selectedSite === 'ABCmart' || selectedSite === 'Nike' || selectedSite === 'SSG' || selectedSite === 'FashionPlus' || selectedSite === 'KREAM') && (
            <button onClick={async () => {
              if (!collectUrl.trim()) { showAlert('URL 또는 키워드를 입력하세요'); return }
              setBrandScanning(true)
              setBrandCategories([]); setBrandSelectedCats(new Set())

              const parsed = (() => { try { return new URL(collectUrl) } catch { return null } })()
              // /brand/{name}/products 경로 패턴 지원
              const pathBrandMatch = parsed?.pathname.match(/\/brand\/([^/]+)/)
              const brand = parsed?.searchParams.get('brand') || pathBrandMatch?.[1] || ''
              const keyword = parsed?.searchParams.get('keyword') || parsed?.searchParams.get('searchWord') || (!brand ? collectUrl.trim() : '')
              const gf = parsed?.searchParams.get('gf') || 'A'
              if (!brand && !keyword) { showAlert('브랜드 또는 키워드를 확인하세요'); setBrandScanning(false); return }

              // 롯데ON / SSG / 패션플러스: 브랜드 탐색 후 선택 모달 표시
              if (selectedSite === 'LOTTEON' || selectedSite === 'SSG' || selectedSite === 'FashionPlus') {
                try {
                  const discoverKeyword = keyword || brand
                  const res = await collectorApi.brandDiscover(discoverKeyword, selectedSite)
                  props.setBrandModalList(res.brands)
                  setBrandModalSelected(new Set())
                  props.setBrandModalKeyword(discoverKeyword)
                  props.setBrandModalParsed({ brand, keyword, gf })
                  setShowBrandModal(true)
                } catch (e) { showAlert(e instanceof Error ? e.message : '브랜드 탐색 실패', 'error') }
                setBrandScanning(false)
                return
              }

              // GS샵: 키워드만으로 바로 스캔 (백화점 탭) + 진행 상황 폴링
              if (selectedSite === 'GSShop') {
                const scanKeyword = keyword || brand || collectUrl.trim()
                addLog(`[카테고리스캔] GS샵 백화점 "${scanKeyword}" 스캔 시작...`)
                // 진행 상황 폴링 (3초 간격)
                const pollId = setInterval(async () => {
                  try {
                    const p = await collectorApi.gsshopScanProgress()
                    if (p.stage === 'search') {
                      addLog(`[카테고리스캔] 검색 중... ${p.page}페이지, ${fmtNum(p.products ?? 0)}개 상품 발견`)
                    } else if (p.stage === 'detail') {
                      const done = (p.detail_ok ?? 0) + (p.detail_fail ?? 0)
                      addLog(`[카테고리스캔] 상세 조회 중... ${fmtNum(done)}/${fmtNum(p.detail_total ?? 0)}건 (성공: ${fmtNum(p.detail_ok ?? 0)}, 실패: ${fmtNum(p.detail_fail ?? 0)})`)
                    }
                  } catch { /* 폴링 실패 무시 */ }
                }, 3000)
                try {
                  const res = await collectorApi.brandScan('', 'A', scanKeyword, 'GSSHOP')
                  clearInterval(pollId)
                  setBrandCategories(res.categories)
                  setBrandTotal(res.total)
                  setBrandSelectedCats(new Set(res.categories.map(c => c.categoryCode)))
                  addLog(`[카테고리스캔] 완료: ${fmtNum(res.groupCount)}개 카테고리, 총 ${fmtNum(res.total)}건`)
                } catch (e) {
                  clearInterval(pollId)
                  showAlert(e instanceof Error ? e.message : '스캔 실패', 'error')
                }
                setBrandScanning(false)
                return
              }

              // ABC마트: 키워드만으로 바로 스캔
              if (selectedSite === 'ABCmart') {
                const scanKeyword = keyword || brand || collectUrl.trim()
                addLog(`[카테고리스캔] ABC마트 "${scanKeyword}" 스캔 시작...`)
                try {
                  const res = await collectorApi.brandScan('', 'A', scanKeyword, 'ABCmart')
                  setBrandCategories(res.categories)
                  setBrandTotal(res.total)
                  setBrandSelectedCats(new Set(res.categories.map(c => c.categoryCode)))
                  addLog(`[카테고리스캔] ABC마트: ${scanKeyword} → ${fmtNum(res.groupCount)}개 카테고리, 총 ${fmtNum(res.total)}건`)
                } catch (e) { addLog(`[카테고리스캔] ABC마트 스캔 실패: ${e instanceof Error ? e.message : '오류'}`); showAlert(e instanceof Error ? e.message : '스캔 실패', 'error') }
                setBrandScanning(false)
                return
              }

              // 나이키: 키워드만으로 바로 스캔
              if (selectedSite === 'Nike') {
                const scanKeyword = keyword || brand || collectUrl.trim()
                addLog(`[카테고리스캔] Nike "${scanKeyword}" 스캔 시작...`)
                try {
                  const res = await collectorApi.brandScan('', 'A', scanKeyword, 'Nike')
                  setBrandCategories(res.categories)
                  setBrandTotal(res.total)
                  setBrandSelectedCats(new Set(res.categories.map(c => c.categoryCode)))
                  addLog(`[카테고리스캔] Nike: ${scanKeyword} → ${fmtNum(res.groupCount)}개 카테고리, 총 ${fmtNum(res.total)}건`)
                } catch (e) { addLog(`[카테고리스캔] Nike 스캔 실패: ${e instanceof Error ? e.message : '오류'}`); showAlert(e instanceof Error ? e.message : '스캔 실패', 'error') }
                setBrandScanning(false)
                return
              }

              // KREAM: 키워드만으로 바로 스캔
              if (selectedSite === 'KREAM') {
                const scanKeyword = keyword || brand || collectUrl.trim()
                addLog(`[카테고리스캔] KREAM "${scanKeyword}" 스캔 시작...`)
                try {
                  const res = await collectorApi.brandScan('', 'A', scanKeyword, 'KREAM')
                  setBrandCategories(res.categories)
                  setBrandTotal(res.total)
                  setBrandSelectedCats(new Set(res.categories.map(c => c.categoryCode)))
                  addLog(`[카테고리스캔] KREAM: ${scanKeyword} → ${fmtNum(res.groupCount)}개 카테고리, 총 ${fmtNum(res.total)}건`)
                } catch (e) { addLog(`[카테고리스캔] KREAM 스캔 실패: ${e instanceof Error ? e.message : '오류'}`); showAlert(e instanceof Error ? e.message : '스캔 실패', 'error') }
                setBrandScanning(false)
                return
              }

              // 무신사: 평문 키워드이고 브랜드 코드 없으면 브랜드 검색 모달 표시
              if (!brand && !parsed) {
                try {
                  const brandRes = await proxyApi.brandSearch(keyword)
                  if (brandRes.brands && brandRes.brands.length > 0) {
                    setPendingKeyword(keyword)
                    pendingScanGf.current = gf
                    setBrandSearchResults(brandRes.brands)
                    setSelectedBrandCodes(new Set())
                    setBrandModalAction('scan')
                    setShowMusinsaBrandModal(true)
                    setBrandScanning(false)
                    return
                  }
                } catch { /* 브랜드 검색 실패 시 키워드로 진행 */ }
              }
              addLog(`[카테고리스캔] ${selectedSite} "${keyword || brand}" 스캔 시작...`)
              try {
                const res = await collectorApi.brandScan(brand, gf, keyword, selectedSite, [], [], 0, checkedOptions)
                setBrandCategories(res.categories)
                setBrandTotal(res.total)
                setBrandSelectedCats(new Set(res.categories.map(c => c.categoryCode)))
                addLog(`[카테고리스캔] ${keyword || brand}: ${fmtNum(res.groupCount)}개 카테고리, 총 ${fmtNum(res.total)}건`)
              } catch (e) { addLog(`[카테고리스캔] ${selectedSite} 스캔 실패: ${e instanceof Error ? e.message : '오류'}`); showAlert(e instanceof Error ? e.message : '스캔 실패', 'error') }
              setBrandScanning(false)
            }} disabled={brandScanning}
              style={{ padding: '0.6rem 1rem', background: brandScanning ? '#333' : 'transparent', border: '1px solid #FF8C00', borderRadius: '6px', color: '#FF8C00', fontSize: '0.82rem', fontWeight: 600, cursor: brandScanning ? 'not-allowed' : 'pointer', whiteSpace: 'nowrap' }}>
              {brandScanning ? '탐색 중...' : '카테고리 스캔'}
            </button>
          )}
          <button
            onClick={async () => {
              // 카테고리 스캔 결과가 있으면 선택된 카테고리별 그룹 생성
              if (brandCategories.length > 0 && brandSelectedCats.size > 0) {
                const selected = brandCategories.filter(c => brandSelectedCats.has(c.categoryCode))
                if (selected.length === 0) { showAlert('카테고리를 선택하세요'); return }
                const parsed = (() => { try { return new URL(collectUrl) } catch { return null } })()
                const pathBrandMatch = parsed?.pathname.match(/\/brand\/([^/]+)/)
                const brand = parsed?.searchParams.get('brand') || pathBrandMatch?.[1] || detectedBrandCode || ''
                const keyword = parsed?.searchParams.get('keyword') || parsed?.searchParams.get('searchWord') || (!brand ? collectUrl.trim() : '')
                const gf = parsed?.searchParams.get('gf') || 'A'
                try {
                  const res = await collectorApi.brandCreateGroups({
                    brand, brand_name: pendingKeyword || keyword || brand, gf,
                    categories: selected,
                    requested_count_per_group: FIXED_REQUESTED_COUNT,
                    real_total: brandTotal,
                    options: checkedOptions,
                    source_site: selectedSite,
                    selected_brands: brandModalParsed ? Array.from(brandModalSelected) : undefined,
                    // SSG repBrandId 필터: 선택된 브랜드 id 목록 전달
                    brand_ids: brandModalParsed
                      ? brandModalList.filter(b => brandModalSelected.has(b.name) && b.id).map(b => b.id as string)
                      : undefined,
                  })
                  addLog(`[카테고리분류] ${fmtNum(res.created)}개 그룹 생성 완료`)
                  showAlert(`${fmtNum(res.created)}개 그룹이 생성되었습니다`, 'success')
                  addLog(`[카테고리분류] ${fmtNum(res.created)}개 그룹 생성 (카테고리 간 중복은 수집 시 자동 스킵)`)
                  setBrandCategories([]); setBrandSelectedCats(new Set())
                  load(); loadTree()
                } catch (e) { showAlert(e instanceof Error ? e.message : '그룹 생성 실패', 'error') }
              } else {
                // 카테고리 스캔 없으면 기존 단일 그룹 생성
                handleCreateGroup()
              }
            }}
            disabled={collecting}
            style={{
              background: 'linear-gradient(135deg, #FF8C00, #FFB84D)', color: '#fff',
              padding: '0.6rem 1.2rem', borderRadius: '6px', fontWeight: 600, fontSize: '0.82rem',
              whiteSpace: 'nowrap', cursor: collecting ? 'not-allowed' : 'pointer',
              border: 'none', opacity: collecting ? 0.6 : 1,
            }}
          >
            {collecting ? '생성중...' : brandCategories.length > 0 ? `그룹 생성 (${fmtNum(brandSelectedCats.size)}개)` : '그룹 생성'}
          </button>
          {/* 1상품수집 버튼 — 무신사 전용 */}
          {selectedSite === 'MUSINSA' && (
            <button
              onClick={async () => {
                const url = collectUrl.trim()
                if (!url) { showAlert('URL을 입력하세요'); return }
                setCollecting(true)
                addLog(`[1상품수집] ${url} 수집 시작...`)
                try {
                  const res = await collectorApi.collectSingleMusinsa(url)
                  addLog(`[1상품수집] 완료: 상품번호 ${res.product_no} (${res.brand})`)
                  showAlert('1상품 수집 완료', 'success')
                  setCollectUrl('')
                  load(); loadTree()
                } catch (e) {
                  addLog(`[1상품수집] 실패: ${e instanceof Error ? e.message : '오류'}`)
                  showAlert(e instanceof Error ? e.message : '수집 실패', 'error')
                }
                setCollecting(false)
              }}
              disabled={collecting}
              style={{
                background: collecting ? '#333' : 'transparent',
                border: '1px solid #51CF66',
                color: '#51CF66',
                padding: '0.6rem 1rem', borderRadius: '6px', fontWeight: 600, fontSize: '0.82rem',
                whiteSpace: 'nowrap', cursor: collecting ? 'not-allowed' : 'pointer', opacity: collecting ? 0.6 : 1,
              }}
            >
              1상품수집
            </button>
          )}
        </div>

        {/* 롯데ON 브랜드 선택 — 무신사 모달 스타일 */}

        {/* 카테고리 스캔 결과 */}
        {brandCategories.length > 0 && (
          <div style={{ marginTop: '0.5rem' }}>
              <div style={{ background: '#111', border: '1px solid #2D2D2D', borderRadius: '8px', padding: '0.75rem', maxHeight: '350px', overflowY: 'auto' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '0.5rem' }}>
                  <span style={{ fontSize: '0.78rem', color: '#888' }}>
                    {fmtNum(brandCategories.length)}개 카테고리 / {fmtNum(brandTotal)}건
                    (선택 {fmtNum(brandSelectedCats.size)}개)
                  </span>
                  <div style={{ display: 'flex', gap: '0.25rem' }}>
                    <button onClick={() => setBrandSelectedCats(new Set(brandCategories.map(c => c.categoryCode)))}
                      style={{ fontSize: '0.68rem', padding: '2px 6px', borderRadius: '4px', border: '1px solid #3D3D3D', background: 'transparent', color: '#888', cursor: 'pointer' }}>전체선택</button>
                    <button onClick={() => setBrandSelectedCats(new Set())}
                      style={{ fontSize: '0.68rem', padding: '2px 6px', borderRadius: '4px', border: '1px solid #3D3D3D', background: 'transparent', color: '#888', cursor: 'pointer' }}>전체해제</button>
                    <button onClick={() => { setBrandCategories([]); setBrandSelectedCats(new Set()) }}
                      style={{ fontSize: '0.68rem', padding: '2px 6px', borderRadius: '4px', border: '1px solid #3D3D3D', background: 'transparent', color: '#888', cursor: 'pointer' }}>초기화</button>
                  </div>
                </div>
                {brandCategories.map(cat => (
                  <label key={cat.categoryCode} style={{ display: 'flex', alignItems: 'center', gap: '0.4rem', padding: '0.2rem 0', cursor: 'pointer', fontSize: '0.78rem' }}>
                    <input type='checkbox' checked={brandSelectedCats.has(cat.categoryCode)}
                      onChange={e => {
                        const next = new Set(brandSelectedCats)
                        if (e.target.checked) next.add(cat.categoryCode); else next.delete(cat.categoryCode)
                        setBrandSelectedCats(next)
                      }} style={{ accentColor: '#FF8C00' }} />
                    <span style={{ color: '#E5E5E5', flex: 1 }}>{cat.path}</span>
                    <span style={{ color: '#FF8C00', fontWeight: 600, fontSize: '0.72rem' }}>{fmtNum(cat.count)}건</span>
                  </label>
                ))}
              </div>
            </div>
        )}
      </div>

      {/* 롯데ON 브랜드 선택 모달 — 제거됨, 인라인 섹션으로 이동 */}

      {/* 무신사 브랜드 선택 모달 */}
      <MusinsaBrandModal
        open={showMusinsaBrandModal}
        brandSearchResults={brandSearchResults}
        pendingKeyword={pendingKeyword}
        selectedBrandCodes={selectedBrandCodes}
        setSelectedBrandCodes={setSelectedBrandCodes}
        onClose={() => { setShowMusinsaBrandModal(false); setCollecting(false) }}
        onConfirm={handleBrandConfirm}
      />

      {/* 롯데ON / SSG 브랜드 선택 모달 */}
      <LotteOnBrandModal
        open={showBrandModal}
        brandModalList={brandModalList}
        brandModalSelected={brandModalSelected}
        brandModalKeyword={brandModalKeyword}
        brandModalParsed={brandModalParsed}
        selectedSite={selectedSite}
        setBrandModalSelected={setBrandModalSelected}
        onClose={() => setShowBrandModal(false)}
        onScanStart={() => { setBrandScanning(true); setBrandCategories([]); setBrandSelectedCats(new Set()) }}
        onScanDone={(categories, total) => {
          setBrandCategories(categories)
          setBrandTotal(total)
          setBrandSelectedCats(new Set(categories.map(c => c.categoryCode)))
          setBrandScanning(false)
        }}
        addLog={addLog}
      />
    </>
  )
}
