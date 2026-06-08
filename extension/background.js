importScripts('config.js')
importScripts('background-core.js')

// 삼바웨이브 쿠키 연동 - 백그라운드 서비스 워커

const {
  API_PREFIX,
  CLOUD_URL,
  DEFAULT_PROXY_URL,
  DEFAULT_SELECTORS,
  apiFetch,
  loadSelectors,
  sendSiteCookieToProxy,
} = globalThis.SambaBackgroundCore

let PROXY_URL = DEFAULT_PROXY_URL

// ==================== KREAM 셀렉터 설정 (서버에서 동적 변경 가능) ====================

let selectors = { ...DEFAULT_SELECTORS }
loadSelectors(PROXY_URL).then(nextSelectors => {
  selectors = nextSelectors
})

// ==================== 쿠키 동기화 공용 함수 ====================

function makeScheduleSync(label, getCookie, sendFn) {
  let timer = null
  return function () {
    if (timer) clearTimeout(timer)
    timer = setTimeout(async () => {
      timer = null
      const cookie = getCookie()
      if (!cookie) return
      try {
        await sendFn(cookie)
        console.log(`[자동동기화] ${label} 쿠키 프록시 전송 완료`)
      } catch {
        console.log('[자동동기화] 프록시 미실행 (무시)')
      }
    }, 3000)
  }
}

// ==================== 무신사 쿠키 ====================

let capturedCookie = ''
let capturedAt = 0

// ==================== KREAM 쿠키 ====================

let kreamCookie = ''

// ==================== 롯데ON 쿠키 ====================

let lotteonCookie = ''

// 동기화 스케줄러 (sendCookiesToProxy 정의 후 초기화)
let scheduleCookieSync
let scheduleKreamCookieSync
let scheduleLotteonCookieSync

// 백엔드 URL 변경 감지
chrome.storage.onChanged.addListener((changes) => {
  if (changes.proxyUrl) {
    PROXY_URL = changes.proxyUrl.newValue || DEFAULT_PROXY_URL
    console.log(`[설정] 백엔드 URL 변경: ${PROXY_URL}`)
  }
})

// Service Worker 시작 시 저장된 쿠키 + 설정 복원
chrome.storage.local.get(['capturedCookie', 'capturedAt', 'kreamCookie', 'lotteonCookie', 'proxyUrl', '_lastAutoLoginSuccessAt']).then(async data => {
  if (data.proxyUrl) {
    PROXY_URL = data.proxyUrl
    console.log(`[복원] 백엔드 URL: ${PROXY_URL}`)
  } else {
    // proxyUrl 미설정 시 웹 확장앱은 프로덕션 서버를 기본값으로 사용
    PROXY_URL = CLOUD_URL
    chrome.storage.local.set({ proxyUrl: CLOUD_URL })
    console.log(`[초기화] 백엔드 URL 자동 설정: ${PROXY_URL}`)
  }
  // 무신사
  if (data.capturedCookie) {
    capturedCookie = data.capturedCookie
    capturedAt = data.capturedAt || 0
    console.log(`[복원] 무신사 쿠키 복원: ${capturedCookie.split(';').length}개`)
    try { await sendCookiesToProxy(capturedCookie) } catch {}
  }
  // KREAM
  if (data.kreamCookie) {
    kreamCookie = data.kreamCookie
    console.log(`[복원] KREAM 쿠키 복원: ${kreamCookie.split(';').length}개`)
    try { await sendKreamCookiesToProxy(kreamCookie) } catch {}
  }
  // 롯데ON
  if (data.lotteonCookie) {
    lotteonCookie = data.lotteonCookie
    console.log(`[복원] 롯데ON 쿠키 복원: ${lotteonCookie.split(';').length}개`)
    try { await sendLotteonCookiesToProxy(lotteonCookie) } catch {}
  }
  // 자동로그인 성공 시각 복원 — 서비스 워커 재시작 후에도 1시간 이내 로그인 이력 유지
  if (data._lastAutoLoginSuccessAt && typeof data._lastAutoLoginSuccessAt === 'object') {
    try {
      globalThis._lastAutoLoginSuccessAt = data._lastAutoLoginSuccessAt
      const entries = Object.entries(data._lastAutoLoginSuccessAt).map(([k, v]) => `${k}:${Math.round((Date.now()-v)/60000)}분전`).join(', ')
      console.log(`[복원] 자동로그인 성공시각 복원: ${entries}`)
    } catch {}
  }
})

// 무신사 webRequest 캡처
chrome.webRequest.onBeforeSendHeaders.addListener(
  (details) => {
    const cookieHeader = details.requestHeaders?.find(
      h => h.name.toLowerCase() === 'cookie'
    )
    if (cookieHeader?.value && cookieHeader.value !== capturedCookie) {
      capturedCookie = cookieHeader.value
      capturedAt = Date.now()
      chrome.storage.local.set({ capturedCookie, capturedAt })
      console.log(`[캡처] 무신사 쿠키 변경감지 ${capturedCookie.split(';').length}개`)
      scheduleCookieSync()
    }
  },
  { urls: ['https://*.musinsa.com/*'] },
  ['requestHeaders', 'extraHeaders']
)

// KREAM webRequest 캡처
chrome.webRequest.onBeforeSendHeaders.addListener(
  (details) => {
    const cookieHeader = details.requestHeaders?.find(
      h => h.name.toLowerCase() === 'cookie'
    )
    if (cookieHeader?.value && cookieHeader.value !== kreamCookie) {
      kreamCookie = cookieHeader.value
      chrome.storage.local.set({ kreamCookie })
      console.log(`[캡처] KREAM 쿠키 변경감지 ${kreamCookie.split(';').length}개`)
      scheduleKreamCookieSync()
    }
  },
  { urls: ['https://*.kream.co.kr/*'] },
  ['requestHeaders', 'extraHeaders']
)

// 롯데ON webRequest 캡처
chrome.webRequest.onBeforeSendHeaders.addListener(
  (details) => {
    const cookieHeader = details.requestHeaders?.find(
      h => h.name.toLowerCase() === 'cookie'
    )
    if (cookieHeader?.value && cookieHeader.value !== lotteonCookie) {
      lotteonCookie = cookieHeader.value
      chrome.storage.local.set({ lotteonCookie })
      console.log(`[캡처] 롯데ON 쿠키 변경감지 ${lotteonCookie.split(';').length}개`)
      scheduleLotteonCookieSync()
    }
  },
  { urls: ['https://*.lotteon.com/*'] },
  ['requestHeaders', 'extraHeaders']
)

// ==================== 공용 결과 전송 함수 ====================

async function postResult(endpoint, body) {
  // 503/429/502/504 일시적 장애 자동 재시도 (지수 백오프 0.5s/1.5s/3s)
  // 백엔드가 일시적으로 응답 못 하면 결과가 유실되어 가격 갱신 차단으로 이어짐
  const url = `${PROXY_URL}${API_PREFIX}/${endpoint}`
  const headers = { 'Content-Type': 'application/json' }
  const payload = JSON.stringify(body)
  const RETRY_STATUSES = new Set([429, 502, 503, 504])
  const RETRY_DELAYS = [500, 1500, 3000]

  for (let attempt = 0; attempt <= RETRY_DELAYS.length; attempt++) {
    let res
    try {
      res = await apiFetch(url, { method: 'POST', headers, body: payload })
    } catch (e) {
      if (attempt < RETRY_DELAYS.length) {
        await new Promise(r => setTimeout(r, RETRY_DELAYS[attempt]))
        continue
      }
      console.warn(`[결과전송] ${endpoint} 네트워크 실패: ${e.message}`)
      return
    }
    if (res.ok) return
    if (RETRY_STATUSES.has(res.status) && attempt < RETRY_DELAYS.length) {
      console.log(`[결과전송] ${endpoint} HTTP ${res.status} → ${RETRY_DELAYS[attempt]}ms 후 재시도(${attempt+1}/${RETRY_DELAYS.length})`)
      await new Promise(r => setTimeout(r, RETRY_DELAYS[attempt]))
      continue
    }
    console.warn(`[결과전송] ${endpoint} 실패: HTTP ${res.status}`)
    return
  }
}

// ==================== 프록시 전송 함수 ====================

async function sendCookiesToProxy(cookieStr) {
  return sendSiteCookieToProxy({ proxyUrl: PROXY_URL, site: 'musinsa', cookieStr })
}

async function sendKreamCookiesToProxy(cookieStr) {
  return sendSiteCookieToProxy({ proxyUrl: PROXY_URL, site: 'kream', cookieStr })
}

async function sendLotteonCookiesToProxy(cookieStr) {
  return sendSiteCookieToProxy({ proxyUrl: PROXY_URL, site: 'lotteon', cookieStr })
}

// 동기화 스케줄러 초기화
scheduleCookieSync = makeScheduleSync('무신사', () => capturedCookie, sendCookiesToProxy)
scheduleKreamCookieSync = makeScheduleSync('KREAM', () => kreamCookie, sendKreamCookiesToProxy)
scheduleLotteonCookieSync = makeScheduleSync('롯데ON', () => lotteonCookie, sendLotteonCookiesToProxy)

// ==================== 무신사 잔액 수신 (content script → background → server) ====================

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.action === 'musinsaBalance') {
    const { money, mileage, username, expired } = msg
    if (expired) {
      console.log(`[잔액] 쿠키 만료 감지 — 재로그인 필요`)
      getProfileEmailAndSend({ money: -1, mileage: -1, username, expired: true })
    } else {
      console.log(`[잔액] 무신사 잔액 수신: 머니 ${money?.toLocaleString()} / 적립금 ${mileage?.toLocaleString()} / 유저: ${username}`)
      getProfileEmailAndSend({ money, mileage, username })
    }
    sendResponse({ ok: true })
  }
  if (msg.action === 'abcmartBalance') {
    const { siteName, money, mileage, username, expired } = msg
    console.log(`[잔액] ${siteName} 잔액 수신: 머니 ${money?.toLocaleString()} / 적립금 ${mileage?.toLocaleString()} / 유저: ${username}`)
    sendAbcmartBalance({ siteName, money, mileage, username, expired: !!expired })
    sendResponse({ ok: true })
  }
  if (msg.action === 'abcmartMembership') {
    const { membershipRate, membershipGrade, needsCookie, expired } = msg
    console.log(`[ABCmart] 멤버십 감지: ${membershipGrade} (${membershipRate}%) cookie=${needsCookie ? 'fetch' : 'skip'} expired=${!!expired}`)
    if (!expired) {
      chrome.storage.local.set({ abcmart_membership_rate: membershipRate, abcmart_membership_grade: membershipGrade })
    } else {
      // ABC마트 만료 확정 신호 — 즉시 자동로그인 트리거 (상품 처리 큐 대기 안 함)
      const abcActive = typeof isSiteActiveForSourcing === 'function'
        && (isSiteActiveForSourcing('ABCmart') || isSiteActiveForSourcing('GrandStage'))
      if (abcActive && typeof reportLoginFailure === 'function') {
        reportLoginFailure('ABCmart', true)
      } else {
        console.log('[ABCmart] expired membership signal ignored - no active ABCmart/GrandStage sourcing job')
      }
    }
    handleAbcmartMembershipSync({ rate: membershipRate, grade: membershipGrade, needsCookie: !!needsCookie, expired: !!expired })
    sendResponse({ ok: true })
  }
  if (msg.type === 'MUSINSA_SAVE_OPT_NOS') {
    // content-musinsa-orderlist.js에서 추출한 ord_no→ord_opt_no 매핑 일괄 저장
    ;(async () => {
      try {
        const stored = await chrome.storage.local.get('proxyUrl')
        const proxyUrl = stored.proxyUrl || ''
        if (!proxyUrl) { sendResponse({ ok: false, error: 'no proxyUrl' }); return }
        const url = `${proxyUrl}/api/v1/samba/musinsa/save-opt-nos`
        const apiFetch = globalThis.SambaBackgroundCore?.apiFetch
        const init = {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ mappings: msg.mappings || [] }),
        }
        const res = apiFetch ? await apiFetch(url, init) : await fetch(url, init)
        if (!res.ok) { sendResponse({ ok: false, status: res.status }); return }
        const data = await res.json()
        sendResponse({ ok: true, ...data })
      } catch (e) {
        sendResponse({ ok: false, error: e?.message || String(e) })
      }
    })()
    return true // 비동기 응답
  }
  if (msg.type === 'MUSINSA_SAVE_RETURN_TRACKING') {
    // content-musinsa-claimlist.js에서 추출한 회수송장(orderNo+택배사+송장) 일괄 저장
    ;(async () => {
      try {
        const stored = await chrome.storage.local.get('proxyUrl')
        const proxyUrl = stored.proxyUrl || ''
        if (!proxyUrl) { sendResponse({ ok: false, error: 'no proxyUrl' }); return }
        const url = `${proxyUrl}/api/v1/samba/musinsa/save-return-tracking`
        const apiFetch = globalThis.SambaBackgroundCore?.apiFetch
        const init = {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ items: msg.items || [] }),
        }
        const res = apiFetch ? await apiFetch(url, init) : await fetch(url, init)
        if (!res.ok) { sendResponse({ ok: false, status: res.status }); return }
        const data = await res.json()
        sendResponse({ ok: true, ...data })
      } catch (e) {
        sendResponse({ ok: false, error: e?.message || String(e) })
      }
    })()
    return true // 비동기 응답
  }
  if (msg.type === 'SCRAPE_SSG_SCORES') {
    scrapeSSGScores().then(data => sendResponse(data)).catch(e => sendResponse({ error: e.message }))
    return true // 비동기 응답
  }
  return false
})

// ==================== 무신사 아이디 추출 (웹: chrome.identity 방식) ====================

async function getProfileEmailAndSend({ money, mileage, username, expired: isExpired } = {}) {
  let profileEmail = ''
  try {
    const info = await chrome.identity.getProfileUserInfo({ accountStatus: 'ANY' })
    profileEmail = info.email || ''
    console.log(`[잔액] 크롬 프로필 이메일: ${profileEmail}`)
  } catch (e) {
    console.log(`[잔액] 프로필 이메일 조회 실패: ${e.message}`)
  }
  sendMusinsaBalance({ money, mileage, profileEmail, username, cookie: capturedCookie, expired: !!isExpired })
}

async function sendMusinsaBalance(data) {
  try {
    const res = await apiFetch(`${PROXY_URL}/api/v1/samba/sourcing-accounts/sync-balance`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    })
    if (res.ok) {
      const result = await res.json()
      console.log(`[잔액] 서버 저장 완료:`, result)
    } else {
      console.warn(`[잔액] 서버 저장 실패: HTTP ${res.status}`)
    }
  } catch (e) {
    console.log(`[잔액] 서버 전송 실패 (무시): ${e.message}`)
  }
}

// ==================== 무신사 쿠키 조회 ====================

async function getMusinsaCookies() {
  if (!capturedCookie) {
    const data = await chrome.storage.local.get(['capturedCookie', 'capturedAt'])
    if (data.capturedCookie) {
      capturedCookie = data.capturedCookie
      capturedAt = data.capturedAt || 0
    }
  }

  if (capturedCookie) {
    const count = capturedCookie.split(';').length
    const age = Math.round((Date.now() - capturedAt) / 1000)
    return {
      cookies: Array.from({ length: count }, (_, i) => ({ domain: '.musinsa.com', name: `c${i}`, value: '' })),
      cookieStr: capturedCookie,
      isLoggedIn: true,
      cookieNames: [`✅ webRequest 캡처: ${count}개 (${age}초 전)`],
    }
  }

  const all = []
  const seen = new Set()
  for (const url of ['https://www.musinsa.com', 'https://member.one.musinsa.com']) {
    try {
      const cookies = await chrome.cookies.getAll({ url })
      for (const c of cookies) {
        const key = `${c.domain}|${c.name}`
        if (!seen.has(key)) { seen.add(key); all.push(c) }
      }
    } catch {}
  }
  const cookies = all.filter(c => c.value && /^[\x21-\x7E]{1,8000}$/.test(c.value))
  return {
    cookies: all,
    cookieStr: cookies.map(c => `${c.name}=${c.value}`).join('; '),
    isLoggedIn: all.length > 0,
    cookieNames: all.map(c => `${c.domain}:${c.name}`),
  }
}


importScripts('background-kream.js')
importScripts('background-autologin.js')
importScripts('recipe-cache.js')
importScripts('recipe-executor.js')
importScripts('background-sourcing.js')
importScripts('background-bootstrap.js')
importScripts('background-messages.js')
