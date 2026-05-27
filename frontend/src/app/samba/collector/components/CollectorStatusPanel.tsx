"use client"

import type { Dispatch, RefObject, SetStateAction } from 'react'
import { fetchWithAuth, API_BASE } from '@/lib/samba/api/shared'
import { fmtNum, fmtTextNumbers } from '@/lib/samba/styles'
import type { PoolInfo } from '../hooks/useProxyAuth'

// 인증/프록시 상태 타입
type StatusState = 'checking' | 'ok' | 'error'

// 수집 큐 상태 타입
type CollectQueueItem = { id: string; filter_name: string; source_site: string; started_at: string | null; current: number; total: number }
type CollectQueueStatus = {
  running: CollectQueueItem[]
  pending: CollectQueueItem[]
}

// 그룹명 파싱 결과 타입
type ParsedGroup = { brand: string; category: string }

// 상태 섹션 전용 props (section='status')
type StatusProps = {
  section: 'status'
  proxyStatus: StatusState
  proxyText: string
  musinsaAuth: StatusState
  musinsaAuthText: string
  musinsaCookieUpdatedAt?: string | null
  poolInfo?: PoolInfo
  setProxyStatus: Dispatch<SetStateAction<StatusState>>
  setProxyText: Dispatch<SetStateAction<string>>
}

// 쿠키 갱신 시각 → 상대시간 문자열 + 색상
// 5분 미만: 회색 '방금 갱신', 24시간 미만: 회색 'N분/시간 전 갱신', 24시간 이상: 주황 'N일 전 갱신'
function formatCookieFreshness(iso: string | null | undefined): { text: string; color: string } | null {
  if (!iso) return null
  const ts = Date.parse(iso)
  if (Number.isNaN(ts)) return null
  const diffSec = Math.max(0, Math.floor((Date.now() - ts) / 1000))
  if (diffSec < 300) return { text: '방금 갱신', color: '#8A95B0' }
  const diffMin = Math.floor(diffSec / 60)
  if (diffMin < 60) return { text: `${diffMin}분 전 갱신`, color: '#8A95B0' }
  const diffHour = Math.floor(diffMin / 60)
  if (diffHour < 24) return { text: `${diffHour}시간 전 갱신`, color: '#8A95B0' }
  const diffDay = Math.floor(diffHour / 24)
  return { text: `${diffDay}일 전 갱신`, color: '#FAB005' }
}

// 로그 섹션 전용 props (section='log')
type LogProps = {
  section: 'log'
  collectLog: string[]
  collecting: boolean
  collectQueueStatus: CollectQueueStatus
  cancellingJobIds: string[]
  logRef: RefObject<HTMLDivElement | null>
  handleStopCollect: () => void | Promise<void>
  handleCancelCollectJob: (jobId: string) => void
  handleCopyLog: () => void
  handleClearLog: () => void
  parseGroupName?: (name: string, site: string) => ParsedGroup
}

type Props = StatusProps | LogProps

export default function CollectorStatusPanel(props: Props) {
  // 프록시 + 무신사 인증 상태 섹션
  if (props.section === 'status') {
    const {
      proxyStatus,
      proxyText,
      musinsaAuth,
      musinsaAuthText,
      musinsaCookieUpdatedAt,
      poolInfo,
      setProxyStatus,
      setProxyText,
    } = props
    const cookieFresh = musinsaAuth === 'ok' ? formatCookieFreshness(musinsaCookieUpdatedAt) : null

    // write/read pool_max 분리 — 같은 값으로 표시하면 read(실제 30) 오인 유발
    const wPoolMax = poolInfo?.write_pool_max ?? poolInfo?.write?.pool_max ?? poolInfo?.pool_max ?? 60
    const rPoolMax = poolInfo?.read_pool_max ?? poolInfo?.read?.pool_max ?? 30
    const wPg = poolInfo?.write?.pg
    const rPg = poolInfo?.read?.pg
    const wTotal = wPg?.total ?? 0
    const rTotal = rPg?.total ?? 0
    // 백엔드 SQLAlchemy 풀 실제 점유 (이게 진짜 풀 사용량 — DB 전체 세션과 비교 금지)
    const wCheckedOut = poolInfo?.write?.checkedout ?? 0
    const rCheckedOut = poolInfo?.read?.checkedout ?? 0
    // IIT 임계 — 단순 카운트는 BEGIN 직후 정상 트랜잭션도 잡혀 false positive.
    // age >= 30s 좀비(iit_zombie) 기반으로 빨강/노랑 판단.
    const wZombie = wPg?.iit_zombie ?? 0
    const rZombie = rPg?.iit_zombie ?? 0
    const maxZombie = Math.max(wZombie, rZombie)
    // 빨강 기준: 실제 백엔드 풀 점유율 또는 좀비 (DB 전체 세션 totals 는 다른 컨테이너/cron 포함이라 무관)
    const wPoolRatio = wPoolMax > 0 ? wCheckedOut / wPoolMax : 0
    const rPoolRatio = rPoolMax > 0 ? rCheckedOut / rPoolMax : 0
    const poolStatusColor = (wPoolRatio >= 1 || rPoolRatio >= 1 || maxZombie >= 5)
      ? '#FF6B6B'
      : (wPoolRatio >= 0.85 || rPoolRatio >= 0.85 || maxZombie >= 2)
        ? '#FAB005'
        : '#51CF66'
    const poolCellColor = (ratio: number) =>
      ratio >= 1 ? '#FF6B6B' : ratio >= 0.85 ? '#FAB005' : '#C4CAD8'
    const iitCellColor = (zombie: number) =>
      zombie >= 5 ? '#FF6B6B' : zombie >= 2 ? '#FAB005' : '#C4CAD8'

    return (
      <div style={{ marginBottom: '12px', display: 'flex', flexDirection: 'column', gap: '8px' }}>
        {/* 프록시 + 무신사 인증 상태 */}
        <div style={{
          display: 'flex', alignItems: 'center', gap: '16px', padding: '6px 14px',
          borderRadius: '8px', background: 'rgba(255,140,0,0.07)', border: '1px solid rgba(255,140,0,0.2)',
          fontSize: '0.78rem',
        }}>
          <span style={{ width: '8px', height: '8px', borderRadius: '50%', flexShrink: 0,
            background: proxyStatus === 'ok' ? '#51CF66' : proxyStatus === 'error' ? '#FF6B6B' : '#555',
          }} />
          <span style={{ color: proxyStatus === 'ok' ? '#51CF66' : '#888' }}>{proxyText}</span>
          <span style={{ color: '#2D2D2D' }}>|</span>
          <span style={{ width: '8px', height: '8px', borderRadius: '50%', flexShrink: 0,
            background: musinsaAuth === 'ok' ? '#51CF66' : musinsaAuth === 'error' ? '#FF6B6B' : '#555',
          }} />
          <span style={{ color: musinsaAuth === 'ok' ? '#51CF66' : '#888' }}>{musinsaAuthText}</span>
          {cookieFresh && (
            <span style={{ color: cookieFresh.color, fontSize: '0.72rem' }}>· {cookieFresh.text}</span>
          )}
          <button
            onClick={() => {
              setProxyStatus('checking')
              setProxyText('프록시 서버 확인 중...')
              fetchWithAuth(`${API_BASE}/api/v1/samba/collector/proxy-status`)
                .then(r => r.json())
                .then(data => {
                  if (data.status === 'ok') { setProxyStatus('ok'); setProxyText(data.message || '프록시 서버 정상 작동 중') }
                  else { setProxyStatus('error'); setProxyText(data.message || '프록시 서버 연결 실패') }
                })
                .catch(() => { setProxyStatus('error'); setProxyText('백엔드 서버 연결 실패') })
            }}
            style={{
              marginLeft: 'auto', background: 'transparent', border: '1px solid #3D3D3D',
              color: '#888', padding: '2px 10px', borderRadius: '4px', fontSize: '0.72rem', cursor: 'pointer',
            }}
          >재확인</button>
        </div>

        {/* DB 커넥션 풀 테이블 */}
        {poolInfo && wPg && rPg && (
          <div style={{
            borderRadius: '8px', overflow: 'hidden',
            border: `1px solid ${poolStatusColor === '#FF6B6B' ? 'rgba(255,107,107,0.4)' : poolStatusColor === '#FAB005' ? 'rgba(250,176,5,0.3)' : 'rgba(81,207,102,0.2)'}`,
            background: 'rgba(8,10,16,0.6)', fontSize: '0.78rem',
          }}>
            <table style={{ width: '100%', borderCollapse: 'collapse' }}>
              <thead>
                <tr style={{ background: 'rgba(255,255,255,0.04)' }}>
                  <th style={{ padding: '6px 14px', textAlign: 'left', color: '#9AA5C0', fontWeight: 600, borderBottom: '1px solid #1C1E2A' }}>상태</th>
                  <th style={{ padding: '6px 14px', textAlign: 'center', color: '#9AA5C0', fontWeight: 600, borderBottom: '1px solid #1C1E2A' }}>Write DB</th>
                  <th style={{ padding: '6px 14px', textAlign: 'center', color: '#9AA5C0', fontWeight: 600, borderBottom: '1px solid #1C1E2A' }}>Read DB</th>
                </tr>
              </thead>
              <tbody>
                {/* 백엔드 SQLAlchemy 풀 실제 점유 — 이게 진짜 "풀 꽉참" 지표 */}
                <tr style={{ background: 'rgba(81,207,102,0.04)', borderBottom: '1px solid rgba(28,30,42,0.8)' }}>
                  <td style={{ padding: '6px 14px', color: '#C4CAD8', fontWeight: 700 }}>
                    백엔드 풀 점유
                    <span style={{ marginLeft: 8, fontSize: '0.7rem', color: '#6A7388', fontWeight: 400 }}>
                      (이 값이 풀 최대 넘으면 진짜 꽉참)
                    </span>
                  </td>
                  <td style={{ padding: '6px 14px', textAlign: 'center', color: poolCellColor(wPoolRatio), fontWeight: 700, fontVariantNumeric: 'tabular-nums' }}>
                    {fmtNum(wCheckedOut)} / {fmtNum(wPoolMax)} ({Math.round(wPoolRatio * 100)}%)
                  </td>
                  <td style={{ padding: '6px 14px', textAlign: 'center', color: poolCellColor(rPoolRatio), fontWeight: 700, fontVariantNumeric: 'tabular-nums' }}>
                    {fmtNum(rCheckedOut)} / {fmtNum(rPoolMax)} ({Math.round(rPoolRatio * 100)}%)
                  </td>
                </tr>
                {([
                  { label: 'active', wVal: wPg.active ?? 0, rVal: rPg.active ?? 0, type: 'normal' as const },
                  { label: 'idle in transaction', wVal: wPg.idle_in_transaction ?? 0, rVal: rPg.idle_in_transaction ?? 0, type: 'iit' as const },
                  { label: 'idle', wVal: wPg.idle ?? 0, rVal: rPg.idle ?? 0, type: 'normal' as const },
                ]).map((row) => (
                  <tr key={row.label} style={{ borderBottom: '1px solid rgba(28,30,42,0.8)' }}>
                    <td style={{ padding: '5px 14px', color: '#8A95B0' }}>
                      {row.label}
                      {row.type === 'iit' && (
                        <span style={{ marginLeft: 8, fontSize: '0.7rem', color: '#6A7388' }}>
                          (좀비 ≥30s: W {fmtNum(wZombie)} / R {fmtNum(rZombie)})
                        </span>
                      )}
                    </td>
                    <td style={{ padding: '5px 14px', textAlign: 'center', color: row.type === 'iit' ? iitCellColor(wZombie) : '#C4CAD8', fontVariantNumeric: 'tabular-nums' }}>{fmtNum(row.wVal)}개</td>
                    <td style={{ padding: '5px 14px', textAlign: 'center', color: row.type === 'iit' ? iitCellColor(rZombie) : '#C4CAD8', fontVariantNumeric: 'tabular-nums' }}>{fmtNum(row.rVal)}개</td>
                  </tr>
                ))}
                <tr style={{ borderTop: '1px solid #2D3040', background: 'rgba(255,255,255,0.02)' }}>
                  <td style={{ padding: '6px 14px', color: '#8A95B0' }}>
                    DB 전체 세션
                    <span style={{ marginLeft: 8, fontSize: '0.7rem', color: '#6A7388' }}>
                      (백엔드 + cron + admin + 다른 컨테이너 합산 — 풀 최대와 비교 X)
                    </span>
                  </td>
                  <td style={{ padding: '6px 14px', textAlign: 'center', color: '#8A95B0', fontVariantNumeric: 'tabular-nums' }}>{fmtNum(wTotal)}개</td>
                  <td style={{ padding: '6px 14px', textAlign: 'center', color: '#8A95B0', fontVariantNumeric: 'tabular-nums' }}>{fmtNum(rTotal)}개</td>
                </tr>
              </tbody>
            </table>
          </div>
        )}
      </div>
    )
  }

  // 로그현황 섹션
  const {
    collectLog,
    collecting,
    collectQueueStatus,
    cancellingJobIds,
    logRef,
    handleStopCollect,
    handleCancelCollectJob,
    handleCopyLog,
    handleClearLog,
  } = props
  const { running, pending } = collectQueueStatus
  const hasJobs = running.length > 0 || pending.length > 0
  return (
    <>
      {/* 수집 잡 진행상황 섹션 */}
      {hasJobs && (
        <div style={{ background: 'rgba(8,10,16,0.98)', border: '1px solid #1C1E2A', borderRadius: '8px', marginBottom: '8px', overflow: 'hidden' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', padding: '8px 14px', background: '#0A0D14', borderBottom: '1px solid #1C1E2A' }}>
            <span style={{ width: '6px', height: '6px', borderRadius: '50%',
              background: running.length > 0 ? '#51CF66' : '#FAB005' }} />
            <span style={{ fontSize: '0.82rem', fontWeight: 600, color: '#9AA5C0' }}>
              수집 잡 진행상황
              {running.length > 0 && ` — 수집 중 ${fmtNum(running.length)}건`}
              {pending.length > 0 && `${running.length > 0 ? ' · ' : ' — '}대기 ${fmtNum(pending.length)}건`}
            </span>
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '4px', padding: '8px 14px' }}>
            {running.map((j, idx) => {
              const started = j.started_at ? new Date(j.started_at) : null
              const startedStr = started
                ? `${String(started.getHours()).padStart(2,'0')}:${String(started.getMinutes()).padStart(2,'0')}:${String(started.getSeconds()).padStart(2,'0')}`
                : '-'
              const pct = j.total > 0 ? Math.floor((j.current / j.total) * 100) : 0
              const busy = cancellingJobIds.includes(j.id)
              return (
                <div key={`rc-${j.id || idx}`} style={{ display: 'flex', alignItems: 'center', gap: '10px', fontSize: '0.75rem', color: '#C4CAD8' }}>
                  <span style={{ color: '#51CF66', fontWeight: 600, minWidth: '40px' }}>수집중</span>
                  <span style={{ color: '#8A95B0', minWidth: '72px' }}>시작 {startedStr}</span>
                  <span style={{ color: '#7BB0FF', minWidth: '64px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{j.source_site}</span>
                  <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{j.filter_name || '—'}</span>
                  <span style={{ color: '#9AA5C0', minWidth: '110px', textAlign: 'right' }}>
                    {j.total > 0 ? `${fmtNum(j.current)} / ${fmtNum(j.total)} (${pct}%)` : '—'}
                  </span>
                  <button
                    onClick={() => handleCancelCollectJob(j.id)}
                    disabled={busy}
                    style={{ padding: '2px 8px', fontSize: '0.7rem', background: busy ? 'rgba(255,80,80,0.3)' : 'rgba(255,80,80,0.12)', color: '#FF6B6B', border: '1px solid rgba(255,80,80,0.4)', borderRadius: '3px', cursor: busy ? 'not-allowed' : 'pointer', fontWeight: 600, minWidth: '44px' }}
                  >{busy ? '취소중' : '취소'}</button>
                </div>
              )
            })}
            {pending.map((j, idx) => {
              const busy = cancellingJobIds.includes(j.id)
              return (
                <div key={`pc-${j.id || idx}`} style={{ display: 'flex', alignItems: 'center', gap: '10px', fontSize: '0.75rem', color: '#8A95B0' }}>
                  <span style={{ color: '#FAB005', fontWeight: 600, minWidth: '40px' }}>대기</span>
                  <span style={{ minWidth: '72px' }}>—</span>
                  <span style={{ color: '#7BB0FF', minWidth: '64px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{j.source_site}</span>
                  <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{j.filter_name || '—'}</span>
                  <span style={{ minWidth: '110px', textAlign: 'right' }}>—</span>
                  <button
                    onClick={() => handleCancelCollectJob(j.id)}
                    disabled={busy}
                    style={{ padding: '2px 8px', fontSize: '0.7rem', background: busy ? 'rgba(255,80,80,0.3)' : 'rgba(255,80,80,0.12)', color: '#FF6B6B', border: '1px solid rgba(255,80,80,0.4)', borderRadius: '3px', cursor: busy ? 'not-allowed' : 'pointer', fontWeight: 600, minWidth: '44px' }}
                  >{busy ? '취소중' : '취소'}</button>
                </div>
              )
            })}
          </div>
        </div>
      )}

      {/* 로그현황 */}
      <div style={{
        background: "rgba(30,30,30,0.5)", border: "1px solid #2D2D2D", borderRadius: "8px",
        overflow: "hidden", marginBottom: "1rem",
      }}>
        <div style={{
          padding: "8px 16px", borderBottom: "1px solid #2D2D2D",
          display: "flex", alignItems: "center", justifyContent: "space-between",
        }}>
          <span style={{ fontSize: "0.85rem", fontWeight: 600, color: "#C5C5C5" }}>로그현황</span>
          <div style={{ display: "flex", gap: "4px" }}>
            {collecting && (
              <button onClick={handleStopCollect} style={{
                fontSize: "0.75rem", color: "#FF6B6B", background: "rgba(255,100,100,0.1)",
                border: "1px solid rgba(255,100,100,0.4)", padding: "2px 10px", borderRadius: "4px", cursor: "pointer",
              }}>수집 중단</button>
            )}
            <button onClick={handleCopyLog} style={{
              fontSize: "0.75rem", color: "#888", background: "transparent",
              border: "1px solid #3D3D3D", padding: "2px 10px", borderRadius: "4px", cursor: "pointer",
            }}>복사</button>
            <button onClick={handleClearLog} style={{
              fontSize: "0.75rem", color: "#888", background: "transparent",
              border: "1px solid #3D3D3D", padding: "2px 10px", borderRadius: "4px", cursor: "pointer",
            }}>초기화</button>
          </div>
        </div>
        <div
          ref={logRef}
          style={{
            height: "160px", overflowY: "auto", padding: "10px 16px",
            fontFamily: "monospace", fontSize: "0.78rem", color: "#8A95B0", zoom: "0.7",
            background: "#080A10", lineHeight: 1.6,
          }}
        >
          {collectLog.map((line, i) => (
            <p key={i} style={{
              color: line.includes("완료") ? "#51CF66"
                : line.includes("실패") || line.includes("오류") ? "#FF6B6B"
                : line.includes("대기") || line.includes("초기화") ? "#555"
                : "#8A95B0",
              margin: 0,
            }}>
              {fmtTextNumbers(line)}
            </p>
          ))}
        </div>
      </div>
    </>
  )
}
