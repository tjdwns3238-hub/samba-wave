'use client'

import { Dispatch, SetStateAction } from 'react'
import type { SambaMarketAccount } from '@/lib/samba/api/commerce'
import { STORE_MARKETS } from '../config'

type OutboundPlace = { code: string; name: string; address: string; deliveryCode?: string }
type InboundPlace = { code: string; name: string; address: string; address_detail: string; zipcode: string; phone: string }

// 마스킹된 secret 값(****XXXX)이 form에 들어가 그대로 저장되면 DB의 진짜 값을
// 마스킹 문자열로 덮어쓰는 사고가 발생 → 수정 모드 진입 시 password 필드는 빈 값으로 주입.
// 사용자가 비워두면 백엔드 가드가 기존 값을 유지하고, 변경 시에만 새 값으로 업데이트됨.
const isMaskedSecret = (v: unknown): boolean =>
  typeof v === 'string' && /^\*{4}.{0,4}$/.test(v)

interface Props {
  marketKey: string
  accounts: SambaMarketAccount[]
  editingAccountId: string | null
  setEditingAccountId: (v: string | null) => void
  setStoreData: Dispatch<SetStateAction<Record<string, Record<string, string>>>>
  setCoupangOutboundList: Dispatch<SetStateAction<OutboundPlace[]>>
  setCoupangInboundList: Dispatch<SetStateAction<InboundPlace[]>>
  handleAccountDelete: (id: string) => void | Promise<void>
  handleAccountSetDefault?: (id: string) => void | Promise<void>
}

export function ConnectedAccountsList(props: Props) {
  const {
    marketKey, accounts, editingAccountId, setEditingAccountId, setStoreData,
    setCoupangOutboundList, setCoupangInboundList, handleAccountDelete,
    handleAccountSetDefault,
  } = props
  const marketAccounts = accounts.filter(a => a.market_type === marketKey)

  return (
    <div style={{ width: '260px', flexShrink: 0 }}>
      <div style={{ fontSize: '0.82rem', fontWeight: 600, color: '#888', marginBottom: '0.5rem' }}>연결 계정</div>
      {marketAccounts.length === 0 ? (
        <div style={{ fontSize: '0.78rem', color: '#555', padding: '0.5rem 0' }}>등록된 계정 없음</div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '0.375rem' }}>
          {marketAccounts.map(a => (
            <div key={a.id} style={{
              display: 'flex', alignItems: 'center', gap: '0.5rem',
              padding: '0.4rem 0.625rem', background: 'rgba(255,255,255,0.02)',
              borderRadius: '6px', border: '1px solid rgba(45,45,45,0.5)',
            }}>
              {handleAccountSetDefault && (
                <label
                  title={a.is_default ? '기본 계정' : '기본 계정으로 지정'}
                  style={{ display: 'flex', alignItems: 'center', cursor: 'pointer' }}
                >
                  <input
                    type="radio"
                    name={`default-${marketKey}`}
                    checked={!!a.is_default}
                    onChange={() => { void handleAccountSetDefault(a.id) }}
                    style={{ cursor: 'pointer', accentColor: '#FF8C00' }}
                  />
                </label>
              )}
              <div style={{ flex: 1, minWidth: 0, fontSize: '0.8rem', color: '#E5E5E5', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {a.account_label}
                {a.is_default && (
                  <span style={{ marginLeft: '0.35rem', fontSize: '0.65rem', color: '#FF8C00' }}>★기본</span>
                )}
              </div>
              <button
                onClick={() => {
                  setEditingAccountId(a.id)
                  const accData = (a.additional_fields || {}) as Record<string, string>
                  // 마켓 설정상 password 타입 필드는 form에 빈 값으로 주입 (마스킹값 덮어쓰기 사고 차단)
                  const marketCfg = STORE_MARKETS.find(m => m.key === marketKey)
                  const passwordFields = new Set(
                    (marketCfg?.fields ?? []).filter(f => f.type === 'password').map(f => f.name)
                  )
                  const sanitized: Record<string, string> = {}
                  for (const [k, v] of Object.entries(accData)) {
                    // non-password 마스킹값은 스킵 (잘못 덮어쓰기 방지)
                    if (!passwordFields.has(k) && isMaskedSecret(v)) continue
                    // password 필드: 마스킹값(****xxxx) 포함 로드 → 폼에 ••• 도트 표시
                    // 저장 시 saveStoreSettings 필터가 마스킹값 자동 제거 → 기존 DB값 유지
                    sanitized[k] = String(v || '')
                  }
                  const formData: Record<string, string> = {
                    businessName: a.business_name || '',
                    storeId: a.seller_id || '',
                    ...sanitized,
                  }
                  setStoreData(prev => ({ ...prev, [marketKey]: formData }))
                  if (marketKey === 'coupang') {
                    const outCode = formData.outboundShippingPlaceCode || ''
                    const outName = formData.outboundShippingPlaceName || ''
                    const outDeliveryCode = formData.outboundDeliveryCode || ''
                    setCoupangOutboundList(outCode ? [{ code: outCode, name: outName, address: '', deliveryCode: outDeliveryCode }] : [])
                    const retCode = formData.returnCenterCode || ''
                    const retName = formData.returnCenterName || ''
                    const retAddr = formData.returnCenterAddress || ''
                    const retAddrDetail = formData.returnCenterAddressDetail || ''
                    const retZip = formData.returnCenterZipcode || ''
                    const retPhone = formData.returnCenterPhone || ''
                    setCoupangInboundList(retCode ? [{ code: retCode, name: retName, address: retAddr, address_detail: retAddrDetail, zipcode: retZip, phone: retPhone }] : [])
                  }
                }}
                style={{
                  padding: '0.15rem 0.4rem', borderRadius: '4px', fontSize: '0.7rem',
                  background: editingAccountId === a.id ? 'rgba(255,140,0,0.15)' : 'rgba(60,60,60,0.8)',
                  color: editingAccountId === a.id ? '#FF8C00' : '#C5C5C5',
                  border: editingAccountId === a.id ? '1px solid #FF8C00' : '1px solid #3D3D3D',
                  cursor: 'pointer', whiteSpace: 'nowrap',
                }}
              >{editingAccountId === a.id ? '수정중' : '수정'}</button>
              <button
                onClick={() => handleAccountDelete(a.id)}
                style={{
                  padding: '0.15rem 0.4rem', borderRadius: '4px', fontSize: '0.7rem',
                  background: 'rgba(255,80,80,0.15)', color: '#FF6B6B', border: '1px solid rgba(255,80,80,0.3)',
                  cursor: 'pointer', whiteSpace: 'nowrap',
                }}
              >삭제</button>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
