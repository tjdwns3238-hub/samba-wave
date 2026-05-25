'use client'

import { useState, useCallback, useEffect } from 'react'
import {
  accountApi,
  forbiddenApi,
  proxyApi,
  type SambaMarketAccount,
} from '@/lib/samba/api/commerce'
import { showAlert, showConfirm } from '@/components/samba/Modal'
import { fmtNum } from '@/lib/samba/styles'
import { STORE_MARKETS, SAFE_SELECT_DEFAULTS } from '../config'

export interface StoreSettingsState {
  accounts: SambaMarketAccount[]
  accountLoading: boolean
  storeTab: string
  visiblePasswords: Set<string>
  storeData: Record<string, Record<string, string>>
  savedStoreData: Record<string, Record<string, string>>
  storeStatus: Record<string, string>
  editingAccountId: string | null
  ssgShippingOptions: { value: string; label: string; divCd: number }[]
  ssgAddrOptions: { value: string; label: string }[]
  esmPlaceOptions: Record<string, { value: string; label: string }[]>
  esmDispatchOptions: Record<string, { value: string; label: string }[]>
  lotteonDeliveryPolicyOptions: { value: string; label: string }[]
  lotteonWarehouseOptions: { departure: { value: string; label: string }[]; return_: { value: string; label: string }[] }
  elevenstDispatchTemplateOptions: { value: string; label: string }[]
  lotteHomeDeliveryPolicyOptions: { value: string; label: string }[]
  lotteHomeExtraPolicyOptions: { value: string; label: string }[]
  lotteHomeShippingPlaceOptions: { value: string; label: string }[]
  lotteHomeReturnPlaceOptions: { value: string; label: string }[]
  networkIps: { web: string; local: string }
  networkIpStatus: string
  coupangOutboundList: Array<{ code: string; name: string; address: string }>
  coupangInboundList: Array<{ code: string; name: string; address: string; address_detail: string; zipcode: string; phone: string }>
}

export interface StoreSettingsActions {
  loadAccounts: () => Promise<void>
  loadStoreSettings: () => Promise<void>
  updateStoreField: (marketKey: string, fieldName: string, value: string) => void
  saveStoreSettings: (marketKey: string) => Promise<void>
  testStoreAuth: (marketKey: string) => Promise<void>
  handleAccountToggle: (id: string) => Promise<void>
  handleAccountDelete: (id: string) => Promise<void>
  handleAccountSetDefault: (id: string) => Promise<void>
  togglePasswordVisibility: (key: string) => void
  setStoreTab: (tab: string) => void
  setStoreData: React.Dispatch<React.SetStateAction<Record<string, Record<string, string>>>>
  setSsgShippingOptions: React.Dispatch<React.SetStateAction<{ value: string; label: string; divCd: number }[]>>
  setSsgAddrOptions: React.Dispatch<React.SetStateAction<{ value: string; label: string }[]>>
  setEsmPlaceOptions: React.Dispatch<React.SetStateAction<Record<string, { value: string; label: string }[]>>>
  setEsmDispatchOptions: React.Dispatch<React.SetStateAction<Record<string, { value: string; label: string }[]>>>
  setLotteonDeliveryPolicyOptions: React.Dispatch<React.SetStateAction<{ value: string; label: string }[]>>
  setLotteonWarehouseOptions: React.Dispatch<React.SetStateAction<{ departure: { value: string; label: string }[]; return_: { value: string; label: string }[] }>>
  setElevenstDispatchTemplateOptions: React.Dispatch<React.SetStateAction<{ value: string; label: string }[]>>
  setCoupangOutboundList: React.Dispatch<React.SetStateAction<Array<{ code: string; name: string; address: string }>>>
  setCoupangInboundList: React.Dispatch<React.SetStateAction<Array<{ code: string; name: string; address: string; address_detail: string; zipcode: string; phone: string }>>>
  loadCoupangShippingPlaces: (accountId?: string) => Promise<void>
  setLotteHomeDeliveryPolicyOptions: React.Dispatch<React.SetStateAction<{ value: string; label: string }[]>>
  setLotteHomeExtraPolicyOptions: React.Dispatch<React.SetStateAction<{ value: string; label: string }[]>>
  setLotteHomeShippingPlaceOptions: React.Dispatch<React.SetStateAction<{ value: string; label: string }[]>>
  setLotteHomeReturnPlaceOptions: React.Dispatch<React.SetStateAction<{ value: string; label: string }[]>>
  setEditingAccountId: (id: string | null) => void
  setVisiblePasswords: React.Dispatch<React.SetStateAction<Set<string>>>
  setNetworkIps: React.Dispatch<React.SetStateAction<{ web: string; local: string }>>
  saveNetworkIps: () => Promise<void>
}

export function useStoreSettings(): StoreSettingsState & StoreSettingsActions {
  const [accounts, setAccounts] = useState<SambaMarketAccount[]>([])
  const [accountLoading, setAccountLoading] = useState(true)
  const [storeTab, setStoreTab] = useState('smartstore')
  const [visiblePasswords, setVisiblePasswords] = useState<Set<string>>(new Set())
  const [storeData, setStoreData] = useState<Record<string, Record<string, string>>>({})
  const [savedStoreData, setSavedStoreData] = useState<Record<string, Record<string, string>>>({})
  const [storeStatus, setStoreStatus] = useState<Record<string, string>>({})
  const [editingAccountId, setEditingAccountId] = useState<string | null>(null)
  const [ssgShippingOptions, setSsgShippingOptions] = useState<{ value: string; label: string; divCd: number }[]>([])
  const [ssgAddrOptions, setSsgAddrOptions] = useState<{ value: string; label: string }[]>([])
  const [esmPlaceOptions, setEsmPlaceOptions] = useState<Record<string, { value: string; label: string }[]>>({})
  const [esmDispatchOptions, setEsmDispatchOptions] = useState<Record<string, { value: string; label: string }[]>>({})
  const [lotteonDeliveryPolicyOptions, setLotteonDeliveryPolicyOptions] = useState<{ value: string; label: string }[]>([])
  const [lotteonWarehouseOptions, setLotteonWarehouseOptions] = useState<{ departure: { value: string; label: string }[]; return_: { value: string; label: string }[] }>({ departure: [], return_: [] })
  const [elevenstDispatchTemplateOptions, setElevenstDispatchTemplateOptions] = useState<{ value: string; label: string }[]>([])
  const [networkIps, setNetworkIps] = useState({ web: '', local: '' })
  const [networkIpStatus, setNetworkIpStatus] = useState('')
  const [coupangOutboundList, setCoupangOutboundList] = useState<Array<{ code: string; name: string; address: string }>>([])
  const [coupangInboundList, setCoupangInboundList] = useState<Array<{ code: string; name: string; address: string; address_detail: string; zipcode: string; phone: string }>>([])
  const [lotteHomeDeliveryPolicyOptions, setLotteHomeDeliveryPolicyOptions] = useState<{ value: string; label: string }[]>([])
  const [lotteHomeExtraPolicyOptions, setLotteHomeExtraPolicyOptions] = useState<{ value: string; label: string }[]>([])
  const [lotteHomeShippingPlaceOptions, setLotteHomeShippingPlaceOptions] = useState<{ value: string; label: string }[]>([])
  const [lotteHomeReturnPlaceOptions, setLotteHomeReturnPlaceOptions] = useState<{ value: string; label: string }[]>([])

  const loadAccounts = useCallback(async () => {
    setAccountLoading(true)
    try { setAccounts(await accountApi.list()) } catch { /* ignore */ }
    setAccountLoading(false)
  }, [])

  // ※ 과거 버그: savedStoreData만 세팅하고 storeData는 빈 상태였음 → select UI는 첫 옵션이 시각적으로 보이지만 state는 ''라서
  //    저장 시 merge 로직이 select 필드값을 누락해 DB에 합배송 key 자체가 들어가지 않음 → 백엔드가 기본값 "Y"로 등록 (합배송 불가 UI와 불일치)
  //    → storeData도 함께 세팅 + 안전한 기본값이 명시된 select 필드(SAFE_SELECT_DEFAULTS)에 한해 초기값 주입해 일관성 확보
  const loadStoreSettings = useCallback(async () => {
    const loaded: Record<string, Record<string, string>> = {}
    const statuses: Record<string, string> = {}
    for (const market of STORE_MARKETS) {
      try {
        const data = await forbiddenApi.getSetting(`store_${market.key}`).catch(() => null) as Record<string, string> | null
        if (data && Object.keys(data).length > 0) {
          loaded[market.key] = data
          statuses[market.key] = '연결됨'
        }
      } catch { /* ignore */ }
    }
    // 안전한 기본값을 가진 select 필드에만 초기값 주입
    try {
      const network = await forbiddenApi.getSetting('store_network_ips').catch(() => null) as Record<string, string> | null
      setNetworkIps({
        web: String(network?.web || ''),
        local: String(network?.local || ''),
      })
      setNetworkIpStatus(network && (network.web || network.local) ? '저장됨' : '')
    } catch {
      setNetworkIpStatus('')
    }
    const withDefaults: Record<string, Record<string, string>> = {}
    for (const market of STORE_MARKETS) {
      const base = { ...(loaded[market.key] || {}) }
      for (const field of market.fields) {
        if (field.type === 'select' && field.name in SAFE_SELECT_DEFAULTS && !(field.name in base)) {
          base[field.name] = SAFE_SELECT_DEFAULTS[field.name]
        }
      }
      withDefaults[market.key] = base
    }
    // [근본수정] storeData(폼)는 mount 시 빈 상태로 시작 — store_${marketKey} 싱글톤은
    // 다계정일 때 마지막 저장값만 갖고 있어서 폼 prefill 시 다른 계정을 silent overwrite하는 사고 원인.
    // 기존 계정 편집은 ConnectedAccountsList의 "수정" 버튼으로만 진입. 빈 폼 = 신규 등록.
    // savedStoreData는 SSG/롯데ON/11번가 탭 진입 시 apiKey 체크용으로만 유지.
    setSavedStoreData(withDefaults)
    setStoreStatus(statuses)
    // [삭제됨] store_${market.key} 싱글톤 → SambaMarketAccount 소급 생성 로직 제거.
    // 사용자가 계정 삭제해도 다음 마운트 때 자동 부활하는 사고 원인이라 통째로 제거.
    // (과거 마이그레이션용 코드라 신규 사용자에겐 무용)
  }, [])

  const updateStoreField = (marketKey: string, fieldName: string, value: string) => {
    setStoreData(prev => ({
      ...prev,
      [marketKey]: { ...(prev[marketKey] || {}), [fieldName]: value }
    }))
  }

  const saveNetworkIps = async () => {
    try {
      const payload = {
        web: networkIps.web.trim(),
        local: networkIps.local.trim(),
      }
      await forbiddenApi.saveSetting('store_network_ips', payload)
      setNetworkIps(payload)
      setNetworkIpStatus('저장됨')
      showAlert('웹/로컬 IP를 저장했습니다.', 'success')
    } catch {
      setNetworkIpStatus('저장 실패')
      showAlert('웹/로컬 IP 저장 실패', 'error')
    }
  }

  const saveStoreSettings = async (marketKey: string) => {
    try {
      // 기존 저장 데이터와 현재 입력 데이터 병합
      // select 필드에서 ''(설정안함)을 선택한 경우 해당 키 삭제
      const current = storeData[marketKey] || {}
      // 복수구매할인 '설정함' + 값 미입력 시 silent disable 방지 — 저장 차단
      if (
        String(current.multiPurchaseDiscount || '').toLowerCase() === 'true'
      ) {
        const qty = String(current.multiPurchaseQty || '').trim()
        const amtKey = marketKey === '11st' ? 'multiPurchaseAmt' : 'multiPurchaseRate'
        const amt = String(current[amtKey] || '').trim()
        if (!qty || qty === '0' || !amt || amt === '0') {
          showAlert(
            `복수구매할인을 '설정함'으로 선택했으면 'N개 이상' 과 '${marketKey === '11st' ? '개당 할인값' : '할인율'}' 을 모두 입력해야 합니다.`,
            'error'
          )
          return
        }
      }
      const marketCfgForMerge = STORE_MARKETS.find(m => m.key === marketKey)
      const selectFields = new Set(
        (marketCfgForMerge?.fields ?? []).filter(f => f.type === 'select').map(f => f.name)
      )
      const passwordFieldsForMerge = new Set(
        (marketCfgForMerge?.fields ?? []).filter(f => f.type === 'password').map(f => f.name)
      )
      const clearKeys = Object.entries(current)
        .filter(([k, v]) => v === '' && selectFields.has(k))
        .map(([k]) => k)
      // password 필드는 빈 값이면 payload에서 제거 → 백엔드가 기존 값 유지
      // 마스킹값(****XXXX)도 동일하게 제거 (defense-in-depth, 백엔드 가드와 이중 방어)
      const filtered = Object.fromEntries(
        Object.entries(current).filter(([k, v]) => {
          if (v === '') return false
          if (passwordFieldsForMerge.has(k) && /^\*{4}.{0,4}$/.test(String(v))) return false
          return true
        })
      )
      // 완전 분리: savedStoreData(Settings 공통) 병합 제거 — account.additional_fields 기반으로만 저장
      const merged = { ...filtered }
      // select "설정안함" 선택 시 해당 키 삭제
      for (const k of clearKeys) delete merged[k]
      // lottehome 배송정책/출고지/반품지 필드: 폼에서 안 건드렸으면 savedStoreData 값 보존
      if (marketKey === 'lottehome') {
        const lhSettingFields = ['dlvPolcNo', 'addDlvPolcNo', 'corpRlsPlSn', 'corpDlvpSn']
        const savedLh = savedStoreData['lottehome'] || {}
        for (const f of lhSettingFields) {
          if (!merged[f] && savedLh[f]) merged[f] = savedLh[f]
        }
      }
      const data = merged
      await forbiddenApi.saveSetting(`store_${marketKey}`, data)

      // lottehome: proxy client는 lottehome_credentials 키를 읽으므로 함께 동기화
      if (marketKey === 'lottehome') {
        try {
          const existingCreds = await forbiddenApi.getSetting('lottehome_credentials').catch(() => null)
          const existingCredsObj = (existingCreds && typeof existingCreds === 'object') ? existingCreds as Record<string, unknown> : {}
          const lotteCreds: Record<string, unknown> = {
            ...existingCredsObj,
            userId: data.storeId || existingCredsObj.userId || '',
            agncNo: data.agncNo || existingCredsObj.agncNo || '',
            env: data.env || String(existingCredsObj.env || '') || 'prod',
          }
          if (data.password) lotteCreds.password = data.password
          await forbiddenApi.saveSetting('lottehome_credentials', lotteCreds)
        } catch { /* credentials 동기화 실패는 무시 */ }
      }

      const marketCfg = STORE_MARKETS.find(m => m.key === marketKey)
      const label = marketCfg?.label || marketKey

      // 계정 자동 생성/업데이트
      const sellerId = data.storeId || data.account || data.email || data.userId || data.vendorId || data.apiKey || ''
      const businessName = data.businessName || ''
      const authFieldFilled = !!(marketCfg?.authField && data[marketCfg.authField])
      if (sellerId || businessName || authFieldFilled) {
        // API 인증정보를 additional_fields에 저장 (계정별 독립 인증)
        // businessName, storeId는 account 필드로 분리 저장 — maxCount는 additional_fields에 포함
        // eslint-disable-next-line @typescript-eslint/no-unused-vars
        const { businessName: _bn, storeId: _si, ...apiFields } = data
        const accountData: Partial<SambaMarketAccount> = {
          market_type: marketKey,
          market_name: label,
          account_label: `${businessName}${sellerId ? '-' + (sellerId.length > 16 ? sellerId.slice(0, 8) + '...' : sellerId) : ''}`.replace(/^-|-$/g, '') || marketKey,
          seller_id: sellerId,
          business_name: businessName,
          is_active: true,
          additional_fields: apiFields, // clientId, clientSecret 등 API 인증정보
        }

        if (editingAccountId) {
          // 수정 모드: 해당 계정만 업데이트
          await accountApi.update(editingAccountId, accountData)
        } else {
          // [근본수정] 신규 모드: 무조건 CREATE.
          // 과거 find-by-seller_id 휴리스틱은 다계정 운영 시 stale state로 다른 계정을 덮어쓰는 사고를 일으킴
          // (가디·도놀 동시 등록 시 도놀 record가 가디 값으로 silent UPDATE되는 버그)
          await accountApi.create(accountData)
        }
        await loadAccounts()
      }
      // [근본수정] 저장 후 폼·편집상태 완전 비움 → 빈 폼 = 다음 신규 등록 진입점
      // 직전 저장값을 폼에 유지하면 사용자가 일부 필드만 바꿔 저장 시 silent overwrite 위험
      setSavedStoreData(prev => ({ ...prev, [marketKey]: { ...data } }))
      setStoreData(prev => { const next = { ...prev }; delete next[marketKey]; return next })
      setStoreStatus(prev => ({ ...prev, [marketKey]: '연결됨' }))
      setEditingAccountId(null)

      showAlert(`${label} 설정이 저장되었습니다.`, 'success')
    } catch { showAlert('저장 실패', 'error') }
  }

  const testStoreAuth = async (marketKey: string) => {
    const data = storeData[marketKey] || {}
    const hasKey = Object.values(data).some(v => v && v.length > 0)
    if (!hasKey) {
      setStoreStatus(prev => ({ ...prev, [marketKey]: '필드를 입력해주세요' }))
      return
    }
    setStoreStatus(prev => ({ ...prev, [marketKey]: '인증 확인 중...' }))
    try {
      // password 필드 가드: 빈 값 또는 마스킹값(****XXXX)은 payload에서 제거
      // 백엔드 store_* save_setting 가드가 누락된 키는 기존 DB 값으로 머지함
      // (과거 editingAccount.additional_fields에서 복원하려 했으나 그 값도 이미 마스킹된 응답이라 의미 없었음)
      const marketCfg = STORE_MARKETS.find(m => m.key === marketKey)
      const pwdFields = new Set(
        (marketCfg?.fields ?? []).filter(f => f.type === 'password').map(f => f.name)
      )
      const safeData = { ...data }
      for (const field of pwdFields) {
        const v = safeData[field]
        if (!v || /^\*{4}.{0,4}$/.test(String(v))) {
          delete safeData[field]
        }
      }
      // 먼저 설정 저장
      await forbiddenApi.saveSetting(`store_${marketKey}`, safeData)
      setSavedStoreData(prev => ({ ...prev, [marketKey]: { ...safeData } }))
      // 마켓별 인증 테스트
      let result: { success: boolean; message: string }
      if (marketKey === 'smartstore') {
        result = await proxyApi.smartstoreAuthTest()
      } else if (marketKey === '11st') {
        result = await proxyApi.elevenstAuthTest({
          api_key: String(safeData.apiKey || ''),
        })
      } else if (marketKey === 'coupang') {
        result = await proxyApi.coupangAuthTest({
          access_key: String(safeData.accessKey || ''),
          secret_key: String(safeData.secretKey || ''),
          vendor_id: String(safeData.vendorId || ''),
        })
      } else if (marketKey === 'lotteon') {
        // 멀티계정 환경 대응(2026-05-25) — 폼 입력값을 직접 전송.
        // store_lotteon 단일 키 폴백을 거치지 않고 폼값으로 인증 → 신규 등록 정확.
        const lotteonResult = await proxyApi.lotteonAuthTest({
          api_key: String(safeData.apiKey || ''),
          dv_cst_pol_no: String(safeData.dvCstPolNo || ''),
          owhp_no: String(safeData.owhpNo || ''),
          rtrp_no: String(safeData.rtrpNo || ''),
        })
        result = lotteonResult
        // 인증 성공 시 배송비정책/출고지/회수지 목록 자동 로드
        if (lotteonResult.success) {
          const [polRes, whRes] = await Promise.all([
            proxyApi.lotteonDeliveryPolicies(),
            proxyApi.lotteonWarehouses(),
          ])
          if (polRes.success) setLotteonDeliveryPolicyOptions(polRes.policies)
          if (whRes.success) setLotteonWarehouseOptions({ departure: whRes.departure, return_: whRes.return_ })
        }
      } else if (marketKey === 'ssg') {
        result = await proxyApi.ssgAuthTest({
          api_key: String(safeData.apiKey || ''),
        })
      } else if (marketKey === 'gsshop') {
        result = await proxyApi.gsshopAuthTest({
          store_id: String(safeData.storeId || ''),
          api_key_dev: String(safeData.apiKeyDev || ''),
          api_key_prod: String(safeData.apiKeyProd || ''),
        })
      } else if (marketKey === 'lottehome') {
        const userId = safeData.storeId || ''
        const password = safeData.password || ''
        const agncNo = safeData.agncNo || ''
        if (!userId || !password) {
          result = { success: false, message: '로그인 ID와 비밀번호를 입력해주세요.' }
        } else {
          result = await proxyApi.lottehomeAuth({ userId, password, agncNo, env: safeData.env || 'prod' })
        }
      } else if (marketKey === 'playauto') {
        result = await proxyApi.playautoAuthTest({
          api_key: String(safeData.apiKey || ''),
        })
      } else {
        result = await proxyApi.marketAuthTest(marketKey)
      }
      if (result.success) {
        setStoreStatus(prev => ({ ...prev, [marketKey]: `✓ ${result.message}` }))
        showAlert(result.message, 'success')
      } else {
        setStoreStatus(prev => ({ ...prev, [marketKey]: `✗ ${result.message}` }))
        showAlert(result.message, 'error')
      }
    } catch (e) {
      const msg = e instanceof Error ? e.message : '알 수 없는 오류'
      const displayMsg = msg === 'Failed to fetch'
        ? '백엔드 서버에 연결할 수 없습니다. 서버가 실행 중인지 확인해주세요.'
        : `인증 테스트 실패: ${msg}`
      setStoreStatus(prev => ({ ...prev, [marketKey]: '연결 실패' }))
      showAlert(displayMsg, 'error')
    }
  }

  // 쿠팡 출고지/반품지 목록 조회
  const loadCoupangShippingPlaces = useCallback(async (accountId?: string) => {
    try {
      // saveStoreSettings 끝에서 발생하는 두 가지 부작용을 차단:
      //   1) setEditingAccountId(null) — 우측 "수정중" 라벨 사라짐
      //   2) setStoreData(prev => delete prev[marketKey]) — 좌측 폼 입력란 모두 초기화
      // 정책: "저장" 버튼은 silent overwrite 방지로 폼을 비우는 게 맞지만, "조회" 흐름에서는
      // 사용자가 같은 폼에서 추가 작업하는 게 자연스러움 → 임시 보존 후 복원.
      const prevEditingAccountId = editingAccountId
      const prevStoreData = storeData['coupang']
      await saveStoreSettings('coupang')
      setEditingAccountId(prevEditingAccountId)
      if (prevStoreData && Object.keys(prevStoreData).length > 0) {
        setStoreData(prev => ({ ...prev, coupang: prevStoreData }))
      }
      const res = await proxyApi.coupangShippingPlaces(accountId)
      if (res.success && res.data) {
        setCoupangOutboundList(res.data.outboundList || [])
        setCoupangInboundList(res.data.inboundList || [])
        showAlert('출고지/반품지 목록을 가져왔습니다.', 'success')
      } else {
        showAlert(res.message || '출고지/반품지 조회 실패', 'error')
      }
    } catch {
      showAlert('출고지/반품지 조회 실패', 'error')
    }
  }, [saveStoreSettings, editingAccountId, storeData])

  // SSG 탭 진입 시 배송비/주소 옵션 자동 로드
  useEffect(() => {
    if (storeTab !== 'ssg') return
    if (ssgShippingOptions.length > 0 || ssgAddrOptions.length > 0) return
    const ssgData = savedStoreData['ssg'] || storeData['ssg'] || {}
    if (!ssgData.apiKey) return
    proxyApi.ssgShippingPolicies().then(res => {
      if (!res.success || !res.policies?.length) return
      const opts = res.policies.map((p: { shppcstId: string; feeAmt: number; prpayCodDivNm: string; shppcstAplUnitNm: string; divCd: number }) => {
        const fee = p.feeAmt ? `${fmtNum(Number(p.feeAmt))}원` : '무료'
        const parts = [p.shppcstId, fee]
        if (p.prpayCodDivNm) parts.push(p.prpayCodDivNm)
        if (p.shppcstAplUnitNm) parts.push(p.shppcstAplUnitNm)
        return { value: p.shppcstId, label: parts.join(' / '), divCd: p.divCd }
      })
      setSsgShippingOptions(opts)
    }).catch(() => {})
    proxyApi.ssgAddresses().then(res => {
      if (!res.success || !res.addresses?.length) return
      setSsgAddrOptions(res.addresses.map((a: { grpAddrId: string; doroAddrId?: string; addrNm: string; bascAddr: string }) => ({
        value: a.doroAddrId || a.grpAddrId,
        label: `${a.addrNm}${a.bascAddr ? ` (${a.bascAddr})` : ''}`,
      })))
    }).catch(() => {})
  }, [storeTab, savedStoreData, storeData, ssgShippingOptions.length, ssgAddrOptions.length])

  // 롯데ON 탭 진입 시 배송비정책/출고지/회수지 자동 로드
  useEffect(() => {
    if (storeTab !== 'lotteon') return
    if (lotteonDeliveryPolicyOptions.length > 0 || lotteonWarehouseOptions.departure.length > 0) return
    const d = savedStoreData['lotteon'] || storeData['lotteon'] || {}
    if (!d.apiKey) return
    Promise.all([proxyApi.lotteonDeliveryPolicies(), proxyApi.lotteonWarehouses()])
      .then(([polRes, whRes]) => {
        if (polRes.success) setLotteonDeliveryPolicyOptions(polRes.policies)
        if (whRes.success) setLotteonWarehouseOptions({ departure: whRes.departure, return_: whRes.return_ })
      }).catch(() => {})
  }, [storeTab, savedStoreData, storeData, lotteonDeliveryPolicyOptions.length, lotteonWarehouseOptions.departure.length])

  // 롯데홈쇼핑 배송비정책/출고지/반품지는 버튼 클릭 시에만 로드 (자동 로드 제거 — 동시 호출 시 롯데 API 오류 방지)

  // 11번가 탭 진입 시 발송마감 템플릿 자동 로드 (출고지 정보 응답에 포함)
  useEffect(() => {
    if (storeTab !== '11st') return
    if (elevenstDispatchTemplateOptions.length > 0) return
    const d = savedStoreData['11st'] || storeData['11st'] || {}
    if (!d.apiKey) return
    proxyApi.elevenstSellerInfo()
      .then((res) => {
        const list = res.success ? res.data?.dispatchTemplateList : null
        if (Array.isArray(list)) {
          setElevenstDispatchTemplateOptions(
            list.map((t) => ({
              value: t.tmpltNo,
              label: t.reprYn === 'Y' ? `${t.tmpltNm} (대표)` : t.tmpltNm,
            }))
          )
        }
      }).catch(() => {})
  }, [storeTab, savedStoreData, storeData, elevenstDispatchTemplateOptions.length])

  const handleAccountToggle = async (id: string) => {
    await accountApi.toggle(id)
    // 낙관적: 토글 즉시 로컬에서 is_active 반전 → UI 즉시 갱신
    setAccounts(prev => prev.map(a => a.id === id ? { ...a, is_active: !a.is_active } : a))
    await loadAccounts()
  }
  const handleAccountSetDefault = async (id: string) => {
    // 라디오 동작 — 백엔드가 같은 (tenant, market_type) 다른 계정 is_default=false 강제.
    // 낙관적 갱신: 즉시 로컬에서 라디오 상태 반영 → 백엔드 응답 후 재조회.
    const target = accounts.find(a => a.id === id)
    if (!target) return
    setAccounts(prev => prev.map(a =>
      a.market_type === target.market_type
        ? { ...a, is_default: a.id === id }
        : a
    ))
    try {
      await accountApi.setDefault(id)
    } catch (e) {
      // 실패 시 원복
      await loadAccounts()
      throw e
    }
    await loadAccounts()
  }

  const handleAccountDelete = async (id: string) => {
    if (!await showConfirm('삭제하시겠습니까?')) return
    await accountApi.delete(id)
    // [근본수정] 낙관적 갱신 + 재조회 시에도 삭제된 id 강제 제외:
    // 읽기 복제본 lag 때문에 await loadAccounts()가 막 삭제된 row를 다시 가져와
    // 카드가 사라졌다 되살아나는 버그가 있어 토스 등에서 "삭제가 안된다"고 보였음.
    setAccounts(prev => prev.filter(a => a.id !== id))
    // 편집 중이던 계정이 삭제되면 폼·편집상태도 비움
    if (editingAccountId === id) {
      setEditingAccountId(null)
      setStoreData(prev => { const next = { ...prev }; delete next[storeTab]; return next })
    }
    try {
      const fresh = await accountApi.list()
      setAccounts(fresh.filter(a => a.id !== id))
    } catch { /* ignore — 낙관적 제거 상태 유지 */ }
  }

  const togglePasswordVisibility = (key: string) => {
    setVisiblePasswords(prev => {
      const n = new Set(prev)
      if (n.has(key)) { n.delete(key) } else { n.add(key) }
      return n
    })
  }

  return {
    accounts,
    accountLoading,
    storeTab,
    visiblePasswords,
    storeData,
    savedStoreData,
    storeStatus,
    editingAccountId,
    ssgShippingOptions,
    ssgAddrOptions,
    esmPlaceOptions,
    esmDispatchOptions,
    lotteonDeliveryPolicyOptions,
    lotteonWarehouseOptions,
    elevenstDispatchTemplateOptions,
    networkIps,
    networkIpStatus,
    coupangOutboundList,
    coupangInboundList,
    lotteHomeDeliveryPolicyOptions,
    lotteHomeExtraPolicyOptions,
    lotteHomeShippingPlaceOptions,
    lotteHomeReturnPlaceOptions,
    loadAccounts,
    loadStoreSettings,
    updateStoreField,
    saveNetworkIps,
    saveStoreSettings,
    testStoreAuth,
    handleAccountToggle,
    handleAccountDelete,
    handleAccountSetDefault,
    togglePasswordVisibility,
    setStoreTab,
    setStoreData,
    setSsgShippingOptions,
    setSsgAddrOptions,
    setEsmPlaceOptions,
    setEsmDispatchOptions,
    setLotteonDeliveryPolicyOptions,
    setLotteonWarehouseOptions,
    setElevenstDispatchTemplateOptions,
    setCoupangOutboundList,
    setCoupangInboundList,
    loadCoupangShippingPlaces,
    setLotteHomeDeliveryPolicyOptions,
    setLotteHomeExtraPolicyOptions,
    setLotteHomeShippingPlaceOptions,
    setLotteHomeReturnPlaceOptions,
    setEditingAccountId,
    setVisiblePasswords,
    setNetworkIps,
  }
}
