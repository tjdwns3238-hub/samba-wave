"""claim 페이지에 진입한 후 hook 재설치 + dump.

사용자가 이미 실제 취소를 진행 중 — POST cancel API가 발생했을 것.
이 스크립트는 (1) hook 재설치, (2) 페이지 모든 fetch/XHR 미래 캡처, (3) 현재 페이지의 사유/버튼 상태 dump.
"""

import asyncio
import json
from urllib.request import urlopen
import websockets


def find_claim_tab():
    for t in json.loads(urlopen("http://localhost:9223/json").read()):
        if t.get("type") != "page":
            continue
        u = t.get("url", "")
        if "musinsa.com/order/claim/order-cancel" in u:
            return t
        if "musinsa.com/order/result" in u:
            return t
    return None


def find_any_musinsa_order_tab():
    for t in json.loads(urlopen("http://localhost:9223/json").read()):
        if t.get("type") != "page":
            continue
        u = t.get("url", "")
        if "musinsa.com" in u and ("order/claim" in u or "order/result" in u or "order-detail" in u):
            return t
    return None


HOOK_JS = r"""
(() => {
  if (window.__sambaHook2) return 'already2';
  window.__sambaLog2 = [];
  const origFetch = window.fetch.bind(window);
  window.fetch = async function(input, init) {
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    const method = (init && init.method) || (input && input.method) || 'GET';
    let body = '';
    let headers = {};
    try {
      if (init && init.body) body = typeof init.body === 'string' ? init.body : JSON.stringify(init.body);
      if (init && init.headers) {
        if (init.headers instanceof Headers) init.headers.forEach((v,k) => headers[k] = v);
        else headers = init.headers;
      }
    } catch(_) {}
    const entry = {url, method, body: body.slice(0, 3000), headers, ts: Date.now(), kind: 'fetch'};
    window.__sambaLog2.push(entry);
    try {
      const res = await origFetch(input, init);
      try { const cl = res.clone(); const t = await cl.text(); entry.status = res.status; entry.respBody = t.slice(0, 2500); } catch(_) {}
      return res;
    } catch(e) { entry.error = String(e); throw e; }
  };
  const origOpen = XMLHttpRequest.prototype.open;
  const origSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function(m, u) { this.__m = m; this.__u = u; return origOpen.apply(this, arguments); };
  XMLHttpRequest.prototype.send = function(b) {
    const entry = {url: this.__u, method: this.__m, body: (typeof b === 'string' ? b : '').slice(0, 3000), ts: Date.now(), kind: 'xhr'};
    window.__sambaLog2.push(entry);
    this.addEventListener('load', () => { try { entry.status = this.status; entry.respBody = (this.responseText || '').slice(0, 2500); } catch(_) {} });
    return origSend.apply(this, arguments);
  };
  window.__sambaHook2 = true;
  return 'hooked';
})()
"""


# 첫 hook (__sambaLog) 도 합쳐서 가져오기
DUMP_BOTH = "JSON.stringify({log1: window.__sambaLog || [], log2: window.__sambaLog2 || [], url: location.href, title: document.title})"


PAGE_STATE_JS = r"""
(() => {
  const buttons = [];
  for (const el of document.querySelectorAll('button, [role=button]')) {
    const t = (el.innerText || '').trim();
    const r = el.getBoundingClientRect();
    if (r.width === 0) continue;
    buttons.push({text: t.slice(0,80), tag: el.tagName, disabled: el.disabled || false, x: Math.round(r.x), y: Math.round(r.y)});
  }
  const radios = Array.from(document.querySelectorAll('input[type=radio]')).map(r => ({
    name: r.name, value: r.value, checked: r.checked,
    label: ((r.closest('label') || r.parentElement || {}).innerText || '').slice(0,80),
  }));
  return JSON.stringify({buttons, radios});
})()
"""


CLICK_FINAL_JS = r"""
(() => {
  for (const el of document.querySelectorAll('button')) {
    const t = (el.innerText || '').trim();
    if (el.disabled) continue;
    if (/취소.*요청하기|취소하기|취소\s*신청/.test(t) && t.length < 40) {
      el.scrollIntoView({block:'center'});
      el.click();
      return 'clicked:' + t;
    }
  }
  return 'final-not-found';
})()
"""


SELECT_REASON_JS = r"""
(() => {
  // 단순변심 라디오/라벨 클릭
  for (const r of document.querySelectorAll('input[type=radio]')) {
    const lbl = ((r.closest('label') || r.parentElement || {}).innerText || '');
    if (/단순.*변심/.test(lbl)) { r.click(); return 'radio:' + lbl.slice(0,40); }
  }
  for (const el of document.querySelectorAll('label, div, span')) {
    const t = (el.innerText || '').trim();
    if (t.startsWith('단순 변심') && t.length < 50) { el.click(); return 'label:' + t.slice(0,40); }
  }
  return 'no-reason';
})()
"""


async def main():
    tab = find_claim_tab() or find_any_musinsa_order_tab()
    if not tab:
        print(json.dumps({"error": "no musinsa tab"}))
        return
    print("TAB:", tab.get("url"))
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

        print("HOOK:", await evalJS(HOOK_JS))

        # 페이지 상태 dump
        state = await evalJS(PAGE_STATE_JS)
        print("STATE:")
        print(json.dumps(json.loads(state) if isinstance(state, str) else state, ensure_ascii=False, indent=2))

        # 사유 선택 시도 + 최종 클릭
        print("REASON:", await evalJS(SELECT_REASON_JS))
        await asyncio.sleep(1.0)
        print("FINAL:", await evalJS(CLICK_FINAL_JS))
        await asyncio.sleep(7.0)

        raw = await evalJS(DUMP_BOTH)
        data = json.loads(raw) if isinstance(raw, str) else raw
        noise = ("google", "facebook", "doubleclick", "analytics", "kakao", "naver.com/wcs", "pinterest", "twitter", "hotjar", "criteo", "airbridge", "tiktok", "braze", "cloudflare", "/cdn-cgi/", "/log/", "static.msscdn", "snippet.maze", "creativecdn", "datadoghq", "capi.madup")
        combined = (data.get("log1") or []) + (data.get("log2") or [])
        cleaned = [e for e in combined if not any(s in (e.get("url") or "").lower() for s in noise)]
        print("\n=== ALL CANCEL CALLS ===")
        print(json.dumps(cleaned, ensure_ascii=False, indent=2))


asyncio.run(main())
