export const STATUS_MAP: Record<string, { label: string; bg: string; text: string }> = {
  pending: { label: '주문접수', bg: 'rgba(255,211,61,0.15)', text: '#FFD93D' },
  preparing: { label: '상품준비중', bg: 'rgba(135,206,250,0.15)', text: '#87CEFA' },
  wait_ship: { label: '배송대기중', bg: 'rgba(100,149,237,0.15)', text: '#6495ED' },
  arrived: { label: '상품도착', bg: 'rgba(72,209,204,0.15)', text: '#48D1CC' },
  ship_failed: { label: '송장전송실패', bg: 'rgba(255,50,50,0.2)', text: '#FF3232' },
  shipping: { label: '국내배송중', bg: 'rgba(76,154,255,0.15)', text: '#4C9AFF' },
  delivered: { label: '배송완료', bg: 'rgba(81,207,102,0.15)', text: '#51CF66' },
  cancelling: { label: '취소중', bg: 'rgba(255,165,0,0.15)', text: '#FFA500' },
  returning: { label: '반품중', bg: 'rgba(200,100,200,0.15)', text: '#CC5DE8' },
  exchanging: { label: '교환중', bg: 'rgba(255,182,193,0.15)', text: '#FFB6C1' },
  cancel_requested: { label: '취소요청', bg: 'rgba(255,80,80,0.2)', text: '#FF5050' },
  return_requested: { label: '반품요청', bg: 'rgba(200,100,200,0.2)', text: '#CC5DE8' },
  cancelled: { label: '취소완료', bg: 'rgba(255,107,107,0.15)', text: '#FF6B6B' },
  returned: { label: '반품완료', bg: 'rgba(180,80,180,0.15)', text: '#B44EB4' },
  exchanged: { label: '교환완료', bg: 'rgba(144,238,144,0.15)', text: '#90EE90' },
  return_completed: { label: '회수확정', bg: 'rgba(160,60,160,0.15)', text: '#A03CA0' },
  undeliverable: { label: '발송불가', bg: 'rgba(217,119,6,0.15)', text: '#F59E0B' },
}

export const SHIPPING_COMPANIES = ['CJ대한통운', '우진택배', '롯데택배', '로젠택배', '우체국택배', '경동택배', '한진택배', '일양로지스', '천일택배', '시리박스', '딜리박스', 'DHL', '직접배송', '기타']

export const ACTION_BUTTONS = [
  { key: 'no_price', label: '가격X', activeColor: '#DC2626' },
  { key: 'no_stock', label: '재고X', activeColor: '#CA8A04' },
  { key: 'direct', label: '직배', activeColor: '#2563EB' },
  { key: 'kkadaegi', label: '까대기', activeColor: '#D97706' },
  { key: 'gift', label: '선물', activeColor: '#059669' },
  { key: 'staff_a', label: '직원A', activeColor: '#7C3AED' },
  { key: 'staff_b', label: '직원B', activeColor: '#DB2777' },
] as const
