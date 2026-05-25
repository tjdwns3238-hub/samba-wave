"""무신사 취소 모달 더 깊게 분석.

1. 페이지 안의 '취소 요청하기' 버튼 react onClick 직접 추출
2. order/[...slug] chunk 다운로드 → cancel API URL grep
3. window.fetch 후킹 — 클릭하지 않고도 wrapper 등록만 (수동 클릭 시 capture)
"""

import asyncio
import json
import re
from urllib.request import urlopen, Request

import websockets


def find_tab():
    for t in json.loads(urlopen("http://localhost:9223/json").read()):
        if t.get("type") == "page" and "musinsa.com" in t.get("url", "") and "order-detail" in t.get("url", ""):
            return t
    return None


async def eval_js(ws, js: str, timeout: float = 10.0):
    mid = int(asyncio.get_event_loop().time() * 1000) % 99999
    await ws.send(json.dumps({
        "id": mid, "method": "Runtime.evaluate",
        "params": {"expression": js, "returnByValue": True, "awaitPromise": True},
    }))
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        evt = json.loads(raw)
        if evt.get("id") == mid:
            return evt
    raise TimeoutError("eval timeout")


# 모든 button + a + clickable 요소에서 cancel 텍스트 찾고 React props 덤프
DEEP_BUTTON_JS = r"""
(() => {
  const out = [];
  const all = document.querySelectorAll('button, a, [role=button], [onclick], input[type=button], input[type=submit]');
  for (const el of all) {
    const t = (el.innerText || el.value || el.textContent || '').trim();
    if (!t) continue;
    if (!/취소|cancel/i.test(t)) continue;
    if (t.length > 60) continue;
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    const propsKey = Object.keys(el).find(k => k.startsWith('__reactProps'));
    let onClickSrc = '';
    let propsKeys: string[] = [];
    if (propsKey && el[propsKey]) {
      propsKeys = Object.keys(el[propsKey]);
      const oc = el[propsKey].onClick;
      if (typeof oc === 'function') onClickSrc = oc.toString();
    }
    out.push({
      text: t, tag: el.tagName,
      cls: (typeof el.className === 'string' ? el.className : '').slice(0, 200),
      rect: {x: r.x, y: r.y, w: r.width, h: r.height},
      disabled: el.disabled || false,
      reactPropsKeys: propsKeys,
      onClickSrc: onClickSrc.slice(0, 4000),
    });
  }
  return out;
})()
""".replace(": string[]", "")


# fetch 후킹 — 캡처만 (실제 호출 시 기록)
FETCH_HOOK_JS = r"""
(() => {
  if (window.__sambaFetchHooked) return 'already-hooked';
  window.__sambaFetchLog = [];
  const orig = window.fetch.bind(window);
  window.fetch = async function(input, init) {
    try {
      const url = typeof input === 'string' ? input : input.url;
      const method = (init && init.method) || (input && input.method) || 'GET';
      let body = '';
      if (init && init.body) {
        try { body = typeof init.body === 'string' ? init.body : JSON.stringify(init.body); }
        catch(_) { body = '[unserializable]'; }
      }
      window.__sambaFetchLog.push({url, method, body: body.slice(0, 2000), ts: Date.now()});
    } catch(_) {}
    return orig(input, init);
  };
  // XHR 후킹
  const origOpen = XMLHttpRequest.prototype.open;
  const origSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function(method, url) {
    this.__sambaUrl = url;
    this.__sambaMethod = method;
    return origOpen.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function(body) {
    try {
      window.__sambaFetchLog.push({
        url: this.__sambaUrl, method: this.__sambaMethod || 'XHR',
        body: (typeof body === 'string' ? body : '').slice(0, 2000),
        ts: Date.now(),
      });
    } catch(_) {}
    return origSend.apply(this, arguments);
  };
  window.__sambaFetchHooked = true;
  return 'hooked';
})()
"""


READ_FETCH_LOG = r"""
(() => JSON.stringify(window.__sambaFetchLog || []))()
"""


# 페이지 안의 react fiber root 통해서 g 함수 (IS_SHIPPING_PROCESSING) 찾기
# 또는 페이지의 inline script + chunk 로딩 후 mutation hook 찾기
ALL_VISIBLE_BUTTONS_AND_RADIOS_JS = r"""
(() => {
  const buttons = [];
  for (const el of document.querySelectorAll('button')) {
    const t = (el.innerText || '').trim();
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    buttons.push({
      text: t.slice(0, 60), tag: 'BUTTON',
      cls: (typeof el.className === 'string' ? el.className : '').slice(0, 150),
      rect: {x: r.x, y: r.y, w: r.width, h: r.height},
      disabled: el.disabled || false,
    });
  }
  const radios = [];
  for (const el of document.querySelectorAll('input[type=radio], input[type=checkbox]')) {
    radios.push({
      type: el.type, name: el.name || '', value: el.value || '',
      id: el.id, checked: el.checked,
      label: (el.closest('label') || el.parentElement || {}).innerText?.slice(0, 80) || '',
    });
  }
  // 사유 보일 가능성 — 텍스트 검색
  const reasonTexts = [];
  for (const el of document.querySelectorAll('label, div, p, span')) {
    const t = (el.innerText || el.textContent || '').trim();
    if (/단순.*변심|주문.*실수|결제\s*수단|기타|상품.*불량/.test(t) && t.length < 100) {
      const r = el.getBoundingClientRect();
      if (r.width > 0 && r.height > 0) {
        reasonTexts.push({text: t, tag: el.tagName, x: r.x, y: r.y});
      }
    }
  }
  return {buttons, radios, reasonTexts};
})()
"""


async def main() -> None:
    tab = find_tab()
    if not tab:
        print(json.dumps({"error": "no musinsa order tab"}))
        return

    out = {"tab_url": tab["url"]}
    async with websockets.connect(tab["webSocketDebuggerUrl"], max_size=80_000_000) as ws:
        # hook fetch first
        r = await eval_js(ws, FETCH_HOOK_JS)
        out["hook_status"] = r.get("result", {}).get("result", {}).get("value")

        # all visible buttons + radios + reason texts
        r = await eval_js(ws, ALL_VISIBLE_BUTTONS_AND_RADIOS_JS)
        out["page_state"] = r.get("result", {}).get("result", {}).get("value", {})

        # deep cancel buttons
        r = await eval_js(ws, DEEP_BUTTON_JS)
        out["deep_cancel_buttons"] = r.get("result", {}).get("result", {}).get("value", [])

        # already-captured fetch log
        r = await eval_js(ws, READ_FETCH_LOG)
        val = r.get("result", {}).get("result", {}).get("value", "[]")
        out["fetch_log"] = json.loads(val) if isinstance(val, str) else val

    # order/[...slug] chunk 직접 다운로드해서 cancel API grep
    slug_chunk = "https://static.msscdn.net/static/nextorder/_next/static/chunks/pages/order/%5B...slug%5D-f97eeddd2374e3a3.js"
    try:
        req = Request(slug_chunk, headers={"User-Agent": "Mozilla/5.0"})
        data = urlopen(req, timeout=15).read().decode("utf-8", errors="replace")
        out["slug_chunk_size"] = len(data)
        # cancel URL pattern
        patterns = [
            r'["\']([^"\']*api\d?[^"\']*cancel[^"\']*)["\']',
            r'["\']([^"\']*claim[^"\']*)["\']',
            r'["\']([^"\']*order-items[^"\']*)["\']',
            r'["\']/(?:order|api\d?)/[^"\']*claim[^"\']*["\']',
            r'IS_SHIPPING_PROCESSING[^}]*["\']([^"\']+)["\']',
            r'"reason[A-Z][a-zA-Z]*"\s*:',
        ]
        slug_matches = {}
        for p in patterns:
            ms = re.findall(p, data, re.IGNORECASE)
            if ms:
                # unique + first 20
                slug_matches[p] = list(dict.fromkeys(ms))[:20]
        out["slug_chunk_matches"] = slug_matches
        # cancel reason code / 단순 변심 mapping
        m = re.search(r'단순.{0,5}변심[\s\S]{0,800}', data)
        if m:
            out["reason_block"] = m.group(0)[:1500]
        # 'order-items/' 주변 컨텍스트
        for m in re.finditer(r'order-items[^"\']{0,80}', data):
            out.setdefault("order_items_contexts", []).append(m.group(0))
            if len(out["order_items_contexts"]) >= 20:
                break
    except Exception as e:
        out["slug_chunk_err"] = str(e)

    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
