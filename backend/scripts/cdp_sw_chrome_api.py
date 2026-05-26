"""SW 컨텍스트에서 chrome.cookies + chrome.scripting 가용성 확인."""

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
  out.has_chrome = typeof chrome !== 'undefined';
  out.has_chrome_cookies = typeof chrome?.cookies !== 'undefined';
  out.has_chrome_tabs = typeof chrome?.tabs !== 'undefined';
  out.has_chrome_scripting = typeof chrome?.scripting !== 'undefined';
  out.has_chrome_runtime = typeof chrome?.runtime !== 'undefined';
  out.manifest_perms = chrome?.runtime?.getManifest()?.permissions || [];

  // chrome.cookies 직접 호출
  if (chrome?.cookies) {
    try {
      const c = await new Promise((resolve, reject) => {
        chrome.cookies.getAll({domain: 'musinsa.com'}, (cookies) => {
          if (chrome.runtime.lastError) reject(chrome.runtime.lastError);
          else resolve(cookies);
        });
      });
      out.musinsa_cookies_count = c.length;
      out.musinsa_cookies_names = c.map(x => x.name).slice(0, 20);
    } catch (e) { out.cookies_err = String(e); }
  }

  // fetch login-status (cookies 자동 첨부 확인)
  try {
    const r = await fetch('https://my.musinsa.com/api/member/v1/login-status', {credentials: 'include'});
    const txt = await r.text();
    out.login_status = r.status;
    out.login_ct = r.headers.get('content-type');
    out.login_head = txt.slice(0, 150);
  } catch (e) { out.login_err = String(e); }

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
