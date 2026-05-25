"""주문상세 페이지에서 취소 버튼 클릭→모달까지 진입.

- Network 캡처
- 취소 사유 옵션·최종 확정 버튼 onclick·form 분석
- 최종 확정 버튼은 절대 클릭 안 함
"""

import asyncio
import json
import sys
from urllib.request import urlopen

import websockets


SITE_URLS = {
    "MUSINSA": "https://www.musinsa.com/order/order-detail/{order_no}",
    "LOTTEON": "https://www.lotteon.com/p/order/orderList?orderNo={order_no}",
    "ABCmart": "https://abcmart.a-rt.com/myabc/order/orderDetail?ordNo={order_no}",
    "SSG": "https://www.ssg.com/myssg/orderDetail.ssg?orderInfoId={order_no}",
    "GSShop": "https://www.gsshop.com/shop/myShop/orderInquiry/orderDetail.gs?ordNo={order_no}",
}


def list_tabs() -> list[dict]:
    return [t for t in json.loads(urlopen("http://localhost:9223/json").read()) if t.get("type") == "page"]


def open_url(url: str) -> str:
    from urllib.request import Request
    req = Request(f"http://localhost:9223/json/new?{url}", method="PUT")
    return json.loads(urlopen(req).read())["webSocketDebuggerUrl"]


def find_tab_by_host(host: str) -> dict | None:
    for t in list_tabs():
        if host in t.get("url", ""):
            return t
    return None


async def cdp_session(ws_url: str):
    return await websockets.connect(ws_url, max_size=80_000_000)


class CDP:
    def __init__(self, ws) -> None:
        self.ws = ws
        self.id = 0
        self.responses: dict[int, dict] = {}
        self.events: list[dict] = []
        self._task = asyncio.create_task(self._pump())

    async def _pump(self) -> None:
        try:
            async for raw in self.ws:
                evt = json.loads(raw)
                if "id" in evt:
                    self.responses[evt["id"]] = evt
                else:
                    self.events.append(evt)
        except Exception:
            pass

    async def call(self, method: str, params: dict | None = None, timeout: float = 8.0) -> dict:
        self.id += 1
        mid = self.id
        await self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        loop = asyncio.get_event_loop()
        end = loop.time() + timeout
        while loop.time() < end:
            if mid in self.responses:
                return self.responses.pop(mid)
            await asyncio.sleep(0.05)
        raise TimeoutError(f"CDP timeout {method}")

    def drain_events(self, method_filter: str | None = None) -> list[dict]:
        out = []
        keep = []
        for e in self.events:
            if method_filter is None or e.get("method", "").startswith(method_filter):
                out.append(e)
            else:
                keep.append(e)
        self.events = keep
        return out


CANCEL_BUTTON_FINDER_JS = r"""
(() => {
  const out = [];
  const all = document.querySelectorAll('a, button, input[type=button], input[type=submit], [role=button]');
  for (const el of all) {
    const t = (el.innerText || el.value || '').trim();
    if (!t) continue;
    if (/^주문\s*취소$|^취소\s*요청$|^주문취소$|^취소$/.test(t) ||
        (/취소/.test(t) && t.length < 12)) {
      const rect = el.getBoundingClientRect();
      out.push({
        text: t,
        tag: el.tagName,
        id: el.id,
        cls: el.className && typeof el.className === 'string' ? el.className : '',
        href: el.getAttribute('href') || '',
        onclick: (el.getAttribute('onclick') || '').slice(0, 300),
        rect: {x: rect.x, y: rect.y, w: rect.width, h: rect.height},
        visible: rect.width > 0 && rect.height > 0 && el.offsetParent !== null,
      });
    }
  }
  return JSON.stringify(out);
})()
"""


MODAL_DUMP_JS = r"""
(() => {
  // 모달/팝업 후보 — z-index 높은 fixed/absolute 요소
  const candidates = [];
  for (const el of document.querySelectorAll('div, section, dialog')) {
    const cs = getComputedStyle(el);
    if ((cs.position === 'fixed' || cs.position === 'absolute') &&
        parseInt(cs.zIndex || '0', 10) >= 100 &&
        el.offsetParent !== null && el.getBoundingClientRect().height > 100) {
      candidates.push({
        id: el.id, cls: typeof el.className === 'string' ? el.className.slice(0, 200) : '',
        innerText: (el.innerText || '').slice(0, 1500),
        html: el.outerHTML.slice(0, 4000),
      });
    }
  }
  return JSON.stringify(candidates);
})()
"""


async def analyze(ws_url: str, click_cancel: bool = True) -> dict:
    ws = await cdp_session(ws_url)
    cdp = CDP(ws)
    try:
        await cdp.call("Network.enable")
        await cdp.call("Page.enable")
        await cdp.call("Runtime.enable")
        await asyncio.sleep(0.3)

        # baseline cancel buttons
        res = await cdp.call("Runtime.evaluate", {"expression": CANCEL_BUTTON_FINDER_JS, "returnByValue": True})
        buttons_raw = res.get("result", {}).get("result", {}).get("value", "[]")
        buttons = json.loads(buttons_raw)

        out: dict = {
            "url": "",
            "title": "",
            "cancel_buttons": buttons,
            "post_click_xhr": [],
            "modal": [],
            "after_click_buttons": [],
        }

        # current url/title
        res = await cdp.call("Runtime.evaluate", {
            "expression": "JSON.stringify({u: location.href, t: document.title})",
            "returnByValue": True,
        })
        info = json.loads(res["result"]["result"]["value"])
        out["url"] = info["u"]
        out["title"] = info["t"]

        if not click_cancel or not buttons:
            return out

        # 첫 번째 visible 취소 버튼 클릭 — 클릭 안전(모달만 열림 예상)
        target = next((b for b in buttons if b["visible"]), None)
        if not target:
            return out

        # 클릭 시점 이전 network 이벤트 비우기
        cdp.drain_events("Network.")

        click_js = f"""
        (() => {{
          const all = document.querySelectorAll('a, button, input[type=button], input[type=submit], [role=button]');
          for (const el of all) {{
            const t = (el.innerText || el.value || '').trim();
            const r = el.getBoundingClientRect();
            if (t === {json.dumps(target['text'])} && Math.abs(r.x - {target['rect']['x']}) < 2 && Math.abs(r.y - {target['rect']['y']}) < 2) {{
              el.click();
              return 'clicked';
            }}
          }}
          return 'not-found';
        }})()
        """
        res = await cdp.call("Runtime.evaluate", {"expression": click_js, "returnByValue": True})
        click_status = res.get("result", {}).get("result", {}).get("value", "")
        out["click_status"] = click_status

        await asyncio.sleep(3.5)

        # XHR 수집
        net = cdp.drain_events("Network.")
        request_map: dict[str, dict] = {}
        for e in net:
            m = e.get("method", "")
            p = e.get("params", {})
            if m == "Network.requestWillBeSent":
                req = p.get("request", {})
                request_map[p["requestId"]] = {
                    "url": req.get("url", "")[:300],
                    "method": req.get("method", ""),
                    "postData": (req.get("postData") or "")[:500],
                }
            elif m == "Network.responseReceived":
                rid = p.get("requestId")
                if rid in request_map:
                    request_map[rid]["status"] = p.get("response", {}).get("status")
                    request_map[rid]["mimeType"] = p.get("response", {}).get("mimeType")
        out["post_click_xhr"] = [v for v in request_map.values() if "google" not in v["url"] and "doubleclick" not in v["url"] and ".css" not in v["url"] and ".woff" not in v["url"]]

        # modal dump
        res = await cdp.call("Runtime.evaluate", {"expression": MODAL_DUMP_JS, "returnByValue": True})
        modal_raw = res.get("result", {}).get("result", {}).get("value", "[]")
        out["modal"] = json.loads(modal_raw)

        # 모달 내 버튼 / form 추출
        res = await cdp.call("Runtime.evaluate", {"expression": CANCEL_BUTTON_FINDER_JS, "returnByValue": True})
        out["after_click_buttons"] = json.loads(res.get("result", {}).get("result", {}).get("value", "[]"))

        # url 변경 여부
        res = await cdp.call("Runtime.evaluate", {
            "expression": "location.href",
            "returnByValue": True,
        })
        out["url_after"] = res["result"]["result"]["value"]

        return out
    finally:
        await ws.close()


async def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    site = args[0] if args else "MUSINSA"
    order_no = args[1] if len(args) > 1 else ""
    click = "--no-click" not in sys.argv

    host_map = {
        "MUSINSA": "musinsa.com", "LOTTEON": "lotteon.com",
        "ABCmart": "a-rt.com", "SSG": "ssg.com", "GSShop": "gsshop.com",
    }

    if order_no:
        url = SITE_URLS[site].format(order_no=order_no)
        ws_url = open_url(url)
        await asyncio.sleep(5)
    else:
        t = find_tab_by_host(host_map[site])
        if not t:
            print(json.dumps({"error": f"no tab for {site}"}, ensure_ascii=False))
            return
        ws_url = t["webSocketDebuggerUrl"]

    res = await analyze(ws_url, click_cancel=click)
    print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
