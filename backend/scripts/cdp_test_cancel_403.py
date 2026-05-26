"""주문 202605261047010005 cancel POST 직접 호출 — 403 재현."""

import asyncio
import json
from urllib.request import urlopen
import websockets


ORD = "202605261047010005"
OPT = "425961349"


def find_tab():
    for t in json.loads(urlopen("http://localhost:9223/json").read()):
        if t.get("type") == "page" and "musinsa.com" in t.get("url", ""):
            return t
    return None


JS = r"""
(async () => {
  const out = {};
  // 0. 현재 로그인 계정
  try {
    const r = await fetch('https://my.musinsa.com/api/member/v1/login-status', {credentials: 'include'});
    const j = await r.json();
    out.account_nick = j?.data?.memberInfo?.nickName || 'unknown';
  } catch (e) { out.account_err = String(e); }

  // 1. get_order_view (user_id 추출)
  try {
    const r = await fetch('https://www.musinsa.com/order-service/my/order/get_order_view/__ORD__', {credentials: 'include'});
    const j = await r.json();
    out.view_status = r.status;
    out.view_user_id = j?.orderInfo?.user_id;
    out.view_items = (j?.orderList?.orderOptionList || []).map(it => ({
      optNo: it.orderOptionNo, orderState: it.orderState, claimState: it.claimState,
      orderStateText: it.orderStateText,
    }));
  } catch (e) { out.view_err = String(e); }

  // 2. status check
  try {
    const r = await fetch('https://order.musinsa.com/api2/order/v1/order-items/__OPT__/status', {credentials: 'include'});
    const j = await r.json();
    out.status_code = j?.data?.code;
    out.status_text = j?.data?.status;
  } catch (e) { out.status_err = String(e); }

  // 3. cancel POST 강행 (다른 계정 로그인 상태라도)
  try {
    const fd = new FormData();
    fd.append('ord_no', '__ORD__');
    fd.append('ord_opt_nos', '__OPT__');
    fd.append('refund_bank', '');
    fd.append('refund_account', '');
    fd.append('refund_nm', '');
    fd.append('claim_reason', '1');
    fd.append('cancel_content', '');
    const r = await fetch('https://api.musinsa.com/api2/claim/command/mypage/order_cancel_cmd/refund', {
      method: 'POST', body: fd, credentials: 'include',
    });
    const txt = await r.text();
    out.cancel_status = r.status;
    out.cancel_body = txt.slice(0, 1500);
    out.cancel_headers = {};
    for (const [k, v] of r.headers.entries()) out.cancel_headers[k] = v;
  } catch (e) { out.cancel_err = String(e); }

  return JSON.stringify(out);
})()
""".replace("__ORD__", ORD).replace("__OPT__", OPT)


async def main():
    tab = find_tab()
    if not tab:
        print(json.dumps({"error": "no musinsa tab"}))
        return
    print(f"TAB: {tab['url']}")
    async with websockets.connect(tab["webSocketDebuggerUrl"], max_size=80_000_000) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {"expression": JS, "returnByValue": True, "awaitPromise": True}}))
        while True:
            e = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
            if e.get("id") == 1:
                v = e["result"]["result"]["value"]
                print(json.dumps(json.loads(v) if isinstance(v, str) else v, ensure_ascii=False, indent=2))
                break


asyncio.run(main())
