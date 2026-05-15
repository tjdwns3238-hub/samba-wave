'use client'

import React, { Dispatch, SetStateAction, useState } from 'react'
import {
  orderApi,
  type SambaOrder,
} from '@/lib/samba/api/commerce'
import { type SambaSourcingAccount } from '@/lib/samba/api/operations'
import { showAlert, showConfirm } from '@/components/samba/Modal'
import { inputStyle, fmtNum } from '@/lib/samba/styles'
import { fmtTime } from '@/lib/samba/utils'
import { STATUS_MAP, SHIPPING_COMPANIES, ACTION_BUTTONS } from '../constants'
import { parseActionTags } from '../utils/actionTag'
import OrderInfoCell from './OrderInfoCell'

// Props 타입 정의
interface OrdersTableProps {
  // 데이터
  loading: boolean
  filteredOrders: SambaOrder[]
  currentPage: number
  pageSize: number
  currentPageIds: string[]
  selectedIds: Set<string>
  setSelectedIds: Dispatch<SetStateAction<Set<string>>>
  toggleSelectAll: () => void

  // 인라인 편집 상태
  editingCosts: Record<string, string>
  setEditingCosts: Dispatch<SetStateAction<Record<string, string>>>
  editingShipFees: Record<string, string>
  setEditingShipFees: Dispatch<SetStateAction<Record<string, string>>>
  editingTrackings: Record<string, string>
  setEditingTrackings: Dispatch<SetStateAction<Record<string, string>>>
  editingOrderNumbers: Record<string, string>
  setEditingOrderNumbers: Dispatch<SetStateAction<Record<string, string>>>
  activeActions: Record<string, string | null>
  collectedProductCosts: Record<string, number>
  collectedProductSourceSites: Record<string, string>

  // 부가 상태
  refreshLog: Record<string, string>
  setRefreshLog: Dispatch<SetStateAction<Record<string, string>>>
  sentFlags: Record<string, { sms: boolean; kakao: boolean }>
  siteAliasMap: Record<string, string>
  sourcingAccounts: SambaSourcingAccount[]

  // 가격이력 모달 setter
  setPriceHistoryProduct: Dispatch<SetStateAction<{ name: string; source_site: string }>>
  setPriceHistoryData: Dispatch<SetStateAction<Record<string, unknown>[]>>
  setPriceHistoryModal: Dispatch<SetStateAction<boolean>>

  // 로그 setter
  setLogMessages: Dispatch<SetStateAction<string[]>>

  // 헬퍼/핸들러
  calcProfit: (o: SambaOrder) => number
  calcProfitRate: (o: SambaOrder) => string
  calcFeeRate: (o: SambaOrder) => string
  splitCustomerAddress: (
    address: string | null | undefined,
    detailColumn?: string | null,
  ) => { base: string; detail: string }
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
  patchOrder: (id: string, patch: Partial<SambaOrder>) => void
  handleStatusChange: (id: string, status: string) => void | Promise<void>
  handleCostSave: (id: string) => void | Promise<void>
  handleShipFeeSave: (id: string) => void | Promise<void>
  toggleAction: (orderId: string, actionKey: string) => void | Promise<void>
}

export default function OrdersTable(props: OrdersTableProps) {
  const {
    loading, filteredOrders, currentPage, pageSize,
    currentPageIds, selectedIds, setSelectedIds, toggleSelectAll,
    editingCosts, setEditingCosts,
    editingShipFees, setEditingShipFees,
    editingTrackings, setEditingTrackings,
    editingOrderNumbers, setEditingOrderNumbers,
    activeActions,
    collectedProductCosts,
    collectedProductSourceSites,
    refreshLog, setRefreshLog,
    sentFlags, siteAliasMap, sourcingAccounts,
    setPriceHistoryProduct, setPriceHistoryData, setPriceHistoryModal,
    setLogMessages,
    calcProfit, calcProfitRate, calcFeeRate, splitCustomerAddress,
    renderCopyableText,
    handleDelete, handleImageClick, handleCopyOrderNumber, openMsgModal,
    handleDanawa, handleNaver, handleSourceLink, handleMarketLink,
    openUrlModal, handleTracking, loadOrders, patchOrder,
    handleStatusChange, handleCostSave, handleShipFeeSave, toggleAction,
  } = props
  const [editingNotes, setEditingNotes] = useState<Record<string, string>>({})

  return (
    <div style={{ border: '1px solid #2D2D2D', borderRadius: '8px', overflowX: 'auto' }}>
      <table style={{ width: '100%', minWidth: '1100px', borderCollapse: 'collapse' }}>
        <thead>
          <tr style={{ background: '#0D1117', borderBottom: '2px solid #1C2333' }}>
            <th style={{ width: '36px', padding: '0.5rem', textAlign: 'center', borderRight: '1px solid #1C2333' }}>
              <input type="checkbox" checked={currentPageIds.length > 0 && currentPageIds.every(id => selectedIds.has(id))} onChange={toggleSelectAll} style={{ accentColor: '#F59E0B', width: '13px', height: '13px', cursor: 'pointer' }} />
            </th>
            <th style={{ padding: '0.6rem 0.75rem', textAlign: 'center', fontSize: '0.75rem', fontWeight: 600, color: '#94A3B8', borderRight: '1px solid #1C2333' }}>주문정보</th>
            <th style={{ padding: '0.6rem 0.75rem', textAlign: 'center', fontSize: '0.75rem', fontWeight: 600, color: '#94A3B8', borderRight: '1px solid #1C2333', width: '143px' }}>금액</th>
            <th style={{ padding: '0.6rem 0.75rem', textAlign: 'center', fontSize: '0.75rem', fontWeight: 600, color: '#94A3B8', width: '460px' }}>주문상태</th>
          </tr>
        </thead>
        <tbody>
          {loading ? (
            <tr><td colSpan={4} style={{ padding: '3rem', textAlign: 'center', color: '#555' }}>로딩 중...</td></tr>
          ) : filteredOrders.length === 0 ? (
            <tr><td colSpan={4} style={{ padding: '3rem', textAlign: 'center', color: '#555' }}>주문이 없습니다</td></tr>
          ) : filteredOrders.map((o, index) => {
            // 편집 중에는 사용자 입력을 그대로 표시 (콤마 자동삽입으로 인한 커서 꼬임/계산식 깨짐 방지)
            // Blur 후 editingCosts에서 제거되면 저장값(o.cost)에 콤마 포맷 적용
            const costDisplay = editingCosts[o.id] !== undefined ? editingCosts[o.id] : (o.cost ? fmtNum(o.cost) : '')
            const shipFeeDisplay = editingShipFees[o.id] !== undefined ? editingShipFees[o.id] : (o.shipping_fee ? fmtNum(o.shipping_fee) : '')
            const liveProfit = calcProfit(o)
            const liveProfitRate = calcProfitRate(o)
            const liveFeeRate = calcFeeRate(o)
            const displayCost = o.collected_product_id
              ? (collectedProductCosts[o.collected_product_id] ?? o.cost ?? 0)
              : (o.cost ?? 0)
            const activeActionTags = parseActionTags(activeActions[o.id] ?? o.action_tag ?? null)
            const customerAddress = splitCustomerAddress(o.customer_address, o.customer_address_detail)

            return (
              <tr key={o.id} style={{ borderBottom: '1px solid #1C2333', verticalAlign: 'top' }}>
                {/* 체크박스 */}
                <td style={{ padding: '0.75rem 0.5rem', textAlign: 'center', borderRight: '1px solid #1C2333' }}>
                  <div style={{ fontSize: '0.65rem', color: '#FFFFFF', fontWeight: 'bold', marginBottom: '2px' }}>{(currentPage - 1) * pageSize + index + 1}</div>
                  <input type="checkbox" checked={selectedIds.has(o.id)} onChange={() => setSelectedIds(prev => { const next = new Set(prev); if (next.has(o.id)) next.delete(o.id); else next.add(o.id); return next })} style={{ accentColor: '#F59E0B', cursor: 'pointer' }} />
                </td>
                {/* 주문정보 */}
                <OrderInfoCell
                  o={o}
                  refreshLog={refreshLog}
                  setRefreshLog={setRefreshLog}
                  sentFlags={sentFlags}
                  siteAliasMap={siteAliasMap}
                  actualSourceSite={o.collected_product_id ? (collectedProductSourceSites[o.collected_product_id] || '') : ''}
                  activeActions={activeActions}
                  setPriceHistoryProduct={setPriceHistoryProduct}
                  setPriceHistoryData={setPriceHistoryData}
                  setPriceHistoryModal={setPriceHistoryModal}
                  customerAddress={customerAddress}
                  renderCopyableText={renderCopyableText}
                  handleDelete={handleDelete}
                  handleImageClick={handleImageClick}
                  handleCopyOrderNumber={handleCopyOrderNumber}
                  openMsgModal={openMsgModal}
                  handleDanawa={handleDanawa}
                  handleNaver={handleNaver}
                  handleSourceLink={handleSourceLink}
                  handleMarketLink={handleMarketLink}
                  openUrlModal={openUrlModal}
                  handleTracking={handleTracking}
                  loadOrders={loadOrders}
                />
                {/* 금액 */}
                <td style={{ padding: '0.75rem', borderRight: '1px solid #1C2333', fontSize: '0.8rem' }}>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: '0.25rem' }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between' }}><span style={{ color: '#888' }}>결제</span><span>{fmtNum(o.total_payment_amount ?? o.sale_price)}</span></div>
                    <div style={{ display: 'flex', justifyContent: 'space-between' }}><span style={{ color: '#888' }}>정산</span><span>{fmtNum(Math.round(o.revenue))}</span></div>
                    <div style={{ display: 'flex', justifyContent: 'space-between' }}><span style={{ color: '#888' }}>실수익</span><span>{liveProfit >= 0 ? '+' : ''}{fmtNum(Math.round(liveProfit))}</span></div>
                    <div style={{ display: 'flex', justifyContent: 'space-between' }}><span style={{ color: '#888' }}>수수료율</span><span style={{ color: '#888' }}>{liveFeeRate}%</span></div>
                    <div style={{ display: 'flex', justifyContent: 'space-between' }}><span style={{ color: '#888' }}>수익률</span><span style={{ color: '#888' }}>{liveProfitRate}%</span></div>
                    <div style={{ display: 'flex', justifyContent: 'space-between' }}><span style={{ color: '#888' }}>원가</span><span style={{ color: '#888' }}>{fmtNum(displayCost)}</span></div>
                  </div>
                  {/* 주문취소 + 가격X/재고X/직배/까대기/선물 */}
                  <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, minmax(0, 1fr))', gap: '4px', marginTop: '0.375rem', borderTop: '1px solid #1C2333', paddingTop: '0.375rem' }}>
                    <button
                      onClick={async () => {
                        const isPlayauto = (o.source === 'playauto' || o.channel_name?.toLowerCase().includes('플레이오토'))
                                                const confirmMsg = isPlayauto ? '플레이오토는 EMP에서 직접 주문취소하셔야 합니다' : '주문취소하시겠습니까?'
                        const yes = await showConfirm(confirmMsg)
                        if (!yes) return
                        try {
                          const res = await orderApi.sellerCancel(o.id, 'SOLD_OUT')
                          showAlert(res.message || '처리 완료', 'success')
                          loadOrders()
                        } catch (err) {
                          showAlert(err instanceof Error ? err.message : '처리 실패', 'error')
                        }
                      }}
                      style={{
                        fontSize: '0.68rem', padding: '0.125rem 0',
                        background: 'rgba(220,38,38,0.8)',
                        color: '#fff', border: '1px solid #DC2626',
                        borderRadius: '4px', cursor: 'pointer', textAlign: 'center',
                        fontWeight: 600,
                      }}
                    >주문취소</button>
                    {ACTION_BUTTONS.map(btn => {
                      const isActive = activeActionTags.includes(btn.key)
                      return (
                        <button
                          key={btn.key}
                          onClick={() => toggleAction(o.id, btn.key)}
                          style={{
                            fontSize: '0.68rem', padding: '0.125rem 0',
                            background: isActive ? btn.activeColor : 'rgba(80,80,80,0.5)',
                            color: '#fff', border: isActive ? `1px solid ${btn.activeColor}` : '1px solid #555',
                            borderRadius: '4px', cursor: 'pointer', textAlign: 'center',
                          }}
                        >{btn.label}</button>
                      )
                    })}
                  </div>
                </td>
                {/* 주문상태 */}
                <td style={{ padding: '0.625rem', fontSize: '0.8rem' }}>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: '0.375rem' }}>
                    {/* 1행: 상태 드롭박스 + 주문번호 인풋 */}
                    <div style={{ display: 'flex', gap: '0.25rem', alignItems: 'stretch' }}>
                      <select value={o.status} onChange={e => handleStatusChange(o.id, e.target.value)}
                        style={{
                          ...inputStyle,
                          flex: 1,
                          fontSize: '0.75rem',
                          fontWeight: 600,
                          cursor: 'pointer',
                          color: o.status === 'ship_failed' ? '#FF3232' : inputStyle.color,
                        }}
                      >
                        {Object.entries(STATUS_MAP).map(([k, v]) => <option key={k} value={k} style={k === 'ship_failed' ? { color: '#FF3232' } : {}}>{v.label}</option>)}
                      </select>
                      <input
                        type="text"
                        placeholder={o.sourcing_account_id ? "소싱주문번호" : "주문계정 먼저 선택"}
                        disabled={!o.sourcing_account_id}
                        title={!o.sourcing_account_id ? '주문계정을 먼저 선택하세요' : undefined}
                        value={editingOrderNumbers[o.id] ?? o.sourcing_order_number ?? ''}
                        onChange={e => setEditingOrderNumbers(prev => ({ ...prev, [o.id]: e.target.value }))}
                        onBlur={async (e) => {
                          const val = e.target.value.trim()
                          setEditingOrderNumbers(prev => { const n = { ...prev }; delete n[o.id]; return n })
                          if (val === (o.sourcing_order_number ?? '')) return
                          try {
                            await orderApi.update(o.id, { sourcing_order_number: val })
                            patchOrder(o.id, { sourcing_order_number: val })
                          } catch { showAlert('소싱주문번호 저장 실패', 'error') }
                        }}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter') {
                            e.preventDefault()
                            ;(e.target as HTMLInputElement).blur()
                          }
                        }}
                        style={{
                          ...inputStyle,
                          flex: 1,
                          fontSize: '0.75rem',
                          opacity: o.sourcing_account_id ? 1 : 0.5,
                          cursor: o.sourcing_account_id ? 'text' : 'not-allowed',
                        }}
                      />
                    </div>

                    {/* 2행: 주문계정 + 마켓상태 */}
                    <div style={{ display: 'flex', gap: '0.25rem', alignItems: 'stretch' }}>
                      <select
                        value={o.sourcing_account_id || ''}
                        onChange={async (e) => {
                          const val = e.target.value
                          try {
                            await orderApi.update(o.id, { sourcing_account_id: val || undefined } as Partial<SambaOrder>)
                            patchOrder(o.id, { sourcing_account_id: val || undefined })
                          } catch { /* ignore */ }
                        }}
                        style={{ ...inputStyle, flex: 1, fontSize: '0.75rem', fontWeight: 600, cursor: 'pointer' }}
                      >
                        <option value="">주문계정</option>
                        {(() => {
                          const allSites = [...new Set(sourcingAccounts.map(sa => sa.site_name))]
                          const siteOrder: Record<string, number> = { MUSINSA: 0, LOTTEON: 1, SSG: 2 }
                          const sites = allSites.sort((a, b) => (siteOrder[a] ?? 99) - (siteOrder[b] ?? 99) || a.localeCompare(b))
                          return sites.map(site => (
                            <optgroup key={site} label={site}>
                              {sourcingAccounts.filter(sa => sa.site_name === site).map(sa => (
                                <option key={sa.id} value={sa.id}>{sa.account_label ? `${sa.account_label}(${sa.username})` : sa.username}</option>
                              ))}
                            </optgroup>
                          ))
                        })()}
                        <option value="etc">기타</option>
                      </select>
                      <div style={{
                        flex: 1, padding: '0.25rem 0.375rem',
                        background: 'rgba(30,30,30,0.6)', border: '1px solid #2D2D2D', borderRadius: '6px',
                        display: 'flex', alignItems: 'center', justifyContent: 'center',
                      }}>
                        <span style={{ fontSize: '0.75rem', color: '#4C9AFF', fontWeight: 600 }}>{(o.shipping_status === '출고지시' || o.shipping_status === '출하지시') ? '주문접수' : o.shipping_status === '발송대기' ? '배송대기중' : o.shipping_status === '송장전송완료' ? '국내배송중' : (STATUS_MAP[o.shipping_status]?.label || o.shipping_status || '-')}</span>
                      </div>
                    </div>

                    {/* 3행: 원가 + 배송비 */}
                    <div style={{ display: 'flex', gap: '0.25rem', alignItems: 'center' }}>
                      <input
                        type="text"
                        style={{ ...inputStyle, flex: 1, fontSize: '0.75rem', textAlign: 'right' }}
                        value={costDisplay}
                        placeholder="실구매가 (식 가능: 30000*.973+2300)"
                        onChange={e => {
                          // 숫자/사칙연산자/괄호/소수점/공백만 허용 (콤마는 입력 중 제거하여 식 평가 가능)
                          const raw = e.target.value.replace(/[^\d+\-*/.() ]/g, '')
                          setEditingCosts(prev => ({ ...prev, [o.id]: raw }))
                        }}
                        onBlur={() => handleCostSave(o.id)}
                        onKeyDown={e => {
                          if (e.key === 'Enter') {
                            e.preventDefault()
                            handleCostSave(o.id)
                          }
                        }}
                      />
                      <input
                        type="text"
                        style={{ ...inputStyle, flex: 1, fontSize: '0.75rem', textAlign: 'right' }}
                        value={shipFeeDisplay}
                        placeholder="배송비 (식 가능)"
                        onChange={e => {
                          const raw = e.target.value.replace(/[^\d+\-*/.() ]/g, '')
                          setEditingShipFees(prev => ({ ...prev, [o.id]: raw }))
                        }}
                        onBlur={() => handleShipFeeSave(o.id)}
                        onKeyDown={e => {
                          if (e.key === 'Enter') {
                            e.preventDefault()
                            handleShipFeeSave(o.id)
                          }
                        }}
                      />
                    </div>

                    {/* 택배사 + 송장번호 + 전송 */}
                    <div style={{ display: 'flex', gap: '0.25rem', alignItems: 'center' }}>
                      <select
                        key={`${o.id}-${o.shipping_company}-${o.status}`}
                        id={`ship-co-${o.id}`}
                        style={{ ...inputStyle, flex: 1, fontSize: '0.72rem' }}
                        defaultValue={o.shipping_company || ''}
                        onChange={async e => {
                          const co = e.target.value
                          const tn = (document.getElementById(`ship-tn-${o.id}`) as HTMLInputElement)?.value.trim() || ''
                          const alreadyShipped = o.shipping_status === '송장전송완료'
                          if (co && tn && alreadyShipped) {
                            const ts = fmtTime
                            try { await orderApi.update(o.id, { shipping_company: co, tracking_number: tn }) } catch { /* ignore */ }
                            patchOrder(o.id, { shipping_company: co, tracking_number: tn })
                            setLogMessages(prev => [...prev, `[${ts()}] ${o.order_number} 송장 수정 저장완료 (${co} ${tn}) — 마켓에서는 송장수정이 반영되지 않습니다. 마켓 판매자센터에서 직접 수정해주세요.`])
                          } else if (co && tn) {
                            const ts = fmtTime
                            setLogMessages(prev => [...prev, `[${ts()}] ${o.order_number} 송장 전송 중... (${co} ${tn})`])
                            try {
                              const res = await orderApi.shipOrder(o.id, co, tn)
                              if (!res.market_sent) {
                                await orderApi.updateStatus(o.id, 'ship_failed')
                                patchOrder(o.id, { shipping_company: co, tracking_number: tn, status: 'ship_failed' })
                                setLogMessages(prev => [...prev, `[${ts()}] ${o.order_number} ${res.message}`])
                              } else {
                                patchOrder(o.id, { shipping_company: co, tracking_number: tn, shipping_status: '송장전송완료' })
                                setLogMessages(prev => [...prev, `[${ts()}] ${o.order_number} ${res.message}`])
                              }
                            } catch {
                              await orderApi.updateStatus(o.id, 'ship_failed').catch(() => {})
                              patchOrder(o.id, { shipping_company: co, tracking_number: tn, status: 'ship_failed' })
                              setLogMessages(prev => [...prev, `[${ts()}] ${o.order_number} 송장 전송 실패`])
                            }
                          } else if (co) {
                            try { await orderApi.update(o.id, { shipping_company: co }) } catch { /* ignore */ }
                            patchOrder(o.id, { shipping_company: co })
                          }
                        }}
                      >
                        <option value="">택배사</option>
                        {SHIPPING_COMPANIES.map(sc => <option key={sc} value={sc}>{sc}</option>)}
                      </select>
                      <input
                        id={`ship-tn-${o.id}`}
                        style={{ ...inputStyle, flex: 1, fontSize: '0.72rem' }}
                        value={editingTrackings[o.id] ?? o.tracking_number ?? ''}
                        placeholder="송장번호"
                        onChange={e => setEditingTrackings(prev => ({ ...prev, [o.id]: e.target.value }))}
                        onBlur={async e => {
                          const tn = e.target.value.trim()
                          const co = (document.getElementById(`ship-co-${o.id}`) as HTMLSelectElement)?.value || ''
                          const changed = tn !== (o.tracking_number || '')
                          const retry = o.status === 'ship_failed'
                          const alreadyShipped = o.shipping_status === '송장전송완료'
                          if (co && tn && changed && alreadyShipped) {
                            // 이미 발송된 주문 — DB만 저장, 마켓 수정은 판매자센터에서
                            const ts = fmtTime
                            try { await orderApi.update(o.id, { shipping_company: co, tracking_number: tn }) } catch { /* ignore */ }
                            patchOrder(o.id, { shipping_company: co, tracking_number: tn })
                            setLogMessages(prev => [...prev, `[${ts()}] ${o.order_number} 송장 수정 저장완료 (${co} ${tn}) — 마켓에서는 송장수정이 반영되지 않습니다. 마켓 판매자센터에서 직접 수정해주세요.`])
                          } else if (co && tn && (changed || retry)) {
                            const ts = fmtTime
                            setLogMessages(prev => [...prev, `[${ts()}] ${o.order_number} 송장 전송 중... (${co} ${tn})`])
                            try {
                              const res = await orderApi.shipOrder(o.id, co, tn)
                              if (!res.market_sent) {
                                await orderApi.updateStatus(o.id, 'ship_failed')
                                patchOrder(o.id, { shipping_company: co, tracking_number: tn, status: 'ship_failed' })
                                setLogMessages(prev => [...prev, `[${ts()}] ${o.order_number} ${res.message}`])
                              } else {
                                patchOrder(o.id, { shipping_company: co, tracking_number: tn, shipping_status: '송장전송완료' })
                                setLogMessages(prev => [...prev, `[${ts()}] ${o.order_number} ${res.message}`])
                              }
                            } catch {
                              await orderApi.updateStatus(o.id, 'ship_failed').catch(() => {})
                              patchOrder(o.id, { shipping_company: co, tracking_number: tn, status: 'ship_failed' })
                              setLogMessages(prev => [...prev, `[${ts()}] ${o.order_number} 송장 전송 실패`])
                            }
                          } else if (tn && tn !== (o.tracking_number || '')) {
                            try { await orderApi.update(o.id, { tracking_number: tn }) } catch { /* ignore */ }
                            patchOrder(o.id, { tracking_number: tn })
                          }
                        }}
                        onKeyDown={e => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur() }}
                      />
                      <button
                        onClick={async () => {
                          const co = (document.getElementById(`ship-co-${o.id}`) as HTMLSelectElement)?.value || o.shipping_company || ''
                          const tn = (editingTrackings[o.id] ?? o.tracking_number ?? '').trim()
                          if (!co || !tn) {
                            setLogMessages(prev => [...prev, `[${fmtTime()}] ${o.order_number} 택배사/송장번호 누락 — 전송 불가`])
                            return
                          }
                          setLogMessages(prev => [...prev, `[${fmtTime()}] ${o.order_number} 마켓 전송 중... (${co} ${tn})`])
                          try {
                            const res = await orderApi.shipOrder(o.id, co, tn)
                            setLogMessages(prev => [...prev, `[${fmtTime()}] ${o.order_number} ${res.message}`])
                            if (!res.market_sent) {
                              await orderApi.updateStatus(o.id, 'ship_failed').catch(() => {})
                              patchOrder(o.id, { shipping_company: co, tracking_number: tn, status: 'ship_failed' })
                            } else {
                              patchOrder(o.id, { shipping_company: co, tracking_number: tn, shipping_status: '송장전송완료' })
                            }
                          } catch (err) {
                            await orderApi.updateStatus(o.id, 'ship_failed').catch(() => {})
                            patchOrder(o.id, { shipping_company: co, tracking_number: tn, status: 'ship_failed' })
                            setLogMessages(prev => [...prev, `[${fmtTime()}] ${o.order_number} 마켓 전송 실패: ${(err as Error).message}`])
                          }
                        }}
                        style={{ padding: '0.18rem 0.5rem', fontSize: '0.7rem', borderRadius: '4px', background: o.status === 'ship_failed' ? '#dc2626' : '#16a34a', color: '#fff', border: '1px solid #4b5563', cursor: 'pointer', whiteSpace: 'nowrap', flexShrink: 0 }}
                        title="택배사+송장번호를 마켓에 전송 (재전송 가능)"
                      >{o.status === 'ship_failed' ? '재전송' : '마켓전송'}</button>
                    </div>

                    {/* 간단메모 */}
                    <textarea
                      style={{ ...inputStyle, fontSize: '0.72rem', resize: 'none', height: '5.38rem', lineHeight: '1.4' }}
                      placeholder="간단메모"
                      value={editingNotes[o.id] ?? o.notes ?? ''}
                      onChange={e => setEditingNotes(prev => ({ ...prev, [o.id]: e.target.value }))}
                      onBlur={async e => {
                        const val = e.target.value.trim()
                        if (val !== (o.notes || '')) {
                          try {
                            await orderApi.update(o.id, { notes: val })
                            patchOrder(o.id, { notes: val })
                          } catch { /* ignore */ }
                        }
                        setEditingNotes(prev => {
                          const next = { ...prev }
                          delete next[o.id]
                          return next
                        })
                      }}
                    />
                  </div>
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

