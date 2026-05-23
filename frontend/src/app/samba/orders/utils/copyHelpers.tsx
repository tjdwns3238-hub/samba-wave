'use client'

import React from 'react'
import { showAlert } from '@/components/samba/Modal'

const copyableTextStyle: React.CSSProperties = {
  color: '#E5E5E5',
  cursor: 'copy',
  textDecoration: 'underline',
  textDecorationColor: 'rgba(229, 229, 229, 0.35)',
  textUnderlineOffset: '2px',
}

const handleCopyText = async (value: string | null | undefined) => {
  let text = (value || '').trim()
  text = text.replace(/\([^)]*\)/g, '').trim()
  if (!text) {
    showAlert('복사할 내용이 없습니다', 'info')
    return
  }
  try {
    await navigator.clipboard.writeText(text)
  } catch {
    showAlert('복사에 실패했습니다', 'error')
  }
}

export const renderCopyableText = (
  value: string | null | undefined,
  _label?: string,
  style?: React.CSSProperties,
): React.ReactNode => {
  const text = value || '-'
  return (
    <span
      role="button"
      tabIndex={0}
      title="Copy"
      onClick={() => handleCopyText(value)}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          handleCopyText(value)
        }
      }}
      style={{ ...copyableTextStyle, ...style }}
    >
      {text}
    </span>
  )
}

// 우선순위:
//  1) 백엔드가 분리 저장한 detail 컬럼 그대로 사용 (롯데ON/스마트스토어/쿠팡/11번가 신규 데이터)
//  2) 기존 누적 데이터(공백 join 단일 문자열)는 휴리스틱으로 fallback 분리
//     - 첫 번째 `)` 가 우편번호 닫는 괄호일 수 있어 마지막 `)` 우선
//     - 콤마 있으면 콤마 기준
//     - 동/호/층/호실 패턴 fallback
export const splitCustomerAddress = (
  address: string | null | undefined,
  detailColumn?: string | null,
): { base: string; detail: string } => {
  const normalized = (address || '').trim().replace(/\s+/g, ' ')
  const detailFromDb = (detailColumn || '').trim().replace(/\s+/g, ' ')

  if (!normalized && !detailFromDb) return { base: '', detail: '' }

  // 1) 백엔드 분리 저장 컬럼 우선
  if (detailFromDb) {
    // 안전망 — 과거 파서가 `(법정동, 건물명)` 안 콤마로 잘라 저장한 케이스 자동 복구.
    // base에 `(` 만 남고 detail이 `)` 로 시작하면 재조립 후 마지막 `)` 기준 재분리.
    const baseOpens = (normalized.match(/\(/g) || []).length
    const baseCloses = (normalized.match(/\)/g) || []).length
    const detailOpens = (detailFromDb.match(/\(/g) || []).length
    const detailCloses = (detailFromDb.match(/\)/g) || []).length
    const mismatch =
      baseOpens > baseCloses && detailCloses > detailOpens
    if (!mismatch) {
      return { base: normalized, detail: detailFromDb }
    }
    // 재조립: 콤마로 잘렸으니 콤마+공백으로 복원해 fallback split 으로 흘려보냄
    const rejoined = `${normalized}, ${detailFromDb}`.replace(/\s+/g, ' ').trim()
    return splitCustomerAddress(rejoined, null)
  }

  if (!normalized) return { base: '', detail: '' }

  // 2-a) 마지막 `)` 기준 (법정동/건물명 괄호 뒤에 상세주소가 오는 패턴)
  //      콤마보다 먼저 — `(풍동, 성원아파트)` 처럼 괄호 안 콤마가 있어도 안전
  const lastParenIdx = normalized.lastIndexOf(')')
  if (lastParenIdx > 0 && lastParenIdx < normalized.length - 1) {
    const after = normalized.slice(lastParenIdx + 1).trim()
    if (after) {
      return {
        base: normalized.slice(0, lastParenIdx + 1).trim(),
        detail: after,
      }
    }
  }

  // 2-b) 콤마 구분자 (괄호가 없는 도로명주소)
  const commaIdx = normalized.indexOf(',')
  if (commaIdx > 0 && commaIdx < normalized.length - 1) {
    return {
      base: normalized.slice(0, commaIdx).trim(),
      detail: normalized.slice(commaIdx + 1).trim(),
    }
  }

  // 2-c) 끝에 메타 괄호 `(법정동, 건물명)` 가 있고 그 앞에 동/호/층 패턴이 있는 케이스
  //      예) "...덕영대로 1462-14 119동 2804호 (망포동 728 힐스테이트 영통)"
  //      → base="...덕영대로 1462-14 (망포동 728 힐스테이트 영통)", detail="119동 2804호"
  //      플레이오토 EMP RecipientAddress 단일 문자열 케이스에서 자주 등장
  const trailingMeta = normalized.match(/^(.*?)\s*(\([^)]*\))\s*$/)
  if (trailingMeta) {
    const beforeMeta = trailingMeta[1].trim()
    const meta = trailingMeta[2]
    const dongHoMatch = beforeMeta.match(
      /^(.+?)\s+((?:\d+\s*동\s*)?\d+\s*(?:호|층|호실))$/,
    )
    if (dongHoMatch) {
      return {
        base: `${dongHoMatch[1].trim()} ${meta}`,
        detail: dongHoMatch[2].trim(),
      }
    }
  }

  // 2-d) 동/호/층/호실 패턴 (괄호 없이 끝나는 일반 케이스)
  //      `\b` 제거 — 한글은 ASCII word boundary와 어울리지 않아 매칭 누락
  const detailMatch = normalized.match(
    /^(.+?)\s+((?:\d+\s*동\s*)?\d+\s*(?:호|층|호실)(?:\s.*)?)$/,
  )
  if (detailMatch) {
    return { base: detailMatch[1].trim(), detail: detailMatch[2].trim() }
  }

  return { base: normalized, detail: '' }
}
