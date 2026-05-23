'use client'

import { Tooltip, ResponsiveContainer, LineChart, Line, XAxis, YAxis, CartesianGrid, BarChart, Bar, Legend } from 'recharts'
import { fmtNum } from '@/lib/samba/styles'

interface ChartProps {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  data: Record<string, any>[]
  height?: number
}

// 매출 추이 라인차트
export function RevenueTrendLine({ data, height = 280 }: ChartProps) {
  if (!data.length) return <p style={{ color: '#666', fontSize: '0.8rem' }}>데이터 없음</p>

  const formatted = data.map(d => ({
    date: (d.date || d.month || '').slice(-5),
    sales: d.sales || 0,
    profit: d.profit || 0,
  }))

  return (
    <ResponsiveContainer width="100%" height={height}>
      <LineChart data={formatted}>
        <CartesianGrid strokeDasharray="3 3" stroke="#2D2D2D" />
        <XAxis dataKey="date" tick={{ fill: '#999', fontSize: 11 }} />
        <YAxis tick={{ fill: '#999', fontSize: 11 }} tickFormatter={v => `${Math.round(v / 10000)}만`} />
        <Tooltip formatter={(v) => `${fmtNum(Number(v))}원`} contentStyle={{ background: '#1A1A1A', border: '1px solid #333', borderRadius: 6 }} labelStyle={{ color: '#999' }} />
        <Legend />
        <Line type="monotone" dataKey="sales" name="매출" stroke="#FF8C00" strokeWidth={2} dot={false} />
        <Line type="monotone" dataKey="profit" name="이익" stroke="#22C55E" strokeWidth={2} dot={false} />
      </LineChart>
    </ResponsiveContainer>
  )
}

// 브랜드/소싱처 매출 바차트
export function SalesBarChart({ data, height = 280, nameKey = 'name', valueKey = 'sales' }: ChartProps & { nameKey?: string; valueKey?: string }) {
  if (!data.length) return <p style={{ color: '#666', fontSize: '0.8rem' }}>데이터 없음</p>

  const formatted = data.slice(0, 10).map(d => ({
    name: (d[nameKey] || '미분류').slice(0, 12),
    value: d[valueKey] || 0,
    profit: d.profit || 0,
  }))

  return (
    <ResponsiveContainer width="100%" height={height}>
      <BarChart data={formatted} layout="vertical">
        <CartesianGrid strokeDasharray="3 3" stroke="#2D2D2D" />
        <XAxis type="number" tick={{ fill: '#999', fontSize: 11 }} tickFormatter={v => `${Math.round(v / 10000)}만`} />
        <YAxis type="category" dataKey="name" tick={{ fill: '#ccc', fontSize: 11 }} width={100} />
        <Tooltip formatter={(v) => `${fmtNum(Number(v))}원`} contentStyle={{ background: '#1A1A1A', border: '1px solid #333', borderRadius: 6 }} labelStyle={{ color: '#999' }} />
        <Bar dataKey="value" name="매출" fill="#FF8C00" radius={[0, 4, 4, 0]} />
        <Bar dataKey="profit" name="이익" fill="#22C55E" radius={[0, 4, 4, 0]} />
      </BarChart>
    </ResponsiveContainer>
  )
}
