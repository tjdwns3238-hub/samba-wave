'use client'

import React, { Dispatch, SetStateAction } from 'react'
import {
  orderApi,
  collectorApi,
  type SambaOrder,
} from '@/lib/samba/api/commerce'
import { fetchWithAuth } from '@/lib/samba/api/shared'
import { showAlert, showConfirm } from '@/components/samba/Modal'
import { fmtNum } from '@/lib/samba/styles'
import { fmtDate, fmtTime } from '@/lib/samba/utils'
import { formatSourceSiteLabel } from '../utils/siteAlias'
import { hasActionTag } from '../utils/actionTag'

interface Props {
  o: SambaOrder
  refreshLog: Record<string, string>
  setRefreshLog: Dispatch<SetStateAction<Record<string, string>>>
  sentFlags: Record<string, { sms: boolean; kakao: boolean }>
  siteAliasMap: Record<string, string>
  actualSourceSite: string
  activeActions: Record<string, string | null>
  setPriceHistoryProduct: Dispatch<SetStateAction<{ name: string; source_site: string }>>
  setPriceHistoryData: Dispatch<SetStateAction<Record<string, unknown>[]>>
  setPriceHistoryModal: Dispatch<SetStateAction<boolean>>
  customerAddress: { base: string; detail: string }
  renderCopyableText: (
    value: string | null | undefined,
    _label?: string,
    style?: React.CSSProperties
  ) => React.ReactNode
  handleDelete: (id: string) => void | Promise<void>
  handleImageClick: (o: SambaOrder) => void
  handleCopyOrderNumber: (orderNumber: string) => void
  openMsgModal: (type: 'sms' | 'kakao', order: SambaOrder) => void
  handleDanawa: (productName: string) => void
  handleNaver: (productName: string) => void
  handleSourceLink: (o: SambaOrder) => void | Promise<void>
  handleMarketLink: (o: SambaOrder) => void | Promise<void>
  openUrlModal: (orderId: string) => void
  handleTracking: (order: SambaOrder) => void
  loadOrders: () => void | Promise<void>
}

export default function OrderInfoCell(props: Props) {
  const {
    o, refreshLog, setRefreshLog, sentFlags, siteAliasMap, actualSourceSite, activeActions,
    setPriceHistoryProduct, setPriceHistoryData, setPriceHistoryModal,
    customerAddress, renderCopyableText,
    handleDelete, handleImageClick, handleCopyOrderNumber, openMsgModal,
    handleDanawa, handleNaver, handleSourceLink, handleMarketLink,
    openUrlModal, handleTracking, loadOrders,
  } = props

  const handleCopyCustomerMemo = async () => {
    const text = (o.customer_note || '').trim()
    if (!text) {
      showAlert('복사할 메모가 없습니다', 'info')
      return
    }
    try {
      await navigator.clipboard.writeText(text)
    } catch {
      showAlert('메모 복사에 실패했습니다', 'error')
    }
  }

  // source_url 도메인 → 소싱처 코드 추론 (판매처와 무관, 가장 정확한 소스)
  const sourceFromUrl = (() => {
    const url = String(o.source_url || '').trim()
    if (!url) return ''
    const host = (() => {
      try { return new URL(url).hostname.toLowerCase() } catch { return url.toLowerCase() }
    })()
    if (host.includes('musinsa.com')) return 'MUSINSA'
    if (host.includes('kream.co.kr')) return 'KREAM'
    if (host.includes('fashionplus.co.kr')) return 'FashionPlus'
    if (host.includes('grandstage.a-rt.com')) return 'GrandStage'
    if (host.includes('abcmart.a-rt.com') || host.includes('abcmart.co.kr')) return 'ABCmart'
    if (host.includes('nike.com')) return 'Nike'
    if (host.includes('ssg.com')) return 'SSG'
    if (host.includes('lotteon.com')) return 'LOTTEON'
    if (host.includes('gsshop.com')) return 'GSShop'
    return ''
  })()
  // 두 배지는 완전 별개 차원 — 항상 함께 표시.
  // (1) 소싱처 배지: 어디서 가져온 상품 (MUSINSA, LOTTEON, SSG 등)
  //     우선순위 source_url 도메인 → collected_product.source_site → o.source_site(레거시 호환)
  // (2) 별칭 배지: 플레이오토 1 channel × 다 site_id 구조의 실제 판매처 별칭
  //     우선순위 o.sales_channel_alias(신규) → o.source_site 안의 괄호 형식(레거시 호환)
  const sourcingSite = String(sourceFromUrl || actualSourceSite || '').trim()
  const sourceBadgeLabel = sourcingSite
    ? (formatSourceSiteLabel(sourcingSite, siteAliasMap) || sourcingSite)
    : ''
  const aliasBadgeRaw = (() => {
    const fromNew = String(o.sales_channel_alias || '').trim()
    if (fromNew) return fromNew
    const raw = String(o.source_site || '').trim()
    return raw && raw.includes('(') ? raw : ''
  })()
  const extraSourceBadgeLabel = aliasBadgeRaw
    ? (formatSourceSiteLabel(aliasBadgeRaw, siteAliasMap) || aliasBadgeRaw)
    : ''

  return (
    <td style={{ padding: '0.75rem', borderRight: '1px solid #1C2333', fontSize: '0.8125rem', position: 'relative' }}>
      {/* 우측 상단: 주문일 + 수량 + 삭제 */}
      <div style={{ position: 'absolute', top: '0.75rem', right: '0.75rem', display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: '0.25rem' }}>
        {o.paid_at && (
          <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
            <span style={{ fontSize: '0.72rem', color: '#fff', fontWeight: 700 }}>{fmtDate(o.paid_at, '.')}</span>
          </div>
        )}
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
          <span style={{ fontSize: '0.72rem', color: '#555' }}>{fmtDate(o.created_at, '.')}</span>
          <button onClick={() => handleDelete(o.id)} style={{ padding: '0.125rem 0.5rem', fontSize: '0.7rem', background: '#8B1A1A', border: '1px solid #C0392B', color: '#fff', borderRadius: '4px', cursor: 'pointer' }}>삭제</button>
        </div>
        <span style={{ fontSize: '0.95rem', fontWeight: 700, color: o.quantity > 1 ? '#F5A623' : '#888' }}>수량: <span style={{ color: o.quantity > 1 ? '#F5A623' : '#888' }}>{fmtNum(o.quantity)}</span></span>
      </div>

      {/* 상품 이미지 (100x100) + 마켓/주문번호 */}
      <div style={{ display: 'flex', gap: '0.625rem', marginBottom: '0.5rem' }}>
        {o.product_image ? (
          <img
            src={o.product_image}
            alt=""
            onClick={() => handleImageClick(o)}
            style={{ width: '100px', height: '100px', objectFit: 'cover', borderRadius: '6px', border: '1px solid #2D2D2D', flexShrink: 0, cursor: 'pointer' }}
          />
        ) : (
          <div
            onClick={() => handleImageClick(o)}
            style={{ width: '100px', height: '100px', background: '#1A1A1A', borderRadius: '6px', border: '1px solid #2D2D2D', display: 'flex', alignItems: 'center', justifyContent: 'center', color: o.product_id?.startsWith('http') ? '#4C9AFF' : '#444', fontSize: '0.75rem', flexShrink: 0, cursor: o.product_id?.startsWith('http') ? 'pointer' : 'default', textDecoration: o.product_id?.startsWith('http') ? 'underline' : 'none' }}
          >{o.product_id?.startsWith('http') ? '링크이동' : 'No IMG'}</div>
        )}
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', marginBottom: '0.25rem', flexWrap: 'wrap' }}>
            {sourceBadgeLabel && <span style={{ fontSize: '0.75rem', color: '#B0B0B0', background: '#1A1A1A', padding: '0.125rem 0.5rem', borderRadius: '4px', border: '1px solid #2D2D2D', flexShrink: 0, whiteSpace: 'nowrap' }}>{sourceBadgeLabel}</span>}
            {extraSourceBadgeLabel && <span style={{ fontSize: '0.75rem', color: '#B0B0B0', background: '#1A1A1A', padding: '0.125rem 0.5rem', borderRadius: '4px', border: '1px solid #2D2D2D', flexShrink: 0, whiteSpace: 'nowrap' }}>{extraSourceBadgeLabel}</span>}
            <span style={{ fontSize: '0.75rem', color: '#B0B0B0', background: '#1A1A1A', padding: '0.125rem 0.5rem', borderRadius: '4px', minWidth: 0, maxWidth: '180px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flexShrink: 1 }}>{o.channel_name || '마켓'}</span>
            <button onClick={() => handleCopyOrderNumber(o.order_number)} style={{ fontSize: '0.7rem', padding: '0.125rem 0.5rem', background: 'transparent', border: '1px solid #2D2D2D', borderRadius: '4px', color: '#B0B0B0', cursor: 'pointer' }}>주문번호복사</button>
            <button onClick={() => openMsgModal('sms', o)} style={{ fontSize: '0.7rem', padding: '0.125rem 0.5rem', background: sentFlags[o.id]?.sms ? '#1F3A24' : 'transparent', border: `1px solid ${sentFlags[o.id]?.sms ? '#51CF66' : '#2D2D2D'}`, borderRadius: '4px', color: sentFlags[o.id]?.sms ? '#51CF66' : '#B0B0B0', cursor: 'pointer' }}>SMS</button>
            <button onClick={() => openMsgModal('kakao', o)} style={{ fontSize: '0.7rem', padding: '0.125rem 0.5rem', background: sentFlags[o.id]?.kakao ? '#3A320F' : 'transparent', border: `1px solid ${sentFlags[o.id]?.kakao ? '#FFD93D' : '#2D2D2D'}`, borderRadius: '4px', color: sentFlags[o.id]?.kakao ? '#FFD93D' : '#B0B0B0', cursor: 'pointer' }}>KAKAO</button>
          </div>
          <div style={{ display: 'flex', gap: '1rem', marginBottom: '0.25rem', fontSize: '0.75rem' }}>
            <div><span style={{ color: '#666' }}>상품주문번호 </span><span style={{ fontFamily: 'monospace', color: '#E5E5E5' }}>{o.order_number}</span></div>
            {o.shipment_id && (
              <div><span style={{ color: '#666' }}>주문번호 </span><span style={{ fontFamily: 'monospace', color: '#B0B0B0' }}>{o.shipment_id}</span></div>
            )}
          </div>
          <div style={{ minWidth: 0 }}>
            <span style={{ color: '#C5C5C5', fontSize: '0.8125rem', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', display: 'block' }}>{o.product_name || '-'}</span>
            {o.product_option && (
              <span style={{ color: '#FACC15', fontSize: '0.75rem', fontWeight: 700, display: 'block', marginTop: '0.125rem' }}>[옵션] {o.product_option}</span>
            )}
          </div>
        </div>
      </div>

      {/* 쿠팡노출 */}
      <div style={{ display: 'flex', alignItems: 'center', gap: '0.25rem', marginBottom: '0.375rem' }}>
        <span style={{ color: '#666', fontSize: '0.7rem', whiteSpace: 'nowrap' }}>쿠팡노출</span>
        <input
          type="text"
          placeholder="쿠팡노출상품명"
          defaultValue={o.coupang_display_name || ''}
          onBlur={async (e) => {
            const val = e.target.value.trim()
            if (val === (o.coupang_display_name ?? '')) return
            try {
              await orderApi.update(o.id, { coupang_display_name: val || undefined })
              loadOrders()
            } catch (err) { showAlert(err instanceof Error ? err.message : '저장 실패', 'error') }
          }}
          onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur() }}
          style={{ flex: 1, fontSize: '0.75rem', padding: '0.125rem 0.375rem', background: '#1A1A1A', border: '1px solid #444', color: '#E5E5E5', borderRadius: '4px', minWidth: 0 }}
        />
      </div>

      {/* 업데이트 로그 */}
      {refreshLog[o.id] && (
        <div style={{ fontSize: '0.72rem', color: '#8A95B0', padding: '0.25rem 0', marginBottom: '0.25rem', whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
          {refreshLog[o.id]}
        </div>
      )}
      {/* 버튼 */}
      <div style={{ display: 'flex', gap: '0.375rem', marginBottom: '0.5rem', flexWrap: 'wrap' }}>
        <button onClick={() => handleDanawa(o.product_name || '')} style={{ fontSize: '0.7rem', padding: '0.125rem 0.375rem', background: 'transparent', border: '1px solid #2D2D2D', borderRadius: '4px', color: '#B0B0B0', cursor: 'pointer' }}>다나와</button>
        <button onClick={() => handleNaver(o.product_name || '')} style={{ fontSize: '0.7rem', padding: '0.125rem 0.375rem', background: 'transparent', border: '1px solid #2D2D2D', borderRadius: '4px', color: '#B0B0B0', cursor: 'pointer' }}>네이버</button>
        <button onClick={async () => {
          if (o.collected_product_id) {
            window.open(`/samba/products?search=${encodeURIComponent(o.collected_product_id)}&search_type=id&highlight=${o.collected_product_id}`, '_blank')
            return
          }
          const _openAndLink = (cpId: string) => {
            window.open(`/samba/products?search=${encodeURIComponent(cpId)}&search_type=id&highlight=${cpId}`, '_blank')
            orderApi.linkProduct(o.id, cpId).catch(() => {})
          }
          if (o.product_id) {
            try {
              const res = await collectorApi.lookupByMarketNo(o.product_id)
              if (res.found && res.id) { _openAndLink(res.id); return }
            } catch { /* ignore */ }
          }
          const _spidMatch = (o.product_name || '').match(/\b(\d{6,})\s*$/)
          if (_spidMatch) {
            try {
              const res = await collectorApi.lookupByMarketNo(_spidMatch[1])
              if (res.found && res.id) { _openAndLink(res.id); return }
            } catch { /* ignore */ }
          }
          const _codeMatch = (o.product_name || '').match(/\b([A-Za-z]{1,5}\d{2,})[\s-]+(\d{2,4})\s*$/)
          if (_codeMatch) {
            try {
              const res = await collectorApi.lookupByMarketNo(`${_codeMatch[1]}${_codeMatch[2]}`)
              if (res.found && res.id) { _openAndLink(res.id); return }
            } catch { /* ignore */ }
          }
          if (o.product_name) {
            try {
              const _scrollRes = await collectorApi.scrollProducts({ search: o.product_name, search_type: 'name', limit: 1 })
              if (_scrollRes.items?.length > 0 && _scrollRes.total === 1) { _openAndLink(_scrollRes.items[0].id); return }
            } catch { /* ignore */ }
            window.open(`/samba/products?search=${encodeURIComponent(o.product_name)}`, '_blank')
          } else {
            showAlert('상품 정보가 없습니다', 'info')
          }
        }} style={{ fontSize: '0.7rem', padding: '0.125rem 0.375rem', background: 'transparent', border: '1px solid #2D2D2D', borderRadius: '4px', color: '#B0B0B0', cursor: 'pointer' }}>상품정보</button>
        <button onClick={async () => {
          if (!o.product_id) { showAlert('상품 정보가 없습니다', 'info'); return }
          try {
            const lookup = await collectorApi.lookupByMarketNo(o.product_id)
            if (!lookup.found || !lookup.id) { showAlert('수집상품을 찾을 수 없습니다', 'info'); return }
            setPriceHistoryProduct({ name: o.product_name || '', source_site: o.source_site || '' })
            setPriceHistoryData([])
            setPriceHistoryModal(true)
            const history = await collectorApi.getPriceHistory(lookup.id)
            setPriceHistoryData(history || [])
          } catch { showAlert('가격이력 조회 실패', 'error') }
        }} style={{ fontSize: '0.7rem', padding: '0.125rem 0.375rem', background: 'transparent', border: '1px solid #2D2D2D', borderRadius: '4px', color: '#B0B0B0', cursor: 'pointer' }}>가격변경이력</button>
        <button onClick={() => handleSourceLink(o)} style={{ fontSize: '0.6875rem', padding: '0.125rem 0.375rem', background: 'transparent', border: '1px solid #2D2D2D', borderRadius: '4px', color: '#B0B0B0', cursor: 'pointer' }}>원문링크</button>
        <button onClick={() => handleMarketLink(o)} style={{ fontSize: '0.6875rem', padding: '0.125rem 0.375rem', background: 'transparent', border: '1px solid #2D2D2D', borderRadius: '4px', color: '#B0B0B0', cursor: 'pointer' }}>판매링크</button>
        <button onClick={() => openUrlModal(o.id)} style={{ fontSize: '0.7rem', padding: '0.125rem 0.375rem', background: 'transparent', border: '1px solid #2D2D2D', borderRadius: '4px', color: '#B0B0B0', cursor: 'pointer' }}>미등록 입력</button>
        <button onClick={() => handleTracking(o)} style={{ fontSize: '0.7rem', padding: '0.125rem 0.375rem', background: 'transparent', border: '1px solid #2D2D2D', borderRadius: '4px', color: '#B0B0B0', cursor: 'pointer' }}>배송조회</button>
        <button onClick={async () => {
          const ts = fmtTime()
          setRefreshLog(prev => ({ ...prev, [o.id]: `[${ts}] 가격재고 갱신 중...` }))
          let cpId = ''
          if (o.collected_product_id) {
            cpId = o.collected_product_id
          } else if (o.product_id) {
            try {
              const lookup = await collectorApi.lookupByMarketNo(o.product_id)
              if (lookup.found && lookup.id) cpId = lookup.id
            } catch { /* ignore */ }
          }
          if (!cpId) {
            const idMatch = (o.product_name || '').match(/\b(\d{6,})\s*$/)
            if (idMatch) {
              try {
                const lookup = await collectorApi.lookupByMarketNo(idMatch[1])
                if (lookup.found && lookup.id) cpId = lookup.id
              } catch { /* ignore */ }
            }
          }
          if (!cpId) {
            setRefreshLog(prev => ({ ...prev, [o.id]: `[${ts}] 수집상품을 찾을 수 없습니다` }))
            return
          }
          try {
            const { API_BASE_URL: apiBase } = await import('@/config/api')
            const apiRes = await fetchWithAuth(`${apiBase}/api/v1/samba/collector/enrich/${cpId}`, { method: 'POST' })
            const data = await apiRes.json()
            const ts2 = fmtTime()
            if (apiRes.ok && data.success) {
              const p = data.product
              const costVal = p?.cost || p?.sale_price
              const priceStr = costVal != null ? `₩${fmtNum(Number(costVal))}` : '-'
              const stockStr = p?.sale_status === 'preorder' ? '판매예정' : p?.sale_status === 'sold_out' || p?.is_sold_out ? '품절' : '재고있음'
              const retransmitStr = data.retransmitted ? ` | 마켓 ${data.retransmit_accounts}계정 수정등록` : ''
              setRefreshLog(prev => ({ ...prev, [o.id]: `[${ts2}] ${priceStr} | ${stockStr}${retransmitStr}` }))
            } else {
              setRefreshLog(prev => ({ ...prev, [o.id]: `[${ts2}] 실패: ${data.detail || data.message || '갱신 실패'}` }))
            }
          } catch (e) {
            setRefreshLog(prev => ({ ...prev, [o.id]: `[${ts}] 갱신 실패: ${e instanceof Error ? e.message : ''}` }))
          }
        }} style={{ fontSize: '0.7rem', padding: '0.125rem 0.375rem', background: 'transparent', border: '1px solid #2D2D2D', borderRadius: '4px', color: '#B0B0B0', cursor: 'pointer' }}>업데이트</button>
        <button onClick={async () => {
          if (!await showConfirm('롯데ON 마켓에서 상품을 삭제(판매종료)하시겠습니까?\n이 작업은 되돌릴 수 없습니다.')) return
          try {
            const result = await orderApi.marketDelete(o.id)
            if (result.ok) {
              showAlert('마켓 상품 삭제 완료')
            } else {
              showAlert(result.message || '삭제 실패', 'error')
            }
          } catch (e) {
            showAlert(e instanceof Error ? e.message : '마켓 상품 삭제 실패', 'error')
          }
        }} style={{ fontSize: '0.7rem', padding: '0.125rem 0.375rem', background: 'transparent', border: '1px solid #2D2D2D', borderRadius: '4px', color: '#B0B0B0', cursor: 'pointer' }}>마켓상품삭제</button>
        <button onClick={() => {
          if (o.ext_order_number) { window.open(o.ext_order_number, '_blank'); return }
          const srcNo = o.sourcing_order_number || ''
          if (!srcNo) { showAlert('소싱 주문번호가 없습니다', 'info'); return }
          const isGift = hasActionTag(activeActions[o.id] ?? o.action_tag, 'gift')
          const sourceSiteRaw = (sourceFromUrl || actualSourceSite || o.source_site || '').trim()
          const sourceSiteCode = sourceSiteRaw.split('(')[0].trim() || sourceSiteRaw
          const orderUrlMap: Record<string, string> = {
            MUSINSA: `https://www.musinsa.com/order/order-detail/${srcNo}`,
            KREAM: `https://kream.co.kr/my/purchasing/${srcNo}`,
            FashionPlus: `https://www.fashionplus.co.kr/mypage/order/detail/${srcNo}`,
            ABCmart: `https://abcmart.a-rt.com/mypage/order/read-order-detail?orderNo=${srcNo}`,
            GrandStage: `https://grandstage.a-rt.com/mypage/order/read-order-detail?orderNo=${srcNo}`,
            Nike: `https://www.nike.com/kr/orders/${srcNo}`,
            SSG: `https://pay.ssg.com/myssg/orderInfoDetail.ssg?orordNo=${encodeURIComponent(srcNo)}&viewType=Ssg`,
            LOTTEON: `https://www.lotteon.com/p/order/claim/giftBoxDetail?odNo=${srcNo}&type=snd`,
          }
          const url = orderUrlMap[sourceSiteCode]
          if (!url) { showAlert(`${o.source_site || '알수없는'} 소싱처는 원주문링크를 지원하지 않습니다`, 'info'); return }
          window.open(url, '_blank')
        }} style={{ fontSize: '0.7rem', padding: '0.125rem 0.375rem', background: 'transparent', border: '1px solid #2D2D2D', borderRadius: '4px', color: '#B0B0B0', cursor: 'pointer' }}>원주문링크</button>
      </div>

      {/* 주문자/수령인/연락처/주소 한 줄 */}
      <div style={{ display: 'flex', gap: '0.75rem', fontSize: '0.8rem', flexWrap: 'wrap' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.25rem' }}>
          <span style={{ color: '#666' }}>주문자</span>
          {renderCopyableText(o.orderer_name || o.customer_name, '주문자')}
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.25rem' }}>
          <span style={{ color: '#666' }}>수령인</span>
          {renderCopyableText(o.customer_name, '수령인')}
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.25rem' }}>
          <span style={{ color: '#666' }}>연락처</span>
          {renderCopyableText(o.customer_phone, '연락처')}
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.25rem' }}>
          <span style={{ color: '#666' }}>주소</span>
          {renderCopyableText(customerAddress.base, '기본주소')}
          {customerAddress.detail && (
            <>
              <span style={{ color: '#555' }}>/</span>
              {renderCopyableText(customerAddress.detail, '상세주소')}
            </>
          )}
          {o.customer_postal_code && (
            // 우편번호 — 확인용만 표시. 복사 버튼 의도적 제외 (주소 복사 시 우편번호 제외)
            <span style={{ color: '#888', fontSize: '0.75rem', marginLeft: '0.25rem' }}>
              [{o.customer_postal_code}]
            </span>
          )}
        </div>
      </div>
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: '0.25rem', fontSize: '0.8rem', marginTop: '0.25rem', marginBottom: '0.25rem' }}>
          <span style={{ color: '#666', whiteSpace: 'nowrap' }}>고객메모</span>
          <span
            role={o.customer_note?.trim() ? 'button' : undefined}
            tabIndex={o.customer_note?.trim() ? 0 : undefined}
            title="클릭하여 복사"
            onClick={o.customer_note?.trim() ? handleCopyCustomerMemo : undefined}
            onKeyDown={(e) => {
              if (!o.customer_note?.trim()) return
              if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault()
                handleCopyCustomerMemo()
              }
            }}
            style={{
              color: o.customer_note?.trim() ? '#E5E5E5' : '#666',
              cursor: o.customer_note?.trim() ? 'copy' : 'default',
              textDecoration: o.customer_note?.trim() ? 'underline' : 'none',
              textDecorationColor: 'rgba(229, 229, 229, 0.35)',
              textUnderlineOffset: '2px',
              wordBreak: 'break-word',
            }}
          >
            {o.customer_note?.trim() || '-'}
          </span>
        </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: '0.25rem', fontSize: '0.8rem' }}>
        <span style={{ color: '#666', whiteSpace: 'nowrap' }}>타마켓주문링크</span>
        <input
          type="text"
          placeholder="타마켓 주문링크 URL"
          defaultValue={o.ext_order_number || ''}
          onBlur={async (e) => {
            const val = e.target.value.trim()
            if (val === (o.ext_order_number ?? '')) return
            try {
              await orderApi.update(o.id, { ext_order_number: val || undefined })
              loadOrders()
            } catch (err) { showAlert(err instanceof Error ? err.message : '저장 실패', 'error') }
          }}
          onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur() }}
          style={{ flex: 1, fontSize: '0.75rem', padding: '0.125rem 0.375rem', background: '#1A1A1A', border: '1px solid #444', color: '#E5E5E5', borderRadius: '4px', fontFamily: 'monospace', minWidth: 0 }}
        />
      </div>
    </td>
  )
}
