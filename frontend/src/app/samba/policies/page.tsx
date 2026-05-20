"use client"

import { useEffect, useState, useCallback, useRef } from "react"
import { useSearchParams } from "next/navigation"
import {
  policyApi,
  forbiddenApi,
  accountApi,
  collectorApi,
  proxyApi,
  type SambaPolicy,
  type SambaMarketAccount,
  type SambaCollectedProduct,
} from "@/lib/samba/api/commerce"
import {
  detailTemplateApi,
  nameRuleApi,
  type SambaDetailTemplate,
  type SambaNameRule,
} from "@/lib/samba/api/support"
import { API_BASE, request } from "@/lib/samba/api/shared"
import { tetrisApi } from '@/lib/samba/api/tetris'
import { MARKETS, MARKET_ID_BY_LABEL, POLICY_MARKETS_DOMESTIC, POLICY_MARKETS_OVERSEAS } from '@/lib/samba/markets'
import { showAlert, showConfirm } from '@/components/samba/Modal'
import { card, inputStyle, fmtNum } from '@/lib/samba/styles'
import { SITE_COLORS } from '@/lib/samba/constants'
import { fmtTime } from '@/lib/samba/utils'
import NumInput from '@/components/samba/NumInput'
import TetrisBoard from './tetris/TetrisBoard'

interface RangeMargin {
  min: number
  max: number
  rate: number
  amount: number
}

interface SourceSiteMargin {
  marginRate: number
  marginAmount: number
  // 적립금 사용 가능 상품에만 추가 마진 적용 (현재 무신사만 지원)
  pointOnly?: boolean
}

interface PricingForm {
  marginRate: number
  shippingCost: number
  extraCharge: number
  minMarginAmount: number
  discountType: string
  discountValue: number
  useRangeMargin: boolean
  rangeMargins: RangeMargin[]
  customFormula: string
  currency: string
  customsIncluded: boolean
  sourceSiteMargins: Record<string, SourceSiteMargin>
}

// 마켓정책
interface MarketPolicyForm {
  accountId: string
  accountIds?: string[] // 복수 계정 지원
  shipType: string
  feeRate: number
  shippingCost: number
  shippingDays: number
  marginRate: number
  brand: string
  // 옥션/지마켓 전용
  bulkDiscountQty: number
  bulkDiscountPrice: number
  smileCashRate: number
  // GS샵 전용
  gsMarginRate: number
  // 신세계몰 전용: 주문수량 제한
  dayMaxQty: number
  onceMinQty: number
  onceMaxQty: number
  // 신세계몰 전용: 고시정보
  ssgNoticeGroup?: '의류' | '신발' | '가방/잡화' | '기타'
  ssgNoticeMaterial?: string
  ssgNoticeColor?: string
  ssgNoticeSize?: string
  ssgNoticeImport?: 'Y' | 'N'
  ssgNoticeImporter?: string
  ssgNoticeCaution?: string
  ssgNoticeAsContact?: string
  ssgNoticeManufacturer?: string
  ssgNoticeOrigin?: string
  // 스마트스토어 전용
  discountRate: number  // 즉시할인율 (%)
  maxStock: number      // 최대 재고수량 (0=무제한)
  // 플레이오토 전용
  origin: string        // 원산지
  streetPriceRate: number // 시중가 비율 (%)
  ssgBrandMappings?: { brandId: string; brandNm: string }[]
  ssgExtraFeeRate?: number
}


// 마켓 목록은 @/lib/samba/markets에서 import
const MARKET_KEY_MAP = MARKET_ID_BY_LABEL

const defaultPricing: PricingForm = {
  marginRate: 15,
  shippingCost: 3000,
  extraCharge: 0,
  minMarginAmount: 1000,
  discountType: 'none',
  discountValue: 0,
  useRangeMargin: false,
  rangeMargins: [],
  customFormula: '',
  currency: 'KRW',
  customsIncluded: false,
  sourceSiteMargins: {},
}

// 소싱처 목록 (SITE_COLORS 키 기반, 표시명 매핑)
const SOURCING_SITE_LABELS: Record<string, string> = {
  MUSINSA: '무신사', KREAM: '크림', FashionPlus: '패션플러스', Nike: '나이키',
  Adidas: '아디다스', ABCmart: 'ABC마트', LOTTEON: '롯데ON', GSShop: 'GS샵',
  SSG: 'SSG', REXMONDE: '렉스몬드', ElandMall: '이랜드몰', SSF: 'SSF',
  NAVERSTORE: '네이버스토어', DANAWA: '다나와',
}

// ─── 롯데홈쇼핑 정책 타입 ────────────────────────────────────────
interface LotteArtcItem { artc: string; artcNm: string }
interface LotteMdGroup { md_gsgr_no: string; md_gsgr_nm: string; superGroupName?: string; artcItems: LotteArtcItem[] }
interface LotteCategory { disp_no: string; disp_nm: string; shop_pos?: string; disp_tp_cd?: string }
interface LotteBrand { brnd_no: string; brnd_nm: string; brnd_en?: string }
interface LottePolicy {
  mdGsgrNo: string; mdGsgrNm: string
  dispNo: string; dispNm: string
  stdCatNo: string; stdCatNm: string
  brandMappings: { brnd_no: string; brnd_nm: string }[]
  manufacturer: string
  taxType: string
  ageLimit: string
  purchaseType: string
  marginRate: string
  saleType: string
  saleMethod: string
  priceCompareDisplay: string
  purchaseQtyLimit: string
  optionModify: string
  optionStockMgmt: string
  imageResize: string
  // 배송정보
  dlvPolcNo: string; dlvPolcNm: string
  corpRlsPlSn: string; corpRlsPlNm: string
  corpDlvpSn: string; corpDlvpNm: string
  addDlvPolcNo: string; addDlvPolcNm: string
  ecGoodsArtcCd: string; ecGoodsArtcNm: string
  // 품목정보 (주관식 8개)
  itemMaterial: string
  itemColor: string
  itemSize: string
  itemImport: string
  itemImportNote: string
  itemWashing: string
  itemMfgDate: string
  itemQuality: string
  itemQualityNote: string
  itemAs: string
}
const defaultLottePolicy: LottePolicy = { mdGsgrNo: '', mdGsgrNm: '', dispNo: '', dispNm: '', stdCatNo: '', stdCatNm: '', brandMappings: [], manufacturer: '', taxType: '', ageLimit: '', purchaseType: '', marginRate: '', saleType: '', saleMethod: '', priceCompareDisplay: '', purchaseQtyLimit: '', optionModify: '', optionStockMgmt: '사용함', imageResize: '', dlvPolcNo: '', dlvPolcNm: '', corpRlsPlSn: '', corpRlsPlNm: '', corpDlvpSn: '', corpDlvpNm: '', addDlvPolcNo: '', addDlvPolcNm: '', ecGoodsArtcCd: '', ecGoodsArtcNm: '', itemMaterial: '', itemColor: '', itemSize: '', itemImport: '', itemImportNote: '', itemWashing: '', itemMfgDate: '', itemQuality: '', itemQualityNote: '', itemAs: '' }

function extractLotteList<T>(data: unknown, ...keys: string[]): T[] {
  if (!data || typeof data !== 'object') return []
  let cur: unknown = data
  for (const k of keys) {
    if (!cur || typeof cur !== 'object') return []
    cur = (cur as Record<string, unknown>)[k]
  }
  if (Array.isArray(cur)) return cur as T[]
  if (cur && typeof cur === 'object') return [cur] as T[]
  return []
}

// MD상품군 응답 파싱 (SuperGroup > SubGroup 2단계 구조 평탄화, 표준카테고리 포함)
function extractMdGroups(data: unknown): LotteMdGroup[] {
  const result: LotteMdGroup[] = []
  const superGroups = extractLotteList<Record<string, unknown>>(data, 'Result', 'SuperGroupInfoList', 'SuperGroupInfo')
  for (const sg of superGroups) {
    const superName = String(sg.SuperGroupName || '')
    const subGroups = extractLotteList<Record<string, unknown>>(sg, 'SubGroupInfoList', 'SubGroupInfo')
    for (const sub of subGroups) {
      const artcRaw = extractLotteList<Record<string, string>>(sub, 'ArtcItemList', 'GoodsArtcInfo')
      const artcItems: LotteArtcItem[] = artcRaw.map(a => ({ artc: a.GoodsArtc || '', artcNm: a.GoodsArtcNm || '' }))
      result.push({ md_gsgr_no: String(sub.GroupCode || ''), md_gsgr_nm: String(sub.GroupName || ''), superGroupName: superName, artcItems })
    }
  }
  return result
}


export default function PoliciesPage() {
  const TETRIS_MATCHING_ENABLED_KEY = 'tetris_matching_enabled'
  useEffect(() => { document.title = 'SAMBA-정책관리' }, [])
  const searchParams = useSearchParams()
  const [policies, setPolicies] = useState<SambaPolicy[]>([])
  const [, setLoading] = useState(true)
  const [showForm, setShowForm] = useState(true)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [name, setName] = useState("새 정책")
  const [policyColor, setPolicyColor] = useState('#3B82F6')
  const [siteName, setSiteName] = useState("")
  const [pricing, setPricing] = useState<PricingForm>({ ...defaultPricing })

  // 정책 선택 드롭다운
  const [selectedPolicyId, setSelectedPolicyId] = useState<string | null>(null)

  // 소싱처별 추가 마진 UI 토글
  const [showSourceSiteMargins, setShowSourceSiteMargins] = useState(false)

  // 메인 탭 (정책관리 vs 테트리스 매칭)
  const [mainTab, setMainTab] = useState<'정책관리' | '테트리스 매칭'>('테트리스 매칭')
  const [tetrisMatchingEnabled, setTetrisMatchingEnabled] = useState(false)
  const [tetrisMatchingSaving, setTetrisMatchingSaving] = useState(false)
  const [syncIntervalInput, setSyncIntervalInput] = useState<number>(1)

  // 마켓정책 설정
  const [marketPolicyTab, setMarketPolicyTab] = useState('쿠팡')
  const [ssgBrands, setSsgBrands] = useState<{ brandId: string; brandNm: string }[]>([])
  const [ssgBrandKeyword, setSsgBrandKeyword] = useState('')
  const [ssgBrandLoading, setSsgBrandLoading] = useState(false)
  const [ssgBrandError, setSsgBrandError] = useState('')
  const [marketPolicies, setMarketPolicies] = useState<Record<string, MarketPolicyForm>>({})

  const ssgAccountId = marketPolicies['신세계몰(전시)']?.accountId || ''

  // SSG 브랜드 검색 (검색어 1글자 이상, 디바운스)
  useEffect(() => {
    if (!ssgBrandKeyword.trim()) { setSsgBrands([]); setSsgBrandLoading(false); setSsgBrandError(''); return }
    setSsgBrandLoading(true)
    setSsgBrandError('')
    const timer = setTimeout(() => {
      request<{ success: boolean; brands: { brandId: string; brandNm: string }[]; message?: string }>(
        `${API_BASE}/api/v1/samba/proxy/ssg/brands${ssgAccountId ? `?account_id=${encodeURIComponent(ssgAccountId)}` : ''}`
      ).then(res => {
        if (!res.success) { setSsgBrandError(res.message || '브랜드 조회 실패'); setSsgBrands([]); return }
        const kw = ssgBrandKeyword.trim().toLowerCase()
        const filtered = (res.brands || []).filter(b => b.brandNm.toLowerCase().includes(kw))
        setSsgBrands(filtered)
        if (filtered.length === 0) setSsgBrandError('검색 결과 없음')
      }).catch(() => setSsgBrandError('네트워크 오류')).finally(() => setSsgBrandLoading(false))
    }, 300)
    return () => clearTimeout(timer)
  }, [ssgBrandKeyword, ssgAccountId])

  // 상세페이지/상품명 템플릿
  const [detailTemplates, setDetailTemplates] = useState<SambaDetailTemplate[]>([])
  const [nameRules, setNameRules] = useState<SambaNameRule[]>([])
  const nameRulesRef = useRef<SambaNameRule[]>([])
  nameRulesRef.current = nameRules
  const [selectedDetailTemplateId, setSelectedDetailTemplateId] = useState('')
  const [marketDetailTemplates, setMarketDetailTemplates] = useState<Record<string, string>>({})
  const [selectedNameRuleId, setSelectedNameRuleId] = useState('')
  const [dragIdx, setDragIdx] = useState<number | null>(null)
  const [imgChecks, setImgChecks] = useState<Record<string, boolean>>({
    main: true, sub: true, title: false, option: false, detail: false, topImg: false, bottomImg: false,
  })
  const [imgOrder, setImgOrder] = useState<string[]>(['topImg', 'main', 'sub', 'title', 'option', 'detail', 'bottomImg'])
  const [imgSaving, setImgSaving] = useState<string>('')
  const [previewProduct, setPreviewProduct] = useState<SambaCollectedProduct | null>(null)

  // AI 정책 변경
  const [aiPolicyLoading, setAiPolicyLoading] = useState(false)
  const [aiPolicyModalOpen, setAiPolicyModalOpen] = useState(false)
  const [aiPolicyCommand, setAiPolicyCommand] = useState('')
  const [aiPolicyChanges, setAiPolicyChanges] = useState<{ policy_id: string; policy_name: string; field: string; market: string; before: number; after: number }[]>([])
  const [aiPolicyApplied, setAiPolicyApplied] = useState(0)
  // AI 비용 추적 (1회 호출 ≈ ₩15: Sonnet4 ~1,500in + ~300out)
  const [lastAiUsage, setLastAiUsage] = useState<{ calls: number; tokens: number; cost: number; date: string } | null>(null)

  // 마켓 계정 목록 (설정에서 등록한 계정)
  const [, setStoreAccounts] = useState<Record<string, Record<string, string>>>({})
  // 실제 마켓 계정 목록 (DB에서 로드 — 다중 계정 지원)
  const [marketAccounts, setMarketAccounts] = useState<SambaMarketAccount[]>([])

  // 롯데홈쇼핑 정책
  const [lottePolicy, setLottePolicy] = useState<LottePolicy>(defaultLottePolicy)
  const [lotteSaving, setLotteSaving] = useState(false)
  const [lotteMdGroups, setLotteMdGroups] = useState<LotteMdGroup[]>([])
  const [lotteMdLoading, setLotteMdLoading] = useState(false)
  const [lotteCategories, setLotteCategories] = useState<LotteCategory[]>([])
  const [lotteCatLoading, setLotteCatLoading] = useState(false)
  const [lotteBrands, setLotteBrands] = useState<LotteBrand[]>([])
  const [lotteBrandKeyword, setLotteBrandKeyword] = useState('')
  const [lotteBrandLoading, setLotteBrandLoading] = useState(false)
  const [lotteBrandError, setLotteBrandError] = useState('')
  const [lotteStdCategories, setLotteStdCategories] = useState<{ no: string; nm: string; path?: string }[]>([])
  const [lotteStdCatLoading, setLotteStdCatLoading] = useState(false)


  // 현재 마켓 정책 가져오기 (부분 데이터에도 기본값 보장)
  const getCurrentMarketPolicy = useCallback((): MarketPolicyForm => {
    const defaults: MarketPolicyForm = { accountId: '', accountIds: [], shipType: 'domestic', feeRate: 21, shippingCost: 0, shippingDays: 3, marginRate: 0, brand: '', bulkDiscountQty: 2, bulkDiscountPrice: 0, smileCashRate: 0, gsMarginRate: 0, discountRate: 0, maxStock: 0, dayMaxQty: 5, onceMinQty: 1, onceMaxQty: 5, origin: '', streetPriceRate: 0, ssgBrandMappings: [] }
    return { ...defaults, ...(marketPolicies[marketPolicyTab] || {}) }
  }, [marketPolicies, marketPolicyTab])

  const setCurrentMarketPolicy = useCallback((mp: MarketPolicyForm) => {
    setMarketPolicies(prev => ({ ...prev, [marketPolicyTab]: mp }))
  }, [marketPolicyTab])

  // 스토어 설정 로드 (설정탭에서 저장한 계정 정보) — 병렬 호출
  const loadStoreAccounts = useCallback(async () => {
    const keys = Object.values(MARKET_KEY_MAP)
    const results = await Promise.all(
      keys.map(key =>
        forbiddenApi.getSetting(`store_${key}`)
          .catch(() => null)
          .then(data => ({ key, data: data as Record<string, string> | null }))
      )
    )
    const loaded: Record<string, Record<string, string>> = {}
    for (const { key, data } of results) {
      if (data && data.businessName) {
        loaded[key] = data
      }
    }
    setStoreAccounts(loaded)
  }, [])

  const loadPolicies = useCallback(async () => {
    setLoading(true)
    try {
      setPolicies(await policyApi.list())
    } catch { /* ignore */ }
    setLoading(false)
  }, [])

  useEffect(() => {
    loadPolicies(); loadStoreAccounts()
    accountApi.listActive().then(setMarketAccounts).catch(() => {})
    detailTemplateApi.list().then(setDetailTemplates).catch(() => {})
    nameRuleApi.list().then(setNameRules).catch(() => {})
    // 미리보기용 최신 상품 1개 로드
    collectorApi.listProducts(0, 1).then(list => {
      if (list.length > 0) setPreviewProduct(list[0])
    }).catch(() => {})
  }, [loadPolicies, loadStoreAccounts])

  useEffect(() => {
    forbiddenApi.getSetting(TETRIS_MATCHING_ENABLED_KEY)
      .then(data => {
        if (typeof data === 'boolean') {
          setTetrisMatchingEnabled(data)
        } else if (data && typeof data === 'object' && 'enabled' in data) {
          setTetrisMatchingEnabled(Boolean((data as { enabled?: unknown }).enabled))
        }
      })
      .catch(() => {})
  }, [TETRIS_MATCHING_ENABLED_KEY])

  useEffect(() => {
    tetrisApi.getSyncInterval()
      .then(res => { if (res.interval_hours > 0) setSyncIntervalInput(res.interval_hours) })
      .catch(() => {})
  }, [])


  // URL highlight 파라미터로 정책 자동 선택
  useEffect(() => {
    const highlightId = searchParams.get('highlight')
    if (highlightId && policies.length > 0 && !editingId) {
      const target = policies.find(p => p.id === highlightId)
      if (target) openEdit(target)
    }
  }, [policies, searchParams]) // eslint-disable-line react-hooks/exhaustive-deps

  // 롯데홈쇼핑: 저장된 정책 + MD상품군 초기 로드
  useEffect(() => {
    request<{ success: boolean; data: Record<string, unknown> }>(`${API_BASE}/api/v1/samba/proxy/lottehome/policy`)
      .then(res => { if (res.success && res.data && Object.keys(res.data).length > 0) { const raw = res.data as Record<string, unknown>; const loaded = { ...defaultLottePolicy, ...raw }; if (!loaded.brandMappings?.length && (raw.brndNo as string)) { loaded.brandMappings = [{ brnd_no: raw.brndNo as string, brnd_nm: (raw.brndNm as string) || '' }] } setLottePolicy({ ...loaded, optionStockMgmt: (loaded.optionStockMgmt as string) || '사용함' }) } })
      .catch(() => {})
    setLotteMdLoading(true)
    request<{ success: boolean; data: unknown }>(`${API_BASE}/api/v1/samba/proxy/lottehome/md-groups`)
      .then(res => setLotteMdGroups(extractMdGroups(res.data)))
      .catch(() => {}).finally(() => setLotteMdLoading(false))
  }, [])

  // 롯데홈쇼핑 전시카테고리 (MD상품군 변경 시 로드)
  useEffect(() => {
    if (!lottePolicy.mdGsgrNo) { setLotteCategories([]); setLotteStdCategories([]); return }
    setLotteCatLoading(true)
    request<{ success: boolean; data: unknown }>(`${API_BASE}/api/v1/samba/proxy/lottehome/categories?md_gsgr_no=${lottePolicy.mdGsgrNo}`)
      .then(res => {
        const raw = extractLotteList<Record<string, string>>(res.data, 'Result', 'CategoryInfoList')
        setLotteCategories(raw.map(c => ({ disp_no: c.DispNo, disp_nm: c.DispNm, shop_pos: c.ShopPos, disp_tp_cd: c.DispTpCd || c.disp_tp_cd || '' })))
      }).catch(() => {}).finally(() => setLotteCatLoading(false))
  }, [lottePolicy.mdGsgrNo])

  // 롯데홈쇼핑 표준카테고리 (전시카테고리 선택 시 로드)
  useEffect(() => {
    if (!lottePolicy.dispNo) { setLotteStdCategories([]); return }
    setLotteStdCatLoading(true)
    request<{ success: boolean; data: unknown }>(`${API_BASE}/api/v1/samba/proxy/lottehome/standard-categories?disp_no=${lottePolicy.dispNo}`)
      .then(res => {
        const raw = extractLotteList<Record<string, string>>(res.data, 'Result', 'StdCatInfoList', 'StdCatInfo')
        setLotteStdCategories(raw.map(c => ({ no: c.StdCatNo || '', nm: c.StdCatNm || '', path: c.FullStdCatNm || c.StdCatNm || '' })))
      }).catch(() => {}).finally(() => setLotteStdCatLoading(false))
  }, [lottePolicy.dispNo])

  // 롯데홈쇼핑 브랜드 검색 (검색어 1글자 이상, 디바운스)
  useEffect(() => {
    if (!lotteBrandKeyword.trim()) { setLotteBrands([]); setLotteBrandLoading(false); setLotteBrandError(''); return }
    setLotteBrandLoading(true)
    setLotteBrandError('')
    const timer = setTimeout(() => {
      request<{ success: boolean; data: unknown; message?: string }>(`${API_BASE}/api/v1/samba/proxy/lottehome/brands?brnd_nm=${encodeURIComponent(lotteBrandKeyword)}`)
        .then(res => {
          if (!res.success) {
            console.warn('[브랜드검색] 서버 오류:', res.message)
            setLotteBrandError(res.message || '브랜드 조회 실패')
            setLotteBrands([])
            return
          }
          const raw = extractLotteList<Record<string, string>>(res.data, 'Result', 'BrandInfoList', 'BrandInfo')
          setLotteBrands(raw.map(b => ({ brnd_no: b.BrandCode || '', brnd_nm: b.BrandName || '', brnd_en: b.BrandEnglishName || '' })))
          if (raw.length === 0) setLotteBrandError('검색 결과 없음')
        })
        .catch(e => { console.error('[브랜드검색] 요청 실패:', e); setLotteBrandError('네트워크 오류') })
        .finally(() => setLotteBrandLoading(false))
    }, 300)
    return () => clearTimeout(timer)
  }, [lotteBrandKeyword])

  // 템플릿 선택 시 imgChecks/imgOrder를 DB 값으로 초기화
  useEffect(() => {
    if (!selectedDetailTemplateId) return
    const t = detailTemplates.find(x => x.id === selectedDetailTemplateId)
    if (!t) return
    if (t.img_checks) {
      setImgChecks(t.img_checks)
    } else {
      setImgChecks({
        main: true, sub: true, title: false, option: false, detail: false,
        topImg: !!t.top_image_s3_key,
        bottomImg: !!t.bottom_image_s3_key,
      })
    }
    if (t.img_order) {
      setImgOrder(t.img_order)
    }
  }, [selectedDetailTemplateId, detailTemplates])

  const openEdit = (p: SambaPolicy) => {
    setEditingId(p.id)
    setName(p.name)
    setSiteName(p.site_name || "")
    const pr = (p.pricing || {}) as Record<string, unknown>
    setPricing({
      marginRate: Number(pr.marginRate ?? 15),
      shippingCost: Number(pr.shippingCost ?? 3000),
      extraCharge: Number(pr.extraCharge ?? 0),
      minMarginAmount: Number(pr.minMarginAmount ?? 1000),
      discountType: String(pr.discountType ?? 'none'),
      discountValue: Number(pr.discountValue ?? 0),
      useRangeMargin: Boolean(pr.useRangeMargin),
      rangeMargins: Array.isArray(pr.rangeMargins) ? pr.rangeMargins as RangeMargin[] : [],
      customFormula: String(pr.customFormula ?? ''),
      currency: String(pr.currency ?? 'KRW'),
      customsIncluded: Boolean(pr.customsIncluded),
      sourceSiteMargins: (pr.sourceSiteMargins || {}) as Record<string, SourceSiteMargin>,
    })
    // 마켓 정책 로드
    const mp = (p.market_policies || {}) as Record<string, MarketPolicyForm>
    setMarketPolicies(mp)
    // extras 복원
    setSelectedDetailTemplateId(p.extras?.detail_template_id || '')
    setMarketDetailTemplates(p.extras?.market_detail_templates || {})
    setSelectedNameRuleId(p.extras?.name_rule_id || '')
    setPolicyColor(p.extras?.color || '#3B82F6')
    setShowForm(true)
  }

  const handleSubmit = async () => {
    try {
      const payload = {
        name,
        site_name: siteName,
        pricing: {
          marginRate: pricing.marginRate,
          shippingCost: pricing.shippingCost,
          extraCharge: pricing.extraCharge,
          minMarginAmount: pricing.minMarginAmount,
          discountType: pricing.discountType,
          discountValue: pricing.discountValue,
          useRangeMargin: pricing.useRangeMargin,
          rangeMargins: pricing.rangeMargins,
          customFormula: pricing.customFormula,
          currency: pricing.currency,
          customsIncluded: pricing.customsIncluded,
          sourceSiteMargins: pricing.sourceSiteMargins,
        },
        market_policies: marketPolicies,
        extras: {
          detail_template_id: selectedDetailTemplateId || undefined,
          market_detail_templates: Object.keys(marketDetailTemplates).length > 0 ? marketDetailTemplates : undefined,
          name_rule_id: selectedNameRuleId || undefined,
          color: policyColor || undefined,
        },
      }
      // 정책 저장 (필수 — 먼저 실행)
      if (editingId) {
        await policyApi.update(editingId, payload)
      } else {
        const created = await policyApi.create(payload)
        setEditingId(created.id)
        setSelectedPolicyId(created.id)
      }

      // 나머지 병렬 실행 (상품명규칙 + 삭제어 + 금지어 + 목록갱신)
      const parallel: Promise<unknown>[] = [policyApi.list().then(list => setPolicies(list)).catch(() => {})]

      if (selectedNameRuleId) {
        const rule = nameRulesRef.current.find(x => x.id === selectedNameRuleId)
        if (rule) {
          parallel.push(
            nameRuleApi.update(rule.id, {
              name: rule.name, prefix: rule.prefix, suffix: rule.suffix,
              replacements: rule.replacements, replace_mode: rule.replace_mode,
              option_rules: rule.option_rules, name_composition: rule.name_composition,
              market_name_compositions: rule.market_name_compositions,
              brand_display: rule.brand_display, dedup_enabled: rule.dedup_enabled,
            }).then(saved => { if (saved) setNameRules(prev => prev.map(x => x.id === saved.id ? saved : x)) }).catch(() => {})
          )
        }
      }

      // 금지어/삭제어는 설정 페이지에서 전역 관리

      await Promise.all(parallel)
      showAlert('정책이 저장되었습니다', 'success')
    } catch (e) {
      showAlert(e instanceof Error ? e.message : '저장 실패', 'error')
    }
  }

  const handleDelete = async (id: string) => {
    if (!await showConfirm('삭제하시겠습니까?')) return
    await policyApi.delete(id)
    setPolicies(await policyApi.list().catch(() => []))
    setEditingId(null)
    setSelectedPolicyId(null)
    setShowForm(false)
  }

  // 자동저장 (debounce 800ms)
  const autoSaveTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const latestPricingRef = useRef(pricing)
  const latestMarketPoliciesRef = useRef(marketPolicies)
  const latestExtrasRef = useRef({ detail_template_id: selectedDetailTemplateId, name_rule_id: selectedNameRuleId, market_detail_templates: marketDetailTemplates, color: policyColor })
  latestPricingRef.current = pricing
  latestMarketPoliciesRef.current = marketPolicies
  latestExtrasRef.current = { detail_template_id: selectedDetailTemplateId, name_rule_id: selectedNameRuleId, market_detail_templates: marketDetailTemplates, color: policyColor }

  const triggerAutoSave = useCallback(() => {
    if (!editingId) return
    if (autoSaveTimer.current) clearTimeout(autoSaveTimer.current)
    autoSaveTimer.current = setTimeout(async () => {
      try {
        const ex = latestExtrasRef.current
        await policyApi.update(editingId, {
          name,
          site_name: siteName,
          pricing: { ...latestPricingRef.current },
          market_policies: latestMarketPoliciesRef.current,
          extras: {
            detail_template_id: ex.detail_template_id || undefined,
            name_rule_id: ex.name_rule_id || undefined,
            market_detail_templates: Object.keys(ex.market_detail_templates || {}).length > 0 ? ex.market_detail_templates : undefined,
            color: ex.color || undefined,
          },
        })
        // 리스트만 갱신 (현재 편집 폼은 유지)
        const list = await policyApi.list().catch(() => [])
        setPolicies(list)
      } catch { /* 자동저장 실패 무시 */ }
    }, 800)
  }, [editingId, name, siteName])

  // 범위 마진 행 추가/삭제
  const addRangeMargin = () => {
    const last = pricing.rangeMargins[pricing.rangeMargins.length - 1]
    setPricing({
      ...pricing,
      rangeMargins: [...pricing.rangeMargins, { min: last ? last.max + 1 : 0, max: last ? last.max + 50000 : 50000, rate: 15, amount: 0 }]
    })
    triggerAutoSave()
  }

  const removeRangeMargin = (idx: number) => {
    setPricing({ ...pricing, rangeMargins: pricing.rangeMargins.filter((_, i) => i !== idx) })
    triggerAutoSave()
  }

  const updateRangeMargin = (idx: number, field: keyof RangeMargin, value: number) => {
    const updated = [...pricing.rangeMargins]
    updated[idx] = { ...updated[idx], [field]: value }
    setPricing({ ...pricing, rangeMargins: updated })
    triggerAutoSave()
  }

  // 소싱처별 추가 마진 업데이트
  const updateSourceSiteMargin = (siteId: string, field: 'marginRate' | 'marginAmount', value: number) => {
    const current = pricing.sourceSiteMargins[siteId] || { marginRate: 0, marginAmount: 0 }
    setPricing({
      ...pricing,
      sourceSiteMargins: { ...pricing.sourceSiteMargins, [siteId]: { ...current, [field]: value } }
    })
    triggerAutoSave()
  }

  // 소싱처별 적립금 사용가능 상품 한정 토글
  const toggleSourceSitePointOnly = (siteId: string, value: boolean) => {
    const current = pricing.sourceSiteMargins[siteId] || { marginRate: 0, marginAmount: 0 }
    setPricing({
      ...pricing,
      sourceSiteMargins: {
        ...pricing.sourceSiteMargins,
        [siteId]: { ...current, pointOnly: value },
      },
    })
    triggerAutoSave()
  }

  // 현재 마켓 탭에 해당하는 스토어 계정 목록 가져오기
  const handleToggleTetrisMatching = async () => {
    const nextValue = !tetrisMatchingEnabled
    setTetrisMatchingEnabled(nextValue)
    setTetrisMatchingSaving(true)
    try {
      await forbiddenApi.saveSetting(TETRIS_MATCHING_ENABLED_KEY, nextValue)
      const res = await tetrisApi.setSyncInterval(nextValue ? Math.max(1, syncIntervalInput) : 0)
      if (!nextValue && res?.cancelled && res.cancelled > 0) {
        showAlert(`테트리스 매칭 OFF — 진행중·대기 잡 ${fmtNum(res.cancelled)}건 취소됨`)
      }
      // ON 시 즉시 기존 tetris 잡 클리어 후 현재 배치 기준으로 재생성
      if (nextValue) {
        const sync = await tetrisApi.runSync(true)
        if (sync?.skipped || sync?.paused) {
          showAlert('테트리스 매칭 ON — 동기화 인터벌이 꺼져 있거나 일시정지 상태입니다')
        } else {
          const cleared = sync?.cancelled_before_sync ?? 0
          showAlert(
            `테트리스 매칭 ON — 기존 잡 ${fmtNum(cleared)}건 정리, ` +
            `신규 잡 ${fmtNum(sync?.jobs ?? 0)}건 생성 (상품 ${fmtNum(sync?.triggered ?? 0)}개)`,
          )
        }
      }
    } catch (error) {
      setTetrisMatchingEnabled(!nextValue)
      showAlert('테트리스 매칭 사용 설정 저장에 실패했습니다: ' + String(error))
    } finally {
      setTetrisMatchingSaving(false)
    }
  }

  const mp = getCurrentMarketPolicy()

  return (
    <div style={{ color: '#E5E5E5' }}>
      {/* 헤더 + 탭 */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '0.75rem' }}>
        <div style={{ display: 'flex', gap: '0.5rem' }}>
          <button
            onClick={() => setMainTab('테트리스 매칭')}
            style={{
              padding: '0.375rem 0.75rem',
              fontSize: '0.75rem',
              borderRadius: '6px',
              border: mainTab === '테트리스 매칭' ? '1px solid #FF8C00' : '1px solid #2D2D2D',
              background: mainTab === '테트리스 매칭' ? 'rgba(255,140,0,0.12)' : 'transparent',
              color: mainTab === '테트리스 매칭' ? '#FF8C00' : '#888',
              cursor: 'pointer',
              fontWeight: 600,
            }}
          >
            테트리스 매칭
          </button>
          <button
            onClick={() => setMainTab('정책관리')}
            style={{
              padding: '0.375rem 0.75rem',
              fontSize: '0.75rem',
              borderRadius: '6px',
              border: mainTab === '정책관리' ? '1px solid #FF8C00' : '1px solid #2D2D2D',
              background: mainTab === '정책관리' ? 'rgba(255,140,0,0.12)' : 'transparent',
              color: mainTab === '정책관리' ? '#FF8C00' : '#888',
              cursor: 'pointer',
              fontWeight: 600,
            }}
          >
            정책관리
          </button>
        </div>
        <div style={{ display: 'flex', gap: '0.5rem' }}>
          <a href="/samba/categories" style={{ fontSize: '0.75rem', color: '#4C9AFF', textDecoration: 'none' }}>카테고리매핑 →</a>
          <a href="/samba/shipments" style={{ fontSize: '0.75rem', color: '#888', textDecoration: 'none' }}>상품전송 →</a>
        </div>
      </div>

      {/* 정책관리 탭 내용 */}
      <div style={{ display: mainTab === '정책관리' ? 'block' : 'none' }}>
        {/* AI 비용 (SMS 잔여량 스타일) */}
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.625rem', padding: '0.5rem 1rem', background: 'rgba(167,139,250,0.08)', border: '1px solid rgba(167,139,250,0.2)', borderRadius: '8px', marginBottom: '0.75rem' }}>
        <span style={{ fontSize: '0.8125rem', color: '#A78BFA', fontWeight: 600 }}>AI 비용</span>
        <span style={{ fontSize: '0.8125rem', color: '#E5E5E5' }}>
          예상 <span style={{ color: '#FFB84D', fontWeight: 700 }}>₩15</span>
          <span style={{ color: '#888' }}> (1회)</span>
        </span>
        {lastAiUsage && (
          <>
            <span style={{ color: '#2D2D2D' }}>|</span>
            <span style={{ fontSize: '0.8125rem', color: '#E5E5E5' }}>
              최근 <span style={{ color: '#51CF66', fontWeight: 700 }}>₩{fmtNum(lastAiUsage.cost)}</span>
              <span style={{ color: '#888' }}> ({fmtNum(lastAiUsage.calls)}회 / ~{fmtNum(lastAiUsage.tokens)}토큰)</span>
            </span>
            <span style={{ fontSize: '0.6875rem', color: '#555' }}>{lastAiUsage.date}</span>
          </>
        )}
        <span style={{ fontSize: '0.625rem', color: '#555', marginLeft: 'auto', cursor: 'help' }} title={'산정 근거: Sonnet4 $3/M in + $15/M out × ₩1,450\n1회: ~1,500 in + ~300 out = ~₩15'}>근거</span>
      </div>

      {/* 정책 선택 */}
      <div style={{ ...card, padding: '1rem 1.25rem', marginBottom: '1.5rem' }}>
        <div style={{ display: 'flex', gap: '0.75rem', flexWrap: 'wrap', alignItems: 'center' }}>
          <select
            value={selectedPolicyId || ''}
            onChange={async (e) => {
              const id = e.target.value
              // 정책 전환 전 진행 중인 자동저장 타이머 취소 (레이스 컨디션 방지)
              if (autoSaveTimer.current) clearTimeout(autoSaveTimer.current)
              if (id === '__new__') {
                // 신규: DB 저장 없이 빈 폼만 열기 (저장 버튼 시 create)
                setEditingId(null)
                setSelectedPolicyId(null)
                setName(`정책 ${policies.length + 1}`)
                setSiteName('')
                setPricing({ ...defaultPricing })
                setMarketPolicies({})
                setSelectedDetailTemplateId('')
                setSelectedNameRuleId('')
                setShowForm(true)
                return
              }
              setSelectedPolicyId(id || null)
              if (id) {
                const p = policies.find(p => p.id === id)
                if (p) openEdit(p)
              } else {
                setEditingId(null)
                setName("새 정책")
                setSiteName("")
                setPricing({ ...defaultPricing })
                setMarketPolicies({})
                setShowForm(false)
              }
            }}
            style={{ ...inputStyle, width: '220px', cursor: 'pointer' }}
          >
            <option value="">정책 선택</option>
            <option value="__new__">+ 신규정책</option>
            {policies.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
          </select>
          {/* 정책 복사 */}
          <button onClick={async () => {
            if (!editingId) { showAlert('복사할 정책을 선택하세요'); return }
            const copied = await policyApi.create({
              name: `${name} (복사)`,
              site_name: siteName,
              pricing: {
                marginRate: pricing.marginRate,
                shippingCost: pricing.shippingCost,
                extraCharge: pricing.extraCharge,
                minMarginAmount: pricing.minMarginAmount,
                discountType: pricing.discountType,
                discountValue: pricing.discountValue,
                useRangeMargin: pricing.useRangeMargin,
                rangeMargins: pricing.rangeMargins,
                customFormula: pricing.customFormula,
                currency: pricing.currency,
                customsIncluded: pricing.customsIncluded,
              },
              market_policies: marketPolicies,
              extras: {
                detail_template_id: selectedDetailTemplateId || undefined,
                market_detail_templates: Object.keys(marketDetailTemplates).length > 0 ? marketDetailTemplates : undefined,
                name_rule_id: selectedNameRuleId || undefined,
              },
            })
            setPolicies(await policyApi.list().catch(() => []))
            setSelectedPolicyId(copied.id)
            openEdit(copied)
            showAlert('정책이 복사되었습니다', 'success')
          }}
            style={{ fontSize: '0.8125rem', padding: '0.4rem 1rem', background: 'rgba(76,154,255,0.1)', border: '1px solid rgba(76,154,255,0.3)', borderRadius: '8px', color: '#4C9AFF', cursor: 'pointer', whiteSpace: 'nowrap' }}
          >정책 복사</button>
          <button
            onClick={() => {
              setAiPolicyModalOpen(true)
              setAiPolicyChanges([])
              setAiPolicyApplied(0)
              setAiPolicyCommand('')
            }}
            style={{ fontSize: '0.8125rem', padding: '0.4rem 1rem', background: 'rgba(167,139,250,0.1)', border: '1px solid rgba(167,139,250,0.3)', borderRadius: '8px', color: '#A78BFA', cursor: 'pointer', whiteSpace: 'nowrap' }}
          >✦ AI정책변경</button>
        </div>
        {/* 정책명 표시/수정 — 신규/수정 모두 표시 */}
        {showForm && (
          <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', marginTop: '0.75rem' }}>
            <span style={{ color: '#888', fontSize: '0.8125rem' }}>정책명</span>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              style={{ ...inputStyle, flex: 1, maxWidth: '300px' }}
            />
            <input
              type="color"
              value={policyColor}
              onChange={(e) => { setPolicyColor(e.target.value); triggerAutoSave() }}
              title="정책 색상"
              style={{ width: 32, height: 32, padding: 2, background: 'none', border: '1px solid #2D2D2D', borderRadius: 6, cursor: 'pointer', flexShrink: 0 }}
            />
            <span style={{ fontSize: '0.75rem', color: '#666' }}>색상</span>
          </div>
        )}
      </div>

      {/* 정책 생성/수정 폼 */}
      {showForm && (
        <div>
          {/* 가격계산 정책 설정 */}
          <div style={{ ...card, padding: '1.5rem', marginBottom: '1.25rem' }}>
            <h3 style={{ fontSize: '1rem', fontWeight: 600, marginBottom: '1rem' }}>가격계산 정책 설정</h3>
            {/* 첫 번째 행: 체크박스 + 배송비 + 마진율 + 기준통화 */}
            <div style={{ display: 'flex', alignItems: 'center', gap: '1rem', flexWrap: 'wrap', marginBottom: '0.75rem' }}>
              <label style={{ display: 'flex', alignItems: 'center', gap: '0.4rem', fontSize: '0.8125rem', color: '#C5C5C5', cursor: 'pointer' }}>
                <input type="checkbox" checked={pricing.useRangeMargin} onChange={(e) => setPricing({ ...pricing, useRangeMargin: e.target.checked })} style={{ accentColor: '#FF8C00' }} />
                상품가격별 범위 마진 설정
              </label>
              <div style={{ display: 'flex', alignItems: 'center', gap: '0.375rem' }}>
                <span style={{ color: '#888', fontSize: '0.8125rem' }}>배송비</span>
                <NumInput value={pricing.shippingCost} onChange={(v) => { setPricing({ ...pricing, shippingCost: v }); triggerAutoSave() }} style={{ width: '90px' }} suffix="원" />
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: '0.375rem' }}>
                <span style={{ color: '#888', fontSize: '0.8125rem' }}>마진율</span>
                <NumInput value={pricing.marginRate} onChange={(v) => { setPricing({ ...pricing, marginRate: v }); triggerAutoSave() }} style={{ width: '62px' }} suffix="%" />
                <span style={{ color: '#555' }}>/</span>
                <NumInput value={pricing.extraCharge} onChange={(v) => { setPricing({ ...pricing, extraCharge: v }); triggerAutoSave() }} style={{ width: '80px' }} suffix="원" />
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: '0.375rem' }}>
                <span style={{ color: '#888', fontSize: '0.8125rem' }}>기준통화</span>
                <select style={{ ...inputStyle, width: 'auto' }} value={pricing.currency} onChange={(e) => setPricing({ ...pricing, currency: e.target.value })}>
                  <option value="KRW">KRW(원화)</option>
                  <option value="USD">USD</option>
                </select>
              </div>
            </div>

            {/* 범위별 마진율 행들 */}
            {pricing.useRangeMargin && (
              <div style={{ marginBottom: '0.75rem' }}>
                {pricing.rangeMargins.map((rm, idx) => (
                  <div key={idx} style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', marginBottom: '0.375rem' }}>
                    <NumInput value={rm.min} onChange={(v) => updateRangeMargin(idx, 'min', v)} style={{ width: '100px' }} />
                    <span style={{ color: '#888', fontSize: '0.75rem' }}>~</span>
                    <NumInput value={rm.max} onChange={(v) => updateRangeMargin(idx, 'max', v)} style={{ width: '100px' }} suffix="원" />
                    <span style={{ color: '#555', margin: '0 0.25rem' }}>│</span>
                    <span style={{ color: '#888', fontSize: '0.75rem' }}>마진율</span>
                    <NumInput value={rm.rate} onChange={(v) => updateRangeMargin(idx, 'rate', v)} style={{ width: '60px' }} suffix="%" />
                    <span style={{ color: '#555' }}>/</span>
                    <span style={{ color: '#888', fontSize: '0.75rem' }}>마진금액</span>
                    <NumInput value={rm.amount} onChange={(v) => updateRangeMargin(idx, 'amount', v)} style={{ width: '90px' }} suffix="원" placeholder="선택" />
                    <button onClick={() => removeRangeMargin(idx)} style={{ color: '#FF6B6B', background: 'transparent', fontSize: '0.75rem', cursor: 'pointer', border: 'none', marginLeft: '0.5rem' }}>삭제</button>
                  </div>
                ))}
                <button onClick={addRangeMargin} style={{ marginTop: '0.5rem', fontSize: '0.8rem', color: '#FF8C00', background: 'transparent', border: '1px dashed #FF8C00', borderRadius: '6px', padding: '0.25rem 0.75rem', cursor: 'pointer' }}>+ 가격범위 추가하기</button>
              </div>
            )}

            {/* 추가요금 + 관세 + 최소마진 + 할인 */}
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: '1rem', marginBottom: '0.75rem' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '0.375rem' }}>
                <span style={{ color: '#888', fontSize: '0.8125rem' }}>추가요금</span>
                <NumInput value={pricing.extraCharge} onChange={(v) => { setPricing({ ...pricing, extraCharge: v }); triggerAutoSave() }} style={{ width: '100px' }} suffix="원" />
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: '0.375rem' }}>
                <span style={{ color: '#888', fontSize: '0.8125rem' }}>관세/부가세</span>
                <select style={{ ...inputStyle, width: 'auto' }} value={pricing.customsIncluded ? 'Y' : 'N'} onChange={(e) => setPricing({ ...pricing, customsIncluded: e.target.value === 'Y' })}>
                  <option value="N">N</option><option value="Y">Y</option>
                </select>
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: '0.375rem' }}>
                <span style={{ color: '#888', fontSize: '0.8125rem' }}>최소마진금액</span>
                <NumInput value={pricing.minMarginAmount} onChange={(v) => { setPricing({ ...pricing, minMarginAmount: v }); triggerAutoSave() }} style={{ width: '100px' }} suffix="원" />
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: '0.375rem' }}>
                <span style={{ color: '#888', fontSize: '0.8125rem' }}>할인율/금액</span>
                <NumInput value={pricing.discountType === 'rate' ? pricing.discountValue : 0} onChange={(v) => { setPricing({ ...pricing, discountType: 'rate', discountValue: v }); triggerAutoSave() }} style={{ width: '60px' }} suffix="%" />
                <span style={{ color: '#555' }}>/</span>
                <NumInput value={pricing.discountType === 'amount' ? pricing.discountValue : 0} onChange={(v) => { setPricing({ ...pricing, discountType: 'amount', discountValue: v }); triggerAutoSave() }} style={{ width: '90px' }} suffix="원" />
              </div>
            </div>

            {/* 가격 계산 공식 */}
            <div style={{ background: 'rgba(0,0,0,0.3)', borderRadius: '8px', padding: '0.75rem 1rem', border: '1px solid #2D2D2D', fontSize: '0.8rem', color: '#888' }}>
              [상품금액] = [원가] + [마진] + [배송비] + [소싱처 추가 마진] + [관세] + [마켓 수수료]
            </div>

            {/* 소싱처별 추가 마진 설정 */}
            <div style={{ marginTop: '1rem', borderTop: '1px solid #2D2D2D', paddingTop: '0.75rem' }}>
              <div
                onClick={() => setShowSourceSiteMargins(!showSourceSiteMargins)}
                style={{ cursor: 'pointer', display: 'flex', alignItems: 'center', gap: '0.5rem', userSelect: 'none' }}
              >
                <span style={{ color: '#888', fontSize: '0.75rem' }}>{showSourceSiteMargins ? '▼' : '▶'}</span>
                <span style={{ color: '#C5C5C5', fontSize: '0.8125rem', fontWeight: 600 }}>소싱처별 추가 마진 설정</span>
                {Object.values(pricing.sourceSiteMargins).some(v => v.marginRate !== 0 || v.marginAmount !== 0) && (
                  <span style={{ fontSize: '0.7rem', color: '#FF8C00' }}>
                    ({fmtNum(Object.values(pricing.sourceSiteMargins).filter(v => v.marginRate !== 0 || v.marginAmount !== 0).length)}개 설정됨)
                  </span>
                )}
                <span style={{ fontSize: '0.7rem', color: '#555', marginLeft: '0.25rem' }}>— 기본 마진에 추가로 가산 (수수료 역산 전 적용)</span>
              </div>
              {showSourceSiteMargins && (
                <div style={{ marginTop: '0.75rem', display: 'flex', flexDirection: 'column', gap: '0.375rem' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', marginBottom: '0.25rem' }}>
                    <span style={{ width: '90px', fontSize: '0.7rem', color: '#555' }}>소싱처</span>
                    <span style={{ width: '100px', fontSize: '0.7rem', color: '#555', textAlign: 'center' }}>추가 마진율(%)</span>
                    <span style={{ width: '110px', fontSize: '0.7rem', color: '#555', textAlign: 'center' }}>추가 마진금액(원)</span>
                    <span style={{ width: '170px', fontSize: '0.7rem', color: '#555', textAlign: 'center' }}>적립금 사용가능 상품만</span>
                  </div>
                  {Object.keys(SOURCING_SITE_LABELS).map(siteId => {
                    const ssm = pricing.sourceSiteMargins[siteId] || { marginRate: 0, marginAmount: 0 }
                    const isSet = ssm.marginRate !== 0 || ssm.marginAmount !== 0
                    // 적립금 제한 정보를 수집하는 소싱처만 활성화 (현재 무신사만)
                    const supportsPointOnly = siteId === 'MUSINSA'
                    return (
                      <div key={siteId} style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                        <span style={{
                          width: '90px', fontSize: '0.75rem', fontWeight: 600,
                          color: isSet ? (SITE_COLORS[siteId] || '#888') : '#555',
                        }}>
                          {SOURCING_SITE_LABELS[siteId]}
                        </span>
                        <NumInput
                          value={ssm.marginRate}
                          onChange={(v) => updateSourceSiteMargin(siteId, 'marginRate', v)}
                          style={{ width: '80px' }}
                          suffix="%"
                        />
                        <NumInput
                          value={ssm.marginAmount}
                          onChange={(v) => updateSourceSiteMargin(siteId, 'marginAmount', v)}
                          style={{ width: '100px' }}
                          suffix="원"
                        />
                        <label
                          title={supportsPointOnly ? '체크 시 적립금 사용 가능 상품에만 추가 마진을 적용합니다' : '이 소싱처는 적립금 사용 정보를 수집하지 않습니다'}
                          style={{
                            width: '170px',
                            display: 'flex',
                            alignItems: 'center',
                            justifyContent: 'center',
                            gap: '0.25rem',
                            fontSize: '0.7rem',
                            color: supportsPointOnly ? '#C5C5C5' : '#444',
                            cursor: supportsPointOnly ? 'pointer' : 'not-allowed',
                            opacity: supportsPointOnly ? 1 : 0.45,
                          }}
                        >
                          <input
                            type="checkbox"
                            checked={Boolean(ssm.pointOnly)}
                            disabled={!supportsPointOnly}
                            onChange={(e) => toggleSourceSitePointOnly(siteId, e.target.checked)}
                            style={{ cursor: supportsPointOnly ? 'pointer' : 'not-allowed' }}
                          />
                          <span>적립금 사용가능만</span>
                        </label>
                        {isSet && (
                          <span style={{ fontSize: '0.7rem', color: '#FF8C00' }}>
                            {ssm.marginRate > 0 ? '+' : ''}{ssm.marginRate !== 0 ? `${ssm.marginRate}%` : ''}{ssm.marginRate !== 0 && ssm.marginAmount !== 0 ? ' + ' : ''}{ssm.marginAmount > 0 ? '+' : ''}{ssm.marginAmount !== 0 ? `${fmtNum(ssm.marginAmount)}원` : ''}
                            {ssm.pointOnly && supportsPointOnly ? ' · 적립금가능만' : ''}
                          </span>
                        )}
                      </div>
                    )
                  })}
                </div>
              )}
            </div>
          </div>



          {/* 마켓정책 설정 */}
          <div style={{ ...card, padding: '1.5rem', marginBottom: '1.25rem' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', marginBottom: '1rem' }}>
              <h4 style={{ fontSize: '0.8125rem', fontWeight: 600, color: '#FF8C00', textTransform: 'uppercase', letterSpacing: '0.05em' }}>마켓정책 설정</h4>
              <span style={{ fontSize: '0.75rem', color: '#888' }}>** 마켓 선택 후 전송에 필요한 기본 설정값 입력</span>
            </div>
            <div style={{ marginBottom: '1rem' }}>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.25rem', alignItems: 'center', marginBottom: '0.375rem' }}>
                <span style={{ fontSize: '0.68rem', color: '#FF8C00', fontWeight: 600, padding: '0.25rem 0.375rem 0.25rem 0', whiteSpace: 'nowrap' }}>국내</span>
                {POLICY_MARKETS_DOMESTIC.map(m => (
                  <button key={m} onClick={() => setMarketPolicyTab(m)}
                    style={{ padding: '0.375rem 0.75rem', borderRadius: '6px', fontSize: '0.75rem', cursor: 'pointer', border: marketPolicyTab === m ? '1px solid #FF8C00' : '1px solid #2D2D2D', background: marketPolicyTab === m ? 'rgba(255,140,0,0.12)' : 'transparent', color: marketPolicyTab === m ? '#FF8C00' : '#888' }}
                  >{m === '신세계몰(전시)' ? '신세계몰' : m}</button>
                ))}
              </div>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.25rem', alignItems: 'center' }}>
                <span style={{ fontSize: '0.68rem', color: '#4C9AFF', fontWeight: 600, padding: '0.25rem 0.375rem 0.25rem 0', whiteSpace: 'nowrap' }}>해외</span>
                {POLICY_MARKETS_OVERSEAS.map(m => (
                  <button key={m} onClick={() => setMarketPolicyTab(m)}
                    style={{ padding: '0.375rem 0.75rem', borderRadius: '6px', fontSize: '0.75rem', cursor: 'pointer', border: marketPolicyTab === m ? '1px solid #FF8C00' : '1px solid #2D2D2D', background: marketPolicyTab === m ? 'rgba(255,140,0,0.12)' : 'transparent', color: marketPolicyTab === m ? '#FF8C00' : '#888' }}
                  >{m}</button>
                ))}
              </div>
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '0.75rem' }}>
              {/* 연결 계정 선택 */}
              <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', gridColumn: '1 / -1' }}>
                <span style={{ color: '#888', fontSize: '0.8125rem', minWidth: '80px' }}>연결 계정</span>
                {(() => {
                  const accsForMarket = marketAccounts.filter(a => a.market_type === MARKET_KEY_MAP[marketPolicyTab])
                  // 현재 존재하는 계정 ID만 필터 (삭제된 계정의 오래된 ID 제거)
                  const rawIds = mp.accountIds?.length ? mp.accountIds : (mp.accountId ? [mp.accountId] : [])
                  const linkedIds = rawIds.filter((id: string) => accsForMarket.some(a => a.id === id))
                  const toggleAccLink = (accId: string) => {
                    const next = linkedIds.includes(accId) ? linkedIds.filter(x => x !== accId) : [...linkedIds, accId]
                    setCurrentMarketPolicy({ ...mp, accountIds: next, accountId: next[0] || '' })
                    triggerAutoSave()
                  }
                  return accsForMarket.length > 0 ? (
                    <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem', flex: 1, flexWrap: 'wrap' }}>
                      {accsForMarket.map(acc => {
                        const linked = linkedIds.includes(acc.id)
                        return (
                          <label key={acc.id} style={{ display: 'inline-flex', alignItems: 'center', gap: '0.25rem', cursor: 'pointer', fontSize: '0.8125rem', color: linked ? '#E5E5E5' : '#666' }}>
                            <input type="checkbox" checked={linked} onChange={() => toggleAccLink(acc.id)} style={{ accentColor: '#51CF66' }} />
                            {acc.business_name || acc.market_name}({acc.seller_id || acc.account_label || '-'})
                          </label>
                        )
                      })}
                      {linkedIds.length > 0 && (
                        <span style={{ fontSize: '0.7rem', color: '#51CF66', padding: '0.15rem 0.4rem', background: 'rgba(81,207,102,0.1)', borderRadius: '4px' }}>{fmtNum(linkedIds.length)}개 연결</span>
                      )}
                    </div>
                  ) : (
                    <span style={{ fontSize: '0.8125rem', color: '#666' }}>설정 탭에서 {marketPolicyTab} 계정을 먼저 등록해주세요</span>
                  )
                })()}
              </div>
              {/* 롯데홈쇼핑 전용: MD상품군, 표준카테고리, 전시카테고리, 브랜드 + 배송정책/품목정보 */}
              {marketPolicyTab === '롯데홈쇼핑' && (
                <div style={{ gridColumn: '1 / -1', borderBottom: '1px solid #2D2D2D', paddingBottom: '0.75rem', marginBottom: '0.25rem', display: 'flex', flexDirection: 'column', gap: '0.75rem' }}>
                  <span style={{ fontSize: '0.75rem', color: '#FF8C00', fontWeight: 600 }}>롯데홈쇼핑 기본 설정</span>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem', maxWidth: '50%' }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '0.375rem' }}>
                      <span style={{ color: '#888', fontSize: '0.8125rem', minWidth: '72px', flexShrink: 0 }}>MD상품군</span>
                      <select style={{ flex: 1, minWidth: 0, padding: '0.3rem 0.4rem', fontSize: '0.8rem', background: '#1A1A1A', border: '1px solid #2D2D2D', borderRadius: '4px', color: '#E5E5E5' }}
                        value={lottePolicy.mdGsgrNo}
                        onChange={e => {
                          const selected = lotteMdGroups.find(g => g.md_gsgr_no === e.target.value)
                          setLottePolicy(p => ({ ...p, mdGsgrNo: e.target.value, mdGsgrNm: selected?.md_gsgr_nm || '', dispNo: '', dispNm: '', stdCatNo: '', stdCatNm: '', ecGoodsArtcCd: '', ecGoodsArtcNm: '' }))
                          setLotteStdCategories([])
                        }}>
                        <option value="">{lotteMdLoading ? '불러오는 중...' : '선택하세요'}</option>
                        {lotteMdGroups.map(g => <option key={g.md_gsgr_no} value={g.md_gsgr_no}>{g.md_gsgr_nm}</option>)}
                      </select>
                    </div>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '0.375rem' }}>
                      <span style={{ color: '#888', fontSize: '0.8125rem', minWidth: '72px', flexShrink: 0 }}>전시카테고리</span>
                      <select style={{ flex: 1, minWidth: 0, padding: '0.3rem 0.4rem', fontSize: '0.8rem', background: '#1A1A1A', border: '1px solid #2D2D2D', borderRadius: '4px', color: '#E5E5E5', opacity: !lottePolicy.mdGsgrNo ? 0.5 : 1 }}
                        value={lottePolicy.dispNo} disabled={!lottePolicy.mdGsgrNo}
                        onChange={e => {
                          const selected = lotteCategories.find(c => c.disp_no === e.target.value)
                          setLottePolicy(p => ({ ...p, dispNo: e.target.value, dispNm: selected?.disp_nm || '', stdCatNo: '', stdCatNm: '' }))
                          setLotteStdCategories([])
                        }}>
                        <option value="">{!lottePolicy.mdGsgrNo ? 'MD상품군 먼저 선택' : lotteCatLoading ? '불러오는 중...' : '선택하세요'}</option>
                        {lotteCategories.map(c => {
                          const label = c.disp_tp_cd === '10' ? '[필수] ' : c.disp_tp_cd === '20' ? '[추가] ' : ''
                          return <option key={c.disp_no} value={c.disp_no}>{label}{c.shop_pos || c.disp_nm}</option>
                        })}
                      </select>
                    </div>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '0.375rem' }}>
                      <span style={{ color: '#888', fontSize: '0.8125rem', minWidth: '72px', flexShrink: 0 }}>표준카테고리</span>
                      <select style={{ flex: 1, minWidth: 0, padding: '0.3rem 0.4rem', fontSize: '0.8rem', background: '#1A1A1A', border: '1px solid #2D2D2D', borderRadius: '4px', color: '#E5E5E5', opacity: !lottePolicy.dispNo ? 0.5 : 1 }}
                        value={lottePolicy.stdCatNo} disabled={!lottePolicy.dispNo}
                        onChange={e => {
                          const sel = lotteStdCategories.find(c => c.no === e.target.value)
                          setLottePolicy(p => ({ ...p, stdCatNo: e.target.value, stdCatNm: sel?.nm || '' }))
                        }}>
                        <option value="">{!lottePolicy.dispNo ? '전시카테고리 먼저 선택' : lotteStdCatLoading ? '불러오는 중...' : lotteStdCategories.length === 0 ? '매핑된 없음' : '선택하세요'}</option>
                        {lotteStdCategories.map(c => <option key={c.no} value={c.no}>{c.path || c.nm}</option>)}
                      </select>
                    </div>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '0.375rem' }}>
                      <span style={{ color: '#888', fontSize: '0.8125rem', minWidth: '72px', flexShrink: 0 }}>과/면세</span>
                      <select style={{ flex: 1, minWidth: 0, padding: '0.3rem 0.4rem', fontSize: '0.8rem', background: '#1A1A1A', border: '1px solid #2D2D2D', borderRadius: '4px', color: '#E5E5E5' }}
                        value={lottePolicy.taxType}
                        onChange={e => setLottePolicy(p => ({ ...p, taxType: e.target.value }))}>
                        <option value="">선택하세요</option>
                        <option value="과세">과세</option>
                        <option value="면세">면세</option>
                        <option value="영세">영세</option>
                        <option value="비과세">비과세</option>
                      </select>
                    </div>
                    <div style={{ display: 'flex', alignItems: 'flex-start', gap: '0.375rem' }}>
                      <span style={{ color: '#888', fontSize: '0.8125rem', minWidth: '72px', flexShrink: 0, paddingTop: '0.3rem' }}>브랜드</span>
                      <div style={{ flex: 1, minWidth: 0, position: 'relative' }}>
                        {lottePolicy.brandMappings.map(b => (
                          <div key={b.brnd_no} style={{ display: 'flex', alignItems: 'center', gap: '0.3rem', marginBottom: '0.2rem' }}>
                            <span style={{ fontSize: '0.8rem', color: '#FF8C00', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>[{b.brnd_no}] {b.brnd_nm}</span>
                            <button onClick={() => setLottePolicy(p => ({ ...p, brandMappings: p.brandMappings.filter(x => x.brnd_no !== b.brnd_no) }))}
                              style={{ fontSize: '0.75rem', color: '#888', background: 'transparent', border: 'none', cursor: 'pointer', padding: '0 2px', flexShrink: 0 }}>✕</button>
                          </div>
                        ))}
                        <input
                          style={{ width: '100%', padding: '0.3rem 0.4rem', fontSize: '0.8rem', background: '#1A1A1A', border: '1px solid #2D2D2D', borderRadius: '4px', color: '#E5E5E5', boxSizing: 'border-box' }}
                          placeholder="브랜드명 검색 (예: nike)"
                          value={lotteBrandKeyword}
                          onChange={e => setLotteBrandKeyword(e.target.value)}
                        />
                        {lotteBrandLoading && (
                          <span style={{ position: 'absolute', right: '0.5rem', top: '50%', transform: 'translateY(-50%)', fontSize: '0.75rem', color: '#666' }}>검색 중...</span>
                        )}
                        {!lotteBrandLoading && lotteBrandError && lotteBrandKeyword.trim() && (
                          <div style={{ marginTop: '0.2rem', fontSize: '0.75rem', color: '#e05c5c' }}>{lotteBrandError}</div>
                        )}
                        {lotteBrands.length > 0 && (
                          <div style={{ position: 'absolute', top: 'calc(100% + 2px)', left: 0, right: 0, background: '#1A1A1A', border: '1px solid #3D3D3D', borderRadius: '4px', zIndex: 50, maxHeight: '180px', overflowY: 'auto' }}>
                            {lotteBrands.map(b => (
                              <div key={b.brnd_no}
                                onClick={() => { setLottePolicy(p => ({ ...p, brandMappings: p.brandMappings.some(x => x.brnd_no === b.brnd_no) ? p.brandMappings : [...p.brandMappings, { brnd_no: b.brnd_no, brnd_nm: b.brnd_nm }] })); setLotteBrandKeyword(''); setLotteBrands([]); setLotteBrandError('') }}
                                style={{ padding: '0.3rem 0.5rem', fontSize: '0.8rem', color: '#E5E5E5', cursor: 'pointer' }}
                                onMouseEnter={e => (e.currentTarget.style.background = 'rgba(255,140,0,0.12)')}
                                onMouseLeave={e => (e.currentTarget.style.background = 'transparent')}
                              >
                                [{b.brnd_no}] {b.brnd_nm}{b.brnd_en ? ` (${b.brnd_en})` : ''}
                              </div>
                            ))}
                          </div>
                        )}
                      </div>
                    </div>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '0.375rem' }}>
                      <span style={{ color: '#888', fontSize: '0.8125rem', minWidth: '72px', flexShrink: 0 }}>매입형태</span>
                      <select style={{ flex: 1, minWidth: 0, padding: '0.3rem 0.4rem', fontSize: '0.8rem', background: '#1A1A1A', border: '1px solid #2D2D2D', borderRadius: '4px', color: '#E5E5E5' }}
                        value={lottePolicy.purchaseType}
                        onChange={e => setLottePolicy(p => ({ ...p, purchaseType: e.target.value }))}>
                        <option value="">선택하세요</option>
                        <option value="특정">특정</option>
                        <option value="위탁판매">위탁판매</option>
                      </select>
                    </div>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '0.375rem' }}>
                      <span style={{ color: '#888', fontSize: '0.8125rem', minWidth: '72px', flexShrink: 0 }}>구입제한나이</span>
                      <select style={{ flex: 1, minWidth: 0, padding: '0.3rem 0.4rem', fontSize: '0.8rem', background: '#1A1A1A', border: '1px solid #2D2D2D', borderRadius: '4px', color: '#E5E5E5' }}
                        value={lottePolicy.ageLimit}
                        onChange={e => setLottePolicy(p => ({ ...p, ageLimit: e.target.value }))}>
                        <option value="">선택하세요</option>
                        <option value="전체">전체</option>
                        <option value="19세 이상">19세 이상</option>
                      </select>
                    </div>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '0.375rem' }}>
                      <span style={{ color: '#888', fontSize: '0.8125rem', minWidth: '72px', flexShrink: 0 }}>마진율</span>
                      <div style={{ display: 'flex', alignItems: 'center' }}>
                        <input
                          type="text"
                          style={{ width: '60px', padding: '0.3rem 0.4rem', fontSize: '0.8rem', background: '#1A1A1A', border: '1px solid #2D2D2D', borderRadius: '4px 0 0 4px', color: '#E5E5E5' }}
                          placeholder="30"
                          value={lottePolicy.marginRate}
                          onChange={e => { setLottePolicy(p => ({ ...p, marginRate: e.target.value })); setCurrentMarketPolicy({ ...mp, feeRate: Number(e.target.value) || 0 }); triggerAutoSave() }}
                        />
                        <span style={{ padding: '0.3rem 0.5rem', fontSize: '0.8rem', background: '#252525', border: '1px solid #2D2D2D', borderLeft: 'none', borderRadius: '0 4px 4px 0', color: '#888' }}>%</span>
                      </div>
                    </div>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '0.375rem' }}>
                      <span style={{ color: '#888', fontSize: '0.8125rem', minWidth: '72px', flexShrink: 0 }}>판매형태</span>
                      <select style={{ flex: 1, minWidth: 0, padding: '0.3rem 0.4rem', fontSize: '0.8rem', background: '#1A1A1A', border: '1px solid #2D2D2D', borderRadius: '4px', color: '#E5E5E5' }}
                        value={lottePolicy.saleType}
                        onChange={e => setLottePolicy(p => ({ ...p, saleType: e.target.value }))}>
                        <option value="">선택하세요</option>
                        <option value="정상">정상</option>
                        <option value="행사">행사</option>
                      </select>
                    </div>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '0.375rem' }}>
                      <span style={{ color: '#888', fontSize: '0.8125rem', minWidth: '72px', flexShrink: 0 }}>판매방식구분</span>
                      <select style={{ flex: 1, minWidth: 0, padding: '0.3rem 0.4rem', fontSize: '0.8rem', background: '#1A1A1A', border: '1px solid #2D2D2D', borderRadius: '4px', color: '#E5E5E5' }}
                        value={lottePolicy.saleMethod}
                        onChange={e => setLottePolicy(p => ({ ...p, saleMethod: e.target.value }))}>
                        <option value="">선택하세요</option>
                        <option value="해당없음">해당없음</option>
                        <option value="렌탈">렌탈</option>
                        <option value="대여">대여</option>
                        <option value="할부">할부</option>
                        <option value="구매대행">구매대행</option>
                        <option value="해외직구">해외직구</option>
                      </select>
                    </div>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '0.375rem' }}>
                      <span style={{ color: '#888', fontSize: '0.8125rem', minWidth: '72px', flexShrink: 0 }}>가격비교노출</span>
                      <div style={{ display: 'flex', gap: '0.25rem' }}>
                        {(['노출', '노출안함'] as const).map(v => (
                          <button key={v} onClick={() => setLottePolicy(p => ({ ...p, priceCompareDisplay: p.priceCompareDisplay === v ? '' : v }))}
                            style={{ padding: '0.25rem 0.6rem', fontSize: '0.8rem', borderRadius: '4px', cursor: 'pointer', border: '1px solid', borderColor: lottePolicy.priceCompareDisplay === v ? '#FF8C00' : '#2D2D2D', background: lottePolicy.priceCompareDisplay === v ? '#FF8C0022' : '#1A1A1A', color: lottePolicy.priceCompareDisplay === v ? '#FF8C00' : '#888', fontWeight: lottePolicy.priceCompareDisplay === v ? 600 : 400 }}>
                            {v}
                          </button>
                        ))}
                      </div>
                    </div>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '0.375rem' }}>
                      <span style={{ color: '#888', fontSize: '0.8125rem', minWidth: '72px', flexShrink: 0 }}>구매수량제한</span>
                      <div style={{ display: 'flex', gap: '0.25rem' }}>
                        {(['사용', '사용안함'] as const).map(v => (
                          <button key={v} onClick={() => setLottePolicy(p => ({ ...p, purchaseQtyLimit: p.purchaseQtyLimit === v ? '' : v }))}
                            style={{ padding: '0.25rem 0.6rem', fontSize: '0.8rem', borderRadius: '4px', cursor: 'pointer', border: '1px solid', borderColor: lottePolicy.purchaseQtyLimit === v ? '#FF8C00' : '#2D2D2D', background: lottePolicy.purchaseQtyLimit === v ? '#FF8C0022' : '#1A1A1A', color: lottePolicy.purchaseQtyLimit === v ? '#FF8C00' : '#888', fontWeight: lottePolicy.purchaseQtyLimit === v ? 600 : 400 }}>
                            {v}
                          </button>
                        ))}
                      </div>
                    </div>
                  </div>
                  <span style={{ fontSize: '0.75rem', color: '#FF8C00', fontWeight: 600 }}>기타상품정보</span>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem', maxWidth: '50%' }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '0.375rem' }}>
                      <span style={{ color: '#888', fontSize: '0.8125rem', minWidth: '72px', flexShrink: 0 }}>옵션수정여부</span>
                      <div style={{ display: 'flex', gap: '0.25rem' }}>
                        {(['수정함', '수정안함'] as const).map(v => (
                          <button key={v} onClick={() => setLottePolicy(p => ({ ...p, optionModify: p.optionModify === v ? '' : v }))}
                            style={{ padding: '0.25rem 0.6rem', fontSize: '0.8rem', borderRadius: '4px', cursor: 'pointer', border: '1px solid', borderColor: lottePolicy.optionModify === v ? '#FF8C00' : '#2D2D2D', background: lottePolicy.optionModify === v ? '#FF8C0022' : '#1A1A1A', color: lottePolicy.optionModify === v ? '#FF8C00' : '#888', fontWeight: lottePolicy.optionModify === v ? 600 : 400 }}>
                            {v}
                          </button>
                        ))}
                      </div>
                    </div>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '0.375rem' }}>
                      <span style={{ color: '#888', fontSize: '0.8125rem', minWidth: '72px', flexShrink: 0 }}>재고관리여부</span>
                      <div style={{ display: 'flex', gap: '0.25rem' }}>
                        {(['사용함', '사용안함'] as const).map(v => (
                          <button key={v} onClick={() => setLottePolicy(p => ({ ...p, optionStockMgmt: p.optionStockMgmt === v ? '' : v }))}
                            style={{ padding: '0.25rem 0.6rem', fontSize: '0.8rem', borderRadius: '4px', cursor: 'pointer', border: '1px solid', borderColor: lottePolicy.optionStockMgmt === v ? '#FF8C00' : '#2D2D2D', background: lottePolicy.optionStockMgmt === v ? '#FF8C0022' : '#1A1A1A', color: lottePolicy.optionStockMgmt === v ? '#FF8C00' : '#888', fontWeight: lottePolicy.optionStockMgmt === v ? 600 : 400 }}>
                            {v}
                          </button>
                        ))}
                      </div>
                    </div>
                    <div style={{ display: 'flex', flexDirection: 'column', gap: '0.25rem' }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: '0.375rem' }}>
                        <span style={{ color: '#888', fontSize: '0.8125rem', minWidth: '72px', flexShrink: 0 }}>이미지리사이징</span>
                        <div style={{ display: 'flex', gap: '0.25rem' }}>
                          {(['적용안함', '적용함'] as const).map(v => (
                            <button key={v} onClick={() => setLottePolicy(p => ({ ...p, imageResize: p.imageResize === v ? '' : v }))}
                              style={{ padding: '0.25rem 0.6rem', fontSize: '0.8rem', borderRadius: '4px', cursor: 'pointer', border: '1px solid', borderColor: lottePolicy.imageResize === v ? '#FF8C00' : '#2D2D2D', background: lottePolicy.imageResize === v ? '#FF8C0022' : '#1A1A1A', color: lottePolicy.imageResize === v ? '#FF8C00' : '#888', fontWeight: lottePolicy.imageResize === v ? 600 : 400 }}>
                              {v}
                            </button>
                          ))}
                        </div>
                      </div>
                      <span style={{ fontSize: '0.75rem', color: '#666', paddingLeft: '76px' }}>- 적용함 선택 시 상품 등록 시 이미지를 롯데홈쇼핑 규격에 맞게 자동 리사이징합니다.</span>
                      <span style={{ fontSize: '0.75rem', color: '#666', paddingLeft: '76px' }}>- 등록하시려는 이미지가 1024 x 1024 보다 클 경우, 1024 x 1024 사이즈로 리사이징 합니다.</span>
                    </div>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '0.375rem' }}>
                      <span style={{ color: '#888', fontSize: '0.8125rem', minWidth: '72px', flexShrink: 0 }}>출고일</span>
                      <NumInput value={mp.shippingDays || 3} onChange={(v) => { setCurrentMarketPolicy({ ...mp, shippingDays: v }); triggerAutoSave() }} style={{ width: '60px' }} suffix="일" />
                    </div>
                  </div>
                  <span style={{ fontSize: '0.75rem', color: '#FF8C00', fontWeight: 600 }}>품목정보</span>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem', maxWidth: '50%' }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '0.375rem' }}>
                      <span style={{ color: '#888', fontSize: '0.8125rem', minWidth: '72px', flexShrink: 0 }}>상품품목코드</span>
                      <select style={{ flex: 1, minWidth: 0, padding: '0.3rem 0.4rem', fontSize: '0.8rem', background: '#1A1A1A', border: '1px solid #2D2D2D', borderRadius: '4px', color: '#E5E5E5' }}
                        value={lottePolicy.ecGoodsArtcCd}
                        onChange={e => {
                          const sel = lotteMdGroups.find(g => g.md_gsgr_no === lottePolicy.mdGsgrNo)?.artcItems.find(a => a.artc === e.target.value)
                          setLottePolicy(p => ({ ...p, ecGoodsArtcCd: e.target.value, ecGoodsArtcNm: sel?.artcNm || '' }))
                        }}>
                        <option value="">{!lottePolicy.mdGsgrNo ? 'MD상품군 먼저 선택' : '선택하세요'}</option>
                        {(lotteMdGroups.find(g => g.md_gsgr_no === lottePolicy.mdGsgrNo)?.artcItems || []).map(a => (
                          <option key={a.artc} value={a.artc}>[{a.artc}] {a.artcNm}</option>
                        ))}
                      </select>
                    </div>
                  </div>
                  <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
                    <button disabled={lotteSaving} onClick={async () => {
                      setLotteSaving(true)
                      try {
                        const res = await request<{ success: boolean; message?: string }>(`${API_BASE}/api/v1/samba/proxy/lottehome/policy`, {
                          method: 'POST', body: JSON.stringify(lottePolicy),
                        })
                        showAlert(res.success ? '저장되었습니다.' : (res.message || '저장 실패'))
                      } catch { showAlert('저장 중 오류가 발생했습니다.') }
                      finally { setLotteSaving(false) }
                    }} style={{ padding: '0.4rem 1.25rem', background: lotteSaving ? '#333' : '#FF8C00', color: lotteSaving ? '#888' : '#111', border: 'none', borderRadius: '6px', fontWeight: 600, cursor: lotteSaving ? 'not-allowed' : 'pointer', fontSize: '0.8125rem' }}>
                      {lotteSaving ? '저장 중...' : '저장'}
                    </button>
                  </div>
                </div>
              )}
              {marketPolicyTab !== '롯데홈쇼핑' && (
                <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                  <span style={{ color: '#888', fontSize: '0.8125rem', minWidth: '80px' }}>배송형태</span>
                  <label style={{ display: 'flex', alignItems: 'center', gap: '0.25rem', cursor: 'pointer', fontSize: '0.8125rem', color: '#C5C5C5' }}>
                    <input type="radio" name="ship-type" checked={mp.shipType === 'domestic'} onChange={() => { setCurrentMarketPolicy({ ...mp, shipType: 'domestic' }); triggerAutoSave() }} /> 국내배송
                  </label>
                  <label style={{ display: 'flex', alignItems: 'center', gap: '0.25rem', cursor: 'pointer', fontSize: '0.8125rem', color: '#C5C5C5' }}>
                    <input type="radio" name="ship-type" checked={mp.shipType === 'overseas'} onChange={() => { setCurrentMarketPolicy({ ...mp, shipType: 'overseas' }); triggerAutoSave() }} /> 해외배송
                  </label>
                </div>
              )}
              {marketPolicyTab !== '롯데홈쇼핑' && marketPolicyTab !== '신세계몰(전시)' && (
                <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                  <span style={{ color: '#888', fontSize: '0.8125rem', minWidth: '80px' }}>수수료</span>
                  <NumInput value={mp.feeRate} onChange={(v) => { setCurrentMarketPolicy({ ...mp, feeRate: v }); triggerAutoSave() }} style={{ width: '70px' }} suffix="%" />
                </div>
              )}
              {marketPolicyTab !== '롯데홈쇼핑' && marketPolicyTab !== '신세계몰(전시)' && (
                <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                  <span style={{ color: '#888', fontSize: '0.8125rem', minWidth: '80px' }}>배송비</span>
                  <NumInput value={mp.shippingCost} onChange={(v) => { setCurrentMarketPolicy({ ...mp, shippingCost: v }); triggerAutoSave() }} style={{ width: '100px' }} suffix="원" />
                </div>
              )}
              {/* 11번가는 판매자 계정의 발송예정일 템플릿을 사용하므로 정책 출고일 미사용 / 롯데홈쇼핑·신세계몰은 자체 블록에서 출고일 표시 */}
              {marketPolicyTab !== '플레이오토' && marketPolicyTab !== '스마트스토어' && marketPolicyTab !== '11번가' && marketPolicyTab !== '롯데홈쇼핑' && marketPolicyTab !== '신세계몰(전시)' && (
              <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                <span style={{ color: '#888', fontSize: '0.8125rem', minWidth: '80px' }}>출고일</span>
                <NumInput value={mp.shippingDays || 3} onChange={(v) => { setCurrentMarketPolicy({ ...mp, shippingDays: v }); triggerAutoSave() }} style={{ width: '60px' }} suffix="일" />
              </div>
              )}
              {/* 플레이오토 전용: 원산지, 시중가 */}
              {marketPolicyTab === '플레이오토' && (
              <>
              <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                <span style={{ color: '#888', fontSize: '0.8125rem', minWidth: '80px' }}>원산지</span>
                <input type="text" value={mp.origin || ''} onChange={(e) => { setCurrentMarketPolicy({ ...mp, origin: e.target.value }); triggerAutoSave() }}
                  placeholder="국내=서울=서울시" style={{ padding: '0.375rem 0.5rem', fontSize: '0.8125rem', background: '#1A1A1A', border: '1px solid #2D2D2D', borderRadius: '4px', color: '#E5E5E5', outline: 'none', width: '200px' }} />
                <span style={{ color: '#555', fontSize: '0.72rem' }}>예: 국내=서울=서울시, 기타=기타=기타</span>
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                <span style={{ color: '#888', fontSize: '0.8125rem', minWidth: '80px' }}>시중가</span>
                <NumInput value={mp.streetPriceRate || 0} onChange={(v) => { setCurrentMarketPolicy({ ...mp, streetPriceRate: v }); triggerAutoSave() }} style={{ width: '70px' }} suffix="%" />
                <span style={{ color: '#555', fontSize: '0.72rem' }}>판매가 대비 시중가 비율 (예: 150 → 판매가의 1.5배)</span>
              </div>
              </>
              )}
              {/* 신세계몰 전용: 주문수량, 브랜드, 고시정보 */}
              {marketPolicyTab === '신세계몰(전시)' && (
                <div style={{ gridColumn: '1 / -1', borderTop: '1px solid #2D2D2D', paddingTop: '0.75rem', display: 'flex', flexDirection: 'column', gap: '0.75rem' }}>
                  <span style={{ fontSize: '0.75rem', color: '#FF8C00', fontWeight: 600 }}>신세계몰 기본 설정</span>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem', maxWidth: '50%' }}>
                  <div style={{ display: 'flex', alignItems: 'flex-start', gap: '0.5rem' }}>
                    <span style={{ color: '#888', fontSize: '0.8125rem', minWidth: '80px', flexShrink: 0, paddingTop: '0.3rem' }}>브랜드</span>
                    <div style={{ flex: 1, minWidth: 0, position: 'relative' }}>
                      {(mp.ssgBrandMappings || []).map(b => (
                        <div key={b.brandId} style={{ display: 'flex', alignItems: 'center', gap: '0.3rem', marginBottom: '0.2rem' }}>
                          <span style={{ fontSize: '0.8rem', color: '#FF8C00', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>[{b.brandId}] {b.brandNm}</span>
                          <button onClick={() => { setCurrentMarketPolicy({ ...mp, ssgBrandMappings: (mp.ssgBrandMappings || []).filter(x => x.brandId !== b.brandId) }); triggerAutoSave() }}
                            style={{ fontSize: '0.75rem', color: '#888', background: 'transparent', border: 'none', cursor: 'pointer', padding: '0 2px', flexShrink: 0 }}>✕</button>
                        </div>
                      ))}
                      <input
                        style={{ width: '100%', padding: '0.3rem 0.4rem', fontSize: '0.8rem', background: '#1A1A1A', border: '1px solid #2D2D2D', borderRadius: '4px', color: '#E5E5E5', boxSizing: 'border-box' }}
                        placeholder="브랜드명 검색 (예: nike)"
                        value={ssgBrandKeyword}
                        onChange={e => setSsgBrandKeyword(e.target.value)}
                      />
                      {ssgBrandLoading && (
                        <span style={{ position: 'absolute', right: '0.5rem', top: '50%', transform: 'translateY(-50%)', fontSize: '0.75rem', color: '#666' }}>검색 중...</span>
                      )}
                      {!ssgBrandLoading && ssgBrandError && ssgBrandKeyword.trim() && (
                        <div style={{ marginTop: '0.2rem', fontSize: '0.75rem', color: '#e05c5c' }}>{ssgBrandError}</div>
                      )}
                      {ssgBrands.length > 0 && (
                        <div style={{ position: 'absolute', top: 'calc(100% + 2px)', left: 0, right: 0, background: '#1A1A1A', border: '1px solid #3D3D3D', borderRadius: '4px', zIndex: 50, maxHeight: '180px', overflowY: 'auto' }}>
                          {ssgBrands.map(b => (
                            <div key={b.brandId}
                              onClick={() => { setCurrentMarketPolicy({ ...mp, ssgBrandMappings: (mp.ssgBrandMappings || []).some(x => x.brandId === b.brandId) ? (mp.ssgBrandMappings || []) : [...(mp.ssgBrandMappings || []), { brandId: b.brandId, brandNm: b.brandNm }] }); triggerAutoSave(); setSsgBrandKeyword(''); setSsgBrands([]); setSsgBrandError('') }}
                              style={{ padding: '0.3rem 0.5rem', fontSize: '0.8rem', color: '#E5E5E5', cursor: 'pointer' }}
                              onMouseEnter={e => (e.currentTarget.style.background = 'rgba(255,140,0,0.12)')}
                              onMouseLeave={e => (e.currentTarget.style.background = 'transparent')}
                            >
                              [{b.brandId}] {b.brandNm}
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  </div>
                  {/* 신세계몰 전용: 고시정보 */}
                  <div style={{ borderTop: '1px solid #2D2D2D', paddingTop: '0.75rem', display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
                    <span style={{ fontSize: '0.75rem', color: '#FF8C00', fontWeight: 600 }}>고시정보</span>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                      <span style={{ color: '#888', fontSize: '0.8125rem', minWidth: '120px', flexShrink: 0 }}>A/S 책임자 및 전화번호</span>
                      <input
                        type="text"
                        value={mp.ssgNoticeAsContact || ''}
                        onChange={e => { setCurrentMarketPolicy({ ...mp, ssgNoticeAsContact: e.target.value }); triggerAutoSave() }}
                        placeholder="예: 고객센터 010-1234-5678"
                        style={{ flex: 1, padding: '0.3rem 0.4rem', fontSize: '0.8rem', background: '#1A1A1A', border: '1px solid #2D2D2D', borderRadius: '4px', color: '#E5E5E5', outline: 'none' }}
                      />
                    </div>
                    <span style={{ fontSize: '0.72rem', color: '#444' }}>나머지 항목(소재·색상·치수·수입여부 등)은 소싱 데이터 자동 입력</span>
                  </div>
                  </div>
                </div>
              )}
              {/* GS샵 전용: MD 협의 마진율 */}
              {marketPolicyTab === 'GS샵' && (
                <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                  <span style={{ color: '#888', fontSize: '0.8125rem', minWidth: '80px' }}>마켓마진율</span>
                  <NumInput value={mp.gsMarginRate || 0} onChange={(v) => { setCurrentMarketPolicy({ ...mp, gsMarginRate: v }); triggerAutoSave() }} style={{ width: '70px' }} suffix="%" />
                  <span style={{ color: '#666', fontSize: '0.75rem' }}>MD 협의 필수항목</span>
                </div>
              )}
              {/* 옥션/지마켓 전용: 복수구매 할인, 스마일캐시 지급 */}
              {(marketPolicyTab === '옥션' || marketPolicyTab === '지마켓') && (
                <>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', marginTop: '0.5rem', borderTop: '1px solid #2D2D2D', paddingTop: '0.75rem' }}>
                    <span style={{ color: '#888', fontSize: '0.8125rem', minWidth: '80px' }}>복수구매 할인</span>
                    <NumInput value={mp.bulkDiscountQty || 2} onChange={(v) => { setCurrentMarketPolicy({ ...mp, bulkDiscountQty: v }); triggerAutoSave() }} style={{ width: '50px' }} suffix="개" />
                    <span style={{ color: '#666', fontSize: '0.8rem' }}>이상</span>
                    <NumInput value={mp.bulkDiscountPrice || 0} onChange={(v) => { setCurrentMarketPolicy({ ...mp, bulkDiscountPrice: v }); triggerAutoSave() }} style={{ width: '80px' }} suffix="원" />
                    <span style={{ color: '#666', fontSize: '0.8rem' }}>할인</span>
                  </div>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                    <span style={{ color: '#888', fontSize: '0.8125rem', minWidth: '80px' }}>스마일캐시</span>
                    <NumInput value={mp.smileCashRate || 0} onChange={(v) => { setCurrentMarketPolicy({ ...mp, smileCashRate: v }); triggerAutoSave() }} style={{ width: '60px' }} suffix="%" />
                    <span style={{ color: '#666', fontSize: '0.8rem' }}>지급</span>
                  </div>
                </>
              )}
            </div>
          </div>

        </div>
      )}

      {/* 정책 선택 드롭다운 (테이블 대체) */}
      {policies.length > 0 && !showForm && (
        <div style={{ ...card, padding: '1rem 1.5rem', marginBottom: '0.5rem', display: 'flex', alignItems: 'center', gap: '1rem' }}>
          <span style={{ color: '#888', fontSize: '0.8125rem' }}>저장된 정책</span>
          <select style={{ padding: '0.375rem 0.75rem', fontSize: '0.8125rem', background: '#1A1A1A', border: '1px solid #2D2D2D', borderRadius: '6px', color: '#E5E5E5', outline: 'none', flex: 1, maxWidth: '300px' }}
            value={selectedPolicyId || ''} onChange={(e) => { const p = policies.find(x => x.id === e.target.value); if (p) openEdit(p) }}>
            <option value="">정책 선택하여 수정</option>
            {policies.map(p => <option key={p.id} value={p.id}>{p.name} ({p.site_name || '전체'})</option>)}
          </select>
          <button onClick={() => { const p = policies.find(x => x.id === selectedPolicyId); if (p) handleDelete(p.id) }}
            style={{ padding: '0.375rem 0.875rem', fontSize: '0.8rem', background: 'rgba(255,107,107,0.1)', border: '1px solid rgba(255,107,107,0.3)', borderRadius: '6px', color: '#FF6B6B', cursor: 'pointer' }}>삭제</button>
        </div>
      )}

      {/* 상세페이지 + 상품/옵션명 관리 (2컬럼) */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '1.5rem', marginTop: '1.5rem' }}>

      {/* 상세페이지 */}
      <div style={{ ...card, padding: '1.5rem' }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '1rem' }}>
          <span style={{ fontSize: '1rem', fontWeight: 700 }}>상세페이지</span>
          <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
            <span style={{ color: '#888', fontSize: '0.8125rem' }}>템플릿선택</span>
            <select style={{ ...inputStyle, width: '200px' }} value={selectedDetailTemplateId} onChange={async (e) => {
              if (e.target.value === '__new__') {
                const t = await detailTemplateApi.create({ name: `템플릿 ${detailTemplates.length + 1}` })
                setDetailTemplates(prev => [t, ...prev])
                setSelectedDetailTemplateId(t.id)
              } else {
                setSelectedDetailTemplateId(e.target.value)
              }
            }}>
              <option value="">선택하세요</option>
              <option value="__new__">+ 신규생성</option>
              {detailTemplates.map(t => <option key={t.id} value={t.id}>{t.name}</option>)}
            </select>
            <button onClick={async () => {
              if (!selectedDetailTemplateId) return
              const t = detailTemplates.find(x => x.id === selectedDetailTemplateId)
              if (!t) return
              await detailTemplateApi.update(t.id, { name: t.name, main_image_index: t.main_image_index }).catch(() => {})
              showAlert('템플릿이 저장되었습니다.', 'success')
            }} style={{ padding: '0.3rem 0.75rem', fontSize: '0.78rem', background: 'rgba(255,140,0,0.1)', border: '1px solid rgba(255,140,0,0.3)', borderRadius: '6px', color: '#FF8C00', cursor: 'pointer', whiteSpace: 'nowrap' }}>저장</button>
            <button onClick={async () => {
              if (!selectedDetailTemplateId) return
              const src = detailTemplates.find(x => x.id === selectedDetailTemplateId)
              if (!src) return
              try {
                const created = await detailTemplateApi.create({
                  name: `${src.name} (복사)`,
                  main_image_index: src.main_image_index,
                  top_html: src.top_html,
                  bottom_html: src.bottom_html,
                  top_image_s3_key: src.top_image_s3_key,
                  bottom_image_s3_key: src.bottom_image_s3_key,
                  img_checks: src.img_checks,
                  img_order: src.img_order,
                })
                setDetailTemplates(prev => [created, ...prev])
                setSelectedDetailTemplateId(created.id)
                showAlert('템플릿이 복사되었습니다.', 'success')
              } catch { showAlert('복사 실패', 'error') }
            }} style={{ padding: '0.3rem 0.75rem', fontSize: '0.78rem', background: 'rgba(76,154,255,0.1)', border: '1px solid rgba(76,154,255,0.3)', borderRadius: '6px', color: '#4C9AFF', cursor: 'pointer', whiteSpace: 'nowrap' }}>복사</button>
            <button onClick={async () => {
              if (!selectedDetailTemplateId) return
              await detailTemplateApi.delete(selectedDetailTemplateId).catch(() => {})
              setDetailTemplates(prev => prev.filter(x => x.id !== selectedDetailTemplateId))
              setSelectedDetailTemplateId('')
            }} style={{ padding: '0.3rem 0.75rem', fontSize: '0.78rem', background: 'rgba(255,107,107,0.1)', border: '1px solid rgba(255,107,107,0.3)', borderRadius: '6px', color: '#FF6B6B', cursor: 'pointer', whiteSpace: 'nowrap' }}>설정 삭제</button>
          </div>
        </div>

        {selectedDetailTemplateId && (() => {
          const t = detailTemplates.find(x => x.id === selectedDetailTemplateId)
          if (!t) return null

          // 이미지 순서 state (템플릿별로 관리 — 간단하게 로컬)
          const IMG_ITEMS = [
            { id: 'topImg', label: '상단이미지', color: '#FF8C00', bg: 'rgba(255,140,0,0.05)', border: 'rgba(255,140,0,0.3)' },
            { id: 'main', label: '대표이미지', color: '#4C9AFF', bg: 'rgba(76,154,255,0.05)', border: 'rgba(76,154,255,0.3)' },
            { id: 'sub', label: '대표추가이미지', color: '#51CF66', bg: 'rgba(81,207,102,0.05)', border: 'rgba(81,207,102,0.3)' },
            { id: 'title', label: '상품제목', color: '#FFD93D', bg: 'rgba(255,217,61,0.05)', border: 'rgba(255,217,61,0.3)' },
            { id: 'option', label: '옵션이미지', color: '#888', bg: 'rgba(136,136,136,0.05)', border: 'rgba(136,136,136,0.3)' },
            { id: 'detail', label: '상세이미지', color: '#CC5DE8', bg: 'rgba(204,93,232,0.05)', border: 'rgba(204,93,232,0.3)' },
            { id: 'bottomImg', label: '하단이미지', color: '#FF8C00', bg: 'rgba(255,140,0,0.05)', border: 'rgba(255,140,0,0.3)' },
          ]

          // 외부 호스팅 이미지 URL 저장 핸들러
          const handleImageUrlSave = async (position: 'top' | 'bottom', url: string) => {
            const key = position === 'top' ? 'topImg' : 'bottomImg'
            setImgSaving(key)
            try {
              const field = position === 'top' ? 'top_image_s3_key' : 'bottom_image_s3_key'
              const updated = await detailTemplateApi.update(t.id, { [field]: url || null } as Partial<SambaDetailTemplate>)
              setDetailTemplates(prev => prev.map(x => x.id === t.id ? { ...x, ...updated } : x))
              if (url) showAlert(`${position === 'top' ? '상단' : '하단'} 이미지 저장 완료`, 'success')
            } catch (e) {
              showAlert(`저장 실패: ${e instanceof Error ? e.message : '알 수 없는 오류'}`)
            } finally {
              setImgSaving('')
            }
          }

          // 이미지 체크 토글 핸들러 — DB에도 저장
          const handleImgCheckToggle = async (itemId: string, checked: boolean) => {
            const next = { ...imgChecks, [itemId]: checked }
            setImgChecks(next)
            try {
              await detailTemplateApi.update(t.id, { img_checks: next })
              // detailTemplates state도 갱신하여 재선택 시 stale 값 방지
              setDetailTemplates(prev => prev.map(dt =>
                dt.id === t.id ? { ...dt, img_checks: next } : dt
              ))
            } catch (e) {
              console.error('img_checks 저장 실패:', e)
            }
          }

          // 체크된 항목만 imgOrder 순서대로 표시
          const checkedItems = imgOrder
            .map(id => IMG_ITEMS.find(item => item.id === id))
            .filter((item): item is NonNullable<typeof item> => !!item && (imgChecks[item.id] ?? false))

          return (
            <div>
              {/* 대표 이미지 설정 */}
              <h4 style={{ fontSize: '0.9375rem', fontWeight: 700, marginBottom: '0.75rem' }}>대표 이미지 설정</h4>
              <div style={{ display: 'flex', flexDirection: 'column', gap: '0.625rem', marginBottom: '1.25rem' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '1rem' }}>
                  <span style={{ color: '#888', fontSize: '0.8125rem', minWidth: '80px' }}>템플릿명</span>
                  <input value={t.name}
                    onChange={(e) => setDetailTemplates(prev => prev.map(x => x.id === t.id ? { ...x, name: e.target.value } : x))}
                    onBlur={() => detailTemplateApi.update(t.id, { name: t.name }).catch(() => {})}
                    style={{ ...inputStyle, width: '300px' }} />
                </div>
                <div style={{ display: 'flex', alignItems: 'center', gap: '1rem' }}>
                  <span style={{ color: '#888', fontSize: '0.8125rem', minWidth: '80px' }}>대표이미지</span>
                  <input type="number" min={1} max={10} value={(t.main_image_index ?? 0) + 1}
                    onChange={(e) => {
                      const v = Math.max(0, Number(e.target.value) - 1)
                      setDetailTemplates(prev => prev.map(x => x.id === t.id ? { ...x, main_image_index: v } : x))
                      detailTemplateApi.update(t.id, { main_image_index: v }).catch(() => {})
                    }}
                    style={{ ...inputStyle, width: '50px', textAlign: 'center' }} />
                  <span style={{ color: '#888', fontSize: '0.8125rem' }}>번 째 이미지 사용</span>
                </div>
                {/* 상단/하단 이미지 URL 입력 */}
                <div style={{ display: 'flex', alignItems: 'flex-start', gap: '1rem' }}>
                  <span style={{ color: '#888', fontSize: '0.8125rem', minWidth: '80px', paddingTop: '0.5rem' }}>상단이미지</span>
                  <div style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: '0.25rem' }}>
                    <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'center' }}>
                      <input
                        type="text"
                        placeholder="https://gi.esmplus.com/... 외부 호스팅 이미지 URL"
                        defaultValue={t.top_image_s3_key || ''}
                        onBlur={(e) => {
                          const url = e.target.value.trim()
                          if (url !== (t.top_image_s3_key || '')) handleImageUrlSave('top', url)
                        }}
                        onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur() }}
                        disabled={imgSaving === 'topImg'}
                        style={{ ...inputStyle, flex: 1, fontSize: '0.8rem' }}
                      />
                      {t.top_image_s3_key && <span style={{ color: '#51CF66', fontSize: '0.75rem', whiteSpace: 'nowrap' }}>등록됨</span>}
                    </div>
                    <span style={{ color: '#555', fontSize: '0.7rem' }}>ESM+ 등 외부 호스팅 이미지 주소를 입력하세요 (입력 후 Enter 또는 포커스 해제 시 저장)</span>
                  </div>
                </div>
                <div style={{ display: 'flex', alignItems: 'flex-start', gap: '1rem' }}>
                  <span style={{ color: '#888', fontSize: '0.8125rem', minWidth: '80px', paddingTop: '0.5rem' }}>하단이미지</span>
                  <div style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: '0.25rem' }}>
                    <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'center' }}>
                      <input
                        type="text"
                        placeholder="https://gi.esmplus.com/... 외부 호스팅 이미지 URL"
                        defaultValue={t.bottom_image_s3_key || ''}
                        onBlur={(e) => {
                          const url = e.target.value.trim()
                          if (url !== (t.bottom_image_s3_key || '')) handleImageUrlSave('bottom', url)
                        }}
                        onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur() }}
                        disabled={imgSaving === 'bottomImg'}
                        style={{ ...inputStyle, flex: 1, fontSize: '0.8rem' }}
                      />
                      {t.bottom_image_s3_key && <span style={{ color: '#51CF66', fontSize: '0.75rem', whiteSpace: 'nowrap' }}>등록됨</span>}
                    </div>
                    <span style={{ color: '#555', fontSize: '0.7rem' }}>ESM+ 등 외부 호스팅 이미지 주소를 입력하세요 (입력 후 Enter 또는 포커스 해제 시 저장)</span>
                  </div>
                </div>
              </div>
              {/* 상세페이지 이미지 순서 설정 */}
              <h4 style={{ fontSize: '0.9375rem', fontWeight: 700, marginBottom: '0.75rem', borderTop: '1px solid #2D2D2D', paddingTop: '1rem' }}>상세페이지 이미지 순서 설정</h4>
              <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', marginBottom: '1rem' }}>
                <span style={{ color: '#888', fontSize: '0.8125rem', minWidth: '65px' }}>기본 이미지</span>
                {IMG_ITEMS.map(item => (
                  <label key={item.id} style={{ display: 'flex', alignItems: 'center', gap: '0.25rem', fontSize: '0.8rem', cursor: 'pointer', color: '#C5C5C5' }}>
                    <input type="checkbox" checked={imgChecks[item.id] ?? false}
                      onChange={(e) => handleImgCheckToggle(item.id, e.target.checked)}
                      disabled={imgSaving === item.id}
                      style={{ accentColor: '#FF8C00' }} />
                    {item.label}
                  </label>
                ))}
              </div>

              {/* 미리보기 — 최신 상품 기준 + 드래그 순서 변경 */}
              <div style={{ background: '#111', border: '1px solid #2D2D2D', borderRadius: '8px', padding: '1rem', minHeight: '200px' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '0.5rem' }}>
                  {previewProduct ? (
                    <span style={{ fontSize: '0.75rem', color: '#888' }}>예시: {previewProduct.name?.slice(0, 30)}{(previewProduct.name?.length || 0) > 30 ? '...' : ''}</span>
                  ) : (
                    <span style={{ fontSize: '0.75rem', color: '#555' }}>수집상품이 없습니다</span>
                  )}
                  <span style={{ fontSize: '0.75rem', color: '#666' }}>미리보기 (드래그로 순서변경)</span>
                </div>
                {checkedItems.length === 0 ? (
                  <div style={{ padding: '2rem', textAlign: 'center', color: '#555', fontSize: '0.8rem' }}>체크박스에서 표시할 항목을 선택하세요</div>
                ) : checkedItems.map((item, idx) => {
                  const isTopImg = item.id === 'topImg'
                  const isBottomImg = item.id === 'bottomImg'
                  const isMain = item.id === 'main'
                  const isSub = item.id === 'sub'
                  const isDetail = item.id === 'detail'
                  const hasImage = isTopImg ? !!t.top_image_s3_key : isBottomImg ? !!t.bottom_image_s3_key : false
                  const imgUrl = isTopImg && t.top_image_s3_key ? t.top_image_s3_key : isBottomImg && t.bottom_image_s3_key ? t.bottom_image_s3_key : ''

                  // 실제 상품 이미지
                  const productImages = previewProduct?.images || []
                  const mainIdx = t.main_image_index ?? 0
                  const mainImgUrl = productImages[mainIdx] || productImages[0] || ''
                  const subImgUrls = productImages.filter((_, i) => i !== mainIdx).slice(0, 3)

                  return (
                    <div key={item.id}
                      draggable
                      onDragStart={() => setDragIdx(idx)}
                      onDragOver={(e) => {
                        e.preventDefault()
                        if (dragIdx === null || dragIdx === idx) return
                        const checkedIds = checkedItems.map(i => i.id)
                        const fromId = checkedIds[dragIdx]
                        const toId = checkedIds[idx]
                        const newOrder = [...imgOrder]
                        const fromOrderIdx = newOrder.indexOf(fromId)
                        const toOrderIdx = newOrder.indexOf(toId)
                        newOrder.splice(fromOrderIdx, 1)
                        newOrder.splice(toOrderIdx, 0, fromId)
                        setImgOrder(newOrder)
                        setDragIdx(idx)
                      }}
                      onDrop={() => {
                        setDragIdx(null)
                        // 드래그 완료 시 순서 DB 저장
                        detailTemplateApi.update(t.id, { img_order: imgOrder }).then(() => {
                          setDetailTemplates(prev => prev.map(dt =>
                            dt.id === t.id ? { ...dt, img_order: imgOrder } : dt
                          ))
                        }).catch(e => console.error('img_order 저장 실패:', e))
                      }}
                      onDragEnd={() => setDragIdx(null)}
                      style={{
                        background: item.bg, border: `1px dashed ${item.border}`, borderRadius: '4px',
                        padding: '0.5rem',
                        textAlign: 'center', marginBottom: '0.375rem', cursor: 'grab',
                        opacity: dragIdx === idx ? 0.5 : 1, transition: 'opacity 0.15s',
                      }}>
                      {/* 상단/하단 이미지 */}
                      {(isTopImg || isBottomImg) && hasImage ? (
                        <img src={imgUrl} alt={item.label} style={{ width: '100%', borderRadius: '4px', maxHeight: '120px', objectFit: 'contain' }} />
                      ) : (isTopImg || isBottomImg) ? (
                        <span style={{ color: item.color, fontSize: '0.8rem', padding: '0.75rem 0', display: 'block' }}>{item.label} (URL 미등록)</span>
                      ) : /* 대표이미지 */ isMain && mainImgUrl ? (
                        <img src={mainImgUrl} alt="대표이미지" style={{ width: '100%', borderRadius: '4px', maxHeight: '120px', objectFit: 'contain' }} />
                      ) : /* 서브이미지 */ isSub && subImgUrls.length > 0 ? (
                        <div style={{ display: 'flex', gap: '0.375rem', justifyContent: 'center' }}>
                          {subImgUrls.map((url, i) => (
                            <img key={i} src={url} alt={`서브${i + 1}`} style={{ width: '60px', height: '60px', borderRadius: '4px', objectFit: 'cover' }} />
                          ))}
                        </div>
                      ) : /* 상세이미지 */ isDetail && previewProduct?.detail_images?.length ? (
                        <div>
                          <span style={{ color: item.color, fontSize: '0.75rem', display: 'block', marginBottom: '0.375rem' }}>상세이미지 ({fmtNum(previewProduct.detail_images.length)}장)</span>
                          <div style={{ display: 'flex', gap: '0.375rem', justifyContent: 'center', flexWrap: 'wrap' }}>
                            {previewProduct.detail_images.slice(0, 4).map((url, i) => (
                              <img key={i} src={url} alt={`상세${i + 1}`} style={{ width: '60px', height: '60px', borderRadius: '4px', objectFit: 'cover' }} />
                            ))}
                            {previewProduct.detail_images.length > 4 && (
                              <span style={{ width: '60px', height: '60px', borderRadius: '4px', background: 'rgba(177,151,252,0.15)', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: '0.7rem', color: '#B197FC' }}>+{previewProduct.detail_images.length - 4}</span>
                            )}
                          </div>
                        </div>
                      ) : (
                        <span style={{ color: item.color, fontSize: '0.8rem', padding: '0.75rem 0', display: 'block' }}>{item.label}</span>
                      )}
                    </div>
                  )
                })}
              </div>
            </div>
          )
        })()}

        {/* ── 마켓별 개별 상세페이지 ── */}
        <div style={{ borderTop: '1px solid #2D2D2D', paddingTop: '0.75rem', marginTop: '1rem' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', marginBottom: '0.5rem' }}>
            <span style={{ fontSize: '0.875rem', fontWeight: 700, color: '#51CF66' }}>마켓별 개별 설정</span>
            <span style={{ fontSize: '0.72rem', color: '#666' }}>특정 마켓에 다른 상세페이지 템플릿을 적용합니다</span>
          </div>
          {/* 설정된 마켓 목록 */}
          {Object.entries(marketDetailTemplates).map(([mkt, tplId]) => {
            const mLabel = MARKETS.find(m => m.id === mkt)?.label || mkt
            const tplName = detailTemplates.find(t => t.id === tplId)?.name || '(미선택)'
            return (
              <div key={mkt} style={{ marginBottom: '0.5rem', padding: '0.5rem', background: 'rgba(81,207,102,0.05)', border: '1px solid rgba(81,207,102,0.15)', borderRadius: '6px' }}>
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                  <span style={{ fontSize: '0.78rem', fontWeight: 600, color: '#51CF66' }}>{mLabel}</span>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                    <select
                      value={tplId || ''}
                      onChange={(e) => {
                        const next = { ...marketDetailTemplates }
                        if (e.target.value) {
                          next[mkt] = e.target.value
                        } else {
                          delete next[mkt]
                        }
                        setMarketDetailTemplates(Object.keys(next).length > 0 ? next : {})
                        triggerAutoSave()
                      }}
                      style={{ ...inputStyle, width: '180px', fontSize: '0.75rem' }}
                    >
                      <option value="">공통 템플릿 사용</option>
                      {detailTemplates.map(t => <option key={t.id} value={t.id}>{t.name}</option>)}
                    </select>
                    <button onClick={() => {
                      const next = { ...marketDetailTemplates }
                      delete next[mkt]
                      setMarketDetailTemplates(Object.keys(next).length > 0 ? next : {})
                      triggerAutoSave()
                    }} style={{ fontSize: '0.65rem', color: '#FF6B6B', background: 'none', border: '1px solid rgba(255,107,107,0.3)', borderRadius: '4px', padding: '1px 6px', cursor: 'pointer' }}>삭제</button>
                  </div>
                </div>
                {tplId && (
                  <span style={{ fontSize: '0.68rem', color: '#888', marginTop: '0.25rem', display: 'block' }}>
                    템플릿: {tplName}
                  </span>
                )}
              </div>
            )
          })}
          {/* 마켓 추가 드롭다운 */}
          <select
            value=""
            onChange={(e) => {
              if (!e.target.value) return
              setMarketDetailTemplates(prev => ({ ...prev, [e.target.value]: '' }))
              triggerAutoSave()
            }}
            style={{ ...inputStyle, width: 'auto', fontSize: '0.75rem' }}
          >
            <option value="">+ 마켓 추가</option>
            {MARKETS.filter(m => !marketDetailTemplates[m.id]).map(m => (
              <option key={m.id} value={m.id}>{m.label}</option>
            ))}
          </select>
        </div>

        {!selectedDetailTemplateId && Object.keys(marketDetailTemplates).length === 0 && (
          <p style={{ textAlign: 'center', color: '#555', padding: '2rem 0', fontSize: '0.875rem' }}>템플릿을 선택하거나 신규생성하세요</p>
        )}
      </div>

      {/* 상품/옵션명 관리 */}
      <div style={{ ...card, padding: '1.5rem' }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '1rem' }}>
          <span style={{ fontSize: '1rem', fontWeight: 700 }}>상품/옵션명</span>
          <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
            <span style={{ color: '#888', fontSize: '0.8125rem' }}>규칙선택</span>
            <select style={{ ...inputStyle, width: '160px' }} value={selectedNameRuleId} onChange={async (e) => {
              if (e.target.value === '__new__') {
                const r = await nameRuleApi.create({ name: `규칙 ${nameRules.length + 1}` })
                setNameRules(prev => [r, ...prev])
                setSelectedNameRuleId(r.id)
              } else {
                setSelectedNameRuleId(e.target.value)
              }
            }}>
              <option value="">선택하세요</option>
              <option value="__new__">+ 신규생성</option>
              {nameRules.map(r => <option key={r.id} value={r.id}>{r.name}</option>)}
            </select>
            <button onClick={async () => {
              if (!selectedNameRuleId) return
              const latest = nameRulesRef.current.find(x => x.id === selectedNameRuleId)
              if (!latest) return
              try {
                const saved = await nameRuleApi.update(latest.id, {
                  name: latest.name, prefix: latest.prefix, suffix: latest.suffix,
                  replacements: latest.replacements, replace_mode: latest.replace_mode,
                  option_rules: latest.option_rules, name_composition: latest.name_composition,
                  market_name_compositions: latest.market_name_compositions,
                  brand_display: latest.brand_display, dedup_enabled: latest.dedup_enabled,
                })
                if (saved) {
                  setNameRules(prev => prev.map(x => x.id === saved.id ? saved : x))
                  showAlert('규칙이 저장되었습니다.', 'success')
                }
              } catch { showAlert('저장 실패', 'error') }
            }} style={{ padding: '0.3rem 0.75rem', fontSize: '0.78rem', background: 'rgba(255,140,0,0.1)', border: '1px solid rgba(255,140,0,0.3)', borderRadius: '6px', color: '#FF8C00', cursor: 'pointer', whiteSpace: 'nowrap' }}>저장</button>
            <button onClick={async () => {
              if (!selectedNameRuleId) return
              const src = nameRulesRef.current.find(x => x.id === selectedNameRuleId)
              if (!src) return
              try {
                const created = await nameRuleApi.create({
                  name: `${src.name} (복사)`,
                  prefix: src.prefix,
                  suffix: src.suffix,
                  replacements: src.replacements,
                  replace_mode: src.replace_mode,
                  option_rules: src.option_rules,
                  name_composition: src.name_composition,
                  market_name_compositions: src.market_name_compositions,
                  brand_display: src.brand_display,
                  dedup_enabled: src.dedup_enabled,
                })
                setNameRules(prev => [created, ...prev])
                setSelectedNameRuleId(created.id)
                showAlert('규칙이 복사되었습니다.', 'success')
              } catch { showAlert('복사 실패', 'error') }
            }} style={{ padding: '0.3rem 0.75rem', fontSize: '0.78rem', background: 'rgba(76,154,255,0.1)', border: '1px solid rgba(76,154,255,0.3)', borderRadius: '6px', color: '#4C9AFF', cursor: 'pointer', whiteSpace: 'nowrap' }}>복사</button>
            <button onClick={async () => {
              if (!selectedNameRuleId) return
              await nameRuleApi.delete(selectedNameRuleId).catch(() => {})
              setNameRules(prev => prev.filter(x => x.id !== selectedNameRuleId))
              setSelectedNameRuleId('')
            }} style={{ padding: '0.3rem 0.75rem', fontSize: '0.78rem', background: 'rgba(255,107,107,0.1)', border: '1px solid rgba(255,107,107,0.3)', borderRadius: '6px', color: '#FF6B6B', cursor: 'pointer', whiteSpace: 'nowrap' }}>삭제</button>
          </div>
        </div>

        {selectedNameRuleId && (() => {
          const r = nameRules.find(x => x.id === selectedNameRuleId)
          if (!r) return null
          const updateRule = (patch: Partial<typeof r>) => setNameRules(prev => prev.map(x => x.id === r.id ? { ...x, ...patch } : x))
          const moveRep = (from: number, to: number) => {
            const reps = [...(r.replacements || [])]
            const [moved] = reps.splice(from, 1)
            reps.splice(to, 0, moved)
            updateRule({ replacements: reps })
          }
          const COMP_TAGS = ['{상품명}', '{브랜드명}', '{모델명}', '{사이트명}', '{상품번호}', '{검색키워드}']
          return (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '1rem' }}>
              {/* 규칙명 */}
              <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
                <span style={{ color: '#888', fontSize: '0.8125rem', minWidth: '55px' }}>규칙명</span>
                <input value={r.name}
                  onChange={(e) => updateRule({ name: e.target.value })}
                  style={{ ...inputStyle, flex: 1 }} />
              </div>
              {/* 접두어/접미어 */}
              <div style={{ display: 'flex', gap: '0.5rem' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '0.375rem', flex: 1 }}>
                  <span style={{ color: '#888', fontSize: '0.78rem', minWidth: '40px' }}>접두어</span>
                  <input value={r.prefix || ''} onChange={(e) => updateRule({ prefix: e.target.value })} style={{ ...inputStyle, flex: 1 }} placeholder="상품명 앞에 추가" />
                </div>
                <div style={{ display: 'flex', alignItems: 'center', gap: '0.375rem', flex: 1 }}>
                  <span style={{ color: '#888', fontSize: '0.78rem', minWidth: '40px' }}>접미어</span>
                  <input value={r.suffix || ''} onChange={(e) => updateRule({ suffix: e.target.value })} style={{ ...inputStyle, flex: 1 }} placeholder="상품명 뒤에 추가" />
                </div>
              </div>

              {/* ── 상품명 치환 정책 ── */}
              <div style={{ borderTop: '1px solid #2D2D2D', paddingTop: '0.75rem' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', marginBottom: '0.5rem' }}>
                  <span style={{ fontSize: '0.875rem', fontWeight: 700, color: '#FF8C00' }}>상품명 치환</span>
                  <span style={{ fontSize: '0.72rem', color: '#666' }}>수집된 상품명의 텍스트를 치환합니다</span>
                </div>
                <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', marginBottom: '0.5rem' }}>
                  <span style={{ color: '#888', fontSize: '0.78rem' }}>치환방식</span>
                  <select value={r.replace_mode || 'simultaneous'} onChange={(e) => updateRule({ replace_mode: e.target.value })} style={{ ...inputStyle, width: 'auto' }}>
                    <option value="simultaneous">동시치환</option>
                    <option value="sequential">순차치환</option>
                  </select>
                  <span style={{ fontSize: '0.68rem', color: '#555' }}>
                    {(r.replace_mode || 'simultaneous') === 'simultaneous' ? '정의한 문자열을 동시에 치환 (겹치면 긴 문자열 우선)' : '위에서 아래로 순서대로 치환'}
                  </span>
                </div>
                {/* 치환 조건 목록 */}
                <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', marginBottom: '0.375rem' }}>
                  <span style={{ color: '#888', fontSize: '0.78rem' }}>치환조건</span>
                  <button onClick={() => updateRule({ replacements: [...(r.replacements || []), { from: '', to: '', caseInsensitive: true }] })}
                    style={{ fontSize: '0.68rem', color: '#4C9AFF', background: 'transparent', border: '1px dashed #4C9AFF', borderRadius: '4px', padding: '1px 8px', cursor: 'pointer' }}>+ 조건추가</button>
                </div>
                {(r.replacements || []).map((rep: {from: string; to: string; caseInsensitive?: boolean}, idx: number) => (
                  <div key={idx} style={{ display: 'flex', alignItems: 'center', gap: '0.25rem', marginBottom: '0.25rem' }}>
                    <input value={rep.from} placeholder="변경전"
                      onChange={(e) => { const reps = [...(r.replacements || [])]; reps[idx] = { ...reps[idx], from: e.target.value }; updateRule({ replacements: reps }) }}
                      style={{ ...inputStyle, flex: 1, fontSize: '0.75rem' }} />
                    <span style={{ color: '#555', fontSize: '0.75rem' }}>→</span>
                    <input value={rep.to} placeholder="변경후"
                      onChange={(e) => { const reps = [...(r.replacements || [])]; reps[idx] = { ...reps[idx], to: e.target.value }; updateRule({ replacements: reps }) }}
                      style={{ ...inputStyle, flex: 1, fontSize: '0.75rem' }} />
                    {idx > 0 && <button onClick={() => moveRep(idx, idx - 1)} style={{ color: '#888', background: 'none', border: '1px solid #2D2D2D', borderRadius: '3px', cursor: 'pointer', fontSize: '0.7rem', padding: '1px 4px' }}>▲</button>}
                    {idx < (r.replacements || []).length - 1 && <button onClick={() => moveRep(idx, idx + 1)} style={{ color: '#888', background: 'none', border: '1px solid #2D2D2D', borderRadius: '3px', cursor: 'pointer', fontSize: '0.7rem', padding: '1px 4px' }}>▼</button>}
                    <label style={{ display: 'flex', alignItems: 'center', gap: '2px', fontSize: '0.65rem', color: '#888', whiteSpace: 'nowrap' }}>
                      <input type="checkbox" checked={rep.caseInsensitive ?? true}
                        onChange={(e) => { const reps = [...(r.replacements || [])]; reps[idx] = { ...reps[idx], caseInsensitive: e.target.checked }; updateRule({ replacements: reps }) }}
                        style={{ accentColor: '#FF8C00', width: '11px', height: '11px' }} />대소문자무시
                    </label>
                    <button onClick={() => updateRule({ replacements: (r.replacements || []).filter((_: unknown, i: number) => i !== idx) })}
                      style={{ color: '#FF6B6B', background: 'none', border: 'none', cursor: 'pointer', fontSize: '0.8rem' }}>×</button>
                  </div>
                ))}
              </div>

              {/* ── 옵션명 치환 ── */}
              <div style={{ borderTop: '1px solid #2D2D2D', paddingTop: '0.75rem' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', marginBottom: '0.375rem' }}>
                  <span style={{ fontSize: '0.875rem', fontWeight: 700, color: '#51CF66' }}>옵션명 치환</span>
                  <span style={{ fontSize: '0.72rem', color: '#666' }}>옵션명 텍스트를 치환합니다</span>
                  <button onClick={() => updateRule({ option_rules: [...(r.option_rules || []), { from: '', to: '' }] })}
                    style={{ fontSize: '0.68rem', color: '#4C9AFF', background: 'transparent', border: '1px dashed #4C9AFF', borderRadius: '4px', padding: '1px 8px', cursor: 'pointer' }}>+ 추가</button>
                </div>
                {(r.option_rules || []).map((opt: {from: string; to: string}, idx: number) => (
                  <div key={idx} style={{ display: 'flex', alignItems: 'center', gap: '0.25rem', marginBottom: '0.25rem' }}>
                    <input value={opt.from} placeholder="변경전"
                      onChange={(e) => { const opts = [...(r.option_rules || [])]; opts[idx] = { ...opts[idx], from: e.target.value }; updateRule({ option_rules: opts }) }}
                      style={{ ...inputStyle, flex: 1, fontSize: '0.75rem' }} />
                    <span style={{ color: '#555', fontSize: '0.75rem' }}>→</span>
                    <input value={opt.to} placeholder="변경후"
                      onChange={(e) => { const opts = [...(r.option_rules || [])]; opts[idx] = { ...opts[idx], to: e.target.value }; updateRule({ option_rules: opts }) }}
                      style={{ ...inputStyle, flex: 1, fontSize: '0.75rem' }} />
                    <button onClick={() => updateRule({ option_rules: (r.option_rules || []).filter((_: unknown, i: number) => i !== idx) })}
                      style={{ color: '#FF6B6B', background: 'none', border: 'none', cursor: 'pointer', fontSize: '0.8rem' }}>×</button>
                  </div>
                ))}
              </div>

              {/* ── 상품명 조합 정책 ── */}
              <div style={{ borderTop: '1px solid #2D2D2D', paddingTop: '0.75rem' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', marginBottom: '0.5rem' }}>
                  <span style={{ fontSize: '0.875rem', fontWeight: 700, color: '#4C9AFF' }}>상품명 조합</span>
                  <span style={{ fontSize: '0.72rem', color: '#666' }}>마켓에 전송될 상품명을 조합합니다</span>
                </div>
                {/* 태그 버튼 */}
                <div style={{ display: 'flex', gap: '0.375rem', flexWrap: 'wrap', marginBottom: '0.5rem' }}>
                  {COMP_TAGS.map(tag => (
                    <button key={tag} onClick={() => updateRule({ name_composition: [...(r.name_composition || []), tag] })}
                      style={{ fontSize: '0.72rem', padding: '3px 10px', background: 'rgba(76,154,255,0.1)', border: '1px solid rgba(76,154,255,0.3)', borderRadius: '4px', color: '#4C9AFF', cursor: 'pointer' }}>{tag}</button>
                  ))}
                </div>
                {/* 조합 미리보기 */}
                <div style={{ display: 'flex', alignItems: 'center', gap: '0.375rem', marginBottom: '0.375rem' }}>
                  <span style={{ color: '#888', fontSize: '0.78rem' }}>조합결과</span>
                  <div style={{ flex: 1, padding: '0.375rem 0.5rem', background: 'rgba(0,0,0,0.3)', border: '1px solid #2D2D2D', borderRadius: '4px', fontSize: '0.78rem', color: '#C5C5C5', minHeight: '28px' }}>
                    {(r.name_composition || []).length > 0 ? (r.name_composition || []).map((t: string, i: number) => (
                      <span key={i} style={{ color: COMP_TAGS.includes(t) ? '#4C9AFF' : '#E5E5E5' }}>{t} </span>
                    )) : <span style={{ color: '#555' }}>태그를 클릭하여 조합하세요 (미설정 시 원본 상품명 사용)</span>}
                  </div>
                  {(r.name_composition || []).length > 0 && (
                    <button onClick={() => updateRule({ name_composition: [] })}
                      style={{ fontSize: '0.68rem', color: '#FF6B6B', background: 'none', border: '1px solid rgba(255,107,107,0.3)', borderRadius: '4px', padding: '2px 8px', cursor: 'pointer' }}>초기화</button>
                  )}
                </div>
              </div>

              {/* ── 마켓별 상품명 조합 ── */}
              <div style={{ borderTop: '1px solid #2D2D2D', paddingTop: '0.75rem' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', marginBottom: '0.5rem' }}>
                  <span style={{ fontSize: '0.875rem', fontWeight: 700, color: '#51CF66' }}>마켓별 개별 설정</span>
                  <span style={{ fontSize: '0.72rem', color: '#666' }}>특정 마켓에 다른 상품명 조합을 적용합니다</span>
                </div>
                {/* 설정된 마켓 목록 */}
                {Object.entries(r.market_name_compositions || {}).map(([mkt, comp]) => {
                  const mLabel = MARKETS.find(m => m.id === mkt)?.label || mkt
                  return (
                    <div key={mkt} style={{ marginBottom: '0.5rem', padding: '0.5rem', background: 'rgba(81,207,102,0.05)', border: '1px solid rgba(81,207,102,0.15)', borderRadius: '6px' }}>
                      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '0.375rem' }}>
                        <span style={{ fontSize: '0.78rem', fontWeight: 600, color: '#51CF66' }}>{mLabel}</span>
                        <button onClick={() => {
                          const next = { ...(r.market_name_compositions || {}) }
                          delete next[mkt]
                          updateRule({ market_name_compositions: Object.keys(next).length > 0 ? next : undefined })
                        }} style={{ fontSize: '0.65rem', color: '#FF6B6B', background: 'none', border: '1px solid rgba(255,107,107,0.3)', borderRadius: '4px', padding: '1px 6px', cursor: 'pointer' }}>삭제</button>
                      </div>
                      <div style={{ display: 'flex', gap: '0.25rem', flexWrap: 'wrap', marginBottom: '0.375rem' }}>
                        {COMP_TAGS.map(tag => (
                          <button key={tag} onClick={() => {
                            const next = { ...(r.market_name_compositions || {}) }
                            next[mkt] = [...(next[mkt] || []), tag]
                            updateRule({ market_name_compositions: next })
                          }} style={{ fontSize: '0.68rem', padding: '2px 8px', background: 'rgba(81,207,102,0.1)', border: '1px solid rgba(81,207,102,0.3)', borderRadius: '4px', color: '#51CF66', cursor: 'pointer' }}>{tag}</button>
                        ))}
                      </div>
                      <div style={{ display: 'flex', alignItems: 'center', gap: '0.375rem' }}>
                        <div style={{ flex: 1, padding: '0.3rem 0.5rem', background: 'rgba(0,0,0,0.3)', border: '1px solid #2D2D2D', borderRadius: '4px', fontSize: '0.75rem', color: '#C5C5C5', minHeight: '26px' }}>
                          {(comp as string[]).length > 0 ? (comp as string[]).map((t: string, i: number) => (
                            <span key={i} style={{ color: COMP_TAGS.includes(t) ? '#51CF66' : '#E5E5E5' }}>{t} </span>
                          )) : <span style={{ color: '#555' }}>태그를 클릭하세요</span>}
                        </div>
                        {(comp as string[]).length > 0 && (
                          <button onClick={() => {
                            const next = { ...(r.market_name_compositions || {}) }
                            next[mkt] = []
                            updateRule({ market_name_compositions: next })
                          }} style={{ fontSize: '0.65rem', color: '#FF6B6B', background: 'none', border: '1px solid rgba(255,107,107,0.3)', borderRadius: '4px', padding: '1px 6px', cursor: 'pointer' }}>초기화</button>
                        )}
                      </div>
                    </div>
                  )
                })}
                {/* 마켓 추가 드롭다운 */}
                <select
                  value=""
                  onChange={(e) => {
                    if (!e.target.value) return
                    const next = { ...(r.market_name_compositions || {}), [e.target.value]: [] }
                    updateRule({ market_name_compositions: next })
                  }}
                  style={{ ...inputStyle, width: 'auto', fontSize: '0.75rem' }}
                >
                  <option value="">+ 마켓 추가</option>
                  {MARKETS.filter(m => !(r.market_name_compositions || {})[m.id]).map(m => (
                    <option key={m.id} value={m.id}>{m.label}</option>
                  ))}
                </select>
              </div>

              {/* ── 중복단어 필터링 ── */}
              <div style={{ borderTop: '1px solid #2D2D2D', paddingTop: '0.75rem' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', marginBottom: '0.375rem' }}>
                  <span style={{ fontSize: '0.875rem', fontWeight: 700, color: '#CC5DE8' }}>중복단어 필터링</span>
                  <span style={{ fontSize: '0.72rem', color: '#666' }}>상품명에서 중복 단어를 제거합니다</span>
                </div>
                <label style={{ display: 'flex', alignItems: 'center', gap: '0.375rem', cursor: 'pointer' }}>
                  <input type="checkbox" checked={r.dedup_enabled ?? true}
                    onChange={(e) => updateRule({ dedup_enabled: e.target.checked })}
                    style={{ accentColor: '#CC5DE8', width: '14px', height: '14px' }} />
                  <span style={{ fontSize: '0.8rem', color: '#C5C5C5' }}>중복단어를 필터링합니다</span>
                </label>
                <div style={{ marginTop: '0.375rem', fontSize: '0.68rem', color: '#555' }}>
                  * 띄어쓰기 기준 중복 단어 1개만 유지 / 치환 완료 후 적용 / 대소문자 구분
                </div>
              </div>
            </div>
          )
        })()}

        {!selectedNameRuleId && (
          <p style={{ textAlign: 'center', color: '#555', padding: '2rem 0', fontSize: '0.875rem' }}>규칙을 선택하거나 신규생성하세요</p>
        )}
      </div>

      </div>{/* 2컬럼 grid 닫기 */}

      {/* 금지어/삭제어는 설정 페이지로 이동 */}

      {/* 설정 저장/삭제 버튼 */}
      {showForm && (
        <div style={{ display: 'flex', gap: '0.75rem', justifyContent: 'center', marginTop: '1.5rem' }}>
          <button onClick={handleSubmit} style={{ padding: '0.625rem 2rem', background: 'linear-gradient(135deg, #FF8C00, #FFB84D)', color: '#fff', border: 'none', borderRadius: '8px', fontSize: '0.875rem', fontWeight: 600, cursor: 'pointer' }}>정책 저장</button>
          <button onClick={() => { if (editingId) { handleDelete(editingId); setEditingId(null); setName("새 정책"); setSiteName(""); setPricing({ ...defaultPricing }) } }} style={{ padding: '0.625rem 2rem', background: 'rgba(60,60,60,0.8)', border: '1px solid #3D3D3D', borderRadius: '8px', color: '#C5C5C5', fontSize: '0.875rem', cursor: 'pointer' }}>정책 삭제</button>
        </div>
      )}
      {/* AI 정책 변경 모달 */}
      {aiPolicyModalOpen && (
        <div
          style={{ position: 'fixed', inset: 0, zIndex: 99998, background: 'rgba(0,0,0,0.7)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}
          onClick={() => { if (!aiPolicyLoading) setAiPolicyModalOpen(false) }}
        >
          <div
            style={{ background: '#1E1E1E', border: '1px solid #3D3D3D', borderRadius: '12px', width: 'min(600px, 92vw)', maxHeight: '80vh', overflow: 'auto' }}
            onClick={e => e.stopPropagation()}
          >
            {/* 헤더 */}
            <div style={{ padding: '1.25rem 1.5rem', borderBottom: '1px solid #2D2D2D', display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
              <div>
                <h3 style={{ fontSize: '1rem', fontWeight: 700, color: '#E5E5E5', marginBottom: '0.25rem' }}>AI 정책 일괄 변경</h3>
                <p style={{ fontSize: '0.75rem', color: '#888' }}>자연어로 명령하면 관련 마켓의 모든 정책을 자동 수정합니다</p>
              </div>
              {!aiPolicyLoading && (
                <button onClick={() => setAiPolicyModalOpen(false)}
                  style={{ background: 'none', border: 'none', color: '#888', fontSize: '1.25rem', cursor: 'pointer' }}>✕</button>
              )}
            </div>

            {/* 본문 */}
            <div style={{ padding: '1.25rem 1.5rem' }}>
              {/* 명령 입력 */}
              {aiPolicyChanges.length === 0 && !aiPolicyLoading && (
                <div>
                  <div style={{ marginBottom: '0.75rem' }}>
                    <input
                      autoFocus
                      value={aiPolicyCommand}
                      onChange={e => setAiPolicyCommand(e.target.value)}
                      onKeyDown={async e => {
                        if (e.key === 'Enter' && aiPolicyCommand.trim()) {
                          setAiPolicyLoading(true)
                          try {
                            const result = await policyApi.aiChange(aiPolicyCommand.trim())
                            setAiPolicyChanges(result.changes)
                            setAiPolicyApplied(result.applied)
                            setLastAiUsage({ calls: 1, tokens: 1800, cost: 15, date: fmtTime() })
                            setPolicies(await policyApi.list().catch(() => []))
                          } catch (err) {
                            const msg = err instanceof Error ? err.message : '실패'
                            showAlert(`AI 정책 변경 실패: ${msg}`, 'error')
                          } finally {
                            setAiPolicyLoading(false)
                          }
                        }
                      }}
                      placeholder="예: 지마켓 마진율 1% 올려, 쿠팡 배송비 500원 내려"
                      style={{
                        width: '100%', padding: '0.75rem 1rem',
                        background: '#1A1A1A', border: '1px solid #3D3D3D', borderRadius: '8px',
                        color: '#E5E5E5', fontSize: '0.875rem', outline: 'none',
                      }}
                    />
                  </div>
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.375rem', marginBottom: '0.5rem' }}>
                    {['지마켓 마진율 1% 올려', '쿠팡 배송비 500원 내려', '옥션 수수료 2% 낮춰', '전체 마진율 2% 올려'].map(ex => (
                      <button key={ex} onClick={() => setAiPolicyCommand(ex)}
                        style={{ padding: '0.25rem 0.5rem', background: 'rgba(255,255,255,0.04)', border: '1px solid #2D2D2D', borderRadius: '4px', color: '#666', fontSize: '0.6875rem', cursor: 'pointer' }}
                        onMouseEnter={e => { e.currentTarget.style.color = '#A78BFA'; e.currentTarget.style.borderColor = 'rgba(167,139,250,0.3)' }}
                        onMouseLeave={e => { e.currentTarget.style.color = '#666'; e.currentTarget.style.borderColor = '#2D2D2D' }}
                      >{ex}</button>
                    ))}
                  </div>
                  <button
                    onClick={async () => {
                      if (!aiPolicyCommand.trim()) return
                      setAiPolicyLoading(true)
                      try {
                        const result = await policyApi.aiChange(aiPolicyCommand.trim())
                        setAiPolicyChanges(result.changes)
                        setAiPolicyApplied(result.applied)
                        setLastAiUsage({ calls: 1, tokens: 1800, cost: 15, date: fmtTime() })
                        setPolicies(await policyApi.list().catch(() => []))
                      } catch (err) {
                        const msg = err instanceof Error ? err.message : '실패'
                        showAlert(`AI 정책 변경 실패: ${msg}`, 'error')
                      } finally {
                        setAiPolicyLoading(false)
                      }
                    }}
                    disabled={!aiPolicyCommand.trim()}
                    style={{
                      width: '100%', padding: '0.625rem', borderRadius: '8px', border: 'none',
                      background: aiPolicyCommand.trim() ? '#A78BFA' : '#2D2D2D',
                      color: aiPolicyCommand.trim() ? '#FFF' : '#555',
                      fontSize: '0.875rem', fontWeight: 600, cursor: aiPolicyCommand.trim() ? 'pointer' : 'not-allowed',
                    }}
                  >실행</button>
                </div>
              )}

              {/* 로딩 */}
              {aiPolicyLoading && (
                <div style={{ textAlign: 'center', padding: '2rem 0', color: '#888' }}>
                  <div style={{ fontSize: '1.5rem', marginBottom: '0.75rem' }}>🤖</div>
                  <p style={{ fontSize: '0.875rem' }}>Claude가 정책을 분석하고 변경 중...</p>
                  <p style={{ fontSize: '0.75rem', color: '#555', marginTop: '0.25rem' }}>"{aiPolicyCommand}"</p>
                </div>
              )}

              {/* 결과 */}
              {!aiPolicyLoading && aiPolicyChanges.length > 0 && (
                <div>
                  <div style={{ padding: '0.75rem 1rem', background: 'rgba(34,197,94,0.08)', border: '1px solid rgba(34,197,94,0.25)', borderRadius: '8px', marginBottom: '1rem', textAlign: 'center' }}>
                    <span style={{ fontSize: '1.25rem', fontWeight: 700, color: '#22C55E' }}>{aiPolicyApplied}</span>
                    <span style={{ fontSize: '0.8125rem', color: '#888', marginLeft: '0.375rem' }}>건 정책 변경 완료</span>
                  </div>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
                    {aiPolicyChanges.map((ch, i) => {
                      const fieldLabels: Record<string, string> = { marginRate: '마진율', shippingCost: '배송비', feeRate: '수수료', extraCharge: '추가요금', minMarginAmount: '최소마진' }
                      const isRate = ch.field.includes('Rate') || ch.field.includes('rate')
                      const prefix = isRate ? '' : '₩'
                      const suffix = isRate ? '%' : ''
                      const diff = ch.after - ch.before
                      return (
                        <div key={i} style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '0.5rem 0.75rem', background: 'rgba(255,255,255,0.02)', borderRadius: '6px', border: '1px solid #2D2D2D' }}>
                          <div>
                            <span style={{ fontSize: '0.8125rem', color: '#E5E5E5' }}>{ch.policy_name}</span>
                            <span style={{ fontSize: '0.6875rem', color: '#888', marginLeft: '0.5rem' }}>
                              {ch.market === 'common' ? '공통' : ch.market}
                            </span>
                          </div>
                          <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                            <span style={{ fontSize: '0.75rem', color: '#888' }}>{fieldLabels[ch.field] || ch.field}</span>
                            <span style={{ fontSize: '0.8125rem', color: '#666' }}>{prefix}{fmtNum(ch.before)}{suffix}</span>
                            <span style={{ color: '#555' }}>→</span>
                            <span style={{ fontSize: '0.875rem', fontWeight: 700, color: '#FF8C00' }}>{prefix}{fmtNum(ch.after)}{suffix}</span>
                            <span style={{ fontSize: '0.6875rem', color: diff > 0 ? '#22C55E' : '#EF4444' }}>
                              ({diff > 0 ? '+' : ''}{prefix}{fmtNum(diff)}{suffix})
                            </span>
                          </div>
                        </div>
                      )
                    })}
                  </div>
                </div>
              )}

              {!aiPolicyLoading && aiPolicyChanges.length === 0 && aiPolicyApplied === 0 && aiPolicyCommand && (
                <p style={{ color: '#888', textAlign: 'center', padding: '1rem' }}>변경할 정책이 없습니다</p>
              )}
            </div>

            {/* 하단 */}
            {!aiPolicyLoading && aiPolicyChanges.length > 0 && (
              <div style={{ padding: '1rem 1.5rem', borderTop: '1px solid #2D2D2D', display: 'flex', justifyContent: 'flex-end' }}>
                <button
                  onClick={() => {
                    setAiPolicyModalOpen(false)
                    // 현재 편집 중인 정책 갱신
                    if (editingId) {
                      const updated = policies.find(p => p.id === editingId)
                      if (updated) openEdit(updated)
                    }
                  }}
                  style={{ padding: '0.5rem 1.25rem', fontSize: '0.8125rem', borderRadius: '6px', border: 'none', background: '#FF8C00', color: '#FFF', cursor: 'pointer', fontWeight: 600 }}
                >확인</button>
              </div>
            )}
          </div>
        </div>
      )}
      </div>

      {/* 테트리스 매칭 탭 */}
      {mainTab === '테트리스 매칭' && (
        <div>
          <div style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            gap: '1rem',
            padding: '0.875rem 1rem',
            marginBottom: '0.875rem',
            background: tetrisMatchingEnabled ? 'rgba(34,197,94,0.08)' : 'rgba(255,140,0,0.08)',
            border: tetrisMatchingEnabled ? '1px solid rgba(34,197,94,0.25)' : '1px solid rgba(255,140,0,0.25)',
            borderRadius: '10px',
          }}>
            <div>
              <div style={{ fontSize: '0.9375rem', fontWeight: 700, color: '#E5E5E5', marginBottom: '0.25rem' }}>
                테트리스 매칭 실제 상품등록 반영
              </div>
              <div style={{ fontSize: '0.75rem', color: '#888' }}>
                OFF면 배치 현황 확인용으로만 사용하고, ON이면 상품등록 시 테트리스 매칭을 실제로 적용합니다.
              </div>
            </div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexShrink: 0 }}>
              <input
                type="number"
                value={syncIntervalInput}
                onChange={e => setSyncIntervalInput(Math.max(1, Number(e.target.value)))}
                min={1}
                max={168}
                style={{
                  width: 48,
                  background: '#2A2A2A',
                  border: '1px solid #444',
                  color: '#ccc',
                  borderRadius: 6,
                  padding: '4px 6px',
                  fontSize: '0.8125rem',
                  textAlign: 'center',
                }}
              />
              <span style={{ color: '#888', fontSize: '0.8125rem' }}>시간</span>
              <button
                onClick={handleToggleTetrisMatching}
                disabled={tetrisMatchingSaving}
                style={{
                  minWidth: '92px',
                  padding: '0.5rem 0.875rem',
                  borderRadius: '999px',
                  border: tetrisMatchingEnabled ? '1px solid rgba(34,197,94,0.35)' : '1px solid rgba(255,140,0,0.35)',
                  background: tetrisMatchingEnabled ? '#22C55E' : '#2A2A2A',
                  color: tetrisMatchingEnabled ? '#06130A' : '#FFB84D',
                  fontSize: '0.8125rem',
                  fontWeight: 700,
                  cursor: tetrisMatchingSaving ? 'not-allowed' : 'pointer',
                  opacity: tetrisMatchingSaving ? 0.7 : 1,
                }}
              >
                {tetrisMatchingSaving ? '저장 중...' : tetrisMatchingEnabled ? 'ON' : 'OFF'}
              </button>
            </div>
          </div>
          <TetrisBoard />
        </div>
      )}
    </div>
  )
}
