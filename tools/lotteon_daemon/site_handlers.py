"""사이트별 PDP 추출 + 로그인 핸들러 레지스트리.

`daemon.py` 가 process_job 에서 site 별로 분기하기 위한 dispatch 테이블.

지원 사이트:
- LOTTEON: 로그인 필수, DOM 추출
- ABCmart/GrandStage: 로그인 시 best_benefit_price 정확, in-tab fetch + DOM 폴백
- SSG: 로그인 불필요, 임직원 alert 자동 dismiss

각 사이트 EXTRACT_JS 는 `backend/domain/samba/plugins/sourcing/<site>.py` 가 dom_ext
에서 읽는 필드 스키마와 일치해야 한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SiteHandler:
    site: str
    extract_js: str
    requires_login: bool = False
    login_url: str = ""
    home_url: str = ""
    login_selectors: dict[str, Any] = field(default_factory=dict)
    login_check_js: str = ""
    dialog_policy: str | None = None  # 'accept' / 'dismiss' / None
    pre_extract_wait_ms: int = 5_000
    pre_extract_marker_js: str = ""
    pre_extract_marker_timeout_ms: int = 15_000
    extract_retry_field: str = "best_benefit_price"
    # ── 송장(tracking) 전용 ──
    # 송장조회 페이지에서 택배사/송장번호를 추출하는 self-contained async IIFE.
    # 반환: {success, courierName, trackingNumber, error?, cancelled?}
    # 확장앱 content-tracking-*.js 를 Playwright evaluate 용으로 이식 + 웨일 CDP 실측 검증(2026-05-24).
    tracking_js: str = ""
    # 계정 전환용 로그아웃 URL (다른 계정 주문 송장조회 전 세션 정리).
    logout_url: str = ""
    # 송장조회는 마이페이지(개인 주문) 접근 → SSG 처럼 가격수집은 무로그인이어도 로그인 필수.
    tracking_requires_login: bool = True
    # 2단계(two-hop) 송장: 주문상세 진입 → 버튼 클릭 → 다른 페이지 네비 → 스크랩 (무신사).
    # tracking_two_hop=True 면 tracking_click_js(클릭) → 네비 대기 → tracking_js(스크랩) 순.
    tracking_two_hop: bool = False
    tracking_click_js: str = ""
    # 2단계 네비 도착 판정용 URL glob (Playwright wait_for_url).
    tracking_trace_url_glob: str = ""
    # 가격수집(detail) 지원 여부. False 면 송장 전용 — 기본 active_sites/startup 로그인에서 제외.
    detail_supported: bool = True
    # ── 발주취소(cancel_order) 전용 ──
    # 주문상세/취소 페이지 URL 템플릿 — {ord_no} 치환. 비어있으면 job.url 그대로 사용.
    cancel_url_template: str = ""
    # 취소 실행 IIFE. 반환: {success, cancelled, alreadyShipped?, reason?, error?}.
    # 사이트별 실제 UX 는 웨일 CDP 실측 후 작성 필수 (추측 금지).
    # 빈 값이면 데몬이 "미지원" 회신 — 부작용 없음.
    cancel_js: str = ""
    cancel_requires_login: bool = True
    # 2단계 취소(주문상세→취소버튼 클릭→확인 페이지→최종 확인).
    cancel_two_hop: bool = False
    cancel_click_js: str = ""
    cancel_trace_url_glob: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# ABCmart / GrandStage
# ─────────────────────────────────────────────────────────────────────────────

ABCMART_LOGIN_URL = "https://abcmart.a-rt.com/login"
ABCMART_HOME_URL = "https://abcmart.a-rt.com"
# 실 페이지 검증 결과(2026-05-23) — `#username`/`#password`/`#login` (id 버튼).
# 버튼은 type=button 으로 form submit 아님. click() 시 abc.login.* JS 핸들러 호출.
ABCMART_LOGIN_SELECTORS = {
    "id": ["#username", 'input[name="username"]'],
    "pw": ["#password", 'input[name="password"]'],
    "btn": ["#login", 'input[type="button"][value*="로그인"]'],
}

# ABCmart 로그인 체크 JS — loginYn(세션 기반) 우선, 헤더 영문 토큰 폴백.
# 과거 한글 토큰("로그아웃/마이페이지") 추측은 ABCmart 헤더가 영문(LOGOUT/LOGIN/JOIN)이라
# 로그인 상태에서도 항상 'unknown' → 로그인 확정 실패 → 사이트 제외 사고. loginYn 으로 교체.
ABCMART_LOGIN_CHECK_JS = r"""
(async () => {
  try {
    // loginYn — 세션 기반 확실한 신호. 전용 member API 가 없어 /product/info 로 세션값 조회.
    try {
      const r = await fetch('/product/info?prdtNo=1010103285', {
        credentials: 'include',
        headers: { 'X-Requested-With': 'XMLHttpRequest', 'Accept': 'application/json' }
      })
      const j = await r.json()
      if (j && typeof j.loginYn === 'string') {
        return j.loginYn.toUpperCase() === 'Y' ? 'logged_in' : 'logged_out'
      }
    } catch (_) {}
    // 폴백: 헤더 토큰 (ABCmart 헤더는 LOGOUT/LOGIN/JOIN 영문)
    const txt = (document.body?.innerText || '').substring(0, 600)
    if (/\bLOGOUT\b|로그아웃/.test(txt)) return 'logged_in'
    if (/\bLOGIN\b|\bJOIN\b|로그인|회원가입/.test(txt)) return 'logged_out'
    return 'unknown'
  } catch (_) { return 'unknown' }
})()
"""

_ABCMART_MARKER_JS = r"""
(() => {
  try {
    const t = (document.body && document.body.innerText) || ''
    // fast-path: 혜택가 텍스트 보이면 즉시 hit.
    if (/최대\s*혜택가\s*[\d,]+\s*원/.test(t)) return true
    // 혜택가 없는 상품(쿠폰/멤버십 0) 무한 대기 차단 — 상품명 요소 렌더되면 ready 판정.
    // extract_js 가 API fetch 로 _apiBenefit 폴백 처리하므로 best_benefit_price 누락 안 됨.
    if (document.querySelector(
      '.product-detail .prd-name, .prdt-name, [class*="prdName"], [class*="productName"], h1'
    )) return true
    // 최후 폴백: 페이지 거의 다 그려진 상태 + 본문 충분히 채워짐.
    if (document.readyState === 'complete' && t.length > 800) return true
    return false
  } catch (_) { return false }
})()
"""

_ABCMART_EXTRACT_JS = r"""
(async () => {
  const _prdt = window.__PRD_ID__ || ''
  const _result = {
    success: false,
    site_product_id: _prdt,
    name: '',
    original_price: 0,
    sale_price: 0,
    best_benefit_price: 0,
    source_site: 'ABCmart',
    options: [],
    images: [],
    login_required: false,
    _domLoginSignal: 'ambiguous',
  }
  try {
    let apiData = null
    try {
      const resp = await fetch(`/product/info?prdtNo=${_prdt}`, {
        credentials: 'include',
        headers: {
          'Accept': 'application/json, text/plain, */*',
          'X-Requested-With': 'XMLHttpRequest',
        }
      })
      const text = await resp.text()
      try { apiData = JSON.parse(text) } catch (_) {}
    } catch (_) {}

    if (apiData && apiData.prdtName) {
      const pi = apiData.productPrice || {}
      const displayPrice = parseInt(apiData.displayProductPrice || 0)
      const sellAmt = parseInt(pi.sellAmt || 0)
      const normalAmt = parseInt(pi.normalAmt || 0)
      const alwaysDscntAmt = parseInt(apiData.alwaysDscntAmt || 0)
      const loginYn = (apiData.loginYn || '').toUpperCase()
      const coupons = apiData.maxBenefitCoupon || apiData.coupon || []
      const salePrice = displayPrice > 0 ? displayPrice : sellAmt
      let couponDiscount = 0
      for (const c of coupons) couponDiscount += parseInt(c.dscntAmt || 0)
      let benefit = salePrice - alwaysDscntAmt - couponDiscount
      if (benefit <= 0 || benefit > salePrice) benefit = salePrice
      _result.name = (apiData.prdtName || '').trim()
      _result.sale_price = salePrice
      _result.original_price = normalAmt || salePrice
      // API 계산은 폴백용으로만 보관 — best_benefit_price 는 DOM "최대 혜택가" 1순위.
      _result._apiBenefit = loginYn === 'Y' ? benefit : 0
      _result._domLoginSignal = loginYn === 'Y' ? 'logout_link' : 'login_link'
      _result.login_required = loginYn !== 'Y'
      if (salePrice > 0) _result.success = true
    }

    try {
      const optEls = document.querySelectorAll(
        '[data-prdt-option], .option-list li, .product-option li'
      )
      optEls.forEach((el) => {
        const nm = (el.textContent || '').trim().replace(/\s+/g, ' ')
        if (!nm) return
        const soldOut = /품절|sold\s*out/i.test(nm) || el.classList.contains('disabled')
        _result.options.push({ name: nm.slice(0, 60), stock: soldOut ? 0 : null, isSoldOut: soldOut })
      })
    } catch (_) {}

    // 최대혜택가: DOM 표시값 1순위. ABCmart API의 alwaysDscntAmt는 등급별 실적용
    // 멤버십과 불일치(예: API 3,000 vs 페이지 2,700) → 재계산 시 300원 과할인.
    // 페이지가 등급+쿠폰 모두 반영해 표시한 "최대 혜택가"가 100% 정확. 확장앱과 동일 정책.
    {
      const bodyText = document.body?.innerText || ''
      const m = bodyText.match(/최대\s*혜택가\s*([\d,]+)\s*원/)
      if (m) {
        const v = parseInt(m[1].replace(/,/g, ''), 10)
        if (v > 0) _result.best_benefit_price = v
      }
    }
    // DOM 미표기 상품(쿠폰/멤버십 0)만 API 계산 폴백
    if (!_result.best_benefit_price && _result._apiBenefit > 0) {
      _result.best_benefit_price = _result._apiBenefit
    }

    try {
      const imgs = document.querySelectorAll('.product-detail-images img, .swiper-slide img, .thumb img')
      imgs.forEach((img) => {
        let src = img.src || img.getAttribute('data-src') || ''
        if (src.startsWith('//')) src = 'https:' + src
        if (src && !_result.images.includes(src) && _result.images.length < 9) _result.images.push(src)
      })
    } catch (_) {}

    return _result
  } catch (e) {
    return { ..._result, success: false, error: String(e) }
  }
})()
"""


# ─────────────────────────────────────────────────────────────────────────────
# SSG
# ─────────────────────────────────────────────────────────────────────────────

_SSG_MARKER_JS = r"""
(() => {
  try {
    const _src = document.documentElement ? document.documentElement.outerHTML : ''
    if (_src.indexOf('임직원 및 사업자 회원') !== -1 || _src.indexOf('임직원만 구매') !== -1) {
      window.__SSG_STAFF_ONLY__ = true
      return true
    }
    const _href = location.href || ''
    if (_href.indexOf('member.ssg.com/member/login') !== -1) {
      window.__SSG_STAFF_ONLY__ = true; return true
    }
    if (document.title === 'flagMsg') { window.__SSG_STAFF_ONLY__ = true; return true }
    if (_href && _href.indexOf('itemView.ssg') === -1) { window.__SSG_STAFF_ONLY__ = true; return true }
    const hasObj = !!(window.resultItemObj && window.resultItemObj.itemNm)
    if (!hasObj) return false
    return true
  } catch (_) { return false }
})()
"""

# SSG 추출 — domCardPrice > domSalePrice > resultItemObj. sellprc = 정상가 (영구 금지: salePrice 절대 X).
_SSG_EXTRACT_JS = r"""
(() => {
  try {
    if (window.__SSG_STAFF_ONLY__) {
      return { success: false, staffOnly: true, source_site: 'SSG' }
    }
    const obj = window.resultItemObj || {}
    const _intVal = (v) => parseInt((v || '0').toString().replace(/[^0-9]/g, ''), 10) || 0

    let domCardPrice = 0
    let domSalePrice = 0
    try {
      // 카드혜택가: "카드혜택가" dt → dd em.ssg_price 1순위 (현 SSG 레이아웃, dt 복귀).
      // .cdtl_price.point 는 판매가라 카드가로 쓰면 안 됨 — 과거 카드가였으나 레이아웃 변경으로
      // 판매가를 잡아 cost=판매가 오류 발생(검증 2026-05-23: 카드 115,200인데 128,000 잡힘).
      document.querySelectorAll('dt').forEach((dt) => {
        if (dt.textContent.trim() !== '카드혜택가') return
        const dd = dt.nextElementSibling
        if (dd) {
          const em = dd.querySelector('em.ssg_price') || dd
          const v = _intVal(em.textContent)
          if (v) domCardPrice = v
        }
      })
      // 폴백: 모바일/구 레이아웃 카드가 영역
      if (!domCardPrice) {
        const cardEl = document.querySelector(
          '.mndtl_card_price em.ssg_price, .mndtl_card_btnmore .ssg_price, .cdtl_card_price .ssg_price'
        )
        if (cardEl) domCardPrice = _intVal(cardEl.textContent)
      }
      const saleEl = document.querySelector('.cdtl_new_price.notranslate em.ssg_price, .cdtl_price .ssg_price')
      if (saleEl) domSalePrice = _intVal(saleEl.textContent)
    } catch (_) {}

    const sellprc = _intVal(obj.sellprc)
    const bestAmt = _intVal(obj.bestAmt)
    const norprc = _intVal(obj.norprc || obj.orgPrc)
    const salePrice = domSalePrice || bestAmt
    const originalPrice = norprc || sellprc || salePrice
    const cost = domCardPrice || bestAmt || salePrice

    // 옵션별 실재고: resultItemObj.uitemObjList.usablInvQty 1순위 (확장앱
    // background-sourcing.js:2833 과 동일). 백엔드 ssg.py 가 uitemOptions 키로
    // 실재고 보정. uitemObjList 없을 때만 DOM li 폴백(품절 여부만).
    const options = []
    const uitemOptions = []
    try {
      const ul = Array.isArray(obj.uitemObjList) ? obj.uitemObjList : []
      ul.forEach((u) => {
        const nm = [u.uitemOptnNm1, u.uitemOptnNm2, u.uitemOptnNm3].filter(Boolean).join('/')
          || u.optnDisplayNm || u.optnNm || u.uitemNm || ''
        if (!nm) return
        const qty = parseInt(u.usablInvQty) || 0
        uitemOptions.push({ name: nm, usablInvQty: qty, isSoldOut: qty === 0 })
        options.push({ name: nm, stock: qty, isSoldOut: qty === 0 })
      })
      if (!options.length) {
        document.querySelectorAll('.cdtl_opt_list li, [class*="option"] li').forEach((el) => {
          const nm = (el.textContent || '').trim().replace(/\s+/g, ' ')
          if (!nm) return
          const soldOut = /품절/.test(nm) || el.classList.contains('disabled')
          options.push({ name: nm.slice(0, 60), stock: soldOut ? 0 : null, isSoldOut: soldOut })
        })
      }
    } catch (_) {}

    const images = []
    try {
      document.querySelectorAll('.cdtl_imgview img, .swiper-slide img, [class*="thumb"] img').forEach((img) => {
        let src = img.src || img.getAttribute('data-src') || ''
        if (src.startsWith('//')) src = 'https:' + src
        if (src && !images.includes(src) && images.length < 9) images.push(src)
      })
    } catch (_) {}

    return {
      success: salePrice > 0 || cost > 0,
      site_product_id: window.__PRD_ID__ || '',
      name: (obj.itemNm || document.querySelector('meta[property="og:title"]')?.content || '').trim(),
      original_price: originalPrice,
      sale_price: salePrice,
      best_benefit_price: cost,
      domCardPrice,
      domSalePrice,
      images,
      options,
      uitemOptions,
      source_site: 'SSG',
    }
  } catch (e) {
    return { success: false, error: String(e), source_site: 'SSG' }
  }
})()
"""


# ─────────────────────────────────────────────────────────────────────────────
# 송장(tracking) 추출 JS — 웨일 CDP 실측 검증(2026-05-24). 확장앱 content-tracking-*.js 이식.
# ─────────────────────────────────────────────────────────────────────────────

# ABCmart/GrandStage — div.status-info .info-desc(택배사) + .info-link(송장)
_ABCMART_TRACKING_JS = r"""
(async () => {
  const href=location.href||'';
  if(href.indexOf('/member/login')!==-1 || href.indexOf('order-detail')===-1)
    return {success:false, needsLogin:true, error:'needs_login/redirect'};
  const t=(document.body?.innerText||'').slice(0,8000);
  if(/(취소완료|취소처리완료|구매취소완료|주문이\s*취소|취소된\s*주문)/.test(t)) return {success:false,cancelled:true,error:'order_cancelled'};
  const t0=Date.now(); let c=null;
  while(Date.now()-t0<10000){ c=document.querySelector('div.status-info .info-desc'); if(c&&(c.textContent||'').trim())break; await new Promise(r=>setTimeout(r,300)); }
  if(!c) return {success:false, error:'no_tracking: status-info 미로드 (미발송)'};
  const courierName=c.textContent.trim();
  const te=document.querySelector('div.status-info .info-link'); const raw=(te?.textContent||'').trim(); const m=raw.match(/\d{8,}/);
  const trackingNumber=m?m[0]:raw;
  if(!trackingNumber) return {success:false, error:'no_tracking: 송장번호 미표시', courierName};
  return {success:true, courierName, trackingNumber};
})()
"""

# LOTTEON — "배송상세조회" 버튼 폴링 → 클릭 → dialog 내 택배사/송장
_LOTTEON_TRACKING_JS = r"""
(async () => {
  const href=location.href||'';
  if(href.indexOf('member.lotteon.com')!==-1 || href.indexOf('/member/login')!==-1)
    return {success:false, needsLogin:true, error:'needs_login redirect'};
  // SPA 본문 로드 대기
  const tw=Date.now();
  while(Date.now()-tw<25000){ if(location.href.indexOf('lotteon.com')!==-1 && document.readyState!=='loading') break; await new Promise(r=>setTimeout(r,300)); }
  const txt=(document.body?.innerText||'').slice(0,8000);
  if(/(취소완료|취소처리완료|구매취소완료|주문이\s*취소|취소된\s*주문)/.test(txt)) return {success:false,cancelled:true,error:'order_cancelled'};
  let dialog=document.querySelector('dialog[open], [role="dialog"]');
  if(!dialog){
    const findBtn=()=>{ for(const b of document.querySelectorAll('button')){ if((b.textContent||'').trim().includes('배송상세조회'))return b; } for(const a of document.querySelectorAll('a')){ if((a.textContent||'').includes('배송상세조회'))return a; } return null; };
    let el=null; const tb=Date.now();
    while(Date.now()-tb<15000){ el=findBtn(); if(el)break; await new Promise(r=>setTimeout(r,400)); }
    if(!el) return {success:false, error:'no_tracking: 배송상세조회 버튼 없음 (미발송/선물주문)'};
    el.click();
    const td=Date.now();
    while(Date.now()-td<8000){ dialog=document.querySelector('dialog[open], [role="dialog"]'); if(dialog)break; await new Promise(r=>setTimeout(r,300)); }
    if(!dialog) return {success:false, error:'dialog 미열림'};
  }
  await new Promise(r=>setTimeout(r,1200));
  const field=(label)=>{ for(const e of dialog.querySelectorAll('*')){ if((e.textContent||'').trim()===label && e.children.length===0){ const s=e.nextElementSibling; if(!s)continue; const lk=s.querySelector('a')||(s.tagName==='A'?s:null); return (lk?.textContent||s.textContent||'').trim(); } } return ''; };
  let courierName=field('택배사'); let trackingNumber=field('송장번호');
  if(!trackingNumber){ for(const lk of dialog.querySelectorAll('a[href*="tracking"], a[href*="InvNo"]')){ const x=lk.textContent.trim(); if(/^\d{8,}$/.test(x)){trackingNumber=x;break;} } }
  if(!trackingNumber) return {success:false, error:'no_tracking: 송장번호 미표시', courierName};
  return {success:true, courierName, trackingNumber};
})()
"""

LOTTEON_LOGOUT_URL = "https://www.lotteon.com/p/member/logout"
ABCMART_LOGOUT_URL = "https://abcmart.a-rt.com/member/logout"

# ── SSG 송장 + 로그인 (마이페이지는 가격수집과 달리 로그인 필수) ──
# 로그인폼 실측(2026-05-24, incognito): #mem_id / #mem_pw / #loginBtn.
SSG_TRACKING_LOGIN_URL = "https://member.ssg.com/member/login.ssg"
SSG_HOME_URL = "https://www.ssg.com/"
SSG_LOGOUT_URL = "https://www.ssg.com/comm/login/logout.ssg"
SSG_LOGIN_SELECTORS = {
    "id": ["#mem_id", 'input[name="mbrLoginId"]'],
    "pw": ["#mem_pw", 'input[name="password"]'],
    "btn": ["#loginBtn", 'button[type="submit"].cmem_btn'],
}
SSG_LOGIN_CHECK_JS = r"""
(() => {
  try {
    const t = (document.querySelector('.gnb_utmenu, .gnb_util, header')?.innerText
      || (document.body?.innerText || '').slice(0, 500)).replace(/\s+/g, ' ');
    if (t.includes('로그아웃')) return 'logged_in';
    if (t.includes('로그인') || t.includes('회원가입')) return 'logged_out';
    return 'unknown';
  } catch (e) { return 'unknown'; }
})()
"""
# SSG 송장 스크랩 — .tx_state em (택배사 span + 송장번호 텍스트). 웨일 CDP 실측 검증.
_SSG_TRACKING_JS = r"""
(async () => {
  const href=location.href||'';
  if(href.indexOf('member.ssg.com')!==-1 || href.indexOf('/member/login')!==-1 || href.indexOf('orderInfoDetail.ssg')===-1)
    return {success:false, needsLogin:true, error:'needs_login/redirect'};
  const t=(document.body?.innerText||'').slice(0,8000);
  if(/(취소완료|취소처리완료|구매취소완료|주문이\s*취소|취소된\s*주문)/.test(t)) return {success:false,cancelled:true,error:'order_cancelled'};
  const t0=Date.now(); let c=null;
  while(Date.now()-t0<12000){ c=document.querySelector('.tx_state em'); if(c)break; await new Promise(r=>setTimeout(r,300)); }
  if(!c) return {success:false, error:'no_tracking: .tx_state 미로드 (미발송)'};
  const courierName=(c.querySelector('span')?.textContent||'').trim();
  let trackingNumber=''; for(const n of c.childNodes){ if(n.nodeType===3){ const m=n.textContent.match(/\d{8,}/); if(m){trackingNumber=m[0];break;} } }
  if(!trackingNumber) return {success:false, error:'no_tracking: 송장번호 미표시', courierName};
  return {success:true, courierName, trackingNumber};
})()
"""

# ── MUSINSA 송장 (2단계) + 로그인 ──
# 로그인폼 실측(2026-05-24, incognito): member.one.musinsa.com/login SPA.
#   id=input.login-v2-input__input[type=text], pw=[type=password], btn=button.login-v2-button__item[type=submit]
MUSINSA_LOGIN_URL = "https://www.musinsa.com/auth/login"
MUSINSA_HOME_URL = "https://www.musinsa.com/mypage"
MUSINSA_LOGOUT_URL = "https://www.musinsa.com/auth/logout"
MUSINSA_LOGIN_SELECTORS = {
    "id": [
        'input.login-v2-input__input[type="text"]',
        'input[type="text"].login-v2-input__input',
    ],
    "pw": [
        'input.login-v2-input__input[type="password"]',
        'input[type="password"].login-v2-input__input',
    ],
    "btn": ['button.login-v2-button__item[type="submit"]', 'button[type="submit"]'],
}
MUSINSA_LOGIN_CHECK_JS = r"""
(() => {
  try {
    const h = location.href || '';
    if (h.indexOf('/auth/login') !== -1 || h.indexOf('member.one.musinsa.com') !== -1) return 'logged_out';
    if (/\/mypage/.test(h)) return 'logged_in';
    return 'unknown';
  } catch (e) { return 'unknown'; }
})()
"""
# 1단계: 주문상세에서 "배송조회" 버튼 폴링 → wrong_account 체크 → 클릭.
_MUSINSA_TRACKING_CLICK_JS = r"""
(async () => {
  const isWrong=()=>{ const t=(document.body?.innerText||'').slice(0,4000); return /주문\s*정보를?\s*찾을\s*수\s*없|잘못된\s*접근/.test(t); };
  const t=(document.body?.innerText||'').slice(0,8000);
  if(/(취소완료|취소처리완료|구매취소완료|주문이\s*취소|취소된\s*주문)/.test(t)) return {clicked:false, cancelled:true, error:'order_cancelled'};
  const t0=Date.now(); let btn=null;
  while(Date.now()-t0<15000){
    if(isWrong()) return {clicked:false, error:'wrong_account: 현 로그인 계정에 주문 없음'};
    const bs=Array.from(document.querySelectorAll('button'));
    btn=bs.find(b=>{ const x=(b.textContent||'').replace(/\s+/g,'').trim(); return x==='배송조회'; });
    if(btn && !btn.disabled) break;
    await new Promise(r=>setTimeout(r,300));
  }
  if(!btn) return {clicked:false, error:'no_tracking: 배송조회 버튼 없음 (배송대기/미발송)'};
  btn.click();
  return {clicked:true};
})()
"""
# 2단계: trace 페이지에서 택배사/송장 스크랩.
_MUSINSA_TRACKING_TRACE_JS = r"""
(async () => {
  if((document.title||'').toLowerCase().includes('보안 인증')) return {success:false, error:'captcha'};
  if(/정상적인\s*접근이\s*아닙니다/.test((document.body?.innerText||'').slice(0,2000))) return {success:false, error:'abnormal_access'};
  const t0=Date.now(); let ce=null;
  while(Date.now()-t0<20000){ ce=document.querySelector('p.company-name'); if(ce&&(ce.textContent||'').trim())break; await new Promise(r=>setTimeout(r,300)); }
  if(!ce) return {success:false, error:'no_tracking: 택배사 DOM 미로드 (미발송)'};
  const courierName=ce.textContent.trim();
  const te=document.querySelector('button.tracking-number');
  const trackingNumber=(te?.textContent||'').trim();
  if(!trackingNumber) return {success:false, error:'no_tracking: 송장번호 없음', courierName};
  return {success:true, courierName, trackingNumber};
})()
"""


# ── GSShop 송장 + 로그인 ──
# 로그인폼 실측(2026-05-25, /cust/login/login.gs 정적 HTML): #id / #passwd / #btnLogin.
# reCAPTCHA 존재 — failCnt 누적 시 발동 가능. storage_state 영속화로 재로그인 빈도 최소화.
# 데몬 송장 전용 — 가격수집(detail) 미지원 (extension 가격수집 흐름 유지).
GSSHOP_LOGIN_URL = "https://www.gsshop.com/cust/login/login.gs"
GSSHOP_HOME_URL = "https://www.gsshop.com/ord/dlvcursta/ordList.gs"
GSSHOP_LOGOUT_URL = "https://www.gsshop.com/cust/login/logout.gs"
GSSHOP_LOGIN_SELECTORS = {
    "id": ["#id", 'input[name="id"]'],
    "pw": ["#passwd", 'input[name="passwd"]'],
    "btn": ["#btnLogin", 'button[type="button"]#btnLogin'],
}
GSSHOP_LOGIN_CHECK_JS = r"""
(() => {
  try {
    const t = (document.body?.innerText || '').slice(0, 1500).replace(/\s+/g, ' ');
    if (t.includes('로그아웃')) return 'logged_in';
    if (t.includes('로그인') || t.includes('회원가입')) return 'logged_out';
    return 'unknown';
  } catch (e) { return 'unknown'; }
})()
"""
# GSShop 송장 스크랩 — content-tracking-gsshop.js 이식.
# 팝업 URL(ordDtl.gs)에서 a[data-action="dlvTrace"] data-* 속성 추출.
# 택배사 코드 매핑은 overlink config.js 기준 (CJ/HJ/KG/LO/LT/EP/POST/RZ/DS/IL/KD/CH/HD/SL/CR/DH/GS).
_GSSHOP_TRACKING_JS = r"""
(async () => {
  const href=location.href||'';
  if(href.indexOf('/cust/login')!==-1 || href.indexOf('/login.gs')!==-1)
    return {success:false, needsLogin:true, error:'needs_login redirect'};
  const t=(document.body?.innerText||'').slice(0,8000);
  if(/(취소완료|취소처리완료|구매취소완료|주문이\s*취소|취소된\s*주문)/.test(t)) return {success:false,cancelled:true,error:'order_cancelled'};
  const MAP={CJ:'CJ대한통운',HJ:'한진택배',KG:'로젠택배',LO:'롯데택배',LT:'롯데택배',EP:'우체국택배',POST:'우체국택배',RZ:'로젠택배',DS:'대신택배',IL:'일양로지스',KD:'경동택배',CH:'천일택배',HD:'롯데택배',SL:'SLX택배',CR:'CVSnet편의점택배',DH:'DHL',GS:'GSMNtoN'};
  const t0=Date.now(); let a=null;
  while(Date.now()-t0<12000){ a=document.querySelector('a[data-action="dlvTrace"]'); if(a)break; await new Promise(r=>setTimeout(r,300)); }
  if(!a) return {success:false, error:'no_tracking: dlvTrace 링크 없음 (미발송)'};
  const code=(a.getAttribute('data-dlvs-co-cd')||'').toUpperCase();
  const trackingNumber=(a.getAttribute('data-inv-no')||'').trim();
  const courierName=MAP[code]||code||'';
  if(!trackingNumber) return {success:false, error:'no_tracking: data-inv-no 비어있음', courierName};
  return {success:true, courierName, trackingNumber};
})()
"""


# ── Nike 송장 + 로그인 ──
# nike-unite SSO (s3.nikecdn.com/unite/scripts/unite.min.js) - iframe 동적 렌더링.
# 셀렉터는 일반적 nike-unite 폼 추정값 (운영 검증 필요).
# 송장 URL: /kr/orders/sales/{ord_no}/ — content-tracking-nike.js 가 외부 배송조회 링크 href 파싱.
NIKE_LOGIN_URL = "https://www.nike.com/kr/login"
NIKE_HOME_URL = "https://www.nike.com/kr/member/orders"
NIKE_LOGOUT_URL = "https://www.nike.com/kr/logout"
# 추정값 — nike-unite 표준 input name. 실측 검증 필요.
NIKE_LOGIN_SELECTORS = {
    "id": [
        'input[name="emailAddress"]',
        'input[type="email"]',
        'input[name="email"]',
        "#email",
    ],
    "pw": [
        'input[name="password"]',
        'input[type="password"]',
        "#password",
    ],
    "btn": [
        'input[type="button"].nike-unite-submit-button',
        'button[type="submit"]',
        ".nike-unite-submit-button",
        'input[type="submit"]',
    ],
}
NIKE_LOGIN_CHECK_JS = r"""
(() => {
  try {
    const h = location.href || '';
    if (h.indexOf('/login') !== -1) return 'logged_out';
    const t = (document.body?.innerText || '').slice(0, 1500).replace(/\s+/g, ' ');
    if (/로그아웃|Sign Out|Logout/i.test(t)) return 'logged_in';
    if (/로그인|Sign In|Log In/i.test(t)) return 'logged_out';
    return 'unknown';
  } catch (e) { return 'unknown'; }
})()
"""
# Nike 송장 — content-tracking-nike.js 이식.
# 주문상세 페이지 외부 배송조회 링크 href 에서 도메인+파라미터 추출.
_NIKE_TRACKING_JS = r"""
(async () => {
  const href=location.href||'';
  if(href.indexOf('/login')!==-1)
    return {success:false, needsLogin:true, error:'needs_login redirect'};
  const t=(document.body?.innerText||'').slice(0,8000);
  if(/(취소완료|취소처리완료|구매취소완료|주문이\s*취소|취소된\s*주문|Cancell?ed)/i.test(t)) return {success:false,cancelled:true,error:'order_cancelled'};
  const URL_MAP={'cjlogistics.com':'CJ대한통운','hanjin.co.kr':'한진택배','lotteglogis.com':'롯데택배','epost.go.kr':'우체국택배','ilogen.com':'로젠택배','doortodoor.co.kr':'KGB택배','kdexp.com':'경동택배','cvsnet.co.kr':'CVSnet편의점택배','daesinlogistics.co.kr':'대신택배','ilyanglogis.com':'일양로지스'};
  const PARAM_MAP={'cjlogistics.com':'gnbInvcNo','hanjin.co.kr':'waybillNo','lotteglogis.com':'InvNo','epost.go.kr':'sid1','ilogen.com':'slipno'};
  const findLink=()=>{ for(const a of document.querySelectorAll('a[href]')){ const h=a.getAttribute('href')||''; for(const d of Object.keys(URL_MAP)){ if(h.includes(d)) return {a,h,d}; } } return null; };
  const t0=Date.now(); let info=null;
  while(Date.now()-t0<12000){ info=findLink(); if(info)break; await new Promise(r=>setTimeout(r,300)); }
  if(!info) return {success:false, error:'no_tracking: 배송조회 링크 없음 (미발송)'};
  const courierName=URL_MAP[info.d]||'';
  let trackingNumber='';
  try{ const u=new URL(info.h, location.href); const pn=PARAM_MAP[info.d]||''; if(pn) trackingNumber=u.searchParams.get(pn)||''; if(!trackingNumber){ for(const v of u.searchParams.values()){ if(/^\d{8,}$/.test(v)){trackingNumber=v;break;} } } }catch(_){}
  if(!trackingNumber) return {success:false, error:'no_tracking: 송장 파라미터 추출 실패', courierName};
  return {success:true, courierName, trackingNumber};
})()
"""


# ── OliveYoung 송장 + 로그인 ──
# 로그인 페이지 JS 렌더링 — 정적 HTML 셀렉터 추출 불가. 일반적 올리브영 폼 추정값.
# 송장 URL: /store/mypage/getOrderDetail.do?ordNo={ord_no} — content-tracking-oliveyoung.js 가 em 라벨 다음 노드 파싱.
OLIVEYOUNG_LOGIN_URL = "https://www.oliveyoung.co.kr/store/member/getLoginForm.do"
OLIVEYOUNG_HOME_URL = "https://www.oliveyoung.co.kr/store/mypage/main.do"
OLIVEYOUNG_LOGOUT_URL = "https://www.oliveyoung.co.kr/store/member/logout.do"
# 추정값 — 올리브영 일반 input id 패턴. 실측 검증 필요.
OLIVEYOUNG_LOGIN_SELECTORS = {
    "id": ["#loginId", "#userId", 'input[name="loginId"]', 'input[name="userId"]'],
    "pw": ["#passwd", "#password", 'input[name="passwd"]', 'input[type="password"]'],
    "btn": ["#btnLogin", "button.btn_login", 'button[type="submit"]', "a.btn_login"],
}
OLIVEYOUNG_LOGIN_CHECK_JS = r"""
(() => {
  try {
    const h = location.href || '';
    if (h.indexOf('/member/getLoginForm') !== -1 || h.indexOf('/login') !== -1) return 'logged_out';
    const t = (document.body?.innerText || '').slice(0, 1500).replace(/\s+/g, ' ');
    if (t.includes('로그아웃')) return 'logged_in';
    if (t.includes('로그인')) return 'logged_out';
    return 'unknown';
  } catch (e) { return 'unknown'; }
})()
"""
# OliveYoung 송장 — content-tracking-oliveyoung.js 이식.
# <em>택배사</em>텍스트노드 / <em>운송장번호</em>텍스트노드 패턴.
_OLIVEYOUNG_TRACKING_JS = r"""
(async () => {
  const href=location.href||'';
  if(href.indexOf('/member/getLoginForm')!==-1 || href.indexOf('/login')!==-1)
    return {success:false, needsLogin:true, error:'needs_login redirect'};
  const t=(document.body?.innerText||'').slice(0,8000);
  if(/(취소완료|취소처리완료|구매취소완료|주문이\s*취소|취소된\s*주문)/.test(t)) return {success:false,cancelled:true,error:'order_cancelled'};
  const tw=Date.now();
  while(Date.now()-tw<10000){ if(document.querySelector('.lineBox2, h3')) break; await new Promise(r=>setTimeout(r,300)); }
  await new Promise(r=>setTimeout(r,800));
  const byLabel=(label)=>{
    const ems=document.querySelectorAll('em');
    for(const em of ems){
      if((em.textContent||'').trim()===label){
        let next=em.nextSibling;
        while(next){
          if(next.nodeType===3){ const x=next.textContent.trim().replace(/^[:：\s]+/,''); if(x) return x; }
          else if(next.nodeType===1){ const x=(next.textContent||'').trim(); if(x) return x; }
          next=next.nextSibling;
        }
      }
    }
    return '';
  };
  const courierName=byLabel('택배사');
  const raw=byLabel('운송장번호')||byLabel('송장번호');
  const m=(raw||'').match(/\d{8,}/);
  const trackingNumber=m?m[0]:'';
  if(!trackingNumber) return {success:false, error:'no_tracking: 운송장번호 미표시 (미발송)', courierName};
  return {success:true, courierName, trackingNumber};
})()
"""


# ── KREAM 송장 + 로그인 ──
# KREAM 송장수집 신규 구현 — content-tracking-kream.js 부재 상태에서 데몬 전용 작성.
# 로그인폼 추정값 (실측 검증 필요). 송장 URL: /my/orders/{ord_no} 추정.
# KREAM 은 자체 검수 후 배송 — 송장 노출 형태/셀렉터 운영 검증 필수.
KREAM_LOGIN_URL = "https://kream.co.kr/login"
KREAM_HOME_URL = "https://kream.co.kr/my/orders"
KREAM_LOGOUT_URL = "https://kream.co.kr/logout"
# 추정값 — KREAM 일반 input name 패턴. 실측 검증 필요.
KREAM_LOGIN_SELECTORS = {
    "id": [
        'input[name="email"]',
        'input[type="email"]',
        "#email",
        'input[name="login_email"]',
    ],
    "pw": [
        'input[name="password"]',
        'input[type="password"]',
        "#password",
    ],
    "btn": [
        "button.btn.full.solid",
        'button[type="submit"]',
        ".login_btn",
        "button.btn_login",
    ],
}
KREAM_LOGIN_CHECK_JS = r"""
(() => {
  try {
    const h = location.href || '';
    if (h.indexOf('/login') !== -1) return 'logged_out';
    const t = (document.body?.innerText || '').slice(0, 1500).replace(/\s+/g, ' ');
    if (t.includes('로그아웃') || /My ?Page|마이/i.test(t)) return 'logged_in';
    if (t.includes('로그인') || t.includes('회원가입')) return 'logged_out';
    return 'unknown';
  } catch (e) { return 'unknown'; }
})()
"""
# KREAM 송장 — 마이페이지 주문 상세에서 택배사/송장 텍스트 추출 (휴리스틱).
# 실제 DOM 구조 운영 검증 필수 — 추정값.
_KREAM_TRACKING_JS = r"""
(async () => {
  const href=location.href||'';
  if(href.indexOf('/login')!==-1)
    return {success:false, needsLogin:true, error:'needs_login redirect'};
  const t=(document.body?.innerText||'').slice(0,8000);
  if(/(취소완료|취소처리완료|구매취소완료|주문이\s*취소|취소된\s*주문)/.test(t)) return {success:false,cancelled:true,error:'order_cancelled'};
  const tw=Date.now();
  while(Date.now()-tw<10000){ if(document.body && (document.body.innerText||'').length>500) break; await new Promise(r=>setTimeout(r,300)); }
  await new Promise(r=>setTimeout(r,1500));
  const txt=document.body?.innerText||'';
  const cm=txt.match(/택배사\s*[:：]?\s*([가-힣A-Za-z0-9()]+택배|[가-힣A-Za-z0-9()]+로지스|CJ대한통운|한진|롯데|우체국|로젠)/);
  const tm=txt.match(/(?:운송장|송장)(?:번호)?\s*[:：]?\s*(\d{8,})/);
  const courierName=cm?.[1]?.trim()||'';
  const trackingNumber=tm?.[1]?.trim()||'';
  if(!trackingNumber) return {success:false, error:'no_tracking: KREAM 송장 미표시 (미발송 또는 셀렉터 변경)'};
  return {success:true, courierName, trackingNumber};
})()
"""


# ── FashionPlus 송장 + 로그인 ──
# 로그인폼 실측(2026-05-25, /auth/login 정적 HTML, Vue 컴포넌트):
#   id=#login_id (v-model="form.id"), pw=input[type="password"] (v-model="form.password"),
#   btn=button.mm_btn[v-on:click="login"].
# Vue 리스너는 표준 DOM click 이벤트 받음 — daemon auto_login_site 의 click() 작동.
# 송장 URL: /mypage/order/detail/{ord_no} (build_tracking_url 참조).
FASHIONPLUS_LOGIN_URL = "https://www.fashionplus.co.kr/auth/login"
FASHIONPLUS_HOME_URL = "https://www.fashionplus.co.kr/mypage"
FASHIONPLUS_LOGOUT_URL = "https://www.fashionplus.co.kr/auth/logout"
FASHIONPLUS_LOGIN_SELECTORS = {
    "id": ["#login_id", 'input[v-model="form.id"]'],
    "pw": [
        'input[type="password"][v-model="form.password"]',
        'input[type="password"]',
    ],
    "btn": ['button[v-on:click="login"]', "button.mm_btn"],
}
FASHIONPLUS_LOGIN_CHECK_JS = r"""
(() => {
  try {
    const h = location.href || '';
    if (h.indexOf('/auth/login') !== -1) return 'logged_out';
    const t = (document.body?.innerText || '').slice(0, 1500).replace(/\s+/g, ' ');
    if (t.includes('로그아웃')) return 'logged_in';
    if (t.includes('로그인') || t.includes('회원가입')) return 'logged_out';
    return 'unknown';
  } catch (e) { return 'unknown'; }
})()
"""
# FashionPlus 송장 — content-tracking-fashionplus.js 이식.
# 주문상세 페이지 텍스트에서 "택배사 X 송장번호 NNN" 정규식 추출.
_FASHIONPLUS_TRACKING_JS = r"""
(async () => {
  const href=location.href||'';
  if(href.indexOf('/auth/login')!==-1)
    return {success:false, needsLogin:true, error:'needs_login redirect'};
  const t=(document.body?.innerText||'').slice(0,8000);
  if(/(취소완료|취소처리완료|구매취소완료|주문이\s*취소|취소된\s*주문)/.test(t)) return {success:false,cancelled:true,error:'order_cancelled'};
  // body 로드 + SPA 렌더 대기
  const tw=Date.now();
  while(Date.now()-tw<10000){ if(document.body && (document.body.innerText||'').length>500) break; await new Promise(r=>setTimeout(r,300)); }
  await new Promise(r=>setTimeout(r,1500));
  const txt=document.body?.innerText||'';
  const cm=txt.match(/택배사\s*[:：]?\s*([가-힣A-Za-z0-9()]+택배|[가-힣A-Za-z0-9()]+로지스|CJ대한통운|한진|롯데|우체국|로젠)/);
  const tm=txt.match(/송장(?:번호)?\s*[:：]?\s*(\d{8,})/);
  const courierName=cm?.[1]?.trim()||'';
  const trackingNumber=tm?.[1]?.trim()||'';
  if(!trackingNumber) return {success:false, error:'no_tracking: 패션플러스 송장 미표시 (미발송 가능)'};
  return {success:true, courierName, trackingNumber};
})()
"""


SITE_HANDLERS: dict[str, SiteHandler] = {
    "ABCmart": SiteHandler(
        site="ABCmart",
        extract_js=_ABCMART_EXTRACT_JS,
        requires_login=True,
        login_url=ABCMART_LOGIN_URL,
        home_url=ABCMART_HOME_URL,
        login_selectors=ABCMART_LOGIN_SELECTORS,
        login_check_js=ABCMART_LOGIN_CHECK_JS,
        pre_extract_marker_js=_ABCMART_MARKER_JS,
        # 실측(2026-05-24, 10상품): "최대 혜택가" 텍스트 최대 1.64s 등장.
        # 6s → 2.5s (2026-05-27 A+C): 혜택가 텍스트 없는 상품군이 6s 풀 타임아웃 소비해
        # 건당 8.5s 유발. marker JS 에 상품명 selector / readyState 폴백 추가해 floor 1.64s 안전 마진.
        pre_extract_marker_timeout_ms=2_500,
        pre_extract_wait_ms=200,
        tracking_js=_ABCMART_TRACKING_JS,
        logout_url=ABCMART_LOGOUT_URL,
    ),
    "GrandStage": SiteHandler(
        site="GrandStage",
        extract_js=_ABCMART_EXTRACT_JS,  # 동일 도메인 a-rt.com
        requires_login=True,
        login_url=ABCMART_LOGIN_URL,
        home_url=ABCMART_HOME_URL,
        login_selectors=ABCMART_LOGIN_SELECTORS,
        login_check_js=ABCMART_LOGIN_CHECK_JS,
        pre_extract_marker_js=_ABCMART_MARKER_JS,
        pre_extract_marker_timeout_ms=6_000,
        pre_extract_wait_ms=200,
        tracking_js=_ABCMART_TRACKING_JS,
        logout_url=ABCMART_LOGOUT_URL,
    ),
    "SSG": SiteHandler(
        site="SSG",
        extract_js=_SSG_EXTRACT_JS,
        requires_login=False,  # 가격수집(detail)은 무로그인 — 변경 금지
        dialog_policy="accept",
        pre_extract_marker_js=_SSG_MARKER_JS,
        # 실측(2026-05-24, 10상품): itemNm·uitemObjList 동시 생성, 최대 1.30s.
        # 마커(itemNm)가 곧 재고 준비 시점 → 정확성 안전. timeout 6→10s 상향
        # (2026-05-27 오토튠 SSG 실패율 높음 — 헤드리스 + 이미지 차단 환경에서
        # resultItemObj XHR 지연 케이스 보강).
        pre_extract_marker_timeout_ms=10_000,
        pre_extract_wait_ms=1_500,
        # 송장(tracking)은 마이페이지 접근 → 로그인 필수. login_url/selectors 추가.
        # requires_login=False 유지 → detail 흐름은 ensure_logged_in_for_site 가 스킵.
        login_url=SSG_TRACKING_LOGIN_URL,
        home_url=SSG_HOME_URL,
        login_selectors=SSG_LOGIN_SELECTORS,
        login_check_js=SSG_LOGIN_CHECK_JS,
        tracking_js=_SSG_TRACKING_JS,
        logout_url=SSG_LOGOUT_URL,
    ),
    "Nike": SiteHandler(
        site="Nike",
        extract_js="(() => ({success:false, error:'Nike detail 데몬 미지원'}))()",
        requires_login=True,
        login_url=NIKE_LOGIN_URL,
        home_url=NIKE_HOME_URL,
        login_selectors=NIKE_LOGIN_SELECTORS,
        login_check_js=NIKE_LOGIN_CHECK_JS,
        tracking_js=_NIKE_TRACKING_JS,
        logout_url=NIKE_LOGOUT_URL,
        detail_supported=False,
    ),
    "OliveYoung": SiteHandler(
        site="OliveYoung",
        extract_js="(() => ({success:false, error:'OliveYoung detail 데몬 미지원'}))()",
        requires_login=True,
        login_url=OLIVEYOUNG_LOGIN_URL,
        home_url=OLIVEYOUNG_HOME_URL,
        login_selectors=OLIVEYOUNG_LOGIN_SELECTORS,
        login_check_js=OLIVEYOUNG_LOGIN_CHECK_JS,
        tracking_js=_OLIVEYOUNG_TRACKING_JS,
        logout_url=OLIVEYOUNG_LOGOUT_URL,
        detail_supported=False,
    ),
    "KREAM": SiteHandler(
        site="KREAM",
        extract_js="(() => ({success:false, error:'KREAM detail 데몬 미지원'}))()",
        requires_login=True,
        login_url=KREAM_LOGIN_URL,
        home_url=KREAM_HOME_URL,
        login_selectors=KREAM_LOGIN_SELECTORS,
        login_check_js=KREAM_LOGIN_CHECK_JS,
        tracking_js=_KREAM_TRACKING_JS,
        logout_url=KREAM_LOGOUT_URL,
        detail_supported=False,
    ),
    "FashionPlus": SiteHandler(
        site="FashionPlus",
        extract_js="(() => ({success:false, error:'FashionPlus detail 데몬 미지원'}))()",
        requires_login=True,
        login_url=FASHIONPLUS_LOGIN_URL,
        home_url=FASHIONPLUS_HOME_URL,
        login_selectors=FASHIONPLUS_LOGIN_SELECTORS,
        login_check_js=FASHIONPLUS_LOGIN_CHECK_JS,
        tracking_js=_FASHIONPLUS_TRACKING_JS,
        logout_url=FASHIONPLUS_LOGOUT_URL,
        detail_supported=False,
    ),
    "GSShop": SiteHandler(
        site="GSShop",
        # GSShop 데몬 가격수집(detail) 미지원 — 송장 전용. extract_js 더미.
        extract_js="(() => ({success:false, error:'GSShop detail 데몬 미지원'}))()",
        requires_login=True,
        login_url=GSSHOP_LOGIN_URL,
        home_url=GSSHOP_HOME_URL,
        login_selectors=GSSHOP_LOGIN_SELECTORS,
        login_check_js=GSSHOP_LOGIN_CHECK_JS,
        tracking_js=_GSSHOP_TRACKING_JS,
        logout_url=GSSHOP_LOGOUT_URL,
        detail_supported=False,
    ),
    "MUSINSA": SiteHandler(
        site="MUSINSA",
        # MUSINSA 는 데몬 가격수집(detail) 미지원 — 송장 전용 핸들러. extract_js 더미.
        extract_js="(() => ({success:false, error:'MUSINSA detail 데몬 미지원'}))()",
        requires_login=True,
        login_url=MUSINSA_LOGIN_URL,
        home_url=MUSINSA_HOME_URL,
        login_selectors=MUSINSA_LOGIN_SELECTORS,
        login_check_js=MUSINSA_LOGIN_CHECK_JS,
        logout_url=MUSINSA_LOGOUT_URL,
        # 2단계: 주문상세 → 배송조회 클릭 → /delivery/trace 네비 → 스크랩
        tracking_two_hop=True,
        tracking_click_js=_MUSINSA_TRACKING_CLICK_JS,
        tracking_js=_MUSINSA_TRACKING_TRACE_JS,
        tracking_trace_url_glob="**/order-service/my/delivery/trace*",
        detail_supported=False,  # 송장 전용 — 가격수집 미지원
    ),
}
