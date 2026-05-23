'use client'

import { Dispatch, SetStateAction } from 'react'
import { collectorApi, type SambaSearchFilter } from '@/lib/samba/api/commerce'
import { showAlert, showConfirm } from '@/components/samba/Modal'
import { fmtNum } from '@/lib/samba/styles'

export interface DeleteGroupsArgs {
  displayedFilters: SambaSearchFilter[]
  selectedIds: Set<string>
  drillBrand: string | null
  filters: SambaSearchFilter[]
  siteFilter: string
  setDeleteJobLogs: Dispatch<SetStateAction<string[]>>
  setDeleteJobDone: Dispatch<SetStateAction<boolean>>
  setDeleteJobModal: Dispatch<SetStateAction<boolean>>
  setSelectedIds: Dispatch<SetStateAction<Set<string>>>
  setSelectAll: Dispatch<SetStateAction<boolean>>
  load: () => void | Promise<void>
  loadTree: () => void | Promise<void>
}

export async function performDeleteSelectedGroups(args: DeleteGroupsArgs) {
  const {
    displayedFilters, selectedIds, drillBrand, filters, siteFilter,
    setDeleteJobLogs, setDeleteJobDone, setDeleteJobModal,
    setSelectedIds, setSelectAll, load, loadTree,
  } = args
  const displayedIds = new Set(displayedFilters.map(f => f.id))
  const baseIds = selectedIds.size > 0
    ? new Set([...selectedIds].filter(id => displayedIds.has(id)))
    : displayedIds
  if (baseIds.size === 0) {
    showAlert(`삭제 대상이 없습니다. (selectedIds=${fmtNum(selectedIds.size)}, displayed=${fmtNum(displayedFilters.length)}, drillBrand=${drillBrand || '없음'})`)
    return
  }

  const allIds = new Set(baseIds)
  const findChildren = (parentId: string) => {
    for (const f of filters) {
      if (f.parent_id === parentId && !allIds.has(f.id)) {
        if (siteFilter && f.source_site && f.source_site !== siteFilter) continue
        allIds.add(f.id)
        findChildren(f.id)
      }
    }
  }
  for (const id of baseIds) findChildren(id)

  const childCount = allIds.size - baseIds.size
  const label = selectedIds.size > 0 ? '선택된' : '표시된'
  const msg = childCount > 0
    ? `${label} ${fmtNum(baseIds.size)}개 + 하위 ${fmtNum(childCount)}개 (총 ${fmtNum(allIds.size)}개) 그룹과 상품을 모두 삭제하시겠습니까?`
    : `${label} ${fmtNum(baseIds.size)}개 그룹과 그룹 내 상품을 모두 삭제하시겠습니까?`
  if (!await showConfirm(msg)) return

  const allIdsArr = [...allIds]
  const nameMap = new Map(filters.map(f => [f.id, f.name]))
  setDeleteJobLogs([`🗑️ 총 ${fmtNum(allIdsArr.length)}개 그룹 삭제 시작...`])
  setDeleteJobDone(false)
  setDeleteJobModal(true)

  let doneCount = 0
  let skipCount = 0
  for (const id of allIdsArr) {
    const groupName = nameMap.get(id) || id
    setDeleteJobLogs(prev => [...prev, `[${fmtNum(doneCount + skipCount + 1)}/${fmtNum(allIdsArr.length)}] "${groupName}" 처리 중...`])
    let shouldSkip = false
    try {
      const res = await collectorApi.scrollProducts({ skip: 0, limit: 10000, search_filter_id: id })
      // 백엔드와 동일하게 registered_accounts 기준으로 마켓등록 여부 판별
      const registered = res.items.filter(p => {
        const accs = (p as unknown as Record<string, unknown>).registered_accounts
        if (Array.isArray(accs)) return accs.length > 0
        if (typeof accs === 'string') return accs !== '[]' && accs !== 'null' && accs !== ''
        return accs != null
      })
      if (registered.length > 0) {
        setDeleteJobLogs(prev => [...prev, `  ⚠️ 마켓등록 상품 ${fmtNum(registered.length)}건 — 삭제 건너뜀`])
        skipCount++
        shouldSkip = true
      } else {
        const productIds = res.items.map(p => p.id)
        if (productIds.length > 0) {
          setDeleteJobLogs(prev => [...prev, `  상품 ${fmtNum(productIds.length)}건 삭제 중...`])
          await collectorApi.bulkDeleteProducts(productIds)
        }
      }
    } catch { /* 상품 조회 실패 시 deleteFilter에서 재처리 */ }
    if (shouldSkip) continue
    try {
      await collectorApi.deleteFilter(id)
      doneCount++
      setDeleteJobLogs(prev => [...prev, `  ✅ 삭제 완료`])
    } catch (e) {
      skipCount++
      const msg = e instanceof Error ? e.message : '삭제 실패'
      setDeleteJobLogs(prev => [...prev, `  ❌ 삭제 실패: ${msg}`])
    }
  }

  setDeleteJobLogs(prev => [...prev, ``, `🎉 완료 — ${fmtNum(doneCount)}개 삭제${skipCount > 0 ? `, ${fmtNum(skipCount)}개 건너뜀` : ''}`])
  setDeleteJobDone(true)
  setSelectedIds(new Set())
  setSelectAll(false)
  load(); loadTree()
}

