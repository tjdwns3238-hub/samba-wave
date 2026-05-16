// 사이트 자동 로그인 엔진
// kream-auto-review v5.2.10 service-worker.js의 _ensureLoggedInSingle 패턴 이식
// Chrome/웨일 비밀번호 관리자에 저장된 첫 번째 계정으로 자동 로그인 수행
//
// 핵심 트릭:
//  1. autocomplete="off" / "new-password" 강제 해제 + 페이지 리로드 → Chrome 자동완성 재평가 유도
//  2. chrome.debugger triple-click(clickCount 1→2→3) → 자동완성 값을 .value에 확정 (Chrome 보안 우회)
//  3. :-webkit-autofill 감지 시 form POST 폴백 (.value 빈 경우)
//  4. chrome.debugger Input.dispatchMouseEvent → trusted click으로 로그인 버튼 클릭
//
// 의존성: background-kream.js의 waitForTabLoad, wait, pauseCollectPolling

const AUTO_LOGIN_SITES = {
  musinsa: {
    name: '무신사',
    loginUrl: 'https://www.musinsa.com/auth/login',
    checkUrl: 'https://www.musinsa.com/mypage/myreview',
    isLoginPage: url => url.includes('/auth/login') || url.includes('member.one.musinsa.com/login') || (url.includes('/login') && url.includes('musinsa')),
    loginButtonSelector: 'button[type="submit"], button.login-btn, form button',
  },
  kream: {
    name: 'KREAM',
    loginUrl: 'https://kream.co.kr/login',
    checkUrl: 'https://kream.co.kr/my/reviews?tab=to_write',
    isLoginPage: url => url.includes('/login'),
    loginButtonSelector: 'button.btn.full.solid, button[type="submit"], .login_btn, button.btn_login',
  },
  abcmart: {
    name: 'ABC마트',
    loginUrl: 'https://abcmart.a-rt.com/login',
    checkUrl: 'https://abcmart.a-rt.com/mypage/claim/claim-order-main?orderPrdtStatCodeClick=10007',
    isLoginPage: url => url.includes('/login'),
    loginButtonSelector: '#login, input#login, button[type="submit"], .btn_login, button.login',
  },
  lotteon: {
    name: '롯데ON',
    loginUrl: 'https://www.lotteon.com/p/member/login/common',
    checkUrl: 'https://www.lotteon.com/p/review/myLotte/reviewWriteListTab',
    isLoginPage: url => url.includes('/login') || url.includes('/member/login'),
    loginButtonSelector: 'button[type="submit"], .btn_login, #loginBtn',
  },
  ssg: {
    name: 'SSG',
    loginUrl: 'https://member.ssg.com/m/member/login.ssg',
    checkUrl: 'https://www.ssg.com/myssg/activityMng/pdtEvalList.ssg?quick=pdtEvalList',
    isLoginPage: url => url.includes('login.ssg') || url.includes('/member/login'),
    loginButtonSelector: 'button[type="submit"], .btn_login, #btn_login',
  },
  gs: {
    name: 'GS샵',
    loginUrl: 'https://www.gsshop.com/cust/login/login.gs',
    checkUrl: 'https://www.gsshop.com/ord/dlvcursta/ordList.gs',
    isLoginPage: url => url.includes('login.gs') || url.includes('/login'),
    loginButtonSelector: '#btnLogin, button[type="submit"], .btn_login, #loginBtn',
  },
}

// 사이트별 자동 로그인 상태 (중복 호출 차단 + 실패 누적 추적)
const autoLoginState = {
  inProgress: {},
  lastAttemptAt: {},
  failedAttempts: {},
  cooldownUntil: {},
}

const AUTO_LOGIN_MAX_RETRIES = 3
const AUTO_LOGIN_COOLDOWN_MS = 5 * 60 * 1000 // 5분간 재시도 차단 (실패 누적 후)
const AUTO_LOGIN_PAUSE_MS = 15 * 1000 // 자동로그인 진행 중 폴링 일시중지 시간

// 오토튠 활성 상태 캐시 — 자동로그인 트리거 전에 체크하여 오토튠 OFF 상태에서는 작동 안 함
let _alAutotuneActiveCache = { value: null, at: 0 }
const _AL_AUTOTUNE_CACHE_MS = 5000

async function _isAutotuneActive() {
  const now = Date.now()
  if (_alAutotuneActiveCache.value !== null && now - _alAutotuneActiveCache.at < _AL_AUTOTUNE_CACHE_MS) {
    return _alAutotuneActiveCache.value
  }
  try {
    const stored = await chrome.storage.local.get('proxyUrl')
    const proxyUrl = stored.proxyUrl || ''
    // X-Api-Key 헤더 자동 부착하는 apiFetch 사용 (raw fetch는 ApiGatewayMiddleware에 의해 403)
    const apiFetch = globalThis.SambaBackgroundCore?.apiFetch
    const res = apiFetch
      ? await apiFetch(`${proxyUrl}/api/v1/samba/collector/autotune/status`, { method: 'GET' })
      : await fetch(`${proxyUrl}/api/v1/samba/collector/autotune/status`, { method: 'GET' })
    if (!res.ok) return _alAutotuneActiveCache.value
    const data = await res.json()
    const active = !!data.running
    _alAutotuneActiveCache = { value: active, at: now }
    return active
  } catch (e) {
    console.log(`[자동로그인] 오토튠 상태 조회 실패 (무시): ${e.message}`)
    return _alAutotuneActiveCache.value
  }
}

function alExternalSiteToKey(externalSite) {
  // background-sourcing.js에서 쓰는 사이트 키를 자동로그인 키로 매핑
  // 'ABCmart' / 'GrandStage' → 'abcmart', 'LOTTEON' → 'lotteon', 'SSG' → 'ssg', 'MUSINSA' → 'musinsa', 'KREAM' → 'kream', 'GSShop' → 'gs'
  const map = {
    ABCmart: 'abcmart',
    GrandStage: 'abcmart',
    LOTTEON: 'lotteon',
    SSG: 'ssg',
    MUSINSA: 'musinsa',
    KREAM: 'kream',
    GSShop: 'gs',
  }
  return map[externalSite] || null
}

// 자동로그인 키 → 백엔드 SambaSourcingAccount.site_name 매핑 (라디오로 지정한 기본 계정 조회용)
const _AL_SITE_NAME_MAP = {
  lotteon: 'LOTTEON',
  abcmart: 'ABCmart',
  ssg: 'SSG',
  musinsa: 'MUSINSA',
  kream: 'KREAM',
  gs: 'GSShop',
}

// 백엔드 fetch — 자격증명 조회.
// accountId 제공 시 해당 계정 단건(주문 매칭 계정), 없으면 site_name 라디오 기본 계정.
async function _fetchLoginCredential(siteKey, accountId) {
  const siteName = _AL_SITE_NAME_MAP[siteKey]
  if (!siteName && !accountId) return null
  try {
    const stored = await chrome.storage.local.get('proxyUrl')
    const proxyUrl = stored.proxyUrl || ''
    const apiFetch = globalThis.SambaBackgroundCore?.apiFetch
    if (!apiFetch) return null
    const qs = accountId
      ? `account_id=${encodeURIComponent(accountId)}`
      : `site_name=${encodeURIComponent(siteName)}`
    const res = await apiFetch(
      `${proxyUrl}/api/v1/samba/sourcing-accounts/login-credential?${qs}`,
      { method: 'GET' }
    )
    if (!res.ok) return null  // 404면 미지정/미존재
    return await res.json()
  } catch (e) {
    console.log(`[자동로그인] 자격증명 조회 실패 (무시): ${e.message}`)
    return null
  }
}

// SPA 직접 로그인 — Chrome 자동완성 의존 없이 .value 직접 설정 + button.click()
// LOTTEON / ABCmart / SSG처럼 vanilla input + form submit 구조의 사이트에서 작동
// (검증 완료: 가짜 자격증명으로도 click()이 서버 응답까지 도달함을 확인)
async function _spaDirectLogin(siteKey, username, password) {
  const site = AUTO_LOGIN_SITES[siteKey]
  if (!site) return false

  // [계정 전환] 정식 로그아웃 URL 호출 → 서버가 세션 expire + Set-Cookie 로 클라 쿠키 정리.
  // 쿠키 직접 삭제는 서버 세션 잔존 + localStorage 잔여 + 무신사 보안 비정상 패턴 감지 위험.
  const _LOGOUT_URLS = {
    musinsa: 'https://www.musinsa.com/auth/logout',
    ssg: 'https://www.ssg.com/comm/login/logout.ssg',
    lotteon: 'https://www.lotteon.com/p/member/logout',
    abcmart: 'https://abcmart.a-rt.com/member/logout',
  }
  const _logoutUrl = _LOGOUT_URLS[siteKey]
  if (_logoutUrl) {
    let logoutTabId = null
    try {
      const logoutTab = await chrome.tabs.create({ url: _logoutUrl, active: false })
      logoutTabId = logoutTab.id
      try { await waitForTabLoad(logoutTabId, 15000) } catch {}
      await wait(1500)  // 로그아웃 처리 + Set-Cookie 적용 대기
      console.log(`[자동로그인][SPA] ${site.name} 정식 로그아웃 완료 (계정 전환 위해)`)
    } catch (e) {
      console.warn(`[자동로그인][SPA] 로그아웃 호출 실패: ${e?.message || e}`)
    } finally {
      if (logoutTabId) {
        try { await chrome.tabs.remove(logoutTabId) } catch {}
      }
    }
  }

  let tabId = null
  let tabCreated = false

  try {
    // 로그인 페이지 — 기존 브라우저 새 탭으로 오픈 (minimized window 사용 X, 사용자 요구사항)
    const tab = await chrome.tabs.create({ url: site.loginUrl, active: false })
    tabId = tab.id
    tabCreated = true

    try { await waitForTabLoad(tabId, 30000) } catch {}
    await wait(2000)

    // SPA 사이트는 input이 동적 렌더링 — input 등장까지 폴링 (최대 10초)
    // 무신사는 /auth/login → member.one.musinsa.com/login 리다이렉트 후 SPA 렌더링됨
    const SPA_INPUT_WAIT_SITES = ['musinsa', 'lotteon']
    if (SPA_INPUT_WAIT_SITES.includes(siteKey)) {
      const spaStart = Date.now()
      const SPA_WAIT_MAX = 10000
      while (Date.now() - spaStart < SPA_WAIT_MAX) {
        try {
          const [r] = await chrome.scripting.executeScript({
            target: { tabId },
            func: () => {
              const id = document.querySelector('input[type="email"], input[type="text"]:not([type="hidden"])')
              const pw = document.querySelector('input[type="password"]')
              return !!(id && id.offsetParent !== null && pw && pw.offsetParent !== null)
            },
          })
          if (r?.result) {
            console.log(`[자동로그인][SPA] ${site.name} input 렌더링 감지 (${Date.now() - spaStart}ms)`)
            break
          }
        } catch {}
        await wait(300)
      }
    }

    // alert dialog 자동 닫기 핸들러 — chrome.debugger Page.handleJavaScriptDialog
    // (가짜 자격증명 또는 잘못된 자격증명 시 alert로 에러 메시지 노출됨, freeze 방지용)
    const target = { tabId }
    let dialogAttached = false
    let dialogMessage = null
    try {
      await chrome.debugger.attach(target, '1.3')
      await chrome.debugger.sendCommand(target, 'Page.enable', {})
      dialogAttached = true
      const dialogHandler = (src, method, params) => {
        if (src.tabId === tabId && method === 'Page.javascriptDialogOpening') {
          dialogMessage = params?.message || ''
          console.log(`[자동로그인][SPA] alert 닫기: "${dialogMessage.substring(0, 80)}"`)
          chrome.debugger.sendCommand(target, 'Page.handleJavaScriptDialog', { accept: true }).catch(() => {})
        }
      }
      chrome.debugger.onEvent.addListener(dialogHandler)

      // 사이트별 셀렉터 + .value 직접 설정 + event dispatch + button.click()
      const [scriptResult] = await chrome.scripting.executeScript({
        target: { tabId },
        func: (siteKeyArg, usernameArg, passwordArg) => {
          // 사이트별 input/button 셀렉터
          const SELECTORS = {
            lotteon: {
              id: ['#inId', 'input[name="inId"]'],
              pw: ['#Password', 'input[type="password"]'],
              btnId: '[data-cmpnt-name="login_btn_select"]',
            },
            abcmart: {
              id: ['#username', 'input[name="username"]'],
              pw: ['#password', 'input[type="password"]'],
              btnId: '#login',
            },
            ssg: {
              // member.ssg.com 실측(2026-05-15): id="inp_id" name="mbrLoginId" / pw id="inp_pw" / 버튼 id="loginBtn"
              id: ['#inp_id', 'input[name="mbrLoginId"]', '#userId', 'input[name="userId"]', 'input[name="usrId"]', 'input[type="email"]'],
              pw: ['#inp_pw', 'input[type="password"]'],
              btnId: '#loginBtn, #btn_login, .btn_login, button[type="submit"]',
            },
            musinsa: {
              // member.one.musinsa.com/login — SPA, selector 실측 불가 → 흔한 패턴 다 등록
              // 첫 매칭 input 사용. 첫 실행 시 콘솔 로그에서 어떤 selector가 매칭됐는지 확인.
              id: [
                '#id', '#userId', '#loginId', '#email',
                'input[name="id"]', 'input[name="userId"]', 'input[name="loginId"]', 'input[name="email"]', 'input[name="memberId"]',
                'input[type="email"]', 'input[type="text"]:not([type="hidden"])',
              ],
              pw: ['#password', '#userPw', '#loginPw', 'input[name="password"]', 'input[name="pw"]', 'input[type="password"]'],
              btnId: 'button[type="submit"], button.login-btn, button.btn-login, #loginBtn',
              btnText: '로그인',
            },
          }
          const sel = SELECTORS[siteKeyArg]
          if (!sel) return { success: false, error: 'unsupported site' }

          // ID/PW 필드 찾기 — 첫 매칭 셀렉터 사용 + 디버그 로그
          let idField = null
          let idSelMatched = null
          for (const s of sel.id) {
            const el = document.querySelector(s)
            if (el) { idField = el; idSelMatched = s; break }
          }
          let pwField = null
          let pwSelMatched = null
          for (const s of sel.pw) {
            const el = document.querySelector(s)
            if (el) { pwField = el; pwSelMatched = s; break }
          }
          if (!idField || !pwField) {
            // 디버그 — 페이지의 input 목록 dump
            const allInputs = Array.from(document.querySelectorAll('input')).map(i => ({
              id: i.id, name: i.name, type: i.type, placeholder: i.placeholder,
              visible: i.offsetParent !== null,
            }))
            return {
              success: false,
              error: 'fields not found',
              idFound: !!idField,
              pwFound: !!pwField,
              allInputs: allInputs.slice(0, 20),
              currentUrl: location.href,
            }
          }
          console.log(`[자동로그인][SPA] selector 매칭: id="${idSelMatched}" pw="${pwSelMatched}"`)

          // Vue 3 reactive 필드는 .value 직접 설정이 v-model에 안 잡힘.
          // native setter로 값 주입 후 input 이벤트 dispatch — Vue/React 공통 패턴.
          const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set
          idField.focus()
          nativeSetter.call(idField, usernameArg)
          idField.dispatchEvent(new Event('input', { bubbles: true }))
          idField.dispatchEvent(new Event('change', { bubbles: true }))

          pwField.focus()
          nativeSetter.call(pwField, passwordArg)
          pwField.dispatchEvent(new Event('input', { bubbles: true }))
          pwField.dispatchEvent(new Event('change', { bubbles: true }))

          // 로그인 버튼 찾기 — id 셀렉터 우선, 미발견 시 텍스트 매칭 폴백
          let btn = null
          if (sel.btnId) {
            btn = document.querySelector(sel.btnId)
          }
          if (!btn && sel.btnText) {
            btn = Array.from(document.querySelectorAll('button, input[type="submit"], input[type="button"], a[role="button"]'))
              .find(b => {
                const txt = (b.textContent || b.value || '').trim()
                return txt === sel.btnText && b.offsetParent !== null
              })
          }
          if (!btn) return { success: false, error: 'login button not found' }

          // disabled 풀고 click
          btn.disabled = false
          btn.classList?.remove('disabled')
          btn.click()
          return { success: true, idValue: idField.value.length, pwLen: pwField.value.length }
        },
        args: [siteKey, username, password],
      })

      const r = scriptResult?.result
      if (!r?.success) {
        console.log(`[자동로그인][SPA] ${site.name} 스크립트 실행 실패:`, JSON.stringify(r))
        chrome.debugger.onEvent.removeListener(dialogHandler)
        return false
      }
      console.log(`[자동로그인][SPA] ${site.name} .value 설정 + click() 완료 (id=${r.idValue}자, pw=${r.pwLen}자) — 응답 폴링`)

      // 응답 폴링 — URL 변경 감지 (로그인 성공 시 isLoginPage=false)
      const POLL_INTERVAL = 1500
      const TIMEOUT = 15000
      const startTime = Date.now()
      // 자격증명 오류로 간주할 alert 메시지 — 다단어 구문만 사용.
      // 단독 키워드("비밀번호"/"확인" 등)는 부수 alert("비밀번호를 변경해 주세요" 등)에
      // 걸려 false-positive 알람을 유발하므로 사용 금지.
      const CREDENTIAL_ERROR_PHRASES = [
        '일치하지 않',
        '일치하는 회원',
        '아이디 또는',
        '비밀번호가 일치',
        '비밀번호를 잘못',
        '비밀번호를 다시',
        '비밀번호가 틀',
        '비밀번호를 확인',
        '아이디를 잘못',
        '아이디를 다시',
        '아이디를 확인',
        '존재하지 않',
        '잘못 입력',
        '잘못되었',
        '재입력',
        '5회 이상',
        '5회를 초과',
        '캡차',
        'captcha',
        '보안문자',
        '로그인 정보가',
        '회원이 아닙',
      ]
      while (Date.now() - startTime < TIMEOUT) {
        await wait(POLL_INTERVAL)

        // URL이 로그인 페이지를 벗어났으면 alert 유무와 관계없이 성공으로 처리
        // (부수 alert가 떴어도 실제 로그인은 이미 완료됨)
        let tabInfo = null
        try { tabInfo = await chrome.tabs.get(tabId) } catch {
          chrome.debugger.onEvent.removeListener(dialogHandler)
          return false
        }
        const urlLeftLoginPage = !site.isLoginPage(tabInfo.url || '')

        // 자격증명 오류 alert만 실패로 간주 (URL이 로그인 페이지에 남아있을 때만)
        if (dialogMessage && dialogMessage.length > 0 && !urlLeftLoginPage) {
          const msgLower = dialogMessage.toLowerCase()
          const looksLikeCredentialError = CREDENTIAL_ERROR_PHRASES.some(p => msgLower.includes(p.toLowerCase()))
          if (looksLikeCredentialError) {
            console.log(`[자동로그인][SPA] ${site.name} 자격증명 오류 alert: "${dialogMessage.substring(0, 60)}"`)
            chrome.debugger.onEvent.removeListener(dialogHandler)
            return false
          }
          // 부수 alert는 메시지만 초기화하고 계속 대기
          console.log(`[자동로그인][SPA] ${site.name} 부수 alert 무시: "${dialogMessage.substring(0, 60)}"`)
          dialogMessage = null
        }

        // 오토튠 진행 중 취소 감지
        const stillActive = await _isAutotuneActive()
        if (stillActive === false) {
          console.log(`[자동로그인][SPA] ${site.name} 오토튠 취소 — 중단`)
          chrome.debugger.onEvent.removeListener(dialogHandler)
          return false
        }

        try {
          if (urlLeftLoginPage) {
            // LOTTEON: URL 이탈만으로 부족 — #memInfo.mbNo 실제 확인 (비로그인 리다이렉트 오판 방지)
            if (siteKey === 'lotteon') {
              await wait(2000)
              try {
                const [ck] = await chrome.scripting.executeScript({
                  target: { tabId },
                  func: () => {
                    const el = document.querySelector('#memInfo')
                    try {
                      const mbNo = el ? JSON.parse(el.value || '{}')?.mbNo || null : null
                      if (mbNo) return 'MEMINFO'
                    } catch {}
                    for (const script of document.querySelectorAll('script')) {
                      const text = script.textContent || ''
                      if (!text || (!text.includes('memInfo') && !text.includes('mbNo'))) continue
                      const m =
                        text.match(/["']mbNo["']\s*:\s*["']([^"']{2,})["']/)
                        || text.match(/\bmbNo\s*:\s*["']([^"']{2,})["']/)
                      if (m?.[1]) return 'SCRIPT'
                    }
                    const headerText = (
                      document.querySelector('header, #header, .header, [class*="header"], nav, [class*="gnb"]')?.innerText
                      || (document.body?.innerText || '').substring(0, 400)
                    ).replace(/\s+/g, ' ')
                    if (['濡쒓렇?꾩썐', '留덉씠濡?뜲', 'MY LOTTE', '二쇰Ц諛곗넚'].some(token => headerText.includes(token))) {
                      return 'HEADER'
                    }
                    if (['濡쒓렇???뚯썝媛??', '濡쒓렇??', '?뚯썝媛??'].some(token => headerText.includes(token))) {
                      return null
                    }
                    return null
                  },
                })
                const mbNo = ck?.result
                if (mbNo) {
                  console.log(`[자동로그인][SPA] ✅ ${site.name} 로그인 확인 — mbNo: ${String(mbNo).slice(0, 4)}****`)
                  chrome.debugger.onEvent.removeListener(dialogHandler)
                  return true
                }
                // mbNo 없어도 URL이 로그인 페이지를 벗어난 것 자체가 로그인 성공 증거
                // (헤더 텍스트 체크 인코딩 문제로 매칭 불가 — URL 이탈 기준으로 단순화)
                // 틀린 자격증명은 위의 alert dialog 핸들러가 이미 처리함
                console.log(`[자동로그인][SPA] ✅ ${site.name} URL 이탈 확인 — #memInfo.mbNo 없음, 로그인 성공 처리`)
                chrome.debugger.onEvent.removeListener(dialogHandler)
                return true
              } catch (e) {
                console.log(`[자동로그인][SPA] ${site.name} mbNo 체크 오류: ${e.message} — URL 이탈로 성공 처리`)
                chrome.debugger.onEvent.removeListener(dialogHandler)
                return true
              }
            } else {
              console.log(`[자동로그인][SPA] ✅ ${site.name} 로그인 성공 — URL: ${tabInfo.url}`)
              chrome.debugger.onEvent.removeListener(dialogHandler)
              return true
            }
          }
        } catch {
          chrome.debugger.onEvent.removeListener(dialogHandler)
          return false
        }
      }

      console.log(`[자동로그인][SPA] ${site.name} 타임아웃 (${TIMEOUT / 1000}초) — 로그인 페이지 잔존`)
      chrome.debugger.onEvent.removeListener(dialogHandler)
      return false
    } catch (err) {
      console.error(`[자동로그인][SPA] ${site.name} 예외:`, err.message)
      return false
    } finally {
      if (dialogAttached) {
        try { await chrome.debugger.detach(target) } catch {}
      }
    }
  } finally {
    if (tabCreated && tabId) {
      try { await chrome.tabs.remove(tabId) } catch {}
    }
  }
}

// 동시 진입 race 차단용 in-flight Map — 어떤 await보다 먼저 동기적으로 체크.
// 기존 autoLoginState.inProgress 가드는 첫 await(_isAutotuneActive) 이후에 설정되어
// 같은 tick에 fire-and-forget으로 들어온 호출들이 모두 통과 → 자동로그인 탭 폭증.
// 이 Map은 같은 사이트의 동시 호출을 즉시 같은 Promise로 합쳐 반환한다.
const _ensureLoggedInInflight = new Map()  // siteKey → Promise<boolean>

// 진입점 — 외부에서 자동로그인을 트리거할 때 호출 (3회 재시도)
// opts.accountId — 주문 매칭 계정으로 강제 로그인 (송장 수집 등 계정별 격리 필요시)
function ensureLoggedIn(siteKey, opts) {
  const accountId = (opts && opts.accountId) || ''
  // accountId별 inflight key — 같은 사이트라도 계정별로는 독립 처리
  const inflightKey = accountId ? `${siteKey}::${accountId}` : siteKey
  if (_ensureLoggedInInflight.has(inflightKey)) {
    return _ensureLoggedInInflight.get(inflightKey)
  }
  const p = (async () => {
    try {
      return await _ensureLoggedInImpl(siteKey, accountId)
    } finally {
      _ensureLoggedInInflight.delete(inflightKey)
    }
  })()
  _ensureLoggedInInflight.set(inflightKey, p)
  return p
}

async function _ensureLoggedInImpl(siteKey, accountId) {
  const site = AUTO_LOGIN_SITES[siteKey]
  if (!site) {
    console.log(`[자동로그인] 미지원 사이트: ${siteKey}`)
    return false
  }

  // 오토튠 비활성 상태면 자동로그인 차단 (사용자가 작업 취소했는데 계속 시도되는 것 방지)
  // 단, accountId 명시(송장 자동수집 등 명시적 사용자 트리거)는 오토튠 게이트 우회 — 별도 기능.
  if (!accountId) {
    const autotuneActive = await _isAutotuneActive()
    if (autotuneActive === false) {
      console.log(`[자동로그인] ${site.name} 트리거 차단 — 오토튠 비활성 상태`)
      return false
    }
  } else {
    console.log(`[자동로그인] ${site.name} 오토튠 게이트 우회 — accountId 명시 트리거`)
  }

  // 중복 호출 차단 — 이미 진행 중이면 즉시 false (in-flight Map과 별개로 cooldown/실패카운트용)
  // accountId 명시(송장 자동수집)는 inflight Map(_ensureLoggedInInflight)으로 이미 격리되므로 우회.
  if (!accountId && autoLoginState.inProgress[siteKey]) {
    console.log(`[자동로그인] ${site.name} 이미 진행 중 — 무시`)
    return false
  }

  // 쿨다운 체크 — 실패 누적 후 일정 시간 차단. accountId 명시 트리거는 쿨다운 무시.
  if (!accountId) {
    const cooldownUntil = autoLoginState.cooldownUntil[siteKey] || 0
    if (Date.now() < cooldownUntil) {
      const remainSec = Math.ceil((cooldownUntil - Date.now()) / 1000)
      console.log(`[자동로그인] ${site.name} 쿨다운 중 (${remainSec}초 남음) — 무시`)
      return false
    }
  }

  // 계정별 최근 성공 캐시 체크 — 같은 계정으로 10분 이내 로그인 확인됐으면 스킵
  // (송장수집 100건 잡 돌릴 때 매 주문마다 ensureLoggedIn 트리거되는 비용 + alert 폭주 차단)
  const ACCOUNT_LOGIN_TTL_MS = 10 * 60 * 1000
  try {
    const cache = globalThis._lastAutoLoginSuccessAt?.[siteKey]
    const accKey = accountId || '_default'
    const lastTs = (cache && typeof cache === 'object') ? (cache[accKey] || 0) : 0
    if (lastTs && (Date.now() - lastTs) < ACCOUNT_LOGIN_TTL_MS) {
      const ageSec = Math.round((Date.now() - lastTs) / 1000)
      console.log(`[자동로그인] ${site.name}(${accKey}) ${ageSec}초 전 성공 — 스킵`)
      return true
    }
  } catch {}

  autoLoginState.inProgress[siteKey] = true
  autoLoginState.lastAttemptAt[siteKey] = Date.now()

  // 자동로그인 진행 중에는 폴링 일시중지 (탭 폭주 차단)
  try {
    if (typeof pauseCollectPolling === 'function') {
      pauseCollectPolling(AUTO_LOGIN_PAUSE_MS, `auto-login ${site.name}`)
    }
  } catch {}

  try {
    let ok = false
    for (let attempt = 1; attempt <= AUTO_LOGIN_MAX_RETRIES; attempt++) {
      console.log(`[자동로그인] ${site.name} 시도 (${attempt}/${AUTO_LOGIN_MAX_RETRIES})`)
      ok = await _ensureLoggedInSingle(siteKey, accountId)
      if (ok) break
      if (attempt < AUTO_LOGIN_MAX_RETRIES) {
        await wait(3000)
      }
    }

    if (ok) {
      autoLoginState.failedAttempts[siteKey] = 0
      autoLoginState.cooldownUntil[siteKey] = 0
      // 자동로그인 성공 시각 기록 — [siteKey][accountId] 2계층 구조.
      // 같은 사이트라도 계정이 다르면 별도 캐시. accountId 없으면 '_default' 키로 저장.
      // storage 동기화로 서비스 워커 재시작 후에도 캐시 복원.
      try {
        globalThis._lastAutoLoginSuccessAt = globalThis._lastAutoLoginSuccessAt || {}
        const accKey = accountId || '_default'
        const prevSite = globalThis._lastAutoLoginSuccessAt[siteKey]
        // 기존에 number(구버전)였으면 객체로 마이그레이션
        const siteMap = (prevSite && typeof prevSite === 'object') ? prevSite : {}
        siteMap[accKey] = Date.now()
        globalThis._lastAutoLoginSuccessAt[siteKey] = siteMap
        chrome.storage.local.set({ _lastAutoLoginSuccessAt: globalThis._lastAutoLoginSuccessAt }).catch(() => {})
      } catch {}
      console.log(`[자동로그인] ✅ ${site.name} 성공 — 폴링 자동 재개`)
    } else {
      autoLoginState.failedAttempts[siteKey] = (autoLoginState.failedAttempts[siteKey] || 0) + 1
      autoLoginState.cooldownUntil[siteKey] = Date.now() + AUTO_LOGIN_COOLDOWN_MS
      console.log(`[자동로그인] ❌ ${site.name} ${AUTO_LOGIN_MAX_RETRIES}회 실패 — ${AUTO_LOGIN_COOLDOWN_MS / 60000}분 쿨다운`)
      // 알람은 라디오 기본 계정 모드(!accountId)만 발송 — accountId 명시 트리거(송장수집)는
      // 잡당 시도라 실패 알람 폭주 위험. 호출자가 wrong_account 로 분류해서 모달로 보여줌.
      if (!accountId) {
        try {
          chrome.notifications?.create?.(`autologin-fail-${siteKey}-${Date.now()}`, {
            type: 'basic',
            iconUrl: 'icon128.png',
            title: 'SAMBA-WAVE 자동로그인 실패',
            message: `${site.name} 자동 로그인이 실패했습니다. 브라우저에서 수동 로그인해주세요. (5분 후 자동 재시도)`,
          })
        } catch {}
      }
    }
    return ok
  } finally {
    autoLoginState.inProgress[siteKey] = false
    // 자동로그인 완료 시 폴링 즉시 재개 (90초 대기 없이)
    try {
      if (typeof globalThis.resumeCollectPolling === 'function') {
        globalThis.resumeCollectPolling()
      }
    } catch {}
  }
}

// 단일 사이트 로그인 시도
async function _ensureLoggedInSingle(siteKey, accountId) {
  const site = AUTO_LOGIN_SITES[siteKey]
  if (!site) return false

  // [SPA 분기] LOTTEON / ABCmart / SSG는 백엔드 라디오 지정 계정으로만 자동로그인
  // 사용자 요구 — 소싱처계정의 username/password를 직접 .value 설정 (Chrome 자동완성 드롭다운 사용 X)
  // 백엔드 자격증명 없으면 즉시 실패. chrome.debugger triple-click 폴백 제거 (드롭다운 노출 방지).
  const SPA_DIRECT_LOGIN_SITES = ['lotteon', 'abcmart', 'ssg', 'musinsa']
  if (SPA_DIRECT_LOGIN_SITES.includes(siteKey)) {
    // accountId 지정 시 — 사용자 요구 = 자동 로그아웃 + 새 계정 로그인 (송장수집 풀 자동화).
    // 사전 로그인 체크 스킵하고 _spaDirectLogin 진입 (그 함수가 쿠키 삭제 후 새 로그인 수행).
    // accountId 없을 때만(라디오 기본 모드) 이미 로그인됐는지 체크해서 스킵.
    if (!accountId) {
      let alreadyLoggedIn = false
      let spaCheckTabId = null
      try {
        const checkTab = await chrome.tabs.create({ url: site.checkUrl, active: false })
        spaCheckTabId = checkTab.id
        try { await waitForTabLoad(spaCheckTabId, 20000) } catch {}
        await wait(1500)
        const checkTabInfo = await chrome.tabs.get(spaCheckTabId)
        alreadyLoggedIn = !site.isLoginPage(checkTabInfo.url || '')
        try { await chrome.tabs.remove(spaCheckTabId) } catch {}
        spaCheckTabId = null
      } catch (e) {
        console.log(`[자동로그인] ${site.name} 사전 로그인 체크 실패 (무시): ${e.message}`)
        if (spaCheckTabId) try { await chrome.tabs.remove(spaCheckTabId) } catch {}
      }

      if (alreadyLoggedIn) {
        console.log(`[자동로그인] ${site.name} 이미 로그인됨 — 자동로그인 스킵 (라디오 모드)`)
        return true
      }
    }

    const credential = await _fetchLoginCredential(siteKey, accountId)
    if (credential?.username && credential?.password) {
      const tag = accountId ? `주문매칭:${credential.account_label}` : `라디오:${credential.account_label}`
      console.log(`[자동로그인] ${site.name} 백엔드 자격증명 사용 (${tag}) — SPA 직접 로그인 시도`)
      return await _spaDirectLogin(siteKey, credential.username, credential.password)
    }
    // 폴백 없이 즉시 중단 — 사용자가 설정 페이지에서 라디오 지정 필요
    console.log(`[자동로그인] ❌ ${site.name} 백엔드 자격증명 없음 — 자동로그인 중단. 설정 페이지에서 자동로그인 계정 라디오 지정 필요`)
    try {
      chrome.notifications?.create?.(`autologin-no-credential-${siteKey}-${Date.now()}`, {
        type: 'basic',
        iconUrl: 'icon128.png',
        title: 'SAMBA-WAVE 자동로그인 설정 필요',
        message: `${site.name} 자동로그인 계정이 지정되지 않았습니다. 설정 페이지 → 소싱처 계정에서 라디오 버튼으로 계정을 선택해주세요.`,
      })
    } catch {}
    return false
  }

  // 무신사/KREAM/ABC마트는 보안 스크립트가 무거워 타임아웃 30초
  // 롯데ON은 Vue SPA로 폼 동적 렌더링 + 로그인 후 리다이렉트가 느림 → 30초
  const LOGIN_TIMEOUT = (siteKey === 'musinsa' || siteKey === 'kream' || siteKey === 'abcmart' || siteKey === 'lotteon') ? 30000 : 15000
  // SPA 사이트는 로그인 페이지 HTML이 빈 div 뿐, JS로 input이 동적 렌더링됨
  // → reload하면 Chrome autofill 후보 0개로 결정되어 영구 미발동 → reload 스킵하고 동적 렌더링 대기
  const IS_SPA_LOGIN = (siteKey === 'lotteon')
  const POLL_INTERVAL = 2000

  let tabId = null
  let tabCreated = false

  try {
    // 1) checkUrl(마이페이지)로 이동 → 비로그인이면 로그인 페이지로 자동 리다이렉트
    // 기존 브라우저의 백그라운드 탭으로 오픈 (사용자 요구사항 — 별도 minimized window 띄우지 말것)
    // active: false로 사용자 현재 작업을 방해하지 않음
    const tab = await chrome.tabs.create({ url: site.checkUrl, active: false })
    tabId = tab.id
    tabCreated = true

    try { await waitForTabLoad(tabId, 30000) } catch {}
    await wait(1500) // 리다이렉트 완료 대기

    let tabInfo = await chrome.tabs.get(tabId)
    let currentUrl = tabInfo.url || ''

    // 이미 로그인 상태면 즉시 종료
    if (!site.isLoginPage(currentUrl)) {
      console.log(`[자동로그인] ${site.name} 이미 로그인됨`)
      try { await chrome.tabs.remove(tabId) } catch {}
      return true
    }

    // 2) 명시적으로 로그인 페이지로 이동
    if (!site.isLoginPage(currentUrl)) {
      await chrome.tabs.update(tabId, { url: site.loginUrl })
      try { await waitForTabLoad(tabId, 30000) } catch {}
    }

    // STEP A-pre: SPA 사이트는 input이 동적 렌더링될 때까지 폴링 대기 (최대 10초)
    // 롯데ON 등 Vue/React SPA는 로드 직후 <div id="app"></div>만 있음 → input 등장 대기 필수
    if (IS_SPA_LOGIN) {
      const SPA_WAIT_MAX = 10000
      const SPA_POLL = 300
      const spaStart = Date.now()
      let inputAppeared = false
      while (Date.now() - spaStart < SPA_WAIT_MAX) {
        try {
          const [r] = await chrome.scripting.executeScript({
            target: { tabId },
            func: () => {
              const visible = (el) => el && el.offsetParent !== null
              const id = document.querySelector('input[type="email"], input[type="text"]:not([type="hidden"])')
              const pw = document.querySelector('input[type="password"]')
              return { idVisible: visible(id), pwVisible: visible(pw) }
            },
          })
          if (r?.result?.idVisible && r?.result?.pwVisible) {
            inputAppeared = true
            console.log(`[자동로그인] ${site.name} SPA input 렌더링 감지 (${Date.now() - spaStart}ms)`)
            break
          }
        } catch {}
        await wait(SPA_POLL)
      }
      if (!inputAppeared) {
        console.log(`[자동로그인] ${site.name} SPA input 렌더링 타임아웃 (10초) — 진행은 계속`)
      }
    }

    // STEP A: autocomplete 차단 강제 해제
    // 일반 사이트: 속성 수정 후 reload → Chrome이 새 autocomplete 보고 autofill 재평가
    // SPA 사이트(롯데ON): reload 금지 — reload하면 input이 사라져 Chrome autofill 후보 0개로 결정되어 영구 미발동
    //   대신 input.dispatchEvent로 mutation 트리거하여 Chrome이 동적 변경을 감지하도록 유도
    try {
      const [acResult] = await chrome.scripting.executeScript({
        target: { tabId },
        func: (isSpa) => {
          let changed = 0
          document.querySelectorAll('input[autocomplete="off"]').forEach(inp => {
            if (inp.type === 'email' || inp.type === 'text') {
              inp.setAttribute('autocomplete', 'username email')
              changed++
            } else if (inp.type === 'password') {
              inp.setAttribute('autocomplete', 'current-password')
              changed++
            }
          })
          // ABC마트 등 "new-password"로 자동완성 차단하는 사이트 대응
          document.querySelectorAll('input[autocomplete="new-password"]').forEach(inp => {
            if (inp.type === 'password') {
              inp.setAttribute('autocomplete', 'current-password')
              changed++
            }
          })
          document.querySelectorAll('input:not([autocomplete])').forEach(inp => {
            if (inp.type === 'text' && (inp.name === 'username' || inp.name === 'userId' || inp.name === 'id' || inp.id === 'username')) {
              inp.setAttribute('autocomplete', 'username')
              changed++
            } else if (inp.type === 'password' && !inp.getAttribute('autocomplete')) {
              inp.setAttribute('autocomplete', 'current-password')
              changed++
            }
          })
          // SPA: 속성 변경 후 input event 발화로 Chrome autofill 재평가 유도
          if (isSpa && changed > 0) {
            document.querySelectorAll('input[type="text"], input[type="email"], input[type="password"]').forEach(inp => {
              try {
                inp.dispatchEvent(new Event('focus', { bubbles: true }))
                inp.dispatchEvent(new Event('blur', { bubbles: true }))
              } catch {}
            })
          }
          return changed
        },
        args: [IS_SPA_LOGIN],
      })
      const acChanged = acResult?.result || 0
      if (acChanged > 0) {
        if (IS_SPA_LOGIN) {
          console.log(`[자동로그인] ${site.name} autocomplete ${acChanged}개 필드 강제 해제 (SPA: reload 스킵, mutation 이벤트로 유도)`)
          await wait(1500)
        } else {
          console.log(`[자동로그인] ${site.name} autocomplete ${acChanged}개 필드 강제 해제 → 리로드`)
          await chrome.tabs.reload(tabId)
          try { await waitForTabLoad(tabId, 30000) } catch {}
          await wait(1500)
        }
      }
    } catch (e) {
      console.log(`[자동로그인] autocomplete 해제 실패 (무시): ${e.message}`)
    }

    // STEP B: 아이디 필드 chrome.debugger triple-click → 자동완성 값을 .value에 확정
    try {
      const [posResult] = await chrome.scripting.executeScript({
        target: { tabId },
        func: (key) => {
          // autocomplete 차단이 다시 설정되는 사이트 대응 — 한 번 더 해제
          document.querySelectorAll('input[autocomplete="off"]').forEach(inp => {
            if (inp.type === 'email' || inp.type === 'text') inp.setAttribute('autocomplete', 'username email')
            else if (inp.type === 'password') inp.setAttribute('autocomplete', 'current-password')
          })
          document.querySelectorAll('input[autocomplete="new-password"]').forEach(inp => {
            if (inp.type === 'password') inp.setAttribute('autocomplete', 'current-password')
          })

          let idField = null
          if (key === 'kream') {
            idField = document.querySelector('input[type="email"]')
          } else if (key === 'abcmart') {
            idField = document.querySelector('input#username, input[name="username"]')
          } else {
            idField = document.querySelector('input[type="email"], input#id, input[name="id"], input[name="userId"], input[name="username"], input[name="email"], input#username')
            if (!idField) {
              idField = Array.from(document.querySelectorAll('input[type="text"]')).find(i => i.offsetParent !== null)
            }
          }
          if (idField) {
            const r = idField.getBoundingClientRect()
            return { x: r.left + r.width / 2, y: r.top + r.height / 2, found: true }
          }
          return { found: false }
        },
        args: [siteKey],
      })

      const idPos = posResult?.result
      if (idPos?.found) {
        await _alTripleClick(tabId, idPos.x, idPos.y)
        console.log(`[자동로그인] ${site.name} 아이디 필드 triple-click 완료`)

        // SPA(롯데ON): 여러 계정이 저장되어 있어 Chrome autofill 드롭다운이 표시됨
        // ID 필드에 표시된 "edelvise06"은 preview일 뿐, 항목 선택 전에는 PW가 채워지지 않음
        // → ArrowDown(첫 항목 하이라이트) + Enter(선택 확정)로 드롭다운 키보드 선택
        // → Chrome이 ID + PW 모두 확정 입력
        if (IS_SPA_LOGIN) {
          await wait(800) // 드롭다운 렌더링 대기
          try {
            const target = { tabId }
            await chrome.debugger.attach(target, '1.3')
            // ArrowDown: 드롭다운 첫 항목 하이라이트 (이미 첫 항목이 preview로 ID 채웠음)
            await chrome.debugger.sendCommand(target, 'Input.dispatchKeyEvent', {
              type: 'rawKeyDown', windowsVirtualKeyCode: 40, nativeVirtualKeyCode: 40, key: 'ArrowDown', code: 'ArrowDown',
            })
            await wait(50)
            await chrome.debugger.sendCommand(target, 'Input.dispatchKeyEvent', {
              type: 'keyUp', windowsVirtualKeyCode: 40, nativeVirtualKeyCode: 40, key: 'ArrowDown', code: 'ArrowDown',
            })
            await wait(200)
            // Enter: 선택 확정 → Chrome 비번 매니저가 ID + PW 모두 채움
            await chrome.debugger.sendCommand(target, 'Input.dispatchKeyEvent', {
              type: 'rawKeyDown', windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13, key: 'Enter', code: 'Enter',
            })
            await wait(50)
            await chrome.debugger.sendCommand(target, 'Input.dispatchKeyEvent', {
              type: 'keyUp', windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13, key: 'Enter', code: 'Enter',
            })
            await chrome.debugger.detach(target)
            console.log(`[자동로그인] ${site.name} ArrowDown+Enter 발화 (autofill 드롭다운 첫 항목 선택)`)
            await wait(1500) // Chrome 비번 매니저가 ID+PW 채우는 시간
          } catch (kbErr) {
            try { await chrome.debugger.detach({ tabId }) } catch {}
            console.log(`[자동로그인] 드롭다운 선택 실패 (무시): ${kbErr.message}`)
          }
        }
      } else {
        console.log(`[자동로그인] ${site.name} 아이디 필드 미발견`)
      }
    } catch (e) {
      console.log(`[자동로그인] 아이디 필드 클릭 실패 (무시): ${e.message}`)
    }

    // STEP C: 비밀번호 필드도 triple-click으로 .value 확정
    try {
      const [pwPosResult] = await chrome.scripting.executeScript({
        target: { tabId },
        func: () => {
          const pwField = document.querySelector('input[type="password"]')
          if (pwField) {
            const r = pwField.getBoundingClientRect()
            return { x: r.left + r.width / 2, y: r.top + r.height / 2, found: true }
          }
          return { found: false }
        },
      })
      const pwPos = pwPosResult?.result
      if (pwPos?.found) {
        await _alTripleClick(tabId, pwPos.x, pwPos.y)
        console.log(`[자동로그인] ${site.name} 비밀번호 필드 triple-click 완료`)
      }
    } catch (e) {
      console.log(`[자동로그인] 비밀번호 필드 클릭 실패 (무시): ${e.message}`)
    }

    // 자동완성 값 반영 대기 (SPA는 더 길게)
    const autoFillWait = (siteKey === 'musinsa' || siteKey === 'kream' || siteKey === 'abcmart') ? 4000 : (IS_SPA_LOGIN ? 4000 : 2500)
    await wait(autoFillWait)

    // input/change 이벤트 강제 발화 — 사이트 유효성 검사가 자동완성 값을 인식하도록 유도
    try {
      await chrome.scripting.executeScript({
        target: { tabId },
        func: () => {
          document.querySelectorAll('input[type="text"], input[type="email"], input[type="password"]').forEach(inp => {
            if (inp.value && inp.offsetParent !== null) {
              inp.dispatchEvent(new Event('input', { bubbles: true }))
              inp.dispatchEvent(new Event('change', { bubbles: true }))
            }
          })
        },
      })
    } catch {}

    // 진단: 로그인 버튼 클릭 직전 input 상태 로그 (다음 디버깅 시 즉시 원인 판별용)
    try {
      const [diag] = await chrome.scripting.executeScript({
        target: { tabId },
        func: () => {
          const af = (el) => { try { return !!el?.matches?.(':-webkit-autofill') } catch { return false } }
          const id = document.querySelector('input[type="email"], input#id, input[name="id"], input[name="userId"], input[name="username"], input#username')
          const pw = document.querySelector('input[type="password"]')
          return {
            id_found: !!id, id_vlen: id?.value?.length || 0, id_af: af(id),
            pw_found: !!pw, pw_vlen: pw?.value?.length || 0, pw_af: af(pw),
          }
        },
      })
      const d = diag?.result || {}
      console.log(`[자동로그인][진단] ${site.name} 클릭 직전 — id(found=${d.id_found},vlen=${d.id_vlen},af=${d.id_af}) pw(found=${d.pw_found},vlen=${d.pw_vlen},af=${d.pw_af})`)
    } catch {}

    // STEP D: :-webkit-autofill 감지 → form POST 폴백 (.value 빈 경우 ABC마트 등)
    try {
      const [autofillResult] = await chrome.scripting.executeScript({
        target: { tabId },
        func: () => {
          const username = document.querySelector('input#username, input[name="username"], input[name="userId"], input#id, input[type="email"]')
          const password = document.querySelector('input[type="password"]')
          if (!username || !password) return { autofilled: false, reason: 'fields_not_found' }

          let isAutofilled = false
          try {
            isAutofilled = username.matches(':-webkit-autofill') && password.matches(':-webkit-autofill')
          } catch {}

          const valueEmpty = !username.value && !password.value
          if (!isAutofilled || !valueEmpty) {
            return { autofilled: false, isAutofilled, valueEmpty }
          }

          const form = username.closest('form')
          if (!form) return { autofilled: true, noForm: true }

          form.method = 'POST'
          form.onsubmit = null
          form.removeAttribute('onsubmit')

          const loginBtn = document.querySelector('#login') || form.querySelector('input[type="button"]') || form.querySelector('button')
          const submitBtn = document.createElement('input')
          submitBtn.type = 'submit'
          submitBtn.value = '로그인'
          submitBtn.id = '__sambaAutoLoginSubmit__'

          if (loginBtn) {
            const rect = loginBtn.getBoundingClientRect()
            submitBtn.style.cssText = `position: fixed; left: ${rect.left}px; top: ${rect.top}px; width: ${rect.width}px; height: ${rect.height}px; z-index: 99999; opacity: 0.01; cursor: pointer;`
            document.body.appendChild(submitBtn)
          } else {
            form.appendChild(submitBtn)
          }

          const r = submitBtn.getBoundingClientRect()
          return {
            autofilled: true,
            formPatched: true,
            submitPos: { x: r.left + r.width / 2, y: r.top + r.height / 2 },
          }
        },
      })

      const af = autofillResult?.result
      if (af?.formPatched && af?.submitPos) {
        console.log(`[자동로그인] ${site.name} :-webkit-autofill 감지 — form POST 폴백 시도`)
        const target = { tabId }
        try {
          await chrome.debugger.attach(target, '1.3')
          await chrome.debugger.sendCommand(target, 'Input.dispatchMouseEvent', {
            type: 'mousePressed', x: af.submitPos.x, y: af.submitPos.y, button: 'left', clickCount: 1,
          })
          await wait(50)
          await chrome.debugger.sendCommand(target, 'Input.dispatchMouseEvent', {
            type: 'mouseReleased', x: af.submitPos.x, y: af.submitPos.y, button: 'left', clickCount: 1,
          })
          await chrome.debugger.detach(target)
          await wait(5000)
          tabInfo = await chrome.tabs.get(tabId)
          currentUrl = tabInfo.url || ''
          if (currentUrl && !site.isLoginPage(currentUrl)) {
            console.log(`[자동로그인] ✅ ${site.name} form POST 폴백 성공`)
            try { await chrome.tabs.remove(tabId) } catch {}
            return true
          }
          console.log(`[자동로그인] form POST 폴백 후에도 로그인 페이지 — 일반 클릭 폴백`)
        } catch (dbgErr) {
          console.log(`[자동로그인] form POST debugger 오류: ${dbgErr.message}`)
          try { await chrome.debugger.detach({ tabId }) } catch {}
        }
      }
    } catch (e) {
      console.log(`[자동로그인] STEP D 오류 (무시): ${e.message}`)
    }

    // 4) 로그인 버튼 클릭 + 폴링으로 성공 여부 확인
    let buttonClicked = false
    const startTime = Date.now()
    while (Date.now() - startTime < LOGIN_TIMEOUT) {
      // 진행 중 오토튠 비활성 감지 시 즉시 중단 (사용자가 작업 취소한 경우)
      const stillActive = await _isAutotuneActive()
      if (stillActive === false) {
        console.log(`[자동로그인] ${site.name} 진행 중 오토튠 취소 감지 → 즉시 중단`)
        try { await chrome.tabs.remove(tabId) } catch {}
        return false
      }

      if (!buttonClicked) {
        try {
          const btnSelector = site.loginButtonSelector || 'button[type="submit"]'

          // 0) 로그인 버튼 disabled 강제 해제 (KREAM 등 빈 필드면 disabled되는 사이트 대응)
          await chrome.scripting.executeScript({
            target: { tabId },
            func: (selector) => {
              const selectors = selector.split(',').map(s => s.trim())
              for (const sel of selectors) {
                const btn = document.querySelector(sel)
                if (btn && btn.disabled) {
                  btn.disabled = false
                  btn.classList.remove('disabled')
                }
              }
              for (const b of document.querySelectorAll('button[disabled], input[disabled]')) {
                const txt = (b.textContent || b.value || '').trim()
                if (txt === '로그인' || txt === 'Login' || txt === '로그인하기') {
                  b.disabled = false
                  b.classList.remove('disabled')
                }
              }
            },
            args: [btnSelector],
          })

          // 1) 로그인 버튼 좌표 계산
          const [posResult] = await chrome.scripting.executeScript({
            target: { tabId },
            func: (selector) => {
              const selectors = selector.split(',').map(s => s.trim())
              for (const sel of selectors) {
                const btn = document.querySelector(sel)
                if (btn && btn.getBoundingClientRect().width > 0) {
                  const r = btn.getBoundingClientRect()
                  return { x: r.left + r.width / 2, y: r.top + r.height / 2 }
                }
              }
              for (const b of document.querySelectorAll('button, input[type="submit"], input[type="button"], a.btn, div[role="button"]')) {
                const txt = (b.textContent || b.value || '').trim()
                if (txt === '로그인' || txt === 'Login' || txt === 'Sign in' || txt === '로그인하기') {
                  const r = b.getBoundingClientRect()
                  return { x: r.left + r.width / 2, y: r.top + r.height / 2 }
                }
              }
              return null
            },
            args: [btnSelector],
          })

          const pos = posResult?.result
          if (pos) {
            // 2) chrome.debugger trusted click + alert 자동 닫기
            const target = { tabId }
            try {
              await chrome.debugger.attach(target, '1.3')
              await chrome.debugger.sendCommand(target, 'Page.enable', {})
              const dialogHandler = (src, method, params) => {
                if (src.tabId === tabId && method === 'Page.javascriptDialogOpening') {
                  console.log(`[자동로그인] alert 자동 닫기: "${(params?.message || '').substring(0, 50)}"`)
                  chrome.debugger.sendCommand(target, 'Page.handleJavaScriptDialog', { accept: true }).catch(() => {})
                }
              }
              chrome.debugger.onEvent.addListener(dialogHandler)

              await chrome.debugger.sendCommand(target, 'Input.dispatchMouseEvent', {
                type: 'mousePressed', x: pos.x, y: pos.y, button: 'left', clickCount: 1,
              })
              await wait(50)
              await chrome.debugger.sendCommand(target, 'Input.dispatchMouseEvent', {
                type: 'mouseReleased', x: pos.x, y: pos.y, button: 'left', clickCount: 1,
              })
              await wait(500)
              chrome.debugger.onEvent.removeListener(dialogHandler)
              await chrome.debugger.detach(target)
              console.log(`[자동로그인] ${site.name} 로그인 버튼 trusted click 완료`)
            } catch (dbgErr) {
              console.log(`[자동로그인] debugger 클릭 실패 (${dbgErr.message}) — 일반 click 폴백`)
              try { await chrome.debugger.detach(target) } catch {}
              await chrome.scripting.executeScript({
                target: { tabId },
                func: (x, y) => {
                  const el = document.elementFromPoint(x, y)
                  if (el) el.click()
                },
                args: [pos.x, pos.y],
              })
            }
            buttonClicked = true
            await wait(3000)
          } else {
            console.log(`[자동로그인] ${site.name} 로그인 버튼 미발견`)
            buttonClicked = true
          }
        } catch {
          buttonClicked = true
        }
      }

      await wait(POLL_INTERVAL)

      try {
        tabInfo = await chrome.tabs.get(tabId)
        currentUrl = tabInfo.url || ''
        if (!site.isLoginPage(currentUrl)) {
          console.log(`[자동로그인] ✅ ${site.name} 로그인 성공 — 탭 닫음`)
          try { await chrome.tabs.remove(tabId) } catch {}
          return true
        }
      } catch {
        return false
      }
    }

    console.log(`[자동로그인] ${site.name} 타임아웃 (${LOGIN_TIMEOUT / 1000}초)`)
    return false
  } catch (err) {
    console.error(`[자동로그인] ${site.name} 예외:`, err.message)
    return false
  } finally {
    if (tabCreated && tabId) {
      try { await chrome.tabs.remove(tabId) } catch {}
    }
  }
}

// chrome.debugger triple-click — 텍스트 전체선택 → Chrome이 자동완성 값을 .value에 확정
// (단순 click이나 Tab 키로는 .value가 빈 문자열로 유지되는 Chrome 보안 정책 우회)
async function _alTripleClick(tabId, x, y) {
  const target = { tabId }
  try {
    await chrome.debugger.attach(target, '1.3')
    for (let cc = 1; cc <= 3; cc++) {
      await chrome.debugger.sendCommand(target, 'Input.dispatchMouseEvent', {
        type: 'mousePressed', x, y, button: 'left', clickCount: cc,
      })
      await wait(30)
      await chrome.debugger.sendCommand(target, 'Input.dispatchMouseEvent', {
        type: 'mouseReleased', x, y, button: 'left', clickCount: cc,
      })
      await wait(50)
    }
    await wait(300)
    await chrome.debugger.detach(target)
  } catch (e) {
    try { await chrome.debugger.detach(target) } catch {}
    // 폴백: JS focus/click (자동완성 트리거가 약하지만 일부 사이트는 작동)
    try {
      await chrome.scripting.executeScript({
        target: { tabId },
        func: (cx, cy) => {
          const el = document.elementFromPoint(cx, cy)
          if (el) { el.focus(); el.click() }
        },
        args: [x, y],
      })
    } catch {}
    throw e
  }
}

// 외부 모듈에서 사용 가능하도록 globalThis에 노출
globalThis.ensureLoggedIn = ensureLoggedIn
globalThis.alExternalSiteToKey = alExternalSiteToKey
globalThis.AUTO_LOGIN_SITES = AUTO_LOGIN_SITES
