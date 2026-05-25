"""헤드리스 Playwright로 소싱처 주문상세 진입 → 취소 흐름 분석.

데몬 storage_state 복사본 사용 (데몬 실행 중 lock 회피).
취소 버튼 클릭 → 사유 모달까지 진입, 최종 확정 절대 클릭 안 함.

usage: python hl_analyze_cancel.py <SITE> <ORDER_NO>
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from playwright.async_api import async_playwright


SITE_DETAIL_URL = {
    "MUSINSA": "https://www.musinsa.com/order/order-detail/{ord_no}",
    "ABCmart": "https://abcmart.a-rt.com/mypage/order/read-order-detail?orderNo={ord_no}",
    "GrandStage": "https://grandstage.a-rt.com/mypage/order/read-order-detail?orderNo={ord_no}",
    "LOTTEON": "https://www.lotteon.com/p/order/claim/orderDetail?odNo={ord_no}",
    "SSG": "https://pay.ssg.com/myssg/orderInfoDetail.ssg?orordNo={ord_no}",
    "GSShop": "https://www.gsshop.com/ord/dlvcursta/popup/ordDtl.gs?ordNo={ord_no}&ecOrdTypCd=S",
}


# 클릭 안전 토큰 — 이 글자로 시작/포함되면 클릭 (모달 트리거)
SAFE_CLICK_TOKENS = ("주문취소", "취소요청", "취소 요청", "주문 취소", "취소신청")
# 절대 클릭 금지 토큰 — 최종 확정 단계
FORBIDDEN_CLICK_TOKENS = (
    "취소확정", "취소 확정", "확정", "취소하기", "취소 완료",
    "확인", "신청완료", "신청 완료", "submit", "ok",
)


def copy_storage_state(daemon_profile: Path) -> Path:
    src = daemon_profile / "storage_state.json"
    if not src.exists():
        raise FileNotFoundError(src)
    dst = Path(tempfile.gettempdir()) / "samba_cancel_analysis_storage.json"
    shutil.copy2(src, dst)
    return dst


async def run(site: str, ord_no: str) -> dict:
    profile = Path(os.environ.get("DAEMON_PROFILE_DIR") or Path.home() / ".autotune_daemon" / "chromium_profile")
    storage = copy_storage_state(profile)

    url = SITE_DETAIL_URL[site].format(ord_no=ord_no)

    out: dict = {
        "site": site, "ord_no": ord_no, "url": url,
        "final_url": "", "title": "",
        "cancel_buttons_initial": [], "cancel_buttons_after_click": [],
        "click_status": "", "xhr_before": [], "xhr_after": [],
        "modal_html": [], "errors": [],
    }

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        ctx = await browser.new_context(
            storage_state=str(storage),
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        page = await ctx.new_page()

        captured: list[dict] = []

        async def on_request(req):
            try:
                if req.method in ("POST", "PUT", "DELETE") or any(k in req.url.lower() for k in ("cancel", "claim", "ord", "order")):
                    captured.append({
                        "url": req.url[:300],
                        "method": req.method,
                        "post_data": (req.post_data or "")[:600],
                        "phase": "pre",
                    })
            except Exception:
                pass

        page.on("request", on_request)
        page.on("dialog", lambda d: asyncio.create_task(d.dismiss()))

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(3000)
        except Exception as e:
            out["errors"].append(f"goto: {e}")

        try:
            out["final_url"] = page.url
            out["title"] = await page.title()
        except Exception as e:
            out["errors"].append(f"meta: {e}")

        # initial buttons
        try:
            btns = await page.evaluate(BUTTON_FINDER_JS)
            out["cancel_buttons_initial"] = btns
        except Exception as e:
            out["errors"].append(f"find_btns: {e}")
            btns = []

        # mark pre-click captures
        for c in captured:
            c["phase"] = "pre"
        out["xhr_before"] = list(captured)
        captured.clear()

        # 안전한 1차 취소 버튼만 클릭
        target = None
        for b in btns:
            t = b.get("text", "").replace(" ", "")
            if any(tok.replace(" ", "") in t for tok in SAFE_CLICK_TOKENS) and b.get("visible"):
                if not any(forb.replace(" ", "") in t for forb in FORBIDDEN_CLICK_TOKENS):
                    target = b
                    break

        if target:
            try:
                clicked = await page.evaluate(MAKE_CLICK_JS(target))
                out["click_status"] = clicked
            except Exception as e:
                out["click_status"] = f"click-error: {e}"
            await page.wait_for_timeout(4500)
        else:
            out["click_status"] = "no-safe-button"

        # after click XHR
        for c in captured:
            c["phase"] = "post"
        out["xhr_after"] = list(captured)

        # modal/page DOM
        try:
            out["modal_html"] = await page.evaluate(MODAL_DUMP_JS)
        except Exception as e:
            out["errors"].append(f"modal: {e}")

        # buttons after click
        try:
            out["cancel_buttons_after_click"] = await page.evaluate(BUTTON_FINDER_JS)
        except Exception as e:
            out["errors"].append(f"find_btns2: {e}")

        try:
            out["final_url"] = page.url
        except Exception:
            pass

        await browser.close()
    return out


BUTTON_FINDER_JS = r"""
(() => {
  const out = [];
  const all = document.querySelectorAll('a, button, input[type=button], input[type=submit], [role=button]');
  for (const el of all) {
    const t = (el.innerText || el.value || '').trim();
    if (!t) continue;
    if (/취소|cancel/i.test(t) && t.length < 50) {
      const r = el.getBoundingClientRect();
      out.push({
        text: t, tag: el.tagName, id: el.id || '',
        cls: typeof el.className === 'string' ? el.className.slice(0, 200) : '',
        href: el.getAttribute('href') || '',
        onclick: (el.getAttribute('onclick') || '').slice(0, 300),
        rect: {x: r.x, y: r.y, w: r.width, h: r.height},
        visible: r.width > 0 && r.height > 0 && el.offsetParent !== null,
      });
    }
  }
  return out;
})()
"""


MODAL_DUMP_JS = r"""
(() => {
  const out = [];
  for (const el of document.querySelectorAll('div, section, dialog, form')) {
    const cs = getComputedStyle(el);
    if ((cs.position === 'fixed' || cs.position === 'absolute') &&
        parseInt(cs.zIndex || '0', 10) >= 100 &&
        el.offsetParent !== null && el.getBoundingClientRect().height > 80) {
      out.push({
        id: el.id || '',
        cls: typeof el.className === 'string' ? el.className.slice(0, 200) : '',
        innerText: (el.innerText || '').slice(0, 2000),
        html: el.outerHTML.slice(0, 5000),
      });
    }
  }
  return out;
})()
"""


def MAKE_CLICK_JS(target: dict) -> str:
    txt = json.dumps(target["text"])
    rx, ry = target["rect"]["x"], target["rect"]["y"]
    return f"""
    (() => {{
      const all = document.querySelectorAll('a, button, input[type=button], input[type=submit], [role=button]');
      for (const el of all) {{
        const t = (el.innerText || el.value || '').trim();
        const r = el.getBoundingClientRect();
        if (t === {txt} && Math.abs(r.x - {rx}) < 3 && Math.abs(r.y - {ry}) < 3) {{
          el.scrollIntoView({{block: 'center'}});
          el.click();
          return 'clicked';
        }}
      }}
      return 'not-found';
    }})()
    """


async def main() -> None:
    if len(sys.argv) < 3:
        print("usage: hl_analyze_cancel.py <SITE> <ORDER_NO>")
        print("sites:", list(SITE_DETAIL_URL))
        sys.exit(1)
    site = sys.argv[1]
    ord_no = sys.argv[2]
    if site not in SITE_DETAIL_URL:
        print(f"unknown site: {site}")
        sys.exit(1)
    res = await run(site, ord_no)
    print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
