/**
 * SambaWave 공용 유틸 함수
 */

/**
 * ISO 날짜 문자열을 읽기 좋은 형식으로 변환
 * @param iso - ISO 날짜 문자열
 * @param sep - 연월일 구분자 (기본 '-')
 * @returns 'YYYY-MM-DD HH:mm' 또는 'YYYY.MM.DD HH:mm'
 */
export function fmtDate(iso: string | undefined | null, sep: string = '-'): string {
  if (!iso) return '-'
  const d = new Date(iso)
  if (isNaN(d.getTime())) return typeof iso === 'string' ? iso.slice(0, 10) : '-'
  // KST(Asia/Seoul, UTC+9) 기준으로 명시적 변환
  const kst = new Date(d.toLocaleString('en-US', { timeZone: 'Asia/Seoul' }))
  const y = kst.getFullYear()
  const m = String(kst.getMonth() + 1).padStart(2, '0')
  const day = String(kst.getDate()).padStart(2, '0')
  const h = String(kst.getHours()).padStart(2, '0')
  const min = String(kst.getMinutes()).padStart(2, '0')
  return `${y}${sep}${m}${sep}${day} ${h}:${min}`
}

/**
 * 현재 시각을 24시간제 HH:mm:ss 형식으로 반환 (로그 타임스탬프용)
 * @returns 'HH:mm:ss'
 */
export function fmtTime(): string {
  return new Date().toLocaleTimeString('ko-KR', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

/**
 * ISO 날짜 문자열을 초 단위까지 포함한 형식으로 변환 (KST 명시적)
 * @returns 'YYYY-MM-DD [HH:mm:ss]'
 */
export function fmtDateTime(iso: string | undefined | null): string {
  if (!iso) return '-'
  const d = new Date(iso)
  if (isNaN(d.getTime())) return typeof iso === 'string' ? iso.slice(0, 10) : '-'
  const kst = new Date(d.toLocaleString('en-US', { timeZone: 'Asia/Seoul' }))
  const y = kst.getFullYear()
  const m = String(kst.getMonth() + 1).padStart(2, '0')
  const day = String(kst.getDate()).padStart(2, '0')
  const h = String(kst.getHours()).padStart(2, '0')
  const min = String(kst.getMinutes()).padStart(2, '0')
  const s = String(kst.getSeconds()).padStart(2, '0')
  return `${y}-${m}-${day} [${h}:${min}:${s}]`
}

function getKstDateParts(base: Date = new Date()) {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Seoul',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(base)
  const year = Number(parts.find(part => part.type === 'year')?.value ?? '0')
  const month = Number(parts.find(part => part.type === 'month')?.value ?? '1')
  const day = Number(parts.find(part => part.type === 'day')?.value ?? '1')
  return { year, month, day }
}

export function getKstTodayDate(): Date {
  const { year, month, day } = getKstDateParts()
  return new Date(year, month - 1, day)
}

export function formatDateInput(date: Date): string {
  const y = date.getFullYear()
  const m = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  return `${y}-${m}-${day}`
}

/**
 * 기간 키를 시작일 Date로 변환 (today/1week/1month 등)
 * @param key - 기간 키 (today, yesterday, thisweek, lastweek, 1week, 1month, thismonth, lastmonth, thisyear)
 * @returns 시작일 Date 또는 null (all 등 전체 기간)
 */
export function getPeriodStart(key: string): Date | null {
  const now = getKstTodayDate()
  now.setHours(0, 0, 0, 0)
  switch (key) {
    case 'today': return now
    case 'yesterday': { const d = new Date(now); d.setDate(d.getDate() - 1); return d }
    case 'thisweek': { const d = new Date(now); d.setDate(d.getDate() - ((d.getDay() + 6) % 7)); return d }
    case 'lastweek': { const d = new Date(now); d.setDate(d.getDate() - ((d.getDay() + 6) % 7) - 7); return d }
    case '5days': { const d = new Date(now); d.setDate(d.getDate() - 4); return d }
    case '1week': { const d = new Date(now); d.setDate(d.getDate() - 6); return d }
    case '1month': { const d = new Date(now); d.setDate(d.getDate() - 29); return d }
    case '2month': { const d = new Date(now); d.setMonth(d.getMonth() - 2); return d }
    case 'thismonth': return new Date(now.getFullYear(), now.getMonth(), 1)
    case 'lastmonth': return new Date(now.getFullYear(), now.getMonth() - 1, 1)
    case 'thisyear': return new Date(now.getFullYear(), 0, 1)
    default: return null
  }
}

/**
 * 기간 키를 종료일 Date로 변환 (지난주/지난달/어제는 해당 기간 마지막 날)
 * @param key - 기간 키
 * @returns 종료일 Date
 */
export function getPeriodEnd(key: string): Date {
  const now = getKstTodayDate()
  now.setHours(0, 0, 0, 0)
  switch (key) {
    case 'yesterday': { const d = new Date(now); d.setDate(d.getDate() - 1); return d }
    case 'lastweek': { const d = new Date(now); d.setDate(d.getDate() - ((d.getDay() + 6) % 7) - 1); return d }
    case 'lastmonth': return new Date(now.getFullYear(), now.getMonth(), 0)
    default: return now
  }
}
