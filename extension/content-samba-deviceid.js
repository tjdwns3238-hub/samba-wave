// 삼바 프론트엔드 페이지에 확장앱 deviceId를 전달한다.
// 프론트엔드(layout.tsx)의 attachDeviceIdListener가 window.message 이벤트로 수신하여
// sessionStorage에 저장하고, 오토튠 시작 시 백엔드로 전송한다.
// 이 deviceId를 가진 확장앱만 collect-queue에서 오토튠 작업을 받아가므로,
// 동일 계정으로 접속한 다른 PC의 브라우저에서는 탭이 열리지 않는다.
//
// 추가: 오토튠 화면의 소싱처 체크박스 = "이 PC가 처리할 사이트" 분담 표시.
// 프론트엔드가 toggleSource 시 window.postMessage로 변경된 사이트 목록을 보내면,
// content script가 chrome.storage.local.allowedSites에 저장한다.
// 익스텐션이 collect-queue 폴링 시 그 값을 X-Allowed-Sites 헤더로 전송하여
// 그 PC가 체크한 사이트의 작업만 받게 된다 (PC별 자동 분담).
(function () {
  function getPreferredProxyUrl() {
    const { protocol, hostname } = window.location
    if (protocol === 'http:' && hostname === 'localhost') {
      return 'http://localhost:28080'
    }
    // 프로덕션 페이지 열리면 백엔드 URL을 production으로 자동 복원.
    // 옛날에 로컬 페이지 한 번 열어서 storage가 localhost로 굳어진 케이스를 자동 복구.
    // vercel.app(프리뷰 포함) / samba-wave.co.kr 둘 다 production 백엔드로 매핑.
    if (hostname.endsWith('.vercel.app') || hostname === 'samba-wave.co.kr' || hostname.endsWith('.samba-wave.co.kr')) {
      return 'https://api.samba-wave.co.kr'
    }
    return ''
  }

  async function syncProxyUrlForPage() {
    const preferred = getPreferredProxyUrl()
    if (!preferred) return
    try {
      const data = await chrome.storage.local.get(['proxyUrl'])
      if (data.proxyUrl !== preferred) {
        await chrome.storage.local.set({ proxyUrl: preferred })
      }
    } catch {}
  }

  function sendDeviceId(deviceId) {
    if (!deviceId) return
    try {
      window.postMessage(
        { source: 'samba-extension', type: 'DEVICE_ID', deviceId },
        window.location.origin,
      )
    } catch {
      // cross-origin 등의 이유로 실패하면 조용히 무시
    }
  }

  function sendAllowedSites(sites) {
    try {
      // null = 미설정(전체처리), [] = 전체해제, [...] = 부분선택 — 구분 유지
      window.postMessage(
        { source: 'samba-extension', type: 'ALLOWED_SITES', sites: sites },
        window.location.origin,
      )
    } catch {}
  }

  // content_script는 chrome.storage.local 접근 가능
  syncProxyUrlForPage()
  chrome.storage.local.get(['deviceId', 'allowedSites'], (data) => {
    if (data && data.deviceId) {
      sendDeviceId(data.deviceId)
      // 페이지가 이후에 mount되는 React 컴포넌트에서도 받을 수 있도록 재전송 스케줄
      setTimeout(() => sendDeviceId(data.deviceId), 500)
      setTimeout(() => sendDeviceId(data.deviceId), 2000)
    } else {
      // 최초 설치 직후 background가 아직 deviceId를 만들지 않은 경우 대비
      // 서비스워커에 요청
      chrome.runtime.sendMessage({ type: 'GET_DEVICE_ID' }, (resp) => {
        if (resp && resp.deviceId) sendDeviceId(resp.deviceId)
      })
    }
    // 페이지 mount 후 현재 저장된 allowedSites도 전달 → 화면 체크박스 초기화에 사용
    // null(미설정)과 [](전체해제)를 구분해서 전달
    const sites = data && Array.isArray(data.allowedSites) ? data.allowedSites : null
    sendAllowedSites(sites)
    setTimeout(() => sendAllowedSites(sites), 800)
    setTimeout(() => sendAllowedSites(sites), 2500)
  })

  // 화면(프론트엔드)이 체크박스 변경을 알릴 때 chrome.storage 동기화
  // 메시지 형식: { source: 'samba-page', type: 'SET_ALLOWED_SITES', sites: [...] }
  window.addEventListener('message', (event) => {
    if (event.source !== window) return
    const msg = event.data
    if (!msg || typeof msg !== 'object') return
    if (msg.source !== 'samba-page') return
    if (msg.type === 'SET_ALLOWED_SITES') {
      // null=전체처리(헤더 미부착), []=전체해제, [...]=부분선택 — 구분 그대로 저장
      const sites = msg.sites === null ? null : Array.isArray(msg.sites) ? msg.sites : null
      chrome.storage.local.set({ allowedSites: sites })
      return
    }
    // SPA 라우팅으로 페이지에 재진입한 경우 페이지가 현재 allowedSites를 다시 요청
    // (content_script는 페이지 최초 로드 때만 실행되므로 재진입 시 자동 전달이 안 됨)
    if (msg.type === 'GET_ALLOWED_SITES') {
      chrome.storage.local.get(['allowedSites'], (data) => {
        const sites = data && Array.isArray(data.allowedSites) ? data.allowedSites : null
        sendAllowedSites(sites)
      })
      return
    }
    // 오토튠 시작/중지 시 이 PC의 폴링 참여 여부 설정
    // joined:true = 이 PC의 시작 버튼을 눌렀음 → 이 PC만 폴링 합류
    // joined:false = 중지 → 이 PC 폴링 탈퇴
    if (msg.type === 'AUTOTUNE_SET_JOIN') {
      chrome.runtime.sendMessage({ type: 'AUTOTUNE_JOIN_LOCAL', joined: !!msg.joined, sourceSites: msg.sourceSites ?? null })
    }
    // 확장앱 연결 페이지(/samba/extension-link)에서 발급된 키 저장
    if (msg.type === 'SAMBA_SET_API_KEY' && msg.apiKey) {
      chrome.storage.local.set({ apiKey: msg.apiKey })
    }
  })
})()
