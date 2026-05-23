export const SITE_COLORS: Record<string, string> = {
  MUSINSA: '#4C9AFF',
  KREAM: '#51CF66',
  DANAWA: '#FF922B',
  FashionPlus: '#CC5DE8',
  Nike: '#FF6B6B',
  Adidas: '#FFD93D',
  ABCmart: '#FF8C00',
  REXMONDE: '#F06595',
  SSG: '#FF5A2E',
  LOTTEON: '#E10044',
  GSShop: '#6B5CE7',
  ElandMall: '#4ECDC4',
  SSF: '#845EF7',
  NAVERSTORE: '#03C75A',
  SNKRDUNK: '#1A1A1A',
}

export const PERIOD_BUTTONS = [
  { key: 'thismonth', label: '이번달' },
  { key: 'lastweek', label: '지난주' },
  { key: 'thisweek', label: '이번주' },
  { key: 'yesterday', label: '어제' },
  { key: 'today', label: '오늘' },
  { key: '1week', label: '일주일' },
  { key: '1month', label: '한달' },
  { key: 'thisyear', label: '올해' },
] as const

export const SOURCING_SEARCH_URLS: Record<string, string> = {
  MUSINSA: 'https://www.musinsa.com/search/musinsa/integration?q=',
  KREAM: 'https://kream.co.kr/search?keyword=',
  ABCmart: 'https://abcmart.a-rt.com/search?q=',
  LOTTEON: 'https://www.lotteon.com/csearch/search/search?render=search&platform=pc&mallId=2&q=',
  NAVERSTORE: 'https://search.shopping.naver.com/search/all?query=',
  SNKRDUNK: 'https://snkrdunk.com/en/search/result?keyword=',
}

export const DELIVERY_TRACKING_URLS: Record<string, string> = {
  'CJ대한통운': 'https://trace.cjlogistics.com/next/tracking.html?wblNo=',
  '한진택배': 'https://www.hanjin.com/kor/CMS/DeliveryMgr/WaybillResult.do?mession=&searchType=General&wblnumText2=',
  '롯데택배': 'https://www.lotteglogis.com/home/reservation/tracking/index?InvNo=',
  '로젠택배': 'https://www.ilogen.com/web/personal/trace/',
  '우체국택배': 'https://service.epost.go.kr/trace.RetrieveDomRi498.postal?sid1=',
  '경동택배': 'https://kdexp.com/deliverySearch?barcode=',
}

export const STORAGE_KEYS = {
  SAMBA_USER: 'samba_user',
  ANALYTICS_SEARCH: 'samba_analytics_search',
} as const
