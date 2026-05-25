"""웨일 CDP 9223 — 무신사 취소 모달 분석.

전제: 사용자가 이미 주문상세 → '취소 요청' 버튼 누름 → 사유 모달까지 진입함.
'취소 요청하기' 버튼은 절대 클릭 X — DOM + JS 번들 캡처만.
"""

import asyncio
import json
import re
from urllib.request import urlopen, Request

import websockets


def list_tabs() -> list[dict]:
    return [t for t in json.loads(urlopen("http://localhost:9223/json").read()) if t.get("type") == "page"]


def find_musinsa_order_tab() -> dict | None:
    for t in list_tabs():
        u = t.get("url", "")
        if "musinsa.com" in u and "order-detail" in u:
            return t
    return None


async def eval_js(ws, js: str, timeout: float = 8.0):
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


MODAL_DUMP_JS = r"""
(() => {
  const out = [];
  // bottom-sheet / dialog / fixed overlay
  for (const el of document.querySelectorAll('div, section, dialog, form, aside')) {
    const cs = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    if ((cs.position === 'fixed' || cs.position === 'absolute') &&
        parseInt(cs.zIndex || '0', 10) >= 100 &&
        rect.height > 200 && el.offsetParent !== null) {
      out.push({
        tag: el.tagName, id: el.id, cls: (typeof el.className === 'string' ? el.className : '').slice(0, 200),
        zIndex: cs.zIndex,
        rect: {x: rect.x, y: rect.y, w: rect.width, h: rect.height},
        innerText: (el.innerText || '').slice(0, 2500),
      });
    }
  }
  return out;
})()
"""


CANCEL_BUTTON_DETAIL_JS = r"""
(() => {
  const out = [];
  for (const el of document.querySelectorAll('button')) {
    const t = (el.innerText || '').trim();
    if (/취소.*요청|취소요청하기|취소하기|취소 신청|cancel/i.test(t) && t.length < 40) {
      const r = el.getBoundingClientRect();
      const fiber = Object.keys(el).find(k => k.startsWith('__reactFiber'));
      const props = Object.keys(el).find(k => k.startsWith('__reactProps'));
      let onClickInfo = '';
      if (props && el[props]) {
        const oc = el[props].onClick;
        if (typeof oc === 'function') onClickInfo = oc.toString().slice(0, 1500);
      }
      out.push({
        text: t, tag: el.tagName, id: el.id,
        cls: (typeof el.className === 'string' ? el.className : '').slice(0, 300),
        rect: {x: r.x, y: r.y, w: r.width, h: r.height},
        disabled: el.disabled,
        hasReactFiber: Boolean(fiber),
        onClickSource: onClickInfo,
      });
    }
  }
  return out;
})()
"""


REASON_OPTIONS_JS = r"""
(() => {
  const out = [];
  // 라디오 + 라벨
  for (const el of document.querySelectorAll('input[type=radio], label')) {
    const t = (el.innerText || el.value || '').trim();
    if (!t) continue;
    if (/단순\s*변심|주문\s*실수|결제\s*수단|기타|상품\s*불량|배송|색상|사이즈|쿠폰/.test(t) || el.tagName === 'INPUT') {
      out.push({
        tag: el.tagName, name: el.name || '', value: el.value || '',
        id: el.id, checked: el.checked || false,
        text: t.slice(0, 60),
      });
    }
  }
  return out;
})()
"""


# 페이지 내 모든 JS chunk URL 수집 → fetch 가능한 것 골라서 'cancel' regex 매치
CHUNK_URLS_JS = r"""
(() => {
  return Array.from(document.querySelectorAll('script[src]')).map(s => s.src);
})()
"""


# fetch interceptor 설치 → 클릭 안 한 채로 어떤 URL이 호출 예정인지 알 수 없음 (실제 호출시만 발현)
# 대안: window.fetch overlay + button onclick 함수 stringify로 호출 URL 추론.


async def main() -> None:
    tab = find_musinsa_order_tab()
    if not tab:
        print(json.dumps({"error": "musinsa order tab not found"}, ensure_ascii=False))
        return

    out: dict = {
        "tab_url": tab["url"],
        "modal": [],
        "cancel_buttons": [],
        "reason_options": [],
        "chunk_urls": [],
        "cancel_pattern_matches": [],
    }

    async with websockets.connect(tab["webSocketDebuggerUrl"], max_size=80_000_000) as ws:
        # discard initial events
        # Modal
        try:
            r = await eval_js(ws, MODAL_DUMP_JS)
            out["modal"] = r.get("result", {}).get("result", {}).get("value", [])
        except Exception as e:
            out["modal_err"] = str(e)

        try:
            r = await eval_js(ws, CANCEL_BUTTON_DETAIL_JS)
            out["cancel_buttons"] = r.get("result", {}).get("result", {}).get("value", [])
        except Exception as e:
            out["btn_err"] = str(e)

        try:
            r = await eval_js(ws, REASON_OPTIONS_JS)
            out["reason_options"] = r.get("result", {}).get("result", {}).get("value", [])
        except Exception as e:
            out["reason_err"] = str(e)

        try:
            r = await eval_js(ws, CHUNK_URLS_JS)
            out["chunk_urls"] = r.get("result", {}).get("result", {}).get("value", [])
        except Exception as e:
            out["chunk_err"] = str(e)

    # JS chunks 에서 cancel API endpoint regex
    interesting: list[dict] = []
    pattern = re.compile(r'["\']([^"\']*(?:cancel|claim|return)[^"\']*)["\']', re.IGNORECASE)
    api_path = re.compile(r'["\']/(?:api\d?|order|p)/[^"\']+["\']')
    for u in out["chunk_urls"]:
        if "msscdn.net" not in u and "musinsa.com" not in u:
            continue
        try:
            req = Request(u, headers={"User-Agent": "Mozilla/5.0"})
            data = urlopen(req, timeout=10).read().decode("utf-8", errors="replace")
        except Exception as e:
            interesting.append({"chunk": u, "error": str(e)})
            continue
        matches = set()
        for m in pattern.finditer(data):
            s = m.group(1)
            if 4 < len(s) < 180:
                matches.add(s)
        if matches:
            interesting.append({"chunk": u[-80:], "matches": sorted(matches)[:30]})
    out["cancel_pattern_matches"] = interesting
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
