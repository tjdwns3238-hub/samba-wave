/**
 * content-musinsa-claimlist.js
 *
 * 무신사 마이페이지에서 반품 회수송장 수집 (CS 답변용).
 *
 * 동작:
 *   - 로그인 세션으로 get_claim_list API 직접 fetch (DOM/SPA 무관)
 *   - 회수 택배사(returnDeliveryCompanyName) + 회수송장(returnDeliveryNo) 있는 항목 추출
 *   - background로 전송 → 백엔드 POST /musinsa/save-return-tracking
 *   - orderNo == samba 주문 sourcing_order_number 로 매칭되어 return_collect_* 저장
 *
 * get_claim_list 응답 한 콜에 orderNo/orderOptionNo/returnDeliveryNo 전부 포함되므로
 * trace 페이지 진입·버튼 클릭·returnId 불필요. (2026-06-08 CDP 실측 확정)
 */
;(() => {
  'use strict'

  let lastSentKeySet = ''
  let ran = false

  function fmtDate(d) {
    const y = d.getFullYear()
    const m = String(d.getMonth() + 1).padStart(2, '0')
    const day = String(d.getDate()).padStart(2, '0')
    return `${y}-${m}-${day}`
  }

  async function fetchClaims() {
    // 최근 90일 반품 — size 100 (페이지당 최대). 회수송장은 최근 건만 의미 있음.
    const end = new Date()
    const start = new Date(end.getTime() - 90 * 24 * 60 * 60 * 1000)
    const url =
      `/order-service/my/claim/get_claim_list?size=100&searchText=` +
      `&startDate=${fmtDate(start)}&endDate=${fmtDate(end)}`
    const r = await fetch(url, {
      credentials: 'include',
      headers: { Accept: 'application/json' },
    })
    if (!r.ok) throw new Error(`get_claim_list HTTP ${r.status}`)
    const j = await r.json()
    return (((j.data || {}).claimList || {}).myPageClaimList) || []
  }

  function extractReturnTracking(claimList) {
    const items = []
    const seen = new Set()
    for (const rep of claimList) {
      for (const u of rep.myPageClaimUnitList || []) {
        const orderNo = String(u.orderNo || u.rootOrderNo || '')
        const trackingNo = String(u.returnDeliveryNo || '')
        if (!orderNo || !trackingNo) continue // 회수 미발송 = 스킵
        const courier = String(u.returnDeliveryCompanyName || '')
        const key = `${orderNo}|${trackingNo}`
        if (seen.has(key)) continue
        seen.add(key)
        items.push({ orderNo, courier, trackingNo })
      }
    }
    return items
  }

  async function run() {
    if (ran) return
    ran = true
    try {
      const claimList = await fetchClaims()
      const items = extractReturnTracking(claimList)
      if (items.length === 0) {
        console.log('[무신사 회수송장] 회수송장 있는 반품 없음')
        return
      }
      const keys = items
        .map((i) => `${i.orderNo}|${i.trackingNo}`)
        .sort()
        .join(',')
      if (keys === lastSentKeySet) return
      lastSentKeySet = keys
      const resp = await chrome.runtime.sendMessage({
        type: 'MUSINSA_SAVE_RETURN_TRACKING',
        items,
      })
      if (resp?.ok) {
        console.log(
          `[무신사 회수송장] ${resp.updated}건 저장 (받은 ${resp.received}, 미매칭 ${resp.notMatched}, 스킵 ${resp.skipped})`
        )
      } else {
        console.warn('[무신사 회수송장] 저장 실패:', resp?.error || resp?.status)
      }
    } catch (e) {
      console.warn('[무신사 회수송장] 수집 실패:', e?.message || e)
    } finally {
      // 다음 mypage 진입/탭전환 때 재수집 허용
      setTimeout(() => {
        ran = false
      }, 60000)
    }
  }

  // 마이페이지 로드 후 한 박자 늦게 실행 (세션/쿠키 준비)
  setTimeout(run, 1500)
})()
