'use client'

import { card, inputStyle, fmtNum } from '@/lib/samba/styles'
import {
  accountApi,
  forbiddenApi,
  proxyApi,
} from '@/lib/samba/api/commerce'
import { showAlert, showConfirm } from '@/components/samba/Modal'
import { NumInputStr as NumInput } from '@/components/samba/NumInput'
import { formatPlayautoAliasEntry, parsePlayautoAliasEntry } from '@/lib/samba/playautoAlias'
import { STORE_MARKETS } from '../config'
import type { StoreSettingsState, StoreSettingsActions } from '../hooks/useStoreSettings'
import { ConnectedAccountsList } from './ConnectedAccountsList'

type Props = StoreSettingsState & Pick<StoreSettingsActions,
  'updateStoreField' | 'saveStoreSettings' | 'testStoreAuth' |
  'handleAccountToggle' | 'handleAccountDelete' | 'handleAccountSetDefault' | 'togglePasswordVisibility' |
  'setStoreTab' | 'setStoreData' | 'setSsgShippingOptions' | 'setSsgAddrOptions' |
  'setEsmPlaceOptions' | 'setEsmDispatchOptions' |
  'setLotteonDeliveryPolicyOptions' | 'setLotteonWarehouseOptions' |
  'setElevenstDispatchTemplateOptions' |
  'setCoupangOutboundList' | 'setCoupangInboundList' | 'loadCoupangShippingPlaces' |
  'setLotteHomeDeliveryPolicyOptions' | 'setLotteHomeExtraPolicyOptions' | 'setLotteHomeShippingPlaceOptions' | 'setLotteHomeReturnPlaceOptions' |
  'setEditingAccountId' | 'setVisiblePasswords' | 'setNetworkIps' | 'saveNetworkIps'
>

export function StoreSettingsPanel(props: Props) {
  const {
    accounts, storeTab, visiblePasswords, storeData, savedStoreData,
    storeStatus, editingAccountId,
    ssgShippingOptions, ssgAddrOptions,
    esmPlaceOptions, esmDispatchOptions,
    lotteonDeliveryPolicyOptions, lotteonWarehouseOptions,
    elevenstDispatchTemplateOptions,
    coupangOutboundList, coupangInboundList,
    lotteHomeDeliveryPolicyOptions, lotteHomeExtraPolicyOptions, lotteHomeShippingPlaceOptions, lotteHomeReturnPlaceOptions,
    networkIps, networkIpStatus,
    updateStoreField, saveNetworkIps, saveStoreSettings, testStoreAuth,
    handleAccountDelete, handleAccountSetDefault, togglePasswordVisibility,
    setStoreTab, setStoreData,
    setSsgShippingOptions, setSsgAddrOptions,
    setEsmPlaceOptions, setEsmDispatchOptions,
    setLotteonDeliveryPolicyOptions, setLotteonWarehouseOptions,
    setElevenstDispatchTemplateOptions,
    setCoupangOutboundList, setCoupangInboundList, loadCoupangShippingPlaces,
    setLotteHomeDeliveryPolicyOptions, setLotteHomeExtraPolicyOptions, setLotteHomeShippingPlaceOptions, setLotteHomeReturnPlaceOptions,
    setEditingAccountId, setNetworkIps,
  } = props

  return (
    <div style={{ ...card, padding: '1.5rem', marginBottom: '1.5rem' }}>
      <div style={{ fontSize: '1rem', fontWeight: 700, color: '#E5E5E5', marginBottom: '0.25rem' }}>스토어 연결</div>
      <p style={{ fontSize: '0.8125rem', color: '#666', marginBottom: '1.25rem' }}>API 연결 및 계정 설정을 관리합니다</p>

      {/* 웹 / 로컬 IP */}
      <div style={{ marginBottom: '1.5rem', padding: '1rem', border: '1px solid #2D2D2D', borderRadius: '8px', background: 'rgba(255,255,255,0.02)' }}>
        <div style={{ fontSize: '0.875rem', fontWeight: 600, color: '#E5E5E5', marginBottom: '0.75rem' }}>웹 / 로컬 IP</div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: '0.75rem' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
            <label style={{ color: '#888', fontSize: '0.875rem', minWidth: '180px', flexShrink: 0 }}>웹 IP</label>
            <input type="text" style={{ ...inputStyle, flex: 1 }} value={networkIps.web}
              onChange={(e) => setNetworkIps(prev => ({ ...prev, web: e.target.value }))}
              placeholder="예: 123.123.123.123" />
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
            <label style={{ color: '#888', fontSize: '0.875rem', minWidth: '180px', flexShrink: 0 }}>로컬 IP</label>
            <input type="text" style={{ ...inputStyle, flex: 1 }} value={networkIps.local}
              onChange={(e) => setNetworkIps(prev => ({ ...prev, local: e.target.value }))}
              placeholder="예: 192.168.0.10" />
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
            <button type="button" onClick={saveNetworkIps}
              style={{ padding: '0.5rem 1rem', background: '#FF8C00', color: '#fff', border: 'none', borderRadius: '6px', fontWeight: 600, fontSize: '0.8125rem', cursor: 'pointer' }}>IP 저장</button>
            {networkIpStatus && (
              <span style={{ fontSize: '0.8125rem', color: networkIpStatus.includes('실패') ? '#FF6B6B' : '#51CF66' }}>{networkIpStatus}</span>
            )}
          </div>
        </div>
      </div>

      {(() => {
        const domestic = ['smartstore', 'coupang', '11st', 'gmarket', 'auction', 'lotteon', 'toss', 'ssg', 'gsshop', 'lottehome', 'homeand', 'hmall', 'musinsa', 'kream', 'playauto', 'cafe24']
        const overseas = ['amazon', 'ebay', 'rakuten', 'qoo10', 'lazada', 'shopee', 'buyma', 'shopify', 'zoom', 'poison']
        const domesticMarkets = STORE_MARKETS.filter(m => domestic.includes(m.key))
        const overseasMarkets = STORE_MARKETS.filter(m => overseas.includes(m.key))
        const renderTab = (m: typeof STORE_MARKETS[number]) => (
          <button
            key={m.key}
            onClick={() => {
              // 이전 탭 + 전환 대상 탭 모두 storeData 초기화 (잔류값 방지)
              setStoreData(prev => {
                const next = { ...prev }
                delete next[storeTab]  // 이전 탭 데이터 제거
                delete next[m.key]     // 전환 대상 탭 데이터 제거
                return next
              })
              setStoreTab(m.key)
              setEditingAccountId(null)
            }}
            style={{
              padding: '0.5rem 0.75rem', background: 'none', border: 'none',
              borderBottom: storeTab === m.key ? '2px solid #FF8C00' : '2px solid transparent',
              color: storeTab === m.key ? '#FF8C00' : '#666',
              fontSize: '0.8125rem', fontWeight: storeTab === m.key ? 600 : 400,
              cursor: 'pointer', marginBottom: '-1px', whiteSpace: 'nowrap',
            }}
          >
            {m.label}
          </button>
        )
        return (
          <div style={{ borderBottom: '1px solid #2D2D2D', marginBottom: '1.5rem' }}>
            <div style={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 0 }}>
              <span style={{ fontSize: '0.68rem', color: '#FF8C00', fontWeight: 600, padding: '0.5rem 0.5rem 0.5rem 0', whiteSpace: 'nowrap' }}>국내</span>
              {domesticMarkets.map(renderTab)}
            </div>
            <div style={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 0 }}>
              <span style={{ fontSize: '0.68rem', color: '#4C9AFF', fontWeight: 600, padding: '0.5rem 0.5rem 0.5rem 0', whiteSpace: 'nowrap' }}>해외</span>
              {overseasMarkets.map(renderTab)}
            </div>
          </div>
        )
      })()}

      {/* 마켓별 설정 폼 + 연결계정 */}
      {STORE_MARKETS.filter(m => m.key === storeTab).map(market => (
        <div key={market.key} style={{ display: 'flex', gap: '2rem', alignItems: 'flex-start' }}>
          <div style={{ flex: 1, maxWidth: '560px' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem', marginBottom: '1rem' }}>
              <span style={{ fontSize: '0.9375rem', fontWeight: 600, color: '#E5E5E5' }}>{market.label} 설정</span>
              {editingAccountId && (
                <>
                  <span style={{ fontSize: '0.75rem', color: '#FF8C00', fontWeight: 600 }}>
                    ({accounts.find(a => a.id === editingAccountId)?.account_label} 수정중)
                  </span>
                  <button
                    onClick={() => {
                      setEditingAccountId(null)
                      setStoreData(prev => { const next = { ...prev }; delete next[market.key]; return next })
                    }}
                    style={{ padding: '0.2rem 0.5rem', fontSize: '0.7rem', background: 'rgba(255,80,80,0.1)', border: '1px solid rgba(255,80,80,0.3)', borderRadius: '4px', color: '#FF6B6B', cursor: 'pointer' }}
                  >취소</button>
                </>
              )}
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '0.75rem' }}>
              {market.fields.map(field => field.type === 'divider' ? (
                <div key={field.name} style={{ borderTop: '1px solid #2D2D2D', paddingTop: '0.75rem', marginTop: '0.25rem', display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
                  <span style={{ fontSize: '0.8rem', fontWeight: 600, color: '#FFB84D' }}>{field.label}</span>
                  {(market.key === 'gmarket' || market.key === 'auction') && field.name === '_divider_shipping' && (
                    <button
                      onClick={async () => {
                        try {
                          const data = storeData[market.key] || savedStoreData[market.key] || {}
                          if (!data.storeId) {
                            showAlert('판매자 ID를 먼저 입력하고 저장해주세요.', 'error')
                            return
                          }
                          await forbiddenApi.saveSetting(`store_${market.key}`, data)
                          const res = await proxyApi.esmDeliveryInfo(market.key, editingAccountId || undefined)
                          if (!res.success) {
                            showAlert(res.message || '배송정보를 불러올 수 없습니다.', 'error')
                            return
                          }
                          const places = (res.places || []).map((p: { placeNo: number; placeNm: string; placeType?: number }) => ({
                            value: String(p.placeNo),
                            label: `[${p.placeType === 2 ? '반품지' : '출고지'}] ${p.placeNm} (${p.placeNo})`,
                          }))
                          const dispatches = (res.dispatchPolicies || []).map((d: { dispatchPolicyNo: number; policyNm: string }) => ({
                            value: String(d.dispatchPolicyNo),
                            label: `${d.policyNm} (${d.dispatchPolicyNo})`,
                          }))
                          setEsmPlaceOptions(prev => ({ ...prev, [market.key]: places }))
                          setEsmDispatchOptions(prev => ({ ...prev, [market.key]: dispatches }))
                          showAlert(`출고지/반품지 ${fmtNum(places.length)}개, 발송정책 ${fmtNum(dispatches.length)}개를 불러왔습니다.`, 'success')
                        } catch {
                          showAlert('배송정보 조회 실패', 'error')
                        }
                      }}
                      style={{ padding: '0.3rem 0.75rem', background: 'rgba(76,154,255,0.1)', border: '1px solid rgba(76,154,255,0.3)', borderRadius: '6px', fontSize: '0.75rem', color: '#4C9AFF', cursor: 'pointer', whiteSpace: 'nowrap', flexShrink: 0 }}
                    >배송정보 불러오기</button>
                  )}
                  {market.key === 'ssg' && field.name === '_divider_shipping_code' && (
                    <button
                      onClick={async () => {
                        try {
                          // 현재 입력된 API Key로 먼저 설정 저장
                          const data = storeData['ssg'] || savedStoreData['ssg'] || {}
                          if (!data.apiKey) {
                            showAlert('API KEY를 먼저 입력하세요.', 'error')
                            return
                          }
                          await forbiddenApi.saveSetting('store_ssg', data)
                          // 배송비정책 조회
                          const shipRes = await proxyApi.ssgShippingPolicies()
                          if (shipRes.success && shipRes.policies?.length) {
                            setSsgShippingOptions(shipRes.policies.map((p: { shppcstId: string; feeAmt: number; prpayCodDivNm: string; shppcstAplUnitNm: string; divCd: number }) => {
                              const fee = p.feeAmt ? `${fmtNum(Number(p.feeAmt))}원` : '무료'
                              const parts = [p.shppcstId, fee]
                              if (p.prpayCodDivNm) parts.push(p.prpayCodDivNm)
                              if (p.shppcstAplUnitNm) parts.push(p.shppcstAplUnitNm)
                              return { value: p.shppcstId, label: parts.join(' / '), divCd: p.divCd }
                            }))
                          }
                          // 주소 조회
                          const addrRes = await proxyApi.ssgAddresses()
                          if (addrRes.success && addrRes.addresses?.length) {
                            setSsgAddrOptions(addrRes.addresses.map((a: { grpAddrId: string; doroAddrId?: string; addrNm: string; bascAddr: string }) => ({
                              value: a.doroAddrId || a.grpAddrId,
                              label: `${a.addrNm}${a.bascAddr ? ` (${a.bascAddr})` : ''}`,
                            })))
                          }
                          showAlert('배송비/주소 정보를 불러왔습니다.', 'success')
                        } catch {
                          showAlert('배송비/주소 조회 실패', 'error')
                        }
                      }}
                      style={{ padding: '0.3rem 0.75rem', background: 'rgba(76,154,255,0.1)', border: '1px solid rgba(76,154,255,0.3)', borderRadius: '6px', fontSize: '0.75rem', color: '#4C9AFF', cursor: 'pointer', whiteSpace: 'nowrap', flexShrink: 0 }}
                    >배송비/주소 불러오기</button>
                  )}
                  {market.key === 'lotteon' && field.name === '_divider_shipping_infra' && (
                    <button
                      onClick={async () => {
                        try {
                          const data = storeData['lotteon'] || savedStoreData['lotteon'] || {}
                          if (!data.apiKey) { showAlert('API Key를 먼저 입력하세요.', 'error'); return }
                          await forbiddenApi.saveSetting('store_lotteon', data)
                          const [polRes, whRes] = await Promise.all([
                            proxyApi.lotteonDeliveryPolicies(),
                            proxyApi.lotteonWarehouses(),
                          ])
                          if (polRes.success) setLotteonDeliveryPolicyOptions(polRes.policies)
                          if (whRes.success) setLotteonWarehouseOptions({ departure: whRes.departure, return_: whRes.return_ })
                          const polCount = polRes.success ? polRes.policies.length : 0
                          const depCount = whRes.success ? whRes.departure.length : 0
                          const retCount = whRes.success ? whRes.return_.length : 0
                          if (polCount > 0 || depCount > 0 || retCount > 0) {
                            showAlert(`배송정책 ${fmtNum(polCount)}건, 출고지 ${fmtNum(depCount)}건, 회수지 ${fmtNum(retCount)}건을 불러왔습니다.`, 'success')
                          } else {
                            const msg = (polRes as { message?: string }).message || (whRes as { message?: string }).message || ''
                            showAlert(msg ? `불러오기 실패: ${msg}` : '설정된 배송정책/출고지가 없습니다. 롯데ON 셀러 센터에서 먼저 등록해주세요.', 'error')
                          }
                        } catch { showAlert('불러오기 실패', 'error') }
                      }}
                      style={{ padding: '0.3rem 0.75rem', background: 'rgba(76,154,255,0.1)', border: '1px solid rgba(76,154,255,0.3)', borderRadius: '6px', fontSize: '0.75rem', color: '#4C9AFF', cursor: 'pointer', whiteSpace: 'nowrap', flexShrink: 0 }}
                    >배송정책/출고지 불러오기</button>
                  )}
                  {market.key === 'lottehome' && field.name === '_divider_lottehome_shipping' && (
                    <button
                      onClick={async () => {
                        try {
                          const [polRes, plcRes] = await Promise.all([
                            proxyApi.lottehomeDeliveryPolicies(),
                            proxyApi.lottehomePlaces(),
                          ])
                          let polCount = 0, extraCount = 0, shpCount = 0, retCount = 0
                          if (polRes.policies) {
                            const opts = polRes.policies.map(p => ({ value: p.no, label: p.nm || p.no }))
                            setLotteHomeDeliveryPolicyOptions(opts)
                            polCount = opts.length
                          }
                          if (polRes.extra_policies) {
                            const opts = polRes.extra_policies.map(p => ({ value: p.no, label: p.nm || p.no }))
                            setLotteHomeExtraPolicyOptions(opts)
                            extraCount = opts.length
                          }
                          if (plcRes.data) {
                            const shpOpts = (plcRes.data.shipping_places || []).map(p => ({ value: p.code, label: p.name + (p.address ? ` (${p.address})` : '') }))
                            const retOpts = (plcRes.data.return_places || []).map(p => ({ value: p.code, label: p.name + (p.address ? ` (${p.address})` : '') }))
                            setLotteHomeShippingPlaceOptions(shpOpts)
                            setLotteHomeReturnPlaceOptions(retOpts)
                            shpCount = shpOpts.length
                            retCount = retOpts.length
                          }
                          showAlert(`배송정책 ${fmtNum(polCount)}건, 추가배송비정책 ${fmtNum(extraCount)}건, 출고지 ${fmtNum(shpCount)}건, 반품지 ${fmtNum(retCount)}건을 불러왔습니다.`, 'success')
                        } catch { showAlert('불러오기 실패', 'error') }
                      }}
                      style={{ padding: '0.3rem 0.75rem', background: 'rgba(76,154,255,0.1)', border: '1px solid rgba(76,154,255,0.3)', borderRadius: '6px', fontSize: '0.75rem', color: '#4C9AFF', cursor: 'pointer', whiteSpace: 'nowrap', flexShrink: 0 }}
                    >배송정책/출고지 불러오기</button>
                  )}
                  {market.key === 'coupang' && field.name === '_divider_shipping_coupang' && (
                    <button
                      onClick={() => loadCoupangShippingPlaces(editingAccountId || undefined)}
                      style={{ padding: '0.3rem 0.75rem', background: 'rgba(76,154,255,0.1)', border: '1px solid rgba(76,154,255,0.3)', borderRadius: '6px', fontSize: '0.75rem', color: '#4C9AFF', cursor: 'pointer', whiteSpace: 'nowrap', flexShrink: 0 }}
                    >출고지/반품지 조회</button>
                  )}
                </div>
              ) : field.type === 'info' ? (
                <div key={field.name} style={{ padding: '0.4rem 0.6rem', background: 'rgba(255,140,0,0.08)', border: '1px solid rgba(255,140,0,0.2)', borderRadius: '4px' }}>
                  <span style={{ fontSize: '0.75rem', color: '#FF8C00' }}>{field.label}</span>
                </div>
              ) : field.type === 'alias' ? (
                <div key={field.name} style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
                  <label style={{ color: '#888', fontSize: '0.875rem', minWidth: '180px', flexShrink: 0 }}>{field.label}</label>
                  <input
                    type="text"
                    style={{ ...inputStyle, flex: 1 }}
                    value={(() => {
                      const v = storeData[market.key]?.[field.name] || ''
                      return parsePlayautoAliasEntry(v).code
                    })()}
                    onChange={(e) => {
                      const nick = parsePlayautoAliasEntry(storeData[market.key]?.[field.name] || '').alias
                      updateStoreField(market.key, field.name, formatPlayautoAliasEntry(e.target.value, nick))
                    }}
                    placeholder={field.placeholder || '마켓번호'}
                  />
                  <span style={{ color: '#555', fontSize: '0.8rem', flexShrink: 0 }}>—</span>
                  <input
                    type="text"
                    style={{ ...inputStyle, width: '120px', flexShrink: 0 }}
                    value={(() => {
                      const v = storeData[market.key]?.[field.name] || ''
                      return parsePlayautoAliasEntry(v).alias
                    })()}
                    onChange={(e) => {
                      const code = parsePlayautoAliasEntry(storeData[market.key]?.[field.name] || '').code
                      updateStoreField(market.key, field.name, formatPlayautoAliasEntry(code, e.target.value))
                    }}
                    placeholder="사업자"
                  />
                </div>
              ) : (
                <div key={field.name} style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
                  <label style={{ color: '#888', fontSize: '0.875rem', minWidth: '180px', flexShrink: 0 }}>{field.label}</label>
                  {field.type === 'esm-place-select' ? (
                    <select
                      style={{ ...inputStyle, flex: 1 }}
                      value={storeData[market.key]?.[field.name] || ''}
                      onChange={(e) => updateStoreField(market.key, field.name, e.target.value)}
                    >
                      <option value=''>불러오기 버튼으로 선택</option>
                      {(esmPlaceOptions[market.key] || []).map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
                    </select>
                  ) : field.type === 'esm-dispatch-select' ? (
                    <select
                      style={{ ...inputStyle, flex: 1 }}
                      value={storeData[market.key]?.[field.name] || ''}
                      onChange={(e) => updateStoreField(market.key, field.name, e.target.value)}
                    >
                      <option value=''>불러오기 버튼으로 선택</option>
                      {(esmDispatchOptions[market.key] || []).map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
                    </select>
                  ) : field.type === 'coupang-outbound' ? (
                    <select
                      style={{ ...inputStyle, flex: 1 }}
                      value={storeData[market.key]?.[field.name] || ''}
                      onChange={(e) => {
                        const sel = coupangOutboundList.find(o => o.code === e.target.value)
                        updateStoreField(market.key, 'outboundShippingPlaceCode', sel?.code || '')
                        updateStoreField(market.key, 'outboundShippingPlaceName', sel?.name || '')
                      }}
                    >
                      <option value=''>버튼으로 불러오기</option>
                      {coupangOutboundList.map(o => (
                        <option key={o.code} value={o.code}>{o.name}{o.address ? ` (${o.address})` : ''}</option>
                      ))}
                    </select>
                  ) : field.type === 'coupang-return' ? (
                    <select
                      style={{ ...inputStyle, flex: 1 }}
                      value={storeData[market.key]?.[field.name] || ''}
                      onChange={(e) => {
                        const sel = coupangInboundList.find(i => i.code === e.target.value)
                        updateStoreField(market.key, 'returnCenterCode', sel?.code || '')
                        updateStoreField(market.key, 'returnCenterName', sel?.name || '')
                        updateStoreField(market.key, 'returnCenterAddress', sel?.address || '')
                        updateStoreField(market.key, 'returnCenterAddressDetail', sel?.address_detail || '')
                        updateStoreField(market.key, 'returnCenterZipcode', sel?.zipcode || '')
                        updateStoreField(market.key, 'returnCenterPhone', sel?.phone || '')
                      }}
                    >
                      <option value=''>버튼으로 불러오기</option>
                      {coupangInboundList.map(i => (
                        <option key={i.code} value={i.code}>{i.name}{i.address ? ` (${i.address})` : ''}</option>
                      ))}
                    </select>
                  ) : (field.type === 'ssg-shipping-select' || field.type === 'ssg-extra-select') ? (
                    <select
                      style={{ ...inputStyle, flex: 1 }}
                      value={storeData[market.key]?.[field.name] || ''}
                      onChange={(e) => updateStoreField(market.key, field.name, e.target.value)}
                    >
                      <option value=''>버튼으로 불러오기</option>
                      {ssgShippingOptions
                        .filter(o => {
                          if (field.name === 'whoutShppcstId') return o.divCd === 10
                          if (field.name === 'retShppcstId') return o.divCd === 20
                          if (field.name === 'addShppcstIdJeju') return o.divCd === 70
                          if (field.name === 'addShppcstIdIsland') return o.divCd === 60
                          return false
                        })
                        .map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
                    </select>
                  ) : field.type === 'ssg-addr-select' ? (
                    <select
                      style={{ ...inputStyle, flex: 1 }}
                      value={storeData[market.key]?.[field.name] || ''}
                      onChange={(e) => updateStoreField(market.key, field.name, e.target.value)}
                    >
                      <option value=''>버튼으로 불러오기</option>
                      {ssgAddrOptions.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
                    </select>
                  ) : field.type === 'lotteon-policy-select' ? (
                    <select
                      style={{ ...inputStyle, flex: 1 }}
                      value={storeData[market.key]?.[field.name] || ''}
                      onChange={(e) => updateStoreField(market.key, field.name, e.target.value)}
                    >
                      <option value=''>-- 불러오기 버튼으로 선택 --</option>
                      {lotteonDeliveryPolicyOptions.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
                    </select>
                  ) : field.type === 'lotteon-departure-select' ? (
                    <select
                      style={{ ...inputStyle, flex: 1 }}
                      value={storeData[market.key]?.[field.name] || ''}
                      onChange={(e) => updateStoreField(market.key, field.name, e.target.value)}
                    >
                      <option value=''>-- 불러오기 버튼으로 선택 --</option>
                      {lotteonWarehouseOptions.departure.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
                    </select>
                  ) : field.type === 'lotteon-return-select' ? (
                    <select
                      style={{ ...inputStyle, flex: 1 }}
                      value={storeData[market.key]?.[field.name] || ''}
                      onChange={(e) => updateStoreField(market.key, field.name, e.target.value)}
                    >
                      <option value=''>-- 불러오기 버튼으로 선택 --</option>
                      {lotteonWarehouseOptions.return_.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
                    </select>
                  ) : field.type === 'lottehome-policy-select' ? (
                    <select
                      style={{ ...inputStyle, flex: 1 }}
                      value={storeData[market.key]?.[field.name] ?? savedStoreData[market.key]?.[field.name] ?? ''}
                      onChange={(e) => updateStoreField(market.key, field.name, e.target.value)}
                    >
                      <option value=''>-- 불러오기 버튼으로 선택 --</option>
                      {lotteHomeDeliveryPolicyOptions.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
                    </select>
                  ) : field.type === 'lottehome-extra-policy-select' ? (
                    <select
                      style={{ ...inputStyle, flex: 1 }}
                      value={storeData[market.key]?.[field.name] ?? savedStoreData[market.key]?.[field.name] ?? ''}
                      onChange={(e) => updateStoreField(market.key, field.name, e.target.value)}
                    >
                      <option value=''>-- 불러오기 버튼으로 선택 --</option>
                      {lotteHomeExtraPolicyOptions.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
                    </select>
                  ) : field.type === 'lottehome-shipping-select' ? (
                    <select
                      style={{ ...inputStyle, flex: 1 }}
                      value={storeData[market.key]?.[field.name] ?? savedStoreData[market.key]?.[field.name] ?? ''}
                      onChange={(e) => updateStoreField(market.key, field.name, e.target.value)}
                    >
                      <option value=''>-- 불러오기 버튼으로 선택 --</option>
                      {lotteHomeShippingPlaceOptions.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
                    </select>
                  ) : field.type === 'lottehome-return-select' ? (
                    <select
                      style={{ ...inputStyle, flex: 1 }}
                      value={storeData[market.key]?.[field.name] ?? savedStoreData[market.key]?.[field.name] ?? ''}
                      onChange={(e) => updateStoreField(market.key, field.name, e.target.value)}
                    >
                      <option value=''>-- 불러오기 버튼으로 선택 --</option>
                      {lotteHomeReturnPlaceOptions.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
                    </select>
                  ) : field.type === 'elevenst-dispatch-select' ? (
                    <select
                      style={{ ...inputStyle, flex: 1 }}
                      value={storeData[market.key]?.[field.name] || ''}
                      onChange={(e) => updateStoreField(market.key, field.name, e.target.value)}
                    >
                      <option value=''>-- 출고지정보 가져오기로 자동 로드 --</option>
                      {elevenstDispatchTemplateOptions.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
                    </select>
                  ) : field.type === 'select' ? (
                    <select
                      style={{ ...inputStyle, flex: 1 }}
                      value={storeData[market.key]?.[field.name] || ''}
                      onChange={(e) => updateStoreField(market.key, field.name, e.target.value)}
                    >
                      {field.options?.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
                    </select>
                  ) : field.type === 'radio' ? (
                    <div style={{ display: 'flex', gap: '0.5rem', flex: 1 }}>
                      {field.options?.map(o => {
                        const selected = (storeData[market.key]?.[field.name] || field.options?.[0]?.value || '') === o.value
                        return (
                          <button
                            key={o.value}
                            type="button"
                            onClick={() => updateStoreField(market.key, field.name, o.value)}
                            style={{
                              padding: '0.4rem 1rem',
                              background: selected ? '#FF8C00' : 'transparent',
                              color: selected ? '#000' : '#888',
                              border: `1px solid ${selected ? '#FF8C00' : '#2D2D2D'}`,
                              borderRadius: '6px',
                              fontSize: '0.8125rem',
                              fontWeight: selected ? 600 : 400,
                              cursor: 'pointer',
                              minWidth: '80px',
                            }}
                          >{o.label}</button>
                        )
                      })}
                    </div>
                  ) : field.type === 'checkbox' ? (
                    <label style={{ display: 'flex', alignItems: 'center', gap: '6px', cursor: 'pointer' }}>
                      <input
                        type="checkbox"
                        checked={storeData[market.key]?.[field.name] === 'true' || storeData[market.key]?.[field.name] as unknown === true}
                        onChange={(e) => updateStoreField(market.key, field.name, e.target.checked ? 'true' : 'false')}
                        style={{ accentColor: '#FF8C00', width: '14px', height: '14px' }}
                      />
                      {field.placeholder && <span style={{ fontSize: '0.72rem', color: '#888' }}>({field.placeholder})</span>}
                    </label>
                  ) : field.type === 'number' ? (
                    <>
                      <NumInput
                        style={{ flex: 1, ...(field.disabled ? { opacity: 0.6, pointerEvents: 'none' as const } : {}) }}
                        value={field.disabled && field.fixedValue != null ? String(field.fixedValue) : (storeData[market.key]?.[field.name] || '')}
                        onChange={(v) => { if (!field.disabled) updateStoreField(market.key, field.name, v) }}
                        placeholder={field.placeholder || '0'}
                      />
                      {field.description && <span style={{ fontSize: '0.7rem', color: '#888', flexShrink: 0 }}>{field.description}</span>}
                    </>
                  ) : field.type === 'password' ? (
                    <div style={{ display: 'flex', flex: 1, gap: '4px', alignItems: 'center' }}>
                      <input
                        type={visiblePasswords.has(`${market.key}_${field.name}`) ? 'text' : 'password'}
                        style={{ ...inputStyle, flex: 1 }}
                        value={storeData[market.key]?.[field.name] || ''}
                        onChange={(e) => updateStoreField(market.key, field.name, e.target.value)}
                        placeholder={field.placeholder || ''}
                      />
                      <button
                        type="button"
                        onClick={async () => {
                          const visKey = `${market.key}_${field.name}`
                          // 수정 모드 + 값이 없거나 마스킹값이면 백엔드에서 실제값 조회
                          const curVal = storeData[market.key]?.[field.name] ?? ''
                          if (editingAccountId && (!curVal || /^\*{4}.{0,4}$/.test(curVal))) {
                            try {
                              const secrets = await accountApi.getSecrets(editingAccountId)
                              const val = (secrets as Record<string, string>)[field.name]
                              if (val) {
                                setStoreData(prev => ({
                                  ...prev,
                                  [market.key]: { ...(prev[market.key] || {}), [field.name]: val },
                                }))
                              }
                            } catch { /* 조회 실패 시 무시 */ }
                          }
                          togglePasswordVisibility(visKey)
                        }}
                        style={{ padding: '0.3rem 0.5rem', fontSize: '0.7rem', background: 'transparent', border: '1px solid #2D2D2D', borderRadius: '4px', color: '#888', cursor: 'pointer', whiteSpace: 'nowrap' }}
                      >{visiblePasswords.has(`${market.key}_${field.name}`) ? '숨김' : '보기'}</button>
                    </div>
                  ) : (
                    <input
                      type={field.type}
                      style={{ ...inputStyle, flex: 1 }}
                      value={storeData[market.key]?.[field.name] || ''}
                      onChange={(e) => updateStoreField(market.key, field.name, e.target.value)}
                      placeholder={field.placeholder || ''}
                    />
                  )}
                  {/* API 인증 필드 우측에 인증 테스트 버튼 */}
                  {market.authField === field.name && !field.name.startsWith('_') && (
                    <>
                      <button
                        onClick={() => testStoreAuth(market.key)}
                        style={{ padding: '0.375rem 0.875rem', background: '#FF8C00', color: '#000', border: 'none', borderRadius: '6px', fontWeight: 600, fontSize: '0.8125rem', cursor: 'pointer', whiteSpace: 'nowrap', flexShrink: 0 }}
                      >인증 테스트</button>
                      {market.guideUrl && (
                        <a href={market.guideUrl} target="_blank" rel="noopener noreferrer"
                          style={{ padding: '0.375rem 0.75rem', background: 'rgba(76,154,255,0.1)', border: '1px solid rgba(76,154,255,0.3)', borderRadius: '6px', fontSize: '0.75rem', color: '#4C9AFF', textDecoration: 'none', whiteSpace: 'nowrap', flexShrink: 0 }}
                        >API 발급</a>
                      )}
                    </>
                  )}
                  {/* 11번가 출고지정보 가져오기 버튼 */}
                  {market.key === '11st' && field.name === 'shipFromAddress' && (
                    <button
                      onClick={async () => {
                        try {
                          // 현재 입력된 API Key로 먼저 설정 저장
                          const data = storeData['11st'] || {}
                          if (data.apiKey) {
                            await forbiddenApi.saveSetting('store_11st', data)
                          }
                          const res = await proxyApi.elevenstSellerInfo()
                          if (res.success && res.data) {
                            const d = res.data
                            if (d.shipFromAddress) updateStoreField('11st', 'shipFromAddress', d.shipFromAddress)
                            if (d.returnAddress) updateStoreField('11st', 'returnAddress', d.returnAddress)
                            if (d.returnFee) updateStoreField('11st', 'returnFee', d.returnFee)
                            if (d.exchangeFee) updateStoreField('11st', 'exchangeFee', d.exchangeFee)
                            if (d.dispatchTemplateNo) updateStoreField('11st', 'dispatchTemplateNo', d.dispatchTemplateNo)
                            const tplList = Array.isArray(d.dispatchTemplateList) ? d.dispatchTemplateList : []
                            if (tplList.length > 0) {
                              setElevenstDispatchTemplateOptions(
                                tplList.map((t) => ({
                                  value: t.tmpltNo,
                                  label: t.reprYn === 'Y' ? `${t.tmpltNm} (대표)` : t.tmpltNm,
                                }))
                              )
                            }
                            const tplCount = tplList.length
                            const tplMsg = tplCount > 0
                              ? ` (발송정책 템플릿 ${fmtNum(tplCount)}건${d.dispatchTemplateName ? ` — 대표: ${d.dispatchTemplateName}` : ''})`
                              : ''
                            showAlert(`출고지/반품지 정보를 가져왔습니다.${tplMsg}`, 'success')
                          } else {
                            showAlert(res.message || '정보를 가져올 수 없습니다.', 'error')
                          }
                        } catch {
                          showAlert('출고지 정보 조회 실패', 'error')
                        }
                      }}
                      style={{ padding: '0.375rem 0.75rem', background: 'rgba(76,154,255,0.1)', border: '1px solid rgba(76,154,255,0.3)', borderRadius: '6px', fontSize: '0.75rem', color: '#4C9AFF', cursor: 'pointer', whiteSpace: 'nowrap', flexShrink: 0 }}
                    >출고지정보 가져오기</button>
                  )}
                </div>
              ))}
              {storeStatus[market.key] && (
                <div style={{ fontSize: '0.8125rem', color: storeStatus[market.key]?.includes('연결') || storeStatus[market.key]?.includes('저장') || storeStatus[market.key]?.includes('✓') ? '#51CF66' : storeStatus[market.key]?.includes('중...') ? '#FFD93D' : '#FF6B6B' }}>
                  {storeStatus[market.key]}
                </div>
              )}
            </div>

            {/* 설정 저장 */}
            <div style={{ marginTop: '1.5rem', display: 'flex', gap: '0.75rem', alignItems: 'center' }}>
              <button
                onClick={() => saveStoreSettings(market.key)}
                style={{ padding: '0.625rem 1.75rem', background: '#FF8C00', color: '#fff', border: 'none', borderRadius: '6px', fontWeight: 700, fontSize: '0.875rem', cursor: 'pointer' }}
              >설정 저장</button>
            </div>
          </div>

          <ConnectedAccountsList
            marketKey={market.key}
            accounts={accounts}
            editingAccountId={editingAccountId}
            setEditingAccountId={setEditingAccountId}
            setStoreData={setStoreData}
            setCoupangOutboundList={setCoupangOutboundList}
            setCoupangInboundList={setCoupangInboundList}
            handleAccountDelete={handleAccountDelete}
            handleAccountSetDefault={handleAccountSetDefault}
          />
        </div>
      ))}
    </div>
  )
}
