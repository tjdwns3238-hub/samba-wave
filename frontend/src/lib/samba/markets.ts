/**
 * 마켓 마스터 설정 — 단일 소스 (Single Source of Truth)
 *
 * 새 마켓 추가 시 이 파일만 수정하면 설정·정책·카테고리·전송·CS 등
 * 모든 페이지에 자동 반영됩니다.
 */

// ── 마켓 정의 ──
export interface MarketDef {
  id: string
  label: string
  group: 'domestic' | 'domestic_home' | 'domestic_fashion' | 'solution' | 'overseas' | 'overseas_fashion'
  /** 카테고리 매핑 대상 여부 (false면 카테고리 매핑에서 제외) */
  hasCategory?: boolean
  /** true면 카테고리 트리 전용 가상 마켓 — 설정/계정/정책 선택 목록에서 제외 */
  categoryOnly?: boolean
}

export const MARKETS: MarketDef[] = [
  // 국내 오픈마켓
  { id: 'gmarket', label: 'G마켓', group: 'domestic', hasCategory: true },
  { id: 'auction', label: '옥션', group: 'domestic', hasCategory: true },
  { id: 'smartstore', label: '스마트스토어', group: 'domestic', hasCategory: true },
  { id: 'coupang', label: '쿠팡', group: 'domestic', hasCategory: true },
  { id: '11st', label: '11번가', group: 'domestic', hasCategory: true },
  { id: 'ssg', label: '신세계몰(전시)', group: 'domestic', hasCategory: true },
  { id: 'ssg_std', label: '신세계몰(표준)', group: 'domestic', hasCategory: true, categoryOnly: true },
  { id: 'lotteon', label: '롯데ON', group: 'domestic', hasCategory: true },
  { id: 'toss', label: '토스', group: 'domestic', hasCategory: true },
  // 국내 홈쇼핑/종합몰
  { id: 'gsshop', label: 'GS샵', group: 'domestic_home', hasCategory: true },
  { id: 'lottehome', label: '롯데홈쇼핑', group: 'domestic_home', hasCategory: true },
  { id: 'homeand', label: '홈앤쇼핑', group: 'domestic_home', hasCategory: true },
  { id: 'hmall', label: 'HMALL', group: 'domestic_home', hasCategory: true },
  { id: 'ktalpha', label: 'KT알파쇼핑', group: 'domestic_home', hasCategory: true },
  // 국내 패션/리셀
  { id: 'musinsa', label: '무신사', group: 'domestic_fashion', hasCategory: false },
  { id: 'kream', label: 'KREAM', group: 'domestic_fashion', hasCategory: true },
  // 종합솔루션
  { id: 'playauto', label: '플레이오토', group: 'solution', hasCategory: false },
  { id: 'cafe24', label: '카페24', group: 'solution', hasCategory: true },
  // 해외 마켓
  { id: 'amazon', label: '아마존', group: 'overseas', hasCategory: true },
  { id: 'ebay', label: 'eBay', group: 'overseas', hasCategory: true },
  { id: 'rakuten', label: '라쿠텐', group: 'overseas', hasCategory: true },
  { id: 'qoo10', label: 'Qoo10', group: 'overseas', hasCategory: true },
  { id: 'quten', label: '큐텐', group: 'overseas', hasCategory: true },
  { id: 'lazada', label: 'Lazada', group: 'overseas', hasCategory: true },
  { id: 'shopee', label: 'Shopee', group: 'overseas', hasCategory: true },
  { id: 'buyma', label: '바이마', group: 'overseas', hasCategory: true },
  { id: 'shopify', label: 'Shopify', group: 'overseas', hasCategory: true },
  { id: 'zoom', label: 'Zum(줌)', group: 'overseas', hasCategory: true },
  // 해외 패션/리셀
  { id: 'poison', label: '포이즌', group: 'overseas_fashion', hasCategory: true },
]

// ── 파생 데이터 (자동 생성) ──

/** id → 한글 라벨 */
export const MARKET_LABELS: Record<string, string> = Object.fromEntries(
  MARKETS.map(m => [m.id, m.label])
)

/** 한글 라벨 → id */
export const MARKET_ID_BY_LABEL: Record<string, string> = Object.fromEntries(
  MARKETS.map(m => [m.label, m.id])
)

/** 설정 페이지용 셀렉트 옵션 (categoryOnly 마켓 제외) */
export const MARKET_SELECT_OPTIONS = [
  { value: '', label: '── 국내 오픈마켓 ──', disabled: true },
  ...MARKETS.filter(m => m.group === 'domestic' && !m.categoryOnly).map(m => ({ value: m.id, label: m.label })),
  { value: '', label: '── 국내 홈쇼핑/종합몰 ──', disabled: true },
  ...MARKETS.filter(m => m.group === 'domestic_home' && !m.categoryOnly).map(m => ({ value: m.id, label: m.label })),
  { value: '', label: '── 국내 패션/리셀 ──', disabled: true },
  ...MARKETS.filter(m => m.group === 'domestic_fashion' && !m.categoryOnly).map(m => ({ value: m.id, label: m.label })),
  { value: '', label: '── 국내 종합솔루션 ──', disabled: true },
  ...MARKETS.filter(m => m.group === 'solution' && !m.categoryOnly).map(m => ({ value: m.id, label: m.label })),
  { value: '', label: '── 해외 마켓 ──', disabled: true },
  ...MARKETS.filter(m => m.group === 'overseas' && !m.categoryOnly).map(m => ({ value: m.id, label: m.label })),
  { value: '', label: '── 해외 패션/리셀 ──', disabled: true },
  ...MARKETS.filter(m => m.group === 'overseas_fashion' && !m.categoryOnly).map(m => ({ value: m.id, label: m.label })),
] as const

// ── 그룹별 목록 ──

const DOMESTIC_GROUPS = new Set(['domestic', 'domestic_home', 'domestic_fashion'])
const OVERSEAS_GROUPS = new Set(['overseas', 'overseas_fashion'])

/** 국내 마켓 라벨 목록 (정책 탭용, categoryOnly 제외) */
export const POLICY_MARKETS_DOMESTIC: string[] = MARKETS
  .filter(m => (DOMESTIC_GROUPS.has(m.group) || m.group === 'solution') && !m.categoryOnly)
  .map(m => m.label)

/** 해외 마켓 라벨 목록 (정책 탭용) */
export const POLICY_MARKETS_OVERSEAS: string[] = MARKETS
  .filter(m => OVERSEAS_GROUPS.has(m.group))
  .map(m => m.label)

/**
 * 카테고리 동기화 시 연관 마켓 확장
 * ssg 선택 시 ssg_std(표준카테고리)도 함께 동기화
 */
export function expandSyncMarkets(marketId: string): string[] {
  if (marketId === 'ssg') return ['ssg', 'ssg_std']
  return [marketId]
}
