"""오토튠 PC 분담 맵 청소 (samba_settings.autotune_pc_allowed_sites).

목적:
  1. 테스트 데몬 찌꺼기 제거 (poc-1/cantest/unknown/verifytest 등).
  2. 확장앱 UUID device 의 LOTTEON 등록 제거 — 롯데온은 데몬 전용으로 전환됨
     (확장앱이 발행한 LOTTEON 잡이 만료만 쌓이던 중복 루프 정리).

안전:
  - 실제 데몬(samba-daemon-<hostname/MAC>, 테스트 제외)의 배정은 건드리지 않는다.
  - DRY-RUN 기본. 실제 반영하려면 APPLY=1 환경변수 지정.
  - 변경 전/후 맵을 모두 출력.
"""

import asyncio
import json
import os
import re

from sqlalchemy import text

from backend.db.orm import get_read_session, get_write_session

KEY_LIKE = "%autotune_pc_allowed_sites"

# 제거 대상 테스트 데몬 (실제 운영 데몬 아님)
_JUNK_DAEMONS = {
    "samba-daemon-poc-1",
    "samba-daemon-cantest",
    "samba-daemon-unknown",
    "samba-daemon-verifytest",
}

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _clean(mapping: dict) -> tuple[dict, list[str]]:
    out: dict = {}
    notes: list[str] = []
    for dev, sites in mapping.items():
        if dev in _JUNK_DAEMONS:
            notes.append(f"제거(테스트데몬): {dev} {sites}")
            continue
        if _UUID_RE.match(dev):
            # 확장앱 device — LOTTEON 만 빼고 나머지 사이트는 유지
            kept = [s for s in sites if s != "LOTTEON"]
            if kept != list(sites):
                notes.append(f"LOTTEON 제거(확장앱): {dev} {sites} → {kept}")
            if kept:
                out[dev] = kept
            else:
                notes.append(f"제거(확장앱 LOTTEON 단독): {dev} {sites}")
            continue
        out[dev] = list(sites)
    return out, notes


async def main():
    apply = os.environ.get("APPLY") == "1"
    async with get_read_session() as s:
        rows = (
            await s.execute(
                text(
                    f"SELECT key, value FROM samba_settings WHERE key LIKE '{KEY_LIKE}'"
                )
            )
        ).fetchall()
    if not rows:
        print("등록값 없음")
        return

    for key, value in rows:
        data = value
        if isinstance(value, str):
            try:
                data = json.loads(value)
            except Exception:
                data = {}
        if not isinstance(data, dict):
            print(f"{key}: dict 아님, 스킵")
            continue

        print(f"\n=== key={key} ===")
        print("[변경 전]")
        for d, sites in data.items():
            print(f"  {d:<34} → {sites}")

        cleaned, notes = _clean(data)
        print("\n[변경 내역]")
        for n in notes:
            print(f"  - {n}")
        if not notes:
            print("  (변경 없음)")

        print("\n[변경 후]")
        for d, sites in cleaned.items():
            print(f"  {d:<34} → {sites}")

        if apply and cleaned != data:
            async with get_write_session() as ws:
                await ws.execute(
                    text("UPDATE samba_settings SET value = :v WHERE key = :k"),
                    {"v": json.dumps(cleaned), "k": key},
                )
                await ws.commit()
            print("\n>>> APPLIED (DB 반영 완료)")
        else:
            print("\n>>> DRY-RUN (APPLY=1 로 실행해야 반영). 변경 없거나 미적용.")


asyncio.run(main())
