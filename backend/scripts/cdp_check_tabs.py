import asyncio
import json
from urllib.request import urlopen
import websockets


async def main():
    tabs = [t for t in json.loads(urlopen("http://localhost:9223/json").read()) if t.get("type") == "page"]
    for t in tabs:
        print(f"- {t.get('url', '')[:120]}")
    print("---")
    # 무신사 order-detail 탭 dump button texts
    for t in tabs:
        u = t.get("url", "")
        if "musinsa.com" in u and "order-detail" in u:
            print(f"INSPECT: {u}")
            async with websockets.connect(t["webSocketDebuggerUrl"], max_size=20_000_000) as ws:
                mid = 1
                await ws.send(json.dumps({"id": mid, "method": "Runtime.evaluate", "params": {"expression": r"""
(() => {
  const btns = [];
  for (const el of document.querySelectorAll('button, [role=button]')) {
    const t = (el.innerText || '').trim();
    const r = el.getBoundingClientRect();
    if (r.width === 0) continue;
    btns.push({text: t.slice(0,60), tag: el.tagName, disabled: el.disabled || false, x: r.x, y: r.y});
  }
  const radios = Array.from(document.querySelectorAll('input[type=radio]')).map(r => ({name: r.name, value: r.value, checked: r.checked, label: (r.closest('label') || r.parentElement || {}).innerText?.slice(0,60) || ''}));
  return JSON.stringify({btns, radios, url: location.href, title: document.title});
})()""", "returnByValue": True}}))
                while True:
                    e = json.loads(await ws.recv())
                    if e.get("id") == mid:
                        v = e["result"]["result"]["value"]
                        print(json.dumps(json.loads(v), ensure_ascii=False, indent=2))
                        break

asyncio.run(main())
