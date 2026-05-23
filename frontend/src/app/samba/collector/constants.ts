// 소싱처 목록
export const SITES: { id: string; label: string; disabled?: boolean }[] = [
  // 활성 소싱처
  { id: 'MUSINSA', label: '무신사' },
  { id: 'KREAM', label: 'KREAM' },
  { id: 'FashionPlus', label: '패션플러스' },
  { id: 'Nike', label: 'Nike' },
  { id: 'ABCmart', label: 'ABC마트' },
  { id: 'SSG', label: '신세계몰' },
  { id: 'LOTTEON', label: '롯데ON' },
  { id: 'GSShop', label: 'GSShop' },
  { id: 'NAVERSTORE', label: '네이버스토어' },
  { id: 'SNKRDUNK', label: '스니덩크' },
  // 개발예정 (비활성)
  { id: 'DANAWA', label: '다나와', disabled: true },
  { id: 'Adidas', label: 'Adidas', disabled: true },
  { id: 'REXMONDE', label: '렉스몬드', disabled: true },
  { id: 'ElandMall', label: '이랜드몰', disabled: true },
  { id: 'SSF', label: 'SSF샵', disabled: true },
]

// 소싱처 옵션 타입
export interface SiteOption {
  id: string
  label: string
  warn?: string
}

// 품절상품 포함 옵션 (모든 소싱처 공통, 기본 체크해제)
const COMMON_OPTIONS: SiteOption[] = [
  { id: 'includeSoldOut', label: '품절상품 포함' },
]

export const SITE_OPTIONS: Record<string, SiteOption[]> = {
  MUSINSA: [
    { id: 'excludePreorder', label: '예약배송 수집제외' },
    { id: 'excludeBoutique', label: '부티끄 수집제외' },
    { id: 'maxDiscount', label: '최대혜택가' },
    ...COMMON_OPTIONS,
  ],
  KREAM: [...COMMON_OPTIONS],
  SNKRDUNK: [...COMMON_OPTIONS],
  FashionPlus: [...COMMON_OPTIONS],
  SSG: [
    { id: 'maxDiscount', label: '최대혜택가', warn: '수집 속도가 느려집니다' },
    ...COMMON_OPTIONS,
  ],
  LOTTEON: [
    { id: 'maxDiscount', label: '최대혜택가', warn: '수집 속도가 느려집니다' },
    ...COMMON_OPTIONS,
  ],
  ABCmart: [
    { id: 'maxDiscount', label: '최대혜택가', warn: '수집 속도가 느려집니다' },
    ...COMMON_OPTIONS,
  ],
  GSShop: [
    { id: 'maxDiscount', label: '최대혜택가', warn: '수집 속도가 느려집니다' },
    ...COMMON_OPTIONS,
  ],
}

// 매핑 대상 마켓 목록
// 카테고리 수 기준 정렬 (DB동기화 > 하드코딩 순)
export const MAPPING_MARKETS: { id: string; name: string }[] = [
  { id: 'smartstore', name: '스마트스토어' },  // 4964
  { id: 'coupang', name: '쿠팡' },            // 73
  { id: 'gmarket', name: '지마켓' },          // 45
  { id: 'kream', name: 'KREAM' },             // 39
  { id: 'auction', name: '옥션' },            // 36
  { id: '11st', name: '11번가' },             // 36
  { id: 'ssg', name: 'SSG' },                 // 35
  { id: 'lotteon', name: '롯데ON' },          // 30
  { id: 'gsshop', name: 'GSSHOP' },           // 29
  { id: 'hmall', name: 'HMALL' },             // 28
  { id: 'lottehome', name: '롯데홈쇼핑' },     // 24
  { id: 'homeand', name: '홈앤쇼핑' },         // 23
  { id: 'ebay', name: 'eBay' },               // 10
  { id: 'shopee', name: 'Shopee' },           // 8
  { id: 'lazada', name: 'Lazada' },           // 8
  { id: 'shopify', name: 'Shopify' },         // 8
  { id: 'playauto', name: '플레이오토' },       // 7
  { id: 'cafe24', name: '카페24' },            // 7
  { id: 'toss', name: '토스' },               // 6
  { id: 'amazon', name: '아마존' },            // 6
  { id: 'qoo10', name: 'Qoo10' },             // 6
  { id: 'rakuten', name: '라쿠텐' },           // 6
  { id: 'buyma', name: '바이마' },             // 6
  { id: 'zoom', name: 'Zum(줌)' },            // 6
  { id: 'poison', name: '포이즌' },            // 6
]
