"""ServiceWorker fetch 컨텍스트에서 무신사 API 직접 호출 — cookies/response 확인."""

import asyncio
import json
from urllib.request import urlopen
import websockets


def find_samba_sw():
    for t in json.loads(urlopen("http://localhost:9223/json").read()):
        if t.get("type") != "service_worker" and "ojfcneljbbajgcmpmklgglhenieehicb" not in t.get("url", ""):
            continue
        if "ojfcneljbbajgcmpmklgglhenieehicb" in t.get("url", ""):
            return t
    return None


JS = r"""
(async () => {
  const out = {};
  // 1. chrome.cookies API — musinsa.com 쿠키 카운트
  try {
    const cookies = await chrome.cookies.getAll({domain: 'musinsa.com'});
    out.cookies_count = cookies.length;
    out.cookies_names = cookies.map(c => c.name).slice(0, 30);
    out.NSI_count = cookies.filter(c => /NSI|MUSINSA|sess/i.test(c.name)).length;
  } catch (e) { out.cookies_err = String(e); }

  // 2. fetch login-status (간단 인증 체크)
  try {
    const r = await fetch('https://my.musinsa.com/api/member/v1/login-status', {credentials: 'include'});
    const txt = await r.text();
    out.login_status = r.status;
    out.login_body_head = txt.slice(0, 200);
    out.login_ct = r.headers.get('content-type');
  } catch (e) { out.login_err = String(e); }

  // 3. fetch get_order_view
  try {
    const r = await fetch('https://www.musinsa.com/order-service/my/order/get_order_view/202605261047010005', {credentials: 'include'});
    const txt = await r.text();
    out.view_status = r.status;
    out.view_ct = r.headers.get('content-type');
    out.view_body_head = txt.slice(0, 400);
  } catch (e) { out.view_err = String(e); }

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
