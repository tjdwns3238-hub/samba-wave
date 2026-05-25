"""오토튠 헤드리스 데몬 — 로컬 PC 전용. 완전 자동.

지원 사이트:
- LOTTEON: 로그인 필수
- ABCmart/GrandStage: 로그인 필수(best_benefit_price 정확성)
- SSG: 로그인 불필요, 임직원 alert 자동 dismiss

`--sites=LOTTEON,ABCmart,SSG` CLI 인자로 멀티 사이트 동시 처리.

자동화 흐름:
1. device_id = `samba-daemon-<hostname>` 자동 생성
2. `/proxy/extension-key` 호출 → API key 발급
3. requires_login 사이트 각각 `/proxy/login-credential?site_name=<site>` → 자동 로그인
4. Playwright Chromium 영속 프로필 launch → 쿠키 살아있으면 폴링 진입
5. `/proxy/sourcing/collect-queue` polling, X-Allowed-Sites 사이트 detail 처리
6. 백엔드 `pick_daemon_owner(site)` (`daemon_pool.py`) 가 polling 중인 daemon
   풀에서 site 별 round-robin 으로 잡 owner 박음 → 여러 PC 동시 운용 자동

운영 위치: 로컬 PC. VM 운영 금지.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import logging.handlers
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

# Windows: subprocess.Popen 자식이 새 콘솔창 열지 않도록.
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
DETACHED_PROCESS = 0x00000008

# PyInstaller frozen 모드 — 번들된 Chromium 경로 사전 설정. playwright import 전에 set.
if getattr(sys, "frozen", False):
    _meipass = getattr(sys, "_MEIPASS", "")
    if _meipass:
        _bundled_browsers = Path(_meipass) / "playwright_browsers"
        if _bundled_browsers.exists():
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(_bundled_browsers)

import httpx  # noqa: E402  # playwright env 설정 후 import 필수
from playwright.async_api import (  # noqa: E402
    BrowserContext,
    Page,
    async_playwright,
)

# 사이트 핸들러 레지스트리 (ABCmart/GrandStage/SSG). LOTTEON 은 본 파일 하단 등록.
try:
    from site_handlers import (  # type: ignore
        LOTTEON_LOGOUT_URL,
        SITE_HANDLERS,
        SiteHandler,
        _LOTTEON_TRACKING_JS,
    )
except ImportError:
    sys.path.insert(0, str(Path(__file__).parent))
    from site_handlers import (  # type: ignore
        LOTTEON_LOGOUT_URL,
        SITE_HANDLERS,
        SiteHandler,
        _LOTTEON_TRACKING_JS,
    )


# ====================================================================
# 데몬 버전 — build.ps1 가 갱신. 자동 업데이트 비교 기준.
# ====================================================================
DAEMON_VERSION = "1.4.2"


# ====================================================================
# Self-install — frozen .exe 가 1번 클릭으로 평생 작동하게 한다
# ====================================================================

_INSTALL_DIR_NAME = "samba-autotune-daemon"
_RUN_KEY_NAME = "SambaAutotuneDaemon"


def _install_dir() -> Path:
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / _INSTALL_DIR_NAME


def _is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def _running_from_install_dir() -> bool:
    if not _is_frozen():
        return True  # 개발 모드 — install 분기 스킵
    try:
        return Path(sys.executable).resolve().parent == _install_dir().resolve()
    except Exception:
        return False


def _register_run_key(exe_path: Path) -> None:
    """HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run 에 데몬 등록.

    로그온 시 자동 시작. admin 권한 불필요.
    재시작 안정성 = 내장 supervisor (_supervisor_loop) 가 child worker 감시.
    """
    if os.name != "nt":
        return
    try:
        import winreg  # type: ignore

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_SET_VALUE,
        )
        winreg.SetValueEx(key, _RUN_KEY_NAME, 0, winreg.REG_SZ, f'"{exe_path}"')
        winreg.CloseKey(key)
        logger_print(f"Run 키 등록 완료: {exe_path}")
    except Exception as exc:
        logger_print(f"Run 키 등록 실패(무시): {exc}")


_DAEMON_UPDATE_URL_LEGACY = (
    "https://github.com/sbk0674-web/samba-wave/releases/latest/download/"
    "samba.exe"
)
# v1.4.2+: backend 경유 self-update — install-token 박힌 exe 자동 받음.
# 데몬이 자동 키 갱신 가능 (옛 키 invalid 케이스도 자동 복구).
_DAEMON_UPDATE_URL_BACKEND = (
    "https://api.samba-wave.co.kr/api/v1/samba/extension-keys/daemon-self-update"
)


def _perform_self_update(api_key: str = "") -> bool:
    """신버전 exe 다운로드 → swap 배치 생성·실행 → True 면 supervisor 종료(배치가 재시작).

    api_key 주입 시 backend 경유 self-update (토큰 박힌 exe → 자동 키 갱신).
    api_key 없으면 GitHub 직접 다운로드(레거시, 토큰 없음).
    """
    if not _is_frozen() or os.name != "nt":
        return False
    import urllib.request

    install_dir = _install_dir()
    exe_path = install_dir / "daemon.exe"
    new_path = install_dir / "daemon.exe.new"
    if api_key:
        # backend 경유 — X-Api-Key 헤더로 인증, install-token 박힌 exe 받음
        try:
            logger_print("자동 업데이트: backend 경유 다운로드 (자동 키 갱신)")
            req = urllib.request.Request(
                _DAEMON_UPDATE_URL_BACKEND, headers={"X-Api-Key": api_key}
            )
            with urllib.request.urlopen(req, timeout=120) as resp, open(
                new_path, "wb"
            ) as f:
                while True:
                    chunk = resp.read(1 << 16)
                    if not chunk:
                        break
                    f.write(chunk)
        except Exception as exc:
            logger_print(f"backend 경유 self-update 실패 → GitHub fallback: {exc}")
            try:
                urllib.request.urlretrieve(_DAEMON_UPDATE_URL_LEGACY, str(new_path))
            except Exception as exc2:
                logger_print(f"GitHub fallback 도 실패(무시): {exc2}")
                return False
    else:
        try:
            logger_print(f"자동 업데이트: 새 exe 다운로드 {_DAEMON_UPDATE_URL_LEGACY}")
            urllib.request.urlretrieve(_DAEMON_UPDATE_URL_LEGACY, str(new_path))
        except Exception as exc:
            logger_print(f"자동 업데이트 다운로드 실패(무시): {exc}")
            return False
    # 원래 args 보존(--worker 제외). install-token(_it-)은 파일명에만 있고 캐시 키 우선이라 무관.
    _args = " ".join(f'"{a}"' for a in sys.argv[1:] if a != "--worker")
    bat = install_dir / "_self_update.bat"
    bat_content = (
        "@echo off\r\n"
        ":wait\r\n"
        'tasklist /FI "IMAGENAME eq daemon.exe" 2>nul | find /I "daemon.exe" >nul '
        "&& (timeout /t 1 >nul & goto wait)\r\n"
        f'move /Y "{new_path}" "{exe_path}" >nul\r\n'
        f'start "" "{exe_path}" {_args}\r\n'
        'del "%~f0"\r\n'
    )
    try:
        bat.write_text(bat_content, encoding="utf-8")
        creationflags = (
            (DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW)
            if os.name == "nt"
            else 0
        )
        subprocess.Popen(
            ["cmd", "/c", str(bat)], close_fds=True, creationflags=creationflags
        )
        logger_print("자동 업데이트 swap 배치 실행 — supervisor 종료(배치가 재시작)")
        return True
    except Exception as exc:
        logger_print(f"swap 배치 실행 실패(무시): {exc}")
        return False


def _self_install_and_relaunch() -> None:
    """현재 .exe 를 %APPDATA%\\samba-autotune-daemon\\daemon.exe 로 복사 후
    그 위치에서 detach 실행. 본 프로세스는 즉시 종료(`os._exit`).

    이미 install dir 에서 실행 중이면 호출되지 않는다.
    """
    src = Path(sys.executable).resolve()
    install_dir = _install_dir()
    install_dir.mkdir(parents=True, exist_ok=True)
    dst = install_dir / "daemon.exe"

    # 옛 데몬이 dst(daemon.exe) 실행 중이면 Windows 파일 잠금 → 복사 실패 → 설치 무산(트레이X).
    # 현재 프로세스는 다운로드 파일명(autotune-daemon-setup_*.exe)이라 daemon.exe 가 아니므로
    # taskkill /IM daemon.exe 가 자기 자신을 죽이지 않는다(설치 디렉토리 밖 실행 시에만 호출됨).
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/IM", "daemon.exe"],
                creationflags=CREATE_NO_WINDOW,
                capture_output=True,
                timeout=10,
            )
        except Exception as exc:
            logger_print(f"옛 데몬 종료 시도 실패(무시): {exc}")

    # 복사 — 잠금 해제까지 최대 5회 재시도
    for _attempt in range(5):
        try:
            shutil.copy2(src, dst)
            break
        except Exception as exc:
            if _attempt == 4:
                logger_print(f"설치 복사 실패: {exc}")
                return
            time.sleep(1.0)

    _register_run_key(dst)

    # 다운로드 파일명(autotune-daemon-setup_it-<token>_be-<hex>.exe)에 박힌 식별자는
    # dst('daemon.exe')로 복사되며 사라진다. 재실행 데몬이 _extract_*(파일명/argv)로
    # 찾도록 argv 에 명시 전달 — 안 하면 install-token 유실 → 글로벌 키 고착(credential 403).
    args = list(sys.argv[1:])
    _src_name = src.name
    for _pat in (r"_it-[0-9a-f]{16,}", r"_be-[0-9a-f]{6,}", r"did=[A-Za-z0-9_.:\-]+"):
        _m = re.search(_pat, _src_name)
        if _m and _m.group(0) not in args:
            args.append(_m.group(0))
    try:
        creationflags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        subprocess.Popen(
            [str(dst), *args],
            close_fds=True,
            creationflags=creationflags if os.name == "nt" else 0,
        )
        logger_print(f"설치 완료 → 신규 프로세스 시작: {dst}")
    except Exception as exc:
        logger_print(f"신규 프로세스 시작 실패: {exc}")

    os._exit(0)


def _log_file_path() -> Path:
    return _install_dir() / "daemon.log"


def logger_print(msg: str) -> None:
    """frozen 초기 부트스트랩 단계 — logging 모듈 set 전 파일 폴백.

    --noconsole 빌드라 stderr 콘솔이 없어 stderr 출력은 사라짐. 파일에 직접 append.
    """
    line = f"[autotune-daemon] {msg}\n"
    try:
        log_path = _log_file_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as fp:
            fp.write(line)
    except Exception:
        pass
    # 디버그용 — 콘솔 있으면 표시(--console 빌드 또는 .py 실행 시)
    try:
        sys.stderr.write(line)
        sys.stderr.flush()
    except Exception:
        pass


logger = logging.getLogger("autotune-daemon")


LOTTEON_LOGIN_URL = "https://www.lotteon.com/p/member/login/common"
LOTTEON_HOME_URL = "https://www.lotteon.com/p/main"

# LOTTEON 로그인 폼 셀렉터 — extension/background-autologin.js:242-246 와 동일
LOTTEON_LOGIN_SELECTORS = {
    "id": ["#inId", 'input[name="inId"]'],
    "pw": ["#Password", 'input[type="password"]'],
    "btn": '[data-cmpnt-name="login_btn_select"]',
}


# 백엔드 LOTTEON 플러그인이 dom_ext 에서 읽는 필드. 변경 시 양쪽 동기화 필요.
LOTTEON_EXTRACT_JS = r"""
(() => {
  try {
    let isLoggedIn = false
    let _domLoginSignal = 'ambiguous'
    try {
      const memInfoEl = document.querySelector('#memInfo')
      if (memInfoEl) {
        const memInfo = JSON.parse(memInfoEl.value || '{}')
        if (memInfo && memInfo.mbNo) { isLoggedIn = true; _domLoginSignal = 'logout_link' }
        else { _domLoginSignal = 'login_link' }
      }
    } catch (_) {}
    if (_domLoginSignal === 'ambiguous') {
      const _headerText = document.querySelector('header, #header')?.innerText
        || (document.body?.innerText || '').substring(0, 300)
      if (_headerText.includes('로그인/회원가입')) { _domLoginSignal = 'login_link'; isLoggedIn = false }
      else { _domLoginSignal = 'logout_link'; isLoggedIn = true }
    }
    if (_domLoginSignal !== 'logout_link') {
      let sawLoggedOutScript = false
      for (const script of document.querySelectorAll('script')) {
        const text = script.textContent || ''
        if (!text || (!text.includes('memInfo') && !text.includes('mbNo'))) continue
        const m = text.match(/["']mbNo["']\s*:\s*["']([^"']{2,})["']/) || text.match(/\bmbNo\s*:\s*["']([^"']{2,})["']/)
        if (m && m[1]) { isLoggedIn = true; _domLoginSignal = 'logout_link'; break }
        if (/["']mbNo["']\s*:\s*(null|["']{2})/.test(text) || /\bmbNo\s*:\s*(null|["']{2})/.test(text)) {
          sawLoggedOutScript = true
        }
      }
      if (_domLoginSignal !== 'logout_link') {
        const _headerText = (
          document.querySelector('header, #header, .header, [class*="header"], nav, [class*="gnb"]')?.innerText
          || (document.body?.innerText || '').substring(0, 400)
        ).replace(/\s+/g, ' ')
        // "로그아웃"만 신뢰 — "마이롯데"/"MY LOTTE"/"주문배송"은 비로그인에도 항상 노출돼
        // login_link(비로그인) 판정을 logout_link로 잘못 덮어써 비로그인 가격 저장 유발.
        if (_headerText.includes('로그아웃')) {
          isLoggedIn = true; _domLoginSignal = 'logout_link'
        } else if (_domLoginSignal !== 'login_link' && (
          ['로그인', '회원가입'].some(t => _headerText.includes(t)) || sawLoggedOutScript
        )) {
          _domLoginSignal = 'login_link'
        }
      }
    }

    let salePrice = 0, originalPrice = 0, benefitPrice = 0
    let name = '', brand = ''

    const nameEl = document.querySelector('h3[class*="product"], [class*="tit_product"], [class*="product-name"], [class*="pdp-title"]')
    name = nameEl?.textContent?.trim() || document.querySelector('meta[property="og:title"]')?.content || ''

    const brandEl = document.querySelector('[class*="brand"] a, [class*="brand-name"]')
    brand = brandEl?.textContent?.trim() || ''

    const bodyText = document.body?.innerText || ''
    const benefitMatch = bodyText.match(/([\d,]+)\s*원\s*나의\s*혜택가/)
    if (benefitMatch) benefitPrice = parseInt(benefitMatch[1].replace(/,/g, ''), 10)

    const promoMatch = bodyText.match(/(\d+)%\s+([\d,]+)\s*원/)
    if (promoMatch) salePrice = parseInt(promoMatch[2].replace(/,/g, ''), 10)

    const delEl = document.querySelector('del, s, [class*="origin"] [class*="price"], [class*="before"] [class*="price"]')
    if (delEl) {
      const delNum = delEl.textContent.replace(/[^0-9]/g, '')
      if (delNum) originalPrice = parseInt(delNum, 10)
    }
    if (!originalPrice && salePrice > 0) {
      const origMatch = bodyText.match(new RegExp((salePrice).toLocaleString() + '\\s*원\\s+([\\.\\d,]+)'))
      if (origMatch) originalPrice = parseInt(origMatch[1].replace(/[^0-9]/g, ''), 10)
    }
    if (!originalPrice) originalPrice = salePrice

    const options = []
    const sizeUl = document.querySelector('ul.selectLists[id^="select-bundleOpt-"]')
    if (sizeUl) {
      sizeUl.querySelectorAll('li').forEach(li => {
        const rawCaption = (li.querySelector('.txt, .caption')?.textContent || '').trim()
        const cleanName = rawCaption.replace(/^\[품절\]\s*/, '').replace(/\s*\(남은수량\s*\d+\)/, '').trim()
        if (!cleanName) return
        const stockText = (li.querySelector('.stock')?.textContent || '').trim()
        const isSoldOut = li.classList.contains('disabled') || stockText === '품절'
        const mStock = stockText.match(/(\d+)\s*개/)
        const mCaption = rawCaption.match(/남은수량\s*(\d+)/)
        const stock = isSoldOut ? 0 : (mStock ? parseInt(mStock[1], 10) : (mCaption ? parseInt(mCaption[1], 10) : null))
        options.push({ name: cleanName, stock, isSoldOut, raw: stockText })
      })
    }

    const images = []
    document.querySelectorAll('[class*="thumb"] img, [class*="swiper"] img, [class*="slide"] img').forEach(img => {
      let src = img.src || img.currentSrc || img.getAttribute('data-src') || ''
      if (src.startsWith('//')) src = 'https:' + src
      if (src && src.includes('http') && !src.includes('data:') && !images.includes(src)) images.push(src)
    })

    const sellerEl = document.querySelector('ul.sellerList > li.currentProduct .sellerGrade strong')
    const seller = sellerEl?.textContent?.trim() || null

    const _productInfoEl = document.querySelector('[class*="pdp"], [class*="prdInfo"], [class*="goods-info"], [class*="product-info"]')
    const _pickupArea = _productInfoEl?.innerText || bodyText.slice(0, 6000)
    const _storePickupOnly = /매장\s*픽업\s*전용/.test(_pickupArea)

    return {
      success: !!(name || salePrice > 0 || options.length > 0),
      site_product_id: window.__PRD_ID__ || '',
      name, brand,
      original_price: originalPrice,
      sale_price: salePrice || benefitPrice,
      best_benefit_price: benefitPrice,
      images: images.slice(0, 9),
      source_site: 'LOTTEON',
      category: '', category1: '', category2: '', category3: '',
      options, seller,
      pageTitle: document.title,
      store_pickup_only: _storePickupOnly,
      _loginRequired: _domLoginSignal === 'login_link',
      login_required: _domLoginSignal === 'login_link',
      _domLoginSignal,
    }
  } catch (e) {
    return { success: false, error: String(e), _domLoginSignal: 'ambiguous' }
  }
})()
"""


# LOTTEON 준비 마커 — "나의 혜택가" 텍스트 등장 = 가격·옵션 모두 렌더 완료 신호.
# 실측(2026-05-24, 웨일 9223, 16상품): 옵션 li 0.64~0.85s, 혜택가 1.13~1.59s 등장.
# 옵션이 혜택가보다 항상 먼저 떠서 혜택가만 마커로 잡으면 가격·재고 모두 안전.
# 과거 5_000ms 고정 대기 → 마커로 ~1.3s 조기 추출, 잡당 ~3.7s 절감(원가 무손실 검증됨).
LOTTEON_MARKER_JS = r"""
(() => {
  try {
    return /([\d,]+)\s*원\s*나의\s*혜택가/.test(document.body?.innerText || '')
  } catch (_) { return false }
})()
"""


# LOTTEON 핸들러 등록 — 본 파일 내부 상수 사용.
SITE_HANDLERS["LOTTEON"] = SiteHandler(
    site="LOTTEON",
    extract_js=LOTTEON_EXTRACT_JS,
    requires_login=True,
    login_url=LOTTEON_LOGIN_URL,
    home_url=LOTTEON_HOME_URL,
    login_selectors=LOTTEON_LOGIN_SELECTORS,
    # 마커 hit 후 300ms 안정화. 혜택가 없는 상품은 marker_timeout(5s)까지 폴링 후
    # 추출 — 기존 5초 고정과 동일 floor라 회귀 없음.
    pre_extract_wait_ms=200,
    pre_extract_marker_js=LOTTEON_MARKER_JS,
    pre_extract_marker_timeout_ms=5_000,
    extract_retry_field="best_benefit_price",
    tracking_js=_LOTTEON_TRACKING_JS,
    logout_url=LOTTEON_LOGOUT_URL,
)


class DaemonState:
    def __init__(self, max_consecutive_fail: int) -> None:
        self.processed = 0
        self.succeeded = 0
        self.failed = 0
        self.consecutive_fail = 0
        self.consecutive_login_required = 0
        self.max_consecutive_fail = max_consecutive_fail
        self.started_at = time.time()

    def record_success(self) -> None:
        self.processed += 1
        self.succeeded += 1
        self.consecutive_fail = 0
        self.consecutive_login_required = 0

    def record_failure(self) -> None:
        self.processed += 1
        self.failed += 1
        self.consecutive_fail += 1

    def record_login_required(self) -> None:
        self.consecutive_login_required += 1

    def reset_login_required(self) -> None:
        self.consecutive_login_required = 0

    def should_die(self) -> bool:
        return self.consecutive_fail >= self.max_consecutive_fail


def _extract_kv_from_argv_or_exename(key: str) -> str | None:
    """`.exe` 파일명 또는 argv 에서 `<key>=<value>` 추출.

    오토튠 페이지가 설치 트리거 시 `autotune-daemon-setup_did=…_backend=…exe`
    형태로 파일명에 박거나, 인자 `--did=…` `--backend=…` 로 전달.
    포크 호환을 위해 backend URL 도 동적 전달한다.
    """
    # 파일명 추출 — URL-safe value (영숫자, 점, 하이픈, 슬래시, 콜론, 언더스코어)
    try:
        exe_name = Path(sys.executable).name
        m = re.search(
            rf"{re.escape(key)}=([A-Za-z0-9_./:-]+?)(?:_(?:did|backend)=|\.exe$|$)",
            exe_name,
        )
        if m:
            return m.group(1)
    except Exception:
        pass
    # argv 에서 추출
    for arg in sys.argv[1:]:
        m = re.match(rf"^--?{re.escape(key)}=(.+)$", arg) or re.match(
            rf"^{re.escape(key)}=(.+)$", arg
        )
        if m:
            return m.group(1)
    return None


def _extract_did_from_argv_or_exename() -> str | None:
    return _extract_kv_from_argv_or_exename("did")


def _extract_install_token() -> str:
    """install-token 추출 — 우선순위: 자기 exe tail 마커 > 파일명/argv `_it-<hex>`.

    1. SaaS 자동등록 (v1.3.1+): 백엔드 `/daemon-installer` 가 exe 파일 끝에 마커
       `#SAMBA_TOKEN=<hex>#` append → 데몬이 자기 exe 마지막 8KB 에서 정규식 추출.
       파일명/PC명 노출 0, 사용자 입력 0. Datadog Agent 패턴.
    2. 레거시 (v1.2.x 이하): 파일명에 `_it-<token>.exe` 박혀있던 시기 호환.
    """
    # 1. 자기 exe tail 마커
    try:
        exe_path = Path(sys.executable)
        if exe_path.exists() and exe_path.is_file():
            with open(exe_path, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 8192))
                tail = f.read().decode("utf-8", errors="ignore")
            m = re.search(r"#SAMBA_TOKEN=([0-9a-f]{32,})#", tail)
            if m:
                return m.group(1)
    except Exception:
        pass
    # 2. 레거시 파일명/argv `_it-<hex>`
    candidates: list[str] = []
    try:
        candidates.append(Path(sys.executable).name)
    except Exception:
        pass
    candidates.extend(sys.argv[1:])
    for src in candidates:
        m = re.search(r"_it-([0-9a-f]{16,})", src)
        if m:
            return m.group(1)
    return ""


def _extract_backend_from_argv_or_exename() -> str | None:
    """파일명/argv 에서 backend URL 추출. 포크 사용자도 본인 backend 가리킬 수 있게.

    다운로드 파일명은 '=' 가 브라우저에서 잘려 `backend=` 가 유실되므로,
    '=' 없는 `_be-<hex>`(URL 을 hex 인코딩) 형식을 우선 추출한다('_it-' 토큰과 동일 전략).
    """
    # 1. '=' 없는 _be-<hex> (다운로드 파일명/argv) 우선 — 브라우저 '=' 절단 회피
    candidates: list[str] = []
    try:
        candidates.append(Path(sys.executable).name)
    except Exception:
        pass
    candidates.extend(sys.argv[1:])
    for src in candidates:
        m = re.search(r"_be-([0-9a-f]{6,})(?:_|\.exe|$)", src)
        if m:
            try:
                url = bytes.fromhex(m.group(1)).decode("utf-8").strip()
            except Exception:
                url = ""
            if url:
                if not url.startswith(("http://", "https://")):
                    url = f"https://{url}"
                return url
    # 2. argv backend= (하위호환)
    val = _extract_kv_from_argv_or_exename("backend")
    if not val:
        return None
    try:
        from urllib.parse import unquote

        val = unquote(val)
    except Exception:
        pass
    if val and not val.startswith(("http://", "https://")):
        val = f"https://{val}"
    return val


def _default_device_id() -> str:
    host = socket.gethostname() or ""
    sanitized = re.sub(r"[^a-zA-Z0-9-]", "-", host).strip("-").lower()
    if not sanitized or sanitized == "unknown":
        # hostname 추출 실패(빈값/한글전용/특수문자) → MAC 기반 고유 ID 폴백.
        # "samba-daemon-unknown" 으로 여러 PC 데몬이 충돌하던 문제 방지.
        import uuid as _uuid

        sanitized = format(_uuid.getnode(), "012x")
    return f"samba-daemon-{sanitized}"


# ====================================================================
# API 키 / 자격증명 부트스트랩
# ====================================================================


async def _exchange_install_token(
    client: httpx.AsyncClient, backend_url: str, token: str, device_id: str
) -> str:
    """install-token → long-lived 테넌트 키 교환. 실패 시 빈 문자열.

    다운로드 시 파일명에 박힌 1시간 만료 install-token 을 첫 실행 때 1회 교환.
    토큰이 install-token 이 아니거나(이미 long-lived) 만료면 403 → 빈 문자열 반환.
    """
    try:
        r = await client.post(
            f"{backend_url}/api/v1/samba/extension-keys/exchange",
            headers={"X-Api-Key": token},
            json={"device_id": device_id, "label": f"데몬 {device_id}"},
            timeout=20.0,
        )
        if r.status_code == 200:
            return (r.json() or {}).get("key", "") or ""
        logger.info(
            "install-token 교환 불가 status=%s (long-lived 키로 간주)", r.status_code
        )
    except Exception as exc:
        logger.warning("install-token 교환 호출 실패: %s", exc)
    return ""


def _prompt_api_key_dialog() -> str:
    """v1.3.0+ — 첫 실행 시 사용자에게 API 키 직접 입력받는 Tk 다이얼로그.

    samba.exe 단일 파일 모델에서 파일명 토큰 없음 → 사용자가 웹 UI '데몬 키 발급' 후
    여기 입력. 입력값은 api_key.txt 에 캐시되어 이후 재사용.
    """
    try:
        import tkinter as tk
        from tkinter import simpledialog

        root = tk.Tk()
        root.withdraw()
        try:
            root.attributes("-topmost", True)
        except Exception:
            pass
        key = simpledialog.askstring(
            "삼바 데몬 API 키 입력",
            (
                "삼바 웹사이트(오토튠 페이지) > '데몬 키 발급' 버튼을 누르고\n"
                "받은 키를 여기에 붙여넣으세요:"
            ),
            parent=root,
        )
        try:
            root.destroy()
        except Exception:
            pass
        return (key or "").strip()
    except Exception as exc:
        logger.warning("API 키 입력 다이얼로그 실패: %s", exc)
        return ""


async def bootstrap_api_key(
    client: httpx.AsyncClient,
    backend_url: str,
    device_id: str,
    cache_path: Path,
    injected_key: str = "",
) -> str:
    """API key 결정. 우선순위: env > injected install-token > 캐시 > Tk dialog > 글로벌(레거시).

    v1.3.0+ samba.exe 단일 파일 모델: 파일명 토큰 없음. 사용자가 웹에서 long-lived 키 발급
    후 (a) 환경변수 SAMBA_API_KEY (b) %APPDATA%\\samba-autotune-daemon\\api_key.txt
    (c) 첫 실행 시 Tk 다이얼로그로 입력. 모든 폴백 실패하면 글로벌 발급(레거시 호환).
    """
    cached = ""
    if cache_path.exists():
        cached = cache_path.read_text(encoding="utf-8").strip()

    # 0. 환경변수 우선 (운영자 자동화 + 변경 즉시 반영)
    env_key = (os.environ.get("SAMBA_API_KEY", "") or "").strip()
    if env_key:
        try:
            if env_key != cached:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(env_key, encoding="utf-8")
                logger.info("SAMBA_API_KEY 환경변수 사용 — 캐시 갱신")
        except Exception as exc:
            logger.debug("env key 캐시 저장 실패(무시): %s", exc)
        return env_key

    # 1. 주입된 install-token 우선 교환 — fresh 다운로드 시 stale 글로벌 캐시를 덮어쓴다.
    if injected_key:
        exchanged = await _exchange_install_token(
            client, backend_url, injected_key, device_id
        )
        if exchanged:
            try:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(exchanged, encoding="utf-8")
            except Exception as exc:
                logger.debug("API key 캐시 저장 실패(무시): %s", exc)
            logger.info("install-token → long-lived 테넌트 키 교환 성공 — 캐시 갱신")
            return exchanged
        # 교환 실패(이미 교환/만료/long-lived) → 캐시된 테넌트 키 우선 사용
        if cached:
            logger.info("install-token 교환 불가 — 캐시 키 사용: %s", cache_path)
            return cached
        # 캐시도 없으면 주입 키 자체를 long-lived 로 간주
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(injected_key, encoding="utf-8")
        except Exception as exc:
            logger.debug("API key 캐시 저장 실패(무시): %s", exc)
        logger.info("주입 키를 long-lived 로 간주하고 사용 — 캐시 저장")
        return injected_key

    # 2. 캐시된 long-lived 키 (install-token 없는 재실행)
    if cached:
        logger.info("API key 캐시 사용: %s", cache_path)
        return cached

    # 3. (v1.3.0+) Tk 다이얼로그로 사용자 직접 입력 — samba.exe 단일 파일 모델
    logger.info("캐시·env 모두 없음 → 사용자 API 키 입력 다이얼로그 표시")
    user_key = _prompt_api_key_dialog()
    if user_key:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(user_key, encoding="utf-8")
            logger.info("사용자 입력 키 캐시 저장 — %s", cache_path)
        except Exception as exc:
            logger.warning("입력 키 캐시 저장 실패(무시): %s", exc)
        return user_key

    # 4. 글로벌 발급 (레거시 — 테넌트 credential 조회 불가, 다이얼로그도 실패한 경우)
    logger.info("API key 발급 요청 → /proxy/extension-key")
    r = await client.post(
        f"{backend_url}/api/v1/samba/sourcing-accounts/extension-key",
        headers={
            "X-Device-Id": device_id,
            "User-Agent": "autotune-daemon/1.0",
            "Origin": "chrome-extension://autotune-daemon",
        },
        json={"gaia_id": device_id, "email": ""},
        timeout=20.0,
    )
    if r.status_code != 200:
        raise RuntimeError(
            f"API key 발급 실패 status={r.status_code} body={r.text[:300]}"
        )
    api_key = (r.json() or {}).get("api_key", "")
    if not api_key:
        raise RuntimeError("API key 발급 응답에 api_key 없음")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(api_key, encoding="utf-8")
    logger.info("API key 발급 완료 — 캐시 저장")
    return api_key


async def fetch_lotteon_credential(
    client: httpx.AsyncClient,
    backend_url: str,
    device_id: str,
    api_key: str,
) -> dict[str, str] | None:
    """등록된 LOTTEON 기본 계정의 username/password 평문 조회."""
    r = await client.get(
        f"{backend_url}/api/v1/samba/sourcing-accounts/login-credential",
        params={"site_name": "LOTTEON"},
        headers={
            "X-Device-Id": device_id,
            "X-Api-Key": api_key,
        },
        timeout=15.0,
    )
    if r.status_code == 404:
        logger.warning(
            "LOTTEON 기본 계정 미등록 — 삼바웨이브 화면에서 소싱처 계정 추가 필요"
        )
        return None
    if r.status_code != 200:
        logger.warning(
            "login-credential 실패 status=%s body=%s", r.status_code, r.text[:200]
        )
        return None
    data = r.json() or {}
    if not data.get("username") or not data.get("password"):
        return None
    return {"username": data["username"], "password": data["password"]}


# ====================================================================
# LOTTEON Playwright 로그인
# ====================================================================


async def is_lotteon_logged_in(page: Page) -> bool:
    """홈페이지 열고 #memInfo.mbNo 또는 헤더 텍스트로 로그인 확정."""
    try:
        await page.goto(LOTTEON_HOME_URL, wait_until="domcontentloaded", timeout=20_000)
    except Exception as exc:
        logger.warning("LOTTEON home 로드 실패: %s", exc)
        return False
    await page.wait_for_timeout(3_000)
    try:
        result = await page.evaluate(
            """
            () => {
              try {
                const el = document.querySelector('#memInfo')
                if (el) {
                  try {
                    const info = JSON.parse(el.value || '{}')
                    if (info && info.mbNo) return 'logged_in'
                  } catch(_) {}
                }
                const txt = (document.querySelector('header, #header, nav, [class*="gnb"]')?.innerText
                  || (document.body?.innerText || '').substring(0, 400)).replace(/\\s+/g, ' ')
                // 비로그인 신호 우선. "마이롯데"/"MY LOTTE"는 로그인 무관 항상 노출돼
                // 로그인으로 오판 → 자동로그인 스킵 사고 유발하므로 제외. "로그아웃"만 신뢰.
                if (txt.includes('로그인/회원가입')) return 'logged_out'
                if (txt.includes('로그아웃')) return 'logged_in'
                return 'unknown'
              } catch (e) { return 'unknown' }
            }
            """
        )
    except Exception as exc:
        logger.warning("login 검사 evaluate 실패: %s", exc)
        return False
    return result == "logged_in"


async def lotteon_auto_login(page: Page, credential: dict[str, str]) -> bool:
    """LOTTEON 로그인 페이지 form fill + submit 자동."""
    logger.info("LOTTEON 자동로그인 시작 (계정=%s)", credential["username"][:4] + "***")
    try:
        await page.goto(
            LOTTEON_LOGIN_URL, wait_until="domcontentloaded", timeout=30_000
        )
    except Exception as exc:
        logger.warning("LOTTEON 로그인 페이지 로드 실패: %s", exc)
        return False
    await page.wait_for_timeout(2_500)

    selectors_payload = json.dumps(LOTTEON_LOGIN_SELECTORS)
    cred_payload = json.dumps(credential)
    fill_js = f"""
    (() => {{
      const sel = {selectors_payload}
      const cred = {cred_payload}
      const nativeSetter = Object.getOwnPropertyDescriptor(
        window.HTMLInputElement.prototype, 'value').set
      function pick(arr) {{
        for (const s of arr) {{
          const el = document.querySelector(s)
          if (el) return el
        }}
        return null
      }}
      const idField = pick(sel.id)
      const pwField = pick(sel.pw)
      if (!idField || !pwField) {{
        return {{ ok: false, reason: 'fields not found',
                 inputs: Array.from(document.querySelectorAll('input')).slice(0, 20).map(i => ({{id:i.id,name:i.name,type:i.type}}))
        }}
      }}
      idField.focus()
      nativeSetter.call(idField, cred.username)
      idField.dispatchEvent(new Event('input', {{ bubbles: true }}))
      idField.dispatchEvent(new Event('change', {{ bubbles: true }}))
      pwField.focus()
      nativeSetter.call(pwField, cred.password)
      pwField.dispatchEvent(new Event('input', {{ bubbles: true }}))
      pwField.dispatchEvent(new Event('change', {{ bubbles: true }}))
      const btn = document.querySelector(sel.btn)
      if (!btn) return {{ ok: false, reason: 'btn not found' }}
      btn.click()
      return {{ ok: true }}
    }})()
    """
    res = await page.evaluate(fill_js)
    if not (isinstance(res, dict) and res.get("ok")):
        logger.warning("LOTTEON 로그인 form fill 실패: %s", res)
        return False

    # 로그인 후 리다이렉트/세션 안정화 대기. 최대 15초.
    for _ in range(15):
        await page.wait_for_timeout(1_000)
        if await is_lotteon_logged_in(page):
            logger.info("LOTTEON 자동로그인 성공")
            return True
    logger.warning("LOTTEON 자동로그인 — 15초 후에도 로그인 확정 안 됨 (CAPTCHA 의심)")
    return False


async def fetch_credential(
    client: httpx.AsyncClient,
    backend_url: str,
    device_id: str,
    api_key: str,
    site_name: str,
    account_id: str = "",
) -> dict[str, str] | None:
    """site 기본 계정(또는 account_id 지정 계정)의 username/password 평문 조회.

    account_id 지정 시 그 계정 단건 조회 (송장조회 = 주문 매칭 계정 로그인).
    백엔드 엔드포인트가 account_id 우선 처리.
    """
    params = {"account_id": account_id} if account_id else {"site_name": site_name}
    r = await client.get(
        f"{backend_url}/api/v1/samba/sourcing-accounts/login-credential",
        params=params,
        headers={
            "X-Device-Id": device_id,
            "X-Api-Key": api_key,
        },
        timeout=15.0,
    )
    if r.status_code == 404:
        logger.warning(
            "%s 계정 미등록 (account_id=%s) — 삼바웨이브에서 소싱처 계정 추가 필요",
            site_name,
            account_id or "기본",
        )
        return None
    if r.status_code != 200:
        logger.warning(
            "%s login-credential 실패 status=%s body=%s",
            site_name,
            r.status_code,
            r.text[:200],
        )
        return None
    data = r.json() or {}
    if not data.get("username") or not data.get("password"):
        return None
    return {"username": data["username"], "password": data["password"]}


async def is_site_logged_in(page: Page, handler: SiteHandler) -> bool:
    """handler.home_url 방문 + login_check_js 로 로그인 여부 확정.

    LOTTEON 은 login_check_js 미정의 시 본 파일 내부 is_lotteon_logged_in 위임.
    """
    if handler.site == "LOTTEON" and not handler.login_check_js:
        return await is_lotteon_logged_in(page)
    if not handler.home_url:
        return False
    try:
        await page.goto(handler.home_url, wait_until="domcontentloaded", timeout=20_000)
    except Exception as exc:
        logger.warning("%s home 로드 실패: %s", handler.site, exc)
        return False
    await page.wait_for_timeout(3_000)
    if not handler.login_check_js:
        return False
    try:
        result = await page.evaluate(handler.login_check_js)
    except Exception as exc:
        logger.warning("%s login 검사 evaluate 실패: %s", handler.site, exc)
        return False
    return result == "logged_in"


async def auto_login_site(
    page: Page, handler: SiteHandler, credential: dict[str, str]
) -> bool:
    """handler.login_url + login_selectors 로 form fill + submit + 검증."""
    logger.info(
        "%s 자동로그인 시작 (계정=%s)",
        handler.site,
        credential["username"][:4] + "***",
    )
    try:
        await page.goto(
            handler.login_url, wait_until="domcontentloaded", timeout=30_000
        )
    except Exception as exc:
        logger.warning("%s 로그인 페이지 로드 실패: %s", handler.site, exc)
        return False
    await page.wait_for_timeout(2_500)

    selectors_payload = json.dumps(handler.login_selectors)
    cred_payload = json.dumps(credential)
    fill_js = f"""
    (() => {{
      const sel = {selectors_payload}
      const cred = {cred_payload}
      const nativeSetter = Object.getOwnPropertyDescriptor(
        window.HTMLInputElement.prototype, 'value').set
      function pick(arr) {{
        if (typeof arr === 'string') arr = [arr]
        for (const s of arr) {{
          const el = document.querySelector(s)
          if (el) return el
        }}
        return null
      }}
      const idField = pick(sel.id)
      const pwField = pick(sel.pw)
      if (!idField || !pwField) {{
        return {{ ok: false, reason: 'fields not found' }}
      }}
      idField.focus()
      nativeSetter.call(idField, cred.username)
      idField.dispatchEvent(new Event('input', {{ bubbles: true }}))
      idField.dispatchEvent(new Event('change', {{ bubbles: true }}))
      pwField.focus()
      nativeSetter.call(pwField, cred.password)
      pwField.dispatchEvent(new Event('input', {{ bubbles: true }}))
      pwField.dispatchEvent(new Event('change', {{ bubbles: true }}))
      const btn = pick(sel.btn)
      if (!btn) return {{ ok: false, reason: 'btn not found' }}
      btn.click()
      return {{ ok: true }}
    }})()
    """
    res = await page.evaluate(fill_js)
    if not (isinstance(res, dict) and res.get("ok")):
        logger.warning("%s 로그인 form fill 실패: %s", handler.site, res)
        return False

    # 로그인 후 세션 안정화 대기. 최대 15초.
    for _ in range(15):
        await page.wait_for_timeout(1_000)
        if await is_site_logged_in(page, handler):
            logger.info("%s 자동로그인 성공", handler.site)
            return True
    logger.warning(
        "%s 자동로그인 — 15초 후에도 확정 안 됨 (CAPTCHA 의심)", handler.site
    )
    return False


async def ensure_logged_in_for_site(
    page: Page,
    client: httpx.AsyncClient,
    backend_url: str,
    device_id: str,
    api_key: str,
    handler: SiteHandler,
) -> bool:
    """site 별 로그인 상태 확인 → 미로그인 시 1회 자동로그인 시도."""
    if not handler.requires_login:
        return True
    if await is_site_logged_in(page, handler):
        logger.info("%s 세션 살아있음 — 자동로그인 스킵", handler.site)
        return True
    cred = await fetch_credential(client, backend_url, device_id, api_key, handler.site)
    if not cred:
        logger.error(
            "%s 자격증명 미등록 — 삼바웨이브에서 %s 기본 계정 추가 필요",
            handler.site,
            handler.site,
        )
        return False
    return await auto_login_site(page, handler, cred)


async def ensure_logged_in(
    page: Page,
    client: httpx.AsyncClient,
    backend_url: str,
    device_id: str,
    api_key: str,
) -> bool:
    """하위호환 shim — LOTTEON 로그인 확인."""
    return await ensure_logged_in_for_site(
        page,
        client,
        backend_url,
        device_id,
        api_key,
        SITE_HANDLERS["LOTTEON"],
    )


# ====================================================================
# 송장(tracking) 처리 — 주문 매칭 계정 로그인 + 배송조회 페이지 스크랩
# ====================================================================

# site → 마지막으로 로그인한 sourcing_account_id (계정 스왑 최소화용).
# 큐가 sourcingAccountId 순으로 잡을 주므로 같은 계정 연속 → 스왑 횟수 = 계정 수.
_last_tracking_account: dict[str, str] = {}


async def logout_site(page: Page, handler: SiteHandler) -> None:
    """계정 전환용 정식 로그아웃 — 서버 세션 expire + Set-Cookie 쿠키 정리."""
    if not handler.logout_url:
        return
    try:
        await page.goto(
            handler.logout_url, wait_until="domcontentloaded", timeout=20_000
        )
        await page.wait_for_timeout(1_500)
        logger.info("%s 로그아웃 완료 (계정 전환)", handler.site)
    except Exception as exc:
        logger.warning("%s 로그아웃 실패(무시): %s", handler.site, str(exc)[:100])


async def ensure_logged_in_as_account(
    page: Page,
    client: httpx.AsyncClient,
    backend_url: str,
    device_id: str,
    api_key: str,
    handler: SiteHandler,
    account_id: str,
) -> bool:
    """주문 매칭 계정(account_id)으로 로그인 보장.

    마지막 로그인 계정과 같고 세션 살아있으면 스킵. 다르면 로그아웃 후 그 계정 로그인.
    account_id 없으면 site 기본 계정으로 로그인(레거시 폴백).
    """
    site = handler.site
    last = _last_tracking_account.get(site, "")

    # 같은 계정 연속 + 세션 살아있으면 스왑 스킵 (빠른 경로)
    if account_id and last == account_id and await is_site_logged_in(page, handler):
        logger.info("%s 계정 %s 세션 유지 — 스왑 스킵", site, account_id)
        return True

    cred = await fetch_credential(
        client, backend_url, device_id, api_key, site, account_id=account_id
    )
    if not cred:
        logger.error("%s 자격증명 조회 실패 account_id=%s", site, account_id or "기본")
        return False

    # 다른 계정 로그인 중이면 먼저 로그아웃 (세션 정리)
    if last and last != account_id:
        await logout_site(page, handler)
        _last_tracking_account.pop(site, None)

    ok = await auto_login_site(page, handler, cred)
    if ok:
        _last_tracking_account[site] = account_id or "_default"
    return ok


async def extract_tracking(
    page: Page, url: str, handler: SiteHandler
) -> dict[str, Any]:
    """송장조회 페이지 진입 + 스크랩 → {success, courierName, trackingNumber}.

    단일 페이지(SSG/ABC/LOTTEON): goto → tracking_js evaluate.
    2단계(MUSINSA): goto 주문상세 → tracking_click_js 클릭 → trace 네비 대기 → tracking_js.
    """
    if not handler.tracking_js:
        return {"success": False, "error": f"{handler.site} tracking_js 미정의"}
    try:
        await page.goto(url, wait_until="commit", timeout=30_000)
    except Exception as exc:
        return {"success": False, "error": f"송장 페이지 로드 실패: {str(exc)[:120]}"}
    # 헤드리스 탭 렌더 보장 (배경 스로틀 회피)
    try:
        await page.bring_to_front()
    except Exception:
        pass

    # ── 2단계(two-hop) 흐름 — MUSINSA: 배송조회 클릭 → trace 페이지 네비 → 스크랩 ──
    if handler.tracking_two_hop:
        try:
            click_res = await page.evaluate(handler.tracking_click_js)
        except Exception as exc:
            return {"success": False, "error": f"tracking click 예외: {str(exc)[:120]}"}
        if not isinstance(click_res, dict) or not click_res.get("clicked"):
            return {
                "success": False,
                "error": (click_res or {}).get("error", "배송조회 클릭 실패"),
                "cancelled": bool((click_res or {}).get("cancelled")),
            }
        # 클릭 후 trace 페이지 도착 대기 (pushState/풀네비 모두 wait_for_url 로 커버)
        try:
            await page.wait_for_url(handler.tracking_trace_url_glob, timeout=20_000)
        except Exception:
            return {
                "success": False,
                "error": f"trace 페이지 미진입 (현재 {page.url})",
            }
        try:
            data = await page.evaluate(handler.tracking_js)
        except Exception as exc:
            return {"success": False, "error": f"trace 스크랩 예외: {str(exc)[:120]}"}
        return (
            data
            if isinstance(data, dict)
            else {"success": False, "error": "trace 결과 비dict"}
        )

    # ── 단일 페이지 흐름 ──
    try:
        data = await page.evaluate(handler.tracking_js)
    except Exception as exc:
        return {"success": False, "error": f"tracking evaluate 예외: {str(exc)[:120]}"}
    return (
        data
        if isinstance(data, dict)
        else {"success": False, "error": "evaluate 결과 비dict"}
    )


async def post_tracking_result(
    client: httpx.AsyncClient,
    backend_url: str,
    request_id: str,
    data: dict[str, Any],
    api_key: str,
) -> bool:
    """송장 결과를 /sourcing/tracking-result 로 전송 (확장앱과 동일 엔드포인트).

    body = {requestId, success, courierName, trackingNumber, error, cancelled}
    """
    url = f"{backend_url}/api/v1/samba/proxy/sourcing/tracking-result"
    body = {
        "requestId": request_id,
        "success": bool(data.get("success")),
        "courierName": data.get("courierName") or "",
        "trackingNumber": data.get("trackingNumber") or "",
        "error": data.get("error") or "",
        "cancelled": bool(data.get("cancelled")),
    }
    headers = {"X-Api-Key": api_key}
    for attempt in range(len(_RETRY_DELAYS) + 1):
        try:
            r = await client.post(url, json=body, headers=headers, timeout=15.0)
        except Exception as exc:
            if attempt < len(_RETRY_DELAYS):
                await asyncio.sleep(_RETRY_DELAYS[attempt])
                continue
            logger.warning("송장 결과전송 예외 (포기): %s", exc)
            return False
        if r.is_success:
            return True
        if r.status_code in _RETRY_STATUSES and attempt < len(_RETRY_DELAYS):
            await asyncio.sleep(_RETRY_DELAYS[attempt])
            continue
        logger.warning(
            "송장 결과전송 실패 status=%s body=%s", r.status_code, r.text[:200]
        )
        return False
    return False


async def process_tracking_job(
    page: Page,
    client: httpx.AsyncClient,
    backend_url: str,
    job: dict[str, Any],
    state: DaemonState,
    api_key: str,
    device_id: str,
) -> None:
    """송장 잡 1개 처리 — 계정 로그인 → 배송조회 스크랩 → tracking-result 회신."""
    request_id = job.get("requestId", "")
    site = job.get("site", "")
    url = job.get("url", "")
    account_id = job.get("sourcingAccountId", "") or ""
    # 송장 잡 site 는 대문자(ABCMART/GRANDSTAGE)인데 핸들러 키는 혼합(ABCmart/GrandStage).
    # 정규화해서 매칭 (detail 잡은 핸들러 키 그대로라 영향 없음).
    _TRACKING_SITE_ALIAS = {"ABCMART": "ABCmart", "GRANDSTAGE": "GrandStage"}
    handler_key = _TRACKING_SITE_ALIAS.get(site.upper(), site)
    handler = SITE_HANDLERS.get(handler_key)

    if not handler or not handler.tracking_js:
        await post_tracking_result(
            client,
            backend_url,
            request_id,
            {"success": False, "error": f"daemon tracking 미지원: {site}"},
            api_key,
        )
        state.record_failure()
        return

    logger.info(
        "[송장] 처리 시작 site=%s req=%s acc=%s", site, request_id, account_id or "-"
    )
    t0 = time.time()

    # 1) 주문 매칭 계정 로그인 (송장은 마이페이지라 로그인 필수)
    login_ok = await ensure_logged_in_as_account(
        page, client, backend_url, device_id, api_key, handler, account_id
    )
    if not login_ok:
        await post_tracking_result(
            client,
            backend_url,
            request_id,
            {"success": False, "error": f"{site} 계정 로그인 실패 (acc={account_id})"},
            api_key,
        )
        state.record_failure()
        return

    # 2) 송장조회 페이지 스크랩
    try:
        data = await asyncio.wait_for(
            extract_tracking(page, url, handler), timeout=90.0
        )
    except asyncio.TimeoutError:
        data = {"success": False, "error": "daemon 송장 추출 타임아웃"}
    except Exception as exc:
        data = {"success": False, "error": f"daemon 송장 예외: {str(exc)[:120]}"}

    # needsLogin 신호면 다음 잡에서 재로그인하도록 캐시 무효화
    if data.get("needsLogin"):
        _last_tracking_account.pop(site, None)

    await post_tracking_result(client, backend_url, request_id, data, api_key)
    dt = time.time() - t0
    if data.get("success"):
        logger.info(
            "[송장] 완료 req=%s %s/%s (%.1fs)",
            request_id,
            data.get("courierName"),
            data.get("trackingNumber"),
            dt,
        )
        state.record_success()
    else:
        logger.info(
            "[송장] 미수집 req=%s err=%s (%.1fs)", request_id, data.get("error"), dt
        )
        state.record_failure()


# ====================================================================
# 잡 폴링 / 처리
# ====================================================================


async def fetch_job(
    client: httpx.AsyncClient,
    backend_url: str,
    device_id: str,
    api_key: str,
    allowed_sites: str = "LOTTEON",
    poll_site: str = "",
) -> dict[str, Any] | None:
    """잡 1개 폴링.

    allowed_sites: X-Allowed-Sites 헤더 — 이 데몬이 처리 가능한 사이트 '전체'.
      백엔드 `_pc_allowed_sites` 등록용 → `pick_daemon_owner(site)` 가 이 데몬을
      모든 활성 사이트의 owner 후보로 인식하게 한다. (전체로 보내야 함)
    poll_site: X-Poll-Site 헤더 — 이번 폴링이 '실제 가져갈' 단일 사이트.
      사이트별 병렬 워커가 자기 사이트 잡만 dequeue 하도록 스코프.
      비어있으면 allowed_sites 전체에서 dequeue (단일 PC 호환).
    [중요] 등록(allowed_sites)과 잡필터(poll_site)를 분리하지 않으면, 병렬 워커가
    단일 사이트로 폴링할 때 등록값이 그 사이트로 덮어써져 다른 사이트 owner 매칭이
    깨진다(LOTTEON env 폴백 → 죽은 디바이스 라우팅 → 60s 타임아웃).
    """
    try:
        _headers = {
            "X-Api-Key": api_key,
            "X-Device-Id": device_id,
            "X-Allowed-Sites": allowed_sites,
            "X-Ext-Version": "99.0.0",
        }
        if poll_site:
            _headers["X-Poll-Site"] = poll_site
        r = await client.get(
            f"{backend_url}/api/v1/samba/proxy/sourcing/collect-queue",
            headers=_headers,
            timeout=10.0,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        logger.warning("polling 실패: %s", exc)
        return None
    if not data.get("hasJob"):
        return None
    return data


_RETRY_STATUSES = {429, 502, 503, 504}
_RETRY_DELAYS = (0.5, 1.5, 3.0)


async def post_result(
    client: httpx.AsyncClient,
    backend_url: str,
    request_id: str,
    data: dict[str, Any],
    api_key: str,
) -> bool:
    url = f"{backend_url}/api/v1/samba/proxy/sourcing/collect-result"
    body = {"requestId": request_id, "data": data}
    headers = {"X-Api-Key": api_key}
    for attempt in range(len(_RETRY_DELAYS) + 1):
        try:
            r = await client.post(url, json=body, headers=headers, timeout=15.0)
        except Exception as exc:
            if attempt < len(_RETRY_DELAYS):
                await asyncio.sleep(_RETRY_DELAYS[attempt])
                continue
            logger.warning("결과전송 예외 (포기): %s", exc)
            return False
        if r.is_success:
            return True
        if r.status_code in _RETRY_STATUSES and attempt < len(_RETRY_DELAYS):
            await asyncio.sleep(_RETRY_DELAYS[attempt])
            continue
        logger.warning("결과전송 실패 status=%s body=%s", r.status_code, r.text[:200])
        return False
    return False


async def extract_pdp(
    page: Page, url: str, product_id: str, handler: SiteHandler
) -> dict[str, Any]:
    """사이트 핸들러 기반 PDP 추출 — marker 폴링 + extract_js + 재시도."""
    # commit — 네비게이션 커밋 즉시 반환. marker 폴링이 준비 판정을 담당하므로
    # DOMContentLoaded 까지 기다리는 중복 대기 제거.
    await page.goto(url, wait_until="commit", timeout=30_000)
    await page.evaluate(f"window.__PRD_ID__ = {json.dumps(product_id)}")

    if handler.pre_extract_marker_js:
        deadline = handler.pre_extract_marker_timeout_ms
        step = 500
        elapsed = 0
        while elapsed < deadline:
            try:
                hit = await page.evaluate(handler.pre_extract_marker_js)
            except Exception:
                hit = False
            if hit:
                break
            await page.wait_for_timeout(step)
            elapsed += step
        await page.wait_for_timeout(handler.pre_extract_wait_ms)
    else:
        await page.wait_for_timeout(handler.pre_extract_wait_ms)

    data = await page.evaluate(handler.extract_js)
    retry_field = handler.extract_retry_field
    if (
        retry_field
        and isinstance(data, dict)
        and not data.get(retry_field)
        and (
            (data.get("sale_price") or 0) > 0
            or (data.get("options") and len(data["options"]) > 0)
        )
    ):
        await page.wait_for_timeout(3_000)
        data2 = await page.evaluate(handler.extract_js)
        if isinstance(data2, dict) and data2.get(retry_field):
            data = data2
    return (
        data
        if isinstance(data, dict)
        else {
            "success": False,
            "error": "evaluate 결과 비dict",
        }
    )


async def extract_lotteon_pdp(page: Page, url: str, product_id: str) -> dict[str, Any]:
    """하위호환 shim — LOTTEON 핸들러로 위임."""
    return await extract_pdp(page, url, product_id, SITE_HANDLERS["LOTTEON"])


async def fetch_autotune_concurrency(
    client: httpx.AsyncClient,
    backend_url: str,
    device_id: str,
    api_key: str,
) -> tuple[dict[str, int], list[str] | None]:
    """백엔드에서 사이트별 동시실행 설정 + 이 데몬이 담당할 사이트 조회.

    반환: (concurrency, assigned_sites)
      concurrency: {site: n} — 담당 사이트별 동시실행 캡. 실패 시 빈 dict.
      assigned_sites: UI에서 지정한 이 데몬의 담당 사이트 목록.
        None = 조회 실패(이전 active_sites 유지). [] = 명시적 미배정(대기).
    이 값만큼 사이트별 페이지를 병렬로 띄워 PC 자원을 활용한다.
    """
    try:
        r = await client.get(
            f"{backend_url}/api/v1/samba/proxy/autotune-daemon/concurrency",
            headers={"X-Device-Id": device_id, "X-Api-Key": api_key},
            timeout=10.0,
        )
        if r.status_code == 200:
            body = r.json() or {}
            raw = body.get("concurrency") or {}
            out: dict[str, int] = {}
            for k, v in raw.items():
                try:
                    out[k] = max(1, int(v))
                except Exception:
                    pass
            assigned = body.get("assigned_sites")
            if isinstance(assigned, list):
                assigned = [str(s).strip() for s in assigned if str(s).strip()]
            else:
                assigned = None
            return out, assigned
    except Exception as exc:
        logger.debug("동시실행/배정 조회 실패: %s", str(exc)[:80])
    return {}, None


async def post_cancel_result(
    client: httpx.AsyncClient,
    backend_url: str,
    request_id: str,
    data: dict[str, Any],
    api_key: str,
) -> bool:
    """발주취소 결과를 /sourcing/cancel-result 로 전송.

    body = {requestId, success, cancelled, alreadyShipped, reason, error}
    """
    url = f"{backend_url}/api/v1/samba/proxy/sourcing/cancel-result"
    body = {
        "requestId": request_id,
        "success": bool(data.get("success")),
        "cancelled": bool(data.get("cancelled")),
        "alreadyShipped": bool(data.get("alreadyShipped")),
        "reason": data.get("reason") or "",
        "error": data.get("error") or "",
    }
    headers = {"X-Api-Key": api_key}
    for attempt in range(len(_RETRY_DELAYS) + 1):
        try:
            r = await client.post(url, json=body, headers=headers, timeout=15.0)
        except Exception as exc:
            if attempt < len(_RETRY_DELAYS):
                await asyncio.sleep(_RETRY_DELAYS[attempt])
                continue
            logger.warning("취소 결과전송 예외 (포기): %s", exc)
            return False
        if r.is_success:
            return True
        if r.status_code in _RETRY_STATUSES and attempt < len(_RETRY_DELAYS):
            await asyncio.sleep(_RETRY_DELAYS[attempt])
            continue
        logger.warning(
            "취소 결과전송 실패 status=%s body=%s", r.status_code, r.text[:200]
        )
        return False
    return False


async def extract_cancel(
    page: Page, url: str, handler: SiteHandler
) -> dict[str, Any]:
    """주문상세/취소 페이지 진입 + 취소 실행 → {success, cancelled, alreadyShipped, ...}.

    단일 페이지: goto → cancel_js evaluate.
    2단계: goto → cancel_click_js → 네비 대기 → cancel_js.
    """
    if not handler.cancel_js:
        return {"success": False, "error": f"{handler.site} cancel_js 미정의"}
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
    except Exception as exc:
        return {"success": False, "error": f"cancel goto 예외: {str(exc)[:120]}"}

    if handler.cancel_two_hop:
        try:
            click_res = await page.evaluate(handler.cancel_click_js)
        except Exception as exc:
            return {"success": False, "error": f"cancel click 예외: {str(exc)[:120]}"}
        if isinstance(click_res, dict) and click_res.get("alreadyShipped"):
            return {
                "success": True,
                "cancelled": False,
                "alreadyShipped": True,
                "reason": "이미 발송 — 취소 불가",
            }
        try:
            if handler.cancel_trace_url_glob:
                await page.wait_for_url(handler.cancel_trace_url_glob, timeout=20_000)
            else:
                await page.wait_for_load_state("domcontentloaded", timeout=20_000)
        except Exception as exc:
            return {"success": False, "error": f"cancel 네비 대기 예외: {str(exc)[:120]}"}

    try:
        data = await page.evaluate(handler.cancel_js)
    except Exception as exc:
        return {"success": False, "error": f"cancel evaluate 예외: {str(exc)[:120]}"}

    return (
        data
        if isinstance(data, dict)
        else {"success": False, "error": "cancel evaluate 결과 비dict"}
    )


async def process_cancel_order_job(
    page: Page,
    client: httpx.AsyncClient,
    backend_url: str,
    job: dict[str, Any],
    state: DaemonState,
    api_key: str,
    device_id: str,
) -> None:
    """발주취소 잡 1개 처리 — 계정 로그인 → 취소 페이지 진입 → 취소 실행 → 회신."""
    request_id = job.get("requestId", "")
    site = job.get("site", "")
    url = job.get("url", "")
    account_id = job.get("sourcingAccountId", "") or ""
    sourcing_order_number = job.get("sourcingOrderNumber", "") or ""

    _ALIAS = {"ABCMART": "ABCmart", "GRANDSTAGE": "GrandStage"}
    handler_key = _ALIAS.get(site.upper(), site)
    handler = SITE_HANDLERS.get(handler_key)

    if not handler or not handler.cancel_js:
        await post_cancel_result(
            client,
            backend_url,
            request_id,
            {"success": False, "error": f"daemon cancel 미지원: {site}"},
            api_key,
        )
        state.record_failure()
        return

    if not url and handler.cancel_url_template:
        url = handler.cancel_url_template.replace("{ord_no}", sourcing_order_number)
    if not url:
        await post_cancel_result(
            client,
            backend_url,
            request_id,
            {
                "success": False,
                "error": f"cancel url 미정 (ord={sourcing_order_number})",
            },
            api_key,
        )
        state.record_failure()
        return

    logger.info(
        "[취소] 처리 시작 site=%s req=%s acc=%s ord=%s",
        site,
        request_id,
        account_id or "-",
        sourcing_order_number,
    )
    t0 = time.time()

    if handler.cancel_requires_login:
        login_ok = await ensure_logged_in_as_account(
            page, client, backend_url, device_id, api_key, handler, account_id
        )
        if not login_ok:
            await post_cancel_result(
                client,
                backend_url,
                request_id,
                {
                    "success": False,
                    "error": f"{site} 계정 로그인 실패 (acc={account_id})",
                },
                api_key,
            )
            state.record_failure()
            return

    try:
        data = await asyncio.wait_for(extract_cancel(page, url, handler), timeout=90.0)
    except asyncio.TimeoutError:
        data = {"success": False, "error": "daemon 취소 추출 타임아웃"}
    except Exception as exc:
        data = {"success": False, "error": f"daemon 취소 예외: {str(exc)[:120]}"}

    await post_cancel_result(client, backend_url, request_id, data, api_key)
    dt = time.time() - t0
    if data.get("cancelled"):
        logger.info(
            "[취소] 완료 req=%s ord=%s (%.1fs)",
            request_id,
            sourcing_order_number,
            dt,
        )
        state.record_success()
    elif data.get("alreadyShipped"):
        logger.info(
            "[취소] 이미발송 req=%s ord=%s (%.1fs)",
            request_id,
            sourcing_order_number,
            dt,
        )
        state.record_success()
    else:
        logger.info(
            "[취소] 실패 req=%s err=%s (%.1fs)", request_id, data.get("error"), dt
        )
        state.record_failure()


async def process_job(
    page: Page,
    client: httpx.AsyncClient,
    backend_url: str,
    job: dict[str, Any],
    state: DaemonState,
    api_key: str,
    device_id: str = "",
) -> str | None:
    """잡 1개 처리. 반환값:
    - None: 정상 처리(성공/실패와 무관, 회신 완료)
    - "login_required": 로그인 만료 감지 → caller 가 재로그인 트리거
    """
    request_id = job.get("requestId", "")
    site = job.get("site", "")
    jtype = job.get("type", "")
    url = job.get("url", "")
    product_id = job.get("productId", "")

    # 송장(tracking) 잡 — 별도 흐름 (계정 로그인 + 배송조회 스크랩)
    if jtype == "tracking":
        await process_tracking_job(
            page, client, backend_url, job, state, api_key, device_id
        )
        return None

    # 발주취소(cancel_order) — 계정 로그인 + 주문상세 진입 + 취소 실행
    if jtype == "cancel_order":
        await process_cancel_order_job(
            page, client, backend_url, job, state, api_key, device_id
        )
        return None

    handler = SITE_HANDLERS.get(site)
    if not handler or jtype != "detail":
        logger.warning("범위 밖 잡 (site=%s type=%s) — 실패 회신", site, jtype)
        await post_result(
            client,
            backend_url,
            request_id,
            {
                "success": False,
                "error": f"daemon scope detail only (got {site}/{jtype})",
            },
            api_key,
        )
        state.record_failure()
        return None

    logger.info("처리 시작 site=%s req=%s pid=%s", site, request_id, product_id)
    t0 = time.time()
    try:
        data = await asyncio.wait_for(
            extract_pdp(page, url, product_id, handler), timeout=50.0
        )
    except asyncio.TimeoutError:
        logger.warning("PDP 추출 타임아웃 req=%s pid=%s", request_id, product_id)
        await post_result(
            client,
            backend_url,
            request_id,
            {"success": False, "error": "daemon PDP 추출 타임아웃"},
            api_key,
        )
        state.record_failure()
        return None
    except Exception as exc:
        logger.exception("PDP 추출 예외 req=%s pid=%s: %s", request_id, product_id, exc)
        await post_result(
            client,
            backend_url,
            request_id,
            {"success": False, "error": f"daemon 예외: {exc}"},
            api_key,
        )
        state.record_failure()
        return None

    # 097bf07b race fix — login_required 즉시 재로그인 + 재추출.
    # startup ensure_logged_in 누락/실패해도 잡별로 자동 회복. 송장의 ensure_logged_in_as_account 패턴과 동일.
    if (
        isinstance(data, dict)
        and data.get("login_required")
        and handler.requires_login
        and device_id
    ):
        logger.warning(
            "[%s] login_required 감지 → 즉시 재로그인 시도 (req=%s)", site, request_id
        )
        try:
            ok_login = await ensure_logged_in_for_site(
                page, client, backend_url, device_id, api_key, handler
            )
            if ok_login:
                logger.info("[%s] 재로그인 성공 → 재추출 (req=%s)", site, request_id)
                try:
                    data = await asyncio.wait_for(
                        extract_pdp(page, url, product_id, handler), timeout=50.0
                    )
                except Exception as exc:
                    logger.warning("재추출 예외 req=%s: %s", request_id, exc)
            else:
                logger.warning("[%s] 재로그인 실패 — 잡 실패 회신 (req=%s)", site, request_id)
        except Exception as exc:
            logger.warning("[%s] 재로그인 예외 req=%s: %s", site, request_id, exc)

    ok = await post_result(client, backend_url, request_id, data, api_key)
    dt = time.time() - t0
    if ok and data.get("success"):
        bp = data.get("best_benefit_price") or 0
        nopt = len(data.get("options") or [])
        logger.info(
            "완료 req=%s pid=%s 혜택가=%s 옵션=%d (%.1fs)",
            request_id,
            product_id,
            f"{bp:,}",
            nopt,
            dt,
        )
        state.record_success()
        if data.get("login_required"):
            state.record_login_required()
            return "login_required"
        state.reset_login_required()
        return None
    # 회신 OK 였으나 success=False
    state.record_failure()
    if isinstance(data, dict) and data.get("login_required"):
        state.record_login_required()
        return "login_required"
    return None


async def _launch_browser(pw, headless: bool):
    """시스템 브라우저(크롬→Edge) 우선 → 번들 chromium 폴백.

    chromium(~300MB) 번들 대신 PC 에 이미 깔린 크롬/Edge 를 헤드리스로 빌려쓴다.
    → exe ~20MB 로 경량화(다운로드 즉시). Edge 는 Windows 기본 설치라 거의 항상 존재.
    SSG/ABCmart/LOTTEON PDP 추출 동작은 시스템 크롬·Edge 헤드리스로 검증됨(2026-05-23).
    """
    _args = ["--no-sandbox", "--disable-blink-features=AutomationControlled"]
    last_err: Exception | None = None
    for ch in ("chrome", "msedge"):
        try:
            b = await pw.chromium.launch(channel=ch, headless=headless, args=_args)
            logger.info("브라우저 실행: 시스템 %s (headless=%s)", ch, headless)
            return b
        except Exception as exc:
            last_err = exc
            logger.warning("브라우저 channel=%s 실행 실패: %s", ch, str(exc)[:100])
    # 폴백: 번들/기본 chromium (번들 제거 시 미존재 → 에러)
    try:
        b = await pw.chromium.launch(headless=headless, args=_args)
        logger.info("브라우저 실행: 번들 chromium (headless=%s)", headless)
        return b
    except Exception as exc:
        raise RuntimeError(
            f"브라우저 실행 실패 (chrome/msedge/chromium 모두): {last_err} / {exc}"
        )


async def run_daemon(args: argparse.Namespace) -> int:
    state = DaemonState(max_consecutive_fail=args.max_consecutive_fail)
    backend_url = args.backend_url.rstrip("/")
    profile_dir = Path(args.profile_dir).expanduser().resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)
    api_key_path = profile_dir / "api_key.txt"

    # 담당 사이트는 백엔드(UI '연결된 데몬' 지정)가 결정한다 — 데몬은 스스로 사이트를
    # 선언하지 않는다(체크 해제 시 실제로 작업에서 빠지도록). --sites 는 테스트/강제
    # 오버라이드용(보통 미지정). 미지정이면 백엔드 배정만 따른다.
    # api_key 부트스트랩 후 fetch_autotune_concurrency 로 active_sites 를 채운다.
    _cli_sites = [
        s.strip()
        for s in (getattr(args, "sites", "") or "").split(",")
        if s.strip() in SITE_HANDLERS
    ]
    active_sites: list[str] = list(_cli_sites)  # 시작값 — 백엔드 배정 조회 후 갱신
    allowed_sites_header = ",".join(active_sites)
    login_sites: list[str] = []  # 백엔드 배정 조회 후 active_sites 기준으로 채움
    _logged_in: set[str] = (
        set()
    )  # 로그인 완료 사이트 — 런타임 추가 사이트 재로그인 판단

    logger.info(
        "데몬 시작 device_id=%s backend=%s profile=%s sites=%s",
        args.device_id,
        backend_url,
        profile_dir,
        allowed_sites_header,
    )

    async with httpx.AsyncClient() as http_client:
        # 자동 업데이트 체크 — 신버전 감지 시 즉시 종료(supervisor가 신버전 다운로드 트리거)
        if await _check_and_self_update(http_client, backend_url):
            return 10  # exit=10 → run.ps1/Run 키 재시작이 신버전 다운로드 트리거

        # API key 부트스트랩 (주입 키 > 캐시 > 발급)
        try:
            api_key = await bootstrap_api_key(
                http_client,
                backend_url,
                args.device_id,
                api_key_path,
                injected_key=getattr(args, "api_key", "") or "",
            )
        except Exception as exc:
            logger.error("API key 부트스트랩 실패: %s", exc)
            return 2

        def _compute_login_sites(sites: list[str]) -> list[str]:
            # startup 로그인은 detail 지원 사이트만 — 송장전용(MUSINSA)은 잡 처리 시
            # 계정별 로그인(startup 로그인하면 기본계정 1개만 돼 무의미 + captcha 위험).
            return [
                s
                for s in sites
                if SITE_HANDLERS[s].requires_login and SITE_HANDLERS[s].detail_supported
            ]

        # 담당 사이트 초기 조회 — UI 지정값(authoritative). CLI --sites 지정 시 그 값 우선.
        _conc0, _assigned0 = await fetch_autotune_concurrency(
            http_client, backend_url, args.device_id, api_key
        )
        if not _cli_sites and _assigned0 is not None:
            active_sites = list(_assigned0)
            allowed_sites_header = ",".join(active_sites)
        login_sites = _compute_login_sites(active_sites)
        logger.info("초기 담당 사이트: %s (login=%s)", active_sites, login_sites)

        # launch + new_context(storage_state) — persistent_context 는 headless=True 무시하고
        # 창 띄우는 알려진 버그가 있어 쿠키만 storage_state.json 영속 저장.
        storage_state_path = profile_dir / "storage_state.json"
        async with async_playwright() as pw:
            browser = await _launch_browser(pw, args.headless)
            context_kwargs: dict[str, Any] = {
                "viewport": {"width": 1280, "height": 900},
                "user_agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
            }
            if storage_state_path.exists():
                context_kwargs["storage_state"] = str(storage_state_path)
            context: BrowserContext = await browser.new_context(**context_kwargs)

            # 무거운 서브리소스 차단 — 가격은 JSON API + DOM 텍스트, 이미지는 <img> src
            # 속성에서만 읽으므로 렌더링된 이미지/동영상/폰트 불필요. 콜드 캐시 헤드리스가
            # 상품마다 전부 재다운로드하던 비용 제거 → PDP 로드 대폭 단축. CSS 는 일부 JS
            # 레이아웃 측정에 쓰일 수 있어 차단 제외(안전 우선).
            async def _block_heavy(route):
                if route.request.resource_type in ("image", "media", "font"):
                    await route.abort()
                else:
                    await route.continue_()

            await context.route("**/*", _block_heavy)
            page = await context.new_page()

            # SSG 임직원 alert 자동 dismiss — 띄워두면 페이지 멈춰 다음 잡 차단.
            # 담당 사이트가 런타임에 바뀌므로(SSG 가 나중에 배정될 수 있음) 핸들러는
            # 항상 등록한다. accept 실패 시 dismiss 폴백이라 다른 사이트에도 안전.
            async def _on_dialog(dialog):
                try:
                    logger.info(
                        "dialog auto-accept type=%s msg=%s",
                        dialog.type,
                        (dialog.message or "")[:80],
                    )
                    await dialog.accept()
                except Exception:
                    try:
                        await dialog.dismiss()
                    except Exception:
                        pass

            page.on("dialog", lambda d: asyncio.create_task(_on_dialog(d)))

            async def _save_storage_state() -> None:
                try:
                    await context.storage_state(path=str(storage_state_path))
                except Exception as exc:
                    logger.debug("storage_state 저장 실패(무시): %s", exc)

            # 시작 시 로그인 — requires_login 사이트 각각.
            # 한 사이트 로그인 실패해도 전체 종료하지 않는다(예: ABCmart CAPTCHA).
            # 실패 사이트만 active 목록에서 제외하고 나머지로 계속 — LOTTEON/SSG 가
            # ABCmart 하나 때문에 같이 죽던 무한 재시작 루프 방지.
            _failed_login: list[str] = []
            for _site in login_sites:
                if not await ensure_logged_in_for_site(
                    page,
                    http_client,
                    backend_url,
                    args.device_id,
                    api_key,
                    SITE_HANDLERS[_site],
                ):
                    logger.warning(
                        "%s 초기 로그인 실패 — 이 사이트만 제외하고 나머지로 계속",
                        _site,
                    )
                    _failed_login.append(_site)
            if _failed_login:
                active_sites = [s for s in active_sites if s not in _failed_login]
                login_sites = [s for s in login_sites if s not in _failed_login]
                allowed_sites_header = ",".join(active_sites)
                if not active_sites:
                    logger.error(
                        "모든 사이트 로그인 실패 — 종료 (supervisor 재기동 유도)"
                    )
                    await context.close()
                    await browser.close()
                    return 3
                logger.info(
                    "로그인 실패 사이트 제외 후 계속: active=%s (실패=%s)",
                    allowed_sites_header,
                    ",".join(_failed_login),
                )
            if login_sites:
                await _save_storage_state()
            _logged_in.update(
                login_sites
            )  # 로그인 성공 사이트 기록(실패는 위에서 제외됨)

            async def _ensure_login_for_new_sites(sites: list[str]) -> None:
                # 런타임에 새로 배정된 requires_login 사이트 로그인(이미 한 건 스킵).
                for _s in _compute_login_sites(sites):
                    if _s in _logged_in:
                        continue
                    try:
                        ok = await ensure_logged_in_for_site(
                            page,
                            http_client,
                            backend_url,
                            args.device_id,
                            api_key,
                            SITE_HANDLERS[_s],
                        )
                        if ok:
                            _logged_in.add(_s)
                            await _save_storage_state()
                        else:
                            logger.warning(
                                "%s 런타임 로그인 실패 — 다음 주기 재시도", _s
                            )
                    except Exception as _e:
                        logger.warning("%s 런타임 로그인 예외: %s", _s, str(_e)[:80])

            # ── 사이트별 병렬 워커 풀 (PC 자원 활용) ───────────────────────
            # 백엔드 _site_autotune_loop 는 사이트별 병렬로 잡을 만들지만, 데몬이 페이지
            # 1개로 직렬 처리하면 get_next_job 의 site ASC 정렬 때문에 알파벳 뒤 사이트
            # (SSG 등)가 굶는다. 워룸 동시실행 설정(인풋박스)만큼 사이트별 페이지를 띄워
            # 각 사이트를 독립 병렬 처리한다. 60초마다 재조회해 인풋박스 변경을 탄력 반영.
            _MAX_TOTAL_PAGES = int(os.environ.get("DAEMON_MAX_PAGES", "16"))

            async def _site_worker(site: str, wid: str, wpage: Page) -> None:
                _idle_at = 0.0
                while not state.should_die():
                    try:
                        # 등록(allowed_sites)=전체 활성 사이트 → pick_daemon_owner 가
                        # 모든 사이트의 owner 후보로 이 데몬 인식. 잡 dequeue 스코프
                        # (poll_site)=이 워커 사이트 하나. 분리 안 하면 등록값이 단일
                        # 사이트로 덮어써져 owner 매칭 깨짐(LOTTEON 60s 타임아웃 원인).
                        job = await fetch_job(
                            http_client,
                            backend_url,
                            args.device_id,
                            api_key,
                            allowed_sites=allowed_sites_header,
                            poll_site=site,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.debug("[%s] fetch_job 예외: %s", wid, str(exc)[:80])
                        await asyncio.sleep(args.poll_interval)
                        continue
                    if not job:
                        _now = time.time()
                        if _now - _idle_at > 60:
                            logger.info(
                                "[%s] 대기 중 (processed=%d ok=%d fail=%d)",
                                wid,
                                state.processed,
                                state.succeeded,
                                state.failed,
                            )
                            _idle_at = _now
                        await asyncio.sleep(args.poll_interval)
                        continue
                    try:
                        await process_job(
                            wpage,
                            http_client,
                            backend_url,
                            job,
                            state,
                            api_key,
                            args.device_id,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.warning("[%s] process_job 예외: %s", wid, str(exc)[:120])

            async def _relogin_monitor(rl_page: Page) -> None:
                while not state.should_die():
                    await asyncio.sleep(10)
                    if login_sites and state.consecutive_login_required >= 3:
                        logger.warning("login_required 3회 연속 — 재로그인 시도")
                        for _site in list(login_sites):
                            try:
                                await ensure_logged_in_for_site(
                                    rl_page,
                                    http_client,
                                    backend_url,
                                    args.device_id,
                                    api_key,
                                    SITE_HANDLERS[_site],
                                )
                            except asyncio.CancelledError:
                                raise
                            except Exception as exc:
                                logger.warning(
                                    "%s 재로그인 예외: %s", _site, str(exc)[:80]
                                )
                        state.reset_login_required()
                        await _save_storage_state()

            def _eff_conc(conc: dict) -> dict:
                # 사이트별 동시실행 — 총 페이지 수를 _MAX_TOTAL_PAGES 로 캡(메모리 보호).
                # conc 키 = 백엔드가 이 데몬 담당 사이트로 필터해 내려준 사이트들.
                # (active_sites 가 아닌 conc 키를 순회 — 미배정 사이트는 conc 에 없어 자동 제외)
                eff: dict = {}
                budget = _MAX_TOTAL_PAGES
                for s in conc:
                    if s not in SITE_HANDLERS:
                        continue
                    n = min(max(1, int(conc.get(s, 1))), budget)
                    if n <= 0:
                        break
                    eff[s] = n
                    budget -= n
                return eff

            _workers: list[asyncio.Task] = []
            _pages: list[Page] = []

            async def _spawn(conc_eff: dict) -> None:
                for site, n in conc_eff.items():
                    for i in range(n):
                        pg = await context.new_page()
                        _pages.append(pg)
                        _workers.append(
                            asyncio.create_task(
                                _site_worker(site, f"{site}#{i + 1}", pg)
                            )
                        )

            async def _despawn() -> None:
                for t in _workers:
                    t.cancel()
                for t in _workers:
                    try:
                        await t
                    except BaseException:
                        pass
                for pg in _pages:
                    try:
                        await pg.close()
                    except Exception:
                        pass
                _workers.clear()
                _pages.clear()

            _mon_task = asyncio.create_task(_relogin_monitor(page))

            async def _sync_assignment() -> dict:
                # 백엔드에서 동시실행 + 담당 사이트 조회 → active_sites/login 반영 후
                # _eff_conc(담당 사이트 워커 맵) 반환. _cli_sites 지정 시 배정 무시(강제).
                nonlocal active_sites, allowed_sites_header
                conc_raw, assigned = await fetch_autotune_concurrency(
                    http_client, backend_url, args.device_id, api_key
                )
                if not _cli_sites and assigned is not None:
                    if set(assigned) != set(active_sites):
                        active_sites = list(assigned)
                        allowed_sites_header = ",".join(active_sites)
                        await _ensure_login_for_new_sites(active_sites)
                return _eff_conc(conc_raw)

            _cur_conc = await _sync_assignment()
            await _spawn(_cur_conc)
            logger.info(
                "사이트별 병렬 워커 스폰: %s (총 %d 페이지)", _cur_conc, len(_workers)
            )
            try:
                while not state.should_die():
                    await asyncio.sleep(60)
                    _new_conc = await _sync_assignment()
                    if _new_conc != _cur_conc:
                        logger.info(
                            "담당/동시실행 변경 %s → %s — 워커 재스폰",
                            _cur_conc,
                            _new_conc,
                        )
                        await _despawn()
                        _cur_conc = _new_conc
                        await _spawn(_cur_conc)
            finally:
                _mon_task.cancel()
                try:
                    await _mon_task
                except BaseException:
                    pass
                await _despawn()
            logger.error(
                "연속 실패 %d 건 초과 — 종료(supervisor 재기동 유도)",
                state.consecutive_fail,
            )
            await context.close()
            await browser.close()
            return 1


def _setup_logging() -> None:
    """파일 로깅 (RotatingFileHandler) — --noconsole 빌드에서 stderr 사라져도 로그 영구 보존."""
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    formatter = logging.Formatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # 기존 핸들러 제거 — basicConfig 중복 방지
    for h in list(root.handlers):
        root.removeHandler(h)
    try:
        log_path = _log_file_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            str(log_path),
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        fh.setFormatter(formatter)
        root.addHandler(fh)
    except Exception:
        pass
    # stderr 핸들러도 추가 — --console 빌드 / .py 실행 시 화면 출력
    try:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(formatter)
        root.addHandler(sh)
    except Exception:
        pass


# ====================================================================
# 트레이 아이콘 — supervisor 모드에서 daemon thread 로 실행.
# 콘솔창 대체용. 우클릭 메뉴: 로그 열기 / 폴더 열기 / 버전 / 종료.
# ====================================================================


def _open_log_file() -> None:
    try:
        log_path = _log_file_path()
        if not log_path.exists():
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.touch()
        os.startfile(str(log_path))  # type: ignore[attr-defined]
    except Exception as exc:
        logger_print(f"로그 열기 실패: {exc}")


def _open_install_dir() -> None:
    try:
        d = _install_dir()
        d.mkdir(parents=True, exist_ok=True)
        os.startfile(str(d))  # type: ignore[attr-defined]
    except Exception as exc:
        logger_print(f"폴더 열기 실패: {exc}")


def _make_tray_icon_image():
    """64x64 단색 PNG 메모리 이미지 생성 (외부 파일 의존성 X)."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # 진한 주황 원 + 흰색 S
    d.ellipse((2, 2, 62, 62), fill=(234, 88, 12, 255))
    try:
        d.text((20, 14), "S", fill=(255, 255, 255, 255))
    except Exception:
        pass
    return img


_tray_icon_ref: Any = None


def _start_tray_icon() -> None:
    """tray icon daemon thread 시작. supervisor 가 메인 스레드 점유."""
    global _tray_icon_ref
    try:
        import pystray  # type: ignore
    except Exception as exc:
        logger_print(f"pystray import 실패 — 트레이 스킵: {exc}")
        return

    image = _make_tray_icon_image()

    def _on_quit(icon: Any, _item: Any) -> None:
        try:
            icon.stop()
        except Exception:
            pass
        # supervisor + 현재 worker 종료. os._exit(0) 으로 강제.
        os._exit(0)

    def _on_log(_icon: Any, _item: Any) -> None:
        _open_log_file()

    def _on_folder(_icon: Any, _item: Any) -> None:
        _open_install_dir()

    def _on_version(_icon: Any, _item: Any) -> None:
        try:
            ver_path = _install_dir() / "version.txt"
            ver_path.parent.mkdir(parents=True, exist_ok=True)
            ver_path.write_text(
                f"autotune-daemon v{DAEMON_VERSION}\n", encoding="utf-8"
            )
            os.startfile(str(ver_path))  # type: ignore[attr-defined]
        except Exception:
            pass

    menu = pystray.Menu(
        pystray.MenuItem("로그 열기", _on_log),
        pystray.MenuItem("설치 폴더 열기", _on_folder),
        pystray.MenuItem(f"버전 {DAEMON_VERSION}", _on_version),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("종료", _on_quit),
    )
    icon = pystray.Icon(
        "autotune-daemon",
        image,
        f"오토튠 데몬 v{DAEMON_VERSION}",
        menu,
    )
    _tray_icon_ref = icon

    def _run() -> None:
        try:
            icon.run()
        except Exception as exc:
            logger_print(f"tray run 예외: {exc}")

    t = threading.Thread(target=_run, name="tray-icon", daemon=True)
    t.start()


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="오토튠 헤드리스 데몬 (LOTTEON/ABCmart/SSG)"
    )
    p.add_argument(
        "--sites",
        default=os.environ.get("DAEMON_SITES", ",".join(SITE_HANDLERS)),
        help=(
            "처리 사이트 콤마구분 (예: LOTTEON,ABCmart,SSG,MUSINSA). "
            "X-Allowed-Sites 헤더로 백엔드에 전달. 기본값은 전체 핸들러 "
            "(MUSINSA 는 송장 전용)."
        ),
    )
    # backend URL 우선순위:
    # 1. URL/파일명/argv 의 backend= (포크 사용자가 본인 backend 가리킬 때)
    # 2. DAEMON_BACKEND_URL 환경변수
    # 3. 본 메인 default
    _backend_default = (
        _extract_backend_from_argv_or_exename()
        or os.environ.get("DAEMON_BACKEND_URL")
        or "https://api.samba-wave.co.kr"
    )
    p.add_argument(
        "--backend-url",
        default=_backend_default,
        help=(
            "백엔드 base URL. URL/파일명/--backend= 자동 추출. "
            "포크 사용자는 본인 backend 지정 가능."
        ),
    )
    # device_id 우선순위:
    # 1. URL/파일명/argv 의 did= (오토튠 페이지가 박은 값) — 최우선
    # 2. DAEMON_DEVICE_ID 환경변수
    # 3. samba-daemon-<hostname> 폴백
    _did_default = (
        _extract_did_from_argv_or_exename()
        or os.environ.get("DAEMON_DEVICE_ID")
        or _default_device_id()
    )
    p.add_argument(
        "--device-id",
        default=_did_default,
        help=(
            "이 데몬의 device_id. URL/파일명/--did= 자동 추출. "
            "백엔드 owner_device_ids 가 samba-daemon-* prefix 자동 허용."
        ),
    )
    # api_key 우선순위:
    # 1. URL/파일명/argv 의 apikey= (설치 트리거가 박은 값)
    # 2. DAEMON_API_KEY 환경변수
    # 3. (미주입) 캐시/글로벌 발급 폴백
    _apikey_default = (
        _extract_install_token()  # 파일명 _it-<token> (다운로드 프록시가 박음)
        or _extract_kv_from_argv_or_exename("apikey")  # 레거시 apikey=
        or os.environ.get("DAEMON_API_KEY")
        or ""
    )
    p.add_argument(
        "--api-key",
        default=_apikey_default,
        help=(
            "cannonfort 테넌트 키 직접 주입. /samba/extension-link 에서 발급. "
            "주입 시 글로벌 키 발급 생략. login-credential 테넌트 격리 통과에 필수."
        ),
    )
    p.add_argument(
        "--profile-dir",
        default=os.environ.get(
            "DAEMON_PROFILE_DIR",
            str(Path.home() / ".autotune_daemon" / "chromium_profile"),
        ),
        help="Chromium 영속 프로필 디렉토리",
    )
    p.add_argument(
        "--poll-interval",
        type=float,
        default=float(os.environ.get("DAEMON_POLL_INTERVAL", "1.0")),
    )
    p.add_argument(
        "--max-consecutive-fail",
        type=int,
        default=int(os.environ.get("DAEMON_MAX_CONSECUTIVE_FAIL", "10")),
    )
    # 기본 headless=True — 사용자 PC 에 Chromium 창 안 뜸 (zero-visual).
    # WAF 차단 발생 시 --no-headless 로 수동 전환 가능.
    p.add_argument(
        "--no-headless",
        dest="headless",
        action="store_false",
        default=True,
        help="headed 모드로 전환 (LOTTEON WAF 차단 시 디버깅용).",
    )
    # parse_known_args: self-install 재실행 시 argv 에 붙는 식별자(_it-<token>/_be-<hex>/did=)는
    # argparse 가 모르는 토큰이라 parse_args 면 'unrecognized arguments' 로 크래시(rc=2)한다.
    # 이 식별자들은 _extract_install_token/_extract_backend_* 가 sys.argv 를 정규식으로 직접
    # 읽어 처리하므로, argparse 는 무시(tolerate)하면 된다.
    return p.parse_known_args()[0]


async def _check_and_self_update(client: httpx.AsyncClient, backend_url: str) -> bool:
    """백엔드 버전 체크 → 신버전이면 True 반환(caller 가 종료 → 재시작 시 신버전 다운로드).

    실패 시 False (현 버전 그대로 진행). 네트워크 일시 장애로 자가 종료되는 사고 방지.
    """
    try:
        r = await client.get(
            f"{backend_url}/api/v1/samba/proxy/autotune-daemon/latest-version",
            timeout=10.0,
        )
        if r.status_code != 200:
            return False
        data = r.json() or {}
        latest = (data.get("version") or "").strip()
        if not latest:
            return False

        def _vt(s: str) -> tuple[int, ...]:
            try:
                return tuple(int(x) for x in s.split(".") if x.isdigit())
            except Exception:
                return ()

        if _vt(latest) <= _vt(DAEMON_VERSION):
            return False  # 로컬 = latest 또는 더 신버전
        logger.info(
            "신버전 감지: 현재=%s latest=%s — 자기 종료 → 다음 시작 시 갱신",
            DAEMON_VERSION,
            latest,
        )
        return True
    except Exception as exc:
        logger.debug("버전 체크 실패(무시): %s", exc)
        return False


def _supervisor_loop() -> int:
    """parent supervisor — 자기 자신을 --worker 모드로 spawn + 죽으면 backoff 재시작.

    NSSM/Service 대체. admin 권한 불필요. UAC 트리거 X.
    parent (supervisor) = 항상 살아있는 가벼운 process.
    child (worker) = 실제 Playwright + polling.

    Backoff: 정상 가동 10초 안에 죽으면 즉시 죽은 것으로 간주 →
    재시작 간격 증가 (5s → 30s → 60s 상한). 정상 가동 ≥10s 시 backoff 리셋.
    """
    logger_print(f"supervisor 시작 pid={os.getpid()}")
    # 트레이 아이콘 시작 — Windows + frozen 일 때만. 콘솔창 없이 상태 표시 + 종료 메뉴 제공.
    if os.name == "nt":
        try:
            _start_tray_icon()
        except Exception as exc:
            logger_print(f"트레이 시작 실패(무시): {exc}")
    restart_count = 0
    backoff = 5
    BACKOFF_MAX = 60
    HEALTHY_SECS = 10
    while True:
        # frozen .exe 모드: sys.executable = daemon.exe, script path 불필요
        # .py 모드: sys.executable = python.exe, sys.argv[0] (script path) 필요
        if _is_frozen():
            cmd = [sys.executable, *sys.argv[1:], "--worker"]
        else:
            cmd = [sys.executable, sys.argv[0], *sys.argv[1:], "--worker"]
        try:
            # CREATE_NEW_PROCESS_GROUP + CREATE_NO_WINDOW — worker 자식이 새 콘솔창 안 띄움
            creationflags = (
                (CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW) if os.name == "nt" else 0
            )
            # worker stdout/stderr 도 log 파일로 redirect — 파편화된 출력 방지
            try:
                log_fp = open(str(_log_file_path()), "a", encoding="utf-8")
            except Exception:
                log_fp = subprocess.DEVNULL  # type: ignore[assignment]
            child = subprocess.Popen(
                cmd,
                creationflags=creationflags,
                stdout=log_fp,
                stderr=log_fp,
                stdin=subprocess.DEVNULL,
            )
        except Exception as exc:
            logger_print(
                f"supervisor: worker spawn 실패: {exc} — {backoff}초 후 재시도"
            )
            time.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX)
            continue
        start_ts = time.time()
        logger_print(
            f"supervisor: worker spawn pid={child.pid} "
            f"(총 {restart_count + 1}회, backoff={backoff}s)"
        )
        try:
            rc = child.wait()
        except KeyboardInterrupt:
            logger_print("supervisor: KeyboardInterrupt — worker 종료 후 본인도 종료")
            try:
                child.terminate()
            except Exception:
                pass
            return 0
        duration = time.time() - start_ts
        logger_print(f"supervisor: worker exit rc={rc} duration={duration:.1f}s")
        if rc == 0:
            logger_print("supervisor: worker 정상 종료 — supervisor 도 종료")
            return 0
        if rc == 10:
            # 신버전 감지 → 자동 업데이트(새 exe 다운 + swap 배치). 성공 시 supervisor 종료
            # (배치가 데몬 종료 대기 후 교체·재시작). 실패 시 구버전으로 계속 재시작.
            logger_print("supervisor: 신버전 감지(rc=10) — 자동 업데이트 시도")
            # 캐시된 api_key 주입 → backend 경유 self-update → 자동 키 갱신
            _cached_key = ""
            try:
                _kp = _install_dir() / "api_key.txt"
                if _kp.exists():
                    _cached_key = _kp.read_text(encoding="utf-8").strip()
            except Exception:
                pass
            if _perform_self_update(api_key=_cached_key):
                logger_print("supervisor: 자동 업데이트 위임 완료 — 종료")
                return 0
            logger_print("supervisor: 자동 업데이트 실패 — 구버전으로 계속")
        if duration >= HEALTHY_SECS:
            backoff = 5  # 정상 가동했으니 backoff 리셋
        else:
            backoff = min(backoff * 2, BACKOFF_MAX)
        restart_count += 1
        logger_print(f"supervisor: {backoff}초 후 재시작 (총 {restart_count}회)")
        time.sleep(backoff)


async def run_seed_session(args: argparse.Namespace) -> int:
    """--seed-session: headed 브라우저를 띄워 사람이 직접 로그인 → storage_state 저장.

    ABCmart 처럼 헤드리스 자동로그인이 CAPTCHA 로 막히는 사이트를 1회 수동 로그인해
    세션 쿠키를 심어두면, 이후 헤드리스 데몬이 '세션 살아있음 — 자동로그인 스킵'으로
    재사용한다. 쿠키 만료 시 다시 한 번 실행하면 된다.
    """
    profile_dir = Path(args.profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)
    storage_state_path = profile_dir / "storage_state.json"
    raw_sites = (getattr(args, "sites", "") or "LOTTEON").strip()
    active_sites = [
        s.strip() for s in raw_sites.split(",") if s.strip() in SITE_HANDLERS
    ]
    login_sites = [s for s in active_sites if SITE_HANDLERS[s].requires_login]
    if not login_sites:
        logger.info("로그인 필요 사이트 없음 — 세션 시드 불필요")
        return 0
    logger.info("=== 세션 시드 모드 시작: %s ===", ",".join(login_sites))
    async with async_playwright() as pw:
        browser = await _launch_browser(pw, headless=False)
        context_kwargs: dict[str, Any] = {
            "viewport": {"width": 1280, "height": 900},
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        }
        if storage_state_path.exists():
            context_kwargs["storage_state"] = str(storage_state_path)
        context: BrowserContext = await browser.new_context(**context_kwargs)
        opened: list[tuple[str, Page]] = []
        for _site in login_sites:
            handler = SITE_HANDLERS[_site]
            p = await context.new_page()
            try:
                await p.goto(
                    handler.login_url or handler.home_url or "about:blank",
                    wait_until="domcontentloaded",
                    timeout=30_000,
                )
            except Exception as exc:
                logger.warning("%s 페이지 로드 실패(무시): %s", _site, exc)
            opened.append((_site, p))
            logger.info("%s 로그인 창 열림 — 브라우저에서 직접 로그인하세요", _site)

        logger.info("=== 각 창에서 로그인 완료까지 대기 (최대 5분, 30초마다 확인) ===")
        for _ in range(10):
            await asyncio.sleep(30)
            states: list[bool] = []
            for _site, p in opened:
                handler = SITE_HANDLERS[_site]
                if not handler.login_check_js:
                    states.append(False)
                    continue
                try:
                    r = await p.evaluate(handler.login_check_js)
                    states.append(r == "logged_in")
                except Exception:
                    states.append(False)
            if states and all(states):
                logger.info("모든 사이트 로그인 확인 — 조기 저장")
                break

        await context.storage_state(path=str(storage_state_path))
        logger.info("세션 저장 완료: %s", storage_state_path)
        await context.close()
        await browser.close()
    logger.info(
        "세션 시드 완료. 이제 일반(헤드리스) 모드로 실행하면 세션 재사용됩니다."
    )
    return 0


def main() -> int:
    # 1. frozen 모드 + install dir 아닐 때 → self-install + 재시작 후 종료
    if _is_frozen() and not _running_from_install_dir():
        logger_print(f"첫 실행 감지 — {_install_dir()} 로 자기 설치 후 재시작")
        _self_install_and_relaunch()
        # _self_install_and_relaunch 가 os._exit(0) 호출하므로 이 라인 도달 X
        return 0

    # 1.5. --seed-session: supervisor/worker 없이 headed 브라우저로 1회 수동 로그인
    #      → storage_state 저장. ABCmart CAPTCHA 회피용 세션 시드.
    if "--seed-session" in sys.argv:
        _setup_logging()
        sys.argv = [a for a in sys.argv if a not in ("--worker", "--seed-session")]
        args = _parse_args()
        try:
            return asyncio.run(run_seed_session(args))
        except KeyboardInterrupt:
            logger.info("KeyboardInterrupt — 세션 시드 중단")
            return 0

    # 2. --worker 인자 없으면 supervisor 모드 (자기를 --worker 로 spawn + watchdog)
    if "--worker" not in sys.argv:
        return _supervisor_loop()

    # 3. --worker 인자 있으면 실제 작업 (이 분기에서만 Playwright + polling)
    _setup_logging()
    # argparse 가 unknown arg 무시하도록 — --worker 만 제거 후 parse
    sys.argv = [a for a in sys.argv if a != "--worker"]
    args = _parse_args()
    logger.info(
        "worker v%s 시작 pid=%d (frozen=%s install_dir=%s)",
        DAEMON_VERSION,
        os.getpid(),
        _is_frozen(),
        _install_dir() if _is_frozen() else "(개발 모드)",
    )
    try:
        return asyncio.run(run_daemon(args))
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt — 종료")
        return 0


if __name__ == "__main__":
    sys.exit(main())
