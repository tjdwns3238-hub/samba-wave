'use client'

import { useEffect, useState, useCallback, useRef } from 'react'
import { accountApi, orderApi, type SambaMarketAccount } from '@/lib/samba/api/commerce'
import { returnApi, type SambaReturn } from '@/lib/samba/api/support'
import { showAlert, showConfirm } from '@/components/samba/Modal'
import { card, inputStyle, fmtNum, fmtTextNumbers } from '@/lib/samba/styles'
import { PERIOD_BUTTONS } from '@/lib/samba/constants'
import { fmtTime, getPeriodStart, getPeriodEnd } from '@/lib/samba/utils'

import {
  STATUS_MAP, TYPE_LABELS, RETURN_REASONS,
  fmtMD, getAccountOptionLabel, tdCenter,
} from './constants'
import { ReturnDetailModal } from './components/ReturnDetailModal'

// 완료내역(completion_detail) 옵션 + 색상 (다크테마: 옅은 배경 + 글자색)
const COMPLETION_DEFAULT = '대기중'
const COMPLETION_OPTIONS = ['대기중', '취소완료', '반품완료', '교환완료']
const COMPLETION_COLORS: Record<string, { bg: string; fg: string }> = {
  '대기중': { bg: 'rgba(255,217,61,0.12)', fg: '#FFD93D' },   // 노랑
  '취소완료': { bg: 'rgba(255,107,107,0.12)', fg: '#FF6B6B' }, // 빨강
  '반품완료': { bg: 'rgba(247,131,172,0.14)', fg: '#F783AC' }, // 핑크
  '교환완료': { bg: 'rgba(76,154,255,0.12)', fg: '#4C9AFF' },  // 파랑
}

export default function ReturnsPage() {
  useEffect(() => { document.title = 'SAMBA-반품관리' }, [])
  const [returns, setReturns] = useState<SambaReturn[]>([])
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const [, setStats] = useState<Record<string, any>>({})
  const [loading, setLoading] = useState(true)
  const [showForm, setShowForm] = useState(false)
  const [detailItem, setDetailItem] = useState<SambaReturn | null>(null)
  const [filterStatus] = useState<string>('')
  const [filterType] = useState<string>('')
  const [form, setForm] = useState({ order_id: '', type: 'return', reason: '', customReason: '', quantity: 1, requested_amount: 0 })

  // 로그 + 검색/필터 상태
  const logRef = useRef<HTMLDivElement>(null)
  const [logMessages, _setLogMessagesRaw] = useState<string[]>(['[대기] 반품교환 가져오기 결과가 여기에 표시됩니다...'])
  const setLogMessages: typeof _setLogMessagesRaw = (v) => _setLogMessagesRaw(prev => {
    const next = typeof v === 'function' ? v(prev) : v
    return next.slice(-30)
  })
  const [period, setPeriod] = useState('today')
  const [syncAccountId, setSyncAccountId] = useState('')
  const [customStart, setCustomStart] = useState((getPeriodStart('today') ?? new Date()).toLocaleDateString('sv-SE'))
  const [customEnd, setCustomEnd] = useState(getPeriodEnd('today').toLocaleDateString('sv-SE'))
  const [startLocked, setStartLocked] = useState(false)
  const [dateLocked, setDateLocked] = useState(false)
  const [accounts, setAccounts] = useState<SambaMarketAccount[]>([])

  useEffect(() => { accountApi.listActiveCached(setAccounts) }, [])
  useEffect(() => { logRef.current && (logRef.current.scrollTop = logRef.current.scrollHeight) }, [logMessages])



  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set())

  // 신규 금액 입력칸 (백엔드 저장 없이 로컬 표시용) — 행 id 기준
  const [customerAmounts, setCustomerAmounts] = useState<Record<string, string>>({})
  const [companyAmounts, setCompanyAmounts] = useState<Record<string, string>>({})

  const [siteFilter, setSiteFilter] = useState('')
  const [pageSize, setPageSize] = useState(50)
  const [searchCategory, setSearchCategory] = useState('product')
  const [searchText, setSearchText] = useState('')
  const [marketFilter, setMarketFilter] = useState('')

  const load = useCallback(async () => {
    setLoading(true)
    const data = await returnApi.list(undefined, filterStatus || undefined, filterType || undefined, 500, customStart || undefined, customEnd || undefined).catch(() => [])
    const st = await returnApi.getStats().catch(() => ({}))
    setReturns(data)
    setStats(st)
    setLoading(false)
  }, [filterStatus, filterType, customStart, customEnd])

  useEffect(() => { load() }, [load])

  // 가져오기 버튼 — 마켓 동기화 후 DB 데이터 로드
  const loadReturns = async () => {
    const ts = fmtTime

    // 마켓타입 선택 시 해당 마켓 계정들만 순회 동기화
    if (syncAccountId.startsWith('type:')) {
      const marketType = syncAccountId.replace('type:', '')
      const marketAccs = accounts.filter(a => a.market_type === marketType)
      const marketName = marketAccs[0]?.market_name || marketType
      setLogMessages(prev => [...prev, `[${ts()}] ${marketName} 반품교환 수집 시작 (${fmtNum(marketAccs.length)}개 계정)...`])
      let totalSynced = 0
      for (const acc of marketAccs) {
        try {
          const syncResult = await returnApi.syncFromMarkets(30, acc.id)
          for (const r of syncResult.results) {
            if (r.status === 'success') {
              setLogMessages(prev => [...prev, `[${ts()}] ${r.account}: ${fmtNum(r.fetched ?? 0)}건 조회, ${fmtNum(r.synced ?? 0)}건 신규`])
            } else if (r.status === 'error') {
              setLogMessages(prev => [...prev, `[${ts()}] ${r.account}: 오류 — ${r.message}`])
            }
          }
          totalSynced += syncResult.total_synced
        } catch (e) {
          setLogMessages(prev => [...prev, `[${ts()}] ${acc.market_name}(${acc.seller_id || '-'}) 오류: ${e}`])
        }
      }
      setLogMessages(prev => [...prev, `[${ts()}] ${marketName} 반품교환 수집 완료 (신규 ${fmtNum(totalSynced)}건)`])
      await load()
      return
    }

    // 전체마켓 또는 개별 계정 동기화
    const isAll = !syncAccountId
    const label = isAll ? '전체마켓' : (accounts.find(a => a.id === syncAccountId)?.market_name || syncAccountId)
    setLogMessages(prev => [...prev, `[${ts()}] ${label} 반품교환 수집 중...`])
    try {
      const syncResult = await returnApi.syncFromMarkets(30, isAll ? undefined : syncAccountId)
      for (const r of syncResult.results) {
        if (r.status === 'success') {
          setLogMessages(prev => [...prev, `[${ts()}] ${r.account}: ${fmtNum(r.fetched ?? 0)}건 조회, ${fmtNum(r.synced ?? 0)}건 신규`])
        } else if (r.status === 'error') {
          setLogMessages(prev => [...prev, `[${ts()}] ${r.account}: 오류 — ${r.message}`])
        }
      }
      setLogMessages(prev => [...prev, `[${ts()}] 반품교환 수집 완료 (신규 ${fmtNum(syncResult.total_synced)}건)`])
    } catch (e) {
      setLogMessages(prev => [...prev, `[오류] 반품교환 수집 실패: ${e}`])
    }
    await load()
  }

  const handleSubmit = async () => {
    try {
      const reason = form.reason || form.customReason
      if (!reason) {
        showAlert('반품/교환 사유를 입력해주세요', 'error')
        return
      }
      await returnApi.create({
        order_id: form.order_id,
        type: form.type,
        reason,
        quantity: form.quantity,
        requested_amount: form.requested_amount || undefined,
      })
      setShowForm(false)
      setForm({ order_id: '', type: 'return', reason: '', customReason: '', quantity: 1, requested_amount: 0 })
      load()
    } catch (e) {
      showAlert(e instanceof Error ? e.message : '저장 실패', 'error')
    }
  }

  const [rejectModal, setRejectModal] = useState<{ id: string; reason: string } | null>(null)
  const [locationModal, setLocationModal] = useState<{ id: string; value: string; address: string } | null>(null)
  const [addressModal, setAddressModal] = useState<{ region: string; address: string; phone: string; customer: string } | null>(null)

  const submitReject = async () => {
    if (!rejectModal || !rejectModal.reason.trim()) {
      showAlert('거절 사유를 입력해주세요', 'error')
      return
    }
    try {
      await returnApi.reject(rejectModal.id, rejectModal.reason)
      setRejectModal(null)
      load()
    } catch (e) { showAlert(e instanceof Error ? e.message : '거절 처리 실패', 'error') }
  }
  const handleBatchDelete = async () => {
    if (selectedIds.size === 0) {
      showAlert('삭제할 항목을 선택해주세요', 'info')
      return
    }
    if (!await showConfirm(`${fmtNum(selectedIds.size)}건을 삭제하시겠습니까?`)) return
    let deleted = 0
    for (const id of selectedIds) {
      try {
        await returnApi.cancel(id)
        deleted++
      } catch (_e) { /* 무시 */ }
    }
    setSelectedIds(new Set())
    load()
    showAlert(`${fmtNum(deleted)}건 삭제 완료`, 'success')
  }

  // 교환/취소 액션
  const [exchangeActionItem, setExchangeActionItem] = useState<SambaReturn | null>(null)
  const [reshipStep, setReshipStep] = useState(false) // 교환재배송 송장 입력 단계
  const [reshipForm, setReshipForm] = useState({ tracking_number: '', shipping_company: '롯데택배' })

  const handleExchangeAction = async (r: SambaReturn, action: string, extra?: { tracking_number?: string; shipping_company?: string }) => {
    const orderNum = r.order_number || r.order_id
    if (!orderNum) { showAlert('주문번호가 없습니다', 'error'); return }
    const labels: Record<string, string> = { reship: '교환재배송', reject: '교환거부', convert_return: '반품변경' }
    if (!await showConfirm(`${orderNum} 주문을 ${labels[action]} 처리하시겠습니까?`)) return
    try {
      const order = await orderApi.findByOrderNumber(orderNum)
      if (!order) { showAlert('해당 주문을 찾을 수 없습니다', 'error'); return }
      const res = await orderApi.exchangeAction(order.id, action, undefined, extra)
      showAlert(res.message || `${labels[action]} 완료`, 'success')
      setExchangeActionItem(null)
      setReshipStep(false)
      setReshipForm({ tracking_number: '', shipping_company: '롯데택배' })
      load()
    } catch (e) { showAlert(e instanceof Error ? e.message : `${labels[action]} 실패`, 'error') }
  }

  const handleCancelApprove = async (r: SambaReturn) => {
    const orderNum = r.order_number || r.order_id
    if (!orderNum) { showAlert('주문번호가 없습니다', 'error'); return }
    if (!await showConfirm(`${orderNum} 주문의 취소요청을 승인하시겠습니까?`)) return
    try {
      const order = await orderApi.findByOrderNumber(orderNum)
      if (!order) { showAlert('해당 주문을 찾을 수 없습니다', 'error'); return }
      const res = await orderApi.approveCancel(order.id)
      showAlert(res.message || '취소승인 완료', 'success')
      load()
    } catch (e) { showAlert(e instanceof Error ? e.message : '취소승인 실패', 'error') }
  }

  const handleReturnAction = async (r: SambaReturn, action: string) => {
    const orderNum = r.order_number || r.order_id
    if (!orderNum) { showAlert('주문번호가 없습니다', 'error'); return }
    const label = action === 'approve' ? '반품승인' : '반품거부'
    if (!await showConfirm(`${orderNum} 주문을 ${label} 처리하시겠습니까?`)) return
    try {
      const order = await orderApi.findByOrderNumber(orderNum)
      if (!order) { showAlert('해당 주문을 찾을 수 없습니다', 'error'); return }
      const res = await orderApi.returnAction(order.id, action)
      showAlert(res.message || `${label} 완료`, 'success')
      load()
    } catch (e) { showAlert(e instanceof Error ? e.message : `${label} 실패`, 'error') }
  }

  // 수익총액 계산 (정산금액 - 환수금액)
  const totalProfit = returns
    .reduce((sum, r) => sum + ((r.settlement_amount || 0) - (r.recovery_amount || 0)), 0)

  // completion_detail 기준 통계
  const completionCounts = {
    total: returns.length,
    requested: returns.filter(r => (r.completion_detail || COMPLETION_DEFAULT) === '대기중').length,
    completed: returns.filter(r => ['취소완료', '반품완료', '교환완료'].includes(r.completion_detail || '')).length,
    rejected: returns.filter(r => (r.completion_detail || '') === '거부').length,
  }

  return (
    <div style={{ color: '#E5E5E5' }}>
      {/* 숫자 input 스피너 제거 */}
      <style>{`
        input[type=number]::-webkit-outer-spin-button,
        input[type=number]::-webkit-inner-spin-button {
          -webkit-appearance: none;
          margin: 0;
        }
        input[type=number] {
          -moz-appearance: textfield;
          appearance: textfield;
        }
      `}</style>
      {/* 관련 페이지 연결 */}
      <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '0.5rem', marginBottom: '0.25rem' }}>
        <a href="/samba/orders" style={{ fontSize: '0.75rem', color: '#888', textDecoration: 'none' }}>← 주문</a>
        <a href="/samba/cs" style={{ fontSize: '0.75rem', color: '#4C9AFF', textDecoration: 'none' }}>CS →</a>
      </div>
      {/* 헤더 */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '1.5rem' }}>
        <div>
          <h2 style={{ fontSize: '1.5rem', fontWeight: 700, color: '#E5E5E5', marginBottom: '0.25rem' }}>반품교환</h2>
          <p style={{ fontSize: '0.875rem', color: '#888' }}>반품교환 요청을 관리</p>
        </div>
      </div>

      {/* 통계 카드 */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(5, 1fr)', gap: '1rem', marginBottom: '1.5rem' }}>
        {[
          { key: 'total', label: '전체', color: '#FF8C00' },
          { key: 'requested', label: '진행내역', color: '#FFD93D' },
          { key: 'completed', label: '완료됨', color: '#51CF66' },
          { key: 'rejected', label: '거절됨', color: '#FF6B6B' },
        ].map(({ key, label, color }) => (
          <div key={key} style={{ ...card, padding: '1rem 1.25rem' }}>
            <p style={{ fontSize: '0.75rem', color: '#666', marginBottom: '0.375rem' }}>{label}</p>
            <p style={{ fontSize: '1.5rem', fontWeight: 700, color }}>{fmtNum(completionCounts[key as keyof typeof completionCounts] ?? 0)}{key === 'requested' ? '건' : ''}</p>
          </div>
        ))}
        {/* 수익총액 통계 */}
        <div style={{ ...card, padding: '1rem 1.25rem', border: `1px solid ${totalProfit >= 0 ? 'rgba(81,207,102,0.2)' : 'rgba(255,107,107,0.2)'}` }}>
          <p style={{ fontSize: '0.75rem', color: '#666', marginBottom: '0.375rem' }}>수익총액</p>
          <p style={{ fontSize: '1.25rem', fontWeight: 700, color: totalProfit >= 0 ? '#51CF66' : '#FF6B6B' }}>₩{fmtNum(totalProfit)}</p>
        </div>
      </div>

      {/* 로그 영역 */}
      <div style={{ border: '1px solid #1C2333', borderRadius: '8px', overflow: 'hidden', marginBottom: '0.75rem' }}>
        <div style={{ padding: '6px 14px', background: '#0D1117', borderBottom: '1px solid #1C2333', display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <span style={{ fontSize: '0.8rem', fontWeight: 600, color: '#94A3B8' }}>반품교환 로그</span>
          <div style={{ display: 'flex', gap: '4px' }}>
            <button onClick={() => navigator.clipboard.writeText(logMessages.join('\n'))} style={{ fontSize: '0.72rem', color: '#555', background: 'transparent', border: '1px solid #1C2333', padding: '1px 8px', borderRadius: '4px', cursor: 'pointer' }}>복사</button>
            <button onClick={() => setLogMessages(['[대기] 반품교환 가져오기 결과가 여기에 표시됩니다...'])} style={{ fontSize: '0.72rem', color: '#555', background: 'transparent', border: '1px solid #1C2333', padding: '1px 8px', borderRadius: '4px', cursor: 'pointer' }}>초기화</button>
          </div>
        </div>
        <div ref={logRef} style={{ height: '144px', overflowY: 'auto', padding: '8px 14px', fontFamily: "'Courier New', monospace", fontSize: '0.788rem', color: '#8A95B0', background: '#080A10', lineHeight: 1.8 }}>
          {logMessages.map((msg, i) => <p key={i} style={{ color: '#8A95B0', fontSize: 'inherit', margin: 0 }}>{fmtTextNumbers(msg)}</p>)}
        </div>
      </div>

      {/* 기간 필터 바 */}
      <div style={{ background: 'rgba(18,18,18,0.98)', border: '1px solid #232323', borderRadius: '10px', padding: '0.625rem 0.875rem', marginBottom: '0.75rem', display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: '0.5rem' }}>
        <div style={{ display: 'flex', gap: '4px', flexWrap: 'nowrap', alignItems: 'center' }}>
          {PERIOD_BUTTONS.map(pb => (
            <button key={pb.key} onClick={() => {
              if (dateLocked) return
              setPeriod(pb.key)
              if (!startLocked) {
                const start = getPeriodStart(pb.key)
                setCustomStart(start ? start.toLocaleDateString('sv-SE') : '')
              }
              setCustomEnd(getPeriodEnd(pb.key).toLocaleDateString('sv-SE'))
            }}
              style={{ padding: '0.22rem 0.55rem', borderRadius: '5px', fontSize: '0.75rem', background: period === pb.key ? 'rgba(80,80,80,0.8)' : 'rgba(50,50,50,0.8)', border: period === pb.key ? '1px solid #666' : '1px solid #3D3D3D', color: period === pb.key ? '#fff' : '#C5C5C5', cursor: dateLocked ? 'not-allowed' : 'pointer', whiteSpace: 'nowrap', opacity: dateLocked && period !== pb.key ? 0.5 : 1 }}
            >{pb.label}</button>
          ))}
          <span style={{ width: '1px', background: '#333', height: '18px', margin: '0 4px' }} />
          <input type="date" value={customStart} onChange={e => setCustomStart(e.target.value)} style={{ ...inputStyle, padding: '0.22rem 0.4rem', fontSize: '0.75rem', ...(startLocked ? { borderColor: '#C0392B', color: '#FF8C00' } : {}) }} />
          <button onClick={() => setStartLocked(p => !p)} style={{ padding: '0.22rem 0.5rem', fontSize: '0.72rem', borderRadius: '4px', cursor: 'pointer', whiteSpace: 'nowrap', background: startLocked ? '#8B1A1A' : 'rgba(50,50,50,0.8)', border: startLocked ? '1px solid #C0392B' : '1px solid #3D3D3D', color: startLocked ? '#fff' : '#C5C5C5' }}>고정</button>
          <span style={{ color: '#555', fontSize: '0.75rem' }}>~</span>
          <input type="date" value={customEnd} onChange={e => setCustomEnd(e.target.value)} style={{ ...inputStyle, padding: '0.22rem 0.4rem', fontSize: '0.75rem' }} />
          <button onClick={() => setDateLocked(p => !p)} style={{ padding: '0.22rem 0.5rem', fontSize: '0.72rem', borderRadius: '4px', cursor: 'pointer', whiteSpace: 'nowrap', background: dateLocked ? '#8B1A1A' : 'rgba(50,50,50,0.8)', border: dateLocked ? '1px solid #C0392B' : '1px solid #3D3D3D', color: dateLocked ? '#fff' : '#C5C5C5' }}>고정</button>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '4px', flexShrink: 0 }}>
          <select value={syncAccountId} onChange={e => setSyncAccountId(e.target.value)} style={{ ...inputStyle, padding: '0.22rem 0.4rem', fontSize: '0.72rem', minWidth: '200px' }}>
            <option value="">전체마켓보기</option>
            {(() => {
              const marketTypes = [...new Map(accounts.map(a => [a.market_type, a.market_name])).entries()]
              return marketTypes.flatMap(([type, name]) => [
                <option key={`type:${type}`} value={`type:${type}`}>{name}</option>,
                ...accounts
                  .filter(a => a.market_type === type)
                  .map(a => <option key={a.id} value={a.id}>- {getAccountOptionLabel(a)}</option>),
              ])
            })()}
          </select>
          <button onClick={loadReturns} style={{ padding: '0.22rem 0.65rem', fontSize: '0.75rem', background: 'rgba(50,50,50,0.9)', border: '1px solid #3D3D3D', color: '#C5C5C5', borderRadius: '4px', cursor: 'pointer', whiteSpace: 'nowrap' }}>가져오기</button>
        </div>
      </div>

      {/* 필터 바 */}
      <div style={{ background: 'rgba(18,18,18,0.98)', border: '1px solid #232323', borderRadius: '10px', padding: '0.75rem 1rem', marginBottom: '1rem', display: 'flex', alignItems: 'center', gap: '6px', flexWrap: 'nowrap' }}>
        <select style={{ ...inputStyle, width: '80px', padding: '0.22rem 0.4rem', fontSize: '0.75rem' }} value={searchCategory} onChange={e => setSearchCategory(e.target.value)}>
          <option value="product">상품</option>
          <option value="customer">고객</option>
          <option value="product_id">상품번호</option>
          <option value="order_number">주문번호</option>
        </select>
        <input style={{ ...inputStyle, width: '140px', padding: '0.22rem 0.4rem', fontSize: '0.75rem' }} value={searchText} onChange={e => setSearchText(e.target.value)} onKeyDown={e => { if (e.key === 'Enter') loadReturns() }} />
        <button onClick={loadReturns} style={{ background: 'linear-gradient(135deg,#FF8C00,#FFB84D)', color: '#fff', padding: '0.22rem 0.75rem', borderRadius: '5px', fontSize: '0.75rem', border: 'none', cursor: 'pointer', whiteSpace: 'nowrap' }}>검색</button>
        <button
          onClick={handleBatchDelete}
          style={{ padding: '0.22rem 0.6rem', fontSize: '0.75rem', background: 'transparent', border: '1px solid #FF6B6B33', borderRadius: '4px', color: '#FF6B6B', cursor: 'pointer', whiteSpace: 'nowrap' }}
        >
          선택삭제
        </button>
        <div style={{ display: 'flex', gap: '4px', marginLeft: 'auto', flexShrink: 0, alignItems: 'center' }}>
          <select style={{ ...inputStyle, width: '130px', padding: '0.22rem 0.4rem', fontSize: '0.75rem' }} value={marketFilter} onChange={e => setMarketFilter(e.target.value)}>
            <option value="">전체마켓보기</option>
            {(() => {
              const marketTypes = [...new Map(accounts.map(a => [a.market_type, a.market_name])).entries()]
              return marketTypes.flatMap(([type, name]) => [
                <option key={`type:${type}`} value={`type:${type}`}>{name}</option>,
                ...accounts
                  .filter(a => a.market_type === type)
                  .map(a => <option key={`acc:${a.id}`} value={`acc:${a.id}`}>- {getAccountOptionLabel(a)}</option>),
              ])
            })()}
          </select>
          <select style={{ ...inputStyle, width: '110px', padding: '0.22rem 0.4rem', fontSize: '0.75rem' }} value={siteFilter} onChange={e => setSiteFilter(e.target.value)}><option value="">전체내역</option>{COMPLETION_OPTIONS.map(s => <option key={s} value={s}>{s}</option>)}</select>
          <select style={{ ...inputStyle, width: '92px', padding: '0.22rem 0.4rem', fontSize: '0.75rem' }} value={pageSize} onChange={e => setPageSize(Number(e.target.value))}>
            <option value={50}>50개 보기</option><option value={100}>100개 보기</option><option value={200}>200개 보기</option><option value={500}>500개 보기</option>
          </select>
        </div>
      </div>

      {/* 등록 폼 */}
      {showForm && (
        <div style={{ ...card, padding: '1.5rem', marginBottom: '1rem' }}>
          <h3 style={{ fontSize: '1rem', fontWeight: 600, marginBottom: '1rem' }}>반품/교환 등록</h3>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '0.75rem', marginBottom: '1rem' }}>
            <div>
              <label style={{ fontSize: '0.75rem', color: '#888', marginBottom: '0.375rem', display: 'block' }}>주문 ID</label>
              <input style={inputStyle} value={form.order_id} onChange={(e) => setForm({ ...form, order_id: e.target.value })} />
            </div>
            <div>
              <label style={{ fontSize: '0.75rem', color: '#888', marginBottom: '0.375rem', display: 'block' }}>유형</label>
              <select style={inputStyle} value={form.type} onChange={(e) => setForm({ ...form, type: e.target.value })}>
                <option value='return'>반품</option>
                <option value='exchange'>교환</option>
                <option value='cancel'>취소</option>
              </select>
            </div>
            {/* 반품사유 드롭다운 */}
            <div>
              <label style={{ fontSize: '0.75rem', color: '#888', marginBottom: '0.375rem', display: 'block' }}>사유 선택</label>
              <select
                style={inputStyle}
                value={form.reason}
                onChange={(e) => setForm({ ...form, reason: e.target.value })}
              >
                {RETURN_REASONS.map(r => (
                  <option key={r.value} value={r.value}>{r.label}</option>
                ))}
              </select>
            </div>
            {/* 직접입력 시 텍스트 필드 */}
            <div>
              <label style={{ fontSize: '0.75rem', color: '#888', marginBottom: '0.375rem', display: 'block' }}>
                {form.reason ? '추가 상세 사유' : '사유 직접입력'}
              </label>
              <input
                style={inputStyle}
                value={form.customReason}
                onChange={(e) => setForm({ ...form, customReason: e.target.value })}
                placeholder={form.reason ? '추가 설명 (선택)' : '반품/교환 사유를 입력하세요'}
              />
            </div>
            <div>
              <label style={{ fontSize: '0.75rem', color: '#888', marginBottom: '0.375rem', display: 'block' }}>수량</label>
              <input type='number' style={inputStyle} value={form.quantity} onChange={(e) => setForm({ ...form, quantity: Number(e.target.value) })} />
            </div>
            <div>
              <label style={{ fontSize: '0.75rem', color: '#888', marginBottom: '0.375rem', display: 'block' }}>요청 금액</label>
              <input type='number' style={inputStyle} value={form.requested_amount} onChange={(e) => setForm({ ...form, requested_amount: Number(e.target.value) })} />
            </div>
          </div>
          <div style={{ display: 'flex', gap: '0.75rem', justifyContent: 'flex-end' }}>
            <button onClick={() => setShowForm(false)} style={{ padding: '0.625rem 1.25rem', background: 'transparent', border: '1px solid #2D2D2D', borderRadius: '8px', color: '#888', fontSize: '0.875rem', cursor: 'pointer' }}>취소</button>
            <button onClick={handleSubmit} style={{ padding: '0.625rem 1.25rem', background: '#FF8C00', border: 'none', borderRadius: '8px', color: '#fff', fontSize: '0.875rem', fontWeight: 600, cursor: 'pointer' }}>저장</button>
          </div>
        </div>
      )}

      {/* 테이블 */}
      <div style={card}>
        <div style={{ overflowX: 'auto' }}>
          {loading ? (
            <div style={{ padding: '3rem', textAlign: 'center', color: '#555' }}>로딩 중...</div>
          ) : (
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.875rem' }}>
              <thead>
                <tr style={{ background: 'rgba(255,255,255,0.03)', borderBottom: '1px solid #1E1E1E' }}>
                  <th style={{ width: '36px', textAlign: 'center', padding: '0.3rem 0.5rem', verticalAlign: 'middle' }}>
                    <input
                      type="checkbox"
                      checked={returns.length > 0 && selectedIds.size === returns.length}
                      onChange={(e) => {
                        if (e.target.checked) setSelectedIds(new Set(returns.map(r => r.id)))
                        else setSelectedIds(new Set())
                      }}
                      style={{ width: '13px', height: '13px', cursor: 'pointer', accentColor: '#F59E0B' }}
                    />
                  </th>
                  {['사진', '고객', '사업자', '주문번호', '마켓', '주문일', '고객', '회사', '완료내역', '상품명', '체크날짜', '고객전화번호', '지역', '메모', '반품링크', 'CS접수일', '상품위치', '반품신청한곳', '상태', '고객주문', '원주문'].map((h, i) => (
                    <th key={i} style={{ textAlign: 'center', padding: '0.5rem 0.625rem', color: '#888', fontWeight: 500, fontSize: '0.75rem', whiteSpace: 'nowrap' }}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {returns.filter(r => {
                  if (siteFilter && (r.completion_detail || COMPLETION_DEFAULT) !== siteFilter) return false
                  if (marketFilter) {
                    if (marketFilter.startsWith('type:')) {
                      const mType = marketFilter.replace('type:', '')
                      const mName = accounts.find(a => a.market_type === mType)?.market_name || ''
                      if (mName && !r.market?.includes(mName)) return false
                    } else if (marketFilter.startsWith('acc:')) {
                      const accId = marketFilter.replace('acc:', '')
                      const acc = accounts.find(a => a.id === accId)
                      if (acc && !r.market?.includes(acc.market_name || '')) return false
                    }
                  }
                  return true
                }).map((r, idx) => {
                  return (
                      <tr key={r.id} style={{ borderBottom: '1px solid rgba(45,45,45,0.5)' }}>
                        <td style={{ width: '36px', textAlign: 'center', padding: '0.5rem', verticalAlign: 'middle' }}>
                          <div style={{ fontSize: '0.675rem', color: '#666', marginBottom: '2px' }}>{idx + 1}</div>
                          <input
                            type="checkbox"
                            checked={selectedIds.has(r.id)}
                            onChange={(e) => {
                              const next = new Set(selectedIds)
                              if (e.target.checked) next.add(r.id)
                              else next.delete(r.id)
                              setSelectedIds(next)
                            }}
                            style={{ width: '13px', height: '13px', cursor: 'pointer', accentColor: '#F59E0B' }}
                          />
                        </td>
                        <td style={{ padding: '0.625rem 0.5rem', textAlign: 'center', verticalAlign: 'middle' }}>
                        {r.product_image ? (
                          <img
                            src={r.product_image}
                            alt=""
                            onClick={() => r.return_link && window.open(r.return_link, '_blank')}
                            style={{ width: '60px', height: '60px', objectFit: 'cover', borderRadius: '6px', border: '1px solid #2D2D2D', cursor: r.return_link ? 'pointer' : 'default', display: 'block', margin: '0 auto' }}
                          />
                        ) : (
                          <div
                            onClick={() => r.return_link && window.open(r.return_link, '_blank')}
                            style={{ width: '60px', height: '60px', background: '#1A1A1A', borderRadius: '6px', border: '1px solid #2D2D2D', display: 'flex', alignItems: 'center', justifyContent: 'center', color: r.return_link ? '#4C9AFF' : '#444', fontSize: '0.625rem', cursor: r.return_link ? 'pointer' : 'default', textDecoration: r.return_link ? 'underline' : 'none', margin: '0 auto' }}
                          >
                            {r.return_link ? '링크' : 'No IMG'}
                          </div>
                        )}
                      </td>
                      <td style={tdCenter}>{r.customer_name || '-'}</td>
                      <td style={tdCenter}>{r.business_name || '-'}</td>
                      <td style={{ ...tdCenter, padding: '0.625rem' }}>
                        <button onClick={() => setDetailItem(r)} style={{ background: 'none', border: 'none', color: '#E5E5E5', cursor: 'pointer', fontSize: '0.8125rem', fontWeight: 400 }}>{r.order_number || r.order_id || '-'}</button>
                      </td>
                      <td style={tdCenter}>
                        <span>{r.market || '-'}</span>
                      </td>
                      <td style={{ ...tdCenter, color: '#888' }}>{fmtMD(r.order_date)}</td>
                      <td style={{ ...tdCenter, padding: '0.375rem' }}>
                        <input
                          type="text"
                          value={customerAmounts[r.id] || ''}
                          placeholder=""
                          onChange={(e) => setCustomerAmounts(prev => ({ ...prev, [r.id]: e.target.value }))}
                          style={{ width: '80px', padding: '0.3rem 0.5rem', background: '#1A1A1A', border: '1px solid #2D2D2D', borderRadius: '4px', color: '#E5E5E5', fontSize: '0.8rem', textAlign: 'right' }}
                        />
                      </td>
                      <td style={{ ...tdCenter, padding: '0.375rem' }}>
                        <input
                          type="text"
                          value={companyAmounts[r.id] || ''}
                          placeholder=""
                          onChange={(e) => setCompanyAmounts(prev => ({ ...prev, [r.id]: e.target.value }))}
                          style={{ width: '80px', padding: '0.3rem 0.5rem', background: '#1A1A1A', border: '1px solid #2D2D2D', borderRadius: '4px', color: '#E5E5E5', fontSize: '0.8rem', textAlign: 'right' }}
                        />
                      </td>
                      <td style={{ ...tdCenter, padding: '0.375rem' }}>
                        {(() => {
                          const cd = r.completion_detail || COMPLETION_DEFAULT
                          const cc = COMPLETION_COLORS[cd]
                          return (
                        <select
                          value={cd}
                          onChange={async (e) => {
                            const val = e.target.value
                            setReturns(prev => prev.map(x => x.id === r.id ? { ...x, completion_detail: val } : x))
                            try {
                              await returnApi.patch(r.id, { completion_detail: val })
                            } catch (_e) { /* 무시 */ }
                          }}
                          style={{ padding: '0.2rem 0.3rem', background: cc?.bg || '#1A1A1A', border: '1px solid #2D2D2D', borderRadius: '4px', color: cc?.fg || '#E5E5E5', fontSize: '0.75rem', fontWeight: 600, cursor: 'pointer', outline: 'none' }}
                        >
                          {COMPLETION_OPTIONS.map(o => <option key={o} value={o}>{o}</option>)}
                        </select>
                          )
                        })()}
                      </td>
                      <td style={{ ...tdCenter, maxWidth: '150px', overflow: 'hidden', textOverflow: 'ellipsis' }}>{r.product_name || '-'}</td>
                      <td style={{ ...tdCenter, padding: '0.375rem' }}>
                        <div
                          onClick={() => {
                            const inp = document.getElementById(`ck-${r.id}`) as HTMLInputElement
                            inp?.showPicker?.()
                          }}
                          style={{ cursor: 'pointer', fontSize: '0.8rem', color: r.check_date ? '#E5E5E5' : '#555', minWidth: '40px' }}
                        >
                          {fmtMD(r.check_date)}
                        </div>
                        <input
                          id={`ck-${r.id}`}
                          type="date"
                          value={r.check_date?.slice(0, 10) || ''}
                          onChange={async (e) => {
                            const val = e.target.value
                            setReturns(prev => prev.map(x => x.id === r.id ? { ...x, check_date: val } : x))
                            try {
                              await returnApi.patch(r.id, { check_date: val || '' })
                            } catch (_e) { /* 무시 */ }
                          }}
                          style={{ width: 0, height: 0, opacity: 0, position: 'absolute', pointerEvents: 'none' }}
                        />
                      </td>
                      <td style={tdCenter}>{r.customer_phone || '-'}</td>
                      <td style={tdCenter}>
                        {r.region ? (
                          <span
                            onClick={() => setAddressModal({ region: r.region || '', address: r.customer_address || '', phone: r.customer_phone || '', customer: r.customer_name || '' })}
                            style={{ color: '#E5E5E5', cursor: 'pointer', textDecoration: 'underline', textDecorationColor: '#3D3D3D', textUnderlineOffset: '3px' }}
                            title={r.customer_address || '주소 정보 없음'}
                          >{r.region}</span>
                        ) : '-'}
                      </td>
                      <td style={{ ...tdCenter, padding: '0.375rem' }}>
                        <input
                          type="text"
                          value={r.memo || ''}
                          placeholder=""
                          onChange={(e) => {
                            const val = e.target.value
                            setReturns(prev => prev.map(x => x.id === r.id ? { ...x, memo: val } : x))
                          }}
                          onBlur={async (e) => {
                            try {
                              await returnApi.patch(r.id, { memo: e.target.value })
                            } catch (_e) { /* 무시 */ }
                          }}
                          style={{ width: '100px', padding: '0.3rem 0.5rem', background: '#1A1A1A', border: '1px solid #2D2D2D', borderRadius: '4px', color: '#E5E5E5', fontSize: '0.8rem', textAlign: 'center' }}
                        />
                      </td>
                      <td style={tdCenter}>
                        {r.return_link ? <a href={r.return_link} target="_blank" rel="noopener noreferrer" style={{ color: '#4C9AFF', textDecoration: 'none' }}>링크</a> : '-'}
                      </td>
                      <td style={{ ...tdCenter, color: '#888' }}>{fmtMD(r.return_request_date || r.created_at)}</td>
                      <td style={{ ...tdCenter, padding: '0.375rem' }}>
                        <select
                          value={r.product_location || '고객'}
                          onChange={async (e) => {
                            const val = e.target.value
                            setReturns(prev => prev.map(x => x.id === r.id ? { ...x, product_location: val } : x))
                            try {
                              await returnApi.patch(r.id, { product_location: val })
                            } catch (_e) { /* 무시 */ }
                          }}
                          style={{ padding: '0.2rem 0.3rem', background: '#1A1A1A', border: '1px solid #2D2D2D', borderRadius: '4px', color: '#E5E5E5', fontSize: '0.75rem', cursor: 'pointer', outline: 'none' }}
                        >
                          <option value="고객">고객</option>
                          <option value="사무실">사무실</option>
                          <option value="원주문">원주문</option>
                          <option value="배송미완료">배송미완료</option>
                        </select>
                      </td>
                      <td style={{ ...tdCenter, padding: '0.375rem' }}>
                        <select value={r.return_source || '원주문'} onChange={async (e) => {
                          try {
                            await returnApi.patch(r.id, { return_source: e.target.value })
                            loadReturns()
                          } catch {}
                        }} style={{ fontSize: '0.72rem', padding: '2px 4px', background: '#1A1A1A', border: '1px solid #2D2D2D', borderRadius: '4px', color: '#E5E5E5', cursor: 'pointer' }}>
                          <option value="원주문">원주문</option>
                          <option value="홈픽">홈픽</option>
                          <option value="자동회수">자동회수</option>
                        </select>
                      </td>
                      <td style={{ ...tdCenter, padding: '0.375rem' }}>
                        <select
                          value={r.status}
                          onChange={async (e) => {
                            const val = e.target.value
                            try {
                              await returnApi.patch(r.id, { status: val })
                              setReturns(prev => prev.map(x => x.id === r.id ? { ...x, status: val } : x))
                            } catch (_e) { /* 무시 */ }
                          }}
                          style={{ padding: '0.2rem 0.3rem', background: '#1A1A1A', border: '1px solid #2D2D2D', borderRadius: '4px', color: '#E5E5E5', fontSize: '0.75rem', cursor: 'pointer', outline: 'none' }}
                        >
                          <option value="not_collected">미수거</option>
                          <option value="collecting">수거중</option>
                          <option value="collected">수거완료</option>
                        </select>
                      </td>
                      <td style={{ ...tdCenter, padding: '0.375rem' }}>
                        <select
                          value={r.customer_order_no || 'return_incomplete'}
                          onChange={async (e) => {
                            const val = e.target.value
                            try {
                              await returnApi.patch(r.id, { customer_order_no: val })
                              setReturns(prev => prev.map(x => x.id === r.id ? { ...x, customer_order_no: val } : x))
                            } catch (_e) { /* 무시 */ }
                          }}
                          style={{ padding: '0.2rem 0.3rem', background: '#1A1A1A', border: '1px solid #2D2D2D', borderRadius: '4px', color: '#E5E5E5', fontSize: '0.75rem', cursor: 'pointer', outline: 'none' }}
                        >
                          <option value="return_incomplete">미완료</option>
                          <option value="return_complete">완료</option>
                        </select>
                      </td>
                      <td style={{ ...tdCenter, padding: '0.375rem' }}>
                        <select
                          value={r.original_order_no || 'return_incomplete'}
                          onChange={async (e) => {
                            const val = e.target.value
                            try {
                              await returnApi.patch(r.id, { original_order_no: val })
                              setReturns(prev => prev.map(x => x.id === r.id ? { ...x, original_order_no: val } : x))
                            } catch (_e) { /* 무시 */ }
                          }}
                          style={{ padding: '0.2rem 0.3rem', background: '#1A1A1A', border: '1px solid #2D2D2D', borderRadius: '4px', color: '#E5E5E5', fontSize: '0.75rem', cursor: 'pointer', outline: 'none' }}
                        >
                          <option value="return_incomplete">미완료</option>
                          <option value="return_complete">완료</option>
                        </select>
                      </td>
                      </tr>
                  )
                })}
                {returns.length === 0 && (
                  <tr><td colSpan={22} style={{ padding: '3rem', textAlign: 'center', color: '#555' }}>반품/교환 내역이 없습니다</td></tr>
                )}
              </tbody>
            </table>
          )}
        </div>
      </div>

      {/* 거절 사유 입력 모달 */}
      {rejectModal && (
        <div style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.7)', backdropFilter: 'blur(4px)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 100 }}>
          <div style={{ background: '#1A1A1A', border: '1px solid #2D2D2D', borderRadius: '16px', padding: '2rem', width: '400px', maxWidth: '90vw' }}>
            <h3 style={{ fontSize: '1.125rem', fontWeight: 700, color: '#E5E5E5', marginBottom: '1rem' }}>거절 사유 입력</h3>
            <input
              style={inputStyle}
              placeholder="거절 사유를 입력하세요"
              value={rejectModal.reason}
              onChange={e => setRejectModal({ ...rejectModal, reason: e.target.value })}
              onKeyDown={e => e.key === 'Enter' && submitReject()}
              autoFocus
            />
            <div style={{ display: 'flex', gap: '0.75rem', justifyContent: 'flex-end', marginTop: '1rem' }}>
              <button onClick={() => setRejectModal(null)} style={{ padding: '0.625rem 1.25rem', background: 'transparent', border: '1px solid #2D2D2D', borderRadius: '8px', color: '#888', fontSize: '0.875rem', cursor: 'pointer' }}>취소</button>
              <button onClick={submitReject} style={{ padding: '0.625rem 1.25rem', background: '#FF6B6B', border: 'none', borderRadius: '8px', color: '#fff', fontSize: '0.875rem', fontWeight: 600, cursor: 'pointer' }}>거절</button>
            </div>
          </div>
        </div>
      )}

{/* 고객 주소 보기 모달 */}
      {addressModal && (
        <div onClick={() => setAddressModal(null)} style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.7)', backdropFilter: 'blur(4px)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 100 }}>
          <div onClick={e => e.stopPropagation()} style={{ background: '#1A1A1A', border: '1px solid #2D2D2D', borderRadius: '16px', padding: '1.75rem', width: '460px', maxWidth: '90vw' }}>
            <h3 style={{ fontSize: '1.125rem', fontWeight: 700, color: '#E5E5E5', marginBottom: '1rem' }}>고객 주소</h3>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '0.6rem', marginBottom: '1.25rem' }}>
              {addressModal.customer && (
                <div style={{ display: 'flex', gap: '0.75rem', fontSize: '0.85rem' }}>
                  <span style={{ color: '#888', minWidth: '64px' }}>고객명</span>
                  <span style={{ color: '#E5E5E5' }}>{addressModal.customer}</span>
                </div>
              )}
              {addressModal.phone && (
                <div style={{ display: 'flex', gap: '0.75rem', fontSize: '0.85rem' }}>
                  <span style={{ color: '#888', minWidth: '64px' }}>전화</span>
                  <span style={{ color: '#E5E5E5' }}>{addressModal.phone}</span>
                </div>
              )}
              <div style={{ display: 'flex', gap: '0.75rem', fontSize: '0.85rem' }}>
                <span style={{ color: '#888', minWidth: '64px' }}>지역</span>
                <span style={{ color: '#E5E5E5' }}>{addressModal.region || '-'}</span>
              </div>
              <div style={{ padding: '0.75rem', background: '#111', border: '1px solid #2D2D2D', borderRadius: '8px', fontSize: '0.85rem', color: '#4C9AFF', lineHeight: 1.5 }}>
                <div style={{ color: '#888', fontSize: '0.72rem', marginBottom: '0.25rem' }}>전체 주소</div>
                {addressModal.address || '주소 정보 없음'}
              </div>
            </div>
            <div style={{ display: 'flex', gap: '0.75rem', justifyContent: 'flex-end' }}>
              {addressModal.address && (
                <button onClick={() => { navigator.clipboard.writeText(addressModal.address); showAlert('주소가 복사되었습니다', 'success') }} style={{ padding: '0.55rem 1.1rem', background: 'transparent', border: '1px solid #2D2D2D', borderRadius: '8px', color: '#C5C5C5', fontSize: '0.85rem', cursor: 'pointer' }}>복사</button>
              )}
              <button onClick={() => setAddressModal(null)} style={{ padding: '0.55rem 1.1rem', background: '#FF8C00', border: 'none', borderRadius: '8px', color: '#fff', fontSize: '0.85rem', fontWeight: 600, cursor: 'pointer' }}>닫기</button>
            </div>
          </div>
        </div>
      )}

      {/* 상품위치 수정 모달 */}      {locationModal && (        <div style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.7)', backdropFilter: 'blur(4px)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 100 }}>          <div style={{ background: '#1A1A1A', border: '1px solid #2D2D2D', borderRadius: '16px', padding: '2rem', width: '420px', maxWidth: '90vw' }}>            <h3 style={{ fontSize: '1.125rem', fontWeight: 700, color: '#E5E5E5', marginBottom: '1rem' }}>상품위치 수정</h3>            {locationModal.address && (              <div style={{ padding: '0.75rem', background: '#111', border: '1px solid #2D2D2D', borderRadius: '8px', marginBottom: '1rem', fontSize: '0.85rem', color: '#4C9AFF', lineHeight: 1.5 }}>                <span style={{ color: '#888', fontSize: '0.75rem' }}>전체 주소</span><br/>                {locationModal.address}              </div>            )}            <input style={inputStyle} placeholder="시/군/구 입력" value={locationModal.value} onChange={e => setLocationModal({ ...locationModal, value: e.target.value })} onKeyDown={async e => { if (e.key === 'Enter') { const val = locationModal.value.trim(); setReturns(prev => prev.map(x => x.id === locationModal.id ? { ...x, product_location: val } : x)); try { await returnApi.patch(locationModal.id, { product_location: val }) } catch (_e) { /* */ } setLocationModal(null) } }} autoFocus />            <div style={{ display: 'flex', gap: '0.75rem', justifyContent: 'flex-end', marginTop: '1rem' }}>              <button onClick={() => setLocationModal(null)} style={{ padding: '0.625rem 1.25rem', background: 'transparent', border: '1px solid #2D2D2D', borderRadius: '8px', color: '#888', fontSize: '0.875rem', cursor: 'pointer' }}>취소</button>              <button onClick={async () => { const val = locationModal.value.trim(); setReturns(prev => prev.map(x => x.id === locationModal.id ? { ...x, product_location: val } : x)); try { await returnApi.patch(locationModal.id, { product_location: val }) } catch (_e) { /* */ } setLocationModal(null) }} style={{ padding: '0.625rem 1.25rem', background: '#FF8C00', border: 'none', borderRadius: '8px', color: '#fff', fontSize: '0.875rem', fontWeight: 600, cursor: 'pointer' }}>저장</button>            </div>          </div>        </div>      )}
      {/* 교환 액션 선택 모달 */}
      {exchangeActionItem && (
        <div style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.7)', backdropFilter: 'blur(4px)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 100 }}>
          <div style={{ background: '#1A1A1A', border: '1px solid #2D2D2D', borderRadius: '16px', padding: '2rem', width: '380px', maxWidth: '90vw' }}>
            <h3 style={{ fontSize: '1.125rem', fontWeight: 700, color: '#E5E5E5', marginBottom: '0.5rem' }}>교환요청 처리</h3>
            <p style={{ fontSize: '0.8125rem', color: '#888', marginBottom: '1.5rem' }}>주문번호: {exchangeActionItem.order_number || exchangeActionItem.order_id || '-'}</p>
            {!reshipStep ? (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '0.75rem' }}>
                <button onClick={() => setReshipStep(true)} style={{ padding: '0.75rem', background: 'rgba(76,154,255,0.1)', border: '1px solid rgba(76,154,255,0.3)', borderRadius: '8px', color: '#4C9AFF', fontSize: '0.875rem', fontWeight: 600, cursor: 'pointer' }}>교환재배송</button>
                <button onClick={() => handleExchangeAction(exchangeActionItem, 'convert_return')} style={{ padding: '0.75rem', background: 'rgba(255,165,0,0.1)', border: '1px solid rgba(255,165,0,0.3)', borderRadius: '8px', color: '#FFA500', fontSize: '0.875rem', fontWeight: 600, cursor: 'pointer' }}>반품변경</button>
              </div>
            ) : (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '0.75rem' }}>
                <p style={{ fontSize: '0.8125rem', color: '#aaa', margin: 0 }}>재배송 송장 정보를 입력하세요 (롯데ON 필수)</p>
                <select
                  value={reshipForm.shipping_company}
                  onChange={e => setReshipForm(f => ({ ...f, shipping_company: e.target.value }))}
                  style={{ padding: '0.5rem 0.75rem', background: '#111', border: '1px solid #2D2D2D', borderRadius: '8px', color: '#E5E5E5', fontSize: '0.875rem' }}
                >
                  {['CJ대한통운','한진택배','롯데택배','로젠택배','우체국택배','경동택배','대신택배','일양로지스','딜리박스'].map(c => (
                    <option key={c} value={c}>{c}</option>
                  ))}
                </select>
                <input
                  placeholder="송장번호 입력"
                  value={reshipForm.tracking_number}
                  onChange={e => setReshipForm(f => ({ ...f, tracking_number: e.target.value }))}
                  style={{ padding: '0.5rem 0.75rem', background: '#111', border: '1px solid #2D2D2D', borderRadius: '8px', color: '#E5E5E5', fontSize: '0.875rem' }}
                />
                <button
                  onClick={() => handleExchangeAction(exchangeActionItem, 'reship', { tracking_number: reshipForm.tracking_number, shipping_company: reshipForm.shipping_company })}
                  style={{ padding: '0.75rem', background: 'rgba(76,154,255,0.15)', border: '1px solid rgba(76,154,255,0.4)', borderRadius: '8px', color: '#4C9AFF', fontSize: '0.875rem', fontWeight: 600, cursor: 'pointer' }}
                >재배송 처리</button>
              </div>
            )}
            <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: '1rem' }}>
              <button
                onClick={() => { setExchangeActionItem(null); setReshipStep(false); setReshipForm({ tracking_number: '', shipping_company: '롯데택배' }) }}
                style={{ padding: '0.625rem 1.25rem', background: 'transparent', border: '1px solid #2D2D2D', borderRadius: '8px', color: '#888', fontSize: '0.875rem', cursor: 'pointer' }}
              >닫기</button>
            </div>
          </div>
        </div>
      )}

      <ReturnDetailModal detailItem={detailItem} onClose={() => setDetailItem(null)} />
    </div>
  )
}
