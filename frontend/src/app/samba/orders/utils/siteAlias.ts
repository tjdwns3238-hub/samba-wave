import { normalizePlayautoAliasCode } from '@/lib/samba/playautoAlias'

// GS샵 표기 통일: GS이숍/GS이샵/GSShop/GS샵 → GSSHOP
export function normalizeSourceSiteName(name: string): string {
  const s = String(name || '').trim()
  if (!s) return s
  if (/^(gs\s?이?[숍샵]|gsshop|gs샵)$/i.test(s)) return 'GSSHOP'
  return s
}

export function formatSourceSiteLabel(sourceSite: string | null | undefined, siteAliasMap: Record<string, string>): string {
  const site = String(sourceSite || '').trim()
  if (!site) return ''
  const match = site.match(/^(.+)\(([^)]+)\)$/)
  const siteName = match?.[1]?.trim()
  const siteCode = match?.[2]?.trim()
  if (!siteName || !siteCode) return normalizeSourceSiteName(site)

  const normalizedName = normalizeSourceSiteName(siteName)
  // 쿠팡은 별칭 대신 스토어 ID(siteCode) 원본 노출
  if (/^쿠팡$/i.test(normalizedName) || /^coupang$/i.test(normalizedName)) {
    return `${normalizedName}(${siteCode})`
  }
  const alias = siteAliasMap[normalizePlayautoAliasCode(siteCode)] || siteAliasMap[siteCode]
  // 괄호 안 코드(고경/캐논/가디/마놀 등)는 플레이오토 판매계정 식별자 — 보존 필수
  return `${normalizedName}(${alias || siteCode})`
}
