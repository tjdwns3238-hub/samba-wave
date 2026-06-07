"""의도 분류기 검증 — 프로덕션 누적 문의에 classify() 적용 (읽기 전용)."""

import asyncio
import importlib.util
import sys
from collections import Counter

import asyncpg

from backend.core.config import settings

# 분류기를 /tmp에서 독립 모듈로 로드 (prod 패키지 무수정)
_spec = importlib.util.spec_from_file_location("cs_classifier", "/tmp/classifier.py")
_mod = importlib.util.module_from_spec(_spec)
sys.modules["cs_classifier"] = _mod  # dataclass forward-ref 해석용 등록
_spec.loader.exec_module(_mod)
classify = _mod.classify


async def main() -> None:
    conn = await asyncpg.connect(
        host="172.18.0.2",
        port=5432,
        user=settings.write_db_user,
        password=settings.write_db_password,
        database=settings.write_db_name,
        ssl=False,
    )
    try:
        rows = await conn.fetch(
            "SELECT content, inquiry_type, market, reply FROM samba_cs_inquiry "
            "WHERE is_hidden=false AND reply_status='replied' AND reply IS NOT NULL "
            "AND length(trim(reply))>0"
        )
        intents = Counter()
        auto = 0
        auto_samples = []
        intent_samples: dict = {}
        for r in rows:
            c = classify(r["content"], r["inquiry_type"], r["market"])
            intents[c.intent] += 1
            if c.auto_send_eligible:
                auto += 1
                if len(auto_samples) < 8:
                    auto_samples.append((r["content"][:55], r["reply"][:40]))
            intent_samples.setdefault(c.intent, [])
            if len(intent_samples[c.intent]) < 2:
                intent_samples[c.intent].append(r["content"][:55])

        total = len(rows)
        print(f"전체 {total}건 분류 결과")
        print("=" * 60)
        for intent, cnt in intents.most_common():
            print(f"  {intent:>16}: {cnt:>4}  ({100 * cnt / total:.0f}%)")
        print("=" * 60)
        print(
            f"자동전송 후보(auto_send_eligible): {auto}건 ({100 * auto / total:.0f}%)"
        )
        print("\n[자동전송 후보 샘플 — 실제 공지/확인응답인지 육안검증]")
        for q, a in auto_samples:
            print(f"  Q: {q}")
            print(f"  A(실제): {a}")
        print("\n[의도별 분류 샘플]")
        for intent, samples in intent_samples.items():
            print(f"  --- {intent} ---")
            for s in samples:
                print(f"    {s}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
