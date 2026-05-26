"""SAMBA-WAVE 확장앱 ServiceWorker 에서 _cancelMusinsa 직접 호출."""

import asyncio
import json
from urllib.request import urlopen
import websockets


ORD = "202605261047010005"
ACCT = "sa_01KPMNMG32S3QA0SBRT95D5D7B"  # 성희(edelvise06)


def find_samba_sw():
    for t in json.loads(urlopen("http://localhost:9223/json").read()):
        if t.get("type") != "service_worker":
            continue
        url = t.get("url", "")
        # SAMBA-WAVE 확장앱 ID 확인 — manifest.name == "SAMBA-WAVE"
        if "ojfcneljbbajgcmpmklgglhenieehicb" in url:
            return t
    return None


CHECK_SAMBA_JS = r"""
(async () => {
  // 확장앱 manifest 확인
  const m = chrome.runtime.getManifest();
  return JSON.stringify({name: m.name, version: m.version, has_cancelMusinsa: typeof _cancelMusinsa === 'function', has_handleCancelOrderJob: typeof handleCancelOrderJob === 'function'});
})()
"""


CALL_JS_TMPL = r"""
(async () => {
  try {
    if (typeof _cancelMusinsa !== 'function') return JSON.stringify({error: '_cancelMusinsa undefined — 확장앱 reload 필요'});
    const result = await _cancelMusinsa('__ORD__', '__ACCT__');
    return JSON.stringify(result);
  } catch (e) {
    return JSON.stringify({error: String(e?.stack || e)});
  }
})()
"""


async def main():
    sw = find_samba_sw()
    if not sw:
        print(json.dumps({"error": "samba service_worker not found"}, ensure_ascii=False))
        return
    print(f"SW: {sw['url']}")
    async with websockets.connect(sw["webSocketDebuggerUrl"], max_size=80_000_000) as ws:
        # 1. check manifest
        await ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {"expression": CHECK_SAMBA_JS, "returnByValue": True, "awaitPromise": True}}))
        while True:
            e = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
            if e.get("id") == 1:
                v = e["result"]["result"]["value"]
                print("CHECK:", v)
                info = json.loads(v) if isinstance(v, str) else v
                if info.get("name") != "SAMBA-WAVE":
                    print(json.dumps({"error": f"not SAMBA — name={info.get('name')}"}))
                    return
                break

        # 2. call _cancelMusinsa
        js = CALL_JS_TMPL.replace("__ORD__", ORD).replace("__ACCT__", ACCT)
        await ws.send(json.dumps({"id": 2, "method": "Runtime.evaluate", "params": {"expression": js, "returnByValue": True, "awaitPromise": True}}))
        while True:
            e = json.loads(await asyncio.wait_for(ws.recv(), timeout=120))
            if e.get("id") == 2:
                v = e["result"]["result"]["value"]
                print("\n=== _cancelMusinsa RESULT ===")
                print(json.dumps(json.loads(v) if isinstance(v, str) else v, ensure_ascii=False, indent=2))
                break


asyncio.run(main())
