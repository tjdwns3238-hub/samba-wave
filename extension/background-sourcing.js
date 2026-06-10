// ==================== AI소싱 큐 폴링 ====================

// AI소싱 큐는 /api/v1/samba/ai-sourcing/ 경로 사용 (proxy 아님)
async function pollAiSourcingOnce() {
  try {
    const res = await apiFetch(`${PROXY_URL}/api/v1/samba/ai-sourcing/collect-queue`)
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    const job = await res.json()
    if (job.hasJob) {
      console.log(`[AI소싱] 작업 수신: ${job.type}`)
      await handleAiSourcingJob(job)
      return true
    }
    return false
  } catch (e) {
    console.log(`[AI소싱] 폴링 오류: ${e.message}`)
    return false
  }
}

// AI소싱 결과 전송도 별도 경로
async function postAiSourcingResult(body) {
  await apiFetch(`${PROXY_URL}/api/v1/samba/ai-sourcing/collect-result`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
}

async function handleAiSourcingJob(job) {
  const jobType = job.type // 'ranking' 또는 'keywords'
  let tabId = null

  try {
    if (jobType === 'ranking') {
      // 레시피 방식 우선 시도 — 레시피 없거나 실패 시 하드코딩 로직으로 fallback
      const rankingRecipe = await globalThis.SambaRecipeCache?.getRecipe('musinsa_ranking')
      if (rankingRecipe) {
        try {
          const date = job.date || '202503'
          const categoryCode = job.categoryCode || '000'
          console.log('[AI소싱] 랭킹 수집 (레시피 방식)')
          const data = await globalThis.SambaRecipeExecutor.executeRecipe(
            rankingRecipe,
            { date, categoryCode },
            null,
          )
          await postAiSourcingResult({ requestId: job.requestId, type: 'ranking', data })
          return
        } catch (recipeErr) {
          console.warn('[레시피] musinsa_ranking 실행 실패, fallback:', recipeErr.message)
          // 외부 catch로 전파하지 않고 아래 하드코딩 로직으로 이어서 실행
        }
      }

      // 랭킹 아카이브 수집 — API 가로채기 방식 (fallback)
      const date = job.date || '202503'
      const categoryCode = job.categoryCode || '000'
      const url = `https://www.musinsa.com/ranking/archive?date=${date}&categoryCode=${categoryCode}&gf=A`
      console.log(`[AI소싱] 랭킹 수집: ${url}`)

      const [prevActive] = await chrome.tabs.query({ active: true, currentWindow: true })
      const prevId = prevActive?.id

      const tab = await chrome.tabs.create({ url, active: true })
      tabId = tab.id
      await waitForTabLoad(tabId, 30000)
      await wait(3000)

      // 페이지 내에서 fetch를 가로채서 API 응답 수집 + DOM 텍스트 파싱 병행
      const results = await chrome.scripting.executeScript({
        target: { tabId },
        world: 'MAIN',
        func: () => {
          // DOM 텍스트 기반 파싱 — 렌더링된 상품 카드에서 추출
          const items = []
          const bodyText = document.body.innerText
          const lines = bodyText.split('\n').map(l => l.trim()).filter(Boolean)

          // 상품 링크
          const goodsNos = []
          document.querySelectorAll('a[href*="/products/"]').forEach(link => {
            const m = link.href?.match(/\/products\/(\d+)/)
            if (m && !goodsNos.includes(m[1])) goodsNos.push(m[1])
          })

          // 순위+브랜드+상품명+가격 텍스트 파싱
          let rank = 0
          let brand = ''
          for (let i = 0; i < lines.length; i++) {
            const line = lines[i]
            // 순위 (1~200)
            if (/^\d{1,3}$/.test(line)) {
              const n = parseInt(line)
              if (n >= 1 && n <= 200) { rank = n; brand = ''; continue }
            }
            if (rank > 0 && !brand) {
              // 가격/할인율이 아닌 짧은 텍스트 = 브랜드
              if (line.length < 30 && !/[\d,]+원/.test(line) && !/^\d+%$/.test(line) && !/^[\d,]+$/.test(line)) {
                brand = line; continue
              }
            }
            if (rank > 0 && brand) {
              // 브랜드 다음 긴 텍스트 = 상품명
              if (line.length >= 3 && !/[\d,]+원/.test(line) && !/^\d+%$/.test(line)) {
                let price = 0
                for (let j = i + 1; j < Math.min(i + 5, lines.length); j++) {
                  const pm = lines[j].replace(/\s/g, '').match(/([\d,]+)원/)
                  if (pm) { price = parseInt(pm[1].replace(/,/g, '')); break }
                }
                items.push({ rank, brand, name: line, price, goodsNo: goodsNos[items.length] || '' })
                rank = 0; brand = ''
              }
            }
          }

          return {
            items,
            debug: {
              title: document.title,
              productLinks: goodsNos.length,
              totalItems: items.length,
              bodyLen: bodyText.length,
              bodyPreview: bodyText.substring(0, 1200),
            },
          }
        },
      })

      try { await chrome.tabs.remove(tabId) } catch {}
      tabId = null
      // 이전 탭 복원
      if (prevId) {
        try { await chrome.tabs.update(prevId, { active: true }) } catch {}
      }

      const data = results?.[0]?.result || {}
      console.log(`[AI소싱] 랭킹: ${data.items?.length || 0}개 상품`)

      await postAiSourcingResult({
        requestId: job.requestId,
        type: 'ranking',
        data,
      })

    } else if (jobType === 'keywords') {
      // 레시피 방식 우선 시도 — 레시피 없거나 실패 시 하드코딩 로직으로 fallback
      // 주의: 레시피 executor는 active:false 탭으로 생성하므로, 검색 팝업 트리거가 안 될 수 있음
      //       musinsa_keywords 레시피 작성 시 active:true 탭이 필요한 점을 감안해야 함
      const keywordsRecipe = await globalThis.SambaRecipeCache?.getRecipe('musinsa_keywords')
      if (keywordsRecipe) {
        try {
          console.log('[AI소싱] 검색 키워드 수집 (레시피 방식)')
          const data = await globalThis.SambaRecipeExecutor.executeRecipe(
            keywordsRecipe,
            {},
            null,
          )
          await postAiSourcingResult({ requestId: job.requestId, type: 'keywords', data })
          return
        } catch (recipeErr) {
          console.warn('[레시피] musinsa_keywords 실행 실패, fallback:', recipeErr.message)
          // 외부 catch로 전파하지 않고 아래 하드코딩 로직으로 이어서 실행
        }
      }

      // 인기/급상승 검색어 수집 — 검색 페이지를 active 탭으로 열어서 키워드 표시 (fallback)
      console.log('[AI소싱] 검색 키워드 수집 시작')
      // 현재 활성 탭 기억 (복원용)
      const [activeTab] = await chrome.tabs.query({ active: true, currentWindow: true })
      const prevTabId = activeTab?.id

      // 검색 페이지를 active 탭으로 열기 (포커스 필요)
      const tab = await chrome.tabs.create({ url: 'https://www.musinsa.com/search', active: true })
      tabId = tab.id
      await waitForTabLoad(tabId, 30000)
      await wait(2000)

      // 검색 입력 클릭 (인기검색어 표시 트리거)
      await chrome.scripting.executeScript({
        target: { tabId }, world: 'MAIN',
        func: () => {
          // 다양한 셀렉터 시도 (무신사 UI 변경 대응)
          const selectors = [
            'input[type="search"]',
            'input[placeholder*="검색"]',
            'input[name*="search"]',
            'input[aria-label*="검색"]',
            '.search-bar input',
            '#search-input',
            'header input',
          ]
          for (const sel of selectors) {
            const el = document.querySelector(sel)
            if (el) { el.focus(); el.click(); break }
          }
          // 검색 버튼/아이콘 클릭도 시도
          const searchBtn = document.querySelector('[class*="search"] button, button[aria-label*="검색"]')
          if (searchBtn) searchBtn.click()
        },
      })
      await wait(4000)

      const results = await chrome.scripting.executeScript({
        target: { tabId }, world: 'MAIN',
        func: () => {
          const popular = []
          const trending = []
          const bodyText = document.body.innerText

          // 방법1: "인기 검색어" / "급상승 검색어" 섹션 텍스트 파싱
          const popularMatch = bodyText.match(/인기\s*검색어([\s\S]*?)(?:급상승\s*검색어|$)/)
          const trendingMatch = bodyText.match(/급상승\s*검색어([\s\S]*?)(?:어바웃|회사|무신사 스토어|$)/)

          function extractKw(text) {
            const kws = []
            const lines = text.split('\n').map(l => l.trim()).filter(Boolean)
            for (const line of lines) {
              const m = line.match(/^(\d{1,2})\s+(.+)$/)
              if (m && m[2].length < 30) {
                kws.push({ rank: parseInt(m[1]), keyword: m[2].trim() })
              }
            }
            return kws
          }

          if (popularMatch) popular.push(...extractKw(popularMatch[1]))
          if (trendingMatch) trending.push(...extractKw(trendingMatch[1]))

          // 방법2: DOM 기반 파싱 (텍스트 매칭 실패 시 fallback)
          if (popular.length === 0 && trending.length === 0) {
            // li 요소에서 순위+키워드 추출 시도
            const listItems = document.querySelectorAll('li, [class*="keyword"], [class*="search-rank"], [class*="popular"]')
            let rank = 1
            listItems.forEach(li => {
              const text = li.textContent?.trim() || ''
              // "1나이키운동화" 또는 "1 나이키 운동화" 패턴
              const m = text.match(/^(\d{1,2})\s*(.{2,25})$/)
              if (m) {
                popular.push({ rank: parseInt(m[1]), keyword: m[2].trim() })
              } else if (text.length >= 2 && text.length <= 25 && !/^(MUSINSA|BEAUTY|SPORTS|OUTLET|BOUTIQUE|KICKS|KIDS|USED|SNAP)$/i.test(text)) {
                // 순위 없이 키워드만 있는 경우
                const exists = popular.some(p => p.keyword === text) || trending.some(t => t.keyword === text)
                if (!exists && rank <= 20) {
                  popular.push({ rank: rank++, keyword: text })
                }
              }
            })
          }

          return {
            keywordItems: [
              ...popular.map(k => ({ ...k, type: 'popular' })),
              ...trending.map(k => ({ ...k, type: 'trending' })),
            ],
            debug: {
              bodyLen: bodyText.length,
              bodyPreview: bodyText.substring(0, 1500),
              hasPopular: !!popularMatch,
              hasTrending: !!trendingMatch,
              domFallback: popular.length > 0 && !popularMatch,
            },
          }
        },
      })

      // 이전 탭으로 복원
      if (prevTabId) {
        try { await chrome.tabs.update(prevTabId, { active: true }) } catch {}
      }

      try { await chrome.tabs.remove(tabId) } catch {}
      tabId = null
      const data = results?.[0]?.result || {}
      console.log(`[AI소싱] 키워드: ${data.keywordItems?.length || 0}개`)

      await postAiSourcingResult({
        requestId: job.requestId,
        type: 'keywords',
        data,
      })
    }
  } catch (err) {
    console.error(`[AI소싱] ${jobType} 오류:`, err)
    if (tabId) try { await chrome.tabs.remove(tabId) } catch {}
    try {
      await postAiSourcingResult({
        requestId: job.requestId,
        type: jobType,
        error: err.message,
      })
    } catch {}
  }
}

// ==================== 통합 소싱 큐 폴링 (ABCmart, GrandStage, REXMONDE, 롯데ON, GSShop) ====================

// 안전 상한 — 실제 동시 실행 수는 백엔드 _SSG_BATCH로 제어
const SOURCING_MAX_POLL_LIMIT = 10
// 오토튠 작업취소 시 in-flight 탭 즉시 종료용 플래그
let _sourcingForceStop = false
// 이 PC가 현재 오토튠에 참여 중인지 여부 — 시작 버튼 클릭 시 true, 중지/forceStop 시 false.
// false이면 collect-queue 폴링 자체를 건너뜀 → 다른 PC의 시작에 자동으로 편승하지 않음.
let _localAutotuneJoined = false
// 이 PC가 처리할 소싱처 목록 — null이면 전체, 배열이면 선택된 소싱처만
let _allowedSourceSites = null
// 사이트별 로그인 확인 완료 플래그 — logout_link 감지 시 set, 오토튠 탈퇴 시 clear
// 한 번 확인된 사이트는 ambiguous여도 login_required 차단 안 함
const _siteLoginConfirmed = new Set()
let _abcLoginCheckTimer = null  // ABCmart 1시간 주기 재체크 타이머
let _lotteonLoginCheckTimer = null  // LOTTEON 1시간 주기 재체크 타이머
let _abcLoginConfirmedAt = 0  // ABCmart 마지막 로그인 확인 시각 (ms) — 재합류 시 재체크 스킵 판단용

// 무신사 자동로그인 실패(쿠키 손실) — 손실 감지 시각(ms, 0=정상). 값이 있으면
// 폴링이 받은 무신사 잡을 drop해 진행중 잡을 중단한다.
// 단, 재확인 인터벌(5분) 경과 시 프로브 잡을 1회 통과시켜 쿠키 복구를 자동 감지한다
// (drop이 _processJobWithCap 상류라 통과시키지 않으면 reportLoginSuccess가 영영 안 떠
//  플래그가 영구 고착됨 → 백엔드 _musinsa_auth_lost_recent와 동일한 프로브 패턴).
let _musinsaCookieLostAt = 0
let _musinsaCookieLostNotifiedAt = 0  // 데스크탑 경고 중복 차단 (ms)
const _MUSINSA_NOTIFY_COOLDOWN_MS = 60 * 60 * 1000  // 1시간
const _MUSINSA_LOST_RECHECK_MS = 5 * 60 * 1000  // 5분마다 프로브 잡 1회 허용

// 무신사 잡을 drop해야 하는지 — 손실 후 재확인 인터벌 내면 true(drop), 경과면 false(프로브 통과).
function _musinsaCookieLostActive() {
  if (!_musinsaCookieLostAt) return false
  return (Date.now() - _musinsaCookieLostAt) < _MUSINSA_LOST_RECHECK_MS
}

// 무신사 쿠키 손실 처리 — 데스크탑 경고 + 백엔드 만료 마킹 + 잡 중단 플래그.
function _handleMusinsaCookieLost() {
  _musinsaCookieLostAt = Date.now()
  // 데스크탑 알림 (1시간 쿨다운)
  const now = Date.now()
  if (now - _musinsaCookieLostNotifiedAt >= _MUSINSA_NOTIFY_COOLDOWN_MS) {
    _musinsaCookieLostNotifiedAt = now
    try {
      chrome.notifications.create('musinsa-cookie-lost', {
        type: 'basic',
        iconUrl: 'icon128.png',
        title: '무신사 로그인 만료',
        message: '무신사 쿠키가 손실되어 수집/오토튠을 중단했습니다. 무신사에 다시 로그인해 주세요.',
        priority: 2,
      })
    } catch (e) {
      console.warn('[무신사쿠키손실] 알림 실패(무시):', e?.message || e)
    }
  }
  // 백엔드 만료 마킹 → 오토튠(백엔드 refresher) 무신사 갱신도 중단 + 프론트 경고
  try {
    apiFetch(`${PROXY_URL}/api/v1/samba/sourcing-accounts/musinsa/mark-cookie-expired`, {
      method: 'POST',
    }).catch(() => {})
  } catch (_) {}
  console.warn('[무신사쿠키손실] 자동로그인 실패 — 무신사 잡 중단 + 경고')
}

// 무신사 쿠키 복구 — 로그인 성공/확인 시 호출, 잡 재개.
function _clearMusinsaCookieLost() {
  if (_musinsaCookieLostAt) {
    _musinsaCookieLostAt = 0
    console.log('[무신사쿠키손실] 로그인 복구 감지 — 무신사 잡 재개')
  }
}

// 사이트별 동시 처리 세마포어 — 폴링이 받은 작업을 사이트별 캡까지만 병렬 처리
// (프론트 "동시실행" 설정값을 백엔드 status API에서 받아 적용)
const _siteSemaphores = new Map() // site → { active: number }
let _siteConcurrencyCache = { value: null, at: 0 }
const _SITE_CONC_CACHE_MS = 5000

function _hasRecentLoginProof(site) {
  if (_siteLoginConfirmed.has(site)) return true
  const siteKey = (typeof alExternalSiteToKey === 'function') ? alExternalSiteToKey(site) : null
  const lastAt = (siteKey && globalThis._lastAutoLoginSuccessAt) ? globalThis._lastAutoLoginSuccessAt[siteKey] : 0
  const AL_GRACE_MS = 60 * 60 * 1000
  return !!(lastAt && Date.now() - lastAt < AL_GRACE_MS)
}
const _activeSourcingSites = new Map()


function _getActiveTrackingSites(site) {
  if (site === 'ABCmart' || site === 'GrandStage') return ['ABCmart', 'GrandStage']
  return [site]
}

function _markSourcingSiteActive(site) {
  for (const key of _getActiveTrackingSites(site)) {
    _activeSourcingSites.set(key, (_activeSourcingSites.get(key) || 0) + 1)
  }
}

function _markSourcingSiteInactive(site) {
  for (const key of _getActiveTrackingSites(site)) {
    const next = Math.max(0, (_activeSourcingSites.get(key) || 0) - 1)
    if (next === 0) _activeSourcingSites.delete(key)
    else _activeSourcingSites.set(key, next)
  }
}

function isSiteActiveForSourcing(site) {
  if (!site) return false
  return _getActiveTrackingSites(site).some(key => (_activeSourcingSites.get(key) || 0) > 0)
}

globalThis.isSiteActiveForSourcing = isSiteActiveForSourcing

async function _getSiteConcurrencyMap() {
  const now = Date.now()
  if (_siteConcurrencyCache.value && now - _siteConcurrencyCache.at < _SITE_CONC_CACHE_MS) {
    return _siteConcurrencyCache.value
  }
  try {
    const stored = await chrome.storage.local.get('proxyUrl')
    const proxyUrl = stored.proxyUrl || ''
    // collector/autotune/status는 JWT 필수 (확장앱은 401), proxy/autotune/concurrency는
    // X-Api-Key 인증만으로 동시처리 캡만 조회 가능 (2026-05-05 백엔드 추가).
    const _apiFetch = globalThis.SambaBackgroundCore?.apiFetch
    const res = _apiFetch
      ? await _apiFetch(`${proxyUrl}/api/v1/samba/proxy/autotune/concurrency`, { method: 'GET' })
      : await fetch(`${proxyUrl}/api/v1/samba/proxy/autotune/concurrency`, { method: 'GET' })
    if (!res.ok) return _siteConcurrencyCache.value || {}
    const data = await res.json()
    const conc = { ...(data.site_autotune_concurrency || {}) }
    _siteConcurrencyCache = { value: conc, at: now }
    return conc
  } catch {
    return _siteConcurrencyCache.value || {}
  }
}

function _normalizeSiteForCap(site) {
  // GrandStage는 a-rt.com 동일 인프라 → ABCmart 캡 공유
  if (site === 'GrandStage') return 'ABCmart'
  return site
}

async function _siteSemAcquire(site) {
  const key = _normalizeSiteForCap(site)
  const concMap = await _getSiteConcurrencyMap()
  // popup 윈도우 처리 사이트(SSG/ABCmart/LOTTEON)는 큐 적체 방지 위해 최소 4개 강제.
  // 백엔드 설정값이 더 크면 그대로 사용 (사용자가 명시적으로 늘린 경우).
  // 검증(2026-05-05): 동시 1개 시 큐 대기 100s+ → 90s timeout 다수 발생.
  const _POPUP_SITES_MIN_CAP = 4
  const _isPopupSite = key === 'SSG' || key === 'ABCmart' || key === 'LOTTEON'
  const _serverCap = concMap[key] || 99
  const cap = _isPopupSite ? Math.max(_serverCap, _POPUP_SITES_MIN_CAP) : _serverCap
  let sem = _siteSemaphores.get(key)
  if (!sem) {
    sem = { active: 0 }
    _siteSemaphores.set(key, sem)
  }
  // 캡 도달 시 대기 (200ms 폴링)
  let waited = 0
  while (sem.active >= cap) {
    if (waited === 0) console.log(`[동시실행] ${key} 캡 도달(${sem.active}/${cap}) — 슬롯 대기`)
    await wait(200)
    waited += 200
    if (waited > 60000) {
      console.log(`[동시실행] ${key} 60초 대기 후에도 캡 도달 — 강제 진행`)
      break
    }
  }
  sem.active++
}

function _siteSemRelease(site) {
  const key = _normalizeSiteForCap(site)
  const sem = _siteSemaphores.get(key)
  if (sem) sem.active = Math.max(0, sem.active - 1)
}

// 무신사 송장 잡 직렬화 — Next.js SPA + 백그라운드 탭에서 React hydration 지연으로
// 버튼 click이 무시되는 회귀 차단. Promise chain 으로 race condition 없이 1건씩 처리.
// (이전 boolean 락은 Promise.all 동시 진입 시 while 검사 모두 false 보고 통과 → 직렬화 실패)
let _musinsaTrackingChain = Promise.resolve()
function _serializeMusinsaTracking(fn) {
  const next = _musinsaTrackingChain.then(() => fn(), () => fn())
  _musinsaTrackingChain = next.catch(() => {})
  return next
}

async function _processJobWithCap(job) {
  const site = job.site || 'unknown'
  // 적립금 자동 적립 잡(type=reward) — 가격수집과 격리, 사이트 세마포어 사용
  if (job.type === 'reward') {
    await _siteSemAcquire(site)
    _markSourcingSiteActive(site)
    try {
      return await handleRewardJob(job)
    } finally {
      _markSourcingSiteInactive(site)
      _siteSemRelease(site)
    }
  }
  // 발주취소 잡(type=cancel_order) — 가격수집과 격리, 사이트 세마포어 사용.
  // 사이트별 cancel_js 분석·작성 전이라 현재는 '미지원' 회신만.
  // 실제 사이트 DOM 자동화는 content-cancel-{site}.js + 본 핸들러에서 라우팅 (분석 후 채움).
  if (job.type === 'cancel_order') {
    await _siteSemAcquire(site)
    _markSourcingSiteActive(site)
    try {
      return await handleCancelOrderJob(job)
    } finally {
      _markSourcingSiteInactive(site)
      _siteSemRelease(site)
    }
  }
  // 송장 추출 잡(type=tracking) — 가격수집과 격리. 동일 사이트 캡 공유로 무신사 폭주 방지
  if (job.type === 'tracking') {
    if (site === 'MUSINSA') {
      return _serializeMusinsaTracking(async () => {
        _markSourcingSiteActive(site)
        try {
          return await handleTrackingJob(job)
        } finally {
          _markSourcingSiteInactive(site)
        }
      })
    }
    await _siteSemAcquire(site)
    _markSourcingSiteActive(site)
    try {
      return await handleTrackingJob(job)
    } finally {
      _markSourcingSiteInactive(site)
      _siteSemRelease(site)
    }
  }
  // ABCmart/GrandStage: _abcPreLoginPromise 차단 제거 — per-job 로그인 검증(line ~1499)이 이미 처리
  // 로그인 체크(~30초) + 잡처리(~45초) = 75초 타임아웃 경계를 넘는 원인이었음
  if (site === 'LOTTEON' && _lotteonPreLoginPromise) {
    try { await _lotteonPreLoginPromise } catch {}
    _lotteonPreLoginPromise = null
  }
  await _siteSemAcquire(site)
  _markSourcingSiteActive(site)
  try {
    return await handleSourcingJob(job)
  } finally {
    _markSourcingSiteInactive(site)
    _siteSemRelease(site)
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// 송장 추출 잡 핸들러 (소싱처 배송조회 페이지 → DOM 파싱 → tracking-result 전송)
// 사이트별 content-tracking-*.js 가 페이지에서 직접 chrome.runtime.sendMessage 로
// 결과를 보내고, 여기서 그 메시지를 받아 백엔드로 POST.
// ─────────────────────────────────────────────────────────────────────────────
const _trackingPending = new Map() // requestId → {resolve, timeoutId, tabId}

// 송장 잡 site(대문자) → 자동로그인 siteKey 매핑.
// SPA_DIRECT_LOGIN_SITES(ssg/lotteon/abcmart/musinsa)는 주문매칭 계정으로 강제 로그인 지원.
const _TRACKING_AUTO_LOGIN_MAP = {
  SSG: 'ssg',
  LOTTEON: 'lotteon',
  ABCMART: 'abcmart',
  GRANDSTAGE: 'abcmart',
  MUSINSA: 'musinsa',
}

// ─────────────────────────────────────────────────────────────────────────────
// 현재 로그인된 소싱처 계정 감지 — 송장수집 안전망: 매칭 잡 우선 처리용.
// 사이트별 마이페이지 진입 → username/nickname 스크랩 → 백엔드 find-by-username
// → account_id 캐싱(chrome.storage.local, TTL 30분).
// 매칭 잡은 빠른 경로(현재 로그인 그대로), 미매칭 잡은 느린 경로(2단계 재로그인 + 재시도).
// ─────────────────────────────────────────────────────────────────────────────
const _CURRENT_ACCOUNT_CACHE_TTL_MS = 30 * 60 * 1000  // 30분

// 사이트별 마이페이지 URL + username 스크랩 함수
const _CURRENT_ACCOUNT_DETECTORS = {
  MUSINSA: {
    // [2026-06-06] /mypage 는 닉네임("임성희1")만 노출 → 백엔드 find-by-username 은
    // username(edelvise06)/account_label(성희) 로만 매칭하므로 닉네임으론 404 → 감지 영영 실패.
    // /mypage/myinfo 는 "ID: edelvise06"(로그인 아이디) 를 정확히 노출 (CDP 실측 확인).
    // 여기서 아이디를 추출해야 백엔드 username 매칭이 성립한다.
    mypageUrl: 'https://www.musinsa.com/mypage/myinfo',
    scrape: () => {
      // 1순위: "ID: edelvise06" 패턴 → 로그인 아이디(username) 추출 (백엔드 username 매칭용).
      const body = document.body?.innerText || ''
      const idMatch = body.match(/ID:\s*([A-Za-z0-9_]{3,30})/)
      if (idMatch) return idMatch[1]
      // 폴백: "OOO님" 패턴 (계정명/account_label 매칭용)
      const greet = body.match(/([가-힣A-Za-z0-9_]{2,20})\s*님/)
      if (greet) return greet[1]
      return ''
    },
  },
}

async function _detectCurrentSourcingAccount(site) {
  const detector = _CURRENT_ACCOUNT_DETECTORS[site]
  if (!detector) return null
  const cacheKey = `_currentSourcingAccountId_${site}`
  // 캐시 조회
  try {
    const stored = await chrome.storage.local.get(cacheKey)
    const entry = stored[cacheKey]
    if (entry && entry.accountId && entry.at && Date.now() - entry.at < _CURRENT_ACCOUNT_CACHE_TTL_MS) {
      return entry.accountId
    }
  } catch {}

  // 마이페이지 진입 → 스크랩
  let tabId = null
  try {
    const tab = await chrome.tabs.create({ url: detector.mypageUrl, active: false })
    tabId = tab.id
    try { await waitForTabLoad(tabId, 20000) } catch {}
    await wait(2500)
    // 로그인 페이지 리다이렉트면 비로그인 — null 캐시
    const tabInfo = await chrome.tabs.get(tabId)
    const curUrl = tabInfo.url || ''
    if (curUrl.includes('/auth/login') || curUrl.includes('member.one.musinsa.com/login') || curUrl.includes('/login.ssg') || curUrl.includes('/member/login')) {
      console.log(`[송장][계정감지] ${site} 비로그인 상태 — 캐시 스킵`)
      return null
    }
    const [r] = await chrome.scripting.executeScript({ target: { tabId }, func: detector.scrape })
    const username = (r?.result || '').trim()
    if (!username) {
      console.log(`[송장][계정감지] ${site} username 스크랩 실패`)
      return null
    }
    // 백엔드 find-by-username 호출
    const stored = await chrome.storage.local.get('proxyUrl')
    const proxyUrl = stored.proxyUrl || ''
    const apiFetch = globalThis.SambaBackgroundCore?.apiFetch
    if (!apiFetch) return null
    const res = await apiFetch(
      `${proxyUrl}/api/v1/samba/sourcing-accounts/find-by-username?site_name=${encodeURIComponent(site)}&username=${encodeURIComponent(username)}`,
      { method: 'GET' }
    )
    if (!res.ok) {
      console.log(`[송장][계정감지] ${site} username="${username}" 백엔드 매칭 실패 (${res.status})`)
      return null
    }
    const data = await res.json()
    const accountId = data?.id || ''
    if (accountId) {
      try { await chrome.storage.local.set({ [cacheKey]: { accountId, username, at: Date.now() } }) } catch {}
      console.log(`[송장][계정감지] ${site} 현재 로그인 계정 식별: ${data.account_label} (id=${accountId})`)
    }
    return accountId || null
  } catch (e) {
    console.warn(`[송장][계정감지] ${site} 예외: ${e?.message || e}`)
    return null
  } finally {
    if (tabId) {
      try { await chrome.tabs.remove(tabId) } catch {}
    }
  }
}

// 계정 캐시 무효화 — 자동로그인 성공 직후 호출하면 다음 잡에서 재감지
async function _invalidateCurrentAccountCache(site) {
  const cacheKey = `_currentSourcingAccountId_${site}`
  try { await chrome.storage.local.remove(cacheKey) } catch {}
}

// 송장수집용 — 마지막으로 ensureLoggedIn 성공한 계정 메모리 캐시 (autoLoginKey → accountId).
// _detectCurrentSourcingAccount 가 username 스크랩에 실패해도(currentAccountId=null)
// 이 캐시로 잡 계정과 비교해 선제 스왑 여부 판단 → 같은 계정 잡 연속이면 스왑 스킵.
const _lastEnsuredTrackingAccount = {}

// ─────────────────────────────────────────────────────────────────────────────
// 적립금 자동 적립 잡 핸들러
// action 별로 적절한 URL 탭 열고 content script 주입 → 결과 메시지 대기 → 백엔드 콜백.
// ─────────────────────────────────────────────────────────────────────────────
const _rewardPending = new Map() // requestId → {resolve, timeoutId, tabId}

const _REWARD_ACTION_CONTENT = {
  musinsa_attendance: 'content-reward-musinsa-attendance.js',
  musinsa_snap_like: 'content-reward-musinsa-snap.js',
  musinsa_balance: 'content-musinsa-balance.js', // 기존 잔액 수집 스크립트 재사용
  abcmart_attendance: 'content-reward-abcmart-attend.js',
  musinsa_review: 'content-review-musinsa.js',
  abcmart_review: 'content-review-abcmart.js',
  ssg_review: 'content-review-ssg.js',
  gs_review: 'content-review-gs.js',
  lotteon_review: 'content-review-lotteon.js',
  naver_review: 'content-review-naver.js',
  kream_review: 'content-review-kream.js',
}

const _REWARD_ACTION_AUTO_LOGIN = {
  musinsa_attendance: 'musinsa',
  musinsa_snap_like: 'musinsa',
  musinsa_balance: 'musinsa',
  musinsa_review: 'musinsa',
  abcmart_attendance: 'abcmart',
  abcmart_review: 'abcmart',
  ssg_review: 'ssg',
  gs_review: 'gs',
  lotteon_review: 'lotteon',
  // naver는 자동로그인 미지원 (수동 로그인 필요)
  kream_review: 'kream',
}

// 리뷰 액션 사이트별 메타 (mode: 'navigate' = path별 새 탭 / 'inplace' = 같은 탭 반복)
const _REVIEW_META = {
  musinsa_review: {
    mode: 'navigate',
    listUrl: 'https://www.musinsa.com/mypage/myreview',
    buildWriteUrl: path => `https://www.musinsa.com${path}?channelSource=musinsa`,
    site: 'MUSINSA',
  },
  abcmart_review: {
    mode: 'inplace',
    listUrl: 'https://abcmart.a-rt.com/mypage/claim/claim-order-main?orderPrdtStatCodeClick=10007',
    site: 'ABCmart',
  },
  ssg_review: {
    mode: 'navigate',
    listUrl: 'https://www.ssg.com/myssg/activityMng/pdtEvalList.ssg?quick=pdtEvalList',
    buildWriteUrl: path => path.startsWith('http') ? path : `https://www.ssg.com${path}`,
    site: 'SSG',
  },
  gs_review: {
    mode: 'navigate',
    listUrl: 'https://www.gsshop.com/ord/dlvcursta/ordList.gs',
    buildWriteUrl: path => path.startsWith('http') ? path : `https://www.gsshop.com${path}`,
    site: 'GSShop',
  },
  lotteon_review: {
    // 인페이지 모달 — 같은 탭에서 클릭→모달→제출→닫힘 반복
    mode: 'inplace',
    listUrl: 'https://www.lotteon.com/p/review/myLotte/reviewWriteListTab',
    site: 'LOTTEON',
  },
  naver_review: {
    // 인페이지 + window.open 팝업 — 같은 탭에서 반복 처리(팝업은 content script가 자체 처리)
    mode: 'inplace',
    listUrl: 'https://shopping.naver.com/my/writable-reviews',
    site: 'NAVERSTORE',
  },
  kream_review: {
    mode: 'inplace',
    listUrl: 'https://kream.co.kr/my/reviews?tab=to_write',
    site: 'KREAM',
  },
}

// 리뷰 잡 처리 (사이트 공통 orchestrator)
async function handleReviewJob(job) {
  const { requestId, action, sourcingAccountId, site } = job
  const meta = _REVIEW_META[action]
  if (!meta) {
    await postResult('sourcing-accounts/extension/reward-result', {
      request_id: requestId, account_id: sourcingAccountId || '', site_name: site, action,
      success: false, error: `unknown review action: ${action}`,
    })
    return
  }
  const contentFile = _REWARD_ACTION_CONTENT[action]
  const DAILY_LIMIT = 30 // 안전 한도 — 무신사 미차단 경험 기반
  let writeCount = 0
  let lastError = ''
  const processed = new Set()

  let listTabId = null
  try {
    // 목록 탭 열기
    const listTab = await chrome.tabs.create({ url: meta.listUrl, active: false })
    listTabId = listTab.id
    try { await waitForTabLoad(listTabId, 30000) } catch {}
    await new Promise(r => setTimeout(r, 4000)) // 가상스크롤 초기 렌더 대기

    // ─── inplace 모드 (Lotteon/Naver): 같은 탭에서 processOne 반복 ───
    if (meta.mode === 'inplace') {
      let noNewCount = 0
      while (writeCount < DAILY_LIMIT) {
        try {
          await chrome.scripting.executeScript({ target: { tabId: listTabId }, files: [contentFile] })
        } catch (e) {
          lastError = `inplace 스크립트 주입 실패: ${e?.message || e}`
          break
        }
        const result = await chrome.tabs.sendMessage(listTabId, { action: 'samba_review_processOne' }).catch(e => ({ success: false, error: e?.message || String(e) }))
        if (result?.noItems || result?.allReviewed) {
          // 더보기 시도
          const more = await chrome.tabs.sendMessage(listTabId, { action: 'samba_review_loadMore' }).catch(() => ({ ok: false }))
          if (!more?.ok) break
          await new Promise(r => setTimeout(r, 2500))
          noNewCount++
          if (noNewCount >= 5) break
          continue
        }
        if (result?.success) {
          writeCount++
          noNewCount = 0
          console.log(`[적립금-리뷰] ${action} ${writeCount}건 완료 (inplace)`)
        } else {
          lastError = result?.error || 'unknown'
          console.log(`[적립금-리뷰] ${action} inplace 실패: ${lastError}`)
          // 실패 시 다음 항목으로 시도 (1회 재시도 후 종료)
          noNewCount++
          if (noNewCount >= 3) break
        }
        await new Promise(r => setTimeout(r, 3000 + Math.random() * 4000))
      }
      await postResult('sourcing-accounts/extension/reward-result', {
        request_id: requestId,
        account_id: sourcingAccountId || '',
        site_name: site,
        action,
        success: writeCount > 0,
        stamp_count: writeCount,
        error: writeCount === 0 ? lastError : '',
      })
      return
    }

    // ─── navigate 모드 (Musinsa/SSG/GS/ABCmart): write URL 새 탭 ───
    let noNewCount = 0
    while (writeCount < DAILY_LIMIT) {
      // content script 주입 (idempotent)
      try {
        await chrome.scripting.executeScript({ target: { tabId: listTabId }, files: [contentFile] })
      } catch (e) {
        lastError = `목록 스크립트 주입 실패: ${e?.message || e}`
        break
      }

      // 페이지 정보 조회
      const pageInfo = await chrome.tabs.sendMessage(listTabId, { action: 'samba_review_getPageInfo' }).catch(() => null)
      const allPaths = (pageInfo?.generalPaths || pageInfo?.paths || []).filter(p => !processed.has(p))

      if (allPaths.length === 0) {
        // 스크롤 더 시도
        const sr = await chrome.tabs.sendMessage(listTabId, { action: 'samba_review_scrollAndCollect' }).catch(() => null)
        const more = (sr?.generalPaths || sr?.paths || []).filter(p => !processed.has(p))
        if (more.length === 0) {
          noNewCount++
          if (sr?.atBottom || noNewCount >= 8) break
          continue
        }
        // 다음 루프에서 getPageInfo 재시도
        continue
      }
      noNewCount = 0

      const path = allPaths[0]
      processed.add(path)
      const writeUrl = meta.buildWriteUrl(path)
      console.log(`[적립금-리뷰] ${action} 작성 시도: ${path}`)

      // write 탭 열고 폼 채우기
      let writeTabId = null
      try {
        const wt = await chrome.tabs.create({ url: writeUrl, active: false })
        writeTabId = wt.id
        try { await waitForTabLoad(writeTabId, 25000) } catch {}
        await new Promise(r => setTimeout(r, 1500))
        try {
          await chrome.scripting.executeScript({ target: { tabId: writeTabId }, files: [contentFile] })
        } catch (e) {
          lastError = `write 스크립트 주입 실패: ${e?.message || e}`
        }
        const result = await chrome.tabs.sendMessage(writeTabId, { action: 'samba_review_fillAndSubmit' }).catch(e => ({ success: false, error: e?.message || String(e) }))
        if (result?.success) {
          writeCount++
          console.log(`[적립금-리뷰] ${action} ${writeCount}건 완료`)
        } else {
          lastError = result?.error || 'unknown'
          console.log(`[적립금-리뷰] ${action} 실패: ${lastError}`)
        }
      } finally {
        if (writeTabId) {
          try { await chrome.tabs.remove(writeTabId) } catch {}
        }
      }

      // 작성 간 랜덤 대기 (3~7초) — 무신사 rate limit 회피
      await new Promise(r => setTimeout(r, 3000 + Math.random() * 4000))

      // 목록 페이지로 복귀 + 새로고침
      try {
        await chrome.tabs.update(listTabId, { url: meta.listUrl })
        await waitForTabLoad(listTabId, 30000)
        await new Promise(r => setTimeout(r, 3000))
      } catch {}
    }
  } catch (e) {
    lastError = String(e?.message || e)
    console.warn(`[적립금-리뷰] ${action} 오류:`, e)
  } finally {
    if (listTabId) {
      try { await chrome.tabs.remove(listTabId) } catch {}
    }
  }

  await postResult('sourcing-accounts/extension/reward-result', {
    request_id: requestId,
    account_id: sourcingAccountId || '',
    site_name: site,
    action,
    success: writeCount > 0,
    stamp_count: writeCount, // 리뷰 작성 건수 (백엔드가 review 카운트로 누적)
    error: writeCount === 0 ? lastError : '',
  })
  console.log(`[적립금-리뷰] ${action} 종료 — 작성 ${writeCount}건, 에러='${lastError}'`)
}

async function handleRewardJob(job) {
  const { requestId, site, url, action, sourcingAccountId } = job
  console.log(`[적립금] 잡 수신 site=${site} action=${action} acc=${sourcingAccountId || '-'} req=${requestId}`)

  // 자동 로그인: 잡의 sourcingAccountId 로 ensureLoggedIn 시도 (현재 다른 계정이면 스왑)
  const autoKey = _REWARD_ACTION_AUTO_LOGIN[action]
  if (autoKey && typeof globalThis.ensureLoggedIn === 'function' && sourcingAccountId) {
    try {
      const ok = await globalThis.ensureLoggedIn(autoKey, { accountId: sourcingAccountId })
      if (!ok) console.warn(`[적립금] 자동 로그인 실패 — 그대로 진행 (site=${site})`)
    } catch (e) {
      console.warn(`[적립금] 자동 로그인 예외: ${e?.message || e}`)
    }
  }

  // 리뷰 액션은 멀티페이지 orchestrator 사용
  if (action.endsWith('_review')) {
    return await handleReviewJob(job)
  }

  const contentFile = _REWARD_ACTION_CONTENT[action]
  if (!contentFile) {
    await postResult('sourcing-accounts/extension/reward-result', {
      request_id: requestId,
      account_id: sourcingAccountId || '',
      site_name: site,
      action,
      success: false,
      error: `unknown action: ${action}`,
    })
    return
  }

  let tabId = null
  try {
    if (!url) throw new Error('reward URL 누락')
    const tab = await chrome.tabs.create({ url, active: false })
    tabId = tab.id
    try { await waitForTabLoad(tabId, 30000) } catch {}
    // 페이지 안정화 대기
    await new Promise(r => setTimeout(r, 1500))

    // musinsa_balance 액션: manifest 자동 주입 content-musinsa-balance.js가 처리
    // → 별도 reward-result 콜백 없이 8초 대기 후 종료(기존 sync-balance가 DB 갱신)
    if (action === 'musinsa_balance') {
      await new Promise(r => setTimeout(r, 8000))
      await postResult('sourcing-accounts/extension/reward-result', {
        request_id: requestId,
        account_id: sourcingAccountId || '',
        site_name: site,
        action,
        success: true,
        already_done: false,
      })
      return
    }

    // content script 주입
    try {
      await chrome.scripting.executeScript({
        target: { tabId },
        files: [contentFile],
      })
    } catch (e) {
      throw new Error(`스크립트 주입 실패: ${e?.message || e}`)
    }

    // 스냅 좋아요는 페이지 이동(미션→피드→미션복귀) 마다 스크립트 재주입 필요
    let navListener = null
    if (action === 'musinsa_snap_like') {
      navListener = (changedTabId, info) => {
        if (changedTabId !== tabId || info.status !== 'complete') return
        // 미션/스냅 페이지 모두 같은 스크립트 사용 (페이지 내부에서 location.href 분기)
        chrome.scripting.executeScript({
          target: { tabId },
          files: [contentFile],
        }).catch(() => {})
      }
      chrome.tabs.onUpdated.addListener(navListener)
    }

    // 결과 수신 대기 — 스냅은 멀티스텝이라 90초, 그 외 45초
    const timeoutMs = action === 'musinsa_snap_like' ? 90000 : 45000
    const result = await new Promise((resolve) => {
      const timeoutId = setTimeout(() => {
        _rewardPending.delete(tabId)
        resolve({ success: false, error: 'timeout: 적립 결과 수신 실패' })
      }, timeoutMs)
      _rewardPending.set(tabId, { resolve, timeoutId, tabId, action, requestId })
    })
    if (navListener) {
      try { chrome.tabs.onUpdated.removeListener(navListener) } catch {}
    }

    await postResult('sourcing-accounts/extension/reward-result', {
      request_id: requestId,
      account_id: sourcingAccountId || '',
      site_name: site,
      action,
      success: !!result.success,
      already_done: !!result.alreadyDone,
      reward: Number(result.reward || 0),
      streak_count: Number(result.streakCount || 0),
      money: result.money !== undefined ? Number(result.money) : null,
      mileage: result.mileage !== undefined ? Number(result.mileage) : null,
      stamp_count: result.stampCount !== undefined ? Number(result.stampCount) : null,
      stamp_score: result.stampScore !== undefined ? Number(result.stampScore) : null,
      error: result.error || '',
    })
    console.log(`[적립금] 결과 전송 ${site}/${action} success=${result.success} reward=${result.reward || 0}`)
  } catch (err) {
    console.warn(`[적립금] 처리 실패 req=${requestId}:`, err)
    try {
      await postResult('sourcing-accounts/extension/reward-result', {
        request_id: requestId,
        account_id: sourcingAccountId || '',
        site_name: site,
        action,
        success: false,
        error: String(err?.message || err),
      })
    } catch {}
  } finally {
    if (tabId) {
      try { await chrome.tabs.remove(tabId) } catch {}
    }
  }
}

// content script(주입된 페이지)가 보내는 적립 결과 메시지 매칭 — sender.tab.id 로 1:1 매칭
chrome.runtime.onMessage.addListener((msg, sender, _sendResponse) => {
  if (!msg || msg.type !== 'REWARD_RESULT') return
  const tabId = sender?.tab?.id
  if (!tabId) return
  const pending = _rewardPending.get(tabId)
  if (pending) {
    clearTimeout(pending.timeoutId)
    _rewardPending.delete(tabId)
    pending.resolve(msg)
  }
})


async function handleTrackingJob(job) {
  const { requestId, site, url, sourcingOrderNumber, sourcingAccountId } = job
  const isReturn = !!job.isReturn
  console.log(`[송장] 잡 수신 site=${site} ord=${sourcingOrderNumber} acc=${sourcingAccountId || '-'} req=${requestId}${isReturn ? ' [회수송장]' : ''}`)

  // [정책 2026-05-16] — 한 PC 다계정 순회 지원.
  // → 0단계: 현재 로그인 계정 감지 (캐시 30분)
  // → 1단계: 잡 계정 ≠ 현재 계정이면 **선제적 ensureLoggedIn 스왑** (잡 처리 전 미리 로그인 전환)
  //          백엔드가 sourcing_account_id 로 그룹화 적재하므로 스왑 횟수는 계정 수 ≈ N회로 최소화.
  // → 2단계: 1차 시도. 실패면 wrong_account/needsLogin 시 한 번 더 재로그인 시도.
  // 멀티 PC 운영 시: ensureLoggedIn 자체가 실패하면 wrong_account 보고 → 다른 PC fallback.

  // 0단계 — 현재 로그인 계정 식별
  let currentAccountId = null
  try {
    currentAccountId = await _detectCurrentSourcingAccount(site)
  } catch (e) {
    console.warn(`[송장] 계정 감지 실패 (무시): ${e?.message || e}`)
  }
  const _isCurrentMatch = currentAccountId && sourcingAccountId && currentAccountId === sourcingAccountId
  if (currentAccountId) {
    console.log(`[송장] 현재 로그인=${currentAccountId} / 잡 매칭=${sourcingAccountId || '-'} → ${_isCurrentMatch ? '매칭(스왑 불필요)' : '미매칭(선제 스왑 시도)'}`)
  } else {
    console.log(`[송장] 현재 로그인 계정 식별 불가 — 1차 시도 후 조건부 스왑`)
  }

  let tabId = null
  let cleaned = false
  try {
    if (!url) throw new Error('tracking URL 누락')

    // 송장 페이지 진입 → 결과 수신
    // 사용자 요청(2026-05-16): 송장수집 시 포커스 뺏지 않도록 모든 사이트 active:false 통일.
    // MUSINSA hydration 이슈는 content-tracking-musinsa.js 의 폴링 로직으로 대응.
    // chrome.tabs.create 호출은 다른 탭이 detach/close 전이 중이거나 사용자가 탭을 드래그하는
      // 짧은 순간에 "Tabs cannot be edited right now (user may be dragging a tab)" 로 거부될 수 있다.
      // 일시적이므로 짧은 백오프로 최대 3회 재시도.
      const _createTabWithRetry = async () => {
        let lastErr = null
        for (let i = 0; i < 3; i++) {
          try {
            return await chrome.tabs.create({ url, active: false })
          } catch (e) {
            lastErr = e
            const msg = String(e?.message || e)
            if (!/Tabs cannot be edited|dragging/i.test(msg)) throw e
            await new Promise(r => setTimeout(r, 800 + i * 600))
          }
        }
        throw lastErr
      }
    const _runOnce = async () => {
      const tab = await _createTabWithRetry()
      tabId = tab.id
      await waitForTabLoad(tabId, 30000)

      return await new Promise((resolve) => {
        const timeoutId = setTimeout(() => {
          _trackingPending.delete(requestId)
          resolve({ success: false, error: 'timeout: content script 응답 없음' })
        }, 120000)  // 60s → 120s: 자동 로그인 직후 무신사 SPA hydration + 모달 폴링 여유
        _trackingPending.set(requestId, { resolve, timeoutId, tabId })

        try {
          chrome.tabs.sendMessage(tabId, {
            type: 'TRACKING_REQUEST',
            requestId,
            site,
            sourcingOrderNumber,
            isReturn,
          }, (_resp) => {
            void chrome.runtime.lastError
          })
        } catch {}
      })
    }

    // 선제 스왑 헬퍼 — 잡 계정으로 ensureLoggedIn → 캐시 무효화. 성공 여부 반환.
    const autoLoginKey = _TRACKING_AUTO_LOGIN_MAP[site]
    const _swapToJobAccount = async (reason) => {
      if (!autoLoginKey || typeof globalThis.ensureLoggedIn !== 'function' || !sourcingAccountId) {
        console.log(`[송장] 스왑 스킵(${reason}) — 미지원 사이트(${site}) 또는 accountId 없음`)
        return false
      }
      console.log(`[송장] 계정 스왑 시도(${reason}) → acc=${sourcingAccountId}`)
      try {
        const ok = await globalThis.ensureLoggedIn(autoLoginKey, { accountId: sourcingAccountId })
        if (ok) {
          try { await _invalidateCurrentAccountCache(site) } catch {}
          // 메모리 캐시 갱신 — 다음 잡에서 같은 계정이면 스왑 스킵
          _lastEnsuredTrackingAccount[autoLoginKey] = sourcingAccountId
          console.log(`[송장] 계정 스왑 성공 — acc=${sourcingAccountId}`)
        } else {
          console.warn(`[송장] 계정 스왑 실패(${reason}) acc=${sourcingAccountId}`)
        }
        return ok
      } catch (e) {
        console.warn(`[송장] 계정 스왑 예외: ${e?.message || e}`)
        return false
      }
    }

    // 1단계 — 잡 계정과 "마지막 스왑 계정" 또는 "현재 로그인 계정" 이 다르면 선제 스왑
    //   • currentAccountId(DOM 스크랩)는 무신사 mypage username 못 잡으면 null → 신뢰 못함.
    //   • _lastEnsuredTrackingAccount(메모리 캐시)는 ensureLoggedIn 성공 이력 — ground truth.
    //   • 둘 다 잡 계정과 다르면 선제 스왑. 메모리 캐시 매칭이면 굳이 스왑 안 함(빠른 경로).
    const _lastEnsured = autoLoginKey ? _lastEnsuredTrackingAccount[autoLoginKey] : null
    const _knownMismatch = sourcingAccountId && (
      (_lastEnsured && _lastEnsured !== sourcingAccountId) ||  // 메모리 캐시와 불일치
      (!_lastEnsured && currentAccountId && currentAccountId !== sourcingAccountId) ||  // DOM 캐시와 불일치
      (!_lastEnsured && !currentAccountId)  // 둘 다 모름 → 안전하게 스왑
    )
    let _preemptiveSwapAttempted = false
    let _loginFailMsg = '' // "로그인 실패" 표준 문구 — 백엔드 서킷브레이커 매칭용
    if (_knownMismatch) {
      _preemptiveSwapAttempted = true
      const swapOk = await _swapToJobAccount('preemptive')
      if (!swapOk) {
        const _le = globalThis._lastEnsureLoginError
        if (_le?.fatal) {
          // [2026-06-10] 계정 잠금/자격증명 오류/쿨다운 차단 = 빈 시도는 로그인 POST만 늘려
          // SSG 잠금을 갱신한다 → 스크랩 시도 없이 즉시 실패 보고 (브레이커가 재큐잉 차단)
          console.warn(`⚠ [송장] 로그인 치명적 실패 — 즉시 실패 보고: ${_le.message}`)
          await postResult('sourcing/tracking-result', {
            requestId,
            success: false,
            error: _le.message,
          })
          return
        }
        if (_le?.message) _loginFailMsg = _le.message
        // 그 외 스왑 실패 — 현재 세션 그대로 1차 시도. wrong_account 보고 후 다른 PC fallback.
      }
    } else if (_lastEnsured && _lastEnsured === sourcingAccountId) {
      console.log(`[송장] 메모리 캐시 매칭 — 스왑 스킵 (acc=${sourcingAccountId})`)
    }

    // 2단계 — 1차 시도
    let result = await _runOnce()

    // 3단계 — wrong_account/needsLogin/timeout 감지 시 최대 2회 재시도 (백오프).
    // wrong_account = 메모리 캐시와 실제 무신사 세션이 동기화 깨진 신호 (세션 만료/무신사 보안).
    // 매 재시도: 메모리 캐시 무효화 + ensureLoggedIn 재호출 + 3초 백오프 + _runOnce.
    const _isWrong = (r) => r && (
      r.wrongAccount === true ||
      (typeof r.error === 'string' && /wrong_account|not_my_order|account_mismatch|계정불일치/i.test(r.error))
    )
    const _isTimeout = (r) => r && typeof r.error === 'string' && /timeout/i.test(r.error)
    // unexpected_page = 무신사 SPA 가 송장 URL 진입 후 다른 페이지로 자동 리다이렉트 케이스.
    // abnormal_access = 무신사 "정상적인 접근이 아닙니다" 차단. trace 진입 타임아웃 포함.
    const _isUnexpectedPage = (r) => r && typeof r.error === 'string' && /unexpected_page|abnormal_access|trace 페이지 진입 타임아웃/i.test(r.error)

    // [정책 2026-05-16] wrong_account = 백엔드 sourcing_account_id 매핑이 잘못됐을 가능성.
    // 같은 계정으로 재시도해도 같은 결과 + 다른 계정 자동 순회는 무신사 보안 차단 위험 ↑.
    // → 자동 재시도 안 함. 경고 메시지만 남기고 운영자 수동 처리(매핑 확인) 위임.
    if (_isWrong(result)) {
      // [2026-06-06] wrong_account = 메모리 캐시(_lastEnsured)와 실제 무신사 세션이 어긋난
      // 캐시-stale 일 수 있음 → 캐시 무효화 + **잡 계정으로만** 강제 재로그인 + 1회 재시도.
      // 같은 잡 계정 재로그인이라 다른 계정 순회(무신사 보안 차단) 위험 없음.
      // 재로그인 후에도 wrong 이면 진짜 매핑 오류 → 경고 후 포기.
      console.warn(`⚠ [송장][wrong_account] 캐시-stale 의심 → 캐시 무효화 + ${sourcingAccountId || '-'} 강제 재로그인 + 1회 재시도 (ord=${sourcingOrderNumber})`)
      if (autoLoginKey && _lastEnsuredTrackingAccount[autoLoginKey]) {
        delete _lastEnsuredTrackingAccount[autoLoginKey]
      }
      try { if (tabId) { await chrome.tabs.remove(tabId); tabId = null } } catch {}
      const _reloginOk = await _swapToJobAccount('wrong_account-retry')
      if (_reloginOk) {
        result = await _runOnce()
        if (_isWrong(result)) {
          console.warn(`⚠ [송장][wrong_account] 재로그인 후에도 불일치 — 진짜 매핑 오류 가능. 운영자가 진짜 무신사 계정 확인 후 매핑 수정 필요. (acc=${sourcingAccountId || '-'}, ord=${sourcingOrderNumber})`)
        }
      } else {
        console.warn(`⚠ [송장][wrong_account] 잡 계정 재로그인 실패 — 다른 PC fallback. (acc=${sourcingAccountId || '-'})`)
      }
    }

    // timeout / unexpected_page / needsLogin 만 자동 재시도 (최대 2회 + 백오프).
    let retryAttempt = 0
    const MAX_RETRY = 2
    while (((result && result.needsLogin) || _isTimeout(result) || _isUnexpectedPage(result)) && retryAttempt < MAX_RETRY) {
      retryAttempt++
      const reason = _isTimeout(result) ? 'timeout' : (_isUnexpectedPage(result) ? 'unexpected_page' : 'needsLogin')
      console.log(`[송장] ${reason} 감지 (${retryAttempt}/${MAX_RETRY}) → 캐시 무효화 + 재로그인 + ${retryAttempt > 1 ? '3초 백오프 + ' : ''}재시도 (acc=${sourcingAccountId || '-'})`)
      if (autoLoginKey && _lastEnsuredTrackingAccount[autoLoginKey]) {
        delete _lastEnsuredTrackingAccount[autoLoginKey]
      }
      try { if (tabId) { await chrome.tabs.remove(tabId); tabId = null } } catch {}
      if (retryAttempt > 1) {
        await new Promise(r => setTimeout(r, 3000))
      }
      const loginOk = await _swapToJobAccount(`${reason}-retry${retryAttempt}`)
      if (!loginOk) {
        const _le = globalThis._lastEnsureLoginError
        if (_le?.message) _loginFailMsg = _le.message
        break  // 무한 retry 방지
      }
      result = await _runOnce()
    }

    // 로그인 실패로 끝난 잡은 에러를 "로그인 실패" 표준 문구로 교체 — timeout/needsLogin 으로
    // 보고되면 백엔드 서킷브레이커(`%로그인 실패%`)가 못 잡아 같은 계정 재큐잉이 계속된다.
    if (!result.success && !result.cancelled && _loginFailMsg && !_isWrong(result)) {
      result = { ...result, error: _loginFailMsg }
    }

    await postResult('sourcing/tracking-result', {
      requestId,
      success: !!result.success,
      courierName: result.courierName || '',
      trackingNumber: result.trackingNumber || '',
      error: result.error || '',
      cancelled: !!result.cancelled,
    })
  } catch (err) {
    console.warn(`[송장] 처리 실패 req=${requestId}:`, err)
    try {
      await postResult('sourcing/tracking-result', {
        requestId,
        success: false,
        error: String(err?.message || err),
      })
    } catch {}
  } finally {
    if (tabId && !cleaned) {
      try { await chrome.tabs.remove(tabId) } catch {}
      cleaned = true
    }
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// 발주취소 잡 핸들러 (소싱처 주문상세 페이지 → DOM 자동화 → cancel-result 전송)
//
// 사이트별 cancel_js 분석 전이라 현재는 스텁 — 미지원 회신만 보낸다.
// 분석 완료 후 사이트별 content-cancel-{site}.js 작성 + 본 함수에서 라우팅 추가 예정.
//
// 라우팅 정책:
//  - SSG/ABCmart/GrandStage/LOTTEON  → 데몬 전용 (확장앱 라우팅 차단됨)
//  - MUSINSA/GSShop/패션플러스/SNKRDUNK/KREAM/Nike/롯데홈쇼핑 → 확장앱(여기)
//
// 결과 스키마: {success, cancelled, alreadyShipped?, reason?, error?}
// ─────────────────────────────────────────────────────────────────────────────
const _cancelPending = new Map() // requestId → {resolve, timeoutId, tabId}

async function handleCancelOrderJob(job) {
  const requestId = job.requestId
  const site = (job.site || '').toUpperCase()
  const ordNo = job.sourcingOrderNumber || ''
  const sourcingAccountId = job.sourcingAccountId || ''

  console.log(`[발주취소] 잡 수신 req=${requestId} site=${site} ord=${ordNo} acc=${sourcingAccountId || '-'}`)

  let result = { success: false, cancelled: false, reason: '미지원 사이트' }

  // 계정 스왑 — 잡의 sourcingAccountId 로 ensureLoggedIn (송장 잡과 동일 패턴)
  // 계정 불일치 상태로 cancel 호출하면 다른 계정 주문에 영향 갈 위험. 무조건 swap 강제.
  const autoLoginKey = _TRACKING_AUTO_LOGIN_MAP[site]
  if (autoLoginKey && sourcingAccountId && typeof globalThis.ensureLoggedIn === 'function') {
    try {
      const ok = await globalThis.ensureLoggedIn(autoLoginKey, { accountId: sourcingAccountId })
      if (!ok) {
        try {
          await postResult('sourcing/cancel-result', {
            requestId,
            success: false,
            cancelled: false,
            error: `계정 스왑 실패 (acc=${sourcingAccountId}) — 운영자 수동 처리 필요`,
          })
        } catch {}
        return
      }
      // 메모리 캐시 갱신
      _lastEnsuredTrackingAccount[autoLoginKey] = sourcingAccountId
    } catch (e) {
      try {
        await postResult('sourcing/cancel-result', {
          requestId,
          success: false,
          cancelled: false,
          error: `계정 스왑 예외: ${e?.message || e}`,
        })
      } catch {}
      return
    }
  } else if (sourcingAccountId && !autoLoginKey) {
    // 자동로그인 매핑 없는 사이트 — sourcingAccountId 검증 불가, 운영자 책임 사용
    console.warn(`[발주취소] ${site} 자동로그인 매핑 없음 — 현재 세션으로 진행 (acc=${sourcingAccountId})`)
  }

  try {
    if (site === 'MUSINSA') {
      result = await _cancelMusinsa(ordNo, sourcingAccountId)
    } else if (site === 'LOTTEON') {
      result = await _cancelLotteon(ordNo, sourcingAccountId)
    } else {
      result.reason = `확장앱 cancel 미구현(site=${site})`
    }
  } catch (err) {
    result = { success: false, cancelled: false, error: String(err && err.message || err) }
    console.warn(`[발주취소] 처리 예외 req=${requestId}:`, err)
  }

  try {
    await postResult('sourcing/cancel-result', { requestId, ...result })
  } catch (err) {
    console.warn(`[발주취소] 결과 전송 실패 req=${requestId}:`, err)
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// _offscreenFetch — debugger API + declarativeNetRequest 패턴.
//
// 배경: MV3 ServiceWorker fetch + offscreen fetch 모두 무신사 HTTPOnly 세션쿠키
// 자동 첨부 못 함. chrome.cookies API 도 first-party context 의 진짜 세션 쿠키
// 안 보여줌(분석 쿠키 5개만 노출).
//
// 해결: chrome.debugger.attach 로 무신사 탭에 attach → Network.getCookies 로
// HTTPOnly 포함 진짜 세션 쿠키 추출 → declarativeNetRequest session rule 로
// Cookie 헤더 set → SW fetch → rule 제거 → debugger detach.
//
// 무신사 탭이 없으면 chrome.tabs.create({active:false}) 로 임시 생성 후 닫음.
// ─────────────────────────────────────────────────────────────────────────────
const _MUSINSA_DNR_RULE_ID = 99001  // 일시 룰 ID

async function _attachDebugger(tabId) {
  return new Promise((resolve, reject) => {
    chrome.debugger.attach({tabId}, '1.3', () => {
      if (chrome.runtime.lastError) reject(new Error(chrome.runtime.lastError.message))
      else resolve()
    })
  })
}

async function _detachDebugger(tabId) {
  return new Promise((resolve) => {
    chrome.debugger.detach({tabId}, () => { void chrome.runtime.lastError; resolve() })
  })
}

async function _sendDebuggerCmd(tabId, method, params) {
  return new Promise((resolve, reject) => {
    chrome.debugger.sendCommand({tabId}, method, params || {}, (result) => {
      if (chrome.runtime.lastError) reject(new Error(chrome.runtime.lastError.message))
      else resolve(result)
    })
  })
}

async function _waitForTabComplete(tabId, timeoutMs = 15000) {
  const start = Date.now()
  while (Date.now() - start < timeoutMs) {
    const t = await chrome.tabs.get(tabId)
    if (t.status === 'complete') return true
    await new Promise(r => setTimeout(r, 200))
  }
  return false
}

async function _getOrCreateMusinsaTab() {
  // 1. 기존 musinsa 탭 우선
  const tabs = await chrome.tabs.query({url: 'https://*.musinsa.com/*'})
  if (tabs && tabs.length > 0) {
    return { tabId: tabs[0].id, ephemeral: false }
  }
  // 2. 백그라운드 탭 임시 생성
  const tab = await chrome.tabs.create({url: 'https://www.musinsa.com/', active: false})
  await _waitForTabComplete(tab.id, 15000)
  return { tabId: tab.id, ephemeral: true }
}

async function _extractMusinsaCookieHeader(tabId) {
  // 모든 musinsa 관련 URL의 cookies 수집
  const urls = [
    'https://www.musinsa.com/',
    'https://api.musinsa.com/',
    'https://order.musinsa.com/',
    'https://my.musinsa.com/',
  ]
  const res = await _sendDebuggerCmd(tabId, 'Network.getCookies', { urls })
  const cookies = res?.cookies || []
  // name=value 형식으로 join. path/domain match 신경 X — 한 도메인 그룹.
  const seen = new Set()
  const parts = []
  for (const c of cookies) {
    const key = c.name
    if (seen.has(key)) continue
    seen.add(key)
    parts.push(`${c.name}=${c.value}`)
  }
  return { header: parts.join('; '), count: parts.length }
}

async function _installDNRCookieRule(cookieHeader) {
  await chrome.declarativeNetRequest.updateSessionRules({
    removeRuleIds: [_MUSINSA_DNR_RULE_ID],
    addRules: [{
      id: _MUSINSA_DNR_RULE_ID,
      priority: 1,
      action: {
        type: 'modifyHeaders',
        requestHeaders: [
          { header: 'Cookie', operation: 'set', value: cookieHeader },
          { header: 'Origin', operation: 'set', value: 'https://www.musinsa.com' },
          { header: 'Referer', operation: 'set', value: 'https://www.musinsa.com/' },
        ],
      },
      condition: {
        urlFilter: '||musinsa.com/',
        resourceTypes: ['xmlhttprequest', 'sub_frame', 'main_frame', 'other'],
      },
    }],
  })
}

async function _removeDNRCookieRule() {
  try {
    await chrome.declarativeNetRequest.updateSessionRules({
      removeRuleIds: [_MUSINSA_DNR_RULE_ID],
      addRules: [],
    })
  } catch (_) {}
}

// 세션 캐시 — 1회 cancel 호출 안에서 cookies 재추출 비용 절감
let _musinsaCookieCache = { header: '', at: 0 }
const _COOKIE_CACHE_TTL_MS = 60 * 1000  // 1분

async function _prepareMusinsaCookies() {
  if (_musinsaCookieCache.header && (Date.now() - _musinsaCookieCache.at < _COOKIE_CACHE_TTL_MS)) {
    await _installDNRCookieRule(_musinsaCookieCache.header)
    return { count: _musinsaCookieCache.header.split('; ').length, cached: true }
  }
  const ctx = await _getOrCreateMusinsaTab()
  try {
    await _attachDebugger(ctx.tabId)
    try {
      const { header, count } = await _extractMusinsaCookieHeader(ctx.tabId)
      _musinsaCookieCache = { header, at: Date.now() }
      await _installDNRCookieRule(header)
      return { count, cached: false }
    } finally {
      await _detachDebugger(ctx.tabId)
    }
  } finally {
    if (ctx.ephemeral) {
      try { await chrome.tabs.remove(ctx.tabId) } catch (_) {}
    }
  }
}

async function _offscreenFetch({url, method = 'GET', formFields = null, body = null, isJson = false, headers = {}}) {
  // 1. cookies 준비 (debugger + DNR rule)
  let cookieInfo
  try {
    cookieInfo = await _prepareMusinsaCookies()
  } catch (e) {
    return { ok: false, error: `cookies prepare 실패: ${e.message || e}` }
  }
  // 2. SW fetch
  try {
    const init = { method, credentials: 'include', headers: { ...headers } }
    if (formFields) {
      const fd = new FormData()
      for (const [k, v] of Object.entries(formFields)) fd.append(k, v == null ? '' : String(v))
      init.body = fd
    } else if (isJson && body) {
      init.body = JSON.stringify(body)
      init.headers['Content-Type'] = 'application/json'
    }
    const r = await fetch(url, init)
    const txt = await r.text()
    return {
      ok: true, status: r.status, body: txt,
      headers: Object.fromEntries(r.headers.entries()),
      cookieInfo,
    }
  } catch (e) {
    return { ok: false, error: e.message || String(e) }
  } finally {
    // DNR rule 제거 — 다른 사이트 영향 방지. cookies 캐시는 유지.
    await _removeDNRCookieRule()
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// 무신사 헤드리스 발주취소 — 페이지 진입 없이 fetch 만 사용.
//
// 사전 분석(2026-05-26 CDP 9223 실측):
//  1. GET /order-service/my/order/get_order_view/{ord_no}      → orderOptionNo 추출
//  2. GET order.musinsa.com/api2/order/v1/order-items/{opt_no}/status
//       → code 10(결제완료) / 20(상품준비중) 만 자동취소 가능
//       code >= 30 (배송중/배송완료/취소완료) → alreadyShipped:true
//  3. POST api.musinsa.com/api2/claim/command/mypage/order_cancel_cmd/refund (multipart)
//     form: ord_no, ord_opt_nos, claim_reason=1(단순변심), refund_bank/account/nm/cancel_content=''
//
// 라인아이템 여러 개일 수 있음 — orderOptionList 순회하며 각각 호출.
// 무신사머니 결제는 refund_* 빈값 OK. 카드/계좌 결제 필요 시 추후 확장.
// ─────────────────────────────────────────────────────────────────────────────
async function _cancelMusinsa(ordNo, expectedAccountId) {
  if (!ordNo) return { success: false, cancelled: false, error: 'ordNo empty' }

  // 1. 주문 정보 조회 → orderOptionList + 현재 로그인 계정 검증
  let view
  try {
    const r = await _offscreenFetch({
      url: `https://www.musinsa.com/order-service/my/order/get_order_view/${ordNo}`,
      method: 'GET',
      headers: { 'Accept': 'application/json' },
    })
    if (!r.ok) return { success: false, cancelled: false, error: `get_order_view 실패: ${r.error}` }
    if (r.status === 401 || r.status === 403) {
      return { success: false, cancelled: false, error: '무신사 로그인 만료 — 운영자 재로그인 필요' }
    }
    try { view = JSON.parse(r.body) } catch (e) {
      return { success: false, cancelled: false, error: `get_order_view JSON parse 실패: ${(r.body||'').slice(0,200)}` }
    }
  } catch (e) {
    return { success: false, cancelled: false, error: `get_order_view 실패: ${e.message || e}` }
  }
  let items = ((view && view.orderList && view.orderList.orderOptionList) || []).filter(Boolean)
  // items 비어있어도 expectedAccountId 있으면 계정 mismatch 가능성 → 일단 스왑 시도 후 재조회.
  // 즉시 abort 금지.

  // 사전 계정 검증 — view.orderInfo.user_id 로 현재 무신사 로그인 username 추출.
  // 송장수집과 동일 패턴: find-by-username 으로 백엔드 accountId 매핑 → expectedAccountId 비교.
  // 불일치 시 → ensureLoggedIn 강제(캐시 무효화) → view 재조회 + 재검증 → 그래도 다르면 abort.
  const _resolveAccountByUsername = async (username) => {
    if (!username) return null
    try {
      const stored = await chrome.storage.local.get('proxyUrl')
      const proxyUrl = stored.proxyUrl || ''
      const apiFetch = globalThis.SambaBackgroundCore?.apiFetch
      if (!apiFetch) return null
      const res = await apiFetch(
        `${proxyUrl}/api/v1/samba/sourcing-accounts/find-by-username?site_name=MUSINSA&username=${encodeURIComponent(username)}`,
        { method: 'GET' }
      )
      if (!res.ok) return null
      const j = await res.json()
      return j?.id || null
    } catch (_) { return null }
  }
  const _refreshView = async () => {
    try {
      const r = await _offscreenFetch({
        url: `https://www.musinsa.com/order-service/my/order/get_order_view/${ordNo}`,
        method: 'GET', headers: {'Accept':'application/json'},
      })
      if (!r.ok) return null
      try { return JSON.parse(r.body) } catch (_) { return null }
    } catch (_) { return null }
  }

  if (expectedAccountId) {
    let currentUserId = (view?.orderInfo?.user_id) || ''
    let currentAccId = await _resolveAccountByUsername(currentUserId)
    if (currentAccId !== expectedAccountId) {
      console.log(`[발주취소-무신사] 계정 불일치 (현재=${currentUserId}/${currentAccId} vs 잡=${expectedAccountId}) — 강제 스왑 시도`)
      // 캐시 무효화 후 ensureLoggedIn 강제
      try { if (globalThis._lastAutoLoginSuccessAt?.musinsa) delete globalThis._lastAutoLoginSuccessAt.musinsa[expectedAccountId] } catch (_) {}
      try { if (globalThis._lastAutoLoginSuccessAt?.musinsa) delete globalThis._lastAutoLoginSuccessAt.musinsa['_default'] } catch (_) {}
      if (typeof globalThis.ensureLoggedIn !== 'function') {
        return { success: false, cancelled: false, error: '계정 스왑 불가 — ensureLoggedIn 미정의' }
      }
      const swapOk = await globalThis.ensureLoggedIn('musinsa', { accountId: expectedAccountId })
      if (!swapOk) {
        return { success: false, cancelled: false, error: `계정 스왑 실패 (현재=${currentUserId}, 목표 acct=${expectedAccountId}). 운영자 수동 처리 필요` }
      }
      // 스왑 성공 — cookies 캐시 무효화 (옛 세션 cookies 폐기). 다음 _offscreenFetch 가 재추출.
      _musinsaCookieCache = { header: '', at: 0 }
      // 안정화 대기 — 로그인 직후 cookies 반영 지연 가능
      await new Promise(r => setTimeout(r, 1500))
      // 재검증
      view = await _refreshView()
      const reItems = (view?.orderList?.orderOptionList || [])
      if (!reItems.length) {
        return { success: false, cancelled: false, error: `스왑 후에도 주문 접근 불가 (현재=${(view?.orderInfo?.user_id)||'unknown'}, 목표 acct=${expectedAccountId})` }
      }
      currentUserId = view?.orderInfo?.user_id || ''
      currentAccId = await _resolveAccountByUsername(currentUserId)
      if (currentAccId !== expectedAccountId) {
        return { success: false, cancelled: false, error: `스왑 후에도 계정 불일치 (현재=${currentUserId}/${currentAccId} vs 잡=${expectedAccountId})` }
      }
      console.log(`[발주취소-무신사] 스왑 성공 → ${currentUserId}`)
      // 재검증 통과 — items 재설정
      items = reItems
    }
  }

  const results = []
  let allCancelled = true
  let anyAlreadyShipped = false
  let alreadyCancelledCount = 0
  let newlyCancelledCount = 0
  let skippedCount = 0
  for (const it of items) {
    const optNo = String(it.orderOptionNo || '')
    if (!optNo) continue

    // claimState !== 0 = 이미 취소/반품 처리됨 (또는 우리 직전 호출이 이미 성공함)
    // raw 검증(2026-05-26 ord=202605260959460003): claimState=61 + orderStateText="취소 완료"
    // + buttonStatus 전부 disabled = 취소 처리 확정.
    // → "실패"가 아니라 "이미 취소됨" 으로 success 처리. cancel-result router 가 status='cancelling' 으로 advance.
    if (it.claimState && it.claimState !== 0) {
      results.push({
        optNo,
        ok: true,
        already: true,
        reason: `claimState=${it.claimState} (이미 취소 처리됨)`,
      })
      alreadyCancelledCount++
      continue
    }

    // 2. status 가용성 체크 — code 10/20만 취소 허용
    let code = it.orderState
    try {
      const rs = await _offscreenFetch({
        url: `https://order.musinsa.com/api2/order/v1/order-items/${optNo}/status`,
        method: 'GET', headers: { 'Accept': 'application/json' },
      })
      if (rs.ok && rs.body) {
        const sj = JSON.parse(rs.body)
        if (sj && sj.data && typeof sj.data.code === 'number') code = sj.data.code
      }
    } catch (_) { /* status 실패 시 orderState 그대로 사용 */ }

    if (code >= 30) {
      // 배송중/배송완료/취소완료 등 — 자동취소 불가
      results.push({ optNo, skipped: true, reason: `code=${code} 배송/완료 단계` })
      anyAlreadyShipped = true
      allCancelled = false
      skippedCount++
      continue
    }
    if (code !== 10 && code !== 20) {
      results.push({ optNo, skipped: true, reason: `code=${code} 미지원 단계` })
      allCancelled = false
      skippedCount++
      continue
    }

    // 3. cancel POST (multipart) — claim_reason=1 (단순변심)
    const _formFields = {
      ord_no: ordNo, ord_opt_nos: optNo,
      refund_bank: '', refund_account: '', refund_nm: '',
      claim_reason: '1', cancel_content: '',
    }
    const _doCancel = async () => {
      const r = await _offscreenFetch({
        url: 'https://api.musinsa.com/api2/claim/command/mypage/order_cancel_cmd/refund',
        method: 'POST', formFields: _formFields,
      })
      if (!r.ok) return { status: 0, ok: false, body: r.error || 'offscreen fetch fail' }
      const txt = r.body || ''
      // 실측(2026-05-26): 권한거부 시 HTTP 200 + {code:999, message:"Invalid Access", meta:{result:"SUCCESS"}}.
      // meta.result 는 신뢰 못함. code===1 만 진짜 성공. errorCode 있으면 무조건 실패.
      let ok = false
      let bodyJson = null
      try { bodyJson = JSON.parse(txt) } catch (_) {}
      if (bodyJson) {
        const code = bodyJson.code ?? bodyJson.cd
        const errorCode = bodyJson?.meta?.errorCode
        ok = (code === 1) && !errorCode
      } else {
        ok = r.status === 200 && /SUCCESS/i.test(txt) && !/Invalid Access|errorCode/i.test(txt)
      }
      return { status: r.status, ok, body: txt.slice(0, 300) }
    }
    try {
      let res = await _doCancel()
      if (!res.ok && (res.status === 401 || res.status === 403) && expectedAccountId
          && typeof globalThis.ensureLoggedIn === 'function') {
        // 강제 재로그인 — 캐시 TTL 무시. ensureLoggedIn 캐시 초기화.
        try { if (globalThis._lastAutoLoginSuccessAt?.musinsa) delete globalThis._lastAutoLoginSuccessAt.musinsa[expectedAccountId] } catch (_) {}
        const swapOk = await globalThis.ensureLoggedIn('musinsa', { accountId: expectedAccountId })
        if (swapOk) {
          // 재로그인 성공 후 — view 다시 조회해서 user_id 재검증
          try {
            const vr = await fetch(`https://www.musinsa.com/order-service/my/order/get_order_view/${ordNo}`, {credentials: 'include', headers: {'Accept':'application/json'}})
            const vj = await vr.json()
            const reUser = (vj?.orderInfo?.user_id || '').toLowerCase()
            // 매핑 다시
            try {
              const mr = await postResult('sourcing/musinsa-account-username', {accountId: expectedAccountId})
              const expUser = (mr?.username || '').toLowerCase()
              if (expUser && reUser && expUser !== reUser) {
                results.push({ optNo, status: res.status, ok: false, body: `403 후 재로그인했으나 계정 여전히 불일치(${reUser} vs ${expUser})` })
                allCancelled = false
                continue
              }
            } catch (_) {}
          } catch (_) {}
          res = await _doCancel()
          res.retried = true
        } else {
          res.retried = 'swap_failed'
        }
      }
      results.push({ optNo, ...res })
      if (res.ok) newlyCancelledCount++
      if (!res.ok) allCancelled = false
    } catch (e) {
      results.push({ optNo, error: e.message || String(e) })
      allCancelled = false
    }
  }

  // 최종 판정 — already + newly + skipped 조합
  // 모든 라인이 already 거나 newly 면 = 전체 취소됨 (success)
  // 일부 배송단계면 alreadyShipped flag
  // 그 외 = 일부 실패
  const totalItems = items.length
  const allDone = (alreadyCancelledCount + newlyCancelledCount) === totalItems
  return {
    success: allDone,
    cancelled: allDone,
    alreadyShipped: anyAlreadyShipped,
    reason: allDone
      ? (alreadyCancelledCount === totalItems
          ? '무신사에 이미 취소 처리됨 (claimState!=0)'
          : (newlyCancelledCount === totalItems
              ? '무신사 발주취소 완료'
              : `완료 (신규=${newlyCancelledCount}, 이미취소=${alreadyCancelledCount})`))
      : (anyAlreadyShipped
          ? '일부/전부 배송단계 — 자동취소 불가'
          : `일부 실패 (신규=${newlyCancelledCount}, 이미취소=${alreadyCancelledCount}, skipped=${skippedCount})`),
    details: results,
  }
}


// ─────────────────────────────────────────────────────────────────────────────
// LOTTEON 발주취소 — 데몬 자동로그인 봇 차단(2026-05-26 실측)으로 확장앱 라우팅.
//
// 무신사와 다른 점: LOTTEON 은 사용자 chrome 세션 필수 + native JS confirm dialog
// 자동 accept 필요. ServiceWorker fetch 만으로는 불가(Vue v-select 등 페이지 JS 필요).
// 해결: chrome.tabs.create({active:false}) 백그라운드 탭 + chrome.debugger.attach
// → Page.handleJavaScriptDialog 자동 accept → cancel_js evaluate → 결과 회수 → 탭 close.
//
// 사용자 시야: active:false 라 활성 탭 변경 X. 탭 리스트에 잠깐 표시되나 즉시 닫힘(~10초).
// ─────────────────────────────────────────────────────────────────────────────
async function _cancelLotteon(ordNo, expectedAccountId) {
  if (!ordNo) return { success: false, cancelled: false, error: 'ordNo empty' }

  const cancelUrl = `https://www.lotteon.com/p/order/claim/cancellation/orderCancellationAccept?odNo=${ordNo}&odSeq=1&procSeq=1`
  let tab = null
  let attached = false
  try {
    // 1. 백그라운드 탭 생성
    tab = await chrome.tabs.create({url: cancelUrl, active: false})
    // 페이지 로드 대기
    await new Promise(resolve => {
      const start = Date.now()
      const check = () => {
        chrome.tabs.get(tab.id, (t) => {
          if (chrome.runtime.lastError) return resolve()
          if (t.status === 'complete' || Date.now() - start > 15000) return resolve()
          setTimeout(check, 300)
        })
      }
      check()
    })
    await new Promise(r => setTimeout(r, 2000))

    // 2. debugger attach + Page.javascriptDialogOpening 자동 accept
    await new Promise((resolve, reject) => {
      chrome.debugger.attach({tabId: tab.id}, '1.3', () => {
        if (chrome.runtime.lastError) return reject(new Error(chrome.runtime.lastError.message))
        resolve()
      })
    })
    attached = true
    await new Promise((resolve, reject) => {
      chrome.debugger.sendCommand({tabId: tab.id}, 'Page.enable', {}, () => {
        if (chrome.runtime.lastError) return reject(new Error(chrome.runtime.lastError.message))
        resolve()
      })
    })
    const dialogHandler = (source, method, params) => {
      if (source.tabId !== tab.id) return
      if (method === 'Page.javascriptDialogOpening') {
        chrome.debugger.sendCommand({tabId: tab.id}, 'Page.handleJavaScriptDialog', {accept: true}, () => {
          void chrome.runtime.lastError
        })
      }
    }
    chrome.debugger.onEvent.addListener(dialogHandler)

    try {
      // 3. cancel_js evaluate
      const cancelJs = _LOTTEON_CANCEL_JS_FOR_EXT
      const result = await new Promise((resolve, reject) => {
        chrome.debugger.sendCommand(
          {tabId: tab.id}, 'Runtime.evaluate',
          {expression: cancelJs, returnByValue: true, awaitPromise: true, timeout: 60000},
          (res) => {
            if (chrome.runtime.lastError) return reject(new Error(chrome.runtime.lastError.message))
            resolve(res)
          }
        )
      })
      const val = result?.result?.value
      const data = typeof val === 'string' ? JSON.parse(val) : (val || {})
      chrome.debugger.onEvent.removeListener(dialogHandler)
      return {
        success: !!data.success,
        cancelled: !!(data.cancelled || data.success),
        alreadyShipped: !!data.alreadyShipped,
        reason: data.reason || '',
        details: data,
      }
    } finally {
      chrome.debugger.onEvent.removeListener(dialogHandler)
    }
  } catch (err) {
    return { success: false, cancelled: false, error: `LOTTEON cancel 예외: ${err.message || err}` }
  } finally {
    if (attached && tab) {
      try { chrome.debugger.detach({tabId: tab.id}, () => void chrome.runtime.lastError) } catch (_) {}
    }
    if (tab) {
      try { await chrome.tabs.remove(tab.id) } catch (_) {}
    }
  }
}


// LOTTEON cancel_js — daemon.py LOTTEON_CANCEL_JS 와 동일 흐름 (Vue v-select mousedown + 사유 + 동의 + 최종 클릭).
// dialog 자동 accept 는 chrome.debugger Page.handleJavaScriptDialog 가 담당.
const _LOTTEON_CANCEL_JS_FOR_EXT = `
(async () => {
  try { window.confirm = () => true; window.alert = () => {}; } catch(_) {}
  let vs = null
  for (let i = 0; i < 50; i++) {
    vs = document.querySelector('div.v-select')
    if (vs) break
    await new Promise(r => setTimeout(r, 200))
  }
  if (!vs) return JSON.stringify({success: false, error: 'v-select 사유 dropdown 미발견'})
  const opener = vs.querySelector('.vs__selected-options') || vs
  opener.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, button: 0}))
  opener.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, button: 0}))
  opener.click()
  await new Promise(r => setTimeout(r, 800))
  let selected = false
  for (const el of document.querySelectorAll('ul.vs__dropdown-menu li[role=option], .vs__dropdown-option')) {
    const t = (el.innerText || el.textContent || '').trim()
    if (t === '구매 의사 없어짐' || t === '구매의사 없어짐') {
      el.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, button: 0}))
      el.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, button: 0}))
      el.click()
      selected = true
      break
    }
  }
  if (!selected) return JSON.stringify({success: false, error: '사유 옵션(구매 의사 없어짐) 미발견'})
  await new Promise(r => setTimeout(r, 500))
  const agreeIds = ['claimAgree','paymentAgree','checkbox_fnclTx','checkbox_indivisualInfoCollection','checkbox_indivisualInfoConsignment']
  for (const id of agreeIds) {
    const el = document.getElementById(id)
    if (el && !el.checked) el.click()
  }
  await new Promise(r => setTimeout(r, 500))
  let cancelResp = null
  const origFetch = window.fetch.bind(window)
  window.fetch = async function(input, init) {
    const url = typeof input === 'string' ? input : (input && input.url) || ''
    const res = await origFetch(input, init)
    if (/processOrderCancellation/.test(url)) {
      try { const cl = res.clone(); cancelResp = JSON.parse(await cl.text()) } catch(_) {}
    }
    return res
  }
  const _xo = XMLHttpRequest.prototype.open
  const _xs = XMLHttpRequest.prototype.send
  XMLHttpRequest.prototype.open = function(m, u) { this.__sambaU = u; return _xo.apply(this, arguments) }
  XMLHttpRequest.prototype.send = function(b) {
    this.addEventListener('load', () => {
      try { if (/processOrderCancellation/.test(this.__sambaU || '')) cancelResp = JSON.parse(this.responseText) } catch(_) {}
    })
    return _xs.apply(this, arguments)
  }
  let clicked = false
  for (const el of document.querySelectorAll('button')) {
    const t = (el.innerText || '').trim()
    if (t === '취소요청' && !el.disabled) { el.click(); clicked = true; break }
  }
  if (!clicked) return JSON.stringify({success: false, error: '취소요청 버튼 미발견'})
  const start = Date.now()
  while (Date.now() - start < 25000) {
    if (cancelResp !== null) break
    await new Promise(r => setTimeout(r, 300))
  }
  if (!cancelResp) return JSON.stringify({success: false, error: 'processOrderCancellation 응답 timeout'})
  const code = (cancelResp.returnCode || cancelResp.code || '').toString()
  const msg = cancelResp.message || cancelResp.msg || ''
  const ok = code === '200' || /SUCCESS/i.test(msg)
  return JSON.stringify({
    success: ok, cancelled: ok,
    alreadyShipped: /이미\\s*발송|이미\\s*취소|배송\\s*시작/.test(msg),
    reason: ok ? 'LOTTEON 발주취소 완료' : (msg || 'returnCode=' + code),
    response: cancelResp,
  })
})()
`

// content script 가 페이지에서 추출 결과를 background로 보낼 때 매칭
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg && msg.type === 'TRACKING_RESULT' && msg.requestId) {
    const pending = _trackingPending.get(msg.requestId)
    if (pending) {
      clearTimeout(pending.timeoutId)
      _trackingPending.delete(msg.requestId)
      pending.resolve({
        success: !!msg.success,
        courierName: msg.courierName || '',
        trackingNumber: msg.trackingNumber || '',
        error: msg.error || '',
        cancelled: !!msg.cancelled,
        needsLogin: !!msg.needsLogin,
      })
    }
    sendResponse({ ack: true })
    return true
  }
  return false
})

let _lastAutotuneRunning = false

async function _checkAutotuneStartTransition() {
  // /autotune/status 직접 호출 (background-autologin.js의 _isAutotuneActive 캐시와 분리해
  // 트랜지션 감지 정확도 우선)
  try {
    const stored = await chrome.storage.local.get('proxyUrl')
    const proxyUrl = stored.proxyUrl || ''
    const apiFetch = globalThis.SambaBackgroundCore?.apiFetch
    const res = apiFetch
      ? await apiFetch(`${proxyUrl}/api/v1/samba/collector/autotune/status`, { method: 'GET' })
      : await fetch(`${proxyUrl}/api/v1/samba/collector/autotune/status`, { method: 'GET' })
    if (!res.ok) return
    const data = await res.json()
    const running = !!data.running
    if (running && !_lastAutotuneRunning) {
      console.log('[오토튠] 백엔드 시작 감지 — 이 PC의 시작 버튼 클릭 시에만 폴링 합류')
    }
    _lastAutotuneRunning = running
  } catch { /* 무시 — 다음 사이클에서 재시도 */ }
}

globalThis._checkAutotuneStartTransition = _checkAutotuneStartTransition
// ABCmart 로그인 체크 — 오토튠 시작 시 1회, 이후 1시간마다 실행
// a-rt.com 메인 페이지를 백그라운드 탭으로 열어 LOGOUT 버튼 감지 (최대 3분 대기)
async function _checkAbcmartLogin() {
  console.log('[ABCmart] 로그인 체크 시작 (최대 3분)')
  // 체크 시작 시점에 clear하지 않음 — 처리 중인 아이템과의 race condition 방지
  // 실패(3분 타임아웃) 확정 시에만 clear (아래 return false 직전)
  let tabId = null
  try {
    const tab = await chrome.tabs.create({ url: 'https://www.a-rt.com/', active: false })
    tabId = tab.id
    const deadline = Date.now() + 3 * 60 * 1000
    while (Date.now() < deadline) {
      await new Promise(r => setTimeout(r, 5000))
      try {
        const [r] = await chrome.scripting.executeScript({
          target: { tabId },
          world: 'MAIN',
          func: () => {
            const scope = document.querySelector('header, #header, .header, nav, #gnb, .gnb, [class*="gnb"], [class*="header"]') || document.body
            for (const el of scope.querySelectorAll('a[href], button')) {
              const href = (el.getAttribute('href') || '').toLowerCase()
              const txt = (el.textContent || '').trim()
              if (href.includes('logout') || txt === '로그아웃' || txt === 'Logout' || txt === 'LOGOUT') return true
            }
            return false
          }
        })
        if (r?.result) {
          _siteLoginConfirmed.add('ABCmart')
          _siteLoginConfirmed.add('GrandStage')
          _abcLoginConfirmedAt = Date.now()
          console.log('[ABCmart] 로그인 확인 완료 — 오토튠 중 재체크 없음')
          return true
        }
      } catch { /* 탭 로딩 중 — 재시도 */ }
    }
    console.warn('[ABCmart] 3분 내 로그인 미확인 — 확장앱에서 a-rt.com 로그인 필요')
    _siteLoginConfirmed.delete('ABCmart')
    _siteLoginConfirmed.delete('GrandStage')
    return false
  } catch (e) {
    console.error('[ABCmart] 로그인 체크 오류:', e.message)
    return false
  } finally {
    if (tabId) try { await chrome.tabs.remove(tabId) } catch {}
  }
}

// ABCmart pre-login 대기 Promise — pollSourcingOnce 진입 시 완료까지 블로킹 (LOTTEON과 동일 패턴)
let _abcPreLoginPromise = null
// LOTTEON pre-login 대기 Promise — pollSourcingOnce 진입 시 완료까지 블로킹
let _lotteonPreLoginPromise = null

async function _runLotteonPreLogin() {
  try {
    if (typeof ensureLoggedIn !== 'function') return
    console.log('[LOTTEON] pre-login 시작 (첫 폴링 전 — 비로그인 시 오토로그인 완료 후 폴링 개시)')
    await ensureLoggedIn('lotteon')
    console.log('[LOTTEON] pre-login 완료')
  } catch (e) {
    console.log('[LOTTEON] pre-login 오류:', e?.message)
  }
}

globalThis._setLocalAutotuneJoined = (joined, sourceSites = null) => {
  _localAutotuneJoined = !!joined
  _allowedSourceSites = joined ? sourceSites : null
  // 폴링 헤더(X-Allowed-Sites)는 chrome.storage.allowedSites를 읽는다(background-core.js).
  // JOIN 시 오토튠 선택 소싱처를 storage에도 반영 — 페이지→확장앱 SET_ALLOWED_SITES가
  // content script 타이밍으로 누락돼도, 폴링이 stale 사이트 목록으로 백엔드 PC분담 등록을
  // 덮어쓰지 않게 한다. (SSG 선택인데 확장앱 stale [MUSINSA]가 등록을 덮어 SSG가
  // active_sites에서 탈락하던 flip-flop의 근본 원인 차단.)
  if (joined && Array.isArray(sourceSites)) {
    try {
      chrome.storage.local.set({ allowedSites: sourceSites })
    } catch (_) {}
  }
  if (joined) {
    _sourcingForceStop = false
    _siteLoginConfirmed.clear()
    console.log(`[오토튠] 이 PC 폴링 합류 (소싱처: ${sourceSites === null ? '전체' : (sourceSites.length ? sourceSites.join(',') : '없음')})`)
    // sourceSites: null=전체선택, [...]=선택된 소싱처 목록
    const abcSelected = sourceSites === null || sourceSites.includes('ABCmart') || sourceSites.includes('GrandStage')
    const lotteonSelected = sourceSites === null || sourceSites.includes('LOTTEON')
    // ABCmart pre-login — ABCmart/GrandStage가 선택된 경우만
    if (abcSelected) {
      // 2시간 이내 로그인 확인됐으면 재체크 스킵 (LotteON 자동로그인 등 무관한 재합류 시 불필요한 3분 체크 방지)
      const _abcRecentlyConfirmed = _abcLoginConfirmedAt > 0 && (Date.now() - _abcLoginConfirmedAt) < 2 * 60 * 60 * 1000
      if (_abcRecentlyConfirmed) {
        _siteLoginConfirmed.add('ABCmart')
        _siteLoginConfirmed.add('GrandStage')
        _abcPreLoginPromise = null
        console.log('[ABCmart] pre-login 스킵 — 최근 로그인 확인됨 (재체크 불필요)')
      } else {
        _abcPreLoginPromise = _checkAbcmartLogin()
      }
      if (_abcLoginCheckTimer) clearInterval(_abcLoginCheckTimer)
      _abcLoginCheckTimer = setInterval(() => { _checkAbcmartLogin() }, 60 * 60 * 1000)
    } else {
      console.log('[ABCmart] pre-login 스킵 — 선택된 소싱처에 ABCmart/GrandStage 없음')
      _abcPreLoginPromise = null
    }
    // LOTTEON pre-login — LOTTEON이 선택된 경우만
    if (lotteonSelected) {
      _lotteonPreLoginPromise = _runLotteonPreLogin()
      if (_lotteonLoginCheckTimer) clearInterval(_lotteonLoginCheckTimer)
      _lotteonLoginCheckTimer = setInterval(() => { _runLotteonPreLogin() }, 60 * 60 * 1000)
    } else {
      console.log('[LOTTEON] pre-login 스킵 — 선택된 소싱처에 LOTTEON 없음')
      _lotteonPreLoginPromise = null
    }
  } else {
    _siteLoginConfirmed.clear()
    if (_abcLoginCheckTimer) { clearInterval(_abcLoginCheckTimer); _abcLoginCheckTimer = null }
    if (_lotteonLoginCheckTimer) { clearInterval(_lotteonLoginCheckTimer); _lotteonLoginCheckTimer = null }
    _abcPreLoginPromise = null
    _lotteonPreLoginPromise = null
    console.log('[오토튠] 이 PC 폴링 탈퇴')
  }
}

// 사이트별 인증 실패 카운트 — 비로그인 가격 수집 차단 정책 (즉시 트리거)
// 정책:
//   - 비로그인 신호 1회로 즉시 자동로그인 트리거 (10건 비로그인 가격 통과 차단)
//   - 한 번 트리거되면 1시간 쿨다운 (그 안에는 추가 트리거 X)
//   - DOM false-positive 차단은 사이트별 사전 게이트(_preCheckLogin)로 처리
const _alFailureCount = {}
const _AL_FAILURE_THRESHOLD = 1  // 10 → 1 (비로그인 가격 수집 즉시 차단)
// 사이트별 마지막 자동로그인 트리거 시각 — 1시간 쿨다운
const _alLastTriggerAt = {}
const _AL_TRIGGER_COOLDOWN_MS = 60 * 60 * 1000  // 5분 → 1시간

// 사용자가 명시적으로 자동로그인을 끄고 싶을 때 chrome.storage.disableAutoLogin = true
// 또는 사이트별 chrome.storage.disableAutoLoginSites = ['LOTTEON','SSG'] 가능
async function _isAutoLoginDisabled(externalSite) {
  try {
    const data = await chrome.storage.local.get(['disableAutoLogin', 'disableAutoLoginSites'])
    if (data.disableAutoLogin === true) return true
    if (Array.isArray(data.disableAutoLoginSites) && data.disableAutoLoginSites.includes(externalSite)) {
      return true
    }
  } catch {}
  return false
}

// immediate 인자는 이제 의미 약화 — 모든 신호에 누적 카운트 적용 (DOM false-positive 차단)
function reportLoginFailure(externalSite, immediate = false) {
  if (!externalSite) return
  if (typeof isSiteActiveForSourcing === 'function' && !isSiteActiveForSourcing(externalSite)) {
    console.log(`[로그인감지] ${externalSite} 비로그인 신호 무시 - 현재 활성 수집 사이트 아님`)
    return
  }
  _alFailureCount[externalSite] = (_alFailureCount[externalSite] || 0) + 1
  // 누적 임계값 미도달이면 silent (로그도 적당히)
  if (_alFailureCount[externalSite] < _AL_FAILURE_THRESHOLD) {
    if (_alFailureCount[externalSite] === 1 || _alFailureCount[externalSite] % 5 === 0) {
      console.log(`[로그인감지] ${externalSite} 비로그인 신호 누적 ${_alFailureCount[externalSite]}/${_AL_FAILURE_THRESHOLD} (자동로그인 미트리거)`)
    }
    return
  }
  const key = (typeof alExternalSiteToKey === 'function') ? alExternalSiteToKey(externalSite) : null
  if (!key || typeof ensureLoggedIn !== 'function') {
    _alFailureCount[externalSite] = 0
    return
  }
  // 자동로그인 비활성 옵션 검사 (사용자 명시적 OFF)
  _isAutoLoginDisabled(externalSite).then(disabled => {
    if (disabled) {
      console.log(`[로그인감지] ${externalSite} 자동로그인 비활성 옵션 켜짐 — 트리거 스킵`)
      _alFailureCount[externalSite] = 0
      return
    }
    // 쿨다운 검사 (1시간) — 병렬 작업 + 누적 임계값 동시 트리거 방지
    const lastAt = _alLastTriggerAt[externalSite] || 0
    const elapsed = Date.now() - lastAt
    if (lastAt && elapsed < _AL_TRIGGER_COOLDOWN_MS) {
      const remainingMin = Math.ceil((_AL_TRIGGER_COOLDOWN_MS - elapsed) / 60000)
      console.log(`[로그인감지] ${externalSite} 자동로그인 쿨다운 중 (잔여 ${remainingMin}분) — 트리거 스킵`)
      _alFailureCount[externalSite] = 0
      return
    }
    console.log(`[로그인감지] ${externalSite} 비로그인 누적 ${_alFailureCount[externalSite]}회 → 자동로그인 트리거`)
    _alFailureCount[externalSite] = 0
    _alLastTriggerAt[externalSite] = Date.now()
    ensureLoggedIn(key)
      .then(ok => {
        // 무신사 자동로그인 실패 = 쿠키 손실 확정 → 잡 중단 + 경고
        if (externalSite === 'MUSINSA' && !ok) {
          _handleMusinsaCookieLost()
        }
      })
      .catch(e => console.error('[자동로그인] 호출 오류:', e?.message || e))
  })
}

function reportLoginSuccess(externalSite) {
  if (_alFailureCount[externalSite]) {
    _alFailureCount[externalSite] = 0
  }
  // 무신사 로그인 확인 → 쿠키 손실 플래그 해제(잡 재개)
  if (externalSite === 'MUSINSA') {
    _clearMusinsaCookieLost()
  }
}

// 전 사이트 공통 로그인 상태 감지 — 헤더 영역의 로그인/로그아웃 링크로 판단
// 비로그인 페이지에 노출되는 마케팅 가격(예: LOTTEON "나의 혜택가") false-positive 차단용
// 반환: true(로그인) | false(비로그인) | null(판단 불가, 안전상 로그인으로 간주)
async function _detectLoginStatus(tabId, site) {
  try {
    const [r] = await chrome.scripting.executeScript({
      target: { tabId },
      func: (siteName) => {
        // 사이트별 로그인/로그아웃 식별 패턴
        const cfg = {
          ABCmart:    { login: ['/login', 'member/login'], logout: ['/logout', '/mypage', '/myinfo'] },
          GrandStage: { login: ['/login', 'member/login'], logout: ['/logout', '/mypage', '/myinfo'] },
          LOTTEON:    { login: ['/member/login/common'], logout: ['/logout', '/mypage', '/p/member/logout'] },
          // SSG: 카드혜택가 비로그인 동일 노출 — 로그인 체크 불필요
          GSShop:     { login: ['login.gs', '/login'], logout: ['logout.gs', '/mypage', '/myinfo'] },
          MUSINSA:    { login: ['/auth/login', 'member.one.musinsa.com/login'], logout: ['/logout', '/mypage'] },
          KREAM:      { login: ['/login'], logout: ['/logout', 'kream.co.kr/my'] },
        }
        const c = cfg[siteName]
        if (!c) return { isLoggedIn: null, reason: 'unsupported' }

        // 헤더 영역 우선 (본문 내 마케팅 링크 노이즈 차단)
        const headerEl = document.querySelector('header, #header, .header, nav, #gnb, .gnb, [class*="gnb"], [class*="header"]')
        const scope = headerEl || document.body

        let hasLoginLink = false
        let hasLogoutLink = false

        const elements = scope.querySelectorAll('a[href], button')
        for (const el of elements) {
          const href = (el.getAttribute('href') || '').toLowerCase()
          const txt = (el.textContent || '').trim()

          // 로그아웃 신호 — 가장 강한 확정 신호
          if (href.includes('logout') || txt === '로그아웃' || txt === 'Logout' || txt === 'LOGOUT') {
            hasLogoutLink = true
            continue
          }

          // 로그인 신호 — 텍스트가 정확히 "로그인"이거나 href에 login 패턴
          if (txt === '로그인' || txt === 'Login' || txt === 'LOGIN') {
            hasLoginLink = true
            continue
          }
          for (const p of c.login) {
            if (href.includes(p.toLowerCase()) && !href.includes('logout')) {
              // href만으로는 약한 신호 — 텍스트가 짧거나 로그인 관련 표시면 인정
              if (txt.length < 20 && (txt.includes('로그인') || txt.includes('Login') || txt === '' || el.querySelector('img[alt*="로그인"], img[alt*="login"]'))) {
                hasLoginLink = true
              }
              break
            }
          }
        }

        // 로그아웃 링크 발견 = 로그인 확정 (로그인 링크도 함께 있어도 무시 — 숨겨진 요소 오탐 방지)
        if (hasLogoutLink) return { isLoggedIn: true }
        if (hasLoginLink) return { isLoggedIn: false, reason: 'login link present' }
        // 둘 다 없으면 헤더 selector가 안 잡혔거나 사이트 구조 변경 — 보수적으로 null
        return { isLoggedIn: null, reason: 'no signal' }
      },
      args: [site],
    })
    const out = r?.result
    if (out && out.isLoggedIn === false) {
      console.log(`[로그인감지] ${site} 비로그인 (${out.reason || ''})`)
    }
    return out?.isLoggedIn
  } catch (e) {
    console.log(`[로그인감지] ${site} 검사 오류 (무시): ${e.message}`)
    return null
  }
}

// globalThis 명시 — let은 service worker importScripts 글로벌에 노출 안 됨 (콘솔 검증 불가).
// 콘솔에서 typeof globalThis._pollSourcingInflight 로 검증 가능.
globalThis._pollSourcingInflight = false
async function pollSourcingOnce() {
  // ⚠️ _localAutotuneJoined 가드 절대 추가 금지 ⚠️
  // "다른 PC 편승 차단"을 위해 guard를 붙이면 수동 업데이트(업데이트 버튼)가 깨진다.
  // 편승 차단은 백엔드 get_next_job()의 ownerDeviceId 매칭이 이미 보장한다.
  // 백엔드가 배치 크기만큼만 큐에 넣으므로 자연히 그 수만큼 처리됨
  //
  // [중요] inflight lock — runFocusPoll(0.5s)과 runPollCycle(30s alarm)이 동시 트리거되어
  // pollSourcingOnce가 병렬 실행되면 tracking 잡이 여러 batch로 흩어져 1번 잡 처리 중
  // 4, 6, 8번이 먼저 끝나는 현상 발생 (직렬 처리 무용). 한 번에 한 호출만 허용.
  if (globalThis._pollSourcingInflight) return false
  globalThis._pollSourcingInflight = true
  try {
    return await _pollSourcingOnceImpl()
  } finally {
    globalThis._pollSourcingInflight = false
  }
}

async function _pollSourcingOnceImpl() {
  const jobs = []
  for (let i = 0; i < SOURCING_MAX_POLL_LIMIT; i++) {
    try {
      const res = await apiFetch(`${PROXY_URL}${API_PREFIX}/sourcing/collect-queue`)
      if (!res.ok) {
        if (res.status === 503) pauseCollectPolling(30000, 'backend shutting down')
        break
      }
      const job = await res.json()
      if (job.shuttingDown) {
        pauseCollectPolling(30000, 'backend shutting down')
        break
      }
      // 백엔드가 요구하는 최소 확장앱 버전 — 미달이면 폴링 중단 + 경고 (사용자 업데이트 유도)
      if (job.minExtVersion && typeof globalThis._isExtVersionBelow === 'function'
          && globalThis._isExtVersionBelow(job.minExtVersion)) {
        console.warn(`[소싱] 확장앱 버전이 백엔드 요구치(${job.minExtVersion}) 미만 — 폴링 중단`)
        pauseCollectPolling(300000, `extension version below ${job.minExtVersion}`)
        break
      }
      if (job.forceStop) {
        // 오토튠 stop 직후 — 이미 받은 작업 포함 전부 버리고 즉시 중단
        _sourcingForceStop = true
        // _allowedSourceSites 까지 함께 초기화하기 위해 세터 경유 (직접 대입 시 상태 불일치)
        if (typeof globalThis._setLocalAutotuneJoined === 'function') {
          globalThis._setLocalAutotuneJoined(false)
        } else {
          _localAutotuneJoined = false
          _allowedSourceSites = null
        }
        jobs.length = 0
        break
      }
      if (!job.hasJob) break
      // 무신사 쿠키 손실 중 — 무신사 잡만 drop(다른 소싱처는 계속).
      // 재확인 인터벌 경과 시엔 통과시켜 프로브(쿠키 복구 자동 감지)로 쓴다.
      if (job.site === 'MUSINSA' && _musinsaCookieLostActive()) {
        console.warn('[소싱] 무신사 쿠키 손실 — 무신사 잡 drop(재로그인 대기)')
        continue
      }
      // 실제 잡 수신 = 서버가 오토튠 재개 상태 → forceStop 플래그 자동 해제
      if (_sourcingForceStop) {
        console.log('[소싱] 오토튠 재개 감지 — forceStop 해제')
        _sourcingForceStop = false
      }
      console.log(`[소싱] ${job.url || '작업 수신'} (${jobs.length + 1}/${SOURCING_MAX_POLL_LIMIT})`)
      jobs.push(job)
    } catch {
      pauseCollectPolling(10000, 'backend unreachable')
      break
    }
  }
  if (jobs.length === 0) return false
  if (jobs.length === 1) {
    await _processJobWithCap(jobs[0])
  } else {
    // tracking 잡은 직렬 처리 — 같은 batch에 여러 계정 잡 섞이면 병렬 ensureLoggedIn으로
    // 계정 swap 충돌 발생 (병기 진행 중인데 성희 잡이 swap 트리거 → 병기 wrong session → DISPATCHED stuck).
    // tracking 외(detail/search 가격수집)는 계정 무관해서 병렬 유지.
    const trackingJobs = jobs.filter(j => j.type === 'tracking')
    const otherJobs = jobs.filter(j => j.type !== 'tracking')
    if (trackingJobs.length > 0) {
      console.log(`[소싱] tracking 직렬 처리: ${trackingJobs.length}개 (계정 swap 충돌 방지)`)
      for (const job of trackingJobs) {
        await _processJobWithCap(job)
      }
    }
    if (otherJobs.length > 0) {
      console.log(`[소싱] 병렬 처리: ${otherJobs.length}개 (사이트별 동시실행 캡 적용)`)
      await Promise.all(otherJobs.map(job => _processJobWithCap(job)))
    }
  }
  return true
}

// 롯데ON: sitmNo + 쿠키 기반 pbf API 직접 호출로 혜택가 수집 (탭 불필요)
async function fetchLotteonBenefitPrice(productId, sitmNo) {
  try {
    if (!sitmNo) {
      console.log(`[LOTTEON] pbf 혜택가: sitmNo 없음 — 스킵 (${productId})`)
      return null
    }

    // 1. lotteon.com 쿠키 수집
    const cookies = await chrome.cookies.getAll({ domain: '.lotteon.com' })
    const cookieStr = cookies.map(c => `${c.name}=${c.value}`).join('; ')
    if (!cookieStr) {
      console.log(`[LOTTEON] pbf 혜택가: 쿠키 없음 — 스킵 (${productId})`)
      return null
    }

    // 2. pbf API 호출 (수동 Cookie 헤더 — 서비스워커에서 credentials:'include' 무효)
    const pbfResp = await fetch(`https://pbf.lotteon.com/product/v2/detail/search/base/sitm/${sitmNo}`, {
      headers: {
        'Cookie': cookieStr,
        'Accept': 'application/json, text/plain, */*',
        'Origin': 'https://www.lotteon.com',
        'Referer': 'https://www.lotteon.com/',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
      }
    })
    const pbfData = await pbfResp.json()
    const data = pbfData.data || {}
    const priceInfo = data.priceInfo || {}
    const slPrc = parseInt(priceInfo.slPrc || 0)
    const immdDc = parseInt(priceInfo.immdDcAplyTotAmt || 0)
    const adtnDc = parseInt(priceInfo.adtnDcAplyTotAmt || 0)

    let benefitPrice = 0
    let salePrice = slPrc
    if (slPrc > 0 && (immdDc > 0 || adtnDc > 0)) {
      benefitPrice = slPrc - immdDc - adtnDc
      if (benefitPrice <= 0 || benefitPrice >= slPrc) benefitPrice = 0
    }

    console.log(`[LOTTEON] pbf 혜택가: ${productId} slPrc=${slPrc}, immdDc=${immdDc}, adtnDc=${adtnDc}, benefit=${benefitPrice}`)

    if (benefitPrice > 0) {
      return {
        success: true,
        site_product_id: productId,
        sale_price: salePrice,
        best_benefit_price: benefitPrice,
        source_site: 'LOTTEON',
      }
    }
    // immdDc=0 → 쿠키 인증 안 됐을 가능성 → null 반환하여 DOM 폴백
    return null
  } catch (err) {
    console.error('[LOTTEON] pbf 혜택가 실패:', err.message)
    return null
  }
}

// ABCmart/GrandStage: 서비스워커에서 직접 fetch — 탭 없이 사용자 IP+세션으로 호출
// LOTTEON pbf 패턴과 동일. 백엔드 IP는 IP-bound 세션 차단당해 alwaysDscntAmt=0 받음.
// 확장앱이 사용자 PC에서 호출하면 loginYn=Y + 정확한 alwaysDscntAmt 수신.
async function fetchAbcmartBenefitPriceServiceWorker(productId, site) {
  try {
    // 1. .a-rt.com 쿠키 수집 (브라우저가 first-party로 저장한 사용자 세션)
    const cookies = await chrome.cookies.getAll({ domain: 'a-rt.com' })
    const cookieStr = cookies.map(c => `${c.name}=${c.value}`).join('; ')
    if (!cookieStr) {
      console.log(`[${site}] SW fetch: 쿠키 없음 — 스킵 (${productId})`)
      return null
    }

    // 2. info API 호출 (수동 Cookie 헤더 — 서비스워커에서 credentials:'include' 무효)
    const subdomain = site === 'GrandStage' ? 'grandstage.a-rt.com' : 'abcmart.a-rt.com'
    const resp = await fetch(`https://${subdomain}/product/info?prdtNo=${productId}`, {
      headers: {
        'Cookie': cookieStr,
        'Accept': 'application/json, text/plain, */*',
        'Origin': 'https://www.a-rt.com',
        'Referer': 'https://www.a-rt.com/',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
      },
    })
    if (!resp.ok) {
      console.log(`[${site}] SW fetch: HTTP ${resp.status} — 폴백 (${productId})`)
      return null
    }
    const data = await resp.json()
    if (!data || !data.prdtName) {
      console.log(`[${site}] SW fetch: 빈 응답 — 폴백 (${productId})`)
      return null
    }

    const loginYn = (data.loginYn || '').toUpperCase()
    if (loginYn !== 'Y') {
      console.log(`[${site}] SW fetch: 비로그인 응답(loginYn=${loginYn}) — 폴백 (${productId})`)
      return null
    }

    // 3. 가격 계산: sale_price - alwaysDscntAmt - max(coupon discount)
    const pi = data.productPrice || {}
    const sellAmt = parseInt(pi.sellAmt || 0)
    const displayPrice = parseInt(data.displayProductPrice || 0)
    const salePrice = displayPrice > 0 ? displayPrice : sellAmt
    const normalAmt = parseInt(pi.normalAmt || 0) || salePrice

    const membershipDiscount = parseInt(data.alwaysDscntAmt || 0)
    const coupons = data.maxBenefitCoupon || data.coupon || []
    // maxBenefitCoupon은 중복적용 가능한 쿠폰 묶음(일반+플러스 등) — 전부 합산해야 정확
    let couponDiscount = 0
    for (const c of coupons) {
      couponDiscount += parseInt(c.dscntAmt || 0)
    }

    let benefitPrice = salePrice - membershipDiscount - couponDiscount
    if (benefitPrice <= 0 || benefitPrice > salePrice) benefitPrice = salePrice

    console.log(`[${site}] SW fetch 성공: ${productId} sale=${salePrice} membership=${membershipDiscount} coupon=${couponDiscount} benefit=${benefitPrice}`)
    reportLoginSuccess(site)

    return {
      success: true,
      site_product_id: productId,
      name: (data.prdtName || '').trim(),
      original_price: normalAmt,
      sale_price: salePrice,
      best_benefit_price: benefitPrice,
      source_site: site,
    }
  } catch (err) {
    console.error(`[${site}] SW fetch 실패:`, err.message)
    return null
  }
}

// ABCmart/GrandStage 로그인 검증 — info API의 loginYn 응답으로 판정.
// 사용자 PC 쿠키로 호출하므로 IP-bound 세션 정상 동작. loginYn=Y면 멤버십+쿠폰 응답 보장.
async function _checkAbcmartLoggedInByApi(productId, site) {
  try {
    const cookies = await chrome.cookies.getAll({ domain: 'a-rt.com' })
    if (!cookies.length) return false
    const cookieStr = cookies.map(c => `${c.name}=${c.value}`).join('; ')
    const subdomain = site === 'GrandStage' ? 'grandstage.a-rt.com' : 'abcmart.a-rt.com'
    const resp = await fetch(`https://${subdomain}/product/info?prdtNo=${productId}`, {
      headers: {
        'Cookie': cookieStr,
        'Accept': 'application/json, text/plain, */*',
        'Origin': 'https://www.a-rt.com',
        'Referer': 'https://www.a-rt.com/',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
      },
    })
    if (!resp.ok) return null  // 판단 불가
    const data = await resp.json()
    if (!data || !data.prdtName) return null
    const loginYn = (data.loginYn || '').toUpperCase()
    return loginYn === 'Y'
  } catch (err) {
    console.log(`[${site}] 로그인 사전체크 실패 (무시): ${err.message}`)
    return null  // 판단 불가 — 진행
  }
}

// 사이트별 사전 로그인 게이트 — detail 잡 진입 시 비로그인이면 자동로그인 완료까지 대기.
// 비로그인 상태 그대로 가격 수집되는 것을 원천 차단 (사후 검증 정책의 한계 보완).
// 반환: true(로그인됨/판단불가, 진행) / false(비로그인, 잡 보류 필요)
async function _preCheckLoginGate(site, productId) {
  // LOTTEON: 쿠키 신뢰 불가 — 사전 게이트 스킵, 탭 오픈 후 DOM 텍스트로만 판단
  let loggedIn = null
  if (site === 'LOTTEON') {
    return true
  } else if (site === 'ABCmart' || site === 'GrandStage') {
    return true  // API/쿠키 신뢰 불가 — 탭 오픈 후 DOM LOGIN/LOGOUT 텍스트로만 판단
  } else {
    return true  // 미지원 사이트 — 진행
  }

  if (loggedIn === true) return true
  // null(판단 불가)도 자동로그인 시도하도록 변경 — 쿠키 자체가 없는 프로필
  // (처음 사용 / 클리어 직후)에서 비로그인으로 진행해 회원가 누락되는 사고 방지.
  // 자격증명 없으면 자동로그인이 즉시 false 리턴해서 잡 보류로 안전하게 끝남.
  if (loggedIn === null) {
    console.log(`[${site}] 사전 로그인 게이트: 판단 불가 → 자동로그인 시도 (안전 측 — 회원가 누락 차단)`)
  }

  // 비로그인 확정 또는 판단 불가 — 자동로그인 시도
  console.log(`[${site}] 사전 로그인 게이트: 비로그인 → 자동로그인 시도 (${productId})`)
  const siteKey = (typeof alExternalSiteToKey === 'function') ? alExternalSiteToKey(site) : null
  if (!siteKey || typeof ensureLoggedIn !== 'function') return false
  try {
    const ok = await ensureLoggedIn(siteKey)
    if (!ok) {
      console.log(`[${site}] 사전 로그인 게이트: 자동로그인 실패 — 잡 보류 (${productId})`)
      return false
    }
    console.log(`[${site}] 사전 로그인 게이트: 자동로그인 성공 — 잡 진행 (${productId})`)
    return true
  } catch (err) {
    console.error(`[${site}] 사전 로그인 게이트 예외:`, err?.message || err)
    return false
  }
}

// LOTTEON 로그인 검증 — 다중 쿠키 후보로 판정.
// 후보 중 하나라도 의미있는 값이면 로그인. false-positive(DOM 비로그인 false 알림) 차단.
// 반환: true=로그인 / false=비로그인 / null=판단 불가
async function _checkLotteonLoggedInByCookies() {
  // LOTTEON 로그인 식별 가능 후보들 (사이트 변경에 견고하게 다중 검증)
  const candidates = ['fo_at_yn', 'fo_mlin', 'fo_ac_tkn', 'fo_sso_tkn', 'fo_mno']
  let anyChecked = false
  for (const name of candidates) {
    try {
      const c = await chrome.cookies.get({ url: 'https://www.lotteon.com', name })
      if (!c) continue
      anyChecked = true
      const v = (c.value || '').trim()
      // 의미없는 값(빈/0/N/null) → 다음 후보로
      if (!v || v === '0' || v === 'null' || v.toUpperCase() === 'N') continue
      // 의미있는 값 = 로그인 식별 가능
      console.log(`[LOTTEON] 로그인 인정: ${name}=${v.length > 20 ? v.slice(0, 8) + '...' : v}`)
      return true
    } catch {}
  }
  // 후보 모두 검사했는데 의미있는 값 없음 → 비로그인 추정
  if (anyChecked) {
    console.log(`[LOTTEON] 모든 쿠키 후보 의미없음 — 비로그인 추정`)
    return false
  }
  // 쿠키 자체 접근 실패 → 판단 불가 (DOM 결과 유지)
  return null
}

// 사이트별 백그라운드 세션 탭 자동 보장 — 웨일/Chrome 모두 호환.
// pinned:true 의존 X (웨일에서 핀탭이 세션 컨테이너 역할 못 하는 케이스 회피).
// 일반 탭(active:false)으로 1회 생성, 이후 재사용. 사용자가 닫으면 다음 호출에서 재생성.
// 사용자가 해당 사이트에 1번 직접 로그인 필요 (IP-bound 세션, 자동로그인 보조 가능).
const SITE_HOME_URLS = {
  ABCmart: 'https://abcmart.a-rt.com/',
  GrandStage: 'https://grandstage.a-rt.com/',
  LOTTEON: 'https://www.lotteon.com/',
  SSG: 'https://www.ssg.com/',
}
const SITE_URL_PATTERNS = {
  ABCmart: '*://abcmart.a-rt.com/*',
  GrandStage: '*://grandstage.a-rt.com/*',
  LOTTEON: '*://*.lotteon.com/*',
  SSG: '*://*.ssg.com/*',
}

async function ensureSiteSessionTab(site) {
  const pattern = SITE_URL_PATTERNS[site]
  const homeUrl = SITE_HOME_URLS[site]
  if (!pattern || !homeUrl) return null
  // 이미 해당 사이트의 탭이 어디든 떠 있으면 그걸 재사용 (사용자 직접 연 탭 포함)
  let tabs = []
  try { tabs = await chrome.tabs.query({ url: pattern }) } catch { tabs = [] }
  if (tabs.length) return tabs[0].id

  console.log(`[${site}] 백그라운드 세션 탭 자동 생성: ${homeUrl}`)
  // pinned:true 안 씀 — 웨일 호환. active:false로 사용자 화면 방해 X.
  const tab = await chrome.tabs.create({ url: homeUrl, active: false })
  try { await waitForTabLoad(tab.id, 30000) } catch {}
  await wait(2000) // SPA hydration
  return tab.id
}

// 하위 호환 — 기존 호출자(fetchAbcmartBenefitPrice 등) 그대로 동작
async function ensureArtTab(site) {
  return ensureSiteSessionTab(site)
}

// ABCmart/GrandStage: 사이트별 서브도메인 탭에서 in-tab fetch로 혜택가 수집 (매번 새 탭 X)
// alwaysDscntAmt(멤버십 상시할인) + maxBenefitCoupon 모두 활용해 정확한 best_benefit_price 산출.
// Cross-subdomain CORS 차단되므로 site에 맞는 서브도메인 탭만 사용.
async function fetchAbcmartBenefitPrice(productId, site) {
  try {
    // ABCmart 상품은 abcmart.a-rt.com, GrandStage 상품은 grandstage.a-rt.com 탭에서만 호출 가능
    // 탭 없으면 자동으로 백그라운드 + 핀 탭 1개 생성 (사용자 부담 제거)
    const tabId = await ensureArtTab(site)
    if (!tabId) {
      console.log(`[${site}] in-tab fetch: 탭 생성 실패 → DOM 폴백 (${productId})`)
      return null
    }
    const [result] = await chrome.scripting.executeScript({
      target: { tabId },
      world: 'MAIN',
      func: async (prdtNo) => {
        try {
          // ABC마트 API가 SPA 표준 헤더 검증 — 일반 헤더로 호출 시 500 "잘못된 접근"
          // X-Requested-With + Accept 확장 추가로 SPA fetch 흉내
          const resp = await fetch(`/product/info?prdtNo=${prdtNo}`, {
            credentials: 'include',
            headers: {
              'Accept': 'application/json, text/plain, */*',
              'X-Requested-With': 'XMLHttpRequest',
            }
          })
          const status = resp.status
          const text = await resp.text()
          let data = null
          try { data = JSON.parse(text) } catch {}
          // 디버그: prdtName 없거나 파싱 실패 시 raw 응답 일부 반환
          if (!data || !data.prdtName) {
            return {
              __debug: true,
              status,
              textPrefix: (text || '').slice(0, 300),
              hasData: !!data,
              dataKeys: data ? Object.keys(data).slice(0, 20) : [],
            }
          }
          const pi = data.productPrice || {}
          return {
            name: (data.prdtName || '').trim(),
            displayPrice: parseInt(data.displayProductPrice || 0),
            sellAmt: parseInt(pi.sellAmt || 0),
            normalAmt: parseInt(pi.normalAmt || 0),
            alwaysDscntAmt: parseInt(data.alwaysDscntAmt || 0),
            loginYn: (data.loginYn || '').toUpperCase(),
            coupons: data.maxBenefitCoupon || data.coupon || [],
          }
        } catch (e) {
          return { error: e.message, errorStack: (e.stack || '').slice(0, 200) }
        }
      },
      args: [productId],
    })

    const apiData = result?.result
    if (!apiData || apiData.error || apiData.__debug) {
      console.log(`[${site}] in-tab fetch: 탭 내 API 실패 (${productId}) DEBUG:`, JSON.stringify(apiData)?.slice(0, 400))
      return null
    }

    const { name, displayPrice, sellAmt, normalAmt, alwaysDscntAmt, loginYn, coupons } = apiData
    const salePrice = displayPrice > 0 ? displayPrice : sellAmt
    if (!salePrice) {
      console.log(`[${site}] in-tab fetch: salePrice 0 (${productId})`)
      return null
    }

    // 비로그인 응답이면 alwaysDscntAmt 못 받음 — null 반환해서 다음 폴백으로
    if (loginYn !== 'Y') {
      console.log(`[${site}] in-tab fetch: 비로그인(loginYn=${loginYn}) → DOM 폴백 (${productId})`)
      return null
    }

    // maxBenefitCoupon은 중복적용 가능한 쿠폰 묶음 — 전부 합산해야 페이지 표시값과 일치
    let couponDiscount = 0
    for (const c of coupons) {
      couponDiscount += parseInt(c.dscntAmt || 0)
    }

    let benefitPrice = salePrice - alwaysDscntAmt - couponDiscount
    if (benefitPrice <= 0 || benefitPrice > salePrice) benefitPrice = salePrice

    console.log(`[${site}] in-tab fetch 성공: ${productId} sale=${salePrice} membership=${alwaysDscntAmt} coupon=${couponDiscount} benefit=${benefitPrice}`)
    reportLoginSuccess(site)

    return {
      success: true,
      site_product_id: productId,
      name,
      original_price: normalAmt || salePrice,
      sale_price: salePrice,
      best_benefit_price: benefitPrice,
      source_site: site,
    }
  } catch (err) {
    console.error(`[${site}] in-tab fetch 실패:`, err.message)
    return null
  }
}

// 소싱 작업 처리 — 탭 열기 → DOM 파싱 → 결과 전송
async function handleSourcingJob(job) {
  // ABCmart/GrandStage detail: DOM 파싱 1순위 (A안).
  // 검증 결과 ABCmart `/product/info` API는 멤버십 상시할인을 일관성 있게 응답하지 않고
  // 페이지 JS가 별도 처리해 표시. SW/in-tab fetch가 사용자 쿠키 컨텍스트에서
  // 부분 응답을 받아 success 반환할 경우 DOM 파싱이 차단되어 페이지 표시값과 다른
  // 가격이 박히는 문제가 있었음(예: 페이지 77,600 → 시스템 77,300, 멤버십 계산 차이).
  // → fast-path 제거. DOM 파싱이 무조건 1순위.
  // SW/in-tab fetch는 페이지에 "최대 혜택가" 표기 자체가 없는 상품(쿠폰/멤버십 0)에
  // 한해서 DOM 파싱 후 fallback으로만 사용 (904 라인 흐름).

  let tabId = null
  let cleanedUp = false
  let prevActiveTabId = null
  let openedForegroundTab = false
  let sourcingWindowId = null
  let openedSourcingWindow = false
  const _jobStartTs = Date.now()
  // hang 방어 timer — try 안 await가 영원히 대기(예: chrome.scripting.executeScript
  // 페이지 컨텍스트 죽음 감지 못함)해도 강제 cleanup. 100초는 백엔드 wrapper(120초)
  // 보다 짧게 잡아 wrapper 만료 전에 탭 정리되게 함.
  const hangTimer = setTimeout(async () => {
    if (!cleanedUp && tabId) {
      console.warn(`[${job.site}] hang 감지(100s) → 강제 탭 닫기: ${job.productId || job.keyword || ''}`)
      try { await chrome.tabs.remove(tabId) } catch {}
      cleanedUp = true
    }
  }, 100000)
  try {
    // 사전 로그인 게이트 — detail 잡 진입 시 비로그인 확정이면 자동로그인 완료까지 대기.
    // 비로그인 상태 그대로 가격 수집되는 것을 원천 차단 (사후 검증의 한계 보완).
    if (job.type === 'detail' && ['ABCmart', 'GrandStage', 'LOTTEON'].includes(job.site)) {
      const gateOk = await _preCheckLoginGate(job.site, job.productId)
      if (!gateOk) {
        clearTimeout(hangTimer)
        await postResult('sourcing/collect-result', {
          requestId: job.requestId,
          data: { success: false, login_required: true, gate_blocked: true, message: '비로그인 — 자동로그인 후 재시도 필요' }
        })
        return
      }
    }

    // 작업취소 직후 탭 생성 막기
    if (_sourcingForceStop) { clearTimeout(hangTimer); return }

    // active:false — 병렬 처리 시 여러 탭 동시 오픈 (백그라운드 탭도 JS 렌더링 됨)
    // SSG/ABCmart/GrandStage는 active 탭에서만 카드/최대 혜택가 AJAX 발동 →
    // 별도 popup 윈도우(minimized)로 분리하여 사용자 포커스 미강탈하면서 정확 추출
    // (검증 2026-05-05: ABCmart는 background 탭에서 "최대 혜택가" 텍스트 미등장 →
    //  benefitPrice=0 → 백엔드 cost 미갱신 → cost=sale 잔존 → 마진 3~6% 손실)
    const needsForegroundTab = job.type === 'detail' &&
      (job.site === 'SSG' || job.site === 'ABCmart' || job.site === 'GrandStage')
    let tab
    if (needsForegroundTab) {
      // SSG/ABCmart/GrandStage 카드/혜택가 추출용 popup
      // 검증(2026-05-10): focused:false popup은 Chromium 페이지 라이프사이클 throttling으로
      //   AJAX(카드혜택가/최대혜택가)가 발화하지 않음 → 사용자 클릭해야 로딩 시작 → 빈화면 멈춤.
      // 해결: focused:true로 활성화 보장 + 좌측 하단 작은 코너에 배치(사용자 작업 영역인 상단 미가림).
      // 포커스 복원(2026-05-17): 팝업 생성 전 "현재 OS 포커스를 가진 크롬 창"이 있으면 ID를 보관하고,
      //   팝업 생성 직후 그 창으로 포커스 되돌림. focused:true 창이 없으면(=크롬이 백그라운드 앱)
      //   복원 안 함 → 다른 앱(엑셀·메모장 등) 가림 회귀 차단.
      let _prevFocusedWinId = null
      try {
        const _allWins = await chrome.windows.getAll()
        const _f = _allWins.find(w => w.focused)
        if (_f) _prevFocusedWinId = _f.id
      } catch {}

      let _bottomY = 800   // fallback (1080 해상도 기준 - 하단 280)
      let _leftX = 10
      try {
        const displays = await chrome.system.display.getInfo()
        const primary = displays.find(d => d.isPrimary) || displays[0]
        if (primary?.workArea) {
          _bottomY = primary.workArea.top + primary.workArea.height - 170  // height=150 + 여유 20
          _leftX = primary.workArea.left + 10
        }
      } catch {}
      const win = await chrome.windows.create({
        url: job.url,
        type: 'popup',
        focused: true,
        width: 200,
        height: 150,
        top: _bottomY,
        left: _leftX,
      })
      tab = win.tabs?.[0]
      sourcingWindowId = win.id
      openedSourcingWindow = true

      // 이전 포커스 창이 크롬에 있었다면 즉시 포커스 복원 — 사용자가 크롬에서 다른 작업 중이었으면
      // 그 창이 다시 위로 올라옴. (페이지 라이프사이클상 popup 생성 시점에 focused:true였으므로
      // AJAX는 발화 시작됨 — 이후 background 상태에서도 in-flight 요청은 정상 완료.)
      if (_prevFocusedWinId && _prevFocusedWinId !== win.id) {
        try { await chrome.windows.update(_prevFocusedWinId, { focused: true }) } catch {}
      }
    } else {
      tab = await chrome.tabs.create({ url: job.url, active: false })
    }
    tabId = tab.id
    await waitForTabLoad(tabId, 30000)
    // popup minimize 제거 — 검증(2026-05-05) 결과 minimize된 popup은 Chromium
    // setTimeout/AJAX throttling 영향으로 SSG 카드혜택가/ABCmart 최대혜택가 DOM이
    // 폴링 timeout까지 안 채워짐 → cost 미반영 (DB cost=bestAmt 잔존).
    // 대신 popup이 focused:false로 생성되어 사용자 메인 윈도우 포커스는 유지됨.
    // popup 자체는 화면에 보이지만 처리 끝나면 즉시 닫히는 짧은 노출(15-25초).

    // 탭 로드 후 재확인 — 로드 중 취소된 경우 즉시 탭 닫고 종료
    if (_sourcingForceStop) {
      try { await chrome.tabs.remove(tabId) } catch {}
      cleanedUp = true
      clearTimeout(hangTimer)
      return
    }

    // GSShop: 동적 DOM 감지 (고정 8초 → 평균 2~3초)
    if (job.type === 'category-scan' && job.site === 'GSShop') {
      await waitForGSShopContent(tabId, 8000)
    } else if (job.type === 'search' && job.site === 'GSShop') {
      await waitForGSShopSearchResults(tabId, 6000)
    } else if (job.type === 'detail' && job.site === 'SSG') {
      // resultItemObj + 카드혜택가 DOM 모두 로드될 때까지 폴링 (최대 15초)
      // 검증 결과(2026-05-05): resultItemObj만 기준 시 카드혜택가가 아직 AJAX 미반영
      //   상태에서 추출되어 domCardPrice=0 → 백엔드가 bestAmt fallback → cost 마진 손실 8-10%.
      // 수정: 카드혜택가 dt 텍스트가 DOM에 등장한 직후 추가 1초 대기 후 추출.
      const _ssgPoll = async (tid) => {
        let ready = false
        let hasObj = false
        let staffOnly = false
        for (let _i = 0; _i < 30; _i++) {
          await wait(500)
          const [_chk] = await chrome.scripting.executeScript({
            target: { tabId: tid }, world: 'MAIN',
            func: () => {
              // 임직원/사업자 회원 전용 상품 — alert("임직원 및 사업자 회원만 구매 가능한 상품입니다.")
              // 페이지 본문(또는 인라인 스크립트)에 동일 문구가 박혀 있어 fail-fast 가능.
              const _src = document.documentElement ? document.documentElement.outerHTML : ''
              if (_src.indexOf('임직원 및 사업자 회원') !== -1 || _src.indexOf('임직원만 구매') !== -1) {
                return { ready: false, hasObj: false, hasCard: false, staffOnly: true }
              }
              // URL 기반 폴백 — content_script suppress 가 race 로 늦어 inline script 의
              // location.href = "member.ssg.com/login..." 또는 history.back() 리다이렉트가 먼저 실행된 케이스.
              // 882B 짜리 flagMsg 페이지는 매우 빨리 파싱되어 world:MAIN 주입이 늦을 수 있음.
              const _href = location.href || ''
              // login 페이지로 리다이렉트
              if (_href.indexOf('member.ssg.com/member/login') !== -1) {
                return { ready: false, hasObj: false, hasCard: false, staffOnly: true }
              }
              // flagMsg 페이지 자체 감지 — 마커 매칭 실패 보호 (확장앱 suppress 가 작동해 페이지 유지된 경우)
              if (document.title === 'flagMsg') {
                return { ready: false, hasObj: false, hasCard: false, staffOnly: true }
              }
              // 상품 URL 컨텍스트 손실 — itemView.ssg 가 URL 에 없으면 어디로 튄 것 (homepage/로그인 등).
              // 진단: history.back() 으로 department.ssg.com/ 홈으로 이동하는 케이스 다수 확인.
              // SSG 상품 잡은 항상 itemView.ssg URL 로 시작하므로 그게 사라지면 staffOnly 리다이렉트.
              if (_href && _href.indexOf('itemView.ssg') === -1) {
                return { ready: false, hasObj: false, hasCard: false, staffOnly: true }
              }
              const hasObj = !!(window.resultItemObj && window.resultItemObj.itemNm)
              if (!hasObj) return { ready: false, hasObj: false, hasCard: false, hasNotice: false }
              let hasCard = false
              document.querySelectorAll('dt').forEach((dt) => {
                if (dt.textContent.trim() === '카드혜택가') hasCard = true
              })
              // 상품필수정보(제조국/색상/재질/제품소재) DOM 등장 여부 — 크론잡 lazy-render 누락 방지
              let hasNotice = false
              const _noticeLabels = ['제조국', '색상', '재질', '제품소재', '제품의주소재', '상품의주소재', '주소재', '소재']
              document.querySelectorAll('dt, th').forEach((el) => {
                const t = (el.textContent || '').replace(/\s+/g, '')
                if (_noticeLabels.some((l) => t === l || t.indexOf(l) !== -1)) hasNotice = true
              })
              return { ready: hasObj && hasCard && hasNotice, hasObj: true, hasCard: hasCard, hasNotice: hasNotice }
            },
          }).catch(() => [{ result: { ready: false } }])
          const r = _chk?.result || {}
          if (r.staffOnly) { staffOnly = true; break }
          if (r.hasObj) hasObj = true
          if (r.ready) { ready = true; break }
          // 카드혜택가 없는 상품(일반가만)도 5초 후엔 통과 — resultItemObj만 있으면 추출 진행
          if (r.hasObj && _i >= 10) { break }
        }
        return { ready, hasObj, staffOnly }
      }
      let _ssgReady = false
      let { ready: _r1, hasObj: _h1, staffOnly: _s1 } = await _ssgPoll(tabId)
      _ssgReady = _r1

      // 임직원/사업자 회원 전용 상품 — 리로드 시도 무의미 (영구 차단)
      if (_s1) {
        console.log(`[SSG] 임직원 전용 상품 감지 — fail-fast: ${job.productId}`)
        // 리로드 스킵 후 그대로 진행 → 백엔드가 HTML에서 staff_only 마커 감지해 sold_out 처리
      } else if (!_h1) {
        // 진단 로그 — 빈 페이지일 때 어떤 URL/title 인지 확인 (임직원 redirect 추적용)
        try {
          const [_diag] = await chrome.scripting.executeScript({
            target: { tabId }, world: 'MAIN',
            func: () => ({
              href: location.href || '',
              title: document.title || '',
              bodyLen: (document.body?.innerHTML || '').length,
              hasMarker: (document.documentElement?.outerHTML || '').indexOf('임직원') !== -1,
            }),
          }).catch(() => [{ result: null }])
          const _d = _diag?.result || {}
          console.log(`[SSG.diag] ${job.productId} href="${_d.href}" title="${_d.title}" bodyLen=${_d.bodyLen} hasMarker=${_d.hasMarker}`)
        } catch {}
        // 빈 페이지 감지(resultItemObj 전혀 없음) — 리로드 1회 재시도
        console.log(`[SSG] 빈 페이지 감지 — 리로드 재시도: ${job.productId}`)
        try { await chrome.tabs.reload(tabId) } catch {}
        await waitForTabLoad(tabId, 10000).catch(() => {})
        const { ready: _r2 } = await _ssgPoll(tabId)
        _ssgReady = _r2
      }

      // 카드혜택가 DOM 등장 후 AJAX 가격 확정까지 짧은 추가 대기
      if (_ssgReady) await wait(1000)
    } else if (job.type === 'detail' && (job.site === 'ABCmart' || job.site === 'GrandStage')) {
      // ABCmart/GrandStage: 최대혜택가 텍스트가 AJAX로 늦게 추가됨.
      // 검증(2026-05-05): DB cost가 실제 최대혜택가보다 4-10% 비쌈 (카드혜택가 미반영).
      // 폴링으로 "최대 혜택가" 텍스트 등장까지 대기 후 추가 1초.
      let _abcReady = false
      await (async () => {
        for (let _i = 0; _i < 20; _i++) {
          await wait(500)
          const [_chk] = await chrome.scripting.executeScript({
            target: { tabId }, world: 'MAIN',
            func: () => {
              const t = (document.body && document.body.innerText) || ''
              return /최대\s*혜택가\s*[\d,]+\s*원/.test(t)
            },
          }).catch(() => [{ result: false }])
          if (_chk?.result) { _abcReady = true; break }
        }
      })()
      if (_abcReady) await wait(1000)
      else await wait(2000)
    } else {
      await wait(5000) // SPA 렌더링 대기
    }

    let result = null
    if (job.type === 'category-scan' && job.site === 'GSShop') {
      // GS샵 카테고리 스캔: 검색 결과 페이지에서 카테고리 분포 파싱
      // 동적 대기 완료 — 고정 대기 제거

      const [scanResult] = await chrome.scripting.executeScript({
        target: { tabId },
        world: 'MAIN',
        func: () => {
          const categories = []
          const seen = new Set()
          const debugInfo = { url: location.href, title: document.title }

          // innerText에서 "이름 (숫자)" 패턴을 전역 탐색
          // DOM 구조에 의존하지 않는 가장 안정적인 방법
          const bodyText = document.body?.innerText || ''
          const lines = bodyText.split('\n')
          for (const line of lines) {
            const trimmed = line.replace(/\s+/g, ' ').trim()
            const match = trimmed.match(/^(.+?)\s*\(([\d,]+)\)\s*$/)
            if (!match) continue
            const name = match[1].trim()
            const count = parseInt(match[2].replace(/,/g, ''), 10)
            // 탭 항목(전체상품, TV상품, 백화점) 및 노이즈 제외
            if (count <= 0 || seen.has(name)) continue
            if (['전체상품', 'TV상품', '백화점', '추천순'].includes(name)) continue
            if (name.includes('검색결과')) continue
            if (name.length > 30 || name.length < 2) continue
            seen.add(name)
            categories.push({ name, count, categoryCode: name, href: '' })
          }

          // href 보강: 카테고리명과 일치하는 a 태그에서 href 추출
          if (categories.length > 0) {
            const allLinks = document.querySelectorAll('a[href]')
            for (const link of allLinks) {
              const linkText = link.textContent.replace(/\s+/g, ' ').trim()
              for (const cat of categories) {
                if (linkText.includes(cat.name) && linkText.includes(`(${cat.count.toLocaleString()}`)) {
                  cat.href = link.getAttribute('href') || ''
                  try {
                    const url = new URL(cat.href, location.origin)
                    cat.categoryCode = url.searchParams.get('cls') || url.searchParams.get('sectCd') || cat.name
                  } catch {}
                  break
                }
              }
            }
          }

          // 백화점 탭 전체 상품 수
          let total = 0
          for (const line of lines) {
            const t = line.replace(/\s+/g, ' ').trim()
            const m = t.match(/백화점\s*\(([\d,]+)\)/)
            if (m) { total = parseInt(m[1].replace(/,/g, ''), 10); break }
          }
          if (total === 0 && categories.length > 0) {
            total = categories.reduce((s, c) => s + c.count, 0)
          }

          debugInfo.categoryCount = categories.length
          debugInfo.bodyTextLength = bodyText.length
          debugInfo.sampleLines = lines.filter(l => l.includes('(')).slice(0, 10).map(l => l.trim().slice(0, 50))

          return { success: categories.length > 0, categories, total, debugInfo }
        }
      })
      result = scanResult?.result || { success: false, categories: [], total: 0, debugInfo: {} }
      console.log(`[소싱] GSShop 카테고리 스캔: ${result.categories?.length || 0}개 카테고리, total=${result.total}`)
      console.log(`[소싱] GSShop 디버그:`, JSON.stringify(result.debugInfo || {}))
    } else if (job.type === 'search' && job.site === 'GSShop') {
      // GS샵: 페이지네이션 반복 수집
      const maxCount = job.maxCount || 999
      const allProducts = []
      const seenIds = new Set()
      let pageNum = 1
      const maxPages = Math.ceil(maxCount / 60) + 1

      while (allProducts.length < maxCount && pageNum <= maxPages) {
        if (pageNum > 1) {
          const eh = btoa(JSON.stringify({ pageNumber: pageNum, selected: 'opt-page' }))
          const nextUrl = new URL(job.url)
          nextUrl.searchParams.set('eh', eh)
          await chrome.tabs.update(tabId, { url: nextUrl.toString() })
          await waitForTabLoad(tabId, 20000)
          await waitForGSShopSearchResults(tabId, 5000)
        }

        const pageResult = await extractSearchResults(tabId, job.site, 999)
        const pageProducts = pageResult?.products || []

        if (pageProducts.length === 0) break

        let newCount = 0
        for (const p of pageProducts) {
          if (!seenIds.has(p.site_product_id)) {
            seenIds.add(p.site_product_id)
            allProducts.push(p)
            newCount++
          }
        }

        console.log(`[소싱] GSShop 페이지 ${pageNum}: +${newCount}건 (총 ${allProducts.length}건)`)
        if (newCount === 0) break

        pageNum++
      }

      result = { success: true, products: allProducts.slice(0, maxCount), total: allProducts.length }
    } else if (job.type === 'search') {
      result = await extractSearchResults(tabId, job.site, job.maxCount || 999)
    } else if (job.type === 'detail' && job.site === 'LOTTEON') {
      // DOM 파싱으로 "나의 혜택가" 수집
      result = await extractDetailData(tabId, job.site, job.productId)
      // 혜택가 미수집 시 3초 대기 후 재시도 (렌더링 지연 대비)
      if (!result?.best_benefit_price && !((result?.sale_price || 0) > 0 || (Array.isArray(result?.options) && result.options.length > 0))) {
        console.log(`[LOTTEON] 혜택가 미수집 — 3초 후 재시도: ${job.productId}`)
        await wait(3000)
        result = await extractDetailData(tabId, job.site, job.productId)
      }
      if (result?.best_benefit_price) {
        console.log(`[LOTTEON] DOM 혜택가: ${job.productId} → ${result.best_benefit_price}`)
      } else {
        console.log(`[LOTTEON] 혜택가 없음: ${job.productId}`)
      }
      // 로그인 검증 — DOM 신호 우선, 쿠키는 DOM이 'ambiguous'일 때만 보조.
      // 좀비 쿠키(만료된 fo_mlin 등) 때문에 DOM의 비로그인 판정이 뒤집혀
      // 비회원가로 처리되던 사고 차단. DOM에 "로그인/회원가입" 링크가 보이면 무조건 비로그인.
      // LOTTEON 로그인 상태는 오토튠 시작 시 1회 + 1시간 주기로만 체크.
      // 중간에 login_link 감지돼도 절대 차단하지 않음 (서비스워커 재시작 false-positive 가능).
      // login_link 시 백그라운드 ensureLoggedIn 트리거 후 진행.
      try {
        if (result && typeof result === 'object') {
          const sig = result._domLoginSignal
          if (sig === 'logout_link') {
            _siteLoginConfirmed.add(job.site)
          } else if (sig === 'login_link' && !_hasRecentLoginProof(job.site)) {
            const lotteonActive = _allowedSourceSites === null || _allowedSourceSites.includes('LOTTEON')
            if (lotteonActive && typeof ensureLoggedIn === 'function') {
              console.log(`[LOTTEON] login_link 감지 — 백그라운드 ensureLoggedIn 트리거 (차단 없이 진행)`)
              ensureLoggedIn('lotteon').catch(e => console.log(`[LOTTEON] bg ensureLoggedIn 오류: ${e?.message}`))
            } else if (!lotteonActive) {
              console.log(`[LOTTEON] login_link 감지 — LOTTEON 미선택 소싱처, ensureLoggedIn 스킵`)
            }
          }
          // 어떤 sig 값이든 _loginRequired = false (차단 금지)
          result._loginRequired = false
        }
      } catch (e) {
        console.log(`[LOTTEON] 로그인 신호 처리 오류 (차단 없이 진행): ${e.message}`)
        if (result && typeof result === 'object') result._loginRequired = false
      }
    } else if (job.type === 'detail' && (job.site === 'ABCmart' || job.site === 'GrandStage')) {
      // ABCmart/GrandStage: 백그라운드 탭(active=false) DOM 파싱 1순위 — 페이지에
      // 표시된 "최대 혜택가"가 사용자 등급별 멤버십+쿠폰 모두 반영된 100% 정확한 값.
      result = await extractDetailData(tabId, job.site, job.productId)
      if (!result?.best_benefit_price && !((result?.sale_price || 0) > 0 || (Array.isArray(result?.options) && result.options.length > 0))) {
        // 가격/재고 정보 전혀 없음 — 3초 후 전체 재시도
        console.log(`[${job.site}] 혜택가 미수집 — 3초 후 재시도: ${job.productId}`)
        await wait(3000)
        result = await extractDetailData(tabId, job.site, job.productId)
      } else if (!result?.best_benefit_price && (result?.sale_price || 0) > 0) {
        // 판매가는 있지만 최대혜택가 없음 — AJAX 아직 미로딩 가능성 — 3초 후 재시도
        console.log(`[${job.site}] 혜택가 미수집(판매가만 있음, AJAX 대기) — 3초 후 재시도: ${job.productId}`)
        await wait(3000)
        const _retry = await extractDetailData(tabId, job.site, job.productId)
        if (_retry?.best_benefit_price > 0) result = _retry
      } else if (result?.best_benefit_price > 0) {
        // 혜택가 수집됐어도 AJAX 쿠폰이 순차 로딩되어 값이 계속 내려갈 수 있음
        // 가격이 더 이상 내려가지 않을 때까지 2초마다 폴링 (최대 3회 = 6초)
        let _prevBp = result.best_benefit_price
        for (let _i = 0; _i < 3; _i++) {
          await wait(2000)
          const _check = await extractDetailData(tabId, job.site, job.productId)
          if ((_check?.best_benefit_price > 0) && (_check.best_benefit_price < _prevBp)) {
            console.log(`[${job.site}] 혜택가 갱신(${_i + 1}/3): ${_prevBp.toLocaleString()}→${_check.best_benefit_price.toLocaleString()}: ${job.productId}`)
            _prevBp = _check.best_benefit_price
            result = _check
          } else {
            break
          }
        }
      }
      if (result?.best_benefit_price) {
        // 혜택가 신뢰 조건: 로그인 확인 이력 있음(세션 내 이미 확인) 또는 logout_link 신호 명시
        // ambiguous 상태에서 로그인 미확인 시: 비로그인 페이지가 판매가를 혜택가로 노출해 오수집 가능
        // → 이 경우 혜택가만 무효화(0), 결과 자체는 통과(차단 금지)
        const _bp_signal = result?._domLoginSignal
        const _bp_loginOK = _siteLoginConfirmed.has(job.site) || _bp_signal === 'logout_link'
        if (_bp_loginOK) {
          _siteLoginConfirmed.add(job.site)
          console.log(`[${job.site}] DOM 혜택가: ${job.productId} → ${result.best_benefit_price}`)
        } else {
          result.best_benefit_price = 0
          console.log(`[${job.site}] 혜택가 무효화 (로그인 미확인+ambiguous): ${job.productId}`)
        }
        result._loginRequired = false
      } else {
        // DOM 혜택가 미수집 — DOM 신호 분기:
        //   - 'logout_link' → 로그인 확정 기록 + sale_price 사용
        //   - 'ambiguous'   → 로그인 여부 불명 = 비로그인 확정 아님 → 통과 (차단 금지)
        //   - 'login_link'  → 비로그인 확정 → 잡 보류 + 자동로그인 트리거
        const _signal = result?._domLoginSignal
        if (_signal === 'logout_link') {
          result._loginRequired = false
          _siteLoginConfirmed.add(job.site)
          console.log(`[${job.site}] 혜택가 없음 — logout 확인됨, 원가 갱신 없이 통과: ${job.productId}`)
        } else if (_signal === 'login_link') {
          // login_link — 아이템별 차단 금지. 백그라운드 ensureLoggedIn만 트리거
          // GrandStage도 abcmart 키로 매핑 (AUTO_LOGIN_SITES에 grandstage 없음)
          result._loginRequired = false
          const _siteAllowed = _allowedSourceSites === null || _allowedSourceSites.includes(job.site)
          if (!_siteAllowed) {
            console.log(`[${job.site}] login_link 감지 — 미선택 소싱처, ensureLoggedIn 스킵: ${job.productId}`)
          } else if (!_hasRecentLoginProof(job.site)) {
            const _loginKey = (typeof alExternalSiteToKey === 'function') ? alExternalSiteToKey(job.site) : job.site.toLowerCase()
            console.log(`[${job.site}] login_link 감지 — bg ensureLoggedIn(${_loginKey}) 트리거 (차단 없이 진행): ${job.productId}`)
            if (typeof ensureLoggedIn === 'function' && _loginKey) {
              ensureLoggedIn(_loginKey).catch(e => console.log(`[${job.site}] bg ensureLoggedIn 오류: ${e?.message}`))
            }
          } else {
            console.log(`[${job.site}] login_link 감지 but 로그인 이력 있음(오탐) — 차단 스킵: ${job.productId}`)
          }
        } else {
          // ambiguous 또는 login_link+로그인이력있음 → 헤더 미렌더/오탐 → 통과
          result._loginRequired = false
          if (_signal === 'login_link') {
            console.log(`[${job.site}] login_link 감지 but 로그인 이력 있음(오탐) — 차단 스킵: ${job.productId}`)
          } else {
            console.log(`[${job.site}] DOM ambiguous — 비로그인 미확정, 차단 스킵: ${job.productId}`)
          }
        }
      }
    } else if (job.type === 'detail' && job.site === 'SSG') {
      // SSG: reCAPTCHA / 임직원 전용 페이지 감지 후 즉시 실패 반환 (타임아웃 낭비 방지)
      const [preCheck] = await chrome.scripting.executeScript({
        target: { tabId },
        world: 'MAIN',
        func: () => {
          const body = document.body?.innerText || ''
          const src = document.documentElement ? document.documentElement.outerHTML : ''
          const href = location.href || ''
          // 임직원 redirect 폴백 — flagMsg 페이지에서 alert 후
          //   1) location.href → member.ssg.com/login 리다이렉트
          //   2) history.back() → department.ssg.com/ 홈으로 튕김
          // 어느 쪽이든 URL 에 itemView.ssg 가 사라지면 staffOnly 리다이렉트.
          const _redirectStaff = href && href.indexOf('itemView.ssg') === -1
          const _flagMsgTitle = document.title === 'flagMsg'
          return {
            captcha: body.includes('연속적인 접근') || body.includes('로봇이 아닙니다'),
            staffOnly: src.indexOf('임직원 및 사업자 회원') !== -1 ||
                       src.indexOf('임직원만 구매') !== -1 ||
                       _redirectStaff ||
                       _flagMsgTitle,
          }
        }
      })
      const _pc = preCheck?.result || {}
      if (_pc.captcha) {
        console.log(`[SSG] reCAPTCHA 차단 감지: ${job.productId}`)
        result = { success: false, blocked: true, message: 'SSG reCAPTCHA 차단' }
      } else if (_pc.staffOnly) {
        // 임직원/사업자 회원 전용 — 일반 고객 구매 불가 → 백엔드에서 sold_out 처리하도록 명시적 신호 전달
        console.log(`[SSG] 임직원 전용 상품 감지 → staffOnly 신호 전송: ${job.productId}`)
        result = { success: false, staffOnly: true, message: 'SSG 임직원/사업자 회원 전용 상품' }
      } else {
        result = await extractDetailData(tabId, job.site, job.productId)
      }
    } else if (job.type === 'detail') {
      result = await extractDetailData(tabId, job.site, job.productId)
    }

    // 전 사이트 공통 — detail 작업 결과 전송 전 로그인 상태 검증
    // 비로그인 페이지에 마케팅 가격(혜택가/판매가)이 노출되어 잘못된 가격 수집되는 것을 차단
    // 비로그인 감지 시: 결과 전송 차단 + 자동로그인 즉시 트리거
    // SSG: 카드혜택가는 비로그인에서도 동일하게 표시 → 로그인 검증 불필요, 제외
    if (job.type === 'detail' && tabId && job.site !== 'SSG' && job.site !== 'LOTTEON' && (result == null || result.success !== false)) {
      let loginNeeded = result?._loginRequired
      if (loginNeeded === undefined) {
        if (_hasRecentLoginProof(job.site)) {
          // 이 세션에서 이미 로그인 확인됨 — detectLoginStatus 스킵 (숨겨진 login link 오탐 방지)
        } else {
          // 자동로그인 성공 직후 N분간 detect 스킵 — _detectLoginStatus false-positive 방지
          const AL_GRACE_MS = 60 * 60 * 1000  // 30분
          const siteKey = (typeof alExternalSiteToKey === 'function') ? alExternalSiteToKey(job.site) : null
          const lastAt = (siteKey && globalThis._lastAutoLoginSuccessAt) ? globalThis._lastAutoLoginSuccessAt[siteKey] : 0
          if (lastAt && Date.now() - lastAt < AL_GRACE_MS) {
            // 최근 자동로그인 성공 — detect 스킵 (로그인 상태로 간주)
          } else {
            const isLoggedIn = await _detectLoginStatus(tabId, job.site)
            if (isLoggedIn === false) loginNeeded = true
          }
        }
      }
      if (loginNeeded) {
        console.log(`[${job.site}] 비로그인 확정 → 결과 전송 차단 + 자동로그인 즉시 트리거: ${job.productId}`)
        reportLoginFailure(job.site, true)
        result = { success: false, login_required: true, message: '비로그인 — 자동로그인 후 재시도 필요' }
      } else {
        reportLoginSuccess(job.site)
        // 로그인 통과 확정 — 이후 동일 소싱처는 _detectLoginStatus 스킵
        _siteLoginConfirmed.add(job.site)
      }
    }

    if (tabId && !cleanedUp) {
      try { await chrome.tabs.remove(tabId) } catch {}
      cleanedUp = true
    }

    // SSG/ABC 카드혜택가 디버그 — DB cost 검증용
    if (job.type === 'detail' && result) {
      if (job.site === 'SSG' && (result.domCardPrice !== undefined || result.resultItemObj?.bestAmt)) {
        console.log(`[SSG.dbg] ${job.productId} domCard=${result.domCardPrice||0} bestAmt=${result.resultItemObj?.bestAmt||0} sellprc=${result.resultItemObj?.sellprc||0}`)
      } else if ((job.site === 'ABCmart' || job.site === 'GrandStage') && result.best_benefit_price !== undefined) {
        console.log(`[${job.site}.dbg] ${job.productId} benefit=${result.best_benefit_price||0} sale=${result.sale_price||0} orig=${result.original_price||0}`)
      }
    }
    await postResult('sourcing/collect-result', { requestId: job.requestId, data: result || { success: false, message: '파싱 실패' } })
    const _elapsedSec = Math.round((Date.now() - _jobStartTs) / 100) / 10
    console.log(`[소싱] ${job.site} 완료: ${result?.products?.length || 0}건 (${_elapsedSec}s)`)
  } catch (err) {
    console.error(`[소싱] ${job.site} 오류:`, err)
    try {
      await postResult('sourcing/collect-result', { requestId: job.requestId, data: { success: false, message: err.message } })
    } catch {}
  } finally {
    // 정상/예외 모든 경로에서 hang timer 해제 + 탭 강제 cleanup 보장
    clearTimeout(hangTimer)
    if (openedSourcingWindow && sourcingWindowId) {
      // SSG 등 별도 윈도우는 윈도우째로 정리 (그 안의 탭도 함께 닫힘)
      try { await chrome.windows.remove(sourcingWindowId) } catch {}
      cleanedUp = true
    } else if (tabId && !cleanedUp) {
      try { await chrome.tabs.remove(tabId) } catch {}
      cleanedUp = true
    }
    if (openedForegroundTab && prevActiveTabId) {
      try { await chrome.tabs.update(prevActiveTabId, { active: true }) } catch {}
    }
  }
}

// 검색 결과 DOM 파싱 — 범용 상품 카드 추출
async function extractSearchResults(tabId, site, maxCount = 999) {
  const [result] = await chrome.scripting.executeScript({
    target: { tabId },
    world: 'MAIN',
    func: (siteName, maxItems) => {
      const products = []
      const seen = new Set()

      // 범용 상품 링크 추출 (a 태그 기반)
      const linkPatterns = {
        'ABCmart': /\/product\?prdtNo=(\d+)/,
        'GrandStage': /\/product\?prdtNo=(\d+)/,
        'REXMONDE': /\/products\/detail\/(\d+)/,
        'LOTTEON': /\/product\/productDetail[^"]*spdNo=(\d+)/,
        'GSShop': /\/(?:prd\/prd\.gs\?prdid|deal\/deal\.gs\?dealNo)=(\d+)/,
        'SSG': /\/itemView\.ssg\?itemId=(\d{10,13})/,
        'ElandMall': /\/goods\/goods\.action\?goodsNo=(\d+)/,
        'SSF': /\/goods\/([A-Z0-9]+)/,
      }
      const pattern = linkPatterns[siteName]
      if (!pattern) return { success: false, products: [], total: 0 }

      // SSG 전용: __NEXT_DATA__ JSON에서 상품 추출 (DOM a 태그보다 정확)
      if (siteName === 'SSG') {
        try {
          const nextDataEl = document.querySelector('script#__NEXT_DATA__')
          if (nextDataEl) {
            const nextData = JSON.parse(nextDataEl.textContent || '{}')
            const queries = nextData?.props?.pageProps?.dehydratedState?.queries || []
            let dataList = []
            for (const q of queries) {
              const qk = q.queryKey || []
              // ssg_sourcing.py와 동일: queryKey에 "fetchSearchItemListArea" 포함 체크
              if (!qk.includes('fetchSearchItemListArea')) continue
              const areaList = q?.state?.data?.areaList || []
              for (const area of areaList) {
                if (area.unitType === 'ITEM_UNIT_LIST') {
                  dataList = area.dataList || []
                  break
                }
              }
              if (dataList.length > 0) break
            }
            for (const it of dataList) {
              if (products.length >= maxItems) break
              const pid = String(it.itemId || '')
              if (!pid || seen.has(pid)) continue
              seen.add(pid)
              let img = it.itemImgUrl || ''
              if (img.startsWith('//')) img = 'https:' + img
              const salePrice = parseInt(String(it.finalPrice || it.sellprc || 0).replace(/[^\d]/g, '')) || 0
              const origPrice = parseInt(String(it.strikeOutPrice || it.norprc || 0).replace(/[^\d]/g, '')) || salePrice
              products.push({
                site_product_id: pid,
                name: it.itemName || '',
                brand: it.repBrandNm || it.brandName || '',
                original_price: origPrice,
                sale_price: salePrice,
                images: img ? [img] : [],
                source_site: 'SSG',
                is_sold_out: !!(it.soldOutMessage || '').trim(),
              })
            }
            if (products.length > 0) {
              return { success: true, products, total: products.length }
            }
          }
        } catch (e) {
          console.warn('[SSG] __NEXT_DATA__ parse 실패, DOM 파싱으로 폴백:', e)
        }
        // 폴백: a 태그 정규식 (아래 일반 로직)
      }

      // 모든 a 태그에서 상품 링크 찾기 (GSShop: 컨테이너 스코핑)
      let allLinks
      if (siteName === 'GSShop') {
        const container = document.querySelector('#searchPrdList .prd-list') || document.querySelector('.prd-list') || document
        allLinks = container.querySelectorAll('a[href]')
      } else {
        allLinks = document.querySelectorAll('a[href]')
      }
      for (const link of allLinks) {
        if (products.length >= maxItems) break
        const match = link.href.match(pattern)
        if (!match || seen.has(match[1])) continue
        seen.add(match[1])

        // 가장 가까운 상품 카드 컨테이너
        const card = link.closest('[class*="product"]') || link.closest('[class*="item"]') || link.closest('li') || link

        // GSShop 전용 파싱 (prd-name, price-info 등 고유 클래스 활용)
        if (siteName === 'GSShop') {
          const nameEl = card.querySelector('dt.prd-name') || card.querySelector('.prd-name')
          const priceEl = card.querySelector('dd.price-info') || card.querySelector('.price-info')
          const imgEl = card.querySelector('.prd-img img') || card.querySelector('img')

          const name = nameEl?.textContent?.trim() || ''
          let image = imgEl?.src || imgEl?.getAttribute('data-src') || imgEl?.getAttribute('data-original') || ''
          if (image.startsWith('//')) image = 'https:' + image
          // GS샵 이미지 고해상도 변환 (250px → 800px)
          if (image.includes('asset.m-gs.kr') && image.includes('/250')) {
            image = image.replace('/250', '/800')
          }

          let salePrice = 0
          let originalPrice = 0
          if (priceEl) {
            const priceText = priceEl.textContent || ''
            const nums = priceText.match(/[\d,]+/g)?.map(n => parseInt(n.replace(/,/g, ''))).filter(n => n > 100) || []
            if (nums.length >= 2) {
              salePrice = Math.min(...nums)
              originalPrice = Math.max(...nums)
            } else if (nums.length === 1) {
              salePrice = nums[0]
              originalPrice = nums[0]
            }
          }

          if (name || salePrice > 0) {
            products.push({
              site_product_id: match[1],
              name: name || `GSShop ${match[1]}`,
              brand: '',
              original_price: originalPrice,
              sale_price: salePrice,
              images: image ? [image] : [],
              source_site: siteName,
            })
          }
          continue
        }

        // 이미지
        const imgEl = card.querySelector('img')
        let image = imgEl?.src || imgEl?.currentSrc || imgEl?.getAttribute('data-src') || imgEl?.getAttribute('data-lazy') || ''
        if (image.startsWith('//')) image = 'https:' + image

        // 텍스트 노드들 (leaf 노드만)
        const texts = Array.from(card.querySelectorAll('*'))
          .filter(el => el.children.length === 0 && el.textContent.trim().length > 1)
          .map(el => el.textContent.trim())

        // 브랜드 (보통 첫번째 짧은 텍스트)
        const brand = texts.find(t => t.length < 30 && t.length > 1 && !/[0-9,]+원/.test(t)) || ''

        // 상품명 (가장 긴 텍스트)
        const name = texts.reduce((a, b) => (b.length > a.length && !/[0-9,]+원/.test(b) ? b : a), '') || ''

        // 가격 (숫자+원 패턴)
        const priceTexts = texts.filter(t => /[\d,]+원/.test(t) || /^\d[\d,]+$/.test(t))
        if (false && _domLoginSignal === 'logout_link' && !isLoggedIn) {
          isLoggedIn = true
        }
        if (false && _domLoginSignal !== 'logout_link') {
          let sawExplicitLoggedOutScript = false
          for (const script of document.querySelectorAll('script')) {
            const text = script.textContent || ''
            if (!text || (!text.includes('memInfo') && !text.includes('mbNo'))) continue
            const mbNoMatch =
              text.match(/["']mbNo["']\s*:\s*["']([^"']{2,})["']/)
              || text.match(/\bmbNo\s*:\s*["']([^"']{2,})["']/)
            if (mbNoMatch?.[1]) {
              isLoggedIn = true
              _domLoginSignal = 'logout_link'
              break
            }
            if (/["']mbNo["']\s*:\s*(null|["']{2})/.test(text) || /\bmbNo\s*:\s*(null|["']{2})/.test(text)) {
              sawExplicitLoggedOutScript = true
            }
          }
          if (_domLoginSignal !== 'logout_link') {
            const _headerText = (
              document.querySelector('header, #header, .header, [class*="header"], nav, [class*="gnb"]')?.innerText
              || (document.body?.innerText || '').substring(0, 400)
            ).replace(/\s+/g, ' ')
            if (['濡쒓렇?꾩썐', '留덉씠濡?뜲', 'MY LOTTE', '二쇰Ц諛곗넚'].some(token => _headerText.includes(token))) {
              isLoggedIn = true
              _domLoginSignal = 'logout_link'
            } else if (_domLoginSignal !== 'login_link' && (['濡쒓렇???뚯썝媛??', '濡쒓렇??', '?뚯썝媛??'].some(token => _headerText.includes(token)) || sawExplicitLoggedOutScript)) {
              _domLoginSignal = 'login_link'
            }
          }
        }

        if (false && _domLoginSignal !== 'logout_link') {
          let sawExplicitLoggedOutScript = false
          for (const script of document.querySelectorAll('script')) {
            const text = script.textContent || ''
            if (!text || (!text.includes('memInfo') && !text.includes('mbNo'))) continue
            const mbNoMatch =
              text.match(/["']mbNo["']\s*:\s*["']([^"']{2,})["']/)
              || text.match(/\bmbNo\s*:\s*["']([^"']{2,})["']/)
            if (mbNoMatch?.[1]) {
              isLoggedIn = true
              _domLoginSignal = 'logout_link'
              break
            }
            if (/["']mbNo["']\s*:\s*(null|["']{2})/.test(text) || /\bmbNo\s*:\s*(null|["']{2})/.test(text)) {
              sawExplicitLoggedOutScript = true
            }
          }
          if (_domLoginSignal !== 'logout_link') {
            const _headerText = (
              document.querySelector('header, #header, .header, [class*="header"], nav, [class*="gnb"]')?.innerText
              || (document.body?.innerText || '').substring(0, 400)
            ).replace(/\s+/g, ' ')
            if (['濡쒓렇?꾩썐', '留덉씠濡?뜲', 'MY LOTTE', '二쇰Ц諛곗넚'].some(token => _headerText.includes(token))) {
              isLoggedIn = true
              _domLoginSignal = 'logout_link'
            } else if (_domLoginSignal !== 'login_link' && (['濡쒓렇???뚯썝媛??', '濡쒓렇??', '?뚯썝媛??'].some(token => _headerText.includes(token)) || sawExplicitLoggedOutScript)) {
              _domLoginSignal = 'login_link'
            }
          }
        }

        let salePrice = 0
        let originalPrice = 0
        for (const pt of priceTexts) {
          const num = parseInt(pt.replace(/[^0-9]/g, ''))
          if (num > 0) {
            if (salePrice === 0) salePrice = num
            else if (num > salePrice) originalPrice = num
            else originalPrice = salePrice, salePrice = num
          }
        }
        if (!originalPrice) originalPrice = salePrice

        if (name || salePrice > 0) {
          products.push({
            site_product_id: match[1],
            name: name || `${siteName} ${match[1]}`,
            brand,
            original_price: originalPrice,
            sale_price: salePrice,
            images: image ? [image] : [],
            source_site: siteName,
          })
        }
      }

      return { success: true, products, total: products.length }
    },
    args: [site, maxCount]
  })

  return result?.result || { success: false, products: [], total: 0 }
}

// 상품 상세 DOM 파싱 — 범용
async function extractDetailData(tabId, site, productId) {
  // 패션플러스: 상세정보 탭 클릭하여 lazy 렌더링 트리거
  if (site === 'FashionPlus') {
    try {
      await chrome.scripting.executeScript({
        target: { tabId }, world: 'MAIN',
        func: () => {
          // 상세정보 탭 클릭
          const tabs = document.querySelectorAll('.mm_tab-link, [class*="tab"] a, [class*="tab"] button')
          for (const tab of tabs) {
            if (tab.textContent.trim().includes('상세정보') || tab.textContent.trim().includes('상세 정보')) {
              tab.click()
              break
            }
          }
        }
      })
      await wait(3000) // 상세 컨텐츠 렌더링 대기
    } catch {}
  }

  const [result] = await chrome.scripting.executeScript({
    target: { tabId },
    world: 'MAIN',
    func: (siteName, prdId) => {
      try {
      // ── SSG 전용: HTML + resultItemObj 객체 모두 반환 ──
      if (siteName === 'SSG') {
        try {
          // resultItemObj의 최상위 키 + 카테고리 관련 키만 추출 (디버그)
          const obj = window.resultItemObj || {}
          const ctgKeys = Object.keys(obj).filter(k => k.toLowerCase().includes('ctg') || k.toLowerCase().includes('cat'))
          const ctgFields = {}
          for (const k of ctgKeys) {
            try { ctgFields[k] = obj[k] } catch {}
          }
          // JSON 직렬화 가능한 필드만 추림
          const safeObj = {}
          for (const k of Object.keys(obj)) {
            try {
              const v = obj[k]
              if (v === null || typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean') {
                safeObj[k] = v
              } else if (typeof v === 'object' && !Array.isArray(v)) {
                // 중첩 객체 키만 기록 (과대 데이터 방지)
                safeObj[k] = Object.keys(v).length > 0 ? '{' + Object.keys(v).slice(0,10).join(',') + '...}' : '{}'
              } else if (Array.isArray(v)) {
                safeObj[k] = `[array len=${v.length}]`
              }
            } catch {}
          }
          // uitemObjList에서 실제 재고 추출 (AJAX 업데이트 후 값, safeObj는 배열 절삭)
          const uitemOptions = Array.isArray(obj.uitemObjList)
            ? obj.uitemObjList.map(u => ({
                name: [u.uitemOptnNm1, u.uitemOptnNm2, u.uitemOptnNm3].filter(Boolean).join('/') || u.optnDisplayNm || u.optnNm || u.uitemNm || '',
                usablInvQty: parseInt(u.usablInvQty) || 0,
                isSoldOut: (parseInt(u.usablInvQty) || 0) === 0,
              })).filter(u => u.name)
            : []
          // DOM 파싱 실재고 — JS 렌더링 후 "남은수량 N" 텍스트에서 추출
          const domSizeUl = document.querySelector('ul.selectLists[id^="select-bundleOpt-"]')
          const domOptions = []
          if (domSizeUl) {
            domSizeUl.querySelectorAll('li').forEach(function(li) {
              const rawTxt = (li.querySelector('.txt, .caption') ? li.querySelector('.txt, .caption').textContent : '').trim()
              const cleanName = rawTxt.replace(/^\[품절\]\s*/, '').replace(/\s*\(남은수량\s*\d+\)/, '').trim()
              if (!cleanName) return
              const isSoldOut = li.classList.contains('disabled')
              const mCnt = rawTxt.match(/남은수량\s*(\d+)/)
              const stock = isSoldOut ? 0 : (mCnt ? parseInt(mCnt[1], 10) : null)
              domOptions.push({ name: cleanName, stock: stock, isSoldOut: isSoldOut })
            })
          }
          // 카드혜택가 DOM 직접 추출 (html 필드는 script 태그만이라 정규식 매칭 불가)
          let domCardPrice = 0
          const dts = document.querySelectorAll('dt')
          for (const dt of dts) {
            if (dt.textContent.trim() === '카드혜택가') {
              const em = dt.parentElement?.querySelector('em.ssg_price')
                || dt.nextElementSibling?.querySelector('em.ssg_price, .ssg_price')
                || dt.closest('dl')?.querySelector('em.ssg_price')
              if (em) {
                domCardPrice = parseInt(em.textContent.replace(/[^\d]/g, ''), 10) || 0
              }
              break
            }
          }
          // 실제 판매가 DOM 직접 추출 — resultItemObj.sellprc는 정상가(할인 전)라 사용 금지
          // 1순위: cdtl_new_price notranslate (현재 최저 비카드가격)
          // 2순위: cdtl_price point (최적가 tooltip)
          // 3순위: resultItemObj.bestAmt (AJAX 값, sellprc보다 정확)
          let domSalePrice = 0
          const _spEl = document.querySelector('.cdtl_new_price.notranslate em.ssg_price')
            || document.querySelector('.cdtl_new_price.notranslate .ssg_price')
            || document.querySelector('.cdtl_price.point em.ssg_price')
            || document.querySelector('.cdtl_price.point .ssg_price')
          if (_spEl) {
            domSalePrice = parseInt(_spEl.textContent.replace(/[^\d]/g, ''), 10) || 0
          }
          // 카테고리 빵부스러기 DOM 직접 추출 — html 필드는 script 태그만이라
          // _parse_result_item_obj의 DOM regex(`카테고리 로케이션`, `신세계백화점 /`)가
          // 모두 매칭 실패해 cat2/cat3가 비고 leaf 1개만 저장되는 버그 차단.
          // 1순위: data-react-tarea="...카테고리 로케이션|대/중/소/세카테고리"
          // 2순위: .cdtl_loca_lst a, .lo_depth_xx a 등 백화점 페이지 호환 셀렉터
          const domBreadcrumb = []
          try {
            const bcSeen = new Set()
            const _pushBc = function(txt) {
              const _t = (txt || '').trim()
              if (!_t || bcSeen.has(_t)) return
              if (_t === '신세계백화점' || _t === 'SSG' || _t === 'SSG.COM') return
              bcSeen.add(_t)
              domBreadcrumb.push(_t)
            }
            // 1순위: data-react-tarea 속성 (ssg.com 일반)
            const _tareaLinks = document.querySelectorAll('[data-react-tarea*="카테고리 로케이션"]')
            if (_tareaLinks.length > 0) {
              _tareaLinks.forEach(function(a) { _pushBc(a.textContent) })
            }
            // 2순위: department.ssg.com 백화점 페이지 — cdtl_loca_lst > li > a
            if (domBreadcrumb.length === 0) {
              const _locLinks = document.querySelectorAll('.cdtl_loca_lst a, .cdtl_loca a, ul.cdtl_loca_lst li a')
              _locLinks.forEach(function(a) { _pushBc(a.textContent) })
            }
            // 3순위: 일반 breadcrumb/location 셀렉터 (호환)
            if (domBreadcrumb.length === 0) {
              const _bcEl = document.querySelector('[class*="breadcrumb"], [class*="location"], nav[aria-label="breadcrumb"]')
              if (_bcEl) {
                _bcEl.querySelectorAll('a').forEach(function(a) { _pushBc(a.textContent) })
              }
            }
          } catch (_e) { /* breadcrumb 추출 실패 시 무시 */ }
          // 추가이미지 — `img.zoom_thumb` 썸네일을 DOM에서 직접 추출.
          // 백엔드에 전달되는 html 필드는 script 태그만 포함하므로 body의 <img> URL은
          // 정규식으로 잡히지 않아 i2~iN 추가이미지가 0건이 되는 문제를 해결한다.
          // 500 → 1200 사이즈 치환 후 중복 제거.
          const domImages = []
          const _imgSeen = new Set()
          document.querySelectorAll('img.zoom_thumb').forEach(function(img) {
            let _s = (img.getAttribute('src') || img.src || '').trim()
            if (!_s) return
            if (_s.indexOf('sitem.ssgcdn.com') === -1) return
            if (_s.indexOf('/' + prdId + '_') === -1) return
            _s = _s.replace(/_i(\d+)_\d+\.jpg/i, '_i$1_1200.jpg')
            if (!_imgSeen.has(_s)) {
              _imgSeen.add(_s)
              domImages.push(_s)
            }
          })
          // 상세설명 추출 — SSG는 `<p class="cdtl_desc">` 가 안내 텍스트(22자)만 담는
          // 함정 element이므로 querySelector 방식은 못 쓴다. 백엔드 ssg_sourcing.py의
          // _parse_detail_content 와 동일한 정규식으로 페이지 outerHTML 에서 상세 영역
          // 통째로 추출. cdtl_review/cdtl_qna/cdtl_notice/footer 가 시작되기 전까지의
          // 본 상세설명+이미지가 모두 포함됨 (정상 8만~10만 chars).
          let detailHtml = ''
          try {
            const _fullHtml = document.documentElement.outerHTML || ''
            const _re = /(?:id="cdtl_desc"|id="detail_cont"|class="[^"]*cdtl_desc[^"]*")[^>]*>([\s\S]*?)(?=<div[^>]+(?:id|class)="[^"]*(?:cdtl_review|cdtl_qna|cdtl_notice|footer)[^"]*")/i
            const _m = _fullHtml.match(_re)
            if (_m && _m[1]) {
              detailHtml = _m[1]
            }
          } catch (_e) { /* detail 추출 실패 시 무시 */ }
          // 상세설명 내부 이미지도 domImages 에 병합 (zoom_thumb 못 잡은 i2~iN 보강)
          if (detailHtml) {
            try {
              const _imgRe = /<img[^>]+src="([^"]+)"/gi
              let _im
              while ((_im = _imgRe.exec(detailHtml)) !== null) {
                let _s2 = (_im[1] || '').trim()
                if (!_s2) continue
                if (_s2.indexOf('ssgcdn.com') === -1) continue
                if (!_imgSeen.has(_s2)) {
                  _imgSeen.add(_s2)
                  domImages.push(_s2)
                }
              }
            } catch (_e) { /* 무시 */ }
          }
          return {
            success: true,
            site_product_id: prdId,
            source_site: 'SSG',
            html: Array.from(document.querySelectorAll('script:not([src])')).map(function(s){return s.textContent}).join('\n'),
            resultItemObj: safeObj,  // 1차 평면 구조
            ctgFields: ctgFields,  // 카테고리 관련 전체 필드
            uitemOptions: uitemOptions,  // 옵션별 실제 재고 (AJAX 후 값)
            domOptions: domOptions,  // DOM 파싱 실재고 (JS 렌더링 후, 우선순위 최상)
            domCardPrice: domCardPrice,  // 카드혜택가 DOM 직접 추출값 → cost(원가)에 반영
            domSalePrice: domSalePrice,  // 판매가 DOM 직접 추출값 → salePrice에 반영 (sellprc 대체)
            domImages: domImages,  // 추가이미지 DOM 직접 추출 (i2~iN, 1200 고해상도) + detail 내부 이미지
            domBreadcrumb: domBreadcrumb,  // 카테고리 빵부스러기 DOM 직접 추출 (대>중>소>세, leaf-only 저장 사고 차단)
            detailHtml: detailHtml,  // 상세설명 영역 innerHTML — 백엔드 detail_html 컬럼 채움
            url: location.href,
          }
        } catch (e) {
          return { success: false, message: 'SSG HTML 추출 실패: ' + e.message, site_product_id: prdId }
        }
      }
      // (구 SSG resultItemObj 데드코드 블록 제거 — CLAUDE.md SSG 가격 로직 영구 금지 규칙 위반 패턴 잔존 위험 차단)
      // ── 패션플러스 전용 파싱 ──
      if (siteName === 'FashionPlus') {
        // JSON-LD 기본 정보
        let name = '', brand = '', origPrice = 0, salePrice = 0, sku = ''
        const jsonLd = document.querySelector('script[type="application/ld+json"]')
        if (jsonLd) {
          try {
            let d = JSON.parse(jsonLd.textContent)
            if (Array.isArray(d)) d = d.find(x => x['@type'] === 'Product') || d[0]
            if (d?.['@type'] === 'Product') {
              name = d.name || ''
              sku = d.sku || ''
              const o = d.offers || {}
              origPrice = parseInt(o.price || 0)
              salePrice = parseInt(o.sale_price || o.price || 0)
              const b = d.brand || {}
              brand = typeof b === 'object' ? (b.name || '') : String(b)
            }
          } catch {}
        }

        // 상품 이미지 — 동일 seller_id의 product_img
        const sellerId = sku.split('_')[0] || ''
        const productImgs = []
        document.querySelectorAll('img').forEach(img => {
          const src = img.src || img.currentSrc || ''
          if (src.includes('product_img') && (!sellerId || src.includes(`/${sellerId}/`)) && !productImgs.includes(src)) {
            productImgs.push(src.replace(/\?.*$/, ''))
          }
        })

        // 상세 이미지 — 상세정보 탭 내 렌더링된 이미지
        const detailImgs = []
        document.querySelectorAll('.mm_tab-item img, [class*="detail"] img, [class*="desc"] img').forEach(img => {
          const src = img.src || img.currentSrc || ''
          if (src && !src.startsWith('data:') && src.includes('http') && !detailImgs.includes(src) && !src.includes('sidebar') && !src.includes('banner') && !src.includes('favicon')) {
            detailImgs.push(src.startsWith('//') ? 'https:' + src : src)
          }
        })

        // 고시정보 (상품 정보 제공고시 테이블)
        const notice = {}
        const noticeArea = document.body.innerHTML.match(/상품\s*정보\s*제공고시([\s\S]*?)(?:상품\s*일반정보|반품|$)/)
        if (noticeArea) {
          const div = document.createElement('div')
          div.innerHTML = noticeArea[1]
          const cells = div.querySelectorAll('th, td')
          for (let i = 0; i < cells.length - 1; i += 2) {
            const key = cells[i].textContent.trim()
            const val = cells[i + 1]?.textContent.trim() || ''
            if (key && val && !key.includes('반품')) notice[key] = val
          }
        }

        // 고시정보 필드 매핑
        let material = '', color = '', manufacturer = '', origin = ''
        let careInstructions = '', qualityGuarantee = ''
        for (const [k, v] of Object.entries(notice)) {
          if (v === '상세설명참조' || v === '상세페이지참조' || !v) continue
          if (k.includes('소재') || k.includes('재질')) material = v
          else if (k === '색상') color = v
          else if (k.includes('제조자') || k.includes('제조사')) manufacturer = v
          else if (k.includes('제조국') || k.includes('원산지')) origin = v
          else if (k.includes('세탁') || k.includes('취급') || k.includes('주의')) careInstructions = v
          else if (k.includes('품질') || k.includes('보증')) qualityGuarantee = v
        }

        // 배송비 추출
        const feeMatch = document.body.innerHTML.match(/배송비\s*(\d[\d,]+)\s*원/)
        const shippingFee = feeMatch ? parseInt(feeMatch[1].replace(/,/g, '')) : 3000

        // 옵션 (사이즈/색상)
        const options = []
        document.querySelectorAll('select option, [class*="option"] li, [class*="size"] button').forEach(el => {
          const t = el.textContent.trim()
          if (t && t !== '선택' && t !== '옵션을 선택하세요' && t.length < 50) {
            options.push({ name: t, stock: 999, isSoldOut: false })
          }
        })

        // 상세 HTML 조합
        const allDetailImgs = [...new Set([...productImgs, ...detailImgs])]
        const detailHtml = allDetailImgs.map(src =>
          `<div style="text-align:center;"><img src="${src}" style="max-width:860px;width:100%;" /></div>`
        ).join('\n')

        return {
          success: true,
          site_product_id: prdId,
          name, brand, original_price: origPrice, sale_price: salePrice,
          images: productImgs.slice(0, 9),
          detail_images: allDetailImgs,
          detail_html: detailHtml,
          source_site: siteName,
          category: '', category1: '', category2: '', category3: '',
          options,
          material, color, manufacturer, origin,
          care_instructions: careInstructions,
          quality_guarantee: qualityGuarantee,
          shipping_fee: shippingFee,
        }
      }

      // ── 롯데ON 전용 파싱 (렌더된 DOM에서 프로모션가/혜택가 추출) ──
      if (siteName === 'LOTTEON') {
        // 로그인 상태 감지 — 헤더의 "로그인" 링크/버튼 존재 여부로 판단
        // 비로그인이면 자동로그인 트리거 신호로 사용 (LOTTEON 페이지에 "나의 혜택가"가 마케팅 텍스트로 노출되어
        // 비로그인 상태에서도 가격이 추출되는 false-positive를 명시적으로 차단)
        // 로그인 판단 — #memInfo hidden input의 mbNo 존재 여부 (가장 확실한 신호)
        let isLoggedIn = false
        let memInfoFound = false
        let _domLoginSignal = 'ambiguous'
        try {
          const memInfoEl = document.querySelector('#memInfo')
          if (memInfoEl) {
            memInfoFound = true
            const memInfo = JSON.parse(memInfoEl.value || '{}')
            if (memInfo?.mbNo) {
              isLoggedIn = true
              _domLoginSignal = 'logout_link'
            } else {
              _domLoginSignal = 'login_link'
            }
          }
        } catch {}
        // #memInfo 미발견(항상) → bodyText "로그인/회원가입" 텍스트로 보조 감지
        // 비로그인: 헤더에 "로그인/회원가입" 노출 확인됨 (CDP 테스트 2026-05-02)
        // 로그인: "로그인/회원가입" 사라짐 → 없으면 로그인 상태로 간주
        if (_domLoginSignal === 'ambiguous') {
          // 헤더 영역만 검사 — 상품 설명에 "로그인/회원가입" 텍스트가 있으면 false-positive 발생
          // 비로그인 테스트: bodyText 앞 60자 이내에 노출 확인 → 300자로 여유 있게 자름
          const _headerText = document.querySelector('header, #header')?.innerText
            || (document.body?.innerText || '').substring(0, 300)
          if (_headerText.includes('로그인/회원가입')) {
            _domLoginSignal = 'login_link'
            isLoggedIn = false
          } else {
            _domLoginSignal = 'logout_link'
            isLoggedIn = true
          }
        }

        if (_domLoginSignal !== 'logout_link') {
          let sawExplicitLoggedOutScript = false
          for (const script of document.querySelectorAll('script')) {
            const text = script.textContent || ''
            if (!text || (!text.includes('memInfo') && !text.includes('mbNo'))) continue
            const mbNoMatch =
              text.match(/["']mbNo["']\s*:\s*["']([^"']{2,})["']/)
              || text.match(/\bmbNo\s*:\s*["']([^"']{2,})["']/)
            if (mbNoMatch?.[1]) {
              isLoggedIn = true
              _domLoginSignal = 'logout_link'
              break
            }
            if (/["']mbNo["']\s*:\s*(null|["']{2})/.test(text) || /\bmbNo\s*:\s*(null|["']{2})/.test(text)) {
              sawExplicitLoggedOutScript = true
            }
          }
          if (_domLoginSignal !== 'logout_link') {
            const _headerText = (
              document.querySelector('header, #header, .header, [class*="header"], nav, [class*="gnb"]')?.innerText
              || (document.body?.innerText || '').substring(0, 400)
            ).replace(/\s+/g, ' ')
            if (['濡쒓렇?꾩썐', '留덉씠濡?뜲', 'MY LOTTE', '二쇰Ц諛곗넚'].some(token => _headerText.includes(token))) {
              isLoggedIn = true
              _domLoginSignal = 'logout_link'
            } else if (_domLoginSignal !== 'login_link' && (['濡쒓렇???뚯썝媛??', '濡쒓렇??', '?뚯썝媛??'].some(token => _headerText.includes(token)) || sawExplicitLoggedOutScript)) {
              _domLoginSignal = 'login_link'
            }
          }
        }
        let salePrice = 0
        let originalPrice = 0
        let benefitPrice = 0
        let name = ''
        let brand = ''

        // 상품명
        const nameEl = document.querySelector('h3[class*="product"], [class*="tit_product"], [class*="product-name"], [class*="pdp-title"]')
        name = nameEl?.textContent?.trim() || document.querySelector('meta[property="og:title"]')?.content || ''

        // 브랜드
        const brandEl = document.querySelector('[class*="brand"] a, [class*="brand-name"]')
        brand = brandEl?.textContent?.trim() || ''

        // 가격: 본문에서 "N원" 패턴 추출
        const bodyText = document.body?.innerText || ''

        // "나의 혜택가" 추출 — "N원 나의 혜택가" 패턴
        const benefitMatch = bodyText.match(/([\d,]+)\s*원\s*나의\s*혜택가/)
        if (benefitMatch) {
          benefitPrice = parseInt(benefitMatch[1].replace(/,/g, ''), 10)
        }

        // 프로모션 판매가 — "N% N원" 패턴 (할인율 + 가격)
        const promoMatch = bodyText.match(/(\d+)%\s+([\d,]+)\s*원/)
        if (promoMatch) {
          salePrice = parseInt(promoMatch[2].replace(/,/g, ''), 10)
        }

        // 정가 — 취소선 가격 (del, s 태그 또는 할인가 옆 큰 숫자)
        const delEl = document.querySelector('del, s, [class*="origin"] [class*="price"], [class*="before"] [class*="price"]')
        if (delEl) {
          const delNum = delEl.textContent.replace(/[^0-9]/g, '')
          if (delNum) originalPrice = parseInt(delNum, 10)
        }
        // 정가 폴백: 본문에서 취소선 가격 옆 숫자
        if (!originalPrice && salePrice > 0) {
          const origMatch = bodyText.match(new RegExp((salePrice).toLocaleString() + '\\s*원\\s+([\\.\\d,]+)'))
          if (origMatch) originalPrice = parseInt(origMatch[1].replace(/[^0-9]/g, ''), 10)
        }
        if (!originalPrice) originalPrice = salePrice

        // 옵션 (사이즈별 재고) — 설계문서 §3.5 정밀 셀렉터 (2026-04-23 Phase 1 실측)
        // 기존 [class*="option"] 셀렉터는 느슨해 가짜 매칭이 많아 교체.
        // 구조: ul.selectLists[id^="select-bundleOpt-"] > li
        //   └── .caption ("075" 또는 "[품절] 075")
        //   └── .stock   ("6개 남음" | "품절" | "" — 빈 값은 10개+ 추정, 백엔드 기존값 유지)
        //   └── li.disabled 클래스 → 품절 플래그 (가장 확실)
        const options = []
        const sizeUl = document.querySelector('ul.selectLists[id^="select-bundleOpt-"]')
        if (sizeUl) {
          sizeUl.querySelectorAll('li').forEach(li => {
            const rawCaption = (li.querySelector('.txt, .caption')?.textContent || '').trim()
            const cleanName = rawCaption.replace(/^\[품절\]\s*/, '').replace(/\s*\(남은수량\s*\d+\)/, '').trim()
            if (!cleanName) return
            const stockText = (li.querySelector('.stock')?.textContent || '').trim()
            const isSoldOut = li.classList.contains('disabled') || stockText === '품절'
            const mStock = stockText.match(/(\d+)\s*개/)
            const mCaption = rawCaption.match(/남은수량\s*(\d+)/)
            // stock: 0=품절, 정수 N=실재고("N개 남음" 또는 caption의 "남은수량 N"), null=UI에 숫자 미노출(충분 재고)
            const stock = isSoldOut ? 0 : (mStock ? parseInt(mStock[1], 10) : (mCaption ? parseInt(mCaption[1], 10) : null))
            options.push({ name: cleanName, stock, isSoldOut, raw: stockText })
          })
        }

        // 이미지
        const images = []
        document.querySelectorAll('[class*="thumb"] img, [class*="swiper"] img, [class*="slide"] img').forEach(img => {
          let src = img.src || img.currentSrc || img.getAttribute('data-src') || ''
          if (src.startsWith('//')) src = 'https:' + src
          if (src && src.includes('http') && !src.includes('data:') && !images.includes(src)) {
            images.push(src)
          }
        })

        // 판매자 지점 (§3.5) — 단일 지점 고정 표기 (고객이 볼 수 있는 재고의 소속).
        // 일부 상품에선 null — 필수 필드 아님, 로그/디버그 전용.
        const sellerEl = document.querySelector('ul.sellerList > li.currentProduct .sellerGrade strong')
        const seller = sellerEl?.textContent?.trim() || null

        // 매장픽업 전용 상품 감지 — 배송 불가 상품은 수집/갱신 차단
        // 사용자 검증 LE1216449916: "매장픽업 전용 롯데백화점" 텍스트 + 배송비 0이지만
        // DB는 배송비 3,000원 가산되어 잘못된 cost. 또한 배송 불가라 수집 자체 부적합.
        // 상품 정보 영역만 검사 — bodyText 전체 스캔 시 하단 추천상품 카드 라벨에서 오탐 가능
        const _productInfoEl = document.querySelector('[class*="pdp"], [class*="prdInfo"], [class*="goods-info"], [class*="product-info"]')
        const _pickupArea = _productInfoEl?.innerText || bodyText.slice(0, 6000)
        const _storePickupOnly = /매장\s*픽업\s*전용/.test(_pickupArea)

        // 데이터 유무와 무관하게 항상 _domLoginSignal 포함 반환
        // — 데이터 없어도 undefined 반환 시 공통 블록 _detectLoginStatus 오탐 차단 방지
        return {
          success: name || salePrice > 0 || options.length > 0,
          site_product_id: prdId,
          name, brand,
          original_price: originalPrice,
          sale_price: salePrice || benefitPrice,
          best_benefit_price: benefitPrice,
          images: images.slice(0, 9),
          source_site: siteName,
          category: '', category1: '', category2: '', category3: '',
          options,
          seller,
          pageTitle: document.title,
          store_pickup_only: _storePickupOnly,
          _loginRequired: _domLoginSignal === 'login_link',
          _domLoginSignal,
        }
      }

      // ── ABCmart/GrandStage 전용 파싱 (최대혜택가 추출) ──
      if (siteName === 'ABCmart' || siteName === 'GrandStage') {
        // 로그인 상태 감지 — 헤더에서 로그인/로그아웃 링크 확인
        let _abcScriptLogin = null
        for (const script of document.querySelectorAll('script')) {
          const text = script.textContent || ''
          if (!text.includes('abc.userDetails')) continue
          const loginMatch = text.match(/loginYn\s*:\s*'(\w+)'/)
          if (loginMatch) {
            _abcScriptLogin = loginMatch[1] === 'true'
            break
          }
        }
        const _abcHeader = document.querySelector('header, #header, .header, nav, #gnb, .gnb, [class*="gnb"], [class*="header"]') || document.body
        let _abcHasLogin = false
        let _abcHasLogout = false
        for (const el of _abcHeader.querySelectorAll('a[href], button')) {
          const href = (el.getAttribute('href') || '').toLowerCase()
          const txt = (el.textContent || '').trim()
          if (href.includes('logout') || txt === '로그아웃' || txt === 'Logout' || txt === 'LOGOUT') { _abcHasLogout = true; continue }
          if (txt === '로그인' || (href.includes('/login') && !href.includes('logout'))) { _abcHasLogin = true }
        }
        // 셀렉터로 못 찾으면 bodyText에서 "LOGIN"/"LOGOUT" 단어 직접 검사 (ABCmart 영문 UI)
        if (!_abcHasLogin && !_abcHasLogout) {
          const _slice = (document.body?.innerText || '').slice(0, 3000).toUpperCase()
          if (_slice.includes('LOGOUT')) _abcHasLogout = true
          else if (_slice.includes('LOGIN')) _abcHasLogin = true
        }
        // LOGOUT 버튼 존재 = 로그인 확정 (login 링크 공존 무관)
        // 상품 페이지에 '/login' URL 포함 링크(포인트 적립, 혜택 안내 등)가 있어도 logout이 우선
        const isLoggedIn = _abcScriptLogin === true || _abcHasLogout
        const _domLoginSignal =
          _abcScriptLogin === true || _abcHasLogout ? 'logout_link'
            : _abcScriptLogin === false || _abcHasLogin ? 'login_link'
              : 'ambiguous'

        const bodyText = document.body?.innerText || ''
        let benefitPrice = 0

        // "최대 혜택가 70,400원" 또는 "최대혜택가 70,400원" 패턴
        const benefitMatch = bodyText.match(/최대\s*혜택가\s*([\d,]+)\s*원/)
        if (benefitMatch) {
          benefitPrice = parseInt(benefitMatch[1].replace(/,/g, ''), 10)
        }

        // 범용 파싱 결과에 best_benefit_price만 보강하여 반환
        if (benefitPrice > 0) {
          // 기본 정보도 함께 추출
          let name = ''
          let salePrice = 0
          let originalPrice = 0
          const nameEl = document.querySelector('h2[class*="name"], [class*="prd-name"], [class*="product_name"]')
          name = nameEl?.textContent?.trim() || document.querySelector('meta[property="og:title"]')?.content || ''

          // 정상가/판매가: "최대 혜택가" 직전 800자만 탐색
          // (전체 이전 영역은 배너/관련상품 가격이 섞여 오매칭됨 — lookback 방식으로 교체)
          // ABCmart 표기 규칙:
          //   - 정상가만: "79,000원" (단독, [%] 미포함)
          //   - 정상가+할인된 판매가: "69,000 55,000원 [20%]" (strikethrough + 할인 후 + 할인율)
          //   - 혜택가는 "최대 혜택가 N원 [P%]" 형태로 [%] 포함 → salePrice/originalPrice 후보에서 제외
          const benefitIdx = bodyText.search(/최대\s*혜택가/)
          const _lookbackStart = benefitIdx > 0 ? Math.max(0, benefitIdx - 800) : 0
          const beforeBenefit = bodyText.slice(_lookbackStart, benefitIdx > 0 ? benefitIdx : undefined)

          // 패턴 A: "정상가 할인가 원 [%]" — 정상가+할인된 판매가
          const discountedMatch = beforeBenefit.match(/(\d{1,3}(?:,\d{3})+)\s+(\d{1,3}(?:,\d{3})+)\s*원\s*\[\d+%\]/)
          if (discountedMatch) {
            originalPrice = parseInt(discountedMatch[1].replace(/,/g, ''), 10)
            salePrice = parseInt(discountedMatch[2].replace(/,/g, ''), 10)
          } else {
            // 패턴 B: 단독 "N,NNN원" 중 가장 큰 값 = 정상가 (할인 없음 → 정상가=판매가)
            // [%] 표기가 따라오는 가격은 제외 (혜택가/할인가)
            const standaloneMatches = [...beforeBenefit.matchAll(/(\d{1,3}(?:,\d{3})+)\s*원(?!\s*\[)/g)]
              .map(m => parseInt(m[1].replace(/,/g, ''), 10))
              .filter(n => n >= 1000)  // 배송비 0원, 적립 100P 등 노이즈 제외
            if (standaloneMatches.length > 0) {
              originalPrice = Math.max(...standaloneMatches)
              salePrice = originalPrice
            }
          }

          return {
            success: true,
            site_product_id: prdId,
            name,
            original_price: originalPrice || salePrice,
            sale_price: salePrice || benefitPrice,
            best_benefit_price: benefitPrice,
            images: [],
            source_site: siteName,
            _loginRequired: _domLoginSignal === 'login_link',
            _domLoginSignal,
          }
        }
      }

      // ── 범용 파싱 (기존 코드) ──
      // JSON-LD 우선 추출
      const jsonLdScripts = document.querySelectorAll('script[type="application/ld+json"]')
      for (const script of jsonLdScripts) {
        try {
          let data = JSON.parse(script.textContent)
          if (Array.isArray(data)) data = data.find(d => d['@type'] === 'Product') || data[0]
          if (data && data['@type'] === 'Product') {
            const offers = data.offers || {}
            const price = Array.isArray(offers) ? parseInt(offers[0]?.price || 0) : parseInt(offers.price || 0)
            const brandObj = data.brand || {}
            const img = Array.isArray(data.image) ? data.image[0] : (data.image || '')
            return {
              success: true,
              site_product_id: prdId,
              name: data.name || '',
              original_price: price,
              sale_price: price,
              images: img ? [img] : [],
              brand: typeof brandObj === 'object' ? (brandObj.name || '') : String(brandObj),
              source_site: siteName,
              category: '', category1: '', category2: '', category3: '',
              options: [], detail_html: '',
            }
          }
        } catch {}
      }

      // og:태그 fallback
      const ogTitle = document.querySelector('meta[property="og:title"]')?.content || ''
      const ogImage = document.querySelector('meta[property="og:image"]')?.content || ''
      const ogPrice = document.querySelector('meta[property="product:price:amount"]')?.content || ''

      // DOM 텍스트 기반 추출
      const allTexts = Array.from(document.querySelectorAll('*'))
        .filter(el => el.children.length === 0)
        .map(el => el.textContent.trim())
        .filter(t => t.length > 1)

      const priceTexts = allTexts.filter(t => /^\d[\d,]+원?$/.test(t))
      let salePrice = ogPrice ? parseInt(ogPrice) : 0
      let originalPrice = 0
      for (const pt of priceTexts) {
        const num = parseInt(pt.replace(/[^0-9]/g, ''))
        if (num > 0) {
          if (!salePrice) salePrice = num
          else if (num > salePrice) originalPrice = num
        }
      }

      // 이미지 (상품 관련)
      const images = []
      document.querySelectorAll('img').forEach(img => {
        const src = img.src || img.currentSrc || img.getAttribute('data-src') || ''
        if (src && (src.includes('product') || src.includes('goods') || src.includes('prd')) && !images.includes(src)) {
          images.push(src.startsWith('//') ? 'https:' + src : src)
        }
      })

      // 옵션 (사이즈/색상 select 또는 버튼)
      const options = []
      document.querySelectorAll('select option, [class*="option"] li, [class*="size"] button, [class*="size"] a').forEach(el => {
        const text = el.textContent.trim()
        if (text && text !== '선택' && text.length < 30) {
          options.push({ name: text, stock: 999 })
        }
      })

      // 카테고리 (breadcrumb)
      const breadcrumb = document.querySelector('[class*="breadcrumb"], [class*="location"], nav[aria-label="breadcrumb"]')
      let cats = []
      if (breadcrumb) {
        cats = Array.from(breadcrumb.querySelectorAll('a, span, li'))
          .map(el => el.textContent.trim())
          .filter(t => t.length > 1 && t !== '>' && t !== 'Home' && t !== '홈')
      }

      return {
        success: true,
        site_product_id: prdId,
        name: ogTitle || document.title || `${siteName} ${prdId}`,
        original_price: originalPrice || salePrice,
        sale_price: salePrice,
        images: images.length > 0 ? images.slice(0, 10) : (ogImage ? [ogImage] : []),
        brand: '',
        source_site: siteName,
        category: cats.join(' > '),
        category1: cats[0] || '',
        category2: cats[1] || '',
        category3: cats[2] || '',
        options,
        detail_html: '',
      }
      } catch (e) {
        return { success: false, message: `스크립트 에러: ${e.message}`, url: location.href }
      }
    },
    args: [site, productId]
  })

  return result?.result || { success: false, message: 'DOM 파싱 실패' }
}

// ============================================================
// 자가 업데이트 — 백엔드 latest 버전과 비교해 구버전이면 chrome.runtime.reload().
// 공유폴더 동기화로 디스크 파일이 최신이면 reload 시 최신본을 다시 읽는다.
// (롯데ON 데몬의 self-update 와 동일 패턴 — sourcing.py)
// ============================================================
const _SELF_UPDATE_ALARM = 'sambaSelfUpdate'
const _SELF_UPDATE_INTERVAL_MIN = 360 // 6시간

function _cmpSemver(a, b) {
  const pa = String(a).split('.').map((n) => parseInt(n, 10) || 0)
  const pb = String(b).split('.').map((n) => parseInt(n, 10) || 0)
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const x = pa[i] || 0
    const y = pb[i] || 0
    if (x !== y) return x - y
  }
  return 0
}

async function _checkSelfUpdate() {
  try {
    // 오토튠/잡 실행 중인 PC는 reload 보류 — 작업 끊김 방지. 다음 주기에 재시도.
    if (_localAutotuneJoined) return

    const { proxyUrl } = await chrome.storage.local.get('proxyUrl')
    if (!proxyUrl) return

    const res = await fetch(
      `${proxyUrl}/api/v1/samba/proxy/autotune-daemon/extension-version`,
      { method: 'GET' },
    )
    if (!res.ok) return
    const data = await res.json().catch(() => ({}))
    const latest = data && data.version
    const current = chrome.runtime.getManifest().version
    if (!latest || _cmpSemver(current, latest) >= 0) return // 이미 최신

    // 무한루프 가드: 디스크 파일이 아직 구버전(공유폴더 미동기화)이면 reload 해도
    // 같은 버전 → 반복. 같은 latest 로 6시간 내 시도했으면 skip.
    const stored = await chrome.storage.local.get('_selfUpdateTried')
    const tried = stored._selfUpdateTried || {}
    const now = Date.now()
    if (tried.version === latest && now - (tried.at || 0) < _SELF_UPDATE_INTERVAL_MIN * 60 * 1000) {
      return
    }
    // reload 가 worker 를 죽이므로 가드 기록 flush 를 await 로 보장한 뒤 reload.
    await chrome.storage.local.set({ _selfUpdateTried: { version: latest, at: now } })
    console.log(`[SAMBA] 자가 업데이트 ${current} → ${latest} — reload`)
    chrome.runtime.reload()
  } catch (e) {
    console.warn('[SAMBA] 자가 업데이트 체크 실패:', e && e.message)
  }
}

// 부팅 1분 후 첫 체크 + 6시간 주기 (동일 이름이면 덮어써져 중복 안 됨)
chrome.alarms.create(_SELF_UPDATE_ALARM, {
  delayInMinutes: 1,
  periodInMinutes: _SELF_UPDATE_INTERVAL_MIN,
})
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm && alarm.name === _SELF_UPDATE_ALARM) _checkSelfUpdate()
})
