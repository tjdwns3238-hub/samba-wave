'use client'

import { useEffect, useState, useCallback, useRef } from 'react'
import { useSearchParams } from 'next/navigation'
import { orderApi, accountApi } from '@/lib/samba/api/commerce'
import { returnApi, csInquiryApi, type SambaCSInquiry, type CSReplyTemplate, type CSSyncResultItem } from '@/lib/samba/api/support'
import type { SambaMarketAccount } from '@/lib/samba/api/commerce'

import { showAlert, showConfirm } from '@/components/samba/Modal'

/** HTML 태그를 줄바꿈으로 변환 후 제거 — CS 문의 텍스트를 깔끔하게 표시 */
function htmlToText(html: string): string {
  if (!html) return ''
  return html
    .replace(/<\/p>/gi, '\n')
    .replace(/<br\s*\/?>/gi, '\n')
    .replace(/<[^>]+>/g, '')
    .replace(/&nbsp;/g, ' ')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&amp;/g, '&')
    .replace(/\n{3,}/g, '\n\n')
    .trim()
}
import { card, inputStyle, fmtNum, fmtTextNumbers } from '@/lib/samba/styles'
import { PERIOD_BUTTONS } from '@/lib/samba/constants'
import { fmtDate, fmtTime, getPeriodStart, getPeriodEnd } from '@/lib/samba/utils'

import { REPLY_STATUS_MAP, INQUIRY_TYPE_MAP } from './constants'


function renderCsLogMessage(message: string) {
  const formatted = fmtTextNumbers(message)
  const parts = formatted.split(/(\d[\d,]*)(건)/g)

  if (parts.length === 1) return formatted

  return parts.map((part, index) => {
    const isCount = index % 3 === 1
    if (isCount && Number(part.replace(/,/g, '')) > 0) {
      return <span key={`${part}-${index}`} style={{ color: '#FFFFFF', fontWeight: 700 }}>{part}</span>
    }
    return <span key={`${part}-${index}`}>{part}</span>
  })
}



export default function CSPage() {
  useEffect(() => { document.title = 'SAMBA-CS관리' }, [])
  // 데이터
  const [inquiries, setInquiries] = useState<SambaCSInquiry[]>([])
  const [total, setTotal] = useState(0)
  const [, setStats] = useState<Record<string, unknown>>({})
  const [templates, setTemplates] = useState<Record<string, CSReplyTemplate>>({})
  const [loading, setLoading] = useState(true)

  // 필터
  const [filterMarket, setFilterMarket] = useState('')
  const [filterType] = useState('')
  // 상품평 필터: '' = 전체보기 / 'only' = 상품평 / 'exclude' = 상품평제외
  const [filterReview, setFilterReview] = useState('')
  const [filterStatus, setFilterStatus] = useState('pending')
  const [search, setSearch] = useState('')
  const [searchInput, setSearchInput] = useState('')
  const [sortDesc, setSortDesc] = useState(true)
  const [pageSize, setPageSize] = useState(30)
  const [page, setPage] = useState(0)

  // 주문관리에서 `/samba/cs?search=주문번호` 로 새 탭 진입 시 해당 주문 CS 자동 검색
  // (마운트 1회만 시드 — 이후 사용자가 검색창을 비우면 정상 동작)
  const searchParams = useSearchParams()
  const seededRef = useRef(false)
  useEffect(() => {
    if (seededRef.current) return
    const q = searchParams.get('search')?.trim()
    if (q) {
      seededRef.current = true
      setSearchInput(q)
      setSearch(q)
      // 기본 filterStatus 가 'pending' 이라 답변완료 건이 숨음 — 쿼리 진입 시에만 전체로 풀어 상태 무관 노출
      setFilterStatus('')
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // 로그 + 기간 + 필터 추가 상태
  const [csLogMessages, setCsLogMessages] = useState<string[]>(['[대기] CS 문의 가져오기 결과가 여기에 표시됩니다...'])
  const [csPeriod, setCsPeriod] = useState('1week')
  const [csSyncAccountId, setCsSyncAccountId] = useState('')
  const [accounts, setAccounts] = useState<SambaMarketAccount[]>([])
  const [csCustomStart, setCsCustomStart] = useState((getPeriodStart('1week') ?? new Date()).toLocaleDateString('sv-SE'))
  const [csCustomEnd, setCsCustomEnd] = useState(getPeriodEnd('1week').toLocaleDateString('sv-SE'))
  const [csStartLocked, setCsStartLocked] = useState(false)
  const [csDateLocked, setCsDateLocked] = useState(false)
  const [csSiteFilter, setCsSiteFilter] = useState('')

  // 선택
  const [selected, setSelected] = useState<Set<string>>(new Set())

  // 답변 모달
  const [replyModal, setReplyModal] = useState<SambaCSInquiry | null>(null)
  const [replyText, setReplyText] = useState('')
  const [selectedTemplate, setSelectedTemplate] = useState('')

  // 교환/취소 액션 모달
  const [exchangeActionItem, setExchangeActionItem] = useState<SambaCSInquiry | null>(null)
  // 11번가 교환 거부 사유 입력 모달
  const [rejectReasonModal, setRejectReasonModal] = useState(false)
  const [rejectReasonText, setRejectReasonText] = useState('')
  const [rejectTargetItem, setRejectTargetItem] = useState<SambaCSInquiry | null>(null)

  // 11번가 교환 승인: returnApi 사용
  const handleElevenstExchangeApprove = async (item: SambaCSInquiry) => {
    if (!item.market_order_id) { showAlert('주문번호가 없습니다', 'error'); return }
    if (!await showConfirm(`${item.market_order_id} 주문의 교환을 승인(재배송) 하시겠습니까?`)) return
    try {
      const order = await orderApi.findByOrderNumber(item.market_order_id)
      if (!order) { showAlert('해당 주문을 찾을 수 없습니다', 'error'); return }
      const returns = await returnApi.list(order.id, undefined, 'exchange')
      const ret = returns[0]
      if (!ret) { showAlert('교환 신청 기록을 찾을 수 없습니다', 'error'); return }
      const res = await returnApi.exchangeAction(ret.id, 'approve')
      showAlert(res.message || '교환승인 완료', 'success')
      setExchangeActionItem(null)
    } catch (e) { showAlert(e instanceof Error ? e.message : '교환승인 실패', 'error') }
  }

  // 11번가 교환 거부: 사유 입력 후 처리
  const handleElevenstExchangeReject = (item: SambaCSInquiry) => {
    setRejectTargetItem(item)
    setRejectReasonText('')
    setRejectReasonModal(true)
  }

  const submitElevenstExchangeReject = async () => {
    if (!rejectTargetItem?.market_order_id) { showAlert('주문번호가 없습니다', 'error'); return }
    if (!rejectReasonText.trim()) { showAlert('거부 사유를 입력해 주세요', 'error'); return }
    try {
      const order = await orderApi.findByOrderNumber(rejectTargetItem.market_order_id)
      if (!order) { showAlert('해당 주문을 찾을 수 없습니다', 'error'); return }
      const returns = await returnApi.list(order.id, undefined, 'exchange')
      const ret = returns[0]
      if (!ret) { showAlert('교환 신청 기록을 찾을 수 없습니다', 'error'); return }
      const res = await returnApi.exchangeAction(ret.id, 'reject', rejectReasonText.trim())
      showAlert(res.message || '교환거부 완료', 'success')
      setRejectReasonModal(false)
      setExchangeActionItem(null)
    } catch (e) { showAlert(e instanceof Error ? e.message : '교환거부 실패', 'error') }
  }

  const handleExchangeAction = async (item: SambaCSInquiry, action: string) => {
    if (!item.market_order_id) {
      showAlert('주문번호가 없습니다', 'error')
      return
    }
    // 11번가 교환은 returnApi 사용 (reship=approve, reject=reject)
    if (item.market === '11st' || item.market === '11번가') {
      if (action === 'reship') { await handleElevenstExchangeApprove(item); return }
      if (action === 'reject') { handleElevenstExchangeReject(item); return }
    }
    // 기타 마켓(스마트스토어 등) — 기존 방식 유지
    const labels: Record<string, string> = { reship: '교환재배송', reject: '교환거부', convert_return: '반품변경' }
    if (!await showConfirm(`${item.market_order_id} 주문을 ${labels[action] || action} 처리하시겠습니까?`)) return
    try {
      const order = await orderApi.findByOrderNumber(item.market_order_id)
      if (!order) { showAlert('해당 주문을 찾을 수 없습니다', 'error'); return }
      const res = await orderApi.exchangeAction(order.id, action)
      showAlert(res.message || `${labels[action]} 완료`, 'success')
      setExchangeActionItem(null)
    } catch (e) { showAlert(e instanceof Error ? e.message : `${labels[action]} 실패`, 'error') }
  }

  const handleCancelApprove = async (item: SambaCSInquiry) => {
    if (!item.market_order_id) {
      showAlert('주문번호가 없습니다', 'error')
      return
    }
    if (!await showConfirm(`${item.market_order_id} 주문의 취소요청을 승인하시겠습니까?`)) return
    try {
      const order = await orderApi.findByOrderNumber(item.market_order_id)
      if (!order) { showAlert('해당 주문을 찾을 수 없습니다', 'error'); return }
      const res = await orderApi.approveCancel(order.id)
      showAlert(res.message || '취소승인 완료', 'success')
    } catch (e) { showAlert(e instanceof Error ? e.message : '취소승인 실패', 'error') }
  }

  // 인라인 템플릿 편집 모달 (SMS 템플릿과 동일한 UX)
  const [templateEditModal, setTemplateEditModal] = useState<{ key: string; name: string; content: string } | null>(null)
  const [isNewTemplate, setIsNewTemplate] = useState(false)
  const tplContentRef = useRef<HTMLTextAreaElement>(null)
  const replyTextRef = useRef<HTMLTextAreaElement>(null)

  const openNewTemplate = () => {
    setIsNewTemplate(true)
    setTemplateEditModal({ key: `t_${Date.now()}`, name: '', content: '' })
  }
  const openEditTemplate = (key: string, tpl: CSReplyTemplate) => {
    setIsNewTemplate(false)
    setTemplateEditModal({ key, name: tpl.name, content: tpl.content })
  }
  const saveTemplate = async () => {
    if (!templateEditModal) return
    const name = templateEditModal.name.trim()
    const content = templateEditModal.content.trim()
    if (!name || !content) {
      showAlert('이름과 내용을 입력해주세요', 'error')
      return
    }
    try {
      await csInquiryApi.addTemplate(templateEditModal.key, name, content)
      setTemplateEditModal(null)
      load()
    } catch (e) {
      showAlert(e instanceof Error ? e.message : '템플릿 저장 실패', 'error')
    }
  }
  const deleteTemplate = async (key: string, name: string) => {
    if (!await showConfirm(`"${name}" 템플릿을 삭제하시겠습니까?`)) return
    try {
      await csInquiryApi.deleteTemplate(key)
      load()
    } catch (e) {
      showAlert(e instanceof Error ? e.message : '삭제 실패', 'error')
    }
  }

  // 변수 태그 목록 (CS/SMS/카카오 공통)
  const VARIABLE_TAGS = [
    { tag: '{{sellerName}}', label: '판매자명' },
    { tag: '{{marketName}}', label: '판매마켓이름' },
    { tag: '{{OrderName}}', label: '주문번호' },
    { tag: '{{rvcName}}', label: '수취인명' },
    { tag: '{{rcvHPNo}}', label: '수취인휴대폰번호' },
    { tag: '{{goodsName}}', label: '상품명' },
  ]

  // textarea에 태그 삽입
  const isProductQuestion = (inquiry?: SambaCSInquiry | null) => inquiry?.inquiry_type === 'product_question'

  const sanitizeReplyTextForInquiry = (text: string, inquiry?: SambaCSInquiry | null) => {
    if (!isProductQuestion(inquiry)) return text
    return text
      .replace(/\{\{sellerName\}\}\s*고객님[,\s]*/g, '')
      .replace(/\{\{sellerName\}\}\s*님[,\s]*/g, '')
      .replace(/\{\{sellerName\}\}[^\S\r\n]*[^\s,.!?:;]+[^\S\r\n]*/g, '')
      .replace(/\{\{sellerName\}\}/g, '')
      .replace(/[ \t]{2,}/g, ' ')
      .replace(/\n{3,}/g, '\n\n')
      .trim()
  }

  const replyVariableTags = isProductQuestion(replyModal)
    ? VARIABLE_TAGS.filter(v => v.tag !== '{{sellerName}}')
    : VARIABLE_TAGS

  const insertTag = (ref: React.RefObject<HTMLTextAreaElement | null>, setter: (v: string) => void, getter: string, tag: string) => {
    const el = ref.current
    if (!el) { setter(getter + tag); return }
    const start = el.selectionStart
    const end = el.selectionEnd
    const newVal = getter.slice(0, start) + tag + getter.slice(end)
    setter(newVal)
    requestAnimationFrame(() => { el.selectionStart = el.selectionEnd = start + tag.length; el.focus() })
  }

  // 데이터 로드
  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [data, st, tpl] = await Promise.all([
        csInquiryApi.list({
          skip: page * pageSize,
          limit: pageSize,
          market: (() => {
            if (!filterMarket) return undefined
            if (filterMarket.startsWith('type:')) return accounts.find(a => a.market_type === filterMarket.slice(5))?.market_name
            if (filterMarket.startsWith('acc:')) return accounts.find(a => a.id === filterMarket.slice(4))?.market_name
            return filterMarket
          })(),
          inquiry_type: filterReview === 'only' ? '상품평' : filterType || undefined,
          exclude_inquiry_type: filterReview === 'exclude' ? '상품평' : undefined,
          reply_status: filterStatus || undefined,
          search: search || undefined,
          sort_desc: sortDesc,
          // 문의일시(inquiry_date) 기준 선택 기간 필터 — 미답변 포함 항상 적용
          // (기간 밖 문의가 목록에 노출되던 버그 수정. 오래된 backlog는 "한달/올해"로 조회)
          start_date: csCustomStart || undefined,
          end_date: csCustomEnd || undefined,
        }).catch(() => ({ items: [], total: 0 })),
        csInquiryApi.getStats().catch(() => ({})),
        csInquiryApi.getTemplates().catch(() => ({})),
      ])
      setInquiries(data.items)
      setTotal(data.total)
      setStats(st)
      setTemplates(tpl)
    } catch {
      // 에러 무시
    }
    setLoading(false)
  }, [filterMarket, filterType, filterReview, filterStatus, search, sortDesc, pageSize, page, csCustomStart, csCustomEnd, accounts])

  useEffect(() => { load() }, [load])
  useEffect(() => { accountApi.listActiveCached(setAccounts) }, [])

  // 검색
  const handleSearch = async () => {
    const ts = fmtTime
    // 드롭다운 value 파싱: "" = 전체, "type:XXX" = 마켓 타입 단위, "{uuid}" = 개별 계정
    let selectedMarket: string | undefined
    let selectedAccountId: string | undefined
    let label: string
    if (csSyncAccountId.startsWith('market:')) {
      selectedMarket = csSyncAccountId.slice(7)
      label = selectedMarket
    } else if (csSyncAccountId.startsWith('type:')) {
      const marketType = csSyncAccountId.slice(5)
      selectedMarket = accounts.find(a => a.market_type === marketType)?.market_name
      label = selectedMarket || marketType
    } else if (csSyncAccountId.startsWith('account:')) {
      selectedAccountId = csSyncAccountId.slice(8)
      const acc = accounts.find(a => a.id === selectedAccountId)
      selectedMarket = acc?.market_name
      label = acc?.account_label?.trim() || acc?.seller_id?.trim() || selectedMarket || selectedAccountId
    } else if (csSyncAccountId) {
      const acc = accounts.find(a => a.id === csSyncAccountId)
      selectedAccountId = acc?.id
      selectedMarket = acc?.market_name
      label = acc?.account_label?.trim() || acc?.seller_id?.trim() || selectedMarket || csSyncAccountId
    } else {
      selectedMarket = undefined
      label = '전체마켓'
    }
    setCsLogMessages(prev => [...prev, `[${ts()}] ${label} CS 문의 동기화 중...`])
    try {
      const result = await csInquiryApi.syncFromMarkets(selectedMarket, selectedAccountId)
      const resultLogs: string[] = []
      for (const item of (result.results || []) as CSSyncResultItem[]) {
        const errorDetail = item.error?.includes(':') ? item.error.split(':').slice(1).join(':').trim() : item.error
        resultLogs.push(`[${ts()}] ${item.account}: ${fmtNum(item.synced || 0)}건 동기화${errorDetail ? ` (${errorDetail})` : ''}`)
      }
      if (resultLogs.length === 0 && result.errors?.length) {
        for (const err of result.errors) resultLogs.push(`[${ts()}] ${err}`)
      }
      resultLogs.push(`[${ts()}] ${result.message}`)
      setCsLogMessages(prev => [...prev, ...resultLogs])
      setPage(0)
      setSearch('')
      setSearchInput('')
      const [data, st, tpl] = await Promise.all([
        csInquiryApi.list({ skip: 0, limit: pageSize, sort_desc: sortDesc, reply_status: filterStatus || undefined, market: (() => { if (!filterMarket) return undefined; if (filterMarket.startsWith('type:')) return accounts.find(a => a.market_type === filterMarket.slice(5))?.market_name; if (filterMarket.startsWith('acc:')) return accounts.find(a => a.id === filterMarket.slice(4))?.market_name; return filterMarket })(), start_date: csCustomStart || undefined, end_date: csCustomEnd || undefined }).catch(() => ({ items: [], total: 0 })),
        csInquiryApi.getStats().catch(() => ({})),
        csInquiryApi.getTemplates().catch(() => ({})),
      ])
      setInquiries(data.items)
      setTotal(data.total)
      setStats(st)
      setTemplates(tpl)
    } catch (err) {
      setCsLogMessages(prev => [...prev, `[${ts()}] 동기화 실패: ${err}`])
    }
  }

  // 고객/주문번호 검색 — 동기화가 아니라 목록 필터만 (search state 변경 → load 재실행)
  const handleFilterSearch = () => {
    setPage(0)
    setSearch(searchInput.trim())
  }

  // 전체 선택
  const toggleAll = () => {
    if (selected.size === inquiries.length) {
      setSelected(new Set())
    } else {
      setSelected(new Set(inquiries.map(i => i.id)))
    }
  }

  // 개별 선택
  const toggleOne = (id: string) => {
    const next = new Set(selected)
    if (next.has(id)) next.delete(id)
    else next.add(id)
    setSelected(next)
  }

  // 선택 삭제
  const handleBatchDelete = async () => {
    if (selected.size === 0) {
      showAlert('삭제할 항목을 선택해주세요', 'error')
      return
    }
    if (!await showConfirm(`${fmtNum(selected.size)}건을 삭제하시겠습니까?`)) return
    try {
      await csInquiryApi.batchDelete(Array.from(selected))
      setSelected(new Set())
      load()
    } catch (e) {
      showAlert(e instanceof Error ? e.message : '삭제 실패', 'error')
    }
  }

  // 답변 등록
  const handleReply = async () => {
    if (!replyModal || !replyText.trim()) {
      showAlert('답변 내용을 입력해주세요', 'error')
      return
    }
    try {
      const finalReplyText = sanitizeReplyTextForInquiry(replyText, replyModal)
      const res = await csInquiryApi.reply(replyModal.id, finalReplyText)
      const marketMsg = (res as unknown as Record<string, unknown>).market_message as string
      const marketSent = (res as unknown as Record<string, unknown>).market_sent as boolean
      setReplyModal(null)
      setReplyText('')
      setSelectedTemplate('')
      await load()
      if (marketSent) {
        showAlert(marketMsg || '답변 등록 + 마켓 전송 완료', 'success')
      } else if (marketMsg) {
        showAlert(`답변 저장 완료 (${marketMsg})`, 'info')
      }
    } catch (e) {
      showAlert(e instanceof Error ? e.message : '답변 등록 실패', 'error')
    }
  }

  // AI 초안 바로 전송 — 모달 없이 목록에서 1클릭 전송
  const handleQuickSend = async (item: SambaCSInquiry) => {
    if (!item.draft_reply) return
    if (!await showConfirm('이 AI 초안으로 바로 전송할까요?')) return
    try {
      const finalText = sanitizeReplyTextForInquiry(item.draft_reply, item)
      const res = await csInquiryApi.reply(item.id, finalText)
      const marketMsg = (res as unknown as Record<string, unknown>).market_message as string
      const marketSent = (res as unknown as Record<string, unknown>).market_sent as boolean
      await load()
      if (marketSent) {
        showAlert(marketMsg || '답변 전송 완료', 'success')
      } else if (marketMsg) {
        showAlert(`답변 저장 완료 (${marketMsg})`, 'info')
      }
    } catch (e) {
      showAlert(e instanceof Error ? e.message : '전송 실패', 'error')
    }
  }

  // 단건 삭제
  const handleDelete = async (id: string) => {
    if (!await showConfirm('이 문의를 삭제하시겠습니까?')) return
    try {
      await csInquiryApi.delete(id)
      load()
    } catch (e) {
      showAlert(e instanceof Error ? e.message : '삭제 실패', 'error')
    }
  }

  // 답변완료로 변경 (이미 답변된 건 — 상태값만 변경해 미답변 필터에서 제외)
  const handleMarkReplied = async (id: string) => {
    if (!await showConfirm('이 문의를 답변완료 상태로 변경하시겠습니까?')) return
    try {
      await csInquiryApi.markReplied(id)
      load()
    } catch (e) {
      showAlert(e instanceof Error ? e.message : '답변완료 변경 실패', 'error')
    }
  }

  const totalPages = Math.ceil(total / pageSize)

  return (
    <div style={{ color: '#E5E5E5' }}>
      {/* 헤더 */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '1.5rem' }}>
        <div>
          <h2 style={{ fontSize: '1.5rem', fontWeight: 700, color: '#E5E5E5', marginBottom: '0.25rem' }}>CS 관리</h2>
          <p style={{ fontSize: '0.875rem', color: '#888' }}>
            연동하신 마켓의 질문/긴급문의/긴급알림 등에 대한 답변이 가능합니다
          </p>
        </div>
      </div>

      {/* 로그 영역 */}
      <div style={{ border: '1px solid #1C2333', borderRadius: '8px', overflow: 'hidden', marginBottom: '0.75rem' }}>
        <div style={{ padding: '6px 14px', background: '#0D1117', borderBottom: '1px solid #1C2333', display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <span style={{ fontSize: '0.8rem', fontWeight: 600, color: '#94A3B8' }}>CS 로그</span>
          <div style={{ display: 'flex', gap: '4px' }}>
            <button onClick={() => navigator.clipboard.writeText(csLogMessages.join('\n'))} style={{ fontSize: '0.72rem', color: '#555', background: 'transparent', border: '1px solid #1C2333', padding: '1px 8px', borderRadius: '4px', cursor: 'pointer' }}>복사</button>
            <button onClick={() => setCsLogMessages(['[대기] CS 문의 가져오기 결과가 여기에 표시됩니다...'])} style={{ fontSize: '0.72rem', color: '#555', background: 'transparent', border: '1px solid #1C2333', padding: '1px 8px', borderRadius: '4px', cursor: 'pointer' }}>초기화</button>
          </div>
        </div>
        <div ref={el => { if (el) el.scrollTop = el.scrollHeight }} style={{ height: '144px', overflowY: 'auto', padding: '8px 14px', fontFamily: "'Courier New', monospace", fontSize: '0.788rem', color: '#8A95B0', background: '#080A10', lineHeight: 1.8 }}>
          {csLogMessages.map((msg, i) => <p key={i} style={{ color: '#8A95B0', fontSize: 'inherit', margin: 0 }}>{renderCsLogMessage(msg)}</p>)}
        </div>
      </div>

      {/* 기간 필터 바 */}
      <div style={{ background: 'rgba(18,18,18,0.98)', border: '1px solid #232323', borderRadius: '10px', padding: '0.625rem 0.875rem', marginBottom: '0.75rem', display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: '0.5rem' }}>
        <div style={{ display: 'flex', gap: '4px', flexWrap: 'nowrap', alignItems: 'center' }}>
          {PERIOD_BUTTONS.map(pb => (
            <button key={pb.key} onClick={() => {
              if (csDateLocked) return
              setCsPeriod(pb.key)
              if (!csStartLocked) {
                const start = getPeriodStart(pb.key)
                setCsCustomStart(start ? start.toLocaleDateString('sv-SE') : '')
              }
              setCsCustomEnd(getPeriodEnd(pb.key).toLocaleDateString('sv-SE'))
            }}
              style={{ padding: '0.22rem 0.55rem', borderRadius: '5px', fontSize: '0.75rem', background: csPeriod === pb.key ? 'rgba(80,80,80,0.8)' : 'rgba(50,50,50,0.8)', border: csPeriod === pb.key ? '1px solid #666' : '1px solid #3D3D3D', color: csPeriod === pb.key ? '#fff' : '#C5C5C5', cursor: csDateLocked ? 'not-allowed' : 'pointer', whiteSpace: 'nowrap', opacity: csDateLocked && csPeriod !== pb.key ? 0.5 : 1 }}
            >{pb.label}</button>
          ))}
          <span style={{ width: '1px', background: '#333', height: '18px', margin: '0 4px' }} />
          <input type="date" value={csCustomStart} onChange={e => setCsCustomStart(e.target.value)} style={{ ...inputStyle, padding: '0 0.4rem', fontSize: '0.75rem', height: '28px', ...(csStartLocked ? { borderColor: '#C0392B', color: '#FF8C00' } : {}) }} />
          <button onClick={() => setCsStartLocked(p => !p)} style={{ padding: '0 0.5rem', fontSize: '0.72rem', height: '28px', borderRadius: '4px', cursor: 'pointer', whiteSpace: 'nowrap', background: csStartLocked ? '#8B1A1A' : 'rgba(50,50,50,0.8)', border: csStartLocked ? '1px solid #C0392B' : '1px solid #3D3D3D', color: csStartLocked ? '#fff' : '#C5C5C5' }}>고정</button>
          <span style={{ color: '#555', fontSize: '0.75rem' }}>~</span>
          <input type="date" value={csCustomEnd} onChange={e => setCsCustomEnd(e.target.value)} style={{ ...inputStyle, padding: '0 0.4rem', fontSize: '0.75rem', height: '28px' }} />
          <button onClick={() => setCsDateLocked(p => !p)} style={{ padding: '0 0.5rem', fontSize: '0.72rem', height: '28px', borderRadius: '4px', cursor: 'pointer', whiteSpace: 'nowrap', background: csDateLocked ? '#8B1A1A' : 'rgba(50,50,50,0.8)', border: csDateLocked ? '1px solid #C0392B' : '1px solid #3D3D3D', color: csDateLocked ? '#fff' : '#C5C5C5' }}>고정</button>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '4px', flexShrink: 0 }}>
          <select value={csSyncAccountId} onChange={e => setCsSyncAccountId(e.target.value)} style={{ ...inputStyle, padding: '0 0.4rem', fontSize: '0.75rem', height: '28px', minWidth: '140px' }}>
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
          <button onClick={handleSearch} style={{ padding: '0 0.65rem', fontSize: '0.75rem', height: '28px', background: 'rgba(50,50,50,0.9)', border: '1px solid #3D3D3D', color: '#C5C5C5', borderRadius: '4px', cursor: 'pointer', whiteSpace: 'nowrap' }}>가져오기</button>
        </div>
      </div>

      {/* 필터 바 */}
      <div style={{ background: 'rgba(18,18,18,0.98)', border: '1px solid #232323', borderRadius: '10px', padding: '0.75rem 1rem', marginBottom: '1rem', display: 'flex', alignItems: 'center', gap: '6px', flexWrap: 'nowrap' }}>
        <input style={{ ...inputStyle, width: '150px', fontSize: '0.75rem', height: '28px', padding: '0 0.3rem' }} value={searchInput} onChange={e => setSearchInput(e.target.value)} onKeyDown={e => { if (e.key === 'Enter') handleFilterSearch() }} placeholder='고객/주문번호 검색' />
        <button onClick={handleFilterSearch} style={{ background: 'linear-gradient(135deg,#FF8C00,#FFB84D)', color: '#fff', padding: '0 0.6rem', borderRadius: '4px', fontSize: '0.75rem', border: 'none', cursor: 'pointer', whiteSpace: 'nowrap', height: '28px' }}>검색</button>
        <button
          onClick={handleBatchDelete}
          style={{ padding: '0 0.6rem', fontSize: '0.75rem', background: 'transparent', border: '1px solid #FF6B6B33', borderRadius: '4px', color: '#FF6B6B', cursor: 'pointer', whiteSpace: 'nowrap', height: '28px', lineHeight: '26px' }}
        >
          선택삭제
        </button>
        <div style={{ display: 'flex', gap: '4px', marginLeft: 'auto', flexShrink: 0, alignItems: 'center' }}>
          <select style={{ ...inputStyle, width: '130px', fontSize: '0.75rem', height: '28px', padding: '0 0.3rem' }} value={filterMarket} onChange={e => { setFilterMarket(e.target.value); setPage(0) }}>
            <option value="">전체마켓보기</option>
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
          <select style={{ ...inputStyle, width: '94px', fontSize: '0.75rem', height: '28px', padding: '0 0.3rem' }} value={csSiteFilter} onChange={e => setCsSiteFilter(e.target.value)}>
            <option value="">전체사이트보기</option>
            {['MUSINSA','KREAM','FashionPlus','Nike','Adidas','ABCmart','REXMONDE','SSG','LOTTEON','GSShop','ElandMall','SSF'].map(s => <option key={s} value={s}>{s}</option>)}
          </select>
          <select style={{ ...inputStyle, width: '95px', fontSize: '0.75rem', height: '28px', padding: '0 0.3rem' }} value={filterStatus} onChange={e => { setFilterStatus(e.target.value); setPage(0) }}>
            <option value="">답변상태</option>
            <option value="pending">미답변</option>
            <option value="replied">답변완료</option>
          </select>
          <select style={{ ...inputStyle, width: '105px', fontSize: '0.75rem', height: '28px', padding: '0 0.3rem' }} value={filterReview} onChange={e => { setFilterReview(e.target.value); setPage(0) }}>
            <option value="">전체보기</option>
            <option value="only">상품평</option>
            <option value="exclude">상품평제외</option>
          </select>
          <span style={{ width: '1px', background: '#333', height: '18px', margin: '0 2px' }} />
          <select style={{ ...inputStyle, width: '75px', fontSize: '0.75rem', height: '28px', padding: '0 0.3rem' }} onChange={() => setSortDesc(!sortDesc)}>
            <option>-- 정렬 --</option>
            <option>문의일자▲</option>
            <option>문의일자▼</option>
          </select>
          <select style={{ ...inputStyle, width: '78px', fontSize: '0.75rem', height: '28px', padding: '0 0.3rem' }} value={pageSize} onChange={e => { setPageSize(Number(e.target.value)); setPage(0) }}>
            <option value={50}>50개 보기</option><option value={100}>100개 보기</option><option value={200}>200개 보기</option><option value={500}>500개 보기</option>
          </select>
        </div>
      </div>

      {/* 테이블 */}
      <div style={card}>
        <div style={{ overflowX: 'auto' }}>
          {loading ? (
            <div style={{ padding: '3rem', textAlign: 'center', color: '#555' }}>로딩 중...</div>
          ) : (
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.8125rem' }}>
              <thead>
                <tr style={{ background: 'rgba(255,255,255,0.03)', borderBottom: '1px solid #2D2D2D' }}>
                  <th style={{ width: '40px', padding: '0.75rem 0.5rem', textAlign: 'center' }}>
                    <input
                      type="checkbox"
                      checked={inquiries.length > 0 && selected.size === inquiries.length}
                      onChange={toggleAll}
                      style={{ accentColor: '#FF8C00' }}
                    />
                  </th>
                  <th style={{ padding: '0.75rem 1rem', color: '#888', fontWeight: 500, textAlign: 'center', whiteSpace: 'nowrap', width: '80px' }}>
                    상품
                  </th>
                  <th style={{ padding: '0.75rem 1rem', color: '#888', fontWeight: 500, textAlign: 'center', whiteSpace: 'nowrap' }}>
                    마켓
                  </th>
                  <th style={{ padding: '0.75rem 1rem', color: '#888', fontWeight: 500, textAlign: 'center', whiteSpace: 'nowrap' }}>
                    주문번호
                  </th>
                  <th style={{ padding: '0.75rem 1rem', color: '#888', fontWeight: 500, textAlign: 'center', whiteSpace: 'nowrap' }}>
                    문의유형
                  </th>
                  <th style={{ padding: '0.75rem 1rem', color: '#888', fontWeight: 500, textAlign: 'center', whiteSpace: 'nowrap' }}>
                    고객
                  </th>
                  <th style={{ padding: '0.75rem 1rem', color: '#888', fontWeight: 500, textAlign: 'center', minWidth: '400px' }}>문의내용</th>
                  <th style={{ padding: '0.75rem 1rem', color: '#888', fontWeight: 500, textAlign: 'center', whiteSpace: 'nowrap' }}>답변여부</th>
                  <th style={{ padding: '0.75rem 1rem', color: '#888', fontWeight: 500, textAlign: 'center', whiteSpace: 'nowrap' }}>
                    문의일시<br /><span style={{ fontSize: '0.75rem' }}>(문의수집일자)</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {inquiries.map(item => {
                  const st = REPLY_STATUS_MAP[item.reply_status] || REPLY_STATUS_MAP.pending
                  const tp = INQUIRY_TYPE_MAP[item.inquiry_type] || { label: item.inquiry_type, color: '#888' }
                  // 본 시스템이 등록하지 않은 외부 상품 — original_link/product_image/collected_product_id 모두 미상
                  const isExternal = !item.collected_product_id
                  const externalTooltip = '본 시스템에 수집되지 않은 외부 등록 상품입니다'
                  return (
                    <tr
                      key={item.id}
                      style={{ borderBottom: '1px solid rgba(45,45,45,0.5)' }}
                      onMouseEnter={e => (e.currentTarget.style.background = 'rgba(255,255,255,0.02)')}
                      onMouseLeave={e => (e.currentTarget.style.background = 'transparent')}
                    >
                      {/* 체크박스 */}
                      <td style={{ padding: '0.75rem 0.5rem', textAlign: 'center', verticalAlign: 'top' }}>
                        <input
                          type="checkbox"
                          checked={selected.has(item.id)}
                          onChange={() => toggleOne(item.id)}
                          style={{ accentColor: '#FF8C00' }}
                        />
                      </td>

                      {/* 사진 */}
                      <td style={{ padding: '0.75rem 0.5rem', verticalAlign: 'top', textAlign: 'center' }}>
                        {item.product_image ? (
                          <img
                            src={item.product_image}
                            alt=""
                            onClick={() => item.product_link && window.open(item.product_link, '_blank')}
                            title={isExternal ? externalTooltip : ''}
                            style={{ width: '60px', height: '60px', objectFit: 'cover', borderRadius: '6px', border: '1px solid #2D2D2D', cursor: item.product_link ? 'pointer' : 'default' }}
                          />
                        ) : (
                          <div
                            onClick={() => item.product_link && window.open(item.product_link, '_blank')}
                            title={isExternal ? externalTooltip : ''}
                            style={{ width: '60px', height: '60px', background: '#1A1A1A', borderRadius: '6px', border: '1px solid #2D2D2D', display: 'flex', alignItems: 'center', justifyContent: 'center', color: isExternal ? '#888' : (item.product_link ? '#4C9AFF' : '#444'), fontSize: '0.625rem', cursor: item.product_link ? 'pointer' : 'default', textDecoration: !isExternal && item.product_link ? 'underline' : 'none', margin: '0 auto' }}
                          >
                            {isExternal ? '외부' : (item.product_link ? '링크' : 'No IMG')}
                          </div>
                        )}
                        {item.market_inquiry_no && (
                          <div style={{ fontSize: '0.6rem', color: '#555', marginTop: '0.25rem', wordBreak: 'break-all' }}>{item.market_inquiry_no}</div>
                        )}
                      </td>

                      {/* 마켓 */}
                      <td style={{ padding: '0.75rem 1rem', verticalAlign: 'top', whiteSpace: 'nowrap', textAlign: 'center' }}>
                        <div style={{ fontWeight: 600, color: '#E5E5E5' }}>
                          {item.market}
                        </div>
                        {item.account_name && (
                          <div style={{ fontSize: '0.75rem', color: '#666', marginTop: '0.125rem' }}>{item.account_name}</div>
                        )}
                      </td>

                      {/* 주문번호 + 링크버튼 */}
                      <td style={{ padding: '0.75rem 1rem', verticalAlign: 'top' }}>
                        {(() => {
                          const ordId = item.market_order_id
                          if (!ordId) {
                            return <div style={{ fontSize: '0.8125rem', color: '#AAA', textAlign: 'center' }}>-</div>
                          }
                          // 스마트스토어는 상품주문번호 기반이라 주문관리 페이지로 바로 이동 가능
                          const isSmartstore = item.market === '스마트스토어' || item.market === 'smartstore'
                          return (
                            <div
                              onClick={() => {
                                window.open(
                                  `/samba/orders?search=${encodeURIComponent(ordId)}&search_type=order_number`,
                                  '_blank',
                                )
                              }}
                              title={isSmartstore ? '주문관리에서 이 상품주문번호 검색 (새 탭)' : '주문관리에서 이 주문번호 검색 (새 탭)'}
                              style={{
                                fontSize: '0.8125rem',
                                color: '#4C9AFF',
                                textAlign: 'center',
                                cursor: 'pointer',
                                textDecoration: 'underline',
                                wordBreak: 'break-all',
                              }}
                            >
                              {ordId}
                            </div>
                          )
                        })()}
                        <div style={{ display: 'flex', flexDirection: 'column', gap: '0.3rem', marginTop: '0.375rem', alignItems: 'center' }}>
                          <button
                            onClick={() => {
                              if (!item.original_link) {
                                showAlert(isExternal ? externalTooltip : '소싱처 원문링크가 없습니다', 'info')
                                return
                              }
                              const link = item.original_link.replace(
                                /https?:\/\/www\.lotteon\.com\/product\/productDetail\.lotte\?spdNo=([^&\s]+).*/,
                                'https://www.lotteon.com/p/product/$1'
                              )
                              window.open(link, '_blank')
                            }}
                            title={isExternal && !item.original_link ? externalTooltip : ''}
                            style={{ fontSize: '0.72rem', padding: '0.15rem 0.375rem', border: '1px solid #444', borderRadius: '3px', color: item.original_link ? '#4C9AFF' : '#555', background: 'transparent', cursor: 'pointer', width: '100%', textAlign: 'center' }}
                          >원문링크</button>
                          <button
                            onClick={() => {
                              const link = item.product_link || item.market_link
                              link ? window.open(link, '_blank') : showAlert('판매 구매페이지 링크가 없습니다', 'info')
                            }}
                            style={{ fontSize: '0.72rem', padding: '0.15rem 0.375rem', border: '1px solid #444', borderRadius: '3px', color: (item.product_link || item.market_link) ? '#51CF66' : '#555', background: 'transparent', cursor: 'pointer', width: '100%', textAlign: 'center' }}
                          >판매링크</button>
                          <button
                            onClick={() => {
                              if (item.collected_product_id) {
                                window.open(`/samba/products?search=${encodeURIComponent(item.collected_product_id)}&search_type=id&highlight=${item.collected_product_id}`, '_blank')
                              } else {
                                showAlert(externalTooltip, 'info')
                              }
                            }}
                            title={isExternal ? externalTooltip : ''}
                            style={{ fontSize: '0.72rem', padding: '0.15rem 0.375rem', border: '1px solid #444', borderRadius: '3px', color: item.collected_product_id ? '#FF8C00' : '#555', background: 'transparent', cursor: 'pointer', width: '100%', textAlign: 'center' }}
                          >상품정보</button>
                        </div>
                      </td>

                      {/* 문의유형 */}
                      <td style={{ padding: '0.75rem 1rem', verticalAlign: 'top', textAlign: 'center' }}>
                        {item.inquiry_type === 'exchange_request' ? (
                          <button
                            onClick={() => setExchangeActionItem(item)}
                            style={{ padding: '0.15rem 0.5rem', borderRadius: '12px', fontSize: '0.75rem', fontWeight: 600, background: `${tp.color}22`, color: tp.color, border: `1px solid ${tp.color}44`, cursor: 'pointer' }}
                          >
                            {tp.label}
                          </button>
                        ) : item.inquiry_type === 'cancel_request' ? (
                          <button
                            onClick={() => handleCancelApprove(item)}
                            style={{ padding: '0.15rem 0.5rem', borderRadius: '12px', fontSize: '0.75rem', fontWeight: 600, background: `${tp.color}22`, color: tp.color, border: `1px solid ${tp.color}44`, cursor: 'pointer' }}
                          >
                            {tp.label}
                          </button>
                        ) : (
                          <span style={{ padding: '0.15rem 0.5rem', borderRadius: '12px', fontSize: '0.75rem', fontWeight: 600, background: `${tp.color}22`, color: tp.color }}>
                            {tp.label}
                          </span>
                        )}
                      </td>

                      {/* 고객 */}
                      <td style={{ padding: '0.75rem 1rem', verticalAlign: 'top', textAlign: 'center' }}>
                        <div style={{ fontSize: '0.8125rem', color: '#AAA' }}>
                          {item.questioner || '-'}
                        </div>
                      </td>

                      {/* 문의내용 */}
                      <td style={{ padding: '0.75rem 1rem', verticalAlign: 'top' }}>
                        {/* 상품명 + 답변 + 링크 */}
                        <div style={{ marginBottom: '0.5rem' }}>
                          {item.product_name && (
                            <span style={{ fontWeight: 600, color: '#E5E5E5', fontSize: '0.8125rem' }}>
                              {item.product_name}
                            </span>
                          )}
                          <button
                            onClick={() => { setReplyModal(item); setReplyText(sanitizeReplyTextForInquiry(item.reply || item.draft_reply || '', item)); setSelectedTemplate('') }}
                            style={{ marginLeft: item.product_name ? '0.375rem' : 0, padding: '0.1rem 0.4rem', background: item.reply_status === 'pending' ? 'rgba(255,140,0,0.15)' : 'rgba(81,207,102,0.1)', border: `1px solid ${item.reply_status === 'pending' ? 'rgba(255,140,0,0.3)' : 'rgba(81,207,102,0.3)'}`, borderRadius: '4px', color: item.reply_status === 'pending' ? '#FF8C00' : '#51CF66', fontSize: '0.6875rem', cursor: 'pointer', whiteSpace: 'nowrap', verticalAlign: 'middle' }}
                          >
                            {item.reply_status === 'pending'
                              ? (item.draft_reply ? '초안검토' : '답변')
                              : '답변수정'}
                          </button>
                          {/* CS 자동화 — Claude 초안/자동전송 배지 */}
                          {item.draft_status === 'auto_sent' && (
                            <span style={{ marginLeft: '0.375rem', padding: '0.1rem 0.4rem', background: 'rgba(76,154,255,0.12)', border: '1px solid rgba(76,154,255,0.3)', borderRadius: '4px', color: '#4C9AFF', fontSize: '0.6875rem', whiteSpace: 'nowrap', verticalAlign: 'middle' }}>
                              자동전송됨
                            </span>
                          )}
                          {item.reply_status === 'pending' && item.draft_status === 'suggested' && item.draft_reply && (
                            <span title={`의도: ${item.intent || '-'}`} style={{ marginLeft: '0.375rem', padding: '0.1rem 0.4rem', background: 'rgba(186,104,255,0.12)', border: '1px solid rgba(186,104,255,0.3)', borderRadius: '4px', color: '#BA68FF', fontSize: '0.6875rem', whiteSpace: 'nowrap', verticalAlign: 'middle' }}>
                              AI초안 {item.draft_confidence != null ? `${Math.round(item.draft_confidence * 100)}%` : ''}
                            </span>
                          )}
                        </div>

                        {/* 문의 내용 — eBay [sender] 형식은 색상 구분 */}
                        <div style={{ color: '#ccc', fontSize: '0.8125rem', lineHeight: '1.5', whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
                          {item.market === 'eBay' && item.content?.includes('[') ? (
                            item.content.split('\n').map((line: string, li: number) => {
                              const m = line.match(/^\[([^\]]+)\]\s*(.*)$/)
                              if (m) {
                                const isSeller = m[1] === 'seller'
                                return (
                                  <div key={li} style={{ marginBottom: '0.25rem' }}>
                                    <span style={{ color: isSeller ? '#51CF66' : '#4C9AFF', fontWeight: 600, fontSize: '0.75rem' }}>[{m[1]}]</span>
                                    <span style={{ marginLeft: '0.375rem' }}>{m[2]}</span>
                                  </div>
                                )
                              }
                              return <div key={li}>{line}</div>
                            })
                          ) : (
                            htmlToText(item.content)
                          )}
                        </div>

                        {/* CS 자동화 — AI 초안 인라인 표시 + 바로 전송 (미답변 + 초안 있을 때) */}
                        {item.reply_status === 'pending' && item.draft_status === 'suggested' && item.draft_reply && (
                          <div style={{ marginTop: '0.5rem', padding: '0.5rem 0.75rem', background: 'rgba(186,104,255,0.08)', borderRadius: '6px', borderLeft: '3px solid #BA68FF' }}>
                            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '0.25rem' }}>
                              <div style={{ fontSize: '0.75rem', color: '#BA68FF', fontWeight: 600 }}>
                                AI 초안{item.draft_confidence != null ? ` ${Math.round(item.draft_confidence * 100)}%` : ''}
                              </div>
                              <div style={{ display: 'flex', gap: '0.375rem' }}>
                                <button
                                  onClick={() => handleQuickSend(item)}
                                  style={{ padding: '0.15rem 0.55rem', background: 'rgba(81,207,102,0.15)', border: '1px solid rgba(81,207,102,0.4)', borderRadius: '4px', color: '#51CF66', fontSize: '0.6875rem', cursor: 'pointer', whiteSpace: 'nowrap', fontWeight: 600 }}
                                >
                                  바로 전송
                                </button>
                                <button
                                  onClick={() => { setReplyModal(item); setReplyText(sanitizeReplyTextForInquiry(item.draft_reply || '', item)); setSelectedTemplate('') }}
                                  style={{ padding: '0.15rem 0.55rem', background: 'transparent', border: '1px solid #2D2D2D', borderRadius: '4px', color: '#888', fontSize: '0.6875rem', cursor: 'pointer', whiteSpace: 'nowrap' }}
                                >
                                  수정
                                </button>
                              </div>
                            </div>
                            <div style={{ color: '#ccc', fontSize: '0.8125rem', lineHeight: '1.5', whiteSpace: 'pre-wrap' }}>
                              {htmlToText(item.draft_reply || '')}
                            </div>
                          </div>
                        )}

                        {/* 답변 내용 */}
                        {item.reply && (
                          <div style={{ marginTop: '0.5rem', padding: '0.5rem 0.75rem', background: 'rgba(81,207,102,0.08)', borderRadius: '6px', borderLeft: '3px solid #51CF66' }}>
                            <div style={{ fontSize: '0.75rem', color: '#51CF66', marginBottom: '0.25rem', fontWeight: 600 }}>답변</div>
                            <div style={{ color: '#aaa', fontSize: '0.8125rem', lineHeight: '1.5', whiteSpace: 'pre-wrap' }}>
                              {htmlToText(item.reply || '')}
                            </div>
                            {item.replied_at && (
                              <div style={{ fontSize: '0.6875rem', color: '#555', marginTop: '0.25rem' }}>
                                [{new Date(item.replied_at).toLocaleString('ko-KR')}]
                              </div>
                            )}
                          </div>
                        )}
                      </td>

                      {/* 답변 여부 */}
                      <td style={{ padding: '0.75rem 1rem', textAlign: 'center', verticalAlign: 'top' }}>
                        <span style={{
                          padding: '0.25rem 0.625rem',
                          borderRadius: '20px',
                          fontSize: '0.75rem',
                          fontWeight: 600,
                          background: st.bg,
                          color: st.text,
                        }}>
                          {st.label}
                        </span>
                      </td>

                      {/* 문의일시 + 액션 */}
                      <td style={{ padding: '0.75rem 1rem', textAlign: 'center', verticalAlign: 'top', whiteSpace: 'nowrap' }}>
                        <div style={{ fontSize: '0.75rem', color: '#888', marginBottom: '0.5rem' }}>
                          {fmtDate(item.inquiry_date)}
                        </div>
                        <div style={{ fontSize: '0.6875rem', color: '#555', marginBottom: '0.5rem' }}>
                          {fmtDate(item.collected_at)}
                        </div>
                        <div style={{ display: 'flex', gap: '0.375rem', justifyContent: 'center' }}>
                          <button
                            onClick={() => handleDelete(item.id)}
                            style={{ padding: '0.25rem 0.5rem', background: 'rgba(255,107,107,0.1)', border: '1px solid rgba(255,107,107,0.2)', borderRadius: '4px', color: '#FF6B6B', fontSize: '0.6875rem', cursor: 'pointer' }}
                          >
                            삭제
                          </button>
                          <button
                            onClick={() => handleMarkReplied(item.id)}
                            style={{ padding: '0.25rem 0.5rem', background: 'rgba(81,207,102,0.1)', border: '1px solid rgba(81,207,102,0.2)', borderRadius: '4px', color: '#51CF66', fontSize: '0.6875rem', cursor: 'pointer', whiteSpace: 'nowrap' }}
                          >
                            답변완료
                          </button>
                        </div>
                      </td>
                    </tr>
                  )
                })}
                {inquiries.length === 0 && (
                  <tr>
                    <td colSpan={7} style={{ padding: '3rem', textAlign: 'center', color: '#555' }}>
                      문의 내역이 없습니다
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          )}
        </div>

        {/* 페이지네이션 */}
        {totalPages > 1 && (
          <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', gap: '0.5rem', padding: '1rem', borderTop: '1px solid #2D2D2D' }}>
            <button
              disabled={page === 0}
              onClick={() => setPage(p => p - 1)}
              style={{ padding: '0.375rem 0.75rem', background: page === 0 ? '#1A1A1A' : '#2A2A2A', border: '1px solid #2D2D2D', borderRadius: '4px', color: page === 0 ? '#444' : '#E5E5E5', fontSize: '0.8125rem', cursor: page === 0 ? 'default' : 'pointer' }}
            >
              이전
            </button>
            <span style={{ fontSize: '0.8125rem', color: '#888' }}>
              {page + 1} / {totalPages} ({fmtNum(total)}건)
            </span>
            <button
              disabled={page >= totalPages - 1}
              onClick={() => setPage(p => p + 1)}
              style={{ padding: '0.375rem 0.75rem', background: page >= totalPages - 1 ? '#1A1A1A' : '#2A2A2A', border: '1px solid #2D2D2D', borderRadius: '4px', color: page >= totalPages - 1 ? '#444' : '#E5E5E5', fontSize: '0.8125rem', cursor: page >= totalPages - 1 ? 'default' : 'pointer' }}
            >
              다음
            </button>
          </div>
        )}
      </div>

      {/* 답변 모달 */}
      {replyModal && (
        <div style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.7)', backdropFilter: 'blur(4px)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 100 }}>
          <div style={{ background: '#1A1A1A', border: '1px solid #2D2D2D', borderRadius: '16px', padding: '2rem', width: '720px', maxWidth: '90vw', maxHeight: '90vh', overflowY: 'auto' }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '1.5rem' }}>
              <h3 style={{ fontSize: '1.125rem', fontWeight: 700, color: '#E5E5E5' }}>답변 작성</h3>
              <button onClick={() => setReplyModal(null)} style={{ background: 'none', border: 'none', color: '#888', fontSize: '1.25rem', cursor: 'pointer' }}>✕</button>
            </div>

            {/* 문의 정보 */}
            <div style={{ background: '#111', borderRadius: '8px', padding: '0.75rem 1rem', marginBottom: '1rem', fontSize: '0.8125rem' }}>
              <div style={{ display: 'flex', gap: '1.5rem', marginBottom: '0.375rem' }}>
                <div><span style={{ color: '#666' }}>마켓: </span><span style={{ color: '#E5E5E5', fontWeight: 600 }}>{replyModal.market}</span></div>
                {replyModal.questioner && <div><span style={{ color: '#666' }}>질문자: </span><span style={{ color: '#E5E5E5' }}>{replyModal.questioner}</span></div>}
              </div>
              {replyModal.product_name && <div style={{ color: '#aaa', marginBottom: '0.375rem' }}>{replyModal.product_name}</div>}
              <div style={{ color: '#ccc', lineHeight: '1.5', whiteSpace: 'pre-wrap' }}>{htmlToText(replyModal.content || '')}</div>
            </div>

            {/* CS 자동화 — Claude AI 초안 안내 (아래 입력창에 자동 채움, 검토 후 전송) */}
            {replyModal.reply_status === 'pending' && replyModal.draft_status === 'suggested' && replyModal.draft_reply && (
              <div style={{ background: 'rgba(186,104,255,0.08)', border: '1px solid rgba(186,104,255,0.3)', borderRadius: '8px', padding: '0.625rem 0.875rem', marginBottom: '0.75rem', fontSize: '0.75rem', color: '#BA68FF', display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                <span style={{ fontWeight: 700 }}>AI 초안</span>
                <span style={{ color: '#9a7bbf' }}>
                  의도 {replyModal.intent || '-'}
                  {replyModal.draft_confidence != null ? ` · 신뢰도 ${Math.round(replyModal.draft_confidence * 100)}%` : ''}
                  {' · 아래 입력창에 채워졌습니다. 검토 후 전송하세요.'}
                </span>
              </div>
            )}

            {/* 템플릿 카드 그리드 — 추가/수정/삭제 인라인 */}
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: '0.5rem', marginBottom: '0.75rem' }}>
              {Object.entries(templates).map(([key, tpl]) => (
                <div
                  key={key}
                  style={{ background: selectedTemplate === key ? 'rgba(255,140,0,0.08)' : '#111', border: `1px solid ${selectedTemplate === key ? '#FF8C00' : '#2D2D2D'}`, borderRadius: '8px', padding: '0.625rem', transition: 'border-color 0.15s', position: 'relative' }}
                  onMouseEnter={e => { if (selectedTemplate !== key) e.currentTarget.style.borderColor = '#444' }}
                  onMouseLeave={e => { if (selectedTemplate !== key) e.currentTarget.style.borderColor = '#2D2D2D' }}
                >
                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '0.375rem' }}>
                    <div
                      style={{ fontSize: '0.75rem', fontWeight: 600, color: selectedTemplate === key ? '#FF8C00' : '#E5E5E5', cursor: 'pointer', flex: 1 }}
                      onClick={() => { setSelectedTemplate(key); setReplyText(sanitizeReplyTextForInquiry(tpl.content, replyModal)) }}
                    >{tpl.name}</div>
                    <div style={{ display: 'flex', gap: '0.25rem', flexShrink: 0 }}>
                      <button
                        onClick={e => { e.stopPropagation(); openEditTemplate(key, tpl) }}
                        style={{ background: 'none', border: 'none', color: '#888', fontSize: '0.65rem', cursor: 'pointer', padding: '0.1rem 0.25rem', lineHeight: 1 }}
                        title='수정'
                      >✏</button>
                      <button
                        onClick={e => { e.stopPropagation(); deleteTemplate(key, tpl.name) }}
                        style={{ background: 'none', border: 'none', color: '#666', fontSize: '0.65rem', cursor: 'pointer', padding: '0.1rem 0.25rem', lineHeight: 1 }}
                        title='삭제'
                      >✕</button>
                    </div>
                  </div>
                  <div
                    style={{ fontSize: '0.625rem', color: '#777', lineHeight: '1.4', maxHeight: '3.5rem', overflow: 'hidden', whiteSpace: 'pre-wrap', wordBreak: 'break-word', cursor: 'pointer' }}
                    onClick={() => { setSelectedTemplate(key); setReplyText(sanitizeReplyTextForInquiry(tpl.content, replyModal)) }}
                  >{tpl.content.slice(0, 80)}...</div>
                </div>
              ))}
              {/* 새 템플릿 추가 카드 */}
              <div
                onClick={openNewTemplate}
                style={{ background: '#111', border: '1px dashed #3D3D3D', borderRadius: '8px', padding: '0.625rem', cursor: 'pointer', display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '0.375rem', transition: 'border-color 0.15s', minHeight: '72px' }}
                onMouseEnter={e => (e.currentTarget.style.borderColor = '#FF8C00')}
                onMouseLeave={e => (e.currentTarget.style.borderColor = '#3D3D3D')}
              >
                <span style={{ fontSize: '1rem', color: '#555' }}>+</span>
                <span style={{ fontSize: '0.75rem', color: '#666' }}>새 템플릿</span>
              </div>
            </div>

            {/* 변수 태그 버튼 */}
            <div style={{ display: 'flex', gap: '0.375rem', flexWrap: 'wrap', marginBottom: '0.5rem' }}>
              {replyVariableTags.map(v => (
                <button
                  key={v.tag}
                  type="button"
                  onClick={() => insertTag(replyTextRef, setReplyText, replyText, v.tag)}
                  style={{ padding: '0.2rem 0.5rem', fontSize: '0.6875rem', background: '#1A1A1A', border: '1px solid #444', borderRadius: '4px', color: '#FF8C00', cursor: 'pointer' }}
                >{v.tag} <span style={{ color: '#888' }}>{v.label}</span></button>
              ))}
            </div>

            {/* 답변 입력 */}
            <textarea
              ref={replyTextRef}
              value={replyText}
              onChange={e => setReplyText(e.target.value)}
              placeholder="답변 내용을 입력하세요"
              rows={6}
              style={{ ...inputStyle, resize: 'vertical', fontFamily: 'inherit', lineHeight: '1.5', marginBottom: '1rem' }}
            />

            <div style={{ display: 'flex', gap: '0.75rem', justifyContent: 'flex-end' }}>
              <button onClick={() => setReplyModal(null)} style={{ padding: '0.625rem 1.25rem', background: 'transparent', border: '1px solid #2D2D2D', borderRadius: '8px', color: '#888', fontSize: '0.875rem', cursor: 'pointer' }}>취소</button>
              <button onClick={handleReply} style={{ padding: '0.625rem 1.25rem', background: '#FF8C00', border: 'none', borderRadius: '8px', color: '#fff', fontSize: '0.875rem', fontWeight: 600, cursor: 'pointer' }}>답변 등록</button>
            </div>
          </div>
        </div>
      )}

      {/* 답변 템플릿 추가/수정 모달 — SMS 템플릿과 동일한 UX */}
      {templateEditModal && (
        <div style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.8)', backdropFilter: 'blur(4px)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 200 }}>
          <div style={{ background: '#1A1A1A', border: '1px solid #2D2D2D', borderRadius: '16px', padding: '1.5rem', width: '520px', maxWidth: '90vw' }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '1.25rem' }}>
              <h3 style={{ fontSize: '1rem', fontWeight: 700, color: '#E5E5E5' }}>{isNewTemplate ? '새 템플릿 추가' : '템플릿 수정'}</h3>
              <button onClick={() => setTemplateEditModal(null)} style={{ background: 'none', border: 'none', color: '#888', fontSize: '1.25rem', cursor: 'pointer' }}>✕</button>
            </div>
            <div style={{ marginBottom: '0.75rem' }}>
              <label style={{ fontSize: '0.8125rem', color: '#888', display: 'block', marginBottom: '0.375rem' }}>템플릿 이름</label>
              <input
                type='text'
                value={templateEditModal.name}
                onChange={e => setTemplateEditModal({ ...templateEditModal, name: e.target.value })}
                placeholder='예: 배송지연안내'
                style={{ width: '100%', padding: '0.5rem 0.75rem', background: '#111', border: '1px solid #2D2D2D', borderRadius: '8px', color: '#E5E5E5', fontSize: '0.875rem', outline: 'none', boxSizing: 'border-box' }}
              />
            </div>
            <div style={{ marginBottom: '0.5rem' }}>
              <label style={{ fontSize: '0.8125rem', color: '#888', display: 'block', marginBottom: '0.375rem' }}>답변 내용</label>
              <div style={{ display: 'flex', gap: '0.375rem', flexWrap: 'wrap', marginBottom: '0.375rem' }}>
                {VARIABLE_TAGS.map(v => (
                  <button
                    key={v.tag}
                    type='button'
                    onClick={() => insertTag(tplContentRef, val => setTemplateEditModal(prev => prev ? { ...prev, content: val } : prev), templateEditModal.content, v.tag)}
                    style={{ padding: '0.2rem 0.5rem', fontSize: '0.6875rem', background: '#1A1A1A', border: '1px solid #444', borderRadius: '4px', color: '#FF8C00', cursor: 'pointer' }}
                  >{v.tag} <span style={{ color: '#888' }}>{v.label}</span></button>
                ))}
              </div>
              <textarea
                ref={tplContentRef}
                value={templateEditModal.content}
                onChange={e => setTemplateEditModal({ ...templateEditModal, content: e.target.value })}
                rows={8}
                style={{ width: '100%', padding: '0.625rem 0.75rem', background: '#111', border: '1px solid #2D2D2D', borderRadius: '8px', color: '#E5E5E5', fontSize: '0.8125rem', outline: 'none', resize: 'vertical', fontFamily: 'inherit', lineHeight: '1.5', boxSizing: 'border-box' }}
              />
            </div>
            <div style={{ display: 'flex', gap: '0.75rem', justifyContent: 'flex-end', marginTop: '1rem' }}>
              <button onClick={() => setTemplateEditModal(null)} style={{ padding: '0.5rem 1rem', background: 'transparent', border: '1px solid #2D2D2D', borderRadius: '8px', color: '#888', fontSize: '0.875rem', cursor: 'pointer' }}>취소</button>
              <button
                onClick={saveTemplate}
                disabled={!templateEditModal.name.trim() || !templateEditModal.content.trim()}
                style={{ padding: '0.5rem 1rem', background: '#FF8C00', border: 'none', borderRadius: '8px', color: '#fff', fontSize: '0.875rem', fontWeight: 600, cursor: 'pointer', opacity: (!templateEditModal.name.trim() || !templateEditModal.content.trim()) ? 0.5 : 1 }}
              >저장</button>
            </div>
          </div>
        </div>
      )}
      {/* 교환 액션 선택 모달 */}
      {exchangeActionItem && (
        <div style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.7)', backdropFilter: 'blur(4px)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 100 }}>
          <div style={{ background: '#1A1A1A', border: '1px solid #2D2D2D', borderRadius: '16px', padding: '2rem', width: '360px', maxWidth: '90vw' }}>
            <h3 style={{ fontSize: '1.125rem', fontWeight: 700, color: '#E5E5E5', marginBottom: '0.5rem' }}>교환요청 처리</h3>
            <p style={{ fontSize: '0.8125rem', color: '#888', marginBottom: '1.5rem' }}>주문번호: {exchangeActionItem.market_order_id || '-'}</p>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '0.75rem' }}>
              <button
                onClick={() => handleExchangeAction(exchangeActionItem, 'reship')}
                style={{ padding: '0.75rem', background: 'rgba(76,154,255,0.1)', border: '1px solid rgba(76,154,255,0.3)', borderRadius: '8px', color: '#4C9AFF', fontSize: '0.875rem', fontWeight: 600, cursor: 'pointer' }}
              >교환재배송</button>
              <button
                onClick={() => handleExchangeAction(exchangeActionItem, 'reject')}
                style={{ padding: '0.75rem', background: 'rgba(255,107,107,0.1)', border: '1px solid rgba(255,107,107,0.3)', borderRadius: '8px', color: '#FF6B6B', fontSize: '0.875rem', fontWeight: 600, cursor: 'pointer' }}
              >교환거부</button>
              <button
                onClick={() => handleExchangeAction(exchangeActionItem, 'convert_return')}
                style={{ padding: '0.75rem', background: 'rgba(255,165,0,0.1)', border: '1px solid rgba(255,165,0,0.3)', borderRadius: '8px', color: '#FFA500', fontSize: '0.875rem', fontWeight: 600, cursor: 'pointer' }}
              >반품변경</button>
            </div>
            <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: '1rem' }}>
              <button onClick={() => setExchangeActionItem(null)} style={{ padding: '0.625rem 1.25rem', background: 'transparent', border: '1px solid #2D2D2D', borderRadius: '8px', color: '#888', fontSize: '0.875rem', cursor: 'pointer' }}>닫기</button>
            </div>
          </div>
        </div>
      )}

      {/* 11번가 교환 거부 사유 입력 모달 */}
      {rejectReasonModal && rejectTargetItem && (
        <div style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.7)', backdropFilter: 'blur(4px)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 101 }}>
          <div style={{ background: '#1A1A1A', border: '1px solid #2D2D2D', borderRadius: '16px', padding: '2rem', width: '400px', maxWidth: '90vw' }}>
            <h3 style={{ fontSize: '1.125rem', fontWeight: 700, color: '#E5E5E5', marginBottom: '0.5rem' }}>교환거부 사유 입력</h3>
            <p style={{ fontSize: '0.8125rem', color: '#888', marginBottom: '1.25rem' }}>주문번호: {rejectTargetItem.market_order_id || '-'}</p>
            <input
              type="text"
              value={rejectReasonText}
              onChange={e => setRejectReasonText(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter') submitElevenstExchangeReject() }}
              placeholder="거부 사유를 입력하세요"
              autoFocus
              style={{ width: '100%', padding: '0.625rem 0.75rem', background: '#111', border: '1px solid #444', borderRadius: '8px', color: '#E5E5E5', fontSize: '0.875rem', boxSizing: 'border-box', marginBottom: '1.25rem' }}
            />
            <div style={{ display: 'flex', gap: '0.75rem', justifyContent: 'flex-end' }}>
              <button
                onClick={() => { setRejectReasonModal(false); setRejectTargetItem(null) }}
                style={{ padding: '0.625rem 1.25rem', background: 'transparent', border: '1px solid #2D2D2D', borderRadius: '8px', color: '#888', fontSize: '0.875rem', cursor: 'pointer' }}
              >취소</button>
              <button
                onClick={submitElevenstExchangeReject}
                style={{ padding: '0.625rem 1.25rem', background: 'rgba(255,107,107,0.15)', border: '1px solid rgba(255,107,107,0.4)', borderRadius: '8px', color: '#FF6B6B', fontSize: '0.875rem', fontWeight: 600, cursor: 'pointer' }}
              >거부 확정</button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
