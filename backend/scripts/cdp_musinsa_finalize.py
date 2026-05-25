"""alert 확인 → 사유 정확 선택 → 최종 cancel POST 캡처."""

import asyncio
import json
from urllib.request import urlopen
import websockets


def find_tab():
    for t in json.loads(urlopen("http://localhost:9223/json").read()):
        if t.get("type") != "page":
            continue
        u = t.get("url", "")
        if "musinsa.com/order/claim/order-cancel" in u and "202605260834580001" in u:
            return t
    return None


HOOK_JS = r"""
(() => {
  if (window.__sambaHook3) return 'already3';
  window.__sambaLog3 = [];
  const origFetch = window.fetch.bind(window);
  window.fetch = async function(input, init) {
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    const method = (init && init.method) || 'GET';
    let body = '';
    let headers = {};
    try {
      if (init && init.body) body = typeof init.body === 'string' ? init.body : JSON.stringify(init.body);
      if (init && init.headers) {
        if (init.headers instanceof Headers) init.headers.forEach((v,k) => headers[k] = v);
        else headers = init.headers;
      }
    } catch(_) {}
    const entry = {url, method, body: body.slice(0, 4000), headers, ts: Date.now(), kind: 'fetch'};
    window.__sambaLog3.push(entry);
    try {
      const res = await origFetch(input, init);
      try { const cl = res.clone(); const t = await cl.text(); entry.status = res.status; entry.respBody = t.slice(0, 3000); } catch(_) {}
      return res;
    } catch(e) { entry.error = String(e); throw e; }
  };
  const origOpen = XMLHttpRequest.prototype.open;
  const origSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function(m, u) { this.__m = m; this.__u = u; return origOpen.apply(this, arguments); };
  XMLHttpRequest.prototype.send = function(b) {
    const entry = {url: this.__u, method: this.__m, body: (typeof b === 'string' ? b : '').slice(0, 4000), ts: Date.now(), kind: 'xhr'};
    window.__sambaLog3.push(entry);
    this.addEventListener('load', () => { try { entry.status = this.status; entry.respBody = (this.responseText || '').slice(0, 3000); } catch(_) {} });
    return origSend.apply(this, arguments);
  };
  window.__sambaHook3 = true;
  return 'hooked';
})()
"""

DISMISS_ALERT_JS = r"""
(() => {
  // 모달의 '확인' 버튼 클릭 (alert dismiss)
  for (const el of document.querySelectorAll('button')) {
    const t = (el.innerText || '').trim();
    if (t === '확인') {
      const r = el.getBoundingClientRect();
      if (r.width === 0 || el.disabled) continue;
      el.click();
      return 'alert-dismissed @' + Math.round(r.x) + ',' + Math.round(r.y);
    }
  }
  return 'no-alert';
})()
"""

# custom radio selection — '단순 변심' 텍스트 근처의 click-handlerable 부모 찾기
SELECT_REASON_V2_JS = r"""
(() => {
  // 0) Radix UI radio: button#claimReasonCode-1 (단순변심)
  const radix = document.querySelector('#claimReasonCode-1');
  if (radix) {
    radix.scrollIntoView({block:'center'});
    radix.click();
    return 'radix:' + radix.getAttribute('aria-checked');
  }
  // 1) input[type=radio] 다시 시도
  const radios = document.querySelectorAll('input[type=radio]');
  for (const r of radios) {
    const labelText = ((r.closest('label') || r.parentElement || {}).innerText || '');
    if (/단순.*변심/.test(labelText)) {
      r.click();
      // React controlled component 케이스 — change event도 발생
      const ev = new Event('change', {bubbles:true});
      r.dispatchEvent(ev);
      return 'real-radio:' + labelText.slice(0,40);
    }
  }
  // 2) 텍스트 가까운 click handler element 찾기 (li, label, button, div with onclick or role)
  const all = document.querySelectorAll('label, li, div, span, button, [role=radio], [role=button]');
  let target = null;
  for (const el of all) {
    const t = (el.textContent || '').trim();
    if (!/^단순.*변심/.test(t)) continue;
    if (t.length > 50) continue;
    // 가장 가까운 클릭가능 ancestor
    let n = el;
    for (let i = 0; i < 5 && n; i++) {
      const propsKey = Object.keys(n).find(k => k.startsWith('__reactProps'));
      if (propsKey && n[propsKey] && (n[propsKey].onClick || n[propsKey].onChange)) {
        target = n;
        break;
      }
      n = n.parentElement;
    }
    if (target) break;
  }
  if (target) {
    target.scrollIntoView({block:'center'});
    target.click();
    const r = target.getBoundingClientRect();
    return 'react-click:' + target.tagName + ' @' + Math.round(r.x) + ',' + Math.round(r.y);
  }
  // 3) 모든 '단순 변심' 텍스트 가진 ancestor 클릭 (브루트)
  for (const el of all) {
    const t = (el.textContent || '').trim();
    if (/^단순.*변심/.test(t) && t.length < 50) {
      const r = el.getBoundingClientRect();
      if (r.width === 0 || r.height === 0) continue;
      el.click();
      return 'plain-click:' + el.tagName + ' @' + Math.round(r.x) + ',' + Math.round(r.y);
    }
  }
  return 'all-failed';
})()
"""

CHECK_REASON_SELECTED_JS = r"""
(() => {
  // '단순 변심' 라인 주변의 svg/icon이 selected 표시인지 확인
  const out = [];
  for (const el of document.querySelectorAll('label, li, div')) {
    const t = (el.textContent || '').trim();
    if (/^단순.*변심/.test(t) && t.length < 50) {
      const html = el.outerHTML.slice(0, 800);
      out.push({tag: el.tagName, html});
    }
  }
  return JSON.stringify(out.slice(0,3));
})()
"""

CLICK_FINAL_JS = r"""
(() => {
  for (const el of document.querySelectorAll('button')) {
    const t = (el.innerText || '').trim();
    if (el.disabled) continue;
    if (/^취소 요청하기/.test(t) && t.length < 30) {
      const r = el.getBoundingClientRect();
      el.scrollIntoView({block:'center'});
      el.click();
      return 'final-clicked:' + t + ' @' + Math.round(r.x) + ',' + Math.round(r.y);
    }
  }
  return 'final-not-found';
})()
"""

DUMP_ALL = "JSON.stringify({log1: window.__sambaLog || [], log2: window.__sambaLog2 || [], log3: window.__sambaLog3 || [], url: location.href})"


async def main():
    tab = find_tab()
    if not tab:
        print(json.dumps({"error": "no claim tab for order 202605260834580001"}))
        return
    print("TAB:", tab["url"])
    async with websockets.connect(tab["webSocketDebuggerUrl"], max_size=80_000_000) as ws:
        mid = [0]
        async def call(method, params=None):
            mid[0] += 1
            await ws.send(json.dumps({"id": mid[0], "method": method, "params": params or {}}))
            while True:
                e = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                if e.get("id") == mid[0]:
                    return e
        async def evalJS(js):
            r = await call("Runtime.evaluate", {"expression": js, "returnByValue": True, "awaitPromise": True})
            return r.get("result", {}).get("result", {}).get("value")

        await call("Runtime.enable")
        await call("Page.enable")
        await call("Network.enable")

        print("HOOK:", await evalJS(HOOK_JS))

        # 1. alert dismiss
        print("DISMISS:", await evalJS(DISMISS_ALERT_JS))
        await asyncio.sleep(1.0)

        # 2. 사유 raw HTML 확인
        chk = await evalJS(CHECK_REASON_SELECTED_JS)
        print("REASON_HTML:", chk)

        # 3. 사유 선택 v2
        print("SELECT:", await evalJS(SELECT_REASON_V2_JS))
        await asyncio.sleep(1.5)

        # 4. 다시 alert 떠 있을 수 있음 — 한번 더 dismiss
        # 사실 alert 없으면 no-alert 반환됨
        print("DISMISS2:", await evalJS(DISMISS_ALERT_JS))
        await asyncio.sleep(0.5)

        # 5. final 클릭
        print("FINAL:", await evalJS(CLICK_FINAL_JS))
        await asyncio.sleep(8.0)

        # 6. 또 alert(확인 모달) 떠 있을 수 있음
        print("DISMISS3:", await evalJS(DISMISS_ALERT_JS))
        await asyncio.sleep(5.0)

        raw = await evalJS(DUMP_ALL)
        data = json.loads(raw) if isinstance(raw, str) else raw
        noise = ("google", "facebook", "doubleclick", "analytics", "kakao", "naver.com/wcs", "pinterest", "twitter", "hotjar", "criteo", "airbridge", "tiktok", "braze", "cloudflare", "/cdn-cgi/", "/log/", "static.msscdn", "snippet.maze", "creativecdn", "datadoghq", "capi.madup", "data.musinsa.com", "rum")
        combined = (data.get("log1") or []) + (data.get("log2") or []) + (data.get("log3") or [])
        # dedup by url+ts
        seen = set()
        dedup = []
        for e in combined:
            k = (e.get("url"), e.get("ts"))
            if k in seen: continue
            seen.add(k)
            dedup.append(e)
        cleaned = [e for e in dedup if not any(s in (e.get("url") or "").lower() for s in noise)]
        # cancel/POST만 강조
        posts = [e for e in cleaned if e.get("method") in ("POST", "PUT", "DELETE")]
        print("\n=== ALL CALLS ===")
        print(json.dumps(cleaned, ensure_ascii=False, indent=2))
        print("\n=== POST/PUT/DELETE ONLY ===")
        print(json.dumps(posts, ensure_ascii=False, indent=2))


asyncio.run(main())
