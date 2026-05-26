"""SW에서 chrome.cookies API로 무신사 모든 도메인 쿠키 수집."""

import asyncio
import json
from urllib.request import urlopen
import websockets


def find_samba_sw():
    for t in json.loads(urlopen("http://localhost:9223/json").read()):
        if t.get("type") == "service_worker" and "ojfcneljbbajgcmpmklgglhenieehicb" in t.get("url", ""):
            return t
    return None


JS = r"""
(async () => {
  const out = {};
  const domains = ['musinsa.com', '.musinsa.com', 'www.musinsa.com', 'my.musinsa.com', 'api.musinsa.com', 'order.musinsa.com'];
  const all = {};
  for (const d of domains) {
    const c = await new Promise(r => chrome.cookies.getAll({domain: d}, r));
    all[d] = c.map(x => ({name: x.name, domain: x.domain, path: x.path, httpOnly: x.httpOnly, secure: x.secure, sameSite: x.sameSite, hostOnly: x.hostOnly}));
  }
  // url 기준
  const byUrl = await new Promise(r => chrome.cookies.getAll({url: 'https://www.musinsa.com/'}, r));
  out.byUrl_www = byUrl.map(x => ({name: x.name, domain: x.domain, sameSite: x.sameSite, httpOnly: x.httpOnly}));
  const byUrl2 = await new Promise(r => chrome.cookies.getAll({url: 'https://my.musinsa.com/api/member/v1/login-status'}, r));
  out.byUrl_my = byUrl2.map(x => ({name: x.name, domain: x.domain, sameSite: x.sameSite, httpOnly: x.httpOnly}));
  const byUrl3 = await new Promise(r => chrome.cookies.getAll({url: 'https://api.musinsa.com/'}, r));
  out.byUrl_api = byUrl3.map(x => ({name: x.name, domain: x.domain, sameSite: x.sameSite, httpOnly: x.httpOnly}));

  out.by_domain = all;
  return JSON.stringify(out);
})()
"""


async def main():
    sw = find_samba_sw()
    if not sw:
        print("no SW")
        return
    async with websockets.connect(sw["webSocketDebuggerUrl"], max_size=80_000_000) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {"expression": JS, "returnByValue": True, "awaitPromise": True}}))
        while True:
            e = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
            if e.get("id") == 1:
                v = e["result"]["result"]["value"]
                print(json.dumps(json.loads(v) if isinstance(v, str) else v, ensure_ascii=False, indent=2))
                break


asyncio.run(main())
