"use client"

import React, { useEffect, useState, useCallback, useMemo } from "react"
import { orderApi, collectorApi, type OrderDashboardStats } from "@/lib/samba/api/commerce"
import { card, fmtNum } from "@/lib/samba/styles"

// 날짜 포맷: 3. 14. 형식
function formatShortDate(d: Date) {
  return `${d.getMonth() + 1}. ${d.getDate()}.`
}

type SourceBrand = { brand: string; total: number; registered: number; sold_out: number }
type SourceStat = { source_site: string; total: number; registered: number; sold_out: number; brands: SourceBrand[] }
type AccountBrand = { source_site: string; brand: string; registered: number }
type AccountStat = { account_id: string; market_name: string; account_label: string; registered: number; sold_products?: number; brands: AccountBrand[] }
type MarketSourceStat = { source_site: string; registered: number; brands: { brand: string; registered: number }[] }
type MarketAcctStat = { account_id: string; account_label: string; registered: number; sold_products?: number; sources: MarketSourceStat[] }
type MarketStat = { market_name: string; registered: number; accounts: MarketAcctStat[] }

export default function SambaDashboard() {
  const [stats, setStats] = useState<OrderDashboardStats | null>(null)
  const [collectedCount, setCollectedCount] = useState(0)
  const [loading, setLoading] = useState(true)
  const [bySource, setBySource] = useState<SourceStat[]>([])
  const [byAccount, setByAccount] = useState<AccountStat[]>([])
  const [expandedSources, setExpandedSources] = useState<Set<string>>(new Set())
  const [expandedMarkets, setExpandedMarkets] = useState<Set<string>>(new Set())
  const [expandedMarketAccts, setExpandedMarketAccts] = useState<Set<string>>(new Set())
  const [expandedAcctSources, setExpandedAcctSources] = useState<Set<string>>(new Set())

  function toggleSource(key: string) {
    setExpandedSources(prev => {
      const next = new Set(prev)
      next.has(key) ? next.delete(key) : next.add(key)
      return next
    })
  }

  function toggleMarket(key: string) {
    setExpandedMarkets(prev => {
      const next = new Set(prev)
      next.has(key) ? next.delete(key) : next.add(key)
      return next
    })
  }

  function toggleMarketAcct(key: string) {
    setExpandedMarketAccts(prev => {
      const next = new Set(prev)
      next.has(key) ? next.delete(key) : next.add(key)
      return next
    })
  }

  function toggleAcctSource(key: string) {
    setExpandedAcctSources(prev => {
      const next = new Set(prev)
      next.has(key) ? next.delete(key) : next.add(key)
      return next
    })
  }

  const byMarket = useMemo((): MarketStat[] => {
    const marketMap = new Map<string, { registered: number; accts: Map<string, { account_id: string; account_label: string; registered: number; sold_products: number; srcMap: Map<string, { registered: number; brandMap: Map<string, number> }> }> }>()
    for (const acct of byAccount) {
      const mKey = acct.market_name
      const mEntry = marketMap.get(mKey) ?? { registered: 0, accts: new Map() }
      mEntry.registered += acct.registered
      const aEntry = mEntry.accts.get(acct.account_id) ?? { account_id: acct.account_id, account_label: acct.account_label, registered: acct.registered, sold_products: acct.sold_products ?? 0, srcMap: new Map() }
      for (const b of acct.brands) {
        const sEntry = aEntry.srcMap.get(b.source_site) ?? { registered: 0, brandMap: new Map() }
        sEntry.registered += b.registered
        sEntry.brandMap.set(b.brand, (sEntry.brandMap.get(b.brand) ?? 0) + b.registered)
        aEntry.srcMap.set(b.source_site, sEntry)
      }
      mEntry.accts.set(acct.account_id, aEntry)
      marketMap.set(mKey, mEntry)
    }
    return Array.from(marketMap.entries())
      .map(([market_name, mData]) => ({
        market_name,
        registered: mData.registered,
        accounts: Array.from(mData.accts.values())
          .map(a => ({
            account_id: a.account_id,
            account_label: a.account_label,
            registered: a.registered,
            sold_products: a.sold_products,
            sources: Array.from(a.srcMap.entries())
              .map(([source_site, sData]) => ({
                source_site,
                registered: sData.registered,
                brands: Array.from(sData.brandMap.entries())
                  .map(([brand, registered]) => ({ brand, registered }))
                  .sort((x, y) => y.registered - x.registered),
              }))
              .sort((x, y) => y.registered - x.registered),
          }))
          .sort((x, y) => y.registered - x.registered),
      }))
      .sort((x, y) => y.registered - x.registered)
  }, [byAccount])

  const now = new Date()
  const year = now.getFullYear()
  const month = now.getMonth()

  const load = useCallback(async () => {
    setLoading(true)
    const [s, counts, dStats] = await Promise.all([
      orderApi.dashboardStats().catch(() => null),
      collectorApi.productCounts().catch(() => ({ total: 0, registered: 0, policy_applied: 0, sold_out: 0 })),
      collectorApi.dashboardStats().catch(() => ({ by_source: [], by_account: [] })),
    ])
    setStats(s)
    setCollectedCount(counts.total)
    setBySource(dStats.by_source)
    setByAccount(dStats.by_account)
    setLoading(false)
  }, [])

  useEffect(() => { load() }, [load])

  // 집계 데이터에서 KPI 추출
  const thisMonthSales = stats?.thisMonth.sales || 0
  const thisMonthCount = stats?.thisMonth.count || 0
  const thisMonthFulfillmentSales = stats?.thisMonth.fulfillmentSales || 0
  const thisMonthFulfillment = thisMonthSales > 0 ? Math.round(thisMonthFulfillmentSales / thisMonthSales * 100) : 0
  const lastMonthSales = stats?.lastMonth.sales || 0
  const lastMonthFulfillmentSales = stats?.lastMonth.fulfillmentSales || 0
  const lastMonthFulfillment = lastMonthSales > 0 ? Math.round(lastMonthFulfillmentSales / lastMonthSales * 100) : 0
  const salesChange = stats?.salesChange || 0
  const marketRegisteredCount = stats?.marketRegisteredCount ?? 0
  const weeklyData = (stats?.weekly || []).map(w => ({
    date: new Date(w.date),
    totalSale: w.sales,
    fulfillmentSale: w.fulfillmentSales,
    rate: w.sales > 0 ? Math.round(w.fulfillmentSales / w.sales * 100) : 0,
    unshippedCount: w.unshippedCount ?? 0,
    newRegistered: w.newRegistered as number | null | undefined,
    marketDeleted: w.marketDeleted ?? 0,
    registeredCount: w.registeredCount as number | null | undefined,
    collectedCount: w.collectedCount ?? 0,
  }))
  const monthlyData = stats?.monthly || []

  if (loading && !stats) {
    return <div style={{ padding: '3rem', textAlign: 'center', color: '#555' }}>로딩 중...</div>
  }

  // 선 그래프 렌더링
  const renderLineChart = () => {
    const W = 720
    const H = 180
    const padL = 45
    const padR = 20
    const padT = 20
    const padB = 30
    const chartW = W - padL - padR
    const chartH = H - padT - padB

    const allValues = monthlyData.flatMap(d => [d.sales, d.fulfillmentSales])
    const maxVal = Math.max(...allValues, 1000)
    // Y축 눈금 계산 (천원 단위)
    const maxK = Math.ceil(maxVal / 1000)
    const step = 50000 // 5천만원 단위
    const yMax = Math.ceil(maxK / step) * step
    const gridLines = []
    for (let v = 0; v <= yMax; v += step) gridLines.push(v)

    const getX = (i: number) => padL + (i / 11) * chartW
    const getY = (v: number) => padT + chartH - (v / 1000 / yMax) * chartH

    // 총매출 선
    const totalPoints = monthlyData.map((d, i) => `${getX(i)},${getY(d.sales)}`).join(' ')
    // 이행매출 선
    const fulfillmentPoints = monthlyData.map((d, i) => `${getX(i)},${getY(d.fulfillmentSales)}`).join(' ')

    return (
      <svg width="100%" viewBox={`0 0 ${W} ${H}`} style={{ overflow: 'visible' }}>
        {/* Y축 눈금선 + 라벨 */}
        {gridLines.map(v => {
          const y = padT + chartH - (v / yMax) * chartH
          return (
            <g key={v}>
              <line x1={padL} y1={y} x2={W - padR} y2={y} stroke="#2D2D2D" strokeWidth={1} />
              <text x={padL - 6} y={y + 4} textAnchor="end" fill="#666" fontSize="8">{fmtNum(v)}</text>
            </g>
          )
        })}
        {/* X축 라벨 */}
        {monthlyData.map((_, i) => (
          <text key={i} x={getX(i)} y={H - 5} textAnchor="middle" fill={i === month ? '#FF8C00' : '#666'} fontSize="8" fontWeight={i === month ? 700 : 400}>{i + 1}월</text>
        ))}
        {/* 총매출 선 */}
        <polyline points={totalPoints} fill="none" stroke="rgba(255,140,0,0.4)" strokeWidth={2} />
        {/* 이행매출 선 */}
        <polyline points={fulfillmentPoints} fill="none" stroke="#FF8C00" strokeWidth={2} />
        {/* 총매출 점 + 값 */}
        {monthlyData.map((d, i) => {
          const x = getX(i)
          const y = getY(d.sales)
          const kVal = Math.round(d.sales / 1000)
          return (
            <g key={`t-${i}`}>
              <circle cx={x} cy={y} r={3} fill="rgba(255,140,0,0.4)" />
              {kVal > 0 && <text x={x} y={y - 10} textAnchor="middle" fill="#888" fontSize="7">{fmtNum(kVal)}</text>}
            </g>
          )
        })}
        {/* 이행매출 점 + 값 */}
        {monthlyData.map((d, i) => {
          const x = getX(i)
          const y = getY(d.fulfillmentSales)
          const kVal = Math.round(d.fulfillmentSales / 1000)
          return (
            <g key={`f-${i}`}>
              <circle cx={x} cy={y} r={3} fill="#FF8C00" />
              {kVal > 0 && <text x={x} y={y - 10} textAnchor="middle" fill="#FF8C00" fontSize="7" fontWeight={600}>{fmtNum(kVal)}</text>}
            </g>
          )
        })}
      </svg>
    )
  }

  return (
    <div style={{ color: '#E5E5E5' }}>
      {/* 헤더 */}
      <div style={{ display: 'flex', alignItems: 'flex-end', justifyContent: 'space-between', marginBottom: '2rem' }}>
        <div>
          <h2 style={{ fontSize: '1.5rem', fontWeight: 700, color: '#E5E5E5', marginBottom: '0.25rem' }}>대시보드</h2>
          <p style={{ fontSize: '0.8125rem', color: '#888' }}>{year}년 · 누적 현황</p>
        </div>
        <p style={{ fontSize: '0.875rem', color: '#888' }}>{year}년 {month + 1}월 {now.getDate()}일</p>
      </div>

      {/* KPI 카드 5개 */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(5, 1fr)', gap: '1rem', marginBottom: '1.5rem' }}>
        <div style={{ ...card, padding: '1.5rem', borderColor: 'rgba(255,140,0,0.25)' }}>
          <p style={{ fontSize: '0.8125rem', color: '#888', marginBottom: '0.5rem' }}>총 매출 (금월)</p>
          <p style={{ fontSize: '1.75rem', fontWeight: 700, color: '#FF8C00' }}>₩{fmtNum(thisMonthSales)}</p>
          <p style={{ fontSize: '0.8125rem', color: '#888', marginTop: '0.5rem' }}>{fmtNum(thisMonthCount)}건</p>
        </div>
        <div style={{ ...card, padding: '1.5rem', borderColor: 'rgba(255,140,0,0.25)' }}>
          <p style={{ fontSize: '0.8125rem', color: '#888', marginBottom: '0.5rem' }}>이행매출 (금월)</p>
          <p style={{ fontSize: '1.75rem', fontWeight: 700, color: '#FF8C00' }}>₩{fmtNum(thisMonthFulfillmentSales)}</p>
          <p style={{ fontSize: '0.8125rem', color: '#888', marginTop: '0.5rem' }}>{month + 1}월 기준 · {Number(salesChange) >= 0 ? '▲' : '▼'}{Math.abs(Number(salesChange))}%</p>
        </div>
        <div style={{ ...card, padding: '1.5rem', borderColor: 'rgba(255,140,0,0.25)' }}>
          <p style={{ fontSize: '0.8125rem', color: '#888', marginBottom: '0.5rem' }}>수집상품</p>
          <p style={{ fontSize: '1.75rem', fontWeight: 700, color: '#FF8C00' }}>{fmtNum(collectedCount)}개</p>
          <p style={{ fontSize: '0.8125rem', color: '#888', marginTop: '0.5rem' }}>전체 수집</p>
        </div>
        <div style={{ ...card, padding: '1.5rem', borderColor: 'rgba(255,140,0,0.25)' }}>
          <p style={{ fontSize: '0.8125rem', color: '#888', marginBottom: '0.5rem' }}>마켓등록 상품수</p>
          <p style={{ fontSize: '1.75rem', fontWeight: 700, color: '#FF8C00' }}>{fmtNum(marketRegisteredCount)}개</p>
          <p style={{ fontSize: '0.8125rem', color: '#888', marginTop: '0.5rem' }}>1개 마켓이라도 등록</p>
        </div>
        <div style={{ ...card, padding: '1.5rem', borderColor: 'rgba(255,140,0,0.25)' }}>
          <p style={{ fontSize: '0.8125rem', color: '#888', marginBottom: '0.5rem' }}>주문이행율</p>
          <p style={{ fontSize: '1.75rem', fontWeight: 700, color: '#FF8C00' }}>{thisMonthFulfillment}%</p>
          <p style={{ fontSize: '0.8125rem', color: '#888', marginTop: '0.5rem' }}>이번 달 기준</p>
        </div>
      </div>

      {/* 최근 일주일 매출 + 금월/전월 비교 */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '1rem', marginBottom: '1.5rem' }}>
        {/* 최근 일주일 매출 */}
        <div style={{ ...card, padding: '1.5rem' }}>
          <h3 style={{ fontSize: '0.9375rem', fontWeight: 700, color: '#E5E5E5', marginBottom: '1rem' }}>최근 일주일 매출</h3>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.875rem' }}>
            <thead>
              <tr style={{ borderBottom: '1px solid #2D2D2D' }}>
                <th style={{ textAlign: 'left', padding: '0.625rem 0', color: '#888', fontWeight: 500 }}>날짜</th>
                <th style={{ textAlign: 'right', padding: '0.625rem 0', color: '#888', fontWeight: 500 }}>총매출</th>
                <th style={{ textAlign: 'right', padding: '0.625rem 0', color: '#888', fontWeight: 500 }}>이행매출</th>
                <th style={{ textAlign: 'right', padding: '0.625rem 0', color: '#888', fontWeight: 500 }}>이행율</th>
                <th style={{ textAlign: 'right', padding: '0.625rem 0', color: '#888', fontWeight: 500 }}>미발송</th>
                <th style={{ textAlign: 'right', padding: '0.625rem 0', color: '#888', fontWeight: 500 }}>신규등록</th>
                <th style={{ textAlign: 'right', padding: '0.625rem 0', color: '#888', fontWeight: 500 }}>마켓삭제</th>
                <th style={{ textAlign: 'right', padding: '0.625rem 0', color: '#888', fontWeight: 500 }}>등록상품수</th>
                <th style={{ textAlign: 'right', padding: '0.625rem 0', color: '#888', fontWeight: 500 }}>수집상품수</th>
              </tr>
            </thead>
            <tbody>
              {weeklyData.map((d) => (
                <tr key={d.date.toISOString()} style={{ borderBottom: '1px solid rgba(45,45,45,0.3)' }}>
                  <td style={{ padding: '0.625rem 0', color: '#E5E5E5' }}>{formatShortDate(d.date)}</td>
                  <td style={{ padding: '0.625rem 0', textAlign: 'right', color: '#E5E5E5' }}>₩{fmtNum(d.totalSale)}</td>
                  <td style={{ padding: '0.625rem 0', textAlign: 'right', color: '#E5E5E5' }}>₩{fmtNum(d.fulfillmentSale)}</td>
                  <td style={{ padding: '0.625rem 0', textAlign: 'right', color: '#E5E5E5' }}>{d.rate}%</td>
                  <td style={{ padding: '0.625rem 0', textAlign: 'right', color: d.unshippedCount > 0 ? '#FF6B6B' : '#E5E5E5' }}>{fmtNum(d.unshippedCount)}</td>
                  <td style={{ padding: '0.625rem 0', textAlign: 'right', color: '#E5E5E5' }}>{d.newRegistered == null ? '—' : fmtNum(d.newRegistered)}</td>
                  <td style={{ padding: '0.625rem 0', textAlign: 'right', color: '#E5E5E5' }}>{fmtNum(d.marketDeleted)}</td>
                  <td style={{ padding: '0.625rem 0', textAlign: 'right', color: '#FF8C00' }}>{d.registeredCount == null ? '—' : fmtNum(d.registeredCount)}</td>
                  <td style={{ padding: '0.625rem 0', textAlign: 'right', color: '#E5E5E5' }}>{fmtNum(d.collectedCount)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {/* 금월/전월 비교 */}
        <div style={{ ...card, padding: '1.5rem' }}>
          <h3 style={{ fontSize: '0.9375rem', fontWeight: 700, color: '#E5E5E5', marginBottom: '1rem' }}>금월 / 전월 비교</h3>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.875rem' }}>
            <thead>
              <tr style={{ borderBottom: '1px solid #2D2D2D' }}>
                <th style={{ textAlign: 'left', padding: '0.625rem 0', color: '#888', fontWeight: 500 }}>구분</th>
                <th style={{ textAlign: 'right', padding: '0.625rem 0', color: '#888', fontWeight: 500 }}>총매출</th>
                <th style={{ textAlign: 'right', padding: '0.625rem 0', color: '#888', fontWeight: 500 }}>이행매출</th>
                <th style={{ textAlign: 'right', padding: '0.625rem 0', color: '#888', fontWeight: 500 }}>이행율</th>
              </tr>
            </thead>
            <tbody>
              <tr style={{ borderBottom: '1px solid rgba(45,45,45,0.3)' }}>
                <td style={{ padding: '0.625rem 0', color: '#E5E5E5' }}>금월</td>
                <td style={{ padding: '0.625rem 0', textAlign: 'right', color: '#E5E5E5' }}>₩{fmtNum(thisMonthSales)}</td>
                <td style={{ padding: '0.625rem 0', textAlign: 'right', color: '#E5E5E5' }}>₩{fmtNum(thisMonthFulfillmentSales)}</td>
                <td style={{ padding: '0.625rem 0', textAlign: 'right', color: '#E5E5E5' }}>{thisMonthFulfillment}%</td>
              </tr>
              <tr>
                <td style={{ padding: '0.625rem 0', color: '#E5E5E5' }}>전월</td>
                <td style={{ padding: '0.625rem 0', textAlign: 'right', color: '#E5E5E5' }}>₩{fmtNum(lastMonthSales)}</td>
                <td style={{ padding: '0.625rem 0', textAlign: 'right', color: '#E5E5E5' }}>₩{fmtNum(lastMonthFulfillmentSales)}</td>
                <td style={{ padding: '0.625rem 0', textAlign: 'right', color: '#E5E5E5' }}>{lastMonthFulfillment}%</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      {/* 월별 매출 추이 */}
      <div style={{ ...card, padding: '1.5rem' }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '1rem' }}>
          <div>
            <h3 style={{ fontSize: '0.9375rem', fontWeight: 700, color: '#E5E5E5', marginBottom: '0.25rem' }}>월별 매출 추이</h3>
            <p style={{ fontSize: '0.75rem', color: '#888' }}>{year}년 월간 매출액 (단위: 천원)</p>
          </div>
          <div style={{ display: 'flex', gap: '1rem' }}>
            <span style={{ display: 'flex', alignItems: 'center', gap: '0.375rem', fontSize: '0.75rem', color: '#888' }}>
              <span style={{ width: '12px', height: '2px', background: 'rgba(255,140,0,0.4)' }} /> 총매출
            </span>
            <span style={{ display: 'flex', alignItems: 'center', gap: '0.375rem', fontSize: '0.75rem', color: '#FF8C00' }}>
              <span style={{ width: '12px', height: '2px', background: '#FF8C00' }} /> 이행매출
            </span>
          </div>
        </div>
        {renderLineChart()}
      </div>

      {/* 소싱처별 수집현황 + 계정별 등록현황 */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '1rem', marginTop: '1.5rem' }}>
        {/* 소싱처별 수집현황 */}
        <div style={{ ...card, padding: '1.5rem' }}>
          <h3 style={{ fontSize: '0.9375rem', fontWeight: 700, color: '#E5E5E5', marginBottom: '1rem' }}>소싱처별 수집현황</h3>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.875rem' }}>
            <thead>
              <tr style={{ borderBottom: '1px solid #2D2D2D' }}>
                <th style={{ textAlign: 'left', padding: '0.5rem 0', color: '#888', fontWeight: 500 }}>소싱처</th>
                <th style={{ textAlign: 'right', padding: '0.5rem 0', color: '#888', fontWeight: 500 }}>수집</th>
                <th style={{ textAlign: 'right', padding: '0.5rem 0', color: '#888', fontWeight: 500 }}>등록</th>
                <th style={{ textAlign: 'right', padding: '0.5rem 0', color: '#888', fontWeight: 500 }}>품절</th>
              </tr>
            </thead>
            <tbody>
              {bySource.map((s) => {
                const isExpanded = expandedSources.has(s.source_site)
                const hasBrands = s.brands && s.brands.length > 0
                return (
                  <React.Fragment key={s.source_site}>
                    <tr
                      style={{ borderBottom: '1px solid rgba(45,45,45,0.3)', cursor: hasBrands ? 'pointer' : 'default' }}
                      onClick={() => hasBrands && toggleSource(s.source_site)}
                    >
                      <td style={{ padding: '0.5rem 0', color: '#E5E5E5', display: 'flex', alignItems: 'center', gap: '0.375rem' }}>
                        {hasBrands && (
                          <span style={{ fontSize: '0.625rem', color: '#888', display: 'inline-block', transform: isExpanded ? 'rotate(90deg)' : 'rotate(0deg)', transition: 'transform 0.15s' }}>▶</span>
                        )}
                        {s.source_site}
                      </td>
                      <td style={{ padding: '0.5rem 0', textAlign: 'right', color: '#E5E5E5' }}>{fmtNum(s.total)}</td>
                      <td style={{ padding: '0.5rem 0', textAlign: 'right', color: '#FF8C00' }}>{fmtNum(s.registered)}</td>
                      <td style={{ padding: '0.5rem 0', textAlign: 'right', color: '#888' }}>{fmtNum(s.sold_out)}</td>
                    </tr>
                    {isExpanded && s.brands.map((b) => (
                      <tr key={`${s.source_site}-${b.brand}`} style={{ borderBottom: '1px solid rgba(45,45,45,0.15)', background: 'rgba(255,255,255,0.02)' }}>
                        <td style={{ padding: '0.3rem 0 0.3rem 1.25rem', color: '#888', fontSize: '0.8125rem' }}>- {b.brand}</td>
                        <td style={{ padding: '0.3rem 0', textAlign: 'right', color: '#888', fontSize: '0.8125rem' }}>{fmtNum(b.total)}</td>
                        <td style={{ padding: '0.3rem 0', textAlign: 'right', color: '#CC7000', fontSize: '0.8125rem' }}>{fmtNum(b.registered)}</td>
                        <td style={{ padding: '0.3rem 0', textAlign: 'right', color: '#666', fontSize: '0.8125rem' }}>{fmtNum(b.sold_out)}</td>
                      </tr>
                    ))}
                  </React.Fragment>
                )
              })}
              {bySource.length > 0 && (
                <tr style={{ borderTop: '1px solid #2D2D2D' }}>
                  <td style={{ padding: '0.5rem 0', color: '#FF8C00', fontWeight: 600 }}>합계</td>
                  <td style={{ padding: '0.5rem 0', textAlign: 'right', color: '#FF8C00', fontWeight: 600 }}>{fmtNum(bySource.reduce((a, s) => a + s.total, 0))}</td>
                  <td style={{ padding: '0.5rem 0', textAlign: 'right', color: '#FF8C00', fontWeight: 600 }}>{fmtNum(bySource.reduce((a, s) => a + s.registered, 0))}</td>
                  <td style={{ padding: '0.5rem 0', textAlign: 'right', color: '#888', fontWeight: 600 }}>{fmtNum(bySource.reduce((a, s) => a + s.sold_out, 0))}</td>
                </tr>
              )}
              {bySource.length === 0 && (
                <tr><td colSpan={4} style={{ padding: '1.5rem 0', textAlign: 'center', color: '#555' }}>데이터 없음</td></tr>
              )}
            </tbody>
          </table>
        </div>

        {/* 계정별 등록현황 */}
        <div style={{ ...card, padding: '1.5rem' }}>
          <h3 style={{ fontSize: '0.9375rem', fontWeight: 700, color: '#E5E5E5', marginBottom: '1rem' }}>계정별 등록현황</h3>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.875rem' }}>
            <thead>
              <tr style={{ borderBottom: '1px solid #2D2D2D' }}>
                <th style={{ textAlign: 'left', padding: '0.5rem 0', color: '#888', fontWeight: 500 }}>마켓 / 계정 / 소싱처</th>
                <th style={{ textAlign: 'right', padding: '0.5rem 0', color: '#888', fontWeight: 500 }}>판매비중</th>
                <th style={{ textAlign: 'right', padding: '0.5rem 0', color: '#888', fontWeight: 500 }}>등록 상품</th>
              </tr>
            </thead>
            <tbody>
              {byMarket.map((m) => {
                const mExpanded = expandedMarkets.has(m.market_name)
                return (
                  <React.Fragment key={m.market_name}>
                    {/* 마켓 행 */}
                    <tr
                      style={{ borderBottom: '1px solid rgba(45,45,45,0.3)', cursor: 'pointer' }}
                      onClick={() => toggleMarket(m.market_name)}
                    >
                      <td style={{ padding: '0.5rem 0', color: '#E5E5E5', fontWeight: 600, display: 'flex', alignItems: 'center', gap: '0.375rem' }}>
                        <span style={{ fontSize: '0.625rem', color: '#888', display: 'inline-block', transform: mExpanded ? 'rotate(90deg)' : 'rotate(0deg)', transition: 'transform 0.15s' }}>▶</span>
                        {m.market_name}
                      </td>
                      <td style={{ padding: '0.5rem 0', textAlign: 'right' }} />
                      <td style={{ padding: '0.5rem 0', textAlign: 'right', color: '#FF8C00', fontWeight: 600 }}>{fmtNum(m.registered)}</td>
                    </tr>
                    {mExpanded && m.accounts.map((a) => {
                      const acctKey = `${m.market_name}::${a.account_id}`
                      const aExpanded = expandedMarketAccts.has(acctKey)
                      return (
                        <React.Fragment key={a.account_id}>
                          {/* 계정 행 */}
                          <tr
                            style={{ borderBottom: '1px solid rgba(45,45,45,0.2)', cursor: 'pointer', background: 'rgba(255,255,255,0.02)' }}
                            onClick={() => toggleMarketAcct(acctKey)}
                          >
                            <td style={{ padding: '0.4rem 0 0.4rem 1.25rem', color: '#CCCCCC', display: 'flex', alignItems: 'center', gap: '0.375rem', fontSize: '0.8125rem' }}>
                              <span style={{ fontSize: '0.5625rem', color: '#666', display: 'inline-block', transform: aExpanded ? 'rotate(90deg)' : 'rotate(0deg)', transition: 'transform 0.15s' }}>▶</span>
                              {a.account_label || a.account_id}
                            </td>
                            {(() => {
                              const ratio = a.registered > 0 ? (a.sold_products ?? 0) / a.registered * 100 : 0
                              return (
                                <td style={{ padding: '0.4rem 0', textAlign: 'right', fontSize: '0.8125rem', color: ratio >= 3 ? '#4CAF50' : ratio >= 1 ? '#FF8C00' : '#FF4444' }}>
                                  {a.registered > 0 ? `${ratio.toFixed(1)}%` : '-'}
                                </td>
                              )
                            })()}
                            <td style={{ padding: '0.4rem 0', textAlign: 'right', color: '#FF8C00', fontSize: '0.8125rem' }}>{fmtNum(a.registered)}</td>
                          </tr>
                          {aExpanded && a.sources.map((s) => {
                            const srcKey = `${a.account_id}::${s.source_site}`
                            const sExpanded = expandedAcctSources.has(srcKey)
                            const hasBrands = s.brands.length > 0
                            return (
                              <React.Fragment key={s.source_site}>
                                {/* 소싱처 행 */}
                                <tr
                                  style={{ borderBottom: '1px solid rgba(45,45,45,0.15)', cursor: hasBrands ? 'pointer' : 'default', background: 'rgba(255,255,255,0.03)' }}
                                  onClick={() => hasBrands && toggleAcctSource(srcKey)}
                                >
                                  <td style={{ padding: '0.35rem 0 0.35rem 2.5rem', color: '#AAAAAA', display: 'flex', alignItems: 'center', gap: '0.375rem', fontSize: '0.8125rem' }}>
                                    {hasBrands && (
                                      <span style={{ fontSize: '0.5rem', color: '#555', display: 'inline-block', transform: sExpanded ? 'rotate(90deg)' : 'rotate(0deg)', transition: 'transform 0.15s' }}>▶</span>
                                    )}
                                    {s.source_site}
                                  </td>
                                  <td style={{ padding: '0.35rem 0', textAlign: 'right' }} />
                                  <td style={{ padding: '0.35rem 0', textAlign: 'right', color: '#CC7000', fontSize: '0.8125rem' }}>{fmtNum(s.registered)}</td>
                                </tr>
                                {sExpanded && s.brands.map((b) => (
                                  <tr key={`${s.source_site}-${b.brand}`} style={{ borderBottom: '1px solid rgba(45,45,45,0.1)', background: 'rgba(255,255,255,0.04)' }}>
                                    <td style={{ padding: '0.3rem 0 0.3rem 3.75rem', color: '#888', fontSize: '0.75rem' }}>- {b.brand}</td>
                                    <td style={{ padding: '0.3rem 0', textAlign: 'right' }} />
                                    <td style={{ padding: '0.3rem 0', textAlign: 'right', color: '#AA5F00', fontSize: '0.75rem' }}>{fmtNum(b.registered)}</td>
                                  </tr>
                                ))}
                              </React.Fragment>
                            )
                          })}
                        </React.Fragment>
                      )
                    })}
                  </React.Fragment>
                )
              })}
              {byMarket.length > 0 && (
                <tr style={{ borderTop: '1px solid #2D2D2D' }}>
                  <td style={{ padding: '0.5rem 0', color: '#FF8C00', fontWeight: 600 }}>합계</td>
                  <td style={{ padding: '0.5rem 0', textAlign: 'right' }} />
                  <td style={{ padding: '0.5rem 0', textAlign: 'right', color: '#FF8C00', fontWeight: 600 }}>{fmtNum(byMarket.reduce((a, m) => a + m.registered, 0))}</td>
                </tr>
              )}
              {byMarket.length === 0 && (
                <tr><td colSpan={3} style={{ padding: '1.5rem 0', textAlign: 'center', color: '#555' }}>데이터 없음</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

    </div>
  )
}
