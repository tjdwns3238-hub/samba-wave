// SAMBA-WAVE offscreen — 무신사 발주취소 fetch 전담
//
// 배경: MV3 ServiceWorker 의 fetch 는 musinsa HTTPOnly 세션쿠키를 자동 첨부하지 못함
// → 로그인 상태 인식 못 함. offscreen document(hidden DOM context)에서 fetch 호출하면
// 일반 페이지처럼 cookies 자동 attach 되는지 검증 필요.
//
// background ServiceWorker 가 chrome.runtime.sendMessage 로 요청 → 여기서 fetch → 응답.

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || msg.target !== 'offscreen-cancel') return false
  if (msg.type === 'FETCH') {
    ;(async () => {
      const { url, method = 'GET', headers = {}, formFields, isJson } = msg
      try {
        const init = { method, credentials: 'include', headers }
        if (formFields) {
          const fd = new FormData()
          for (const [k, v] of Object.entries(formFields)) fd.append(k, v == null ? '' : String(v))
          init.body = fd
        } else if (isJson && msg.body) {
          init.body = JSON.stringify(msg.body)
          init.headers = { ...headers, 'Content-Type': 'application/json' }
        }
        const r = await fetch(url, init)
        const txt = await r.text()
        sendResponse({ ok: true, status: r.status, body: txt, headers: Object.fromEntries(r.headers.entries()) })
      } catch (e) {
        sendResponse({ ok: false, error: String(e?.message || e) })
      }
    })()
    return true // async
  }
  return false
})
