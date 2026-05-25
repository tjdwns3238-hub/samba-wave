"""웨일 CDP — 무신사 주문 실제 취소 + 전체 호출 캡처.

순서:
1. 주문상세 탭 (없으면 새 탭)
2. fetch/XHR 후킹
3. '취소 요청' 클릭 → 모달
4. '단순 변심' 라디오 선택
5. '취소 요청하기' 버튼 클릭
6. 5초 대기 후 fetch_log 덤프 → POST cancel endpoint + body 확보
"""

import asyncio
import json
from urllib.request import Request, urlopen

import websockets


ORDER_URL = "https://www.musinsa.com/order/order-detail/202605260834580001"


def list_tabs():
    return [t for t in json.loads(urlopen("http://localhost:9223/json").read()) if t.get("type") == "page"]


def find_tab(url_substr: str):
    for t in list_tabs():
        if url_substr in t.get("url", ""):
            return t
    return None


def new_tab(url: str):
    req = Request(f"http://localhost:9223/json/new?{url}", method="PUT")
    return json.loads(urlopen(req).read())


class CDP:
    def __init__(self, ws):
        self.ws = ws
        self.id = 0
        self.responses: dict = {}
        self.events: list = []
        self._task = asyncio.create_task(self._pump())

    async def _pump(self):
        try:
            async for raw in self.ws:
                e = json.loads(raw)
                if "id" in e:
                    self.responses[e["id"]] = e
                else:
                    self.events.append(e)
        except Exception:
            pass

    async def call(self, method, params=None, timeout=15.0):
        self.id += 1
        mid = self.id
        await self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        loop = asyncio.get_event_loop()
        end = loop.time() + timeout
        while loop.time() < end:
            if mid in self.responses:
                return self.responses.pop(mid)
            await asyncio.sleep(0.05)
        raise TimeoutError(method)

    async def eval(self, js, timeout=15.0):
        r = await self.call("Runtime.evaluate", {"expression": js, "returnByValue": True, "awaitPromise": True}, timeout=timeout)
        return r.get("result", {}).get("result", {}).get("value")


HOOK_JS = r"""
(() => {
  if (window.__sambaHook) return 'already';
  window.__sambaLog = [];
  const origFetch = window.fetch.bind(window);
  window.fetch = async function(input, init) {
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    const method = (init && init.method) || (input && input.method) || 'GET';
    let body = '';
    let headers = {};
    try {
      if (init && init.body) {
        body = typeof init.body === 'string' ? init.body : JSON.stringify(init.body);
      }
      if (init && init.headers) {
        if (init.headers instanceof Headers) {
          init.headers.forEach((v,k) => headers[k] = v);
        } else {
          headers = init.headers;
        }
      }
    } catch(_) {}
    const entry = {url, method, body: body.slice(0, 3000), headers, ts: Date.now(), kind: 'fetch'};
    window.__sambaLog.push(entry);
    try {
      const res = await origFetch(input, init);
      try {
        const cl = res.clone();
        const txt = await cl.text();
        entry.status = res.status;
        entry.respBody = txt.slice(0, 2000);
      } catch(_) {}
      return res;
    } catch(e) {
      entry.error = String(e);
      throw e;
    }
  };
  const origOpen = XMLHttpRequest.prototype.open;
  const origSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function(m, u) { this.__m = m; this.__u = u; return origOpen.apply(this, arguments); };
  XMLHttpRequest.prototype.send = function(b) {
    const entry = {url: this.__u, method: this.__m, body: (typeof b === 'string' ? b : '').slice(0, 3000), ts: Date.now(), kind: 'xhr'};
    window.__sambaLog.push(entry);
    this.addEventListener('load', () => { try { entry.status = this.status; entry.respBody = (this.responseText || '').slice(0, 2000); } catch(_) {} });
    return origSend.apply(this, arguments);
  };
  window.__sambaHook = true;
  return 'hooked';
})()
"""


CLICK_CANCEL_REQ_JS = r"""
(() => {
  // 본문 영역의 '취소 요청' 또는 '주문 취소' 같은 단일 액션 버튼
  for (const el of document.querySelectorAll('button')) {
    const t = (el.innerText || '').trim();
    const r = el.getBoundingClientRect();
    if (r.width === 0 || el.disabled) continue;
    if (t.length > 12) continue;
    if (/취소.*요청|주문.*취소|^취소$/.test(t)) {
      el.scrollIntoView({block:'center'});
      el.click();
      return 'clicked:' + t + ' @' + Math.round(r.x) + ',' + Math.round(r.y);
    }
  }
  return 'not-found';
})()
"""


SELECT_REASON_JS = r"""
(() => {
  // 단순 변심 라디오 클릭
  const radios = document.querySelectorAll('input[type=radio]');
  for (const r of radios) {
    const lbl = (r.closest('label') || r.parentElement || {}).innerText || '';
    if (/단순.*변심/.test(lbl)) {
      r.click();
      return 'radio-clicked:' + lbl.trim().slice(0, 30);
    }
  }
  // 라디오가 없으면 label 자체 클릭
  for (const el of document.querySelectorAll('label, div, button')) {
    const t = (el.innerText || '').trim();
    if (t.startsWith('단순 변심') && t.length < 50) {
      el.click();
      return 'label-clicked:' + t.slice(0, 40);
    }
  }
  return 'reason-not-found';
})()
"""


CLICK_FINAL_JS = r"""
(() => {
  for (const el of document.querySelectorAll('button')) {
    const t = (el.innerText || '').trim();
    if (el.disabled) continue;
    // "취소 요청하기 (1개)" / "취소하기" / "주문 취소하기" 형태
    if (/취소.*요청하기|취소하기|취소\s*신청/.test(t) && t.length < 30) {
      el.scrollIntoView({block:'center'});
      el.click();
      return 'final-clicked:' + t;
    }
  }
  return 'final-not-found';
})()
"""


DUMP_LOG_JS = "JSON.stringify(window.__sambaLog || [])"


async def main():
    tab = find_tab("202605260834580001")
    if not tab:
        # 새 탭
        nt = new_tab(ORDER_URL)
        ws_url = nt["webSocketDebuggerUrl"]
        await asyncio.sleep(6)
    else:
        ws_url = tab["webSocketDebuggerUrl"]

    async with websockets.connect(ws_url, max_size=80_000_000) as ws:
        cdp = CDP(ws)
        await cdp.call("Page.enable")
        await cdp.call("Runtime.enable")
        await cdp.call("Network.enable")

        # hook
        r = await cdp.eval(HOOK_JS)
        print("HOOK:", r)

        # 1. 취소 요청 클릭
        r = await cdp.eval(CLICK_CANCEL_REQ_JS)
        print("BTN1:", r)
        await asyncio.sleep(3.0)

        # 2. 단순 변심 선택
        r = await cdp.eval(SELECT_REASON_JS)
        print("REASON:", r)
        await asyncio.sleep(1.0)

        # 3. 최종 취소 요청하기 클릭
        r = await cdp.eval(CLICK_FINAL_JS)
        print("FINAL:", r)
        await asyncio.sleep(6.0)

        # 4. 로그 덤프
        raw = await cdp.eval(DUMP_LOG_JS)
        log = json.loads(raw) if isinstance(raw, str) else (raw or [])

        # 필터 — 분석/광고 노이즈 제거
        noise_substr = ("google", "facebook", "doubleclick", "analytics", "kakao", "naver.com/wcs", "pinterest", "twitter", "hotjar", "criteo", "airbridge", "tiktok", "braze", "cloudflare-rum", "/cdn-cgi/", "/log/", "static.msscdn", "snippet.maze", "creativecdn", "datadoghq")
        cleaned = []
        for e in log:
            u = (e.get("url") or "").lower()
            if any(s in u for s in noise_substr):
                continue
            cleaned.append(e)
        print("\n=== CANCEL CANDIDATE CALLS ===")
        print(json.dumps(cleaned, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
