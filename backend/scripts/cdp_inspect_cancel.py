"""웨일 CDP(9223) 통해 주문상세 페이지 DOM·네트워크 캡처.

사용: python cdp_inspect_cancel.py <site> [<sourcing_order_no>]
analyze-only — 실제 취소 버튼 클릭 안 함.
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


async def find_tab(target_host: str | None = None) -> dict | None:
    data = json.loads(urlopen("http://localhost:9223/json").read())
    pages = [t for t in data if t.get("type") == "page"]
    if target_host:
        for t in pages:
            if target_host in t.get("url", ""):
                return t
    return pages[0] if pages else None


async def open_or_navigate(site: str, order_no: str) -> str:
    """기존 탭 재사용 또는 새 탭 — about:blank 1탭 만들고 navigate."""
    url = SITE_URLS[site].format(order_no=order_no)
    # 새 탭 (백그라운드 — focused=false)
    data = json.loads(urlopen(f"http://localhost:9223/json/new?{url}").read())
    return data["webSocketDebuggerUrl"]


async def collect_dom_and_xhr(ws_url: str, dwell_sec: float = 6.0) -> dict:
    out: dict = {
        "xhr": [],
        "console": [],
        "title": "",
        "url": "",
        "cancel_candidates": [],
    }
    async with websockets.connect(ws_url, max_size=50_000_000) as ws:
        msg_id = 0

        async def send(method: str, params: dict | None = None) -> int:
            nonlocal msg_id
            msg_id += 1
            await ws.send(
                json.dumps({"id": msg_id, "method": method, "params": params or {}})
            )
            return msg_id

        await send("Network.enable")
        await send("Page.enable")
        await send("Runtime.enable")

        deadline = asyncio.get_event_loop().time() + dwell_sec
        request_map: dict[str, dict] = {}
        while asyncio.get_event_loop().time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            evt = json.loads(raw)
            method = evt.get("method", "")
            if method == "Network.requestWillBeSent":
                p = evt["params"]
                req = p["request"]
                if (
                    req.get("method") in ("POST", "PUT", "DELETE")
                    or "cancel" in req["url"].lower()
                    or "ord" in req["url"].lower()
                ):
                    request_map[p["requestId"]] = {
                        "url": req["url"],
                        "method": req["method"],
                        "postData": req.get("postData", "")[:500],
                        "headers": {
                            k: v for k, v in list(req.get("headers", {}).items())[:8]
                        },
                    }
            elif method == "Network.responseReceived":
                p = evt["params"]
                rid = p["requestId"]
                if rid in request_map:
                    request_map[rid]["status"] = p["response"]["status"]
                    request_map[rid]["mimeType"] = p["response"]["mimeType"]

        out["xhr"] = list(request_map.values())

        # 페이지 정보
        mid = await send(
            "Runtime.evaluate",
            {
                "expression": "JSON.stringify({title: document.title, url: location.href})",
                "returnByValue": True,
            },
        )
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
            evt = json.loads(raw)
            if evt.get("id") == mid:
                val = evt.get("result", {}).get("result", {}).get("value", "{}")
                info = json.loads(val) if isinstance(val, str) else val
                out["title"] = info.get("title", "")
                out["url"] = info.get("url", "")
                break

        # 취소 버튼 후보 추출
        js = r"""
        (() => {
          const out = [];
          const all = document.querySelectorAll('a, button, input[type=button], input[type=submit], [role=button]');
          for (const el of all) {
            const t = (el.innerText || el.value || '').trim();
            if (!t) continue;
            if (/취소|cancel/i.test(t) && t.length < 40) {
              const path = (() => {
                const parts = [];
                let n = el;
                while (n && n !== document.body && parts.length < 6) {
                  let s = n.tagName.toLowerCase();
                  if (n.id) s += '#' + n.id;
                  if (n.className && typeof n.className === 'string') {
                    s += '.' + n.className.trim().split(/\s+/).slice(0,3).join('.');
                  }
                  parts.unshift(s);
                  n = n.parentElement;
                }
                return parts.join(' > ');
              })();
              out.push({
                text: t,
                tag: el.tagName,
                href: el.getAttribute('href') || '',
                onclick: (el.getAttribute('onclick') || '').slice(0, 200),
                path: path,
                visible: el.offsetParent !== null,
              });
            }
          }
          return JSON.stringify(out);
        })()
        """
        mid = await send("Runtime.evaluate", {"expression": js, "returnByValue": True})
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
            evt = json.loads(raw)
            if evt.get("id") == mid:
                val = evt.get("result", {}).get("result", {}).get("value", "[]")
                out["cancel_candidates"] = (
                    json.loads(val) if isinstance(val, str) else val
                )
                break
    return out


async def main() -> None:
    if len(sys.argv) < 2:
        print("usage: cdp_inspect_cancel.py <site> [<order_no>]")
        sys.exit(1)
    site = sys.argv[1]
    if site not in SITE_URLS:
        print(f"unknown site. choices: {list(SITE_URLS)}")
        sys.exit(1)
    order_no = sys.argv[2] if len(sys.argv) > 2 else ""

    if order_no:
        ws_url = await open_or_navigate(site, order_no)
        await asyncio.sleep(4)
    else:
        # 기존 탭 사용
        t = await find_tab()
        ws_url = t["webSocketDebuggerUrl"]

    res = await collect_dom_and_xhr(ws_url, dwell_sec=8.0)
    print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
