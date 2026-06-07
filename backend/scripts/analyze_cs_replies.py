"""누적 CS 답변 분석 — 자동화 설계용 (읽기 전용).

분석 항목:
  1. 전체 답변완료 건수 + reply_status 분포
  2. inquiry_type x market 별 건수
  3. 답변 중복률(distinct/total) + 길이 분포 → 정형성 측정
  4. inquiry_type 별 답변 샘플 (자주 쓰는 답변 Top)
  5. 기존 8개 템플릿과 정확/근접 일치 비율
"""

import asyncio
from collections import Counter

import asyncpg

from backend.core.config import settings
from backend.domain.samba.cs_inquiry.service import CS_REPLY_TEMPLATES


def _norm(s: str) -> str:
    """공백/줄바꿈 정규화 후 비교용."""
    return " ".join((s or "").split()).strip()


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
        print("=" * 70)
        print("[1] reply_status 분포")
        rows = await conn.fetch(
            "SELECT reply_status, count(*) cnt FROM samba_cs_inquiry "
            "WHERE is_hidden = false GROUP BY reply_status ORDER BY cnt DESC"
        )
        for r in rows:
            print(f"  {r['reply_status']:>12}: {r['cnt']:,}")

        total_replied = await conn.fetchval(
            "SELECT count(*) FROM samba_cs_inquiry "
            "WHERE is_hidden = false AND reply_status='replied' "
            "AND reply IS NOT NULL AND length(trim(reply)) > 0"
        )
        print(f"\n  실제 답변본문 보유 건수: {total_replied:,}")

        print("=" * 70)
        print("[2] inquiry_type x market 별 답변완료 건수")
        rows = await conn.fetch(
            "SELECT inquiry_type, market, count(*) cnt FROM samba_cs_inquiry "
            "WHERE is_hidden=false AND reply_status='replied' AND reply IS NOT NULL "
            "GROUP BY inquiry_type, market ORDER BY cnt DESC LIMIT 40"
        )
        for r in rows:
            print(f"  {r['inquiry_type']:>18} | {r['market']:>14} : {r['cnt']:,}")

        print("\n  [inquiry_type 합계]")
        rows = await conn.fetch(
            "SELECT inquiry_type, count(*) cnt FROM samba_cs_inquiry "
            "WHERE is_hidden=false AND reply_status='replied' AND reply IS NOT NULL "
            "GROUP BY inquiry_type ORDER BY cnt DESC"
        )
        type_totals = {r["inquiry_type"]: r["cnt"] for r in rows}
        for t, c in type_totals.items():
            print(f"  {t:>18}: {c:,}")

        print("=" * 70)
        print("[3] 답변 정형성 (중복률 + 길이)")
        distinct_reply = await conn.fetchval(
            "SELECT count(DISTINCT trim(reply)) FROM samba_cs_inquiry "
            "WHERE is_hidden=false AND reply_status='replied' AND reply IS NOT NULL "
            "AND length(trim(reply))>0"
        )
        if total_replied:
            print(f"  고유 답변 수: {distinct_reply:,} / 전체 {total_replied:,}")
            print(
                f"  중복률(1 - distinct/total): "
                f"{100 * (1 - distinct_reply / total_replied):.1f}%  (높을수록 정형)"
            )
        lrows = await conn.fetch(
            "SELECT length(trim(reply)) AS len_chars FROM samba_cs_inquiry "
            "WHERE is_hidden=false AND reply_status='replied' AND reply IS NOT NULL "
            "AND length(trim(reply))>0"
        )
        lengths = sorted(r["len_chars"] for r in lrows)
        if lengths:
            n = len(lengths)
            print(
                f"  답변길이 최소/중앙/p90/최대: "
                f"{lengths[0]} / {lengths[n // 2]} / {lengths[int(n * 0.9)]} / {lengths[-1]} 자"
            )

        print("=" * 70)
        print("[4] 자주 쓰는 답변 Top 25 (정규화 후 빈도)")
        rows = await conn.fetch(
            "SELECT reply FROM samba_cs_inquiry "
            "WHERE is_hidden=false AND reply_status='replied' AND reply IS NOT NULL "
            "AND length(trim(reply))>0"
        )
        all_replies = [r["reply"] for r in rows]
        counter = Counter(_norm(x) for x in all_replies)
        covered_by_top = 0
        for i, (text, cnt) in enumerate(counter.most_common(25), 1):
            covered_by_top += cnt
            disp = text[:80] + ("…" if len(text) > 80 else "")
            print(f"  {i:>2}. ({cnt:,}회) {disp}")
        if total_replied:
            print(
                f"\n  Top25가 전체의 {100 * covered_by_top / total_replied:.1f}% 커버"
            )
            # Top N 누적 커버리지
            cum = 0
            cov_marks = {}
            for idx, (_, cnt) in enumerate(counter.most_common(), 1):
                cum += cnt
                if idx in (5, 10, 20, 50, 100):
                    cov_marks[idx] = 100 * cum / total_replied
            print("  누적 커버리지:", {k: f"{v:.0f}%" for k, v in cov_marks.items()})
            print(f"  서로 다른 답변 패턴 총 개수: {len(counter):,}")

        print("=" * 70)
        print("[5] 기존 8개 템플릿 일치율")
        tmpl_norms = {
            _norm(v["content"]): v["name"] for v in CS_REPLY_TEMPLATES.values()
        }
        exact = 0
        for text, cnt in counter.items():
            if text in tmpl_norms:
                exact += cnt
        if total_replied:
            print(
                f"  기존 템플릿과 정확일치 답변: {exact:,}건 "
                f"({100 * exact / total_replied:.1f}%)"
            )
        # 템플릿별 사용량
        print("  [템플릿별 정확일치 사용량]")
        for tnorm, tname in tmpl_norms.items():
            print(f"    {tname:>12}: {counter.get(tnorm, 0):,}")

        print("=" * 70)
        print("[6] inquiry_type 별 답변 샘플 (각 3건)")
        for t in type_totals:
            srows = await conn.fetch(
                "SELECT content, reply FROM samba_cs_inquiry "
                "WHERE is_hidden=false AND reply_status='replied' AND reply IS NOT NULL "
                "AND length(trim(reply))>0 AND inquiry_type=$1 "
                "ORDER BY replied_at DESC LIMIT 3",
                t,
            )
            print(f"\n  --- {t} ---")
            for s in srows:
                q = _norm(s["content"])[:60]
                a = _norm(s["reply"])[:90]
                print(f"    Q: {q}")
                print(f"    A: {a}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
