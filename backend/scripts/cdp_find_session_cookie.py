"""무신사 진짜 세션 쿠키 도메인 찾기."""

import asyncio
import json
from urllib.request import urlopen
import websockets


def find_samba_sw():
    for t in json.loads(urlopen("http://localhost:9223/json").read()):
        if t.get("type") == "service_worker" and "ojfcneljbbajgcmpmklgglhenieehicb" in t.get("url", ""):
            return t
    return None


def find_musinsa_tab():
    for t in json.loads(urlopen("http://localhost:9223/json").read()):
        if t.get("type") == "page" and "musinsa.com" in t.get("url", ""):
            return t
    return None


JS_SW = r"""
(async () => {
  const out = {};
  // 전체 cookies — 도메인 필터 없이
  const all = await new Promise(r => chrome.cookies.getAll({}, r));
  // musinsa 도메인 포함 모든 쿠키
  const musinsa = all.filter(c => /musinsa/i.test(c.domain));
  out.all_count = all.length;
  out.musinsa_cookies = musinsa.map(c => ({domain: c.domain, name: c.name, sameSite: c.sameSite, httpOnly: c.httpOnly, secure: c.secure, partitionKey: c.partitionKey}));
  // 모든 musinsa 도메인 카운트
  const domains = {};
  for (const c of musinsa) domains[c.domain] = (domains[c.domain] || 0) + 1;
  out.musinsa_domains = domains;
  return JSON.stringify(out);
})()
"""


JS_TAB = r"""
(() => {
  const out = {};
  out.location = location.href;
  out.document_cookie = document.cookie.slice(0, 1500);
  out.document_cookie_count = document.cookie.split(';').filter(Boolean).length;
  return JSON.stringify(out);
})()
"""


async def main():
    sw = find_samba_sw()
    if sw:
        async with websockets.connect(sw["webSocketDebuggerUrl"], max_size=80_000_000) as ws:
            await ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {"expression": JS_SW, "returnByValue": True, "awaitPromise": True}}))
            while True:
                e = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                if e.get("id") == 1:
                    v = e["result"]["result"]["value"]
                    print("=== SW chrome.cookies (전체 musinsa) ===")
                    print(json.dumps(json.loads(v) if isinstance(v, str) else v, ensure_ascii=False, indent=2))
                    break

    tab = find_musinsa_tab()
    if tab:
        async with websockets.connect(tab["webSocketDebuggerUrl"], max_size=80_000_000) as ws:
            await ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {"expression": JS_TAB, "returnByValue": True}}))
            while True:
                e = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                if e.get("id") == 1:
                    v = e["result"]["result"]["value"]
                    print("\n=== 무신사 탭 document.cookie ===")
                    print(json.dumps(json.loads(v) if isinstance(v, str) else v, ensure_ascii=False, indent=2))
                    break


asyncio.run(main())
