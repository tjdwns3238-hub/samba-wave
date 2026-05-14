/**
 * content-tracking-ssg.js
 *
 * SSG 주문상세 → .tx_state 영역에서 택배사 + 송장번호 추출.
 * URL: https://pay.ssg.com/myssg/orderInfoDetail.ssg?orordNo={주문번호}
 * 출처: overlink-invoice-extension content-ssg.js 셀렉터 검증.
 */
;(() => {
  'use strict'

  function isOrderCancelled() {
    try {
      const text = (document.body?.innerText || '').slice(0, 8000)
      return /(취소완료|취소처리완료|구매취소완료|주문이\s*취소|취소된\s*주문)/.test(text)
    } catch { return false }
  }

  async function waitFor(selector, timeoutMs = 12000) {
    const start = Date.now()
    while (Date.now() - start < timeoutMs) {
      const el = document.querySelector(selector)
      if (el) return el
      await new Promise(r => setTimeout(r, 300))
    }
    return null
  }

  function isLoginRedirect() {
    try {
      const href = location.href || ''
      // SSG 로그인 페이지로 튕긴 경우 — 주문상세 URL(orderInfoDetail.ssg)이 사라짐
      if (href.indexOf('member.ssg.com') !== -1 || href.indexOf('/member/login') !== -1) return true
      if (href && href.indexOf('orderInfoDetail.ssg') === -1) return true
      return false
    } catch { return false }
  }

  async function scrape() {
    if (isLoginRedirect()) {
      return { success: false, needsLogin: true, error: 'needs_login: SSG 로그인 페이지로 리다이렉트' }
    }
    if (isOrderCancelled()) {
      return { success: false, cancelled: true, error: 'order_cancelled' }
    }
    const container = await waitFor('.tx_state em', 12000)
    if (!container) {
      // 12초 후에도 미로드면 리다이렉트 가능성 재확인
      if (isLoginRedirect()) {
        return { success: false, needsLogin: true, error: 'needs_login: SSG 로그인 페이지로 리다이렉트' }
      }
      return { success: false, error: 'no_tracking: .tx_state 미로드 (미발송)' }
    }
    const courierEl = container.querySelector('span')
    const courierName = (courierEl?.textContent || '').trim()
    let trackingNumber = ''
    for (const node of container.childNodes) {
      if (node.nodeType === 3) {
        const m = node.textContent.match(/\d{8,}/)
        if (m) { trackingNumber = m[0]; break }
      }
    }
    if (!trackingNumber) {
      return { success: false, error: 'no_tracking: 송장번호 미표시', courierName }
    }
    return { success: true, courierName, trackingNumber }
  }

  function send(requestId, payload) {
    try { chrome.runtime.sendMessage({ type: 'TRACKING_RESULT', requestId, ...payload }) } catch {}
  }

  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg?.type === 'TRACKING_REQUEST') {
      sendResponse({ ack: true })
      scrape().then(r => send(msg.requestId, r))
        .catch(err => send(msg.requestId, { success: false, error: String(err?.message || err) }))
      return true
    }
    return false
  })
})()
