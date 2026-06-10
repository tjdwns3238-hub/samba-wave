'use client'

import React, { Dispatch, SetStateAction, useState } from 'react'
import { type SambaMarketAccount } from '@/lib/samba/api/commerce'
import { type SambaSourcingAccount } from '@/lib/samba/api/operations'
import { orderApi } from '@/lib/samba/legacy'
import { PERIOD_BUTTONS } from '@/lib/samba/constants'
import { inputStyle, fmtNum } from '@/lib/samba/styles'
import { formatDateInput, getPeriodStart, getPeriodEnd } from '@/lib/samba/utils'
import { showAlert } from '@/components/samba/Modal'
import { STATUS_MAP } from '../constants'

interface Props {
  isProductMode: boolean
  period: string
  setPeriod: Dispatch<SetStateAction<string>>
  customStart: string
  setCustomStart: Dispatch<SetStateAction<string>>
  customEnd: string
  setCustomEnd: Dispatch<SetStateAction<string>>
  startLocked: boolean
  setStartLocked: Dispatch<SetStateAction<boolean>>
  dateLocked: boolean
  setDateLocked: Dispatch<SetStateAction<boolean>>
  syncAccountId: string
  setSyncAccountId: Dispatch<SetStateAction<string>>
  syncing: boolean
  handleFetch: () => void | Promise<void>
  bulkStatus: string
  setBulkStatus: Dispatch<SetStateAction<string>>
  bulkUpdating: boolean
  handleBulkAction: () => void | Promise<void>
  selectedIdsSize: number
  filteredOrdersCount: number
  filteredOrdersTotalSale: number
  searchCategory: string
  setSearchCategory: Dispatch<SetStateAction<string>>
  searchText: string
  setSearchText: Dispatch<SetStateAction<string>>
  loadOrders: () => void | Promise<void>
  marketFilter: string
  setMarketFilter: Dispatch<SetStateAction<string>>
  siteFilter: string
  setSiteFilter: Dispatch<SetStateAction<string>>
  accountFilter: string
  setAccountFilter: Dispatch<SetStateAction<string>>
  marketStatus: string
  setMarketStatus: Dispatch<SetStateAction<string>>
  registrationFilter: string
  setRegistrationFilter: Dispatch<SetStateAction<string>>
  inputFilter: string
  setInputFilter: Dispatch<SetStateAction<string>>
  invoiceFilter: string
  setInvoiceFilter: Dispatch<SetStateAction<string>>
  statusFilter: string
  setStatusFilter: Dispatch<SetStateAction<string>>
  sortBy: string
  setSortBy: Dispatch<SetStateAction<string>>
  pageSize: number
  setPageSize: Dispatch<SetStateAction<number>>
  accounts: SambaMarketAccount[]
  sourcingAccounts: SambaSourcingAccount[]
  siteOptions: Array<{ value: string; label: string }>
  selectedOrderIds: string[]
}

export default function OrdersFilterBar(props: Props) {
  const {
    isProductMode,
    period, setPeriod, customStart, setCustomStart, customEnd, setCustomEnd,
    startLocked, setStartLocked, dateLocked, setDateLocked,
    syncAccountId, setSyncAccountId, syncing, handleFetch,
    bulkStatus, setBulkStatus, bulkUpdating, handleBulkAction, selectedIdsSize,
    filteredOrdersCount, filteredOrdersTotalSale,
    searchCategory, setSearchCategory, searchText, setSearchText, loadOrders,
    marketFilter, setMarketFilter, siteFilter, setSiteFilter,
    accountFilter, setAccountFilter, marketStatus, setMarketStatus,
    registrationFilter, setRegistrationFilter,
    inputFilter, setInputFilter, invoiceFilter, setInvoiceFilter, statusFilter, setStatusFilter,
    sortBy, setSortBy, pageSize, setPageSize,
    accounts, sourcingAccounts, siteOptions,
    selectedOrderIds,
  } = props

  const [excelDownloading, setExcelDownloading] = useState(false)
  const [excelMenuOpen, setExcelMenuOpen] = useState(false)
  const handleExcelDownload = async (format: 'ub1' | 'lotte' = 'ub1') => {
    if (excelDownloading) return
    setExcelDownloading(true)
    try {
      if (selectedOrderIds.length > 0) {
        await orderApi.downloadExcel({ order_ids: selectedOrderIds, sort_by: sortBy, format })
      } else {
        if (!customStart || !customEnd) {
          showAlert('날짜 범위를 선택해주세요', 'info')
          return
        }
        await orderApi.downloadExcel({
          start: customStart,
          end: customEnd,
          market_filter: marketFilter,
          site_filter: siteFilter,
          account_filter: accountFilter,
          market_status: marketStatus,
          status_filter: statusFilter,
          input_filter: inputFilter,
          invoice_filter: invoiceFilter,
          registration_filter: registrationFilter,
          search_text: searchText,
          search_category: searchCategory,
          sort_by: sortBy,
          format,
        })
      }
    } catch (e) {
      showAlert((e as Error)?.message || '엑셀 다운로드 실패', 'error')
    } finally {
      setExcelDownloading(false)
    }
  }

  return (
    <>
      {!isProductMode && (
        <div style={{ background: 'rgba(18,18,18,0.98)', border: '1px solid #232323', borderRadius: '10px', padding: '0.625rem 0.875rem', marginBottom: '0.75rem', display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: '0.5rem' }}>
          <div style={{ display: 'flex', gap: '4px', flexWrap: 'wrap', alignItems: 'center' }}>
            {PERIOD_BUTTONS.map(pb => (
              <button
                key={pb.key}
                onClick={() => {
                  if (dateLocked) return
                  setPeriod(pb.key)
                  if (!startLocked) {
                    const start = getPeriodStart(pb.key)
                    setCustomStart(start ? formatDateInput(start) : '')
                  }
                  setCustomEnd(formatDateInput(getPeriodEnd(pb.key)))
                }}
                style={{
                  padding: '0.22rem 0.55rem',
                  borderRadius: '5px',
                  fontSize: '0.75rem',
                  background: period === pb.key ? 'rgba(80,80,80,0.8)' : 'rgba(50,50,50,0.8)',
                  border: period === pb.key ? '1px solid #666' : '1px solid #3D3D3D',
                  color: period === pb.key ? '#fff' : '#C5C5C5',
                  cursor: dateLocked ? 'not-allowed' : 'pointer',
                  opacity: dateLocked && period !== pb.key ? 0.5 : 1,
                }}
              >
                {pb.label}
              </button>
            ))}
            <input type="date" value={customStart} onChange={e => setCustomStart(e.target.value)} style={{ ...inputStyle, width: '160px', padding: '0.22rem 0.4rem', fontSize: '0.75rem', ...(startLocked ? { borderColor: '#C0392B', color: '#FF8C00' } : {}) }} />
            <button onClick={() => setStartLocked(prev => !prev)} style={{ padding: '0.22rem 0.5rem', fontSize: '0.72rem', borderRadius: '4px', cursor: 'pointer', background: startLocked ? '#8B1A1A' : 'rgba(50,50,50,0.8)', border: startLocked ? '1px solid #C0392B' : '1px solid #3D3D3D', color: startLocked ? '#fff' : '#C5C5C5' }}>시작고정</button>
            <span style={{ color: '#555', fontSize: '0.75rem' }}>~</span>
            <input type="date" value={customEnd} onChange={e => setCustomEnd(e.target.value)} style={{ ...inputStyle, width: '160px', padding: '0.22rem 0.4rem', fontSize: '0.75rem' }} />
            <button onClick={() => setDateLocked(prev => !prev)} style={{ padding: '0.22rem 0.5rem', fontSize: '0.72rem', borderRadius: '4px', cursor: 'pointer', background: dateLocked ? '#8B1A1A' : 'rgba(50,50,50,0.8)', border: dateLocked ? '1px solid #C0392B' : '1px solid #3D3D3D', color: dateLocked ? '#fff' : '#C5C5C5' }}>날짜고정</button>
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: '4px', flexWrap: 'wrap' }}>
            <select value={syncAccountId} onChange={e => setSyncAccountId(e.target.value)} style={{ ...inputStyle, width: '200px', padding: '0.22rem 0.4rem', fontSize: '0.72rem', minWidth: '200px' }}>
              <option value="">전체마켓보기</option>
              {(() => {
                const marketTypes = [...new Map(accounts.map(a => [a.market_type, a.market_name])).entries()]
                return marketTypes.flatMap(([type, name]) => [
                  <option key={`type:${type}`} value={`type:${type}`}>{name}</option>,
                  ...accounts
                    .filter(a => a.market_type === type)
                    .map(a => {
                      const accountName = a.account_label?.trim() || a.seller_id?.trim() || a.business_name?.trim() || a.market_name
                      return <option key={a.id} value={a.id}>- {accountName}</option>
                    }),
                ])
              })()}
            </select>
            <button onClick={handleFetch} disabled={syncing} style={{ padding: '0.22rem 0.65rem', fontSize: '0.75rem', background: 'rgba(50,50,50,0.9)', border: '1px solid #3D3D3D', color: '#C5C5C5', borderRadius: '4px', cursor: syncing ? 'not-allowed' : 'pointer', whiteSpace: 'nowrap' }}>{syncing ? '주문수집 중...' : '가져오기'}</button>
            <select value={bulkStatus} onChange={e => setBulkStatus(e.target.value)} style={{ ...inputStyle, width: '130px', padding: '0.22rem 0.4rem', fontSize: '0.72rem', minWidth: '130px' }}>
              <option value="">일괄 작업 선택</option>
              <option value="pending">주문접수</option>
              <option value="wait_ship">배송대기중</option>
              <option value="arrived">상품도착</option>
              <option value="ship_failed">송장전송실패</option>
              <option value="shipping">국내배송중</option>
              <option value="delivered">배송완료</option>
              <option value="cancelling">취소중</option>
              <option value="returning">반품중</option>
              <option value="exchanging">교환중</option>
              <option value="cancel_requested">취소요청</option>
              <option value="approve_cancel">취소승인(일괄)</option>
              <option value="return_requested">반품요청</option>
              <option value="cancelled">취소완료</option>
              <option value="returned">반품완료</option>
              <option value="exchanged">교환완료</option>
              <option value="return_completed">회수확정</option>
              <option value="undeliverable">발송불가</option>
              <option value="delete">삭제</option>
            </select>
            <button onClick={handleBulkAction} disabled={bulkUpdating || !bulkStatus || selectedIdsSize === 0} style={{ padding: '0.22rem 0.65rem', fontSize: '0.75rem', background: selectedIdsSize > 0 && bulkStatus ? '#C0392B' : 'rgba(50,50,50,0.9)', border: '1px solid #3D3D3D', color: selectedIdsSize > 0 && bulkStatus ? '#fff' : '#666', borderRadius: '4px', cursor: bulkUpdating || !bulkStatus || selectedIdsSize === 0 ? 'not-allowed' : 'pointer' }}>{bulkUpdating ? '처리 중...' : `일괄 실행 (${fmtNum(selectedIdsSize)})`}</button>
          </div>
        </div>
      )}

      <div style={{ background: 'rgba(18,18,18,0.98)', border: '1px solid #232323', borderRadius: '10px', padding: '0.75rem 1rem', marginBottom: '1rem', display: 'flex', alignItems: 'center', gap: '6px', flexWrap: 'wrap' }}>
        <span style={{ fontSize: '0.72rem', color: '#aaa' }}>
          <span style={{ color: '#FF8C00', fontWeight: 600 }}>{fmtNum(filteredOrdersCount)}</span>건 /
          <span style={{ color: '#FF8C00', fontWeight: 600 }}> {fmtNum(filteredOrdersTotalSale)}원</span>
        </span>
        <select style={{ ...inputStyle, width: '90px', padding: '0.22rem 0.4rem', fontSize: '0.75rem' }} value={searchCategory} onChange={e => setSearchCategory(e.target.value)}>
          <option value="product">상품명</option>
          <option value="customer">고객명</option>
          <option value="product_id">상품ID</option>
          <option value="order_number">주문번호</option>
        </select>
        <input style={{ ...inputStyle, width: '86px', padding: '0.22rem 0.4rem', fontSize: '0.75rem' }} value={searchText} onChange={e => setSearchText(e.target.value)} onKeyDown={e => { if (e.key === 'Enter') loadOrders() }} />
        <button onClick={loadOrders} style={{ background: 'linear-gradient(135deg,#FF8C00,#FFB84D)', color: '#fff', padding: '0.22rem 0.75rem', borderRadius: '5px', fontSize: '0.75rem', border: 'none', cursor: 'pointer' }}>검색</button>
        <div style={{ display: 'flex', gap: '4px', marginLeft: 'auto', flexWrap: 'wrap' }}>
          <select style={{ ...inputStyle, width: '140px', padding: '0.22rem 0.4rem', fontSize: '0.75rem' }} value={marketFilter} onChange={e => setMarketFilter(e.target.value)}>
            <option value="">전체 마켓</option>
            {(() => {
              const marketTypes = [...new Map(accounts.map(a => [a.market_type, a.market_name])).entries()]
              return marketTypes.flatMap(([type, name]) => [
                <option key={`type:${type}`} value={`type:${type}`}>{name}</option>,
                ...accounts
                  .filter(a => a.market_type === type)
                  .map(a => {
                    const accountName = a.account_label?.trim() || a.seller_id?.trim() || a.business_name?.trim() || a.market_name
                    return <option key={`acc:${a.id}`} value={`acc:${a.id}`}>- {accountName}</option>
                  }),
              ])
            })()}
          </select>
          <select style={{ ...inputStyle, width: '97px', padding: '0.22rem 0.4rem', fontSize: '0.75rem' }} value={siteFilter} onChange={e => setSiteFilter(e.target.value)}>
            <option value="">전체 소싱처</option>
            {siteOptions.map(site => <option key={site.value} value={site.value}>{site.label}</option>)}
          </select>
          <select style={{ ...inputStyle, width: '140px', padding: '0.22rem 0.4rem', fontSize: '0.75rem' }} value={accountFilter} onChange={e => setAccountFilter(e.target.value)}>
            <option value="">전체 소싱계정</option>
            <option value="etc">기타(미매핑)</option>
            {[...new Set(sourcingAccounts.map(sa => sa.site_name))].sort().map(site => (
              <optgroup key={site} label={site}>
                {sourcingAccounts.filter(sa => sa.site_name === site).map(sa => (
                  <option key={sa.id} value={sa.id}>{sa.account_label ? `${sa.account_label}(${sa.username})` : sa.username}</option>
                ))}
              </optgroup>
            ))}
          </select>
          <select style={{ ...inputStyle, width: '86px', padding: '0.22rem 0.4rem', fontSize: '0.75rem' }} value={marketStatus} onChange={e => setMarketStatus(e.target.value)}>
            <option value="">배송상태</option>
            <option value="결제완료">주문접수</option>
            <option value="배송대기중">배송대기중</option>
            <option value="국내배송중">국내배송중</option>
            <option value="배송완료">배송완료</option>
            <option value="취소요청">취소요청</option>
            <option value="취소완료">취소완료</option>
            <option value="반품요청">반품요청</option>
            <option value="교환요청">교환요청</option>
            <option value="교환완료">교환완료</option>
          </select>
          <select style={{ ...inputStyle, width: '86px', padding: '0.22rem 0.4rem', fontSize: '0.75rem' }} value={registrationFilter} onChange={e => setRegistrationFilter(e.target.value)}>
            <option value="">등록필터</option>
            <option value="registered">등록상품</option>
            <option value="unregistered">미등록상품</option>
          </select>
          <select style={{ ...inputStyle, width: '84px', padding: '0.22rem 0.4rem', fontSize: '0.75rem' }} value={inputFilter} onChange={e => setInputFilter(e.target.value)}>
            <option value="">입력필터</option>
            <option value="has_order">주문번호O</option>
            <option value="no_order">주문번호X</option>
            <option value="direct">직배</option>
            <option value="kkadaegi">까대기</option>
            <option value="gift">선물</option>
            <option value="no_price">가격X</option>
            <option value="no_stock">재고X</option>
            <option value="staff_a">직원A</option>
            <option value="staff_b">직원B</option>
          </select>
          <select style={{ ...inputStyle, width: '108px', padding: '0.22rem 0.4rem', fontSize: '0.75rem' }} value={invoiceFilter} onChange={e => setInvoiceFilter(e.target.value)}>
            <option value="">송장필터</option>
            <option value="has_invoice">송장입력</option>
            <option value="no_invoice">송장미입력</option>
          </select>
          <select style={{ ...inputStyle, width: '140px', padding: '0.22rem 0.4rem', fontSize: '0.75rem' }} value={statusFilter} onChange={e => setStatusFilter(e.target.value)}>
            <option value="">전체 주문상태</option>
            <option value="cancel_return_excluded">취소/반품/교환/배송 제외</option>
            {Object.entries(STATUS_MAP)
              .filter(([k]) => k !== 'preparing')
              .map(([k, v]) => <option key={k} value={k}>{v.label}</option>)}
          </select>
          <select value={sortBy} onChange={e => setSortBy(e.target.value)} style={{ ...inputStyle, width: '63px', padding: '0.22rem 0.4rem', fontSize: '0.75rem' }}>
            <option value="date_desc">최신순</option>
            <option value="date_asc">오래된순</option>
            <option value="profit_desc">마진높음</option>
            <option value="profit_asc">마진낮음</option>
            <option value="price_desc">매출높음</option>
            <option value="price_asc">매출낮음</option>
          </select>
          <select style={{ ...inputStyle, width: '66px', padding: '0.22rem 0.4rem', fontSize: '0.75rem' }} value={pageSize} onChange={e => setPageSize(Number(e.target.value))}>
            <option value={20}>20개</option>
            <option value={50}>50개</option>
            <option value={100}>100개</option>
            <option value={200}>200개</option>
            <option value={500}>500개</option>
          </select>
          <div style={{ position: 'relative', display: 'inline-block' }}>
            <button
              onClick={() => setExcelMenuOpen(prev => !prev)}
              disabled={excelDownloading}
              style={{
                padding: '0.22rem 0.65rem',
                fontSize: '0.75rem',
                background: selectedOrderIds.length > 0 ? '#1F6F3A' : 'rgba(50,50,50,0.9)',
                border: '1px solid #3D3D3D',
                color: selectedOrderIds.length > 0 ? '#fff' : '#C5C5C5',
                borderRadius: '4px',
                cursor: excelDownloading ? 'not-allowed' : 'pointer',
                whiteSpace: 'nowrap',
              }}
              title={selectedOrderIds.length > 0 ? `선택 ${fmtNum(selectedOrderIds.length)}건 — 양식 선택 후 다운로드` : '현재 필터 전체 — 양식 선택 후 다운로드'}
            >
              {excelDownloading
                ? '다운로드 중...'
                : selectedOrderIds.length > 0
                  ? `엑셀 다운(${fmtNum(selectedOrderIds.length)}) ▾`
                  : '엑셀 다운 ▾'}
            </button>
            {excelMenuOpen && !excelDownloading && (
              <>
                {/* 외부 클릭 감지용 투명 오버레이 */}
                <div
                  onClick={() => setExcelMenuOpen(false)}
                  style={{
                    position: 'fixed',
                    inset: 0,
                    zIndex: 20,
                    background: 'transparent',
                  }}
                />
                <div
                  style={{
                    position: 'absolute',
                    top: 'calc(100% + 4px)',
                    right: 0,
                    minWidth: '200px',
                    background: '#1A1A1A',
                    border: '1px solid #3D3D3D',
                    borderRadius: '6px',
                    boxShadow: '0 8px 24px rgba(0,0,0,0.45)',
                    zIndex: 30,
                    overflow: 'hidden',
                  }}
                >
                  <button
                    onClick={() => {
                      setExcelMenuOpen(false)
                      handleExcelDownload('ub1')
                    }}
                    style={{
                      display: 'block',
                      width: '100%',
                      padding: '0.5rem 0.75rem',
                      fontSize: '0.78rem',
                      textAlign: 'left',
                      background: 'transparent',
                      border: 'none',
                      borderBottom: '1px solid #2A2A2A',
                      color: '#E5E5E5',
                      cursor: 'pointer',
                    }}
                    onMouseEnter={(e) => { e.currentTarget.style.background = 'rgba(255,255,255,0.05)' }}
                    onMouseLeave={(e) => { e.currentTarget.style.background = 'transparent' }}
                  >
                    <div style={{ fontWeight: 600 }}>기본 양식 (UB1 발주)</div>
                    <div style={{ fontSize: '0.68rem', color: '#888', marginTop: '2px' }}>
                      마켓·마켓주문번호·구매가격 등 10컬럼
                    </div>
                  </button>
                  <button
                    onClick={() => {
                      setExcelMenuOpen(false)
                      handleExcelDownload('lotte')
                    }}
                    style={{
                      display: 'block',
                      width: '100%',
                      padding: '0.5rem 0.75rem',
                      fontSize: '0.78rem',
                      textAlign: 'left',
                      background: 'transparent',
                      border: 'none',
                      color: '#E5E5E5',
                      cursor: 'pointer',
                    }}
                    onMouseEnter={(e) => { e.currentTarget.style.background = 'rgba(255,255,255,0.05)' }}
                    onMouseLeave={(e) => { e.currentTarget.style.background = 'transparent' }}
                  >
                    <div style={{ fontWeight: 600 }}>롯데택배 송장 양식</div>
                    <div style={{ fontSize: '0.68rem', color: '#888', marginTop: '2px' }}>
                      수령자·연락처·주소·상품명·수량·배송메세지 7컬럼
                    </div>
                  </button>
                </div>
              </>
            )}
          </div>
        </div>
      </div>
    </>
  )
}
