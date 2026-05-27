'use client'

import { Dispatch, SetStateAction, useState } from 'react'
import { orderApi, type SambaMarketAccount } from '@/lib/samba/api/commerce'
import { jobApi } from '@/lib/samba/api/operations'
import { fmtNum } from '@/lib/samba/styles'
import { fmtTime } from '@/lib/samba/utils'

interface UseOrderSyncArgs {
  accounts: SambaMarketAccount[]
  period: string
  customStart?: string
  customEnd?: string
  setLogMessages: Dispatch<SetStateAction<string[]>>
  showNotification: (message: string, type?: string) => void
  loadOrders: () => void | Promise<void>
}

export function useOrderSync({ accounts, period, customStart, customEnd, setLogMessages, showNotification, loadOrders }: UseOrderSyncArgs) {
  const [syncing, setSyncing] = useState(false)
  const [syncAccountId, setSyncAccountId] = useState('')
  const [backgroundMode, setBackgroundMode] = useState(true)

  const handleFetch = async () => {
    setSyncing(true)

    const ts = () => fmtTime()
    const daysMap: Record<string, number> = {
      yesterday: 1,
      today: 1,
      thisweek: 7,
      lastweek: 14,
      '5days': 5,
      '1week': 7,
      '15days': 15,
      thismonth: 31,
      lastmonth: 60,
      '1month': 30,
      '3months': 90,
      '6months': 180,
      thisyear: Math.ceil((Date.now() - new Date(new Date().getFullYear(), 0, 1).getTime()) / 86400000) + 1,
      all: 365,
    }
    // customStart/End 가 들어와있으면 항상 백엔드에 그대로 전달 (프리셋 days 무시).
    // 사용자가 날짜 input을 바꿔도 period state가 그대로 'today'에 남아 days=1로
    // 박히던 버그 보완 — 화면 날짜 입력 = 사용자의 명시적 의도.
    let days = daysMap[period] || 7
    let payloadStart: string | undefined
    let payloadEnd: string | undefined
    if (customStart && customEnd) {
      const sd = new Date(customStart)
      const ed = new Date(customEnd)
      if (!isNaN(sd.getTime()) && !isNaN(ed.getTime()) && ed >= sd) {
        days = Math.max(1, Math.ceil((ed.getTime() - sd.getTime()) / 86400000) + 1)
        payloadStart = customStart.replace(/-/g, '')
        payloadEnd = customEnd.replace(/-/g, '')
      }
    }

    const runBackgroundSync = async (accountIds?: string[]) => {
      try {
        const payload: Record<string, unknown> = { days }
        if (accountIds && accountIds.length > 0) payload.account_ids = accountIds
        if (payloadStart && payloadEnd) {
          payload.start_date = payloadStart
          payload.end_date = payloadEnd
        }

        const created = await jobApi.create({ job_type: 'order_sync', payload })
        const jobId = created.id
        const reused = created.duplicate
        setLogMessages(prev => [...prev, `[${ts()}] 백그라운드 주문수집 ${reused ? '재연결' : '시작'} (${jobId.slice(0, 12)}...)`])

        let logSince = 0
        let done = false

        while (!done) {
          await new Promise(resolve => setTimeout(resolve, 2000))

          try {
            const logsRes = await jobApi.jobLogs(jobId, logSince)
            if (logsRes.logs.length > 0) {
              setLogMessages(prev => [...prev, ...logsRes.logs])
              logSince += logsRes.logs.length
            }

            const job = await jobApi.get(jobId)
            const status = job.status
            if (status === 'completed' || status === 'failed' || status === 'cancelled') {
              // 잡 종료 직전 백엔드가 추가한 결과 로그(계정별 success/error/skip + "전체마켓 주문수집 완료") 누락 방지
              try {
                const finalLogs = await jobApi.jobLogs(jobId, logSince)
                if (finalLogs.logs.length > 0) {
                  setLogMessages(prev => [...prev, ...finalLogs.logs])
                  logSince += finalLogs.logs.length
                }
              } catch { /* 최종 로그 폴링 실패는 무시 */ }
              setLogMessages(prev => [...prev, `[${ts()}] ${status === 'completed' ? '주문수집 완료' : status === 'failed' ? '주문수집 실패' : '주문수집 취소'}`])
              done = true
            }
          } catch {
            // 폴링 실패는 일시적 네트워크/DB 풀 압박 — 다음 사이클에 자동 재시도, 로그 출력 생략
          }
        }
      } catch (e) {
        setLogMessages(prev => [...prev, `[${ts()}] 백그라운드 주문수집 시작 실패: ${e instanceof Error ? e.message : String(e)}`])
      } finally {
        await loadOrders()
        try {
          const { count, by_fault } = await orderApi.getCancelAlertCount()
          if (count > 0) {
            // 귀책별 분리 표시 (#246 PR-6) — 운영자 우선순위 판단 도움
            const cust = by_fault?.customer ?? 0
            const nonCust = by_fault?.non_customer ?? 0
            const detail = (cust > 0 || nonCust > 0)
              ? ` (구매자 사유 ${fmtNum(cust)}건 / 판매자·쿠팡 사유 ${fmtNum(nonCust)}건)`
              : ''
            showNotification(`처리 중인 주문 중 취소요청이 ${fmtNum(count)}건 있습니다.${detail} 확인해 주세요.`)
          }
        } catch { /* 알람 조회 실패는 무시 */ }
        setSyncing(false)
      }
    }

    const isAll = !syncAccountId
    const isMarketGroup = syncAccountId.startsWith('type:')

    if (isMarketGroup) {
      const marketType = syncAccountId.replace('type:', '')
      const marketAccs = accounts.filter(account => account.market_type === marketType)
      const marketName = marketAccs[0]?.market_name || marketType
      setLogMessages(prev => [...prev, `[${ts()}] ${marketName} 주문수집 시작 (${fmtNum(marketAccs.length)}개 계정, 최근 ${days}일)...`])
      await runBackgroundSync(marketAccs.map(account => account.id))
      return
    }

    if (backgroundMode && isAll) {
      setLogMessages(prev => [...prev, `[${ts()}] 전체마켓 주문수집 시작 (${fmtNum(accounts.length)}개 계정, 최근 ${days}일)...`])
      await runBackgroundSync(accounts.map(account => account.id))
      return
    }

    const account = accounts.find(item => item.id === syncAccountId)
    const label = account ? `${account.market_name}(${account.seller_id || '-'})` : syncAccountId

    // backgroundMode: Caddy 120초 타임아웃 우회 — 특정 계정도 background job 경로 사용
    if (backgroundMode && !isAll) {
      setLogMessages(prev => [...prev, `[${ts()}] ${label} 주문수집 시작 (최근 ${days}일)...`])
      await runBackgroundSync([syncAccountId])
      return
    }

    setLogMessages(prev => [...prev, `[${ts()}] ${label} 주문수집 시작 (최근 ${days}일)...`])

    try {
      const res = await orderApi.syncFromMarkets(days, syncAccountId, payloadStart, payloadEnd)

      for (const result of res.results) {
        if (result.status === 'success') {
          const confirmed = (result as Record<string, unknown>).confirmed
          setLogMessages(prev => [
            ...prev,
            `[${ts()}] ${result.account}: ${fmtNum(result.fetched)}건 조회, ${fmtNum(result.synced)}건 신규 저장${confirmed ? `, ${fmtNum(confirmed as number)}건 발주확인` : ''}`,
          ])
        } else if (result.status === 'skip') {
          setLogMessages(prev => [...prev, `[${ts()}] ${result.account}: ${result.message}`])
        } else {
          setLogMessages(prev => [...prev, `[${ts()}] ${result.account}: 오류 - ${result.message}`])
        }
      }

      setLogMessages(prev => [...prev, `[${ts()}] 주문수집 완료 - 총 ${fmtNum(res.total_synced)}건 신규 저장`])

      let totalCancelRequested = 0
      for (const result of res.results) {
        totalCancelRequested += ((result as Record<string, unknown>).cancel_requested as number) || 0
      }
      if (totalCancelRequested > 0) {
        showNotification(`주문 취소요청 ${fmtNum(totalCancelRequested)}건이 있습니다. 반품교환 탭에서도 확인해 주세요.`)
      }
    } catch (e) {
      setLogMessages(prev => [...prev, `[${ts()}] 오류: ${e instanceof Error ? e.message : String(e)}`])
    } finally {
      await loadOrders()
      setSyncing(false)
    }
  }

  return {
    syncing,
    syncAccountId,
    setSyncAccountId,
    backgroundMode,
    setBackgroundMode,
    handleFetch,
  }
}
