/**
 * content-tracking-musinsa.js
 *
 * 무신사 배송조회 — 2-hop 흐름 (2026-05-14 변경):
 *   1) /order/order-detail/{ord_no} 진입 → "배송 조회" 버튼 클릭 → trace 페이지로 navigation
 *   2) /order-service/my/delivery/trace?ord_no=...&ord_opt_no=... → DOM 스크랩
 *
 * 직접 trace URL 접근은 "정상적인 접근이 아닙니다" 거부됨 (ord_opt_no 필수).
 * 같은 탭 navigation이므로 requestId 를 sessionStorage 에 저장해서 이어받음.
 *
 * DOM:
 *   배송조회 버튼:   button > span (텍스트 "배송 조회") — OrderStatusButton__ButtonItem
 *   택배사:    p.company-name
 *   송장번호:  button.tracking-number
 */
;(() => {
  'use strict'

  const MAX_WAIT_MS = 15000
  const POLL_INTERVAL = 300
  const SS_KEY = 'samba_tracking_request_id'

  function isOrderCancelled() {
    try {
      const text = (document.body?.innerText || '').slice(0, 8000)
      return /(취소완료|취소처리완료|구매취소완료|주문이\s*취소|취소된\s*주문)/.test(text)
    } catch { return false }
  }

  function isCaptcha() {
    const title = (document.title || '').toLowerCase()
    if (title.includes('보안 인증') || title.includes('captcha')) return true
    const body = (document.body?.innerText || '').slice(0, 500)
    if (body.includes('보안 인증')) return true
    return false
  }

  function isAbnormalAccess() {
    try {
      const text = (document.body?.innerText || '').slice(0, 2000)
      return /정상적인\s*접근이\s*아닙니다/.test(text)
    } catch { return false }
  }

  // 현재 로그인 무신사 계정에 해당 주문이 없는 경우 — 다른 계정 주문
  // (주문상세 페이지가 정상 로드됐는데 주문번호와 매칭되는 데이터가 없는 패턴)
  function isWrongAccount() {
    try {
      const text = (document.body?.innerText || '').slice(0, 4000)
      if (/주문\s*정보가?\s*(없|존재하지)/.test(text)) return true
      if (/조회\s*(된|할\s*수\s*있는)?\s*주문이?\s*(없|존재하지)/.test(text)) return true
      if (/잘못된\s*접근/.test(text)) return true
      return false
    } catch { return false }
  }

  async function waitFor(selector, timeoutMs) {
    const start = Date.now()
    while (Date.now() - start < timeoutMs) {
      const el = document.querySelector(selector)
      if (el && (el.textContent || '').trim()) return el
      await new Promise((r) => setTimeout(r, POLL_INTERVAL))
    }
    return null
  }

  // 주문상세 페이지에서 "배송 조회" 버튼 탐색 (텍스트 매칭 — class 변경에 견고)
  async function findTraceButton(timeoutMs) {
    const start = Date.now()
    while (Date.now() - start < timeoutMs) {
      const buttons = Array.from(document.querySelectorAll('button'))
      const btn = buttons.find(b => {
        const t = (b.textContent || '').replace(/\s+/g, '').trim()
        return t === '배송조회' || t.includes('배송조회')
      })
      if (btn && !btn.disabled) return btn
      await new Promise((r) => setTimeout(r, POLL_INTERVAL))
    }
    return null
  }

  // trace 페이지에서 DOM 스크랩
  async function scrapeTrace() {
    if (isCaptcha()) {
      return { success: false, error: 'captcha' }
    }
    if (isAbnormalAccess()) {
      return { success: false, error: 'abnormal_access: 정상적인 접근이 아닙니다 (ord_opt_no 누락 또는 세션 만료)' }
    }
    if (isOrderCancelled()) {
      return { success: false, cancelled: true, error: 'order_cancelled' }
    }
    const courierEl = await waitFor('p.company-name', MAX_WAIT_MS)
    if (!courierEl) {
      return { success: false, error: '택배사 DOM 미로드 (미발송 가능)' }
    }
    const courierName = courierEl.textContent.trim()
    const trackingEl = document.querySelector('button.tracking-number')
    const trackingNumber = (trackingEl?.textContent || '').trim()
    if (!trackingNumber) {
      return {
        success: false,
        error: 'no_tracking: 송장번호 없음 (아직 미발송)',
        courierName,
      }
    }
    const params = new URLSearchParams(location.search)
    const ordNo = params.get('ord_no') || ''
    return { success: true, courierName, trackingNumber, ordNo }
  }

  function send(requestId, payload) {
    try {
      chrome.runtime.sendMessage({
        type: 'TRACKING_RESULT',
        requestId,
        ...payload,
      })
    } catch (e) {
      console.warn('[송장-무신사] sendMessage 실패:', e)
    }
  }

  // 주문상세 페이지에서 배송조회 버튼 클릭 — 같은 탭 navigation 후 trace 페이지에서 이어받음
  async function clickTraceButton(requestId) {
    try {
      sessionStorage.setItem(SS_KEY, requestId)
    } catch {}

    if (isOrderCancelled()) {
      // 취소된 주문이면 더 진행하지 않고 결과 전송
      send(requestId, { success: false, cancelled: true, error: 'order_cancelled' })
      try { sessionStorage.removeItem(SS_KEY) } catch {}
      return
    }

    if (isWrongAccount()) {
      // 현재 로그인된 무신사 계정에 해당 주문이 존재하지 않음 — 다른 계정 주문
      send(requestId, { success: false, error: 'wrong_account: 현재 로그인 계정에 해당 주문 없음' })
      try { sessionStorage.removeItem(SS_KEY) } catch {}
      return
    }

    const btn = await findTraceButton(MAX_WAIT_MS)
    if (!btn) {
      // 배송조회 버튼이 없음 = 아직 배송이 시작되지 않은 단계 (택배사가 송장 발급 전)
      send(requestId, { success: false, error: '배송대기중' })
      try { sessionStorage.removeItem(SS_KEY) } catch {}
      return
    }
    try {
      btn.click()
    } catch (e) {
      send(requestId, { success: false, error: `배송조회 버튼 클릭 실패: ${e?.message || e}` })
      try { sessionStorage.removeItem(SS_KEY) } catch {}
      return
    }
    // 무신사는 Next.js SPA — 버튼 click이 history.pushState만 일으키는 경우가 많음.
    // 이 경우 content script는 재주입되지 않으므로(manifest matches는 페이지 로드 시점에만 평가)
    // 같은 컨텍스트에서 location.pathname을 폴링해 trace 페이지 도달을 감지하고 직접 스크랩.
    // 풀 페이지 reload가 발생하면 이 컨텍스트는 destroy되고, trace 페이지의 새 content script가
    // sessionStorage 의 requestId 로 이어받아 처리한다(아래 isTracePage 분기).
    const navStart = Date.now()
    while (Date.now() - navStart < MAX_WAIT_MS) {
      await new Promise((r) => setTimeout(r, POLL_INTERVAL))
      if (isTracePage()) {
        try { sessionStorage.removeItem(SS_KEY) } catch {}
        try {
          const res = await scrapeTrace()
          send(requestId, res)
        } catch (e) {
          send(requestId, { success: false, error: String(e?.message || e) })
        }
        return
      }
    }
    send(requestId, { success: false, error: 'trace 페이지 진입 타임아웃 (SPA navigation 실패)' })
    try { sessionStorage.removeItem(SS_KEY) } catch {}
  }

  function isOrderDetailPage() {
    return /\/order\/order-detail\//.test(location.pathname)
  }

  function isTracePage() {
    return /\/order-service\/my\/delivery\/trace/.test(location.pathname)
  }

  // background → TRACKING_REQUEST 수신 (order-detail 페이지에서만 유효)
  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg?.type === 'TRACKING_REQUEST') {
      sendResponse({ ack: true })
      if (isOrderDetailPage()) {
        clickTraceButton(msg.requestId).catch((err) =>
          send(msg.requestId, { success: false, error: String(err?.message || err) })
        )
      } else if (isTracePage()) {
        // 직접 trace 페이지로 들어왔거나, 이미 trace 상태인 경우 즉시 스크랩
        scrapeTrace()
          .then((res) => send(msg.requestId, res))
          .catch((err) =>
            send(msg.requestId, { success: false, error: String(err?.message || err) })
          )
      }
      return true
    }
    return false
  })

  // trace 페이지 자동 진입 — sessionStorage에 저장된 requestId 로 자동 스크랩
  if (isTracePage()) {
    let pendingReqId = null
    try {
      pendingReqId = sessionStorage.getItem(SS_KEY)
    } catch {}
    if (pendingReqId) {
      try { sessionStorage.removeItem(SS_KEY) } catch {}
      scrapeTrace()
        .then((res) => send(pendingReqId, res))
        .catch((err) =>
          send(pendingReqId, { success: false, error: String(err?.message || err) })
        )
    }
  }
})()
