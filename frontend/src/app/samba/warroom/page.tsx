'use client'

import React, { useCallback, useEffect, useRef, useState, memo } from 'react'
import { collectorApi } from '@/lib/samba/api/commerce'
import { fetchWithAuth } from '@/lib/samba/api/shared'
import { monitorApi, type DashboardStats, type MonitorEvent, type RefreshLogEntry } from '@/lib/samba/api/operations'
import { SITE_COLORS } from '@/lib/samba/constants'
import { fmtNum, fmtTextNumbers, LOG_FONT_FAMILY } from '@/lib/samba/styles'

const POLL_INTERVAL = 10_000
const LOG_POLL_INTERVAL = 500

// 로그 메시지 앞부분의 [SITE] 태그 추출 — 예: "[12:34:56] [1/100] [MUSINSA] ..." → "MUSINSA"
// 알려진 사이트명 화이트리스트 — 위치 변동에 robust 한 site 추출.
// 옛 "3번째 [...]" 가정은 _idx_prefix 비거나 [int=X.Xs] 추가 시 매칭 깨짐
// (2026-05-26 사고: 재고변동/스킵 로그 등 일부 msg 가 사용자 화면에 안 보임).
const KNOWN_SITES = new Set([
  'ABCmart', 'GrandStage', 'GSShop', 'MUSINSA', 'LOTTEON', 'SSG',
  'KREAM', '다나와', '패션플러스', 'FashionPlus', 'Nike', 'Adidas',
  '렉스몬드', 'Lexmond', '이랜드몰', 'SSF샵', 'SSFShop',
])

const extractSiteFromLog = (msg: string): string | null => {
  const matches = msg.match(/\[([^\]]+)\]/g)
  if (!matches) return null
  for (const m of matches) {
    const inner = m.slice(1, -1)
    if (KNOWN_SITES.has(inner)) return inner
  }
  return null
}

// PC분담 필터: filterSources 기준으로 로그 표시 여부 결정
// - null = 전체 표시 (단일 PC 또는 미설정)
// - [] = 아무것도 표시 안 함 (전체해제 PC) — 태그 무관 메시지(사이클 완료 등)도 숨김
// - [...] = 해당 사이트만 표시 (태그 없는 글로벌 메시지는 표시 — A/B PC가 사이클 진행 알림 보도록)
const shouldShowLog = (msg: string, filterSources: string[] | null): boolean => {
  if (filterSources === null) return true
  if (filterSources.length === 0) return false  // C PC: 글로벌 메시지도 숨김
  const site = extractSiteFromLog(msg)
  if (!site) return true  // A/B PC: 사이클 완료/쿠키 로테이션 등 글로벌 메시지는 보임
  return filterSources.includes(site)
}

// sessionStorage 키 — 새로고침 시 filterSources 즉시 복원 (chrome.storage 메시지 도착 전 leak 방지)
const FILTER_SOURCES_KEY = 'samba.warroom.filterSources'
const loadInitialFilterSources = (): string[] | null => {
  if (typeof window === 'undefined') return null
  try {
    const v = window.sessionStorage.getItem(FILTER_SOURCES_KEY)
    if (v === null) return null
    const parsed = JSON.parse(v)
    return Array.isArray(parsed) ? parsed : null
  } catch {
    return null
  }
}
const saveFilterSourcesToSession = (v: string[] | null): void => {
  if (typeof window === 'undefined') return
  try {
    if (v === null) window.sessionStorage.removeItem(FILTER_SOURCES_KEY)
    else window.sessionStorage.setItem(FILTER_SOURCES_KEY, JSON.stringify(v))
  } catch { /* ignore */ }
}

// 오토튠 실시간 로그 (독립 컴포넌트 — 대시보드 리렌더링 영향 없음)
const AutotuneLogPanel = memo(function AutotuneLogPanel({ onStatusChange, externalRunning, filterSources, deviceId }: {
  onStatusChange?: (running: boolean, cycles: number, lastTick: string | null, refreshed: number) => void
  externalRunning?: boolean
  filterSources?: string[] | null
  deviceId?: string  // 이 PC device_id — 본인 잡 로그만 표시 (PC 분리, 2026-05-25)
}) {
  // 로그에 클라이언트 부여 시퀀스 번호 — React key 안정화용
  const [logs, setLogs] = useState<Array<RefreshLogEntry & { __seq: number }>>([])
  const [, setIntervals] = useState<Record<string, number>>({})
  const sinceIdxRef = useRef(0)
  const seqRef = useRef(0)
  // filterSources를 폴링 클로저에서 최신값으로 읽기 위한 ref
  const filterSourcesRef = useRef(filterSources)
  const containerRef = useRef<HTMLDivElement>(null)
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null)

  // 단일 useEffect로 폴링 관리 — 타이머 중복 방지
  const pollingRef = useRef(false)

  // 마운트 시 오토튠 상태 자동 감지 (탭 재진입 대응)
  const [selfDetectedRunning, setSelfDetectedRunning] = useState(false)
  // 일시적 running:false 무시 — 3회 연속 false일 때만 selfDetectedRunning 해제
  const selfFalseCountRef = useRef(0)
  const isRunning = externalRunning || selfDetectedRunning

  useEffect(() => { filterSourcesRef.current = filterSources }, [filterSources])

  // externalRunning(부모의 10초 status 폴링 결과)이 false로 떨어지면 panel 자체 감지값도 클리어.
  // 폴링 루프에서 autotuneStatus 호출을 빼면서 selfDetectedRunning을 명시적으로 해제할 곳이
  // 사라졌기 때문 — 부모를 단일 진실원으로 사용한다.
  useEffect(() => {
    if (!externalRunning) setSelfDetectedRunning(false)
  }, [externalRunning])

  useEffect(() => {
    // 마운트 직후 서버 상태 확인 — running이면 자동 폴링 시작
    collectorApi.autotuneStatus().then(st => {
      if (st) {
        if (onStatusChange) onStatusChange(st.running, st.cycle_count, st.last_tick, st.refreshed_count || 0)
        if (st.running) { selfFalseCountRef.current = 0; setSelfDetectedRunning(true) }
      }
    }).catch(() => {})
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  // 폴링 중단 시 자체 복구 타이머 — 백엔드 재시작 후 running:true가 되면 10초 내 자동 재개
  useEffect(() => {
    if (isRunning) return
    const recoveryTimer = setInterval(async () => {
      try {
        const st = await collectorApi.autotuneStatus()
        if (st?.running) { selfFalseCountRef.current = 0; setSelfDetectedRunning(true) }
      } catch { /* 무시 */ }
    }, 10_000)
    return () => clearInterval(recoveryTimer)
  }, [isRunning])

  useEffect(() => {
    // 오토튠 꺼져있으면 폴링 안 함
    if (!isRunning) {
      if (timerRef.current) { clearInterval(timerRef.current); timerRef.current = null }
      return
    }

    // 이미 타이머가 있으면 중복 생성 안 함
    if (timerRef.current) return

    const poll = async () => {
      if (pollingRef.current) return
      pollingRef.current = true
      try {
        // 오토튠 status는 주기적으로 안 가져온다 — 한번 실행되면 사용자 액션 외엔 바뀔 일 없음.
        // 마운트 시 1회 + 비실행 상태 10초 recovery 폴링으로 충분. 여기서 매 tick 호출하면
        // 무거운 status 쿼리(samba_extension_key + count(samba_collected_product 24h))가
        // 500ms마다 read 풀을 점유해 refreshLogs가 직렬 await에 막혀 1분+ 지연 발생.
        const idx = sinceIdxRef.current
        const res = await monitorApi.refreshLogs(idx, deviceId || '')
        if (res.current_idx < idx) {
          sinceIdxRef.current = 0
          pollingRef.current = false
          return
        }
        if (res.logs.length > 0 && res.current_idx > idx) {
          sinceIdxRef.current = res.current_idx
          setLogs(prev => {
            const tagged = res.logs.map(l => ({ ...l, __seq: ++seqRef.current }))
            const next = [...prev, ...tagged]
            // slice 전에 선택된 소싱처 필터 적용 — 다른 소싱처 로그가 30개 버퍼 채워 밀려나는 현상 방지
            const fs = filterSourcesRef.current
            const kept = fs && fs.length > 0 ? next.filter(l => shouldShowLog(l.msg, fs)) : next
            return kept.slice(-30)
          })
          requestAnimationFrame(() => {
            if (containerRef.current) {
              containerRef.current.scrollTop = containerRef.current.scrollHeight
            }
          })
        }
        if (res.intervals?.intervals) {
          setIntervals(res.intervals.intervals)
        }
      } catch { /* 무시 */ }
      pollingRef.current = false
    }
    poll()
    timerRef.current = setInterval(poll, LOG_POLL_INTERVAL)

    return () => {
      if (timerRef.current) { clearInterval(timerRef.current); timerRef.current = null }
    }
  }, [isRunning, onStatusChange])

  return (
    <div style={{ background: 'rgba(8,10,16,0.98)', border: '1px solid #1C1E2A', borderRadius: '8px', marginBottom: '12px', overflow: 'hidden' }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '8px 14px', background: '#0A0D14', borderBottom: '1px solid #1C1E2A' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
          <span style={{ fontSize: '0.82rem', fontWeight: 600, color: '#9AA5C0' }}>오토튠 실시간 로그</span>
          <span style={{ fontSize: '0.65rem', color: '#666' }}>실시간</span>
        </div>
        <div style={{ display: 'flex', gap: '6px', alignItems: 'center' }}>
          <button onClick={() => {
            const text = logs
              .filter(l => shouldShowLog(l.msg, filterSources ?? null))
              .map(l => l.msg).join('\n')
            navigator.clipboard.writeText(text)
          }} style={{ padding: '2px 8px', fontSize: '0.65rem', background: 'rgba(76,154,255,0.1)', border: '1px solid rgba(76,154,255,0.3)', color: '#4C9AFF', borderRadius: '4px', cursor: 'pointer' }}>복사</button>
          <button onClick={async () => {
            setLogs([]); sinceIdxRef.current = 0
            try {
              const { API_BASE_URL: apiBase } = await import('@/config/api')
              await fetchWithAuth(`${apiBase}/api/v1/samba/monitor/refresh-logs/clear`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })
            } catch { /* ignore */ }
          }} style={{ padding: '2px 8px', fontSize: '0.65rem', background: 'rgba(255,107,107,0.1)', border: '1px solid rgba(255,107,107,0.3)', color: '#FF6B6B', borderRadius: '4px', cursor: 'pointer' }}>초기화</button>
        </div>
      </div>
      <div
        ref={containerRef}
        style={{ height: '250px', overflowY: 'auto', padding: '10px 14px', fontFamily: LOG_FONT_FAMILY, fontSize: '0.73rem', lineHeight: 1.8, color: '#4A5568' }}
      >
        {(() => {
          const visibleLogs = logs.filter(l => shouldShowLog(l.msg, filterSources ?? null))
          if (visibleLogs.length === 0) {
            return (
              <div style={{ color: '#555', textAlign: 'center', padding: '1.5rem 0' }}>
                {logs.length > 0 ? '이 PC가 담당한 소싱처 로그 없음' : '갱신 로그 대기 중...'}
              </div>
            )
          }
          return visibleLogs.map(log => {
            let color = '#DCE0E8'
            let fontWeight: number | string = 400
            if (log.msg.includes('쿠키 로테이션')) { color = '#FFFFFF'; fontWeight = 700 }
            else if (
              log.msg.includes('실패')
              || log.msg.includes('오류')
              || log.msg.includes('차단 HTTP')
              || log.msg.includes('차단 감지')
              || log.msg.includes('회 차단')
              || log.msg.includes('타임아웃')
              || log.msg.includes('건너뜀')
              || log.msg.includes('갱신 차단')
              || log.msg.includes('미응답')
            ) { color = '#FF6B6B'; fontWeight = 600 }
            else if (log.msg.includes('품절')) color = '#A78BFA'
            else if (log.msg.includes('사이클 완료')) { color = '#4C9AFF'; fontWeight = 700 }
            else if (log.msg.includes('전송완료')) {
              if (log.msg.includes('가격변동') && log.msg.includes('재고전송')) color = '#4C9AFF'  // 가격+재고 동시 전송
              else if (log.msg.includes('재고전송')) color = '#FFD93D'  // 재고만
              // 가격변동만 → 기본색(흰색) 유지
            }
            else if (log.msg.includes('스킵')) color = '#888'
            else if (log.msg.includes('재고변동')) color = '#FFD93D'
            else if (log.msg.includes('성공')) color = '#7BAF7E'
            return <div key={log.__seq} style={{ color, fontWeight }}>{fmtTextNumbers(log.msg)}</div>
          })
        })()}
      </div>
    </div>
  )
})

const card: React.CSSProperties = {
  background: 'rgba(30,30,30,0.5)',
  backdropFilter: 'blur(20px)',
  border: '1px solid #2D2D2D',
  borderRadius: '12px',
  padding: '1.25rem',
}

const normalizeWarroomSourceSite = (value: string | null | undefined) => {
  const site = String(value || '').trim()
  if (!site) return ''
  if (site.toUpperCase() === 'GSSHOP') return 'GSShop'
  return site
}

const normalizeWarroomSiteChanges = (
  changes: Record<string, Record<string, Array<{ id: string; product_id: string | null; product_name: string | null; detail: Record<string, unknown> | null; created_at: string }>>>,
) => {
  return Object.entries(changes).reduce<typeof changes>((acc, [site, byType]) => {
    const key = normalizeWarroomSourceSite(site)
    if (!acc[key]) acc[key] = {}
    for (const [eventType, items] of Object.entries(byType)) {
      const prev = acc[key][eventType] || []
      acc[key][eventType] = [...prev, ...items]
        .sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime())
        .slice(0, 5)
    }
    return acc
  }, {})
}

// LOTTEON 데몬 device_id — 본 PC localStorage 영속. 첫 방문 시 자동 생성.
// 설치 트리거 시 URL 파라미터 ?did=… 로 .exe 다운로드에 전달.
const AUTOTUNE_DAEMON_DID_KEY = 'samba.autotune.daemon.deviceId'
const getOrCreateAutotuneDaemonDeviceId = (): string => {
  if (typeof window === 'undefined') return ''
  try {
    const cached = window.localStorage.getItem(AUTOTUNE_DAEMON_DID_KEY)
    if (cached && cached.startsWith('samba-daemon-')) return cached
    // 8글자 영숫자 random + samba-daemon- prefix
    const rnd = Array.from(window.crypto.getRandomValues(new Uint8Array(6)))
      .map(b => b.toString(16).padStart(2, '0')).join('').slice(0, 12)
    const did = `samba-daemon-${rnd}`
    window.localStorage.setItem(AUTOTUNE_DAEMON_DID_KEY, did)
    return did
  } catch {
    return ''
  }
}

// 데몬 .exe 다운로드 URL — GitHub Release 직접. 본 메인 backend/CDN 트래픽 0.
// cross-origin 이라 a.download 무시되지만, 데몬은 hostname 으로 device_id 자동 생성 →
// 파일명에 정보 박을 필요 없음. backend URL 도 데몬 default 디폴트(env / argv 로 오버라이드 가능).
// 데몬 설치 exe 다운로드 — 백엔드 프록시(JWT) 경유. 백엔드가 로그인 사용자 테넌트로
// 1시간 만료 install-token 을 발급해 exe 파일명에 박아 내려준다. 데몬은 첫 실행 시
// 파일명에서 토큰을 추출해 long-lived 키와 교환 → 사용자는 "다운로드 → 실행"만 하면 됨.
// (기존 GitHub 직접 링크는 cross-origin 이라 파일명에 키를 못 박아 수동 키 주입이 필요했음)
async function downloadDaemonInstaller(did: string): Promise<boolean> {
  const apiBase = process.env.NEXT_PUBLIC_API_URL || 'https://api.samba-wave.co.kr'
  const { showAlert } = await import('@/components/samba/Modal')
  try {
    const { fetchWithAuth } = await import('@/lib/samba/legacy')
    const res = await fetchWithAuth(
      `${apiBase}/api/v1/samba/extension-keys/daemon-installer?device_id=${encodeURIComponent(did)}`,
    )
    if (!res.ok) {
      let body = ''
      try { body = (await res.text()).slice(0, 300) } catch { /* ignore */ }
      if (res.status === 401 || res.status === 403) {
        showAlert(`로그인 만료 — 재로그인 후 다시 시도 (status=${res.status})`, 'error')
      } else {
        showAlert(`데몬 다운로드 실패: status=${res.status}\n${body}`, 'error')
      }
      return false
    }
    const blob = await res.blob()
    if (!blob || blob.size < 1_000_000) {
      // 정상 데몬 exe = 59MB. 1MB 미만이면 redirect HTML 또는 에러 페이지 가능.
      showAlert(`다운로드 응답 비정상 (size=${blob?.size || 0} bytes). backend 로그 확인 필요`, 'error')
      return false
    }
    const cd = res.headers.get('Content-Disposition') || ''
    const m = cd.match(/filename="(.+?)"/)
    const fname = m?.[1] || 'samba.exe'
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = fname
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    setTimeout(() => URL.revokeObjectURL(url), 10_000)
    return true
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e)
    showAlert(`데몬 다운로드 예외: ${msg}`, 'error')
    return false
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// 활성 사이클 패널 — backend in-memory _pc_site_tasks 의 모든 (device, site)
// 진행 중 cycle 표시 + 개별 [중단] 버튼. 사용자 visibility/control 요구
// (2026-05-26 "다른 PC 잡도 보이게 + 인지 못 한 잡 stop").
// ─────────────────────────────────────────────────────────────────────────────
interface ActiveCycle {
  device_id: string
  site: string
  status?: 'active' | 'inactive'
  idx: number
  total: number
  cycle_count: number
  last_tick: string
  heartbeat_ago_sec: number | null
  avg_sec_per_item: number | null
  started_at: string | null
  elapsed_sec: number | null
  price_count: number
  stock_count: number
  soldout_count: number
  last_seen_ago_sec?: number | null
}

function ActiveCyclesPanel(): React.ReactElement {
  const [cycles, setCycles] = useState<ActiveCycle[]>([])
  const [busy, setBusy] = useState<string>('')
  const card: React.CSSProperties = {
    background: '#1F1F1F', border: '1px solid #3D3D3D', borderRadius: '8px',
    padding: '1rem', marginTop: '1rem',
  }
  const fetchCycles = useCallback(async () => {
    try {
      const { API_BASE_URL: api } = await import('@/config/api')
      const r = await fetchWithAuth(`${api}/api/v1/samba/collector/autotune/active-cycles`)
      if (!r.ok) return
      const d = await r.json() as { count: number; cycles: ActiveCycle[] }
      setCycles(d.cycles || [])
    } catch { /* ignore */ }
  }, [])
  useEffect(() => {
    fetchCycles()
    const t = setInterval(fetchCycles, 5000)
    return () => clearInterval(t)
  }, [fetchCycles])
  const cancelCycle = useCallback(async (device_id: string, site: string) => {
    const key = `${device_id}|${site}`
    setBusy(key)
    try {
      const { API_BASE_URL: api } = await import('@/config/api')
      const r = await fetchWithAuth(`${api}/api/v1/samba/collector/autotune/cancel-cycle`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ device_id, site }),
      })
      const d = await r.json().catch(() => ({} as { ok?: boolean; error?: string }))
      if (!d || !d.ok) {
        const { showAlert } = await import('@/components/samba/Modal')
        showAlert(d?.error || '중단 실패', 'error')
      }
      await fetchCycles()
    } finally {
      setBusy('')
    }
  }, [fetchCycles])
  // 비활성 분담 삭제 — pc-allowed-sites 에서 해당 site 제거.
  // 같은 device 의 다른 사이트는 보존 (authoritative=True 전체 덮어쓰기 보호).
  const removeAllowedSite = useCallback(async (device_id: string, site: string) => {
    const key = `${device_id}|${site}`
    setBusy(key)
    try {
      const allSitesForDev = cycles.filter(c => c.device_id === device_id).map(c => c.site)
      const remaining = Array.from(new Set(allSitesForDev.filter(s => s !== site)))
      const { API_BASE_URL: api } = await import('@/config/api')
      const r = await fetchWithAuth(`${api}/api/v1/samba/collector/autotune/pc-allowed-sites`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ device_id, sites: remaining }),
      })
      const d = await r.json().catch(() => ({} as { ok?: boolean; error?: string }))
      if (!d || !d.ok) {
        const { showAlert } = await import('@/components/samba/Modal')
        showAlert(d?.error || '삭제 실패', 'error')
      }
      await fetchCycles()
    } finally {
      setBusy('')
    }
  }, [cycles, fetchCycles])
  return (
    <div style={card}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '0.5rem' }}>
        <div style={{ fontSize: '0.96rem', fontWeight: 600, color: '#E5E5E5' }}>
          활성 사이클 ({fmtNum(cycles.length)}개)
        </div>
        <button onClick={fetchCycles} style={{ padding: '0.25rem 0.6rem', background: 'rgba(76,154,255,0.12)', border: '1px solid rgba(76,154,255,0.35)', borderRadius: '6px', color: '#4C9AFF', fontSize: '0.75rem', cursor: 'pointer' }}>새로고침</button>
      </div>
      {cycles.length === 0 ? (
        <div style={{ fontSize: '0.85rem', color: '#666', padding: '0.5rem 0' }}>활성 사이클 없음</div>
      ) : (
        <table style={{ width: '100%', fontSize: '0.85rem', color: '#E5E5E5', borderCollapse: 'collapse' }}>
          <thead>
            <tr style={{ color: '#888', borderBottom: '1px solid #3D3D3D' }}>
              <th style={{ textAlign: 'left', padding: '0.4rem' }}>PC (device)</th>
              <th style={{ textAlign: 'left', padding: '0.4rem' }}>사이트</th>
              <th style={{ textAlign: 'center', padding: '0.4rem' }}>상태</th>
              <th style={{ textAlign: 'right', padding: '0.4rem' }}>진행</th>
              <th style={{ textAlign: 'right', padding: '0.4rem' }}>처리속도</th>
              <th style={{ textAlign: 'right', padding: '0.4rem' }}>가격</th>
              <th style={{ textAlign: 'right', padding: '0.4rem' }}>재고</th>
              <th style={{ textAlign: 'right', padding: '0.4rem' }}>품절</th>
              <th style={{ textAlign: 'right', padding: '0.4rem' }}>사이클#</th>
              <th style={{ textAlign: 'left', padding: '0.4rem' }}>시작 시각</th>
              <th style={{ textAlign: 'right', padding: '0.4rem' }}>경과</th>
              <th style={{ textAlign: 'right', padding: '0.4rem' }}>최근 활동</th>
              <th style={{ textAlign: 'center', padding: '0.4rem' }}>조치</th>
            </tr>
          </thead>
          <tbody>
            {cycles.map(c => {
              const k = `${c.device_id}|${c.site}`
              const hbStr = c.heartbeat_ago_sec === null ? '-' : `${fmtNum(c.heartbeat_ago_sec)}초 전`
              const avgStr = c.avg_sec_per_item === null || c.avg_sec_per_item === undefined
                ? '-'
                : `${c.avg_sec_per_item.toFixed(1)}초/1건`
              // 시작 시각 → KST HH:MM:SS
              let startedStr = '-'
              if (c.started_at) {
                try {
                  const d = new Date(c.started_at)
                  const kst = new Date(d.getTime() + 9 * 3600 * 1000)
                  startedStr = kst.toISOString().slice(11, 19)
                } catch { /* ignore */ }
              }
              // 경과 시간 → 분:초 또는 시:분:초
              let elapsedStr = '-'
              if (c.elapsed_sec !== null && c.elapsed_sec !== undefined) {
                const s = c.elapsed_sec
                const h = Math.floor(s / 3600)
                const m = Math.floor((s % 3600) / 60)
                const sec = s % 60
                elapsedStr = h > 0
                  ? `${fmtNum(h)}:${String(m).padStart(2,'0')}:${String(sec).padStart(2,'0')}`
                  : `${fmtNum(m)}:${String(sec).padStart(2,'0')}`
              }
              const isInactive = c.status === 'inactive'
              const rowOpacity = isInactive ? 0.55 : 1
              const statusLabel = isInactive
                ? (c.last_seen_ago_sec != null ? `비활성 (${fmtNum(c.last_seen_ago_sec)}초 전 폴링)` : '비활성 (폴링 없음)')
                : '활성'
              const statusColor = isInactive ? '#888' : '#4CD964'
              return (
                <tr key={k} style={{ borderBottom: '1px solid #2A2A2A', opacity: rowOpacity }}>
                  <td style={{ padding: '0.4rem', fontFamily: 'monospace', fontSize: '0.75rem' }}>
                    {c.device_id.slice(0, 28)}
                    <span style={{ marginLeft: '0.3rem', fontSize: '0.7rem', color: c.device_id.startsWith('samba-daemon-') ? '#4C9AFF' : '#FFB84D' }}>
                      ({c.device_id.startsWith('samba-daemon-') ? '데몬' : '확장앱'})
                    </span>
                  </td>
                  <td style={{ padding: '0.4rem' }}>{c.site}</td>
                  <td style={{ padding: '0.4rem', textAlign: 'center', fontSize: '0.72rem', color: statusColor }}>{statusLabel}</td>
                  <td style={{ padding: '0.4rem', textAlign: 'right' }}>{isInactive ? '-' : `${fmtNum(c.idx)} / ${fmtNum(c.total)}`}</td>
                  <td style={{ padding: '0.4rem', textAlign: 'right', color: '#FFB84D' }}>{isInactive ? '-' : avgStr}</td>
                  <td style={{ padding: '0.4rem', textAlign: 'right', color: '#4CD964' }}>{isInactive ? '-' : fmtNum(c.price_count)}</td>
                  <td style={{ padding: '0.4rem', textAlign: 'right', color: '#4C9AFF' }}>{isInactive ? '-' : fmtNum(c.stock_count)}</td>
                  <td style={{ padding: '0.4rem', textAlign: 'right', color: '#EF4444' }}>{isInactive ? '-' : fmtNum(c.soldout_count)}</td>
                  <td style={{ padding: '0.4rem', textAlign: 'right' }}>{isInactive ? '-' : fmtNum(c.cycle_count)}</td>
                  <td style={{ padding: '0.4rem', fontFamily: 'monospace', fontSize: '0.75rem', color: '#9AA5C0' }}>{isInactive ? '-' : startedStr}</td>
                  <td style={{ padding: '0.4rem', textAlign: 'right', color: '#9AA5C0' }}>{isInactive ? '-' : elapsedStr}</td>
                  <td style={{ padding: '0.4rem', textAlign: 'right', color: '#888' }}>{isInactive ? '-' : hbStr}</td>
                  <td style={{ padding: '0.4rem', textAlign: 'center' }}>
                    {isInactive ? (
                      <button
                        disabled={busy === k}
                        onClick={() => removeAllowedSite(c.device_id, c.site)}
                        style={{
                          padding: '0.2rem 0.6rem',
                          background: busy === k ? '#444' : 'rgba(180,180,180,0.15)',
                          border: '1px solid rgba(180,180,180,0.4)',
                          borderRadius: '4px',
                          color: '#CCCCCC',
                          fontSize: '0.75rem',
                          cursor: busy === k ? 'wait' : 'pointer',
                        }}
                      >{busy === k ? '삭제중…' : '분담삭제'}</button>
                    ) : (
                      <button
                        disabled={busy === k}
                        onClick={() => cancelCycle(c.device_id, c.site)}
                        style={{
                          padding: '0.2rem 0.6rem',
                          background: busy === k ? '#444' : 'rgba(239,68,68,0.15)',
                          border: '1px solid rgba(239,68,68,0.4)',
                          borderRadius: '4px',
                          color: '#EF4444',
                          fontSize: '0.75rem',
                          cursor: busy === k ? 'wait' : 'pointer',
                        }}
                      >{busy === k ? '중단중…' : '중단'}</button>
                    )}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}
    </div>
  )
}

export default function WarroomPage() {
  useEffect(() => { document.title = 'SAMBA-오토튠' }, [])

  // 무신사 자동로그인계정 상태 — 60s 폴링. 미설정/만료 시 모달 경고.
  // cost 계산이 자동로그인계정 단일 쿠키만 사용하므로 미설정 시 오토튠 무효.
  const [musinsaAuthMissing, setMusinsaAuthMissing] = useState<{
    reason: 'unset' | 'cookie_expired' | 'no_cookie'
    account_label: string | null
  } | null>(null)
  const [musinsaAuthDismissed, setMusinsaAuthDismissed] = useState<boolean>(false)
  useEffect(() => {
    let cancelled = false
    const apiBase = process.env.NEXT_PUBLIC_API_URL || 'https://api.samba-wave.co.kr'
    const tick = async () => {
      try {
        const r = await fetchWithAuth(`${apiBase}/api/v1/samba/sourcing-accounts/musinsa/autologin-status`)
        if (!r.ok) return
        const j = await r.json()
        if (cancelled) return
        if (j?.missing) {
          setMusinsaAuthMissing({ reason: j.reason, account_label: j.account_label })
        } else {
          setMusinsaAuthMissing(null)
          setMusinsaAuthDismissed(false)
        }
      } catch { /* ignore */ }
    }
    tick()
    const t = setInterval(tick, 60_000)
    return () => { cancelled = true; clearInterval(t) }
  }, [])

  const [stats, setStats] = useState<DashboardStats | null>(null)
  // 이벤트 타임라인 state 제거 (2026-05-26 사용자 요구) — 활성 사이클 패널이 대체.
  // monitorApi.recentEvents/siteChanges/marketChanges 폴링 + DB 부담 제거.

  const [loading, setLoading] = useState(true)
  const [, setLastFetched] = useState<Date | null>(null)
  const nextPollRef = useRef(POLL_INTERVAL / 1000)

  // 실시간 로그 상태

  // 소싱처/마켓 상태

  // 오토튠 상태
  const [autotuneRunning, setAutotuneRunning] = useState(false)
  const [autotuneCycles, setAutotuneCycles] = useState(0)
  const [autotuneRestarts, setAutotuneRestarts] = useState(0)
  // 이 PC device_id — 실시간 로그 PC별 분리(2026-05-25)
  // 브라우저 device + 본인 PC 데몬 device 둘 다 합쳐 보냄 → 데몬 잡 로그도 본인 PC 로그로 표시.
  // 데몬 device_id 는 localhost:51425/device_id (데몬이 띄운 sync 서버)에서 자동 fetch.
  // 같은 PC 데몬만 응답(loopback) → 사용자 수동 입력 X, 포크 유저 동일 흐름.
  const [pcDeviceId, setPcDeviceId] = useState<string>('')
  useEffect(() => {
    // 마운트 1회 + 매 60초 51425 fetch — 데몬 v1.4.6 자동업데이트 시 device_id 가 새로 발급되면
    // localStorage 캐시를 즉시 갱신해 다음 시작/register 가 신규 device_id 로 박힘.
    // (2026-05-26 회귀: 데몬 v2 마이그레이션 후 페이지 stale localStorage 가 옛 device_id 박은 사고)
    let cancelled = false
    const syncDaemonDev = async () => {
      try {
        const { getDeviceId } = await import('@/lib/samba/deviceId')
        const dev = getDeviceId()
        let daemonDev = ''
        try {
          const ctrl = new AbortController()
          const t = setTimeout(() => ctrl.abort(), 1500)
          const r = await fetch('http://localhost:51425/device_id', { signal: ctrl.signal })
          clearTimeout(t)
          if (r.ok) {
            const j = await r.json()
            if (j && typeof j.device_id === 'string' && j.device_id) {
              daemonDev = j.device_id
              const cached = window.localStorage.getItem('samba.autotune.daemon.deviceId') || ''
              if (cached !== daemonDev) {
                window.localStorage.setItem('samba.autotune.daemon.deviceId', daemonDev)
              }
            }
          }
        } catch { /* 데몬 안 켜진 PC — 무시 */ }
        if (!daemonDev) {
          daemonDev = (typeof window !== 'undefined' && (
            window.localStorage.getItem('samba.autotune.daemon.deviceId') ||
            window.localStorage.getItem('samba.lotteon.daemon.deviceId')
          )) || ''
        }
        if (cancelled) return
        const ids = [dev, daemonDev].filter(Boolean)
        if (ids.length) setPcDeviceId(prev => {
          const next = ids.join(',')
          return next === prev ? prev : next
        })
      } catch { /* ignore */ }
    }
    syncDaemonDev()
    const timer = setInterval(syncDaemonDev, 60_000)
    return () => { cancelled = true; clearInterval(timer) }
  }, [])
  const [singleProductNo, setSingleProductNo] = useState('')
  const [, setAutotuneLastTick] = useState<string | null>(null)
  const prevCyclesRef = useRef(0)
  const prevEventsFetchedAtRef = useRef(0)
  const falseCountRef = useRef(0)
  // 자동 재등록 쿨다운 (백엔드 재시작 시 무한 루프 방지) — 60초
  const autoRejoinAtRef = useRef(0)
  // load() 폴링 클로저에서 최신 filter/avail를 읽기 위한 ref (load deps 안정화)
  const filterSourcesOuterRef = useRef<string[] | null>(null)
  const availSourcesOuterRef = useRef<string[]>([])
  // 데몬 id(localStorage daemonDev)를 마지막으로 재전송한 값 — 첫 감지/변경 시 1회만 재등록.
  const reregisteredDaemonDevRef = useRef('')

  // 소싱처별 인터벌 설정
  const INTERVAL_SITES = [
    { key: 'MUSINSA', label: '무신사' },
    { key: 'KREAM', label: 'KREAM' },
    { key: 'DANAWA', label: '다나와' },
    { key: 'FashionPlus', label: '패션플러스' },
    { key: 'Nike', label: 'Nike' },
    { key: 'Adidas', label: 'Adidas' },
    { key: 'ABCmart', label: 'ABC마트' },
    { key: 'REXMONDE', label: '렉스몬드' },
    { key: 'SSG', label: 'SSG' },
    { key: 'LOTTEON', label: '롯데ON' },
    { key: 'GSShop', label: 'GSShop' },
    { key: 'ElandMall', label: '이랜드몰' },
    { key: 'SSF', label: 'SSF샵' },
  ]
  const [siteIntervals, setSiteIntervals] = useState<Record<string, string>>({})
  const intervalTimerRef = useRef<Record<string, ReturnType<typeof setTimeout>>>({})
  const [siteConcurrency, setSiteConcurrency] = useState<Record<string, string>>({})
  const concurrencyTimerRef = useRef<Record<string, ReturnType<typeof setTimeout>>>({})

  // 현재 인터벌은 load()의 autotuneStatus 응답에서 함께 설정됨 — 중복 호출 제거
  // 동시성도 status 응답에 포함됨

  const handleIntervalChange = useCallback((site: string, value: string) => {
    setSiteIntervals(prev => ({ ...prev, [site]: value }))
    // 디바운스 — 0.5초 후 자동 저장
    if (intervalTimerRef.current[site]) clearTimeout(intervalTimerRef.current[site])
    intervalTimerRef.current[site] = setTimeout(async () => {
      const num = parseFloat(value)
      if (isNaN(num) || num < 0 || num > 60) return
      try {
        await collectorApi.autotuneUpdateInterval(site, num)
      } catch { /* ignore */ }
    }, 500)
  }, [])

  const handleConcurrencyChange = useCallback((site: string, value: string) => {
    setSiteConcurrency(prev => ({ ...prev, [site]: value }))
    if (concurrencyTimerRef.current[site]) clearTimeout(concurrencyTimerRef.current[site])
    concurrencyTimerRef.current[site] = setTimeout(async () => {
      const num = parseInt(value, 10)
      if (isNaN(num) || num < 1 || num > 50) return
      try {
        await collectorApi.autotuneUpdateConcurrency(site, num)
      } catch { /* ignore */ }
    }, 500)
  }, [])
  // ── 오토튠 필터 (소싱처/판매처 체크박스) ──
  // 소싱처 체크박스 = AND 조건 (체크된 사이트만 갱신 + 이 PC가 처리):
  //   1) 백엔드 enabled_sources(글로벌 갱신 사이트) 업데이트 → 그 사이트만 큐에 작업 발행
  //   2) chrome.storage.allowedSites(이 PC 분담) 업데이트 → 익스텐션 폴링 헤더로 전송
  //   3) 백엔드는 enabled_sources 사이트 작업만 발행 + 익스텐션은 allowedSites 사이트 작업만 받음
  //      → 단일 PC: 화면 체크 = 갱신 = 처리 (사용자 의도)
  //      → 다중 PC: 마지막 변경한 PC의 값이 백엔드 글로벌로 살아남음 (사용자 합의 필요)
  // 판매처 체크박스는 기존 백엔드 글로벌 enabled_markets 그대로.
  // sessionStorage에서 동기 복원 — 새로고침 시 chrome.storage 메시지 도착 전 1프레임 leak 방지.
  // 같은 탭이 살아있는 동안 유지되며 탭 닫으면 자동 비움(다른 PC 설정 누수 방지).
  // SSR(window undefined) 시점에는 null이 박혀 클라이언트에도 그대로 hydrate됨 → 모든 체크박스가 켜진 상태로 표시.
  // 그래서 초기값은 null로 두고, 마운트 직후 useEffect에서 sessionStorage를 읽어 복원한다.
  const [filterSources, setFilterSources] = useState<string[] | null>(null)
  const [filterMarkets, setFilterMarkets] = useState<string[] | null>(null) // null=전체
  // fetch 실패/지연 시에도 체크박스가 항상 보이도록 default 리스트로 즉시 초기화.
  // 백엔드 filters fetch 가 100초 걸리거나 실패할 때 페이지가 빈 상태로 보이던
  // 사고 차단 (2026-05-25 포크 유저도 동일 UX 보장).
  // GrandStage 는 ABCmart 의 a-rt.com 하부 도메인 — UI 별도 노출 X (ABCmart 에 포함).
  const _DEFAULT_AVAIL_SOURCES = [
    'ABCmart', 'GSShop', 'LOTTEON', 'MUSINSA', 'SSG',
  ]
  const _DEFAULT_AVAIL_MARKETS = [
    '11번가', '옥션', '쿠팡', '롯데홈쇼핑', '롯데ON', '플레이오토', '스마트스토어', 'SSG', 'G마켓',
  ]
  const [availSources, setAvailSources] = useState<string[]>(_DEFAULT_AVAIL_SOURCES)
  const [availMarkets, setAvailMarkets] = useState<string[]>(_DEFAULT_AVAIL_MARKETS)
  const filterTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  // load() 폴링 closure stale 방지 — 최신값 ref 동기화
  useEffect(() => { filterSourcesOuterRef.current = filterSources }, [filterSources])
  useEffect(() => { availSourcesOuterRef.current = availSources }, [availSources])

  // 마운트 즉시 sessionStorage에서 filterSources 복원 (SSR hydration이 null로 덮어쓰는 문제 보정)
  // 같은 탭에서 페이지 이탈 후 돌아왔을 때 부분선택이 전체체크로 되돌아가지 않도록.
  useEffect(() => {
    const restored = loadInitialFilterSources()
    if (Array.isArray(restored)) setFilterSources(restored)
  }, [])

  useEffect(() => {
    // 1) 사용 가능 사이트/마켓 목록은 백엔드에서, 소싱처 체크 상태는 chrome.storage 우선
    collectorApi.autotuneGetFilters().then(res => {
      setAvailSources(res.available_sources)
      setAvailMarkets(res.available_markets)
      setFilterMarkets(res.enabled_markets)
      // filterSources는 useState 초기값 null로 이미 세팅됨 — 여기서 또 setFilterSources(null) 하면
      // 동시에 도착한 chrome.storage ALLOWED_SITES 메시지 결과([] 등)를 덮어쓰는 race 발생.
      // C PC가 전체해제해도 새로고침마다 전체체크로 되돌아가는 버그의 원인이었음.
    }).catch(() => {})

    // 2) 이 PC의 chrome.storage.allowedSites로 체크박스 초기화
    //    null=미설정(전체처리), []=전체해제, [...]=부분선택
    // (2026-05-25) 자동 register 폐기 — registered 플래그도 제거.
    const onMessage = (e: MessageEvent) => {
      if (e.source !== window) return
      const msg = e.data
      if (!msg || typeof msg !== 'object') return
      if (msg.source !== 'samba-extension') return
      if (msg.type !== 'ALLOWED_SITES') return
      const sites = msg.sites
      const fromExt = Array.isArray(sites) ? sites : null
      // 확장앱이 null 보내는데 로컬(sessionStorage)에 명시 선택값이 있으면 → 로컬 값으로 확장앱 storage 복구
      // (페이지 갔다오면 부분선택이 전체선택으로 되돌아가는 버그 방지)
      const localStored = loadInitialFilterSources()
      let next: string[] | null
      if (fromExt === null && Array.isArray(localStored)) {
        next = localStored
        try {
          window.postMessage({ source: 'samba-page', type: 'SET_ALLOWED_SITES', sites: localStored }, window.location.origin)
        } catch { /* ignore */ }
      } else {
        next = fromExt
      }
      setFilterSources(next)
      saveFilterSourcesToSession(next)
      // (2026-05-25) 자동 register 폐기 — 페이지 접속만으로 backend 분담 박히는 사고 차단.
      // 사용자가 "오토튠 활성" 토글 ON 한 경우만 register 호출.
    }
    window.addEventListener('message', onMessage)
    // SPA 라우팅으로 페이지에 재진입한 경우 content_script가 다시 실행되지 않으므로
    // 확장앱에 명시적으로 현재 allowedSites를 다시 보내달라고 요청한다.
    try {
      window.postMessage({ source: 'samba-page', type: 'GET_ALLOWED_SITES' }, window.location.origin)
    } catch { /* ignore */ }
    return () => window.removeEventListener('message', onMessage)
  }, [])

  // (2026-05-25) availSources 로드 완료 후 자동 register 폐기.
  // 페이지 접속 자동 분담 박힘 차단 — 사용자 "오토튠 활성" 토글 ON 시만 register.

  // 소싱처 체크 변경 시 익스텐션 chrome.storage 동기화 (PC별 분담 헤더용)
  // null=전체처리(미설정), []=전체해제, [...]=부분선택 — 구분 그대로 전달
  const syncAllowedSitesToExtension = useCallback((sites: string[] | null) => {
    try {
      window.postMessage(
        { source: 'samba-page', type: 'SET_ALLOWED_SITES', sites },
        window.location.origin,
      )
    } catch { /* ignore */ }
  }, [])

  // 백엔드 + 익스텐션 동시 저장 (debounce 500ms)
  const saveFilters = useCallback((sources: string[] | null, markets: string[] | null) => {
    if (filterTimerRef.current) clearTimeout(filterTimerRef.current)
    filterTimerRef.current = setTimeout(async () => {
      try {
        await collectorApi.autotuneSetFilters(sources, markets)
      } catch { /* ignore */ }
    }, 500)
  }, [])

  const saveMarketFilter = useCallback((markets: string[] | null) => {
    saveFilters(filterSources, markets)
  }, [filterSources, saveFilters])

  const registerPcAllowedSites = useCallback(async (sites: string[] | null) => {
    try {
      const { getDeviceId } = await import('@/lib/samba/deviceId')
      const dev = getDeviceId()
      const daemonDev = (typeof window !== 'undefined' &&
        window.localStorage.getItem('samba.autotune.daemon.deviceId')) || ''
      // 확장앱 id 가 페이지에 안 꽂힌 PC(데몬 전용 원격 PC 등)에서도 데몬 사이트는 등록돼야 함.
      // 둘 다 없을 때만 중단 (2026-06-04: 확장앱 id null → 데몬 ABC 등록 누락 사고).
      if (!dev && !daemonDev) return
      const { API_BASE_URL: apiBase } = await import('@/config/api')
      // 사이트별 dev 분리 (2026-05-25 사용자 룰):
      //  - 데몬 전용(SSG/ABCmart/GrandStage/LOTTEON) → 데몬 dev 에만 (가격수집+송장 둘 다 데몬)
      //  - 무신사/GSShop → 브라우저 dev 에만 (가격수집은 확장앱). 송장(tracking)은 데몬에
      //    등록하지 않아도 백엔드 dequeue 의 tracking site-분담 예외로 데몬 기존 워커가 처리.
      //    (무신사를 데몬 active_sites 에 넣으면 가격수집 워커가 중복 스폰되는 사고 — 등록 금지)
      const _DAEMON_ONLY = new Set(['SSG', 'ABCmart', 'GrandStage', 'LOTTEON'])
      // ABCmart 체크 = ABCmart + GrandStage 자동 expand (같은 a-rt.com 도메인)
      const _SITE_EXPAND: Record<string, string[]> = {
        ABCmart: ['ABCmart', 'GrandStage'],
      }
      const expanded = sites === null
        ? null
        : sites.flatMap(s => _SITE_EXPAND[s] || [s])
      // 사이트 분리
      const browserSites = expanded === null
        ? null
        : expanded.filter(s => !_DAEMON_ONLY.has(s))
      const daemonSites = expanded === null
        ? null
        : expanded.filter(s => _DAEMON_ONLY.has(s))
      // race fix (2026-05-28): Promise.all 동시 POST 시 백엔드 persist 가 read-modify-write
      // 라 last-write-wins 으로 먼저 박힌 device row 가 덮어써짐 → 브라우저 dev 분담이 DB
      // 에서 사라지고 lifecycle sync 가 _pc_allowed_sites.pop → 무신사/GS 사이클 cancel.
      // 순차 await 로 직렬화.
      if (dev) {
        await fetchWithAuth(
          `${apiBase}/api/v1/samba/collector/autotune/pc-allowed-sites`,
          {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ device_id: dev, sites: browserSites }),
          },
        )
      }
      if (daemonDev) {
        await fetchWithAuth(
          `${apiBase}/api/v1/samba/collector/autotune/pc-allowed-sites`,
          {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ device_id: daemonDev, sites: daemonSites }),
          },
        )
      }
    } catch { /* ignore */ }
  }, [])

  const toggleSource = useCallback((site: string) => {
    setFilterSources(prev => {
      const all = availSources
      const current = prev ?? [...all]
      const next = current.includes(site) ? current.filter(s => s !== site) : [...current, site]
      const result = next.length === all.length ? null : next
      // 4중 동기화:
      // 1) chrome.storage.allowedSites: 확장앱이 X-Allowed-Sites 헤더로 잡 dispatch 필터
      // 2) 백엔드 PC분담 등록: 모든 PC의 합집합으로 백엔드 active_sites 계산
      // 3) sessionStorage: 새로고침 즉시 복원 (1프레임 leak 방지)
      // 4) saveFilters(null, ...): legacy 글로벌 enabled_sources는 항상 null로 비활성화
      // 전체선택(null)이어도 extension에 명시 목록 전달 — null이면 X-Allowed-Sites 헤더 미전송으로
      // 서버 재시작 후 PC 재등록이 안 되는 버그 방지 (extension은 항상 명시 목록 유지)
      syncAllowedSitesToExtension(result === null ? [...all] : result)
      // 전체선택(null)이어도 availSources 명시 전달 — DB에만 있고 UI에 없는 소싱처 레거시 모드 실행 차단
      registerPcAllowedSites(result === null ? [...all] : result)
      saveFilterSourcesToSession(result)
      saveFilters(null, filterMarkets)
      return result
    })
  }, [availSources, filterMarkets, saveFilters, syncAllowedSitesToExtension, registerPcAllowedSites])

  // 데몬 id가 처음 감지/변경되는 순간 현재 체크 상태를 데몬에 1회 재전송 (오토튠 체크 desync 차단).
  // 페이지 진입 직후 daemonDev(localhost:51425) 미해결 상태에서 SSG 등을 체크하면 toggleSource의
  // 데몬 POST가 `if(daemonDev)` 거짓으로 skip돼, 화면은 체크인데 백엔드 분담엔 미반영되던 버그 fix.
  // 명시 선택(non-null)일 때만 재전송 — null(전체/미설정)은 데몬 과배정 위험이라 제외.
  useEffect(() => {
    const daemonDev = (typeof window !== 'undefined' &&
      window.localStorage.getItem('samba.autotune.daemon.deviceId')) || ''
    if (!daemonDev || reregisteredDaemonDevRef.current === daemonDev) return
    const fs = filterSourcesOuterRef.current
    if (fs === null) return
    reregisteredDaemonDevRef.current = daemonDev
    registerPcAllowedSites(fs)
  }, [pcDeviceId, registerPcAllowedSites])

  const toggleMarket = useCallback((marketType: string) => {
    setFilterMarkets(prev => {
      const all = availMarkets
      const current = prev ?? [...all]
      const next = current.includes(marketType) ? current.filter(m => m !== marketType) : [...current, marketType]
      const result = next.length === all.length ? null : next
      saveMarketFilter(result)
      return result
    })
  }, [availMarkets, saveMarketFilter])

  const handleAutotuneStatus = useCallback((running: boolean, cycles: number, lastTick: string | null, refreshed: number) => {
    // 별도 스레드 타이밍 차이 대응 — 2회 연속 false일 때 정지 표시 (POLL_INTERVAL 10초 × 2 = 20초 desync 감지)
    if (!running) {
      falseCountRef.current++
      if (falseCountRef.current < 2) return  // 일시적 false 무시
    } else {
      falseCountRef.current = 0
    }
    setAutotuneRunning(running)
    setAutotuneCycles(cycles)
    setAutotuneLastTick(lastTick)
    // 이벤트 타임라인 갱신 조건:
    //  (1) 사이클 증가 시 — 정상 동작 시 트리거
    //  (2) Watchdog 강제재시작으로 cycles=0 리셋된 경우엔 (1)이 영영 안 와서
    //      마지막 fetch로부터 60초 경과 시 강제 갱신
    const _nowMs = Date.now()
    const _cyclesAdvanced = cycles > prevCyclesRef.current
    const _staleFetch = _nowMs - prevEventsFetchedAtRef.current > 60_000
    if (_cyclesAdvanced || _staleFetch) {
      prevCyclesRef.current = cycles
      prevEventsFetchedAtRef.current = _nowMs
      // 이벤트 타임라인 fetch 제거 — 활성 사이클 패널로 대체.
    }
  }, [])

  const load = useCallback(async () => {
    // 각 API를 독립 발사 — 도착하는 대로 setState (Promise.all 블로킹 제거).
    // 이벤트 타임라인 3개 (recentEvents/siteChanges/marketChanges) 는 30분 폴링용 별도 useEffect 로 분리.
    // setLoading(false) 는 dashboard fetch 와 무관하게 즉시 — 페이지 골격 우선 표시.
    // dashboard 빈 구조(_warming:true) 도착 시 stats 영역은 자체 폴링으로 데이터 채워짐.
    setLoading(false)
    monitorApi.dashboard()
      .then(d => { if (d) setStats(d) })
      .catch(() => { /* ignore */ })

    ;(async () => {
      const { getDeviceId } = await import('@/lib/samba/deviceId')
      const dev = getDeviceId()
      // (2026-05-26) chrome dev + 데몬 dev 둘 다 status 체크 → 둘 중 하나라도 running 이면 본인 PC 작동.
      // 이전: chrome dev 만 보다가 데몬 device 가 실제 사이클을 돌리는데 "정지" 잘못 표시되던 사고.
      const daemonDev = (typeof window !== 'undefined' && window.localStorage.getItem('samba.autotune.daemon.deviceId')) || ''
      const [stChrome, stDaemon] = await Promise.all([
        dev ? collectorApi.autotuneStatus(dev) : Promise.resolve(null),
        daemonDev ? collectorApi.autotuneStatus(daemonDev) : Promise.resolve(null),
      ])
      const st = (() => {
        const a = stChrome || ({} as Record<string, unknown>)
        const b = stDaemon || ({} as Record<string, unknown>)
        const _running = !!(a as { running?: boolean }).running || !!(b as { running?: boolean }).running
        const _enabled = ((a as { enabled?: boolean }).enabled ?? (b as { enabled?: boolean }).enabled) ?? null
        const _last = [(a as { last_tick?: string | null }).last_tick, (b as { last_tick?: string | null }).last_tick].filter(Boolean).sort().pop() || null
        const _cycle = Math.max(((a as { cycle_count?: number }).cycle_count) || 0, ((b as { cycle_count?: number }).cycle_count) || 0)
        const _refreshed = Math.max(((a as { refreshed_count?: number }).refreshed_count) || 0, ((b as { refreshed_count?: number }).refreshed_count) || 0)
        const _restart = ((a as { restart_count?: number }).restart_count) || ((b as { restart_count?: number }).restart_count) || 0
        const _running_pcs_raw = [...(((a as { running_pcs?: string[] }).running_pcs) || []), ...(((b as { running_pcs?: string[] }).running_pcs) || [])]
        const _running_pcs = [...new Set(_running_pcs_raw)]
        const _site_intervals = (a as { site_intervals?: Record<string, unknown> }).site_intervals || (b as { site_intervals?: Record<string, unknown> }).site_intervals
        const _site_autotune_concurrency = (a as { site_autotune_concurrency?: Record<string, unknown> }).site_autotune_concurrency || (b as { site_autotune_concurrency?: Record<string, unknown> }).site_autotune_concurrency
        return { ...a, ...b, running: _running, enabled: _enabled, last_tick: _last, cycle_count: _cycle, refreshed_count: _refreshed, restart_count: _restart, running_pcs: _running_pcs, site_intervals: _site_intervals, site_autotune_concurrency: _site_autotune_concurrency }
      })()
      return { st, dev }
    })()
      .then(async ({ st: atStatus, dev }) => {
        // PC별 분리 표시 — 본인 dev 의 running 만 사용 (테넌트 합산 OR 제거).
        // 이전: 다른 PC 가 켜지면 본인 UI 도 "실행중" 으로 뜨던 사고 → 2026-05-25 strict per-PC.
        // did 불일치(서버 재시작 자동복원) 케이스는 사용자가 시작 버튼 누르면 본인 dev 로 정렬됨.
        handleAutotuneStatus(atStatus.running, atStatus.cycle_count, atStatus.last_tick, atStatus.refreshed_count || 0)
        setAutotuneRestarts(atStatus.restart_count || 0)
        // 본인 PC가 서버에서 실행 중으로 확인되면 intent='start'로 복원 (페이지 새로고침 대응)
        // 단, 백엔드 enabled=false(사용자가 정지)면 intent를 'stop'으로 내려 자동재합류 차단.
        // 정지 직후 코디네이터가 채 안 죽은 순간의 status 폴링이 intent를 'start'로 되살려
        // 60초마다 재시작하던 "정지 안 됨" 루프의 근본 원인을 막는다.
        try {
          if (atStatus.enabled === false) {
            if (window.localStorage.getItem('samba.autotune.userIntent') !== 'stop') {
              window.localStorage.setItem('samba.autotune.userIntent', 'stop')
            }
          } else if (atStatus.running && dev && (atStatus.running_pcs || []).includes(dev)) {
            if (window.localStorage.getItem('samba.autotune.userIntent') !== 'start') {
              window.localStorage.setItem('samba.autotune.userIntent', 'start')
            }
          }
        } catch { /* ignore */ }
        // 자동 재등록 — 사용자가 시작 의도를 가진 채 백엔드가 재시작된 경우 자동 복구
        try {
          const intent = window.localStorage.getItem('samba.autotune.userIntent')
          const runningPcs = atStatus.running_pcs || []
          const meMissing = !!dev && !runningPcs.includes(dev)
          const now = Date.now()
          // cooldown은 localStorage에 박아 페이지 재마운트 시에도 유지
          // useRef는 unmount 시 0으로 리셋되어, 페이지 들어올 때마다 즉시 재시작 트리거되는 문제 방지
          let _lastAutoRejoinAt = 0
          try {
            _lastAutoRejoinAt = Number(window.localStorage.getItem('samba.autotune.autoRejoinAt') || '0')
          } catch { /* ignore */ }
          const cooldownPassed = now - Math.max(autoRejoinAtRef.current, _lastAutoRejoinAt) > 10_000
          // (2026-05-25) 자동재합류 폐기 — 사용자가 명시 "오토튠 활성" 토글 ON 한 경우만 시작.
          // 페이지 접속 + intent==='start' 흔적만으로 자동 register/autotuneStart 호출하던 사고 차단.
        } catch { /* ignore */ }
        // 소싱처 인터벌 동기화 (마운트 시 초기값 포함) — 별도 useEffect 제거하고 여기서 일원화
        if (atStatus.site_intervals) {
          setSiteIntervals(prev => {
            // 사용자가 디바운스 중인 값을 덮어쓰지 않도록 — 빈 상태일 때만 초기화
            if (Object.keys(prev).length > 0) return prev
            const init: Record<string, string> = {}
            for (const [site, val] of Object.entries(atStatus.site_intervals!)) {
              init[site] = String(val)
            }
            return init
          })
        }
        if (atStatus.site_autotune_concurrency) {
          setSiteConcurrency(prev => {
            if (Object.keys(prev).length > 0) return prev
            const init: Record<string, string> = {}
            for (const [site, val] of Object.entries(atStatus.site_autotune_concurrency!)) {
              init[site] = String(val)
            }
            return init
          })
        }
      })
      .catch(() => { /* ignore */ })

    setLastFetched(new Date())
    nextPollRef.current = POLL_INTERVAL / 1000
  }, [handleAutotuneStatus, registerPcAllowedSites])

  // 로그 폴링은 AutotuneLogPanel 내부에서 독립적으로 처리

  useEffect(() => {
    load()
    const poll = setInterval(() => load(), POLL_INTERVAL)
    return () => clearInterval(poll)
  }, [load])

  // 이벤트 타임라인 폴링/state 제거 (2026-05-26 사용자 요구) — 활성 사이클 패널이 대체.

  if (loading || !stats) {
    return (
      <div style={{ color: '#888', textAlign: 'center', padding: '4rem' }}>
        대시보드 로딩 중...
      </div>
    )
  }

  const { product_stats, site_health, market_health, hourly_changes } = stats

  // 가로 바 차트 최대값
  const maxHourly = Math.max(...hourly_changes, 1)

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '1.25rem' }}>
      {/* 무신사 자동로그인계정 미설정/만료 경고 모달 */}
      {musinsaAuthMissing && !musinsaAuthDismissed && (
        <div style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.75)', backdropFilter: 'blur(4px)', zIndex: 9999, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
          <div style={{ background: '#1A1A1A', border: '2px solid #FF4444', borderRadius: '16px', padding: '2rem', maxWidth: '480px', width: '90%', boxShadow: '0 8px 32px rgba(255,68,68,0.3)', position: 'relative' }}>
            <button
              aria-label='알람 닫기'
              title='닫기'
              onClick={() => setMusinsaAuthDismissed(true)}
              style={{ position: 'absolute', top: '0.75rem', right: '0.75rem', width: '28px', height: '28px', display: 'flex', alignItems: 'center', justifyContent: 'center', background: 'transparent', border: 'none', borderRadius: '6px', color: '#AAA', fontSize: '1.25rem', fontWeight: 700, cursor: 'pointer', lineHeight: 1 }}
            >
              &#10005;
            </button>
            <div style={{ textAlign: 'center', marginBottom: '1.5rem' }}>
              <div style={{ fontSize: '3rem', marginBottom: '0.75rem' }}>&#9888;</div>
              <h3 style={{ fontSize: '1.25rem', fontWeight: 700, color: '#FF6B6B', marginBottom: '0.5rem' }}>무신사 원가 갱신 중단</h3>
              <p style={{ fontSize: '0.875rem', color: '#AAA', lineHeight: 1.5 }}>
                {musinsaAuthMissing.reason === 'cookie_expired'
                  ? <>자동로그인계정 <b style={{ color: '#FFD' }}>{musinsaAuthMissing.account_label}</b>의 쿠키가 만료됨. 무신사 재로그인 필요.</>
                  : musinsaAuthMissing.reason === 'no_cookie'
                  ? <>자동로그인계정 <b style={{ color: '#FFD' }}>{musinsaAuthMissing.account_label}</b>에 쿠키 없음. 무신사 로그인 필요.</>
                  : <>무신사 자동로그인계정 미설정. <b style={{ color: '#FFD' }}>설정 → 소싱처계정</b>에서 자동로그인 계정을 지정하세요.</>}
                <br/>
                <span style={{ color: '#FF8888' }}>cost 계산이 일관되지 않아 자동 갱신을 차단했습니다.</span>
              </p>
            </div>
            <div style={{ display: 'flex', gap: '0.5rem' }}>
              <button
                onClick={() => setMusinsaAuthDismissed(true)}
                style={{ flex: 1, padding: '0.75rem', background: 'transparent', border: '1px solid #444', borderRadius: '8px', color: '#AAA', fontSize: '0.9375rem', fontWeight: 600, cursor: 'pointer' }}
              >
                나중에
              </button>
              <button
                onClick={() => { window.location.href = '/samba/settings#sourcing-accounts-MUSINSA' }}
                style={{ flex: 2, padding: '0.75rem', background: '#FF4444', border: 'none', borderRadius: '8px', color: '#fff', fontSize: '0.9375rem', fontWeight: 700, cursor: 'pointer' }}
              >
                지금 설정하기
              </button>
            </div>
          </div>
        </div>
      )}

      {/* A. 상단 상태바 */}
      <div
        style={{
          ...card,
          padding: '0.75rem 1.25rem',
          display: 'flex',
          flexDirection: 'column',
          gap: '0.5rem',
          borderColor: '#FF8C00',
          borderWidth: '1px',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', flexWrap: 'wrap' }}>
            <span style={{ width: 8, height: 8, borderRadius: '50%', background: autotuneRunning ? '#51CF66' : '#FF6B6B', display: 'inline-block' }} />
            <span style={{ fontWeight: 700, color: '#FF8C00', fontSize: '0.875rem' }}>오토튠 모니터링</span>
            {!pcDeviceId && <span style={{ fontSize: '0.75rem', color: '#FFB020' }}>확장앱 미감지 (시크릿창/포크 — 본인 PC만 제어 가능)</span>}
            {pcDeviceId && autotuneRunning && <span style={{ fontSize: '0.75rem', color: '#51CF66' }}>실행 중</span>}
            {pcDeviceId && autotuneRunning && autotuneRestarts > 0 && <span style={{ fontSize: '0.75rem', color: '#FF6B6B' }}>재시작 {fmtNum(autotuneRestarts)}회</span>}
            {pcDeviceId && !autotuneRunning && <span style={{ fontSize: '0.75rem', color: '#FF6B6B' }}>정지</span>}
          </div>
          <div style={{ display: 'flex', gap: '0.5rem', fontSize: '0.8rem', color: '#888', alignItems: 'center' }}>
            <button
              onClick={() => { downloadDaemonInstaller(getOrCreateAutotuneDaemonDeviceId()) }}
              title="데몬 설치/재설치 — 미감지 배너 없어도 항상 다운로드 가능"
              style={{
                padding: '0.25rem 0.6rem',
                background: 'rgba(76,154,255,0.12)', border: '1px solid rgba(76,154,255,0.35)',
                borderRadius: '6px', color: '#4C9AFF', fontSize: '0.75rem', fontWeight: 600, cursor: 'pointer',
              }}
            >데몬 다운로드</button>
            <input
              type="text"
              placeholder="상품번호"
              value={singleProductNo}
              onChange={e => setSingleProductNo(e.target.value)}
              style={{
                width: '110px', padding: '0.25rem 0.5rem',
                background: '#1A1A1A', border: '1px solid #3D3D3D', borderRadius: '6px',
                color: '#E5E5E5', fontSize: '0.75rem', outline: 'none',
              }}
              onKeyDown={e => { if (e.key === 'Enter' && singleProductNo.trim()) document.getElementById('btn-autotune-start')?.click() }}
            />
            <button
            id="btn-autotune-start"
            disabled={!pcDeviceId}
            onClick={async () => {
              try {
                const { API_BASE_URL: apiBase } = await import('@/config/api')
                await fetchWithAuth(`${apiBase}/api/v1/samba/shipments/emergency-clear`, { method: 'POST' })
                const pno = singleProductNo.trim() || undefined
                const { getDeviceId } = await import('@/lib/samba/deviceId')
                // 확장앱 allowedSites 먼저 동기화 — 페이지 로드 후 체크박스 변경이 extension storage에
                // 반영됐는지 확실히 보장. null(전체선택) 포함 항상 명시 목록 전달
                syncAllowedSitesToExtension(filterSources === null ? [...availSources] : filterSources)
                // PC분담 먼저 등록 — 오토튠 첫 사이클에서 올바른 소싱처만 실행되도록 보장
                // (등록 없이 시작하면 첫 사이클에서 union=None → 전체 소싱처 루프 생성됨)
                await registerPcAllowedSites(filterSources === null ? [...availSources] : filterSources)
                const extDev = getDeviceId()
                const daemonDev = (typeof window !== 'undefined' && window.localStorage.getItem('samba.autotune.daemon.deviceId')) || ''
                // 확장앱 id 가 페이지에 안 꽂힌 PC(데몬 전용 원격 PC)에서도 데몬 사이클은 시작돼야 함.
                // 확장앱 id 있으면 확장앱 사이클 시작, 없으면 데몬 id 로만 진행 (2026-06-04 사고 fix).
                if (extDev) {
                  const res = await collectorApi.autotuneStart('registered', pno, extDev)
                  if (!res.ok) {
                    const { showAlert } = await import('@/components/samba/Modal')
                    showAlert(res.error || '시작 실패', 'error')
                    return
                  }
                } else if (!daemonDev) {
                  const { showAlert } = await import('@/components/samba/Modal')
                  showAlert('확장앱/데몬 미감지 — 시작 불가', 'error')
                  return
                }
                // (옵션 C) 데몬 device 도 사이클 시작 — 같은 PC 의 데몬 분담 사이트 (SSG/ABC/GS/LOTTEON) 사이클 트리거
                try {
                  if (daemonDev) {
                    const dres = await collectorApi.autotuneStart('registered', pno, daemonDev)
                    // 확장앱 id 없는 데몬 전용 PC 면 데몬 start 결과가 곧 시작 성공 여부
                    if (!dres.ok && !extDev) {
                      const { showAlert } = await import('@/components/samba/Modal')
                      showAlert(dres.error || '데몬 시작 실패', 'error')
                      return
                    }
                  }
                } catch { /* 데몬 사이클 시작 실패는 무시 */ }
                // 이 PC의 확장앱에만 폴링 합류 신호 전달 (다른 PC는 자동 편승 안 함)
                // sourceSites: null=전체, [...]=지정 소싱처 — 불필요한 pre-login 차단
                window.postMessage({ source: 'samba-page', type: 'AUTOTUNE_SET_JOIN', joined: true, sourceSites: filterSources }, window.location.origin)
                falseCountRef.current = 0
                setAutotuneRunning(true)
                setAutotuneCycles(0)
                if (pno) setSingleProductNo('')
                // 사용자 의도 저장 — 백엔드 재시작 시 자동 재등록 트리거용
                try {
                  window.localStorage.setItem('samba.autotune.userIntent', 'start')
                  // 정지 때 박아둔 24h autoRejoin 잠금 해제 — 사용자 명시 start 의도
                  window.localStorage.removeItem('samba.autotune.autoRejoinAt')
                } catch { /* ignore */ }
                autoRejoinAtRef.current = 0
              } catch { /* ignore */ }
            }}
            style={{
              padding: '0.25rem 0.75rem',
              background: pcDeviceId ? 'rgba(34,197,94,0.12)' : 'rgba(100,100,100,0.12)',
              border: `1px solid ${pcDeviceId ? 'rgba(34,197,94,0.35)' : 'rgba(100,100,100,0.35)'}`,
              borderRadius: '6px',
              color: pcDeviceId ? '#22C55E' : '#666',
              fontSize: '0.8125rem',
              fontWeight: 600,
              cursor: pcDeviceId ? 'pointer' : 'not-allowed',
            }}
            title={pcDeviceId ? '' : '확장앱/데몬 미감지 — 시크릿창에서는 제어 불가'}
          >시작</button>
          <button
            disabled={!pcDeviceId}
            onClick={async () => {
              const { showAlert } = await import('@/components/samba/Modal')
              try {
                const { API_BASE_URL: apiBase } = await import('@/config/api')
                const { getDeviceId } = await import('@/lib/samba/deviceId')
                const dev = getDeviceId()
                // device_id 누락 가드 — 시크릿창/확장앱 미설치 시 버튼 자체가 disabled.
                // 여기 도달 시 데몬 device 만 있는 케이스 → 정지 호출 불가 (확장앱 없음).
                if (!dev) {
                  showAlert('확장앱 device_id 미감지 — 시크릿창/확장앱 미설치에서는 정지 불가', 'error')
                  return
                }
                // 본인 PC만 정지 — 다른 PC는 영향 없음
                const r = await fetchWithAuth(`${apiBase}/api/v1/samba/collector/autotune/stop`, {
                  method: 'POST',
                  headers: { 'Content-Type': 'application/json' },
                  body: JSON.stringify({ device_id: dev }),
                })
                let data: { ok?: boolean; error?: string; status?: string } = {}
                try { data = await r.json() } catch { /* ignore */ }
                if (r.ok && data.ok !== false) {
                  window.postMessage({ source: 'samba-page', type: 'AUTOTUNE_SET_JOIN', joined: false }, window.location.origin)
                  setAutotuneRunning(false)
                  falseCountRef.current = 0
                  // 정지 = polling 중단만. 분담 mapping 은 유지 (다음 시작 시 체크박스로 덮어씀).
                  // 사용자 룰 (2026-05-26): "다시 시작할때 기준으로 체크박스 소싱처 처리".
                  // backend pick 단계에서 정지 PC 매칭 차단은 autotune_running_devices set 이 담당.
                  try {
                    window.localStorage.setItem('samba.autotune.userIntent', 'stop')
                    window.localStorage.removeItem('samba.autotune.autoRejoinAt')
                  } catch { /* ignore */ }
                  autoRejoinAtRef.current = 0
                  showAlert('이 PC 오토튠 정지 완료', 'success')
                } else if (r.ok && data.ok === false) {
                  showAlert(`정지 실패 — ${data.error || '백엔드 거절'}`, 'error')
                } else {
                  showAlert(`정지 요청 응답 ${r.status} — UI는 정지 상태로 동기화됨`, 'info')
                  setAutotuneRunning(false)
                  falseCountRef.current = 0
                }
              } catch {
                setAutotuneRunning(false)
                showAlert('정지 요청 실패 — 백엔드 연결 확인 필요', 'error')
              }
            }}
            style={{
              padding: '0.25rem 0.75rem',
              background: pcDeviceId ? 'rgba(239,68,68,0.12)' : 'rgba(100,100,100,0.12)',
              border: `1px solid ${pcDeviceId ? 'rgba(239,68,68,0.35)' : 'rgba(100,100,100,0.35)'}`,
              borderRadius: '6px',
              color: pcDeviceId ? '#EF4444' : '#666',
              fontSize: '0.8125rem',
              fontWeight: 600,
              cursor: pcDeviceId ? 'pointer' : 'not-allowed',
            }}
            title={pcDeviceId ? '' : '확장앱/데몬 미감지 — 시크릿창에서는 제어 불가'}
            >오토튠 정지</button>
          </div>
        </div>
        {/* 소싱처 체크박스 — device_id 없으면 (시크릿창) 숨김.
            null=전체체크 렌더가 다른 PC 분담을 침범하는 사고 차단(2026-05-25). */}
        {availSources.length > 0 && pcDeviceId && (
          <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', flexWrap: 'wrap' }}>
            <span style={{ fontSize: '0.75rem', color: '#9AA5C0', fontWeight: 600, whiteSpace: 'nowrap' }}>소싱처</span>
            {availSources.map(src => {
              const checked = filterSources === null || filterSources.includes(src)
              const labelMap: Record<string, string> = { MUSINSA: '무신사', KREAM: 'KREAM', DANAWA: '다나와', FashionPlus: '패션플러스', Nike: 'Nike', Adidas: 'Adidas', ABCmart: 'ABC마트', REXMONDE: '렉스몬드', SSG: 'SSG', LOTTEON: '롯데ON', GSShop: 'GSShop', ElandMall: '이랜드몰', SSF: 'SSF샵' }
              return (
                <label key={src} style={{ display: 'flex', alignItems: 'center', gap: '2px', cursor: 'pointer' }}>
                  <input
                    type="checkbox"
                    checked={checked}
                    onChange={() => toggleSource(src)}
                    style={{ accentColor: '#FF8C00', width: 13, height: 13, cursor: 'pointer' }}
                  />
                  <span style={{ fontSize: '0.7rem', color: checked ? '#ddd' : '#666', whiteSpace: 'nowrap' }}>{labelMap[src] || src}</span>
                </label>
              )
            })}
          </div>
        )}
        {/* 판매처 체크박스 (마켓 단위) — device_id 없으면 숨김 */}
        {availMarkets.length > 0 && pcDeviceId && (
          <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', flexWrap: 'wrap' }}>
            <span style={{ fontSize: '0.75rem', color: '#9AA5C0', fontWeight: 600, whiteSpace: 'nowrap' }}>판매처</span>
            {availMarkets.map(mt => {
              const checked = filterMarkets === null || filterMarkets.includes(mt)
              const marketLabel: Record<string, string> = { smartstore: '스마트스토어', coupang: '쿠팡', '11st': '11번가', auction: '옥션', gmarket: 'G마켓', lotteon: '롯데ON', lottehome: '롯데홈쇼핑', ssg: 'SSG', tmon: '티몬', wemakeprice: '위메프', kream: 'KREAM', playauto: '플레이오토', gsshop: 'GS샵', elandmall: '이랜드몰', ssf: 'SSF샵' }
              return (
                <label key={mt} style={{ display: 'flex', alignItems: 'center', gap: '2px', cursor: 'pointer' }}>
                  <input
                    type="checkbox"
                    checked={checked}
                    onChange={() => toggleMarket(mt)}
                    style={{ accentColor: '#4C9AFF', width: 13, height: 13, cursor: 'pointer' }}
                  />
                  <span style={{ fontSize: '0.7rem', color: checked ? '#ddd' : '#666', whiteSpace: 'nowrap' }}>{marketLabel[mt] || mt}</span>
                </label>
              )
            })}
          </div>
        )}
        {/* 수집인터벌 영역 제거 (2026-05-26 사용자 요구) — backend SITE_BASE_INTERVAL=0 하드코딩.
            차단 시 자동 backoff 로직은 refresher.py 안에 유지. */}
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', flexWrap: 'wrap', marginTop: '0.4rem' }}>
          <span style={{ fontSize: '0.75rem', color: '#9AA5C0', fontWeight: 600, whiteSpace: 'nowrap' }}>동시실행</span>
          {INTERVAL_SITES.map(({ key, label }) => (
            <span key={key} style={{ display: 'flex', alignItems: 'center', gap: '2px' }}>
              <span style={{ fontSize: '0.7rem', color: '#aaa', whiteSpace: 'nowrap' }}>{label}</span>
              <input
                type="text"
                inputMode="numeric"
                value={siteConcurrency[key] ?? ''}
                onChange={e => handleConcurrencyChange(key, e.target.value)}
                style={{
                  width: '2.5rem',
                  padding: '0.1rem 0.25rem',
                  background: 'rgba(255,255,255,0.06)',
                  border: '1px solid rgba(255,255,255,0.15)',
                  borderRadius: '4px',
                  color: '#4C9AFF',
                  fontSize: '0.75rem',
                  textAlign: 'center',
                  outline: 'none',
                }}
                onFocus={e => { e.target.style.borderColor = '#4C9AFF' }}
                onBlur={e => { e.target.style.borderColor = 'rgba(255,255,255,0.15)' }}
              />
            </span>
          ))}
          <span style={{ fontSize: '0.65rem', color: '#666' }}>병렬</span>
        </div>
      </div>

      {/* 오토튠 실시간 로그 (시작/강제중단 버튼 바로 아래) */}
      <AutotuneLogPanel
        onStatusChange={handleAutotuneStatus}
        externalRunning={autotuneRunning}
        filterSources={filterSources}
        deviceId={pcDeviceId}
      />

      {/* 활성 사이클 — backend in-memory 의 모든 (device, site) 진행 중 cycle.
          사용자 visibility 보완 (2026-05-26 요구) + 인지 못 한 사이클 개별 중단. */}
      <ActiveCyclesPanel />




    </div>
  )
}
