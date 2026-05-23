'use client'

import { useEffect, useRef, useState } from 'react'
import { fetchWithAuth, API_BASE } from '@/lib/samba/api/shared'

interface CollectQueueItem {
  id: string
  filter_name: string
  source_site: string
  started_at: string | null
  current: number
  total: number
}

interface QueueStatus {
  running: CollectQueueItem[]
  pending: CollectQueueItem[]
}

export function useCollectQueuePolling() {
  const [collectQueueStatus, setCollectQueueStatus] = useState<QueueStatus>({ running: [], pending: [] })
  const collectQueuePollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  useEffect(() => {
    const fetchStatus = async () => {
      try {
        const res = await fetchWithAuth(`${API_BASE}/api/v1/samba/jobs/collect-queue-status`)
        if (res.ok) {
          const data = await res.json() as QueueStatus
          setCollectQueueStatus(data)
        }
      } catch { /* 무시 */ }
    }
    // 마운트 동시 요청 폭주 방지 — 1.5초 지연 후 첫 폴링 시작
    // 다른 마운트 요청(load/loadTree/accountApi/proxyAuth)이 워커를 먼저 점유하지 않도록 양보
    const startTimer = setTimeout(() => {
      fetchStatus()
      collectQueuePollRef.current = setInterval(fetchStatus, 3000)
    }, 1500)
    return () => {
      clearTimeout(startTimer)
      if (collectQueuePollRef.current) clearInterval(collectQueuePollRef.current)
    }
  }, [])

  return collectQueueStatus
}
