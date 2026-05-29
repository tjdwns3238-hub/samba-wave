'use client'

import { useEffect, useCallback } from 'react'
import { type AnalyticsAggregateRow } from '@/lib/samba/api/commerce'
import { useLocalStorageState } from '@/hooks/useLocalStorageState'
import { STORAGE_KEYS } from '@/lib/samba/constants'
import { card, fmtNum } from '@/lib/samba/styles'
import { RevenueTrendLine, SalesBarChart } from '@/components/samba/AnalyticsCharts'
import {
  SOURCE_SITES, ORDER_STATUSES, DEFAULT_STATUSES,
  type AnalyticsSearch, type MonthlyCell,
} from './constants'
import { useAnalyticsData } from './hooks/useAnalyticsData'

const fmt = fmtNum

export default function AnalyticsPage() {
  useEffect(() => { document.title = 'SAMBA-분석' }, [])
  // 검색 조건 (localStorage 자동 복원/저장)
  const now = new Date()
  const defaultSearch: AnalyticsSearch = {
    year: now.getFullYear(),
    month: 0,
    markets: [],
    sites: [],
    statuses: DEFAULT_STATUSES,
  }
  const [search, setSearch] = useLocalStorageState<AnalyticsSearch>(
    STORAGE_KEYS.ANALYTICS_SEARCH,
    defaultSearch,
  )
  const searchYear = search.year
  const searchMonth = search.month
  const selectedMarkets = search.markets
  const selectedSites = search.sites
  const selectedStatuses = search.statuses
  const setSearchYear = (v: number) => setSearch(prev => ({ ...prev, year: v }))
  const setSearchMonth = (v: number) => setSearch(prev => ({ ...prev, month: v }))
  const setSelectedMarkets = useCallback((v: string[]) => setSearch(prev => ({ ...prev, markets: v })), [setSearch])
  const setSelectedSites = useCallback((v: string[]) => setSearch(prev => ({ ...prev, sites: v })), [setSearch])
  const setSelectedStatuses = (v: string[]) => setSearch(prev => ({ ...prev, statuses: v }))

  const toggleItem = (arr: string[], setArr: (v: string[]) => void, item: string) => {
    setArr(arr.includes(item) ? arr.filter(x => x !== item) : [...arr, item])
  }

  const {
    loading, error, marketAccounts, aggregate,
    dailyData, sourcingRoi, bestSellers, brandData, load,
  } = useAnalyticsData({
    searchYear, searchMonth, setSelectedSites, setSelectedMarkets,
    hasStoredMarkets: selectedMarkets.length > 0,
    hasStoredSites: selectedSites.length > 0,
  })

  const channelToMarket = (name: string | undefined): string => {
    if (!name) return '기타'
    const idx = name.indexOf('(')
    return idx > 0 ? name.substring(0, idx) : name
  }

  // 기간 + 마켓 + 소싱처 + 주문상태 필터링 (paid_at KST 기준)
  // aggregate row.date 형식: 'YYYY-MM-DD' (백엔드에서 KST 변환 완료)
  const filteredRows: AnalyticsAggregateRow[] = aggregate.filter(r => {
    if (!r.date) return false
    const [yStr, mStr] = r.date.split('-')
    const y = Number(yStr), m = Number(mStr)
    if (y !== searchYear) return false
    if (searchMonth > 0 && m !== searchMonth) return false
    if (selectedStatuses.length < ORDER_STATUSES.length) {
      if (!selectedStatuses.includes(r.status)) return false
    }
    if (selectedMarkets.length > 0) {
      if (!selectedMarkets.includes(channelToMarket(r.channel_name))) return false
    }
    if (selectedSites.length > 0 && selectedSites.length < SOURCE_SITES.length) {
      const siteKey = r.source_site && SOURCE_SITES.includes(r.source_site) ? r.source_site : '미등록상품'
      if (siteKey !== '미등록상품' && !selectedSites.includes(siteKey)) return false
    }
    return true
  })

  // 전체 합계
  const totalSales = filteredRows.reduce((s, r) => s + (r.sales || 0), 0)
  const totalOrders = filteredRows.reduce((s, r) => s + (r.orders || 0), 0)

  // ──────────────────────────────────────────────
  // 집계 함수: 월 선택 시 일별, 전체 시 월별
  // ──────────────────────────────────────────────
  const isDaily = searchMonth > 0
  const rowCount = isDaily ? new Date(searchYear, searchMonth, 0).getDate() : 12

  const buildTable = (
    getKey: (r: AnalyticsAggregateRow) => string,
  ): { columns: string[], data: Record<number, Record<string, MonthlyCell>> } => {
    const colSet = new Set<string>()
    const data: Record<number, Record<string, MonthlyCell>> = {}
    for (let r = 1; r <= rowCount; r++) data[r] = {}

    for (const ar of filteredRows) {
      const [, mStr, dStr] = ar.date.split('-')
      const row = isDaily ? Number(dStr) : Number(mStr)
      if (!row || row < 1 || row > rowCount) continue
      const key = getKey(ar)
      colSet.add(key)
      if (!data[row][key]) data[row][key] = { sales: 0, orders: 0 }
      data[row][key].sales += ar.sales || 0
      data[row][key].orders += ar.orders || 0
    }

    const columns = [...colSet].sort()
    return { columns, data }
  }

  // 마켓별 통계
  const marketTable = buildTable(r => channelToMarket(r.channel_name))
  // 소싱처별 통계
  const siteTable = buildTable(r => {
    const site = r.source_site
    if (!site || !SOURCE_SITES.includes(site)) return '미등록상품'
    return site
  })
  // 주문상태별 통계
  const statusLabelMap: Record<string, string> = {}
  for (const s of ORDER_STATUSES) statusLabelMap[s.key] = s.label
  const statusTable = buildTable(r => statusLabelMap[r.status] || r.status || '미지정')
  // 주문상태별 컬럼을 ORDER_STATUSES 정의 순서로 정렬
  const statusColumnOrder = ORDER_STATUSES.map(s => s.label)
  const orderedStatusColumns = statusColumnOrder.filter(label => statusTable.columns.includes(label))
  const extraStatusColumns = statusTable.columns.filter(col => !statusColumnOrder.includes(col))
  const finalStatusColumns = [...orderedStatusColumns, ...extraStatusColumns]

  // 테이블 셀 스타일
  const thStyle: React.CSSProperties = {
    padding: '8px 12px',
    fontSize: '0.75rem',
    fontWeight: 600,
    color: '#B0B0B0',
    borderBottom: '2px solid #3D3D3D',
    borderRight: '1px solid #2D2D2D',
    textAlign: 'center',
    whiteSpace: 'nowrap',
    position: 'sticky',
    top: 0,
    background: '#1A1A1A',
    zIndex: 2,
  }
  const tdStyle: React.CSSProperties = {
    padding: '6px 10px',
    fontSize: '0.75rem',
    color: '#D0D0D0',
    borderBottom: '1px solid #2D2D2D',
    borderRight: '1px solid #2D2D2D',
    textAlign: 'right',
    whiteSpace: 'nowrap',
  }
  const tdEmptyStyle: React.CSSProperties = {
    ...tdStyle,
    textAlign: 'center',
    color: '#555',
  }

  // 월별 테이블 렌더러
  const renderMonthlyTable = (
    title: string,
    columns: string[],
    data: Record<number, Record<string, MonthlyCell>>,
  ) => {
    // 열별 합계
    const colTotals: Record<string, MonthlyCell> = {}
    for (const col of columns) {
      colTotals[col] = { sales: 0, orders: 0 }
      for (let r = 1; r <= rowCount; r++) {
        const cell = data[r]?.[col]
        if (cell) {
          colTotals[col].sales += cell.sales
          colTotals[col].orders += cell.orders
        }
      }
    }
    // 전체 합계
    const grandTotal: MonthlyCell = { sales: 0, orders: 0 }
    for (const col of columns) {
      grandTotal.sales += colTotals[col].sales
      grandTotal.orders += colTotals[col].orders
    }
    // 행별 합계
    const rowTotals: Record<number, MonthlyCell> = {}
    for (let r = 1; r <= rowCount; r++) {
      rowTotals[r] = { sales: 0, orders: 0 }
      for (const col of columns) {
        const cell = data[r]?.[col]
        if (cell) {
          rowTotals[r].sales += cell.sales
          rowTotals[r].orders += cell.orders
        }
      }
    }

    return (
      <div style={{ marginBottom: '1.5rem' }}>
        <h3 style={{
          fontSize: '0.9375rem', fontWeight: 600, color: '#FF8C00',
          position: 'sticky', top: '64px', zIndex: 10,
          background: 'rgba(15,15,15,0.97)', backdropFilter: 'blur(4px)',
          padding: '0.5rem 1.25rem', marginBottom: 0,
          borderBottom: '1px solid #2D2D2D',
        }}>{title}</h3>
        <div style={{ ...card, padding: '1.25rem', borderTopLeftRadius: 0, borderTopRightRadius: 0 }}>
        <div style={{ overflowX: 'auto' }}>
          <table style={{ borderCollapse: 'collapse', width: '100%', minWidth: `${(columns.length + 2) * 120}px` }}>
            <thead>
              <tr>
                <th style={{ ...thStyle, left: 0, zIndex: 3 }}></th>
                {columns.map(col => (
                  <th key={col} style={thStyle}>{col}</th>
                ))}
                <th style={{ ...thStyle, color: '#FF8C00', fontWeight: 700 }}>합계</th>
              </tr>
            </thead>
            <tbody>
              {Array.from({ length: rowCount }, (_, i) => i + 1).map(row => {
                const hasData = rowTotals[row].orders > 0
                return (
                  <tr key={row} style={{ background: hasData ? 'transparent' : 'rgba(40,50,40,0.15)' }}>
                    <td style={{ ...tdStyle, textAlign: 'center', fontWeight: 600, position: 'sticky', left: 0, background: hasData ? '#1E1E1E' : 'rgba(30,40,30,0.6)', zIndex: 1 }}>
                      {isDaily ? `${row}일` : `${row}월`}
                    </td>
                    {columns.map(col => {
                      const cell = data[row]?.[col]
                      if (!cell || cell.orders === 0) {
                        return <td key={col} style={tdEmptyStyle}>-</td>
                      }
                      return (
                        <td key={col} style={tdStyle}>
                          <div>{fmt(cell.sales)}</div>
                          <div style={{ fontSize: '0.625rem', color: '#888' }}>({fmt(cell.orders)}건)</div>
                        </td>
                      )
                    })}
                    <td style={{ ...tdStyle, fontWeight: 700, color: '#FF8C00' }}>
                      {hasData ? (
                        <>
                          <div>{fmt(rowTotals[row].sales)}</div>
                          <div style={{ fontSize: '0.625rem', color: '#888' }}>({fmt(rowTotals[row].orders)}건)</div>
                        </>
                      ) : '-'}
                    </td>
                  </tr>
                )
              })}
              {/* 합계 행 */}
              <tr style={{ background: 'rgba(30,30,30,0.8)', borderTop: '2px solid #3D3D3D' }}>
                <td style={{ ...tdStyle, textAlign: 'center', fontWeight: 700, position: 'sticky', left: 0, background: '#1A1A1A', zIndex: 1, borderTop: '2px solid #3D3D3D' }}>
                  합계
                </td>
                {columns.map(col => {
                  const total = colTotals[col]
                  const pct = grandTotal.sales > 0 ? ((total.sales / grandTotal.sales) * 100).toFixed(1) : '0.0'
                  return (
                    <td key={col} style={{ ...tdStyle, fontWeight: 600, borderTop: '2px solid #3D3D3D' }}>
                      {total.orders > 0 ? (
                        <>
                          <div>{fmt(total.sales)}</div>
                          <div style={{ fontSize: '0.625rem', color: '#888' }}>({pct}%)</div>
                        </>
                      ) : '-'}
                    </td>
                  )
                })}
                <td style={{ ...tdStyle, fontWeight: 700, color: '#FF8C00', borderTop: '2px solid #3D3D3D' }}>
                  <div>{fmt(grandTotal.sales)}</div>
                  <div style={{ fontSize: '0.625rem', color: '#888' }}>({fmt(grandTotal.orders)}건)</div>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
      </div>
    )
  }

  if (loading) {
    return <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '50vh', color: '#555' }}>로딩 중...</div>
  }

  return (
    <div style={{ color: '#E5E5E5' }}>
      {/* 헤더 */}
      <div style={{ marginBottom: '1.5rem' }}>
        <h2 style={{ fontSize: '1.5rem', fontWeight: 700, marginBottom: '0.25rem' }}>매출통계</h2>
      </div>

      {/* 에러 배너 — 무음 0건 회귀 차단 */}
      {error && (
        <div style={{
          padding: '0.75rem 1rem', marginBottom: '1rem',
          background: 'rgba(220,38,38,0.12)', border: '1px solid #DC2626',
          borderRadius: '4px', color: '#FCA5A5', fontSize: '0.8125rem',
        }}>
          매출 데이터 조회 실패: {error} — 잠시 후 [매출검색] 다시 눌러주세요.
        </div>
      )}

      {/* 검색 조건 */}
      <div style={{ ...card, padding: '1.25rem', marginBottom: '1.5rem' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', marginBottom: '1rem' }}>
          <select value={searchYear} onChange={e => setSearchYear(Number(e.target.value))}
            style={{ padding: '0.375rem 0.5rem', fontSize: '0.8125rem', background: '#1A1A1A', border: '1px solid #2D2D2D', borderRadius: '4px', color: '#E5E5E5', outline: 'none', cursor: 'pointer' }}>
            {[2024, 2025, 2026].map(y => <option key={y} value={y}>{y}년</option>)}
          </select>
          <select value={searchMonth} onChange={e => setSearchMonth(Number(e.target.value))}
            style={{ padding: '0.375rem 0.5rem', fontSize: '0.8125rem', background: '#1A1A1A', border: '1px solid #2D2D2D', borderRadius: '4px', color: '#E5E5E5', outline: 'none', cursor: 'pointer' }}>
            <option value={0}>전체</option>
            {Array.from({ length: 12 }, (_, i) => <option key={i + 1} value={i + 1}>{i + 1}월</option>)}
          </select>
          <button onClick={load}
            style={{ padding: '0.375rem 0.875rem', fontSize: '0.8125rem', background: '#FF8C00', color: '#fff', border: 'none', borderRadius: '4px', fontWeight: 600, cursor: 'pointer' }}>매출검색</button>
          <span style={{ marginLeft: 'auto', fontSize: '0.8125rem', color: '#888' }}>
            총 <span style={{ color: '#FF8C00', fontWeight: 700 }}>{fmtNum(totalOrders)}</span>건 · 매출 <span style={{ color: '#FF8C00', fontWeight: 700 }}>₩{fmtNum(totalSales)}</span>
          </span>
        </div>

        {/* 마켓 */}
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem', padding: '0.625rem 0', borderTop: '1px solid #2D2D2D', flexWrap: 'wrap' }}>
          <span style={{ color: '#888', fontSize: '0.8125rem', minWidth: '65px', flexShrink: 0 }}>마켓</span>
          {(() => {
            const marketNames = [...new Set([...marketAccounts.map(a => a.market_name)])]
            const allMarkets = marketNames.length > 0 ? marketNames : ['스마트스토어', '11번가']
            const isAll = allMarkets.length > 0 && selectedMarkets.length === allMarkets.length
            return (
              <>
                <label style={{ display: 'flex', alignItems: 'center', gap: '0.25rem', fontSize: '0.8125rem', color: '#888', cursor: 'pointer' }}>
                  <input type="checkbox" checked={isAll} onChange={() => setSelectedMarkets(isAll ? [] : [...allMarkets])} /> 전체
                </label>
                {allMarkets.map(name => (
                  <label key={name} style={{ display: 'flex', alignItems: 'center', gap: '0.25rem', fontSize: '0.8125rem', cursor: 'pointer' }}>
                    <input type="checkbox" checked={isAll || selectedMarkets.includes(name)} onChange={() => {
                      if (isAll) setSelectedMarkets(allMarkets.filter(m => m !== name))
                      else toggleItem(selectedMarkets, setSelectedMarkets, name)
                    }} style={{ accentColor: '#FF8C00' }} />
                    <span style={{ color: '#FF8C00' }}>{name}</span>
                  </label>
                ))}
              </>
            )
          })()}
        </div>

        {/* 소싱사이트 */}
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem', padding: '0.625rem 0', borderTop: '1px solid #2D2D2D', flexWrap: 'wrap' }}>
          <span style={{ color: '#888', fontSize: '0.8125rem', minWidth: '65px', flexShrink: 0 }}>소싱사이트</span>
          {(() => {
            const isAll = selectedSites.length === SOURCE_SITES.length
            return (
              <>
                <label style={{ display: 'flex', alignItems: 'center', gap: '0.25rem', fontSize: '0.8125rem', color: '#888', cursor: 'pointer' }}>
                  <input type="checkbox" checked={isAll} onChange={() => setSelectedSites(isAll ? [] : [...SOURCE_SITES])} /> 전체
                </label>
                {SOURCE_SITES.map(site => (
                  <label key={site} style={{ display: 'flex', alignItems: 'center', gap: '0.25rem', fontSize: '0.8125rem', cursor: 'pointer' }}>
                    <input type="checkbox" checked={isAll || selectedSites.includes(site)} onChange={() => {
                      if (isAll) setSelectedSites(SOURCE_SITES.filter(s => s !== site))
                      else toggleItem(selectedSites, setSelectedSites, site)
                    }} style={{ accentColor: '#FF8C00' }} />
                    <span style={{ color: '#FF8C00' }}>{site}</span>
                  </label>
                ))}
              </>
            )
          })()}
        </div>

        {/* 주문상태 */}
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem', padding: '0.625rem 0', borderTop: '1px solid #2D2D2D', flexWrap: 'wrap' }}>
          <span style={{ color: '#888', fontSize: '0.8125rem', minWidth: '65px', flexShrink: 0 }}>주문상태</span>
          {(() => {
            const allKeys = ORDER_STATUSES.map(s => s.key)
            const isAll = selectedStatuses.length === allKeys.length
            return (
              <>
                <label style={{ display: 'flex', alignItems: 'center', gap: '0.25rem', fontSize: '0.8125rem', color: '#888', cursor: 'pointer' }}>
                  <input type="checkbox" checked={isAll} onChange={() => setSelectedStatuses(isAll ? [] : [...allKeys])} /> 전체
                </label>
                {ORDER_STATUSES.map(st => (
                  <label key={st.key} style={{ display: 'flex', alignItems: 'center', gap: '0.25rem', fontSize: '0.8125rem', cursor: 'pointer' }}>
                    <input type="checkbox" checked={isAll || selectedStatuses.includes(st.key)} onChange={() => {
                      if (isAll) setSelectedStatuses(allKeys.filter(k => k !== st.key))
                      else toggleItem(selectedStatuses, setSelectedStatuses, st.key)
                    }} style={{ accentColor: '#FF8C00' }} />
                    <span style={{ color: '#FF8C00' }}>{st.label}</span>
                  </label>
                ))}
              </>
            )
          })()}
        </div>
      </div>

      {/* 마켓별 통계 */}
      {renderMonthlyTable('마켓별 통계', marketTable.columns, marketTable.data)}

      {/* 소싱처별 통계 */}
      {renderMonthlyTable('소싱처별 통계', siteTable.columns, siteTable.data)}

      {/* 주문상태별 통계 */}
      {renderMonthlyTable('주문상태별 통계', finalStatusColumns, statusTable.data)}

      {/* ── 차트 + 추가 분석 섹션 ── */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr', gap: '1.5rem', marginTop: '1.5rem' }}>
        {/* 매출 추이 라인차트 */}
        <div>
          <div style={{
            fontSize: '0.9375rem', fontWeight: 700,
            position: 'sticky', top: '64px', zIndex: 10,
            background: 'rgba(15,15,15,0.97)', backdropFilter: 'blur(4px)',
            padding: '0.5rem 1.25rem',
            borderBottom: '1px solid #2D2D2D',
            borderRadius: '12px 12px 0 0',
            border: '1px solid #2D2D2D',
          }}>최근 30일 매출 추이</div>
          <div style={{ ...card, padding: '1.25rem', borderTopLeftRadius: 0, borderTopRightRadius: 0, borderTop: 'none' }}>
            <RevenueTrendLine data={dailyData} />
          </div>
        </div>
      </div>

      {/* 브랜드별 매출 */}
      {brandData.length > 0 && (
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '1.5rem', marginTop: '1.5rem' }}>
          <div>
            <div style={{
              fontSize: '0.9375rem', fontWeight: 700,
              position: 'sticky', top: '64px', zIndex: 10,
              background: 'rgba(15,15,15,0.97)', backdropFilter: 'blur(4px)',
              padding: '0.5rem 1.25rem',
              borderRadius: '12px 12px 0 0', border: '1px solid #2D2D2D',
            }}>브랜드별 매출 TOP 10</div>
            <div style={{ ...card, padding: '1.25rem', borderTopLeftRadius: 0, borderTopRightRadius: 0, borderTop: 'none' }}>
              <SalesBarChart data={brandData} nameKey="brand" valueKey="sales" />
            </div>
          </div>
          <div>
            <div style={{
              fontSize: '0.9375rem', fontWeight: 700,
              position: 'sticky', top: '64px', zIndex: 10,
              background: 'rgba(15,15,15,0.97)', backdropFilter: 'blur(4px)',
              padding: '0.5rem 1.25rem',
              borderRadius: '12px 12px 0 0', border: '1px solid #2D2D2D',
            }}>브랜드별 상세</div>
            <div style={{ ...card, padding: '1.25rem', borderTopLeftRadius: 0, borderTopRightRadius: 0, borderTop: 'none' }}>
              <div style={{ overflowX: 'auto' }}>
                <table style={{ width: '100%', fontSize: '0.8125rem', borderCollapse: 'collapse' }}>
                  <thead>
                    <tr style={{ borderBottom: '1px solid #2D2D2D' }}>
                      <th style={{ padding: '0.5rem', textAlign: 'left', color: '#999' }}>브랜드</th>
                      <th style={{ padding: '0.5rem', textAlign: 'right', color: '#999' }}>매출</th>
                      <th style={{ padding: '0.5rem', textAlign: 'right', color: '#999' }}>이익</th>
                      <th style={{ padding: '0.5rem', textAlign: 'right', color: '#999' }}>건수</th>
                      <th style={{ padding: '0.5rem', textAlign: 'right', color: '#999' }}>이윤율</th>
                    </tr>
                  </thead>
                  <tbody>
                    {brandData.slice(0, 15).map(b => (
                      <tr key={b.brand} style={{ borderBottom: '1px solid #1A1A1A' }}>
                        <td style={{ padding: '0.4rem 0.5rem' }}>{b.brand}</td>
                        <td style={{ padding: '0.4rem 0.5rem', textAlign: 'right', color: '#FF8C00' }}>₩{fmt(b.sales)}</td>
                        <td style={{ padding: '0.4rem 0.5rem', textAlign: 'right', color: '#22C55E' }}>₩{fmt(b.profit)}</td>
                        <td style={{ padding: '0.4rem 0.5rem', textAlign: 'right' }}>{fmt(b.orders)}</td>
                        <td style={{ padding: '0.4rem 0.5rem', textAlign: 'right' }}>{b.avg_margin_rate}%</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* 소싱처 ROI */}
      {sourcingRoi.length > 0 && (
        <div style={{ marginTop: '1.5rem' }}>
          <div style={{
            fontSize: '0.9375rem', fontWeight: 700,
            position: 'sticky', top: '64px', zIndex: 10,
            background: 'rgba(15,15,15,0.97)', backdropFilter: 'blur(4px)',
            padding: '0.5rem 1.25rem',
            borderRadius: '12px 12px 0 0', border: '1px solid #2D2D2D',
          }}>소싱처별 ROI 분석</div>
          <div style={{ ...card, padding: '1.25rem', borderTopLeftRadius: 0, borderTopRightRadius: 0, borderTop: 'none' }}>
            <div style={{ overflowX: 'auto' }}>
              <table style={{ width: '100%', fontSize: '0.8125rem', borderCollapse: 'collapse' }}>
                <thead>
                  <tr style={{ borderBottom: '1px solid #2D2D2D' }}>
                    <th style={{ padding: '0.5rem', textAlign: 'left', color: '#999' }}>소싱처</th>
                    <th style={{ padding: '0.5rem', textAlign: 'right', color: '#999' }}>매출</th>
                    <th style={{ padding: '0.5rem', textAlign: 'right', color: '#999' }}>원가</th>
                    <th style={{ padding: '0.5rem', textAlign: 'right', color: '#999' }}>이익</th>
                    <th style={{ padding: '0.5rem', textAlign: 'right', color: '#999' }}>건수</th>
                    <th style={{ padding: '0.5rem', textAlign: 'right', color: '#999' }}>건당이익</th>
                    <th style={{ padding: '0.5rem', textAlign: 'right', color: '#999' }}>이윤율</th>
                    <th style={{ padding: '0.5rem', textAlign: 'right', color: '#999' }}>ROI</th>
                  </tr>
                </thead>
                <tbody>
                  {sourcingRoi.map(r => (
                    <tr key={r.source_site} style={{ borderBottom: '1px solid #1A1A1A' }}>
                      <td style={{ padding: '0.4rem 0.5rem', fontWeight: 600 }}>{r.source_site}</td>
                      <td style={{ padding: '0.4rem 0.5rem', textAlign: 'right', color: '#FF8C00' }}>₩{fmt(r.total_revenue)}</td>
                      <td style={{ padding: '0.4rem 0.5rem', textAlign: 'right' }}>₩{fmt(r.total_cost)}</td>
                      <td style={{ padding: '0.4rem 0.5rem', textAlign: 'right', color: '#22C55E' }}>₩{fmt(r.total_profit)}</td>
                      <td style={{ padding: '0.4rem 0.5rem', textAlign: 'right' }}>{fmt(r.order_count)}</td>
                      <td style={{ padding: '0.4rem 0.5rem', textAlign: 'right' }}>₩{fmt(r.avg_profit_per_order)}</td>
                      <td style={{ padding: '0.4rem 0.5rem', textAlign: 'right' }}>{r.avg_margin_rate}%</td>
                      <td style={{ padding: '0.4rem 0.5rem', textAlign: 'right', color: r.roi >= 0 ? '#22C55E' : '#EF4444', fontWeight: 600 }}>{r.roi}%</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}

      {/* 베스트셀러 */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr', gap: '1.5rem', marginTop: '1.5rem' }}>
        <div>
          <div style={{
            fontSize: '0.9375rem', fontWeight: 700, color: '#FF8C00',
            position: 'sticky', top: '64px', zIndex: 10,
            background: 'rgba(15,15,15,0.97)', backdropFilter: 'blur(4px)',
            padding: '0.5rem 1.25rem',
            borderRadius: '12px 12px 0 0', border: '1px solid #2D2D2D',
          }}>베스트셀러 TOP 10 (30일)</div>
          <div style={{ ...card, padding: '1.25rem', borderTopLeftRadius: 0, borderTopRightRadius: 0, borderTop: 'none' }}>
            {bestSellers.length > 0 ? (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
                {bestSellers.map((p, i) => (
                  <div key={i} style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '0.4rem 0', borderBottom: '1px solid #1A1A1A', fontSize: '0.8125rem' }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', flex: 1, minWidth: 0 }}>
                      <span style={{ color: '#FF8C00', fontWeight: 700, width: '1.5rem' }}>{i + 1}</span>
                      <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{p.product_name}</span>
                    </div>
                    <span style={{ display: 'flex', alignItems: 'center', gap: '0.75rem', whiteSpace: 'nowrap', marginLeft: '0.5rem' }}>
                      <span style={{ color: '#888', fontSize: '0.75rem' }}>{fmt(p.orders)}건</span>
                      <span style={{ color: '#FF8C00', fontWeight: 600 }}>₩{fmt(p.sales)}</span>
                    </span>
                  </div>
                ))}
              </div>
            ) : <p style={{ color: '#666', fontSize: '0.8rem' }}>데이터 없음</p>}
          </div>
        </div>
      </div>
    </div>
  )
}
