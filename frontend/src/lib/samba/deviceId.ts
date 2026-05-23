// 현재 브라우저의 확장앱 deviceId를 받아 보관하는 유틸.
// 확장앱이 content_script/background에서 window.postMessage로 deviceId를 전달하면
// layout.tsx에서 listener로 받아 sessionStorage + 모듈 변수에 저장한다.
//
// 오토튠 시작 시 getDeviceId()를 호출해 백엔드로 전송하면, 이 deviceId와 일치하는
// 확장앱만 collect-queue에서 작업을 받아가므로 "다른 PC에서 탭이 열리는" 현상이 방지된다.

const STORAGE_KEY = 'samba.extensionDeviceId'
let cached: string = ''

export const getDeviceId = (): string => {
  if (cached) return cached
  if (typeof window === 'undefined') return ''
  try {
    const v = window.sessionStorage.getItem(STORAGE_KEY) || ''
    if (v) cached = v
    return cached
  } catch {
    return ''
  }
}

const setDeviceId = (v: string): void => {
  const cleaned = (v || '').trim()
  if (!cleaned) return
  cached = cleaned
  if (typeof window === 'undefined') return
  try {
    window.sessionStorage.setItem(STORAGE_KEY, cleaned)
  } catch {
    // storage 접근 실패는 무시
  }
}

// window.postMessage({ source: 'samba-extension', type: 'DEVICE_ID', deviceId: '...' })
// 형태로 확장앱이 전달한 deviceId를 캐치한다.
export const attachDeviceIdListener = (): (() => void) => {
  if (typeof window === 'undefined') return () => {}
  const handler = (e: MessageEvent) => {
    if (!e.data || typeof e.data !== 'object') return
    const d = e.data as { source?: string; type?: string; deviceId?: string }
    if (d.source !== 'samba-extension') return
    if (d.type !== 'DEVICE_ID') return
    if (d.deviceId) setDeviceId(d.deviceId)
  }
  window.addEventListener('message', handler)
  return () => window.removeEventListener('message', handler)
}
