'use client'

import React, { useState, useMemo, useCallback } from 'react'
import {
  collectorApi,
  type SambaCollectedProduct,
  type SambaPolicy,
  type SambaMarketAccount,
  type SambaSearchFilter,
} from '@/lib/samba/api/commerce'
import { API_BASE } from '@/lib/samba/api/shared'
import { type SambaNameRule, type SambaDetailTemplate } from '@/lib/samba/api/support'
import { showAlert } from '@/components/samba/Modal'
import { fmtNum } from '@/lib/samba/styles'
import { fmtDate } from '@/lib/samba/utils'
import ProductImage from './ProductImage'
import OptionPanel from './OptionPanel'

// 마켓별 상품 검색 URL (구매페이지 바로가기용)
export const MARKETS = [
  // 국내 오픈마켓
  { id: 'smartstore', name: '스마트스토어', url: 'https://sell.smartstore.naver.com', searchUrl: 'https://search.shopping.naver.com/search/all?query=' },
  { id: 'gmarket', name: '지마켓', url: 'https://www.esmplus.com', searchUrl: 'https://browse.gmarket.co.kr/search?keyword=' },
  { id: 'auction', name: '옥션', url: 'https://www.esmplus.com', searchUrl: 'https://browse.auction.co.kr/search?keyword=' },
  { id: 'coupang', name: '쿠팡', url: 'https://wing.coupang.com', searchUrl: 'https://www.coupang.com/np/search?q=' },
  { id: 'lotteon', name: '롯데ON', url: 'https://partner.lotteon.com', searchUrl: 'https://www.lotteon.com/csearch/search/search?render=search&platform=pc&mallId=2&q=' },
  { id: '11st', name: '11번가', url: 'https://spc.11st.co.kr', searchUrl: 'https://search.11st.co.kr/Search.tmall?kwd=' },
  { id: 'toss', name: '토스', url: 'https://seller.toss.im', searchUrl: 'https://shopping.toss.im/search?keyword=' },
  { id: 'ssg', name: '신세계몰', url: 'https://sellerpick.ssg.com', searchUrl: 'https://www.ssg.com/search.ssg?query=' },
  // 국내 홈쇼핑/종합몰
  { id: 'gsshop', name: 'GS샵', url: 'https://partner.gsshop.com', searchUrl: 'https://www.gsshop.com/shop/search/totalSearch.gs?tq=' },
  { id: 'lottehome', name: '롯데홈쇼핑', url: 'https://partner.lottehome.com', searchUrl: 'https://www.lotteimall.com/search/searchMain.lotte?searchKeyword=' },
  { id: 'homeand', name: '홈앤쇼핑', url: 'https://partner.homeandshopping.com', searchUrl: 'https://www.hnsmall.com/search?keyword=' },
  { id: 'hmall', name: 'HMALL', url: 'https://partner.hmall.com', searchUrl: 'https://www.hmall.com/search?searchTerm=' },
  // 리셀/패션
  { id: 'kream', name: 'KREAM', url: 'https://kream.co.kr', searchUrl: 'https://kream.co.kr/search?keyword=' },
  { id: 'poison', name: '포이즌', url: 'https://www.poizon.com', searchUrl: 'https://www.poizon.com/search?keyword=' },
  // 해외 마켓
  { id: 'qoo10', name: 'Qoo10', url: 'https://qsm.qoo10.com', searchUrl: 'https://www.qoo10.com/s?keyword=' },
  { id: 'rakuten', name: '라쿠텐', url: 'https://merchant.rakuten.co.jp', searchUrl: 'https://search.rakuten.co.jp/search/mall/' },
  { id: 'buyma', name: '바이마', url: 'https://www.buyma.com/buyer/', searchUrl: 'https://www.buyma.com/r/-C/' },
  { id: 'lazada', name: 'Lazada', url: 'https://sellercenter.lazada.com', searchUrl: 'https://www.lazada.com/catalog/?q=' },
  { id: 'shopify', name: 'Shopify', url: 'https://admin.shopify.com', searchUrl: '' },
  { id: 'shopee', name: 'Shopee', url: 'https://seller.shopee.com', searchUrl: 'https://shopee.com/search?keyword=' },
  { id: 'zoom', name: 'Zum(줌)', url: 'https://zum.com', searchUrl: 'https://search.zum.com/search.zum?method=uni&query=' },
  { id: 'ebay', name: 'eBay', url: 'https://www.ebay.com/sh/ovw', searchUrl: 'https://www.ebay.com/sch/i.html?_nkw=' },
  { id: 'amazon', name: '아마존', url: 'https://sellercentral.amazon.com', searchUrl: 'https://www.amazon.com/s?k=' },
  // 종합솔루션
  { id: 'playauto', name: '플레이오토', url: '', searchUrl: '' },
]

// 마켓별 상품명 글자수 제한
const MARKET_NAME_LIMITS: Record<string, number> = {
  '스마트스토어': 49,
  '쿠팡': 100,
  '지마켓': 100,
  '옥션': 100,
}

// byte 기준 제한 마켓 (한글 3byte 기준)
const MARKET_NAME_BYTE_LIMITS: Record<string, number> = {
  '롯데ON': 149,
  '11번가': 99,
}

function truncateToBytes(text: string, maxBytes: number): string {
  const encoded = new TextEncoder().encode(text)
  if (encoded.length <= maxBytes) return text
  return new TextDecoder('utf-8', { fatal: false }).decode(encoded.slice(0, maxBytes))
}

function getByteLength(text: string): number {
  return new TextEncoder().encode(text).length
}

// 숫자 포맷 유틸
function fmt(n: number): string {
  return fmtNum(n)
}

// 마켓별 상품 구매페이지 URL 생성 (상품번호가 있을 때만)
function buildMarketProductUrl(
  marketType: string,
  sellerId: string,
  productNo: string,
  storeSlug?: string,
  extras?: { pid?: string; vid?: string }
): string {
  if (!productNo) return ''
  switch (marketType) {
    case 'smartstore':
      // 스토어 슬러그 우선 사용 (seller_id는 이메일일 수 있음)
      return `https://smartstore.naver.com/${storeSlug || sellerId}/products/${productNo}`
    case 'coupang': {
      // 쿠팡 vp/products URL 은 {productId}?vendorItemId={vendorItemId} 형식.
      const pid = extras?.pid
      const vid = extras?.vid
      if (pid) {
        return vid
          ? `https://www.coupang.com/vp/products/${pid}?vendorItemId=${vid}`
          : `https://www.coupang.com/vp/products/${pid}`
      }
      return `https://www.coupang.com/vp/products/${productNo}`
    }
    case '11st':
      return `https://www.11st.co.kr/products/${productNo}`
    case 'gmarket':
      return `https://item.gmarket.co.kr/Item?goodscode=${productNo}`
    case 'auction':
      return `https://itempage3.auction.co.kr/DetailView.aspx?ItemNo=${productNo}`
    case 'ssg':
      return `https://www.ssg.com/item/itemView.ssg?itemId=${productNo}`
    case 'lotteon':
      return `https://www.lotteon.com/p/product/${productNo}`
    case 'gsshop':
      return `https://www.gsshop.com/prd/prd.gs?prdid=${productNo}`
    case 'lottehome':
      return `https://www.lotteimall.com/goods/viewGoodsDetail.lotte?goods_no=${productNo}`
    case 'kream':
      return `https://kream.co.kr/products/${productNo}`
    case 'ebay':
      return `https://www.ebay.com/itm/${productNo}`
    case 'cafe24':
      return `https://${sellerId}.cafe24.com/product/detail.html?product_no=${productNo}`
    default:
      return ''
  }
}

// 가격범위별 마진 매칭 (백엔드 policy/service.py:_calculate_range_margin과 동일 로직)
// cost >= min && cost < max 인 첫 번째 범위의 rate를 반환, 매칭 실패 시 fallback
export function pickRangeMargin(
  cost: number,
  ranges: Array<{ min?: number; max?: number; rate?: number }>,
  fallback: number,
): number {
  for (const r of ranges) {
    const min = r.min ?? 0
    const max = r.max || 9999999999
    if (cost >= min && cost < max) return r.rate ?? fallback
  }
  return fallback
}

// 가격 계산 공통 함수 (ProductCard 내 2곳 중복 제거)
export function calcPrice(
  cost: number, mRate: number, ship: number, fee: number, extra: number, minMargin: number,
  ssMRate = 0, ssMAmount = 0,
): { price: number; marginAmt: number; usedMin: boolean; feeAmt: number; calcStr: string } {
  let marginAmt = Math.round(cost * mRate / 100)
  const usedMin = minMargin > 0 && marginAmt < minMargin
  if (usedMin) marginAmt = minMargin
  let price = cost + marginAmt + ship
  // 소싱처별 추가 마진 (수수료 역산 전 적용 — 백엔드 calc_market_price와 동일)
  if (ssMRate !== 0) price += Math.round(cost * ssMRate / 100)
  if (ssMAmount !== 0) price += ssMAmount
  if (fee > 0 && price > 0) price = Math.ceil(price / (1 - fee / 100))
  if (extra > 0) price += extra
  // 100원 단위 절사 (백엔드 calc_market_price와 동일)
  price = Math.floor(price / 100) * 100
  const feeAmt = fee > 0 && price > 0 ? Math.round(price * fee / 100) : 0
  const ssAmt = Math.round(cost * ssMRate / 100) + ssMAmount
  const ssRateLabel = ssMRate !== 0 && ssMAmount !== 0
    ? ` (${ssMRate}% + ${fmt(ssMAmount)})`
    : ssMRate !== 0
      ? ` (${ssMRate}%)`
      : ''
  const ssExtra = ssMRate !== 0 || ssMAmount !== 0
    ? ` + 소싱추가마진 ${fmt(ssAmt)}${ssRateLabel}`
    : ''
  const parts = [
    `원가 ${fmt(cost)}`,
    usedMin ? `마진 ${fmt(marginAmt)}(최소마진)` : `마진 ${fmt(marginAmt)}(${mRate}%)`,
    `배송비 ${fmt(ship)}`,
    `추가요금 ${fmt(extra)}`,
    `수수료 ${fmt(feeAmt)}(${fee}%)`,
  ]
  return { price, marginAmt, usedMin, feeAmt, calcStr: `₩${fmt(price)} = ${parts.join(' + ')}${ssExtra}` }
}

// 소싱처별 원문링크 URL 템플릿 (통합 관리)
const SOURCE_URL_MAP: Record<string, string> = {
  MUSINSA: 'https://www.musinsa.com/products/{id}',
  KREAM: 'https://kream.co.kr/products/{id}',
  FASHIONPLUS: 'https://www.fashionplus.co.kr/goods/detail/{id}',
  ABCMART: 'https://www.a-rt.com/product?prdtNo={id}',
  GRANDSTAGE: 'https://www.a-rt.com/product?prdtNo={id}&tChnnlNo=10002',
  REXMONDE: 'https://www.okmall.com/products/detail/{id}',
  LOTTEON: 'https://www.lotteon.com/p/product/{id}',
  GSSHOP: 'https://www.gsshop.com/prd/prd.gs?prdid={id}',
  ELANDMALL: 'https://www.elandmall.com/goods/goods.action?goodsNo={id}',
  SSF: 'https://www.ssfshop.com/goods/{id}',
  SSG: 'https://www.ssg.com/item/itemView.ssg?itemId={id}',
  NIKE: 'https://www.nike.com/kr/t/{id}',
  ADIDAS: 'https://www.adidas.co.kr/{id}.html',
}

function getSourceUrl(p: { source_url?: string; source_site: string; site_product_id?: string; video_url?: string | null }): string {
  if (p.source_url) return p.source_url
  if (!p.site_product_id) return ''
  const site = (p.source_site || '').toUpperCase()
  if (site === 'NIKE' && p.video_url) return p.video_url
  const tpl = SOURCE_URL_MAP[site]
  return tpl ? tpl.replace('{id}', p.site_product_id) : ''
}

// 모듈 레벨 캐시: 삭제어 정규식
let _deletionRegexCache: { words: string[]; regex: RegExp } | null = null
function getDeletionRegex(deletionWords: string[]): RegExp | null {
  if (!deletionWords.length) return null
  // 값 기반 비교로 캐시 히트 판단 (배열 참조가 달라도 내용이 같으면 재사용)
  if (_deletionRegexCache && _deletionRegexCache.words.join(',') === deletionWords.join(',')) return _deletionRegexCache.regex
  const escaped = deletionWords.map(w => w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))
  const regex = new RegExp(`(${escaped.join('|')})`, 'gi')
  _deletionRegexCache = { words: deletionWords, regex }
  return regex
}

function getSourceSiteMargin(
  sourceSiteMargins: Record<string, { marginRate?: number; marginAmount?: number }>,
  sourceSite: string,
): { marginRate?: number; marginAmount?: number } {
  if (sourceSiteMargins[sourceSite]) return sourceSiteMargins[sourceSite]
  if (sourceSite === 'GSSHOP' && sourceSiteMargins.GSShop) return sourceSiteMargins.GSShop
  if (sourceSite === 'GSShop' && sourceSiteMargins.GSSHOP) return sourceSiteMargins.GSSHOP
  return {}
}

// 모듈 레벨 캐시: 치환어 정규식
const _replacementRegexCache = new Map<string, RegExp>()
function getReplacementRegex(from: string, caseInsensitive: boolean): RegExp {
  const key = `${from}:${caseInsensitive}`
  let cached = _replacementRegexCache.get(key)
  if (!cached) {
    const flags = caseInsensitive ? 'gi' : 'g'
    cached = new RegExp(from.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), flags)
    _replacementRegexCache.set(key, cached)
  }
  return cached
}

// 동시치환: 모든 치환규칙의 매칭을 한번에 수집 → 긴 문자열 우선 → 비겹침 선택
function simultaneousReplace(
  text: string,
  replacements: Array<{ from: string; to: string; caseInsensitive?: boolean }>,
): string {
  type Match = { start: number; end: number; to: string; fromLen: number; priority: number }
  const allMatches: Match[] = []

  for (let i = 0; i < replacements.length; i++) {
    const r = replacements[i]
    if (!r.from) continue
    const escaped = r.from.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
    const flags = (r.caseInsensitive ?? true) ? 'gi' : 'g'
    const regex = new RegExp(escaped, flags)
    let m: RegExpExecArray | null
    while ((m = regex.exec(text)) !== null) {
      allMatches.push({
        start: m.index,
        end: m.index + m[0].length,
        to: r.to || '',
        fromLen: m[0].length,
        priority: i,
      })
    }
  }

  if (allMatches.length === 0) return text

  // 위치(ASC) → 길이(DESC, 긴 것 우선) → 규칙순서(ASC)
  allMatches.sort((a, b) =>
    a.start - b.start || b.fromLen - a.fromLen || a.priority - b.priority
  )

  // 겹치지 않는 매칭만 선택 (greedy left-to-right)
  const selected: Match[] = []
  let lastEnd = 0
  for (const match of allMatches) {
    if (match.start >= lastEnd) {
      selected.push(match)
      lastEnd = match.end
    }
  }

  // 결과 문자열 조립
  let result = ''
  let pos = 0
  for (const match of selected) {
    result += text.slice(pos, match.start) + match.to
    pos = match.end
  }
  result += text.slice(pos)
  return result
}

// 상품명 조합 적용 (name_composition 태그 기반)
export function composeProductName(
  product: SambaCollectedProduct,
  nameRule: SambaNameRule | undefined,
  deletionWords?: string[],
): string {
  if (!nameRule?.name_composition?.length) return product.name
  const seoKws = product.seo_keywords || []
  const tagMap: Record<string, string> = {
    '{상품명}': product.name || '',
    '{브랜드명}': product.brand || '',
    '{모델명}': product.style_code || '',
    '{사이트명}': product.source_site || '',
    '{상품번호}': product.site_product_id || '',
    '{검색키워드}': seoKws.slice(0, 3).join(' '),
  }
  // 조합 태그 순서대로 값 치환
  let composed = nameRule.name_composition
    .map(tag => tagMap[tag] ?? tag)
    .filter(v => v.trim() !== '')
    .join(' ')
  // 치환어 적용 (동시치환/순차치환 분기)
  if (nameRule.replacements?.length) {
    if (nameRule.replace_mode === 'sequential') {
      // 순차치환: 위에서 아래로 순서대로 치환
      for (const r of nameRule.replacements) {
        if (!r.from) continue
        const regex = getReplacementRegex(r.from, r.caseInsensitive ?? true)
        composed = composed.replace(regex, r.to || '')
      }
    } else {
      // 동시치환(기본): 모든 규칙을 한번에 적용, 긴 문자열 우선
      composed = simultaneousReplace(composed, nameRule.replacements)
    }
  }
  // 삭제어 적용 (dedup 전에 적용하여 중복 단어 감지 가능하게)
  if (deletionWords?.length) {
    const delRegex = getDeletionRegex(deletionWords)
    if (delRegex) {
      composed = composed.replace(delRegex, ' ').replace(/\s{2,}/g, ' ').trim()
    }
  }
  // 중복 제거 — 구두점 안에 묶인 부분단어까지 감지
  if (nameRule.dedup_enabled) {
    const seen = new Set<string>()
    // 2자 이상 한글/영문 + 하이픈 연결 숫자(품번) + 3자 이상 순수 숫자
    composed = composed.replace(/\p{L}{2,}|\d+(?:-\d+)+|\d{3,}/gu, (match) => {
      const lower = match.toLowerCase()
      if (seen.has(lower)) return ''
      seen.add(lower)
      return match
    })
    composed = composed.replace(/\s+/g, ' ').trim()
  }
  // prefix/suffix 적용
  if (nameRule.prefix) composed = `${nameRule.prefix} ${composed}`
  if (nameRule.suffix) composed = `${composed} ${nameRule.suffix}`
  return composed.trim()
}

// 삭제어 취소선이 적용된 등록 상품명 렌더링 (캐싱된 정규식 사용)
function renderRegisteredName(name: string, deletionWords: string[]): React.ReactNode {
  const regex = getDeletionRegex(deletionWords)
  if (!regex) return name
  const parts = name.split(regex)
  if (parts.length === 1) return name
  return parts.map((part, i) => {
    const isMatch = deletionWords.some(w => w.toLowerCase() === part.toLowerCase())
    if (isMatch) {
      return <span key={i} style={{ textDecoration: 'line-through', textDecorationColor: '#FF6B6B', color: '#666' }}>{part}</span>
    }
    return <span key={i}>{part}</span>
  })
}

interface ProductCardProps {
  product: SambaCollectedProduct
  idx: number
  policies: SambaPolicy[]
  accounts: SambaMarketAccount[]
  nameRules: SambaNameRule[]
  selectedIds: Set<string>
  filterNameMap: Record<string, string>
  deletionWords: string[]
  onCheckboxToggle: (id: string, checked: boolean) => void
  onDelete: (id: string) => void
  onPolicyChange: (productId: string, policyId: string) => void
  onToggleMarket: (productId: string, marketId: string) => void
  onEnrich: (productId: string) => void
  onLockToggle: (productId: string, field: 'lock_delete' | 'lock_stock', value: boolean) => void
  onBlockCollect: (productId: string) => Promise<void>
  onTagUpdate: (productId: string, tags: string[]) => void
  onMarketDelete: (productId: string) => void
  onProductUpdate: (productId: string, data: Partial<SambaCollectedProduct>) => void
  logMessage?: string
  catMappingMap: Map<string, Record<string, string>>
  filters?: SambaSearchFilter[]
  detailTemplates: SambaDetailTemplate[]
  compact?: boolean
  expanded?: boolean
  onToggleExpand?: () => void
}

const ProductCard = React.memo(function ProductCard({
  product: p, idx, policies, accounts, nameRules, selectedIds, filterNameMap, deletionWords,
  onCheckboxToggle, onDelete, onPolicyChange, onToggleMarket, onEnrich, onLockToggle, onBlockCollect, onTagUpdate, onMarketDelete, onProductUpdate, logMessage,
  catMappingMap, filters, detailTemplates, compact, expanded, onToggleExpand,
}: ProductCardProps) {
  const accMap = useMemo(() => new Map(accounts.map(a => [a.id, a])), [accounts])
  const [showPriceHistoryModal, setShowPriceHistoryModal] = useState(false)
  const [priceHistoryData, setPriceHistoryData] = useState<Record<string, unknown>[] | null>(null)
  const openPriceHistory = useCallback(() => {
    setShowPriceHistoryModal(true)
    setPriceHistoryData(null)
    collectorApi.getPriceHistory(p.id).then(data => {
      // API 응답이 배열이 아닌 경우 방어 (DB 데이터 손상 대비)
      setPriceHistoryData(Array.isArray(data) ? data : [])
    }).catch(() => setPriceHistoryData([]))
  }, [p.id])
  const [showImageModal, setShowImageModal] = useState(false)
  const [zoomImg, setZoomImg] = useState<string | null>(null)
  const [zoomIdx, setZoomIdx] = useState(0)
  const [zoomList, setZoomList] = useState<string[]>([])
  const openZoom = (url: string, images?: string[]) => {
    const list = images || productImages || p.images || []
    setZoomList(list)
    const idx = list.indexOf(url)
    setZoomIdx(idx >= 0 ? idx : 0)
    setZoomImg(url)
  }
  // 알림/확인 모달 (alert/confirm 대체)
  const [cardAlert, setCardAlert] = useState<{ msg: string; type?: 'success' | 'error' } | null>(null)
  const [cardConfirm, setCardConfirm] = useState<{ msg: string; onOk: () => void } | null>(null)
  const [collectBlocking, setCollectBlocking] = useState(false)
  const [imageTab, setImageTab] = useState<'main' | 'extra' | 'detail' | 'video'>('main')
  const [productImages, setProductImages] = useState<string[]>(p.images || [])
  const [detailImgList, setDetailImgList] = useState<string[]>(
    (p.detail_images && p.detail_images.length > 0)
      ? [...p.detail_images]
      : (p.detail_html || '').match(/src=["']([^"']+)["']/gi)
          ?.map((m: string) => m.replace(/src=["']/i, '').replace(/["']$/, '')) || []
  )
  // 모달 열 때 상세이미지/HTML 단일 fetch (목록 API에서는 defer되어 비어있음)
  const openImageModal = () => {
    setProductImages(p.images || [])
    setShowImageModal(true)
    collectorApi.getProduct(p.id).then((full) => {
      const dimgs = (full?.detail_images && full.detail_images.length > 0)
        ? full.detail_images
        : (full?.detail_html || '').match(/src=["']([^"']+)["']/gi)
            ?.map((m: string) => m.replace(/src=["']/i, '').replace(/["']$/, '')) || []
      setDetailImgList(dimgs)
      if (full?.images && full.images.length > 0) setProductImages(full.images)
    }).catch(() => {})
  }
  // 원가: best_benefit_price(최대혜택가) > sale_price > original_price 순 우선
  const cost = p.cost || p.sale_price || p.original_price || 0
  const policy = policies.find((pol) => pol.id === p.applied_policy_id)
  const pricing = (policy?.pricing || {}) as Record<string, unknown>
  const baseMarginRate = (pricing.marginRate as number) || 15
  // 가격범위별 마진 매칭 (백엔드 _calculate_range_margin과 동일: cost >= min && cost < max)
  const useRangeMargin = Boolean(pricing.useRangeMargin)
  const rangeMargins = (pricing.rangeMargins as Array<{ min?: number; max?: number; rate?: number }>) || []
  const marginRate = useRangeMargin && rangeMargins.length > 0
    ? pickRangeMargin(cost, rangeMargins, baseMarginRate)
    : baseMarginRate
  const extraCharge = (pricing.extraCharge as number) || 0
  const shippingCost = (pricing.shippingCost as number) || 0
  const feeRate = (pricing.feeRate as number) || 0
  const minMarginAmount = (pricing.minMarginAmount as number) || 0
  // 소싱처별 추가 마진 추출
  const sourceSiteMargins = (pricing.sourceSiteMargins || {}) as Record<string, { marginRate?: number; marginAmount?: number }>
  const ssmData = getSourceSiteMargin(sourceSiteMargins, p.source_site)
  const ssMRate = ssmData.marginRate || 0
  const ssMAmount = ssmData.marginAmount || 0

  // 공통 가격 계산 (useMemo 캐싱)
  const { price: marketPrice, calcStr } = useMemo(
    () => calcPrice(cost, marginRate, shippingCost, feeRate, extraCharge, minMarginAmount, ssMRate, ssMAmount),
    [cost, marginRate, shippingCost, feeRate, extraCharge, minMarginAmount, ssMRate, ssMAmount],
  )

  const isActive = p.status === 'registered' || p.status === 'saved'
  const statusColor = isActive ? '#51CF66' : '#888'
  const statusBg = isActive ? 'rgba(81,207,102,0.12)' : 'rgba(100,100,100,0.15)'
  const statusText = p.status === 'registered' ? '등록됨' : p.status === 'saved' ? '저장됨' : ''

  const regDate = fmtDate(p.created_at)
  const updatedDate = fmtDate(p.updated_at)
  const no = String(idx + 1).padStart(6, '0')

  // 마켓별 개별 가격 계산 (useMemo 캐싱)
  const mp = (policy?.market_policies || {}) as Record<string, { accountId?: string; feeRate?: number; shippingCost?: number; marginRate?: number; brand?: string }>
  const marketPriceList = useMemo(() => Object.entries(mp)
    .filter(([, v]) => v.accountId)
    .map(([marketName, v]) => {
      const acct = v.accountId ? accMap.get(v.accountId) : undefined
      const acctFeeRate = Number((acct?.additional_fields as Record<string, unknown> | undefined)?.feeRate || 0)
      const acctExtraFeeRate = Number((acct?.additional_fields as Record<string, unknown> | undefined)?.extraFeeRate || 0)
      const r = calcPrice(cost, marginRate, (v.shippingCost ?? shippingCost) || shippingCost, acctFeeRate || v.feeRate || feeRate, extraCharge, minMarginAmount, ssMRate, ssMAmount)
      let displayPrice = r.price
      let displayCalcStr = r.calcStr
      // 스마트스토어: 300원 올림 반영 (백엔드 25% 역산과 동일)
      if (marketName.includes('스마트스토어')) {
        displayPrice = Math.ceil(r.price / 300) * 300
        const diff = displayPrice - r.price
        if (diff > 0) {
          displayCalcStr = `₩${fmt(displayPrice)} = ${r.calcStr.split(' = ')[1]} + 300원올림 +${fmt(diff)}`
        }
      }
      // SSG: 추가수수료율 역산 + 100원 단위 올림
      if (marketName === '신세계몰(전시)') {
        if (acctExtraFeeRate > 0) {
          const before = displayPrice
          displayPrice = Math.ceil(before / (1 - acctExtraFeeRate / 100))
          const extraAmt = displayPrice - before
          const baseCalc = displayCalcStr.split(' = ').slice(1).join(' = ')
          displayCalcStr = `₩${fmt(displayPrice)} = ${baseCalc} + 추가수수료 ${fmt(extraAmt)}(${acctExtraFeeRate}%)`
        }
        const rounded = Math.ceil(displayPrice / 100) * 100
        if (rounded !== displayPrice) {
          displayCalcStr = displayCalcStr.replace(/^₩[\d,]+/, `₩${fmt(rounded)}`)
          displayPrice = rounded
        }
      }
      return { marketName, price: displayPrice, calcStr: displayCalcStr }
    }), [mp, cost, marginRate, shippingCost, extraCharge, minMarginAmount, ssMRate, ssMAmount])

  const marketEnabled = (p.market_enabled || {}) as Record<string, boolean>

  // 상품의 카테고리 매핑 조회 (그룹 매핑 우선 → 카테고리 매핑 fallback)
  const productCatMapping = useMemo(() => {
    // 1순위: 그룹(search_filter_id)의 target_mappings
    if (p.search_filter_id && filters) {
      const sf = filters.find(f => f.id === p.search_filter_id)
      if (sf?.target_mappings && Object.keys(sf.target_mappings).length > 0) {
        return sf.target_mappings as Record<string, string>
      }
    }
    // 2순위: 카테고리 경로 기반 매핑 — product.category(전체 경로) 우선
    const site = p.source_site || ''
    let leafPath = ''
    if (p.category) {
      leafPath = p.category.split('>').map((c: string) => c.trim()).filter(Boolean).join(' > ')
    } else {
      leafPath = [p.category1, p.category2, p.category3, p.category4].filter(Boolean).join(' > ')
    }
    if (!site || !leafPath) return {}
    return catMappingMap.get(`${site}::${leafPath}`) || {}
  }, [p.source_site, p.category, p.category1, p.category2, p.category3, p.category4, p.search_filter_id, catMappingMap, filters])

  // 등록된 계정 기반 마켓 정보 (등록한 마켓만 표시용)
  const regAccIds = p.registered_accounts ?? []
  const marketProductNos = p.market_product_nos || {}
  const registeredMarkets = useMemo(() => {
    return regAccIds
      .map(aid => accMap.get(aid))
      .filter((a): a is SambaMarketAccount => !!a)
      .map(acc => {
        const market = MARKETS.find(m => m.id === acc.market_type)
        // channelProductNo(구매페이지용) 우선, 없으면 originProductNo 사용
        const productNo = marketProductNos[acc.id] || marketProductNos[`${acc.id}_origin`] || ''
        // 마켓 상품번호가 있으면 구매페이지 직접 링크, 없으면 검색 URL
        const extras = (acc.additional_fields || {}) as Record<string, string>
        const url = buildMarketProductUrl(
          acc.market_type,
          acc.seller_id || '',
          productNo,
          extras.storeSlug,
          { pid: marketProductNos[`${acc.id}_pid`], vid: marketProductNos[`${acc.id}_vid`] }
        )
          || (market?.searchUrl ? market.searchUrl + encodeURIComponent(p.name) : market?.url || '')
        return {
          marketId: acc.market_type,
          label: `${acc.market_name}(${acc.seller_id || acc.account_label || acc.business_name || '-'})`,
          accountName: acc.account_label || acc.seller_id || acc.business_name || '-',
          url,
          accId: acc.id,
        }
      })
  }, [regAccIds, accounts, p.name, marketProductNos]) // eslint-disable-line react-hooks/exhaustive-deps

  const tdLabel: React.CSSProperties = { padding: '6px 8px', color: '#555', fontSize: '0.75rem', whiteSpace: 'nowrap', verticalAlign: 'middle' }
  const tdVal: React.CSSProperties = { padding: '6px 8px', verticalAlign: 'middle' }
  const marketNameInputBaseStyle: React.CSSProperties = {
    width: '100%',
    padding: '2px 6px',
    fontSize: '0.72rem',
    background: '#1A1A1A',
    borderRadius: '3px',
    outline: 'none',
    userSelect: 'text',
    WebkitUserSelect: 'text',
  }

  return (
    <div style={{
      background: 'rgba(22,22,22,0.9)', border: '1px solid #2A2A2A', borderRadius: '10px',
      overflow: 'hidden',
    }}>
      {/* 업데이트 로그 바 */}
      {logMessage && (
        <div style={{
          padding: '6px 14px', fontSize: '0.75rem', color: '#FFB84D',
          background: 'rgba(255,140,0,0.08)', borderBottom: '1px solid rgba(255,140,0,0.15)',
          display: 'flex', alignItems: 'center', gap: '6px', overflow: 'hidden',
        }}>
          <span style={{ opacity: 0.7, flexShrink: 0 }}>&#9654;</span>
          <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{logMessage}</span>
        </div>
      )}
      {/* 가격/재고 이력 모달 */}
      {showPriceHistoryModal && (() => {
        if (priceHistoryData === null) {
          return (
            <div style={{ position: 'fixed', inset: 0, zIndex: 99998, display: 'flex', alignItems: 'center', justifyContent: 'center', background: 'rgba(0,0,0,0.6)' }}
              onClick={() => setShowPriceHistoryModal(false)}>
              <div style={{ background: '#1A1A1A', borderRadius: '10px', padding: '2rem', color: '#888', fontSize: '0.85rem' }}>이력 로딩 중...</div>
            </div>
          )
        }
        try {
        // null/non-object 엔트리 필터링 — DB 데이터 손상 방어
        const history = (Array.isArray(priceHistoryData) ? priceHistoryData : [])
          .filter((h): h is Record<string, unknown> => h != null && typeof h === 'object' && !Array.isArray(h))
        const isKream = p.source_site === 'KREAM'
        // 안전한 가격 포맷
        const fmtPrice = (v: unknown): string => { const n = Number(v); return isNaN(n) || n === 0 ? '-' : fmtNum(n) }
        // 원가(cost) 기준으로 최저/최고가 계산
        const costPrices = history.map(h => Number(h.cost || h.sale_price || 0)).filter(Boolean)
        const currentPrice = costPrices[0] || cost || p.sale_price || 0
        const minPrice = costPrices.length ? Math.min(...costPrices) : 0
        const maxPrice = costPrices.length ? Math.max(...costPrices) : 0
        const minEntry = history.find(h => Number(h.cost || h.sale_price || 0) === minPrice)
        const maxEntry = history.find(h => Number(h.cost || h.sale_price || 0) === maxPrice)
        // KREAM 빠른배송/일반배송 현재가
        const kreamFastMin = isKream && history[0] ? Number((history[0] as Record<string, unknown>).kream_fast_min) || 0 : 0
        const kreamGeneralMin = isKream && history[0] ? Number((history[0] as Record<string, unknown>).kream_general_min) || 0 : 0
        const fmtHistDate = (d: unknown) => {
          if (!d) return '-'
          const dt = new Date(String(d))
          return isNaN(dt.getTime()) ? String(d) : dt.toLocaleString('ko-KR', { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
        }
        const fmtShortDate = (d: unknown) => {
          if (!d) return '-'
          const dt = new Date(String(d))
          return isNaN(dt.getTime()) ? String(d) : dt.toLocaleString('ko-KR', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
        }

        return (
          <div
            style={{
              position: 'fixed', inset: 0, zIndex: 9999,
              background: 'rgba(0,0,0,0.75)', display: 'flex',
              alignItems: 'center', justifyContent: 'center',
            }}
            onClick={() => setShowPriceHistoryModal(false)}
          >
            <div
              style={{
                background: '#1A1A1A', border: '1px solid #2D2D2D', borderRadius: '12px',
                width: 'min(700px, 95vw)', maxHeight: '85vh', overflow: 'hidden',
                display: 'flex', flexDirection: 'column',
              }}
              onClick={(e) => e.stopPropagation()}
            >
              {/* 헤더 */}
              <div style={{
                display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                padding: '14px 20px', borderBottom: '1px solid #2D2D2D',
              }}>
                <h3 style={{ margin: 0, fontSize: '0.9rem', fontWeight: 600, color: '#E5E5E5' }}>
                  가격 / 재고 이력
                </h3>
                <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                  <span style={{ fontSize: '0.75rem', color: '#666' }}>{fmtNum(history.length)}건 기록</span>
                  <button
                    onClick={() => setShowPriceHistoryModal(false)}
                    style={{ background: 'transparent', border: 'none', color: '#888', fontSize: '1.2rem', cursor: 'pointer' }}
                  >✕</button>
                </div>
              </div>

              {/* 상품 정보 + 요약 */}
              <div style={{ padding: '12px 20px', borderBottom: '1px solid #2D2D2D' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '10px' }}>
                  <span style={{
                    fontSize: '0.65rem', padding: '2px 6px', borderRadius: '3px',
                    background: 'rgba(255,140,0,0.15)', color: '#FF8C00', fontWeight: 600,
                  }}>{p.source_site}</span>
                  <span style={{ fontSize: '0.75rem', color: '#999' }}>{p.name}</span>
                </div>
                {costPrices.length > 0 && (
                  <div style={{ display: 'flex', gap: '20px', fontSize: '0.78rem', flexWrap: 'wrap' }}>
                    {isKream && kreamFastMin > 0 && (
                      <div>
                        <span style={{ color: '#666' }}>빠른배송 </span>
                        <span style={{ color: '#FF8C00', fontWeight: 600 }}>₩ {fmtPrice(kreamFastMin)}</span>
                      </div>
                    )}
                    {isKream && kreamGeneralMin > 0 && (
                      <div>
                        <span style={{ color: '#666' }}>일반배송 </span>
                        <span style={{ color: '#E5E5E5', fontWeight: 600 }}>₩ {fmtPrice(kreamGeneralMin)}</span>
                      </div>
                    )}
                    {!isKream && (
                      <div>
                        <span style={{ color: '#666' }}>현재가 </span>
                        <span style={{ color: '#E5E5E5', fontWeight: 600 }}>₩ {fmtPrice(currentPrice)}</span>
                      </div>
                    )}
                    <div>
                      <span style={{ color: '#666' }}>최저가 </span>
                      <span style={{ color: '#51CF66', fontWeight: 600 }}>₩ {fmtPrice(minPrice)}</span>
                      {minEntry && <span style={{ color: '#555', fontSize: '0.68rem' }}> ({fmtShortDate(minEntry.date)})</span>}
                    </div>
                    <div>
                      <span style={{ color: '#666' }}>최고가 </span>
                      <span style={{ color: '#FF6B6B', fontWeight: 600 }}>₩ {fmtPrice(maxPrice)}</span>
                      {maxEntry && <span style={{ color: '#555', fontSize: '0.68rem' }}> ({fmtShortDate(maxEntry.date)})</span>}
                    </div>
                  </div>
                )}
              </div>

              {/* 이력 테이블 */}
              <div style={{ overflowY: 'auto', padding: '0' }}>
                {history.length === 0 ? (
                  <div style={{ padding: '2rem', textAlign: 'center', color: '#555', fontSize: '0.85rem' }}>
                    가격 변동 이력 없음<br />
                    <span style={{ fontSize: '0.75rem', color: '#444' }}>업데이트 시 이력이 기록됩니다</span>
                  </div>
                ) : (
                  <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.78rem' }}>
                    <thead>
                      <tr style={{ borderBottom: '1px solid #2D2D2D' }}>
                        <th style={{ padding: '8px 16px', textAlign: 'left', color: '#888', fontWeight: 500 }}>날짜</th>
                        {isKream ? (
                          <>
                            <th style={{ padding: '8px 16px', textAlign: 'right', color: '#888', fontWeight: 500 }}>빠른배송(₩)</th>
                            <th style={{ padding: '8px 16px', textAlign: 'right', color: '#888', fontWeight: 500 }}>일반배송(₩)</th>
                          </>
                        ) : (
                          <th style={{ padding: '8px 16px', textAlign: 'right', color: '#888', fontWeight: 500 }}>원가(₩)</th>
                        )}
                        <th style={{ padding: '8px 16px', textAlign: 'right', color: '#888', fontWeight: 500 }}>재고(수량/O/X)</th>
                      </tr>
                    </thead>
                    <tbody>
                      {history.map((h, i) => {
                        const rawOpts = h.options
                        // null/non-object 옵션 필터링
                        const opts = (Array.isArray(rawOpts) ? rawOpts : [])
                          .filter((o): o is Record<string, unknown> => o != null && typeof o === 'object') as Array<{ name?: string; price?: number; stock?: number; isSoldOut?: boolean }>
                        return (
                          <React.Fragment key={i}>
                            {/* 메인 행: 날짜 + 가격 + 옵션 요약 */}
                            <tr style={{ borderTop: i > 0 ? '1px solid #2D2D2D' : 'none', background: 'rgba(255,255,255,0.02)' }}>
                              <td style={{ padding: '8px 16px', color: '#C5C5C5', fontWeight: 600, fontSize: '0.78rem' }}>
                                {fmtHistDate(h.date)}
                              </td>
                              {isKream ? (
                                <>
                                  <td style={{ padding: '8px 16px', textAlign: 'right', color: '#FF8C00', fontWeight: 600 }}>
                                    {Number(h.kream_fast_min) > 0 ? `₩ ${fmtPrice(h.kream_fast_min)}` : '-'}
                                  </td>
                                  <td style={{ padding: '8px 16px', textAlign: 'right', color: '#FFB84D', fontWeight: 600 }}>
                                    {Number(h.kream_general_min) > 0 ? `₩ ${fmtPrice(h.kream_general_min)}` : '-'}
                                  </td>
                                </>
                              ) : (
                                <td style={{ padding: '8px 16px', textAlign: 'right', color: '#FFB84D', fontWeight: 600 }}>
                                  ₩ {fmtPrice(h.cost || h.sale_price)}
                                </td>
                              )}
                              <td style={{ padding: '8px 16px', textAlign: 'right', color: '#888' }}>
                                {opts.length > 0 ? `${fmtNum(opts.length)}개 옵션` : '-'}
                              </td>
                            </tr>
                            {/* 옵션 상세 행 */}
                            {opts.map((opt, oi) => {
                              const kOpt = opt as Record<string, unknown>
                              const stk = Number(opt.stock)
                              const soldOut = opt.isSoldOut || (opt.stock !== undefined && opt.stock !== null && stk <= 0)
                              const stockLabel = soldOut
                                ? '품절'
                                : opt.stock !== undefined && opt.stock !== null
                                  ? `${fmtNum(stk)}개`
                                  : 'O'
                              return (
                                <tr key={oi} style={{ borderTop: '1px solid #1A1A1A' }}>
                                  <td style={{ padding: '4px 16px 4px 32px', color: '#666', fontSize: '0.73rem' }}>
                                    ㄴ {opt.name || `옵션${oi + 1}`}
                                  </td>
                                  {isKream ? (
                                    <>
                                      <td style={{ padding: '4px 16px', textAlign: 'right', color: '#888', fontSize: '0.73rem' }}>
                                        {Number(kOpt.kreamFastPrice) > 0 ? `₩ ${fmtPrice(kOpt.kreamFastPrice)}` : '-'}
                                      </td>
                                      <td style={{ padding: '4px 16px', textAlign: 'right', color: '#888', fontSize: '0.73rem' }}>
                                        {Number(kOpt.kreamGeneralPrice) > 0 ? `₩ ${fmtPrice(kOpt.kreamGeneralPrice)}` : '-'}
                                      </td>
                                    </>
                                  ) : (
                                    <td style={{ padding: '4px 16px', textAlign: 'right', color: '#888', fontSize: '0.73rem' }}>
                                      ₩ {fmtPrice(h.cost || h.sale_price)}
                                    </td>
                                  )}
                                  <td style={{
                                    padding: '4px 16px', textAlign: 'right', fontSize: '0.73rem', fontWeight: 600,
                                    color: soldOut ? '#FF6B6B' : '#51CF66',
                                  }}>
                                    {stockLabel}
                                  </td>
                                </tr>
                              )
                            })}
                          </React.Fragment>
                        )
                      })}
                    </tbody>
                  </table>
                )}
              </div>
            </div>
          </div>
        )
        } catch (err) {
          const errMsg = err instanceof Error ? err.message : String(err)
          console.error('[가격이력] 렌더링 에러:', errMsg, err instanceof Error ? err.stack : '')
          console.error('[가격이력] 데이터 샘플:', JSON.stringify(priceHistoryData?.slice(0, 2)))
          return (
            <div style={{ position: 'fixed', inset: 0, zIndex: 99998, display: 'flex', alignItems: 'center', justifyContent: 'center', background: 'rgba(0,0,0,0.6)' }}
              onClick={() => setShowPriceHistoryModal(false)}>
              <div style={{ background: '#1A1A1A', borderRadius: '10px', padding: '2rem', maxWidth: '400px' }}>
                <div style={{ color: '#FF6B6B', fontSize: '0.85rem', marginBottom: '8px' }}>이력 데이터 표시 실패</div>
                <div style={{ color: '#666', fontSize: '0.72rem' }}>{errMsg}</div>
              </div>
            </div>
          )
        }
      })()}

      {/* 이미지 변경 모달 */}
      {showImageModal && (() => {
        // 대표이미지: 첫번째, 추가이미지: 나머지
        const mainImg = productImages[0] || ''
        const coupangMainImg = p.coupang_main_image || ''
        const extraImgs = productImages.slice(1)
        // 상세페이지 이미지: detail_images 필드 우선, 없으면 detail_html에서 추출
        const detailImgs = detailImgList
            ?.map((url: string) => url.startsWith('//') ? `https:${url}` : url) || []

        const tabStyle = (active: boolean) => ({
          padding: '8px 16px', fontSize: '0.8rem', fontWeight: active ? 600 : 400,
          color: active ? '#FF8C00' : '#888', cursor: 'pointer',
          border: 'none', borderBottom: active ? '2px solid #FF8C00' : '2px solid transparent',
          background: 'transparent',
        })

        // 이미지 행 렌더
        const renderImageRow = (img: string, i: number, list: string[], setList: (imgs: string[]) => void, label?: string) => (
          <div key={i} style={{
            display: 'flex', alignItems: 'center', gap: '12px', padding: '8px', borderRadius: '8px',
            background: label ? 'rgba(255,140,0,0.06)' : 'rgba(30,30,30,0.5)',
            border: label ? '1px solid rgba(255,140,0,0.2)' : '1px solid #2D2D2D',
          }}>
            <div
              onClick={() => openZoom(img, list)}
              style={{ width: 64, height: 64, borderRadius: '6px', border: '1px solid #2D2D2D', flexShrink: 0, cursor: 'pointer', overflow: 'hidden', background: '#1A1A1A', position: 'relative' }}
            >
              <img src={img} alt="" loading="lazy" referrerPolicy="no-referrer"
                onError={e => {
                  const el = e.target as HTMLImageElement
                  // 프록시 재시도 (1회)
                  if (!el.dataset.retried) {
                    el.dataset.retried = '1'
                    el.src = `${API_BASE}/api/v1/samba/proxy/image-proxy?url=${encodeURIComponent(img)}`
                  } else {
                    el.style.display = 'none'
                  }
                }}
                style={{ width: '100%', height: '100%', objectFit: 'cover', display: 'block', position: 'relative', zIndex: 1 }} />
              <span style={{ position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#555', fontSize: '0.6rem', zIndex: 0 }}>IMG</span>
            </div>
            <div style={{ flex: 1, minWidth: 0 }}>
              {label && <span style={{ fontSize: '0.7rem', color: '#FF8C00', fontWeight: 600 }}>{label}</span>}
              <p style={{ margin: 0, fontSize: '0.68rem', color: '#555', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{img}</p>
            </div>
            <div style={{ display: 'flex', gap: '4px', flexShrink: 0 }}>
              {i > 0 && <button onClick={() => { const a = [...list]; [a[i-1], a[i]] = [a[i], a[i-1]]; setList(a) }}
                style={{ padding: '3px 8px', fontSize: '0.7rem', borderRadius: '4px', cursor: 'pointer', border: '1px solid #2D2D2D', background: 'transparent', color: '#888' }}>▲</button>}
              {i < list.length - 1 && <button onClick={() => { const a = [...list]; [a[i+1], a[i]] = [a[i], a[i+1]]; setList(a) }}
                style={{ padding: '3px 8px', fontSize: '0.7rem', borderRadius: '4px', cursor: 'pointer', border: '1px solid #2D2D2D', background: 'transparent', color: '#888' }}>▼</button>}
              <button onClick={() => {
                setCardConfirm({
                  msg: '이 이미지를 모든 상품에서 삭제하시겠습니까?',
                  onOk: async () => {
                    setCardConfirm(null)
                    try {
                      // 상세페이지 이미지는 detail_images 배열 + detail_html 본문 두 곳 모두 처리
                      // (탭 상태로 판별 — detailImgs는 .map()으로 복사된 새 배열이라 detailImgList와 참조 불일치)
                      const fields = imageTab === 'detail' ? ['detail_images', 'detail_html'] : ['images']
                      const res = await collectorApi.bulkRemoveImage(img, fields)
                      setList(list.filter((_, j) => j !== i))
                      setCardAlert({ msg: `${fmtNum(res.removed)}개 상품에서 삭제 완료`, type: 'success' })
                    } catch (e) { setCardAlert({ msg: '추적삭제 실패: ' + (e instanceof Error ? e.message : String(e)), type: 'error' }) }
                  },
                })
              }}
                style={{ padding: '3px 8px', fontSize: '0.7rem', borderRadius: '4px', cursor: 'pointer', border: '1px solid rgba(168,85,247,0.3)', background: 'rgba(168,85,247,0.08)', color: '#A855F7' }}>추적삭제</button>
              <button onClick={() => setList(list.filter((_, j) => j !== i))}
                style={{ padding: '3px 8px', fontSize: '0.7rem', borderRadius: '4px', cursor: 'pointer', border: '1px solid rgba(255,107,107,0.3)', background: 'rgba(255,107,107,0.08)', color: '#FF6B6B' }}>삭제</button>
            </div>
          </div>
        )

        return (
          <div style={{ position: 'fixed', inset: 0, zIndex: 9999, background: 'rgba(0,0,0,0.75)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}
            onClick={() => setShowImageModal(false)}>
            <div style={{ background: '#1A1A1A', border: '1px solid #2D2D2D', borderRadius: '12px', width: 'min(750px, 95vw)', maxHeight: '85vh', overflow: 'hidden', display: 'flex', flexDirection: 'column' }}
              onClick={e => e.stopPropagation()}>
              {/* 헤더 + 탭 */}
              <div style={{ padding: '14px 20px 0', borderBottom: '1px solid #2D2D2D' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '10px' }}>
                  <div>
                    <h3 style={{ margin: 0, fontSize: '0.9rem', fontWeight: 600, color: '#E5E5E5' }}>이미지 변경</h3>
                    <p style={{ margin: 0, fontSize: '0.72rem', color: '#666' }}>{p.name?.slice(0, 50)}</p>
                  </div>
                  <button onClick={() => setShowImageModal(false)} style={{ background: 'transparent', border: 'none', color: '#888', fontSize: '1.2rem', cursor: 'pointer' }}>✕</button>
                </div>
                <div style={{ display: 'flex', gap: '0' }}>
                  <button onClick={() => setImageTab('main')} style={tabStyle(imageTab === 'main')}>대표 이미지변경</button>
                  <button onClick={() => setImageTab('extra')} style={tabStyle(imageTab === 'extra')}>추가이미지 변경</button>
                  <button onClick={() => setImageTab('detail')} style={tabStyle(imageTab === 'detail')}>상세페이지 이미지</button>
                  <button onClick={() => setImageTab('video')} style={tabStyle(imageTab === 'video')}>영상</button>
                </div>
              </div>

              {/* 탭 내용 */}
              <div style={{ overflowY: 'auto', padding: '16px 20px', flex: 1 }}>
                {imageTab === 'main' && (
                  <div>
                    {/* ── 공통 대표이미지 ── */}
                    <div style={{ marginBottom: '20px' }}>
                      <p style={{ fontSize: '0.78rem', color: '#FF8C00', fontWeight: 600, marginBottom: '8px' }}>공통 대표이미지</p>
                      <p style={{ fontSize: '0.72rem', color: '#888', marginBottom: '12px' }}>
                        ※ 쿠팡을 제외한 모든 마켓에 적용됩니다. 쿠팡 대표이미지가 미설정이면 공통이 사용됩니다.
                      </p>
                      {mainImg ? (
                        <div style={{ display: 'flex', gap: '20px', alignItems: 'flex-start' }}>
                          <div>
                            <p style={{ fontSize: '0.72rem', color: '#888', marginBottom: '6px' }}>[현재 대표이미지]</p>
                            <img src={mainImg} alt="대표이미지" loading="lazy" referrerPolicy="no-referrer"
                              onError={e => { (e.target as HTMLImageElement).style.display = 'none' }}
                              onClick={() => openZoom(mainImg)}
                              style={{ width: 200, height: 200, objectFit: 'cover', borderRadius: '8px', border: '1px solid #2D2D2D', cursor: 'pointer' }} />
                            <p style={{ margin: '6px 0 0', fontSize: '0.65rem', color: '#555', wordBreak: 'break-all' }}>{mainImg}</p>
                          </div>
                          <div style={{ flex: 1 }}>
                            <p style={{ fontSize: '0.72rem', color: '#888', marginBottom: '6px' }}>이미지 URL 변경</p>
                            <div style={{ display: 'flex', gap: '6px' }}>
                              <input type="text" placeholder="http:// 를 포함한 이미지 경로" defaultValue=""
                                id="main-image-url-input"
                                style={{ flex: 1, fontSize: '0.78rem', padding: '6px 10px', background: '#1E1E1E', border: '1px solid #3D3D3D', color: '#E5E5E5', borderRadius: '6px' }} />
                              <button onClick={() => {
                                const input = document.getElementById('main-image-url-input') as HTMLInputElement
                                if (input?.value.trim()) {
                                  const newImgs = [input.value.trim(), ...productImages.slice(1)]
                                  setProductImages(newImgs)
                                  const ud: Partial<SambaCollectedProduct> = { images: newImgs }
                                  if (!(p.tags || []).includes('__img_edited__')) {
                                    ud.tags = [...(p.tags || []), '__img_edited__']
                                  }
                                  collectorApi.updateProduct(p.id, ud).then(() => {
                                    onProductUpdate(p.id, ud)
                                  }).catch(() => {})
                                  input.value = ''
                                }
                              }} style={{ padding: '6px 14px', fontSize: '0.78rem', borderRadius: '6px', border: '1px solid #FF8C00', background: 'rgba(255,140,0,0.15)', color: '#FF8C00', cursor: 'pointer', whiteSpace: 'nowrap' }}>변경완료</button>
                            </div>
                            <button onClick={() => {
                              const remaining = productImages.slice(1)
                              const newImgs = remaining.length > 0 ? remaining : []
                              setProductImages(newImgs)
                              const updateData: Partial<SambaCollectedProduct> = { images: newImgs }
                              if (!(p.tags || []).includes('__img_edited__')) {
                                updateData.tags = [...(p.tags || []), '__img_edited__']
                              }
                              collectorApi.updateProduct(p.id, updateData).then(() => {
                                onProductUpdate(p.id, updateData)
                              }).catch(() => {})
                            }} style={{
                              marginTop: '8px', padding: '5px 14px', fontSize: '0.72rem', borderRadius: '6px',
                              border: '1px solid rgba(255,107,107,0.4)', background: 'rgba(255,107,107,0.08)',
                              color: '#FF6B6B', cursor: 'pointer', whiteSpace: 'nowrap',
                            }}>대표이미지 삭제</button>
                            <button onClick={() => {
                              setCardConfirm({
                                msg: '이 대표이미지를 동일 이미지를 가진 모든 상품에서 삭제하시겠습니까?',
                                onOk: async () => {
                                  setCardConfirm(null)
                                  try {
                                    const res = await collectorApi.bulkRemoveImage(mainImg, ['images'])
                                    const remaining = productImages.slice(1)
                                    setProductImages(remaining)
                                    setCardAlert({ msg: `${fmtNum(res.removed)}개 상품에서 대표이미지 추적삭제 완료`, type: 'success' })
                                  } catch (e) { setCardAlert({ msg: '추적삭제 실패: ' + (e instanceof Error ? e.message : String(e)), type: 'error' }) }
                                },
                              })
                            }} style={{
                              marginTop: '4px', padding: '5px 14px', fontSize: '0.72rem', borderRadius: '6px',
                              border: '1px solid rgba(168,85,247,0.4)', background: 'rgba(168,85,247,0.08)',
                              color: '#A855F7', cursor: 'pointer', whiteSpace: 'nowrap',
                            }}>추적삭제</button>
                          </div>
                        </div>
                      ) : (
                        <div style={{ padding: '2rem', textAlign: 'center', color: '#555' }}>대표이미지 없음</div>
                      )}
                    </div>

                    {/* ── 쿠팡 대표이미지 ── */}
                    <div style={{ borderTop: '1px solid #2D2D2D', paddingTop: '16px' }}>
                      <p style={{ fontSize: '0.78rem', color: '#00B4D8', fontWeight: 600, marginBottom: '8px' }}>쿠팡 대표이미지</p>
                      <p style={{ fontSize: '0.72rem', color: '#888', marginBottom: '12px' }}>
                        ※ 쿠팡은 상품컷(누끼)이 필요합니다. 미설정 시 공통 대표이미지가 사용됩니다.
                      </p>
                      <div style={{ display: 'flex', gap: '20px', alignItems: 'flex-start' }}>
                        {coupangMainImg && (
                          <div>
                            <p style={{ fontSize: '0.72rem', color: '#888', marginBottom: '6px' }}>[현재 쿠팡 대표이미지]</p>
                            <img src={coupangMainImg} alt="쿠팡 대표이미지" loading="lazy" referrerPolicy="no-referrer"
                              onError={e => { (e.target as HTMLImageElement).style.display = 'none' }}
                              onClick={() => openZoom(coupangMainImg)}
                              style={{ width: 200, height: 200, objectFit: 'cover', borderRadius: '8px', border: '1px solid #00B4D8', cursor: 'pointer' }} />
                            <p style={{ margin: '6px 0 0', fontSize: '0.65rem', color: '#555', wordBreak: 'break-all' }}>{coupangMainImg}</p>
                          </div>
                        )}
                        <div style={{ flex: 1 }}>
                          <p style={{ fontSize: '0.72rem', color: '#888', marginBottom: '6px' }}>쿠팡 대표이미지 URL</p>
                          <div style={{ display: 'flex', gap: '6px' }}>
                            <input type="text" placeholder="http:// 를 포함한 상품컷 이미지 경로" defaultValue=""
                              id="coupang-main-image-url-input"
                              style={{ flex: 1, fontSize: '0.78rem', padding: '6px 10px', background: '#1E1E1E', border: '1px solid #3D3D3D', color: '#E5E5E5', borderRadius: '6px' }} />
                            <button onClick={() => {
                              const input = document.getElementById('coupang-main-image-url-input') as HTMLInputElement
                              const val = input?.value.trim() || ''
                              const ud: Partial<SambaCollectedProduct> = { coupang_main_image: val || undefined }
                              collectorApi.updateProduct(p.id, ud).then(() => {
                                onProductUpdate(p.id, ud)
                              }).catch(() => {})
                              if (input) input.value = ''
                            }} style={{ padding: '6px 14px', fontSize: '0.78rem', borderRadius: '6px', border: '1px solid #00B4D8', background: 'rgba(0,180,216,0.15)', color: '#00B4D8', cursor: 'pointer', whiteSpace: 'nowrap' }}>변경완료</button>
                          </div>
                          {coupangMainImg && (
                            <button onClick={() => {
                              const ud: Partial<SambaCollectedProduct> = { coupang_main_image: '' }
                              collectorApi.updateProduct(p.id, ud).then(() => {
                                onProductUpdate(p.id, ud)
                              }).catch(() => {})
                            }} style={{
                              marginTop: '8px', padding: '5px 14px', fontSize: '0.72rem', borderRadius: '6px',
                              border: '1px solid rgba(255,107,107,0.4)', background: 'rgba(255,107,107,0.08)',
                              color: '#FF6B6B', cursor: 'pointer', whiteSpace: 'nowrap',
                            }}>쿠팡 대표이미지 삭제</button>
                          )}
                          {!coupangMainImg && (
                            <p style={{ marginTop: '8px', fontSize: '0.72rem', color: '#666' }}>
                              미설정 → 공통 대표이미지 사용 중
                            </p>
                          )}
                        </div>
                      </div>
                    </div>
                  </div>
                )}

                {imageTab === 'extra' && (
                  <div>
                    <p style={{ fontSize: '0.72rem', color: '#888', marginBottom: '12px' }}>
                      ※ 추가이미지 순서를 변경하거나 삭제할 수 있습니다.
                    </p>
                    {extraImgs.length === 0 ? (
                      <div style={{ padding: '2rem', textAlign: 'center', color: '#555' }}>추가이미지 없음</div>
                    ) : (
                      <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                        {extraImgs.map((img, i) => renderImageRow(img, i, extraImgs, async (newList) => {
                          const newImgs = [mainImg, ...newList]
                          try {
                            const updateData: Partial<SambaCollectedProduct> = { images: newImgs }
                            if (!(p.tags || []).includes('__img_edited__')) {
                              updateData.tags = [...(p.tags || []), '__img_edited__']
                            }
                            await collectorApi.updateProduct(p.id, updateData)
                            setProductImages(newImgs)
                            onProductUpdate(p.id, updateData)
                          } catch (e) {
                            console.error('[이미지삭제] 저장 실패:', e)
                            setCardAlert({ msg: '이미지 변경 저장 실패: ' + (e instanceof Error ? e.message : String(e)), type: 'error' })
                          }
                        }, i === 0 ? `추가 1` : undefined))}
                      </div>
                    )}
                    {/* URL로 추가 */}
                    <div style={{ display: 'flex', gap: '6px', marginTop: '12px' }}>
                      <input type="text" placeholder="추가할 이미지 URL" id="extra-image-url-input"
                        style={{ flex: 1, fontSize: '0.78rem', padding: '6px 10px', background: '#1E1E1E', border: '1px solid #3D3D3D', color: '#E5E5E5', borderRadius: '6px' }} />
                      <button onClick={() => {
                        const input = document.getElementById('extra-image-url-input') as HTMLInputElement
                        if (input?.value.trim()) {
                          const newImgs = [...productImages, input.value.trim()]
                          setProductImages(newImgs)
                          const ud: Partial<SambaCollectedProduct> = { images: newImgs }
                          if (!(p.tags || []).includes('__img_edited__')) {
                            ud.tags = [...(p.tags || []), '__img_edited__']
                          }
                          collectorApi.updateProduct(p.id, ud).then(() => {
                            onProductUpdate(p.id, ud)
                          }).catch(() => {})
                          input.value = ''
                        }
                      }} style={{ padding: '6px 14px', fontSize: '0.78rem', borderRadius: '6px', border: '1px solid #3D3D3D', background: 'rgba(255,255,255,0.05)', color: '#C5C5C5', cursor: 'pointer', whiteSpace: 'nowrap' }}>추가</button>
                    </div>
                  </div>
                )}

                {imageTab === 'detail' && (
                  <div>
                    <p style={{ fontSize: '0.72rem', color: '#888', marginBottom: '12px' }}>
                      ※ 상세페이지에 포함된 이미지입니다. ({fmtNum(detailImgs.length)}개) — 클릭하여 삭제
                    </p>
                    {detailImgs.length === 0 ? (
                      <div style={{ padding: '2rem', textAlign: 'center', color: '#555' }}>상세페이지 이미지 없음</div>
                    ) : (
                      <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                        {detailImgs.map((img, i) => renderImageRow(img, i, detailImgs, async (newList) => {
                          try {
                            const updateData: Partial<SambaCollectedProduct> = { detail_images: newList }
                            if (!(p.tags || []).includes('__img_edited__')) {
                              updateData.tags = [...(p.tags || []), '__img_edited__']
                            }
                            await collectorApi.updateProduct(p.id, updateData)
                            setDetailImgList(newList)
                            onProductUpdate(p.id, updateData)
                          } catch (e) {
                            console.error('[상세이미지삭제] 저장 실패:', e)
                            setCardAlert({ msg: '상세이미지 변경 저장 실패: ' + (e instanceof Error ? e.message : String(e)), type: 'error' })
                          }
                        }))}
                      </div>
                    )}
                  </div>
                )}

                {imageTab === 'video' && (
                  <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', padding: '20px 0', gap: '12px' }}>
                    {p.video_url ? (
                      <>
                        <video
                          src={p.video_url}
                          controls
                          style={{ width: '100%', maxWidth: '480px', borderRadius: '8px', border: '1px solid #2D2D2D' }}
                        />
                        <div style={{ display: 'flex', gap: '8px' }}>
                          <a
                            href={p.video_url}
                            download={`${p.site_product_id || p.id}_video.mp4`}
                            style={{
                              fontSize: '0.78rem', padding: '6px 16px', borderRadius: '6px',
                              color: '#4C9AFF', border: '1px solid rgba(76,154,255,0.4)',
                              background: 'rgba(76,154,255,0.08)', textDecoration: 'none', cursor: 'pointer',
                            }}>다운로드</a>
                        </div>
                      </>
                    ) : (
                      <p style={{ fontSize: '0.8rem', color: '#666' }}>생성된 영상이 없습니다. 상단 영상생성 버튼으로 생성해주세요.</p>
                    )}
                  </div>
                )}

              </div>
            </div>

            {/* 이미지 확대 팝업 */}
            {zoomImg && (
              <div
                onClick={() => setZoomImg(null)}
                style={{
                  position: 'fixed', inset: 0, zIndex: 10000,
                  background: 'rgba(0,0,0,0.85)',
                  display: 'flex', alignItems: 'center', justifyContent: 'center',
                  cursor: 'pointer',
                }}
              >
                {/* 왼쪽 화살표 */}
                {zoomList.length > 1 && (
                  <button
                    onClick={e => { e.stopPropagation(); const prev = (zoomIdx - 1 + zoomList.length) % zoomList.length; setZoomIdx(prev); setZoomImg(zoomList[prev]) }}
                    style={{ position: 'absolute', left: '20px', top: '50%', transform: 'translateY(-50%)', background: 'rgba(0,0,0,0.5)', border: '1px solid #555', color: '#fff', fontSize: '1.5rem', padding: '8px 14px', borderRadius: '8px', cursor: 'pointer', zIndex: 1 }}
                  >‹</button>
                )}
                <div onClick={e => e.stopPropagation()} style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '8px', cursor: 'default' }}>
                  <img
                    src={zoomList[zoomIdx] || zoomImg}
                    alt=""
                    style={{ maxWidth: '85vw', maxHeight: '80vh', objectFit: 'contain', borderRadius: '8px' }}
                  />
                  <span style={{ color: '#888', fontSize: '0.8rem' }}>
                    {zoomIdx === 0 ? '대표' : `추가 ${fmtNum(zoomIdx)}`} ({fmtNum(zoomIdx + 1)}/{fmtNum(zoomList.length)})
                  </span>
                </div>
                {/* 오른쪽 화살표 */}
                {zoomList.length > 1 && (
                  <button
                    onClick={e => { e.stopPropagation(); const next = (zoomIdx + 1) % zoomList.length; setZoomIdx(next); setZoomImg(zoomList[next]) }}
                    style={{ position: 'absolute', right: '20px', top: '50%', transform: 'translateY(-50%)', background: 'rgba(0,0,0,0.5)', border: '1px solid #555', color: '#fff', fontSize: '1.5rem', padding: '8px 14px', borderRadius: '8px', cursor: 'pointer', zIndex: 1 }}
                  >›</button>
                )}
                <button
                  onClick={() => setZoomImg(null)}
                  style={{ position: 'absolute', top: '20px', right: '20px', background: 'rgba(0,0,0,0.5)', border: '1px solid #555', color: '#ccc', fontSize: '1.2rem', padding: '4px 10px', borderRadius: '6px', cursor: 'pointer' }}
                >✕</button>
              </div>
            )}
          </div>
        )
      })()}

      {/* Card header */}
      <div style={{
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        padding: '7px 14px', background: 'rgba(15,15,15,0.8)', borderBottom: '1px solid #222',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '12px', fontSize: '0.75rem', color: '#666' }}>
          {compact && (
            <button
              onClick={(e) => { e.stopPropagation(); onToggleExpand?.() }}
              style={{
                background: 'none', border: 'none', color: expanded ? '#FF8C00' : '#666',
                fontSize: '0.85rem', cursor: 'pointer', padding: '0 2px', lineHeight: 1,
              }}
            >{expanded ? '−' : '+'}</button>
          )}
          <input
            type="checkbox"
            checked={selectedIds.has(p.id)}
            onChange={(e) => onCheckboxToggle(p.id, e.target.checked)}
            style={{ accentColor: '#FF8C00', width: '13px', height: '13px', cursor: 'pointer' }}
          />
          <span style={{ color: '#FFFFFF', fontWeight: 600 }}>{p.site_product_id || no}</span>
          {p.source_site && (
            <span style={{
              fontSize: '0.7rem', color: '#FF8C00', background: 'rgba(255,140,0,0.1)',
              border: '1px solid rgba(255,140,0,0.25)', borderRadius: '4px',
              padding: '2px 8px', whiteSpace: 'nowrap',
            }}>{p.source_site}</span>
          )}
          <span>수집 <span style={{ color: '#888' }}>{regDate}</span></span>
          {p.updated_at && <span>최신화 <span style={{ color: '#888' }}>{updatedDate}</span></span>}
          {isActive && (
            <span style={{
              padding: '2px 10px', borderRadius: '4px', fontSize: '0.72rem', fontWeight: 500,
              background: statusBg, color: statusColor,
            }}>
              {statusText}
            </span>
          )}
          {p.sale_status === 'preorder' && (
            <span style={{
              padding: '2px 8px', borderRadius: '4px', fontSize: '0.72rem', fontWeight: 500,
              background: 'rgba(100,130,255,0.12)', color: '#6B8AFF',
              border: '1px solid rgba(100,130,255,0.25)',
            }}>판매예정</span>
          )}
          {(p.sale_status === 'sold_out' || p.is_sold_out ||
            (Array.isArray(p.options) && p.options.length > 0 &&
             (p.options as Array<{stock?: number}>).every(o => ((o as {stock?: number}).stock ?? 0) <= 0))
          ) && (
            <span style={{
              padding: '2px 8px', borderRadius: '4px', fontSize: '0.72rem', fontWeight: 500,
              background: 'rgba(255,107,107,0.12)', color: '#FF6B6B',
              border: '1px solid rgba(255,107,107,0.25)',
            }}>품절</span>
          )}
        </div>
        <div style={{ display: 'flex', gap: '6px', alignItems: 'center' }}>
          <label style={{ display: 'flex', alignItems: 'center', gap: '4px', cursor: collectBlocking ? 'wait' : 'pointer' }}>
            <input
              type="checkbox"
              checked={collectBlocking}
              disabled={collectBlocking}
              onChange={async (e) => {
                if (!e.target.checked || collectBlocking) return
                setCollectBlocking(true)
                try {
                  await onBlockCollect(p.id)
                } catch {
                  setCollectBlocking(false)
                }
              }}
              style={{ accentColor: '#FF6B6B', width: '12px', height: '12px', cursor: collectBlocking ? 'wait' : 'pointer' }}
            />
            <span style={{ fontSize: '0.7rem', color: collectBlocking ? '#FF6B6B' : '#888' }}>수집차단</span>
          </label>
          <label style={{ display: 'flex', alignItems: 'center', gap: '4px', cursor: 'pointer' }}>
            <input
              type="checkbox"
              checked={p.lock_stock || false}
              onChange={(e) => onLockToggle(p.id, 'lock_stock', e.target.checked)}
              style={{ accentColor: '#51CF66', width: '12px', height: '12px', cursor: 'pointer' }}
            />
            <span style={{ fontSize: '0.7rem', color: '#888' }}>재고잠금</span>
          </label>
          <label style={{ display: 'flex', alignItems: 'center', gap: '4px', cursor: 'pointer' }}>
            <input
              type="checkbox"
              checked={p.lock_delete || false}
              onChange={(e) => onLockToggle(p.id, 'lock_delete', e.target.checked)}
              style={{ accentColor: '#FF8C00', width: '12px', height: '12px', cursor: 'pointer' }}
            />
            <span style={{ fontSize: '0.7rem', color: '#888' }}>삭제잠금</span>
          </label>
          <button style={{
            fontSize: '0.7rem', padding: '3px 10px',
            border: '1px solid rgba(255,140,0,0.3)', borderRadius: '5px',
            color: '#FF8C00', background: 'rgba(255,140,0,0.08)', cursor: 'pointer',
          }}>수정</button>
          <button
            onClick={() => onDelete(p.id)}
            style={{
              fontSize: '0.7rem', padding: '3px 10px',
              border: '1px solid rgba(255,107,107,0.3)', borderRadius: '5px',
              color: '#FF6B6B', background: 'rgba(255,107,107,0.08)', cursor: 'pointer',
            }}
          >삭제</button>
        </div>
      </div>

      {/* Card body */}
      {(compact && !expanded) ? (
        /* 간단보기: 원 상품명 + 등록 상품명 + 브랜드 + 원가 한 줄 */
        <div style={{ padding: '8px 14px', display: 'flex', gap: '10px', alignItems: 'center', fontSize: '0.78rem' }}>
          <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '2px', flexShrink: 0 }}>
            <div onClick={() => openImageModal()} style={{ cursor: 'pointer' }}>
              <ProductImage src={p.images?.[0]} name={p.name} size={50} />
            </div>
            {(p.tags || []).includes('__ai_image__') && (
              <span style={{ fontSize: '0.55rem', padding: '1px 4px', borderRadius: '3px', color: '#FF8C00', border: '1px solid rgba(255,140,0,0.3)', background: 'rgba(255,140,0,0.08)' }}>AI</span>
            )}
          </div>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
              <span style={{ color: '#FFFFFF', fontWeight: 500, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1 }}>{p.name}</span>
              <button onClick={(e) => { e.stopPropagation(); openPriceHistory() }}
                style={{ fontSize: '0.6rem', padding: '2px 5px', borderRadius: '3px', cursor: 'pointer', border: '1px solid #2D2D2D', background: 'transparent', color: '#888', whiteSpace: 'nowrap' }}>이력</button>
              <button onClick={(e) => { e.stopPropagation(); const url = getSourceUrl(p); if (url) window.open(url, '_blank') }}
                style={{ fontSize: '0.6rem', padding: '2px 5px', borderRadius: '3px', cursor: 'pointer', border: '1px solid #2D2D2D', background: 'transparent', color: '#888', whiteSpace: 'nowrap' }}>원문</button>
              <button onClick={(e) => { e.stopPropagation(); onEnrich(p.id) }}
                style={{ fontSize: '0.6rem', padding: '2px 5px', borderRadius: '3px', cursor: 'pointer', border: '1px solid #2D2D2D', background: 'transparent', color: '#888', whiteSpace: 'nowrap' }}>업데이트</button>
              <button onClick={(e) => { e.stopPropagation(); window.open(`/samba/orders?cpId=${encodeURIComponent(p.id)}&cpName=${encodeURIComponent(p.name)}`, '_blank') }}
                style={{ fontSize: '0.6rem', padding: '2px 5px', borderRadius: '3px', cursor: 'pointer', border: '1px solid rgba(255,140,0,0.3)', background: 'transparent', color: '#FF8C00', whiteSpace: 'nowrap' }}>판매</button>
              <span style={{ color: '#FFB84D', fontWeight: 600, flexShrink: 0 }}>₩{fmt(cost)}</span>
            </div>
            <div style={{ color: '#888', fontSize: '0.72rem', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {composeProductName(p, nameRules.find(r => r.id === (policy?.extras as Record<string, string> | undefined)?.name_rule_id), deletionWords)}
            </div>
          </div>
        </div>
      ) : (
      <div style={{ display: 'flex', gap: '0', padding: '14px' }}>
        {/* Left: Image section */}
        <div style={{
          width: '130px', flexShrink: 0, display: 'flex', flexDirection: 'column',
          alignItems: 'center', gap: '8px', paddingRight: '14px', borderRight: '1px solid #222',
        }}>
          <div onClick={() => openImageModal()} style={{ cursor: 'pointer' }}>
            <ProductImage src={p.images?.[0]} name={p.name} size={110} />
          </div>
          {(p.tags || []).includes('__ai_image__') && (
            <span style={{
              fontSize: '0.68rem', padding: '3px 10px', borderRadius: '4px', width: '100%', textAlign: 'center',
              color: '#FF8C00', border: '1px solid rgba(255,140,0,0.3)', background: 'rgba(255,140,0,0.08)',
            }}>AI이미지</span>
          )}
          {/* 무배당발 배지 — 제거됨 */}
        </div>

        {/* Right: Detail info */}
        <div style={{ flex: 1, paddingLeft: '16px' }}>
          {/* Action button bar */}
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: '3px', marginBottom: '8px' }}>
            <button
              onClick={() => openPriceHistory()}
              style={{
                fontSize: '0.72rem', padding: '3px 9px', background: '#1E1E1E',
                color: '#999',
                border: '1px solid #2D2D2D',
                borderRadius: '3px', cursor: 'pointer', whiteSpace: 'nowrap',
              }}>가격변경이력</button>
            <button
              onClick={() => {
                const url = getSourceUrl(p)
                if (url) window.open(url, '_blank')
              }}
              style={{
                fontSize: '0.72rem', padding: '3px 9px', background: '#1E1E1E',
                color: '#999', border: '1px solid #2D2D2D', borderRadius: '3px', cursor: 'pointer', whiteSpace: 'nowrap',
              }}>원문링크</button>
            <button
              onClick={() => {
                // 상세페이지 미리보기 — 백엔드 _build_detail_html과 동일한 로직
                const imgs = p.images || []
                const detailImgs = (p as unknown as Record<string, string[]>).detail_images || []
                if (imgs.length === 0 && detailImgs.length === 0) { showAlert('이미지가 없습니다'); return }

                // 정책에서 상세 템플릿 조회 (detailTemplates에서 실제 매칭)
                const policy = policies.find(pol => pol.id === p.applied_policy_id)
                const tplId = policy?.extras?.detail_template_id
                const tpl = tplId ? detailTemplates.find(t => t.id === tplId) : null
                const topImg = tpl?.top_image_s3_key || ''
                const bottomImg = tpl?.bottom_image_s3_key || ''
                const checks: Record<string, boolean> = {
                  topImg: true, main: true, sub: true,
                  title: false, option: false, detail: false, bottomImg: true,
                  ...(tpl?.img_checks || {}),
                  ...(topImg ? { topImg: true } : {}),
                  ...(bottomImg ? { bottomImg: true } : {}),
                }
                const order = tpl?.img_order || ['topImg', 'main', 'sub', 'title', 'option', 'detail', 'bottomImg']

                const imgTag = (url: string) => `<div style="text-align:center;"><img src="${url}" style="max-width:860px;width:100%;" /></div>`
                const parts: string[] = []

                // 추가이미지(sub)에서 출력된 URL → detail에서 중복 제외
                const subSet = new Set(imgs.slice(1))
                for (const item of order) {
                  if (!checks[item]) continue
                  if (item === 'topImg' && topImg) { parts.push(imgTag(topImg)) }
                  else if (item === 'main' && imgs[0]) { parts.push(imgTag(imgs[0])) }
                  else if (item === 'sub' && imgs.length > 1) { imgs.slice(1).forEach(u => parts.push(imgTag(u))) }
                  else if (item === 'title' && p.name) { parts.push(`<div style="text-align:center;padding:1rem 0;"><h2 style="color:#333;font-size:1.25rem;">${p.name}</h2></div>`) }
                  else if (item === 'detail' && detailImgs.length > 0) { detailImgs.filter(u => !subSet.has(u)).forEach(u => parts.push(imgTag(u))) }
                  else if (item === 'bottomImg' && bottomImg) { parts.push(imgTag(bottomImg)) }
                }

                if (parts.length === 0) { showAlert('정책 템플릿이 적용되지 않았거나 표시할 이미지가 없습니다'); return }
                const html = `<!DOCTYPE html><html><head><meta charset="utf-8"><title>상세페이지 미리보기 - ${(p.name || '').replace(/"/g, '')}</title><style>body{margin:0 auto;background:#fff;padding:0;max-width:860px}img{max-width:860px;width:100%;display:block;margin:0 auto}</style></head><body>${parts.join('\n')}</body></html>`
                const blob = new Blob([html], { type: 'text/html' })
                window.open(URL.createObjectURL(blob), '_blank')
              }}
              style={{
                fontSize: '0.72rem', padding: '3px 9px', background: '#1E1E1E',
                color: '#4C9AFF', border: '1px solid rgba(76,154,255,0.3)', borderRadius: '3px', cursor: 'pointer', whiteSpace: 'nowrap',
              }}>상세페이지</button>
            <button
              onClick={() => onEnrich(p.id)}
              style={{
              fontSize: '0.72rem', padding: '3px 9px', background: '#1E1E1E',
              color: '#999', border: '1px solid #2D2D2D', borderRadius: '3px', cursor: 'pointer', whiteSpace: 'nowrap',
            }}>업데이트</button>
            <button
              onClick={() => window.open(`/samba/orders?cpId=${encodeURIComponent(p.id)}&cpName=${encodeURIComponent(p.name)}`, '_blank')}
              style={{
              fontSize: '0.72rem', padding: '3px 9px', background: '#1E1E1E',
              color: '#FF8C00', border: '1px solid rgba(255,140,0,0.3)', borderRadius: '3px', cursor: 'pointer', whiteSpace: 'nowrap',
            }}>판매이력</button>
            <button
              onClick={() => onMarketDelete(p.id)}
              style={{
              fontSize: '0.72rem', padding: '3px 9px', background: '#1E1E1E',
              color: '#FF6B6B', border: '1px solid rgba(255,107,107,0.2)', borderRadius: '3px', cursor: 'pointer', whiteSpace: 'nowrap',
            }}>마켓삭제</button>
          </div>

          {/* Detail table */}
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.8125rem' }}>
            <colgroup>
              <col style={{ width: '80px' }} />
              <col />
            </colgroup>
            <tbody>
              {/* 원 상품명 */}
              <tr style={{ borderBottom: '1px solid #1E1E1E' }}>
                <td style={tdLabel}>원 상품명</td>
                <td style={tdVal}>
                  <span style={{ color: '#FFFFFF', fontWeight: 500 }}>{p.name}</span>
                </td>
              </tr>
              {/* 등록 상품명 (상품명 조합 + 삭제어 취소선 적용) */}
              <tr style={{ borderBottom: '1px solid #1E1E1E' }}>
                <td style={tdLabel}>등록 상품명</td>
                <td style={tdVal}>
                  <span style={{ color: '#FFFFFF', fontSize: '0.8rem' }}>{
                    renderRegisteredName(
                      composeProductName(p, nameRules.find(r => r.id === (policy?.extras as Record<string, string> | undefined)?.name_rule_id)),
                      deletionWords ?? []
                    )
                  }</span>
                </td>
              </tr>
              {/* SEO 검색키워드 */}
              <tr style={{ borderBottom: '1px solid #1E1E1E' }}>
                <td style={tdLabel}>SEO</td>
                <td style={tdVal}>
                  <span style={{ color: (p.seo_keywords || []).length > 0 ? '#4C9AFF' : '#444', fontSize: '0.78rem' }}>
                    {(p.seo_keywords || []).join(', ') || '미설정 (AI태그 생성 필요)'}
                  </span>
                </td>
              </tr>
              {/* 영문 상품명 */}
              <tr style={{ borderBottom: '1px solid #1E1E1E' }}>
                <td style={tdLabel}>영문 상품명</td>
                <td style={tdVal}>
                  <input type="text" placeholder="영문 상품명 (English)" defaultValue={p.name_en || ''}
                    style={{ width: '100%', padding: '3px 7px', fontSize: '0.8rem', background: '#1A1A1A', border: '1px solid #2D2D2D', color: '#C5C5C5', borderRadius: '4px', outline: 'none' }} />
                </td>
              </tr>
              {/* 일문 상품명 */}
              <tr style={{ borderBottom: '1px solid #1E1E1E' }}>
                <td style={tdLabel}>일문 상품명</td>
                <td style={tdVal}>
                  <input type="text" placeholder="일문 상품명 (日本語)" defaultValue={p.name_ja || ''}
                    style={{ width: '100%', padding: '3px 7px', fontSize: '0.8rem', background: '#1A1A1A', border: '1px solid #2D2D2D', color: '#C5C5C5', borderRadius: '4px', outline: 'none' }} />
                </td>
              </tr>
              {/* 브랜드 */}
              <tr style={{ borderBottom: '1px solid #1E1E1E' }}>
                <td style={tdLabel}>브랜드</td>
                <td style={tdVal}>
                  <span style={{ color: '#888', fontSize: '0.8rem' }}>{p.brand || '-'}</span>
                </td>
              </tr>
              {/* 정상가 */}
              <tr style={{ borderBottom: '1px solid #1E1E1E' }}>
                <td style={tdLabel}>정상가</td>
                <td style={tdVal}>
                  <span style={{ color: '#C5C5C5', fontWeight: 600 }}>
                    {p.original_price > 0 ? `₩${fmt(p.original_price)}` : '-'}
                  </span>
                </td>
              </tr>
              {/* 할인가 (sale_price) */}
              {p.sale_price > 0 && p.sale_price < p.original_price && (
                <tr style={{ borderBottom: '1px solid #1E1E1E' }}>
                  <td style={tdLabel}>할인가</td>
                  <td style={tdVal}>
                    <span style={{ color: '#51CF66', fontWeight: 600 }}>₩{fmt(p.sale_price)}</span>
                    <span style={{ color: '#FF6B6B', fontSize: '0.72rem', marginLeft: '6px' }}>
                      {Math.round((1 - p.sale_price / p.original_price) * 100)}% 할인
                    </span>
                  </td>
                </tr>
              )}
              {/* 원가 (최대혜택가) */}
              <tr style={{ borderBottom: '1px solid #1E1E1E' }}>
                <td style={tdLabel}>원가</td>
                <td style={tdVal}>
                  <span style={{ color: '#FFB84D', fontWeight: 600 }}>₩{fmt(cost)}</span>
                  {(p.sourcing_shipping_fee ?? 0) > 0 && (
                    <span style={{ color: '#888', fontSize: '0.7rem', marginLeft: '0.25rem' }}>
                      (상품가 {fmt(cost - (p.sourcing_shipping_fee ?? 0))}+배송비 {fmt(p.sourcing_shipping_fee ?? 0)})
                    </span>
                  )}
                  {p.source_site === 'MUSINSA' && p.is_point_restricted === false && (
                    <span style={{ marginLeft: '0.4rem', fontSize: '0.65rem', padding: '1px 6px', borderRadius: '3px', background: 'rgba(81,207,102,0.12)', color: '#51CF66', border: '1px solid rgba(81,207,102,0.3)' }}>
                      적립금 사용
                    </span>
                  )}
                  {p.source_site === 'MUSINSA' && p.is_point_restricted === true && (
                    <span style={{ marginLeft: '0.4rem', fontSize: '0.65rem', padding: '1px 6px', borderRadius: '3px', background: 'rgba(150,150,150,0.12)', color: '#888', border: '1px solid rgba(150,150,150,0.3)' }}>
                      적립금 사용불가
                    </span>
                  )}
                </td>
              </tr>
              {/* Market price — 마켓별 또는 공통 */}
              {marketPriceList.length > 0 ? marketPriceList.map((m) => {
                const marketNames = (p.market_names || {}) as Record<string, string>
                const nameLimit = MARKET_NAME_LIMITS[m.marketName] || 100
                const byteLimit = MARKET_NAME_BYTE_LIMITS[m.marketName]
                const composedName = composeProductName(p, nameRules.find(r => r.id === (policy?.extras as Record<string, string> | undefined)?.name_rule_id), deletionWords)
                const currentMarketName = marketNames[m.marketName] || ''
                const baseText = currentMarketName || composedName
                const displayName = byteLimit
                  ? truncateToBytes(baseText, byteLimit)
                  : (currentMarketName || (composedName.length > nameLimit ? composedName.slice(0, nameLimit) : composedName))
                const isOverLimit = byteLimit
                  ? getByteLength(baseText) > byteLimit
                  : baseText.length > nameLimit
                const countLabel = byteLimit
                  ? `${fmtNum(getByteLength(displayName))}/${fmtNum(byteLimit)}B`
                  : `${fmtNum(displayName.length)}/${fmtNum(nameLimit)}`
                const placeholder = byteLimit ? truncateToBytes(composedName, byteLimit) : composedName.slice(0, nameLimit)
                return (
                <tr key={m.marketName} style={{ borderBottom: '1px solid #1E1E1E' }}>
                  <td style={tdLabel}>{m.marketName === '신세계몰(전시)' ? '신세계몰' : m.marketName}</td>
                  <td style={tdVal}>
                    <div style={{ display: 'flex', flexDirection: 'column', gap: '2px' }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
                        <span style={{ color: '#FFB84D', fontWeight: 600 }}>₩{fmt(m.price)}</span>
                        {(() => {
                          const marketKey = MARKETS.find(mk => m.marketName.includes(mk.name))?.id
                            || m.marketName.toLowerCase().replace(/\s/g, '')
                          const rms = registeredMarkets.filter(r => r.marketId === marketKey)
                          const mappedCat = productCatMapping[marketKey] || ''
                          return (<>
                            {rms.map(rm => (
                              <React.Fragment key={rm.accId}>
                              {rm.url ? (
                                <button
                                  onClick={() => window.open(rm.url, '_blank')}
                                  style={{ fontSize: '0.6rem', padding: '1px 5px', background: 'rgba(81,207,102,0.08)', color: '#51CF66', border: '1px solid rgba(81,207,102,0.25)', borderRadius: '3px', cursor: 'pointer', whiteSpace: 'nowrap' }}
                                  onMouseEnter={e => { e.currentTarget.style.background = 'rgba(81,207,102,0.2)' }}
                                  onMouseLeave={e => { e.currentTarget.style.background = 'rgba(81,207,102,0.08)' }}
                                  title={`${rm.label} 판매페이지`}
                                >{rm.accountName}</button>
                              ) : (
                                <span
                                  style={{ fontSize: '0.6rem', padding: '1px 5px', background: 'rgba(81,207,102,0.08)', color: '#51CF66', border: '1px solid rgba(81,207,102,0.25)', borderRadius: '3px', whiteSpace: 'nowrap' }}
                                  title={`${rm.label} 등록됨`}
                                >{rm.accountName}</span>
                              )}
                              {(() => {
                                const sentAt = p.last_sent_data?.[rm.accId]?.sent_at
                                if (!sentAt) return null
                                const d = new Date(sentAt)
                                const mm = String(d.getMonth() + 1).padStart(2, '0')
                                const dd = String(d.getDate()).padStart(2, '0')
                                const hh = String(d.getHours()).padStart(2, '0')
                                const mi = String(d.getMinutes()).padStart(2, '0')
                                return <span style={{ fontSize: '0.6rem', color: '#666', whiteSpace: 'nowrap' }}>{mm}-{dd} {hh}:{mi}</span>
                              })()}
                              </React.Fragment>
                            ))}
                            {mappedCat ? (
                              <span style={{ fontSize: '0.68rem', color: '#888', background: 'rgba(255,255,255,0.04)', padding: '1px 6px', borderRadius: '3px', border: '1px solid #2D2D2D' }}>{mappedCat}</span>
                            ) : (
                              <span style={{ fontSize: '0.68rem', color: '#555' }}>미매핑</span>
                            )}
                          </>)
                        })()}
                      </div>
                      <span style={{ fontSize: '0.72rem', color: '#666' }}>{m.calcStr}</span>
                      {/* 마켓별 등록 상품명 */}
                      <div style={{ display: 'flex', alignItems: 'center', gap: '4px', marginTop: '2px' }}>
                        <input
                          type="text"
                          defaultValue={currentMarketName}
                          placeholder={placeholder}
                          style={{
                            ...marketNameInputBaseStyle,
                            border: `1px solid ${isOverLimit ? '#FF6B6B' : '#2D2D2D'}`,
                            color: isOverLimit ? '#FF6B6B' : '#C5C5C5',
                          }}
                          onMouseDown={(e) => e.stopPropagation()}
                          onClick={(e) => e.stopPropagation()}
                          onBlur={(e) => {
                            const val = e.target.value.trim()
                            if (val === currentMarketName) return
                            const updated = { ...marketNames, [m.marketName]: val || undefined }
                            // 빈 값이면 키 제거
                            if (!val) delete updated[m.marketName]
                            const clean = Object.fromEntries(Object.entries(updated).filter(([, v]) => v))
                            collectorApi.updateProduct(p.id, { market_names: Object.keys(clean).length > 0 ? clean : undefined } as Partial<SambaCollectedProduct>).then(() => {
                              onProductUpdate(p.id, { market_names: Object.keys(clean).length > 0 ? clean : undefined } as Partial<SambaCollectedProduct>)
                              e.target.style.borderColor = '#51CF66'
                              setTimeout(() => { e.target.style.borderColor = '#2D2D2D' }, 1500)
                            }).catch(() => {
                              e.target.style.borderColor = '#FF6B6B'
                              setTimeout(() => { e.target.style.borderColor = '#2D2D2D' }, 1500)
                            })
                          }}
                          onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur() }}
                        />
                        <span style={{ fontSize: '0.65rem', color: isOverLimit ? '#FF6B6B' : '#555', whiteSpace: 'nowrap' }}>
                          {countLabel}
                        </span>
                      </div>
                    </div>
                  </td>
                </tr>
                )
              }) : (
                <tr style={{ borderBottom: '1px solid #1E1E1E' }}>
                  <td style={tdLabel}>마켓가격</td>
                  <td style={tdVal}>
                    <div style={{ display: 'flex', flexDirection: 'column', gap: '2px' }}>
                      <span style={{ color: '#FFB84D', fontWeight: 600 }}>₩{fmt(marketPrice)}</span>
                      <span style={{ fontSize: '0.72rem', color: '#666' }}>{calcStr}</span>
                    </div>
                  </td>
                </tr>
              )}
              {/* 스토어별 상품명 — 마켓가격 행에 없는 등록 스토어도 상품명 편집 가능 */}
              {(() => {
                const _mktNames = (p.market_names || {}) as Record<string, string>
                const _composed = composeProductName(p, nameRules.find(r => r.id === (policy?.extras as Record<string, string> | undefined)?.name_rule_id), deletionWords)
                // 마켓가격 행에 이미 표시된 마켓 ID 목록 (이름이 아닌 ID 기준 중복 제거)
                // "신세계몰(전시)" 같이 정책 마켓명이 MARKETS.name과 다른 경우도 처리
                const priceMarketIds = new Set(
                  marketPriceList
                    .map(m => MARKETS.find(mk => m.marketName.includes(mk.name))?.id)
                    .filter((id): id is string => !!id)
                )
                // 등록된 스토어 중 마켓가격 행이 없는 것만 추출
                const extraStores = registeredMarkets.filter(rm => !priceMarketIds.has(rm.marketId))
                if (extraStores.length === 0) return null
                return extraStores.map(rm => {
                  const mkt = MARKETS.find(m => m.id === rm.marketId)
                  const mktName = mkt?.name || rm.marketId
                  const nameLimit = MARKET_NAME_LIMITS[mktName] || 100
                  const byteLimit = MARKET_NAME_BYTE_LIMITS[mktName]
                  const curName = _mktNames[mktName] || ''
                  const baseText = curName || _composed
                  const dispName = byteLimit
                    ? truncateToBytes(baseText, byteLimit)
                    : (curName || (_composed.length > nameLimit ? _composed.slice(0, nameLimit) : _composed))
                  const isOver = byteLimit
                    ? getByteLength(baseText) > byteLimit
                    : baseText.length > nameLimit
                  const cntLabel = byteLimit
                    ? `${fmtNum(getByteLength(dispName))}/${fmtNum(byteLimit)}B`
                    : `${fmtNum(dispName.length)}/${fmtNum(nameLimit)}`
                  const ph = byteLimit ? truncateToBytes(_composed, byteLimit) : _composed.slice(0, nameLimit)
                  const _sentAt = p.last_sent_data?.[rm.accId]?.sent_at
                  let _sentLabel: string | null = null
                  if (_sentAt) {
                    const _d = new Date(_sentAt)
                    const _mm = String(_d.getMonth() + 1).padStart(2, '0')
                    const _dd = String(_d.getDate()).padStart(2, '0')
                    const _hh = String(_d.getHours()).padStart(2, '0')
                    const _mi = String(_d.getMinutes()).padStart(2, '0')
                    _sentLabel = `${_mm}-${_dd} ${_hh}:${_mi}`
                  }
                  const _mappedCat = productCatMapping[rm.marketId] || ''
                  return (
                    <tr key={`store-name-${rm.accId}`} style={{ borderBottom: '1px solid #1E1E1E' }}>
                      <td style={tdLabel}>{mktName}</td>
                      <td style={tdVal}>
                        <div style={{ display: 'flex', flexDirection: 'column', gap: '2px' }}>
                          {/* 등록 계정 초록 버튼 + 마지막 발송 시각 + 카테고리 (정책 marketsConfig 비어 있어도 표시) */}
                          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
                            {rm.url ? (
                              <button
                                onClick={() => window.open(rm.url, '_blank')}
                                style={{ fontSize: '0.6rem', padding: '1px 5px', background: 'rgba(81,207,102,0.08)', color: '#51CF66', border: '1px solid rgba(81,207,102,0.25)', borderRadius: '3px', cursor: 'pointer', whiteSpace: 'nowrap' }}
                                onMouseEnter={e => { e.currentTarget.style.background = 'rgba(81,207,102,0.2)' }}
                                onMouseLeave={e => { e.currentTarget.style.background = 'rgba(81,207,102,0.08)' }}
                                title={`${rm.label} 판매페이지`}
                              >{rm.accountName}</button>
                            ) : (
                              <span
                                style={{ fontSize: '0.6rem', padding: '1px 5px', background: 'rgba(81,207,102,0.08)', color: '#51CF66', border: '1px solid rgba(81,207,102,0.25)', borderRadius: '3px', whiteSpace: 'nowrap' }}
                                title={`${rm.label} 등록됨`}
                              >{rm.accountName}</span>
                            )}
                            {_sentLabel && <span style={{ fontSize: '0.6rem', color: '#666', whiteSpace: 'nowrap' }}>{_sentLabel}</span>}
                            {_mappedCat ? (
                              <span style={{ fontSize: '0.68rem', color: '#888', background: 'rgba(255,255,255,0.04)', padding: '1px 6px', borderRadius: '3px', border: '1px solid #2D2D2D' }}>{_mappedCat}</span>
                            ) : (
                              <span style={{ fontSize: '0.68rem', color: '#555' }}>미매핑</span>
                            )}
                          </div>
                          <div style={{ display: 'flex', alignItems: 'center', gap: '4px' }}>
                            <input
                              type="text"
                              defaultValue={curName}
                              placeholder={ph}
                              style={{
                                ...marketNameInputBaseStyle,
                                border: `1px solid ${isOver ? '#FF6B6B' : '#2D2D2D'}`,
                                color: isOver ? '#FF6B6B' : '#C5C5C5',
                              }}
                              onMouseDown={(e) => e.stopPropagation()}
                              onClick={(e) => e.stopPropagation()}
                              onBlur={(e) => {
                                const val = e.target.value.trim()
                                if (val === curName) return
                                const updated = { ..._mktNames, [mktName]: val || undefined }
                                if (!val) delete updated[mktName]
                                const clean = Object.fromEntries(Object.entries(updated).filter(([, v]) => v))
                                collectorApi.updateProduct(p.id, { market_names: Object.keys(clean).length > 0 ? clean : undefined } as Partial<SambaCollectedProduct>).then(() => {
                                  onProductUpdate(p.id, { market_names: Object.keys(clean).length > 0 ? clean : undefined } as Partial<SambaCollectedProduct>)
                                  e.target.style.borderColor = '#51CF66'
                                  setTimeout(() => { e.target.style.borderColor = '#2D2D2D' }, 1500)
                                }).catch(() => {
                                  e.target.style.borderColor = '#FF6B6B'
                                  setTimeout(() => { e.target.style.borderColor = '#2D2D2D' }, 1500)
                                })
                              }}
                              onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur() }}
                            />
                            <span style={{ fontSize: '0.65rem', color: isOver ? '#FF6B6B' : '#555', whiteSpace: 'nowrap' }}>
                              {cntLabel}
                            </span>
                          </div>
                        </div>
                      </td>
                    </tr>
                  )
                })
              })()}
              {/* 카테고리 */}
              <tr style={{ borderBottom: '1px solid #1E1E1E' }}>
                <td style={tdLabel}>카테고리</td>
                <td style={tdVal}>
                  <span style={{ fontSize: '0.8rem', color: '#C5C5C5' }}>{p.category || '-'}</span>
                </td>
              </tr>
              {/* 상품정보 */}
              <tr style={{ borderBottom: '1px solid #1E1E1E' }}>
                <td style={{ ...tdLabel, verticalAlign: 'top', paddingTop: '10px' }}>상품정보</td>
                <td style={tdVal}>
                  {(() => {
                    const editableFields: { key: keyof SambaCollectedProduct; label: string }[] = [
                      { key: 'brand', label: '브랜드' },
                      { key: 'manufacturer', label: '제조사' },
                      { key: 'style_code', label: '품번' },
                      { key: 'origin', label: '제조국' },
                      { key: 'sex', label: '성별' },
                      { key: 'season', label: '시즌' },
                      { key: 'color', label: '색상' },
                      { key: 'material', label: '재질' },
                    ]
                    const readonlyFields = [
                      p.quality_guarantee && ['품질보증', p.quality_guarantee],
                      p.care_instructions && ['취급주의', p.care_instructions],
                    ].filter(Boolean) as [string, string][]
                    const inputStyle = { background: '#1A1A1A', border: '1px solid #333', color: '#C5C5C5', fontSize: '0.75rem', padding: '2px 6px', borderRadius: '3px', width: '140px', outline: 'none' }
                    return (
                      <div style={{ display: 'flex', flexDirection: 'column', gap: '3px', fontSize: '0.78rem' }}>
                        {editableFields.map(({ key, label }) => (
                          <span key={key} style={{ color: '#888', display: 'flex', alignItems: 'center', gap: '4px' }}>
                            {label}
                            <input
                              defaultValue={(p[key] as string) || ''}
                              style={inputStyle}
                              onBlur={(e) => {
                                const val = e.target.value.trim()
                                if (val !== ((p[key] as string) || '')) {
                                  collectorApi.updateProduct(p.id, { [key]: val } as Partial<SambaCollectedProduct>).then(() => {
                                    onProductUpdate(p.id, { [key]: val } as Partial<SambaCollectedProduct>)
                                    e.target.style.borderColor = '#51CF66'
                                    setTimeout(() => { e.target.style.borderColor = '#333' }, 1500)
                                  }).catch(() => {
                                    e.target.style.borderColor = '#FF6B6B'
                                    setTimeout(() => { e.target.style.borderColor = '#333' }, 1500)
                                  })
                                }
                              }}
                              onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur() }}
                            />
                          </span>
                        ))}
                        {readonlyFields.map(([label, val], i) => (
                          <span key={i} style={{ color: '#888' }}>{label} <span style={{ color: '#555', fontSize: '0.72rem' }}>{String(val).slice(0, 40)}{String(val).length > 40 ? '...' : ''}</span></span>
                        ))}
                      </div>
                    )
                  })()}
                </td>
              </tr>
              {/* 검색그룹 */}
              <tr style={{ borderBottom: '1px solid #1E1E1E' }}>
                <td style={tdLabel}>검색그룹</td>
                <td style={tdVal}>
                  {p.search_filter_id ? (
                    <span style={{ background: 'rgba(255,140,0,0.08)', border: '1px solid rgba(255,140,0,0.25)', color: 'rgba(255,180,100,0.85)', fontSize: '0.72rem', padding: '1px 8px', borderRadius: '10px' }}>
                      {filterNameMap[p.search_filter_id] || p.source_site || '삭제된 그룹'}
                    </span>
                  ) : <span style={{ color: '#444', fontSize: '0.75rem' }}>-</span>}
                </td>
              </tr>
              {/* 태그 */}
              <tr style={{ borderBottom: '1px solid #1E1E1E' }}>
                <td style={tdLabel}>태그</td>
                <td style={tdVal}>
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: '4px', alignItems: 'center' }}>
                    {(p.tags || []).filter(t => !t.startsWith('__')).map((tag, ti) => (
                      <span key={ti} style={{
                        fontSize: '0.7rem', padding: '1px 8px', borderRadius: '10px',
                        background: 'rgba(100,100,255,0.1)', border: '1px solid rgba(100,100,255,0.25)', color: '#8B8FD4',
                        display: 'inline-flex', alignItems: 'center', gap: '4px',
                      }}>
                        {tag}
                        <span
                          style={{ cursor: 'pointer', color: '#666', fontSize: '0.8rem', lineHeight: 1 }}
                          onClick={() => {
                            const newTags = (p.tags || []).filter(t => t !== tag)
                            onTagUpdate(p.id, newTags)
                          }}
                        >×</span>
                      </span>
                    ))}
                    <input
                      type="text"
                      placeholder="태그는 ','로 구분입력"
                      style={{ fontSize: '0.7rem', padding: '2px 7px', border: '1px solid #2D2D2D', borderRadius: '4px', color: '#C5C5C5', background: '#1A1A1A', outline: 'none', width: '160px' }}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') {
                          const input = e.currentTarget
                          const val = input.value.trim()
                          if (!val) return
                          const newTags = val.split(',').map(t => t.trim()).filter(Boolean)
                          const merged = [...new Set([...(p.tags || []), ...newTags])]
                          onTagUpdate(p.id, merged)
                          input.value = ''
                        }
                      }}
                    />
                    <button
                      style={{ fontSize: '0.68rem', padding: '2px 7px', border: '1px solid rgba(100,100,255,0.3)', borderRadius: '4px', color: '#8B8FD4', background: 'rgba(100,100,255,0.08)', cursor: 'pointer', whiteSpace: 'nowrap' }}
                      onClick={() => {
                        const input = document.querySelector<HTMLInputElement>(`input[placeholder="태그는 ','로 구분입력"]`)
                        if (!input || !input.value.trim()) return
                        const newTags = input.value.trim().split(',').map(t => t.trim()).filter(Boolean)
                        const merged = [...new Set([...(p.tags || []), ...newTags])]
                        onTagUpdate(p.id, merged)
                        input.value = ''
                      }}
                    >추가</button>
                    {(p.tags || []).includes('__ai_tagged__') && (
                      <span style={{ fontSize: '0.62rem', padding: '1px 6px', background: 'rgba(255,140,0,0.12)', border: '1px solid rgba(255,140,0,0.3)', borderRadius: '3px', color: '#FF8C00', fontWeight: 600, whiteSpace: 'nowrap' }}>AI</span>
                    )}
                  </div>
                </td>
              </tr>
              {/* 적용정책 */}
              <tr style={{ borderBottom: '1px solid #1E1E1E' }}>
                <td style={tdLabel}>적용정책</td>
                <td style={tdVal}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '0.375rem' }}>
                    <select
                      value={p.applied_policy_id || ''}
                      onChange={(e) => onPolicyChange(p.id, e.target.value)}
                      style={{
                        background: 'rgba(22,22,22,0.9)', border: '1px solid #2D2D2D',
                        color: '#C5C5C5', borderRadius: '4px', padding: '2px 6px',
                        fontSize: '0.75rem', outline: 'none',
                      }}
                    >
                      <option value="">정책 선택</option>
                      {policies.map((pol) => (
                        <option key={pol.id} value={pol.id}>{pol.name}</option>
                      ))}
                    </select>
                    {p.applied_policy_id && (
                      <button
                        onClick={() => window.location.href = `/samba/policies?highlight=${p.applied_policy_id}`}
                        style={{
                          background: 'none', border: '1px solid #2D2D2D', borderRadius: '4px',
                          color: '#888', fontSize: '0.625rem', padding: '2px 6px',
                          cursor: 'pointer', whiteSpace: 'nowrap',
                        }}
                        onMouseEnter={e => { e.currentTarget.style.color = '#FF8C00'; e.currentTarget.style.borderColor = 'rgba(255,140,0,0.4)' }}
                        onMouseLeave={e => { e.currentTarget.style.color = '#888'; e.currentTarget.style.borderColor = '#2D2D2D' }}
                        title="정책 페이지로 이동"
                      >이동</button>
                    )}
                  </div>
                </td>
              </tr>
              {/* Options (메인) */}
              <tr style={{ borderBottom: '1px solid #1E1E1E' }}>
                <td style={tdLabel}>
                  옵션
                  {p.option_group_names && p.option_group_names.length > 0 && (
                    <div style={{ color: '#666', fontSize: '0.65rem', marginTop: '2px' }}>
                      {p.option_group_names.join(' / ')}
                    </div>
                  )}
                </td>
                <td style={tdVal}>
                  {p.options && p.options.length > 0 ? (
                    <OptionPanel
                      options={p.options}
                      productCost={cost}
                      productId={p.id}
                      sourceSite={p.source_site}
                      nameRule={nameRules.find(r => r.id === (policy?.extras as Record<string, string> | undefined)?.name_rule_id)}
                    />
                  ) : (
                    <span style={{ color: '#444', fontSize: '0.75rem' }}>※ 옵션 미설정 -- 단일상품</span>
                  )}
                </td>
              </tr>
              {/* Addon Options (추가구성상품) */}
              {p.addon_options && p.addon_options.length > 0 && (
                <tr style={{ borderBottom: '1px solid #1E1E1E' }}>
                  <td style={tdLabel}>
                    추가옵션
                    <div style={{ color: '#666', fontSize: '0.65rem', marginTop: '2px' }}>
                      {p.addon_options[0]?.group || ''}
                    </div>
                  </td>
                  <td style={tdVal}>
                    <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.72rem' }}>
                      <thead>
                        <tr style={{ color: '#666', textAlign: 'left' }}>
                          <th style={{ padding: '4px 6px', fontWeight: 'normal' }}>이름</th>
                          <th style={{ padding: '4px 6px', fontWeight: 'normal', textAlign: 'right' }}>추가금액</th>
                          <th style={{ padding: '4px 6px', fontWeight: 'normal', textAlign: 'right' }}>재고</th>
                          <th style={{ padding: '4px 6px', fontWeight: 'normal' }}>필수</th>
                        </tr>
                      </thead>
                      <tbody>
                        {p.addon_options.map((ao, idx) => {
                          const noneChoice = ao.is_none_choice || ao.name.includes('선택안함') || ao.name.includes('선택없음')
                          const rowColor = noneChoice ? '#666' : undefined
                          return (
                            <tr key={`${ao.no ?? idx}-${ao.name}`} style={{ borderTop: '1px solid #1E1E1E', color: rowColor }}>
                              <td style={{ padding: '4px 6px' }}>{ao.name}</td>
                              <td style={{ padding: '4px 6px', textAlign: 'right' }}>{noneChoice ? '-' : `+${(ao.add_price ?? 0).toLocaleString()}원`}</td>
                              <td style={{ padding: '4px 6px', textAlign: 'right' }}>{noneChoice ? '-' : (ao.stock ?? 0).toLocaleString()}</td>
                              <td style={{ padding: '4px 6px' }}>{ao.is_required ? 'Y' : 'N'}</td>
                            </tr>
                          )
                        })}
                      </tbody>
                    </table>
                  </td>
                </tr>
              )}
              {/* Market ON/OFF switches */}
              <tr>
                <td style={tdLabel}>ON-OFF</td>
                <td style={tdVal}>
                  <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center' }}>
                    {(() => {
                      // 등록된 마켓 타입만 ON-OFF 토글 표시
                      const regMarketTypes = new Set(registeredMarkets.map(rm => rm.marketId))
                      const visibleMarkets = MARKETS.filter(m => regMarketTypes.has(m.id))
                      if (visibleMarkets.length === 0) return <span style={{ color: '#555', fontSize: '0.72rem' }}>등록된 마켓이 없습니다</span>
                      return visibleMarkets.map((m) => {
                        const on = marketEnabled[m.id] !== false
                        return (
                          <span key={m.id} style={{ display: 'inline-flex', alignItems: 'center', gap: '4px', marginRight: '10px', marginBottom: '2px' }}>
                            <button
                              onClick={() => onToggleMarket(p.id, m.id)}
                              style={{
                                width: '32px', height: '18px', borderRadius: '9px',
                                border: 'none', cursor: 'pointer', position: 'relative',
                                background: on ? '#FF8C00' : '#333', transition: 'background 0.2s',
                                padding: 0,
                              }}
                            >
                              <span style={{
                                position: 'absolute', top: '2px',
                                left: on ? '14px' : '2px',
                                width: '14px', height: '14px', borderRadius: '50%',
                                background: '#fff', transition: 'left 0.2s',
                              }} />
                            </button>
                            <span style={{ fontSize: '0.7rem', color: on ? '#C5C5C5' : '#555' }}>{m.name}</span>
                          </span>
                        )
                      })
                    })()}
                  </div>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
      )}
      {/* 카드 알림 모달 */}

      {cardAlert && (
        <div style={{ position: 'fixed', inset: 0, zIndex: 999999, background: 'rgba(0,0,0,0.6)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}
          onClick={() => setCardAlert(null)}>
          <div style={{ background: '#1A1A1A', border: `1px solid ${cardAlert.type === 'error' ? 'rgba(255,107,107,0.4)' : 'rgba(34,197,94,0.4)'}`, borderRadius: '12px', padding: '24px 32px', minWidth: '320px', textAlign: 'center' }}
            onClick={e => e.stopPropagation()}>
            <p style={{ margin: '0 0 16px', color: '#E5E5E5', fontSize: '0.9rem' }}>{cardAlert.msg}</p>
            <button onClick={() => setCardAlert(null)}
              style={{ padding: '6px 24px', fontSize: '0.85rem', borderRadius: '6px', cursor: 'pointer', border: '1px solid #3D3D3D', background: 'rgba(50,50,50,0.6)', color: '#E5E5E5' }}>확인</button>
          </div>
        </div>
      )}
      {/* 카드 확인 모달 */}
      {cardConfirm && (
        <div style={{ position: 'fixed', inset: 0, zIndex: 999999, background: 'rgba(0,0,0,0.6)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}
          onClick={() => setCardConfirm(null)}>
          <div style={{ background: '#1A1A1A', border: '1px solid #2D2D2D', borderRadius: '12px', padding: '24px 32px', minWidth: '320px', textAlign: 'center' }}
            onClick={e => e.stopPropagation()}>
            <p style={{ margin: '0 0 20px', color: '#E5E5E5', fontSize: '0.9rem' }}>{cardConfirm.msg}</p>
            <div style={{ display: 'flex', justifyContent: 'center', gap: '10px' }}>
              <button onClick={() => setCardConfirm(null)}
                style={{ padding: '6px 24px', fontSize: '0.85rem', borderRadius: '6px', cursor: 'pointer', border: '1px solid #3D3D3D', background: 'transparent', color: '#888' }}>취소</button>
              <button onClick={cardConfirm.onOk}
                style={{ padding: '6px 24px', fontSize: '0.85rem', borderRadius: '6px', cursor: 'pointer', border: '1px solid rgba(168,85,247,0.5)', background: 'rgba(168,85,247,0.15)', color: '#A855F7', fontWeight: 600 }}>확인</button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
})

export default ProductCard
