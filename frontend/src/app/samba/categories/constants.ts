import { MARKET_LABELS, MARKETS } from '@/lib/samba/markets'

// AI 매핑 비용 추정 근거:
// Claude Sonnet 4 ($3/M input, $15/M output, 환율 ₩1,450)
// 1회 호출: ~1,500 input tokens × $3/M = $0.0045 = ₩6.5
//         + ~300 output tokens × $15/M = $0.0045 = ₩6.5
// 합계: ~₩13, 여유분 포함 ₩15
export const COST_PER_CALL_KRW = 15
export const COST_BASIS = 'Sonnet4 $3/M in + $15/M out × ₩1,450'

const ORDERED_ADJACENT_MARKETS = ['smartstore', 'lotteon', '11st']
const HIDDEN_CATEGORY_MARKETS = new Set(['gsshop', 'lottehome'])

// 카테고리 매핑 미지원 마켓 제외 (예: 무신사 — hasCategory: false)
const NO_CATEGORY_MARKETS = new Set(
  MARKETS.filter(m => m.hasCategory === false).map(m => m.id),
)

export const MARKET_KEYS = [
  ...ORDERED_ADJACENT_MARKETS.filter(mk => MARKET_LABELS[mk] && !NO_CATEGORY_MARKETS.has(mk) && !HIDDEN_CATEGORY_MARKETS.has(mk)),
  ...Object.keys(MARKET_LABELS).filter(
    mk => !ORDERED_ADJACENT_MARKETS.includes(mk) && !NO_CATEGORY_MARKETS.has(mk) && !HIDDEN_CATEGORY_MARKETS.has(mk),
  ),
]
